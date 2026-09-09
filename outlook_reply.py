"""メール束PDFから読み取った1通の内容をもとに、Outlookの返信下書きを作成する。

Outlook本体とのやり取りはCOM経由(pywin32)で行うため、Windows + Outlookデスクトップ版が
インストール・設定済みの環境でのみ動作する。
"""
from __future__ import annotations

import re

import db
import parser

_EMAIL_RE = re.compile(r"<([^<>]+)>")
_BARE_EMAIL_RE = re.compile(r"^[^\s<>]+@[^\s<>]+$")

_OL_MAIL_ITEM = 0


def extract_emails(address_field: str) -> list[str]:
    """'表示名 <email> / 表示名2 <email2>' や 'email' からメールアドレスの一覧を取り出す。"""
    if not address_field:
        return []
    emails: list[str] = []
    for part in address_field.split("/"):
        part = part.strip()
        if not part:
            continue
        m = _EMAIL_RE.search(part)
        if m:
            emails.append(m.group(1).strip())
        elif _BARE_EMAIL_RE.match(part):
            emails.append(part)
    return emails


def reply_subject(subject: str) -> str:
    normalized = parser.normalize_subject(subject or "")
    return f"Re: {normalized}" if normalized else "Re:"


def build_quoted_body(mail: "db.MailRow", body_text: str) -> str:
    sent = mail.title_datetime.replace("T", " ") if mail.title_datetime else (mail.sent_at or mail.received_at)
    lines = [
        "",
        "",
        "________________________________",
        f"差出人: {mail.sender}",
        f"送信日時: {sent}",
        f"宛先: {mail.to_addr}",
    ]
    if mail.cc:
        lines.append(f"CC: {mail.cc}")
    lines.append(f"件名: {mail.subject}")
    lines.append("")
    lines.append(body_text.strip())
    return "\n".join(lines)


def create_reply_draft(mail: "db.MailRow", body_text: str, reply_all: bool = False, display: bool = True):
    """Outlookで返信メールの下書きを作成する(送信はしない)。作成したMailItemを返す。"""
    import win32com.client  # 遅延import: Outlook/pywin32が無い環境でも他機能に影響させない

    to_emails = extract_emails(mail.sender)
    if not to_emails:
        raise RuntimeError("差出人のメールアドレスを特定できませんでした。")

    cc_emails: list[str] = []
    if reply_all:
        others = extract_emails(mail.to_addr) + extract_emails(mail.cc)
        cc_emails = list(dict.fromkeys(e for e in others if e not in to_emails))

    outlook = win32com.client.Dispatch("Outlook.Application")
    item = outlook.CreateItem(_OL_MAIL_ITEM)
    item.To = "; ".join(to_emails)
    if cc_emails:
        item.CC = "; ".join(cc_emails)
    item.Subject = reply_subject(mail.subject)
    item.Body = build_quoted_body(mail, body_text)
    item.Save()
    if display:
        item.Display()
    return item
