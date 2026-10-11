"""Contact form: validation, spam protection, storage, and email delivery.

Every message is stored in contact_messages first, then emailed to CONTACT_TO over SMTP.
If email isn't configured or fails, the message stays 'pending'/'failed' and is retried
by the next submission and by the daily sync. Message contents and addresses are never logged.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

import psycopg

from . import config

log = logging.getLogger(__name__)

TOPICS = {
    "correction": "Report wrong or missing information",
    "feedback": "Feedback about the site",
    "feature": "Suggest an improvement",
    "security": "Report a security problem",
    "other": "Something else",
}
MIN_FILL_SECONDS = 3          # humans take longer than this to write a message
TOKEN_MAX_AGE = 24 * 3600
PER_CLIENT_PER_HOUR = 5
SITE_WIDE_PER_DAY = 200
MAX_ATTEMPTS = 5
# Privacy policy commitments (see /privacy): messages are deleted after RETENTION_DAYS, and the
# keyed IP hash used for rate limiting is cleared after CLIENT_HASH_DAYS.
RETENTION_DAYS = config.CONTACT_RETENTION_DAYS
CLIENT_HASH_DAYS = 7

_EMAIL_RE = re.compile(r"^[^@\s<>\"'(),;:\\\[\]]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}$")


def _key() -> bytes:
    base = config.SECRET_KEY or ("db:" + config.DATABASE_URL)
    return hashlib.sha256(("wapeople-contact:" + base).encode()).digest()


def form_token(now: float | None = None) -> str:
    """Signed render time; checked on submit to stop instant bot posts and stale replays."""
    ts = str(int(now if now is not None else time.time()))
    sig = hmac.new(_key(), ts.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{ts}.{sig}"


def token_ok(token: str, now: float | None = None) -> bool:
    try:
        ts, sig = (token or "").split(".", 1)
        age = (now if now is not None else time.time()) - int(ts)
    except ValueError:
        return False
    good = hmac.new(_key(), ts.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, good) and MIN_FILL_SECONDS <= age <= TOKEN_MAX_AGE


def client_hash(ip: str) -> str:
    """Keyed hash of the client IP: lets us rate-limit without storing the IP."""
    return hmac.new(_key(), (ip or "unknown").encode(), hashlib.sha256).hexdigest()[:32]


def _one_line(s: str, limit: int) -> str:
    return " ".join((s or "").replace("\r", " ").replace("\n", " ").split())[:limit]


def validate(form: dict) -> tuple[dict, dict]:
    """Returns (clean values, errors by field)."""
    errors: dict[str, str] = {}
    topic = form.get("topic") or ""
    if topic not in TOPICS:
        errors["topic"] = "Choose what your message is about."
    message = (form.get("message") or "").replace("\r\n", "\n").strip()
    if len(message) < 20:
        errors["message"] = "Please describe the issue in a little more detail (at least 20 characters)."
    elif len(message) > 5000:
        errors["message"] = "Please keep your message under 5,000 characters."
    reply_to = _one_line(form.get("email") or "", 254)
    if reply_to and not _EMAIL_RE.match(reply_to):
        errors["email"] = "That email address doesn't look right. You can also leave it blank."
    clean = {
        "topic": topic,
        "page": _one_line(form.get("page") or "", 300) or None,
        "name": _one_line(form.get("name") or "", 100) or None,
        "reply_to": reply_to or None,
        "message": message,
    }
    return clean, errors


def rate_limited(conn: psycopg.Connection, chash: str) -> bool:
    row = conn.execute(
        "SELECT count(*) FILTER (WHERE client_hash = %s AND created_at > now() - interval '1 hour') AS mine,"
        " count(*) FILTER (WHERE created_at > now() - interval '1 day') AS all_day FROM contact_messages",
        (chash,),
    ).fetchone()
    return row["mine"] >= PER_CLIENT_PER_HOUR or row["all_day"] >= SITE_WIDE_PER_DAY


def save(conn: psycopg.Connection, clean: dict, chash: str) -> int:
    return conn.execute(
        "INSERT INTO contact_messages (topic, page, name, reply_to, message, client_hash)"
        " VALUES (%(topic)s, %(page)s, %(name)s, %(reply_to)s, %(message)s, %(client_hash)s) RETURNING id",
        {**clean, "client_hash": chash},
    ).fetchone()["id"]


def email_configured() -> bool:
    return bool(config.CONTACT_TO and config.SMTP_HOST and config.SMTP_FROM)


def build_email(msg: dict) -> EmailMessage:
    topic = TOPICS.get(msg["topic"], msg["topic"])
    subject = f"[WA People] {topic}"
    if msg.get("page"):
        subject += f" — {_one_line(msg['page'], 80)}"
    page = msg.get("page") or "(not given)"
    if page.startswith("/") and config.SITE_URL:
        page = config.SITE_URL + page
    e = EmailMessage()
    e["Subject"] = subject
    e["From"] = formataddr(("WA People contact form", config.SMTP_FROM))
    e["To"] = config.CONTACT_TO
    if msg.get("reply_to"):
        e["Reply-To"] = formataddr((_one_line(msg.get("name") or "", 100), msg["reply_to"]))
    e["Message-ID"] = make_msgid(domain="wapeople.local")
    e.set_content(
        f"New message from the WA People contact form (#{msg['id']})\n\n"
        f"Topic:    {topic}\n"
        f"Page:     {page}\n"
        f"Name:     {msg.get('name') or '(not given)'}\n"
        f"Reply to: {msg.get('reply_to') or '(no email given)'}\n"
        f"Received: {msg['created_at']:%Y-%m-%d %H:%M %Z}\n\n"
        f"{msg['message']}\n"
    )
    return e


def _send(email: EmailMessage) -> None:
    context = ssl.create_default_context()
    if config.SMTP_PORT == 465:
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30, context=context) as s:
            if config.SMTP_USER:
                s.login(config.SMTP_USER, config.SMTP_PASSWORD or "")
            s.send_message(email)
    else:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as s:
            if config.SMTP_STARTTLS:
                s.starttls(context=context)
            if config.SMTP_USER:
                s.login(config.SMTP_USER, config.SMTP_PASSWORD or "")
            s.send_message(email)


def send_pending(conn: psycopg.Connection, limit: int = 20) -> dict:
    """Email stored messages that haven't been delivered yet."""
    if not email_configured():
        return {"contact_sent": 0, "contact_note": "email not configured"}
    rows = conn.execute(
        "SELECT * FROM contact_messages WHERE email_status <> 'sent' AND attempts < %s"
        " ORDER BY created_at LIMIT %s",
        (MAX_ATTEMPTS, limit),
    ).fetchall()
    conn.commit()
    sent = failed = 0
    for r in rows:
        try:
            _send(build_email(r))
        except Exception as e:  # keep it for the next retry; record only the error type
            failed += 1
            conn.execute(
                "UPDATE contact_messages SET email_status = 'failed', attempts = attempts + 1,"
                " email_error = %s WHERE id = %s",
                (f"{type(e).__name__}: {str(e)[:200]}", r["id"]),
            )
            log.warning("contact message %s not emailed: %s", r["id"], type(e).__name__)
        else:
            sent += 1
            conn.execute(
                "UPDATE contact_messages SET email_status = 'sent', attempts = attempts + 1,"
                " email_error = NULL, emailed_at = now() WHERE id = %s",
                (r["id"],),
            )
        conn.commit()
    return {"contact_sent": sent, "contact_failed": failed}


def apply_retention(conn: psycopg.Connection) -> dict:
    """Enforce the privacy policy: clear old rate-limit hashes, delete messages past retention."""
    cleared = conn.execute(
        "UPDATE contact_messages SET client_hash = '' WHERE client_hash <> ''"
        " AND created_at < now() - make_interval(days => %s)",
        (CLIENT_HASH_DAYS,),
    ).rowcount
    deleted = conn.execute(
        "DELETE FROM contact_messages WHERE created_at < now() - make_interval(days => %s)",
        (RETENTION_DAYS,),
    ).rowcount
    conn.commit()
    return {"contact_hashes_cleared": cleared, "contact_deleted": deleted}
