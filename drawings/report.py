"""チェック結果・変更記録の一覧を CSV / HTML に書き出す。

CSV は Excel でそのまま開けるよう UTF-8（BOM付き）で書く。HTML は1ファイルで完結し、
ブラウザで開いて印刷できる（インターネット接続は不要）。
"""
from __future__ import annotations

import csv
import html
import os
from datetime import datetime

from .checks import LEVEL_CHECK, CheckResult


def _write_csv(path: str, header: list[str], rows: list[list]):
    """Excelで開けるCSV（UTF-8 BOM付き）を書く。"""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


_CSS = """
body { font-family: "Yu Gothic UI", "Meiryo", sans-serif; color: #1F2328; margin: 24px; }
h1 { font-size: 18px; } h2 { font-size: 15px; margin-top: 24px; border-bottom: 2px solid #2563EB; padding-bottom: 4px; }
.meta { color: #5B6470; font-size: 12px; }
table { border-collapse: collapse; width: 100%; font-size: 12px; margin-top: 8px; }
th, td { border: 1px solid #D0D4D9; padding: 4px 6px; vertical-align: top; text-align: left; }
th { background: #F4F5F7; }
tr.warn td { background: #FFF4E5; }
.ok { color: #1B7F3B; } .ng { color: #B45309; font-weight: 600; } .info { color: #5B6470; }
.check { width: 18px; height: 18px; border: 1px solid #5B6470; display: inline-block; }
@media print { body { margin: 8mm; } h2 { break-after: avoid; } tr { break-inside: avoid; } }
"""


def _html_page(title: str, body: str) -> str:
    return (f"<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'><title>{_e(title)}</title>"
            f"<style>{_CSS}</style></head><body>{body}</body></html>")


# ---------------------------------------------------------------- 発注前チェック
CHECK_CSV_HEADER = ["ページ", "図面番号", "図面名称", "縮尺", "工事名", "製図年", "製図月", "総枚数",
                    "区分", "規則", "判定内容"]


def check_rows(res: CheckResult) -> list[list]:
    """チェック結果をCSVの行にする（指摘1件につき1行。指摘のないページも1行出す）。"""
    rows = []
    for s in res.sheets:
        tb = s.title
        base = [s.page, tb.図面番号, tb.図面名称, tb.縮尺, tb.工事名, tb.製図年, tb.製図月, tb.総枚数]
        if not s.findings:
            rows.append(base + ["OK", "", ""])
        for f in s.findings:
            rows.append(base + [f.level, f.rule_name, f.message])
    for f in res.general_findings:
        detail = f.message + ("（" + "、".join(f.details) + "）" if f.details else "")
        rows.append([f.page or "", f.drawing_no, "（図面セット全体）" if f.page is None else "（目次）",
                     "", "", "", "", "", f.level, f.rule_name, detail])
    return rows


def write_check_csv(res: CheckResult, path: str):
    """発注前チェックの結果をCSVで保存する。"""
    _write_csv(path, CHECK_CSV_HEADER, check_rows(res))


def write_check_html(res: CheckResult, path: str):
    """発注前チェックの結果を、印刷できるHTMLレポートで保存する。"""
    n_check = res.count(LEVEL_CHECK)
    kekban = [e.番号 for e in res.index if e.欠番]
    parts = [f"<h1>図面セット 発注前チェック結果</h1>",
             f"<p class='meta'>PDF: {_e(os.path.basename(res.pdf_path))}　様式: {_e(res.layout.name)}　"
             f"作成: {datetime.now():%Y-%m-%d %H:%M}</p>",
             f"<p>目次 {len(res.index)} 行（欠番 {len(kekban)} 件{'：' + '・'.join(kekban) if kekban else ''}）／"
             f"図面 {len(res.sheets)} ページ／<span class='{'ng' if n_check else 'ok'}'>要確認 {n_check} 件</span></p>",
             "<p class='meta'>※「要確認」は誤りと決まったものではありません。図面を見て判断してください。</p>"]
    general = res.general_findings
    if general:
        parts.append("<h2>目次・図面セット全体</h2><table><tr><th>区分</th><th>規則</th><th>内容</th></tr>")
        for f in general:
            detail = f"<br><span class='info'>{_e('、'.join(f.details))}</span>" if f.details else ""
            parts.append(f"<tr class='{'warn' if f.level == LEVEL_CHECK else ''}'><td>{_e(f.level)}</td>"
                         f"<td>{_e(f.rule_name)}</td><td>{_e(f.message)}{detail}</td></tr>")
        parts.append("</table>")
    parts.append("<h2>各図面</h2><table><tr><th>ページ</th><th>図面番号</th><th>図面名称</th><th>縮尺</th><th>判定</th></tr>")
    for s in res.sheets:
        if s.findings:
            judge = "<br>".join(f"<span class='ng'>要確認</span> {_e(f.message)}" for f in s.findings)
        else:
            judge = "<span class='ok'>OK</span>"
        parts.append(f"<tr class='{'warn' if s.findings else ''}'><td>{s.page}</td><td>{_e(s.drawing_no)}</td>"
                     f"<td>{_e(s.name)}</td><td>{_e(s.title.縮尺)}</td><td>{judge}</td></tr>")
    parts.append("</table>")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_html_page("発注前チェック結果", "".join(parts)))


# ---------------------------------------------------------------- 変更記録（工事台帳）
def _trigger(rec: dict) -> str:
    t = rec.get("きっかけ") or {}
    return " ".join(x for x in (t.get("種別", ""), t.get("番号", "")) if x)


def _places(rec: dict, no: str | None = None) -> str:
    """図面上の箇所のコメントを1つの文字列にする（no を渡すとその図面の箇所だけ）。"""
    items = [p for p in rec.get("図面上の箇所", []) if no is None or p.get("図面番号") == no]
    return "／".join(f"No.{p.get('図面番号', '')}" + (f" {p['コメント']}" if p.get("コメント") else "") for p in items)


RECORD_CSV_HEADER = ["ID", "状態", "記録日", "指示日", "きっかけ", "内容", "理由", "決定者", "指示方法",
                     "対象図面", "図面上の箇所", "変更契約", "竣工図"]


def record_row(rec: dict) -> list:
    refl = rec.get("反映先") or {}
    return [rec.get("id", ""), rec.get("状態", ""), rec.get("記録日", ""), rec.get("指示日", ""), _trigger(rec),
            rec.get("内容", ""), rec.get("理由", ""), rec.get("決定者", ""), rec.get("指示方法", ""),
            "・".join(rec.get("対象図面", [])), _places(rec), refl.get("変更契約") or "", refl.get("竣工図") or ""]


def write_records_csv(records: list[dict], path: str):
    """変更記録の一覧をCSVで保存する。"""
    _write_csv(path, RECORD_CSV_HEADER, [record_row(r) for r in records])


def _koji_meta(ledger) -> str:
    k = ledger.koji
    items = [f"{key}: {_e(k.get(key))}" for key in ("工事名", "工事番号", "施設名", "請負者", "工期") if k.get(key)]
    return f"<p class='meta'>{'　'.join(items)}　作成: {datetime.now():%Y-%m-%d %H:%M}</p>"


GROUPED_CSV_HEADER = ["図面番号", "図面名称", "ID", "状態", "指示日", "きっかけ", "内容", "理由", "決定者",
                      "指示方法", "図面上の箇所"]


def _grouped_rows(groups) -> list[list]:
    rows = []
    for d, recs in groups:
        for r in recs:
            rows.append([d.get("図面番号", ""), d.get("図面名称", ""), r.get("id", ""), r.get("状態", ""),
                         r.get("指示日", ""), _trigger(r), r.get("内容", ""), r.get("理由", ""),
                         r.get("決定者", ""), r.get("指示方法", ""), _places(r, d.get("図面番号"))])
    return rows


def write_contract_list_csv(ledger, path: str):
    """変更契約用一覧（状態が「指示済」の記録を図面別）をCSVで保存する。"""
    _write_csv(path, GROUPED_CSV_HEADER, _grouped_rows(ledger.pending_for_contract()))


def write_contract_list_html(ledger, path: str):
    """変更契約用一覧をHTMLで保存する。変更図面の作成指示や、変更理由の下書きに使う。"""
    groups = ledger.pending_for_contract()
    parts = ["<h1>変更契約用 変更一覧（未反映の変更）</h1>", _koji_meta(ledger),
             f"<p>対象の図面 {len(groups)} 枚／変更記録 {len({r['id'] for _, rs in groups for r in rs})} 件"
             "（状態が「指示済」のもの）</p>"]
    if not groups:
        parts.append("<p>変更契約に反映していない変更はありません。</p>")
    for d, recs in groups:
        parts.append(f"<h2>No.{_e(d.get('図面番号'))}　{_e(d.get('図面名称'))}</h2>"
                     "<table><tr><th>ID</th><th>指示日</th><th>きっかけ</th><th>変更内容</th><th>理由</th>"
                     "<th>決定者・指示方法</th><th>図面上の箇所</th></tr>")
        for r in recs:
            parts.append(f"<tr><td>{_e(r.get('id'))}</td><td>{_e(r.get('指示日'))}</td><td>{_e(_trigger(r))}</td>"
                         f"<td>{_e(r.get('内容'))}</td><td>{_e(r.get('理由'))}</td>"
                         f"<td>{_e(r.get('決定者'))}<br>{_e(r.get('指示方法'))}</td>"
                         f"<td>{_e(_places(r, d.get('図面番号')))}</td></tr>")
        parts.append("</table>")
    parts.append("<h2>変更理由の下書き</h2><ul>")
    seen = set()
    for _d, recs in groups:
        for r in recs:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            reason = r.get("理由") or "（理由未記入）"
            parts.append(f"<li>{_e(r.get('内容'))}（{_e(reason)}。対象図面 No.{_e('・'.join(r.get('対象図面', [])))}）</li>")
    parts.append("</ul>")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_html_page("変更契約用 変更一覧", "".join(parts)))


def write_completion_checklist_csv(ledger, path: str):
    """竣工図チェックリストをCSVで保存する（反映済の欄つき）。"""
    rows = []
    for d, recs in ledger.completion_checklist():
        for r in recs:
            done = "済" if ledger.is_completion_done(r) else ""
            rows.append([done] + _grouped_rows([(d, [r])])[0])
    _write_csv(path, ["竣工図反映"] + GROUPED_CSV_HEADER, rows)


def write_completion_checklist_html(ledger, path: str):
    """竣工図チェックリストをHTMLで保存する（印刷してチェック欄に記入できる）。"""
    groups = ledger.completion_checklist()
    total = sum(len(rs) for _, rs in groups)
    done = sum(1 for _, rs in groups for r in rs if ledger.is_completion_done(r))
    parts = ["<h1>竣工図 反映チェックリスト</h1>", _koji_meta(ledger),
             f"<p>図面 {len(groups)} 枚／確認項目 {total} 件（うち反映済 {done} 件）"
             "※契約変更の有無を問わず、竣工図に反映すべき変更の一覧です。</p>"]
    for d, recs in groups:
        parts.append(f"<h2>No.{_e(d.get('図面番号'))}　{_e(d.get('図面名称'))}</h2>"
                     "<table><tr><th>反映</th><th>ID</th><th>状態</th><th>指示日</th><th>変更内容</th>"
                     "<th>図面上の箇所</th></tr>")
        for r in recs:
            mark = "✔" if ledger.is_completion_done(r) else "<span class='check'></span>"
            parts.append(f"<tr><td>{mark}</td><td>{_e(r.get('id'))}</td><td>{_e(r.get('状態'))}</td>"
                         f"<td>{_e(r.get('指示日'))}</td><td>{_e(r.get('内容'))}</td>"
                         f"<td>{_e(_places(r, d.get('図面番号')))}</td></tr>")
        parts.append("</table>")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_html_page("竣工図 反映チェックリスト", "".join(parts)))
