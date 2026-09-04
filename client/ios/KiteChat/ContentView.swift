// ContentView.swift
// Main entry view — login or chat

import SwiftUI

struct ContentView: View {
    @EnvironmentObject var appState: AppState

    var body: some View {
        Group {
            if appState.isConnected && appState.currentUser != nil {
                ChatView()
            } else {
                LoginView()
            }
        }
    }
}

// MARK: - Login View

struct LoginView: View {
    @EnvironmentObject var appState: AppState
    @State private var username = ""
    @State private var password = ""
    @State private var serverURL = ""
    @State private var isLogin = true
    @State private var isLoading = false
    @State private var error: String?

    var body: some View {
        VStack(spacing: 20) {
            Text("KiteChat")
                .font(.largeTitle)
                .fontWeight(.bold)

            Picker("", selection: $isLogin) {
                Text("登录").tag(true)
                Text("注册").tag(false)
            }
            .pickerStyle(.segmented)
            .frame(width: 200)

            TextField("用户名", text: $username)
                .textFieldStyle(.roundedBorder)
                .frame(width: 250)

            SecureField("密码", text: $password)
                .textFieldStyle(.roundedBorder)
                .frame(width: 250)

            TextField("服务器地址", text: $serverURL)
                .textFieldStyle(.roundedBorder)
                .frame(width: 250)

            if let error = error {
                Text(error).foregroundColor(.red)
            }

            Button(action: submit) {
                if isLoading {
                    ProgressView()
                } else {
                    Text(isLogin ? "登录" : "注册")
                }
            }
            .disabled(isLoading || username.isEmpty || password.isEmpty)
        }
        .padding()
        .onAppear {
            serverURL = appState.config.serverURL
        }
    }

    private func submit() {
        isLoading = true
        error = nil
        UserDefaults.standard.set(serverURL, forKey: "server_url")
        Task {
            if isLogin {
                await appState.login(username: username, password: password)
            } else {
                await appState.register(username: username, password: password)
            }
            isLoading = false
        }
    }
}

// MARK: - Chat View

struct ChatView: View {
    @EnvironmentObject var appState: AppState
    @State private var messageText = ""
    @State private var showNewSession = false

    var body: some View {
        NavigationSplitView {
            // Sidebar
            List {
                Section("对话") {
                    ForEach(appState.sessions) { session in
                        Button(action: { selectSession(session) }) {
                            HStack {
                                VStack(alignment: .leading) {
                                    Text(session.name).font(.headline)
                                    if let last = session.lastMessage {
                                        Text(last).font(.caption).lineLimit(1)
                                    }
                                }
                                Spacer()
                            }
                        }
                        .buttonStyle(.plain)
                    }
                }

                Section("好友") {
                    ForEach(appState.friends) { friend in
                        Text(friend.nickname)
                    }
                }
            }
            .navigationTitle("KiteChat")
            .toolbar {
                Button(action: { showNewSession = true }) {
                    Image(systemName: "plus")
                }
            }
        } detail: {
            // Chat area
            if let session = appState.currentSession {
                MessageListView(session: session)
            } else {
                Text("选择一个对话")
                    .foregroundStyle(.secondary)
            }
        }
        .sheet(isPresented: $showNewSession) {
            NewSessionView()
        }
    }

    private func selectSession(_ session: ChatSession) {
        appState.currentSession = session
        appState.loadHistory(sessionId: session.id)
    }
}

// MARK: - Message List View

struct MessageListView: View {
    @EnvironmentObject var appState: AppState
    let session: ChatSession

    var body: some View {
        VStack {
            // Messages
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack {
                        ForEach(appState.messages) { message in
                            MessageBubble(message: message, isOwn: message.senderId == appState.currentUser?.id)
                                .id(message.id)
                        }
                    }
                    .padding()
                }
                .onChange(of: appState.messages.count) { _ in
                    if let last = appState.messages.last {
                        proxy.scrollTo(last.id, anchor: .bottom)
                    }
                }
            }

            // Input
            HStack {
                TextField("输入消息...", text: $messageText)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { sendMessage() }

                Button(action: sendMessage) {
                    Image(systemName: "paperplane.fill")
                }
                .disabled(messageText.isEmpty)
            }
            .padding()
        }
        .navigationTitle(session.name)
    }

    private func sendMessage() {
        let text = messageText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        appState.sendMessage(text: text)
        messageText = ""
    }
}

// MARK: - Message Bubble

struct MessageBubble: View {
    let message: Message
    let isOwn: Bool

    var body: some View {
        HStack {
            if isOwn { Spacer() }

            VStack(alignment: isOwn ? .trailing : .leading) {
                if !isOwn {
                    Text(message.senderName)
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                Text(message.content)
                    .padding(10)
                    .background(isOwn ? Color.blue : Color(.systemGray5))
                    .foregroundColor(isOwn ? .white : .primary)
                    .cornerRadius(12)
            }

            if !isOwn { Spacer() }
        }
    }
}

// MARK: - New Session View

struct NewSessionView: View {
    @EnvironmentObject var appState: AppState
    @Environment(\.dismiss) var dismiss
    @State private var name = ""

    var body: some View {
        VStack(spacing: 20) {
            Text("新建对话").font(.headline)

            TextField("对话名称", text: $name)
                .textFieldStyle(.roundedBorder)
                .frame(width: 250)

            HStack {
                Button("取消") { dismiss() }
                Button("创建") {
                    appState.createSession(name: name)
                    dismiss()
                }
                .disabled(name.isEmpty)
            }
        }
        .padding()
    }
}
