#!/usr/bin/env python3
"""KiteChat 服务端 Windows 一键安装包 —— 打包脚本。

流程：
  1. 下载嵌入式 Python (python-<ver>-embed-amd64.zip) 到 build/pkgsrc/KiteChatServer/python
  2. 复制服务端源码 (server/, run.py, requirements.txt, server/webui) 到 KiteChatServer/
  3. 给嵌入式 Python 注入依赖 (aiohttp 等) —— 嵌入式无 pip，用 wheel 解包到 site-packages
  4. PyInstaller 打包 tools/windows_installer.py 为单 EXE，把 KiteChatServer 挂为 datas

产物： dist/KiteChat-Server-Installer.exe

用法： python tools/build_windows_installer.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
BUILD = os.path.join(ROOT, "build")
PKGSRC = os.path.join(BUILD, "pkgsrc")
SERVER_HOME = os.path.join(PKGSRC, "KiteChatServer")
PY_VER = "3.11.9"
EMBED_URL = ("https://registry.npmmirror.com/-/binary/python/%s/"
             "python-%s-embed-amd64.zip") % (PY_VER, PY_VER)

# 需要注入的依赖（服务端运行必需；pywebview/pyinstaller 仅客户端导出用，服务端不需要）
SERVER_DEPS = ["aiohttp", "pillow"]


def log(msg): print("\033[1;34m[build]\033[0m %s" % msg)
def ok(msg):  print("\033[1;32m[✓]\033[0m %s" % msg)
def die(msg): print("\033[1;31m[x]\033[0m %s" % msg, file=sys.stderr); sys.exit(1)


def download(url: str, out: str) -> None:
    log("下载 %s" % url)
    req = urllib.request.Request(url, headers={"User-Agent": "agent/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(out, "wb") as f:
        shutil.copyfileobj(r, f)
    ok("  下载完成 -> %s" % out)


def ensure_embed_python() -> str:
    """解压嵌入式 Python 到 SERVER_HOME/python，返回该目录。"""
    python_dir = os.path.join(SERVER_HOME, "python")
    if os.path.isfile(os.path.join(python_dir, "python.exe")):
        ok("嵌入式 Python 已就绪: %s" % python_dir)
        return python_dir
    os.makedirs(python_dir, exist_ok=True)
    ztmp = os.path.join(BUILD, "embed.zip")
    download(EMBED_URL, ztmp)
    with zipfile.ZipFile(ztmp) as z:
        z.extractall(python_dir)
    os.remove(ztmp)
    ok("嵌入式 Python 解压完成")
    return python_dir


def copy_server_source() -> None:
    """把服务端源码复制到 SERVER_HOME（不含 .venv / tools 大件 / 数据）。"""
    os.makedirs(SERVER_HOME, exist_ok=True)
    shutil.copy2(os.path.join(ROOT, "run.py"), SERVER_HOME)
    shutil.copy2(os.path.join(ROOT, "requirements.txt"), SERVER_HOME)
    # 幂等：旧构建残留的子目录先删掉再复制，避免 copytree 目标已存在报 FileExistsError
    shutil.rmtree(os.path.join(SERVER_HOME, "server"), ignore_errors=True)
    shutil.copytree(os.path.join(ROOT, "server"), os.path.join(SERVER_HOME, "server"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    # 服务端 / 与 /static/ 需要客户端网页资源
    shutil.rmtree(os.path.join(SERVER_HOME, "client", "web"), ignore_errors=True)
    shutil.copytree(os.path.join(ROOT, "client", "web"),
                    os.path.join(SERVER_HOME, "client", "web"),
                    ignore=shutil.ignore_patterns("config.bin"))
    # webui 图标处理用 desktop/app.ico 可能被引用，拷贝一份
    os.makedirs(os.path.join(SERVER_HOME, "client", "desktop"), exist_ok=True)
    ico = os.path.join(ROOT, "client", "desktop", "app.ico")
    if os.path.isfile(ico):
        shutil.copy2(ico, os.path.join(SERVER_HOME, "client", "desktop", "app.ico"))
    ok("服务端源码已复制")


def inject_deps(python_dir: str) -> None:
    """嵌入式 Python 无 pip —— 用系统 pip download 拿 wheel，解包到 site-packages。"""
    site_pkg = os.path.join(python_dir, "Lib", "site-packages")
    os.makedirs(site_pkg, exist_ok=True)
    dl = os.path.join(BUILD, "wheels")
    os.makedirs(dl, exist_ok=True)
    # 找一个有 pip 的解释器（本机 venv 无 pip，用系统 python）
    pips = [sys.executable]
    if not _has_pip(pips[0]):
        for cand in (r"D:\Program Files\Python\python.exe",
                     r"C:\Program Files\Python\python.exe",
                     shutil.which("python")):
            if cand and _has_pip(cand):
                pips = [cand]
                break
    if not _has_pip(pips[0]):
        die("找不到带 pip 的 Python，无法下载依赖 wheel")
    cmd = pips + ["-m", "pip", "download", "--only-binary=:all:",
                  "--platform=win_amd64", "--python-version=311",
                  "--implementation=cp", "--abi=cp311", "-d", dl] + SERVER_DEPS
    log("下载依赖 wheel: %s" % " ".join(SERVER_DEPS))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        die("pip download 失败: %s" % (r.stderr or r.stdout)[-800:])
    count = 0
    for fn in os.listdir(dl):
        if fn.endswith(".whl"):
            wp = os.path.join(dl, fn)
            with zipfile.ZipFile(wp) as z:
                z.extractall(site_pkg)
            count += 1
    ok("已注入 %d 个依赖 wheel 到 site-packages" % count)
    shutil.rmtree(dl, ignore_errors=True)
    # 让嵌入 python 能找到 site-packages（._pth 加一行，幂等）
    pth_files = [f for f in os.listdir(python_dir) if f.endswith("._pth")]
    for pf in pth_files:
        pth = os.path.join(python_dir, pf)
        with open(pth, "r", encoding="utf-8") as f:
            lines = [l.rstrip() for l in f.readlines()]
        if "Lib\\site-packages" not in lines:
            lines.append("Lib\\site-packages")
            with open(pth, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            ok("已把 site-packages 写入 %s" % pf)


def _has_pip(py: str) -> bool:
    if not py or not os.path.isfile(py):
        return False
    r = subprocess.run([py, "-m", "pip", "--version"], capture_output=True,
                       text=True, timeout=20)
    return r.returncode == 0


def pyinstaller_bundle() -> None:
    """用 PyInstaller 打包 windows_installer.py，KiteChatServer 挂为 datas。"""
    log("PyInstaller 打包单 EXE …")
    # 生成 spec 数据挂载参数（data 为 (源, 目标)）
    datas_arg = os.path.join(SERVER_HOME, "KiteChatServer")
    spec = os.path.join(BUILD, "kite_installer.spec")
    icon = os.path.join(ROOT, "client", "desktop", "app.ico")
    with open(spec, "w", encoding="utf-8") as f:
        f.write("""# -*- mode: python ; coding: utf-8 -*-
a = Analysis(
    [%r],
    pathex=[],
    binaries=[],
    datas=[(%r, 'KiteChatServer')],
    hiddenimports=['tkinter'],
    hookspath=[],
    runtime_hooks=[],
    excludes=['pywebview', 'PyInstaller', 'PIL', 'pywin32', 'win32com',
              'pythoncom', 'win32api', 'win32con', 'win32gui', 'win32process',
              'win32evtlog', 'win32event', 'win32job'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    name='KiteChat-Server-Installer',
    console=False,
    icon=%r if %r else None,
    upx=False,
)
""" % (os.path.join(TOOLS, "windows_installer.py"), SERVER_HOME,
             icon if os.path.isfile(icon) else "", icon if os.path.isfile(icon) else ""))
    cmd = [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
           "--distpath", os.path.join(ROOT, "dist"),
           "--workpath", os.path.join(BUILD, "pyi-work"),
           spec]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        die("PyInstaller 失败: %s" % (r.stderr or r.stdout)[-1500:])
    ok("打包完成 -> dist/KiteChat-Server-Installer.exe")


def main() -> None:
    os.makedirs(BUILD, exist_ok=True)
    python_dir = ensure_embed_python()
    copy_server_source()
    inject_deps(python_dir)
    pyinstaller_bundle()
    ok("全部完成")


if __name__ == "__main__":
    main()
