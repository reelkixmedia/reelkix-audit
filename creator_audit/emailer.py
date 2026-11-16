"""Email sending: SMTP when configured, otherwise .eml files in data/outbox.

Every email gets a row in `emails` with a token used for the open pixel, click
redirects and (for outreach) the one-click unsubscribe link.
"""
from __future__ import annotations

import re
import smtplib
import urllib.parse
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

from . import config, db

OUTREACH_KINDS = {"outreach", "outreach_followup"}
UNTRACKED = {"weekly", "alert"}


class SendBlocked(Exception):
    pass


def is_dnc(email: str | None, channel_id: str | None = None) -> bool:
    if email and db.q1("SELECT 1 FROM dnc WHERE lower(email)=lower(?)", (email,)):
        return True
    if channel_id and db.q1("SELECT 1 FROM dnc WHERE channel_id=?", (channel_id,)):
        return True
    return False


def _track_links(html: str, tok: str) -> str:
    def sub(m):
        url = m.group(1)
        if url.startswith(f"{config.BASE_URL}/u/") or url.startswith("mailto:"):
            return m.group(0)
        return f'href="{config.BASE_URL}/c/{tok}?u={urllib.parse.quote(url, safe="")}"'
    return re.sub(r'href="([^"]+)"', sub, html)


def _footer_html(kind: str, tok: str) -> str:
    if kind in OUTREACH_KINDS:
        return (f'<hr style="border:none;border-top:1px solid #ddd;margin:28px 0 12px">'
                f'<p style="font-size:12px;color:#888;line-height:1.5">You got this because your channel came up in our '
                f'scan of public YouTube channels and your email is published on your own site. One email plus at most '
                f'{config.OUTREACH_MAX_FOLLOWUPS} follow-up, then we stop.<br>'
                f'<a href="{config.BASE_URL}/u/{tok}" style="color:#888">Unsubscribe</a> · {config.BRAND} · {config.BUSINESS_ADDRESS}</p>')
    if kind in UNTRACKED:
        return ""
    return (f'<hr style="border:none;border-top:1px solid #ddd;margin:28px 0 12px">'
            f'<p style="font-size:12px;color:#888">{config.BRAND} · {config.BUSINESS_ADDRESS} · '
            f'<a href="{config.BASE_URL}/delete-my-data" style="color:#888">Delete my data</a></p>')


def _footer_text(kind: str, tok: str) -> str:
    if kind in OUTREACH_KINDS:
        return f"\n\n--\nUnsubscribe: {config.BASE_URL}/u/{tok}\n{config.BRAND}, {config.BUSINESS_ADDRESS}\n"
    if kind in UNTRACKED:
        return ""
    return f"\n\n--\n{config.BRAND}, {config.BUSINESS_ADDRESS}\n"


def send(kind: str, to: str, subject: str, html: str, text: str, *, attachments: list[Path] | None = None,
         prospect_id: int | None = None, job_id: int | None = None, order_id: int | None = None,
         channel_id: str | None = None) -> int:
    if kind in OUTREACH_KINDS and is_dnc(to, channel_id):
        raise SendBlocked(f"{to} is on the do-not-contact list")
    tok = db.token(16)
    eid = db.insert("emails", {"token": tok, "kind": kind, "to_email": to, "subject": subject,
                               "prospect_id": prospect_id, "job_id": job_id, "order_id": order_id,
                               "status": "pending", "created_at": db.iso()})
    body_html = html
    if kind not in UNTRACKED:
        body_html = _track_links(body_html, tok)
        body_html += f'<img src="{config.BASE_URL}/o/{tok}.gif" width="1" height="1" alt="" style="display:none">'
    body_html = (f'<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:15px;'
                 f'line-height:1.6;color:#1a1a1a;max-width:560px">{body_html}{_footer_html(kind, tok)}</div>')

    msg = EmailMessage()
    msg["From"] = config.OUTREACH_FROM if kind in OUTREACH_KINDS else config.MAIL_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain="reelkix.com")
    if kind in OUTREACH_KINDS:
        msg["List-Unsubscribe"] = f"<{config.BASE_URL}/u/{tok}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(text + _footer_text(kind, tok))
    msg.add_alternative(body_html, subtype="html")
    for a in attachments or []:
        msg.add_attachment(Path(a).read_bytes(), maintype="application", subtype="pdf", filename=Path(a).name)

    try:
        if config.SMTP_HOST:
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as s:
                if config.SMTP_TLS:
                    s.starttls()
                if config.SMTP_USER:
                    s.login(config.SMTP_USER, config.SMTP_PASSWORD)
                s.send_message(msg)
        else:
            safe = re.sub(r"[^\w.@-]", "_", to)
            (config.OUTBOX_DIR / f"{eid:05d}_{kind}_{safe}.eml").write_bytes(bytes(msg))
        db.update("emails", eid, {"status": "sent"})
    except Exception as e:  # noqa: BLE001
        db.update("emails", eid, {"status": "failed", "error": str(e)[:500]})
        raise
    return eid


def alert(subject: str, text: str) -> None:
    if config.ADMIN_EMAIL:
        try:
            send("alert", config.ADMIN_EMAIL, f"[{config.PRODUCT_NAME}] {subject}", f"<pre>{text}</pre>", text)
        except Exception:  # noqa: BLE001
            pass
