"""IMAP bounce watcher.

Polls the sending mailbox (QQ) for bounce mails ("来自qq.com的退信" and
similar). When a recently-sent verification code is found to have bounced,
it is marked failed and its codes are invalidated, so the registration page
can tell the user the e-mail address is wrong.

IMAP reuses the SMTP credentials; only host/port/ssl are configurable
(imap_host defaults to imap.<smtp domain>, port 993, SSL on).
"""
from __future__ import annotations

import asyncio
import email as emaillib
import imaplib
import logging
import re
import time
from email.header import decode_header

from . import db as dbmod

log = logging.getLogger("kitechat.bounce")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PENDING_WINDOW = 900  # only code sends within the last 15 minutes matter
_POLL_INTERVAL = 15
_WATCH_WINDOW = 24 * 3600


def _imap_cfg() -> dict:
    db = dbmod.get_db()
    smtp_host = db.get_config("smtp_host").strip()
    host = db.get_config("imap_host").strip()
    if not host and smtp_host:
        domain = smtp_host.split(".", 1)[-1] if "." in smtp_host else smtp_host
        host = f"imap.{domain}"
    return {
        "host": host,
        "port": int(db.get_config("imap_port") or 993),
        "ssl": db.get_config("imap_ssl") != "0",
        "user": db.get_config("smtp_user").strip(),
        "password": db.get_config("smtp_pass").strip(),
    }


def _decode(value) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(enc or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def _is_bounce(subject: str) -> bool:
    return ("退信" in subject or "undeliver" in subject.lower()
            or "returned mail" in subject.lower()
            or "delivery failure" in subject.lower()
            or "mail delivery failed" in subject.lower())


def _extract_recipient(msg) -> str:
    """Best-effort: pull the failed recipient address out of a bounce mail."""
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct in ("text/plain", "text/html"):
                try:
                    payload = part.get_payload(decode=True) or b""
                    text += payload.decode(part.get_content_charset() or "utf-8",
                                           errors="replace")
                except Exception:  # noqa: BLE001
                    continue
    else:
        try:
            payload = msg.get_payload(decode=True) or b""
            text = payload.decode(msg.get_content_charset() or "utf-8",
                                  errors="replace")
        except Exception:  # noqa: BLE001
            text = str(msg.get_payload(decode=False))
    header_blob = " ".join(str(msg.get(h, "")) for h in ("To", "Subject"))
    text = header_blob + "\n" + text
    # prefer addresses explicitly labelled as the failed recipient
    labelled = re.search(
        r"(?:收件人|Final-?Recipient|Original-?Recipient|<([^<>@\s]+@[^<>@\s]+)>)\s*[:：]?\s*"
        r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", text)
    if labelled:
        return (labelled.group(2) or labelled.group(1)).lower()
    found = _EMAIL_RE.findall(text)
    cfg = _imap_cfg()
    me = cfg["user"].lower()
    others = [a.lower() for a in found if a.lower() != me]
    if len(set(others)) == 1:
        return others[0]
    return ""


def _poll_once() -> list[str]:
    """One IMAP poll. Returns emails newly marked as bounced."""
    cfg = _imap_cfg()
    if not (cfg["host"] and cfg["user"] and cfg["password"]):
        return []
    db = dbmod.get_db()
    conn = (imaplib.IMAP4_SSL(cfg["host"], cfg["port"]) if cfg["ssl"]
            else imaplib.IMAP4(cfg["host"], cfg["port"]))
    bounced: list[str] = []
    try:
        conn.login(cfg["user"], cfg["password"])
        conn.select("INBOX", readonly=True)
        typ, data = conn.search(None, "UNSEEN")
        if typ != "OK" or not data[0]:
            return []
        for mid in data[0].split()[-20:]:
            typ, msg_data = conn.fetch(mid, "(RFC822)")
            if typ != "OK" or not msg_data or msg_data[0] is None:
                continue
            raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
            msg = emaillib.message_from_bytes(raw)
            subject = _decode(msg.get("Subject", ""))
            if not _is_bounce(subject):
                continue
            recipient = _extract_recipient(msg)
            if not recipient:
                continue
            # did we recently send a code to this address?
            rows = db.conn.execute(
                "SELECT email FROM sent_codes WHERE email=? AND bounced=0 "
                "AND sent_at > ? ORDER BY sent_at DESC",
                (recipient, time.time() - _PENDING_WINDOW)).fetchall()
            if not rows:
                continue
            db.conn.execute(
                "UPDATE sent_codes SET bounced=1 WHERE email=? AND sent_at > ?",
                (recipient, time.time() - _PENDING_WINDOW))
            # invalidate the still-valid codes for that address
            db.conn.execute(
                "UPDATE codes SET used=1 WHERE target=? AND used=0", (recipient,))
            db.conn.commit()
            log.info("bounce detected for %s (subject=%r)", recipient, subject[:40])
            bounced.append(recipient)
    except Exception as e:  # noqa: BLE001
        log.warning("bounce watcher error: %s", e)
    finally:
        try:
            conn.close()
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
    return bounced


def check_bounce(email: str) -> bool:
    """True when a recent send to this address has bounced."""
    db = dbmod.get_db()
    row = db.conn.execute(
        "SELECT 1 FROM sent_codes WHERE email=? AND bounced=1 AND sent_at > ? "
        "LIMIT 1", (email, time.time() - _PENDING_WINDOW)).fetchone()
    return row is not None


class BounceWatcher:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                await loop.run_in_executor(None, _poll_once)
                self._cleanup(loop)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("bounce watcher loop error: %s", e)
            await asyncio.sleep(_POLL_INTERVAL)

    def _cleanup(self, loop) -> None:
        """Drop send records older than the watch window."""
        db = dbmod.get_db()
        db.conn.execute("DELETE FROM sent_codes WHERE sent_at < ?",
                        (time.time() - _WATCH_WINDOW,))
        db.conn.commit()


watcher = BounceWatcher()
