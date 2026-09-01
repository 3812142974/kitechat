"""Browser E2E via CDP: verify topbar layout + account switch page + restore."""
import asyncio
import base64
import hashlib
import json
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import aiohttp

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
APP = "http://127.0.0.1:8920"
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "kitechat.db")
SHOTS = r"C:\Users\Administrator\workspace\kc_shots"
UNAME = "browsertest1"
PASSWORD = "TestPass123"

os.makedirs(SHOTS, exist_ok=True)


def hash_pw(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode("ascii"), 120_000).hex()
    return f"pbkdf2${salt}${digest}"


def make_user_db():
    """Create the test user directly in the DB (avoids email-code flow)."""
    c = sqlite3.connect(DB)
    row = c.execute("SELECT id FROM users WHERE username=?", (UNAME,)).fetchone()
    if row:
        uid = row[0]
        c.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_pw(PASSWORD), uid))
    else:
        cur = c.execute(
            "INSERT INTO users(username,password_hash,email,created_at,avatar_color,signature,nickname,avatar) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (UNAME, hash_pw(PASSWORD), UNAME + "@local", time.time(), "#4C9BE8", "", "", ""))
        uid = cur.lastrowid
    c.commit()
    c.close()
    return uid


async def login(sess):
    r = await sess.post(APP + "/api/login", json={"username": UNAME, "password": PASSWORD})
    d = await r.json()
    assert d.get("status") == "ok", d
    return d["data"]["token"], d["data"]["user"]
def start_chrome():
    p = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                          "--remote-debugging-port=9333", "--window-size=390,844",
                          "--hide-scrollbars", "about:blank"])
    for _ in range(40):
        try:
            urllib.request.urlopen("http://127.0.0.1:9333/json/version", timeout=1).read()
            return p
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("chrome did not start")


async def cdp(ws, method, params=None, timeout=15):
    global _mid
    _mid += 1
    await ws.send_json({"id": _mid, "method": method, "params": params or {}})
    while True:
        msg = await asyncio.wait_for(ws.receive_json(), timeout)
        if msg.get("id") == _mid:
            if "error" in msg:
                raise RuntimeError(msg["error"])
            return msg.get("result", {})


async def shoot(ws, name):
    r = await cdp(ws, "Page.captureScreenshot", {"format": "png"})
    with open(os.path.join(SHOTS, name), "wb") as f:
        f.write(base64.b64decode(r["data"]))
    print(f"  📸 {name} ({len(r['data'])*3//4//1024} KB)")


async def main():
    global _mid
    _mid = 0
    results = {}

    async with aiohttp.ClientSession() as sess:
        uid = make_user_db()
        token, user = await login(sess)
        print(f"✅ 测试用户已创建: {user['username']} (uid={user['user_id']})")

        chrome = start_chrome()
        # open a tab pointed at the app
        req = urllib.request.Request(
            "http://127.0.0.1:9333/json/new?" + urllib.parse.quote(APP + "/", safe=""),
            method="PUT")
        info = json.loads(urllib.request.urlopen(req, timeout=5).read())
        ws_url = info["webSocketDebuggerUrl"]

        try:
            async with sess.ws_connect(ws_url, max_msg_size=50 * 1024 * 1024) as ws:
                await cdp(ws, "Page.enable")
                await cdp(ws, "Runtime.enable")

                # -- 1. login page first paint --
                await asyncio.sleep(2.5)
                await shoot(ws, "1_login.png")

                # -- 2. inject token → reload → logged-in home --
                await cdp(ws, "Runtime.evaluate", {"expression":
                    f"localStorage.setItem('nova_token',{json.dumps(token)});"
                    f"localStorage.setItem('nova_me',JSON.stringify({json.dumps(user)}));"
                    "location.reload();", "awaitPromise": False})
                await asyncio.sleep(4)
                js = await cdp(ws, "Runtime.evaluate", {"expression":
                    "JSON.stringify({hasToken:!!localStorage.getItem('nova_token'),"
                    "title:document.getElementById('sbTitle')?.textContent,"
                    "connDotRemoved:!document.getElementById('connDot'),"
                    "appShown:document.getElementById('appView').classList.contains('show')})"})
                print("  主界面状态:", js["result"]["value"])
                await shoot(ws, "2_home.png")

                # -- 3. account switch page --
                js = await cdp(ws, "Runtime.evaluate", {"expression":
                    "(function(){try{openAccountPage();return 'ok'}catch(e){return 'ERR:'+e.message+' at '+e.stack?.split('\\n')[1]}})()",
                    "returnByValue": True})
                print("  openAccountPage result:", js["result"]["value"])
                await asyncio.sleep(1)
                js = await cdp(ws, "Runtime.evaluate", {"expression":
                    "JSON.stringify({open:document.getElementById('accountSwitchPage').style.display==='flex',"
                    "items:document.querySelectorAll('.acct-item').length,"
                    "currentMarked:document.querySelectorAll('.acct-item.current').length,"
                    "storage:localStorage.getItem('kc_acct_page')})"})
                print("  账号页状态:", js["result"]["value"])
                await shoot(ws, "3_account_page.png")

                # -- 4. manage mode: current account must NOT be deletable --
                await cdp(ws, "Runtime.evaluate", {"expression": "toggleAccountManage();"})
                await asyncio.sleep(0.5)
                js = await cdp(ws, "Runtime.evaluate", {"expression":
                    "JSON.stringify({manageMode:S.acctManage,"
                    "delButtons:document.querySelectorAll('.acct-del').length,"
                    "manageLabel:document.getElementById('acctManageBtn').textContent})"})
                print("  管理模式状态:", js["result"]["value"])
                await shoot(ws, "4_manage_mode.png")

                # -- 5. askDelAccount on CURRENT account (index 0) must be blocked --
                js = await cdp(ws, "Runtime.evaluate", {"expression":
                    "(function(){ const before=accountStore().length;"
                    "askDelAccount(0);"
                    "return JSON.stringify({blocked:document.getElementById('delAcctModal').style.display!=='flex', storeUnchanged:accountStore().length===before}); })()"})
                print("  删除当前账号拦截:", js["result"]["value"])

                # -- 6. kill-restart restore: set marker, reload --
                await cdp(ws, "Runtime.evaluate", {"expression":
                    "localStorage.setItem('kc_acct_page','1');location.reload();"})
                await asyncio.sleep(4)
                js = await cdp(ws, "Runtime.evaluate", {"expression":
                    "JSON.stringify({restored:document.getElementById('accountSwitchPage').style.display==='flex'})"})
                print("  杀后台恢复:", js["result"]["value"])
                await shoot(ws, "5_restored.png")

                # -- 7. close page → marker cleared --
                await cdp(ws, "Runtime.evaluate", {"expression": "closeAccountPage();"})
                await asyncio.sleep(0.5)
                js = await cdp(ws, "Runtime.evaluate", {"expression":
                    "JSON.stringify({markerCleared:localStorage.getItem('kc_acct_page')===null})"})
                print("  关闭后标记清除:", js["result"]["value"])
        finally:
            chrome.terminate()
            try:
                chrome.wait(timeout=5)
            except Exception:
                chrome.kill()

        # -- cleanup test user --
        r = await sess.post(APP + "/api/admin/delete-user",
                            json={"user_id": user["user_id"]},
                            headers={"X-Admin-Token": get_admin_token()})
        print("✅ 测试用户已清理:", (await r.json()).get("status"))


def get_admin_token():
    c = sqlite3.connect(DB)
    t = c.execute("SELECT value FROM config WHERE key='admin_token'").fetchone()
    c.close()
    return t[0]


if __name__ == "__main__":
    asyncio.run(main())
