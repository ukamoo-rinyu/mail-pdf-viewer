"""発注前チェック（drawings.checks）と出力（drawings.report）のテスト。

実図面PDFを使うテストは、tests/data/ にPDFがない環境ではスキップする。
"""
import csv
import importlib.util
import os
import tempfile
import unittest
from collections import Counter

from drawings import report
from drawings.checks import (LEVEL_CHECK, LEVEL_INFO, Sheet, check_blank_fields, check_duplicate_pages,
                             check_index, check_index_not_in_set, check_kojimei, check_number_and_name,
                             check_references, check_totals, main as cli_main, run_checks)
from drawings.extract import IndexEntry, Reference, TitleBlock
from drawings.layouts import load_layout
from tests.drawing_samples import ERROR_PDF, REAL_PDF, make_synthetic_set

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAT = r"屋外一般附帯詳細図\((\d+)\)"


def entry(no, name, kekban=False, cat="意匠"):
    return IndexEntry(番号=no, 名称=name, 区分=cat, 欠番=kekban, rect=(0, 0, 10, 10), page=1)


def sheet(page, no, name, scale="1:100", total="", year="5", month="3", koji="工事", refs=()):
    tb = TitleBlock(工事名=koji, 図面名称=name, 縮尺=scale, 図面番号=no, 製図年=year, 製図月=month, 総枚数=total,
                    rects={k: (1, 2, 3, 4) for k in ("図面番号", "図面名称", "縮尺", "製図年", "製図月", "総枚数")})
    return Sheet(page=page, title=tb,
                 references=[Reference("附帯詳細図", n, PAT, (5, 5, 9, 9)) for n in refs])


def ids(findings):
    return [f.rule_id for f in findings]


class RuleTest(unittest.TestCase):
    """ルールごとの判定（PDFを使わない）。"""

    INDEX = [entry("1", "図面リスト"), entry("17", "2~13階平面図"), entry("304", "外構詳細図1", cat="外構"),
             entry("303", "雨水排水桝リスト", cat="外構"), entry("314", "屋外一般附帯詳細図(8)", True, "外構"),
             entry("315", "屋外一般附帯詳細図(9)", cat="外構")]

    def test_index_rules(self):
        idx = [entry("1", "A"), entry("1", "B"), entry("2", "A"), entry("3", ""), entry("4", "C", True),
               entry("5", "C", True)]
        self.assertEqual(Counter(ids(check_index(idx))),
                         Counter({"INDEX_DUP_NO": 1, "INDEX_DUP_NAME": 1, "INDEX_BLANK": 1}))
        self.assertEqual(check_index(self.INDEX), [])

    def test_number_and_name(self):
        self.assertEqual(check_number_and_name(sheet(1, "17", "2~13階平面図"), self.INDEX), [])
        self.assertEqual(ids(check_number_and_name(sheet(1, "", "x"), self.INDEX)), ["NO_UNREADABLE"])
        self.assertEqual(ids(check_number_and_name(sheet(1, "999", "x"), self.INDEX)), ["NO_NOT_IN_INDEX"])
        self.assertEqual(ids(check_number_and_name(sheet(1, "314", "x"), self.INDEX)), ["NO_IS_KEKBAN"])
        f = check_number_and_name(sheet(1, "17", "2~12階平面図"), self.INDEX)
        self.assertEqual(ids(f), ["NAME_MISMATCH"])
        self.assertNotIn("付け違い", f[0].message)
        f = check_number_and_name(sheet(9, "303", "外構詳細図1"), self.INDEX)
        self.assertEqual(ids(f), ["NAME_MISMATCH"])
        self.assertIn("No.304", f[0].message)
        self.assertIn("付け違い", f[0].message)
        self.assertEqual(f[0].rect, (1, 2, 3, 4))

    def test_blank_fields(self):
        self.assertEqual(ids(check_blank_fields(sheet(1, "1", "a", total="10"))), [])
        self.assertEqual(ids(check_blank_fields(sheet(1, "1", "a", scale="", year="", total=""))),
                         ["SCALE_BLANK", "DATE_BLANK", "TOTAL_BLANK"])
        self.assertEqual(ids(check_blank_fields(sheet(1, "1", "a", month="", total="1"))), ["DATE_BLANK"])

    def test_references(self):
        f = check_references(sheet(8, "301", "外構配置図", refs=(8, 9, 22)), self.INDEX)
        self.assertEqual(ids(f), ["REF_KEKBAN", "REF_MISSING"])
        self.assertIn("屋外一般附帯詳細図(8)", f[0].message)
        self.assertIn("No.314", f[0].message)
        self.assertIn("屋外一般附帯詳細図(22)", f[1].message)

    def test_totals_kojimei_duplicates(self):
        idx = [entry("1", "A"), entry("2", "B"), entry("3", "C", True)]
        self.assertEqual(check_totals([sheet(1, "1", "A", total="2"), sheet(2, "2", "B", total="2")], idx), [])
        self.assertEqual(check_totals([sheet(1, "1", "A"), sheet(2, "2", "B")], idx), [])
        f = check_totals([sheet(1, "1", "A", total="2"), sheet(2, "2", "B", total="3")], idx)
        self.assertEqual(ids(f), ["TOTAL_MISMATCH", "TOTAL_MISMATCH"])
        self.assertEqual(ids(check_kojimei([sheet(1, "1", "A"), sheet(2, "2", "B", koji="別工事")])),
                         ["KOJIMEI_MISMATCH"])
        f = check_duplicate_pages([sheet(1, "1", "A"), sheet(2, "1", "A"), sheet(3, "", "")])
        self.assertEqual([(x.rule_id, x.page) for x in f], [("DUP_PAGE_NO", 2)])

    def test_index_not_in_set_is_info(self):
        f = check_index_not_in_set([sheet(1, "1", "図面リスト")], self.INDEX)
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].level, LEVEL_INFO)
        self.assertEqual(len(f[0].details), 4)  # 欠番は数えない


class SyntheticSetTest(unittest.TestCase):
    """合成PDFで、読み取りからチェック・出力まで通して動かす。"""

    def test_run_and_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = os.path.join(tmp, "set.pdf")
            base = {"工事名": "テスト工事", "縮尺": "１：１００", "製図年": "５", "製図月": "３", "総枚数": "３"}
            make_synthetic_set(pdf, [dict(base, 図面名称="図面リスト", 図面番号="１"),
                                     dict(base, 図面名称="平面図", 図面番号="２"),
                                     dict(base, 図面名称="平面図", 図面番号="２", 縮尺="")],
                               index_rows=[(0, "1", "図面リスト", False), (0, "2", "平面図", False),
                                           (0, "3", "立面図", False)])
            res = run_checks(pdf, load_layout("toshiseibi_A1"))
            self.assertEqual(Counter(ids(res.findings)),
                             Counter({"DUP_PAGE_NO": 1, "SCALE_BLANK": 1, "INDEX_NOT_IN_SET": 1}))
            self.assertEqual([s.index_name for s in res.sheets], ["図面リスト", "平面図", "平面図"])
            out_csv, out_html = os.path.join(tmp, "r.csv"), os.path.join(tmp, "r.html")
            report.write_check_csv(res, out_csv)
            report.write_check_html(res, out_html)
            with open(out_csv, "rb") as f:
                self.assertTrue(f.read().startswith(b"\xef\xbb\xbf"))  # Excel用のBOM
            with open(out_csv, encoding="utf-8-sig") as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0][:3], ["ページ", "図面番号", "図面名称"])
            with open(out_html, encoding="utf-8") as f:
                self.assertIn("ページで重複しています", f.read())

    def test_external_index(self):
        """目次のない変更図面を、渡した図面一覧と照合できる。"""
        with tempfile.TemporaryDirectory() as tmp:
            pdf = os.path.join(tmp, "change.pdf")
            make_synthetic_set(pdf, [{"工事名": "工事", "図面名称": "平面図", "図面番号": "２", "縮尺": "1:100",
                                      "製図年": "6", "製図月": "1", "総枚数": ""}])
            res = run_checks(pdf, load_layout("toshiseibi_A1"), index=[entry("2", "平面図")])
            self.assertEqual(ids(res.findings), ["TOTAL_BLANK"])


def _load_prototype():
    spec = importlib.util.spec_from_file_location("check_drawings_prototype",
                                                  os.path.join(ROOT, "tools", "check_drawings_prototype.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _proto_kinds(messages: list[str]) -> Counter:
    """試作スクリプトの判定文を、本実装の rule_id に読み替える。"""
    table = [("目次では欠番です", "REF_KEKBAN"), ("参照していますが目次にありません", "REF_MISSING"),
             ("読み取れません", "NO_UNREADABLE"), ("が目次にありません", "NO_NOT_IN_INDEX"),
             ("目次で欠番になっています", "NO_IS_KEKBAN"), ("名称不一致", "NAME_MISMATCH"),
             ("縮尺が空欄", "SCALE_BLANK"), ("総枚数が空欄", "TOTAL_BLANK"), ("製図年月が空欄", "DATE_BLANK")]
    out = Counter()
    for m in messages:
        if "番号の付け違い" in m:
            continue  # 本実装では NAME_MISMATCH の文中に併記する
        out[next(rid for key, rid in table if key in m)] += 1
    return out


class RealPdfTest(unittest.TestCase):
    """仕様書 §4.5 の合格条件。"""

    @classmethod
    def setUpClass(cls):
        cls.layout = load_layout("toshiseibi_A1")

    @unittest.skipUnless(os.path.isfile(REAL_PDF), "tests/data/実図面.pdf がないためスキップ")
    def test_real_set(self):
        res = run_checks(REAL_PDF, self.layout)
        self.assertEqual(len(res.index), 202)
        self.assertEqual(sorted(e.番号 for e in res.index if e.欠番), ["314", "326"])
        self.assertEqual(len(res.sheets), 11)
        for s in res.sheets:
            self.assertEqual(s.index_name, s.name, f"p{s.page}")
            self.assertNotIn("NAME_MISMATCH", ids(s.findings))
        c = Counter(ids(res.findings))
        self.assertEqual(c["TOTAL_BLANK"], 11)
        refs = [f for f in res.findings if f.rule_id.startswith("REF_")]
        self.assertEqual(sorted((f.rule_id, f.page) for f in refs),
                         [("REF_KEKBAN", 8), ("REF_KEKBAN", 8), ("REF_MISSING", 8)])
        self.assertTrue(any("(22)" in f.message for f in refs if f.rule_id == "REF_MISSING"))
        self.assertTrue(any("(8)" in f.message and "314" in f.message for f in refs))
        self.assertTrue(any("(20)" in f.message and "326" in f.message for f in refs))
        # 上記以外の要確認は出ない（参考の「図面がない」は件数のみ）
        self.assertEqual(sum(c.values()), 11 + 3 + 1)
        self.assertEqual(c["INDEX_NOT_IN_SET"], 1)

    @unittest.skipUnless(os.path.isfile(ERROR_PDF), "tests/data/実図面_間違い仕込み.pdf がないためスキップ")
    def test_error_set(self):
        res = run_checks(ERROR_PDF, self.layout)
        by_page = {s.page: s for s in res.sheets}
        f3 = [f for f in by_page[3].findings if f.rule_id == "NAME_MISMATCH"]
        self.assertEqual(len(f3), 1)
        self.assertIn("2~12階平面図", f3[0].message)
        f9 = [f for f in by_page[9].findings if f.rule_id == "NAME_MISMATCH"]
        self.assertEqual(by_page[9].drawing_no, "303")
        self.assertIn("No.304", f9[0].message)
        self.assertIn("付け違い", f9[0].message)
        self.assertIn("SCALE_BLANK", ids(by_page[10].findings))
        mismatch = [f.page for f in res.findings if f.rule_id in ("NAME_MISMATCH", "SCALE_BLANK")]
        self.assertEqual(sorted(mismatch), [3, 9, 10])

    def test_same_as_prototype(self):
        """試作スクリプトと同じ結果になる（ページごとの読み取り値と判定の種類・件数）。"""
        pdfs = [p for p in (REAL_PDF, ERROR_PDF) if os.path.isfile(p)]
        if not pdfs:
            self.skipTest("tests/data/ に実図面PDFがないためスキップ")
        proto = _load_prototype()
        for pdf in pdfs:
            with self.subTest(pdf=os.path.basename(pdf)):
                old = proto.check(pdf)
                new = run_checks(pdf, self.layout)
                self.assertEqual(old["目次件数"], len(new.index))
                self.assertEqual([(e["番号"], e["名称"], e["欠番"], e["区分"]) for e in old["目次"]],
                                 [(e.番号, e.名称, e.欠番, e.区分) for e in new.index])
                for o, n in zip(old["図面"], new.sheets):
                    self.assertEqual((o["図面番号"], o["図面名称"], o["縮尺"], o["工事名"], o["総枚数"]),
                                     (n.title.図面番号, n.title.図面名称, n.title.縮尺, n.title.工事名, n.title.総枚数))
                    self.assertEqual(_proto_kinds(o["判定"]), Counter(ids(n.findings)), f"p{n.page}")
                self.assertEqual(len(old["全体の指摘"]),
                                 len([f for f in new.general_findings if f.level == LEVEL_CHECK]))

    @unittest.skipUnless(os.path.isfile(REAL_PDF), "tests/data/実図面.pdf がないためスキップ")
    def test_cli_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out.csv")
            self.assertEqual(cli_main([REAL_PDF, "--layout", "toshiseibi_A1", "--csv", out]), 0)
            with open(out, encoding="utf-8-sig") as f:
                rows = list(csv.reader(f))
            self.assertEqual(sum(1 for r in rows if r[10].startswith("「（")), 11)


if __name__ == "__main__":
    unittest.main()
