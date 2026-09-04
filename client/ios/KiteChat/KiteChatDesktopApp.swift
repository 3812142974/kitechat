// macOS KiteChat Desktop Client
// 用 SwiftUI 实现的桌面聊天客户端

import SwiftUI

@main
struct KiteChatDesktopApp: App {
    @StateObject private var appState = AppState()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(appState)
        }
        .windowStyle(.titleBar)
        .defaultSize(width: 900, height: 600)

        // Menu bar extra
        MenuBarExtra("KiteChat", systemImage: "bubble.left.and.bubble.right") {
            Button("显示主窗口") {
                NSApplication.shared.activate(ignoringOtherApps: true)
                for window in NSApplication.shared.windows {
                    if window.title == "KiteChat" {
                        window.makeKeyAndOrderFront(nil)
                    }
                }
            }
            Divider()
            Button("退出") { NSApplication.shared.terminate(nil) }
        }
    }
}
