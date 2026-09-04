// macOS KiteChat Server Launcher
// macOS 服务端一键启动器

import SwiftUI

@main
struct KiteChatServerApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        WindowGroup {
            ServerView()
        }
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 600, height: 400)

        MenuBarExtra("KiteChat", systemImage: "server.rack") {
            Button("启动服务端") { appDelegate.startServer() }
            Button("停止服务端") { appDelegate.stopServer() }
            Divider()
            Button("退出") { NSApplication.shared.terminate(nil) }
        }
    }
}

// MARK: - App Delegate

class AppDelegate: NSObject, NSApplicationDelegate {
    var serverProcess: Process?
    var serverStatus: String = "stopped"

    func startServer() {
        // Find Python and server path
        guard let pythonPath = findPython(),
              let serverPath = findServerScript() else {
            print("Python or server not found")
            return
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: pythonPath)
        process.arguments = [serverPath]
        process.currentDirectoryURL = URL(fileURLWithPath: serverPath.deletingLastPathComponent().path)

        // Set environment
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = serverPath.deletingLastPathComponent().path
        process.environment = env

        // Capture output
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe

        do {
            try process.run()
            serverProcess = process
            serverStatus = "running"
            print("Server started")
        } catch {
            print("Failed to start server: \(error)")
        }
    }

    func stopServer() {
        serverProcess?.terminate()
        serverProcess = nil
        serverStatus = "stopped"
        print("Server stopped")
    }

    private func findPython() -> String? {
        let candidates = [
            "/usr/local/bin/python3",
            "/opt/homebrew/bin/python3",
            "/usr/bin/python3",
        ]
        for path in candidates {
            if FileManager.default.fileExists(atPath: path) {
                return path
            }
        }
        // Try which
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/which")
        task.arguments = ["python3"]
        let pipe = Pipe()
        task.standardOutput = pipe
        try? task.run()
        task.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let path = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
        if let path = path, !path.isEmpty {
            return path
        }
        return nil
    }

    private func findServerScript() -> URL? {
        // Look for run.py in the same directory as this app, or in known locations
        let candidates = [
            "./run.py",
            "../run.py",
            "~/bot/KiteChat/run.py",
            "/opt/kitechat/run.py",
        ]
        for candidate in candidates {
            let expanded = NSString(string: candidate).expandingTildeInPath
            if FileManager.default.fileExists(atPath: expanded) {
                return URL(fileURLWithPath: expanded)
            }
        }
        // Fallback: use Bundle resources
        if let url = Bundle.main.url(forResource: "run", withExtension: "py") {
            return url
        }
        return nil
    }
}

// MARK: - Server View

struct ServerView: View {
    @State private var status = "stopped"
    @State private var log = ""
    @State private var port = "8920"
    @StateObject private var appDelegate = AppDelegate()

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            // Header
            HStack {
                Text("KiteChat Server")
                    .font(.title2)
                    .fontWeight(.bold)
                Spacer()
                Circle()
                    .fill(status == "running" ? Color.green : Color.red)
                    .frame(width: 12, height: 12)
                Text(status.capitalized)
                    .foregroundColor(.secondary)
            }

            // Controls
            HStack {
                Button("启动") { startServer() }
                    .disabled(status == "running")
                Button("停止") { stopServer() }
                    .disabled(status == "stopped")
                Spacer()
                TextField("端口", text: $port)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 80)
            }

            // Log output
            ScrollView {
                Text(log)
                    .font(.system(.caption, design: .monospaced))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(8)
            }
            .background(Color(.textBackgroundColor))
            .cornerRadius(8)
        }
        .padding()
    }

    private func startServer() {
        appDelegate.startServer()
        status = "running"
        log += "[\(timestamp())] Server starting...\n"
    }

    private func stopServer() {
        appDelegate.stopServer()
        status = "stopped"
        log += "[\(timestamp())] Server stopped\n"
    }

    private func timestamp() -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        return formatter.string(from: Date())
    }
}
