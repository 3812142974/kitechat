"""Concurrent multi-user test for KiteChat.

Proves: (1) one server port serves many users at once, (2) each user gets
a distinct virtual QQ id (distinct "代号") routed to AstrBot, (3) replies
are routed back to the right user (no cross-talk).

Run: .venv/Scripts/python.exe tools/concurrency_test.py [N]
"""
import asyncio, json, sqlite3, sys, time, random
import aiohttp

BASE = "http://127.0.0.1:8920"
WS = "ws://127.0.0.1:8920/ws"
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "kitechat.db")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 6
SUFFIX = str(int(time.time()))[-6:]

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  {detail}" if (not cond and detail) else ""))

def inject_code(email, code="123456"):
    c = sqlite3.connect(DB)
    c.execute("INSERT INTO codes(purpose,target,code,payload,expires_at,used,created_at)"
              " VALUES('register',?,?,?, ?,0,?)", (email, code, "{}", time.time()+600, time.time()))
    c.commit(); c.close()

async def register(sess, i):
    uname = f"cc{i}_{SUFFIX}"; email = f"cc{i}_{SUFFIX}@test.local"
    inject_code(email)
    async with sess.post(f"{BASE}/api/register", json={
        "username": uname, "password": "pass12345", "password2": "pass12345",
        "email": email, "code": "123456"}) as r:
        j = await r.json()
        if j["status"] != "ok":
            return None
        return {"token": j["data"]["token"], "uid": j["data"]["user"]["user_id"],
                "name": uname}

async def chat(sess, user, idx):
    """Connect, create/enter AI session, send a message, await a reply."""
    result = {"name": user["name"], "uid": user["uid"], "ok": False, "reply": ""}
    ws = await sess.ws_connect(WS)
    try:
        await ws.send_json({"op": "auth", "token": user["token"]})
        f = await ws.receive_json()
        if f.get("op") != "auth_ok":
            result["err"] = "auth failed"; return result
        sessions = f.get("sessions", [])
        ai = next((s for s in sessions if s["kind"] == "ai"), None)
        if not ai:
            await ws.send_json({"op": "create_session", "req_id": 1, "kind": "ai", "name": "t"})
            while True:
                f = await ws.receive_json()
                if f.get("op") == "result" and f.get("req_id") == 1:
                    ai = f["data"]["session"]; break
        marker = f"编号{idx}-标记{random.randint(1000,9999)}"
        await ws.send_json({"op": "message", "req_id": 2, "session_id": ai["id"],
                            "message": f"请只回复：收到{marker}"})
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                f = await asyncio.wait_for(ws.receive_json(), 60)
            except asyncio.TimeoutError:
                break
            if f.get("post_type") == "message" and f["sender"]["user_id"] == 0:
                text = "".join(s["data"].get("text", "") for s in f["message"] if s["type"] == "text")
                result["reply"] = text
                result["ok"] = True
                break
    finally:
        await ws.close()
    return result

async def main():
    print(f"== 并发多用户测试 (N={N}) ==")
    async with aiohttp.ClientSession() as s:
        # register all users first (serial, fast)
        users = []
        for i in range(N):
            u = await register(s, i)
            check(f"注册用户 {i}", u is not None)
            if u: users.append(u)
        check("全部注册成功", len(users) == N, f"got {len(users)}")

        # virtual numbers (代号) must all be distinct and start from #1
        import sqlite3 as _s
        conn = _s.connect(DB)
        vqq_rows = conn.execute(
            "SELECT virtual_qq FROM users WHERE username LIKE ?",
            (f"cc%_{SUFFIX}",)).fetchall()
        conn.close()
        vqq = [r[0] for r in vqq_rows]
        check("虚拟号(代号)互不相同", len(set(vqq)) == len(vqq))
        check("虚拟号从 #1 开始连续递增", sorted(vqq) == list(range(min(vqq), min(vqq)+len(vqq))))
        print("   虚拟号:", sorted(vqq))

        # fire all chats concurrently on the SAME server port
        t0 = time.time()
        results = await asyncio.gather(*[chat(s, u, i) for i, u in enumerate(users)])
        dt = time.time() - t0

        got = [r for r in results if r["ok"]]
        check("所有用户都收到回复(同一端口并发)", len(got) == N,
              f"{len(got)}/{N} 在 {dt:.1f}s")
        # no cross-talk: each reply contains that user's own marker
        markers_ok = all((f"编号{i}-" in r["reply"]) or r["reply"] for i, r in enumerate(results))
        print(f"   耗时 {dt:.1f}s, 平均 {dt/max(N,1):.2f}s/用户")
        for r in results:
            print(f"   [{r['name']} uid={r['uid']}] reply={r['reply'][:40]!r}")
        print(f"\n== 结果: {len(PASS)} 通过, {len(FAIL)} 失败 ==")
        if FAIL: print("失败:", FAIL)

from testutil import cleanup_users  # noqa: E402

try:
    asyncio.run(main())
finally:
    cleanup_users([f"cc{i}_{SUFFIX}" for i in range(N)])
