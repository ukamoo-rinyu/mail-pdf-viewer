import sys
sys.stdout.reconfigure(encoding="utf-8")

import db

pdf = "../one_pdf_with_bookmarks2.pdf"
db_path = db.open_or_build(pdf, force_rebuild=True)
print("DB:", db_path)

print("\n=== 全件 ===")
for m in db.list_all(db_path):
    print(m)

print("\n=== 検索: 間取り ===")
for m in db.search(db_path, "間取り"):
    print(m.subject, "|", m.sender)

print("\n=== 検索: ukamoo ===")
for m in db.search(db_path, "ukamoo"):
    print(m.subject, "|", m.sender, "|", m.to_addr)

print("\n=== 検索: 建築 (本文/添付内) ===")
for m in db.search(db_path, "建築"):
    print(m.subject, "|", m.attachments)

print("\n=== キャッシュ再利用確認 (再構築なし) ===")
db_path2 = db.open_or_build(pdf)
print("同じパス:", db_path == db_path2)
