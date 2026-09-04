import sys
sys.stdout.reconfigure(encoding="utf-8")

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from main import MainWindow

app = QApplication(sys.argv)
win = MainWindow()
win.show()
win.load_pdf("../one_pdf_with_bookmarks3.pdf")
tab = win.tabs.currentWidget()


def run_checks():
    print("初期選択行:", tab.list_view.currentIndex().row(),
          "| page:", tab.pdf_view.pageNavigator().currentPage())

    tab.list_view.setFocus()
    ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(tab.list_view, ev)
    QApplication.processEvents()
    print("Down後 選択行:", tab.list_view.currentIndex().row(),
          "| page:", tab.pdf_view.pageNavigator().currentPage(),
          "(期待: 行1 = ご返信ください, page=1)")

    ev2 = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(tab.list_view, ev2)
    QApplication.processEvents()
    print("Down後 選択行:", tab.list_view.currentIndex().row(),
          "| page:", tab.pdf_view.pageNavigator().currentPage(),
          "(期待: 行2 = 時間割, page=5)")

    win.search_box.clearFocus()
    from PySide6.QtGui import QShortcut
    shortcuts = win.findChildren(QShortcut)
    for sc in shortcuts:
        if sc.key().toString() == "Ctrl+F":
            sc.activated.emit()
            break
    print("Ctrl+F後、検索ボックスにフォーカス:", win.search_box.hasFocus())

    app.quit()


QTimer.singleShot(800, run_checks)
app.exec()
