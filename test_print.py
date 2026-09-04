"""印刷機能(ページ範囲のレンダリング)を、実プリンタ/ダイアログなしで検証する。
QPrinterの出力先をPDFファイルにして、生成されたページ数・見た目を確認する。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

from PySide6.QtCore import QTimer
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import QApplication

import fitz  # PyMuPDF: 出力結果の検証用
from main import MainWindow

app = QApplication(sys.argv)
win = MainWindow()
win.load_pdf("../one_pdf_with_bookmarks3.pdf")
tab = win.tabs.currentWidget()

results = []


def check(label, actual, expected):
    ok = actual == expected
    results.append(ok)
    print(f"{label}: {actual!r} (期待値: {expected!r}) -> {'OK' if ok else 'NG'}")


def run_checks():
    # メール2件目(ご返信ください、start_page=2, end_page=5)の全4ページを印刷
    mail = tab.model.mail_at(1)
    print("対象メール:", mail.subject, "range=", mail.start_page, "-", mail.end_page)

    out_path = "../print_test_mail.pdf"
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(out_path)
    tab.render_page_range_to_printer(printer, mail.start_page, mail.end_page)

    out_doc = fitz.open(out_path)
    check("出力PDFのページ数(メール全体)", out_doc.page_count, mail.end_page - mail.start_page + 1)
    out_doc.close()

    # 添付1件だけ(01_入口の間取り.jpg, start=4, end=4)を印刷 -> 1ページのみ
    att = mail.attachment_list()[0]
    print("対象添付:", att.name, "range=", att.start_page, "-", att.end_page)
    out_path2 = "../print_test_attachment.pdf"
    printer2 = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer2.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer2.setOutputFileName(out_path2)
    tab.render_page_range_to_printer(printer2, att.start_page, att.end_page or att.start_page)

    out_doc2 = fitz.open(out_path2)
    check("出力PDFのページ数(添付のみ)", out_doc2.page_count, 1)
    out_doc2.close()

    print("\n総合結果:", "OK" if all(results) else "NG")
    app.quit()


QTimer.singleShot(800, run_checks)
app.exec()
