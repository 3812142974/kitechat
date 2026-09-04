// Models.swift
// KiteChat iOS/macOS Client

import Foundation

// MARK: - User

struct User: Codable, Identifiable {
    let id: Int
    let nickname: String
    var avatar: String?
}

// MARK: - Chat Session

struct ChatSession: Codable, Identifiable {
    let id: String
    let name: String
    let kind: String // "ai" or "private"
    var lastMessage: String?
    var lastTime: Int?

    enum CodingKeys: String, CodingKey {
        case id, name, kind
        case lastMessage = "last_message"
        case lastTime = "last_time"
    }
}

// MARK: - Message

struct Message: Codable, Identifiable {
    let id: Int
    let sessionId: String
    let senderId: Int
    let senderName: String
    let content: String
    let time: Int

    enum CodingKeys: String, CodingKey {
        case id
        case sessionId = "session_id"
        case senderId = "sender_id"
        case senderName = "sender_name"
        case content, time
    }
}

// MARK: - Friend

struct Friend: Codable, Identifiable {
    let id: Int
    let nickname: String
}

// MARK: - Friend Request

struct FriendRequest: Codable, Identifiable {
    let userId: Int
    let nickname: String
    let comment: String?

    var id: Int { userId }

    enum CodingKeys: String, CodingKey {
        case userId = "user_id"
        case nickname, comment
    }
}

// MARK: - Notice Event

struct NoticeEvent: Codable {
    let postType: String
    let noticeType: String
    let sessionId: String?
    let userId: Int?
    let nickname: String?
    let session: ChatSession?
    let typing: Bool?

    enum CodingKeys: String, CodingKey {
        case postType = "post_type"
        case noticeType = "notice_type"
        case sessionId = "session_id"
        case userId = "user_id"
        case nickname, session, typing
    }
}

// MARK: - App Config

struct AppConfig {
    let wsAddress: String
    let serverURL: String
    let appName: String

    static func load() -> AppConfig {
        // Try to load from bundled config.bin (XOR + base64)
        if let configData = loadConfigBin(),
           let json = parseConfigJSON(configData) {
            return AppConfig(
                wsAddress: json["ws_address"] as? String ?? "",
                serverURL: json["server_url"] as? String ?? "",
                appName: json["app_name"] as? String ?? "KiteChat"
            )
        }
        // Fallback: use server URL from UserDefaults or default
        let serverURL = UserDefaults.standard.string(forKey: "server_url") ?? "http://localhost:8920"
        let wsAddress = serverURL.replacingOccurrences(of: "http://", with: "ws://")
            .replacingOccurrences(of: "https://", with: "wss://") + "/ws"
        return AppConfig(wsAddress: wsAddress, serverURL: serverURL, appName: "KiteChat")
    }

    private static func loadConfigBin() -> Data? {
        // Try to load config.bin from app bundle
        if let url = Bundle.main.url(forResource: "config", withExtension: "bin"),
           let data = try? Data(contentsOf: url) {
            return data
        }
        return nil
    }

    private static func parseConfigJSON(_ data: Data) -> [String: Any]? {
        guard let b64 = String(data: data, encoding: .utf8) else { return nil }
        let key = "n0v4ch4t$cfg"
        guard let decoded = Data(base64Encoded: b64.trimmingCharacters(in: .whitespacesAndNewlines)) else { return nil }
        var result = Data()
        for (i, byte) in decoded.enumerated() {
            result.append(byte ^ UInt8(key[key.index(key.startIndex, offsetBy: i % key.count)]))
        }
        guard let json = try? JSONSerialization.jsonObject(with: result) as? [String: Any] else { return nil }
        return json
    }
}
