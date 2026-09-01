"""Verify the 'WS not connected' client-facing notice. Cleans up after itself."""
import asyncio, json, os, sqlite3, time
import aiohttp
from testutil import cleanup_users

BASE = "http://127.0.0.1:8920"
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "kitechat.db")
APP_WS = "ws://127.0.0.1:6199/ws"
created_users = []

def get_token():
    c = sqlite3.connect(DB)
    tok = c.execute("SELECT value FROM config WHERE key='admin_token'").fetchone()[0]
    c.close()
    return tok

async def main():
    admin = get_token()
    h = {"X-Admin-Token": admin, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as s:
        try:
            # 1) disconnect bridge: clear app_ws_url
            r = await s.post(f"{BASE}/api/admin/config", headers=h,
                             json={"app_ws_url": "", "bot_ws_url": ""})
            print("clear urls:", (await r.json())["status"])
            await asyncio.sleep(7)  # let the reconnect loop notice

            # 2) register a fresh user
            suffix = str(int(time.time()))[-6:]
            user, email = f"down{suffix}", f"down{suffix}@t.local"
            created_users.append(user)
            c = sqlite3.connect(DB)
            c.execute("INSERT INTO codes(purpose,target,code,payload,expires_at,used,created_at) VALUES('register',?,?,?, ?,0,?)",
                      (email, "123456", "{}", time.time()+600, time.time()))
            c.commit(); c.close()
            r = await s.post(f"{BASE}/api/register", json={"username":user,"password":"downtest1","password2":"downtest1","email":email,"code":"123456"})
            tok = (await r.json())["data"]["token"]

            ws = await s.ws_connect(f"{BASE}/ws")
            await ws.send_json({"op":"auth","token":tok})
            f = await ws.receive_json()
            assert f["op"] == "auth_ok"
            print("bridge_connected in auth_ok:", f.get("server", {}).get("bridge_connected"))
            sid = f["sessions"][0]["id"]

            # 3) send message while WS 'down' -> expect explicit notice
            await ws.send_json({"op":"message","req_id":1,"session_id":sid,"message":"hello"})
            deadline = time.time() + 15
            got = None
            while time.time() < deadline:
                try:
                    f = await asyncio.wait_for(ws.receive_json(), 5)
                except asyncio.TimeoutError:
                    break
                if f.get("post_type") == "message" and f.get("sender", {}).get("user_id") == 0:
                    got = f
                    break
            assert got, "no system reply received"
            text = got["message"][0]["data"]["text"]
            print("system reply:", text[:60].replace(chr(10), " "))
            assert "WS 未连接" in text, f"unexpected reply: {text}"
            print("PASS: 'WS 未连接' notice delivered to client")

            # 4) restore bridge config
            r = await s.post(f"{BASE}/api/admin/config", headers=h,
                             json={"app_ws_url": APP_WS})
            print("restore:", (await r.json())["status"])
            await asyncio.sleep(7)
            r = await s.get(f"{BASE}/api/admin/config", headers=h)
            print("bridge_connected now:", (await r.json())["data"]["bridge_connected"])
            await ws.close()
        finally:
            # always restore the bridge config, even on failure
            try:
                await s.post(f"{BASE}/api/admin/config", headers=h,
                             json={"app_ws_url": APP_WS})
            except Exception:
                pass
            cleanup_users(created_users)

asyncio.run(main())
