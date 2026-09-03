# KiteChat

**核心特点**：服务端独立部署 + 多端客户端（Windows EXE / Android APK），
全程 OneBot V11 标准协议通信，与各 QQ 机器人框架无缝对接。

**支持平台**：Windows / Linux（服务端；客户端导出双端）。mac 暂无适配计划。

```
KiteChat/            # 本仓库根目录
├── run.py                  # 服务端启动入口
├── requirements.txt
├── server/                 # 服务端核心（aiohttp 单端口：HTTP API + 客户端WS + OneBot反向WS + WebUI）
│   ├── web.py              #   REST API / WS 端点 / 静态托管
│   ├── hub.py              #   在线状态、消息路由、好友逻辑（AI 回复全部转发给 AstrBot）
│   ├── bot_bridge.py       #   OneBot V11 双向桥接（正向连接 + 反向接入 /onebot）
│   ├── onebot.py           #   OneBot V11 消息段解析（CQ码 <-> 消息段，合并转发）
│   ├── db.py               #   SQLite 存储（用户/验证码/会话/消息/好友/配置）
│   ├── mailer.py           #   SMTP 验证码邮件

│   ├── exporter.py         #   一键导出引擎（注入WS地址 + PyInstaller + Gradle签名）
│   ├── config.py           #   路径/密码哈希/网络工具
│   └── webui/index.html    #   服务端管理后台 WebUI
├── client/
│   ├── web/                # 客户端前端（登录注册/多会话/好友/合并转发，EXE与APK共用）
│   │   ├── index.html / style.css / app.js
│   ├── desktop/            # Windows 壳（pywebview，PyInstaller 打包成 EXE）
│   └── android/            # Android 壳（WebView，Gradle 打包成 APK）
├── tools/                  # 环境工具：Android SDK、Gradle、E2E 测试
├── exports/                # 一键导出产物（EXE zip / 签名 APK）
├── data/                   # SQLite 数据库、签名密钥
└── logs/
```

## Windows 一键安装（服务端 · 无需装 Python）

> **给最终用户最省事的装法**：拿到 `KiteChat-Server-Installer.exe`（服务端一键安装包，**自带 Python 运行时，无需预装任何东西**），双击运行即可。

**安装流程（全自动）：**
1. 双击 `KiteChat-Server-Installer.exe`
2. 在「安装界面」选择安装路径（默认 `~\KiteChat`），点 **一键安装**
3. 安装器自动：解压服务端 + 嵌入式 Python 运行时 → 创建**桌面快捷方式** → 自动启动服务端
4. 之后每次打开桌面快捷方式/安装器 = **服务端后台面板**（原生窗口，非网页壳）：
   - **运行中** → 绿点，显示后台状态，可点「打开后台」进 WebUI
   - **未运行** → 红点，点「**启动服务**」一键拉起
   - 右上角「**卸载**」→ 二次确认 → 停止服务 + 删除安装目录 + 删桌面快捷方式

**特点：** 完全自包含（内置 Python 3.11 + aiohttp/pillow），服务端源码以数据形式打进 EXE，安装到哪都能独立跑，不污染系统、不装任何全局依赖。mac 暂无适配计划。

**从源码构建安装包：**
```bash
python tools/build_windows_installer.py   # 产出 dist/KiteChat-Server-Installer.exe
```

## 快速开始（服务端）

### 一键启动（推荐，最快）

进入项目目录后，**双击 / 运行启动脚本即可**——它会自动创建虚拟环境、安装依赖并启动服务端，你需要 Python ≥ 3.11（首次会自动装好依赖）：

| 平台 | 一键启动 |
|---|---|
| **Windows** | 双击 `启动服务端.bat` |
| **Linux** | `bash 启动服务端.sh` |

> 两个脚本都会**首次自动创建 `.venv`**（`uv` 优先、`pip` 兜底）并安装依赖，之后直接启动服务端监听 8920。依赖只有三个：`aiohttp`（运行必需）、`pywebview` + `pyinstaller`（一键导出 Windows EXE 时用）。

### 若一键启动不行 → 手动执行命令

```bash
# 进入项目目录（本仓库根目录）
cd ./kitechat
# 需要 Python >= 3.11（3.11/3.12/3.14 均可用；pyproject.toml 的 requires-python 已锁定下限）
# 方式一（推荐，uv 管理，依赖锁定在 uv.lock，uv 会自动挑满足要求的 Python）：
uv venv .venv
uv sync                          # 按 pyproject.toml + uv.lock 精确安装

# 方式二（没装 uv 时自动等价，Python 自带 venv + pip，需本机 Python >= 3.11）：
python -m venv .venv
# Linux: .venv/bin/python -m pip install -r requirements.txt
# Windows: .venv\Scripts\python -m pip install -r requirements.txt
```

启动后控制台打印：

```
WebUI 后台 :  http://<局域网IP>:8920/admin     ← 管理员令牌也在这里打印
客户端页面 :  http://<局域网IP>:8920/          ← 浏览器直接可用
WS 接入    :  ws://<局域网IP>:8920/ws          ← 客户端 WebSocket
OneBot V11 :  ws://<局域网IP>:8920/onebot      ← Bot 反向 WS 接入
```

## WebUI 后台（/admin）

1. **服务端设置**：手动填写服务端 WS 地址（`ws://ip:端口`，留空自动取局域网 IP）。
2. **SMTP 邮箱**：发件邮箱、授权码、SMTP 端口、服务器地址、SSL/STARTTLS；
   支持一键发送测试邮件。注册验证码由服务端随机生成并通过该 SMTP 发送。
3. **AI / Bot 接入**：
   - OneBot V11 Bot 正向 WS（NapCat / go-cqhttp 等标准实现）
   - OneBot 应用反向 WS（AstrBot 等）——所有 AI 会话回复均来自该应用
4. **一键导出客户端**：
   - 自动把当前 WS 地址注入客户端，编译 **Windows EXE** 与 **Android APK**；
   - 用户下载后打开即用，无需任何配置；
   - 产物在「已构建的客户端」列表下载。
5. **用户管理**：全部注册用户、在线状态、删除。
6. **运行日志**：最近 200 行。

## 一键导出客户端说明

| 平台 | 技术 | 产物 |
|---|---|---|
| Windows | pywebview(WebView2) + PyInstaller onefile | `exports/KiteChat-Windows-*.exe`（单文件，双击即用，无需解压安装） |
| Android | 原生 WebView 壳 + Gradle + apksigner | `exports/android/KiteChat-android-*.apk`（直接安装） |

导出前需保证：
- 服务端运行环境装有 Python **≥ 3.11** + aiohttp/pywebview/pyinstaller。
  若未安装，可先执行：`pip install -r requirements.txt`
- Android 构建需要 **JDK 17+** 与 **Android SDK**。**导出 APK 时才检查，不影响服务端启动**：
  安装器 `tools/setup_android_sdk.py`（跨平台，无 Git 依赖）自动完成 **检测 → 缺失自动安装 → 写入配置**：
  - **检测顺序**（平台自适应：Windows 查 Java/Program Files，Linux 查 /usr/lib/jvm、/opt）：
    - SDK：环境变量 `ANDROID_HOME` → `ANDROID_SDK_ROOT` → 项目内 `tools/android-sdk` → 常见路径
    - JDK：环境变量 `JAVA_HOME` → `java` 命令解析 → 项目内 `tools/jdk` → 常见 JDK 目录
  - **检测到**：自动把 `sdk.dir` 写入 `client/android/local.properties`，无需手动填。
  - **未检测到（自动安装）**：直接按平台下载安装到**项目内**，无需用户动手：
    - Windows：`commandlinetools-win` + JDK `windows/x64`；Linux：`commandlinetools-linux` + JDK `linux/x64`
    - Android SDK → `tools/android-sdk/`（自动装 `platform-tools`、`platforms;android-35`、`build-tools;35.0.0`）
    - JDK 17（Temurin）→ `tools/jdk/`；安装后自动写配置，可直接开始构建。
- **执行入口**（三者等价，自动检测平台）：
  - `python tools/setup_android_sdk.py`（推荐，跨平台）
  - Windows 也可双击 `tools/setup_android_sdk.bat`
  - Linux 也可 `bash tools/setup_android_sdk.sh`
- **手动安装**（可选）：Android 命令行工具官网 <https://developer.android.com/studio#command-line-tools-only> ；OpenJDK 17+ <https://adoptium.net/temurin/releases/?version=17> ；然后 `export ANDROID_HOME=<你的sdk路径>`、`export JAVA_HOME=<你的jdk路径>`。

| 依赖 | 安装到 | 大约占用空间 |
|---|---|---|
| Android SDK（commandlinetools + platform-tools + android-35 + build-tools 35.0.0） | `tools/android-sdk/` | **约 1.5~2.5 GB** |
| JDK 17（Temurin） | `tools/jdk/` | **约 200~300 MB** |

## 客户端功能

- 打开自动连接服务端（地址已内置），自带注册/登录。
- **注册流程**：用户名 → 密码+确认密码 → 绑定邮箱 → 获取邮箱验证码（服务端随机生成、SMTP 发送）→ 输入验证码完成注册。
- 多会话管理（AI 对话可建多个、可重命名/删除；好友私聊独立会话）。
- 文字消息实时收发、输入中提示、在线状态同步。
- **OneBot V11 合并转发**：完整解析 forward/node 消息段，卡片式展示，点开查看全部消息。
- 好友系统：搜索用户名添加、申请/同意/拒绝、好友列表、在线状态、右键删除。
- 会话与消息本地缓存（localStorage），启动秒开。
- 响应式 UI：桌面双栏 / 移动端抽屉式侧栏。

## OneBot V11 协议兼容

- 消息全程使用 OneBot V11 **消息段格式**（`[{"type":"text","data":{"text":"..."}}]`），
  同时兼容 CQ 码字符串自动转换。
- 事件格式对齐 OneBot：`post_type: message/notice/request/meta_event`。
- `/onebot` 端点为标准 **反向 WebSocket** 接入：Bot 连上来后会收到
  `lifecycle.connect`，可调用 `send_private_msg / get_login_info /
  get_friend_list / get_version_info / get_status`。
- KiteChat 用户拥有虚拟号（代号），从 **#1** 开始递增（第一个用户 #1，第二个 #2…）；Bot 给虚拟号发消息
  即路由到对应用户的 AI 会话。
- 服务端也可作为**正向 WS 客户端**连接外部 Bot（后台填 Bot WS 地址）。
- AI 会话消息统一转发给 OneBot V11 应用（如 AstrBot），KiteChat 本身不带任何 AI 模型；
  WS 未连接时会提示「WS 未连接」。

## 开发/测试

```bash
# E2E 测试（覆盖管理API/注册/登录/WS通信/好友/私聊/历史/合并转发/OneBot桥接/CQ解析）
.venv/Scripts/python.exe tools/e2e_test.py
# 重新生成管理员令牌
.venv/Scripts/python.exe run.py --new-token
```

## 客户端 WS 协议（客户端 <-> 服务端）

认证后双向 JSON：客户端发 `{"op":"message|create_session|history|friend_add|..."}`，
服务端回 `{"op":"result",...}`，并推送 OneBot 风格事件（详见 `server/protocol.py` 头注）。

## 开源协议

本项目基于 **MIT License** 开源（见 [LICENSE](LICENSE)）。
允许免费使用、复制、修改、合并、发布、分发、再许可及销售，只需保留版权声明与许可协议。
