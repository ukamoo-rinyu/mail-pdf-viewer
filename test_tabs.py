import sys
sys.stdout.reconfigure(encoding="utf-8")

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication

from main import MainWindow

app = QApplication(sys.argv)

settings = QSettings("ukawa", "MailPDFViewer")
_original_recent = settings.value("recentFiles", [], type=list)

win = MainWindow()
win.show()

results = []


def check(label, actual, expected):
    ok = actual == expected
    results.append(ok)
    print(f"{label}: {actual!r} (期待値: {expected!r}) -> {'OK' if ok else 'NG'}")


def run_checks():
    try:
        win.load_pdf("../one_pdf_with_bookmarks2.pdf")
        check("1つ目を開いた後のタブ数", win.tabs.count(), 1)

        win.load_pdf("../one_pdf_with_bookmarks3.pdf")
        check("2つ目を開いた後のタブ数", win.tabs.count(), 2)
        check("2つ目のタブがアクティブ", win.tabs.currentIndex(), 1)

        # 1つ目のタブで検索語を入れてから2つ目に切り替え、検索ボックスが2つ目の状態(空)になるか
        win.tabs.setCurrentIndex(0)
        win.search_box.setText("間取り")
        win.tabs.setCurrentIndex(1)
        check("2つ目タブに切替後、検索ボックスが2つ目の状態(空)", win.search_box.text(), "")

        win.tabs.setCurrentIndex(0)
        check("1つ目タブに戻すと検索語が復元される", win.search_box.text(), "間取り")
        win.search_box.setText("")

        # 既に開いているPDFを再度開くと新規タブを作らず既存タブに切り替わる
        win.tabs.setCurrentIndex(1)
        win.load_pdf("../one_pdf_with_bookmarks2.pdf")
        check("既存PDFを再オープンしてもタブ数は増えない", win.tabs.count(), 2)
        check("既存タブに切り替わる", win.tabs.currentIndex(), 0)

        # 最近使ったPDFに反映されているか
        recent = win._recent_files()
        check("最近使ったPDFに2件登録", len(recent) >= 2, True)

        # タブを閉じる
        win.tabs.setCurrentIndex(1)
        win._close_tab(1)
        check("タブを閉じた後のタブ数", win.tabs.count(), 1)

        print("\n総合結果:", "OK" if all(results) else "NG")
    finally:
        settings.setValue("recentFiles", _original_recent)
        app.quit()


QTimer.singleShot(800, run_checks)
app.exec()
