"""Verify KiteChat -> AstrBot impl bridge end to end."""
import asyncio, json, os, sqlite3, time
import aiohttp

BASE = "http://127.0.0.1:8920"
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "kitechat.db")
suffix = str(int(time.time()))[-6:]
USER, EMAIL = f"astr{suffix}", f"astr{suffix}@t.local"

def inject_code(email):
    c = sqlite3.connect(DB)
    c.execute("INSERT INTO codes(purpose,target,code,payload,expires_at,used,created_at) VALUES('register',?,?,?, ?,0,?)",
              (email, "123456", "{}", time.time()+600, time.time()))
    c.commit(); c.close()

async def main():
    async with aiohttp.ClientSession() as s:
        inject_code(EMAIL)
        r = await s.post(f"{BASE}/api/register", json={"username":USER,"password":"astrtest1","password2":"astrtest1","email":EMAIL,"code":"123456"})
        j = await r.json()
        assert j["status"]=="ok", j
        vqq = None
        c = sqlite3.connect(DB)
        row = c.execute("SELECT virtual_qq FROM users WHERE username=?", (USER,)).fetchone()
        vqq = row[0] if row else None
        print("registered", USER, "virtual_qq =", vqq)
        tok = j["data"]["token"]
        ws = await s.ws_connect(f"{BASE}/ws")
        await ws.send_json({"op":"auth","token":tok})
        f = await ws.receive_json()
        assert f["op"]=="auth_ok"
        sid = f["sessions"][0]["id"]
        await ws.send_json({"op":"message","req_id":1,"session_id":sid,"message":"你好，这是一条桥接测试消息"})
        # collect frames for a while — AstrBot may or may not reply depending on its own LLM config
        got_result = False
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                f = await asyncio.wait_for(ws.receive_json(), 5)
            except asyncio.TimeoutError:
                continue
            if f.get("op")=="result" and f.get("req_id")==1:
                got_result = f.get("status")=="ok"
                print("message accepted:", got_result)
            if f.get("post_type")=="message" and f.get("sender",{}).get("user_id")==0:
                print("BOT REPLY via AstrBot:", json.dumps(f["message"], ensure_ascii=False)[:200])
                break
        await ws.close()
        print("push test done (check AstrBot log for incoming event)")

from testutil import cleanup_users  # noqa: E402

try:
    asyncio.run(main())
finally:
    cleanup_users([USER])
