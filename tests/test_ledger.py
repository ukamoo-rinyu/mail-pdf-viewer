"""工事台帳（drawings.ledger）と、変更契約・竣工図の出力のテスト。"""
import json
import os
import shutil
import tempfile
import time
import unittest

from drawings import report
from drawings.layouts import load_layout
from drawings.ledger import (BACKUP_FILENAME, LEDGER_FILENAME, STATUS_COMPLETED, STATUS_COMPLETION_ONLY,
                             STATUS_CONTRACTED, STATUS_DISCUSSING, STATUS_INSTRUCTED, Ledger, LedgerConflictError,
                             LedgerError, build_from_order_set, is_valid_date)
from tests.drawing_samples import REAL_PDF, make_synthetic_set


def _rec(ledger, drawings, content="手すり高さ変更", status=STATUS_INSTRUCTED, day="2024-05-01"):
    r = Ledger.new_record()
    r.update({"内容": content, "対象図面": list(drawings), "状態": status, "指示日": day,
              "理由": "現場条件", "決定者": "監督員", "指示方法": "工事打合せ簿",
              "きっかけ": {"種別": "質疑", "番号": "質疑書No.12"},
              "図面上の箇所": [{"図面番号": drawings[0], "page": 2, "rect": [10, 20, 110, 80], "コメント": "北側"}]})
    return ledger.add_record(r, user="tester")


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = self.tmp.name
        self.ledger = Ledger(self.folder)
        for no, name in (("16", "1階平面図"), ("17", "2~13階平面図"), ("301", "外構配置図")):
            self.ledger.ensure_drawing(no, name, "意匠")
            self.ledger.add_version(no, "発注", "2023-04-05", "発注図/発注図一式.pdf", int(no) % 10 + 1)

    def test_save_load_backup_and_conflict(self):
        L = self.ledger
        L.koji["工事名"] = "テスト工事"
        L.save()
        self.assertFalse(os.path.exists(os.path.join(self.folder, BACKUP_FILENAME)))
        _rec(L, ["17"])
        L.save()
        self.assertTrue(os.path.exists(os.path.join(self.folder, BACKUP_FILENAME)))
        with open(os.path.join(self.folder, BACKUP_FILENAME), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["変更記録"], [])  # バックアップは上書き前の内容

        loaded = Ledger.load(self.folder)
        self.assertEqual(loaded.koji["工事名"], "テスト工事")
        self.assertEqual(loaded.records[0]["id"], "C-001")
        self.assertEqual(loaded.data["format_version"], 1)

        # 他で更新されたら保存を止める
        time.sleep(0.05)
        other = Ledger.load(self.folder)
        other.koji["請負者"] = "別の人"
        other.save()
        os.utime(other.path, (time.time() + 5, time.time() + 5))
        loaded.koji["工期"] = "x"
        with self.assertRaises(LedgerConflictError):
            loaded.save()
        loaded.save(force=True)

    def test_broken_json(self):
        with open(os.path.join(self.folder, LEDGER_FILENAME), "w", encoding="utf-8") as f:
            f.write("{壊れた")
        with self.assertRaises(LedgerError):
            Ledger.load(self.folder)

    def test_relative_paths(self):
        L = self.ledger
        self.assertEqual(L.rel_path(os.path.join(self.folder, "発注図", "a.pdf")), "発注図/a.pdf")
        with self.assertRaises(LedgerError):
            L.rel_path(os.path.join(os.path.dirname(self.folder), "outside.pdf"))
        self.assertEqual(L.abs_path("発注図/a.pdf"), os.path.join(self.folder, "発注図", "a.pdf"))

    def test_record_history_and_filters(self):
        L = self.ledger
        r1 = _rec(L, ["17", "16"], day="2024-05-01")
        r2 = _rec(L, ["17"], content="建具変更", status=STATUS_DISCUSSING, day="2024-04-01")
        self.assertEqual((r1["id"], r2["id"]), ("C-001", "C-002"))
        self.assertEqual(r1["履歴"][0]["操作"], "登録")
        self.assertEqual(r1["履歴"][0]["操作者"], "tester")
        self.assertEqual([r["id"] for r in L.records_for_drawing("17")], ["C-002", "C-001"])  # 指示日順
        self.assertEqual([r["id"] for r in L.records_for_drawing("16")], ["C-001"])

        changed = L.update_record("C-001", {"内容": "手すり高さ変更（H1100）", "状態": STATUS_COMPLETION_ONLY},
                                  user="tester")
        self.assertEqual(sorted(changed), ["内容", "状態"])
        ops = [h["操作"] for h in L.record("C-001")["履歴"]]
        self.assertEqual(ops, ["登録", "編集", "状態変更"])
        self.assertEqual(L.update_record("C-001", {"内容": "手すり高さ変更（H1100）"}), [])

        self.assertEqual([r["id"] for r in L.filter_records(status=STATUS_DISCUSSING)], ["C-002"])
        self.assertEqual([r["id"] for r in L.filter_records(drawing_no="16")], ["C-001"])
        self.assertEqual([r["id"] for r in L.filter_records(date_from="2024-04-15")], ["C-001"])
        self.assertEqual([r["id"] for r in L.filter_records(date_to="2024-04-15")], ["C-002"])

    def test_validation(self):
        r = Ledger.new_record()
        self.assertEqual(len(Ledger.validate_record(r)), 2)  # 内容・対象図面
        r.update({"内容": "x", "対象図面": ["17"], "指示日": "2024/5/1"})
        self.assertEqual(len(Ledger.validate_record(r)), 1)
        self.assertTrue(is_valid_date("2024-02-29"))
        self.assertFalse(is_valid_date("2023-02-29"))

    def test_contract_and_completion_flow(self):
        L = self.ledger
        _rec(L, ["17"])                                              # C-001 指示済
        _rec(L, ["17", "301"], content="舗装変更")                     # C-002 指示済
        _rec(L, ["16"], content="軽微な変更", status=STATUS_COMPLETION_ONLY)  # C-003
        _rec(L, ["16"], content="協議中の件", status=STATUS_DISCUSSING)       # C-004

        pending = L.pending_for_contract()
        self.assertEqual([(d["図面番号"], [r["id"] for r in rs]) for d, rs in pending],
                         [("17", ["C-001", "C-002"]), ("301", ["C-002"])])

        kind = L.next_contract_kind()
        self.assertEqual(kind, "変更契約1")
        L.register_contract_version(kind, "2024-09-01", "変更図/変更1.pdf", {"17": 1, "401": 2},
                                    ["C-001", "C-002"], user="tester", names={"401": "新しい図面"})
        self.assertEqual(L.record("C-001")["状態"], STATUS_CONTRACTED)
        self.assertEqual(L.record("C-002")["反映先"]["変更契約"], "変更契約1")
        self.assertEqual(L.latest_version("17")["版区分"], "変更契約1")
        self.assertEqual(L.latest_version("17")["ページ"], 1)
        self.assertEqual(L.drawing("401")["図面名称"], "新しい図面")
        self.assertEqual(L.pending_for_contract(), [])
        self.assertEqual(L.next_contract_kind(), "変更契約2")

        checklist = L.completion_checklist()
        ids = sorted({r["id"] for _, rs in checklist for r in rs})
        self.assertEqual(ids, ["C-001", "C-002", "C-003"])  # 協議中は含めない
        self.assertTrue(L.set_completion_done("C-003", True, user="tester"))
        self.assertEqual(L.record("C-003")["状態"], STATUS_COMPLETED)
        self.assertTrue(L.set_completion_done("C-003", False, user="tester"))
        self.assertEqual(L.record("C-003")["状態"], STATUS_COMPLETION_ONLY)  # 元の状態に戻る
        L.set_completion_done("C-001", True)
        L.set_completion_done("C-001", False)
        self.assertEqual(L.record("C-001")["状態"], STATUS_CONTRACTED)

        out = self.folder
        report.write_contract_list_html(L, os.path.join(out, "c.html"))
        report.write_contract_list_csv(L, os.path.join(out, "c.csv"))
        report.write_completion_checklist_html(L, os.path.join(out, "k.html"))
        report.write_completion_checklist_csv(L, os.path.join(out, "k.csv"))
        report.write_records_csv(L.records, os.path.join(out, "r.csv"))
        with open(os.path.join(out, "k.html"), encoding="utf-8") as f:
            self.assertIn("軽微な変更", f.read())


class BuildFromOrderSetTest(unittest.TestCase):
    def test_synthetic(self):
        with tempfile.TemporaryDirectory() as folder:
            os.makedirs(os.path.join(folder, "発注図"))
            pdf = os.path.join(folder, "発注図", "一式.pdf")
            base = {"工事名": "テスト工事", "縮尺": "1:100", "製図年": "5", "製図月": "3", "総枚数": ""}
            make_synthetic_set(pdf, [dict(base, 図面名称="図面リスト", 図面番号="１"),
                                     dict(base, 図面名称="平面図", 図面番号="２")],
                               index_rows=[(0, "1", "図面リスト", False), (0, "2", "平面図", False),
                                           (0, "3", "立面図", False), (0, "4", "旧図", True)])
            L = build_from_order_set(folder, pdf, load_layout("toshiseibi_A1"), {"工事番号": "R5-1"},
                                     day="2023-04-05")
            self.assertEqual([d["図面番号"] for d in L.drawings], ["1", "2", "3"])  # 欠番は入れない
            self.assertEqual(L.koji["工事名"], "テスト工事")
            self.assertEqual(L.koji["工事番号"], "R5-1")
            self.assertEqual(L.latest_version("2"), {"版区分": "発注", "日付": "2023-04-05",
                                                     "pdf": "発注図/一式.pdf", "ページ": 2})
            self.assertIsNone(L.latest_version("3"))
            L.save()
            self.assertTrue(Ledger.exists(folder))

    @unittest.skipUnless(os.path.isfile(REAL_PDF), "tests/data/実図面.pdf がないためスキップ")
    def test_real(self):
        with tempfile.TemporaryDirectory() as folder:
            pdf = os.path.join(folder, "発注図.pdf")
            shutil.copy(REAL_PDF, pdf)
            L = build_from_order_set(folder, pdf, load_layout("toshiseibi_A1"), {})
            self.assertEqual(len(L.drawings), 200)  # 202行 − 欠番2
            self.assertEqual(sum(1 for d in L.drawings if d["版"]), 11)
            self.assertEqual(L.drawing("17")["縮尺"], "1:100")
            self.assertEqual(L.drawing("301")["区分"], "外構")


if __name__ == "__main__":
    unittest.main()
