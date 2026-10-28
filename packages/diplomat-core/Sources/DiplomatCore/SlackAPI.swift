import Foundation

/// One search result, as Slack's MCP server prints it. It carries no handle or workspace
/// for the sender: those come from their profile (`SlackProfile`).
public struct SlackMatch: Equatable, Sendable {
    public struct Channel: Equatable, Sendable {
        public var id: String
        /// Without the `#`; empty for a DM.
        public var name: String
        /// A DM or a group DM.
        public var isDM: Bool

        public init(id: String, name: String = "", isDM: Bool = false) {
            self.id = id
            self.name = name
            self.isDM = isDM
        }
    }

    public var ts: String
    public var text: String
    public var user: String
    /// The sender's name as the result prints it.
    public var displayName: String
    public var permalink: String
    public var channel: Channel
    public var isBot: Bool

    public init(ts: String, text: String = "", user: String = "", displayName: String = "",
                permalink: String = "", channel: Channel, isBot: Bool = false) {
        self.ts = ts
        self.text = text
        self.user = user
        self.displayName = displayName
        self.permalink = permalink
        self.channel = channel
        self.isBot = isBot
    }

    public var id: String { "\(channel.id):\(ts)" }
}

/// What `slack_read_user_profile` says about one user.
public struct SlackProfile: Equatable, Sendable {
    public var userID: String
    public var username: String
    public var organization: String
    public var email: String
    public var isBot: Bool

    public init(userID: String, username: String = "", organization: String = "",
                email: String = "", isBot: Bool = false) {
        self.userID = userID
        self.username = username
        self.organization = organization
        self.email = email
        self.isBot = isBot
    }

    /// The user's workspace as far as a profile tells: its organization's name, or the
    /// email's `@domain` where the profile names none. A Slack Connect sender's profile
    /// names no organization (or another one), so it never matches the operator's.
    public var workspace: String {
        if !organization.isEmpty { return organization }
        guard let at = email.lastIndex(of: "@"), email.index(after: at) < email.endIndex else {
            return ""
        }
        return email[at...].lowercased()
    }

    public var identity: CommsIdentity {
        CommsIdentity(userID: userID, teamID: workspace, handle: username)
    }

    /// The tool prints one `Key: value` line per field.
    static func parse(_ text: String) -> SlackProfile? {
        var fields: [String: String] = [:]
        for line in text.split(separator: "\n") {
            guard let colon = line.range(of: ": ") ?? line.range(of: ":") else { continue }
            let key = String(line[..<colon.lowerBound])
            if fields[key] == nil {
                fields[key] = line[colon.upperBound...].trimmingCharacters(in: .whitespaces)
            }
        }
        guard let id = fields["User ID"], !id.isEmpty else { return nil }
        return SlackProfile(userID: id, username: fields["Username"] ?? "",
                            organization: fields["Organization Name"] ?? "",
                            email: fields["Email"] ?? "", isBot: fields["Bot"] == "Yes")
    }
}

/// Reads one page of `slack_search_*` markdown.
///
/// A message's text is printed raw, so a sender can write a line that looks like the next
/// result's heading and forge a sender, a channel and a ts after it. Every heading the
/// server writes is `### Result k of N`, N of them; a page holding more such lines than
/// that cannot be split with any trust, and is read again one result per call, where
/// everything before `Text:` is the server's own.
public enum SlackSearchPage {
    public enum Parsed: Equatable {
        case matches([SlackMatch])
        /// N results, with more heading lines than that.
        case ambiguous(count: Int)
    }

    public static func parse(_ markdown: String) -> Parsed {
        let lines = markdown.components(separatedBy: "\n")
        let headings = lines.indices.filter { isHeading(lines[$0]) }
        guard let first = headings.first,
              let count = Int(lines[first].split(separator: " ").last ?? "") else {
            return .matches([])
        }
        guard headings.count == count else { return .ambiguous(count: count) }
        var out: [SlackMatch] = []
        for (n, start) in headings.enumerated() {
            let end = n + 1 < headings.count ? headings[n + 1] : lines.count
            if let m = match(Array(lines[(start + 1)..<end])) { out.append(m) }
        }
        return .matches(out)
    }

    /// A page of one result: whatever its text holds, it is that result's text.
    public static func parseSingle(_ markdown: String) -> SlackMatch? {
        let lines = markdown.components(separatedBy: "\n")
        guard let first = lines.firstIndex(where: isHeading) else { return nil }
        return match(Array(lines[(first + 1)...]))
    }

    static func isHeading(_ line: String) -> Bool {
        let parts = line.split(separator: " ", omittingEmptySubsequences: false)
        return parts.count == 5 && parts[0] == "###" && parts[1] == "Result" && parts[3] == "of"
            && Int(parts[2]) != nil && Int(parts[4]) != nil
    }

    /// One result's lines: fields up to `Text:`, then the text, then the `---` rule.
    private static func match(_ lines: [String]) -> SlackMatch? {
        guard let textAt = lines.firstIndex(where: { $0.trimmingCharacters(in: .whitespaces) == "Text:" }) else {
            return nil
        }
        var body = Array(lines[(textAt + 1)...])
        while let last = body.last, last.isEmpty { body.removeLast() }
        if body.last == "---" { body.removeLast() }
        while let last = body.last, last.isEmpty { body.removeLast() }
        var channel = SlackMatch.Channel(id: "")
        var ts = "", user = "", name = "", permalink = "", bot = false
        for line in lines[..<textAt] {
            if let v = value(line, "Channel") {
                channel.id = lastID(v) ?? ""
                let label = beforeID(v)
                channel.isDM = label == "DM" || label == "Group DM"
                channel.name = label.hasPrefix("#") ? String(label.dropFirst()) : ""
            } else if let v = value(line, "From") {
                user = lastID(v) ?? ""
                bot = v.trimmingCharacters(in: .whitespaces).hasSuffix("[BOT]")
                let label = beforeID(v)
                name = label.range(of: " <").map { String(label[..<$0.lowerBound]) } ?? label
            } else if let v = value(line, "Message_ts") {
                ts = v.trimmingCharacters(in: .whitespaces)
            } else if let v = value(line, "Permalink"), let open = v.range(of: "]("),
                      let close = v.lastIndex(of: ")"), open.upperBound <= close {
                permalink = String(v[open.upperBound..<close])
            }
        }
        guard !ts.isEmpty, !channel.id.isEmpty else { return nil }
        return SlackMatch(ts: ts, text: body.joined(separator: "\n"), user: user,
                          displayName: name, permalink: permalink, channel: channel, isBot: bot)
    }

    private static func value(_ line: String, _ key: String) -> String? {
        line.hasPrefix(key + ":") ? String(line.dropFirst(key.count + 1)) : nil
    }

    /// The last `(ID: X)`: a display name can hold one of its own, never after the real one.
    private static func lastID(_ v: String) -> String? {
        guard let r = v.range(of: "(ID: ", options: .backwards),
              let close = v[r.upperBound...].firstIndex(of: ")") else { return nil }
        return String(v[r.upperBound..<close])
    }

    private static func beforeID(_ v: String) -> String {
        let cut = v.range(of: "(ID: ", options: .backwards)?.lowerBound ?? v.endIndex
        return v[..<cut].trimmingCharacters(in: .whitespaces)
    }
}

/// Which matches become pings, and why. Pure.
public enum CommsMatch {
    /// Read off the match, not the query that found it, so a message two queries find is
    /// one ping with both reasons.
    public static func reasons(for match: SlackMatch, me: CommsIdentity,
                               keywords: [String]) -> (reasons: [CommsReason], keyword: String?) {
        var out: [CommsReason] = []
        if match.channel.isDM { out.append(.dm) }
        if !me.userID.isEmpty,
           match.text.contains("<@\(me.userID)>") || match.text.contains("<@\(me.userID)|") {
            out.append(.mention)
        }
        let keyword = keywords.first { containsWord(match.text, $0) }
        if keyword != nil { out.append(.keyword) }
        return (out, keyword)
    }

    /// Refuses the operator's own messages, bots, anyone without a profile or outside the
    /// operator's workspace (Slack Connect), and, with a non-empty `allowlist`, anyone it
    /// does not name.
    public static func admit(_ match: SlackMatch, sender: SlackProfile?, me: CommsIdentity,
                             allowlist: [String]) -> Bool {
        guard let sender, !match.user.isEmpty, sender.userID == match.user,
              match.user != me.userID, !match.isBot, !sender.isBot,
              !me.teamID.isEmpty, sender.workspace == me.teamID else { return false }
        guard !allowlist.isEmpty else { return true }
        return allowlist.contains { entry in
            entry == match.user
                || (!sender.username.isEmpty && entry.lowercased() == sender.username.lowercased())
        }
    }

    public static func message(from match: SlackMatch, sender: SlackProfile?, me: CommsIdentity,
                               settings: CommsSettings) -> CommsMessage? {
        guard CommsTS.number(match.ts) != nil, !match.channel.id.isEmpty,
              admit(match, sender: sender, me: me, allowlist: settings.senderAllowlist),
              let sender else { return nil }
        let (reasons, keyword) = self.reasons(for: match, me: me, keywords: settings.keywords)
        guard !reasons.isEmpty else { return nil }
        return CommsMessage(id: match.id, channelID: match.channel.id,
                            channelName: match.channel.name, isDM: match.channel.isDM,
                            userID: match.user,
                            userName: sender.username.isEmpty ? match.displayName : sender.username,
                            teamID: sender.workspace, text: match.text, ts: match.ts,
                            permalink: match.permalink, reasons: reasons, keyword: keyword)
    }

    /// Case-insensitive, with no letter or digit either side of `word`.
    static func containsWord(_ text: String, _ word: String) -> Bool {
        let w = word.trimmingCharacters(in: .whitespaces)
        guard !w.isEmpty else { return false }
        var from = text.startIndex
        while let r = text.range(of: w, options: [.caseInsensitive], range: from..<text.endIndex) {
            let before = r.lowerBound == text.startIndex ? nil : text[text.index(before: r.lowerBound)]
            let after = r.upperBound == text.endIndex ? nil : text[r.upperBound]
            let edge: (Character?) -> Bool = { c in c.map { !($0.isLetter || $0.isNumber) } ?? true }
            if edge(before) && edge(after) { return true }
            from = text.index(after: r.lowerBound)
        }
        return false
    }
}

/// The descriptions are what the Comms screen's error banner shows.
public enum SlackError: LocalizedError, Equatable {
    case notSignedIn
    case transport(String)
    case http(Int)
    /// HTTP 401: the login Claude Code holds has expired or been revoked.
    case unauthorized
    /// A JSON-RPC error, or a tool answering `isError`, with its text.
    case api(String)
    /// HTTP 429; the tick is skipped and the next one asks again.
    case rateLimited(retryAfter: Int?)
    /// Past `SlackAPI.deadline`.
    case outOfTime
    case badResponse

    public var errorDescription: String? {
        switch self {
        case .notSignedIn:
            return "Claude Code is not signed in to Slack: in Claude Code, run /mcp and "
                + "authenticate the Slack server"
        case .transport(let s):
            return "Slack could not be reached: \(s)"
        case .http(let code):
            return "Slack answered HTTP \(code)"
        case .unauthorized:
            return "Slack refused Claude Code's Slack login: in Claude Code, run /mcp and "
                + "authenticate the Slack server again"
        case .api(let text):
            return "Slack: \(text)"
        case .rateLimited(let after):
            return "Slack rate-limited the watcher" + (after.map { " (retry after \($0)s)" } ?? "")
        case .outOfTime:
            return "Slack took longer than one poll allows"
        case .badResponse:
            return "Slack answered something that is not MCP's JSON"
        }
    }
}

/// Claude Code's own login to Slack's MCP server, which Comms borrows: read on each use,
/// never cached or written, and refreshed only by Claude Code itself.
public enum SlackToken {
    /// Overrides Claude Code's login (tests, a headless run). Never reaches an agent.
    public static let envVar = "DIPLOMAT_SLACK_TOKEN"

    public struct Login: Equatable, Sendable {
        public var token: String
        public var expiresAt: Date?

        public func isExpired(at now: Date = Date()) -> Bool {
            expiresAt.map { $0 <= now.addingTimeInterval(60) } ?? false
        }
    }

    /// From `.credentials.json` under Claude Code's directory and, on macOS, the
    /// `Claude Code-credentials` Keychain item; of the logins to the server `SlackAPI`
    /// talks to, the one expiring last. `DIPLOMAT_CLAUDE_DIR` stands for all of Claude
    /// Code's state, so with it set the Keychain is not read.
    public static func read() -> Login? {
        if let env = ProcessInfo.processInfo.environment[envVar], !env.isEmpty {
            return Login(token: env, expiresAt: nil)
        }
        let host = SlackAPI.defaultBase().host ?? ""
        var blobs: [Data] = []
        if let data = try? Data(contentsOf: AgentModel.claudeHomeURL()
            .appendingPathComponent(".credentials.json")) {
            blobs.append(data)
        }
#if os(macOS)
        if (ProcessInfo.processInfo.environment["DIPLOMAT_CLAUDE_DIR"] ?? "").isEmpty {
            blobs += keychainBlobs()
        }
#endif
        return blobs.flatMap { logins(in: $0, host: host) }.max { a, b in
            (a.expiresAt ?? .distantFuture) < (b.expiresAt ?? .distantFuture)
        }
    }

    /// `mcpOAuth` holds one entry per MCP server login, keyed by Claude Code's own name
    /// for the server, so the server's URL is what picks Slack's.
    static func logins(in blob: Data, host: String) -> [Login] {
        guard let raw = (try? JSONSerialization.jsonObject(with: blob)) as? [String: Any],
              let servers = raw["mcpOAuth"] as? [String: Any] else { return [] }
        return servers.values.compactMap { value -> Login? in
            guard let entry = value as? [String: Any],
                  let url = (entry["serverUrl"] as? String).flatMap(URL.init(string:)),
                  url.host == host,
                  let token = entry["accessToken"] as? String, !token.isEmpty else { return nil }
            let expires = (entry["expiresAt"] as? NSNumber).map {
                Date(timeIntervalSince1970: $0.doubleValue / 1000)
            }
            return Login(token: token, expiresAt: expires)
        }
    }

#if os(macOS)
    /// Through `security`, as `Quota` reads it: the Keychain API from this binary would
    /// prompt for an item another app owns. Claude Code's own account first, since an
    /// empty same-named item can shadow it.
    private static func keychainBlobs() -> [Data] {
        let user = ProcessInfo.processInfo.environment["USER"] ?? ""
        let account = user.isEmpty ? NSUserName() : user
        return [["-a", account], []].compactMap { extra -> Data? in
            let args = ["find-generic-password"] + extra + ["-s", "Claude Code-credentials", "-w"]
            guard case .exited(0, let out, _) = CommsClassifier.execute(
                "/usr/bin/security", args, input: Data(), timeout: 10) else { return nil }
            return out
        }
    }
#endif

    /// Has Claude Code refresh its logins: `claude mcp list` connects to every server it
    /// knows, refreshing an expired token on the way. Bounded, and silent on failure; the
    /// next read tells.
    public static func refresh(timeout: TimeInterval) {
        guard let claude = CommsClassifier.binary() else { return }
        _ = CommsClassifier.execute(claude, ["mcp", "list"], input: Data(), timeout: timeout)
    }
}

/// Slack's MCP server, spoken to directly over its streamable-HTTP JSON-RPC: one session
/// per instance, opened on the first call. Synchronous: a tick runs off the main thread on
/// both front-ends, and the CLI is a one-shot.
public struct SlackAPI {
    /// Not `URLRequest`: on Linux that lives in FoundationNetworking, whose libcurl link
    /// would stop the whole `diplomat-core` binary (every prompt the applet builds) loading
    /// on a machine without `libcurl.so.4`.
    public struct Request: Equatable, Sendable {
        public var url: URL
        public var headers: [String: String]
        /// Always a POST of this JSON.
        public var body: Data
        public var timeout: TimeInterval

        public func value(forHTTPHeaderField name: String) -> String? { headers[name] }
    }

    /// Header names lowercased.
    public typealias Transport = (Request) throws -> (status: Int, headers: [String: String], body: Data)

    public let token: String
    public let base: URL
    private let transport: Transport
    private let session = Session()

    /// No call starts after it, and none outlasts it.
    public var deadline: Date? {
        get { session.deadline }
        nonmutating set { session.deadline = newValue }
    }

    public init(token: String, base: URL? = nil, transport: Transport? = nil) {
        self.token = token
        self.base = base ?? Self.defaultBase()
        self.transport = transport ?? Self.live
    }

    public static func defaultBase() -> URL {
        if let env = ProcessInfo.processInfo.environment["DIPLOMAT_SLACK_API"], !env.isEmpty,
           let url = URL(string: env) {
            return url
        }
        return URL(string: "https://mcp.slack.com/mcp")!
    }

    /// Under Claude Code's Slack login, and who it is. `refresh`, when given, runs once at
    /// most: before the first call for a login past its expiry, or after Slack refuses it.
    public static func signIn(refresh: (() -> Void)? = nil) throws -> (api: SlackAPI, me: SlackProfile) {
        var refresh = refresh
        guard var login = SlackToken.read() else { throw SlackError.notSignedIn }
        if login.isExpired(), let r = refresh {
            r()
            refresh = nil
            login = SlackToken.read() ?? login
        }
        do {
            let api = SlackAPI(token: login.token)
            return (api, try api.auth())
        } catch SlackError.unauthorized {
            guard let r = refresh else { throw SlackError.unauthorized }
            r()
            guard let again = SlackToken.read() else { throw SlackError.notSignedIn }
            let api = SlackAPI(token: again.token)
            return (api, try api.auth())
        }
    }

    /// The token's own user.
    public func auth() throws -> SlackProfile {
        guard let me = SlackProfile.parse(try profileText([:])) else { throw SlackError.badResponse }
        return me
    }

    public func authTest() throws -> CommsIdentity { try auth().identity }

    public func profile(_ userID: String) throws -> SlackProfile? {
        SlackProfile.parse(try profileText(["user_id": userID]))
    }

    private func profileText(_ args: [String: Any]) throws -> String {
        let obj = try toolJSON("slack_read_user_profile",
                               args.merging(["response_format": "detailed"]) { a, _ in a })
        guard let text = obj["result"] as? String else { throw SlackError.badResponse }
        return text
    }

    /// One page (1-based) of `count` matches, newest first, from `after` (Unix seconds) on.
    public func search(_ query: CoreAssets.CommsModel.Query, after: Int, page: Int = 1,
                       count: Int = 20) throws -> [SlackMatch] {
        switch SlackSearchPage.parse(try searchText(query, after: after, page: page, count: count)) {
        case .matches(let matches):
            return matches
        case .ambiguous(let n):
            return try singles(query, after: after, from: (page - 1) * count + 1, want: n)
        }
    }

    /// Results `from`... one per call, until `want` distinct ones or the end. A message
    /// posted meanwhile shifts every index by one, so a repeat asks one further.
    private func singles(_ query: CoreAssets.CommsModel.Query, after: Int, from: Int,
                         want: Int) throws -> [SlackMatch] {
        var out: [SlackMatch] = []
        var index = from
        while out.count < want, index < from + 2 * want {
            guard let m = SlackSearchPage.parseSingle(
                try searchText(query, after: after, page: index, count: 1)) else { break }
            if !out.contains(where: { $0.id == m.id }) { out.append(m) }
            index += 1
        }
        return out
    }

    private func searchText(_ query: CoreAssets.CommsModel.Query, after: Int, page: Int,
                            count: Int) throws -> String {
        var args: [String: Any] = [
            "after": String(after), "sort": "timestamp",
            "sort_dir": "desc", "limit": count, "include_context": false,
            "include_bots": false, "content_types": "messages", "response_format": "detailed",
        ]
        if !query.keywords.isEmpty { args["keywords"] = query.keywords }
        if !query.filters.isEmpty { args["filters"] = query.filters }
        if page > 1 { args["cursor"] = Data("CURRENT_PAGE:\(page)".utf8).base64EncodedString() }
        let obj = try toolJSON("slack_search_public_and_private", args)
        guard let text = obj["results"] as? String else { throw SlackError.badResponse }
        return text
    }

    /// A tool's one text block, which Slack's tools fill with a JSON object.
    private func toolJSON(_ name: String, _ arguments: [String: Any]) throws -> [String: Any] {
        try open()
        let result = try rpc("tools/call", ["name": name, "arguments": arguments])
        let text = ((result?["content"] as? [Any])?.first as? [String: Any])?["text"] as? String
        if result?["isError"] as? Bool == true { throw SlackError.api(text ?? "\(name) failed") }
        guard let text,
              let obj = (try? JSONSerialization.jsonObject(with: Data(text.utf8))) as? [String: Any] else {
            throw SlackError.badResponse
        }
        return obj
    }

    private func open() throws {
        guard !session.opened else { return }
        _ = try rpc("initialize", [
            "protocolVersion": Self.protocolVersion, "capabilities": [String: Any](),
            "clientInfo": ["name": "diplomat", "version": "1"],
        ])
        _ = try rpc("notifications/initialized", nil, notify: true)
        session.opened = true
    }

    static let protocolVersion = "2025-06-18"

    /// The JSON-RPC `result`; nil for a notification.
    private func rpc(_ method: String, _ params: [String: Any]?,
                     notify: Bool = false) throws -> [String: Any]? {
        var message: [String: Any] = ["jsonrpc": "2.0", "method": method]
        if let params { message["params"] = params }
        if !notify {
            message["id"] = session.nextID
            session.nextID += 1
        }
        guard let body = try? JSONSerialization.data(withJSONObject: message,
                                                     options: [.withoutEscapingSlashes]) else {
            throw SlackError.badResponse
        }
        var headers = ["Authorization": "Bearer \(token)", "Content-Type": "application/json",
                       "Accept": "application/json, text/event-stream",
                       "MCP-Protocol-Version": Self.protocolVersion]
        if let id = session.id { headers["Mcp-Session-Id"] = id }
        var timeout = Self.timeout
        if let deadline = session.deadline {
            let left = deadline.timeIntervalSinceNow
            guard left > 0 else { throw SlackError.outOfTime }
            timeout = min(timeout, left)
        }
        let (status, answerHeaders, data) = try transport(
            Request(url: base, headers: headers, body: body, timeout: timeout))
        if status == 401 { throw SlackError.unauthorized }
        if status == 429 {
            throw SlackError.rateLimited(retryAfter: answerHeaders["retry-after"].flatMap { Int($0) })
        }
        guard (200..<300).contains(status) else { throw SlackError.http(status) }
        if let id = answerHeaders["mcp-session-id"], !id.isEmpty { session.id = id }
        if notify { return nil }
        guard let obj = Self.answer(data, eventStream:
                (answerHeaders["content-type"] ?? "").hasPrefix("text/event-stream")) else {
            throw SlackError.badResponse
        }
        if let error = obj["error"] as? [String: Any] {
            throw SlackError.api(error["message"] as? String ?? "error \(error["code"] ?? "")")
        }
        guard let result = obj["result"] as? [String: Any] else { throw SlackError.badResponse }
        return result
    }

    /// The response object, from a JSON body or the last `data:` event of a stream.
    static func answer(_ data: Data, eventStream: Bool) -> [String: Any]? {
        guard eventStream else {
            return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        }
        return String(decoding: data, as: UTF8.self).components(separatedBy: "\n")
            .filter { $0.hasPrefix("data:") }
            .compactMap { line -> [String: Any]? in
                let json = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
                return (try? JSONSerialization.jsonObject(with: Data(json.utf8))) as? [String: Any]
            }
            .last { $0["id"] != nil }
    }

    private final class Session: @unchecked Sendable {
        var id: String?
        var opened = false
        var nextID = 1
        var deadline: Date?
    }

    static var timeout: TimeInterval { (try? CoreAssets.comms())?.httpTimeoutSecs ?? 15 }

#if canImport(FoundationNetworking)
    /// Off Darwin, URLSession would need FoundationNetworking.
    public static func live(_ request: Request) throws -> (status: Int, headers: [String: String], body: Data) {
        try curl(request)
    }
#else
    /// The wait is bounded past the request's own timeout, so a session that never calls
    /// back cannot hold the tick.
    public static func live(_ request: Request) throws -> (status: Int, headers: [String: String], body: Data) {
        var urlRequest = URLRequest(url: request.url)
        urlRequest.httpMethod = "POST"
        urlRequest.httpBody = request.body
        urlRequest.timeoutInterval = request.timeout
        for (k, v) in request.headers { urlRequest.setValue(v, forHTTPHeaderField: k) }
        let box = Exchange()
        let task = URLSession.shared.dataTask(with: urlRequest) { data, response, error in
            box.finish(data: data, response: response as? HTTPURLResponse, error: error)
        }
        task.resume()
        guard box.done.wait(timeout: .now() + request.timeout + 5) == .success else {
            task.cancel()
            throw SlackError.transport("timed out after \(Int(request.timeout))s")
        }
        let (data, response, error) = box.result
        if let error { throw SlackError.transport(error.localizedDescription) }
        guard let response else { throw SlackError.badResponse }
        var headers: [String: String] = [:]
        for (k, v) in response.allHeaderFields {
            if let k = k as? String, let v = v as? String { headers[k.lowercased()] = v }
        }
        return (response.statusCode, headers, data ?? Data())
    }

    private final class Exchange: @unchecked Sendable {
        let done = DispatchSemaphore(value: 0)
        private let lock = NSLock()
        private var value: (Data?, HTTPURLResponse?, Error?) = (nil, nil, nil)

        func finish(data: Data?, response: HTTPURLResponse?, error: Error?) {
            lock.lock()
            value = (data, response, error)
            lock.unlock()
            done.signal()
        }

        var result: (Data?, HTTPURLResponse?, Error?) {
            lock.lock()
            defer { lock.unlock() }
            return value
        }
    }
#endif

    /// Through `curl`, so only Comms depends on it. The token and the body go in a config
    /// on stdin, never argv where `ps` shows them; `-q` comes first so no `~/.curlrc` is read (a
    /// `verbose` would print the token, a `write-out` corrupt the answer).
    public static func curl(_ request: Request) throws -> (status: Int, headers: [String: String], body: Data) {
        guard let curl = ["/usr/bin/curl", "/bin/curl"].first(where: {
            FileManager.default.isExecutableFile(atPath: $0)
        }) ?? LoginShell.onPath("curl") else {
            throw SlackError.transport("curl is not installed")
        }
        func quoted(_ s: String) -> String {
            "\"" + s.replacingOccurrences(of: "\\", with: "\\\\")
                .replacingOccurrences(of: "\"", with: "\\\"") + "\""
        }
        var config = "url = \(quoted(request.url.absoluteString))\n"
        for (k, v) in request.headers { config += "header = \(quoted("\(k): \(v)"))\n" }
        config += "data-binary = \(quoted(String(decoding: request.body, as: UTF8.self)))\n"
        let args = ["-q", "-sS", "--max-time", String(Int(request.timeout)), "-K", "-",
                    "-D", "-", "-o", "-"]
        switch CommsClassifier.execute(curl, args, input: Data(config.utf8),
                                       timeout: request.timeout + 5) {
        case .failedToStart(let why):
            throw SlackError.transport(why)
        case .timedOut:
            throw SlackError.transport("timed out after \(Int(request.timeout))s")
        case .exited(let code, _, let err) where code != 0:
            let why = String(decoding: err, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            throw SlackError.transport(why.isEmpty ? "curl exited \(code)" : why)
        case .exited(_, let out, _):
            return try parse(curlOutput: out)
        }
    }

    /// `curl -D - -o -` prints one header block per response (redirects and
    /// `100 Continue` included), then the body; the last block is the answer.
    public static func parse(curlOutput out: Data) throws -> (status: Int, headers: [String: String], body: Data) {
        let crlf = Data("\r\n\r\n".utf8)
        var rest = out[...]
        var status = 0
        var headers: [String: String] = [:]
        while rest.starts(with: Data("HTTP/".utf8)), let end = rest.range(of: crlf) {
            let lines = String(decoding: rest[rest.startIndex..<end.lowerBound], as: UTF8.self)
                .components(separatedBy: "\r\n")
            status = lines.first.flatMap { Int($0.split(separator: " ").dropFirst().first ?? "") } ?? 0
            headers = [:]
            for line in lines.dropFirst() {
                guard let colon = line.firstIndex(of: ":") else { continue }
                headers[line[..<colon].lowercased()] = line[line.index(after: colon)...]
                    .trimmingCharacters(in: .whitespaces)
            }
            rest = rest[end.upperBound...]
        }
        guard status > 0 else { throw SlackError.badResponse }
        return (status, headers, Data(rest))
    }
}
