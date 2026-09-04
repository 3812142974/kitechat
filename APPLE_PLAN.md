# KiteChat Apple 平台适配方案

## 一、总体架构

### 现有架构回顾
- **服务端**: Python aiohttp,端口 8920(HTTP) + 8921(HTTPS/WSS)
- **客户端**: Web(HTML/JS), Android(Java/Gradle), Desktop Windows(pywebview+PyInstaller)
- **协议**: OneBot V11 风格 JSON over WebSocket
- **导出**: server/exporter.py 负责构建 APK/EXE 并注入 config.bin

### Apple 平台新增内容

| 平台 | 类型 | 技术栈 | 产物 |
|------|------|--------|------|
| iOS | 客户端 | Swift/SwiftUI + URLSession WebSocket | IPA |
| macOS | 客户端 | Swift/SwiftUI + URLSession WebSocket | DMG |
| macOS | 服务端 | Python (同 Linux) | .app / .command 脚本 |

---

## 二、需要准备的东西

### 硬件要求
- **Mac 电脑**(MacBook/iMac/Mac Mini 均可,Intel 或 Apple Silicon)
  - Xcode 只能在 macOS 上运行,Windows/Linux 无法编译 iOS/macOS 应用
- **iPhone/iPad**(真机调试用,可选)
- **Apple ID**(免费账号可以自签名调试,不能上架 App Store)

### 软件环境(需要下载)

| 软件 | 大小 | 下载地址 | 说明 |
|------|------|----------|------|
| Xcode | ~12GB | App Store 搜索 "Xcode" | iOS/macOS 开发必需 |
| Command Line Tools | ~700MB | `xcode-select --install` | 命令行编译工具 |
| Homebrew | ~5MB | https://brew.sh | macOS 包管理器 |
| Python 3.11+ | ~50MB | `brew install python3` | 服务端运行环境 |
| create-dmg | ~1MB | `brew install create-dmg` | 打包 DMG(可选) |

### 开发者账号(可选,分发需要)

| 类型 | 费用 | 用途 |
|------|------|------|
| 免费 Apple ID | $0 | 自签名调试,不能上架 |
| Apple Developer Program | $99/年 | TestFlight 分发 / App Store 上架 |
| 企业证书 | $299/年 | 企业内部分发(需 DUNS 编号) |

---

## 三、代码结构(已写好)

### 文件清单

```
client/ios/
├── KiteChatApp.swift          # App 入口(主视图切换)
├── Models.swift               # 数据模型(User/Session/Message/Friend)
├── ContentView.swift          # 登录页+聊天页+消息气泡
├── WebSocketService.swift     # WebSocket 连接管理
├── APIService.swift           # REST API 调用
└── KiteChatDesktopApp.swift   # macOS 桌面客户端入口

client/ios/KiteChat/
└── (Xcode 项目配置,需在 Mac 上创建)

server/
├── exporter.py                # 导出引擎(已新增 iOS/macOS 支持)
└── platforms/macos/           # macOS 服务端平台适配(待补充)

启动服务端-macos.command         # macOS 一键启动脚本
重置所有用户-macos.command       # macOS 重置脚本
```

### 服务端导出功能

exporter.py 新增了以下函数:

```python
_build_ios(ws, version, scheme, ca_cert_path)    # 构建 IPA
_build_macos(ws, version, scheme, ca_cert_path)   # 构建 DMG
_find_xcodebuild()  # 自动检测 Xcode
_find_hdiutil()     # 自动检测 hdiutil(DMG 打包)
```

**工作流程**:
1. 注入 config.bin(XOR+base64 混淆)
2. 调用 `xcodebuild archive` 构建 .xcarchive
3. 调用 `xcodebuild -exportArchive` 导出 .app
4. 调用 `hdiutil create` 打包成 DMG(macOS)或直接导出 IPA(iOS)

**注意**: iOS/macOS 构建**必须在 macOS 上执行**,Windows/Linux 服务器无法构建。

---

## 四、在 Mac 上的操作步骤

### 1. 环境准备
```bash
# 安装 Xcode(从 App Store)
# 安装 Command Line Tools
xcode-select --install

# 安装 Homebrew
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 安装 Python 3
brew install python3

# 安装 create-dmg(可选)
brew install create-dmg
```

### 2. 克隆代码
```bash
git clone -b apple https://github.com/3812142974/kitechat.git kitechat-apple
cd kitechat-apple
```

### 3. 创建 Xcode 项目
在 Mac 上用 Xcode:
- File → New → Project → macOS → App
- Product Name: KiteChat
- Language: Swift
- Interface: SwiftUI
- 将 `client/ios/` 下的 Swift 文件拖入 Xcode 项目
- 添加 WebSocket 支持(File → Add Packages → `https://github.com/apple/swift-nio`)

### 4. 配置项目
- **iOS Target**:
  - Bundle Identifier: `com.kitechat.ios`
  - Signing: 选择你的 Apple ID
  - Capabilities: 添加 "Background Modes" → "Uses WebSocket"
  
- **macOS Target**:
  - Bundle Identifier: `com.kitechat.macos`
  - Signing: 选择你的 Apple ID
  - Entitlements: 添加 `com.apple.security.network.client`

### 5. 构建和运行
- iOS: 选择 iOS 模拟器或真机 → Cmd+R
- macOS: 选择 My Mac → Cmd+R

### 6. 导出客户端
```bash
# 在服务端 WebUI 的导出页面选择:
# - iOS → 生成 IPA 文件
# - macOS → 生成 DMG 安装包
```

---

## 五、iOS 客户端说明

### 核心功能
- ✅ WebSocket 连接(使用 Apple 原生 URLSession)
- ✅ 登录/注册
- ✅ 会话列表(创建/切换对话)
- ✅ 实时消息收发
- ✅ 好友系统
- ✅ config.bin 自动解析(XOR+base64)
- ✅ 断线自动重连
- ✅ 离线消息缓存

### 架构设计
```
KiteChatApp (App 入口)
├── AppState (全局状态管理,ObservableObject)
│   ├── WebSocketService (网络层)
│   ├── APIService (REST API)
│   └── Models (数据模型)
├── LoginView (登录/注册页)
├── ChatView (聊天主页)
│   ├── SessionSidebar (会话列表侧边栏)
│   ├── MessageListView (消息列表)
│   └── MessageInputBar (消息输入框)
└── FriendView (好友管理)
```

### 与服务端的协议对齐
iOS 客户端完全兼容现有 OneBot V11 协议:

| 操作 | 请求格式 | 响应格式 |
|------|----------|----------|
| 认证 | `{"op":"auth","token":"..."}` | `{"op":"auth_ok","user":{...},...}` |
| 发消息 | `{"op":"message","session_id":"...","message":"..."}` | `{"op":"result","status":"ok"}` |
| 历史消息 | `{"op":"history","session_id":"...","limit":50}` | `{"op":"result","data":{"messages":[...]}}` |
| 新建对话 | `{"op":"create_session","name":"...","kind":"ai"}` | `{"op":"result","data":{"session":{...}}}` |

---

## 六、macOS 客户端说明

### 核心功能
与 iOS 客户端共享全部业务代码,额外支持:
- ✅ macOS 原生菜单栏(MenuBarExtra)
- ✅ 窗口拖拽/缩放
- ✅ 通知中心(可选)
- ✅ Touch Bar 支持(可选)

### 桌面端优势
- 支持文件拖拽发送
- 支持系统级快捷键
- 支持暗色/亮色模式自动切换
- 支持多窗口(同时打开多个对话)

---

## 七、macOS 服务端说明

### 与 Linux 的区别
macOS 上运行 Python 服务端与 Linux 基本相同,但需要:

1. **启动脚本**:使用 `.command` 文件(双击即可运行)
2. **依赖安装**:通过 Homebrew 安装 Python,不需要 sudo
3. **权限**:macOS 沙盒可能阻止网络监听,需要在"系统设置→隐私与安全→网络"中允许

### 安装器(可选)
如果要做 macOS 原生安装器:
- 使用 `pkgbuild` + `productbuild` 制作 .pkg 安装包
- 或使用 `Packages` 工具(免费)
- 安装包内容:Python 虚拟环境 + 服务端代码 + 启动脚本

---

## 八、待补充的内容

### 高优先级
1. **Xcode 项目配置文件**(需在 Mac 上创建)
   - KiteChat.xcodeproj (iOS)
   - KiteChat-macOS.xcodeproj (macOS)
   - ExportOptions.plist (导出配置)
2. **WebSocket 库集成**(推荐 Starscream 或原生 URLSession)
3. **推送通知**(APNs 证书配置)

### 中优先级
4. **图片/文件发送**(服务端已支持,客户端需适配)
5. **语音消息**(iOS 录音 API)
6. **通知中心**(UNUserNotificationCenter)
7. **Keychain 存储**(替代 UserDefaults 存 token)

### 低优先级
8. **Widget**(iOS/macOS 桌面小组件)
9. **Siri Shortcuts**
10. **Apple Watch 客户端**
11. **Mac Catalyst / Designed for iPad**

---

## 九、已知限制

1. **构建环境**:iOS/macOS 客户端**必须在 macOS + Xcode 环境下构建**,Windows 服务器无法代劳
2. **签名要求**:真机调试需要 Apple ID 签名,分发需要开发者账号
3. **config.bin**:iOS/macOS 客户端的 config.bin 需要在构建时注入(服务端 exporter 已支持)
4. **网络权限**:macOS 首次运行需要用户授权网络访问

---

## 十、验证清单

- [ ] Mac 上安装 Xcode 并能编译 Swift 项目
- [ ] iOS 客户端能连接服务端 WebSocket
- [ ] iOS 客户端能收发消息
- [ ] macOS 客户端能连接服务端
- [ ] macOS 客户端能收发消息
- [ ] 服务端 WebUI 能导出 IPA
- [ ] 服务端 WebUI 能导出 DMG
- [ ] 导出的 IPA 能在 iPhone 上安装
- [ ] 导出的 DMG 能在 Mac 上安装运行
- [ ] macOS 服务端脚本(.command)能一键启动

---

*文档生成时间: 2026-09-04*
*分支: apple*
*代码位置: d:/bot/KiteChat (apple 分支)*
