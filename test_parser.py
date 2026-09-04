import sys
import io
from parser import extract_mails

sys.stdout.reconfigure(encoding="utf-8")

for path in ["../メール出力.pdf", "../one_pdf_with_bookmarks2.pdf"]:
    print(f"##### {path} #####")
    mails = extract_mails(path)
    for m in mails:
        print(f"- 件名: {m.subject!r}")
        print(f"  差出人: {m.sender!r}")
        print(f"  宛先: {m.to!r}")
        print(f"  CC: {m.cc!r}")
        print(f"  受信日時: {m.received_at!r}")
        print(f"  送信日時: {m.sent_at!r}")
        print(f"  添付: {m.attachments!r}")
        print(f"  タイトル日時: {m.sent_date_from_title}")
        print(f"  ページ範囲: {m.start_page}-{m.end_page}")
        print(f"  本文文字数: {len(m.body_text)}")
        print(f"  本文冒頭: {m.body_text[:60]!r}")
        print()
