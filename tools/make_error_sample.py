"""実図面PDFから「間違いを仕込んだ版」を作る（テスト用）。

- p3 の図面名称「２～１３階平面図」を「２～１２階平面図」に書き換える（名称1文字違い）
- p9 の図面番号「３０４」を「３０３」に書き換える（番号の付け違い）
- p10 の縮尺「図示」を消す（縮尺空欄）

使い方: python tools/make_error_sample.py tests/data/実図面.pdf tests/data/実図面_間違い仕込み.pdf
"""
import sys

import pymupdf


def _replace(page, old: str, new: str | None, clip):
    """clip の範囲にある old の文字を消し、new があれば同じ位置・大きさで書き直す。"""
    hits = page.search_for(old, clip=clip)
    if not hits:
        raise ValueError(f"p{page.number + 1}: 「{old}」が見つかりません")
    rect = hits[0]
    size = rect.height * 0.86
    page.add_redact_annot(rect)
    # 線画・画像は残して文字だけ消す
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    if new:
        page.insert_text((rect.x0, rect.y1 - rect.height * 0.12), new, fontname="japan", fontsize=size)


def main(src: str, dst: str):
    """3か所の間違いを仕込んだPDFを dst に保存する。"""
    doc = pymupdf.open(src)
    title = pymupdf.Rect(2000, 1520, 2340, 1590)
    _replace(doc[2], "２～１３階平面図", "２～１２階平面図", title)
    _replace(doc[8], "３０４", "３０３", title)
    _replace(doc[9], "図示", None, title)
    doc.save(dst, garbage=3, deflate=True)
    print("保存しました:", dst)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
