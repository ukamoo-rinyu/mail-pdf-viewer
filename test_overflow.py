"""添付ファイルが多く「+N」に折りたたまれるケースのチップ生成を検証する。
QMenu.exec()はモーダルでブロックするため、ポップアップ自体はここでは開かず、
overflowチップの構造(remaining一覧)が正しいことと、そこからのジャンプ配線を確認する。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

from PySide6.QtCore import QModelIndex, QRect
from PySide6.QtWidgets import QApplication

from db import AttachmentInfo, MailRow
from main import MailItemDelegate

app = QApplication(sys.argv)

# 6個の添付ファイルを持つダミーメールを作る(実PDFに依存せず、折り返しを強制的に再現)
attachments = [AttachmentInfo(name=f"添付ファイルサンプル名前{i:02d}.pdf", start_page=10 + i) for i in range(6)]
import json
mail = MailRow(
    id=0, subject="テストメール", sender="差出人テスト", sender_short="差出人テスト",
    to_addr="宛先テスト", cc="", attachments=" / ".join(a.name for a in attachments),
    attachments_json=json.dumps([{"name": a.name, "start_page": a.start_page} for a in attachments]),
    received_at="Mon, 01 Jan 2027 00:00:00 +0900", sent_at="", title_datetime="2027-01-01T00:00:00",
    start_page=10, end_page=16, preview="",
)

delegate = MailItemDelegate()
rect = QRect(0, 0, 400, 104)  # 一覧の一般的なカード幅を想定
_, _, _, y4 = delegate._row_positions(rect)
chips, _ = delegate._attachment_chips(rect, mail, y4)

print("チップ数:", len(chips))
for c in chips:
    print(" -", c["label"], "kind=", c.get("kind"), "clickable=", c["clickable"])

overflow_chips = [c for c in chips if c.get("kind") == "overflow"]
assert len(overflow_chips) == 1, "overflowチップが1つ生成されているはず"
overflow = overflow_chips[0]
print("\noverflowチップの残り件数:", len(overflow["remaining"]))
for a in overflow["remaining"]:
    print("  -", a.name, "page=", a.start_page)

shown_count = len(chips) - 1  # overflow chip自体を除く
total_covered = shown_count + len(overflow["remaining"])
print("\n表示済み+残り =", total_covered, "(期待値: 6)")
assert total_covered == 6

print("\n結果: OK")
