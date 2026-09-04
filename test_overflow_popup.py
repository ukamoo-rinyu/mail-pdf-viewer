"""「+N」チップをクリックしてポップアップメニューを開き、項目選択でジャンプするかを検証する。
QMenu.exec()はネストしたイベントループでブロックするため、開いた直後にQTimerで
メニューを探して先頭項目をクリックする。
"""
import json
import sys
sys.stdout.reconfigure(encoding="utf-8")

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu

from db import AttachmentInfo, MailRow
from main import MainWindow

app = QApplication(sys.argv)
win = MainWindow()
win.show()
win.load_pdf("../one_pdf_with_bookmarks3.pdf")
tab = win.tabs.currentWidget()

attachments = [AttachmentInfo(name=f"添付資料サンプル{i:02d}.pdf", start_page=30 + i) for i in range(6)]
fake = MailRow(
    id=99, subject="添付が多いテストメール", sender="テスト送信者 <test@example.com>",
    sender_short="テスト送信者", to_addr="recipient@example.com", cc="",
    attachments=" / ".join(a.name for a in attachments),
    attachments_json=json.dumps([{"name": a.name, "start_page": a.start_page} for a in attachments]),
    received_at="Mon, 01 Jan 2027 00:00:00 +0900", sent_at="", title_datetime="2027-01-01T00:00:00",
    start_page=30, end_page=30, preview="",
)


def click_first_menu_action():
    menu = next((w for w in app.topLevelWidgets() if isinstance(w, QMenu) and w.isVisible()), None)
    if menu is None:
        print("メニューが見つからない -> NG")
        return
    actions = menu.actions()
    print("メニュー項目数:", len(actions), "先頭:", actions[0].text())
    rect = menu.actionGeometry(actions[0])
    QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=rect.center())


def run_checks():
    rows = [fake] + list(tab.model._rows)
    tab.model.set_rows(rows)
    QApplication.processEvents()

    idx = tab.model.index(0, 0)
    tab.list_view.setCurrentIndex(idx)
    QApplication.processEvents()
    rect = tab.list_view.visualRect(idx)
    _, _, _, y4 = tab.item_delegate._row_positions(rect)
    chips, _ = tab.item_delegate._attachment_chips(rect, fake, y4)
    overflow_chip = next(c for c in chips if c.get("kind") == "overflow")
    print("overflowチップ:", overflow_chip["label"], "残り:", len(overflow_chip["remaining"]))

    # QMenu.exec()はブロックするので、開いた直後にクリックするようタイマーを仕込んでからクリックする
    QTimer.singleShot(150, click_first_menu_action)
    QTest.mouseClick(tab.list_view.viewport(), Qt.MouseButton.LeftButton, pos=overflow_chip["rect"].center())
    QApplication.processEvents()
    QTest.qWait(100)

    page = tab.pdf_view.pageNavigator().currentPage()
    expected = fake.attachment_list()[2].start_page - 1  # remaining先頭 = 3件目の添付
    print("ジャンプ後ページ:", page, "(期待値:", expected, ")")
    print("結果:", "OK" if page == expected else "NG")

    app.quit()


QTimer.singleShot(800, run_checks)
app.exec()
