"""図面様式（表題欄・目次の位置）の設定ファイル layouts/*.json を読み込む。

座標は設定ファイルの page_size_pt を基準に書いておき、実際のページの大きさとの比率で
換算して使う（A1の原寸とA3の縮小版のどちらでも同じ設定で読めるようにするため）。
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field

Box = tuple[float, float, float, float]  # (x0, y0, x1, y1) 単位はPDFポイント、左上原点


@dataclass
class IndexColumn:
    """目次の1列分（番号列のx範囲・区分・名称欄の右端）。"""
    num_x: tuple[float, float]
    category: str
    name_right_x: float | None = None


@dataclass
class IndexLayout:
    """目次ページの読み取り位置。"""
    page: int                      # 目次があるページ（1始まり）
    header_bottom_y: float         # これより下を本文として読む
    body_bottom_y: float           # 名称はこれより上だけ読む
    columns: list[IndexColumn]
    row_tolerance: float = 6       # 番号と名称を同じ行とみなすyの差


@dataclass
class ReferenceRule:
    """凡例の参照番号の読み方（例：「附帯詳細図」の下に並ぶ (n)）。"""
    label: str
    target_pattern: str            # 目次の名称から参照番号を取り出す正規表現
    x_tolerance: float = 30
    ref_pattern: str = r"\((\d+)\)"


@dataclass
class Layout:
    """図面様式1つ分の設定。"""
    key: str                       # ファイル名（拡張子なし）。工事台帳にはこれを記録する
    name: str
    page_size: tuple[float, float]
    title_block: dict[str, Box]
    index: IndexLayout | None
    references: list[ReferenceRule] = field(default_factory=list)
    path: str = ""

    def scale(self, page_width: float, page_height: float) -> tuple[float, float]:
        """設定の基準サイズに対する実ページの倍率 (sx, sy) を返す。"""
        w, h = self.page_size
        return (page_width / w if w else 1.0, page_height / h if h else 1.0)

    def box_on(self, box: Box, page_width: float, page_height: float) -> Box:
        """設定上の範囲を、実ページ上の範囲に換算する。"""
        sx, sy = self.scale(page_width, page_height)
        x0, y0, x1, y1 = box
        return (x0 * sx, y0 * sy, x1 * sx, y1 * sy)


def default_layout_dir() -> str:
    """様式設定フォルダ layouts/ の場所（exe化した場合は実行ファイルの隣）。"""
    candidates = []
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "layouts"))
    candidates.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "layouts"))
    for path in candidates:
        if os.path.isdir(path):
            return path
    return candidates[-1]


def _box(value) -> Box:
    x0, y0, x1, y1 = (float(v) for v in value)
    return (x0, y0, x1, y1)


def load_layout(path_or_key: str, layout_dir: str | None = None) -> Layout:
    """様式設定ファイルを読み込む。ファイルパスか、layouts/ 内のファイル名（拡張子なし）を渡す。"""
    path = path_or_key
    if not os.path.isfile(path):
        path = os.path.join(layout_dir or default_layout_dir(), f"{path_or_key}.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    index = None
    if data.get("index"):
        ix = data["index"]
        index = IndexLayout(
            page=int(ix.get("page", 1)),
            header_bottom_y=float(ix.get("header_bottom_y", 0)),
            body_bottom_y=float(ix.get("body_bottom_y", 1e9)),
            columns=[IndexColumn(num_x=(float(c["num_x"][0]), float(c["num_x"][1])),
                                 category=c.get("category", ""),
                                 name_right_x=float(c["name_right_x"]) if c.get("name_right_x") is not None else None)
                     for c in ix.get("columns", [])],
            row_tolerance=float(ix.get("row_tolerance", 6)),
        )
    refs = [ReferenceRule(label=r["label"], target_pattern=r["target_pattern"],
                          x_tolerance=float(r.get("x_tolerance", 30)),
                          ref_pattern=r.get("ref_pattern", r"\((\d+)\)"))
            for r in data.get("references", [])]
    return Layout(
        key=os.path.splitext(os.path.basename(path))[0],
        name=data.get("name", ""),
        page_size=(float(data["page_size_pt"][0]), float(data["page_size_pt"][1])),
        title_block={k: _box(v) for k, v in data.get("title_block", {}).items()},
        index=index,
        references=refs,
        path=os.path.abspath(path),
    )


def list_layouts(layout_dir: str | None = None) -> list[Layout]:
    """layouts/ にある様式設定をすべて読み込んで返す（読めないファイルは飛ばす）。"""
    folder = layout_dir or default_layout_dir()
    result = []
    if not os.path.isdir(folder):
        return result
    for name in sorted(os.listdir(folder)):
        if name.lower().endswith(".json"):
            try:
                result.append(load_layout(os.path.join(folder, name)))
            except (OSError, ValueError, KeyError, TypeError, IndexError):
                continue
    return result
