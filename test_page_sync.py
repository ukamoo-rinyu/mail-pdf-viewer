"""PDFのページ移動(スクロール相当)と一覧選択の同期、ツールバーのページ送りボタンを検証する。"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

from PySide6.QtCore import QPointF, QTimer
from PySide6.QtWidgets import QApplication

from main import MainWindow

app = QApplication(sys.argv)
win = MainWindow()
win.show()

results = []


def check(label, actual, expected):
    ok = actual == expected
    results.append(ok)
    print(f"{label}: {actual!r} (期待値: {expected!r}) -> {'OK' if ok else 'NG'}")


def run_checks():
    check("タブなし時のページ表示", win.page_label.text(), "- / -")
    check("タブなし時は前後ボタン無効", win.prev_page_button.isEnabled(), False)

    win.load_pdf("../one_pdf_with_bookmarks5.pdf")  # 8通・23ページ
    tab = win.tabs.currentWidget()
    nav = tab.pdf_view.pageNavigator()

    check("読み込み後のページ表示", win.page_label.text(), "8 / 23")
    check("読み込み後、前へボタンは有効", win.prev_page_button.isEnabled(), True)

    # ツールバーの「次へ」ボタン
    win.go_next_page()
    QApplication.processEvents()
    check("次へ後のページ表示", win.page_label.text(), "9 / 23")

    win.go_prev_page()
    QApplication.processEvents()
    check("前へ後のページ表示", win.page_label.text(), "8 / 23")

    # PDF側でページ14へジャンプ(スクロール相当) -> 一覧が該当メールに追従するか
    nav.jump(13, QPointF(0, 0))
    QApplication.processEvents()
    check("ページ14へジャンプ後のツールバー表示", win.page_label.text(), "14 / 23")
    selected_mail = tab.model.mail_at(tab.list_view.currentIndex().row())
    check("一覧側が該当メールに追従", selected_mail.subject, "ご返信ください。（間取りの件） ※添付資料あります。")
    check("追従後もページ位置は14のまま(巻き戻りなし)", nav.currentPage(), 13)

    # 2つ目のPDFをタブで開き、ページ表示がそのタブのものに切り替わるか
    win.load_pdf("../one_pdf_with_bookmarks2.pdf")
    tab2 = win.tabs.currentWidget()
    check("2つ目のタブのページ表示", win.page_label.text(), f"{tab2.current_page() + 1} / {tab2.page_count()}")

    # 1つ目のタブに戻すとページ表示も1つ目の状態に戻るか
    win.tabs.setCurrentIndex(win.tabs.indexOf(tab))
    QApplication.processEvents()
    check("1つ目タブに戻すとページ表示も復元される", win.page_label.text(), "14 / 23")

    print("\n総合結果:", "OK" if all(results) else "NG")
    app.quit()


QTimer.singleShot(800, run_checks)
app.exec()
