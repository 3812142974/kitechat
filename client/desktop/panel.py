"""KiteChat 桌面管理面板 (pywebview + 服务端管理)。

功能：
  - 双击打开 → 加载 WebUI 界面
  - 服务未运行 → 自动启动
  - 右上角菜单 → 停止/重启服务
  - 关闭窗口 → 只关闭界面,服务继续运行
  - 下次打开 → 直接加载界面

用法:
  python panel.py
  或 PyInstaller 打包成 EXE
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

APP_NAME = "KiteChat"
DEFAULT_PORT = 8920
HEALTH_URL = f"http://127.0.0.1:{DEFAULT_PORT}/healthz"
WEBUI_URL = f"http://127.0.0.1:{DEFAULT_PORT}"


# ---------------------------------------------------------------- 路径
def base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def server_dir() -> str:
    """服务端目录(与本文件同级或父级的 server/)"""
    d = base_dir()
    for cand in [d, os.path.dirname(d)]:
        if os.path.isfile(os.path.join(cand, "run.py")):
            return cand
    return d


def find_python() -> str:
    """找 Python 解释器"""
    # 嵌入式 Python
    if sys.platform == "win32":
        embed_py = os.path.join(base_dir(), "python", "python.exe")
        if os.path.isfile(embed_py):
            return embed_py
    # 系统 Python
    for cmd in ["python3", "python"]:
        try:
            out = subprocess.check_output(
                [cmd, "--version"], stderr=subprocess.STDOUT, timeout=5
            )
            if b"Python 3" in out:
                return cmd
        except Exception:
            continue
    return ""


# ---------------------------------------------------------------- 服务管理
def is_running() -> bool:
    """检测服务是否在运行"""
    try:
        r = urllib.request.urlopen(HEALTH_URL, timeout=2)
        data = json.loads(r.read())
        return data.get("status") == "ok"
    except Exception:
        return False


def start_server() -> None:
    """启动服务端(后台)"""
    if is_running():
        return
    py = find_python()
    if not py:
        print("找不到 Python 解释器")
        return
    runpy = os.path.join(server_dir(), "run.py")
    if not os.path.isfile(runpy):
        print(f"找不到 {runpy}")
        return
    subprocess.Popen(
        [py, runpy],
        cwd=server_dir(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        start_new_session=True,
    )
    # 等待启动
    for _ in range(30):
        time.sleep(0.5)
        if is_running():
            return


def stop_server() -> None:
    """停止服务端"""
    if sys.platform == "win32":
        # 找占用端口的 PID 并 kill
        try:
            out = subprocess.check_output(
                ["netstat", "-ano"], text=True, timeout=5
            )
            for line in out.splitlines():
                if f":{DEFAULT_PORT}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    subprocess.run(
                        ["taskkill", "/F", "/PID", pid],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
        except Exception:
            pass
    else:
        try:
            out = subprocess.check_output(
                ["lsof", "-ti", f":{DEFAULT_PORT}"],
                text=True, timeout=5
            ).strip()
            for pid in out.split():
                try:
                    os.kill(int(pid), 15)
                except (ProcessLookupError, ValueError):
                    pass
        except Exception:
            pass


# ---------------------------------------------------------------- GUI
def main() -> None:
    import webview

    # 启动服务
    if not is_running():
        print("服务未运行,正在启动...")
        start_server()
        if not is_running():
            print("服务启动失败")
            sys.exit(1)
    print(f"服务运行中: {WEBUI_URL}")

    # 创建窗口
    window = webview.create_window(
        APP_NAME,
        WEBUI_URL,
        width=1100,
        height=750,
        min_size=(800, 600),
        text_select=True,
    )

    # 菜单(停止/重启)
    def stop():
        stop_server()
        window.destroy()

    def restart():
        stop_server()
        time.sleep(1)
        start_server()
        window.load_url(WEBUI_URL)

    menu = {
        "tools": [
            {
                "name": "停止服务",
                "action": stop,
            },
            {
                "name": "重启服务",
                "action": restart,
            },
        ]
    }

    webview.start(menu=menu, debug=False)


if __name__ == "__main__":
    main()
