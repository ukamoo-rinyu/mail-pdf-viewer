"""kojiPDFviewer

KojiPDFが出力した「メール束PDF」を読み込み、メール一覧(差出人・件名・宛先・日時・
添付・本文冒頭)をカード形式で表示し、全文検索・ページジャンプ・PDF内ハイライトを行う。
しおりの構造が上記の形式に一致しない場合は、メール以外の一般資料を結合したPDFとみなし、
しおりをそのまま階層ツリーとして閲覧するモードに自動的に切り替わる。
複数のPDFをタブで同時に開ける。
"""
from __future__ import annotations

import functools
import os
import re
import sys
import tempfile
import unicodedata
from datetime import datetime

import pymupdf
from PySide6.QtCore import (
    QAbstractListModel,
    QCoreApplication,
    QEvent,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSettings,
    QSize,
    QSizeF,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QAction, QBrush, QColor, QCursor, QDesktopServices, QDragEnterEvent, QDropEvent, QFont, QFontMetrics, QGuiApplication, QIcon, QKeySequence, QPainter, QPalette, QPen, QPixmap, QShortcut, QStandardItem, QStandardItemModel
from PySide6.QtPdf import QPdfDocument, QPdfLinkModel, QPdfSearchModel
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QStyle,
    QStyledItemDelegate,
    QTabBar,
    QTabWidget,
    QToolBar,
    QToolButton,
    QToolTip,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

import db
import outlook_reply
import parser

# 図面モード(drawing_tab.py)が「import main」でこのファイルの部品を使うため、
# python main.py で起動したとき(モジュール名が __main__ になる)も同じものを参照させる。
sys.modules.setdefault("main", sys.modules[__name__])

APP_NAME = "kojiPDFviewer"

MAIL_ROLE = Qt.UserRole + 1
SECTION_ROLE = Qt.UserRole + 1
HEADER_ROLE = Qt.UserRole + 3
MAX_RECENT_FILES = 10

MAIL_SEARCH_PLACEHOLDER = "件名・差出人・宛先・本文・添付ファイル名で検索  (Ctrl+F)"
DOCUMENT_SEARCH_PLACEHOLDER = "しおりの見出し・本文で検索  (Ctrl+F)"

# 配色(ブラウザ版 MailPDFViewer.html のライトテーマと同じ値)
C_BG = "#F4F5F7"
C_PANEL = "#FFFFFF"
C_PANEL2 = "#F9FAFB"
C_LINE = "#E3E5E8"
C_LINE2 = "#D0D4D9"
C_TEXT = "#1F2328"
C_SUB = "#5B6470"
C_FAINT = "#8A929C"
C_ACCENT = "#2563EB"
C_ACCENT_BG = "#E8EFFD"
C_ACCENT_LINE = "#B9CCF7"
C_VIEWER = "#6B7280"
C_UNLINKED = "#C62828"     # しおりと対応付けられない添付(押しても移動しない)
C_UNLINKED_BG = "#FDE8E8"

UI_FONT_FAMILIES = ["Yu Gothic UI", "Meiryo UI", "Meiryo"]

def _style_pdf_view(view: QPdfView):
    """PDF表示部の余白(ページの周り)をブラウザ版と同じグレーにする。QPdfViewは
    palette().dark()で余白を塗るため、スタイルシートではなくパレットで指定する。"""
    pal = view.palette()
    pal.setColor(QPalette.ColorRole.Dark, QColor(C_VIEWER))
    view.setPalette(pal)


# ------------------------------------------------------------ 一覧の表示設定
class ViewSettings(QObject):
    """一覧の表示設定。QSettingsに保存して次回起動時も使い、変更は開いているすべての
    タブへ通知する(ブラウザ版の「⚙ 表示設定」と同じ項目)。"""

    changed = Signal(bool)  # 引数: 初期設定に戻したか

    def __init__(self, prefix: str, defaults: dict):
        super().__init__()
        self._prefix = prefix
        self.defaults = defaults
        self._qs = QSettings("ukawa", APP_NAME)
        self._values = {}
        for key, default in defaults.items():
            self._values[key] = self._qs.value(f"{prefix}/{key}", default, type=type(default))

    def get(self, key: str):
        return self._values[key]

    def set(self, key: str, value, force: bool = False):
        if self._values[key] == value and not force:
            return
        self._values[key] = value
        self._qs.setValue(f"{self._prefix}/{key}", value)
        self.changed.emit(force)

    def reset(self):
        self._values = dict(self.defaults)
        for key, value in self._values.items():
            self._qs.setValue(f"{self._prefix}/{key}", value)
        self.changed.emit(True)


BOOKMARK_VIEW_DEFAULTS = {
    "sort": "pdf",        # pdf | title
    "fontSize": 14,       # 12 | 14 | 16 | 18 (px)
    "density": "normal",  # normal | compact
    "wrap": True,         # 長い名前を折り返す / 1行で省略
    "pages": True,        # ページ番号を表示する
}

# メール一覧で表示する項目(キーは設定名 "f_<キー>")
MAIL_FIELDS = [
    ("from", "差出人"), ("date", "日時"), ("subject", "件名"), ("to", "宛先"), ("cc", "CC"),
    ("preview", "本文プレビュー"), ("attach", "添付ファイル"), ("pages", "ページ番号"), ("unread", "未読マーク"),
]
ROW_FIELDS = {"from", "date", "subject", "attach", "pages", "unread"}  # 1行リストで使える項目

MAIL_VIEW_DEFAULTS = {
    "layout": "card",       # card | row(1行リスト)
    "sort": "date-desc",    # pdf | pdf-desc | date-desc | date-asc | from | subject
    "fontSize": 14,         # 12 | 14 | 16 | 18 (px)
    "density": "normal",    # normal | compact
    "previewLines": 2,      # 1 | 2 | 3 | 5
    "dateFmt": "ymdhm",     # ymdhm | mdhm | raw
    "group": False,         # 同じ件名のメールをまとめて表示する
    **{f"f_{key}": True for key, _label in MAIL_FIELDS},
}

_view_settings: dict[str, ViewSettings] = {}


def bookmark_settings() -> ViewSettings:
    if "bookmark" not in _view_settings:
        _view_settings["bookmark"] = ViewSettings("bookmarkView", BOOKMARK_VIEW_DEFAULTS)
    return _view_settings["bookmark"]


def mail_settings() -> ViewSettings:
    if "mail" not in _view_settings:
        _view_settings["mail"] = ViewSettings("mailView", MAIL_VIEW_DEFAULTS)
    return _view_settings["mail"]


def _natural_key(text: str):
    """名前順の並べ替えキー。数字は数の大きさで比較する(「第2章」<「第10章」)。"""
    text = unicodedata.normalize("NFKC", text or "").lower()
    return [(0, int(part), "") if part.isdigit() else (1, 0, part)
            for part in re.split(r"(\d+)", text) if part]


_DATE_PATTERNS = [
    re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日(?:\D*?(\d{1,2}):(\d{2})(?::(\d{2}))?)?"),
    re.compile(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})(?:[^\d]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?"),
]


@functools.lru_cache(maxsize=4096)
def _parse_mail_time(*candidates: str) -> "datetime | None":
    """受信日時などの文字列を日時に変換する(読めなければNone)。"""
    for raw in candidates:
        if not raw:
            continue
        text = unicodedata.normalize("NFKC", raw)
        for pattern in _DATE_PATTERNS:
            m = pattern.search(text)
            if m:
                try:
                    return datetime(int(m[1]), int(m[2]), int(m[3]),
                                    int(m[4] or 0), int(m[5] or 0), int(m[6] or 0))
                except ValueError:
                    break
    return None


def _mail_time(mail: "db.MailRow") -> "datetime | None":
    return _parse_mail_time(mail.title_datetime or "", mail.received_at or "", mail.sent_at or "")


def _format_mail_date(mail: "db.MailRow", fmt: str) -> str:
    """日時の表示: ymdhm=2026/09/03 08:39, mdhm=9/3 08:39, raw=PDFの表記のまま。"""
    raw = mail.received_at or mail.sent_at or (mail.title_datetime or "").replace("T", " ")
    t = _mail_time(mail)
    if fmt == "raw" or t is None:
        return raw
    if fmt == "mdhm":
        return f"{t.month}/{t.day} {t:%H:%M}"
    return f"{t:%Y/%m/%d %H:%M}"


def _sort_mails(rows: list["db.MailRow"], sort: str) -> list["db.MailRow"]:
    """メール一覧の並び順(ブラウザ版と同じ6種類)。日時が読めないメールは常に末尾。"""
    by_pdf = sorted(rows, key=lambda r: (r.start_page, r.id))
    if sort == "pdf-desc":
        return by_pdf[::-1]
    if sort in ("date-desc", "date-asc"):
        dated = [r for r in by_pdf if _mail_time(r) is not None]
        undated = [r for r in by_pdf if _mail_time(r) is None]
        dated.sort(key=_mail_time, reverse=(sort == "date-desc"))
        return dated + undated
    if sort == "from":
        return sorted(by_pdf, key=lambda r: (not r.sender_short, _natural_key(r.sender_short)))
    if sort == "subject":
        return sorted(by_pdf, key=lambda r: _natural_key(parser.normalize_subject(r.subject)))
    return by_pdf


class SettingsPanel(QFrame):
    """一覧の上に開く「表示設定」パネル。ラベルと切り替えボタン(セグメント)を1行ずつ並べる。
    ボタンを押すと即座にViewSettingsへ保存され、各タブへ反映される。"""

    def __init__(self, title: str, settings: ViewSettings, on_close, parent=None):
        super().__init__(parent)
        self.setObjectName("settingsPanel")
        self._settings = settings
        self._segs: dict[str, list[tuple[object, QToolButton]]] = {}
        self._checks: dict[str, QCheckBox] = {}
        self._syncers = []

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 10, 12, 12)
        self._layout.setSpacing(3)
        head = QHBoxLayout()
        caption = QLabel(title)
        caption.setObjectName("settingsTitle")
        head.addWidget(caption)
        head.addStretch(1)
        close_button = QToolButton()
        close_button.setObjectName("smallButton")
        close_button.setText("閉じる")
        close_button.clicked.connect(on_close)
        head.addWidget(close_button)
        self._layout.addLayout(head)

        self._grid = QGridLayout()
        self._grid.setContentsMargins(0, 6, 0, 0)
        self._grid.setHorizontalSpacing(10)
        self._grid.setVerticalSpacing(6)
        self._grid.setColumnStretch(1, 1)
        self._layout.addLayout(self._grid)

    def add_seg(self, label: str, key: str, options: list[tuple[object, str]],
                force: bool = False) -> tuple[QLabel, QWidget]:
        caption = QLabel(label)
        caption.setObjectName("settingsLabel")
        line = self._grid.rowCount()
        self._grid.addWidget(caption, line, 0, Qt.AlignmentFlag.AlignVCenter)
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        group = QButtonGroup(holder)
        group.setExclusive(True)
        pairs = []
        for i, (value, text) in enumerate(options):
            button = QToolButton()
            button.setObjectName("segButton")
            button.setText(text)
            button.setCheckable(True)
            pos = ("only" if len(options) == 1 else "first" if i == 0
                   else "last" if i == len(options) - 1 else "mid")
            button.setProperty("segPos", pos)
            button.clicked.connect(lambda _checked=False, v=value: self._settings.set(key, v, force))
            group.addButton(button)
            row.addWidget(button)
            pairs.append((value, button))
        row.addStretch(1)
        self._grid.addWidget(holder, line, 1)
        self._segs[key] = pairs
        return caption, holder

    def add_checks(self, label: str, items: list[tuple[str, str]], columns: int = 3) -> QLabel:
        """チェックボックス群(items: [(設定キー, 表示名)])。"""
        caption = QLabel(label)
        caption.setObjectName("settingsLabel")
        line = self._grid.rowCount()
        self._grid.addWidget(caption, line, 0, Qt.AlignmentFlag.AlignTop)
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 2, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(2)
        for i, (key, text) in enumerate(items):
            box = QCheckBox(text)
            box.toggled.connect(lambda checked, k=key: self._settings.set(k, bool(checked)))
            grid.addWidget(box, i // columns, i % columns)
            self._checks[key] = box
        grid.setColumnStretch(columns, 1)
        self._grid.addWidget(holder, line, 1)
        return caption

    def add_widget(self, label: str, widget: QWidget) -> QLabel:
        caption = QLabel(label)
        caption.setObjectName("settingsLabel")
        line = self._grid.rowCount()
        self._grid.addWidget(caption, line, 0, Qt.AlignmentFlag.AlignVCenter)
        self._grid.addWidget(widget, line, 1, Qt.AlignmentFlag.AlignLeft)
        return caption

    def seg_buttons(self, key: str) -> list[tuple[object, QToolButton]]:
        return self._segs[key]

    def check_box(self, key: str) -> QCheckBox:
        return self._checks[key]

    def add_layout(self, layout, spacing: int = 6):
        self._layout.addSpacing(spacing)
        self._layout.addLayout(layout)

    def add_footer(self, on_reset):
        foot = QHBoxLayout()
        note = QLabel("設定は自動で保存されます")
        note.setObjectName("settingsNote")
        foot.addWidget(note)
        foot.addStretch(1)
        reset_button = QToolButton()
        reset_button.setObjectName("smallButton")
        reset_button.setText("初期設定に戻す")
        reset_button.clicked.connect(on_reset)
        foot.addWidget(reset_button)
        self.add_layout(foot, 4)

    def add_syncer(self, func):
        """設定値に合わせてパネルの見た目(項目の表示/無効化など)を変える処理を登録する。"""
        self._syncers.append(func)

    def sync(self):
        for key, pairs in self._segs.items():
            for value, button in pairs:
                button.setChecked(self._settings.get(key) == value)
        for key, box in self._checks.items():
            box.blockSignals(True)
            box.setChecked(bool(self._settings.get(key)))
            box.blockSignals(False)
        for func in self._syncers:
            func()


_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def _sanitize_filename(name: str, fallback: str = "PDF") -> str:
    name = _INVALID_FILENAME_CHARS.sub("_", (name or "").strip())
    return name or fallback



class MailGroupHeader:
    """件名グループ化モードで、同一件名(正規化後)のメールをまとめる見出し行。"""

    def __init__(self, key: str, title: str):
        self.key = key
        self.title = title
        self.mails: list[db.MailRow] = []
        self.collapsed = False


# ----------------------------------------------------------------- モデル
class MailListModel(QAbstractListModel):
    def __init__(self):
        super().__init__()
        self._all_rows: list[db.MailRow] = []
        self._entries: list = []  # db.MailRow または MailGroupHeader の混在リスト
        self._grouped = False
        self._collapsed: set[str] = set()  # 折りたたみ中のグループキー(件名グループ化トグル/再検索をまたいで保持)

    def set_rows(self, rows: list[db.MailRow], grouped: bool = False):
        self._all_rows = rows
        self._grouped = grouped
        self._rebuild_entries()

    def toggle_collapsed(self, key: str):
        if key in self._collapsed:
            self._collapsed.discard(key)
        else:
            self._collapsed.add(key)
        self._rebuild_entries()

    def expand_group(self, key: str):
        if key in self._collapsed:
            self._collapsed.discard(key)
            self._rebuild_entries()

    def is_group_collapsed(self, key: str) -> bool:
        return key in self._collapsed

    def _rebuild_entries(self):
        self.beginResetModel()
        entries: list = []
        if not self._grouped:
            entries = list(self._all_rows)
        else:
            groups: dict[str, MailGroupHeader] = {}
            order: list[str] = []
            for row in self._all_rows:
                if not row.is_mail:
                    order.append(f"__document_{row.id}")
                    groups[f"__document_{row.id}"] = MailGroupHeader(f"__document_{row.id}", row.subject)
                    groups[f"__document_{row.id}"].mails.append(row)
                    continue
                key = parser.normalize_subject(row.subject)
                header = groups.get(key)
                if header is None:
                    header = MailGroupHeader(key, key)
                    groups[key] = header
                    order.append(key)
                header.mails.append(row)
            for key in order:
                header = groups[key]
                if len(header.mails) < 2:
                    # 同じ件名が他に無ければグループ化する意味が無いのでそのまま1件表示する。
                    entries.append(header.mails[0])
                    continue
                header.mails.sort(key=lambda r: r.title_datetime or "")
                header.collapsed = key in self._collapsed
                entries.append(header)
                if not header.collapsed:
                    entries.extend(header.mails)
        self._entries = entries
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._entries)

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        if isinstance(self._entries[index.row()], MailGroupHeader):
            # 見出し行は選択不可(クリックは効くが一覧のハイライト対象にはしない)にする。
            return Qt.ItemFlag.ItemIsEnabled
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        entry = self._entries[index.row()]
        if isinstance(entry, MailGroupHeader):
            if role == Qt.DisplayRole:
                return entry.title
            if role == HEADER_ROLE:
                return entry
            return None
        row = entry
        if role == Qt.DisplayRole:
            return row.subject
        if role == MAIL_ROLE:
            return row
        if role == Qt.ToolTipRole:
            return (
                f"件名: {row.subject}\n差出人: {row.sender}\n宛先: {row.to_addr}"
                + (f"\nCC: {row.cc}" if row.cc else "")
                + (f"\n添付: {row.attachments}" if row.attachments else "")
            )
        return None

    def mail_at(self, row_idx: int) -> "db.MailRow | None":
        if not 0 <= row_idx < len(self._entries):
            return None
        entry = self._entries[row_idx]
        return entry if isinstance(entry, db.MailRow) else None

    def header_at(self, row_idx: int) -> "MailGroupHeader | None":
        if not 0 <= row_idx < len(self._entries):
            return None
        entry = self._entries[row_idx]
        return entry if isinstance(entry, MailGroupHeader) else None

    def all_rows(self) -> list["db.MailRow"]:
        return self._all_rows

    def first_mail_index(self) -> QModelIndex:
        for i, entry in enumerate(self._entries):
            if isinstance(entry, db.MailRow):
                return self.index(i, 0)
        return QModelIndex()

    def index_for_mail_id(self, mail_id: int) -> QModelIndex:
        for i, entry in enumerate(self._entries):
            if isinstance(entry, db.MailRow) and entry.id == mail_id:
                return self.index(i, 0)
        return QModelIndex()

    def is_empty(self) -> bool:
        return not self._all_rows

    def set_read(self, mail_id: int, read: bool = True):
        """既読/未読状態をメモリ上のRowにも反映し、該当行を再描画させる。"""
        for row in self._all_rows:
            if row.id == mail_id:
                row.is_read = read
                break
        idx = self.index_for_mail_id(mail_id)
        if idx.isValid():
            self.dataChanged.emit(idx, idx, [MAIL_ROLE])

    def unread_count(self) -> int:
        return sum(1 for row in self._all_rows if row.is_mail and not row.is_read)


def _wrap_lines(fm: QFontMetrics, text: str, width: int, max_lines: int) -> list[str]:
    """textを幅widthで折り返し、最大max_lines行にする(収まらない分は最終行を「…」で省略)。"""
    text = " ".join((text or "").split())
    lines: list[str] = []
    pos = 0
    while pos < len(text) and len(lines) < max_lines:
        if len(lines) == max_lines - 1:
            lines.append(fm.elidedText(text[pos:], Qt.TextElideMode.ElideRight, width))
            break
        end = pos + 1
        while end < len(text) and fm.horizontalAdvance(text[pos:end + 1]) <= width:
            end += 1
        # 英単語の途中で切れる場合は、直前の空白で改行する(日本語は文字単位で折り返す)
        if end < len(text) and text[end - 1].isascii() and text[end - 1].isalnum()                 and text[end].isascii() and text[end].isalnum():
            space = text.rfind(" ", pos, end)
            if space > pos:
                end = space + 1
        lines.append(text[pos:end].rstrip())
        pos = end
    return lines


class MailItemDelegate(QStyledItemDelegate):
    """メール1件の描画。ブラウザ版と同じ「カード」表示と「1行リスト」表示に対応し、
    表示する項目・文字サイズ・行の間隔・プレビュー行数・日時の形式は表示設定(mail_settings)に従う。"""

    ROW_HEIGHT = 104    # 高さ計算にビューを使わない場合(テスト等)の目安の行の高さ
    CARD_MARGIN_X = 8   # 一覧の左右の余白(カード表示)
    CHIP_GAP = 4
    CHIP_MAX_WIDTH = 220

    attachment_clicked = Signal(QModelIndex, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.view: QListView | None = None  # 行の幅・選択状態を知るために一覧ビューを持つ
        self.hover_chip: tuple[int, int] | None = None  # (row, chip_index)

    # ------------------------------------------------------------ 寸法
    @staticmethod
    def _fonts() -> dict:
        fs = mail_settings().get("fontSize")
        f_main = QFont(); f_main.setPixelSize(fs)
        f_bold = QFont(f_main); f_bold.setWeight(QFont.Weight.Bold)
        f_small = QFont(); f_small.setPixelSize(max(9, round(fs * 0.86)))
        f_page = QFont(); f_page.setPixelSize(max(9, round(fs * 0.8)))
        return {"fs": fs, "main": f_main, "bold": f_bold, "small": f_small, "page": f_page,
                "fm_main": QFontMetrics(f_main), "fm_bold": QFontMetrics(f_bold),
                "fm_small": QFontMetrics(f_small), "fm_page": QFontMetrics(f_page)}

    def is_selected(self, index: QModelIndex) -> bool:
        view = self.view
        return bool(view is not None and view.selectionModel() is not None
                    and view.selectionModel().isSelected(index))

    @staticmethod
    def _page_text(mail: "db.MailRow") -> str:
        return f"p.{mail.start_page}" + (f"–{mail.end_page}" if mail.end_page != mail.start_page else "")

    def _chips(self, mail: "db.MailRow", x: int, y: int, right: int, fonts: dict,
               wrap: bool = False) -> tuple[list[dict], int]:
        """添付ファイルのチップを並べ、(チップ一覧, 使った高さ)を返す。
        wrap=False: 1行に並べ、入り切らない分は「+N」にまとめる。
        wrap=True : 選択中のメール用。全件を折り返して並べる(長い名前も1行幅まで表示)。"""
        fm = fonts["fm_small"]
        h = fm.height() + 4
        chips = []
        attachments = mail.attachment_list()
        left, top = x, y
        max_w = max(40, right - left) if wrap else self.CHIP_MAX_WIDTH
        for i, att in enumerate(attachments):
            label = f"\U0001F4CE {att.name}"
            w = min(fm.horizontalAdvance(label) + 18, max_w)
            if wrap and x + w > right and x > left:
                x = left
                y += h + self.CHIP_GAP
            elif not wrap and x + w > right:
                remaining = attachments[i:]
                extra_label = f"+{len(remaining)}"
                extra_w = fm.horizontalAdvance(extra_label) + 20
                if x + extra_w <= right:
                    chips.append({"rect": QRect(x, y, extra_w, h), "attachment": None, "label": extra_label,
                                  "clickable": True, "kind": "overflow", "remaining": remaining})
                elif chips:  # 「+N」を置く余地が無ければ直前のチップと入れ替える
                    prev = chips.pop()
                    rest = ([prev["attachment"]] if prev["attachment"] else prev.get("remaining", [])) + remaining
                    chips.append({"rect": QRect(prev["rect"].x(), y, extra_w, h), "attachment": None,
                                  "label": f"+{len(rest)}", "clickable": True, "kind": "overflow",
                                  "remaining": rest})
                break
            # しおりと対応付けられない添付(start_page=None)は赤く表示し、押しても移動しない
            chips.append({"rect": QRect(x, y, w, h), "attachment": att, "label": label,
                          "clickable": att.start_page is not None, "kind": "attachment"})
            x += w + self.CHIP_GAP
        return chips, (y - top + h if attachments else 0)

    def layout(self, rect: QRect, mail: "db.MailRow", selected: bool) -> dict:
        """描画・クリック判定・ホバー判定・高さ計算で共有する配置計算。"""
        S = mail_settings()
        if S.get("layout") == "row":
            return self._layout_row(rect, mail, selected, S)
        if not mail.is_mail:
            return self._layout_document(rect, S)
        return self._layout_card(rect, mail, selected, S)

    # 旧来の配置計算と同じ呼び出し方(添付チップの位置)を使うコード・テスト向け
    def _row_positions(self, rect: QRect) -> tuple[int, int, int, int]:
        return rect.top(), rect.top(), rect.top(), rect.top()

    def _attachment_chips(self, rect: QRect, mail: "db.MailRow", _y: int = 0):
        """選択していない状態で描画したときの添付チップ(+Nを含む)と、その文字フォント。"""
        L = self.layout(rect, mail, False)
        return [c for c in L["chips"] if c["kind"] != "count"], L["fonts"]["small"]

    def _layout_document(self, rect: QRect, S) -> dict:
        """しおりを対象にした一覧で、メールと判定できないしおり(資料行)のカード。"""
        fonts = self._fonts()
        fs = fonts["fs"]
        compact = S.get("density") == "compact"
        gap = 3 if compact else 6
        pad_y = round(fs * (0.25 if compact else 0.5))
        pad_x = round(fs * (0.6 if compact else 0.72))
        card_left = rect.left() + self.CARD_MARGIN_X
        card_right = rect.right() - self.CARD_MARGIN_X
        top = rect.top() + gap // 2
        x0 = card_left + 3 + pad_x
        x1 = card_right - pad_x
        y = top + pad_y
        L = {"mode": "document", "fonts": fonts, "chips": [], "x0": x0, "x1": x1}
        h = fonts["fm_bold"].height()
        L["title"] = QRect(x0, y, max(20, x1 - x0), h)
        y += h
        h = fonts["fm_small"].height()
        L["detail"] = QRect(x0, y, max(20, x1 - x0), h)
        y += h
        bottom = y + pad_y
        L["card"] = QRect(card_left, top, card_right - card_left, bottom - top)
        L["height"] = bottom - rect.top() + (gap - gap // 2)
        return L

    def _layout_card(self, rect: QRect, mail: "db.MailRow", selected: bool, S) -> dict:
        F = lambda key: S.get(f"f_{key}")  # noqa: E731
        fonts = self._fonts()
        fs = fonts["fs"]
        compact = S.get("density") == "compact"
        gap = 3 if compact else 6                    # カード同士の間隔
        pad_y = round(fs * (0.25 if compact else 0.5))
        pad_x = round(fs * (0.6 if compact else 0.72))
        card_left = rect.left() + self.CARD_MARGIN_X
        card_right = rect.right() - self.CARD_MARGIN_X
        top = rect.top() + gap // 2
        x0 = card_left + 3 + pad_x
        x1 = card_right - pad_x
        width = max(20, x1 - x0)
        y = top + pad_y
        L = {"mode": "card", "fonts": fonts, "chips": [], "x0": x0, "x1": x1}

        if F("from") or F("date") or F("unread"):
            h = fonts["fm_bold"].height()
            L["r1"] = QRect(x0, y, width, h)
            y += h
        if F("subject"):
            h = fonts["fm_main"].height()
            L["subject"] = QRect(x0, y, width, h)
            y += h
        parts = []
        if F("to") and mail.to_addr:
            parts.append(f"宛先: {mail.to_addr}")
        if F("cc") and mail.cc:
            parts.append(f"CC: {mail.cc}")
        if parts:
            h = fonts["fm_small"].height()
            L["to"] = (QRect(x0, y, width, h), " ／ ".join(parts))
            y += h
        if F("preview") and mail.preview:
            fm = fonts["fm_small"]
            lines = _wrap_lines(fm, mail.preview, width, S.get("previewLines"))
            y += 1
            L["preview"] = [(QRect(x0, y + i * fm.height(), width, fm.height()), line) for i, line in enumerate(lines)]
            y += len(lines) * fm.height()
        page_w = fonts["fm_page"].horizontalAdvance(self._page_text(mail)) if F("pages") else 0
        if F("attach") and mail.attachments:
            y += max(3, round(fs * 0.3))
            right = x1 - (page_w + 8 if page_w else 0)
            # 選択中のカードは添付ファイルを全件表示する(選択していないカードは1行+「+N」)
            L["chips"], chips_h = self._chips(mail, x0, y, right, fonts, wrap=selected)
            if page_w:
                L["pages"] = QRect(x1 - page_w, y, page_w, fonts["fm_small"].height() + 4)
            y += chips_h
        elif page_w:
            h = fonts["fm_page"].height()
            L["pages"] = QRect(x1 - page_w, y, page_w, h)
            y += h
        bottom = y + pad_y
        L["card"] = QRect(card_left, top, card_right - card_left, bottom - top)
        L["height"] = bottom - rect.top() + (gap - gap // 2)
        return L

    def _layout_row(self, rect: QRect, mail: "db.MailRow", selected: bool, S) -> dict:
        F = lambda key: S.get(f"f_{key}")  # noqa: E731
        fonts = self._fonts()
        fs = fonts["fs"]
        compact = S.get("density") == "compact"
        pad_y = round(fs * (0.12 if compact else 0.32))
        col_gap = round(fs * 0.5)
        line_h = fonts["fm_main"].height()
        x = rect.left() + 3 + round(fs * 0.6)
        right = rect.right() - round(fs * 0.6)
        y = rect.top() + pad_y
        L = {"mode": "row", "fonts": fonts, "chips": [], "x0": x, "x1": right}

        def take(width_em: float) -> QRect:
            nonlocal x
            r = QRect(x, y, round(width_em * fs), line_h)
            x += r.width() + col_gap
            return r

        L["dot"] = QRect(x, y + (line_h - 8) // 2, 8, 8)
        x += 8 + col_gap
        if F("date"):
            L["date"] = take({"ymdhm": 8.6, "mdhm": 5.8, "raw": 11}.get(S.get("dateFmt"), 8.6))
        if F("from"):
            L["from"] = take(6.5)
        tail = (round(3.2 * fs) + col_gap if F("attach") else 0) + (round(4 * fs) + col_gap if F("pages") else 0)
        subj_w = max(20, right - x - tail)
        if F("subject"):
            L["subject"] = QRect(x, y, subj_w, line_h)
        x += subj_w + col_gap
        if F("attach"):
            att_rect = take(3.2)
            atts = mail.attachment_list()
            first = next((a for a in atts if a.start_page is not None), None)
            if atts:
                # しおりと対応付けられない添付があれば「📎移動できる数/全体」を赤で示す
                linked = sum(1 for a in atts if a.start_page is not None)
                count = f"{len(atts)}" if linked == len(atts) else f"{linked}/{len(atts)}"
                L["chips"].append({"rect": att_rect, "attachment": first, "label": f"\U0001F4CE{count}",
                                   "clickable": first is not None, "kind": "count",
                                   "unlinked": linked < len(atts), "atts": atts})
        if F("pages"):
            L["pages"] = QRect(right - round(4 * fs), y, round(4 * fs), line_h)
        y += line_h
        # 選択中の行だけ、下に添付ファイル名を並べる(ブラウザ版と同じ)
        if selected and F("attach") and mail.attachments:
            y += 2
            chips, chips_h = self._chips(mail, L["x0"] + 14, y, right, fonts, wrap=True)
            L["chips"] += chips
            y += chips_h
        L["height"] = y + pad_y - rect.top() + 1
        return L

    def sizeHint(self, option, index):
        width = self.view.viewport().width() if self.view is not None else max(200, option.rect.width())
        if index.data(HEADER_ROLE) is not None:
            return QSize(width, self._fonts()["fm_small"].height() + 14)
        mail: db.MailRow | None = index.data(MAIL_ROLE)
        if mail is None:
            return super().sizeHint(option, index)
        return QSize(width, self.layout(QRect(0, 0, width, 0), mail, self.is_selected(index))["height"])

    # ------------------------------------------------------------ 描画
    def paint(self, painter, option, index):
        header: MailGroupHeader | None = index.data(HEADER_ROLE)
        if header is not None:
            self._paint_header(painter, option, header)
            return

        mail: db.MailRow | None = index.data(MAIL_ROLE)
        if mail is None:
            return super().paint(painter, option, index)

        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        S = mail_settings()
        L = self.layout(rect, mail, selected)
        fonts = L["fonts"]
        unread = mail.is_mail and not mail.is_read

        painter.fillRect(rect, QColor(C_PANEL))
        bg = QColor(C_ACCENT_BG if selected else C_PANEL2 if hovered else C_PANEL)
        if L["mode"] in ("card", "document"):
            card = QRectF(L["card"]).adjusted(0.5, 0.5, -0.5, -0.5)
            painter.setPen(QPen(QColor(C_ACCENT_LINE if selected else C_LINE), 1))
            painter.setBrush(QBrush(bg))
            painter.drawRoundedRect(card, 8, 8)
            if selected:  # 選択中は左端に青い線(ブラウザ版の border-left と同じ)
                painter.save()
                painter.setClipRect(QRectF(card.left(), card.top(), 3, card.height()))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(C_ACCENT))
                painter.drawRoundedRect(card, 8, 8)
                painter.restore()
        else:
            painter.fillRect(rect, bg)
            painter.fillRect(QRect(rect.left(), rect.bottom(), rect.width(), 1),
                             QColor(C_ACCENT_LINE if selected else C_LINE))
            if selected:
                painter.fillRect(QRect(rect.left(), rect.top(), 3, rect.height()), QColor(C_ACCENT))

        date_text = (_format_mail_date(mail, S.get("dateFmt"))
                     if S.get("f_date") and mail.is_mail else "")

        if L["mode"] == "document":
            # 資料行: しおりのタイトルと「資料 · ページ範囲」
            painter.setFont(fonts["bold"])
            painter.setPen(QColor(C_ACCENT if selected else C_TEXT))
            painter.drawText(L["title"], Qt.AlignmentFlag.AlignVCenter,
                             fonts["fm_bold"].elidedText(mail.subject or "(見出しなし)",
                                                         Qt.TextElideMode.ElideRight, L["title"].width()))
            painter.setFont(fonts["small"])
            painter.setPen(QColor(C_SUB))
            painter.drawText(L["detail"], Qt.AlignmentFlag.AlignVCenter,
                             f"資料  ·  {mail.start_page}–{mail.end_page} ページ")
            painter.restore()
            return

        if L["mode"] == "card":
            if "r1" in L:
                r1 = L["r1"]
                x = r1.left()
                if S.get("f_unread"):
                    if unread:
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.setBrush(QColor(C_ACCENT))
                        painter.drawEllipse(QRect(x, r1.center().y() - 3, 8, 8))
                    x += 14
                date_w = 0
                if date_text:
                    painter.setFont(fonts["small"])
                    painter.setPen(QColor(C_FAINT))
                    date_w = fonts["fm_small"].horizontalAdvance(date_text)
                    painter.drawText(QRect(r1.right() - date_w, r1.top(), date_w, r1.height()),
                                     Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, date_text)
                if S.get("f_from"):
                    painter.setFont(fonts["bold"])
                    painter.setPen(QColor(C_TEXT))
                    from_rect = QRect(x, r1.top(), max(10, r1.right() - date_w - 8 - x), r1.height())
                    painter.drawText(from_rect, Qt.AlignmentFlag.AlignVCenter,
                                     fonts["fm_bold"].elidedText(mail.sender_short or "(差出人不明)",
                                                                 Qt.TextElideMode.ElideRight, from_rect.width()))
            if "subject" in L:
                f = QFont(fonts["main"])
                f.setWeight(QFont.Weight.Bold if unread else QFont.Weight.Medium)
                painter.setFont(f)
                painter.setPen(QColor(C_TEXT))
                r = L["subject"]
                painter.drawText(r, Qt.AlignmentFlag.AlignVCenter,
                                 QFontMetrics(f).elidedText(mail.subject or "(件名なし)",
                                                            Qt.TextElideMode.ElideRight, r.width()))
            painter.setFont(fonts["small"])
            painter.setPen(QColor(C_SUB))
            if "to" in L:
                r, text = L["to"]
                painter.drawText(r, Qt.AlignmentFlag.AlignVCenter,
                                 fonts["fm_small"].elidedText(text, Qt.TextElideMode.ElideRight, r.width()))
            for r, line in L.get("preview", []):
                painter.drawText(r, Qt.AlignmentFlag.AlignVCenter, line)
        else:
            if S.get("f_unread") and unread:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(C_ACCENT))
                painter.drawEllipse(L["dot"])
            if "date" in L:
                painter.setFont(fonts["small"])
                painter.setPen(QColor(C_FAINT))
                painter.drawText(L["date"], Qt.AlignmentFlag.AlignVCenter,
                                 fonts["fm_small"].elidedText(date_text, Qt.TextElideMode.ElideRight, L["date"].width()))
            if "from" in L:
                painter.setFont(fonts["bold"])
                painter.setPen(QColor(C_TEXT if mail.is_mail else C_SUB))
                sender = (mail.sender_short or "(差出人不明)") if mail.is_mail else "資料"
                painter.drawText(L["from"], Qt.AlignmentFlag.AlignVCenter,
                                 fonts["fm_bold"].elidedText(sender, Qt.TextElideMode.ElideRight,
                                                             L["from"].width()))
            if "subject" in L:
                f = QFont(fonts["main"])
                f.setWeight(QFont.Weight.Bold if unread else QFont.Weight.Medium)
                painter.setFont(f)
                painter.setPen(QColor(C_TEXT))
                painter.drawText(L["subject"], Qt.AlignmentFlag.AlignVCenter,
                                 QFontMetrics(f).elidedText(mail.subject or "(件名なし)",
                                                            Qt.TextElideMode.ElideRight, L["subject"].width()))

        if "pages" in L:
            painter.setFont(fonts["page"])
            painter.setPen(QColor(C_FAINT))
            painter.drawText(L["pages"], Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                             self._page_text(mail))

        self._paint_chips(painter, L, index.row())
        painter.restore()

    def _paint_chips(self, painter, L: dict, row: int):
        fonts = L["fonts"]
        painter.setFont(fonts["small"])
        fm = fonts["fm_small"]
        for i, chip in enumerate(L["chips"]):
            clickable = chip["clickable"]
            is_hovered = clickable and self.hover_chip == (row, i)
            if chip["kind"] == "count":  # 1行リストの「📎N」(移動できない添付があれば「📎移動できる数/全体」を赤で)
                painter.setPen(QColor(C_ACCENT if is_hovered else C_UNLINKED if chip.get("unlinked") else C_SUB))
                painter.drawText(chip["rect"], Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, chip["label"])
                continue
            if chip["kind"] == "attachment" and not clickable:
                # しおりと対応付けられない添付: 赤いラベル(押しても移動しない)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(QColor(C_UNLINKED_BG)))
                painter.drawRoundedRect(chip["rect"], 6, 6)
                painter.setPen(QColor(C_UNLINKED))
                inner = chip["rect"].adjusted(9, 0, -9, 0)
                painter.drawText(inner, Qt.AlignmentFlag.AlignVCenter,
                                 fm.elidedText(chip["label"], Qt.TextElideMode.ElideRight, inner.width()))
                continue
            # ブラウザ版の .chip と同じ丸いラベル。マウスが乗ると枠と文字が青くなる
            draw_rect = QRectF(chip["rect"]).adjusted(0.5, 0.5, -0.5, -0.5)
            radius = draw_rect.height() / 2
            painter.setPen(QPen(QColor(C_ACCENT if is_hovered else C_LINE2), 1))
            painter.setBrush(QBrush(QColor(C_PANEL if is_hovered else C_PANEL2)))
            painter.drawRoundedRect(draw_rect, radius, radius)
            painter.setPen(QColor(C_ACCENT if is_hovered else C_TEXT))
            inner = chip["rect"].adjusted(9, 0, -9, 0)
            painter.drawText(inner, Qt.AlignmentFlag.AlignVCenter,
                             fm.elidedText(chip["label"], Qt.TextElideMode.ElideRight, inner.width()))

    def _paint_header(self, painter, option, header: "MailGroupHeader"):
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        rect = option.rect
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        fonts = self._fonts()

        painter.fillRect(rect, QColor(C_PANEL))
        if hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(C_PANEL2))
            painter.drawRoundedRect(QRectF(rect.adjusted(self.CARD_MARGIN_X, 2, -self.CARD_MARGIN_X, -2)), 6, 6)

        painter.setFont(fonts["main"])
        painter.setPen(QColor(C_FAINT))
        painter.drawText(QRect(rect.left() + 12, rect.top(), 16, rect.height()), Qt.AlignmentFlag.AlignCenter,
                         "▾" if not header.collapsed else "▸")

        # 右端: 未読数(青)と件数
        right = rect.right() - 12
        f_small, fm_small = fonts["small"], fonts["fm_small"]
        painter.setFont(f_small)
        count_text = f"{len(header.mails)}件"
        count_w = fm_small.horizontalAdvance(count_text)
        painter.setPen(QColor(C_SUB))
        painter.drawText(QRect(right - count_w, rect.top(), count_w, rect.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, count_text)
        right -= count_w + 8
        unread = sum(1 for m in header.mails if m.is_mail and not m.is_read)
        if unread:
            unread_text = f"未読{unread}"
            unread_w = fm_small.horizontalAdvance(unread_text)
            painter.setPen(QColor(C_ACCENT))
            painter.drawText(QRect(right - unread_w, rect.top(), unread_w, rect.height()),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, unread_text)
            right -= unread_w + 8

        f_title = QFont(f_small); f_title.setBold(True)
        painter.setFont(f_title)
        painter.setPen(QColor(C_TEXT))
        title_rect = QRect(rect.left() + 32, rect.top(), max(10, right - (rect.left() + 32)), rect.height())
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignVCenter,
                         QFontMetrics(f_title).elidedText(header.title or "(件名なし)", Qt.TextElideMode.ElideRight,
                                                          title_rect.width()))
        painter.restore()

    # ------------------------------------------------------------ 操作
    def chips_at(self, rect: QRect, index: QModelIndex) -> list[dict]:
        mail: db.MailRow | None = index.data(MAIL_ROLE)
        if mail is None or not mail.attachments:
            return []
        return self.layout(rect, mail, self.is_selected(index))["chips"]

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            for chip in self.chips_at(option.rect, index):
                if chip["clickable"] and chip["rect"].contains(pos):
                    if chip.get("kind") == "overflow":
                        self._show_overflow_menu(chip, option, index)
                    else:
                        self.attachment_clicked.emit(index, chip["attachment"].start_page)
                    return True
        return False

    def _show_overflow_menu(self, chip: dict, option, index: QModelIndex):
        """「+N」チップを押したときに、表示しきれなかった添付ファイルを一覧表示するポップアップ。"""
        menu = QMenu()
        for att in chip["remaining"]:
            page = att.start_page
            if page is not None:
                label = f"\U0001F4CE {att.name}"
            else:
                label = f"\U0001F4CE {att.name}（対応するしおりが見つかりません）"
            action = menu.addAction(label)
            action.setEnabled(page is not None)
            if page is not None:
                action.triggered.connect(
                    lambda checked=False, p=page: self.attachment_clicked.emit(index, p))

        widget = option.widget
        anchor = chip["rect"].bottomLeft()
        global_pos = widget.mapToGlobal(anchor) if widget is not None else QCursor.pos()
        menu.exec(global_pos)


# -------------------------------------------------------------------- 一覧
class MailListView(QListView):
    """添付チップのホバー検出(カーソル変更・強調表示)を行うQListView。"""

    def __init__(self, delegate: MailItemDelegate, parent=None):
        super().__init__(parent)
        self._delegate = delegate
        delegate.view = self
        self.setMouseTracking(True)

    def _unlinked_chip_at(self, pos) -> bool:
        index = self.indexAt(pos)
        if not index.isValid():
            return False
        return any(chip["kind"] == "attachment" and not chip["clickable"] and chip["rect"].contains(pos)
                   for chip in self._delegate.chips_at(self.visualRect(index), index))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._unlinked_chip_at(event.position().toPoint()):
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._unlinked_chip_at(event.position().toPoint()):
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        self._update_hover(event.position().toPoint())

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._set_hover(None)
        self.viewport().unsetCursor()

    def _update_hover(self, pos):
        index = self.indexAt(pos)
        new_hover = None
        tip = ""
        if index.isValid():
            for i, chip in enumerate(self._delegate.chips_at(self.visualRect(index), index)):
                if chip["rect"].contains(pos):
                    tip = self._chip_tooltip(chip)
                    if chip["clickable"]:
                        new_hover = (index.row(), i)
                    break
        if new_hover != self._delegate.hover_chip or tip:
            if tip:
                QToolTip.showText(self.viewport().mapToGlobal(pos), tip, self.viewport())
            else:
                QToolTip.hideText()
        self._set_hover(new_hover)
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if new_hover else Qt.CursorShape.ArrowCursor)

    @staticmethod
    def _chip_tooltip(chip: dict) -> str:
        if chip["kind"] == "overflow":
            return "ほかの添付ファイルを表示"
        if chip["kind"] == "count" and chip.get("unlinked"):
            unlinked = [a.name for a in chip["atts"] if a.start_page is None]
            return ("次の添付ファイルは対応するしおりが見つからないため、移動できません:\n"
                    + "\n".join(f"・{n}" for n in unlinked))
        att = chip["attachment"]
        if att is None:
            return ""
        if att.start_page is None:
            return f"{att.name}\n対応するしおりが見つからないため、移動できません"
        end = att.end_page or att.start_page
        pages = f"p.{att.start_page}" + (f"–{end}" if end != att.start_page else "")
        return f"{att.name}（{pages}）へ移動"

    def _set_hover(self, value: tuple[int, int] | None):
        if self._delegate.hover_chip != value:
            self._delegate.hover_chip = value
            self.viewport().update()


# ------------------------------------------------------ 資料PDF用モデル・デリゲート
class SectionTreeModel(QStandardItemModel):
    """一般資料PDFのしおり階層を表すモデル。全件は階層ツリー、検索結果はフラット表示で使う。"""

    def __init__(self):
        super().__init__()
        self.setColumnCount(1)
        self._flat = False

    def set_tree(self, rows: list["db.SectionRow"], sort_by_title: bool = False):
        """しおりを階層ツリーとして並べる。sort_by_title=Trueなら同じ階層の中で名前順にする
        (数字は数の大きさで比較)。Falseなら元PDFのしおりの順番のまま。"""
        self.clear()
        self.setColumnCount(1)
        self._flat = False
        ids = {row.id for row in rows}
        children: dict[int | None, list["db.SectionRow"]] = {}
        for row in rows:  # start_page昇順 = 常に親が子より先に来る
            parent_id = row.parent_id if row.parent_id in ids else None
            children.setdefault(parent_id, []).append(row)
        if sort_by_title:
            for siblings in children.values():
                siblings.sort(key=lambda r: _natural_key(r.title))

        def add(parent_item: QStandardItem, parent_id: int | None):
            for row in children.get(parent_id, []):
                item = QStandardItem(row.title)
                item.setEditable(False)
                item.setToolTip(row.title)
                item.setData(row, SECTION_ROLE)
                parent_item.appendRow(item)
                add(item, row.id)

        add(self.invisibleRootItem(), None)

    def set_flat(self, rows: list["db.SectionRow"]):
        self.clear()
        self.setColumnCount(1)
        self._flat = True
        root = self.invisibleRootItem()
        for row in rows:
            item = QStandardItem(row.title)
            item.setEditable(False)
            item.setToolTip(f"{row.path_titles}  ›  {row.title}" if row.path_titles else row.title)
            item.setData(row, SECTION_ROLE)
            root.appendRow(item)

    def is_flat(self) -> bool:
        """検索結果のフラット表示中か(=ツリー階層を無視してパンくずで文脈を示すべきか)。"""
        return self._flat

    def is_empty(self) -> bool:
        return self.rowCount() == 0

    def section_at(self, index: QModelIndex) -> "db.SectionRow | None":
        item = self.itemFromIndex(index)
        return item.data(SECTION_ROLE) if item else None


WRAP_TEXT_FLAGS = (Qt.AlignmentFlag.AlignLeft.value | Qt.AlignmentFlag.AlignTop.value
                   | Qt.TextFlag.TextWrapAnywhere.value)


class SectionItemDelegate(QStyledItemDelegate):
    """しおり1件(見出し+ページ範囲)を描画する。文字サイズ・行の間隔・長い名前の折り返し・
    ページ番号の表示は「表示設定」(BookmarkViewSettings)に従う。"""

    PAD_LEFT = 6
    PAD_RIGHT = 10
    PAGE_GAP = 8

    def __init__(self, view: QTreeView, parent=None):
        super().__init__(parent)
        self._view = view

    def _layout(self, index: QModelIndex, width: int) -> dict:
        """paint()とsizeHint()で共有する寸法計算。"""
        S = bookmark_settings()
        section: db.SectionRow = index.data(SECTION_ROLE)
        fs = S.get("fontSize")
        f_title = QFont(); f_title.setPixelSize(fs)
        f_small = QFont(); f_small.setPixelSize(max(9, round(fs * 0.8)))
        fm_title, fm_small = QFontMetrics(f_title), QFontMetrics(f_small)
        pad_y = 1 if S.get("density") == "compact" else max(4, round(fm_title.height() * 0.25))

        page_text = (f"{section.start_page}" if section.start_page == section.end_page
                     else f"{section.start_page}-{section.end_page}")
        page_w = fm_small.horizontalAdvance(page_text) if S.get("pages") else 0
        title_w = max(10, width - self.PAD_LEFT - self.PAD_RIGHT - (page_w + self.PAGE_GAP if page_w else 0))

        model = index.model()
        crumb = bool(section.path_titles) and isinstance(model, SectionTreeModel) and model.is_flat()
        wrap = S.get("wrap")
        line_h = fm_title.height()
        if wrap:
            title_h = max(line_h, fm_title.boundingRect(QRect(0, 0, title_w, 100000), WRAP_TEXT_FLAGS,
                                                        section.title or "").height())
            crumb_h = fm_small.height() if crumb else 0  # 折り返し時はパンくずを見出しの上の行に出す
        else:
            title_h, crumb_h = line_h, 0
        return {
            "section": section, "f_title": f_title, "f_small": f_small, "fm_title": fm_title,
            "fm_small": fm_small, "pad_y": pad_y, "page_text": page_text, "page_w": page_w,
            "crumb": crumb, "wrap": wrap, "line_h": line_h, "title_h": title_h, "crumb_h": crumb_h,
            "height": pad_y * 2 + crumb_h + title_h,
        }

    def _item_width(self, index: QModelIndex) -> int:
        """ツリー内での字下げを除いた、この行の描画幅(sizeHintの時点ではoption.rectが当てにならないため)。"""
        depth = 0
        parent = index.parent()
        while parent.isValid():
            depth += 1
            parent = parent.parent()
        view = self._view
        indent = view.indentation() * (depth + (1 if view.rootIsDecorated() else 0))
        return max(60, view.viewport().width() - indent)

    def sizeHint(self, option, index):
        if index.data(SECTION_ROLE) is None:
            return super().sizeHint(option, index)
        width = self._item_width(index)
        return QSize(width, self._layout(index, width)["height"])

    def paint(self, painter, option, index):
        if index.data(SECTION_ROLE) is None:
            return super().paint(painter, option, index)

        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        L = self._layout(index, rect.width())
        section = L["section"]

        painter.fillRect(rect, QColor(C_PANEL))
        if selected or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(C_ACCENT_BG if selected else C_PANEL2))
            painter.drawRoundedRect(QRectF(rect.adjusted(0, 1, -4, -1)), 6, 6)

        left = rect.left() + self.PAD_LEFT
        right = rect.right() - self.PAD_RIGHT
        top = rect.top() + L["pad_y"]
        title_right = right - (L["page_w"] + self.PAGE_GAP if L["page_w"] else 0)

        if L["page_w"]:
            painter.setFont(L["f_small"])
            painter.setPen(QColor(C_FAINT))
            page_rect = QRect(right - L["page_w"], top + L["crumb_h"], L["page_w"], L["line_h"])
            painter.drawText(page_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, L["page_text"])

        f_title = L["f_title"]
        f_title.setWeight(QFont.Weight.DemiBold if selected else QFont.Weight.Normal)
        title_color = QColor(C_ACCENT if selected else C_TEXT)

        if L["wrap"]:
            if L["crumb"]:
                painter.setFont(L["f_small"])
                painter.setPen(QColor(C_FAINT))
                crumb_rect = QRect(left, top, max(10, right - left), L["crumb_h"])
                painter.drawText(crumb_rect, Qt.AlignmentFlag.AlignVCenter,
                                  L["fm_small"].elidedText(section.path_titles, Qt.TextElideMode.ElideLeft,
                                                           crumb_rect.width()))
                top += L["crumb_h"]
            painter.setFont(f_title)
            painter.setPen(title_color)
            painter.drawText(QRect(left, top, max(10, title_right - left), L["title_h"]), WRAP_TEXT_FLAGS,
                              section.title or "")
        else:
            x = left
            if L["crumb"]:
                fm_bc = L["fm_small"]
                max_bc_w = max(0, int((title_right - left) * 0.45))
                bc_text = fm_bc.elidedText(section.path_titles + "  ›  ", Qt.TextElideMode.ElideLeft, max_bc_w)
                painter.setFont(L["f_small"])
                painter.setPen(QColor(C_FAINT))
                bc_w = fm_bc.horizontalAdvance(bc_text)
                painter.drawText(QRect(x, rect.top(), bc_w, rect.height()), Qt.AlignmentFlag.AlignVCenter, bc_text)
                x += bc_w
            painter.setFont(f_title)
            painter.setPen(title_color)
            title_rect = QRect(x, rect.top(), max(10, title_right - x), rect.height())
            painter.drawText(title_rect, Qt.AlignmentFlag.AlignVCenter,
                              QFontMetrics(f_title).elidedText(section.title, Qt.TextElideMode.ElideRight,
                                                               title_rect.width()))

        painter.restore()


class SectionTreeView(QTreeView):
    """しおり一覧のツリー。折り返し表示中は幅が変わると各行の高さも変わるため、
    リサイズが落ち着いたところで行の高さを計算し直す。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._relayout_timer = QTimer(self)
        self._relayout_timer.setSingleShot(True)
        self._relayout_timer.setInterval(60)
        self._relayout_timer.timeout.connect(self.doItemsLayout)

    def resizeEvent(self, event):  # ビューポートのリサイズ時に呼ばれる
        super().resizeEvent(event)
        if bookmark_settings().get("wrap") and event.size().width() != event.oldSize().width():
            self._relayout_timer.start()


# ------------------------------------------------------------ リンクを踏めるPDF表示
# 本文中に文字として書かれただけのURL・メールアドレス(PDFにリンクとして埋め込まれていないもの)
_TEXT_LINK_RE = re.compile(
    r"(?P<url>(?:https?://|www\.)[A-Za-z0-9\-._~:/?#\[\]@!$&'*+,;=%]+)"
    r"|(?P<mail>[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+)")
_OPENABLE_SCHEMES = {"http", "https", "mailto", "file"}


class PdfLinkInfo:
    """PDF内の1つのリンク。rectsはページ内の座標(ポイント、左上原点)。"""

    def __init__(self, rects: list[QRectF], url: QUrl | None = None, page: int = -1,
                 location: QPointF | None = None):
        self.rects = self._merge_lines(rects)
        self.url = url
        self.page = page
        self.location = location or QPointF(0, 0)

    @staticmethod
    def _merge_lines(rects: list[QRectF]) -> list[QRectF]:
        """PDFからは文字のかたまりごと(ハイフンは高さ1ptほど)の細切れの矩形で返るため、
        同じ行のものを1つにまとめる。まとめないと強調表示やクリック範囲が途切れて見える。"""
        lines: list[QRectF] = []
        for r in sorted(rects, key=lambda r: (r.center().y(), r.x())):
            for i, line in enumerate(lines):
                if line.top() - 2 <= r.center().y() <= line.bottom() + 2:
                    lines[i] = line.united(r)
                    break
            else:
                lines.append(QRectF(r))
        return lines

    def label(self) -> str:
        if self.url is not None:
            text = self.url.toString()
            return text[len("mailto:"):] if text.startswith("mailto:") else text
        return f"{self.page + 1} ページへ移動"


class LinkedPdfView(QPdfView):
    """本文中のリンクをクリックできるQPdfView(ブラウザ版のリンク層と同じ働き)。

    - PDFに埋め込まれたリンク(Webページ・メールアドレス・ファイル・PDF内の別ページ)
    - 本文に文字で書かれているだけのURL・メールアドレス
    をクリックできるようにする。Webページ・メールアドレス・ファイルはOSの既定のアプリで開く
    (ファイルリンクはPDFのフォルダ基準の相対パスや、文字化けした日本語のパスにも対応)。
    マウスを乗せると指カーソルになり、リンク先をツールチップとステータスバーに表示する。
    """

    DRAG_THRESHOLD = 4  # これ以上マウスが動いたらクリックではなくドラッグとみなす

    def __init__(self, pdf_path: str, parent=None):
        super().__init__(parent)
        self.pdf_path = pdf_path
        self._links: dict[int, list[PdfLinkInfo]] = {}  # ページ番号 -> リンク(必要になったページだけ読む)
        self.link_model = QPdfLinkModel(self)
        self._hover: tuple[int, PdfLinkInfo] | None = None
        self._press_pos: QPointF | None = None
        self._press_hit: tuple[int, PdfLinkInfo] | None = None
        self.viewport().setMouseTracking(True)
        self.documentChanged.connect(self._reset_links)
        # 表示の大きさが変わっても見ていたページを保つため、スクロールのたびに位置を覚えておく
        self._last_anchor: tuple[int, float] | None = None
        self.verticalScrollBar().valueChanged.connect(self._remember_anchor)

    def _reset_links(self, *_args):
        self._links.clear()
        self._hover = None
        doc = self.document()
        self.link_model.setDocument(doc)
        if doc is not None:
            doc.statusChanged.connect(self._reset_link_cache)

    def _reset_link_cache(self, *_args):
        self._links.clear()
        self._hover = None
        # QPdfLinkModelは読み込み完了を自動では拾わないため、文書を設定し直して読み直させる
        doc = self.document()
        if doc is not None and doc.status() == QPdfDocument.Status.Ready:
            self.link_model.setDocument(None)
            self.link_model.setDocument(doc)

    # ------------------------------------------------------------ 座標の対応
    def _page_rects(self) -> list[QRect]:
        """各ページの表示位置(スクロールを含む内容全体での座標)。QPdfView内部のページ配置と同じ計算。"""
        doc = self.document()
        if doc is None or doc.pageCount() <= 0:
            return []
        margins = self.documentMargins()
        spacing = self.pageSpacing()
        viewport = self.viewport().size()
        screen = self.screen() or QGuiApplication.primaryScreen()
        resolution = screen.logicalDotsPerInch() / 72.0
        mode = self.zoomMode()
        count = doc.pageCount()
        single = self.pageMode() == QPdfView.PageMode.SinglePage
        pages = [self.pageNavigator().currentPage()] if single else range(count)

        sizes: dict[int, QSize] = {}
        for page in pages:
            points = doc.pagePointSize(page)
            if mode == QPdfView.ZoomMode.FitToWidth:
                size = QSizeF(points * resolution).toSize()
                if size.width() <= 0:
                    size = QSize(1, 1)
                factor = (viewport.width() - margins.left() - margins.right()) / size.width()
                size = QSize(round(size.width() * factor), round(size.height() * factor))
            elif mode == QPdfView.ZoomMode.FitInView:
                available = QSize(viewport.width() - margins.left() - margins.right(), viewport.height() - spacing)
                size = QSizeF(points * resolution).toSize().scaled(available, Qt.AspectRatioMode.KeepAspectRatio)
            else:
                size = QSizeF(points * resolution * self.zoomFactor()).toSize()
            sizes[page] = size
        total_width = max((s.width() for s in sizes.values()), default=0) + margins.left() + margins.right()

        rects = [QRect() for _ in range(count)]
        y = margins.top()
        for page in pages:
            size = sizes[page]
            x = (max(total_width, viewport.width()) - size.width()) // 2
            rects[page] = QRect(QPoint(x, y), size)
            y += size.height() + spacing
        return rects

    # ------------------------------------------------------------ 表示の切り替え
    def _remember_anchor(self, *_args):
        self._last_anchor = self._view_anchor()

    def resizeEvent(self, event):  # ビューポートの大きさが変わったとき
        """ウインドウの大きさ・一覧の表示/非表示・全画面の切り替えなどで表示の大きさが変わっても、
        見ていたページの位置を保つ。QPdfViewはスクロール位置をピクセルのまま残すため、
        そのままだと別のページに移ってしまう。"""
        anchor = self._last_anchor
        super().resizeEvent(event)
        if anchor is not None and event.size() != event.oldSize():
            self._restore_anchor(anchor)
            # ページ配置の確定が次のイベントループになる場合に備えて、もう一度合わせる
            QTimer.singleShot(0, lambda a=anchor: self._restore_anchor(a))

    def setZoomMode(self, mode):
        """表示(幅に合わせる/ページ全体)を切り替えても、見ていたページの位置を保つ。
        QPdfViewはスクロール位置をピクセルのまま残すため、そのままだと別のページに飛んでしまう。"""
        if mode == self.zoomMode():
            return
        anchor = self._view_anchor()
        super().setZoomMode(mode)
        if anchor is not None:
            # 新しい表示倍率でのページ配置が確定してから戻す
            QTimer.singleShot(0, lambda: self._restore_anchor(anchor))

    def _view_anchor(self) -> tuple[int, float] | None:
        """いま画面の上端にあるページと、そのページ内の位置(0=上端〜1=下端)。"""
        rects = self._page_rects()
        # ページの上には余白があるため、余白の分だけ下を基準にする(_restore_anchorと対称)
        top = self.verticalScrollBar().value() + self.documentMargins().top()
        for page, rect in enumerate(rects):
            if rect.isNull():
                continue
            if top < rect.bottom() + self.pageSpacing():
                return page, min(1.0, max(0.0, (top - rect.top()) / max(1, rect.height())))
        return None

    def _restore_anchor(self, anchor: tuple[int, float]):
        page, fraction = anchor
        rects = self._page_rects()
        if not 0 <= page < len(rects) or rects[page].isNull():
            return
        rect = rects[page]
        if self.zoomMode() == QPdfView.ZoomMode.FitInView:
            fraction = 0.0  # ページ全体表示では、そのページがちょうど画面に収まるよう上端に合わせる
        value = rect.top() + round(fraction * rect.height()) - self.documentMargins().top()
        self.verticalScrollBar().setValue(max(0, value))

    def _scroll_offset(self) -> QPointF:
        return QPointF(self.horizontalScrollBar().value(), self.verticalScrollBar().value())

    def _to_view(self, page_rect: QRect, page: int, rect: QRectF) -> QRectF:
        """ページ内の座標(ポイント)を、ビューポート上の座標に変換する。"""
        points = self.document().pagePointSize(page)
        sx = page_rect.width() / points.width() if points.width() else 1
        sy = page_rect.height() / points.height() if points.height() else 1
        offset = self._scroll_offset()
        return QRectF(page_rect.x() + rect.x() * sx - offset.x(), page_rect.y() + rect.y() * sy - offset.y(),
                      rect.width() * sx, rect.height() * sy)

    # ------------------------------------------------------------ リンクの読み込み
    def _links_on_page(self, page: int) -> list[PdfLinkInfo]:
        if page in self._links:
            return self._links[page]
        doc = self.document()
        links: list[PdfLinkInfo] = []
        seen: set[tuple] = set()
        # 1) PDFに埋め込まれたリンク
        self.link_model.setPage(page)
        for row in range(self.link_model.rowCount(QModelIndex())):
            link = self.link_model.data(self.link_model.index(row, 0), QPdfLinkModel.Role.Link.value)
            if link is None:  # isValid()はWebリンクだとFalseになるため使わない
                continue
            rects = [QRectF(r) for r in link.rectangles() if r.width() > 0 and r.height() > 0]
            if not rects:
                continue
            url = link.url()
            if not url.isEmpty():
                # 文字化けした日本語のファイルリンクはQUrlとして不正でも、開くときに元の文字列から復元する
                if url.scheme().lower() not in _OPENABLE_SCHEMES:
                    continue
                info = PdfLinkInfo(rects, url=url)
            elif link.page() >= 0:
                info = PdfLinkInfo(rects, page=link.page(), location=link.location())
            else:
                continue
            key = (info.label(), tuple((round(r.x()), round(r.y())) for r in rects))
            if key not in seen:
                seen.add(key)
                links.append(info)
        # 2) 本文に文字で書かれているだけのURL・メールアドレス
        try:
            text = doc.getAllText(page).text()
        except Exception:  # noqa: BLE001
            text = ""
        for m in _TEXT_LINK_RE.finditer(text):
            raw = m.group(0).rstrip(".,;:!?)]}'\"")
            if not raw:
                continue
            if m.group("mail"):
                url = QUrl("mailto:" + raw)
            else:
                url = QUrl(raw if raw.lower().startswith("http") else "http://" + raw)
            selection = doc.getSelectionAtIndex(page, m.start(), len(raw))
            rects = [poly.boundingRect() for poly in selection.bounds()]
            rects = [r for r in rects if r.width() > 0 and r.height() > 0]
            if not rects or not url.isValid():
                continue
            # 埋め込みリンクと重なる場合はそちらを優先する
            if any(r.intersects(er) for r in rects for existing in links for er in existing.rects):
                continue
            links.append(PdfLinkInfo(rects, url=url))
        self._links[page] = links
        return links

    def link_at(self, pos: QPointF) -> tuple[int, PdfLinkInfo] | None:
        """ビューポート上の位置にあるリンク。"""
        doc = self.document()
        if doc is None or doc.status() != QPdfDocument.Status.Ready:
            return None
        offset = self._scroll_offset()
        content = QPointF(pos.x() + offset.x(), pos.y() + offset.y())
        for page, page_rect in enumerate(self._page_rects()):
            if page_rect.isNull() or not QRectF(page_rect).contains(content):
                continue
            for info in self._links_on_page(page):
                for r in info.rects:
                    if self._to_view(page_rect, page, r).adjusted(-1, -1, 1, 1).contains(pos):
                        return page, info
            return None
        return None

    # ------------------------------------------------------------ マウス操作
    def _set_hover(self, hit: tuple[int, PdfLinkInfo] | None, global_pos=None):
        old = self._hover[1] if self._hover else None
        new = hit[1] if hit else None
        if old is new:
            return
        self._hover = hit
        if new is None:
            self.viewport().unsetCursor()
            QToolTip.hideText()
            self._show_status("")
        else:
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
            if global_pos is not None:
                QToolTip.showText(global_pos, new.label(), self.viewport())
            self._show_status(new.label())
        self.viewport().update()

    def _show_status(self, text: str):
        window = self.window()
        if isinstance(window, QMainWindow) and window.statusBar() is not None and window.statusBar().isVisible():
            if text:
                window.statusBar().showMessage(f"リンク: {text}")
            else:
                window.statusBar().clearMessage()

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if event.buttons() == Qt.MouseButton.NoButton:
            self._set_hover(self.link_at(event.position()), event.globalPosition().toPoint())

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            hit = self.link_at(event.position())
            if hit is not None:
                self._press_pos = event.position()
                self._press_hit = hit
                event.accept()
                return
        self._press_hit = None
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        pressed, press_pos = self._press_hit, self._press_pos
        self._press_hit = self._press_pos = None
        if event.button() == Qt.MouseButton.LeftButton and pressed is not None:
            moved = (event.position() - press_pos).manhattanLength() if press_pos is not None else 0
            hit = self.link_at(event.position())
            if moved <= self.DRAG_THRESHOLD and hit is not None and hit[1] is pressed[1]:
                self._open_link(*hit)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._set_hover(None)

    def _open_link(self, page: int, link: PdfLinkInfo):
        if link.url is not None:
            # ファイルリンクの元の文字列を探すため、リンクの位置(ページ内の座標)も渡す
            self._open_external_link(link.url, page, link.rects[0].center())
        elif 0 <= link.page < self.document().pageCount():
            self.pageNavigator().jump(link.page, link.location)

    def _raw_file_path(self, page: int, point: QPointF) -> str | None:
        """PDFの元バイト列から、文字化けしたファイルリンクを復元する。"""
        with pymupdf.open(self.pdf_path) as pdf:
            for link in pdf[page].get_links():
                rect = link["from"]
                if not (rect.x0 <= point.x() <= rect.x1 and rect.y0 <= point.y() <= rect.y1):
                    continue
                obj = pdf.xref_object(link["xref"])
                match = re.search(r"/(?:URI|UF|F)\s*<([0-9A-Fa-f]+)>", obj)
                if match is not None:
                    raw = bytes.fromhex(match.group(1))
                else:
                    match = re.search(r"/(?:URI|UF|F)\s*\(((?:\\.|[^\\)])*)\)", obj)
                    if match is None:
                        continue
                    literal = match.group(1).encode("latin-1")

                    def unescape(m):
                        escaped = m.group(1)
                        if escaped[:1] in (b"\r", b"\n"):
                            return b""
                        if escaped[:1] in b"01234567":
                            return bytes((int(escaped, 8),))
                        return {b"n": b"\n", b"r": b"\r", b"t": b"\t",
                                b"b": b"\b", b"f": b"\f"}.get(escaped, escaped)

                    raw = re.sub(rb"\\([0-7]{1,3}|\r?\n|.)", unescape, literal)
                encodings = ("utf-16",) if raw.startswith((b"\xfe\xff", b"\xff\xfe")) else (
                    "utf-8", "cp932")
                for encoding in encodings:
                    try:
                        decoded = raw.decode(encoding)
                        break
                    except UnicodeError:
                        continue
                else:
                    continue
                if decoded.startswith("file:"):
                    return QUrl(decoded).toLocalFile()
                return decoded
        return None

    def _open_external_link(self, url: QUrl, page: int, point: QPointF):
        scheme = url.scheme().lower()
        if scheme not in ("http", "https", "mailto", "file"):
            return
        if scheme == "file":
            path = url.toLocalFile()
            if not os.path.exists(os.path.join(os.path.dirname(self.pdf_path), path)):
                try:
                    raw_path = self._raw_file_path(page, point)
                except (OSError, RuntimeError, ValueError):
                    raw_path = None
                if raw_path:
                    path = raw_path
            path = path.replace("¥", "\\")
            if not os.path.isabs(path):
                path = os.path.join(os.path.dirname(self.pdf_path), path)
            path = os.path.abspath(path)
            if not os.path.exists(path):
                QMessageBox.warning(self, "リンク先が見つかりません", path)
                return
            url = QUrl.fromLocalFile(path)
        if not QDesktopServices.openUrl(url):
            QMessageBox.warning(self, "リンクを開けません", url.toString())

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._hover is None:
            return
        page, info = self._hover
        rects = self._page_rects()
        if page >= len(rects) or rects[page].isNull():
            return
        # マウスが乗っているリンクを薄い青で囲んで下線を引く(ブラウザ版と同じ見た目)
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for r in info.rects:
            view_rect = self._to_view(rects[page], page, r).adjusted(-1, -1, 1, 1)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(37, 99, 235, 36))
            painter.drawRoundedRect(view_rect, 2, 2)
            painter.setPen(QPen(QColor(37, 99, 235, 230), 1.5))
            painter.drawLine(view_rect.bottomLeft(), view_rect.bottomRight())
        painter.end()


def _render_pages_to_printer(document: QPdfDocument, printer: QPrinter, start_page: int, end_page: int):
    """1始まりのページ範囲[start_page, end_page]をprinterへ描画する。BasePdfTab/PageRangeWindowで共有する。"""
    painter = QPainter()
    if not painter.begin(printer):
        return
    try:
        page_rect = printer.pageRect(QPrinter.Unit.DevicePixel)
        first = True
        for page in range(start_page - 1, end_page):
            if not first:
                printer.newPage()
            first = False

            pt_size = document.pagePointSize(page)
            if pt_size.width() <= 0 or pt_size.height() <= 0:
                continue
            scale = min(page_rect.width() / pt_size.width(), page_rect.height() / pt_size.height())
            img_w = max(1, round(pt_size.width() * scale))
            img_h = max(1, round(pt_size.height() * scale))
            image = document.render(page, QSize(img_w, img_h))

            x = round((page_rect.width() - img_w) / 2)
            y = round((page_rect.height() - img_h) / 2)
            painter.drawImage(x, y, image)
    finally:
        painter.end()


def _render_pdf_thumbnail(path: str, width: int) -> "QPixmap | None":
    """PDFの1ページ目を指定幅の縮小画像にして返す(開始画面のサムネイル用)。失敗時はNone。"""
    doc = QPdfDocument()
    try:
        doc.load(path)
        if doc.pageCount() < 1:
            return None
        pt_size = doc.pagePointSize(0)
        if pt_size.width() <= 0 or pt_size.height() <= 0:
            return None
        scale = width / pt_size.width()
        height = max(1, round(pt_size.height() * scale))
        image = doc.render(0, QSize(width, height))
        pixmap = QPixmap.fromImage(image)
        # 白いページが背景に溶け込んで境界が分からなくなるため、枠線を描画しておく
        painter = QPainter(pixmap)
        painter.setPen(QPen(QColor("#C7CBD1"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(0, 0, pixmap.width() - 1, pixmap.height() - 1)
        painter.end()
        return pixmap
    except Exception:  # noqa: BLE001
        return None
    finally:
        doc.close()


class RatioSplitter(QSplitter):
    """一覧側の幅を、常に全体幅に対する比率で保つQSplitter(ユーザーが手動で
    ドラッグするまでは)。

    構築直後やウィンドウ最大化中はまだ実際の幅が確定しておらず、setSizes()に絶対値
    を渡しても比率通りに解釈されない(Qtは値を比率としてではなく現在の幅からの差分
    として扱うため)。また、OS側のウィンドウ最大化はQtのイベントループの1ティックで
    確定するとは限らず、最初のresizeEventがまだ最大化前の暫定サイズのことがある。
    そのため一度きりの適用にはせず、ユーザーがハンドルを手動でドラッグするまでは
    リサイズのたびに比率を再適用する。
    """

    def __init__(self, orientation: Qt.Orientation, left_ratio: float, parent=None):
        super().__init__(orientation, parent)
        self._left_ratio = left_ratio
        self._user_moved = False
        self.splitterMoved.connect(self._on_user_moved)

    def _on_user_moved(self, _pos: int, _index: int):
        self._user_moved = True

    def ensure_left_width(self, width: int):
        """左側(一覧)が指定幅より狭ければ広げる。以後のリサイズでもその比率を保つ。"""
        total = self.width()
        sizes = self.sizes()
        if total <= 0 or not sizes or sizes[0] >= width:
            return
        width = min(width, round(total * 0.6))
        self._left_ratio = width / total
        self.setSizes([width, total - width])

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._user_moved:
            return
        total = event.size().width()
        if total > 0:
            left = round(total * self._left_ratio)
            self.setSizes([left, total - left])


class PageNumberControl(QWidget):
    """1始まりのページ入力と総ページ数表示を共有する。"""

    page_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current = -1
        self._total = 0
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(4)
        self.input = QLineEdit("-")
        self.input.setObjectName("pageNumberInput")
        self.input.setFixedWidth(54)
        self.input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.input.setToolTip("ページ番号を入力してEnterで移動 (Ctrl+G)")
        self.input.returnPressed.connect(self._submit)
        self.input.textEdited.connect(self._clear_error)
        self.input.installEventFilter(self)
        layout.addWidget(self.input)
        self.total_label = QLabel("/ -")
        layout.addWidget(self.total_label)
        self.input.setEnabled(False)

    def set_page(self, current: int, total: int):
        self._current, self._total = current, total
        self.total_label.setText(f"/ {total}" if total > 0 else "/ -")
        self.input.setEnabled(total > 0)
        if not self.input.hasFocus():
            self._restore_text()

    def focus_input(self):
        if self._total > 0:
            self.input.setFocus()
            self.input.selectAll()

    def cancel(self):
        self._restore_text()
        self.input.clearFocus()

    def _restore_text(self):
        self._clear_error()
        self.input.setText(str(self._current + 1) if self._total > 0 else "-")

    def _clear_error(self, *_args):
        self.input.setStyleSheet("")
        self.input.setToolTip("ページ番号を入力してEnterで移動 (Ctrl+G)")

    def _submit(self):
        value = self.input.text().strip()
        if not value.isdecimal() or not 1 <= int(value) <= self._total:
            self.input.setStyleSheet("border: 1px solid #C62828;")
            self.input.setToolTip(f"1〜{self._total} のページ番号を入力してください")
            return
        self.page_requested.emit(int(value))
        self.cancel()

    def eventFilter(self, watched, event):
        if watched is self.input:
            if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
                self.cancel()
                return True
            if event.type() == QEvent.Type.FocusOut:
                self._restore_text()
        return super().eventFilter(watched, event)


# --------------------------------------------------------- PDF表示・ページ送りの共通基底
class BasePdfTab(QWidget):
    """PDF描画・ページ送り・印刷など、メール束PDF/一般資料PDFで共通する部分。"""

    page_changed = Signal()  # ページ送りボタンはMainWindow側の共通ツールバーにあるため、状態変化を通知する

    def __init__(self, pdf_path: str, parent=None):
        super().__init__(parent)
        self.pdf_path = pdf_path
        self.db_path: str | None = None
        self.current_query = ""
        self._syncing_selection = False  # ページ同期による選択変更中はジャンプを抑止するためのガード

        self.document = QPdfDocument(self)
        self.search_model = QPdfSearchModel(self)
        self.search_model.setDocument(self.document)

        self.pdf_view = LinkedPdfView(pdf_path)
        self.pdf_view.setDocument(self.document)
        self.pdf_view.link_model.setDocument(self.document)
        self.pdf_view.setPageMode(QPdfView.PageMode.MultiPage)
        self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        self.pdf_view.setSearchModel(self.search_model)
        _style_pdf_view(self.pdf_view)

        self.pdf_view.pageNavigator().currentPageChanged.connect(self._on_page_changed)

        self.pdf_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.pdf_view.customContextMenuRequested.connect(self._show_pdf_context_menu)

    @property
    def title(self) -> str:
        return os.path.basename(self.pdf_path)

    def set_zoom_mode(self, mode):
        self.pdf_view.setZoomMode(mode)
        idx = self.zoom_combo.findData(mode)
        if idx >= 0 and idx != self.zoom_combo.currentIndex():
            self.zoom_combo.blockSignals(True)
            self.zoom_combo.setCurrentIndex(idx)
            self.zoom_combo.blockSignals(False)

    def set_sidebar_visible(self, visible: bool):
        self.sidebar.setVisible(visible)

    def _request_mode_switch(self):
        """「しおり一覧へ」「メール一覧へ」ボタン: 同じタブの表示をもう一方の一覧に切り替える。"""
        window = self.window()
        if isinstance(window, MainWindow):
            window._switch_current_mode()

    def _request_toggle_sidebar(self):
        """一覧の下にある「一覧を隠す」ボタンから呼ばれる。実際の表示状態はMainWindow側で
        一元管理しているため、そちらのトグル処理を呼び出す(Ctrl+B/右クリックと共通)。"""
        window = self.window()
        if isinstance(window, QMainWindow) and hasattr(window, "_toggle_sidebar"):
            window._toggle_sidebar()

    # --------------------------------------------------------- 一覧まわりの部品
    def _build_search_box(self, placeholder: str) -> QLineEdit:
        """一覧の上に置く検索欄(「⚙ 表示設定」の左)。"""
        box = QLineEdit()
        box.setObjectName("searchBox")
        box.addAction(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogContentsView),
                      QLineEdit.ActionPosition.LeadingPosition)
        box.setPlaceholderText(placeholder)
        box.setToolTip(placeholder)
        box.setClearButtonEnabled(True)
        box.textChanged.connect(self._on_search_text_changed)
        self.search_box = box
        return box

    def _on_search_text_changed(self, text: str):
        self.set_search(text)
        window = self.window()
        if isinstance(window, QMainWindow) and window.statusBar() is not None:
            suffix = f" (未読 {self.unread_count()})" if self.unread_count() else ""
            window.statusBar().showMessage(f"{self.result_count()} 件表示中{suffix}")

    def focus_search(self):
        self.search_box.setFocus()
        self.search_box.selectAll()

    def _build_collapse_bar(self) -> QWidget:
        """一覧の下に置くバー。左に件数、右に「一覧を隠す」ボタン。"""
        bar = QWidget()
        bar.setObjectName("collapseBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 6, 12, 6)
        self.count_label = QLabel()
        self.count_label.setObjectName("countLabel")
        layout.addWidget(self.count_label)
        layout.addStretch(1)
        button = QToolButton()
        button.setObjectName("smallButton")
        button.setText("一覧を隠す")
        button.setToolTip("メール一覧・しおり一覧を隠す (Ctrl+Bで再表示)")
        button.clicked.connect(self._request_toggle_sidebar)
        layout.addWidget(button)
        return bar

    # --------------------------------------------------------- PDF表示の上のバー
    def _build_viewer_area(self) -> QWidget:
        """PDF表示と、その上部中央に置くページ送り・表示の切り替え・全画面ボタンのバー。"""
        area = QWidget()
        layout = QVBoxLayout(area)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        bar = QWidget()
        bar.setObjectName("viewerBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 5, 10, 5)
        row.setSpacing(6)
        row.addStretch(1)

        self.prev_page_button = QToolButton()
        self.prev_page_button.setText("◀")
        self.prev_page_button.setToolTip("前のページ (←)")
        self.prev_page_button.clicked.connect(self.go_prev_page)
        row.addWidget(self.prev_page_button)

        # ページ番号の直接入力(Ctrl+Gで入力欄へ、Escで取り消し)
        self.page_control = PageNumberControl()
        self.page_control.page_requested.connect(self.go_to_page)
        # クリックかCtrl+Gのときだけ入力できるようにする(勝手にフォーカスが移ると←/→キーのページ送りが効かなくなるため)
        self.page_control.input.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        row.addWidget(self.page_control)

        self.next_page_button = QToolButton()
        self.next_page_button.setText("▶")
        self.next_page_button.setToolTip("次のページ (→)")
        self.next_page_button.clicked.connect(self.go_next_page)
        row.addWidget(self.next_page_button)

        row.addSpacing(8)
        self.zoom_combo = QComboBox()
        self.zoom_combo.addItem("幅に合わせる", QPdfView.ZoomMode.FitToWidth)
        self.zoom_combo.addItem("ページ全体", QPdfView.ZoomMode.FitInView)
        self.zoom_combo.setToolTip("表示倍率")
        self.zoom_combo.currentIndexChanged.connect(
            lambda _i: self.set_zoom_mode(self.zoom_combo.currentData()))
        row.addWidget(self.zoom_combo)

        row.addSpacing(8)
        self.fullscreen_button = QToolButton()
        self.fullscreen_button.setText("⛶ 全画面")
        self.fullscreen_button.setToolTip("全画面表示を切り替え (F11)")
        self.fullscreen_button.clicked.connect(self._request_toggle_fullscreen)
        row.addWidget(self.fullscreen_button)
        row.addStretch(1)
        for widget in (self.prev_page_button, self.next_page_button, self.zoom_combo, self.fullscreen_button):
            widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        layout.addWidget(bar)
        layout.addWidget(self.pdf_view, 1)
        self.page_changed.connect(self._update_page_bar)
        self._update_page_bar()
        return area

    def _request_toggle_fullscreen(self):
        window = self.window()
        if isinstance(window, QMainWindow) and hasattr(window, "fullscreen_action"):
            window.fullscreen_action.toggle()

    def set_fullscreen_state(self, full: bool):
        self.fullscreen_button.setText("全画面を終了" if full else "⛶ 全画面")

    def _update_page_bar(self):
        total = self.page_count()
        current = self.current_page() if total > 0 else -1
        self.page_control.set_page(current, total)
        self.prev_page_button.setEnabled(total > 0 and current > 0)
        self.next_page_button.setEnabled(total > 0 and current < total - 1)

    def go_to_page(self, page_number: int):
        """1始まりのページ番号へ移動する(ページ番号欄から)。"""
        if 1 <= page_number <= self.page_count():
            self.pdf_view.pageNavigator().jump(page_number - 1, QPointF(0, 0))

    def _show_pdf_context_menu(self, pos):
        """PDF表示部分の右クリックメニュー。全画面表示中はしおり呼び出し・全画面解除の導線が
        ツールバーから消えてしまうため、ここから操作できるようにする。"""
        window = self.window()
        if not isinstance(window, QMainWindow) or not hasattr(window, "fullscreen_action"):
            return
        menu = QMenu(self)
        sidebar_visible = self.sidebar.isVisible()
        toggle_action = menu.addAction("しおり・メール一覧を隠す" if sidebar_visible else "しおり・メール一覧を表示")
        toggle_action.triggered.connect(window._toggle_sidebar)
        menu.addSeparator()
        if window.isFullScreen():
            exit_action = menu.addAction("全画面表示を終了")
            exit_action.triggered.connect(lambda: window.fullscreen_action.setChecked(False))
        else:
            enter_action = menu.addAction("全画面表示にする")
            enter_action.triggered.connect(lambda: window.fullscreen_action.setChecked(True))
        menu.exec(self.pdf_view.mapToGlobal(pos))

    def set_search(self, query: str):
        self.current_query = query
        self.refresh_list()
        self.search_model.setSearchString(query)

    # サブクラスで実装する
    def refresh_list(self):
        raise NotImplementedError

    def result_count(self) -> int:
        raise NotImplementedError

    def unread_count(self) -> int:
        return 0

    def load(self, force_rebuild: bool = False, status_cb=None) -> bool:
        raise NotImplementedError

    def reindex(self, status_cb=None):
        self.load(force_rebuild=True, status_cb=status_cb)

    # --------------------------------------------------------------- 印刷
    def print_page_range(self, start_page: int, end_page: int):
        """1始まりのページ範囲[start_page, end_page]をレンダリングして印刷する。"""
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        dialog = QPrintDialog(printer, self)
        dialog.setWindowTitle("印刷")
        if dialog.exec() != QPrintDialog.DialogCode.Accepted:
            return
        self.render_page_range_to_printer(printer, start_page, end_page)

    def render_page_range_to_printer(self, printer: QPrinter, start_page: int, end_page: int):
        """1始まりのページ範囲[start_page, end_page]をprinterへ描画する(印刷ダイアログとは独立してテスト可能)。"""
        _render_pages_to_printer(self.document, printer, start_page, end_page)

    # --------------------------------------------------------------- 保存
    def save_page_range(self, start_page: int, end_page: int, suggested_name: str):
        """1始まりのページ範囲[start_page, end_page]を、元PDFの品質のまま別ファイルとして保存する。"""
        suggested_name = _sanitize_filename(suggested_name)
        if not suggested_name.lower().endswith(".pdf"):
            suggested_name += ".pdf"
        default_dir = os.path.dirname(self.pdf_path)
        dest_path, _ = QFileDialog.getSaveFileName(
            self, "PDFとして保存", os.path.join(default_dir, suggested_name), "PDF Files (*.pdf)")
        if not dest_path:
            return
        try:
            parser.save_page_range(self.pdf_path, dest_path, start_page, end_page)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "保存に失敗しました", f"PDFの保存に失敗しました。\n\n詳細: {e}")
            return
        window = self.window()
        if isinstance(window, QMainWindow) and window.statusBar():
            window.statusBar().showMessage(f"保存しました: {dest_path}", 5000)

    # --------------------------------------------------------- ページ移動
    def page_count(self) -> int:
        return self.document.pageCount()

    def current_page(self) -> int:
        return self.pdf_view.pageNavigator().currentPage()

    def go_prev_page(self):
        nav = self.pdf_view.pageNavigator()
        if nav.currentPage() > 0:
            nav.jump(nav.currentPage() - 1, QPointF(0, 0))

    def go_next_page(self):
        nav = self.pdf_view.pageNavigator()
        if nav.currentPage() < self.page_count() - 1:
            nav.jump(nav.currentPage() + 1, QPointF(0, 0))

    def _on_page_changed(self, page: int):
        self.page_changed.emit()
        self._sync_selection_to_page(page)

    def _sync_selection_to_page(self, page: int):
        """PDFのスクロール(ページ送り)に追従して一覧側の選択を更新する。サブクラスで実装する。"""


class NoToolbarMenuMainWindow(QMainWindow):
    """ツールバーの上で右クリックしたときの標準メニュー(ツールバーの表示/非表示の切り替え)を出さない
    メインウインドウ。名前のない「✔」だけの項目が出て、押すとツールバーが消えて戻せなくなるため。"""

    def createPopupMenu(self):
        return None


class PageRangeWindow(NoToolbarMenuMainWindow):
    """一覧から切り離して1件(1メール、または資料PDFの1しおり区間)だけを表示する、独立した閲覧用ウィンドウ。

    元PDFから該当ページ範囲だけを一時ファイルへ抽出して表示する(メイン一覧の
    QPdfDocumentと共有すると、双方のページ送りが干渉してしまうため)。ウィンドウを
    閉じると一時ファイルは削除する。
    """

    def __init__(self, title: str, pdf_path: str, start_page: int, end_page: int,
                 attachments: "list[db.AttachmentInfo] | None" = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        if avail is not None:
            self.resize(min(1180, int(avail.width() * 0.95)), min(1000, int(avail.height() * 0.9)))
        else:
            self.resize(1180, 1000)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._start_page = start_page
        self._attachments = [a for a in (attachments or []) if a.start_page is not None]

        fd, self._tmp_path = tempfile.mkstemp(suffix=".pdf", prefix="mailpdf_")
        os.close(fd)
        parser.save_page_range(pdf_path, self._tmp_path, start_page, end_page)

        self.document = QPdfDocument(self)
        self.document.load(self._tmp_path)

        self.pdf_view = LinkedPdfView(self._tmp_path)
        self.pdf_view.setDocument(self.document)
        self.pdf_view.setPageMode(QPdfView.PageMode.MultiPage)
        self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        _style_pdf_view(self.pdf_view)
        self.setCentralWidget(self.pdf_view)

        self._build_toolbar()
        self.pdf_view.pageNavigator().currentPageChanged.connect(self._update_page_bar)
        self._update_page_bar()

        prev_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Left), self)
        prev_shortcut.activated.connect(self.go_prev_page)
        next_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Right), self)
        next_shortcut.activated.connect(self.go_next_page)
        page_shortcut = QShortcut(QKeySequence("Ctrl+G"), self)
        page_shortcut.activated.connect(self.page_control.focus_input)

    def _build_toolbar(self):
        toolbar = QToolBar()
        toolbar.setMovable(False)
        # 右クリックで出る「ツールバーの表示/非表示」メニューで消してしまうと戻せないため、切り替えられないようにする
        toolbar.toggleViewAction().setEnabled(False)
        toolbar.toggleViewAction().setVisible(False)
        self.addToolBar(toolbar)

        toolbar.addWidget(QLabel("表示:"))
        zoom_combo = QComboBox()
        zoom_combo.addItem("幅に合わせる", QPdfView.ZoomMode.FitToWidth)
        zoom_combo.addItem("ページ全体", QPdfView.ZoomMode.FitInView)
        zoom_combo.currentIndexChanged.connect(
            lambda: self.pdf_view.setZoomMode(zoom_combo.currentData()))
        toolbar.addWidget(zoom_combo)

        toolbar.addSeparator()
        self.prev_page_button = QToolButton()
        self.prev_page_button.setText("◀")
        self.prev_page_button.setToolTip("前のページ (←)")
        self.prev_page_button.clicked.connect(self.go_prev_page)
        toolbar.addWidget(self.prev_page_button)

        self.page_control = PageNumberControl()
        self.page_control.page_requested.connect(self._go_to_page)
        toolbar.addWidget(self.page_control)

        self.next_page_button = QToolButton()
        self.next_page_button.setText("▶")
        self.next_page_button.setToolTip("次のページ (→)")
        self.next_page_button.clicked.connect(self.go_next_page)
        toolbar.addWidget(self.next_page_button)

        toolbar.addSeparator()
        print_action = toolbar.addAction("印刷")
        print_action.triggered.connect(self._print)

        if self._attachments:
            toolbar.addSeparator()
            mail_action = toolbar.addAction("メール本文へ")
            mail_action.triggered.connect(self._jump_to_mail_start)

            toolbar.addWidget(QLabel("添付ファイル:"))
            attach_combo = QComboBox()
            for att in self._attachments:
                attach_combo.addItem(att.name, att.start_page)
            attach_combo.setMinimumWidth(220)
            attach_combo.setMaximumWidth(360)
            # currentIndexChangedだと、既定で選択状態になる先頭(1件目)を選んでも
            # インデックスが変化しないため反応しない。activatedならユーザーが選ぶたびに
            # (選び直しでも)発火するのでこちらを使う。
            attach_combo.activated.connect(
                lambda idx: self._jump_to_attachment(attach_combo.itemData(idx)))
            toolbar.addWidget(attach_combo)

    # --------------------------------------------------------- ページ移動
    def go_prev_page(self):
        nav = self.pdf_view.pageNavigator()
        if nav.currentPage() > 0:
            nav.jump(nav.currentPage() - 1, QPointF(0, 0))

    def go_next_page(self):
        nav = self.pdf_view.pageNavigator()
        if nav.currentPage() < self.document.pageCount() - 1:
            nav.jump(nav.currentPage() + 1, QPointF(0, 0))

    def _go_to_page(self, page_number: int):
        if 1 <= page_number <= self.document.pageCount():
            self.pdf_view.pageNavigator().jump(page_number - 1, QPointF(0, 0))

    def _update_page_bar(self, *_args):
        total = self.document.pageCount()
        current = self.pdf_view.pageNavigator().currentPage() if total > 0 else -1
        self.page_control.set_page(current, total)
        self.prev_page_button.setEnabled(total > 0 and current > 0)
        self.next_page_button.setEnabled(total > 0 and current < total - 1)

    def _jump_to_mail_start(self):
        self.pdf_view.pageNavigator().jump(0, QPointF(0, 0))

    def _jump_to_attachment(self, original_start_page: int):
        local_page = original_start_page - self._start_page  # 抽出後PDFでの0始まりページ番号
        if 0 <= local_page < self.document.pageCount():
            self.pdf_view.pageNavigator().jump(local_page, QPointF(0, 0))

    def _print(self):
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        dialog = QPrintDialog(printer, self)
        dialog.setWindowTitle("印刷")
        if dialog.exec() != QPrintDialog.DialogCode.Accepted:
            return
        _render_pages_to_printer(self.document, printer, 1, self.document.pageCount())

    def closeEvent(self, event):
        super().closeEvent(event)
        # QPdfDocument.close()だけではpdfiumが一時ファイルのハンドルを保持し続け、
        # 直後のos.remove()がWindowsで失敗する(WinError 32)ため、documentを明示的に
        # 破棄してから削除する。
        self.pdf_view.setDocument(None)
        self.document.close()
        self.document.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        try:
            os.remove(self._tmp_path)
        except OSError:
            pass


# -------------------------------------------------------------- 1PDF分のタブ(メール)
class PdfTab(BasePdfTab):
    """1つのメール束PDFに対応するメール一覧+PDF表示ペイン。タブとして複数同時に開ける。"""

    MODE = "mail"

    def __init__(self, pdf_path: str, parent=None):
        super().__init__(pdf_path, parent)
        self.group_by_subject = mail_settings().get("group")
        self._total_count = 0
        self.scope_toc_index: int | None = None  # 一覧の対象にするしおり(None=PDF全体の第1階層)
        self.include_descendants = False

        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        splitter = self._splitter = RatioSplitter(Qt.Orientation.Horizontal, 0.3)
        outer.addWidget(splitter)

        left = QWidget()
        left.setObjectName("sidebar")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        sort_bar = QWidget()
        sort_bar.setObjectName("sortBar")
        sort_layout = QHBoxLayout(sort_bar)
        sort_layout.setContentsMargins(10, 8, 10, 8)
        sort_layout.addWidget(self._build_search_box(MAIL_SEARCH_PLACEHOLDER), 1)

        mode_button = QToolButton()
        mode_button.setObjectName("smallButton")
        mode_button.setText("しおり一覧へ")
        mode_button.setToolTip("同じPDFをしおりの階層で表示する")
        mode_button.clicked.connect(self._request_mode_switch)
        sort_layout.addWidget(mode_button)

        self.settings_button = QToolButton()
        self.settings_button.setObjectName("smallButton")
        self.settings_button.setText("⚙ 表示設定")
        self.settings_button.setToolTip("メール一覧の表示形式・並び順・表示する項目・文字サイズなどを変更")
        self.settings_button.setCheckable(True)
        sort_layout.addWidget(self.settings_button)
        left_layout.addWidget(sort_bar)

        # 一覧の対象: PDF全体(第1階層) または 選んだしおりの直下/配下すべて
        scope_bar = QWidget()
        scope_bar.setObjectName("scopeBar")
        scope_layout = QHBoxLayout(scope_bar)
        scope_layout.setContentsMargins(10, 0, 10, 8)
        scope_label = QLabel("対象:")
        scope_label.setObjectName("settingsLabel")
        scope_layout.addWidget(scope_label)
        self.scope_combo = QComboBox()
        self.scope_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.scope_combo.setMinimumContentsLength(18)
        self.scope_combo.addItem("PDF全体（ルート）", None)
        self.scope_combo.currentIndexChanged.connect(self._on_scope_changed)
        scope_layout.addWidget(self.scope_combo, 1)
        self.range_combo = QComboBox()
        self.range_combo.addItem("直下のみ", False)
        self.range_combo.addItem("配下すべて", True)
        self.range_combo.currentIndexChanged.connect(self._on_scope_changed)
        scope_layout.addWidget(self.range_combo)
        left_layout.addWidget(scope_bar)

        self.settings_panel = self._build_settings_panel()
        self.settings_panel.setVisible(False)
        self.settings_button.toggled.connect(self.settings_panel.setVisible)
        left_layout.addWidget(self.settings_panel)

        self.model = MailListModel()
        self.item_delegate = MailItemDelegate()
        self.item_delegate.attachment_clicked.connect(self._on_attachment_clicked)
        self.list_view = MailListView(self.item_delegate)
        self.list_view.setModel(self.model)
        self.list_view.setItemDelegate(self.item_delegate)
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # 宛先の有無・添付の有無などで行の高さが変わる。幅が変わるとプレビューの折り返しも変わる
        self.list_view.setUniformItemSizes(False)
        self.list_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.list_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_view.setFrameShape(QFrame.Shape.NoFrame)
        self.list_view.setAlternatingRowColors(False)
        self.list_view.clicked.connect(self._on_index_activated)
        self.list_view.doubleClicked.connect(self._on_index_double_clicked)
        self.list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_view.customContextMenuRequested.connect(self._show_context_menu)
        left_layout.addWidget(self.list_view)
        left_layout.addWidget(self._build_collapse_bar())
        self.sidebar = left
        splitter.addWidget(left)

        splitter.addWidget(self._build_viewer_area())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 7)

        self.list_view.selectionModel().currentChanged.connect(self._on_index_activated)
        self.list_view.selectionModel().currentChanged.connect(self._on_current_changed_resize)
        mail_settings().changed.connect(self._on_view_settings_changed)

    # --------------------------------------------------------- 表示設定パネル
    def _build_settings_panel(self) -> SettingsPanel:
        """ブラウザ版の「メール一覧の表示設定」と同じ項目を持つパネル。"""
        S = mail_settings()
        panel = SettingsPanel("メール一覧の表示設定", S, lambda: self.settings_button.setChecked(False))
        layout_caption, layout_seg = panel.add_seg("表示形式", "layout", [("card", "カード"), ("row", "1行リスト")])
        for value, button in panel.seg_buttons("layout"):
            if value == "row":
                button.clicked.connect(self._widen_for_rows)
        # 並び順は選択肢が多いため、ブラウザ版と同じくプルダウンにする
        sort_combo = QComboBox()
        for value, label in [("pdf", "PDFの順番"), ("pdf-desc", "PDFの逆順"), ("date-desc", "日時が新しい順"),
                             ("date-asc", "日時が古い順"), ("from", "差出人順"),
                             ("subject", "件名順（同じ件名が並ぶ）")]:
            sort_combo.addItem(label, value)
        sort_combo.activated.connect(lambda i: S.set("sort", sort_combo.itemData(i)))
        panel.add_widget("並び順", sort_combo)
        panel.add_seg("件名でまとめる", "group", [(False, "まとめない"), (True, "まとめる")])
        panel.add_seg("文字サイズ", "fontSize", [(12, "小"), (14, "中"), (16, "大"), (18, "特大")])
        panel.add_seg("行の間隔", "density", [("normal", "ゆったり"), ("compact", "詰める")])
        fields_caption = panel.add_checks("表示する項目", [(f"f_{k}", label) for k, label in MAIL_FIELDS])
        preview_caption, preview_seg = panel.add_seg(
            "プレビューの行数", "previewLines", [(1, "1行"), (2, "2行"), (3, "3行"), (5, "5行")])
        panel.add_seg("日時の表示", "dateFmt",
                      [("ymdhm", "2026/09/03 08:39"), ("mdhm", "9/3 08:39"), ("raw", "PDFの表記のまま")])
        panel.add_footer(self._reset_view_settings)

        def sync_extra():
            row = S.get("layout") == "row"
            sort_combo.setCurrentIndex(max(0, sort_combo.findData(S.get("sort"))))
            fields_caption.setText("表示する項目\n（1行リストでは一部のみ）" if row else "表示する項目")
            for key, _label in MAIL_FIELDS:
                box = panel.check_box(f"f_{key}")
                usable = not row or key in ROW_FIELDS
                box.setEnabled(usable)
                box.setToolTip("" if usable else "1行リストでは表示されません")
            preview_caption.setVisible(not row)
            preview_seg.setVisible(not row)

        panel.add_syncer(sync_extra)
        panel.sync()
        return panel

    def _widen_for_rows(self):
        """1行リストに切り替えたとき、件名が読める幅まで一覧を広げる(ブラウザ版と同じ)。"""
        S = mail_settings()
        em = 0.6 + 1.2 + 14 + 3.5
        if S.get("f_date"):
            em += {"ymdhm": 8.6, "mdhm": 5.8, "raw": 11}.get(S.get("dateFmt"), 8.6) + 0.5
        if S.get("f_from"):
            em += 7
        if S.get("f_attach"):
            em += 3.7
        if S.get("f_pages"):
            em += 4.5
        self._splitter.ensure_left_width(round(em * S.get("fontSize")) + 20)

    def _reset_view_settings(self):
        mail_settings().reset()
        window = self.window()
        if isinstance(window, QMainWindow) and window.statusBar():
            window.statusBar().showMessage("メール一覧の表示設定を初期設定に戻しました", 4000)

    def _on_view_settings_changed(self, _reset: bool):
        self.settings_panel.sync()
        rows = self.model.all_rows()
        resorted = _sort_mails(rows, mail_settings().get("sort"))
        grouped = mail_settings().get("group")
        if [r.id for r in resorted] != [r.id for r in rows] or grouped != self.group_by_subject:
            self.group_by_subject = grouped  # 同じ件名(返信・転送を除く)のメールをまとめて表示する
            self._set_rows_keeping_selection(resorted)
        self.list_view.doItemsLayout()  # 表示項目・文字サイズなどで行の高さが変わるため
        self.list_view.viewport().update()

    def _set_rows_keeping_selection(self, rows: list["db.MailRow"]):
        """並び順だけを変える。選択中のメールはそのまま選択し、PDFの表示位置は動かさない。"""
        current = self.model.mail_at(self.list_view.currentIndex().row())
        self._syncing_selection = True
        try:
            self.model.set_rows(rows, grouped=self.group_by_subject)
            if current is not None:
                idx = self.model.index_for_mail_id(current.id)
                if idx.isValid():
                    self.list_view.setCurrentIndex(idx)
                    self.list_view.scrollTo(idx)
        finally:
            self._syncing_selection = False

    def _on_current_changed_resize(self, current: QModelIndex, previous: QModelIndex):
        # 選択中のメールだけ添付ファイルを全件表示して行が高くなるため、行の高さを計算し直させる
        for idx in (previous, current):
            if idx.isValid():
                self.item_delegate.sizeHintChanged.emit(idx)
        if current.isValid():
            # 高さが変わった後の配置で、選択したメール全体が見えるようにスクロールし直す
            QTimer.singleShot(0, lambda idx=QPersistentModelIndex(current): (
                self.list_view.scrollTo(QModelIndex(idx)) if idx.isValid() else None))

    # ------------------------------------------------------------- 動作
    def load(self, force_rebuild: bool = False, status_cb=None) -> bool:
        if status_cb:
            status_cb("インデックス作成中...")
        QApplication.processEvents()
        try:
            self.db_path = db.open_or_build(self.pdf_path, mode=self.MODE, force_rebuild=force_rebuild)
        except Exception as e:  # noqa: BLE001
            if status_cb:
                status_cb(f"インデックス作成に失敗しました: {e}")
            return False

        self.document.load(self.pdf_path)
        self._populate_scopes()
        self.page_changed.emit()
        return True

    def refresh_list(self):
        rows = (db.list_scoped(self.db_path, self.current_query, "date", True,
                               self.scope_toc_index, self.include_descendants)
                if self.db_path else [])
        if not self.current_query.strip():
            self._total_count = len(rows)
        self.model.set_rows(_sort_mails(rows, mail_settings().get("sort")), grouped=self.group_by_subject)
        self._update_count_label()
        first = self.model.first_mail_index()
        if first.isValid():
            self.list_view.setCurrentIndex(first)
        else:
            self.pdf_view.pageNavigator().jump(0, QPointF(0, 0))

    def _populate_scopes(self):
        self.scope_combo.blockSignals(True)
        self.scope_combo.clear()
        self.scope_combo.addItem("PDF全体（ルート）", None)
        path: list[str] = []
        for toc_index, level, title in db.list_scope_options(self.db_path):
            path = path[:level - 1]
            path.append(title)
            label = "  " * (level - 1) + title
            self.scope_combo.addItem(label, toc_index)
            self.scope_combo.setItemData(self.scope_combo.count() - 1,
                                         " › ".join(path),
                                         Qt.ItemDataRole.ToolTipRole)
        self.scope_combo.blockSignals(False)
        self.set_scope(self.scope_toc_index, self.include_descendants)

    def set_scope(self, toc_index: int | None, include_descendants: bool = False):
        self.scope_toc_index = toc_index
        self.include_descendants = include_descendants
        index = self.scope_combo.findData(toc_index)
        self.scope_combo.blockSignals(True)
        self.scope_combo.setCurrentIndex(index if index >= 0 else 0)
        self.scope_combo.blockSignals(False)
        self.range_combo.blockSignals(True)
        self.range_combo.setCurrentIndex(1 if include_descendants else 0)
        self.range_combo.setEnabled(toc_index is not None)
        self.range_combo.blockSignals(False)
        if self.db_path:
            self.refresh_list()

    def _on_scope_changed(self, *_args):
        self.scope_toc_index = self.scope_combo.currentData()
        self.include_descendants = bool(self.range_combo.currentData())
        self.range_combo.setEnabled(self.scope_toc_index is not None)
        if self.db_path:
            self.refresh_list()

    def _update_count_label(self):
        shown = len(self.model.all_rows())
        if self.current_query.strip():
            self.count_label.setText(f"{shown} / {self._total_count} 件ヒット")
        else:
            self.count_label.setText(f"{shown} 件（未読 {self.model.unread_count()}）")

    def result_count(self) -> int:
        return self.model.rowCount()

    def unread_count(self) -> int:
        return self.model.unread_count()


    def _toggle_group(self, key: str):
        """見出し行クリック時の折りたたみ/展開。選択中メールがまだ表示されていれば選択を維持する。"""
        current = self.list_view.currentIndex()
        current_mail = self.model.mail_at(current.row()) if current.isValid() else None

        self.model.toggle_collapsed(key)

        if current_mail is not None:
            idx = self.model.index_for_mail_id(current_mail.id)
            if idx.isValid():
                self._syncing_selection = True
                try:
                    self.list_view.setCurrentIndex(idx)
                finally:
                    self._syncing_selection = False

    def _on_index_activated(self, index: QModelIndex, *_args):
        if not index.isValid() or self.model.is_empty():
            return
        header = self.model.header_at(index.row())
        if header is not None:
            self._toggle_group(header.key)
            return
        if self._syncing_selection:
            # PDFのスクロールに追従して一覧側の選択を更新しているだけなので、
            # ここでページジャンプを行うとスクロール位置が巻き戻ってしまう。
            return
        mail = self.model.mail_at(index.row())
        if mail is None:
            return
        if mail.is_mail:
            self._mark_read(mail)
        self.pdf_view.pageNavigator().jump(mail.start_page - 1, QPointF(0, 0))

    def _on_index_double_clicked(self, index: QModelIndex):
        if not index.isValid() or self.model.is_empty() or self.model.header_at(index.row()) is not None:
            return
        mail = self.model.mail_at(index.row())
        if mail is None:
            return
        if mail.is_mail:
            self._mark_read(mail)
        self._open_mail_window(mail)

    def _on_attachment_clicked(self, index: QModelIndex, start_page: int):
        self.list_view.setCurrentIndex(index)
        mail = self.model.mail_at(index.row())
        if mail is not None:
            self._mark_read(mail)
        # QListViewの通常クリック処理(clicked -> _on_index_activatedでメール先頭ページへジャンプ)が
        # editorEventの後に実行され、このジャンプを上書きしてしまうため、
        # 次のイベントループへ遅延させて確実に最後に反映されるようにする。
        QTimer.singleShot(0, lambda: self.pdf_view.pageNavigator().jump(start_page - 1, QPointF(0, 0)))

    def _mark_read(self, mail: "db.MailRow", read: bool = True):
        if not mail.is_mail:
            return
        if mail.is_read == read:
            return
        db.set_read(self.db_path, mail.id, read)
        self.model.set_read(mail.id, read)
        self._update_count_label()

    # --------------------------------------------------------------- 印刷
    def _show_context_menu(self, pos):
        index = self.list_view.indexAt(pos)
        if not index.isValid():
            menu = QMenu(self)
            action = menu.addAction("すべて既読にする")
            action.triggered.connect(self._mark_all_read)
            menu.exec(self.list_view.viewport().mapToGlobal(pos))
            return
        mail = self.model.mail_at(index.row())
        if mail is None:
            return
        self.list_view.setCurrentIndex(index)

        menu = QMenu(self)
        if mail.is_mail:
            read_action = menu.addAction("未読にする" if mail.is_read else "既読にする")
            read_action.triggered.connect(lambda: self._mark_read(mail, not mail.is_read))
            menu.addSeparator()

            reply_action = menu.addAction("\U0001F4E7 Outlookで返信を作成...")
            reply_action.triggered.connect(lambda: self._create_outlook_reply(mail, reply_all=False))
            reply_all_action = menu.addAction("\U0001F4E7 Outlookで全員に返信を作成...")
            reply_all_action.triggered.connect(lambda: self._create_outlook_reply(mail, reply_all=True))
            menu.addSeparator()

        window_action = menu.addAction("\U0001F5D4 別ウインドウで開く")
        window_action.triggered.connect(lambda: self._open_mail_window(mail))
        menu.addSeparator()

        page_range = f"{mail.start_page}" if mail.start_page == mail.end_page else f"{mail.start_page}-{mail.end_page}"
        mail_name = f"{mail.subject or '(件名なし)'}"
        noun = "メール" if mail.is_mail else "資料"
        mail_action = menu.addAction(f"\U0001F5A8 この{noun}を印刷... ({page_range}ページ)")
        mail_action.triggered.connect(lambda: self.print_page_range(mail.start_page, mail.end_page))
        save_action = menu.addAction(f"\U0001F4BE この{noun}をPDFで保存... ({page_range}ページ)")
        save_action.triggered.connect(
            lambda: self.save_page_range(mail.start_page, mail.end_page, mail_name))

        attachments = [a for a in mail.attachment_list() if a.start_page is not None]
        if attachments:
            menu.addSeparator()
            attach_print_menu = menu.addMenu("\U0001F4CE 添付書類を印刷...")
            attach_save_menu = menu.addMenu("\U0001F4BE 添付書類をPDFで保存...")
            for att in attachments:
                end = att.end_page or att.start_page
                label = att.name if att.start_page == end else f"{att.name} ({att.start_page}-{end}ページ)"
                print_action = attach_print_menu.addAction(label)
                print_action.triggered.connect(
                    lambda checked=False, s=att.start_page, e=end: self.print_page_range(s, e))
                save_action = attach_save_menu.addAction(label)
                save_action.triggered.connect(
                    lambda checked=False, s=att.start_page, e=end, n=att.name: self.save_page_range(s, e, n))

        menu.exec(self.list_view.viewport().mapToGlobal(pos))

    def _open_mail_window(self, mail: "db.MailRow"):
        window = self.window()
        if not isinstance(window, MainWindow):
            return
        title = (f"{mail.sender_short or '(差出人不明)'} - {mail.subject or '(件名なし)'}"
                 if mail.is_mail else mail.subject)
        window.open_page_range_window(title, self.pdf_path, mail.start_page, mail.end_page,
                                       attachments=mail.attachment_list())

    def _create_outlook_reply(self, mail: "db.MailRow", reply_all: bool = False):
        body_text = db.get_body_text(self.db_path, mail.id) if self.db_path else ""
        try:
            outlook_reply.create_reply_draft(mail, body_text, reply_all=reply_all)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(
                self, "返信の作成に失敗しました",
                "Outlookでの下書き作成に失敗しました。Outlookが起動しているか、"
                f"pywin32がインストールされているか確認してください。\n\n詳細: {e}")

    def _mark_all_read(self):
        if not self.db_path:
            return
        db.mark_all_read(self.db_path, True)
        for row in self.model.all_rows():
            if row.is_mail:
                row.is_read = True
        count = self.model.rowCount()
        if count:
            self.model.dataChanged.emit(self.model.index(0, 0), self.model.index(count - 1, 0), [MAIL_ROLE])
        self._update_count_label()

    def _find_row_for_page(self, page: int) -> int | None:
        """0始まりのページ番号を含むメールを一覧から探す(現在の並び替え/絞り込み後の順序に対して線形探索)。

        件名グループ化中で該当メールが折りたたみで隠れている場合は、そのグループを展開してから探す。
        """
        page_1indexed = page + 1
        mail = next((m for m in self.model.all_rows()
                     if m.start_page <= page_1indexed <= m.end_page), None)
        if mail is None:
            return None

        if self.group_by_subject:
            key = parser.normalize_subject(mail.subject)
            if self.model.is_group_collapsed(key):
                self.model.expand_group(key)

        idx = self.model.index_for_mail_id(mail.id)
        return idx.row() if idx.isValid() else None

    def _sync_selection_to_page(self, page: int):
        row = self._find_row_for_page(page)
        if row is None:
            return

        mail = self.model.mail_at(row)
        if mail is not None:
            self._mark_read(mail)

        if self.list_view.currentIndex().row() == row:
            return

        self._syncing_selection = True
        try:
            self.list_view.setCurrentIndex(self.model.index(row, 0))
        finally:
            self._syncing_selection = False


# -------------------------------------------------------------- 1PDF分のタブ(資料)
class DocumentPdfTab(BasePdfTab):
    """メール形式によらない、しおり付き結合PDFを階層ツリーで閲覧するタブ。

    しおりのタイトルをそのまま見出しとして表示し、ページ範囲はしおりの実際の位置から算出する。
    検索時のみ、ヒットしたしおりをパンくず付きのフラット一覧に切り替える。
    """

    MODE = "document"

    def __init__(self, pdf_path: str, parent=None):
        super().__init__(pdf_path, parent)
        self._all_rows: list[db.SectionRow] = []
        self._built_sort_by_title = False
        self._display_depth = 1   # 表示するしおりの階層数(最上位が1)。開いたときは最上位だけ
        self._max_depth = 1
        self._chosen_scope_id: int | None = None  # 「メール一覧へ」で対象にするしおり
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        splitter = RatioSplitter(Qt.Orientation.Horizontal, 0.3)
        outer.addWidget(splitter)

        left = QWidget()
        left.setObjectName("sidebar")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        info_bar = QWidget()
        info_bar.setObjectName("sortBar")
        info_layout = QHBoxLayout(info_bar)
        info_layout.setContentsMargins(10, 8, 10, 8)
        info_layout.addWidget(self._build_search_box(DOCUMENT_SEARCH_PLACEHOLDER), 1)
        mode_button = QToolButton()
        mode_button.setObjectName("smallButton")
        mode_button.setText("メール一覧へ")
        mode_button.setToolTip("選んだしおり(未選択ならPDF全体)をメール一覧で表示する")
        mode_button.clicked.connect(self._request_scoped_mail)
        info_layout.addWidget(mode_button)
        self.settings_button = QToolButton()
        self.settings_button.setObjectName("smallButton")
        self.settings_button.setText("⚙ 表示設定")
        self.settings_button.setToolTip("しおり一覧の並び順・文字サイズ・折り返しなどを変更")
        self.settings_button.setCheckable(True)
        info_layout.addWidget(self.settings_button)
        left_layout.addWidget(info_bar)

        # 表示するしおりの階層数: 全展開 / ＋ / 数字 / − / 全折り
        depth_bar = QWidget()
        depth_bar.setObjectName("scopeBar")
        depth_bar_layout = QHBoxLayout(depth_bar)
        depth_bar_layout.setContentsMargins(10, 0, 10, 8)
        depth_caption = QLabel("表示する階層:")
        depth_caption.setObjectName("settingsLabel")
        depth_bar_layout.addWidget(depth_caption)
        depth_group = QWidget()
        depth_group.setObjectName("bookmarkDepthGroup")
        depth_layout = QHBoxLayout(depth_group)
        depth_layout.setContentsMargins(5, 4, 5, 4)
        depth_layout.setSpacing(4)
        self.expand_button = QToolButton()
        self.expand_button.setObjectName("smallButton")
        self.expand_button.setText("全展開")
        self.expand_button.clicked.connect(lambda: self._set_display_depth(self._max_depth))
        depth_layout.addWidget(self.expand_button)
        self.deeper_button = QToolButton()
        self.deeper_button.setObjectName("smallButton")
        self.deeper_button.setText("＋")
        self.deeper_button.setToolTip("表示するしおりを1階層増やす")
        self.deeper_button.clicked.connect(lambda: self._set_display_depth(self._display_depth + 1))
        depth_layout.addWidget(self.deeper_button)
        self.depth_label = QLabel("1")
        self.depth_label.setObjectName("bookmarkDepth")
        self.depth_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.depth_label.setMinimumWidth(24)
        self.depth_label.setToolTip("表示するしおりの階層数（最上位は1）")
        depth_layout.addWidget(self.depth_label)
        self.shallower_button = QToolButton()
        self.shallower_button.setObjectName("smallButton")
        self.shallower_button.setText("−")
        self.shallower_button.setToolTip("表示するしおりを1階層減らす")
        self.shallower_button.clicked.connect(lambda: self._set_display_depth(self._display_depth - 1))
        depth_layout.addWidget(self.shallower_button)
        self.collapse_button = QToolButton()
        self.collapse_button.setObjectName("smallButton")
        self.collapse_button.setText("全折り")
        self.collapse_button.clicked.connect(lambda: self._set_display_depth(1))
        depth_layout.addWidget(self.collapse_button)
        depth_bar_layout.addWidget(depth_group)
        depth_bar_layout.addStretch(1)
        left_layout.addWidget(depth_bar)

        self.settings_panel = self._build_settings_panel()
        self.settings_panel.setVisible(False)
        self.settings_button.toggled.connect(self.settings_panel.setVisible)
        left_layout.addWidget(self.settings_panel)

        self.model = SectionTreeModel()
        self.list_view = SectionTreeView()
        self.item_delegate = SectionItemDelegate(self.list_view)
        self.list_view.setModel(self.model)
        self.list_view.setItemDelegate(self.item_delegate)
        self.list_view.setHeaderHidden(True)
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list_view.setUniformRowHeights(not bookmark_settings().get("wrap"))
        self.list_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_view.setFrameShape(QFrame.Shape.NoFrame)
        self.list_view.setIndentation(16)
        self.list_view.setAnimated(True)
        self.list_view.setMouseTracking(True)
        self.list_view.clicked.connect(self._on_index_activated)
        self.list_view.doubleClicked.connect(self._on_index_double_clicked)
        self.list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_view.customContextMenuRequested.connect(self._show_context_menu)
        left_layout.addWidget(self.list_view)
        left_layout.addWidget(self._build_collapse_bar())
        self.sidebar = left
        splitter.addWidget(left)

        splitter.addWidget(self._build_viewer_area())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 7)

        self.list_view.selectionModel().currentChanged.connect(self._on_index_activated)
        bookmark_settings().changed.connect(self._on_view_settings_changed)

    # --------------------------------------------------------- 表示設定パネル
    def _build_settings_panel(self) -> SettingsPanel:
        """ブラウザ版の「しおり一覧の表示設定」と同じ項目を持つパネル。設定は自動で保存され、
        開いているすべての資料タブに反映される。"""
        panel = SettingsPanel("しおり一覧の表示設定", bookmark_settings(),
                              lambda: self.settings_button.setChecked(False))
        panel.add_seg("並び順", "sort", [("pdf", "PDFの順番"), ("title", "名前順")])
        panel.add_seg("文字サイズ", "fontSize", [(12, "小"), (14, "中"), (16, "大"), (18, "特大")])
        panel.add_seg("行の間隔", "density", [("normal", "ゆったり"), ("compact", "詰める")])
        panel.add_seg("長い名前", "wrap", [(True, "折り返す"), (False, "1行で省略")])
        panel.add_seg("ページ番号", "pages", [(True, "表示する"), (False, "表示しない")])
        panel.add_footer(self._reset_view_settings)
        panel.sync()
        return panel

    def _reset_view_settings(self):
        bookmark_settings().reset()
        window = self.window()
        if isinstance(window, QMainWindow) and window.statusBar():
            window.statusBar().showMessage("しおり一覧の表示設定を初期設定に戻しました", 4000)

    def _on_view_settings_changed(self, _reset: bool):
        S = bookmark_settings()
        self.settings_panel.sync()
        self.list_view.setUniformRowHeights(not S.get("wrap"))
        sort_changed = (S.get("sort") == "title") != self._built_sort_by_title
        if not self.current_query.strip() and sort_changed:
            self._rebuild_tree()
        else:
            self.list_view.doItemsLayout()  # 文字サイズ・行間などが変わると行の高さが変わるため
        self.list_view.viewport().update()

    # --------------------------------------------------------- ツリーの展開状態
    def _iter_indexes(self, parent: QModelIndex = QModelIndex()):
        for row in range(self.model.rowCount(parent)):
            idx = self.model.index(row, 0, parent)
            yield idx
            yield from self._iter_indexes(idx)

    def _index_for_section_id(self, section_id: int) -> QModelIndex | None:
        for idx in self._iter_indexes():
            section = self.model.section_at(idx)
            if section is not None and section.id == section_id:
                return idx
        return None

    def _set_display_depth(self, depth: int):
        if self.current_query.strip() or not self._all_rows:
            return
        self._display_depth = min(max(1, depth), self._max_depth)
        self._apply_display_depth()
        self._sync_selection_to_page(self.current_page())
        self._update_depth_controls()

    def _apply_display_depth(self):
        self.list_view.collapseAll()
        if self._display_depth > 1:
            # QtのexpandToDepth(0)は最上位を開き、第2階層まで表示する。
            self.list_view.expandToDepth(self._display_depth - 2)

    def _update_depth_controls(self):
        searching = bool(self.current_query.strip())
        available = bool(self._all_rows) and not searching
        self.depth_label.setText("—" if searching else str(self._display_depth if self._all_rows else 0))
        self.deeper_button.setEnabled(available and self._display_depth < self._max_depth)
        self.shallower_button.setEnabled(available and self._display_depth > 1)
        self.expand_button.setEnabled(available)
        self.collapse_button.setEnabled(available)

    def _set_tree(self):
        sort_by_title = bookmark_settings().get("sort") == "title"
        self.model.set_tree(self._all_rows, sort_by_title=sort_by_title)
        self._built_sort_by_title = sort_by_title

    def _rebuild_tree(self):
        """並び順の変更でツリーを作り直す。開閉状態と選択中のしおりはそのまま保つ。
        PDFの表示位置は動かさない。"""
        expanded: set[int] = set()
        for idx in self._iter_indexes():
            section = self.model.section_at(idx)
            if section is not None and self.list_view.isExpanded(idx):
                expanded.add(section.id)
        current = self.model.section_at(self.list_view.currentIndex())

        self._syncing_selection = True
        try:
            self._set_tree()
            for idx in self._iter_indexes():
                section = self.model.section_at(idx)
                if section is not None and section.id in expanded:
                    self.list_view.expand(idx)
            if current is not None:
                idx = self._index_for_section_id(current.id)
                if idx is not None:
                    self.list_view.setCurrentIndex(idx)
                    self.list_view.scrollTo(idx)
        finally:
            self._syncing_selection = False

    def _update_count_label(self):
        if self.current_query.strip():
            self.count_label.setText(f"{self.model.rowCount()} / {len(self._all_rows)} 件ヒット")
        else:
            self.count_label.setText(f"しおり {len(self._all_rows)} 件 · {self.page_count()} ページ")

    # ------------------------------------------------------------- 動作
    def load(self, force_rebuild: bool = False, status_cb=None) -> bool:
        if status_cb:
            status_cb("インデックス作成中...")
        QApplication.processEvents()
        try:
            self.db_path = db.open_or_build(self.pdf_path, mode=self.MODE, force_rebuild=force_rebuild)
        except Exception as e:  # noqa: BLE001
            if status_cb:
                status_cb(f"インデックス作成に失敗しました: {e}")
            return False

        self.document.load(self.pdf_path)
        self.refresh_list()
        self.page_changed.emit()
        return True

    def refresh_list(self):
        query = self.current_query.strip()
        if not self.db_path:
            self._all_rows = []
            self._set_tree()
            self._update_count_label()
            self._update_depth_controls()
            return

        if query:
            rows = db.search_sections(self.db_path, query)
            self.model.set_flat(rows)
        else:
            self._all_rows = db.list_sections(self.db_path)
            self._set_tree()
            self._max_depth = max((row.level for row in self._all_rows), default=1)
            self._display_depth = min(self._display_depth, self._max_depth)
            self._apply_display_depth()
        self._update_count_label()
        self._update_depth_controls()

        if not self.model.is_empty():
            self.list_view.setCurrentIndex(self.model.index(0, 0))
        else:
            self.pdf_view.pageNavigator().jump(0, QPointF(0, 0))

    def result_count(self) -> int:
        return self.model.rowCount() if self.current_query.strip() else len(self._all_rows)

    def _on_index_activated(self, index: QModelIndex, *_args):
        if not index.isValid() or self.model.is_empty():
            return
        if self._syncing_selection:
            return
        section = self.model.section_at(index)
        if section is None:
            return
        self._chosen_scope_id = section.id
        self.pdf_view.pageNavigator().jump(section.start_page - 1, QPointF(0, 0))

    def _on_index_double_clicked(self, index: QModelIndex):
        if not index.isValid() or self.model.is_empty():
            return
        section = self.model.section_at(index)
        if section is None:
            return
        self._open_section_window(section)

    def _show_context_menu(self, pos):
        index = self.list_view.indexAt(pos)
        if not index.isValid():
            return
        self.list_view.setCurrentIndex(index)
        section = self.model.section_at(index)
        if section is None:
            return

        menu = QMenu(self)
        scope_action = menu.addAction("このしおりの直下をメール一覧で表示")
        scope_action.triggered.connect(lambda: self._request_scoped_mail(section.id))
        all_action = menu.addAction("このしおりの配下すべてをメール一覧で表示")
        all_action.triggered.connect(lambda: self._request_scoped_mail(section.id, True))
        menu.addSeparator()
        page_range = (f"{section.start_page}" if section.start_page == section.end_page
                      else f"{section.start_page}-{section.end_page}")

        window_action = menu.addAction("\U0001F5D4 別ウインドウで開く")
        window_action.triggered.connect(lambda: self._open_section_window(section))
        menu.addSeparator()

        print_action = menu.addAction(f"\U0001F5A8 この区間を印刷... ({page_range}ページ)")
        print_action.triggered.connect(lambda: self.print_page_range(section.start_page, section.end_page))
        save_action = menu.addAction(f"\U0001F4BE この区間をPDFで保存... ({page_range}ページ)")
        save_action.triggered.connect(
            lambda: self.save_page_range(section.start_page, section.end_page, section.title))
        menu.exec(self.list_view.viewport().mapToGlobal(pos))

    def _request_scoped_mail(self, section_id: int | None = None, include_descendants: bool = False):
        # QToolButton.clicked passes a boolean; use the current tree selection for that path.
        if isinstance(section_id, bool):
            section_id = None
        if section_id is None:
            section = self.model.section_at(self.list_view.currentIndex())
            section_id = self._chosen_scope_id if self._chosen_scope_id is not None else (section.id if section else None)
        window = self.window()
        if isinstance(window, MainWindow):
            window._switch_current_mode(section_id, include_descendants)

    def _open_section_window(self, section: "db.SectionRow"):
        window = self.window()
        if not isinstance(window, MainWindow):
            return
        window.open_page_range_window(section.title or "(見出しなし)", self.pdf_path,
                                       section.start_page, section.end_page)

    def _find_index_for_page(self, page: int, parent: QModelIndex = QModelIndex()) -> QModelIndex | None:
        """0始まりのページ番号を含む、最も深い(範囲が狭い)しおりを木構造から探す。"""
        page_1indexed = page + 1
        best: QModelIndex | None = None
        for row in range(self.model.rowCount(parent)):
            idx = self.model.index(row, 0, parent)
            section = self.model.section_at(idx)
            if section and section.start_page <= page_1indexed <= section.end_page:
                best = self._find_index_for_page(page, idx) or idx
                break
        return best

    def _sync_selection_to_page(self, page: int):
        if self.current_query.strip():
            return  # 検索結果のフラット表示中は、ページ追従で一覧を組み替えない
        index = self._find_index_for_page(page)
        # 表示している階層より深いしおりは、見えている一番近い上位のしおりを選ぶ
        while index is not None and index.parent().isValid() and not self.list_view.isExpanded(index.parent()):
            index = index.parent()
        if index is None or self.list_view.currentIndex() == index:
            return

        self._syncing_selection = True
        try:
            self.list_view.setCurrentIndex(index)
            self.list_view.scrollTo(index)
        finally:
            self._syncing_selection = False


# ------------------------------------------------------------ 開始画面(PDF未オープン時)
class RecentPdfListWidget(QListWidget):
    """お気に入り/最近使ったPDFを、サムネイル付きで横に並べて表示する一覧。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setFlow(QListWidget.Flow.LeftToRight)
        self.setWrapping(False)
        self.setMovement(QListWidget.Movement.Static)
        self.setIconSize(QSize(110, 142))
        self.setGridSize(QSize(140, 176))
        self.setSpacing(6)
        self.setWordWrap(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setUniformItemSizes(True)
        self.setFixedHeight(190)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 押せることが分かるよう、ファイルの上では指カーソルにして背景を変える
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        on_item = self.itemAt(event.position().toPoint()) is not None
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if on_item else Qt.CursorShape.ArrowCursor)

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.viewport().unsetCursor()

    MAX_CONTENT_WIDTH = 1000  # これを超える項目数のときは幅を打ち切り、内部の横スクロールに任せる

    def resize_to_content(self):
        """項目数に合わせて実際に必要な幅ちょうどに固定する
        (親レイアウト側のAlignHCenterと組み合わせて中央寄せするため)。
        親レイアウトにAlignHCenterを渡すとQtはこのウィジェットを引き伸ばさず自然な
        サイズで配置するので、幅を明示的に固定しておかないと常にsizeHint()の
        デフォルト値(内容に関わらず固定の256px)で描画されてしまう。
        """
        count = self.count()
        if count == 0:
            return
        self.setMinimumWidth(0)
        self.setMaximumWidth(16777215)  # 一旦解除してから正しい位置でレイアウトさせる
        self.doItemsLayout()
        last_rect = self.visualItemRect(self.item(count - 1))
        content_width = last_rect.right() + 16
        self.setFixedWidth(min(content_width, self.MAX_CONTENT_WIDTH))


class WelcomeWidget(QWidget):
    """PDFを1つも開いていないときに表示する開始画面。操作案内と、お気に入り/最近使った
    PDFのサムネイル一覧を表示する(タブが無いと画面が真っ白になってしまうのを防ぐ)。"""

    THUMB_WIDTH = 110

    def __init__(self, window: "MainWindow", parent=None):
        super().__init__(parent)
        self._window = window
        self._thumb_cache: dict[str, QPixmap] = {}  # f"{path}:{mtime}" -> サムネイル画像
        self.setObjectName("welcome")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._build_ui()

    @property
    def title(self) -> str:
        return "スタート"

    def _build_ui(self):
        # お気に入りと最近使ったPDFの両方が並ぶと画面の高さに収まらないことがある。
        # そのままだと各部品が押し潰されて重なるため、スクロールできる領域に入れる。
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setObjectName("welcomeViewport")
        root.addWidget(scroll)
        content = QWidget()
        content.setObjectName("welcomeContent")
        scroll.setWidget(content)

        outer = QVBoxLayout(content)
        outer.setContentsMargins(40, 32, 40, 32)
        outer.setSpacing(16)
        outer.addStretch(1)

        # ブラウザ版の開始画面と同じ、点線枠のドロップ案内
        drop = QFrame()
        drop.setObjectName("dropZone")
        drop.setMaximumWidth(1000)
        drop_layout = QVBoxLayout(drop)
        drop_layout.setContentsMargins(20, 26, 20, 26)
        drop_layout.setSpacing(6)
        heading = QLabel("メール束PDF・しおり付きPDFを開く")
        heading.setObjectName("dropTitle")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_layout.addWidget(heading)
        guide = QLabel("PDFファイルをここにドラッグ&ドロップするか、下のボタンから開いてください。")
        guide.setObjectName("dropGuide")
        guide.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_layout.addWidget(guide)
        drop_layout.addSpacing(8)
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        open_button = QPushButton("PDFを開く")
        open_button.setObjectName("primaryButton")
        open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        open_button.clicked.connect(self._window.open_pdf_dialog)
        button_row.addWidget(open_button)
        # 図面モード(図面セットの発注前チェック・工事台帳)の入口
        drawing_button = QPushButton("図面セットとして開く")
        drawing_button.setCursor(Qt.CursorShape.PointingHandCursor)
        drawing_button.setToolTip("図面PDFの目次と各図面の表題欄を照合し、要確認の項目を一覧にする")
        drawing_button.clicked.connect(lambda: self._window.open_drawing_set())
        button_row.addWidget(drawing_button)
        ledger_button = QPushButton("工事フォルダを開く")
        ledger_button.setCursor(Qt.CursorShape.PointingHandCursor)
        ledger_button.setToolTip("工事台帳(図面ごとの変更記録)を開く・新しく作る")
        ledger_button.clicked.connect(lambda: self._window.open_ledger_folder())
        button_row.addWidget(ledger_button)
        button_row.addStretch(1)
        drop_layout.addLayout(button_row)
        outer.addWidget(drop, 0, Qt.AlignmentFlag.AlignHCenter)
        drop.setMinimumWidth(560)

        outer.addSpacing(12)

        self.favorites_label = QLabel("★ お気に入り")
        self.favorites_label.setObjectName("sectionHeading")
        self.favorites_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.favorites_label)
        self.favorites_list = RecentPdfListWidget()
        self._wire_list(self.favorites_list)
        outer.addWidget(self.favorites_list, 0, Qt.AlignmentFlag.AlignHCenter)

        self.recent_label = QLabel("最近使ったPDF")
        self.recent_label.setObjectName("sectionHeading")
        self.recent_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.recent_label)
        self.recent_list = RecentPdfListWidget()
        self._wire_list(self.recent_list)
        outer.addWidget(self.recent_list, 0, Qt.AlignmentFlag.AlignHCenter)

        outer.addStretch(1)

    def _wire_list(self, list_widget: "RecentPdfListWidget"):
        # ブラウザ版と同じく1回のクリックで開く
        list_widget.itemClicked.connect(self._on_item_activated)
        list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        list_widget.customContextMenuRequested.connect(
            lambda pos, w=list_widget: self._show_context_menu(w, pos))

    # ------------------------------------------------------------- 表示更新
    def refresh(self):
        self._populate(self.favorites_list, self._window.favorite_files())
        self._populate(self.recent_list, self._window.recent_files())
        has_favorites = self.favorites_list.count() > 0
        has_recent = self.recent_list.count() > 0
        self.favorites_label.setVisible(has_favorites)
        self.favorites_list.setVisible(has_favorites)
        self.recent_label.setVisible(has_recent)
        self.recent_list.setVisible(has_recent)

    def _populate(self, list_widget: "RecentPdfListWidget", paths: list[str]):
        list_widget.clear()
        for path in paths:
            item = QListWidgetItem(os.path.basename(path))
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(path)
            pixmap = self._thumbnail_for(path)
            if pixmap is not None:
                item.setIcon(QIcon(pixmap))
            list_widget.addItem(item)
        list_widget.resize_to_content()

    def _thumbnail_for(self, path: str) -> "QPixmap | None":
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        cache_key = f"{path}:{mtime}"
        cached = self._thumb_cache.get(cache_key)
        if cached is not None:
            return cached
        pixmap = _render_pdf_thumbnail(path, self.THUMB_WIDTH)
        if pixmap is not None:
            self._thumb_cache[cache_key] = pixmap
        return pixmap

    # ------------------------------------------------------------- 操作
    def _on_item_activated(self, item: "QListWidgetItem"):
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            self._window.load_pdf(path)

    def _show_context_menu(self, list_widget: "RecentPdfListWidget", pos):
        item = list_widget.itemAt(pos)
        if item is None:
            return
        path = item.data(Qt.ItemDataRole.UserRole)

        menu = QMenu(self)
        open_action = menu.addAction("開く")
        open_action.triggered.connect(lambda: self._window.load_pdf(path))
        menu.addSeparator()
        if self._window.is_favorite(path):
            fav_action = menu.addAction("お気に入りから外す")
        else:
            fav_action = menu.addAction("お気に入りに追加")
        fav_action.triggered.connect(lambda: self._window.toggle_favorite(path))
        menu.exec(list_widget.viewport().mapToGlobal(pos))


# ------------------------------------------------------------------ 本体
class MainWindow(NoToolbarMenuMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        if avail is not None:
            self.resize(min(1440, int(avail.width() * 0.92)), min(900, int(avail.height() * 0.9)))
        else:
            self.resize(1440, 900)

        self.settings = QSettings("ukawa", APP_NAME)
        # 旧アプリ名で保存された履歴・お気に入りを初回起動時に引き継ぐ。
        legacy_settings = QSettings("ukawa", "MailPDFViewer")
        for key in ("recentFiles", "favoriteFiles"):
            if not self.settings.contains(key) and legacy_settings.contains(key):
                self.settings.setValue(key, legacy_settings.value(key))
        self.setAcceptDrops(True)
        self._sidebar_visible = True  # メール一覧・しおり一覧の表示/非表示(全画面・通常表示どちらでも共通)
        # 全画面にする直前の状態(全画面を終了したときに戻す)
        self._was_maximized_before_full = False
        self._sidebar_before_full = True
        self._geometry_before_full: QRect | None = None
        self._child_windows: list[PageRangeWindow] = []

        self._build_ui()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        # 上部のツールバーは置かず、タブを最上部に置く。ページ送り・表示・全画面は各タブのPDF表示の上、
        # 検索欄と表示設定は一覧の上、件数は一覧の下にある。
        self.status = QStatusBar()
        self.setStatusBar(self.status)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._on_current_tab_changed)
        self.tabs.tabBarClicked.connect(self._on_tab_bar_clicked)
        tab_bar = self.tabs.tabBar()
        tab_bar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tab_bar.customContextMenuRequested.connect(self._show_tab_context_menu)

        # タブ一覧の末尾に固定表示する「+」タブ(閉じるボタンなし)。クリックすると
        # 開始画面(お気に入り・最近使ったPDF・PDFを開くボタン)をタブとして開く。
        self._plus_widget = QWidget()
        plus_index = self.tabs.addTab(self._plus_widget, "+")
        self.tabs.tabBar().setTabButton(plus_index, QTabBar.ButtonPosition.RightSide, None)
        self.tabs.setTabToolTip(plus_index, "開始画面を表示（PDFを開く・最近使ったPDF）")

        self.setCentralWidget(self.tabs)
        self._install_shortcuts()

        # 起動直後、PDFを1つも開いていないとき画面が真っ白にならないよう開始画面を開く。
        self.open_welcome_tab()

    def _install_shortcuts(self):
        # 全画面の状態を持つアクション(ボタンは各タブのPDF表示の上にある)
        self.fullscreen_action = QAction("全画面表示", self)
        self.fullscreen_action.setCheckable(True)
        self.fullscreen_action.toggled.connect(self._on_fullscreen_toggled)

        # ボタンを持たない操作もキーで使えるよう、ウインドウ直付けのショートカットにする
        for key, slot in [
            ("Ctrl+O", self.open_pdf_dialog),
            ("Ctrl+F", self._focus_search),
            ("Esc", self._on_escape_pressed),
            ("Ctrl+G", self._focus_page_input),
            ("Ctrl+B", self._toggle_sidebar),
            ("F11", self.fullscreen_action.toggle),
            (Qt.Key.Key_Left, self.go_prev_page),
            (Qt.Key.Key_Right, self.go_next_page),
        ]:
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(slot)

    # 現在のタブのページ番号欄・検索欄(各タブのPDF表示の上・一覧の上にある)
    @property
    def page_control(self) -> "PageNumberControl":
        tab = self._current_tab()
        if isinstance(tab, BasePdfTab):
            return tab.page_control
        if not hasattr(self, "_idle_page_control"):
            self._idle_page_control = PageNumberControl()  # PDFを開いていないときの空の欄
        return self._idle_page_control

    @property
    def search_box(self) -> QLineEdit:
        tab = self._current_tab()
        if isinstance(tab, BasePdfTab):
            return tab.search_box
        if not hasattr(self, "_idle_search_box"):
            self._idle_search_box = QLineEdit()
        return self._idle_search_box

    def _focus_page_input(self):
        self.page_control.focus_input()

    def _focus_search(self):
        tab = self._current_tab()
        if isinstance(tab, BasePdfTab):
            tab.focus_search()

    # --------------------------------------------------------- ドラッグ&ドロップ
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls() and any(
            u.toLocalFile().lower().endswith(".pdf") for u in event.mimeData().urls()
        ):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith(".pdf"):
                self.load_pdf(path)
        event.acceptProposedAction()

    # --------------------------------------------------------------- 最近使ったPDF
    def _recent_files(self) -> list[str]:
        return [p for p in self.settings.value("recentFiles", [], type=list) if os.path.exists(p)]

    def recent_files(self) -> list[str]:
        return self._recent_files()

    def _add_recent_file(self, path: str):
        path = os.path.abspath(path)
        files = [p for p in self._recent_files() if os.path.abspath(p) != path]
        files.insert(0, path)
        self.settings.setValue("recentFiles", files[:MAX_RECENT_FILES])

    # --------------------------------------------------------------- お気に入り
    def favorite_files(self) -> list[str]:
        return [p for p in self.settings.value("favoriteFiles", [], type=list) if os.path.exists(p)]

    def is_favorite(self, path: str) -> bool:
        path = os.path.abspath(path)
        return any(os.path.abspath(p) == path for p in self.favorite_files())

    def toggle_favorite(self, path: str):
        path = os.path.abspath(path)
        current = self.favorite_files()
        if any(os.path.abspath(p) == path for p in current):
            updated = [p for p in current if os.path.abspath(p) != path]
        else:
            updated = current + [path]
        self.settings.setValue("favoriteFiles", updated)
        self._update_favorite_stars()
        welcome_index = self._find_welcome_tab_index()
        if welcome_index >= 0:
            self.tabs.widget(welcome_index).refresh()

    def _install_favorite_star(self, tab: "BasePdfTab"):
        """タブの左端に☆/★ボタンを置き、クリックでお気に入りを登録/解除できるようにする。"""
        button = QToolButton()
        button.setObjectName("tabStar")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda: self.toggle_favorite(tab.pdf_path))
        self.tabs.tabBar().setTabButton(self.tabs.indexOf(tab), QTabBar.ButtonPosition.LeftSide, button)
        self._update_favorite_stars()

    def _update_favorite_stars(self):
        bar = self.tabs.tabBar()
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            button = bar.tabButton(i, QTabBar.ButtonPosition.LeftSide)
            if isinstance(tab, BasePdfTab) and isinstance(button, QToolButton):
                fav = self.is_favorite(tab.pdf_path)
                button.setText("★" if fav else "☆")
                button.setProperty("fav", fav)
                button.setToolTip("お気に入りから外す" if fav else "お気に入りに登録")
                button.style().unpolish(button)
                button.style().polish(button)

    def _show_tab_context_menu(self, pos):
        bar = self.tabs.tabBar()
        index = bar.tabAt(pos)
        tab = self.tabs.widget(index) if index >= 0 else None
        menu = QMenu(self)
        if isinstance(tab, BasePdfTab):
            fav = self.is_favorite(tab.pdf_path)
            menu.addAction("★ お気に入りから外す" if fav else "☆ お気に入りに登録",
                           lambda: self.toggle_favorite(tab.pdf_path))
            menu.addAction("再インデックス（一覧を作り直す）", lambda: self.load_pdf(tab.pdf_path, force_rebuild=True))
            menu.addSeparator()
        menu.addAction("PDFを開く...  (Ctrl+O)", self.open_pdf_dialog)
        menu.addAction("図面セットとして開く...",
                       lambda: self.open_drawing_set(tab.pdf_path if isinstance(tab, BasePdfTab) else None))
        menu.addAction("工事フォルダ(工事台帳)を開く...", self.open_ledger_folder)
        recent = self._recent_files()
        if recent:
            recent_menu = menu.addMenu("最近使ったPDF")
            for path in recent:
                action = recent_menu.addAction(os.path.basename(path), lambda p=path: self.load_pdf(p))
                action.setToolTip(path)
        if tab is not None and tab is not self._plus_widget:
            menu.addSeparator()
            menu.addAction("このタブを閉じる", lambda: self._close_tab(self.tabs.indexOf(tab)))
        menu.exec(bar.mapToGlobal(pos))

    # --------------------------------------------------------------- タブ管理
    def _find_tab_index(self, path: str) -> int:
        path = os.path.abspath(path)
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if isinstance(tab, BasePdfTab) and os.path.abspath(tab.pdf_path) == path:
                return i
        return -1

    def _find_welcome_tab_index(self) -> int:
        for i in range(self.tabs.count()):
            if isinstance(self.tabs.widget(i), WelcomeWidget):
                return i
        return -1

    def _plus_tab_index(self) -> int:
        """常に末尾に固定表示している「+」タブの現在位置(通常タブ挿入時の基準位置)。"""
        idx = self.tabs.indexOf(self._plus_widget)
        return idx if idx >= 0 else self.tabs.count()

    def _install_close_button(self, widget: QWidget):
        """タブの閉じるボタンを、ブラウザ版と同じ控えめな「×」にする(Fusion標準のアイコンの代わり)。"""
        button = QToolButton()
        button.setObjectName("tabClose")
        button.setText("×")
        button.setToolTip("閉じる")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda: self._close_tab(self.tabs.indexOf(widget)))
        self.tabs.tabBar().setTabButton(self.tabs.indexOf(widget), QTabBar.ButtonPosition.RightSide, button)

    def _on_tab_bar_clicked(self, index: int):
        if self.tabs.widget(index) is self._plus_widget:
            self.open_welcome_tab()

    def open_welcome_tab(self):
        """開始画面(お気に入り・最近使ったPDF)を新しいタブとして開く(既にあればそこへ切り替える)。"""
        existing = self._find_welcome_tab_index()
        if existing >= 0:
            widget = self.tabs.widget(existing)
            widget.refresh()
            self.tabs.setCurrentIndex(existing)
            return
        welcome = WelcomeWidget(self)
        welcome.refresh()
        idx = self.tabs.insertTab(self._plus_tab_index(), welcome, welcome.title)
        self._install_close_button(welcome)
        self.tabs.setCurrentIndex(idx)

    def open_pdf_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "PDFを開く", "", "PDF Files (*.pdf)")
        for path in paths:
            self.load_pdf(path)

    def load_pdf(self, path: str, force_rebuild: bool = False):
        existing = self._find_tab_index(path)
        if existing >= 0 and not force_rebuild:
            self.tabs.setCurrentIndex(existing)
            return

        if existing >= 0:
            tab = self.tabs.widget(existing)
        else:
            mode = parser.detect_mode(path)
            tab_cls = PdfTab if mode == "mail" else DocumentPdfTab
            tab = tab_cls(path)
            self.tabs.insertTab(self._plus_tab_index(), tab, tab.title)
            self._install_close_button(tab)
            self._install_favorite_star(tab)

        ok = tab.load(force_rebuild=force_rebuild, status_cb=self.status.showMessage)
        idx = self.tabs.indexOf(tab)
        self.tabs.setTabToolTip(idx, path)
        self.tabs.setCurrentIndex(idx)
        # PDFを開いたら、役目を終えた開始画面タブは閉じてタブバーを整理する。
        welcome_index = self._find_welcome_tab_index()
        if welcome_index >= 0:
            welcome_widget = self.tabs.widget(welcome_index)
            self.tabs.removeTab(welcome_index)
            welcome_widget.deleteLater()
        if ok:
            self._add_recent_file(path)
            unit = getattr(tab, "RESULT_UNIT", "件のメール" if isinstance(tab, PdfTab) else "件のしおり")
            self.status.showMessage(
                f"{tab.result_count()} {unit}を読み込みました{self._unread_suffix(tab)}", 5000)

    # --------------------------------------------------------------- 図面モード(drawing_tab.py)
    def open_drawing_set(self, path: str | None = None):
        """図面セットとして開く(発注前チェックの結果一覧つき)。pathがなければファイルを選ぶ。"""
        import drawing_tab
        drawing_tab.open_drawing_set(self, path)

    def open_ledger_folder(self, folder: str | None = None):
        """工事フォルダの工事台帳を開く(なければ新しく作る)。"""
        import drawing_tab
        drawing_tab.open_ledger_folder(self, folder)

    # --------------------------------------------------------------- 別ウインドウ表示
    def open_page_range_window(self, title: str, pdf_path: str, start_page: int, end_page: int,
                                attachments: "list[db.AttachmentInfo] | None" = None):
        try:
            win = PageRangeWindow(title, pdf_path, start_page, end_page, attachments=attachments)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "別ウインドウで開けませんでした", f"詳細: {e}")
            return
        self._child_windows.append(win)
        win.destroyed.connect(lambda _=None, w=win: self._forget_child_window(w))
        win.show()

    def _forget_child_window(self, win: "PageRangeWindow"):
        if win in self._child_windows:
            self._child_windows.remove(win)

    def _close_tab(self, index: int):
        tab = self.tabs.widget(index)
        if tab is self._plus_widget:
            return  # 「+」タブは閉じるボタンが無いため通常は到達しないが、念のため保護する
        self.tabs.removeTab(index)
        if isinstance(tab, BasePdfTab):
            tab.document.close()
        if tab is not None:
            tab.deleteLater()
        # 「+」タブ以外に何も残らないと画面が真っ白になってしまうため、開始画面を開いておく。
        if self.tabs.count() <= 1:
            self.open_welcome_tab()

    def _current_tab(self) -> "BasePdfTab | WelcomeWidget | None":
        return self.tabs.currentWidget()

    def _switch_current_mode(self, scope_toc_index: int | None = None, include_descendants: bool = False):
        """現在のタブを、同じPDFのメール一覧⇔しおり一覧に切り替える(同じタブ位置・同じページのまま)。"""
        old_tab = self._current_tab()
        if not isinstance(old_tab, BasePdfTab):
            return
        tab_cls = DocumentPdfTab if isinstance(old_tab, PdfTab) else PdfTab
        new_tab = tab_cls(old_tab.pdf_path)
        new_tab.current_query = old_tab.current_query
        new_tab.search_box.blockSignals(True)
        new_tab.search_box.setText(old_tab.current_query)
        new_tab.search_box.blockSignals(False)
        new_tab.set_zoom_mode(old_tab.pdf_view.zoomMode())
        if not new_tab.load(status_cb=self.status.showMessage):
            new_tab.document.close()
            new_tab.deleteLater()
            return

        if isinstance(new_tab, PdfTab):
            new_tab.set_scope(scope_toc_index, include_descendants)

        page = old_tab.current_page()
        index = self.tabs.indexOf(old_tab)
        title = self.tabs.tabText(index)
        tooltip = self.tabs.tabToolTip(index)
        new_tab.pdf_view.pageNavigator().jump(page, QPointF(0, 0))
        self.tabs.insertTab(index, new_tab, title)
        self.tabs.setTabToolTip(index, tooltip)
        self._install_close_button(new_tab)
        self._install_favorite_star(new_tab)
        self.tabs.setCurrentWidget(new_tab)
        self.tabs.removeTab(self.tabs.indexOf(old_tab))
        old_tab.document.close()
        old_tab.deleteLater()
        self._apply_fullscreen_chrome()

    def _unread_suffix(self, tab: BasePdfTab) -> str:
        count = tab.unread_count()
        return f" (未読 {count})" if count else ""

    def _on_current_tab_changed(self, _index: int):
        self._apply_fullscreen_chrome()
        tab = self._current_tab()
        if isinstance(tab, BasePdfTab):
            self.status.showMessage(f"{tab.result_count()} 件表示中{self._unread_suffix(tab)}")

    # --------------------------------------------------------------- 動作
    def reindex_current(self):
        tab = self._current_tab()
        if isinstance(tab, BasePdfTab):
            self.load_pdf(tab.pdf_path, force_rebuild=True)

    def go_prev_page(self):
        tab = self._current_tab()
        if isinstance(tab, BasePdfTab):
            tab.go_prev_page()

    def go_next_page(self):
        tab = self._current_tab()
        if isinstance(tab, BasePdfTab):
            tab.go_next_page()

    def _on_escape_pressed(self):
        """ページ番号の入力中は取り消し、全画面表示中はEscで解除、それ以外は検索ボックスをクリアする。"""
        if self.page_control.input.hasFocus():
            self.page_control.cancel()
        elif self.isFullScreen():
            self.fullscreen_action.setChecked(False)
        else:
            tab = self._current_tab()
            if isinstance(tab, BasePdfTab):
                tab.search_box.clear()

    # --------------------------------------------------------------- 全画面表示
    def _on_fullscreen_toggled(self, checked: bool):
        if checked:
            # 全画面表示に入るときは、スライドショーのようにPDFを大きく見せるため
            # 一覧を自動的に畳む(全画面中はCtrl+B/右クリックで出し入れできる)。
            # 終了時に元へ戻せるよう、直前の最大化・一覧の表示状態を覚えておく。
            self._was_maximized_before_full = self.isMaximized()
            self._geometry_before_full = None if self.isMaximized() else self.geometry()
            self._sidebar_before_full = self._sidebar_visible
            self._sidebar_visible = False
            self.showFullScreen()
        else:
            # showNormal()だと最大化前の小さいウインドウサイズに戻ってしまうため、
            # 全画面の前に最大化していたなら最大化に戻す。一覧も全画面前の表示状態に戻す。
            self._sidebar_visible = self._sidebar_before_full
            if self._was_maximized_before_full:
                self.showMaximized()
            else:
                self.showNormal()
                geometry = self._geometry_before_full
                if geometry is not None:
                    # OSによる復元はツールバー等の分だけずれることがあるため、全画面前の位置・大きさを当て直す
                    self.setGeometry(geometry)
                    QTimer.singleShot(0, lambda g=geometry: self.setGeometry(g) if not self.isFullScreen() else None)
        self._apply_fullscreen_chrome()

    def _toggle_sidebar(self):
        """メール一覧・しおり一覧の表示/非表示を切り替える(全画面・通常表示どちらでも使える)。"""
        self._sidebar_visible = not self._sidebar_visible
        self._apply_fullscreen_chrome()

    def _apply_fullscreen_chrome(self):
        """全画面時はタブとステータスバーを畳んで、PDF表示を大きく使う。PDF表示の上のページ送り・
        表示の切り替え・全画面ボタンは全画面でも使える。一覧の表示/非表示は
        全画面・通常表示に共通の状態(_sidebar_visible)に従う。"""
        full = self.isFullScreen()
        self.tabs.tabBar().setVisible(not full)
        self.status.setVisible(not full)
        tab = self._current_tab()
        if isinstance(tab, BasePdfTab):
            tab.set_sidebar_visible(self._sidebar_visible)
            tab.set_fullscreen_state(full)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            is_full = bool(self.windowState() & Qt.WindowState.WindowFullScreen)
            if self.fullscreen_action.isChecked() != is_full:
                self.fullscreen_action.blockSignals(True)
                self.fullscreen_action.setChecked(is_full)
                self.fullscreen_action.blockSignals(False)
            self._apply_fullscreen_chrome()
            if not self.isMaximized() and not self.isFullScreen():
                # 最大化を解除した直後は、以前のウィンドウサイズ・位置(別のモニターに
                # 合わせたものかもしれない)に戻るため、一覧下部の「一覧を隠す」ボタン
                # などが画面外に出ないよう現在の画面内に収まるよう調整する。
                # サイズと位置の復元はOS側で別々の非同期処理として行われ、この
                # イベント処理の直後やresizeEventの時点ではまだ両方とも確定していない
                # ことがあるため、少し待ってから(両方の復元が完了した後に)実行する。
                QTimer.singleShot(200, self._clamp_to_screen)

    def _clamp_to_screen(self):
        if self.isMaximized() or self.isFullScreen():
            return  # 待っている間に最大化・全画面になった場合は調整しない(サイズを縮めてしまうため)
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        frame = self.frameGeometry()
        width = min(frame.width(), avail.width())
        height = min(frame.height(), avail.height())
        x = min(max(frame.x(), avail.left()), avail.right() - width + 1)
        y = min(max(frame.y(), avail.top()), avail.bottom() - height + 1)
        if (width, height) != (frame.width(), frame.height()):
            self.resize(width - (frame.width() - self.width()), height - (frame.height() - self.height()))
        if (x, y) != (frame.x(), frame.y()):
            self.move(x, y)


def _build_stylesheet() -> str:
    """ブラウザ版(MailPDFViewer.html)と同じ配色・角丸・余白のスタイルシート。"""
    css = """
QMainWindow { background: @BG; }
QToolTip { color: @TEXT; background: @PANEL; border: 1px solid @LINE2; padding: 4px 6px; }

/* ツールバー */
QToolBar { background: @PANEL; border: none; border-bottom: 1px solid @LINE; padding: 6px 10px; spacing: 6px; }
QToolBar::separator { background: @LINE; width: 1px; margin: 4px 6px; }
QToolBar QLabel { color: @SUB; }
/* PDF表示の上のバー(ページ送り・表示・全画面) */
#viewerBar { background: @PANEL; border-bottom: 1px solid @LINE; }
#pageInput { border-radius: 6px; padding: 3px 6px; }
#searchBox { padding: 4px 8px; }
QToolButton#tabStar { border: none; background: transparent; color: @FAINT; padding: 0 2px; font-size: 11pt; }
QToolButton#tabStar:hover { color: #E0A100; }
QToolButton#tabStar[fav="true"] { color: #E0A100; }
#pageLabel { color: @TEXT; font-weight: 600; }

/* ボタン(.btn) */
QToolButton, QPushButton { border: 1px solid @LINE2; border-radius: 6px; padding: 4px 10px; background: @PANEL; color: @TEXT; }
QToolButton:hover, QPushButton:hover { background: @PANEL2; }
QToolButton:checked { background: @ACCENT_BG; border-color: @ACCENT_LINE; color: @ACCENT; }
QToolButton:disabled, QPushButton:disabled { color: #B4BAC2; border-color: @LINE; }
QToolButton::menu-indicator { image: none; width: 0; }
#primaryButton { background: @ACCENT; border-color: @ACCENT; color: #FFFFFF; font-weight: 600; }
#primaryButton:hover { background: #3B74F0; }
QPushButton#primaryButton { padding: 8px 22px; font-size: 11pt; }
#smallButton { padding: 1px 8px; font-size: 8pt; }

/* 入力欄 */
QLineEdit { border: 1px solid @LINE2; border-radius: 8px; padding: 5px 8px; background: @PANEL; color: @TEXT; selection-background-color: @ACCENT_LINE; }
QLineEdit:focus { border: 1px solid @ACCENT; }
QComboBox { border: 1px solid @LINE2; border-radius: 6px; padding: 4px 8px; background: @PANEL; color: @TEXT; }
QComboBox:hover { background: @PANEL2; }
QComboBox QAbstractItemView { background: @PANEL; border: 1px solid @LINE2; selection-background-color: @ACCENT_BG; selection-color: @ACCENT; outline: 0; }

/* メニュー */
QMenu { background: @PANEL; border: 1px solid @LINE2; padding: 4px; }
QMenu::item { padding: 6px 14px; border-radius: 5px; color: @TEXT; }
QMenu::item:selected { background: @ACCENT_BG; color: @ACCENT; }
QMenu::item:disabled { color: #B4BAC2; }
QMenu::separator { height: 1px; background: @LINE; margin: 4px 2px; }

/* タブ */
QTabWidget::pane { border: none; border-top: 1px solid @LINE; }
QTabWidget::tab-bar { left: 8px; }
QTabBar { background: @BG; }
QTabBar::tab { padding: 6px 12px; margin-top: 4px; margin-right: 2px; background: @PANEL2; color: @SUB;
               border: 1px solid @LINE; border-bottom: none; border-top-left-radius: 8px; border-top-right-radius: 8px; }
QTabBar::tab:hover { color: @TEXT; }
QTabBar::tab:selected { background: @PANEL; color: @TEXT; font-weight: 600; }
QToolButton#tabClose { border: none; background: transparent; color: @FAINT; padding: 0 4px; border-radius: 4px; font-size: 11pt; }
QToolButton#tabClose:hover { background: @LINE; color: @TEXT; }

/* 一覧(サイドバー) */
#sidebar { background: @PANEL; }
QListView, QTreeView { background: @PANEL; border: none; outline: 0; }
QTreeView { show-decoration-selected: 0; }
QTreeView::item:hover, QTreeView::item:selected { background: transparent; }
#sortBar { background: @PANEL; border-bottom: 1px solid @LINE; }
#collapseBar { background: @PANEL; border-top: 1px solid @LINE; }
#countLabel { color: @SUB; font-size: 8pt; }
QSplitter::handle { background: @LINE; }
QSplitter::handle:hover { background: @ACCENT_LINE; }

/* 一覧の対象・しおりの階層操作 */
#scopeBar { background: @PANEL; border-bottom: 1px solid @LINE; }
#bookmarkDepthGroup { background: @ACCENT_BG; border: 1px solid @ACCENT_LINE; border-radius: 8px; }
#bookmarkDepth { color: @ACCENT; font-weight: 700; }

/* しおり一覧の表示設定パネル */
#settingsPanel { background: @PANEL2; border-bottom: 1px solid @LINE; }
#settingsTitle { color: @TEXT; font-weight: 700; }
#settingsLabel { color: @SUB; font-size: 8pt; }
#settingsNote { color: @FAINT; font-size: 8pt; }
QToolButton#segButton { border-radius: 0; padding: 3px 10px; background: @PANEL; border: 1px solid @LINE2; }
QToolButton#segButton[segPos="mid"], QToolButton#segButton[segPos="last"] { border-left: none; }
QToolButton#segButton[segPos="first"] { border-top-left-radius: 7px; border-bottom-left-radius: 7px; }
QToolButton#segButton[segPos="last"] { border-top-right-radius: 7px; border-bottom-right-radius: 7px; }
QToolButton#segButton[segPos="only"] { border-radius: 7px; }
QToolButton#segButton:hover { background: @PANEL2; }
QToolButton#segButton:checked { background: @ACCENT; color: #FFFFFF; }

/* 開始画面 */
#welcome, #welcomeViewport, #welcomeContent { background: @BG; }
#welcome QListView { background: transparent; }
#welcome QListView::item { border: 1px solid transparent; border-radius: 10px; padding: 4px; color: @TEXT; }
#welcome QListView::item:hover { background: @PANEL; border-color: @ACCENT; }
#dropZone { background: @PANEL; border: 2px dashed @LINE2; border-radius: 14px; }
#dropZone:hover { border-color: @ACCENT; }
#dropTitle { color: @TEXT; font-size: 15pt; font-weight: 700; }
#dropGuide { color: @SUB; }
#sectionHeading { color: @TEXT; font-weight: 700; }

QStatusBar { background: @PANEL; color: @SUB; border-top: 1px solid @LINE; }
"""
    tokens = {
        "@ACCENT_LINE": C_ACCENT_LINE, "@ACCENT_BG": C_ACCENT_BG, "@ACCENT": C_ACCENT,
        "@PANEL2": C_PANEL2, "@PANEL": C_PANEL, "@LINE2": C_LINE2, "@LINE": C_LINE,
        "@TEXT": C_TEXT, "@SUB": C_SUB, "@FAINT": C_FAINT, "@BG": C_BG,
    }
    for token, color in tokens.items():  # 長いトークン名から置換する(@PANEL2 と @PANEL など)
        css = css.replace(token, color)
    return css


def _light_palette() -> QPalette:
    """Windowsのダークモード設定に引きずられず、ブラウザ版と同じ明るい配色にする。"""
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(C_BG))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(C_TEXT))
    pal.setColor(QPalette.ColorRole.Base, QColor(C_PANEL))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(C_PANEL2))
    pal.setColor(QPalette.ColorRole.Text, QColor(C_TEXT))
    pal.setColor(QPalette.ColorRole.Button, QColor(C_PANEL))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(C_TEXT))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(C_ACCENT))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(C_PANEL))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(C_TEXT))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(C_FAINT))
    pal.setColor(QPalette.ColorRole.Mid, QColor(C_LINE2))
    pal.setColor(QPalette.ColorRole.Dark, QColor(C_VIEWER))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor("#B4BAC2"))
    return pal


def _set_application_icon(app: QApplication):
    """Python実行とstandaloneで同じアイコンをQtの各ウィンドウに設定する。"""
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "assets", "icons", "kojiPDFviewer.ico")
    if os.path.isfile(icon_path):
        icon = QIcon(icon_path)
        if not icon.isNull():
            app.setWindowIcon(icon)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    _set_application_icon(app)
    # OS標準(windows11)スタイルはスタイルシートとの相性が悪く見た目が崩れるため、
    # 素直に描画されるFusionを土台にしてブラウザ版の配色を当てる。
    app.setStyle("Fusion")
    app.setPalette(_light_palette())
    font = QFont()
    font.setFamilies(UI_FONT_FAMILIES)
    font.setPointSize(10)
    app.setFont(font)
    app.setStyleSheet(_build_stylesheet())
    win = MainWindow()
    win.showMaximized()
    if len(sys.argv) > 1:
        win.load_pdf(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
