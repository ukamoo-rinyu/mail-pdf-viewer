"""実際のQtイベントパイプラインを通したマウスクリックの回帰テスト。

editorEvent()を直接呼ぶテストだけでは、QListViewの標準クリック処理(clicked信号)が
添付チップのジャンプ処理を上書きしてしまうバグを検出できなかったため、
QTest.mouseClickで本物のクリックを再現して確認する。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from main import MainWindow

app = QApplication(sys.argv)
win = MainWindow()
win.show()
win.load_pdf("../one_pdf_with_bookmarks3.pdf")
tab = win.tabs.currentWidget()

results = []


def check(label, actual, expected):
    ok = actual == expected
    results.append(ok)
    print(f"{label}: {actual} (期待値: {expected}) -> {'OK' if ok else 'NG'}")


def run_checks():
    idx1 = tab.model.index(1, 0)
    tab.list_view.setCurrentIndex(idx1)
    QApplication.processEvents()
    rect1 = tab.list_view.visualRect(idx1)
    mail1 = tab.model.mail_at(1)
    _, _, _, y4_1 = tab.item_delegate._row_positions(rect1)
    chips1, _ = tab.item_delegate._attachment_chips(rect1, mail1, y4_1)

    QTest.mouseClick(tab.list_view.viewport(), Qt.MouseButton.LeftButton, pos=chips1[0]["rect"].center())
    QApplication.processEvents(); QTest.qWait(50)
    check("1つ目チップクリック", tab.pdf_view.pageNavigator().currentPage(), 3)

    QTest.mouseClick(tab.list_view.viewport(), Qt.MouseButton.LeftButton, pos=chips1[1]["rect"].center())
    QApplication.processEvents(); QTest.qWait(50)
    check("2つ目チップクリック", tab.pdf_view.pageNavigator().currentPage(), 4)

    subject_pos = rect1.topLeft() + type(rect1.topLeft())(120, 45)
    QTest.mouseClick(tab.list_view.viewport(), Qt.MouseButton.LeftButton, pos=subject_pos)
    QApplication.processEvents(); QTest.qWait(50)
    check("カード本体クリック", tab.pdf_view.pageNavigator().currentPage(), 1)

    print("\n総合結果:", "OK" if all(results) else "NG")
    app.quit()


QTimer.singleShot(800, run_checks)
app.exec()
