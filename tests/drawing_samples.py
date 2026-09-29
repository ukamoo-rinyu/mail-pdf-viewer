"""図面機能のテストで使う、テストデータの場所と合成PDFの作成。"""

import os

import pymupdf

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
REAL_PDF = os.path.join(DATA_DIR, "実図面.pdf")
ERROR_PDF = os.path.join(DATA_DIR, "実図面_間違い仕込み.pdf")
LAYOUT_PATH = os.path.join(os.path.dirname(DATA_DIR), "..", "layouts", "toshiseibi_A1.json")

A1 = (2383.92, 1683.84)


def _put(page, x, y, text, size=9.8):
    """左上が (x, y) 付近になるよう文字を置く（insert_text は文字の下端を指定するため）。"""
    page.insert_text((x, y + size), text, fontname="japan", fontsize=size)


def make_synthetic_set(path: str, sheets: list[dict], index_rows: list[tuple] | None = None,
                       scale: float = 1.0, refs: dict[int, list[int]] | None = None):
    """A1様式をまねた小さな図面セットPDFを作る。

    sheets: 各ページの表題欄 {"工事名","図面名称","縮尺","図面番号","製図年","製図月","総枚数"}
    index_rows: 1ページ目の目次 (列番号0〜4, 番号, 名称, 欠番か)
    refs: ページ番号(1始まり) → 凡例「附帯詳細図」の下に並べる参照番号
    """
    doc = pymupdf.open()
    s = scale
    for i, tb in enumerate(sheets):
        page = doc.new_page(width=A1[0] * s, height=A1[1] * s)
        _put(page, 2019.6 * s, 1531.1 * s, tb.get("工事名", ""), 9.8 * s)
        _put(page, 2019.6 * s, 1553.3 * s, tb.get("図面名称", ""), 8.4 * s)
        _put(page, 2019.6 * s, 1570.3 * s, tb.get("縮尺", ""), 8.4 * s)
        _put(page, 2156.6 * s, 1552.5 * s, "製　　図令和", 9.8 * s)
        _put(page, 2235.4 * s, 1552.5 * s, tb.get("製図年", ""), 9.8 * s)
        _put(page, 2254.3 * s, 1552.5 * s, "年", 9.8 * s)
        _put(page, 2280.0 * s, 1552.5 * s, tb.get("製図月", ""), 9.8 * s)
        _put(page, 2301.1 * s, 1552.5 * s, "月", 9.8 * s)
        _put(page, 2231.0 * s, 1569.6 * s, "　" + tb.get("図面番号", ""), 9.7 * s)
        _put(page, 2256.0 * s, 1569.5 * s, "（", 9.8 * s)
        _put(page, 2270.0 * s, 1569.5 * s, tb.get("総枚数", ""), 9.8 * s)
        _put(page, 2291.3 * s, 1569.5 * s, "枚の内）", 9.8 * s)
        if i == 0 and index_rows:
            num_x = [140, 495, 845, 1195, 1550]
            per_col: dict[int, int] = {}
            _put(page, 140 * s, 300 * s, "番号", 9.8 * s)
            for col, no, name, kekban in index_rows:
                row = per_col.get(col, 0)
                per_col[col] = row + 1
                y = (340 + row * 22) * s
                _put(page, num_x[col] * s, y, " ".join(no), 9.8 * s)
                # 番号と名称が近すぎると1行にまとめて読まれてしまうため、実図面と同じく間を空ける
                _put(page, (num_x[col] + 70) * s, y, name, 9.8 * s)
                if kekban:
                    _put(page, (num_x[col] + 130) * s, y, "欠番", 9.8 * s)
        for n_i, n in enumerate((refs or {}).get(i + 1, [])):
            if n_i == 0:
                _put(page, 1700 * s, 900 * s, "附帯詳細図", 9.8 * s)
            _put(page, 1705 * s, (930 + n_i * 25) * s, f"({n})", 9.8 * s)
    doc.save(path)
    doc.close()


def make_scaled_copy(src: str, dst: str, scale: float):
    """PDFの各ページを縮小して貼り付けたPDFを作る（A1原寸 → A3縮小版の再現）。"""
    with pymupdf.open(src) as s, pymupdf.open() as d:
        for page in s:
            r = page.rect
            new = d.new_page(width=r.width * scale, height=r.height * scale)
            new.show_pdf_page(new.rect, s, page.number)
        d.save(dst)
