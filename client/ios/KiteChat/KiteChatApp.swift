// KiteChatApp.swift
// iOS/MacOS KiteChat Client
// WebSocket-based chat client connecting to KiteChat server

import SwiftUI

@main
struct KiteChatApp: App {
    @StateObject private var appState = AppState()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(appState)
        }
    }
}

// MARK: - App State

class AppState: ObservableObject {
    @Published var isConnected = false
    @Published var currentUser: User?
    @Published var sessions: [ChatSession] = []
    @Published var friends: [Friend] = []
    @Published var friendRequests: [FriendRequest] = []
    @Published var currentSession: ChatSession?
    @Published var messages: [Message] = []

    let config = AppConfig.load()
    let wsService = WebSocketService()

    init() {
        wsService.delegate = self
        loadSession()
    }

    func loadSession() {
        // Try to load saved token from UserDefaults
        if let token = UserDefaults.standard.string(forKey: "auth_token") {
            wsService.connect(config: config, token: token)
        }
    }

    func login(username: String, password: String) async {
        let result = await APIService.shared.login(
            serverURL: config.serverURL,
            username: username,
            password: password
        )
        if let token = result {
            UserDefaults.standard.set(token, forKey: "auth_token")
            wsService.connect(config: config, token: token)
        }
    }

    func register(username: String, password: String) async {
        let result = await APIService.shared.register(
            serverURL: config.serverURL,
            username: username,
            password: password
        )
        if let token = result {
            UserDefaults.standard.set(token, forKey: "auth_token")
            wsService.connect(config: config, token: token)
        }
    }

    func logout() {
        UserDefaults.standard.removeObject(forKey: "auth_token")
        wsService.disconnect()
        DispatchQueue.main.async {
            self.isConnected = false
            self.currentUser = nil
            self.sessions = []
        }
    }

    func sendMessage(text: String) {
        guard let session = currentSession else { return }
        wsService.sendMessage(sessionId: session.id, text: text)
    }

    func createSession(name: String) {
        wsService.createSession(name: name, kind: "ai")
    }

    func loadHistory(sessionId: String) {
        wsService.loadHistory(sessionId: sessionId, limit: 50)
    }

    func addFriend(username: String) {
        wsService.addFriend(username: username)
    }
}

// MARK: - WebSocket Delegate

extension AppState: WebSocketServiceDelegate {
    func didConnect() {
        DispatchQueue.main.async { self.isConnected = true }
    }

    func didDisconnect() {
        DispatchQueue.main.async { self.isConnected = false }
    }

    func didReceiveAuthOK(user: User, sessions: [ChatSession], friends: [Friend], requests: [FriendRequest]) {
        DispatchQueue.main.async {
            self.currentUser = user
            self.sessions = sessions
            self.friends = friends
            self.friendRequests = requests
        }
    }

    func didReceiveMessage(_ message: Message) {
        DispatchQueue.main.async {
            if message.sessionId == self.currentSession?.id {
                self.messages.append(message)
            }
        }
    }

    func didReceiveNotice(_ notice: NoticeEvent) {
        DispatchQueue.main.async {
            switch notice.noticeType {
            case "bot_typing":
                break // Handle typing indicator
            case "friend_added":
                if let userId = notice.userId {
                    self.friends.append(Friend(id: userId, nickname: notice.nickname ?? ""))
                }
            case "session_created":
                if let session = notice.session {
                    self.sessions.append(session)
                }
            default:
                break
            }
        }
    }

    func didReceiveResult(reqId: Int, status: String, data: Any?) {
        // Handle request results
    }
}
