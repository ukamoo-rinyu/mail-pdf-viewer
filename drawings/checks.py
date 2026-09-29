"""発注前チェックのルール群。

目次（1ページ目の図面リスト）と各ページの表題欄を照合し、「要確認」の項目を Finding として返す。
判定は「エラー」ではなく「要確認」。最終判断は人が行う。

コマンドラインから単体で実行できる:
    python -m drawings.checks <PDFパス> --layout layouts/toshiseibi_A1.json [--csv 出力.csv]
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import pymupdf

from .extract import IndexEntry, Reference, TitleBlock, lines_of, read_index, read_references, read_title_block
from .layouts import Box, Layout, load_layout

LEVEL_CHECK = "要確認"
LEVEL_INFO = "参考"

# 規則の一覧（rule_id → 利用者向けの短い名前）。画面やCSVの見出しに使う
RULES = {
    "INDEX_DUP_NO": "目次の番号重複",
    "INDEX_DUP_NAME": "目次の名称重複",
    "INDEX_BLANK": "目次の名称空欄",
    "NO_UNREADABLE": "図面番号が読めない",
    "NO_NOT_IN_INDEX": "目次にない番号",
    "NO_IS_KEKBAN": "欠番の番号を使用",
    "NAME_MISMATCH": "名称不一致",
    "DUP_PAGE_NO": "番号重複ページ",
    "SCALE_BLANK": "縮尺空欄",
    "DATE_BLANK": "製図年月空欄",
    "TOTAL_BLANK": "総枚数空欄",
    "TOTAL_MISMATCH": "総枚数不一致",
    "KOJIMEI_MISMATCH": "工事名不統一",
    "REF_MISSING": "参照先なし",
    "REF_KEKBAN": "参照先が欠番",
    "INDEX_NOT_IN_SET": "図面がない（参考）",
}


@dataclass
class Finding:
    """チェック1件分の結果。"""
    rule_id: str        # 例 "NAME_MISMATCH"
    level: str          # "要確認" または "参考"
    page: int | None    # 図面のページ番号（1始まり）。図面セット全体についての指摘なら None
    drawing_no: str     # 図面番号（わかれば）
    message: str        # 利用者向けの説明文
    rect: Box | None    # 図面上で強調表示する範囲（PDFポイント、左上原点）
    details: list[str] = field(default_factory=list)  # 件数だけ表示する指摘の内訳

    @property
    def rule_name(self) -> str:
        return RULES.get(self.rule_id, self.rule_id)


@dataclass
class Sheet:
    """図面1ページ分の読み取り結果と、そのページについての指摘。"""
    page: int
    title: TitleBlock
    references: list[Reference]
    index_name: str | None = None      # 目次上の名称（照合できたとき）
    findings: list[Finding] = field(default_factory=list)

    @property
    def drawing_no(self) -> str:
        return self.title.図面番号

    @property
    def name(self) -> str:
        return self.title.図面名称


@dataclass
class CheckResult:
    """図面セット全体のチェック結果。"""
    pdf_path: str
    layout: Layout
    index: list[IndexEntry]
    sheets: list[Sheet]
    findings: list[Finding]            # すべての指摘（ページ別の指摘も含む）

    @property
    def general_findings(self) -> list[Finding]:
        """特定の図面ページに属さない指摘（目次そのもの・図面セット全体）。"""
        return [f for f in self.findings if f.page is None or f.rule_id.startswith("INDEX_")]

    def count(self, level: str = LEVEL_CHECK) -> int:
        return sum(1 for f in self.findings if f.level == level)


# ---------------------------------------------------------------- 目次そのものの点検
def check_index(index: list[IndexEntry]) -> list[Finding]:
    """目次の番号重複・名称重複・名称空欄を調べる。"""
    out = []
    seen_no: dict[str, IndexEntry] = {}
    for e in index:
        if e.番号 in seen_no:
            out.append(Finding("INDEX_DUP_NO", LEVEL_CHECK, e.page, e.番号,
                               f"目次で番号 {e.番号} が2回以上出てきます", e.rect))
        else:
            seen_no[e.番号] = e
    seen_name: dict[str, IndexEntry] = {}
    for e in index:
        if not e.名称 or e.欠番:
            continue
        if e.名称 in seen_name:
            first = seen_name[e.名称]
            out.append(Finding("INDEX_DUP_NAME", LEVEL_CHECK, e.page, e.番号,
                               f"目次で名称「{e.名称}」が No.{first.番号} と No.{e.番号} に重複しています", e.rect))
        else:
            seen_name[e.名称] = e
    for e in index:
        if not e.名称:
            out.append(Finding("INDEX_BLANK", LEVEL_CHECK, e.page, e.番号,
                               f"目次の No.{e.番号} に図面名称がありません", e.rect))
    return out


# ---------------------------------------------------------------- 各ページと目次の照合
def check_number_and_name(sheet: Sheet, index: list[IndexEntry]) -> list[Finding]:
    """表題欄の図面番号・名称を目次と照合する。"""
    idx_by_no = _index_by_no(index)
    tb = sheet.title
    no, name = tb.図面番号, tb.図面名称
    no_rect = tb.rects.get("図面番号")
    if not no:
        return [Finding("NO_UNREADABLE", LEVEL_CHECK, sheet.page, "", "表題欄の図面番号が読み取れません", no_rect)]
    if no not in idx_by_no:
        return [Finding("NO_NOT_IN_INDEX", LEVEL_CHECK, sheet.page, no, f"図面番号 {no} が目次にありません", no_rect)]
    entry = idx_by_no[no]
    if entry.欠番:
        return [Finding("NO_IS_KEKBAN", LEVEL_CHECK, sheet.page, no,
                        f"図面番号 {no} は目次では欠番になっています（目次の名称「{entry.名称}」）", no_rect)]
    sheet.index_name = entry.名称
    if entry.名称 == name:
        return []
    ratio = SequenceMatcher(None, entry.名称, name).ratio()
    msg = f"名称が目次と違います（目次「{entry.名称}」／図面「{name}」 似ている度合い {ratio:.0%}）"
    others = [e.番号 for e in index if e.名称 == name and e.番号 != no and not e.欠番]
    if others:
        msg += f"。この名称は目次では No.{'・'.join(others)} です（番号の付け違い？）"
    return [Finding("NAME_MISMATCH", LEVEL_CHECK, sheet.page, no, msg, tb.rects.get("図面名称"))]


def check_blank_fields(sheet: Sheet) -> list[Finding]:
    """縮尺・製図年月・総枚数の空欄を調べる。"""
    tb = sheet.title
    out = []
    if not tb.縮尺:
        out.append(Finding("SCALE_BLANK", LEVEL_CHECK, sheet.page, tb.図面番号, "縮尺が空欄です", tb.rects.get("縮尺")))
    if not (tb.製図年 and tb.製図月):
        rects = [r for k, r in tb.rects.items() if k in ("製図年", "製図月")]
        rect = (min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects),
                max(r[3] for r in rects)) if rects else None
        missing = "・".join(k for k in ("製図年", "製図月") if not tb.get(k))
        out.append(Finding("DATE_BLANK", LEVEL_CHECK, sheet.page, tb.図面番号, f"製図年月の{missing}が空欄です", rect))
    if not tb.総枚数:
        out.append(Finding("TOTAL_BLANK", LEVEL_CHECK, sheet.page, tb.図面番号,
                           "「（　枚の内）」の総枚数が空欄です", tb.rects.get("総枚数")))
    return out


def check_references(sheet: Sheet, index: list[IndexEntry]) -> list[Finding]:
    """凡例が参照している図面が、目次にあるか・欠番でないかを調べる。"""
    out = []
    for ref in sheet.references:
        targets = []
        for e in index:
            m = re.fullmatch(ref.target_pattern, e.名称)
            if m and int(m.group(1)) == ref.number:
                targets.append(e)
        label = _ref_label(ref)
        if not targets:
            out.append(Finding("REF_MISSING", LEVEL_CHECK, sheet.page, sheet.drawing_no,
                               f"凡例が「{label}」を参照していますが、目次にありません", ref.rect))
        elif all(e.欠番 for e in targets):
            nos = "・".join(e.番号 for e in targets)
            out.append(Finding("REF_KEKBAN", LEVEL_CHECK, sheet.page, sheet.drawing_no,
                               f"凡例が「{label}」を参照していますが、目次では欠番です（No.{nos}）", ref.rect))
    return out


def _ref_label(ref: Reference) -> str:
    """参照先の図面名を人が読める形にする（正規表現の (\\d+) を番号に置き換える）。"""
    text = re.sub(r"\(\\d\+\)|\(\[0-9\]\+\)", str(ref.number), ref.target_pattern, count=1)
    return text.replace("\\", "")


# ---------------------------------------------------------------- 図面セット全体
def check_totals(sheets: list[Sheet], index: list[IndexEntry]) -> list[Finding]:
    """総枚数が記入されている場合に、ページ間で同じか・目次の枚数（欠番除く）と合うかを調べる。"""
    out = []
    totals = sorted({s.title.総枚数 for s in sheets if s.title.総枚数}, key=int)
    expected = len([e for e in index if e.名称 and not e.欠番])
    if len(totals) > 1:
        out.append(Finding("TOTAL_MISMATCH", LEVEL_CHECK, None, "",
                           f"総枚数がページによって違います（{'・'.join(totals)} 枚）", None))
    if index:
        for t in totals:
            if int(t) != expected:
                out.append(Finding("TOTAL_MISMATCH", LEVEL_CHECK, None, "",
                                   f"総枚数 {t} 枚が、目次の枚数（欠番を除いて {expected} 枚）と合いません", None))
    return out


def check_kojimei(sheets: list[Sheet]) -> list[Finding]:
    """全ページで工事名が同じかを調べる。"""
    names: dict[str, list[int]] = {}
    for s in sheets:
        names.setdefault(s.title.工事名, []).append(s.page)
    if len(names) <= 1:
        return []
    parts = [f"「{n or '（空欄）'}」p{','.join(map(str, pages))}" for n, pages in names.items()]
    return [Finding("KOJIMEI_MISMATCH", LEVEL_CHECK, None, "", "工事名が統一されていません: " + " / ".join(parts), None)]


def check_duplicate_pages(sheets: list[Sheet]) -> list[Finding]:
    """同じ図面番号のページが複数ないかを調べる。"""
    out = []
    seen: dict[str, int] = {}
    for s in sheets:
        no = s.drawing_no
        if not no:
            continue
        if no in seen:
            out.append(Finding("DUP_PAGE_NO", LEVEL_CHECK, s.page, no,
                               f"図面番号 {no} が {seen[no]} ページと {s.page} ページで重複しています",
                               s.title.rects.get("図面番号")))
        else:
            seen[no] = s.page
    return out


def check_index_not_in_set(sheets: list[Sheet], index: list[IndexEntry]) -> list[Finding]:
    """目次にあるが図面セットに含まれない図面を数える（訂正分などの部分セットでは多数出るため参考扱い）。"""
    present = {s.drawing_no for s in sheets}
    missing = [e for e in index if e.名称 and not e.欠番 and e.番号 not in present]
    if not missing:
        return []
    return [Finding("INDEX_NOT_IN_SET", LEVEL_INFO, None, "",
                    f"目次にあってこのPDFに含まれない図面が {len(missing)} 件あります（訂正分など一部だけのセットなら問題ありません）",
                    None, details=[f"No.{e.番号} {e.名称}" for e in missing])]


def _index_by_no(index: list[IndexEntry]) -> dict[str, IndexEntry]:
    """番号→目次の行。番号が重複している場合は後の行を採る（試作と同じ）。"""
    return {e.番号: e for e in index}


# ---------------------------------------------------------------- まとめて実行
def run_checks(pdf_path: str, layout: Layout, index: list[IndexEntry] | None = None) -> CheckResult:
    """PDFを開いて全ルールを実行する。index を渡すと目次ページの代わりにそれと照合する
    （目次のない変更図面を、工事台帳の図面一覧と照合するときに使う）。"""
    with pymupdf.open(pdf_path) as doc:
        return check_document(doc, layout, index=index, pdf_path=pdf_path)


def check_document(doc: "pymupdf.Document", layout: Layout, index: list[IndexEntry] | None = None,
                   pdf_path: str = "") -> CheckResult:
    """開いてあるPDFに対して全ルールを実行する。"""
    own_index = index is None
    if own_index:
        index = read_index(doc, layout)
    findings: list[Finding] = check_index(index) if own_index else []

    sheets: list[Sheet] = []
    for i, page in enumerate(doc):
        lines = lines_of(page)
        sheet = Sheet(page=i + 1, title=read_title_block(page, layout, lines),
                      references=read_references(page, layout, lines))
        sheet.findings += check_number_and_name(sheet, index)
        sheet.findings += check_blank_fields(sheet)
        sheet.findings += check_references(sheet, index)
        sheets.append(sheet)

    dup = check_duplicate_pages(sheets)
    for f in dup:
        next(s for s in sheets if s.page == f.page).findings.append(f)
    for s in sheets:
        findings += s.findings
    findings += check_totals(sheets, index)
    findings += check_kojimei(sheets)
    if own_index:
        findings += check_index_not_in_set(sheets, index)
    return CheckResult(pdf_path=pdf_path, layout=layout, index=index, sheets=sheets, findings=findings)


# ---------------------------------------------------------------- コマンドライン
def _print_result(res: CheckResult):
    """チェック結果を画面に一覧で表示する。"""
    kekban = [e.番号 for e in res.index if e.欠番]
    print(f"目次: {len(res.index)} 行（欠番 {len(kekban)} 件: {'・'.join(kekban) or 'なし'}）  図面: {len(res.sheets)} ページ")
    print("ページ | 図面番号 | 図面名称 | 縮尺 | 判定")
    for s in res.sheets:
        mark = "OK" if not s.findings else "要確認: " + " / ".join(f.message for f in s.findings)
        print(f"p{s.page:>3} | {s.drawing_no:>5} | {s.name} | {s.title.縮尺} | {mark}")
    for f in res.general_findings:
        print(f"[{f.level}] {f.rule_name}: {f.message}")


def main(argv: list[str] | None = None) -> int:
    """コマンドラインの入口。"""
    ap = argparse.ArgumentParser(prog="python -m drawings.checks", description="図面セットの発注前チェック")
    ap.add_argument("pdf", help="図面セットのPDF")
    ap.add_argument("--layout", default="toshiseibi_A1", help="様式設定ファイル（JSON）かその名前")
    ap.add_argument("--csv", help="結果をCSV（Excelで開けるUTF-8 BOM付き）で保存するパス")
    ap.add_argument("--html", help="結果をHTMLレポートで保存するパス")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    res = run_checks(args.pdf, load_layout(args.layout))
    _print_result(res)
    from . import report
    if args.csv:
        report.write_check_csv(res, args.csv)
        print("CSVを保存しました:", args.csv)
    if args.html:
        report.write_check_html(res, args.html)
        print("HTMLを保存しました:", args.html)
    return 0


if __name__ == "__main__":
    sys.exit(main())
