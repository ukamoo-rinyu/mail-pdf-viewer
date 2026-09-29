"""図面PDFからの読み取り（drawings.extract / layouts）のテスト。

実図面PDF（tests/data/実図面.pdf）がない環境では、実図面を使うテストはスキップする。
"""
import os
import tempfile
import unittest

import pymupdf

from drawings.extract import norm, read_index, read_references, read_title_block
from drawings.layouts import list_layouts, load_layout
from tests.drawing_samples import LAYOUT_PATH, REAL_PDF, make_scaled_copy, make_synthetic_set

HAS_REAL = os.path.isfile(REAL_PDF)


class NormTest(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(norm("２～１３階　平面図"), "2~13階平面図")
        self.assertEqual(norm(" 3 0 1 "), "301")
        self.assertEqual(norm(""), "")


class LayoutTest(unittest.TestCase):
    def test_load_by_key_and_path(self):
        by_path = load_layout(LAYOUT_PATH)
        by_key = load_layout("toshiseibi_A1")
        self.assertEqual(by_path.key, "toshiseibi_A1")
        self.assertEqual(by_key.title_block["図面番号"], (2225, 1566, 2255, 1584))
        self.assertEqual(len(by_key.index.columns), 5)
        self.assertEqual(by_key.index.columns[4].name_right_x, 1890)
        self.assertIn("toshiseibi_A1", [la.key for la in list_layouts()])

    def test_scaled_box(self):
        la = load_layout("toshiseibi_A1")
        box = la.box_on((2000, 1500, 2100, 1600), la.page_size[0] / 2, la.page_size[1] / 2)
        self.assertEqual(box, (1000, 750, 1050, 800))


class SyntheticExtractTest(unittest.TestCase):
    """合成PDFで、表題欄・目次・参照番号の読み取りを確かめる（実図面がなくても動く）。"""

    def setUp(self):
        self.layout = load_layout("toshiseibi_A1")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _make(self, scale=1.0):
        path = os.path.join(self.tmp.name, f"set_{scale}.pdf")
        make_synthetic_set(
            path,
            sheets=[{"工事名": "テスト工事", "図面名称": "図面リスト", "縮尺": "－", "図面番号": "１",
                     "製図年": "５", "製図月": "３", "総枚数": ""},
                    {"工事名": "テスト工事", "図面名称": "２～１３階平面図", "縮尺": "１：１００",
                     "図面番号": "１７", "製図年": "５", "製図月": "３", "総枚数": "２"}],
            index_rows=[(0, "1", "図面リスト", False), (0, "17", "2～13階平面図", False),
                        (4, "314", "屋外一般附帯詳細図(8)", True)],
            refs={2: [8, 22]}, scale=scale)
        return path

    def _check(self, path):
        with pymupdf.open(path) as doc:
            tb = read_title_block(doc[1], self.layout)
            self.assertEqual((tb.工事名, tb.図面名称, tb.縮尺, tb.図面番号, tb.製図年, tb.製図月, tb.総枚数),
                             ("テスト工事", "2~13階平面図", "1:100", "17", "5", "3", "2"))
            self.assertEqual(read_title_block(doc[0], self.layout).総枚数, "")
            index = read_index(doc, self.layout)
            self.assertEqual([(e.番号, e.名称, e.区分, e.欠番) for e in index],
                             [("1", "図面リスト", "意匠", False), ("17", "2~13階平面図", "意匠", False),
                              ("314", "屋外一般附帯詳細図(8)", "外構", True)])
            refs = read_references(doc[1], self.layout)
            self.assertEqual([r.number for r in refs], [8, 22])
            self.assertEqual(read_references(doc[0], self.layout), [])

    def test_a1(self):
        self._check(self._make())

    def test_a3_reduced(self):
        """A3縮小版（座標が約半分）でも、同じ様式設定で読める。"""
        self._check(self._make(scale=0.5))

    def test_blank_page_does_not_raise(self):
        path = os.path.join(self.tmp.name, "blank.pdf")
        with pymupdf.open() as doc:
            doc.new_page(width=100, height=100)
            doc.save(path)
        with pymupdf.open(path) as doc:
            tb = read_title_block(doc[0], self.layout)
            self.assertEqual(tb.図面番号, "")
            self.assertEqual(read_index(doc, self.layout), [])


@unittest.skipUnless(HAS_REAL, "tests/data/実図面.pdf がないためスキップ")
class RealExtractTest(unittest.TestCase):
    def setUp(self):
        self.layout = load_layout("toshiseibi_A1")

    def test_index_202_rows_and_kekban(self):
        with pymupdf.open(REAL_PDF) as doc:
            self.assertAlmostEqual(doc[0].rect.width, 2383.92, delta=0.5)
            index = read_index(doc, self.layout)
        self.assertEqual(len(index), 202)
        self.assertEqual(sorted(e.番号 for e in index if e.欠番), ["314", "326"])
        kekban = {e.番号: e.名称 for e in index if e.欠番}
        self.assertEqual(kekban["314"], "屋外一般附帯詳細図(8)")
        self.assertTrue(all(e.名称 for e in index))

    def test_title_blocks(self):
        with pymupdf.open(REAL_PDF) as doc:
            nos = [read_title_block(p, self.layout).図面番号 for p in doc]
            p3 = read_title_block(doc[2], self.layout)
        self.assertEqual(nos, ["1", "16", "17", "18", "19", "20", "21", "301", "304", "305", "355"])
        self.assertEqual((p3.図面名称, p3.縮尺, p3.製図年, p3.製図月), ("2~13階平面図", "1:100", "5", "3"))

    def test_references(self):
        with pymupdf.open(REAL_PDF) as doc:
            refs = [r.number for r in read_references(doc[7], self.layout)]
        for n in (8, 20, 22):
            self.assertIn(n, refs)

    def test_a3_reduced_copy_reads_same(self):
        with tempfile.TemporaryDirectory() as tmp:
            a3 = os.path.join(tmp, "a3.pdf")
            make_scaled_copy(REAL_PDF, a3, 1190.55 / 2383.92)
            with pymupdf.open(REAL_PDF) as a, pymupdf.open(a3) as b:
                self.assertEqual(len(read_index(b, self.layout)), 202)
                for pa, pb in zip(a, b):
                    self.assertEqual(read_title_block(pa, self.layout).as_dict(),
                                     read_title_block(pb, self.layout).as_dict())


if __name__ == "__main__":
    unittest.main()
