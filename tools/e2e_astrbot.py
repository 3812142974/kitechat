"""E2E: user message -> KiteChat -> AstrBot (impl mode) -> reply back."""
import asyncio, json, sqlite3, time, os
import aiohttp

BASE = "http://127.0.0.1:8920"
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "kitechat.db")
suffix = str(int(time.time()))[-6:]
USER, EMAIL = f"astr{suffix}", f"astr{suffix}@test.local"


def inject_code(email):
    c = sqlite3.connect(DB)
    c.execute("INSERT INTO codes(purpose,target,code,payload,expires_at,used,created_at) "
              "VALUES('register',?,?,?, ?,0,?)",
              (email, "123456", "{}", time.time() + 600, time.time()))
    c.commit(); c.close()


async def main():
    async with aiohttp.ClientSession() as s:
        inject_code(EMAIL)
        r = await s.post(f"{BASE}/api/register", json={
            "username": USER, "password": "astrtest1", "password2": "astrtest1",
            "email": EMAIL, "code": "123456"})
        j = await r.json()
        assert j["status"] == "ok", j
        tok = j["data"]["token"]
        ws = await s.ws_connect(f"{BASE}/ws")
        await ws.send_json({"op": "auth", "token": tok})
        f = await ws.receive_json()
        assert f["op"] == "auth_ok", f
        print("bridge_connected:", f["server"].get("bridge_connected"))
        sid = f["sessions"][0]["id"]

        await ws.send_json({"op": "message", "req_id": 1, "session_id": sid,
                            "message": "你好，介绍一下你自己"})
        reply = None
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                f = await asyncio.wait_for(ws.receive_json(), 20)
            except asyncio.TimeoutError:
                continue
            if f.get("post_type") == "message" and f["sender"]["user_id"] == 0:
                reply = f
                break
        if reply is None:
            print("NO REPLY from AstrBot within 120s")
            return
        text = json.dumps(reply["message"], ensure_ascii=False)
        print("REPLY:", text[:400])
        await ws.close()


from testutil import cleanup_users  # noqa: E402

try:
    asyncio.run(main())
finally:
    cleanup_users([USER])
