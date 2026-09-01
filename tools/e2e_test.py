"""End-to-end integration test for KiteChat server (no real SMTP needed:
verification codes are injected directly into DB, everything else runs
through the real HTTP/WS API stack).

Run:  .venv/Scripts/python.exe tools/e2e_test.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import sys
import time

import aiohttp

from testutil import cleanup_users

BASE = os.environ.get("NOVA_TEST_BASE", "http://127.0.0.1:8920")
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "kitechat.db")

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  ✅ {name}")
    else:
        FAILED.append(name)
        print(f"  ❌ {name}  {detail}")


def inject_code(email: str, code: str = "123456") -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO codes(purpose,target,code,payload,expires_at,used,created_at) "
        "VALUES('register',?,?,?, ?,0,?)",
        (email, code, "{}", time.time() + 600, time.time()),
    )
    conn.commit()
    conn.close()


def get_admin_token() -> str:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT value FROM config WHERE key='admin_token'").fetchone()
    conn.close()
    return row[0]


async def drain(ws, pred, timeout: float = 5.0, max_frames: int = 12):
    """Read frames until pred(frame) is true; returns frame or None."""
    for _ in range(max_frames):
        try:
            f = await asyncio.wait_for(ws.receive_json(), timeout)
        except asyncio.TimeoutError:
            return None
        if pred(f):
            return f
    return None


async def collect(ws, seconds: float = 3.0) -> list:
    """Gather every frame arriving within the window (order-insensitive)."""
    frames = []
    deadline = asyncio.get_event_loop().time() + seconds
    while True:
        remain = deadline - asyncio.get_event_loop().time()
        if remain <= 0:
            break
        try:
            f = await asyncio.wait_for(ws.receive_json(), remain)
            frames.append(f)
        except asyncio.TimeoutError:
            break
    return frames


async def collect2(ws1, ws2, seconds: float = 3.0) -> tuple[list, list]:
    """Read both sockets concurrently for `seconds`.

    aiohttp only answers server pings while a socket is being read; a
    socket idle past the server heartbeat (25s) gets disconnected.
    """
    f1: list = []
    f2: list = []
    stop = asyncio.Event()

    async def reader(ws, sink):
        while not stop.is_set():
            try:
                f = await asyncio.wait_for(ws.receive_json(), 1.0)
                sink.append(f)
            except asyncio.TimeoutError:
                continue
            except Exception:  # noqa: BLE001
                break

    t1 = asyncio.create_task(reader(ws1, f1))
    t2 = asyncio.create_task(reader(ws2, f2))
    await asyncio.sleep(seconds)
    stop.set()
    await asyncio.gather(t1, t2, return_exceptions=True)
    return f1, f2


async def main() -> None:
    print(f"== KiteChat E2E test against {BASE} ==")
    # unique suffix so reruns don't collide with existing accounts
    suffix = str(int(time.time()))[-6:]
    ALICE, BOB = f"alice{suffix}", f"bob{suffix}"
    ALICE_EMAIL, BOB_EMAIL = f"alice{suffix}@test.local", f"bob{suffix}@test.local"
    token = get_admin_token()
    admin_h = {"X-Admin-Token": token}

    async with aiohttp.ClientSession() as sess:
        # ---------- admin ----------
        print("\n[1] 管理后台 API")
        r = await sess.get(f"{BASE}/api/admin/config", headers=admin_h)
        j = await r.json()
        check("admin config", j["status"] == "ok" and j["data"]["smtp_ready"] in (True, False))

        r = await sess.post(f"{BASE}/api/admin/config", headers=admin_h,
                            json={"ws_address": "ws://127.0.0.1:8920"})
        j = await r.json()
        check("admin set ws_address (auto /ws path)",
              j["status"] == "ok" and j["data"]["public_ws"].endswith("/ws"),
              str(j))

        r = await sess.post(f"{BASE}/api/admin/config", headers=admin_h,
                            json={"ws_address": "bad url here"})
        j = await r.json()
        check("admin rejects invalid ws address", j["status"] == "failed")

        r = await sess.get(f"{BASE}/api/admin/users", headers=admin_h)
        j = await r.json()
        check("admin user list", j["status"] == "ok")

        r = await sess.get(f"{BASE}/api/admin/config", headers={"X-Admin-Token": "wrong"})
        check("admin rejects bad token", r.status == 401)

        # ---------- register flow ----------
        print("\n[2] 注册流程（邮箱验证码）")
        # We never want the test to actually send real e-mails (fake
        # *@test.local addresses bounce and spam the operator inbox).
        # Temporarily disable SMTP so send-code takes the "not configured"
        # branch; the code is injected directly below anyway.
        # Also clear the domain whitelist — the fake test domain would be
        # rejected by the whitelist check before reaching the SMTP check.
        cfg_r = await sess.get(f"{BASE}/api/admin/config",
                               headers={"X-Admin-Token": token})
        cfg_data = (await cfg_r.json())["data"]
        saved_smtp = cfg_data.get("smtp_host", "")
        saved_wl = cfg_data.get("email_domain_whitelist", "")
        await sess.post(f"{BASE}/api/admin/config",
                        headers={"X-Admin-Token": token},
                        json={"smtp_host": "", "email_domain_whitelist": ""})
        r = await sess.post(f"{BASE}/api/register/send-code",
                            json={"email": ALICE_EMAIL, "purpose": "register"})
        j = await r.json()
        check("send-code without SMTP -> friendly error",
              j["status"] == "failed" and "SMTP" in j["msg"], str(j))
        # restore the real SMTP host and whitelist so other flows/next runs
        # are unaffected
        if saved_smtp or saved_wl:
            await sess.post(f"{BASE}/api/admin/config",
                            headers={"X-Admin-Token": token},
                            json={"smtp_host": saved_smtp,
                                  "email_domain_whitelist": saved_wl})

        # inject code to bypass SMTP, then register alice & bob
        inject_code(ALICE_EMAIL)
        r = await sess.post(f"{BASE}/api/register", json={
            "username": ALICE, "password": "alice123", "password2": "alice123",
            "email": ALICE_EMAIL, "code": "123456"})
        j = await r.json()
        check("register alice", j["status"] == "ok" and j["data"]["token"], str(j))
        alice_token = j["data"]["token"] if j["status"] == "ok" else ""
        alice_user = j["data"]["user"] if j["status"] == "ok" else {}

        inject_code(BOB_EMAIL)
        r = await sess.post(f"{BASE}/api/register", json={
            "username": BOB, "password": "bob12345", "password2": "bob12345",
            "email": BOB_EMAIL, "code": "123456"})
        j = await r.json()
        check("register bob", j["status"] == "ok", str(j))
        bob_token = j["data"]["token"] if j["status"] == "ok" else ""
        bob_id = j["data"]["user"]["user_id"] if j["status"] == "ok" else 0

        # duplicate checks
        r = await sess.post(f"{BASE}/api/register", json={
            "username": ALICE, "password": "alice123", "password2": "alice123",
            "email": "other@test.local", "code": "000000"})
        j = await r.json()
        check("duplicate username rejected", j["status"] == "failed")

        r = await sess.post(f"{BASE}/api/register", json={
            "username": "carol", "password": "x", "password2": "y",
            "email": "carol@test.local", "code": "000000"})
        j = await r.json()
        check("password mismatch rejected", j["status"] == "failed")

        # ---------- login ----------
        print("\n[3] 登录")
        r = await sess.post(f"{BASE}/api/login",
                            json={"username": ALICE, "password": "alice123"})
        j = await r.json()
        check("login alice", j["status"] == "ok")
        r = await sess.post(f"{BASE}/api/login",
                            json={"username": ALICE, "password": "wrong"})
        check("wrong password rejected", r.status == 401)

        # ---------- websocket chat ----------
        print("\n[4] WebSocket 实时通信")
        ws_a = await sess.ws_connect(f"{BASE}/ws")
        await ws_a.send_json({"op": "auth", "token": alice_token})
        f = await ws_a.receive_json()
        check("alice auth_ok", f.get("op") == "auth_ok", str(f)[:200])
        check("alice got welcome session", len(f.get("sessions", [])) >= 1)
        alice_session = f["sessions"][0]["id"]
        # bridge connected? -> AI replies come from AstrBot (slower, real LLM)
        BRIDGE = bool((f.get("server") or {}).get("bridge_connected"))

        ws_b = await sess.ws_connect(f"{BASE}/ws")
        await ws_b.send_json({"op": "auth", "token": bob_token})
        fb = await ws_b.receive_json()
        check("bob auth_ok", fb.get("op") == "auth_ok")

        # AI chat: send message. In BRIDGE mode reply comes from AstrBot
        # (real LLM, slower, may lead with a 'reply' segment); otherwise the
        # built-in offline responder answers quickly.
        await ws_a.send_json({"op": "message", "req_id": 1,
                              "session_id": alice_session, "message": "ping"})
        if BRIDGE:
            frames, _fb_frames = await collect2(ws_a, ws_b, 45)
        else:
            frames = await collect(ws_a, 3.5)
        result1 = next((x for x in frames if x.get("op") == "result" and x.get("req_id") == 1), None)
        check("message accepted", result1 and result1.get("status") == "ok", str(result1))
        got_own = next((x for x in frames if x.get("post_type") == "message"
                        and x.get("sender", {}).get("user_id") == alice_user["user_id"]), None)
        check("own message broadcast", got_own is not None)
        got_reply = next((x for x in frames if x.get("post_type") == "message"
                          and x.get("sender", {}).get("user_id") == 0), None)
        check(("AstrBot reply received" if BRIDGE else "AI builtin reply received"),
              got_reply is not None)
        if got_reply:
            seg0 = got_reply["message"][0]["type"] if got_reply.get("message") else ""
            check("bot reply is OneBot segments",
                  isinstance(got_reply["message"], list)
                  and seg0 in ("text", "reply", "image"))

        # ---------- friends ----------
        print("\n[5] 好友系统")
        await ws_a.send_json({"op": "friend_add", "req_id": 2, "username": BOB})
        f = await drain(ws_a, lambda x: x.get("op") == "result" and x.get("req_id") == 2)
        check("friend request sent", f and f.get("status") == "ok", str(f))
        fb = await drain(ws_b, lambda x: x.get("post_type") == "request"
                         and x.get("request_type") == "friend")
        check("bob receives friend request", fb is not None, str(fb)[:200])

        await ws_b.send_json({"op": "friend_handle", "req_id": 3,
                              "user_id": alice_user["user_id"], "approve": True})
        f = await drain(ws_b, lambda x: x.get("op") == "result" and x.get("req_id") == 3)
        check("bob approved", f and f.get("status") == "ok", str(f))
        fa = await drain(ws_a, lambda x: x.get("notice_type") == "friend_added")
        check("alice notified friend_added", fa is not None, str(fa)[:200])

        # presence: bob disconnects -> alice should get presence offline
        await ws_b.close()
        f = await drain(ws_a, lambda x: x.get("meta_event_type") == "presence"
                        and x.get("online") is False, timeout=8)
        check("presence offline broadcast", f is not None, str(f)[:200])
        ws_b = await sess.ws_connect(f"{BASE}/ws")
        await ws_b.send_json({"op": "auth", "token": bob_token})
        fb = await ws_b.receive_json()
        f = await drain(ws_a, lambda x: x.get("meta_event_type") == "presence"
                        and x.get("online") is True, timeout=8)
        check("presence online broadcast", f is not None, str(f)[:200])

        # ---------- direct chat ----------
        print("\n[6] 好友私聊（独立会话）")
        direct = None
        for s in json.loads(json.dumps(fb.get("sessions", []))):
            if s["kind"] == "direct":
                direct = s
        check("direct session auto-created", direct is not None)
        if direct:
            await ws_a.send_json({"op": "message", "req_id": 4,
                                  "session_id": direct["id"],
                                  "message": "你好 bob！"})
            f = await drain(ws_a, lambda x: x.get("op") == "result" and x.get("req_id") == 4)
            check("dm accepted", f and f.get("status") == "ok", str(f))
            found = await drain(ws_b, lambda x: x.get("post_type") == "message"
                                and "你好" in json.dumps(x, ensure_ascii=False))
            check("bob received DM in real time", found is not None)

        # ---------- history ----------
        print("\n[7] 历史消息分页")
        await ws_a.send_json({"op": "history", "req_id": 5,
                              "session_id": alice_session, "limit": 50})
        f = await drain(ws_a, lambda x: x.get("op") == "result" and x.get("req_id") == 5)
        msgs = (f or {}).get("data", {}).get("messages", [])
        check("history returns stored messages", len(msgs) >= 3, str(f)[:200])

        # ---------- forward message parsing ----------
        print("\n[8] OneBot V11 合并转发")
        fwd = [{
            "type": "forward",
            "data": {
                "title": "群聊的合并转发",
                "brief": "来看看",
                "content": [
                    {"type": "node", "data": {"name": "张三", "uin": "111",
                                              "content": [{"type": "text", "data": {"text": "第一条消息"}}]}},
                    {"type": "node", "data": {"name": "李四", "uin": "222",
                                              "content": "[CQ:image,file=http://example.com/a.jpg]"}},
                ],
            },
        }]
        await ws_a.send_json({"op": "message", "req_id": 6,
                              "session_id": direct["id"] if direct else alice_session,
                              "message": fwd})
        f = await drain(ws_a, lambda x: x.get("op") == "result" and x.get("req_id") == 6)
        check("forward message accepted", f and f.get("status") == "ok", str(f))

        # ---------- OneBot reverse bridge ----------
        print("\n[9] OneBot V11 桥接端点")
        ws_bot = await sess.ws_connect(f"{BASE}/onebot")
        f = await ws_bot.receive_json()
        check("lifecycle connect event",
              f.get("post_type") == "meta_event" and f.get("meta_event_type") == "lifecycle",
              str(f))
        await ws_bot.send_json({"action": "get_version_info", "params": {}, "echo": "e1"})
        f = await ws_bot.receive_json()
        check("get_version_info answered",
              f.get("status") == "ok" and f["data"]["protocol_version"] == "v11", str(f))
        await ws_bot.send_json({"action": "get_friend_list", "params": {}, "echo": "e2"})
        f = await ws_bot.receive_json()
        check("get_friend_list returns virtual users",
              f.get("status") == "ok" and len(f["data"]) >= 2, str(f)[:200])

        # virtual numbers (代号) are assigned #1..N; look them up live from DB
        import sqlite3 as _s
        conn = _s.connect(DB_PATH)
        row = conn.execute(
            "SELECT virtual_qq FROM users WHERE username=?", (ALICE,)).fetchone()
        conn.close()
        virtual_alice = row[0] if row else None
        check("virtual numbers start at #1", virtual_alice is not None and virtual_alice >= 1)
        await ws_bot.send_json({
            "action": "send_private_msg",
            "params": {"user_id": virtual_alice,
                       "message": "[CQ:text,text=来自外部OneBot Bot的消息]"},
            "echo": "e3"})
        f = await ws_bot.receive_json()
        check("send_private_msg ok", f.get("status") == "ok", str(f))
        got_bot_msg = False
        for _ in range(6):
            try:
                f = await asyncio.wait_for(ws_a.receive_json(), 3)
            except asyncio.TimeoutError:
                break
            if f.get("post_type") == "message" and "外部OneBot" in json.dumps(f, ensure_ascii=False):
                got_bot_msg = True
                break
        check("external bot message routed to alice", got_bot_msg)
        await ws_bot.close()

        # ---------- CQ parse unit checks ----------
        print("\n[10] CQ 码解析")
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from server import onebot as ob
        segs = ob.cq_to_segments("hello[CQ:face,id=5]world[CQ:image,url=http://x/y.png]")
        check("cq parse mixed", len(segs) == 4 and segs[0]["data"]["text"] == "hello"
              and segs[3]["type"] == "image")
        back = ob.segments_to_cq(segs)
        check("cq roundtrip", "CQ:face" in back and "CQ:image" in back)
        check("preview", ob.preview(segs).startswith("hello"))

        await ws_a.close()
        await ws_b.close()

    print(f"\n===== 结果: {len(PASSED)} 通过, {len(FAILED)} 失败 =====")
    if FAILED:
        print("失败项:", FAILED)
        cleanup_users([ALICE, BOB])
        sys.exit(1)
    cleanup_users([ALICE, BOB])


if __name__ == "__main__":
    asyncio.run(main())
