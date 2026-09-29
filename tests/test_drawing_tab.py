"""図面モードの画面（drawing_tab.py）を、画面を出さずに操作して確かめる。"""
import os
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
from PySide6.QtCore import QRectF, QSettings, Qt
from PySide6.QtWidgets import QApplication

import drawing_tab
import main
from drawings.layouts import load_layout
from drawings.ledger import STATUS_COMPLETED, STATUS_CONTRACTED, Ledger, build_from_order_set
from tests.drawing_samples import make_synthetic_set

KEY = "toshiseibi_A1"


def pump(app, n=20):
    for _ in range(n):
        app.processEvents()
        time.sleep(0.005)


def _set(path, bad_name=False):
    base = {"工事名": "テスト工事", "縮尺": "1:100", "製図年": "5", "製図月": "3", "総枚数": ""}
    make_synthetic_set(path, [dict(base, 図面名称="図面リスト", 図面番号="１"),
                              dict(base, 図面名称="平面図違い" if bad_name else "平面図", 図面番号="２")],
                       index_rows=[(0, "1", "図面リスト", False), (0, "2", "平面図", False),
                                   (0, "3", "立面図", False)])


class DrawingTabTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)

    def setUp(self):
        # 閉じたPDFのファイルハンドルがすぐには解放されないことがあるため、後片付けの失敗は無視する
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, self.tmp.name)
        self.win = main.MainWindow()
        self.win.resize(1400, 900)
        self.win.show()
        self.addCleanup(self._close)

    def _close(self):
        for i in reversed(range(self.win.tabs.count())):
            w = self.win.tabs.widget(i)
            if isinstance(w, main.BasePdfTab):
                w.document.close()
            elif isinstance(w, drawing_tab.LedgerTab):
                w.viewer.setDocument(None)
                w.docs.close_all()
        self.win.close()
        self.win.deleteLater()
        pump(self.app, 5)

    def test_drawing_set_tab(self):
        pdf = os.path.join(self.tmp.name, "set.pdf")
        _set(pdf, bad_name=True)
        tab = drawing_tab.open_drawing_set(self.win, pdf, KEY)
        pump(self.app)
        self.assertIsInstance(tab, drawing_tab.DrawingSetTab)
        self.assertIs(self.win.tabs.currentWidget(), tab)
        self.assertEqual(tab.result_count(), 2)
        # 名称不一致の指摘をクリック → そのページへ移動し、赤枠が付く
        p2 = next(tab.tree.topLevelItem(i) for i in range(tab.tree.topLevelItemCount())
                  if tab.tree.topLevelItem(i).text(0) == "p2")
        finding = next(p2.child(i) for i in range(p2.childCount()) if "名称" in p2.child(i).text(0))
        tab.tree.setCurrentItem(finding)
        pump(self.app, 40)
        self.assertEqual(tab.current_page(), 1)
        self.assertIn("strong", [m[2] for m in tab.pdf_view._marks])
        self.assertIs(tab.tree.currentItem(), finding)
        # 「要確認のみ」と絞り込み
        tab.only_check.setChecked(True)
        self.assertEqual(tab.result_count(), 2)  # 総枚数空欄で全ページ要確認
        tab.search_box.setText("平面図違い")
        self.assertEqual(tab.result_count(), 1)
        # 同じPDFをもう一度図面セットとして開いても、タブは1つ
        drawing_tab.open_drawing_set(self.win, pdf, KEY)
        pump(self.app)
        n = sum(1 for i in range(self.win.tabs.count())
                if isinstance(self.win.tabs.widget(i), main.BasePdfTab))
        self.assertEqual(n, 1)
        # 再インデックス（タブの右クリックメニューと同じ処理）でも動く
        self.win.load_pdf(pdf, force_rebuild=True)
        pump(self.app)
        self.assertIsInstance(self.win.tabs.currentWidget(), drawing_tab.DrawingSetTab)

    def test_existing_modes_still_open(self):
        """既存の一般資料PDFモードが従来どおり開ける。"""
        pdf = os.path.join(self.tmp.name, "doc.pdf")
        with pymupdf.open() as doc:
            for i in range(3):
                doc.new_page().insert_text((72, 72), f"page {i + 1}")
            doc.set_toc([[1, "第1章", 1], [1, "第2章", 3]])
            doc.save(pdf)
        self.win.load_pdf(pdf)
        pump(self.app)
        self.assertIsInstance(self.win.tabs.currentWidget(), main.DocumentPdfTab)
        # その後に図面セットとして開くと、同じタブ位置で置き換わる
        drawing_tab.open_drawing_set(self.win, pdf, KEY)
        pump(self.app)
        self.assertIsInstance(self.win.tabs.currentWidget(), drawing_tab.DrawingSetTab)
        self.assertFalse(any(isinstance(self.win.tabs.widget(i), main.DocumentPdfTab)
                             for i in range(self.win.tabs.count())))

    def test_ledger_tab_flow(self):
        folder = os.path.join(self.tmp.name, "工事")
        os.makedirs(os.path.join(folder, "発注図"))
        pdf = os.path.join(folder, "発注図", "一式.pdf")
        _set(pdf)
        build_from_order_set(folder, pdf, load_layout(KEY), {}, day="2023-04-05").save()
        tab = drawing_tab.open_ledger_folder(self.win, folder)
        pump(self.app)
        self.assertIsInstance(tab, drawing_tab.LedgerTab)
        self.assertEqual(tab.drawing_tree.topLevelItemCount(), 3)
        self.assertIs(drawing_tab.open_ledger_folder(self.win, folder), tab)  # 二重に開かない

        # 変更記録フォーム：図面上をドラッグしたのと同じ操作で箇所を登録
        dlg = drawing_tab.RecordDialog(tab.ledger, None, ["2"], tab.docs, tab)
        dlg.e_content.setPlainText("手すり高さ変更")
        dlg.e_day.setText("2024-06-10")
        dlg._on_area_selected(1, QRectF(100, 200, 50, 40))
        values = dlg.values()
        dlg.close()
        self.assertEqual(values["対象図面"], ["2"])
        self.assertEqual(values["図面上の箇所"][0]["rect"], [100, 200, 150, 240])
        self.assertEqual(values["図面上の箇所"][0]["版区分"], "発注")
        tab.ledger.add_record(values)
        tab._commit("登録")
        pump(self.app)
        self.assertEqual(Ledger.load(folder).records[0]["id"], "C-001")  # 保存されている

        # 図面を選ぶと表示し、記録を選ぶと赤枠
        item = next(tab.drawing_tree.topLevelItem(i) for i in range(tab.drawing_tree.topLevelItemCount())
                    if tab.drawing_tree.topLevelItem(i).text(0) == "2")
        tab.drawing_tree.setCurrentItem(item)
        pump(self.app)
        self.assertEqual(tab.drawing_records.topLevelItemCount(), 1)
        tab.drawing_records.setCurrentItem(tab.drawing_records.topLevelItem(0))
        pump(self.app)
        self.assertEqual(len(tab.viewer._marks), 1)

        # 変更図面の登録
        os.makedirs(os.path.join(folder, "変更1"))
        new_pdf = os.path.join(folder, "変更1", "変更.pdf")
        shutil.copy(pdf, new_pdf)
        rd = drawing_tab.RegisterVersionDialog(tab.ledger, tab)
        rd.load_pdf(new_pdf)
        self.assertEqual(rd.selected_record_ids(), ["C-001"])
        rd._pdf = new_pdf
        self.assertEqual(rd.apply(), (2, 1))
        rd.close()
        tab._commit("登録")
        self.assertEqual(tab.ledger.record("C-001")["状態"], STATUS_CONTRACTED)
        self.assertEqual(tab.ledger.latest_version("2")["版区分"], "変更契約1")

        # 竣工図チェックリストのチェック
        child = tab.checklist.topLevelItem(0).child(0)
        child.setCheckState(0, Qt.CheckState.Checked)
        pump(self.app)
        self.assertEqual(Ledger.load(folder).record("C-001")["状態"], STATUS_COMPLETED)

        # 新旧対照ウインドウ
        tab.drawing_tree.setCurrentItem(next(
            tab.drawing_tree.topLevelItem(i) for i in range(tab.drawing_tree.topLevelItemCount())
            if tab.drawing_tree.topLevelItem(i).text(0) == "2"))
        before = len(drawing_tab._open_windows)
        tab._compare_current()
        pump(self.app)
        self.assertEqual(len(drawing_tab._open_windows), before + 1)
        drawing_tab._open_windows[-1].close()
        pump(self.app)


if __name__ == "__main__":
    unittest.main()
