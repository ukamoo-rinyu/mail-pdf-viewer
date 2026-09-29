"""図面モードの画面（kojiPDFviewer に組み込む）。

- DrawingSetTab : 図面セットPDFの発注前チェック結果の一覧＋PDF表示（指摘箇所を赤枠で強調）
- LedgerTab     : 工事台帳（図面ごとの変更記録・変更契約・竣工図チェックリスト）
- RecordDialog / NewLedgerDialog / RegisterVersionDialog / CompareWindow : 上記から開く画面

処理の中身（読み取り・判定・台帳の読み書き・出力）は drawings/ パッケージにあり、
この画面はそれを呼び出して表示するだけにしている。
"""
from __future__ import annotations

import os
import shutil

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import main as app
from drawings import report
from drawings.checks import LEVEL_CHECK, CheckResult, Finding, run_checks
from drawings.extract import IndexEntry
from drawings.layouts import list_layouts, load_layout
from drawings.ledger import (INSTRUCTION_METHODS, KOJI_FIELDS, STATUS_CANCELLED, STATUS_COMPLETED,
                             STATUS_DISCUSSING, STATUS_INSTRUCTED, STATUSES, TRIGGER_KINDS, VERSION_KIND_ORDER,
                             Ledger, LedgerConflictError, LedgerError, build_from_order_set, today_text)

ITEM_ROLE = Qt.ItemDataRole.UserRole + 1
C_WARN_BG = QColor("#FFF4E5")
C_WARN_TEXT = QColor("#B45309")
C_OK_TEXT = QColor("#1B7F3B")
C_INFO_TEXT = QColor(app.C_SUB)
C_MARK = QColor(220, 38, 38)          # 指摘箇所の赤枠
C_MARK_OLD = QColor(234, 88, 12)      # 別の版で指定した箇所（参考表示）

# 一覧（QTreeWidget）の選択色。アプリ全体のスタイルでは選択の背景を透明にしているため、ここで付け直す
TREE_STYLE = (f"QTreeView::item {{ padding: 3px 2px; }}"
              f"QTreeView::item:hover {{ background: {app.C_PANEL2}; }}"
              f"QTreeView::item:selected {{ background: {app.C_ACCENT_BG}; color: {app.C_TEXT}; }}"
              f"QHeaderView::section {{ background: {app.C_PANEL2}; color: {app.C_SUB}; border: none;"
              f" border-bottom: 1px solid {app.C_LINE}; padding: 4px 6px; }}")

_open_windows: list[QWidget] = []  # 閉じるまで参照を持っておく別ウインドウ


def _status(widget: QWidget, text: str, timeout: int = 5000):
    """メインウインドウのステータスバーに表示する（あれば）。"""
    window = widget.window()
    if hasattr(window, "statusBar") and window.statusBar() is not None:
        window.statusBar().showMessage(text, timeout)


def _make_tree(headers: list[str]) -> QTreeWidget:
    tree = QTreeWidget()
    tree.setObjectName("drawingTree")
    tree.setHeaderLabels(headers)
    tree.setStyleSheet(TREE_STYLE)
    tree.setFrameShape(QFrame.Shape.NoFrame)
    tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    tree.setUniformRowHeights(False)
    tree.setAlternatingRowColors(False)
    return tree


def _fit_to_screen(widget: QWidget, width: int, height: int):
    """画面（タスクバーを除く）に収まる大きさにして、画面の中央に置く。
    Windowsの表示倍率を大きくしていると画面が小さくなり、下のボタンが画面外に出てしまうため。"""
    screen = widget.screen() or QApplication.primaryScreen()
    if screen is None:
        widget.resize(width, height)
        return
    avail = screen.availableGeometry()
    w = min(width, int(avail.width() * 0.95))
    h = min(height, int(avail.height() * 0.92))
    widget.resize(w, h)
    widget.move(avail.x() + (avail.width() - w) // 2, avail.y() + max(0, (avail.height() - h) // 2 - 10))


def _small_button(text: str, tip: str = "") -> QToolButton:
    b = QToolButton()
    b.setObjectName("smallButton")
    b.setText(text)
    if tip:
        b.setToolTip(tip)
    return b


def choose_layout(parent: QWidget, current_key: str | None = None) -> str | None:
    """様式（layouts/*.json）を利用者に選んでもらう。取り消したら None。"""
    layouts = list_layouts()
    if not layouts:
        QMessageBox.warning(parent, "様式がありません", "layouts フォルダに様式設定ファイル（JSON）がありません。")
        return None
    names = [f"{la.name}（{la.key}）" for la in layouts]
    keys = [la.key for la in layouts]
    idx = keys.index(current_key) if current_key in keys else 0
    name, ok = QInputDialog.getItem(parent, "図面の様式", "図面の様式（表題欄・目次の位置）を選んでください:",
                                    names, idx, False)
    return keys[names.index(name)] if ok else None


# ================================================================ PDF表示（赤枠・範囲指定つき）
class HighlightPdfView(app.LinkedPdfView):
    """指定した範囲を赤枠で強調表示でき、ドラッグで範囲を指定できるPDF表示。"""

    area_selected = Signal(int, QRectF)  # (0始まりのページ, ページ上の範囲[ポイント])
    revealed = Signal()                  # reveal() の位置合わせが終わった

    def __init__(self, pdf_path: str = "", parent=None):
        super().__init__(pdf_path, parent)
        self._marks: list[tuple[int, QRectF, str]] = []  # (ページ, 範囲, "strong"/"normal"/"old")
        self._select_mode = False
        self._drag: tuple[int, QPointF, QPointF] | None = None  # (ページ, 開始点, 現在点) ビューポート座標

    # ------------------------------------------------------------ 強調表示
    def set_marks(self, marks: list[tuple[int, QRectF, str]]):
        """強調する範囲を設定する。"""
        self._marks = list(marks)
        self.viewport().update()

    def reveal(self, page: int, rect: QRectF | None = None, zoom: bool = False):
        """ページへ移動し、rect があればそこが画面の中央に来るようにする（zoom なら拡大する）。"""
        doc = self.document()
        if doc is None or not 0 <= page < doc.pageCount():
            return
        if zoom and rect is not None:
            screen = self.screen() or QGuiApplication.primaryScreen()
            resolution = screen.logicalDotsPerInch() / 72.0
            width = max(rect.width(), 80.0)
            factor = self.viewport().width() * 0.45 / (width * resolution)
            if self.zoomMode() != QPdfView.ZoomMode.Custom:
                self.setZoomMode(QPdfView.ZoomMode.Custom)
            self.setZoomFactor(min(4.0, max(0.25, factor)))
        self.pageNavigator().jump(page, QPointF(0, 0))
        if rect is not None:
            # ページ配置の確定・表示位置の復元（LinkedPdfView）が終わってから中央に合わせる
            QTimer.singleShot(0, lambda: QTimer.singleShot(0, lambda: self._center_on(page, rect)))
        else:
            self.revealed.emit()

    def _center_on(self, page: int, rect: QRectF):
        try:
            self._center_on_page(page, rect)
        finally:
            self.revealed.emit()

    def _center_on_page(self, page: int, rect: QRectF):
        rects = self._page_rects()
        if not 0 <= page < len(rects) or rects[page].isNull():
            return
        pr = rects[page]
        pts = self.document().pagePointSize(page)
        if pts.width() <= 0 or pts.height() <= 0:
            return
        cx = pr.x() + rect.center().x() * pr.width() / pts.width()
        cy = pr.y() + rect.center().y() * pr.height() / pts.height()
        self.horizontalScrollBar().setValue(int(cx - self.viewport().width() / 2))
        self.verticalScrollBar().setValue(int(cy - self.viewport().height() / 2))

    # ------------------------------------------------------------ 範囲指定
    def set_select_mode(self, on: bool):
        """オンのあいだ、ドラッグでページ上の範囲を指定できる（リンクは反応しない）。"""
        self._select_mode = on
        self._drag = None
        if on:
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.viewport().unsetCursor()
        self.viewport().update()

    def _page_at(self, pos: QPointF) -> int | None:
        offset = self._scroll_offset()
        for page, r in enumerate(self._page_rects()):
            if not r.isNull() and QRectF(r).translated(-offset).contains(pos):
                return page
        return None

    def _to_page_point(self, page: int, pos: QPointF) -> QPointF:
        r = self._page_rects()[page]
        pts = self.document().pagePointSize(page)
        offset = self._scroll_offset()
        x = (pos.x() + offset.x() - r.x()) * pts.width() / max(1, r.width())
        y = (pos.y() + offset.y() - r.y()) * pts.height() / max(1, r.height())
        return QPointF(min(max(0.0, x), pts.width()), min(max(0.0, y), pts.height()))

    def mousePressEvent(self, event):
        if self._select_mode and event.button() == Qt.MouseButton.LeftButton:
            page = self._page_at(event.position())
            if page is not None:
                self._drag = (page, event.position(), event.position())
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            page, start, _ = self._drag
            self._drag = (page, start, event.position())
            self.viewport().update()
            event.accept()
            return
        if self._select_mode:
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag is not None:
            page, start, _ = self._drag
            self._drag = None
            a = self._to_page_point(page, start)
            b = self._to_page_point(page, event.position())
            rect = QRectF(a, b).normalized()
            self.viewport().update()
            if rect.width() >= 3 and rect.height() >= 3:
                self.area_selected.emit(page, rect)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ------------------------------------------------------------ 描画
    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._marks and self._drag is None:
            return
        rects = self._page_rects()
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for page, rect, style in self._marks:
            if not 0 <= page < len(rects) or rects[page].isNull():
                continue
            view = self._to_view(rects[page], page, rect).adjusted(-3, -3, 3, 3)
            color = C_MARK_OLD if style == "old" else C_MARK
            pen = QPen(color, 3 if style == "strong" else 2)
            if style == "old":
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            fill = QColor(color)
            fill.setAlpha(40 if style == "strong" else 18)
            painter.setBrush(QBrush(fill))
            painter.drawRect(view)
        if self._drag is not None:
            _page, start, now = self._drag
            painter.setPen(QPen(QColor(app.C_ACCENT), 2, Qt.PenStyle.DashLine))
            painter.setBrush(QColor(37, 99, 235, 30))
            painter.drawRect(QRectF(start, now).normalized())
        painter.end()


def _qrect(box) -> QRectF | None:
    if not box:
        return None
    x0, y0, x1, y1 = box
    return QRectF(x0, y0, x1 - x0, y1 - y0)


# ================================================================ 図面セットタブ（フェーズ2）
class DrawingSetTab(app.BasePdfTab):
    """図面セットPDFの発注前チェック結果（ページ別の一覧）と、PDF表示を並べたタブ。"""

    MODE = "drawing"
    RESULT_UNIT = "枚の図面"
    SEARCH_PLACEHOLDER = "図面番号・名称・判定で絞り込み  (Ctrl+F)"

    def __init__(self, pdf_path: str, layout_key: str, parent=None):
        super().__init__(pdf_path, parent)
        self.layout_key = layout_key
        self.result: CheckResult | None = None
        self._revealing = False  # 指摘箇所への移動中は、スクロールに合わせた一覧の選択変更をしない
        self._replace_view()
        self._build_ui()

    @property
    def title(self) -> str:
        return "図面: " + os.path.basename(self.pdf_path)

    def _replace_view(self):
        """基底クラスが作ったPDF表示を、赤枠の強調ができる HighlightPdfView に取り替える。"""
        old = self.pdf_view
        view = HighlightPdfView(self.pdf_path)
        view.setDocument(self.document)
        view.link_model.setDocument(self.document)
        view.setPageMode(QPdfView.PageMode.MultiPage)
        view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        view.setSearchModel(self.search_model)
        app._style_pdf_view(view)
        view.pageNavigator().currentPageChanged.connect(self._on_page_changed)
        view.revealed.connect(self._end_reveal)
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        view.customContextMenuRequested.connect(self._show_pdf_context_menu)
        self.pdf_view = view
        old.setDocument(None)
        old.deleteLater()

    # ------------------------------------------------------------ 画面
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        splitter = app.RatioSplitter(Qt.Orientation.Horizontal, 0.36)
        outer.addWidget(splitter)

        left = QWidget()
        left.setObjectName("sidebar")
        lay = QVBoxLayout(left)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        top = QWidget()
        top.setObjectName("sortBar")
        top_l = QVBoxLayout(top)
        top_l.setContentsMargins(10, 8, 10, 8)
        top_l.setSpacing(6)
        row = QHBoxLayout()
        row.addWidget(self._build_search_box(self.SEARCH_PLACEHOLDER), 1)
        self.only_check = QCheckBox("要確認のみ")
        self.only_check.setToolTip("要確認の指摘があるページだけを表示する")
        self.only_check.toggled.connect(lambda _on: self.refresh_list())
        row.addWidget(self.only_check)
        top_l.addLayout(row)
        self.summary_label = QLabel()
        self.summary_label.setObjectName("settingsLabel")
        self.summary_label.setWordWrap(True)
        top_l.addWidget(self.summary_label)
        row2 = QHBoxLayout()
        self.zoom_check = QCheckBox("指摘箇所を拡大して表示")
        self.zoom_check.setChecked(True)
        self.zoom_check.setToolTip("指摘をクリックしたとき、表題欄などの該当箇所を拡大して画面の中央に出す")
        row2.addWidget(self.zoom_check)
        row2.addStretch(1)
        for text, tip, slot in (("CSV出力", "チェック結果をCSV（Excelで開ける）で保存", self._export_csv),
                                ("HTMLレポート", "チェック結果を印刷できるHTMLで保存", self._export_html),
                                ("様式を変更", "表題欄・目次の位置の設定（様式）を選び直す", self._change_layout)):
            b = _small_button(text, tip)
            b.clicked.connect(slot)
            row2.addWidget(b)
        top_l.addLayout(row2)
        lay.addWidget(top)

        self.tree = _make_tree(["ページ", "図面番号", "図面名称", "判定"])
        self.tree.setColumnWidth(0, 56)
        self.tree.setColumnWidth(1, 70)
        self.tree.setColumnWidth(2, 190)
        self.tree.setWordWrap(True)
        self.tree.currentItemChanged.connect(self._on_item_changed)
        self.tree.itemClicked.connect(lambda item, _c: self._on_item_changed(item, None, force=True))
        lay.addWidget(self.tree, 1)
        lay.addWidget(self._build_collapse_bar())
        self.sidebar = left
        splitter.addWidget(left)

        splitter.addWidget(self._build_viewer_area())
        self.zoom_combo.addItem("拡大表示", QPdfView.ZoomMode.Custom)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 6)

    # ------------------------------------------------------------ 読み込み
    def load(self, force_rebuild: bool = False, status_cb=None) -> bool:
        """図面を読み取ってチェックし、一覧とPDFを表示する。"""
        if status_cb:
            status_cb("図面を読み取ってチェックしています...")
        QApplication.processEvents()
        try:
            layout = load_layout(self.layout_key)
            self.result = run_checks(self.pdf_path, layout)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "図面を読み取れませんでした", f"{os.path.basename(self.pdf_path)}\n\n詳細: {e}")
            if status_cb:
                status_cb("図面を読み取れませんでした")
            return False
        if force_rebuild or self.document.status() != QPdfDocument.Status.Ready:
            self.document.load(self.pdf_path)
        self.refresh_list()
        self.page_changed.emit()
        return True

    # ------------------------------------------------------------ 一覧
    def _matches(self, texts: list[str]) -> bool:
        q = self.current_query.strip().lower()
        if not q:
            return True
        joined = " ".join(texts).lower()
        return all(tok in joined for tok in q.split())

    def refresh_list(self):
        """チェック結果から一覧を作り直す（絞り込み・「要確認のみ」を反映）。"""
        self._syncing_selection = True
        try:
            self.tree.clear()
            res = self.result
            if res is None:
                self._update_summary()
                return
            only = self.only_check.isChecked()
            general = res.general_findings
            if general and (not only or any(f.level == LEVEL_CHECK for f in general)):
                g_item = QTreeWidgetItem(["", "", "目次・図面セット全体", f"{len(general)} 件"])
                g_item.setData(0, ITEM_ROLE, ("general", None))
                self._style_row(g_item, any(f.level == LEVEL_CHECK for f in general), neutral=True)
                self.tree.addTopLevelItem(g_item)
                g_item.setFirstColumnSpanned(False)
                for f in general:
                    self._add_finding(g_item, f)
                g_item.setExpanded(True)
            expand = len(res.sheets) <= 40
            for s in res.sheets:
                texts = [str(s.page), s.drawing_no, s.name] + [f.message for f in s.findings]
                if only and not any(f.level == LEVEL_CHECK for f in s.findings):
                    continue
                if not self._matches(texts):
                    continue
                n = sum(1 for f in s.findings if f.level == LEVEL_CHECK)
                item = QTreeWidgetItem([f"p{s.page}", s.drawing_no, s.name, f"要確認 {n}" if n else "OK"])
                item.setData(0, ITEM_ROLE, ("sheet", s.page))
                item.setToolTip(2, s.name)
                self._style_row(item, n > 0)
                self.tree.addTopLevelItem(item)
                for f in s.findings:
                    self._add_finding(item, f)
                item.setExpanded(expand)
        finally:
            self._syncing_selection = False
        self._update_summary()

    def _style_row(self, item: QTreeWidgetItem, warn: bool, neutral: bool = False):
        for c in range(4):
            if warn:
                item.setBackground(c, C_WARN_BG)
        item.setForeground(3, C_WARN_TEXT if warn else (C_INFO_TEXT if neutral else C_OK_TEXT))
        font = item.font(3)
        font.setBold(True)
        item.setFont(3, font)

    def _add_finding(self, parent: QTreeWidgetItem, f: Finding):
        child = QTreeWidgetItem([f"{'⚠' if f.level == LEVEL_CHECK else 'ℹ'} {f.rule_name}：{f.message}"])
        child.setData(0, ITEM_ROLE, ("finding", f))
        child.setToolTip(0, f.message)
        child.setForeground(0, C_WARN_TEXT if f.level == LEVEL_CHECK else C_INFO_TEXT)
        parent.addChild(child)
        child.setFirstColumnSpanned(True)
        for d in f.details:
            sub = QTreeWidgetItem([d])
            sub.setData(0, ITEM_ROLE, ("detail", f))
            sub.setForeground(0, C_INFO_TEXT)
            child.addChild(sub)
            sub.setFirstColumnSpanned(True)

    def _update_summary(self):
        res = self.result
        if res is None:
            self.summary_label.setText("")
            self.count_label.setText("")
            return
        kekban = [e.番号 for e in res.index if e.欠番]
        n_check = res.count(LEVEL_CHECK)
        self.summary_label.setText(
            f"様式: {res.layout.name}　目次 {len(res.index)} 行（欠番 {len(kekban)} 件）　"
            f"図面 {len(res.sheets)} ページ　要確認 {n_check} 件\n"
            "※「要確認」は誤りと決まったものではありません。図面を見て判断してください。")
        shown = self.result_count()
        self.count_label.setText(f"{shown} / {len(res.sheets)} ページ表示 · 要確認 {n_check} 件")

    def result_count(self) -> int:
        return sum(1 for i in range(self.tree.topLevelItemCount())
                   if (self.tree.topLevelItem(i).data(0, ITEM_ROLE) or ("",))[0] == "sheet")

    # ------------------------------------------------------------ 選択・表示
    def _finding_marks(self, page: int, strong: Finding | None = None) -> list:
        marks = []
        if self.result is None:
            return marks
        for f in self.result.findings:
            if f.page == page and f.rect:
                marks.append((page - 1, _qrect(f.rect), "strong" if f is strong else "normal"))
        return marks

    def _on_item_changed(self, item: QTreeWidgetItem | None, _prev=None, force: bool = False):
        if item is None or (self._syncing_selection and not force):
            return
        kind, value = item.data(0, ITEM_ROLE) or (None, None)
        if kind == "sheet":
            self.pdf_view.set_marks(self._finding_marks(value))
            self._begin_reveal()
            self.pdf_view.reveal(value - 1)
        elif kind in ("finding", "detail"):
            f: Finding = value
            if f.page is None:
                self.pdf_view.set_marks([])
                _status(self, f.message, 8000)
                return
            self.pdf_view.set_marks(self._finding_marks(f.page, strong=f))
            rect = _qrect(f.rect)
            zoom = self.zoom_check.isChecked() and rect is not None
            self._begin_reveal()
            if zoom:
                self.set_zoom_mode(QPdfView.ZoomMode.Custom)
            self.pdf_view.reveal(f.page - 1, rect, zoom=zoom)
            _status(self, f.message, 8000)

    def _begin_reveal(self):
        self._revealing = True
        QTimer.singleShot(1000, self._end_reveal)  # 念のため（位置合わせの完了通知が来なかったとき）

    def _end_reveal(self):
        self._revealing = False

    def _sync_selection_to_page(self, page: int):
        """PDFのスクロールに合わせて、一覧の該当ページの行を選ぶ。"""
        if self._revealing:
            return
        cur = self.tree.currentItem()
        if cur is not None:
            kind, value = cur.data(0, ITEM_ROLE) or (None, None)
            if (kind == "sheet" and value == page + 1) or (kind == "finding" and value.page == page + 1):
                return
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            kind, value = item.data(0, ITEM_ROLE) or (None, None)
            if kind == "sheet" and value == page + 1:
                self._syncing_selection = True
                try:
                    self.tree.setCurrentItem(item)
                    self.tree.scrollToItem(item)
                finally:
                    self._syncing_selection = False
                return

    # ------------------------------------------------------------ 操作
    def _default_output(self, suffix: str) -> str:
        stem = os.path.splitext(os.path.basename(self.pdf_path))[0]
        return os.path.join(os.path.dirname(self.pdf_path), f"{stem}_チェック結果{suffix}")

    def _export_csv(self):
        if self.result is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "CSVで保存", self._default_output(".csv"), "CSV (*.csv)")
        if path:
            self._try_write(lambda: report.write_check_csv(self.result, path), path)

    def _export_html(self):
        if self.result is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "HTMLレポートを保存", self._default_output(".html"),
                                              "HTML (*.html)")
        if path:
            self._try_write(lambda: report.write_check_html(self.result, path), path)

    def _try_write(self, fn, path: str):
        try:
            fn()
        except OSError as e:
            QMessageBox.warning(self, "保存できませんでした", f"{path}\n\n詳細: {e}\n（Excelなどで開いていないか確認してください）")
            return
        _status(self, f"保存しました: {path}")

    def _change_layout(self):
        key = choose_layout(self, self.layout_key)
        if key and key != self.layout_key:
            self.layout_key = key
            self.load(status_cb=lambda t: _status(self, t))


def open_drawing_set(window, path: str | None = None, layout_key: str | None = None) -> DrawingSetTab | None:
    """PDFを図面セットとして開く（新しいタブ）。同じPDFを別の表示で開いていれば置き換える。"""
    if not path:
        path, _ = QFileDialog.getOpenFileName(window, "図面セットのPDFを開く", "", "PDF Files (*.pdf)")
        if not path:
            return None
    if layout_key is None:
        layout_key = choose_layout(window)
        if layout_key is None:
            return None
    tabs = window.tabs
    replaced_index = -1
    for i in range(tabs.count()):
        w = tabs.widget(i)
        if isinstance(w, app.BasePdfTab) and os.path.abspath(w.pdf_path) == os.path.abspath(path):
            replaced_index = i
            break
    tab = DrawingSetTab(path, layout_key)
    if not tab.load(status_cb=window.status.showMessage):
        tab.document.close()
        tab.deleteLater()
        return None
    index = replaced_index if replaced_index >= 0 else window._plus_tab_index()
    tabs.insertTab(index, tab, tab.title)
    tabs.setTabToolTip(index, path)
    window._install_close_button(tab)
    window._install_favorite_star(tab)
    tabs.setCurrentWidget(tab)
    if replaced_index >= 0:
        old = tabs.widget(tabs.indexOf(tab) + 1)
        if isinstance(old, app.BasePdfTab):
            tabs.removeTab(tabs.indexOf(old))
            old.document.close()
            old.deleteLater()
    _close_welcome(window)
    window._apply_fullscreen_chrome()
    res = tab.result
    window.status.showMessage(f"図面 {len(res.sheets)} ページを読み取りました（要確認 {res.count(LEVEL_CHECK)} 件）", 8000)
    return tab


def _close_welcome(window):
    idx = window._find_welcome_tab_index()
    if idx >= 0:
        w = window.tabs.widget(idx)
        window.tabs.removeTab(idx)
        w.deleteLater()


# ================================================================ 工事台帳（フェーズ3・4）
def _record_summary(rec: dict, width: int = 40) -> str:
    text = (rec.get("内容") or "").replace("\n", " ")
    return text if len(text) <= width else text[:width - 1] + "…"


def _drawing_label(d: dict) -> str:
    return f"No.{d.get('図面番号', '')}　{d.get('図面名称', '')}"


def _ledger_index(ledger: Ledger) -> list[IndexEntry]:
    """工事台帳の図面一覧を、チェック用の目次の形にする（目次のない変更図面の照合用）。"""
    return [IndexEntry(番号=d["図面番号"], 名称=d.get("図面名称", ""), 区分=d.get("区分", ""), 欠番=False,
                       rect=(0, 0, 0, 0), page=0) for d in ledger.drawings]


class PdfDocCache:
    """開いたPDFを使い回す（同じPDFの別ページを表示するたびに読み込み直さないため）。"""

    def __init__(self, owner):
        self._owner = owner
        self._docs: dict[str, QPdfDocument] = {}

    def get(self, path: str) -> QPdfDocument | None:
        if not path or not os.path.isfile(path):
            return None
        key = os.path.abspath(path)
        doc = self._docs.get(key)
        if doc is None:
            doc = QPdfDocument(self._owner)
            doc.load(key)
            self._docs[key] = doc
        return doc

    def close_all(self):
        for doc in self._docs.values():
            doc.close()
        self._docs.clear()


class LedgerTab(QWidget):
    """工事台帳のタブ。左に図面・変更記録・変更契約/竣工の3つの画面、右に図面の表示。"""

    def __init__(self, ledger: Ledger, parent=None):
        super().__init__(parent)
        self.ledger = ledger
        self.docs = PdfDocCache(self)
        self._shown: tuple[str, dict] | None = None   # (図面番号, 表示中の版)
        self._updating = False
        self._build_ui()
        self.refresh_all()

    @property
    def title(self) -> str:
        return "工事台帳: " + (self.ledger.koji.get("工事名") or os.path.basename(self.ledger.folder))

    @property
    def folder(self) -> str:
        return self.ledger.folder

    # ------------------------------------------------------------ 画面
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        splitter = app.RatioSplitter(Qt.Orientation.Horizontal, 0.42)
        outer.addWidget(splitter)

        left = QWidget()
        left.setObjectName("sidebar")
        lay = QVBoxLayout(left)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head = QWidget()
        head.setObjectName("sortBar")
        head_l = QHBoxLayout(head)
        head_l.setContentsMargins(10, 8, 10, 8)
        self.koji_label = QLabel()
        self.koji_label.setWordWrap(True)
        head_l.addWidget(self.koji_label, 1)
        edit_koji = _small_button("工事情報", "工事名・請負者などを編集する")
        edit_koji.clicked.connect(self._edit_koji)
        head_l.addWidget(edit_koji)
        lay.addWidget(head)

        self.pages = QTabWidget()
        self.pages.setDocumentMode(True)
        self.pages.addTab(self._build_drawings_page(), "図面")
        self.pages.addTab(self._build_records_page(), "変更記録一覧")
        self.pages.addTab(self._build_contract_page(), "変更契約・竣工図")
        lay.addWidget(self.pages, 1)
        self.sidebar = left
        splitter.addWidget(left)
        splitter.addWidget(self._build_viewer())
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 6)

    def _build_drawings_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(8, 8, 8, 8)
        row = QHBoxLayout()
        self.drawing_filter = QLineEdit()
        self.drawing_filter.setPlaceholderText("図面番号・名称で絞り込み")
        self.drawing_filter.setClearButtonEnabled(True)
        self.drawing_filter.textChanged.connect(lambda _t: self._fill_drawings())
        row.addWidget(self.drawing_filter, 1)
        self.only_changed = QCheckBox("変更記録のある図面のみ")
        self.only_changed.toggled.connect(lambda _on: self._fill_drawings())
        row.addWidget(self.only_changed)
        lay.addLayout(row)

        split = QSplitter(Qt.Orientation.Vertical)
        self.drawing_tree = _make_tree(["番号", "図面名称", "区分", "最新の版", "変更", "指示済"])
        for c, w in enumerate((56, 190, 44, 80, 44, 50)):
            self.drawing_tree.setColumnWidth(c, w)
        self.drawing_tree.setRootIsDecorated(False)
        self.drawing_tree.currentItemChanged.connect(self._on_drawing_selected)
        split.addWidget(self.drawing_tree)

        box = QWidget()
        box_l = QVBoxLayout(box)
        box_l.setContentsMargins(0, 6, 0, 0)
        self.drawing_records_label = QLabel("この図面の変更記録")
        self.drawing_records_label.setObjectName("sectionHeading")
        box_l.addWidget(self.drawing_records_label)
        self.drawing_records = _make_tree(["ID", "指示日", "状態", "内容"])
        for c, w in enumerate((56, 84, 128)):
            self.drawing_records.setColumnWidth(c, w)
        self.drawing_records.setRootIsDecorated(False)
        self.drawing_records.currentItemChanged.connect(lambda item, _p: self._on_record_selected(item, "drawing"))
        self.drawing_records.itemDoubleClicked.connect(lambda item, _c: self._edit_record(item))
        box_l.addWidget(self.drawing_records, 1)
        btns = QHBoxLayout()
        add = QPushButton("新規登録")
        add.setObjectName("primaryButton")
        add.clicked.connect(lambda: self._new_record([self._current_drawing_no()] if self._current_drawing_no() else []))
        btns.addWidget(add)
        edit = QPushButton("編集")
        edit.clicked.connect(lambda: self._edit_record(self.drawing_records.currentItem()))
        btns.addWidget(edit)
        btns.addWidget(self._status_button(lambda: self.drawing_records.currentItem()))
        btns.addStretch(1)
        compare = QPushButton("新旧対照")
        compare.setToolTip("この図面の前の版と最新の版を並べて表示する")
        compare.clicked.connect(self._compare_current)
        btns.addWidget(compare)
        box_l.addLayout(btns)
        split.addWidget(box)
        split.setSizes([400, 300])
        lay.addWidget(split, 1)
        return page

    def _status_button(self, current_item_fn) -> QToolButton:
        b = QToolButton()
        b.setText("状態を変える ▾")
        b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(b)
        for st in STATUSES:
            menu.addAction(st, lambda s=st: self._change_status(current_item_fn(), s))
        b.setMenu(menu)
        return b

    def _build_records_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(8, 8, 8, 8)
        grid = QGridLayout()
        grid.addWidget(QLabel("状態"), 0, 0)
        self.f_status = QComboBox()
        self.f_status.addItem("すべて", "")
        for st in STATUSES:
            self.f_status.addItem(st, st)
        grid.addWidget(self.f_status, 0, 1)
        grid.addWidget(QLabel("図面"), 0, 2)
        self.f_drawing = QComboBox()
        self.f_drawing.setMinimumWidth(160)
        grid.addWidget(self.f_drawing, 0, 3)
        grid.addWidget(QLabel("期間"), 1, 0)
        self.f_from = QLineEdit()
        self.f_from.setPlaceholderText("2024-04-01")
        self.f_to = QLineEdit()
        self.f_to.setPlaceholderText("2025-03-31")
        period = QHBoxLayout()
        period.addWidget(self.f_from)
        period.addWidget(QLabel("〜"))
        period.addWidget(self.f_to)
        grid.addLayout(period, 1, 1, 1, 3)
        clear = _small_button("条件を解除")
        clear.clicked.connect(self._clear_filters)
        grid.addWidget(clear, 1, 4)
        grid.setColumnStretch(3, 1)
        lay.addLayout(grid)
        for w in (self.f_status, self.f_drawing):
            w.currentIndexChanged.connect(lambda _i: self._fill_records())
        for w in (self.f_from, self.f_to):
            w.textChanged.connect(lambda _t: self._fill_records())
        note = QLabel("期間は指示日（なければ記録日）で絞り込みます。")
        note.setObjectName("settingsNote")
        lay.addWidget(note)

        self.records_tree = _make_tree(["ID", "状態", "指示日", "対象図面", "内容", "きっかけ", "指示方法"])
        for c, w in enumerate((56, 128, 84, 90, 220, 110)):
            self.records_tree.setColumnWidth(c, w)
        self.records_tree.setRootIsDecorated(False)
        self.records_tree.currentItemChanged.connect(lambda item, _p: self._on_record_selected(item, "list"))
        self.records_tree.itemDoubleClicked.connect(lambda item, _c: self._edit_record(item))
        lay.addWidget(self.records_tree, 1)
        self.records_count = QLabel()
        self.records_count.setObjectName("countLabel")
        lay.addWidget(self.records_count)
        btns = QHBoxLayout()
        add = QPushButton("新規登録")
        add.setObjectName("primaryButton")
        add.clicked.connect(lambda: self._new_record([]))
        btns.addWidget(add)
        edit = QPushButton("編集")
        edit.clicked.connect(lambda: self._edit_record(self.records_tree.currentItem()))
        btns.addWidget(edit)
        btns.addWidget(self._status_button(lambda: self.records_tree.currentItem()))
        btns.addStretch(1)
        csv_b = QPushButton("表示中の一覧をCSV出力")
        csv_b.clicked.connect(self._export_records_csv)
        btns.addWidget(csv_b)
        lay.addLayout(btns)
        return page

    def _build_contract_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(8, 8, 8, 8)

        contract = QGroupBox("変更契約")
        c_l = QVBoxLayout(contract)
        self.contract_label = QLabel()
        self.contract_label.setWordWrap(True)
        c_l.addWidget(self.contract_label)
        row = QHBoxLayout()
        for text, tip, slot in (
                ("変更契約用一覧（HTML）", "状態が「指示済」の変更を図面別に一覧にする（変更図面の作成指示・変更理由の下書き用）",
                 lambda: self._export("contract_html")),
                ("同（CSV）", "", lambda: self._export("contract_csv"))):
            b = QPushButton(text)
            if tip:
                b.setToolTip(tip)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        reg = QPushButton("変更図面を登録...")
        reg.setObjectName("primaryButton")
        reg.setToolTip("変更図面のPDFを新しい版として登録し、反映した変更記録を「変更契約に反映済」にする")
        reg.clicked.connect(self._register_version)
        row.addWidget(reg)
        c_l.addLayout(row)
        lay.addWidget(contract)

        done = QGroupBox("竣工図チェックリスト（竣工図に反映すべき全変更）")
        d_l = QVBoxLayout(done)
        self.checklist_label = QLabel()
        self.checklist_label.setWordWrap(True)
        d_l.addWidget(self.checklist_label)
        self.checklist = _make_tree(["反映確認", "状態", "指示日", "内容"])
        for c, w in enumerate((200, 128, 84)):
            self.checklist.setColumnWidth(c, w)
        self.checklist.itemChanged.connect(self._on_checklist_changed)
        self.checklist.currentItemChanged.connect(lambda item, _p: self._on_record_selected(item, "checklist"))
        d_l.addWidget(self.checklist, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        for text, kind in (("チェックリスト（HTML）", "checklist_html"), ("同（CSV）", "checklist_csv")):
            b = QPushButton(text)
            b.clicked.connect(lambda _c=False, k=kind: self._export(k))
            row.addWidget(b)
        d_l.addLayout(row)
        lay.addWidget(done, 1)
        return page

    def _build_viewer(self) -> QWidget:
        area = QWidget()
        lay = QVBoxLayout(area)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QWidget()
        bar.setObjectName("viewerBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 5, 10, 5)
        self.viewer_label = QLabel("図面を選ぶと、ここに表示します")
        self.viewer_label.setObjectName("pageLabel")
        row.addWidget(self.viewer_label, 1)
        row.addWidget(QLabel("版:"))
        self.version_combo = QComboBox()
        self.version_combo.setMinimumWidth(190)
        self.version_combo.activated.connect(self._on_version_chosen)
        row.addWidget(self.version_combo)
        self.viewer_zoom = QComboBox()
        self.viewer_zoom.addItem("ページ全体", QPdfView.ZoomMode.FitInView)
        self.viewer_zoom.addItem("幅に合わせる", QPdfView.ZoomMode.FitToWidth)
        self.viewer_zoom.currentIndexChanged.connect(
            lambda _i: self.viewer.setZoomMode(self.viewer_zoom.currentData()))
        row.addWidget(self.viewer_zoom)
        open_b = _small_button("PDFを開く", "表示中のPDFをタブで開く")
        open_b.clicked.connect(self._open_shown_pdf)
        row.addWidget(open_b)
        lay.addWidget(bar)
        self.viewer = HighlightPdfView()
        self.viewer.setPageMode(QPdfView.PageMode.SinglePage)
        self.viewer.setZoomMode(QPdfView.ZoomMode.FitInView)
        app._style_pdf_view(self.viewer)
        lay.addWidget(self.viewer, 1)
        return area

    # ------------------------------------------------------------ 一覧の作り直し
    def refresh_all(self):
        """台帳の内容から、すべての一覧を作り直す（選択はなるべく保つ）。"""
        L = self.ledger
        k = L.koji
        info = "　".join(f"{key}: {k.get(key)}" for key in ("工事番号", "請負者", "工期") if k.get(key))
        self.koji_label.setText(f"<b>{k.get('工事名') or '（工事名未入力）'}</b><br>"
                                f"<span style='color:{app.C_SUB}'>{info or os.path.basename(L.folder)}</span>")
        self._fill_drawings()
        self._fill_drawing_filter()
        self._fill_records()
        self._fill_contract()
        window = self.window()
        if hasattr(window, "tabs"):
            idx = window.tabs.indexOf(self)
            if idx >= 0:
                window.tabs.setTabText(idx, self.title)

    def _current_drawing_no(self) -> str | None:
        item = self.drawing_tree.currentItem()
        return item.data(0, ITEM_ROLE) if item is not None else None

    def _fill_drawings(self):
        keep = self._current_drawing_no()
        q = self.drawing_filter.text().strip().lower()
        self._updating = True
        try:
            self.drawing_tree.clear()
            select = None
            for d in self.ledger.drawings:
                recs = self.ledger.records_for_drawing(d["図面番号"])
                if self.only_changed.isChecked() and not recs:
                    continue
                if q and q not in f"{d['図面番号']} {d.get('図面名称', '')}".lower():
                    continue
                latest = self.ledger.latest_version(d["図面番号"])
                n_inst = sum(1 for r in recs if r.get("状態") == STATUS_INSTRUCTED)
                item = QTreeWidgetItem([d["図面番号"], d.get("図面名称", ""), d.get("区分", ""),
                                        latest["版区分"] if latest else "（PDFなし）",
                                        str(len(recs)) if recs else "", str(n_inst) if n_inst else ""])
                item.setData(0, ITEM_ROLE, d["図面番号"])
                item.setToolTip(1, d.get("図面名称", ""))
                if latest is None:
                    item.setForeground(3, C_INFO_TEXT)
                if n_inst:
                    item.setForeground(5, C_WARN_TEXT)
                    for c in range(6):
                        item.setBackground(c, C_WARN_BG)
                self.drawing_tree.addTopLevelItem(item)
                if d["図面番号"] == keep:
                    select = item
        finally:
            self._updating = False
        if select is not None:
            self.drawing_tree.setCurrentItem(select)
        else:
            self._fill_drawing_records(None)

    def _fill_drawing_records(self, no: str | None):
        self.drawing_records.clear()
        if not no:
            self.drawing_records_label.setText("この図面の変更記録（図面を選んでください）")
            return
        d = self.ledger.drawing(no)
        recs = self.ledger.records_for_drawing(no)
        self.drawing_records_label.setText(f"{_drawing_label(d)} の変更記録（{len(recs)} 件・古い順）")
        for r in recs:
            self.drawing_records.addTopLevelItem(self._record_item(r, ["id", "指示日", "状態", "内容"]))

    def _record_item(self, r: dict, cols: list[str]) -> QTreeWidgetItem:
        values = []
        for c in cols:
            if c == "内容":
                values.append(_record_summary(r))
            elif c == "指示日":
                values.append(r.get("指示日") or f"({r.get('記録日', '')})")
            elif c == "対象図面":
                values.append("・".join(r.get("対象図面", [])))
            elif c == "きっかけ":
                t = r.get("きっかけ") or {}
                values.append(" ".join(x for x in (t.get("種別", ""), t.get("番号", "")) if x))
            else:
                values.append(str(r.get(c, "")))
        item = QTreeWidgetItem(values)
        item.setData(0, ITEM_ROLE, r["id"])
        item.setToolTip(cols.index("内容") if "内容" in cols else 0, r.get("内容", ""))
        st = r.get("状態")
        if "状態" in cols:
            col = cols.index("状態")
            if st == STATUS_INSTRUCTED:
                item.setForeground(col, C_WARN_TEXT)
            elif st == STATUS_COMPLETED:
                item.setForeground(col, C_OK_TEXT)
            elif st in (STATUS_CANCELLED, STATUS_DISCUSSING):
                item.setForeground(col, C_INFO_TEXT)
        return item

    def _fill_drawing_filter(self):
        keep = self.f_drawing.currentData()
        self.f_drawing.blockSignals(True)
        self.f_drawing.clear()
        self.f_drawing.addItem("すべて", "")
        for d in self.ledger.drawings:
            self.f_drawing.addItem(_drawing_label(d), d["図面番号"])
        idx = self.f_drawing.findData(keep) if keep else 0
        self.f_drawing.setCurrentIndex(max(0, idx))
        self.f_drawing.blockSignals(False)

    def _clear_filters(self):
        for w in (self.f_status, self.f_drawing):
            w.blockSignals(True)
            w.setCurrentIndex(0)
            w.blockSignals(False)
        for w in (self.f_from, self.f_to):
            w.blockSignals(True)
            w.clear()
            w.blockSignals(False)
        self._fill_records()

    def _filtered_records(self) -> list[dict]:
        date_from, date_to = self.f_from.text().strip(), self.f_to.text().strip()
        return self.ledger.filter_records(status=self.f_status.currentData() or None,
                                          drawing_no=self.f_drawing.currentData() or None,
                                          date_from=date_from, date_to=date_to)

    def _fill_records(self):
        keep = self.records_tree.currentItem().data(0, ITEM_ROLE) if self.records_tree.currentItem() else None
        self.records_tree.clear()
        recs = self._filtered_records()
        for r in recs:
            item = self._record_item(r, ["id", "状態", "指示日", "対象図面", "内容", "きっかけ", "指示方法"])
            self.records_tree.addTopLevelItem(item)
            if r["id"] == keep:
                self.records_tree.setCurrentItem(item)
        self.records_count.setText(f"{len(recs)} / {len(self.ledger.records)} 件表示")

    def _fill_contract(self):
        L = self.ledger
        pending = L.pending_for_contract()
        n_rec = len({r["id"] for _, rs in pending for r in rs})
        kinds = [k for k in L.version_kinds() if k != VERSION_KIND_ORDER]
        self.contract_label.setText(
            f"変更契約に反映していない変更（状態「指示済」）: 図面 {len(pending)} 枚・変更記録 {n_rec} 件"
            + (f"<br>登録済みの版: {'、'.join(L.version_kinds())}" if kinds or L.version_kinds() else ""))
        self._updating = True
        try:
            self.checklist.clear()
            groups = L.completion_checklist()
            total = done = 0
            for d, recs in groups:
                top = QTreeWidgetItem([_drawing_label(d)])
                top.setData(0, ITEM_ROLE, None)
                self.checklist.addTopLevelItem(top)
                top.setFirstColumnSpanned(True)
                for r in recs:
                    child = QTreeWidgetItem([r["id"], r.get("状態", ""), r.get("指示日", ""), _record_summary(r)])
                    child.setData(0, ITEM_ROLE, r["id"])
                    child.setData(1, ITEM_ROLE, d["図面番号"])
                    child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    is_done = L.is_completion_done(r)
                    child.setCheckState(0, Qt.CheckState.Checked if is_done else Qt.CheckState.Unchecked)
                    child.setToolTip(3, r.get("内容", ""))
                    top.addChild(child)
                    total += 1
                    done += is_done
                top.setExpanded(True)
            self.checklist_label.setText(
                f"竣工図への反映確認: {done} / {total} 件（契約変更の有無を問わず、協議中・取消を除く全変更）。"
                "反映を確認したらチェックを付けてください（状態が「竣工図に反映済」になります）。")
        finally:
            self._updating = False

    # ------------------------------------------------------------ 図面の表示
    def _on_drawing_selected(self, item, _prev=None):
        if self._updating:
            return
        no = item.data(0, ITEM_ROLE) if item is not None else None
        self._fill_drawing_records(no)
        if no:
            self.show_drawing(no)

    def show_drawing(self, no: str, version: dict | None = None, rec: dict | None = None):
        """図面の版（既定は最新の版）を右側に表示し、変更記録の箇所を赤枠で示す。"""
        d = self.ledger.drawing(no)
        versions = self.ledger.versions_with_pdf(no)
        self.version_combo.clear()
        for v in versions:
            self.version_combo.addItem(f"{v['版区分']} {v.get('日付', '')}".strip(), v)
        if not versions:
            self.viewer.setDocument(None)
            self.viewer.set_marks([])
            self.viewer_label.setText(f"{_drawing_label(d)}（PDFが登録されていません）")
            self._shown = None
            return
        version = version or versions[-1]
        self.version_combo.setCurrentIndex(versions.index(version) if version in versions else len(versions) - 1)
        path = self.ledger.abs_path(version["pdf"])
        doc = self.docs.get(path)
        if doc is None:
            self.viewer.setDocument(None)
            self.viewer_label.setText(f"{_drawing_label(d)}（PDFが見つかりません: {version['pdf']}）")
            self._shown = None
            return
        if self.viewer.document() is not doc:
            self.viewer.setDocument(doc)
        page = int(version.get("ページ") or 1) - 1
        self.viewer.pageNavigator().jump(page, QPointF(0, 0))
        self.viewer_label.setText(f"{_drawing_label(d)}　［{version['版区分']}］ {version['pdf']} p{page + 1}")
        self._shown = (no, version)
        self._show_marks(rec)

    def _show_marks(self, rec: dict | None):
        """表示中の図面について、変更記録の「図面上の箇所」を赤枠で示す（rec を渡すとその記録だけ強調）。"""
        if self._shown is None:
            return
        no, version = self._shown
        page = int(version.get("ページ") or 1) - 1
        marks = []
        for r in self.ledger.records_for_drawing(no):
            for p in r.get("図面上の箇所", []):
                if p.get("図面番号") != no or not p.get("rect"):
                    continue
                strong = rec is not None and r["id"] == rec["id"]
                if rec is not None and not strong:
                    continue
                same = p.get("版区分", version["版区分"]) == version["版区分"]
                marks.append((page, _qrect(p["rect"]), ("strong" if strong else "normal") if same else "old"))
        self.viewer.set_marks(marks)

    def _on_version_chosen(self, _index: int):
        v = self.version_combo.currentData()
        if self._shown and v:
            self.show_drawing(self._shown[0], v)

    def _on_record_selected(self, item, source: str):
        if self._updating or item is None:
            return
        rid = item.data(0, ITEM_ROLE)
        rec = self.ledger.record(rid) if rid else None
        if rec is None:
            return
        if source == "drawing" and self._shown:
            self._show_marks(rec)
            return
        no = item.data(1, ITEM_ROLE) if source == "checklist" else None
        targets = rec.get("対象図面", [])
        no = no or (targets[0] if targets else None)
        if no:
            self.show_drawing(no, rec=rec)

    def _open_shown_pdf(self):
        if self._shown:
            window = self.window()
            if hasattr(window, "load_pdf"):
                window.load_pdf(self.ledger.abs_path(self._shown[1]["pdf"]))

    # ------------------------------------------------------------ 保存
    def save(self) -> bool:
        """台帳を保存する。他で更新されていたら、上書きするか読み込み直すかを尋ねる。"""
        try:
            self.ledger.save()
            return True
        except LedgerConflictError:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("工事台帳が他で更新されています")
            box.setText("この工事台帳は、開いた後に別のパソコンなどで更新されています。")
            box.setInformativeText("「上書き保存」: この画面の内容で上書きします（他での変更は失われます）。\n"
                                   "「読み込み直す」: 他での変更を読み込みます（今回の変更は失われます）。")
            over = box.addButton("上書き保存", QMessageBox.ButtonRole.AcceptRole)
            reload_b = box.addButton("読み込み直す", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is over:
                self.ledger.save(force=True)
                return True
            if box.clickedButton() is reload_b:
                self.reload()
            return False
        except OSError as e:
            QMessageBox.warning(self, "保存できませんでした", f"{self.ledger.path}\n\n詳細: {e}")
            return False

    def reload(self):
        try:
            self.ledger = Ledger.load(self.ledger.folder)
        except LedgerError as e:
            QMessageBox.warning(self, "工事台帳を読み込めませんでした", str(e))
            return
        self.refresh_all()

    def _commit(self, message: str):
        if self.save():
            _status(self, message)
        self.refresh_all()

    # ------------------------------------------------------------ 変更記録の操作
    def _new_record(self, preset: list[str]):
        dlg = RecordDialog(self.ledger, None, preset, self.docs, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            rec = self.ledger.add_record(dlg.values())
            self._commit(f"変更記録 {rec['id']} を登録しました")

    def _edit_record(self, item):
        rid = item.data(0, ITEM_ROLE) if item is not None else None
        rec = self.ledger.record(rid) if rid else None
        if rec is None:
            QMessageBox.information(self, "変更記録", "編集する変更記録を一覧から選んでください。")
            return
        dlg = RecordDialog(self.ledger, rec, [], self.docs, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            changed = self.ledger.update_record(rid, dlg.values())
            if changed:
                self._commit(f"変更記録 {rid} を更新しました（{'・'.join(changed)}）")

    def _change_status(self, item, status: str):
        rid = item.data(0, ITEM_ROLE) if item is not None else None
        if not rid or self.ledger.record(rid) is None:
            QMessageBox.information(self, "状態を変える", "状態を変える変更記録を一覧から選んでください。")
            return
        if status == STATUS_COMPLETED:
            changed = self.ledger.set_completion_done(rid, True)
        else:
            changed = self.ledger.set_status(rid, status)
        if changed:
            self._commit(f"変更記録 {rid} の状態を「{status}」にしました")

    def _on_checklist_changed(self, item: QTreeWidgetItem, column: int):
        if self._updating or column != 0:
            return
        rid = item.data(0, ITEM_ROLE)
        if not rid:
            return
        done = item.checkState(0) == Qt.CheckState.Checked
        if self.ledger.set_completion_done(rid, done):
            QTimer.singleShot(0, lambda: self._commit(
                f"変更記録 {rid} を{'竣工図に反映済にしました' if done else '竣工図に未反映に戻しました'}"))

    def _edit_koji(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("工事情報")
        form = QFormLayout(dlg)
        edits = {}
        for k in KOJI_FIELDS:
            e = QLineEdit(self.ledger.koji.get(k, ""))
            form.addRow(k, e)
            edits[k] = e
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            for k, e in edits.items():
                self.ledger.koji[k] = e.text().strip()
            self._commit("工事情報を保存しました")

    # ------------------------------------------------------------ 出力・変更図面
    def _export(self, kind: str):
        names = {"contract_html": ("変更契約用一覧.html", "HTML (*.html)", report.write_contract_list_html),
                 "contract_csv": ("変更契約用一覧.csv", "CSV (*.csv)", report.write_contract_list_csv),
                 "checklist_html": ("竣工図チェックリスト.html", "HTML (*.html)", report.write_completion_checklist_html),
                 "checklist_csv": ("竣工図チェックリスト.csv", "CSV (*.csv)", report.write_completion_checklist_csv)}
        name, flt, fn = names[kind]
        path, _ = QFileDialog.getSaveFileName(self, "保存", os.path.join(self.ledger.folder, name), flt)
        if not path:
            return
        try:
            fn(self.ledger, path)
        except OSError as e:
            QMessageBox.warning(self, "保存できませんでした", f"{path}\n\n詳細: {e}")
            return
        _status(self, f"保存しました: {path}")

    def _export_records_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "CSVで保存", os.path.join(self.ledger.folder, "変更記録一覧.csv"),
                                              "CSV (*.csv)")
        if path:
            try:
                report.write_records_csv(self._filtered_records(), path)
            except OSError as e:
                QMessageBox.warning(self, "保存できませんでした", f"{path}\n\n詳細: {e}")
                return
            _status(self, f"保存しました: {path}")

    def _register_version(self):
        dlg = RegisterVersionDialog(self.ledger, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            try:
                n_pages, n_recs = dlg.apply()
            except (OSError, LedgerError) as e:
                QMessageBox.warning(self, "登録できませんでした", str(e))
                return
            self._commit(f"変更図面 {n_pages} 枚を登録し、変更記録 {n_recs} 件を「変更契約に反映済」にしました")

    def _compare_current(self):
        no = self._current_drawing_no()
        if not no:
            QMessageBox.information(self, "新旧対照", "図面を一覧から選んでください。")
            return
        versions = self.ledger.versions_with_pdf(no)
        if len(versions) < 2:
            QMessageBox.information(self, "新旧対照", "この図面には比べられる版が2つ以上ありません。")
            return
        old, new = versions[-2], versions[-1]
        marks = []
        for r in self.ledger.records_for_drawing(no):
            if (r.get("反映先") or {}).get("変更契約") == new["版区分"]:
                marks += [p for p in r.get("図面上の箇所", []) if p.get("図面番号") == no and p.get("rect")]
        win = CompareWindow(_drawing_label(self.ledger.drawing(no)),
                            (old["版区分"], self.ledger.abs_path(old["pdf"]), int(old["ページ"])),
                            (new["版区分"], self.ledger.abs_path(new["pdf"]), int(new["ページ"])),
                            [_qrect(p["rect"]) for p in marks])
        _open_windows.append(win)
        win.destroyed.connect(lambda _=None, w=win: _open_windows.remove(w) if w in _open_windows else None)
        win.show()

    def closeEvent(self, event):
        self.viewer.setDocument(None)
        self.docs.close_all()
        super().closeEvent(event)


# ================================================================ 変更記録の登録・編集
class RecordDialog(QDialog):
    """変更記録の登録・編集フォーム。右側の図面上でドラッグして「図面上の箇所」を登録できる。"""

    def __init__(self, ledger: Ledger, rec: dict | None, preset: list[str], docs: PdfDocCache, parent=None):
        super().__init__(parent)
        self.ledger = ledger
        self.docs = docs
        self.rec = dict(rec) if rec else Ledger.new_record()
        self.places: list[dict] = [dict(p) for p in self.rec.get("図面上の箇所", [])]
        self.setWindowTitle(f"変更記録の編集（{rec['id']}）" if rec else "変更記録の新規登録")
        self._build_ui(preset)
        _fit_to_screen(self, 1400, 900)

    def _build_ui(self, preset: list[str]):
        r = self.rec
        root = QVBoxLayout(self)
        split = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(split, 1)

        # 入力欄が多く画面の高さに収まらないことがあるため、左側はスクロールできるようにする
        # （下の「保存」「キャンセル」ボタンは常に見える位置に置く）
        left = QWidget()
        form = QFormLayout(left)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setWidget(left)
        left_scroll.setMinimumWidth(420)
        self.e_record_day = QLineEdit(r.get("記録日", ""))
        self.e_record_day.setPlaceholderText("2024-04-01")
        form.addRow("記録日", self.e_record_day)
        day_row = QHBoxLayout()
        self.e_day = QLineEdit(r.get("指示日", ""))
        self.e_day.setPlaceholderText("2024-04-01")
        day_row.addWidget(self.e_day)
        today_b = _small_button("今日")
        today_b.clicked.connect(lambda: self.e_day.setText(today_text()))
        day_row.addWidget(today_b)
        form.addRow("指示日", day_row)
        trig = r.get("きっかけ") or {}
        trig_row = QHBoxLayout()
        self.e_trig_kind = QComboBox()
        self.e_trig_kind.setEditable(True)
        self.e_trig_kind.addItems([""] + TRIGGER_KINDS)
        self.e_trig_kind.setCurrentText(trig.get("種別", ""))
        trig_row.addWidget(self.e_trig_kind)
        self.e_trig_no = QLineEdit(trig.get("番号", ""))
        self.e_trig_no.setPlaceholderText("例: 質疑書No.12")
        trig_row.addWidget(self.e_trig_no, 1)
        form.addRow("きっかけ", trig_row)
        self.e_content = QPlainTextEdit(r.get("内容", ""))
        self.e_content.setPlaceholderText("変更の内容（必須）")
        self.e_content.setFixedHeight(80)
        form.addRow("内容 *", self.e_content)
        self.e_reason = QPlainTextEdit(r.get("理由", ""))
        self.e_reason.setFixedHeight(56)
        form.addRow("理由", self.e_reason)
        self.e_decider = QLineEdit(r.get("決定者", ""))
        form.addRow("決定者", self.e_decider)
        self.e_method = QComboBox()
        self.e_method.setEditable(True)
        self.e_method.addItems([""] + INSTRUCTION_METHODS)
        self.e_method.setCurrentText(r.get("指示方法", ""))
        form.addRow("指示方法", self.e_method)
        self.e_status = QComboBox()
        self.e_status.addItems(STATUSES)
        self.e_status.setCurrentText(r.get("状態", STATUS_INSTRUCTED))
        form.addRow("状態", self.e_status)

        targets = set(r.get("対象図面", [])) | {p for p in preset if p}
        tgt_box = QVBoxLayout()
        self.e_target_filter = QLineEdit()
        self.e_target_filter.setPlaceholderText("図面番号・名称で絞り込み")
        self.e_target_filter.textChanged.connect(self._filter_targets)
        tgt_box.addWidget(self.e_target_filter)
        self.e_targets = QListWidget()
        self.e_targets.setFixedHeight(140)
        # チェック欄が見えにくい環境があるため、枠と色をはっきり付ける
        self.e_targets.setStyleSheet(
            f"QListWidget::indicator {{ width: 14px; height: 14px; border: 1px solid {app.C_SUB};"
            f" border-radius: 3px; background: #FFFFFF; }}"
            f"QListWidget::indicator:checked {{ background: {app.C_ACCENT}; border-color: {app.C_ACCENT}; }}")
        for d in self.ledger.drawings:
            it = QListWidgetItem(_drawing_label(d))
            it.setData(ITEM_ROLE, d["図面番号"])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if d["図面番号"] in targets else Qt.CheckState.Unchecked)
            self.e_targets.addItem(it)
        self.e_targets.itemChanged.connect(lambda _it: self._on_targets_changed())
        tgt_box.addWidget(self.e_targets)
        self.e_targets_label = QLabel()
        self.e_targets_label.setObjectName("settingsNote")
        self.e_targets_label.setWordWrap(True)
        tgt_box.addWidget(self.e_targets_label)
        form.addRow("対象図面 *", tgt_box)

        self.e_places = QTreeWidget()
        self.e_places.setHeaderLabels(["図面番号", "版", "コメント（ダブルクリックで入力）"])
        self.e_places.setStyleSheet(TREE_STYLE)
        self.e_places.setRootIsDecorated(False)
        self.e_places.setFixedHeight(110)
        self.e_places.itemChanged.connect(self._on_place_edited)
        self.e_places.currentItemChanged.connect(lambda _i, _p: self._show_place_marks())
        places_box = QVBoxLayout()
        places_box.addWidget(self.e_places)
        del_b = _small_button("選んだ箇所を削除")
        del_b.clicked.connect(self._delete_place)
        places_box.addWidget(del_b, 0, Qt.AlignmentFlag.AlignLeft)
        form.addRow("図面上の箇所", places_box)

        if r.get("履歴"):
            hist = QPlainTextEdit("\n".join(
                f"{h.get('日時', '')}　{h.get('操作者', '')}　{h.get('操作', '')}：{h.get('内容', '')}"
                for h in r["履歴"]))
            hist.setReadOnly(True)
            hist.setFixedHeight(80)
            form.addRow("履歴", hist)
        split.addWidget(left_scroll)

        right = QWidget()
        r_l = QVBoxLayout(right)
        r_l.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("表示する図面:"))
        self.view_combo = QComboBox()
        self.view_combo.setMinimumWidth(220)
        self.view_combo.currentIndexChanged.connect(lambda _i: self._show_selected_drawing())
        bar.addWidget(self.view_combo, 1)
        self.select_button = QToolButton()
        self.select_button.setText("範囲を指定（ドラッグ）")
        self.select_button.setCheckable(True)
        self.select_button.setToolTip("オンにして図面上をドラッグすると、その範囲を「図面上の箇所」に追加します")
        self.select_button.toggled.connect(lambda on: self.view.set_select_mode(on))
        bar.addWidget(self.select_button)
        zoom = QComboBox()
        zoom.addItem("ページ全体", QPdfView.ZoomMode.FitInView)
        zoom.addItem("幅に合わせる", QPdfView.ZoomMode.FitToWidth)
        zoom.currentIndexChanged.connect(lambda _i: self.view.setZoomMode(zoom.currentData()))
        bar.addWidget(zoom)
        r_l.addLayout(bar)
        self.view_note = QLabel()
        self.view_note.setObjectName("settingsNote")
        r_l.addWidget(self.view_note)
        self.view = HighlightPdfView()
        self.view.setPageMode(QPdfView.PageMode.SinglePage)
        self.view.setZoomMode(QPdfView.ZoomMode.FitInView)
        app._style_pdf_view(self.view)
        self.view.area_selected.connect(self._on_area_selected)
        r_l.addWidget(self.view, 1)
        split.addWidget(right)
        split.setChildrenCollapsible(False)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 6)
        split.setSizes([520, 820])

        bb = QDialogButtonBox()
        save_b = bb.addButton("保存  (Ctrl+S)", QDialogButtonBox.ButtonRole.AcceptRole)
        save_b.setObjectName("primaryButton")
        bb.addButton("キャンセル", QDialogButtonBox.ButtonRole.RejectRole)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)
        QShortcut(QKeySequence("Ctrl+S"), self).activated.connect(self._accept)
        self._fill_places()
        self._on_targets_changed()

    # ------------------------------------------------------------ 対象図面
    def _on_targets_changed(self):
        nos = self._checked_targets()
        self.e_targets_label.setText("選択中: " + ("、".join(f"No.{n}" for n in nos) if nos
                                                  else "なし（左端の□をクリックして選んでください）"))
        self._fill_view_combo()

    def _checked_targets(self) -> list[str]:
        return [self.e_targets.item(i).data(ITEM_ROLE) for i in range(self.e_targets.count())
                if self.e_targets.item(i).checkState() == Qt.CheckState.Checked]

    def _filter_targets(self, text: str):
        q = text.strip().lower()
        for i in range(self.e_targets.count()):
            it = self.e_targets.item(i)
            it.setHidden(bool(q) and q not in it.text().lower() and it.checkState() != Qt.CheckState.Checked)

    def _fill_view_combo(self):
        keep = self.view_combo.currentData()
        self.view_combo.blockSignals(True)
        self.view_combo.clear()
        for no in self._checked_targets():
            self.view_combo.addItem(_drawing_label(self.ledger.drawing(no)), no)
        idx = self.view_combo.findData(keep)
        self.view_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.view_combo.blockSignals(False)
        self._show_selected_drawing()

    def _show_selected_drawing(self):
        no = self.view_combo.currentData()
        self._view_version = None
        if not no:
            self.view.setDocument(None)
            self.view_note.setText("対象図面を選ぶと、ここに図面を表示します。")
            return
        v = self.ledger.latest_version(no)
        doc = self.docs.get(self.ledger.abs_path(v["pdf"])) if v else None
        if doc is None:
            self.view.setDocument(None)
            self.view_note.setText("この図面のPDFが登録されていないため、箇所は指定できません。")
            return
        self.view.setDocument(doc)
        page = int(v["ページ"]) - 1
        self.view.pageNavigator().jump(page, QPointF(0, 0))
        self._view_version = v
        self.view_note.setText(f"［{v['版区分']}］ {v['pdf']} p{page + 1}　"
                               "「範囲を指定」をオンにして、変更する箇所をドラッグで囲んでください。")
        self._show_place_marks()

    # ------------------------------------------------------------ 図面上の箇所
    def _fill_places(self):
        self.e_places.blockSignals(True)
        self.e_places.clear()
        for i, p in enumerate(self.places):
            it = QTreeWidgetItem([p.get("図面番号", ""), p.get("版区分", ""), p.get("コメント", "")])
            it.setData(0, ITEM_ROLE, i)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsEditable)
            self.e_places.addTopLevelItem(it)
        self.e_places.blockSignals(False)

    def _on_place_edited(self, item: QTreeWidgetItem, column: int):
        if column == 2:
            self.places[item.data(0, ITEM_ROLE)]["コメント"] = item.text(2)
        else:  # 番号・版は編集させない
            self._fill_places()

    def _delete_place(self):
        item = self.e_places.currentItem()
        if item is not None:
            del self.places[item.data(0, ITEM_ROLE)]
            self._fill_places()
            self._show_place_marks()

    def _on_area_selected(self, page: int, rect: QRectF):
        no = self.view_combo.currentData()
        v = getattr(self, "_view_version", None)
        if not no or v is None:
            return
        self.places.append({"図面番号": no, "版区分": v["版区分"], "page": page + 1,
                            "rect": [round(rect.left(), 1), round(rect.top(), 1),
                                     round(rect.right(), 1), round(rect.bottom(), 1)],
                            "コメント": ""})
        self._fill_places()
        item = self.e_places.topLevelItem(len(self.places) - 1)
        self.e_places.setCurrentItem(item)
        self.e_places.editItem(item, 2)
        self._show_place_marks()

    def _show_place_marks(self):
        no = self.view_combo.currentData()
        v = getattr(self, "_view_version", None)
        if not no or v is None:
            self.view.set_marks([])
            return
        cur = self.e_places.currentItem()
        cur_i = cur.data(0, ITEM_ROLE) if cur is not None else None
        page = int(v["ページ"]) - 1
        marks = []
        for i, p in enumerate(self.places):
            if p.get("図面番号") == no and p.get("rect"):
                same = p.get("版区分", v["版区分"]) == v["版区分"]
                marks.append((page, _qrect(p["rect"]), ("strong" if i == cur_i else "normal") if same else "old"))
        self.view.set_marks(marks)

    # ------------------------------------------------------------ 保存
    def values(self) -> dict:
        """入力内容を、変更記録の項目の辞書にして返す。"""
        targets = self._checked_targets()
        out = dict(self.rec)
        out.update({
            "記録日": self.e_record_day.text().strip(),
            "指示日": self.e_day.text().strip(),
            "きっかけ": {"種別": self.e_trig_kind.currentText().strip(), "番号": self.e_trig_no.text().strip()},
            "内容": self.e_content.toPlainText().strip(),
            "理由": self.e_reason.toPlainText().strip(),
            "決定者": self.e_decider.text().strip(),
            "指示方法": self.e_method.currentText().strip(),
            "対象図面": targets,
            "図面上の箇所": [p for p in self.places if p.get("図面番号") in targets],
            "状態": self.e_status.currentText(),
        })
        return out

    def _accept(self):
        errors = Ledger.validate_record(self.values())
        if errors:
            QMessageBox.warning(self, "入力内容を確認してください", "\n".join(errors))
            return
        self.accept()


# ================================================================ 新しい工事台帳
class NewLedgerDialog(QDialog):
    """工事台帳の新規作成（工事情報の入力と、発注図PDFの登録）。"""

    def __init__(self, folder: str, parent=None):
        super().__init__(parent)
        self.folder = folder
        self.setWindowTitle("工事台帳を新しく作る")
        _fit_to_screen(self, 640, 480)
        lay = QVBoxLayout(self)
        note = QLabel(f"工事フォルダ: {folder}\n発注図PDFの目次から図面一覧を作り、各ページの表題欄の図面番号で「発注」の版として登録します。")
        note.setWordWrap(True)
        lay.addWidget(note)
        form = QFormLayout()
        self.edits = {}
        for k in KOJI_FIELDS:
            e = QLineEdit()
            if k == "工事名":
                e.setPlaceholderText("空欄なら図面の表題欄から読み取ります")
            elif k in ("契約日",):
                e.setPlaceholderText("2024-04-01")
            form.addRow(k, e)
            self.edits[k] = e
        self.layout_combo = QComboBox()
        for la in list_layouts():
            self.layout_combo.addItem(la.name, la.key)
        form.addRow("図面の様式", self.layout_combo)
        pdf_row = QHBoxLayout()
        self.pdf_edit = QLineEdit()
        self.pdf_edit.setReadOnly(True)
        pdf_row.addWidget(self.pdf_edit, 1)
        pick = QPushButton("選ぶ...")
        pick.clicked.connect(self._pick_pdf)
        pdf_row.addWidget(pick)
        form.addRow("発注図PDF *", pdf_row)
        self.day_edit = QLineEdit()
        self.day_edit.setPlaceholderText("発注図の日付（例 2023-04-05）")
        form.addRow("発注図の日付", self.day_edit)
        lay.addLayout(form)
        lay.addStretch(1)
        bb = QDialogButtonBox()
        bb.addButton("作成", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton("キャンセル", QDialogButtonBox.ButtonRole.RejectRole)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.ledger: Ledger | None = None

    def _pick_pdf(self):
        path, _ = QFileDialog.getOpenFileName(self, "発注図PDFを選ぶ", self.folder, "PDF Files (*.pdf)")
        if path:
            self.pdf_edit.setText(path)

    def _accept(self):
        src = self.pdf_edit.text()
        if not src:
            QMessageBox.warning(self, "発注図PDF", "発注図PDFを選んでください。")
            return
        key = self.layout_combo.currentData()
        if not key:
            QMessageBox.warning(self, "様式", "layouts フォルダに様式設定がありません。")
            return
        try:
            pdf = _ensure_inside(self, self.folder, src, "発注図")
            if pdf is None:
                return
            koji = {k: e.text().strip() for k, e in self.edits.items()}
            self.ledger = build_from_order_set(self.folder, pdf, load_layout(key), koji,
                                               day=self.day_edit.text().strip())
            self.ledger.save()
        except (OSError, LedgerError, ValueError) as e:
            QMessageBox.warning(self, "作成できませんでした", str(e))
            return
        self.accept()


def _ensure_inside(parent, folder: str, src: str, subdir: str) -> str | None:
    """PDFが工事フォルダの外にあれば、フォルダ内（subdir）へコピーしてよいか尋ねてコピーする。"""
    folder = os.path.abspath(folder)
    src = os.path.abspath(src)
    try:
        inside = not os.path.relpath(src, folder).startswith("..")
    except ValueError:
        inside = False
    if inside:
        return src
    dest = os.path.join(folder, subdir, os.path.basename(src))
    ans = QMessageBox.question(parent, "PDFを工事フォルダにコピー",
                               f"PDFは工事フォルダの中に置く決まりです。\n次の場所にコピーして登録しますか？\n\n{dest}")
    if ans != QMessageBox.StandardButton.Yes:
        return None
    if os.path.exists(dest):
        QMessageBox.warning(parent, "コピーできません", f"同じ名前のファイルが既にあります。\n{dest}")
        return None
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(src, dest)
    return dest


# ================================================================ 変更図面の登録
class RegisterVersionDialog(QDialog):
    """変更図面PDFを新しい版として登録する。PDFの各ページを読み取ってチェックし、
    反映した変更記録を選んで「変更契約に反映済」にする。"""

    def __init__(self, ledger: Ledger, parent=None):
        super().__init__(parent)
        self.ledger = ledger
        self.check_result: CheckResult | None = None
        self.src = ""
        self.setWindowTitle("変更図面を登録")
        _fit_to_screen(self, 980, 720)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        pdf_row = QHBoxLayout()
        self.pdf_edit = QLineEdit()
        self.pdf_edit.setReadOnly(True)
        pdf_row.addWidget(self.pdf_edit, 1)
        pick = QPushButton("PDFを選ぶ...")
        pick.clicked.connect(self._pick)
        pdf_row.addWidget(pick)
        form.addRow("変更図面PDF", pdf_row)
        self.kind = QComboBox()
        self.kind.setEditable(True)
        self.kind.addItem(ledger.next_contract_kind())
        self.kind.setToolTip("版の名前（例: 変更契約1）。同じ名前の版があれば置き換えます")
        form.addRow("版区分", self.kind)
        self.day = QLineEdit(today_text())
        form.addRow("日付", self.day)
        lay.addLayout(form)

        lay.addWidget(QLabel("PDFの各ページ（表題欄の読み取り結果と、台帳の図面一覧との照合）:"))
        self.pages = _make_tree(["ページ", "図面番号", "図面名称", "台帳", "チェック"])
        for c, w in enumerate((56, 70, 200, 90)):
            self.pages.setColumnWidth(c, w)
        self.pages.setRootIsDecorated(False)
        lay.addWidget(self.pages, 2)
        self.check_label = QLabel()
        self.check_label.setWordWrap(True)
        lay.addWidget(self.check_label)
        save_html = _small_button("チェック結果をHTMLで保存")
        save_html.clicked.connect(self._save_check_html)
        lay.addWidget(save_html, 0, Qt.AlignmentFlag.AlignLeft)

        lay.addWidget(QLabel("この変更図面に反映した変更記録（チェックしたものを「変更契約に反映済」にします）:"))
        self.records = QListWidget()
        lay.addWidget(self.records, 2)
        bb = QDialogButtonBox()
        self.ok_button = bb.addButton("登録", QDialogButtonBox.ButtonRole.AcceptRole)
        self.ok_button.setEnabled(False)
        bb.addButton("キャンセル", QDialogButtonBox.ButtonRole.RejectRole)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _pick(self):
        path, _ = QFileDialog.getOpenFileName(self, "変更図面のPDFを選ぶ", self.ledger.folder, "PDF Files (*.pdf)")
        if path:
            self.load_pdf(path)

    def load_pdf(self, path: str):
        """PDFを読み取り、台帳の図面一覧と照合してチェックする。"""
        key = self.ledger.layout_key
        if key not in [la.key for la in list_layouts()]:
            # 台帳に記録された様式が見つからない（設定ファイルの名前が変わった等）ときは選び直してもらう
            key = choose_layout(self)
            if key is None:
                return
            self.ledger.data["様式"] = key
        try:
            layout = load_layout(key)
            self.check_result = run_checks(path, layout, index=_ledger_index(self.ledger))
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "PDFを読み取れませんでした", str(e))
            return
        self.src = path
        self.pdf_edit.setText(path)
        self.pages.clear()
        for s in self.check_result.sheets:
            known = self.ledger.drawing(s.drawing_no) is not None
            state = "登録済の図面" if known else ("新しい図面" if s.drawing_no else "番号なし")
            judge = " / ".join(f.message for f in s.findings if f.rule_id != "NO_NOT_IN_INDEX") or "OK"
            item = QTreeWidgetItem([f"p{s.page}", s.drawing_no, s.name, state, judge])
            item.setToolTip(4, judge)
            if judge != "OK" or not s.drawing_no:
                for c in range(5):
                    item.setBackground(c, C_WARN_BG)
            self.pages.addTopLevelItem(item)
        n = sum(1 for f in self.check_result.findings if f.level == LEVEL_CHECK and f.rule_id != "NO_NOT_IN_INDEX")
        self.check_label.setText(f"発注前チェックと同じ点検の結果: 要確認 {n} 件"
                                 "（台帳にない図面番号は「新しい図面」として追加します）")
        nos = {s.drawing_no for s in self.check_result.sheets if s.drawing_no}
        self.records.clear()
        for r in self.ledger.records:
            if r.get("状態") not in (STATUS_INSTRUCTED, STATUS_DISCUSSING):
                continue
            it = QListWidgetItem(f"{r['id']}［{r.get('状態')}］No.{'・'.join(r.get('対象図面', []))}　{_record_summary(r, 60)}")
            it.setData(ITEM_ROLE, r["id"])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            hit = r.get("状態") == STATUS_INSTRUCTED and bool(nos & set(r.get("対象図面", [])))
            it.setCheckState(Qt.CheckState.Checked if hit else Qt.CheckState.Unchecked)
            self.records.addItem(it)
        self.ok_button.setEnabled(bool(nos))

    def _save_check_html(self):
        if self.check_result is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "HTMLで保存", os.path.join(self.ledger.folder, "変更図面_チェック結果.html"),
                                              "HTML (*.html)")
        if path:
            report.write_check_html(self.check_result, path)

    def selected_record_ids(self) -> list[str]:
        return [self.records.item(i).data(ITEM_ROLE) for i in range(self.records.count())
                if self.records.item(i).checkState() == Qt.CheckState.Checked]

    def _accept(self):
        kind = self.kind.currentText().strip()
        if not kind:
            QMessageBox.warning(self, "版区分", "版区分を入力してください。")
            return
        if kind in self.ledger.version_kinds():
            ans = QMessageBox.question(self, "版区分", f"「{kind}」の版は既にあります。同じ図面の{kind}を置き換えますか？")
            if ans != QMessageBox.StandardButton.Yes:
                return
        pdf = _ensure_inside(self, self.ledger.folder, self.src, kind)
        if pdf is None:
            return
        self._pdf = pdf
        self.accept()

    def apply(self) -> tuple[int, int]:
        """台帳に登録する（登録したページ数・反映済にした記録数を返す）。"""
        page_map: dict[str, int] = {}
        names: dict[str, str] = {}
        for s in self.check_result.sheets:
            if s.drawing_no and s.drawing_no not in page_map:
                page_map[s.drawing_no] = s.page
                names[s.drawing_no] = s.name
        ids = self.selected_record_ids()
        self.ledger.register_contract_version(self.kind.currentText().strip(), self.day.text().strip(),
                                              self.ledger.rel_path(self._pdf), page_map, ids, names=names)
        return len(page_map), len(ids)


# ================================================================ 新旧対照
class CompareWindow(QWidget):
    """同じ図面の前の版と新しい版を左右に並べて表示する別ウインドウ。"""

    def __init__(self, title: str, old: tuple[str, str, int], new: tuple[str, str, int],
                 marks_new: list[QRectF] | None = None, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(f"新旧対照: {title}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        self.resize(int(avail.width() * 0.95) if avail else 1600, int(avail.height() * 0.85) if avail else 900)
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addWidget(QLabel(f"<b>{title}</b>"))
        bar.addStretch(1)
        self.sync = QCheckBox("スクロールを連動")
        self.sync.setChecked(True)
        bar.addWidget(self.sync)
        zoom = QComboBox()
        zoom.addItem("ページ全体", QPdfView.ZoomMode.FitInView)
        zoom.addItem("幅に合わせる", QPdfView.ZoomMode.FitToWidth)
        zoom.addItem("拡大（2倍）", QPdfView.ZoomMode.Custom)
        bar.addWidget(zoom)
        lay.addLayout(bar)
        note = QLabel("差分の自動検出はありません。左右を見比べて確認してください。"
                      "赤枠はこの版に反映した変更記録の箇所です。")
        note.setObjectName("settingsNote")
        lay.addWidget(note)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.docs: list[QPdfDocument] = []
        self.views: list[HighlightPdfView] = []
        for label, path, page in (old, new):
            box = QWidget()
            b_l = QVBoxLayout(box)
            b_l.setContentsMargins(0, 0, 0, 0)
            b_l.addWidget(QLabel(f"［{label}］ {os.path.basename(path)} p{page}"))
            doc = QPdfDocument(self)
            doc.load(path)
            view = HighlightPdfView(path)
            view.setDocument(doc)
            view.setPageMode(QPdfView.PageMode.SinglePage)
            view.setZoomMode(QPdfView.ZoomMode.FitInView)
            app._style_pdf_view(view)
            view.pageNavigator().jump(page - 1, QPointF(0, 0))
            b_l.addWidget(view, 1)
            split.addWidget(box)
            self.docs.append(doc)
            self.views.append(view)
        if marks_new:
            self.views[1].set_marks([(new[2] - 1, r, "strong") for r in marks_new if r is not None])
        lay.addWidget(split, 1)
        zoom.currentIndexChanged.connect(lambda _i: self._set_zoom(zoom.currentData()))
        a, b = self.views
        for src, dst in ((a, b), (b, a)):
            src.verticalScrollBar().valueChanged.connect(
                lambda v, d=dst: d.verticalScrollBar().setValue(v) if self.sync.isChecked() else None)
            src.horizontalScrollBar().valueChanged.connect(
                lambda v, d=dst: d.horizontalScrollBar().setValue(v) if self.sync.isChecked() else None)

    def _set_zoom(self, mode):
        for v in self.views:
            v.setZoomMode(mode)
            if mode == QPdfView.ZoomMode.Custom:
                v.setZoomFactor(2.0)

    def closeEvent(self, event):
        for v in self.views:
            v.setDocument(None)
        for d in self.docs:
            d.close()
        super().closeEvent(event)


# ================================================================ 工事フォルダを開く
def open_ledger_folder(window, folder: str | None = None) -> LedgerTab | None:
    """工事フォルダの工事台帳をタブで開く。台帳がなければ新しく作るか尋ねる。"""
    if not folder:
        folder = QFileDialog.getExistingDirectory(window, "工事フォルダを選ぶ")
        if not folder:
            return None
    folder = os.path.abspath(folder)
    tabs = window.tabs
    for i in range(tabs.count()):
        w = tabs.widget(i)
        if isinstance(w, LedgerTab) and os.path.abspath(w.folder) == folder:
            tabs.setCurrentIndex(i)
            return w
    if Ledger.exists(folder):
        try:
            ledger = Ledger.load(folder)
        except LedgerError as e:
            QMessageBox.warning(window, "工事台帳を読み込めませんでした", str(e))
            return None
    else:
        ans = QMessageBox.question(window, "工事台帳がありません",
                                   f"このフォルダには工事台帳（工事台帳.json）がありません。\n新しく作りますか？\n\n{folder}")
        if ans != QMessageBox.StandardButton.Yes:
            return None
        dlg = NewLedgerDialog(folder, window)
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.ledger is None:
            return None
        ledger = dlg.ledger
    return _insert_ledger_tab(window, ledger)


def _insert_ledger_tab(window, ledger: Ledger) -> LedgerTab:
    tab = LedgerTab(ledger)
    index = window._plus_tab_index()
    window.tabs.insertTab(index, tab, tab.title)
    window.tabs.setTabToolTip(index, ledger.path)
    window._install_close_button(tab)
    window.tabs.setCurrentWidget(tab)
    _close_welcome(window)
    window.status.showMessage(f"工事台帳を開きました（図面 {len(ledger.drawings)} 枚・変更記録 {len(ledger.records)} 件）", 6000)
    return tab
