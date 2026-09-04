import sys
sys.stdout.reconfigure(encoding="utf-8")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from main import MainWindow

app = QApplication(sys.argv)
win = MainWindow()
win.load_pdf("../one_pdf_with_bookmarks2.pdf")
tab = win.tabs.currentWidget()

print("=== 読み込み直後(新しい順) ===")
print("表示件数:", tab.model.rowCount())
for r in tab.model._rows:
    print(" -", r.sender_short, "|", r.subject, "|", r.title_datetime, "|", repr(r.preview[:20]))

print("\n=== 自動選択で先頭メールへジャンプしているか ===")
print("currentIndex row:", tab.list_view.currentIndex().row())

print("\n=== 検索: '間取り' (グローバル検索ボックス経由) ===")
win.search_box.setText("間取り")
print("表示件数:", tab.model.rowCount())
for r in tab.model._rows:
    print(" -", r.subject)
print("PDF内検索文字列:", tab.search_model.searchString())

print("\n=== 並び替え: 差出人昇順 ===")
win.search_box.setText("")
tab.sort_combo.setCurrentIndex(tab.sort_combo.findData("sender"))
tab.sort_dir_button.setChecked(False)
for r in tab.model._rows:
    print(" -", r.sender_short, r.subject)
print("並び替えボタン表示:", tab.sort_dir_button.text())

print("\n=== 並び替えを日時・新しい順に戻し、2件目をクリック相当でジャンプ ===")
tab.sort_combo.setCurrentIndex(tab.sort_combo.findData("date"))
tab.sort_dir_button.setChecked(True)
print("並び替えボタン表示:", tab.sort_dir_button.text())
idx = tab.model.index(1, 0)
mail = tab.model.mail_at(1)
print("対象メール:", mail.subject, "start_page=", mail.start_page)
tab.list_view.setCurrentIndex(idx)


def check_jump():
    nav = tab.pdf_view.pageNavigator()
    print("pageNavigator.currentPage() =", nav.currentPage(), "(期待値: start_page-1 =", mail.start_page - 1, ")")
    app.quit()


QTimer.singleShot(500, check_jump)
app.exec()
