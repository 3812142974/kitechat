"""aiohttp web layer: REST API + client WS + OneBot reverse WS + WebUI static."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import secrets
import string
import time
from typing import Any

from aiohttp import WSMsgType, web
from PIL import ImageOps

from . import config as cfg
from . import db as dbmod
from . import exporter
from . import mailer
from . import onebot as ob
from .bot_bridge import bridge
from .bounce import watcher as bounce_watcher
from . import bounce
from .hub import hub, _session_view

log = logging.getLogger("kitechat.web")

EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
USERNAME_RE = re.compile(r"^[\w\u4e00-\u9fa5-]{2,20}$")


# ============================================================ helpers
def json_ok(data: Any = None) -> web.Response:
    return web.json_response({"status": "ok", "data": data})


def json_err(msg: str, code: int = 400) -> web.Response:
    return web.json_response({"status": "failed", "msg": msg}, status=code)


async def read_json(request: web.Request) -> dict:
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError
        return data
    except Exception:  # noqa: BLE001
        return {}


def make_token_payload(user_id: int) -> str:
    """Token = base64url(payload).json + '.' + hmac-ish signature via secrets db."""
    tok = secrets.token_urlsafe(32)
    db = dbmod.get_db()
    db.set_config(f"token:{tok}", str(user_id))
    return tok


def resolve_token(token: str) -> int | None:
    if not token:
        return None
    val = dbmod.get_db().get_config(f"token:{token}")
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def require_admin(request: web.Request) -> str | None:
    """Returns error message or None if admin auth OK."""
    want = dbmod.get_db().get_config("admin_token")
    got = (request.headers.get("X-Admin-Token")
           or request.rel_url.query.get("token") or "")
    if not want or got != want:
        return "管理员令牌无效"
    return None


def display_name(row) -> str:
    """nickname if set, otherwise username."""
    try:
        nick = row["nickname"]
    except (IndexError, KeyError):
        nick = ""
    return nick or row["username"]


def user_public(row) -> dict:
    return {
        "user_id": row["id"],
        "nickname": display_name(row),
        "username": row["username"],
        "email": row["email"],
        "avatar_color": row["avatar_color"],
        "avatar": _avatar_url(row),
        "signature": row["signature"],
        "online": hub.online(row["id"]),
    }


def _avatar_url(row) -> str:
    """Public URL of the user's custom avatar ('' = colored initial)."""
    from .hub import avatar_url
    return avatar_url(row)


AVATAR_DIR = os.path.join(cfg.DATA_DIR, "avatars")
MAX_AVATAR_BYTES = 4 * 1024 * 1024


def _avatar_path(user_id: int) -> str:
    return os.path.join(AVATAR_DIR, f"{user_id}.jpg")


# ============================================================ auth API
async def api_send_code(request: web.Request) -> web.Response:
    data = await read_json(request)
    email = str(data.get("email", "")).strip().lower()
    purpose = data.get("purpose", "register")
    if purpose not in ("register", "reset"):
        return json_err("无效用途")
    if not EMAIL_RE.match(email):
        return json_err("邮箱格式不正确")
    # domain whitelist (configured in admin → SMTP card). Empty list = allow all.
    wl_raw = dbmod.get_db().get_config("email_domain_whitelist")
    wl = [d.strip().lower() for d in wl_raw.split(",") if d.strip()]
    if wl and email.split("@", 1)[1] not in wl:
        return json_err("该邮箱域名不在允许注册的范围内")
    if not mailer.configured():
        return json_err("服务端尚未配置 SMTP，请联系管理员在后台配置邮箱")
    db = dbmod.get_db()
    if purpose == "register":
        if db.get_user_by_email(email):
            return json_err("该邮箱已注册")
    # a recent send to this address already bounced -> reject fast
    if bounce.check_bounce(email):
        return json_err("验证码发送失败：该邮箱地址不存在，请检查填写是否正确")
    code = "".join(secrets.choice(string.digits) for _ in range(6))
    db.add_code(purpose, email, code, {}, ttl=600)
    ok, msg = await mailer.send_code_mail(email, code, purpose="注册")
    if not ok:
        db.conn.execute("DELETE FROM codes WHERE target=? AND code=?",
                        (email, code))
        db.conn.commit()
        if "不存在" in msg or "已被拒绝" in msg:
            return json_err("验证码发送失败：请检查邮箱填写正确")
        return json_err(f"验证码发送失败：{msg}")
    # record the send so the IMAP bounce watcher can match a bounce back
    db.conn.execute(
        "INSERT INTO sent_codes(email, purpose, sent_at, bounced) "
        "VALUES(?,?,?,0)", (email, purpose, time.time()))
    db.conn.commit()
    return json_ok({"cooldown": 60})


async def api_check_bounce(request: web.Request) -> web.Response:
    """Client polls this after send-code: has the send bounced (IMAP)?"""
    email = str(request.query.get("email", "")).strip().lower()
    if not EMAIL_RE.match(email):
        return json_err("邮箱格式不正确")
    return json_ok({"bounced": bounce.check_bounce(email)})


async def api_register(request: web.Request) -> web.Response:
    data = await read_json(request)
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    password2 = str(data.get("password2", ""))
    email = str(data.get("email", "")).strip().lower()
    code = str(data.get("code", "")).strip()

    if not USERNAME_RE.match(username):
        return json_err("用户名需为 2-20 位中英文/数字/下划线")
    if len(password) < 6 or len(password) > 64:
        return json_err("密码长度需为 6-64 位")
    if password != password2:
        return json_err("两次输入的密码不一致")
    if not EMAIL_RE.match(email):
        return json_err("邮箱格式不正确")
    if not re.fullmatch(r"\d{6}", code):
        return json_err("验证码格式不正确")

    db = dbmod.get_db()
    if db.get_user_by_name(username):
        return json_err("用户名已被占用")
    if db.get_user_by_email(email):
        return json_err("该邮箱已注册")
    payload = db.pop_code("register", email, code)
    if payload is None:
        return json_err("验证码错误或已过期，请重新获取")

    color = secrets.choice([
        "#7C6CF0", "#4C9BE8", "#F08C6C", "#4CC9A0",
        "#E86CA8", "#B8863B", "#6C8BF0", "#8A6CF0",
    ])
    uid = db.create_user(username, cfg.hash_password(password), email)
    db.conn.execute("UPDATE users SET avatar_color=? WHERE id=?", (color, uid))
    db.conn.commit()
    # welcome AI session
    sid = db.create_session("ai", "新对话", uid)
    db.add_message(sid, 0, "Kite AI", "text",
                   [{"type": "text", "data": {"text":
                    f"你好 {username}！我是 KiteChat 的 AI 助手，有什么可以帮你的吗？"}}],
                   f"你好 {username}！")
    db.update_session_preview(sid, f"你好 {username}！", time.time())
    token = make_token_payload(uid)
    return json_ok({"token": token, "user": user_public(db.get_user(uid))})


async def api_login(request: web.Request) -> web.Response:
    data = await read_json(request)
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    db = dbmod.get_db()
    user = db.get_user_by_name(username) or db.get_user_by_email(username.lower())
    if user is None or not cfg.verify_password(password, user["password_hash"]):
        return json_err("用户名或密码错误", 401)
    token = make_token_payload(user["id"])
    return json_ok({"token": token, "user": user_public(user)})


async def api_client_settings(request: web.Request) -> web.Response:
    """Public settings pushed to every client (no auth needed, no secrets)."""
    db = dbmod.get_db()
    try:
        interval = max(2.0, min(float(db.get_config("client_reconnect_interval") or 5), 600.0))
    except (TypeError, ValueError):
        interval = 5.0
    return json_ok({
        "reconnect_interval": interval,
        "app_name": db.get_config("app_name") or "KiteChat",
    })


async def api_client_diag(request: web.Request) -> web.Response:
    """Public: one-time Android shell diagnostics (window insets, startup
    timing, final URL). Logged server-side so layout issues on specific
    devices can be fixed with real numbers instead of guesses."""
    try:
        data = await read_json(request)
    except Exception:
        return json_err("bad json")
    log.info("[client-diag] %s", json.dumps(data, ensure_ascii=False)[:1200])
    return json_ok({"ok": True})


async def api_session_info(request: web.Request) -> web.Response:
    db = dbmod.get_db()
    return json_ok({
        "app_name": db.get_config("app_name") or "KiteChat",
        "ws_address": cfg.public_ws_address(),
        "smtp_configured": mailer.configured(),
    })


# ============================================================ avatar API
def _require_user_token(request: web.Request):
    """Resolve the caller's login token -> user row (or None)."""
    tok = (request.headers.get("X-Auth-Token")
           or request.rel_url.query.get("token") or "")
    uid = resolve_token(tok)
    if not uid:
        return None
    return dbmod.get_db().get_user(uid)


async def api_avatar_upload(request: web.Request) -> web.Response:
    """POST {image_base64} — center-crop to square, resize 256x256 JPEG.
    The caller's token decides WHOSE avatar is replaced."""
    user = _require_user_token(request)
    if not user:
        return json_err("未登录或登录已过期", 401)
    data = await read_json(request)
    img_b64 = str(data.get("image_base64", ""))
    # accept "data:image/png;base64,...." and raw base64 alike
    if "," in img_b64[:64] and img_b64.lstrip().lower().startswith("data:"):
        img_b64 = img_b64.split(",", 1)[1]
    raw = base64.b64decode(img_b64, validate=False) if img_b64 else b""
    if not raw:
        return json_err("没有收到图片数据")
    if len(raw) > MAX_AVATAR_BYTES:
        return json_err("图片太大（限 4MB 以内）")
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        return json_err("无法识别的图片格式")
    # EXIF orientation fix, then center-crop to square
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:
        pass
    im = im.convert("RGB")
    w, h = im.size
    side = min(w, h)
    im = im.crop(((w - side) // 2, (h - side) // 2,
                  (w + side) // 2, (h + side) // 2))
    im = im.resize((256, 256), Image.LANCZOS)
    os.makedirs(AVATAR_DIR, exist_ok=True)
    dest = _avatar_path(user["id"])
    im.save(dest, "JPEG", quality=88)
    db = dbmod.get_db()
    db.update_avatar(user["id"], f"{user['id']}.jpg")
    # notify everyone connected so lists/profiles refresh immediately
    await hub.broadcast({
        "post_type": "notice", "notice_type": "avatar_changed",
        "user_id": user["id"],
        "avatar": _avatar_url(db.get_user(user["id"])),
    })
    return json_ok({"user": user_public(db.get_user(user["id"]))})


async def avatar_file(request: web.Request) -> web.Response:
    """GET /avatar/{uid}.jpg — public (avatars are visible to friends)."""
    name = request.match_info["name"]
    if not re.fullmatch(r"\d+\.jpg", name):
        return json_err("无效头像", 404)
    path = os.path.join(AVATAR_DIR, name)
    if not os.path.isfile(path):
        return json_err("头像不存在", 404)
    return web.FileResponse(path, headers={
        "Cache-Control": "public, max-age=86400"})


# ============================================================ admin API
async def admin_get_config(request: web.Request) -> web.Response:
    err = require_admin(request)
    if err:
        return json_err(err, 401)
    db = dbmod.get_db()
    out = db.all_config()
    # mask secrets
    if out.get("smtp_pass"):
        out["smtp_pass_masked"] = "*" * min(10, len(out["smtp_pass"]))
        out["smtp_pass"] = ""
    out["smtp_ready"] = mailer.configured()
    out["bridge_connected"] = bridge.any_connected
    out["online_users"] = len(hub.conns)
    out["user_count"] = len(db.list_users())
    out["public_ws"] = cfg.public_ws_address()
    out["lan_ip"] = cfg.lan_ip()
    out["export_scheme"] = db.get_config("export_scheme") or "auto"
    out["ssl_ca_cert_pem"] = db.get_config("ssl_ca_cert_pem") or ""
    out["tls_cert"] = db.get_config("tls_cert") or ""
    out["tls_key"] = db.get_config("tls_key") or ""
    out["tls_enabled"] = db.get_config("tls_enabled") or "1"
    return json_ok(out)


def _normalize_domain_whitelist(raw: str) -> str:
    """Comma-separated list of domains (without '@'), lowercase, deduped."""
    domains = []
    for part in str(raw).replace("\n", ",").split(","):
        d = part.strip().lower().lstrip("@")
        if d and d not in domains:
            domains.append(d)
    return ",".join(domains)


async def admin_set_config(request: web.Request) -> web.Response:
    err = require_admin(request)
    if err:
        return json_err(err, 401)
    data = await read_json(request)
    db = dbmod.get_db()
    editable = {
        "ws_address", "host", "port",
        "smtp_host", "smtp_port", "smtp_ssl", "smtp_user", "smtp_pass",
        "smtp_sender_name", "imap_host", "imap_port", "imap_ssl",
        "email_domain_whitelist",
        "bot_ws_url", "app_ws_url", "app_name",
        "bridge_reconnect_interval", "client_reconnect_interval",
        "tls_enabled",
    }
    changed = []
    for k, v in data.items():
        if k not in editable:
            continue
        if k == "smtp_pass" and not str(v).strip():
            continue  # empty = keep existing
        if k == "email_domain_whitelist":
            v = _normalize_domain_whitelist(str(v))
        if k == "bridge_reconnect_interval":
            try:
                v = max(2.0, min(float(v), 600.0))
            except (TypeError, ValueError):
                continue
        if k == "client_reconnect_interval":
            try:
                v = max(2.0, min(float(v), 600.0))
            except (TypeError, ValueError):
                continue
        if k == "ws_address":
            v = cfg.normalize_ws_url(str(v))
            ok, msg = cfg.validate_ws_url(v)
            if not ok:
                return json_err(msg)
        if k == "port":
            try:
                p = int(v)
                assert 1 <= p <= 65535
            except (ValueError, AssertionError):
                return json_err("端口无效")
            v = str(p)
        db.set_config(k, str(v))
        changed.append(k)
    # Write TLS cert/key files to data/tls/ if provided
    if "tls_cert" in changed or "tls_key" in changed:
        tls_dir = os.path.join(cfg.DATA_DIR, "tls")
        os.makedirs(tls_dir, exist_ok=True)
        if "tls_cert" in changed:
            cert = data.get("tls_cert", "").strip()
            if cert:
                with open(os.path.join(tls_dir, "fullchain.pem"), "w", encoding="utf-8") as f:
                    f.write(cert + "\n")
        if "tls_key" in changed:
            key = data.get("tls_key", "").strip()
            if key:
                with open(os.path.join(tls_dir, "privkey.pem"), "w", encoding="utf-8") as f:
                    f.write(key + "\n")
    if any(k in ("bot_ws_url", "app_ws_url") for k in changed):
        await bridge.apply_config()
    return json_ok({"changed": changed,
                    "public_ws": cfg.public_ws_address()})


async def admin_restart(request: web.Request) -> web.Response:
    """Restart the server process (HTTPS toggle, etc)."""
    err = require_admin(request)
    if err:
        return json_err(err, 401)
    log.info("[admin] server restart requested")
    # Schedule a restart after responding — use os.execv to replace
    # the current process with a fresh one (clean restart, no orphans).
    async def _do_restart():
        await asyncio.sleep(0.5)  # let the response reach the client
        import sys, os
        os.execv(sys.executable, [sys.executable] + sys.argv)
    asyncio.ensure_future(_do_restart())
    return json_ok({"msg": "服务端正在重启"})


async def admin_reconnect(request: web.Request) -> web.Response:
    """Manually trigger a bridge reconnect attempt and report the result."""
    err = require_admin(request)
    if err:
        return json_err(err, 401)
    res = await bridge.reconnect()
    return json_ok({**res,
                    "bridge_connected": bridge.any_connected})


async def admin_test_mail(request: web.Request) -> web.Response:
    err = require_admin(request)
    if err:
        return json_err(err, 401)
    data = await read_json(request)
    to = str(data.get("to", "")).strip()
    if not to:
        to = dbmod.get_db().get_config("smtp_user")
    if not EMAIL_RE.match(to):
        return json_err("收件邮箱无效")
    ok, msg = await mailer.send_test_mail(to)
    return json_ok({"sent": ok, "detail": msg}) if ok else json_err(msg, 500)


async def admin_users(request: web.Request) -> web.Response:
    err = require_admin(request)
    if err:
        return json_err(err, 401)
    db = dbmod.get_db()
    users = []
    for row in db.list_users():
        users.append({
            **user_public(row),
            "created_at": row["created_at"],
            "last_seen": row["last_seen"],
        })
    return json_ok({"users": users})


async def admin_delete_user(request: web.Request) -> web.Response:
    err = require_admin(request)
    if err:
        return json_err(err, 401)
    data = await read_json(request)
    try:
        uid = int(data.get("user_id"))
    except (TypeError, ValueError):
        return json_err("无效 user_id")
    if uid == 1:
        return json_err("不能删除初始账号")
    dbmod.get_db().delete_user(uid)
    hub.conns.pop(uid, None)
    return json_ok({})


import re as _re

VERSION_RE = _re.compile(r"^\d+(\.\d+){0,3}$")


async def admin_export(request: web.Request) -> web.Response:
    err = require_admin(request)
    if err:
        return json_err(err, 401)
    data = await read_json(request)
    target = data.get("target", "all")
    if target not in ("windows", "android", "all"):
        return json_err("target 必须是 windows / android / all")
    ws = cfg.normalize_ws_url(
        str(data.get("ws_address") or "").strip()
        or dbmod.get_db().get_config("ws_address")
    )
    if not ws:
        ws = cfg.public_ws_address()
    version = str(data.get("version") or "").strip()
    if version:
        if not VERSION_RE.match(version):
            return json_err("版本号格式不正确，应为如 1.2.3 的数字形式")
        # remember the last used version for next time
        dbmod.get_db().set_config("export_version", version)
    else:
        version = (dbmod.get_db().get_config("export_version")
                   or cfg.VERSION)
    scheme = str(data.get("scheme") or "").strip().lower()
    if scheme and scheme not in ("auto", "http", "https"):
        return json_err("scheme 必须是 auto / http / https")
    if scheme:
        dbmod.get_db().set_config("export_scheme", scheme)
    else:
        scheme = dbmod.get_db().get_config("export_scheme") or "auto"
    # SSL CA certificate: user may paste PEM content to embed in the APK
    ca_cert = str(data.get("ca_cert") or "").strip()
    ca_cert_path = ""
    if ca_cert and "-----BEGIN" in ca_cert:
        ca_cert_path = os.path.join(cfg.DATA_DIR, "ssl_ca_cert.pem")
        with open(ca_cert_path, "w", encoding="utf-8") as f:
            f.write(ca_cert + "\n")
        dbmod.get_db().set_config("ssl_ca_cert_pem", ca_cert)
    elif ca_cert == "":
        # empty string: clear any previously saved cert
        old = dbmod.get_db().get_config("ssl_ca_cert_pem") or ""
        if old:
            dbmod.get_db().set_config("ssl_ca_cert_pem", "")
            try:
                os.remove(os.path.join(cfg.DATA_DIR, "ssl_ca_cert.pem"))
            except OSError:
                pass
    job = exporter.start_build(target, ws, version, scheme, ca_cert_path)
    return json_ok(job)


async def admin_export_status(request: web.Request) -> web.Response:
    err = require_admin(request)
    if err:
        return json_err(err, 401)
    return json_ok(exporter.jobs_snapshot())


async def admin_export_download(request: web.Request) -> web.Response:
    err = require_admin(request)
    if err:
        return json_err(err, 401)
    target = request.match_info["target"]
    path = exporter.artifact_path(target)
    if not path or not os.path.exists(path):
        return json_err("产物不存在，请先执行导出", 404)
    resp = web.FileResponse(path)
    # FileResponse has no `filename` kwarg in current aiohttp; set the
    # attachment header manually so browsers download instead of displaying
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="{os.path.basename(path)}"')
    return resp


async def api_apk_latest(request: web.Request) -> web.Response:
    """Public endpoint for the in-app updater (no admin token — the client
    itself calls this)."""
    m = exporter.latest_apk_manifest()
    if not m:
        return json_ok({"version": cfg.VERSION, "filename": "", "size": 0})
    return json_ok(m)


async def api_apk_download(request: web.Request) -> web.Response:
    """Public APK download (clients carry no admin token)."""
    path = exporter.artifact_path("android")
    if not path or not os.path.exists(path):
        return json_err("暂无可下载的 APK", 404)
    resp = web.FileResponse(path)
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="{os.path.basename(path)}"')
    return resp


async def admin_logs(request: web.Request) -> web.Response:
    err = require_admin(request)
    if err:
        return json_err(err, 401)
    path = os.path.join(cfg.ROOT, "logs", "server.log")
    lines: list[str] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-200:]
    return json_ok({"lines": [l.rstrip() for l in lines]})


async def admin_login(request: web.Request) -> web.Response:
    data = await read_json(request)
    want = dbmod.get_db().get_config("admin_token")
    if not want or data.get("token") != want:
        return json_err("令牌错误", 401)
    return json_ok({"ok": True})


async def admin_change_token(request: web.Request) -> web.Response:
    """Change the admin token. Requires the CURRENT token (X-Admin-Token)."""
    err = require_admin(request)
    if err:
        return json_err(err, 401)
    data = await read_json(request)
    new_token = str(data.get("new_token", "")).strip()
    if not new_token:
        new_token = secrets.token_urlsafe(16)
    if len(new_token) < 8:
        return json_err("令牌至少 8 个字符")
    if len(new_token) > 128:
        return json_err("令牌太长（最多 128 字符）")
    if any(c.isspace() for c in new_token):
        return json_err("令牌不能包含空格")
    if new_token == dbmod.get_db().get_config("admin_token"):
        return json_err("新令牌与当前令牌相同")
    dbmod.get_db().set_config("admin_token", new_token)
    log.info("admin token changed")
    return json_ok({"token": new_token})


# ============================================================ client WS
class ClientConn:
    def __init__(self, ws: web.WebSocketResponse, user_id: int):
        self.ws = ws
        self.user_id = user_id

    async def send_raw(self, data: str) -> None:
        await self.ws.send_str(data)

    async def send(self, frame: dict) -> None:
        await self.ws.send_str(json.dumps(frame, ensure_ascii=False))


async def ws_client(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    conn: ClientConn | None = None
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    frame = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(frame, dict):
                    continue
                if conn is None:
                    if frame.get("op") != "auth":
                        await ws.send_json({"op": "error", "msg": "请先认证"})
                        continue
                    user_id = resolve_token(str(frame.get("token", "")))
                    if user_id is None:
                        await ws.send_json({"op": "auth_failed", "msg": "令牌无效或已过期"})
                        await ws.close()
                        break
                    # token may point at a user that no longer exists
                    # (account deleted / DB reset) — treat as expired so the
                    # client clears it and shows the login screen, instead of
                    # crashing the handler below
                    if dbmod.get_db().get_user(user_id) is None:
                        dbmod.get_db().conn.execute(
                            "DELETE FROM config WHERE key=?",
                            (f"token:{frame.get('token', '')}",))
                        dbmod.get_db().conn.commit()
                        await ws.send_json({"op": "auth_failed", "msg": "账号不存在，请重新登录"})
                        await ws.close()
                        break
                    conn = ClientConn(ws, user_id)
                    came_online = hub.register(user_id, conn)
                    await _send_auth_ok(conn)
                    if came_online:
                        asyncio.create_task(hub.broadcast_presence(user_id, True))
                    continue
                asyncio.create_task(_handle_client_op(conn, frame))
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSED):
                break
    finally:
        if conn is not None:
            went_offline = hub.unregister(conn.user_id, conn)
            if went_offline:
                asyncio.create_task(hub.broadcast_presence(conn.user_id, False))
    return ws


async def _send_auth_ok(conn: ClientConn) -> None:
    db = dbmod.get_db()
    user = db.get_user(conn.user_id)
    sessions = [
        _session_view(s, conn.user_id, hub)
        for s in db.list_user_sessions(conn.user_id)
    ]
    friends = []
    for row in db.list_friends(conn.user_id):
        if row["status"] == "accepted":
            friends.append({
                "user_id": row["id"], "nickname": row["username"],
                "avatar_color": row["avatar_color"],
                "avatar": _avatar_url(row),
                "signature": row["signature"],
                "online": hub.online(row["id"]),
            })
    requests = [
        {"user_id": r["id"], "nickname": r["username"],
         "avatar_color": r["avatar_color"], "avatar": _avatar_url(r),
         "time": r["created_at"]}
        for r in db.list_requests(conn.user_id)
    ]
    await conn.send({
        "op": "auth_ok",
        "user": user_public(user),
        "sessions": sessions,
        "friends": friends,
        "requests": requests,
        "server": {
            "app_name": db.get_config("app_name") or "KiteChat",
            "bridge_connected": bridge.any_connected,
        },
    })


async def _handle_client_op(conn: ClientConn, frame: dict) -> None:
    op = frame.get("op")
    req_id = frame.get("req_id")
    db = dbmod.get_db()

    async def result(status: str, data: Any = None, msg: str = "") -> None:
        await conn.send({"op": "result", "req_id": req_id, "status": status,
                         "data": data, "msg": msg})

    try:
        if op == "ping":
            await result("ok", {"pong": True})

        elif op == "reconnect_bridge":
            # users can manually retry the OneBot app connection from the
            # client (shown on the "WS 未连接" banner)
            res = await bridge.reconnect()
            if res["connected"]:
                await result("ok", {"bridge_connected": bridge.any_connected},
                             msg=res["msg"])
            else:
                await result("failed",
                             {"bridge_connected": bridge.any_connected},
                             msg=res["msg"])

        elif op == "message":
            session_id = str(frame.get("session_id", ""))
            resp = await hub.handle_user_message(conn.user_id, session_id,
                                                 frame.get("message"))
            if resp.get("status") == "ok":
                await result("ok", resp.get("data"))
            else:
                await result("failed", msg=resp.get("msg", ""))

        elif op == "create_session":
            kind = frame.get("kind", "ai")
            name = str(frame.get("name", "")).strip()[:30] or "新对话"
            if kind != "ai":
                await result("failed", msg="仅支持创建 AI 会话")
                return
            sid = db.create_session("ai", name, conn.user_id)
            sess = db.get_session(sid)
            await result("ok", {"session": _session_view(sess, conn.user_id, hub)})

        elif op == "rename_session":
            sid = str(frame.get("session_id", ""))
            sess = db.get_session(sid)
            if not sess or conn.user_id not in db.session_members(sid):
                await result("failed", msg="会话不存在")
                return
            name = str(frame.get("name", "")).strip()[:30]
            if name:
                db.conn.execute("UPDATE sessions SET name=? WHERE id=?", (name, sid))
                db.conn.commit()
            await result("ok", {})

        elif op == "delete_session":
            sid = str(frame.get("session_id", ""))
            sess = db.get_session(sid)
            if not sess or conn.user_id not in db.session_members(sid):
                await result("failed", msg="会话不存在")
                return
            if sess["kind"] == "direct":
                await result("failed", msg="好友会话不能删除")
                return
            db.conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
            db.conn.execute("DELETE FROM session_members WHERE session_id=?", (sid,))
            db.conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
            db.conn.commit()
            await result("ok", {})
            await conn.send({"post_type": "notice", "notice_type": "session_deleted",
                             "session_id": sid})

        elif op == "history":
            sid = str(frame.get("session_id", ""))
            if conn.user_id not in db.session_members(sid):
                await result("failed", msg="会话不存在")
                return
            limit = min(int(frame.get("limit", 50)), 200)
            before = frame.get("before_id")
            rows = db.list_messages(sid, limit=limit,
                                    before_id=int(before) if before else None)
            msgs = []
            for r in rows:
                msgs.append({
                    "message_id": r["id"],
                    "session_id": sid,
                    "sender": {"user_id": r["sender_id"],
                               "nickname": r["sender_name"]},
                    "message": json.loads(r["content"]),
                    "raw_message": r["raw"],
                    "time": int(r["created_at"]),
                })
            await result("ok", {"messages": msgs,
                                "has_more": len(msgs) == limit})

        elif op == "friend_add":
            resp = await hub.add_friend_request(
                conn.user_id, str(frame.get("username", "")).strip())
            if resp.get("status") == "ok":
                await result("ok", resp.get("data"))
            else:
                await result("failed", msg=resp.get("msg", ""))

        elif op == "friend_add_id":
            target = db.get_user(int(frame.get("user_id", 0)))
            if target is None:
                await result("failed", msg="用户不存在")
            else:
                resp = await hub.add_friend_request(conn.user_id,
                                                    target["username"])
                if resp.get("status") == "ok":
                    await result("ok", resp.get("data"))
                else:
                    await result("failed", msg=resp.get("msg", ""))

        elif op == "user_profile":
            row = db.get_user(int(frame.get("user_id", 0)))
            if row is None:
                await result("failed", msg="用户不存在")
            else:
                st = db.friend_status(conn.user_id, row["id"])
                if st is None:
                    # the other side may have a pending request to me
                    rev = db.friend_status(row["id"], conn.user_id)
                    st = "incoming_pending" if rev == "pending" else "none"
                profile = user_public(row)
                profile["friend_status"] = st
                profile["is_me"] = row["id"] == conn.user_id
                await result("ok", profile)

        elif op == "update_profile":
            nick = str(frame.get("nickname", "")).strip()
            if not re.fullmatch(r"[\w\u4e00-\u9fa5\- ]{2,20}", nick):
                await result("failed",
                             msg="昵称需为 2-20 位中英文/数字/下划线")
            else:
                db.update_nickname(conn.user_id, nick)
                await result("ok", {"user": user_public(
                    db.get_user(conn.user_id))})

        elif op == "friend_requests":
            rows = db.list_requests(conn.user_id)
            await result("ok", {"requests": [
                {"user_id": r["id"], "nickname": display_name(r),
                 "avatar_color": r["avatar_color"], "avatar": _avatar_url(r),
                 "time": r["created_at"]}
                for r in rows]})

        elif op == "friend_handle":
            resp = await hub.handle_friend_request(
                conn.user_id, int(frame.get("user_id", 0)),
                bool(frame.get("approve")))
            if resp.get("status") == "ok":
                await result("ok", resp.get("data"))
            else:
                await result("failed", msg=resp.get("msg", ""))

        elif op == "friend_delete":
            resp = await hub.delete_friend(conn.user_id,
                                           int(frame.get("user_id", 0)))
            await result("ok", resp.get("data"))

        elif op == "typing":
            # broadcast typing to session peers
            sid = str(frame.get("session_id", ""))
            for uid in db.session_members(sid):
                if uid != conn.user_id:
                    await hub.send_to_user(uid, {
                        "post_type": "notice", "notice_type": "typing",
                        "session_id": sid, "user_id": conn.user_id,
                        "typing": bool(frame.get("typing", True)),
                    })
            await result("ok", {})

        else:
            await result("failed", msg=f"未知操作 {op}")
    except Exception as e:  # noqa: BLE001
        log.exception("client op %s failed", op)
        await result("failed", msg=str(e))


async def ws_onebot(request: web.Request) -> web.WebSocketResponse:
    """Reverse WebSocket endpoint for OneBot V11 bots."""
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    log.info("onebot reverse ws connected from %s", request.remote)
    await bridge.attach_reverse(ws)
    return ws


# ============================================================ app factory
def create_app() -> web.Application:
    cfg.ensure_dirs()
    db = dbmod.init_db(cfg.db_path())
    if not db.get_config("admin_token"):
        db.set_config("admin_token", secrets.token_urlsafe(16))

    app = web.Application(client_max_size=8 * 1024 * 1024)

    app.router.add_get("/", index)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/api/client-settings", api_client_settings)
    app.router.add_post("/api/client-diag", api_client_diag)
    app.router.add_get("/api/apk/latest", api_apk_latest)
    app.router.add_get("/api/apk/download", api_apk_download)
    app.router.add_post("/api/register/send-code", api_send_code)
    app.router.add_get("/api/register/check-bounce", api_check_bounce)
    app.router.add_post("/api/register", api_register)
    app.router.add_post("/api/login", api_login)
    app.router.add_get("/api/session-info", api_session_info)
    app.router.add_post("/api/avatar/upload", api_avatar_upload)
    app.router.add_get("/avatar/{name}", avatar_file)

    app.router.add_post("/api/admin/login", admin_login)
    app.router.add_post("/api/admin/change-token", admin_change_token)
    app.router.add_get("/api/admin/config", admin_get_config)
    app.router.add_post("/api/admin/config", admin_set_config)
    app.router.add_post("/api/admin/test-mail", admin_test_mail)
    app.router.add_post("/api/admin/reconnect", admin_reconnect)
    app.router.add_post("/api/admin/restart", admin_restart)
    app.router.add_get("/api/admin/users", admin_users)
    app.router.add_post("/api/admin/delete-user", admin_delete_user)
    app.router.add_post("/api/admin/export", admin_export)
    app.router.add_get("/api/admin/export/status", admin_export_status)
    app.router.add_get("/api/admin/export/download/{target}", admin_export_download)
    app.router.add_get("/api/admin/logs", admin_logs)

    app.router.add_get("/ws", ws_client)
    app.router.add_get("/onebot", ws_onebot)

    # static: client web app + admin webui
    app.router.add_static("/static/", cfg.CLIENT_WEB_DIR, show_index=False)
    app.router.add_static("/admin/static/", os.path.join(cfg.ROOT, "server", "webui"),
                          show_index=False)
    app.router.add_get("/admin", admin_page)
    # admin page references favicon.ico relatively -> /admin/favicon.ico
    app.router.add_get("/admin/favicon.ico", client_asset_favicon)

    # index.html references style.css / app.js / config.bin relatively,
    # so they must resolve at the root level too (not only under /static/).
    for asset in ("style.css", "app.js", "config.json", "config.bin", "favicon.ico", "logo.png"):
        app.router.add_get("/" + asset, client_asset)
    # scan-code libs (qr decoder + generator)
    app.router.add_get("/vendor/{name}", client_vendor_asset)

    async def on_startup(app_: web.Application) -> None:
        hub.bridge = bridge  # wire bridge into hub for AI message routing
        bridge.start()
        bounce_watcher.start()
        log.info("KiteChat started. ws=%s admin_token=%s",
                 cfg.public_ws_address(), db.get_config("admin_token"))

    async def on_cleanup(app_: web.Application) -> None:
        await bridge.stop()
        await bounce_watcher.stop()
        exporter.shutdown()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


def _nocache_file(path: str) -> web.Response:
    """FileResponse that forbids caching — clients must always pick up
    freshly deployed client code (e.g. reconnect fixes)."""
    resp = web.FileResponse(path)
    resp.headers.update(_NO_CACHE)
    return resp


async def index(request: web.Request) -> web.Response:
    path = os.path.join(cfg.CLIENT_WEB_DIR, "index.html")
    if not os.path.exists(path):
        return web.Response(text="KiteChat client web assets missing", status=500)
    return _nocache_file(path)


async def client_asset(request: web.Request) -> web.Response:
    """Serve client web assets referenced relatively from index.html."""
    name = request.path.lstrip("/")
    allowed = {"style.css", "app.js", "config.json", "config.bin", "favicon.ico", "logo.png"}
    if name not in allowed:
        raise web.HTTPNotFound()
    path = os.path.join(cfg.CLIENT_WEB_DIR, name)
    if not os.path.exists(path):
        raise web.HTTPNotFound()
    return _nocache_file(path)


async def client_vendor_asset(request: web.Request) -> web.Response:
    """Serve bundled scan-code libraries (jsQR decoder, qr generator)."""
    name = os.path.basename(request.match_info["name"])
    allowed = {"jsQR.js", "qrcode.js"}
    if name not in allowed:
        raise web.HTTPNotFound()
    path = os.path.join(cfg.CLIENT_WEB_DIR, "vendor", name)
    if not os.path.exists(path):
        raise web.HTTPNotFound()
    return _nocache_file(path)


async def client_asset_favicon(request: web.Request) -> web.Response:
    """Favicon for the /admin page (relative reference resolves here)."""
    path = os.path.join(cfg.CLIENT_WEB_DIR, "favicon.ico")
    if not os.path.exists(path):
        raise web.HTTPNotFound()
    return _nocache_file(path)


async def admin_page(request: web.Request) -> web.Response:
    path = os.path.join(cfg.ROOT, "server", "webui", "index.html")
    if not os.path.exists(path):
        return web.Response(text="admin webui missing", status=500)
    resp = web.FileResponse(path)
    # never serve a cached admin page — branding/config updates must show up
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


async def healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "app": "KiteChat", "time": time.time()})
