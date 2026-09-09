"""メールPDF閲覧アプリ

KojiPDFが出力した「メール束PDF」を読み込み、メール一覧(差出人・件名・宛先・日時・
添付・本文冒頭)をカード形式で表示し、全文検索・ページジャンプ・PDF内ハイライトを行う。
しおりの構造が上記の形式に一致しない場合は、メール以外の一般資料を結合したPDFとみなし、
しおりをそのまま階層ツリーとして閲覧するモードに自動的に切り替わる。
複数のPDFをタブで同時に開ける。
"""
from __future__ import annotations

import os
import re
import sys

from PySide6.QtCore import (
    QAbstractListModel,
    QEvent,
    QModelIndex,
    QPointF,
    QRect,
    QSettings,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QAction, QBrush, QColor, QCursor, QDragEnterEvent, QDropEvent, QFont, QFontMetrics, QKeySequence, QPainter, QPen, QShortcut, QStandardItem, QStandardItemModel
from PySide6.QtPdf import QPdfDocument, QPdfSearchModel
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSplitter,
    QStatusBar,
    QStyle,
    QStyledItemDelegate,
    QTabWidget,
    QToolBar,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

import db
import outlook_reply
import parser

MAIL_ROLE = Qt.UserRole + 1
SECTION_ROLE = Qt.UserRole + 1
HEADER_ROLE = Qt.UserRole + 3
MAX_RECENT_FILES = 10

MAIL_SEARCH_PLACEHOLDER = "件名・差出人・宛先・本文・添付ファイル名で検索  (Ctrl+F)"
DOCUMENT_SEARCH_PLACEHOLDER = "しおりの見出し・本文で検索  (Ctrl+F)"

AVATAR_PALETTE = [
    "#4C6EF5", "#F76707", "#2F9E44", "#E64980", "#7048E8",
    "#1098AD", "#F08C00", "#0CA678", "#D6336C", "#5C7CFA",
]

SORT_DIR_LABELS = {
    "date": ("新しい順 ↓", "古い順 ↑"),
    "subject": ("Z→A ↓", "A→Z ↑"),
    "sender": ("Z→A ↓", "A→Z ↑"),
}


def _avatar_color(name: str) -> QColor:
    if not name:
        return QColor("#9AA0A8")
    return QColor(AVATAR_PALETTE[sum(map(ord, name)) % len(AVATAR_PALETTE)])


def _format_date(title_datetime: str, fallback: str) -> str:
    if title_datetime:
        return title_datetime.replace("T", "  ")
    return fallback


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
        return sum(1 for row in self._all_rows if not row.is_read)


# --------------------------------------------------------------- デリゲート
class MailItemDelegate(QStyledItemDelegate):
    ROW_HEIGHT = 104
    HEADER_HEIGHT = 34
    AVATAR_SIZE = 40
    CHIP_FONT_SIZE = 8
    CHIP_HEIGHT = 22
    CHIP_MAX_WIDTH = 190
    CHIP_GAP = 6

    attachment_clicked = Signal(QModelIndex, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hover_chip: tuple[int, int] | None = None  # (row, chip_index)

    def sizeHint(self, option, index):
        if index.data(HEADER_ROLE) is not None:
            return QSize(option.rect.width(), self.HEADER_HEIGHT)
        return QSize(option.rect.width(), self.ROW_HEIGHT)

    def _text_geometry(self, rect: QRect) -> tuple[int, int, int]:
        avatar_rect_left = rect.left() + 18
        text_left = avatar_rect_left + self.AVATAR_SIZE + 16
        text_right = rect.right() - 18
        return text_left, text_right, max(10, text_right - text_left)

    @staticmethod
    def _row_positions(rect: QRect) -> tuple[int, int, int, int]:
        """各行のY座標(差出人行/件名行/宛先行/添付or本文プレビュー行)。paint()とeditorEvent()で共有する。"""
        y = rect.top() + 10
        y2 = y + 21
        y3 = y2 + 20
        y4 = y3 + 19
        return y, y2, y3, y4

    def _attachment_chips(self, rect: QRect, mail: "db.MailRow", y: int):
        """添付チップの矩形リストを返す。paint()・editorEvent()・ホバー検出で同じ計算式を共有する。"""
        text_left, text_right, _ = self._text_geometry(rect)
        font = QFont(); font.setPointSize(self.CHIP_FONT_SIZE)
        fm = QFontMetrics(font)

        chips = []
        x = text_left
        attachments = mail.attachment_list()
        for i, att in enumerate(attachments):
            label = f"\U0001F4CE {att.name}"
            raw_w = fm.horizontalAdvance(label) + 16
            w = min(raw_w, self.CHIP_MAX_WIDTH)
            if x + w > text_right:
                remaining_atts = attachments[i:]
                extra_label = f"+{len(remaining_atts)}"
                extra_w = fm.horizontalAdvance(extra_label) + 20
                if x + extra_w <= text_right:
                    chips.append({"rect": QRect(x, y, extra_w, self.CHIP_HEIGHT), "attachment": None,
                                  "label": extra_label, "clickable": True, "kind": "overflow",
                                  "remaining": remaining_atts})
                break
            chips.append({"rect": QRect(x, y, w, self.CHIP_HEIGHT), "attachment": att,
                          "label": label, "clickable": att.start_page is not None, "kind": "attachment"})
            x += w + self.CHIP_GAP
        return chips, font

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

        if selected:
            painter.fillRect(rect, QColor("#E4EDFC"))
            painter.fillRect(QRect(rect.left(), rect.top(), 3, rect.height()), QColor("#2F6FE4"))
        elif hovered:
            painter.fillRect(rect, QColor("#F5F7FA"))
        else:
            painter.fillRect(rect, QColor("#FFFFFF"))

        painter.setPen(QColor("#E9EBEF"))
        painter.drawLine(rect.left() + 20, rect.bottom(), rect.right() - 20, rect.bottom())

        if not mail.is_read:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor("#2F6FE4")))
            painter.drawEllipse(QRect(rect.left() + 6, rect.top() + rect.height() // 2 - 4, 8, 8))

        avatar_rect = QRect(rect.left() + 18, rect.top() + (rect.height() - self.AVATAR_SIZE) // 2,
                             self.AVATAR_SIZE, self.AVATAR_SIZE)
        painter.setBrush(QBrush(_avatar_color(mail.sender_short)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(avatar_rect)
        painter.setPen(QColor("#FFFFFF"))
        f = QFont(); f.setBold(True); f.setPointSize(13)
        painter.setFont(f)
        initial = (mail.sender_short or "?")[0].upper()
        painter.drawText(avatar_rect, Qt.AlignmentFlag.AlignCenter, initial)

        text_left, text_right, text_width = self._text_geometry(rect)

        date_text = _format_date(mail.title_datetime, mail.received_at)
        f_date = QFont(); f_date.setPointSize(9)
        fm_date = QFontMetrics(f_date)
        date_w = fm_date.horizontalAdvance(date_text)

        y, y2, y3, y4 = self._row_positions(rect)
        f1 = QFont(); f1.setBold(not mail.is_read); f1.setPointSize(10)
        painter.setFont(f1)
        painter.setPen(QColor("#16181D") if not mail.is_read else QColor("#4A4F58"))
        sender_rect = QRect(text_left, y, max(10, text_width - date_w - 12), 20)
        painter.drawText(sender_rect, Qt.AlignmentFlag.AlignVCenter,
                          QFontMetrics(f1).elidedText(mail.sender_short or "(差出人不明)",
                                                       Qt.TextElideMode.ElideRight, sender_rect.width()))
        painter.setFont(f_date)
        painter.setPen(QColor("#4A4F58"))
        date_rect = QRect(text_right - date_w, y, date_w, 20)
        painter.drawText(date_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, date_text)

        f2 = QFont(); f2.setPointSize(10); f2.setBold(not mail.is_read)
        painter.setFont(f2)
        painter.setPen(QColor("#2A2D33") if not mail.is_read else QColor("#6B7078"))
        subj_rect = QRect(text_left, y2, text_width, 19)
        subj = mail.subject or "(件名なし)"
        painter.drawText(subj_rect, Qt.AlignmentFlag.AlignVCenter,
                          QFontMetrics(f2).elidedText(subj, Qt.TextElideMode.ElideRight, text_width))

        f3 = QFont(); f3.setPointSize(8)
        painter.setFont(f3)
        painter.setPen(QColor("#6B7078"))
        to_text = f"宛先: {mail.to_addr}" if mail.to_addr else "宛先: (不明)"
        to_rect = QRect(text_left, y3, text_width, 16)
        painter.drawText(to_rect, Qt.AlignmentFlag.AlignVCenter,
                          QFontMetrics(f3).elidedText(to_text, Qt.TextElideMode.ElideRight, to_rect.width()))

        if mail.attachments:
            chips, chip_font = self._attachment_chips(rect, mail, y4)
            painter.setFont(chip_font)
            fm_chip = QFontMetrics(chip_font)
            for i, chip in enumerate(chips):
                clickable = chip["clickable"]
                is_hovered = clickable and self.hover_chip == (index.row(), i)

                draw_rect = chip["rect"]
                if is_hovered:
                    # マウスが乗っているチップだけ影を敷いて2px浮き上がらせ、押せることを示す
                    shadow_rect = chip["rect"].adjusted(0, 1, 0, 3)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor(0, 0, 0, 45))
                    painter.drawRoundedRect(shadow_rect, 6, 6)
                    draw_rect = chip["rect"].adjusted(0, -2, 0, -2)

                bg = QColor("#CFE0FA") if is_hovered else (QColor("#E7EEFB") if clickable else QColor("#F0F1F3"))
                fg = QColor("#12386B") if is_hovered else (QColor("#2F6FE4") if clickable else QColor("#9AA0A8"))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(bg))
                painter.drawRoundedRect(draw_rect, 6, 6)
                if is_hovered:
                    painter.setPen(QPen(QColor("#2F6FE4"), 1.2))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRoundedRect(draw_rect, 6, 6)
                painter.setPen(fg)
                inner = draw_rect.adjusted(8, 0, -8, 0)
                painter.drawText(inner, Qt.AlignmentFlag.AlignVCenter,
                                  fm_chip.elidedText(chip["label"], Qt.TextElideMode.ElideRight, inner.width()))
        else:
            f4 = QFont(); f4.setPointSize(8)
            painter.setFont(f4)
            painter.setPen(QColor("#9AA0A8"))
            preview_rect = QRect(text_left, y4, text_width, self.CHIP_HEIGHT)
            preview = mail.preview or "(本文プレビューなし)"
            painter.drawText(preview_rect, Qt.AlignmentFlag.AlignVCenter,
                              QFontMetrics(f4).elidedText(preview, Qt.TextElideMode.ElideRight, text_width))

        painter.restore()

    def _paint_header(self, painter, option, header: "MailGroupHeader"):
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        rect = option.rect
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        painter.fillRect(rect, QColor("#E7EBF1") if hovered else QColor("#EFF2F6"))
        painter.setPen(QColor("#D8DBE0"))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())

        arrow = "▼" if not header.collapsed else "▶"
        f_arrow = QFont(); f_arrow.setPointSize(9)
        painter.setFont(f_arrow)
        painter.setPen(QColor("#6B7078"))
        arrow_rect = QRect(rect.left() + 16, rect.top(), 18, rect.height())
        painter.drawText(arrow_rect, Qt.AlignmentFlag.AlignVCenter, arrow)

        f_title = QFont(); f_title.setBold(True); f_title.setPointSize(9)
        painter.setFont(f_title)
        painter.setPen(QColor("#2A2D33"))
        title_text = f"{header.title or '(件名なし)'}  ({len(header.mails)}件)"
        title_rect = QRect(rect.left() + 38, rect.top(), rect.width() - 54, rect.height())
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignVCenter,
                          QFontMetrics(f_title).elidedText(title_text, Qt.TextElideMode.ElideRight,
                                                            title_rect.width()))
        painter.restore()

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            mail: db.MailRow | None = index.data(MAIL_ROLE)
            if mail is not None and mail.attachments:
                _, _, _, y4 = self._row_positions(option.rect)
                chips, _ = self._attachment_chips(option.rect, mail, y4)
                pos = event.position().toPoint()
                for chip in chips:
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
            has_page = att.start_page is not None
            label = f"\U0001F4CE {att.name}" if has_page else f"\U0001F4CE {att.name}（ページ位置不明）"
            action = menu.addAction(label)
            action.setEnabled(has_page)
            if has_page:
                action.triggered.connect(
                    lambda checked=False, p=att.start_page: self.attachment_clicked.emit(index, p))

        widget = option.widget
        anchor = chip["rect"].bottomLeft()
        global_pos = widget.mapToGlobal(anchor) if widget is not None else QCursor.pos()
        menu.exec(global_pos)


# -------------------------------------------------------------------- 一覧
class MailListView(QListView):
    """添付チップのホバー検出(カーソル変更・浮き上がり表示)を行うQListView。"""

    def __init__(self, delegate: MailItemDelegate, parent=None):
        super().__init__(parent)
        self._delegate = delegate
        self.setMouseTracking(True)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        self._update_hover(event.pos())

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._set_hover(None)
        self.viewport().unsetCursor()

    def _update_hover(self, pos):
        index = self.indexAt(pos)
        new_hover = None
        if index.isValid():
            mail = index.data(MAIL_ROLE)
            if mail is not None and mail.attachments:
                rect = self.visualRect(index)
                _, _, _, y4 = self._delegate._row_positions(rect)
                chips, _ = self._delegate._attachment_chips(rect, mail, y4)
                for i, chip in enumerate(chips):
                    if chip["clickable"] and chip["rect"].contains(pos):
                        new_hover = (index.row(), i)
                        break
        self._set_hover(new_hover)
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if new_hover else Qt.CursorShape.ArrowCursor)

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

    def set_tree(self, rows: list["db.SectionRow"]):
        self.clear()
        self.setColumnCount(1)
        self._flat = False
        items_by_id: dict[int, QStandardItem] = {}
        root = self.invisibleRootItem()
        for row in rows:  # start_page昇順 = 常に親が子より先に来る
            item = QStandardItem(row.title)
            item.setEditable(False)
            item.setData(row, SECTION_ROLE)
            items_by_id[row.id] = item
            parent_item = items_by_id.get(row.parent_id) if row.parent_id is not None else None
            (parent_item or root).appendRow(item)

    def set_flat(self, rows: list["db.SectionRow"]):
        self.clear()
        self.setColumnCount(1)
        self._flat = True
        root = self.invisibleRootItem()
        for row in rows:
            item = QStandardItem(row.title)
            item.setEditable(False)
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


class SectionItemDelegate(QStyledItemDelegate):
    """しおり1件を1行だけで表示する(見出し+ページ範囲)。多数のしおりを一度に見渡せるようにする。"""

    ROW_HEIGHT = 30

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), self.ROW_HEIGHT)

    def paint(self, painter, option, index):
        section: db.SectionRow | None = index.data(SECTION_ROLE)
        if section is None:
            return super().paint(painter, option, index)

        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        if selected:
            painter.fillRect(rect, QColor("#E4EDFC"))
            painter.fillRect(QRect(rect.left(), rect.top(), 3, rect.height()), QColor("#2F6FE4"))
        elif hovered:
            painter.fillRect(rect, QColor("#F5F7FA"))
        else:
            painter.fillRect(rect, QColor("#FFFFFF"))

        painter.setPen(QColor("#F0F1F3"))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())

        left = rect.left() + 6
        right = rect.right() - 12

        page_text = (f"{section.start_page}" if section.start_page == section.end_page
                     else f"{section.start_page}-{section.end_page}")
        f_page = QFont(); f_page.setPointSize(8)
        fm_page = QFontMetrics(f_page)
        page_w = fm_page.horizontalAdvance(page_text)

        f_title = QFont(); f_title.setPointSize(9)
        fm_title = QFontMetrics(f_title)

        x = left
        title_area_right = right - page_w - 8
        model = index.model()
        show_breadcrumb = section.path_titles and isinstance(model, SectionTreeModel) and model.is_flat()
        if show_breadcrumb:
            f_bc = QFont(); f_bc.setPointSize(8)
            fm_bc = QFontMetrics(f_bc)
            max_bc_w = max(0, int((title_area_right - left) * 0.45))
            bc_text = fm_bc.elidedText(section.path_titles + "  ›  ", Qt.TextElideMode.ElideLeft, max_bc_w)
            painter.setFont(f_bc)
            painter.setPen(QColor("#9AA0A8"))
            bc_w = fm_bc.horizontalAdvance(bc_text)
            bc_rect = QRect(x, rect.top(), bc_w, rect.height())
            painter.drawText(bc_rect, Qt.AlignmentFlag.AlignVCenter, bc_text)
            x += bc_w

        painter.setFont(f_title)
        painter.setPen(QColor("#16181D"))
        title_rect = QRect(x, rect.top(), max(10, title_area_right - x), rect.height())
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignVCenter,
                          fm_title.elidedText(section.title, Qt.TextElideMode.ElideRight, title_rect.width()))

        painter.setFont(f_page)
        painter.setPen(QColor("#9AA0A8"))
        page_rect = QRect(right - page_w, rect.top(), page_w, rect.height())
        painter.drawText(page_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, page_text)

        painter.restore()


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

        self.pdf_view = QPdfView()
        self.pdf_view.setDocument(self.document)
        self.pdf_view.setPageMode(QPdfView.PageMode.MultiPage)
        self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        self.pdf_view.setSearchModel(self.search_model)

        self.pdf_view.pageNavigator().currentPageChanged.connect(self._on_page_changed)

        self.pdf_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.pdf_view.customContextMenuRequested.connect(self._show_pdf_context_menu)

    @property
    def title(self) -> str:
        return os.path.basename(self.pdf_path)

    def set_zoom_mode(self, mode):
        self.pdf_view.setZoomMode(mode)

    def set_sidebar_visible(self, visible: bool):
        self.sidebar.setVisible(visible)

    def _show_pdf_context_menu(self, pos):
        """PDF表示部分の右クリックメニュー。全画面表示中はしおり呼び出し・全画面解除の導線が
        ツールバーから消えてしまうため、ここから操作できるようにする。"""
        window = self.window()
        if not isinstance(window, QMainWindow) or not hasattr(window, "fullscreen_action"):
            return
        menu = QMenu(self)
        if window.isFullScreen():
            sidebar_visible = self.sidebar.isVisible()
            toggle_action = menu.addAction("しおり・メール一覧を隠す" if sidebar_visible else "しおり・メール一覧を表示")
            toggle_action.triggered.connect(window._toggle_fullscreen_sidebar)
            menu.addSeparator()
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

                pt_size = self.document.pagePointSize(page)
                if pt_size.width() <= 0 or pt_size.height() <= 0:
                    continue
                scale = min(page_rect.width() / pt_size.width(), page_rect.height() / pt_size.height())
                img_w = max(1, round(pt_size.width() * scale))
                img_h = max(1, round(pt_size.height() * scale))
                image = self.document.render(page, QSize(img_w, img_h))

                x = round((page_rect.width() - img_w) / 2)
                y = round((page_rect.height() - img_h) / 2)
                painter.drawImage(x, y, image)
        finally:
            painter.end()

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


# -------------------------------------------------------------- 1PDF分のタブ(メール)
class PdfTab(BasePdfTab):
    """1つのメール束PDFに対応するメール一覧+PDF表示ペイン。タブとして複数同時に開ける。"""

    MODE = "mail"

    def __init__(self, pdf_path: str, parent=None):
        super().__init__(pdf_path, parent)
        self.sort_key = "date"
        self.sort_descending = True
        self.group_by_subject = False

        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(splitter)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        sort_bar = QWidget()
        sort_bar.setObjectName("sortBar")
        sort_layout = QHBoxLayout(sort_bar)
        sort_layout.setContentsMargins(12, 8, 12, 8)
        sort_layout.addWidget(QLabel("並び替え:"))
        self.sort_combo = QComboBox()
        self.sort_combo.addItem("受信日時", "date")
        self.sort_combo.addItem("件名", "subject")
        self.sort_combo.addItem("差出人", "sender")
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        sort_layout.addWidget(self.sort_combo)

        self.sort_dir_button = QToolButton()
        self.sort_dir_button.setCheckable(True)
        self.sort_dir_button.setChecked(True)
        self.sort_dir_button.toggled.connect(self._on_sort_dir_toggled)
        sort_layout.addWidget(self.sort_dir_button)

        sort_layout.addSpacing(12)
        self.group_button = QToolButton()
        self.group_button.setCheckable(True)
        self.group_button.setText("件名でグループ化")
        self.group_button.setToolTip("同じ件名(返信・転送を除く)のメールをまとめて表示します")
        self.group_button.toggled.connect(self._on_group_toggled)
        sort_layout.addWidget(self.group_button)

        sort_layout.addStretch(1)
        self._update_sort_dir_label()
        left_layout.addWidget(sort_bar)

        self.model = MailListModel()
        self.item_delegate = MailItemDelegate()
        self.item_delegate.attachment_clicked.connect(self._on_attachment_clicked)
        self.list_view = MailListView(self.item_delegate)
        self.list_view.setModel(self.model)
        self.list_view.setItemDelegate(self.item_delegate)
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list_view.setUniformItemSizes(True)
        self.list_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list_view.setFrameShape(QFrame.Shape.NoFrame)
        self.list_view.setAlternatingRowColors(False)
        self.list_view.clicked.connect(self._on_index_activated)
        self.list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_view.customContextMenuRequested.connect(self._show_context_menu)
        left_layout.addWidget(self.list_view)
        self.sidebar = left
        splitter.addWidget(left)

        splitter.addWidget(self.pdf_view)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)

        self.list_view.selectionModel().currentChanged.connect(self._on_index_activated)

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
        rows = (
            db.search(self.db_path, self.current_query, self.sort_key, self.sort_descending)
            if self.db_path else []
        )
        self.model.set_rows(rows, grouped=self.group_by_subject)
        first = self.model.first_mail_index()
        if first.isValid():
            self.list_view.setCurrentIndex(first)
        else:
            self.pdf_view.pageNavigator().jump(0, QPointF(0, 0))

    def result_count(self) -> int:
        return self.model.rowCount()

    def unread_count(self) -> int:
        return self.model.unread_count()

    def _update_sort_dir_label(self):
        desc_label, asc_label = SORT_DIR_LABELS.get(self.sort_key, SORT_DIR_LABELS["date"])
        self.sort_dir_button.setText(desc_label if self.sort_descending else asc_label)

    def _on_sort_changed(self):
        self.sort_key = self.sort_combo.currentData()
        self._update_sort_dir_label()
        self.refresh_list()

    def _on_sort_dir_toggled(self, checked: bool):
        self.sort_descending = checked
        self._update_sort_dir_label()
        self.refresh_list()

    def _on_group_toggled(self, checked: bool):
        self.group_by_subject = checked
        self.list_view.setUniformItemSizes(not checked)
        self.refresh_list()

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
        self._mark_read(mail)
        self.pdf_view.pageNavigator().jump(mail.start_page - 1, QPointF(0, 0))

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
        if mail.is_read == read:
            return
        db.set_read(self.db_path, mail.id, read)
        self.model.set_read(mail.id, read)

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
        read_action = menu.addAction("未読にする" if mail.is_read else "既読にする")
        read_action.triggered.connect(lambda: self._mark_read(mail, not mail.is_read))
        menu.addSeparator()

        reply_action = menu.addAction("\U0001F4E7 Outlookで返信を作成...")
        reply_action.triggered.connect(lambda: self._create_outlook_reply(mail, reply_all=False))
        reply_all_action = menu.addAction("\U0001F4E7 Outlookで全員に返信を作成...")
        reply_all_action.triggered.connect(lambda: self._create_outlook_reply(mail, reply_all=True))
        menu.addSeparator()

        page_range = f"{mail.start_page}" if mail.start_page == mail.end_page else f"{mail.start_page}-{mail.end_page}"
        mail_name = f"{mail.subject or '(件名なし)'}"
        mail_action = menu.addAction(f"\U0001F5A8 このメールを印刷... ({page_range}ページ)")
        mail_action.triggered.connect(lambda: self.print_page_range(mail.start_page, mail.end_page))
        save_action = menu.addAction(f"\U0001F4BE このメールをPDFで保存... ({page_range}ページ)")
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
            row.is_read = True
        count = self.model.rowCount()
        if count:
            self.model.dataChanged.emit(self.model.index(0, 0), self.model.index(count - 1, 0), [MAIL_ROLE])

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
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(splitter)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        info_bar = QWidget()
        info_bar.setObjectName("sortBar")
        info_layout = QHBoxLayout(info_bar)
        info_layout.setContentsMargins(12, 8, 12, 8)
        info_layout.addWidget(QLabel("しおり一覧"))
        info_layout.addStretch(1)
        expand_button = QToolButton()
        expand_button.setText("すべて展開")
        expand_button.clicked.connect(lambda: self.list_view.expandAll())
        info_layout.addWidget(expand_button)
        collapse_button = QToolButton()
        collapse_button.setText("折りたたむ")
        collapse_button.clicked.connect(lambda: self.list_view.collapseAll())
        info_layout.addWidget(collapse_button)
        left_layout.addWidget(info_bar)

        self.model = SectionTreeModel()
        self.item_delegate = SectionItemDelegate()
        self.list_view = QTreeView()
        self.list_view.setModel(self.model)
        self.list_view.setItemDelegate(self.item_delegate)
        self.list_view.setHeaderHidden(True)
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list_view.setUniformRowHeights(True)
        self.list_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list_view.setFrameShape(QFrame.Shape.NoFrame)
        self.list_view.setAnimated(True)
        self.list_view.clicked.connect(self._on_index_activated)
        self.list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_view.customContextMenuRequested.connect(self._show_context_menu)
        left_layout.addWidget(self.list_view)
        self.sidebar = left
        splitter.addWidget(left)

        splitter.addWidget(self.pdf_view)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)

        self.list_view.selectionModel().currentChanged.connect(self._on_index_activated)

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
            self.model.set_tree([])
            return

        if query:
            rows = db.search_sections(self.db_path, query)
            self.model.set_flat(rows)
        else:
            self._all_rows = db.list_sections(self.db_path)
            self.model.set_tree(self._all_rows)
            self.list_view.expandAll()

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
        self.pdf_view.pageNavigator().jump(section.start_page - 1, QPointF(0, 0))

    def _show_context_menu(self, pos):
        index = self.list_view.indexAt(pos)
        if not index.isValid():
            return
        self.list_view.setCurrentIndex(index)
        section = self.model.section_at(index)
        if section is None:
            return

        menu = QMenu(self)
        page_range = (f"{section.start_page}" if section.start_page == section.end_page
                      else f"{section.start_page}-{section.end_page}")
        print_action = menu.addAction(f"\U0001F5A8 この区間を印刷... ({page_range}ページ)")
        print_action.triggered.connect(lambda: self.print_page_range(section.start_page, section.end_page))
        save_action = menu.addAction(f"\U0001F4BE この区間をPDFで保存... ({page_range}ページ)")
        save_action.triggered.connect(
            lambda: self.save_page_range(section.start_page, section.end_page, section.title))
        menu.exec(self.list_view.viewport().mapToGlobal(pos))

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
        if index is None or self.list_view.currentIndex() == index:
            return

        self._syncing_selection = True
        try:
            self.list_view.expand(index.parent())
            self.list_view.setCurrentIndex(index)
            self.list_view.scrollTo(index)
        finally:
            self._syncing_selection = False


# ------------------------------------------------------------------ 本体
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("メールPDF閲覧アプリ")
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        if avail is not None:
            self.resize(min(1440, int(avail.width() * 0.92)), min(900, int(avail.height() * 0.9)))
        else:
            self.resize(1440, 900)

        self.settings = QSettings("ukawa", "MailPDFViewer")
        self.setAcceptDrops(True)
        self._page_synced_tab: BasePdfTab | None = None
        self._sidebar_visible_in_fullscreen = False

        self._build_ui()
        self._update_controls_enabled()
        self._update_page_bar()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        self._build_toolbar()

        self.status = QStatusBar()
        self.setStatusBar(self.status)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._on_current_tab_changed)
        self.setCentralWidget(self.tabs)

    def _build_toolbar(self):
        toolbar = self.toolbar = QToolBar()
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(20, 20))
        self.addToolBar(toolbar)

        style = self.style()

        open_action = QAction(style.standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton),
                               "PDFを開く", self)
        open_action.triggered.connect(self.open_pdf_dialog)
        toolbar.addAction(open_action)

        self.recent_button = QToolButton()
        self.recent_button.setText("最近使ったPDF")
        self.recent_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.recent_menu = QMenu(self.recent_button)
        self.recent_menu.aboutToShow.connect(self._populate_recent_menu)
        self.recent_button.setMenu(self.recent_menu)
        toolbar.addWidget(self.recent_button)

        self.reindex_action = QAction(style.standardIcon(QStyle.StandardPixmap.SP_BrowserReload),
                                       "再インデックス", self)
        self.reindex_action.triggered.connect(self.reindex_current)
        toolbar.addAction(self.reindex_action)

        toolbar.addSeparator()

        search_icon = style.standardIcon(QStyle.StandardPixmap.SP_FileDialogContentsView)
        toolbar.addWidget(QLabel(" 検索: "))
        self.search_box = QLineEdit()
        self.search_box.addAction(search_icon, QLineEdit.ActionPosition.LeadingPosition)
        self.search_box.setPlaceholderText(MAIL_SEARCH_PLACEHOLDER)
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setMinimumWidth(220)
        self.search_box.setMaximumWidth(420)
        self.search_box.textChanged.connect(self.on_search_changed)
        toolbar.addWidget(self.search_box)

        toolbar.addSeparator()
        toolbar.addWidget(QLabel(" PDF表示: "))
        self.zoom_combo = QComboBox()
        self.zoom_combo.addItem("幅に合わせる", QPdfView.ZoomMode.FitToWidth)
        self.zoom_combo.addItem("ページ全体", QPdfView.ZoomMode.FitInView)
        self.zoom_combo.currentIndexChanged.connect(self.on_zoom_mode_changed)
        toolbar.addWidget(self.zoom_combo)

        self.prev_page_button = QToolButton()
        self.prev_page_button.setText("◀")
        self.prev_page_button.setToolTip("前のページ")
        self.prev_page_button.clicked.connect(self.go_prev_page)
        toolbar.addWidget(self.prev_page_button)

        self.page_label = QLabel("- / -")
        self.page_label.setObjectName("pageLabel")
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_label.setMinimumWidth(70)
        toolbar.addWidget(self.page_label)

        self.next_page_button = QToolButton()
        self.next_page_button.setText("▶")
        self.next_page_button.setToolTip("次のページ")
        self.next_page_button.clicked.connect(self.go_next_page)
        toolbar.addWidget(self.next_page_button)

        toolbar.addSeparator()
        self.fullscreen_action = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_TitleBarMaxButton), "全画面表示", self)
        self.fullscreen_action.setCheckable(True)
        self.fullscreen_action.setToolTip(
            "全画面表示を切り替え (F11)\n全画面中はCtrl+Bでメール一覧・しおりの表示/非表示を切り替え")
        self.fullscreen_action.toggled.connect(self._on_fullscreen_toggled)
        toolbar.addAction(self.fullscreen_action)

        find_shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
        find_shortcut.activated.connect(lambda: (self.search_box.setFocus(), self.search_box.selectAll()))

        escape_shortcut = QShortcut(QKeySequence("Esc"), self.search_box)
        escape_shortcut.activated.connect(self.search_box.clear)

        sidebar_shortcut = QShortcut(QKeySequence("Ctrl+B"), self)
        sidebar_shortcut.activated.connect(self._toggle_fullscreen_sidebar)

        # 全画面表示中はツールバー(fullscreen_actionの置き場所)ごと非表示になり、
        # QActionにぶら下げたショートカットだけでは復帰できなくなるため、ウィンドウ直付けのQShortcutにする。
        fullscreen_shortcut = QShortcut(QKeySequence("F11"), self)
        fullscreen_shortcut.activated.connect(self.fullscreen_action.toggle)

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

    def _add_recent_file(self, path: str):
        path = os.path.abspath(path)
        files = [p for p in self._recent_files() if os.path.abspath(p) != path]
        files.insert(0, path)
        self.settings.setValue("recentFiles", files[:MAX_RECENT_FILES])

    def _populate_recent_menu(self):
        self.recent_menu.clear()
        files = self._recent_files()
        if not files:
            action = self.recent_menu.addAction("(履歴はありません)")
            action.setEnabled(False)
            return
        for path in files:
            action = self.recent_menu.addAction(os.path.basename(path))
            action.setToolTip(path)
            action.triggered.connect(lambda checked=False, p=path: self.load_pdf(p))

    # --------------------------------------------------------------- タブ管理
    def _find_tab_index(self, path: str) -> int:
        path = os.path.abspath(path)
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if os.path.abspath(tab.pdf_path) == path:
                return i
        return -1

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
            self.tabs.addTab(tab, tab.title)

        ok = tab.load(force_rebuild=force_rebuild, status_cb=self.status.showMessage)
        idx = self.tabs.indexOf(tab)
        self.tabs.setTabToolTip(idx, path)
        self.tabs.setCurrentIndex(idx)
        if ok:
            self._add_recent_file(path)
            unit = "件のメール" if isinstance(tab, PdfTab) else "件のしおり"
            self.status.showMessage(
                f"{tab.result_count()} {unit}を読み込みました{self._unread_suffix(tab)}", 5000)
        self._update_controls_enabled()
        self._update_page_bar()

    def _close_tab(self, index: int):
        tab = self.tabs.widget(index)
        self.tabs.removeTab(index)
        if tab is not None:
            tab.document.close()
            tab.deleteLater()
        self._update_controls_enabled()

    def _current_tab(self) -> BasePdfTab | None:
        return self.tabs.currentWidget()

    def _unread_suffix(self, tab: BasePdfTab) -> str:
        count = tab.unread_count()
        return f" (未読 {count})" if count else ""

    def _on_current_tab_changed(self, _index: int):
        if self._page_synced_tab is not None:
            try:
                self._page_synced_tab.page_changed.disconnect(self._update_page_bar)
            except (RuntimeError, TypeError):
                pass
            self._page_synced_tab = None

        tab = self._current_tab()
        if tab is None:
            self._update_controls_enabled()
            self._update_page_bar()
            return

        self._apply_fullscreen_chrome()

        self.search_box.blockSignals(True)
        self.search_box.setText(tab.current_query)
        self.search_box.setPlaceholderText(
            MAIL_SEARCH_PLACEHOLDER if isinstance(tab, PdfTab) else DOCUMENT_SEARCH_PLACEHOLDER)
        self.search_box.blockSignals(False)

        self.zoom_combo.blockSignals(True)
        zoom_idx = self.zoom_combo.findData(tab.pdf_view.zoomMode())
        if zoom_idx >= 0:
            self.zoom_combo.setCurrentIndex(zoom_idx)
        self.zoom_combo.blockSignals(False)

        tab.page_changed.connect(self._update_page_bar)
        self._page_synced_tab = tab

        self.status.showMessage(f"{tab.result_count()} 件表示中{self._unread_suffix(tab)}")
        self._update_controls_enabled()
        self._update_page_bar()

    def _update_controls_enabled(self):
        has_tab = self.tabs.count() > 0
        self.search_box.setEnabled(has_tab)
        self.zoom_combo.setEnabled(has_tab)
        self.reindex_action.setEnabled(has_tab)

    def _update_page_bar(self):
        tab = self._current_tab()
        if tab is None:
            self.page_label.setText("- / -")
            self.prev_page_button.setEnabled(False)
            self.next_page_button.setEnabled(False)
            return
        total = tab.page_count()
        current = tab.current_page() if total > 0 else -1
        self.page_label.setText(f"{current + 1} / {total}" if total > 0 else "- / -")
        self.prev_page_button.setEnabled(total > 0 and current > 0)
        self.next_page_button.setEnabled(total > 0 and current < total - 1)

    # --------------------------------------------------------------- 動作
    def reindex_current(self):
        tab = self._current_tab()
        if tab is not None:
            self.load_pdf(tab.pdf_path, force_rebuild=True)

    def on_search_changed(self, text: str):
        tab = self._current_tab()
        if tab is not None:
            tab.set_search(text)
            self.status.showMessage(f"{tab.result_count()} 件表示中{self._unread_suffix(tab)}")

    def on_zoom_mode_changed(self):
        tab = self._current_tab()
        if tab is not None:
            tab.set_zoom_mode(self.zoom_combo.currentData())

    def go_prev_page(self):
        tab = self._current_tab()
        if tab is not None:
            tab.go_prev_page()

    def go_next_page(self):
        tab = self._current_tab()
        if tab is not None:
            tab.go_next_page()

    # --------------------------------------------------------------- 全画面表示
    def _on_fullscreen_toggled(self, checked: bool):
        if checked:
            self._sidebar_visible_in_fullscreen = False
            self.showFullScreen()
        else:
            self.showNormal()
        self._apply_fullscreen_chrome()

    def _toggle_fullscreen_sidebar(self):
        """全画面表示中にCtrl+Bでメール一覧・しおりの表示/非表示を切り替える。"""
        if not self.isFullScreen():
            return
        self._sidebar_visible_in_fullscreen = not self._sidebar_visible_in_fullscreen
        self._apply_fullscreen_chrome()

    def _apply_fullscreen_chrome(self):
        """全画面時はツールバー・ステータスバー・サイドバーを畳んで、PDF表示をスライドショーのように最大化する。"""
        full = self.isFullScreen()
        self.toolbar.setVisible(not full)
        self.status.setVisible(not full)
        tab = self._current_tab()
        if tab is not None:
            tab.set_sidebar_visible(True if not full else self._sidebar_visible_in_fullscreen)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            is_full = bool(self.windowState() & Qt.WindowState.WindowFullScreen)
            if self.fullscreen_action.isChecked() != is_full:
                self.fullscreen_action.blockSignals(True)
                self.fullscreen_action.setChecked(is_full)
                self.fullscreen_action.blockSignals(False)
            self._apply_fullscreen_chrome()


APP_STYLESHEET = """
QMainWindow { background: #FFFFFF; }
QToolBar { background: #F7F8FA; border: none; padding: 6px; spacing: 4px; }
QToolBar QLabel { color: #4A4F58; padding-left: 4px; }
QLineEdit { border: 1px solid #D6D9DE; border-radius: 6px; padding: 5px 8px; background: #FFFFFF; }
QLineEdit:focus { border: 1px solid #2F6FE4; }
QComboBox { border: 1px solid #D6D9DE; border-radius: 6px; padding: 4px 8px; background: #FFFFFF; }
QToolButton { border: 1px solid #D6D9DE; border-radius: 6px; padding: 5px 10px; background: #FFFFFF; }
QToolButton:checked { background: #EAF1FE; border-color: #2F6FE4; color: #2F6FE4; }
QStatusBar { background: #F7F8FA; color: #6B7078; }
QListView, QTreeView { background: #FFFFFF; border: none; outline: 0; }
QSplitter::handle { background: #E9EBEF; }
#sortBar { background: #FBFCFD; border-bottom: 1px solid #E9EBEF; }
#pageLabel { color: #2A2D33; font-weight: 600; }
QTabWidget::pane { border: none; }
QTabBar::tab { padding: 7px 16px; margin-right: 2px; background: #EEF0F3; border-top-left-radius: 6px; border-top-right-radius: 6px; }
QTabBar::tab:selected { background: #FFFFFF; border: 1px solid #E9EBEF; border-bottom: none; }
"""


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(APP_STYLESHEET)
    win = MainWindow()
    win.show()
    if len(sys.argv) > 1:
        win.load_pdf(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
