// WebSocketService.swift
// KiteChat WebSocket Client

import Foundation

protocol WebSocketServiceDelegate: AnyObject {
    func didConnect()
    func didDisconnect()
    func didReceiveAuthOK(user: User, sessions: [ChatSession], friends: [Friend], requests: [FriendRequest])
    func didReceiveMessage(_ message: Message)
    func didReceiveNotice(_ notice: NoticeEvent)
    func didReceiveResult(reqId: Int, status: String, data: Any?)
}

class WebSocketService: NSObject, URLSessionWebSocketDelegate {
    weak var delegate: WebSocketServiceDelegate?
    private var session: URLSession?
    private var webSocket: URLSessionWebSocketTask?
    private var isConnected = false
    private var reconnectTimer: Timer?
    private var reqIdCounter = 0

    func connect(config: AppConfig, token: String) {
        guard let url = URL(string: config.wsAddress) else { return }

        let sessionConfig = URLSessionConfiguration.default
        sessionConfig.waitsForConnectivity = true
        session = URLSession(configuration: sessionConfig, delegate: self, delegateQueue: nil)

        webSocket = session?.webSocketTask(with: url)
        webSocket?.resume()

        // Send auth after connection
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            self?.authenticate(token: token)
        }
    }

    func disconnect() {
        webSocket?.cancel(with: .normalClosure, reason: nil)
        webSocket = nil
        isConnected = false
        reconnectTimer?.invalidate()
    }

    // MARK: - Send Operations

    func authenticate(token: String) {
        let msg: [String: Any] = ["op": "auth", "token": token]
        sendJSON(msg)
    }

    func sendMessage(sessionId: String, text: String) {
        let reqId = nextReqId()
        let msg: [String: Any] = [
            "op": "message",
            "req_id": reqId,
            "session_id": sessionId,
            "message": text
        ]
        sendJSON(msg)
    }

    func createSession(name: String, kind: String) {
        let reqId = nextReqId()
        let msg: [String: Any] = [
            "op": "create_session",
            "req_id": reqId,
            "kind": kind,
            "name": name
        ]
        sendJSON(msg)
    }

    func loadHistory(sessionId: String, limit: Int) {
        let reqId = nextReqId()
        let msg: [String: Any] = [
            "op": "history",
            "req_id": reqId,
            "session_id": sessionId,
            "before_id": NSNull(),
            "limit": limit
        ]
        sendJSON(msg)
    }

    func addFriend(username: String) {
        let reqId = nextReqId()
        let msg: [String: Any] = [
            "op": "friend_add",
            "req_id": reqId,
            "username": username
        ]
        sendJSON(msg)
    }

    func handleFriendRequest(userId: Int, approve: Bool) {
        let reqId = nextReqId()
        let msg: [String: Any] = [
            "op": "friend_handle",
            "req_id": reqId,
            "user_id": userId,
            "approve": approve
        ]
        sendJSON(msg)
    }

    func sendPing() {
        let reqId = nextReqId()
        let msg: [String: Any] = ["op": "ping", "req_id": reqId]
        sendJSON(msg)
    }

    // MARK: - Internal

    private func nextReqId() -> Int {
        reqIdCounter += 1
        return reqIdCounter
    }

    private func sendJSON(_ obj: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: obj),
              let text = String(data: data, encoding: .utf8) else { return }
        webSocket?.send(.string(text)) { error in
            if let error = error {
                print("WebSocket send error: \(error)")
            }
        }
    }

    private func receive() {
        webSocket?.receive { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .success(let message):
                switch message {
                case .string(let text):
                    self.handleMessage(text)
                case .data(let data):
                    if let text = String(data: data, encoding: .utf8) {
                        self.handleMessage(text)
                    }
                @unknown default:
                    break
                }
                self.receive() // Continue listening
            case .failure(let error):
                print("WebSocket receive error: \(error)")
                self.isConnected = false
                self.delegate?.didDisconnect()
                self.scheduleReconnect()
            }
        }
    }

    private func handleMessage(_ text: String) {
        guard let data = text.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }

        guard let op = json["op"] as? String else { return }

        switch op {
        case "auth_ok":
            handleAuthOK(json)
        case "result":
            handleResult(json)
        case "message":
            handleMessageEvent(json)
        case "notice":
            handleNotice(json)
        default:
            break
        }
    }

    private func handleAuthOK(_ json: [String: Any]) {
        isConnected = true
        delegate?.didConnect()

        // Parse user
        if let userData = json["user"] as? [String: Any],
           let userId = userData["id"] as? Int,
           let nickname = userData["nickname"] as? String {
            let user = User(id: userId, nickname: nickname)

            // Parse sessions
            var sessions: [ChatSession] = []
            if let sessionsArray = json["sessions"] as? [[String: Any]] {
                for s in sessionsArray {
                    if let sid = s["id"] as? String,
                       let name = s["name"] as? String,
                       let kind = s["kind"] as? String {
                        sessions.append(ChatSession(id: sid, name: name, kind: kind))
                    }
                }
            }

            // Parse friends
            var friends: [Friend] = []
            if let friendsArray = json["friends"] as? [[String: Any]] {
                for f in friendsArray {
                    if let fid = f["id"] as? Int,
                       let fname = f["nickname"] as? String {
                        friends.append(Friend(id: fid, nickname: fname))
                    }
                }
            }

            // Parse friend requests
            var requests: [FriendRequest] = []
            if let requestsArray = json["requests"] as? [[String: Any]] {
                for r in requestsArray {
                    if let rid = r["user_id"] as? Int,
                       let rname = r["nickname"] as? String {
                        requests.append(FriendRequest(userId: rid, nickname: rname, comment: r["comment"] as? String))
                    }
                }
            }

            delegate?.didReceiveAuthOK(user: user, sessions: sessions, friends: friends, requests: requests)
        }

        // Start ping timer
        DispatchQueue.main.async {
            self.reconnectTimer?.invalidate()
            self.reconnectTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
                self?.sendPing()
            }
        }
    }

    private func handleResult(_ json: [String: Any]) {
        let reqId = json["req_id"] as? Int ?? 0
        let status = json["status"] as? String ?? "unknown"
        let data = json["data"]
        delegate?.didReceiveResult(reqId: reqId, status: status, data: data)
    }

    private func handleMessageEvent(_ json: [String: Any]) {
        // Parse OneBot V11 message event
        guard let sessionId = json["session_id"] as? String,
              let sender = json["sender"] as? [String: Any],
              let senderId = sender["user_id"] as? Int,
              let senderName = sender["nickname"] as? String,
              let messageArray = json["message"] as? [[String: Any]],
              let time = json["time"] as? Int else { return }

        // Extract text from message segments
        var content = ""
        for seg in messageArray {
            if let type = seg["type"] as? String, type == "text",
               let segData = seg["data"] as? [String: Any],
               let text = segData["text"] as? String {
                content += text
            }
        }

        let messageId = json["message_id"] as? Int ?? Int.random(in: 1...Int.max)
        let message = Message(
            id: messageId,
            sessionId: sessionId,
            senderId: senderId,
            senderName: senderName,
            content: content,
            time: time
        )
        delegate?.didReceiveMessage(message)
    }

    private func handleNotice(_ json: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: json),
              let notice = try? JSONDecoder().decode(NoticeEvent.self, from: data) else { return }
        delegate?.didReceiveNotice(notice)
    }

    private func scheduleReconnect() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
            guard let self = self, !self.isConnected else { return }
            // Reconnect using saved token
            if let token = UserDefaults.standard.string(forKey: "auth_token"),
               let config = try? JSONDecoder().decode(AppConfig.self, from: Data()) {
                self.connect(config: config, token: token)
            }
        }
    }

    // MARK: - URLSessionWebSocketDelegate

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didOpenWithProtocol protocol: String?) {
        print("WebSocket connected")
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        print("WebSocket closed: \(closeCode)")
        isConnected = false
        delegate?.didDisconnect()
        scheduleReconnect()
    }
}
