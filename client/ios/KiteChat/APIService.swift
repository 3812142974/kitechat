// APIService.swift
// KiteChat REST API Client

import Foundation

class APIService {
    static let shared = APIService()

    private init() {}

    // MARK: - Login

    func login(serverURL: String, username: String, password: String) async -> String? {
        let url = URL(string: "\(serverURL)/api/login")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let body: [String: Any] = ["username": username, "password": password]
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)

        do {
            let (data, _) = try await URLSession.shared.data(for: request)
            let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            if let status = json?["status"] as? String, status == "ok",
               let dataDict = json?["data"] as? [String: Any],
               let token = dataDict["token"] as? String {
                return token
            }
        } catch {
            print("Login error: \(error)")
        }
        return nil
    }

    // MARK: - Register

    func register(serverURL: String, username: String, password: String) async -> String? {
        let url = URL(string: "\(serverURL)/api/register")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let body: [String: Any] = ["username": username, "password": password]
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)

        do {
            let (data, _) = try await URLSession.shared.data(for: request)
            let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            if let status = json?["status"] as? String, status == "ok",
               let dataDict = json?["data"] as? [String: Any],
               let token = dataDict["token"] as? String {
                return token
            }
        } catch {
            print("Register error: \(error)")
        }
        return nil
    }

    // MARK: - Fetch Client Settings

    func fetchClientSettings(serverURL: String) async -> [String: Any]? {
        let url = URL(string: "\(serverURL)/api/client-settings")!
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            return json?["data"] as? [String: Any]
        } catch {
            return nil
        }
    }
}
