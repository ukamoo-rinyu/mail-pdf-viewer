"""一般資料PDF(しおり階層閲覧)モードの自動判定・ツリー表示・検索・ページ追従を検証する。"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

from PySide6.QtCore import QPointF, QTimer
from PySide6.QtWidgets import QApplication

from main import DocumentPdfTab, MainWindow, PdfTab

app = QApplication(sys.argv)
win = MainWindow()
win.show()

results = []


def check(label, actual, expected):
    ok = actual == expected
    results.append(ok)
    print(f"{label}: {actual!r} (期待値: {expected!r}) -> {'OK' if ok else 'NG'}")


def dump_tree(model, parent=None, indent=0):
    from PySide6.QtCore import QModelIndex
    parent = parent if parent is not None else QModelIndex()
    for row in range(model.rowCount(parent)):
        idx = model.index(row, 0, parent)
        section = model.section_at(idx)
        print("  " * indent + f"- {section.title} ({section.start_page}-{section.end_page})")
        dump_tree(model, idx, indent + 1)


def run_checks():
    win.load_pdf("../一般資料サンプル.pdf")
    tab = win.tabs.currentWidget()
    check("資料PDFはDocumentPdfTabとして開かれる", isinstance(tab, DocumentPdfTab), True)
    check("メールPDFタブ用のPdfTabではない", isinstance(tab, PdfTab), False)

    print("\n=== ツリー全件 ===")
    dump_tree(tab.model)
    check("読み込み件数(全しおり数)", tab.result_count(), 6)

    print("\n=== 検索: '構造' ===")
    win.search_box.setText("構造")
    dump_tree(tab.model)
    check("検索結果件数", tab.result_count(), 1)

    win.search_box.setText("")
    check("検索クリア後の件数", tab.result_count(), 6)

    # 一覧クリックでページジャンプ
    idx = tab.model.index(0, 0)  # 第1章 はじめに (1-3ページ)
    tab.list_view.setCurrentIndex(idx)
    QApplication.processEvents()
    check("先頭しおりクリックでページ1へ", tab.pdf_view.pageNavigator().currentPage(), 0)

    child_idx = tab.model.index(1, 0, idx)  # 1.2 目的 (3ページ)
    tab.list_view.setCurrentIndex(child_idx)
    QApplication.processEvents()
    check("子しおり(1.2 目的)クリックでページ3へ", tab.pdf_view.pageNavigator().currentPage(), 2)

    # PDF側で付録Aのページ(6, 0始まりで5)へジャンプ -> 一覧が追従するか
    nav = tab.pdf_view.pageNavigator()
    nav.jump(5, QPointF(0, 0))
    QApplication.processEvents()
    selected = tab.model.section_at(tab.list_view.currentIndex())
    check("PDF側ジャンプ後、一覧が付録Aに追従", selected.title if selected else None, "付録A 見積書")

    print("\n総合結果:", "OK" if all(results) else "NG")
    app.quit()


QTimer.singleShot(500, run_checks)
app.exec()
