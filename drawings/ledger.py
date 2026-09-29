"""工事台帳（工事ごとのフォルダにある 工事台帳.json）の読み書き。

- 台帳は人が読めるJSON。PDFは工事フォルダ内に置き、台帳には工事フォルダからの相対パスで記録する
- 保存時は上書き前に 工事台帳.bak.json を残す
- 他のPCなどで先に更新されていたら（読み込み後にファイルの更新日時が変わっていたら）
  LedgerConflictError を出して上書きしない（共有フォルダでの同時編集は想定しない）
- 変更記録の登録・編集・状態変更は「履歴」に自動で残す（いつ・誰が・何をしたか）
"""
from __future__ import annotations

import copy
import getpass
import json
import os
import re
import shutil
from datetime import date, datetime

LEDGER_FILENAME = "工事台帳.json"
BACKUP_FILENAME = "工事台帳.bak.json"
FORMAT_VERSION = 1

# ---------------------------------------------------------------- 選択肢（仮決め。STATE.md 参照）
# 状態：指示済 → 変更契約に反映済 → 竣工図に反映済、または 竣工図のみ反映（契約変更しない軽微な変更）
STATUS_DISCUSSING = "協議中"          # 仮に追加（まだ指示していない）
STATUS_INSTRUCTED = "指示済"
STATUS_CONTRACTED = "変更契約に反映済"
STATUS_COMPLETION_ONLY = "竣工図のみ反映"   # 契約変更しない軽微な変更（竣工図への反映待ち）
STATUS_COMPLETED = "竣工図に反映済"
STATUS_CANCELLED = "取消"              # 仮に追加（記録は消さずに残す）
STATUSES = [STATUS_DISCUSSING, STATUS_INSTRUCTED, STATUS_CONTRACTED, STATUS_COMPLETION_ONLY,
            STATUS_COMPLETED, STATUS_CANCELLED]
# 竣工図に反映すべき状態（協議中・取消以外）
COMPLETION_TARGET_STATUSES = [STATUS_INSTRUCTED, STATUS_CONTRACTED, STATUS_COMPLETION_ONLY, STATUS_COMPLETED]

TRIGGER_KINDS = ["質疑", "協議", "監督員指示", "受注者提案", "現場条件", "その他"]
INSTRUCTION_METHODS = ["工事打合せ簿", "質疑回答書", "協議書", "指示書", "口頭（後日書面）", "その他"]
VERSION_KIND_ORDER = "発注"             # 最初の版の版区分

KOJI_FIELDS = ["工事名", "工事番号", "施設名", "請負者", "契約日", "工期"]
RECORD_FIELDS = ["記録日", "指示日", "きっかけ", "内容", "理由", "決定者", "指示方法", "対象図面",
                 "図面上の箇所", "状態", "反映先"]

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class LedgerError(Exception):
    """台帳の読み書きで利用者に知らせるべき問題。"""


class LedgerConflictError(LedgerError):
    """読み込んだ後に、他で台帳ファイルが更新されていた。"""


def current_user() -> str:
    """操作者名（Windowsのログオン名）。取れなければ空文字。"""
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return os.environ.get("USERNAME", "")


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_text() -> str:
    return date.today().isoformat()


def is_valid_date(text: str) -> bool:
    """空文字か YYYY-MM-DD 形式の実在する日付なら True。"""
    if not text:
        return True
    if not _DATE_RE.match(text):
        return False
    try:
        date.fromisoformat(text)
        return True
    except ValueError:
        return False


def empty_ledger(layout_key: str = "") -> dict:
    """新しい台帳の中身（§5の形式）を作る。"""
    return {
        "format_version": FORMAT_VERSION,
        "工事": {k: "" for k in KOJI_FIELDS},
        "様式": layout_key,
        "図面": [],
        "変更記録": [],
    }


def _natural_no_key(no: str):
    return (0, int(no)) if no.isdigit() else (1, no)


class Ledger:
    """工事台帳1つ分。data は §5 の形式の辞書そのもの。"""

    def __init__(self, folder: str, data: dict | None = None):
        self.folder = os.path.abspath(folder)
        self.data = data if data is not None else empty_ledger()
        self._loaded_mtime: float | None = None

    # ------------------------------------------------------------ ファイル
    @property
    def path(self) -> str:
        return os.path.join(self.folder, LEDGER_FILENAME)

    @property
    def backup_path(self) -> str:
        return os.path.join(self.folder, BACKUP_FILENAME)

    @staticmethod
    def exists(folder: str) -> bool:
        return os.path.isfile(os.path.join(folder, LEDGER_FILENAME))

    @classmethod
    def load(cls, folder: str) -> "Ledger":
        """工事フォルダの台帳を読み込む。"""
        ledger = cls(folder)
        try:
            with open(ledger.path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise LedgerError(f"工事台帳の形式が壊れています（{e}）。{BACKUP_FILENAME} から戻せるか確認してください。") from e
        if not isinstance(data, dict):
            raise LedgerError("工事台帳の形式が正しくありません。")
        base = empty_ledger()
        for key, value in base.items():
            data.setdefault(key, value)
        for k in KOJI_FIELDS:
            data["工事"].setdefault(k, "")
        ledger.data = data
        ledger._loaded_mtime = os.path.getmtime(ledger.path)
        return ledger

    def changed_elsewhere(self) -> bool:
        """読み込み（または前回保存）の後に、他で台帳ファイルが書き換えられたか。"""
        if not os.path.isfile(self.path):
            return False
        if self._loaded_mtime is None:
            return True  # 新規作成のつもりが、既に台帳があった
        return abs(os.path.getmtime(self.path) - self._loaded_mtime) > 1e-6

    def save(self, force: bool = False):
        """台帳を保存する。上書き前にバックアップを残し、他で更新されていたら止める（force で強行）。"""
        if not force and self.changed_elsewhere():
            raise LedgerConflictError("工事台帳が、読み込んだ後に別の場所で更新されています。")
        os.makedirs(self.folder, exist_ok=True)
        if os.path.isfile(self.path):
            shutil.copy2(self.path, self.backup_path)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
        self._loaded_mtime = os.path.getmtime(self.path)

    # ------------------------------------------------------------ パス
    def rel_path(self, path: str) -> str:
        """工事フォルダからの相対パス（区切りは / ）。フォルダの外なら LedgerError。"""
        path = os.path.abspath(path)
        try:
            rel = os.path.relpath(path, self.folder)
        except ValueError as e:  # ドライブが違う
            raise LedgerError("PDFは工事フォルダの中に置いてください。") from e
        if rel.startswith(".."):
            raise LedgerError("PDFは工事フォルダの中に置いてください。")
        return rel.replace(os.sep, "/")

    def abs_path(self, rel: str) -> str:
        return os.path.normpath(os.path.join(self.folder, rel)) if rel else ""

    def is_inside(self, path: str) -> bool:
        try:
            self.rel_path(path)
            return True
        except LedgerError:
            return False

    # ------------------------------------------------------------ 工事情報
    @property
    def koji(self) -> dict:
        return self.data["工事"]

    @property
    def layout_key(self) -> str:
        return self.data.get("様式", "")

    # ------------------------------------------------------------ 図面
    @property
    def drawings(self) -> list[dict]:
        return self.data["図面"]

    def drawing(self, no: str) -> dict | None:
        return next((d for d in self.drawings if d.get("図面番号") == no), None)

    def ensure_drawing(self, no: str, name: str = "", category: str = "", scale: str = "") -> dict:
        """図面を探し、なければ追加する（番号順に並べ直す）。"""
        d = self.drawing(no)
        if d is None:
            d = {"図面番号": no, "図面名称": name, "区分": category, "縮尺": scale, "版": []}
            self.drawings.append(d)
            self.drawings.sort(key=lambda x: _natural_no_key(x.get("図面番号", "")))
        else:
            if name and not d.get("図面名称"):
                d["図面名称"] = name
            if scale and not d.get("縮尺"):
                d["縮尺"] = scale
        return d

    def add_version(self, no: str, kind: str, day: str, pdf_rel: str, page: int | None) -> dict:
        """図面に新しい版を追加する（同じ版区分があれば置き換える）。"""
        d = self.ensure_drawing(no)
        entry = {"版区分": kind, "日付": day, "pdf": pdf_rel, "ページ": page}
        for i, v in enumerate(d["版"]):
            if v.get("版区分") == kind:
                d["版"][i] = entry
                return entry
        d["版"].append(entry)
        return entry

    def versions_with_pdf(self, no: str) -> list[dict]:
        """PDFが登録されている版を古い順に返す。"""
        d = self.drawing(no)
        return [v for v in (d or {}).get("版", []) if v.get("pdf") and v.get("ページ")]

    def latest_version(self, no: str) -> dict | None:
        vs = self.versions_with_pdf(no)
        return vs[-1] if vs else None

    def version_kinds(self) -> list[str]:
        """台帳にある版区分（登場順）。"""
        kinds: list[str] = []
        for d in self.drawings:
            for v in d.get("版", []):
                if v.get("版区分") and v["版区分"] not in kinds:
                    kinds.append(v["版区分"])
        return kinds

    def next_contract_kind(self) -> str:
        """次の変更契約の版区分（変更契約1, 変更契約2, ...）。"""
        n = 0
        for k in self.version_kinds():
            m = re.fullmatch(r"変更契約(\d+)", k)
            if m:
                n = max(n, int(m.group(1)))
        return f"変更契約{n + 1}"

    # ------------------------------------------------------------ 変更記録
    @property
    def records(self) -> list[dict]:
        return self.data["変更記録"]

    def record(self, rec_id: str) -> dict | None:
        return next((r for r in self.records if r.get("id") == rec_id), None)

    def next_record_id(self) -> str:
        n = 0
        for r in self.records:
            m = re.fullmatch(r"C-(\d+)", r.get("id", ""))
            if m:
                n = max(n, int(m.group(1)))
        return f"C-{n + 1:03d}"

    @staticmethod
    def new_record() -> dict:
        """空の変更記録（§5の項目）。"""
        return {"id": "", "記録日": today_text(), "指示日": "",
                "きっかけ": {"種別": "", "番号": ""},
                "内容": "", "理由": "", "決定者": "", "指示方法": "",
                "対象図面": [], "図面上の箇所": [],
                "状態": STATUS_INSTRUCTED,
                "反映先": {"変更契約": None, "竣工図": None},
                "履歴": []}

    def _history(self, rec: dict, op: str, detail: str, user: str | None):
        rec.setdefault("履歴", []).append(
            {"日時": now_text(), "操作者": current_user() if user is None else user, "操作": op, "内容": detail})

    @staticmethod
    def validate_record(rec: dict) -> list[str]:
        """入力内容の問題点を返す（空なら問題なし）。"""
        errors = []
        if not rec.get("内容", "").strip():
            errors.append("「内容」を入力してください。")
        if not rec.get("対象図面"):
            errors.append("「対象図面」を1つ以上選んでください。")
        for key in ("記録日", "指示日"):
            if not is_valid_date(rec.get(key, "")):
                errors.append(f"「{key}」は 2024-04-01 の形で入力してください。")
        if rec.get("状態") not in STATUSES:
            errors.append("「状態」を選んでください。")
        return errors

    def add_record(self, rec: dict, user: str | None = None) -> dict:
        """変更記録を登録する（idを振り、履歴に「登録」を残す）。"""
        rec = copy.deepcopy(rec)
        rec["id"] = self.next_record_id()
        rec.setdefault("履歴", [])
        rec.setdefault("反映先", {"変更契約": None, "竣工図": None})
        self._history(rec, "登録", f"状態「{rec.get('状態', '')}」で登録", user)
        self.records.append(rec)
        return rec

    def update_record(self, rec_id: str, new_values: dict, user: str | None = None) -> list[str]:
        """変更記録を書き換え、変わった項目を履歴に残す。変わった項目名のリストを返す。"""
        rec = self.record(rec_id)
        if rec is None:
            raise LedgerError(f"変更記録 {rec_id} が見つかりません。")
        changed = []
        old_status = rec.get("状態")
        for key in RECORD_FIELDS:
            if key in new_values and new_values[key] != rec.get(key):
                changed.append(key)
                rec[key] = copy.deepcopy(new_values[key])
        if changed:
            others = [k for k in changed if k != "状態"]
            if others:
                self._history(rec, "編集", "変更した項目: " + "・".join(others), user)
            if "状態" in changed:
                self._history(rec, "状態変更", f"{old_status} → {rec['状態']}", user)
        return changed

    def set_status(self, rec_id: str, status: str, user: str | None = None, note: str = "") -> bool:
        """状態だけを変える（履歴に「状態変更」を残す）。変わったら True。"""
        rec = self.record(rec_id)
        if rec is None or status not in STATUSES or rec.get("状態") == status:
            return False
        old = rec.get("状態")
        rec["状態"] = status
        self._history(rec, "状態変更", f"{old} → {status}" + (f"（{note}）" if note else ""), user)
        return True

    def records_for_drawing(self, no: str) -> list[dict]:
        """その図面が対象の変更記録を、指示日（なければ記録日）の古い順に返す。"""
        rows = [r for r in self.records if no in r.get("対象図面", [])]
        return sorted(rows, key=lambda r: (r.get("指示日") or r.get("記録日") or "", r.get("id", "")))

    def filter_records(self, status: str | None = None, drawing_no: str | None = None,
                       date_from: str = "", date_to: str = "") -> list[dict]:
        """状態・図面・期間（指示日、なければ記録日）で絞り込む。"""
        out = []
        for r in self.records:
            if status and r.get("状態") != status:
                continue
            if drawing_no and drawing_no not in r.get("対象図面", []):
                continue
            day = r.get("指示日") or r.get("記録日") or ""
            if date_from and (not day or day < date_from):
                continue
            if date_to and (not day or day > date_to):
                continue
            out.append(r)
        return sorted(out, key=lambda r: (r.get("指示日") or r.get("記録日") or "", r.get("id", "")))

    # ------------------------------------------------------------ 変更契約・竣工図
    def pending_for_contract(self) -> list[tuple[dict, list[dict]]]:
        """状態が「指示済」の変更記録を図面別にまとめる（変更契約用一覧）。[(図面, [記録...])]"""
        out = []
        for d in self.drawings:
            recs = [r for r in self.records_for_drawing(d["図面番号"]) if r.get("状態") == STATUS_INSTRUCTED]
            if recs:
                out.append((d, recs))
        return out

    def completion_checklist(self) -> list[tuple[dict, list[dict]]]:
        """竣工図に反映すべき全変更（契約変更の有無を問わず）を図面別にまとめる。"""
        out = []
        for d in self.drawings:
            recs = [r for r in self.records_for_drawing(d["図面番号"])
                    if r.get("状態") in COMPLETION_TARGET_STATUSES]
            if recs:
                out.append((d, recs))
        return out

    @staticmethod
    def is_completion_done(rec: dict) -> bool:
        return rec.get("状態") == STATUS_COMPLETED

    def set_completion_done(self, rec_id: str, done: bool, user: str | None = None) -> bool:
        """竣工図チェックリストのチェックを付ける/外す。外したときは反映前の状態に戻す。"""
        rec = self.record(rec_id)
        if rec is None or self.is_completion_done(rec) == done:
            return False
        refl = rec.setdefault("反映先", {"変更契約": None, "竣工図": None})
        if done:
            refl["竣工図"] = today_text()
            return self.set_status(rec_id, STATUS_COMPLETED, user, "竣工図チェックリスト")
        refl["竣工図"] = None
        back = self._status_before_completion(rec)
        return self.set_status(rec_id, back, user, "竣工図チェックを外した")

    @staticmethod
    def _status_before_completion(rec: dict) -> str:
        """「竣工図に反映済」にする直前の状態（履歴から探す。なければ反映先から推定）。"""
        for h in reversed(rec.get("履歴", [])):
            if h.get("操作") == "状態変更":
                m = re.match(r"^(.+?) → " + re.escape(STATUS_COMPLETED), h.get("内容", ""))
                if m and m.group(1) in STATUSES:
                    return m.group(1)
        if (rec.get("反映先") or {}).get("変更契約"):
            return STATUS_CONTRACTED
        return STATUS_INSTRUCTED

    def register_contract_version(self, kind: str, day: str, pdf_rel: str, page_map: dict[str, int],
                                  record_ids: list[str], user: str | None = None,
                                  names: dict[str, str] | None = None):
        """変更図面PDFを新しい版として登録し、反映した変更記録を「変更契約に反映済」にする。

        page_map: 図面番号 → PDF内のページ（1始まり）。names があれば台帳にない図面の名称に使う。
        """
        for no, page in page_map.items():
            self.ensure_drawing(no, (names or {}).get(no, ""))
            self.add_version(no, kind, day, pdf_rel, page)
        for rid in record_ids:
            rec = self.record(rid)
            if rec is None:
                continue
            rec.setdefault("反映先", {"変更契約": None, "竣工図": None})["変更契約"] = kind
            if rec.get("状態") in (STATUS_DISCUSSING, STATUS_INSTRUCTED):
                self.set_status(rid, STATUS_CONTRACTED, user, f"{kind}の変更図面を登録")
            else:
                self._history(rec, "反映", f"{kind}の変更図面に反映", user)


def build_from_order_set(folder: str, pdf_path: str, layout, koji: dict, kind: str = VERSION_KIND_ORDER,
                         day: str = "") -> Ledger:
    """発注図PDFから新しい台帳を作る。目次から図面一覧を作り、各ページの表題欄の番号で版を登録する。"""
    import pymupdf  # GUIなしでも使えるよう、ここで読み込む

    from .extract import lines_of, read_index, read_title_block

    ledger = Ledger(folder, empty_ledger(layout.key))
    for k in KOJI_FIELDS:
        ledger.koji[k] = koji.get(k, "")
    pdf_rel = ledger.rel_path(pdf_path)
    with pymupdf.open(pdf_path) as doc:
        index = read_index(doc, layout)
        for e in index:
            if e.欠番 or not e.名称:
                continue
            ledger.ensure_drawing(e.番号, e.名称, e.区分)
        for i, page in enumerate(doc):
            tb = read_title_block(page, layout, lines_of(page))
            if not tb.図面番号:
                continue
            d = ledger.ensure_drawing(tb.図面番号, tb.図面名称, "", tb.縮尺)
            if tb.縮尺:
                d["縮尺"] = tb.縮尺
            if not ledger.koji.get("工事名") and tb.工事名:
                ledger.koji["工事名"] = tb.工事名
            if not any(v.get("版区分") == kind for v in d["版"]):
                ledger.add_version(tb.図面番号, kind, day, pdf_rel, i + 1)
    return ledger
