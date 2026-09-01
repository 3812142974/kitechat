"""SMTP email delivery for verification codes (stdlib smtplib, thread pool).

Config comes from DB (smtp_host/smtp_port/smtp_ssl/smtp_user/smtp_pass/
smtp_sender_name). Sending runs in an executor so the event loop never blocks.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

from . import db as dbmod

log = logging.getLogger("kitechat.smtp")

# Addresses on these domains are NEVER real mailboxes (test fixtures).
# Sending to them only produces bounce mail flooding the operator inbox,
# so the server refuses outright. Tests must inject codes into the DB
# directly instead of triggering real delivery.
_TEST_DOMAINS = (
    "test.local", "example.com", "example.org", "example.net",
    "invalid", "localhost", "test",
)


def _is_test_address(to_addr: str) -> bool:
    addr = to_addr.strip().lower()
    domain = addr.rsplit("@", 1)[-1] if "@" in addr else addr
    return any(domain == d or domain.endswith("." + d) for d in _TEST_DOMAINS)


def _smtp_cfg() -> dict:
    db = dbmod.get_db()
    return {
        "host": db.get_config("smtp_host").strip(),
        "port": int(db.get_config("smtp_port") or 465),
        "ssl": db.get_config("smtp_ssl") == "1",
        "user": db.get_config("smtp_user").strip(),
        "password": db.get_config("smtp_pass").strip(),
        "sender_name": db.get_config("smtp_sender_name") or "KiteChat",
    }


def configured() -> bool:
    c = _smtp_cfg()
    return bool(c["host"] and c["user"] and c["password"])


def _send_blocking(to_addr: str, subject: str, html_body: str) -> tuple[bool, str]:
    # hard block: never deliver to test-fixture domains (would bounce and
    # spam the operator inbox)
    if _is_test_address(to_addr):
        log.warning("refusing to mail test-fixture address %s", to_addr)
        return False, "该邮箱为测试保留域名，不允许发送"
    cfg = _smtp_cfg()
    if not (cfg["host"] and cfg["user"] and cfg["password"]):
        return False, "SMTP 未配置（请在后台填写发件邮箱/授权码/服务器）"
    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((cfg["sender_name"], cfg["user"]))
    msg["To"] = to_addr
    try:
        if cfg["ssl"]:
            ctx = ssl.create_default_context()
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=20,
                                      context=ctx)
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=20)
            server.starttls(context=ssl.create_default_context())
        with server:
            server.login(cfg["user"], cfg["password"])
            server.sendmail(cfg["user"], [to_addr], msg.as_string())
        return True, "ok"
    except smtplib.SMTPAuthenticationError as e:
        return False, f"SMTP 认证失败（检查授权码）: {e.smtp_error!r}"
    except smtplib.SMTPDataError as e:
        code = getattr(e, "smtp_code", 0)
        if code == 550 or b"non-existent" in getattr(e, "smtp_error", b""):
            return False, "邮箱地址不存在，请检查填写是否正确"
        return False, f"SMTP 发送失败: {e}"
    except smtplib.SMTPRecipientsRefused:
        return False, "邮箱地址不存在或已被拒绝，请检查填写是否正确"
    except Exception as e:  # noqa: BLE001 - surface to admin UI
        return False, f"SMTP 发送失败: {e}"


async def send_mail(to_addr: str, subject: str, html_body: str) -> tuple[bool, str]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _send_blocking, to_addr, subject, html_body
    )


async def send_code_mail(to_addr: str, code: str, app_name: str = "KiteChat",
                         purpose: str = "注册") -> tuple[bool, str]:
    subject = f"【{app_name}】{purpose}验证码"
    html = f"""
<div style="font-family:'Segoe UI',system-ui,sans-serif;max-width:520px;margin:0 auto;
padding:32px;background:#f7f7fb;border-radius:16px">
  <div style="background:#fff;border-radius:16px;padding:32px;
  box-shadow:0 2px 12px rgba(80,70,200,.08)">
    <h2 style="margin:0 0 8px;color:#1c1b2e;font-size:20px">{app_name}</h2>
    <p style="color:#6b6880;font-size:14px;margin:0 0 24px">
      您正在进行<b>{purpose}</b>操作，验证码 10 分钟内有效。</p>
    <div style="font-size:32px;font-weight:700;letter-spacing:8px;text-align:center;
    color:#5b4df0;background:#f0eeff;border-radius:12px;padding:18px 0">{code}</div>
    <p style="color:#9b98ad;font-size:12px;margin-top:24px">
      如非本人操作，请忽略此邮件。此邮件由系统自动发送。</p>
  </div>
</div>"""
    return await send_mail(to_addr, subject, html)


async def send_test_mail(to_addr: str) -> tuple[bool, str]:
    html = """
<div style="font-family:'Segoe UI',system-ui,sans-serif;padding:32px;background:#f7f7fb">
<div style="background:#fff;border-radius:16px;padding:32px;max-width:480px">
<h2 style="color:#1c1b2e">KiteChat SMTP 测试邮件</h2>
<p style="color:#6b6880">如果你收到这封邮件，说明 SMTP 配置可用 ✅</p>
</div></div>"""
    return await send_mail(to_addr, "KiteChat SMTP 测试", html)
