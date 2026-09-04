import sys
sys.stdout.reconfigure(encoding="utf-8")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem

from main import MainWindow

app = QApplication(sys.argv)
win = MainWindow()
win.show()
win.load_pdf("../one_pdf_with_bookmarks3.pdf")
tab = win.tabs.currentWidget()


def run_checks():
    idx = tab.model.index(1, 0)
    tab.list_view.setCurrentIndex(idx)
    QApplication.processEvents()
    mail = tab.model.mail_at(1)
    print("対象メール:", mail.subject)
    for a in mail.attachment_list():
        print("  -", a.name, "page=", a.start_page)

    rect = tab.list_view.visualRect(idx)
    option = QStyleOptionViewItem()
    option.rect = rect
    chips, _ = tab.item_delegate._attachment_chips(rect, mail, tab.item_delegate._row_positions(rect)[3])
    print("チップ数:", len(chips))
    for c in chips:
        print("  chip:", c["label"], "clickable=", c["clickable"], "rect=", c["rect"])

    print("\n初期zoomMode:", tab.pdf_view.zoomMode())
    win.zoom_combo.setCurrentIndex(win.zoom_combo.findText("ページ全体"))
    QApplication.processEvents()
    print("変更後zoomMode:", tab.pdf_view.zoomMode(), "(期待値: ZoomMode.FitInView)")

    app.quit()


QTimer.singleShot(800, run_checks)
app.exec()
