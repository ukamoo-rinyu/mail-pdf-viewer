"""図面セットの発注前チェック（試作）
- 目次（1ページ目の図面リスト）と各ページの表題欄を座標で読み取り、照合する
"""
import json
import re
import sys
import unicodedata
from difflib import SequenceMatcher

import pymupdf

# ---- 表題欄の位置（この図面セットの様式にあわせた設定値） ----
# 単位はPDFポイント。様式が変わったらここを変えるだけで対応できるようにしておく
TITLE_FIELDS = {
    "工事名":   (2010, 1525, 2155, 1545),
    "図面名称": (2010, 1547, 2155, 1566),
    "縮尺":     (2010, 1566, 2155, 1584),
    "図面番号": (2225, 1566, 2255, 1584),
    "年":       (2225, 1547, 2250, 1566),
    "月":       (2270, 1547, 2298, 1566),
    "総枚数":   (2268, 1566, 2292, 1584),
}

# ---- 目次の列（番号列のx範囲）と、名称列はその右側 ----
INDEX_NUM_COLS = [(135, 170), (490, 520), (840, 875), (1190, 1225), (1545, 1580)]
INDEX_CATEGORY = ["意匠", "意匠", "意匠", "構造", "外構"]


def norm(s: str) -> str:
    """表記ゆれをそろえる（全角半角・空白）"""
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", "", s)


def lines_of(page):
    out = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            t = "".join(sp["text"] for sp in l["spans"]).strip()
            if t:
                out.append((l["bbox"], t))
    return out


def text_in(lines, box):
    x0, y0, x1, y1 = box
    hits = [(bb[0], t) for bb, t in lines
            if bb[0] >= x0 - 1 and bb[1] >= y0 - 1 and bb[0] < x1 and bb[1] < y1]
    hits.sort()
    return norm("".join(t for _, t in hits))


def read_title_block(page):
    lines = lines_of(page)
    tb = {k: text_in(lines, box) for k, box in TITLE_FIELDS.items()}
    tb["図面番号"] = re.sub(r"[^0-9]", "", tb["図面番号"])
    tb["総枚数"] = re.sub(r"[^0-9]", "", tb["総枚数"])
    return tb


def read_refs(page):
    """凡例の「屋外一般附帯詳細図」欄にある (n) の参照番号を拾う"""
    lines = [(bb, norm(t)) for bb, t in lines_of(page)]
    heads = [bb for bb, t in lines if t == "附帯詳細図"]
    refs = []
    for hb in heads:
        for bb, t in lines:
            m = re.fullmatch(r"\((\d+)\)", t)
            if m and abs(bb[0] - hb[0]) < 30 and bb[1] > hb[1]:
                refs.append(int(m.group(1)))
    return sorted(set(refs))


def read_index(page):
    lines = lines_of(page)
    entries = []
    for ci, (nx0, nx1) in enumerate(INDEX_NUM_COLS):
        nums = [(bb, norm(t)) for bb, t in lines
                if nx0 <= bb[0] < nx1 and bb[1] > 325 and re.fullmatch(r"[0-9]+", norm(t))]
        right = INDEX_NUM_COLS[ci + 1][0] - 5 if ci + 1 < len(INDEX_NUM_COLS) else 1890
        names = [(bb, t) for bb, t in lines
                 if nx1 + 5 <= bb[0] < right and bb[1] > 325 and bb[1] < 1500]
        for nb, num in nums:
            row_names = [t for bb, t in names if abs(bb[1] - nb[1]) < 6]
            name = norm("".join(row_names))
            entries.append({
                "番号": num,
                "名称": name.replace("欠番", ""),
                "欠番": "欠番" in name,
                "区分": INDEX_CATEGORY[ci],
                "y": round(nb[1]),
            })
    return entries


def check(pdf_path):
    doc = pymupdf.open(pdf_path)
    index = read_index(doc[0])
    idx_by_no = {}
    findings = []

    # --- 目次そのものの点検 ---
    for e in index:
        if e["番号"] in idx_by_no:
            findings.append(("目次", f"番号 {e['番号']} が目次に重複しています"))
        idx_by_no[e["番号"]] = e
    names_seen = {}
    for e in index:
        if e["名称"] and not e["欠番"]:
            if e["名称"] in names_seen:
                findings.append(("目次", f"名称「{e['名称']}」が {names_seen[e['名称']]} と {e['番号']} に重複しています"))
            names_seen[e["名称"]] = e["番号"]
    blank_rows = [e["番号"] for e in index if not e["名称"]]

    # --- 各ページの表題欄と目次の照合 ---
    sheets = []
    kojimei = set()
    for i, page in enumerate(doc):
        tb = read_title_block(page)
        kojimei.add(tb["工事名"])
        no, name = tb["図面番号"], tb["図面名称"]
        row = {"ページ": i + 1, **tb, "判定": [], "目次名称": None}
        if not no:
            row["判定"].append("図面番号が読み取れません")
        elif no not in idx_by_no:
            row["判定"].append(f"図面番号 {no} が目次にありません")
        elif idx_by_no[no]["欠番"]:
            row["判定"].append(f"図面番号 {no} は目次で欠番になっています")
        else:
            idx_name = idx_by_no[no]["名称"]
            row["目次名称"] = idx_name
            if idx_name != name:
                r = SequenceMatcher(None, idx_name, name).ratio()
                row["判定"].append(f"名称不一致（目次「{idx_name}」／図面「{name}」類似度{r:.0%}）")
                # 名称が別番号の目次と一致していないか（番号の付け違い）
                for e in index:
                    if e["名称"] == name and e["番号"] != no and not e["欠番"]:
                        row["判定"].append(f"この名称は目次では {e['番号']} 番です（番号の付け違い？）")
        if not tb["縮尺"]:
            row["判定"].append("縮尺が空欄です")
        if not tb["総枚数"]:
            row["判定"].append("「（　枚の内）」の総枚数が空欄です")
        if not (tb["年"] and tb["月"]):
            row["判定"].append("製図年月が空欄です")
        # 参照先の存在チェック（屋外一般附帯詳細図）
        fu = {}
        for e in index:
            m = re.fullmatch(r"屋外一般附帯詳細図\((\d+)\)", e["名称"])
            if m:
                fu.setdefault(int(m.group(1)), []).append(e)
        for n in read_refs(page):
            live = [e for e in fu.get(n, []) if not e["欠番"]]
            if not fu.get(n):
                row["判定"].append(f"凡例が「屋外一般附帯詳細図({n})」を参照していますが目次にありません")
            elif not live:
                nos = "・".join(e["番号"] for e in fu[n])
                row["判定"].append(f"凡例が「屋外一般附帯詳細図({n})」を参照していますが目次では欠番です（No.{nos}）")
        sheets.append(row)

    # --- 総枚数の整合（記入されている場合：全ページ同じ値か、目次の枚数と合うか） ---
    totals = {s["総枚数"] for s in sheets if s["総枚数"]}
    expected = len([e for e in index if e["名称"] and not e["欠番"]])
    if len(totals) > 1:
        findings.append(("表題欄", f"総枚数がページによって違います: {sorted(totals)}"))
    for t in totals:
        if int(t) != expected:
            findings.append(("表題欄", f"総枚数 {t} 枚が目次の枚数（欠番除く {expected} 枚）と合いません"))

    if len(kojimei) > 1:
        findings.append(("表題欄", f"工事名が統一されていません: {sorted(kojimei)}"))

    # --- 同じ図面番号のページが複数ないか ---
    seen = {}
    for s in sheets:
        if s["図面番号"] in seen:
            findings.append(("表題欄", f"図面番号 {s['図面番号']} が {seen[s['図面番号']]} ページと {s['ページ']} ページで重複"))
        seen[s["図面番号"]] = s["ページ"]

    return {"目次件数": len(index), "目次_名称なし番号": blank_rows,
            "目次": index, "図面": sheets, "全体の指摘": findings}


if __name__ == "__main__":
    res = check(sys.argv[1])
    json.dump(res, open("result.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("目次件数:", res["目次件数"], " 名称なし:", res["目次_名称なし番号"])
    for s in res["図面"]:
        mark = "OK " if not s["判定"] else "要確認"
        print(f"p{s['ページ']:>2} No.{s['図面番号']:>4} {s['図面名称']:<24} {s['縮尺']:<8} {mark} {' / '.join(s['判定'])}")
    for f in res["全体の指摘"]:
        print("[全体]", *f)
