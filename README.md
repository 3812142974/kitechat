# KiteChat

**基于 OneBot V11 协议的私有化 AI 聊天双向通信系统**

服务端独立部署 + 多端客户端（Windows EXE / Android APK），
体验对标豆包 / DeepSeek / QQ，全程 OneBot V11 标准协议通信。

```
d:/bot/KiteChat/
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

## 快速开始（服务端）

```bash
cd d:/bot/KiteChat
# 方式一（推荐，uv 管理，依赖锁定在 uv.lock）：
uv venv .venv --python 3.11
uv sync                          # 按 pyproject.toml + uv.lock 精确安装

# 方式二（没装 uv 时自动等价，Python 自带 venv + pip）：
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt

# 启动：双击 启动服务端.bat
#   → 自动检测：有 uv 走 uv（首建 venv + sync，之后每次启动秒级对齐依赖）
#   → 没有 uv 则自动切换 python -m venv + pip install -r requirements.txt
#   或直接手动：.venv/Scripts/python.exe run.py
```

依赖只有三个：`aiohttp`（运行必需）、`pywebview` + `pyinstaller`（一键导出 Windows EXE 时用）。

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
- 服务端运行环境装有 Python 3.11 + aiohttp/pywebview/pyinstaller（本机已装）
- Android 构建需要 JDK 17+ 与 Android SDK。本机工具链（不在项目目录内，D 盘共享）：
  `D:\Android\sdk`（platform-tools / android-35 / build-tools 35.0.0）
  `D:\Android\gradle-home`（Gradle 依赖缓存）
  `D:\Android\gradle-8.10.2`（Gradle 发行版）
  也可用环境变量 ANDROID_HOME / GRADLE_USER_HOME 指向别处。
- 首次 APK 构建约 3-8 分钟（Gradle 下载依赖），之后增量很快。

注入机制：导出时把 `{"ws_address": "...", "server_url": "...", "app_name": "..."}`
写入混淆后的 `config.bin`（XOR+Base64，客户端运行时解码）。客户端文件里不含明文服务器地址，解包只能看到一串乱码。EXE 与 APK 启动时自动解码并连接。

应用图标：三端（网页 favicon / EXE / APK）统一使用 `tools/kite-logo.png`。
更换图标：替换该文件后运行 `tools/make_icons.py`（需 Pillow），再重新导出双端。

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
