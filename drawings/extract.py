"""PDFから表題欄・目次・凡例の参照番号を座標で読み取る。

読み取れなかった項目は空文字にし、例外では止めない。座標はすべてPDFポイント（左上原点）で、
様式設定（layouts.Layout）の値を実ページの大きさに換算して使う。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

import pymupdf

from .layouts import Box, Layout

# 表題欄の項目（様式設定の title_block のキーと同じ）
TITLE_KEYS = ["工事名", "図面名称", "縮尺", "図面番号", "製図年", "製図月", "総枚数"]
# 数字以外を取り除く項目（全角数字もNFKCで半角になる）
_DIGIT_ONLY_KEYS = {"図面番号", "総枚数"}


def norm(s: str) -> str:
    """表記ゆれをそろえる（全角英数→半角などのNFKC正規化と、空白の除去）。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or ""))


@dataclass
class Line:
    """PDFの1行分の文字と、その範囲。chars は1文字ずつの (左端x, 文字)。"""
    bbox: Box
    text: str
    chars: list[tuple[float, str]] = field(default_factory=list)


def lines_of(page: "pymupdf.Page") -> list[Line]:
    """ページ内の文字を行（line）単位で、座標つきで取り出す。"""
    out = []
    try:
        blocks = page.get_text("rawdict")["blocks"]
    except Exception:  # noqa: BLE001  壊れたページでも止めない
        return out
    for b in blocks:
        for ln in b.get("lines", []):
            chars = [(ch["bbox"][0], ch["c"]) for sp in ln["spans"] for ch in sp.get("chars", [])]
            text = "".join(c for _, c in chars).strip()
            if text:
                out.append(Line(tuple(ln["bbox"]), text, chars))
    return out


def _text_in(lines: list[Line], box: Box, tol: float) -> str:
    """box の中から始まる文字を、行ごとに左から順につないで返す（正規化済み）。

    基本は試作と同じく「行の左上の点が box の中にある行」を拾う。ただし文字どうしが近いと、
    隣の欄の文字と1行にまとめられてしまう（例「１７（２」）ため、box の左端より前・右端より後から
    始まる文字は除く。
    """
    x0, y0, x1, y1 = box
    hits = []
    for ln in lines:
        lx0, ly0, lx1, _ly1 = ln.bbox
        if not (y0 - tol <= ly0 < y1) or lx1 <= x0 - tol or lx0 >= x1:
            continue
        part = [(cx, c) for cx, c in ln.chars if x0 - tol <= cx < x1] if ln.chars else [(lx0, ln.text)]
        if part:
            hits.append((part[0][0], "".join(c for _, c in part)))
    hits.sort()
    return norm("".join(t for _, t in hits))


# ---------------------------------------------------------------- 表題欄
@dataclass
class TitleBlock:
    """1ページ分の表題欄の読み取り結果。rects には各項目の読取り範囲（ページ上の座標）を持つ。"""
    工事名: str = ""
    図面名称: str = ""
    縮尺: str = ""
    図面番号: str = ""
    製図年: str = ""
    製図月: str = ""
    総枚数: str = ""
    rects: dict[str, Box] = field(default_factory=dict)

    def get(self, key: str) -> str:
        """項目名で値を取り出す。"""
        return getattr(self, key, "") if key in TITLE_KEYS else ""

    def as_dict(self) -> dict[str, str]:
        """項目名→値の辞書にする（CSV出力などに使う）。"""
        return {k: self.get(k) for k in TITLE_KEYS}


def read_title_block(page: "pymupdf.Page", layout: Layout, lines: list[Line] | None = None) -> TitleBlock:
    """ページの表題欄を様式設定の位置で読み取る。"""
    if lines is None:
        lines = lines_of(page)
    w, h = page.rect.width, page.rect.height
    sx, _sy = layout.scale(w, h)
    tb = TitleBlock()
    for key, box in layout.title_block.items():
        rect = layout.box_on(box, w, h)
        tb.rects[key] = rect
        if key not in TITLE_KEYS:
            continue
        value = _text_in(lines, rect, 1.0 * sx)
        if key in _DIGIT_ONLY_KEYS:
            value = re.sub(r"[^0-9]", "", value)
        setattr(tb, key, value)
    return tb


# ---------------------------------------------------------------- 目次
@dataclass
class IndexEntry:
    """目次の1行。欠番の行は 名称 に元の名称（「欠番」を除いたもの）を持つ。"""
    番号: str
    名称: str
    区分: str
    欠番: bool
    rect: Box            # 目次ページ上の範囲（番号と名称をあわせた範囲）
    page: int            # 目次のページ番号（1始まり）

    @property
    def y(self) -> int:
        return round(self.rect[1])


def _union(boxes: list[Box]) -> Box:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def read_index(doc: "pymupdf.Document", layout: Layout) -> list[IndexEntry]:
    """目次ページから図面リストを読み取る。目次がない・読めないときは空のリストを返す。"""
    ix = layout.index
    if ix is None or not 1 <= ix.page <= doc.page_count:
        return []
    page = doc[ix.page - 1]
    w, h = page.rect.width, page.rect.height
    sx, sy = layout.scale(w, h)
    lines = lines_of(page)
    top = ix.header_bottom_y * sy
    bottom = ix.body_bottom_y * sy
    entries: list[IndexEntry] = []
    for ci, col in enumerate(ix.columns):
        nx0, nx1 = col.num_x[0] * sx, col.num_x[1] * sx
        if col.name_right_x is not None:
            right = col.name_right_x * sx
        elif ci + 1 < len(ix.columns):
            right = ix.columns[ci + 1].num_x[0] * sx - 5 * sx
        else:
            right = w
        nums = [(ln.bbox, norm(ln.text)) for ln in lines
                if nx0 <= ln.bbox[0] < nx1 and ln.bbox[1] > top and re.fullmatch(r"[0-9]+", norm(ln.text))]
        names = [ln for ln in lines if nx1 + 5 * sx <= ln.bbox[0] < right and top < ln.bbox[1] < bottom]
        for nb, num in nums:
            row = sorted((ln for ln in names if abs(ln.bbox[1] - nb[1]) < ix.row_tolerance * sy),
                         key=lambda ln: ln.bbox[0])
            name = norm("".join(ln.text for ln in row))
            entries.append(IndexEntry(
                番号=num,
                名称=name.replace("欠番", ""),
                欠番="欠番" in name,
                区分=col.category,
                rect=_union([nb] + [ln.bbox for ln in row]),
                page=ix.page,
            ))
    return entries


# ---------------------------------------------------------------- 凡例の参照番号
@dataclass
class Reference:
    """凡例などに書かれた、別図面への参照番号。"""
    label: str
    number: int
    target_pattern: str
    rect: Box


def read_references(page: "pymupdf.Page", layout: Layout, lines: list[Line] | None = None) -> list[Reference]:
    """凡例の見出し（例「附帯詳細図」）の下に並ぶ (n) の参照番号を拾う。同じ番号は1つにまとめる。"""
    if not layout.references:
        return []
    if lines is None:
        lines = lines_of(page)
    sx, _sy = layout.scale(page.rect.width, page.rect.height)
    normed = [(ln.bbox, norm(ln.text)) for ln in lines]
    found: dict[tuple[str, int], Reference] = {}
    for rule in layout.references:
        label = norm(rule.label)
        heads = [bb for bb, t in normed if t == label]
        for hb in heads:
            for bb, t in normed:
                m = re.fullmatch(rule.ref_pattern, t)
                if m and abs(bb[0] - hb[0]) < rule.x_tolerance * sx and bb[1] > hb[1]:
                    key = (rule.target_pattern, int(m.group(1)))
                    if key not in found:
                        found[key] = Reference(rule.label, int(m.group(1)), rule.target_pattern, bb)
    return sorted(found.values(), key=lambda r: (r.target_pattern, r.number))
