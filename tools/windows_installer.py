#!/usr/bin/env python3
"""KiteChat 服务端 —— Windows 一键安装包（原生 GUI，无网页壳）。

这是一个 PyInstaller 打包成的单 EXE，内含：
  - 服务端源码（server/、run.py、requirements.txt、server/webui 后台页）
  - 嵌入式 Python 运行时（随 PyInstaller 的 data 携带，安装时解压）

第一次运行 = 安装器：
  - 选择安装路径
  - 一键把"嵌入式 Python + 服务端源码"解压到该目录
  - 创建桌面快捷方式（指向启动脚本）
  - 自动启动服务

之后运行 = 服务端后台管理面板（原生 tkinter）：
  - 检测 8920 是否在运行（读 /healthz）
  - 已运行 → 显示"运行中"+ 管理页入口
  - 未运行 → 点击"启动服务"拉起
  - 右上角"卸载" → 二次确认 → 停服务 + 删安装目录 + 删快捷方式

注意：本脚本既可被 PyInstaller 打包（frozen, sys._MEIPASS 携带数据），
也可在开发目录直接运行（用本机 venv 跑，便于测试 UI 逻辑）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import messagebox, filedialog

APP_NAME = "KiteChat"
DEFAULT_PORT = 8920
DEFAULT_INSTALL_DIR = os.path.join(os.path.expanduser("~"), "KiteChat")

# ---------------------------------------------------------------- 路径
def bundle_dir() -> str:
    """数据目录：打包后 = sys._MEIPASS；开发 = 项目根。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # 开发模式：若 build/pkgsrc/KiteChatServer 已生成，直接用其父目录作为 bundle
    # （bundle_dir() 返回的目录下应有 KiteChatServer/）
    pkgs = os.path.join(root, "build", "pkgsrc")
    if os.path.isdir(os.path.join(pkgs, "KiteChatServer")):
        return pkgs
    return root


def install_root() -> str:
    """已安装的服务端根目录；未安装返回 ''。"""
    cfg_path = os.path.join(os.path.expanduser("~"), APP_NAME + ".install.json")
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            root = d.get("install_dir", "")
            if root and os.path.isfile(os.path.join(root, "run.py")):
                return root
        except (OSError, ValueError):
            pass
    return ""


def _write_state(install_dir: str) -> None:
    cfg_path = os.path.join(os.path.expanduser("~"), APP_NAME + ".install.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"install_dir": install_dir}, f)


def _clear_state() -> None:
    cfg_path = os.path.join(os.path.expanduser("~"), APP_NAME + ".install.json")
    try:
        os.remove(cfg_path)
    except OSError:
        pass


def state_path() -> str:
    return os.path.join(os.path.expanduser("~"), APP_NAME + ".install.json")


# ---------------------------------------------------------------- 服务进程
def server_python(root: str) -> str:
    """已安装副本里的服务端解释器（嵌入式复制出的 python）。"""
    cand = os.path.join(root, "python", "python.exe")
    return cand if os.path.isfile(cand) else ""


def _port_pid() -> int:
    """返回监听 DEFAULT_PORT 的 PID；0 表示未运行。"""
    try:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            if s.connect_ex(("127.0.0.1", DEFAULT_PORT)) != 0:
                return 0
        # 端口占用即视为在运行（无论 pid 是否可查）
        return -1
    except OSError:
        return 0


def is_running() -> bool:
    return _port_pid() != 0


def start_server(root: str) -> str:
    """启动服务端（detached，避免窗口被安装器带走）。"""
    py = server_python(root)
    if not py:
        return "未找到服务端解释器: python/python.exe"
    logp = os.path.join(root, "logs", "server.log")
    os.makedirs(os.path.dirname(logp), exist_ok=True)
    flog = open(logp, "w", encoding="utf-8", errors="replace")
    try:
        subprocess.Popen(
            [py, "run.py"], cwd=root,
            stdout=flog, stderr=subprocess.STDOUT,
            creationflags=0x00000008 | 0x08000000,  # DETACHED_PROCESS|CREATE_NO_WINDOW
        )
    finally:
        flog.close()
    return "ok"


def stop_server() -> None:
    """尝试停止监听 8920 的进程（尽力而为）。"""
    import urllib.request
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/api/admin/stop" % DEFAULT_PORT,
                               timeout=2)
        return
    except Exception:
        pass
    # 兜底：taskkill 占用端口进程
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=8).stdout
        pids = set()
        for line in out.splitlines():
            if f":{DEFAULT_PORT}" in line and "LISTENING" in line:
                parts = line.split()
                if parts and parts[-1].isdigit():
                    pids.add(parts[-1])
        for pid in pids:
            subprocess.run(["taskkill", "/F", "/PID", pid],
                           capture_output=True, timeout=8)
    except Exception:
        pass


def check_health() -> str:
    """返回 /healthz 结果用于 UI 展示。"""
    import urllib.request
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/healthz" % DEFAULT_PORT, timeout=2) as r:
            body = r.read().decode("utf-8", "replace")
            return body if body else "running"
    except Exception:
        return ""


# ---------------------------------------------------------------- 安装
def _copy_tree(src: str, dst: str) -> None:
    for root_dir, dirs, files in os.walk(src):
        rel = os.path.relpath(root_dir, src)
        tgt = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(tgt, exist_ok=True)
        for f in files:
            shutil.copy2(os.path.join(root_dir, f), os.path.join(tgt, f))


def do_install(target: str, progress_cb) -> str:
    """把 bundle 里的服务端 + 嵌入式 python 解压到 target。"""
    b = bundle_dir()
    src_server = os.path.join(b, "KiteChatServer")
    if not os.path.isdir(src_server):
        # 开发兜底：用项目根（编译产物）
        return "未找到内置服务端数据 (KiteChatServer)"
    os.makedirs(target, exist_ok=True)
    _copy_tree(src_server, target)
    # 生成启动脚本（用嵌入式 python）
    py = os.path.join(target, "python", "python.exe")
    bat = os.path.join(target, "启动服务端.bat")
    with open(bat, "w", encoding="ascii", newline="\r\n") as f:
        f.write("@echo off\r\n"
                'cd /d "%~dp0"\r\n'
                f'start "" "{py}" run.py\r\n')
    _write_state(target)
    return "ok"


def desktop_lnk(target: str) -> str:
    """在桌面创建真正的 .lnk 快捷方式（无 pywin32 依赖）。

    用 Python 内嵌的 PowerShell WScript.Shell COM 生成（Win10/11 自带 powershell），
    支持图标/目标程序/工作目录/启动参数。返回 .lnk 路径或 ""。
    """
    try:
        import ctypes
        sh = ctypes.windll.shell32
        buf = ctypes.create_unicode_buffer(300)
        # 0x0010 = CSIDL_DESKTOPDIRECTORY（真实桌面，兼容自定义桌面路径）
        rc = sh.SHGetFolderPathW(None, 0x0010, None, 0, buf)
        if rc != 0 or not buf.value:
            return ""
        desktop = buf.value
        lnk = os.path.join(desktop, APP_NAME + ".lnk")

        pyw = os.path.join(target, "python", "pythonw.exe")
        if not os.path.isfile(pyw):
            pyw = os.path.join(target, "python", "python.exe")
        if not os.path.isfile(pyw):
            return ""
        runpy = os.path.join(target, "run.py")
        if not os.path.isfile(runpy):
            return ""

        # 图标：优先安装目录内随包的 app.ico
        icon = ""
        for cand in (os.path.join(target, "client", "desktop", "app.ico"),
                     os.path.join(target, "server", "webui", "app.ico")):
            if os.path.isfile(cand):
                icon = cand
                break

        def psq(s: str) -> str:
            # PowerShell 单引号转义（单引号以内原样，'' 代表一个单引号）
            return "'" + str(s).replace("'", "''") + "'"

        lines = [
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut(%s)" % psq(lnk),
            "$s.TargetPath=%s" % psq(pyw),
            "$s.Arguments=%s" % psq('"' + runpy + '"'),
            "$s.WorkingDirectory=%s" % psq(target),
            "$s.WindowStyle=7",          # 最小化（pythonw 无控制台，尽量不闪窗）
            "$s.Description='KiteChat 服务端'",
        ]
        if icon:
            lines.append("$s.IconLocation=%s" % psq(icon))
        lines.append("$s.Save()")
        script = "\r\n".join(lines)

        tmp = os.path.join(tempfile.gettempdir(), "kitechat_mklnk.ps1")
        with open(tmp, "w", encoding="utf-8-sig") as f:
            f.write(script)
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", tmp],
                capture_output=True, timeout=20)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

        return lnk if os.path.isfile(lnk) else ""
    except Exception:
        return ""


def do_uninstall(root: str) -> None:
    stop_server()
    # 删除桌面快捷方式（与 desktop_lnk 保持一致：0x0010 定位 + .lnk；兼容旧版 .bat）
    try:
        import ctypes
        sh = ctypes.windll.shell32
        buf = ctypes.create_unicode_buffer(300)
        if sh.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0 and buf.value:
            desktop = buf.value
            for fn in (APP_NAME + ".lnk", APP_NAME + ".bat"):
                p = os.path.join(desktop, fn)
                if os.path.isfile(p):
                    os.remove(p)
    except Exception:
        pass
    # 删除安装目录
    _rmtree(root)
    _clear_state()


def _rmtree(root: str) -> None:
    def onerr(func, path, exc):
        try:
            os.chmod(path, 0o777)
            func(path)
        except Exception:
            pass
    shutil.rmtree(root, onerror=onerr)


# ---------------------------------------------------------------- GUI
class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("%s 服务端 一键安装包" % APP_NAME)
        self.geometry("620x440")
        self.resizable(False, False)
        self.configure(bg="#17161e")
        self._style()
        self.root_installed = install_root()
        self._build()

    def _style(self) -> None:
        self.BG = "#17161e"
        self.PANEL = "#1f1e28"
        self.BORDER = "#2c2b3a"
        self.FG = "#ece9f6"
        self.MUTED = "#9a97ad"
        self.ACCENT = "#6c9bff"
        self.GREEN = "#3ecf8e"
        self.RED = "#ff6b6b"

    def _build(self) -> None:
        self.configure(bg=self.BG)
        # 标题
        tk.Label(self, text=APP_NAME, font=("Segoe UI", 20, "bold"),
                 bg=self.BG, fg=self.FG).pack(anchor="w", padx=26, pady=(22, 2))
        tk.Label(self, text="OneBot V11 私有化 AI 聊天 · 服务端",
                 bg=self.BG, fg=self.MUTED).pack(anchor="w", padx=26)

        self.frame = tk.Frame(self, bg=self.PANEL, bd=0,
                              highlightthickness=1, highlightbackground=self.BORDER)
        self.frame.pack(fill="both", expand=True, padx=26, pady=18)
        self._build_installed() if self.root_installed else self._build_installer()

    # ---------- 安装界面 ----------
    def _build_installer(self) -> None:
        self._card_clear(self.frame)
        tk.Label(self.frame, text="尚未安装", font=("Segoe UI", 14, "bold"),
                 bg=self.PANEL, fg=self.FG).pack(anchor="w", padx=22, pady=(20, 4))
        tk.Label(self.frame, text="选择安装路径，点击「一键安装」即可完成部署（无需安装 Python）。",
                 bg=self.PANEL, fg=self.MUTED, wraplength=520, justify="left"
                 ).pack(anchor="w", padx=22, pady=(0, 12))

        row = tk.Frame(self.frame, bg=self.PANEL)
        row.pack(anchor="w", padx=22, pady=6)
        self.path_var = tk.StringVar(value=DEFAULT_INSTALL_DIR)
        tk.Entry(row, textvariable=self.path_var, width=44,
                 bg=self.BG, fg=self.FG, insertbackground=self.FG,
                 relief="flat", highlightthickness=1,
                 highlightbackground=self.BORDER).pack(side="left", ipady=5)
        tk.Button(row, text="浏览…", command=self._browse, bg=self.BG,
                  fg=self.MUTED, relief="flat", padx=12).pack(side="left", padx=8)

        self.status_var = tk.StringVar(value="")
        tk.Label(self.frame, textvariable=self.status_var, bg=self.PANEL,
                 fg=self.MUTED).pack(anchor="w", padx=22, pady=(10, 4))

        btnbar = tk.Frame(self.frame, bg=self.PANEL)
        btnbar.pack(side="bottom", fill="x", padx=22, pady=(10, 20))
        tk.Button(btnbar, text="一键安装", command=self._install, bg=self.ACCENT,
                  fg="#0d0d12", relief="flat", padx=24, pady=6, font=("Segoe UI", 11, "bold")
                  ).pack(side="left")

    def _browse(self) -> None:
        fp = filedialog.askdirectory(initialdir=os.path.expanduser("~"))
        if fp:
            self.path_var.set(fp)

    def _install(self) -> None:
        target = os.path.join(self.path_var.get(), APP_NAME)
        self.status_var.set("安装中…")
        self.update()
        def _run():
            try:
                res = do_install(target, None)
            except Exception as e:
                res = str(e)
            self.after(0, lambda: self._on_install_done(target, res))
        threading.Thread(target=_run, daemon=True).start()

    def _on_install_done(self, target: str, res: str) -> None:
        if res != "ok":
            self.status_var.set("安装失败: " + res)
            return
        lnk = desktop_lnk(target)
        start_server(target)
        self.status_var.set("安装完成并已启动。\n桌面快捷方式: %s" % (lnk or "已创建"))
        self.root_installed = target
        self.after(500, lambda: self._build_installed())

    # ---------- 后台面板 ----------
    def _card_clear(self, frame: tk.Frame) -> None:
        for w in frame.winfo_children():
            w.destroy()

    def _build_installed(self) -> None:
        self._card_clear(self.frame)
        root = self.root_installed
        # 右上角卸载
        topbar = tk.Frame(self.frame, bg=self.PANEL)
        topbar.pack(fill="x", padx=22, pady=(14, 0))
        tk.Button(topbar, text="卸载", command=self._uninstall, bg=self.BG,
                  fg=self.RED, relief="flat", font=("Segoe UI", 9)
                  ).pack(side="right")

        tk.Label(self.frame, text="服务端后台", font=("Segoe UI", 14, "bold"),
                 bg=self.PANEL, fg=self.FG).pack(anchor="w", padx=22, pady=(8, 2))
        tk.Label(self.frame, text="安装路径: %s" % root, bg=self.PANEL, fg=self.MUTED,
                 wraplength=520, justify="left").pack(anchor="w", padx=22)

        self.dot = tk.Canvas(self.frame, width=14, height=14, bg=self.PANEL,
                             highlightthickness=0)
        self.dot.pack(anchor="w", padx=22, pady=(16, 2))
        self.dot_circle = self.dot.create_oval(2, 2, 12, 12, fill=self.MUTED)
        self.state_lbl = tk.Label(self.frame, text="检测中…", bg=self.PANEL, fg=self.FG,
                                  font=("Segoe UI", 13))
        self.state_lbl.pack(anchor="w", padx=22)

        self.health_lbl = tk.Label(self.frame, text="", bg=self.PANEL, fg=self.MUTED,
                                   justify="left", wraplength=520)
        self.health_lbl.pack(anchor="w", padx=22, pady=(6, 0))

        btnbar = tk.Frame(self.frame, bg=self.PANEL)
        btnbar.pack(side="bottom", fill="x", padx=22, pady=(10, 20))
        self.start_btn = tk.Button(btnbar, text="启动服务", command=self._run,
                                   bg=self.ACCENT, fg="#0d0d12", relief="flat",
                                   padx=22, pady=6, font=("Segoe UI", 11, "bold"))
        self.start_btn.pack(side="left")
        self.open_btn = tk.Button(btnbar, text="打开后台", command=self._open_admin,
                                  bg=self.BG, fg=self.FG, relief="flat", padx=16, pady=6)
        self.open_btn.pack(side="left", padx=10)
        self.refresh_btn = tk.Button(btnbar, text="刷新", command=self.refresh,
                                     bg=self.BG, fg=self.MUTED, relief="flat", padx=10)
        self.refresh_btn.pack(side="left")

        self.refresh()

    def refresh(self) -> None:
        self.dot.itemconfig(self.dot_circle, fill=self.MUTED)
        if is_running():
            h = check_health()
            self.dot.itemconfig(self.dot_circle, fill=self.GREEN)
            self.state_lbl.config(text="运行中", fg=self.GREEN)
            self.health_lbl.config(text=("后台: " + h[:120]) if h else "后台已启动")
            self.start_btn.config(state="disabled", text="运行中")
        else:
            self.dot.itemconfig(self.dot_circle, fill=self.RED)
            self.state_lbl.config(text="未运行", fg=self.RED)
            self.health_lbl.config(text="")
            self.start_btn.config(state="normal", text="启动服务")

    def _run(self) -> None:
        r = start_server(self.root_installed)
        if r == "ok":
            self.after(1200, self.refresh)
        else:
            messagebox.showerror("启动失败", r)

    def _open_admin(self) -> None:
        import webbrowser
        webbrowser.open("http://127.0.0.1:%d/admin" % DEFAULT_PORT)

    def _uninstall(self) -> None:
        if not messagebox.askyesno("确认卸载",
                                   "将停止服务并删除安装目录 %s，确定？\n（此操作不可恢复）"
                                   % self.root_installed):
            return
        self.status_var.set("卸载中…")
        self.update()
        root = self.root_installed
        def _run():
            do_uninstall(root)
            self.after(0, self._on_uninstall_done)
        threading.Thread(target=_run, daemon=True).start()

    def _on_uninstall_done(self) -> None:
        self.root_installed = ""
        self._build_installer()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
