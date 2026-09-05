#!/usr/bin/env python3
"""KiteChat 服务端 —— macOS 一键安装包（tkinter GUI）。

首次运行 = 安装器：
  - 选择安装路径
  - 一键把"服务端源码"解压到该目录
  - 创建 .command 启动脚本
  - 自动启动服务

之后运行 = 服务端后台管理面板（tkinter）：
  - 检测 8920 是否在运行（读 /healthz）
  - 已运行 → 显示"运行中"+ 管理页入口
  - 未运行 → 点击"启动服务"拉起
  - 右上角"卸载" → 二次确认 → 停服务 + 删安装目录

运行方式：
  python3 macos_installer.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, filedialog

APP_NAME = "KiteChat"
DEFAULT_PORT = 8920
DEFAULT_INSTALL_DIR = os.path.join(os.path.expanduser("~"), "KiteChat")


# ---------------------------------------------------------------- 路径
def bundle_dir() -> str:
    """数据目录：开发模式 = 项目根。"""
    root = os.path.dirname(os.path.abspath(__file__))
    if os.path.isdir(os.path.join(root, "server")):
        return root
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


def installed_python(install_dir: str) -> str:
    """已安装副本里的 Python 解释器路径。"""
    py = os.path.join(install_dir, "python", "bin", "python3")
    if os.path.isfile(py):
        return py
    # 系统 python3
    try:
        return subprocess.check_output(["which", "python3"], text=True, timeout=5).strip()
    except Exception:
        return ""


def start_server(install_dir: str) -> None:
    """启动服务端（detached）。"""
    py = installed_python(install_dir)
    if not py:
        return
    runpy = os.path.join(install_dir, "run.py")
    if not os.path.isfile(runpy):
        return
    subprocess.Popen(
        [py, runpy],
        cwd=install_dir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def stop_server(install_dir: str) -> None:
    """停止服务端（kill 所有监听 8920 的进程）。"""
    try:
        # 找占用端口的 PID
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


def _copy_tree(src: str, dst: str) -> None:
    for root_dir, dirs, files in os.walk(src):
        rel = os.path.relpath(root_dir, src)
        tgt = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(tgt, exist_ok=True)
        for f in files:
            shutil.copy2(os.path.join(root_dir, f), os.path.join(tgt, f))


def do_install(target: str) -> str:
    """把 bundle 里的服务端解压到 target。"""
    b = bundle_dir()
    src_server = os.path.join(b, "server")
    src_runpy = os.path.join(b, "run.py")
    src_req = os.path.join(b, "requirements.txt")
    src_webui = os.path.join(b, "server", "webui")
    src_client = os.path.join(b, "client")

    if not os.path.isdir(src_server) and not os.path.isfile(src_runpy):
        return "未找到内置服务端数据"

    os.makedirs(target, exist_ok=True)

    # 复制服务端
    if os.path.isdir(src_server):
        _copy_tree(src_server, os.path.join(target, "server"))
    if os.path.isfile(src_runpy):
        shutil.copy2(src_runpy, os.path.join(target, "run.py"))
    if os.path.isfile(src_req):
        shutil.copy2(src_req, os.path.join(target, "requirements.txt"))
    if os.path.isdir(src_client):
        _copy_tree(src_client, os.path.join(target, "client"))

    # 生成 .command 启动脚本
    cmd_path = os.path.join(target, "启动服务端.command")
    with open(cmd_path, "w", encoding="utf-8") as f:
        f.write("#!/bin/bash\n")
        f.write('cd "$(dirname "$0")"\n')
        f.write("python3 run.py\n")
    os.chmod(cmd_path, 0o755)

    # 安装依赖
    req = os.path.join(target, "requirements.txt")
    if os.path.isfile(req):
        subprocess.run(
            ["pip3", "install", "-r", req],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120
        )

    _write_state(target)
    return "ok"


def do_uninstall(root: str) -> None:
    """卸载：停服务 + 删除安装目录 + 清状态。"""
    stop_server(root)
    shutil.rmtree(root, onerror=lambda *_: None)
    _clear_state()


# ============================================================ GUI
class App(tk.Tk):
    BG = "#1a1a2e"
    PANEL = "#16213e"
    FG = "#e0e0e0"
    MUTED = "#888"
    ACCENT = "#0f3460"
    RED = "#e74c3c"
    BORDER = "#333"

    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} 服务端 一键安装包 (macOS)")
        self.configure(bg=self.BG)
        self.geometry("580x340")
        self.resizable(False, False)
        self.frame = tk.Frame(self, bg=self.PANEL)
        self.frame.pack(fill="both", expand=True, padx=10, pady=10)

        self.root_installed = install_root()
        self._build_installed() if self.root_installed else self._build_installer()

    # ---------- 安装界面 ----------
    def _build_installer(self) -> None:
        self._card_clear(self.frame)
        tk.Label(self.frame, text="尚未安装", font=("Helvetica", 14, "bold"),
                 bg=self.PANEL, fg=self.FG).pack(anchor="w", padx=22, pady=(20, 4))
        tk.Label(self.frame, text="选择安装路径，点击「一键安装」即可完成部署。",
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
                  fg="#fff", relief="flat", padx=24, pady=6, font=("Helvetica", 11, "bold")
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
                res = do_install(target)
            except Exception as e:
                res = str(e)
            self.after(0, lambda: self._on_install_done(target, res))
        threading.Thread(target=_run, daemon=True).start()

    def _on_install_done(self, target: str, res: str) -> None:
        if res != "ok":
            self.status_var.set("安装失败: " + res)
            return
        start_server(target)
        self.status_var.set("安装完成并已启动。")
        self.root_installed = target
        self.after(500, lambda: self._build_installed())

    # ---------- 后台面板 ----------
    def _card_clear(self, frame: tk.Frame) -> None:
        for w in frame.winfo_children():
            w.destroy()

    def _build_installed(self) -> None:
        self._card_clear(self.frame)
        root = self.root_installed
        topbar = tk.Frame(self.frame, bg=self.PANEL)
        topbar.pack(fill="x", padx=22, pady=(14, 0))
        tk.Button(topbar, text="卸载", command=self._uninstall, bg=self.BG,
                  fg=self.RED, relief="flat", font=("Helvetica", 9)
                  ).pack(side="right")

        tk.Label(self.frame, text="服务端后台", font=("Helvetica", 14, "bold"),
                 bg=self.PANEL, fg=self.FG).pack(anchor="w", padx=22, pady=(8, 2))
        tk.Label(self.frame, text="安装路径: %s" % root, bg=self.PANEL, fg=self.MUTED,
                 wraplength=520, justify="left").pack(anchor="w", padx=22)

        self.dot = tk.Canvas(self.frame, width=14, height=14, bg=self.PANEL,
                             highlightthickness=0)
        self.dot.pack(anchor="w", padx=22, pady=(16, 2))
        self.dot_circle = self.dot.create_oval(2, 2, 12, 12, fill=self.MUTED)
        self.state_lbl = tk.Label(self.frame, text="检测中…", bg=self.PANEL, fg=self.FG,
                                  font=("Helvetica", 13))
        self.state_lbl.pack(anchor="w", padx=22)

        self.health_lbl = tk.Label(self.frame, text="", bg=self.PANEL, fg=self.MUTED,
                                   justify="left", wraplength=520)
        self.health_lbl.pack(anchor="w", padx=22, pady=(6, 0))

        btnbar = tk.Frame(self.frame, bg=self.PANEL)
        btnbar.pack(side="bottom", fill="x", padx=22, pady=(10, 20))
        self.btn_start = tk.Button(btnbar, text="启动服务", command=self._start,
                                   bg=self.ACCENT, fg="#fff", relief="flat", padx=20, pady=6)
        self.btn_start.pack(side="left")
        self.btn_stop = tk.Button(btnbar, text="停止服务", command=self._stop,
                                  bg=self.BG, fg=self.FG, relief="flat", padx=20, pady=6)
        self.btn_stop.pack(side="left", padx=8)
        self.btn_panel = tk.Button(btnbar, text="打开后台面板",
                                   command=self._open_panel, bg=self.BG,
                                   fg=self.MUTED, relief="flat", padx=20, pady=6)
        self.btn_panel.pack(side="left")

        self._check_health()

    def _check_health(self) -> None:
        import urllib.request
        try:
            r = urllib.request.urlopen(f"http://127.0.0.1:{DEFAULT_PORT}/healthz", timeout=2)
            data = json.loads(r.read())
            if data.get("status") == "ok":
                self.dot.itemconfig(self.dot_circle, fill="#2ecc71")
                self.state_lbl.config(text="运行中")
                self.health_lbl.config(text=f"端口 {DEFAULT_PORT}")
                self.btn_start.config(state="disabled")
                self.btn_stop.config(state="normal")
            else:
                raise ValueError("not ok")
        except Exception:
            self.dot.itemconfig(self.dot_circle, fill=self.RED)
            self.state_lbl.config(text="未运行")
            self.health_lbl.config(text="")
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")
        self.after(3000, self._check_health)

    def _start(self) -> None:
        start_server(self.root_installed)
        self.after(1000, self._check_health)

    def _stop(self) -> None:
        stop_server(self.root_installed)
        self.after(1000, self._check_health)

    def _open_panel(self) -> None:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{DEFAULT_PORT}/admin")

    def _uninstall(self) -> None:
        if not messagebox.askyesno("确认卸载",
                                   "将停止服务并删除安装目录 %s，确定？\n（此操作不可恢复）"
                                   % self.root_installed):
            return
        self.status_var.set("卸载中…") if hasattr(self, "status_var") else None
        self.state_lbl.config(text="卸载中…") if hasattr(self, "state_lbl") else None
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
