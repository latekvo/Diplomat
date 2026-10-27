import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// One `search.messages` match, as Slack returns it. Every field is optional on the
/// wire; a missing one decodes as empty.
public struct SlackMatch: Decodable, Equatable, Sendable {
    public struct Channel: Decodable, Equatable, Sendable {
        public var id: String
        public var name: String
        public var isIM: Bool
        public var isMPIM: Bool
        public var isPrivate: Bool

        public init(id: String, name: String = "", isIM: Bool = false, isMPIM: Bool = false,
                    isPrivate: Bool = false) {
            self.id = id
            self.name = name
            self.isIM = isIM
            self.isMPIM = isMPIM
            self.isPrivate = isPrivate
        }

        private enum CodingKeys: String, CodingKey {
            case id, name, isIM = "is_im", isMPIM = "is_mpim", isPrivate = "is_private"
        }

        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            self.init(id: (try? c.decode(String.self, forKey: .id)) ?? "",
                      name: (try? c.decode(String.self, forKey: .name)) ?? "",
                      isIM: (try? c.decode(Bool.self, forKey: .isIM)) ?? false,
                      isMPIM: (try? c.decode(Bool.self, forKey: .isMPIM)) ?? false,
                      isPrivate: (try? c.decode(Bool.self, forKey: .isPrivate)) ?? false)
        }
    }

    public var ts: String
    public var text: String
    public var user: String
    public var username: String
    /// The workspace the message belongs to.
    public var team: String
    /// The sender's own workspace, which Slack sets on a message in a shared channel.
    public var userTeam: String
    public var permalink: String
    public var channel: Channel
    /// `im`, `mpim`, `message` or `group`.
    public var type: String
    public var botID: String
    public var subtype: String

    public init(ts: String, text: String = "", user: String = "", username: String = "",
                team: String = "", userTeam: String = "", permalink: String = "",
                channel: Channel, type: String = "message", botID: String = "",
                subtype: String = "") {
        self.ts = ts
        self.text = text
        self.user = user
        self.username = username
        self.team = team
        self.userTeam = userTeam
        self.permalink = permalink
        self.channel = channel
        self.type = type
        self.botID = botID
        self.subtype = subtype
    }

    private enum CodingKeys: String, CodingKey {
        case ts, text, user, username, team, userTeam = "user_team", permalink, channel, type
        case botID = "bot_id", subtype
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        func s(_ k: CodingKeys) -> String { (try? c.decode(String.self, forKey: k)) ?? "" }
        self.init(ts: s(.ts), text: s(.text), user: s(.user), username: s(.username),
                  team: s(.team), userTeam: s(.userTeam), permalink: s(.permalink),
                  channel: (try? c.decode(Channel.self, forKey: .channel)) ?? Channel(id: ""),
                  type: s(.type), botID: s(.botID), subtype: s(.subtype))
    }

    /// `<channelID>:<ts>`, the id every Comms event about this message carries.
    public var id: String { "\(channel.id):\(ts)" }

    public var isDM: Bool {
        channel.isIM || channel.isMPIM || type == "im" || type == "mpim"
    }
}

/// Which matches become pings, and why. Pure.
public enum CommsMatch {
    /// Why `match` concerns the operator: a DM, a mention of their user id, a keyword
    /// standing as a whole word (case-insensitive). Read off the match itself rather than
    /// off the query that returned it, so one message found by two queries is one ping
    /// with both reasons.
    public static func reasons(for match: SlackMatch, me: CommsIdentity,
                               keywords: [String]) -> (reasons: [CommsReason], keyword: String?) {
        var out: [CommsReason] = []
        if match.isDM { out.append(.dm) }
        if !me.userID.isEmpty,
           match.text.contains("<@\(me.userID)>") || match.text.contains("<@\(me.userID)|") {
            out.append(.mention)
        }
        let keyword = keywords.first { containsWord(match.text, $0) }
        if keyword != nil { out.append(.keyword) }
        return (out, keyword)
    }

    /// Whether a message may become a ping at all. Refused: the operator's own messages,
    /// a bot's (a `bot_id`, the `bot_message` subtype, or no user), anyone whose
    /// workspace is not the operator's (Slack Connect — either team field naming another
    /// workspace), and, when `allowlist` is not empty, anyone it does not name by user id
    /// or handle.
    public static func admit(_ match: SlackMatch, me: CommsIdentity, allowlist: [String]) -> Bool {
        if match.user.isEmpty || match.user == me.userID { return false }
        if !match.botID.isEmpty || match.subtype == "bot_message" { return false }
        if [match.team, match.userTeam].contains(where: { !$0.isEmpty && $0 != me.teamID }) {
            return false
        }
        guard !allowlist.isEmpty else { return true }
        return allowlist.contains { entry in
            entry == match.user
                || (!match.username.isEmpty && entry.lowercased() == match.username.lowercased())
        }
    }

    /// The ping `match` is, or nil when it is not one: refused by `admit`, carrying no
    /// reason, or with no ts to order it by.
    public static func message(from match: SlackMatch, me: CommsIdentity,
                               settings: CommsSettings) -> CommsMessage? {
        guard CommsTS.number(match.ts) != nil, !match.channel.id.isEmpty,
              admit(match, me: me, allowlist: settings.senderAllowlist) else { return nil }
        let (reasons, keyword) = self.reasons(for: match, me: me, keywords: settings.keywords)
        guard !reasons.isEmpty else { return nil }
        return CommsMessage(id: match.id, channelID: match.channel.id,
                            channelName: match.channel.name, isDM: match.isDM,
                            userID: match.user, userName: match.username,
                            teamID: match.userTeam.isEmpty ? match.team : match.userTeam,
                            text: match.text, ts: match.ts, permalink: match.permalink,
                            reasons: reasons, keyword: keyword)
    }

    /// `word` in `text` with no letter or digit either side of it.
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

/// What a Slack call can fail with. The descriptions are what the Comms screen's error
/// banner shows.
public enum SlackError: LocalizedError, Equatable {
    case transport(String)
    case http(Int)
    /// `ok: false`, with Slack's error code.
    case api(String)
    /// HTTP 429 or `ratelimited`; the tick is skipped, and the next one asks again.
    case rateLimited(retryAfter: Int?)
    case badResponse

    public var errorDescription: String? {
        switch self {
        case .transport(let s):
            return "Slack could not be reached: \(s)"
        case .http(let code):
            return "Slack answered HTTP \(code)"
        case .api(let code) where code == "missing_scope":
            return "Slack: missing_scope. The token needs the user scopes search:read and users:read."
        case .api(let code) where ["invalid_auth", "not_authed", "token_revoked",
                                   "token_expired", "account_inactive"].contains(code):
            return "Slack rejected the token (\(code))"
        case .api(let code):
            return "Slack: \(code)"
        case .rateLimited(let after):
            return "Slack rate-limited the watcher" + (after.map { " (retry after \($0)s)" } ?? "")
        case .badResponse:
            return "Slack answered something that is not its JSON"
        }
    }
}

/// The two documented Web API methods Comms reads, with a user token (`xoxp-`).
/// Synchronous: a tick runs off the main thread on both front-ends, and the CLI is a
/// one-shot.
public struct SlackAPI {
    /// One HTTP exchange: the status, the headers (names lowercased) and the body.
    public typealias Transport = (URLRequest) throws -> (status: Int, headers: [String: String], body: Data)

    public let token: String
    public let base: URL
    private let transport: Transport

    /// `base` defaults to `$DIPLOMAT_SLACK_API`, else `https://slack.com/api/`;
    /// `transport` to a URLSession exchange. Tests replace either.
    public init(token: String, base: URL? = nil, transport: Transport? = nil) {
        self.token = token
        self.base = base ?? Self.defaultBase()
        self.transport = transport ?? Self.urlSession
    }

    public static func defaultBase() -> URL {
        if let env = ProcessInfo.processInfo.environment["DIPLOMAT_SLACK_API"], !env.isEmpty,
           let url = URL(string: env.hasSuffix("/") ? env : env + "/") {
            return url
        }
        return URL(string: "https://slack.com/api/")!
    }

    /// `auth.test`: who the token is.
    public func authTest() throws -> CommsIdentity {
        let obj = try call("auth.test", [])
        guard let user = obj["user_id"] as? String, let team = obj["team_id"] as? String else {
            throw SlackError.badResponse
        }
        return CommsIdentity(userID: user, teamID: team, handle: obj["user"] as? String ?? "")
    }

    /// One page of `search.messages`, newest first.
    public func search(query: String, page: Int = 1,
                       count: Int = 50) throws -> (matches: [SlackMatch], pages: Int) {
        let obj = try call("search.messages", [
            URLQueryItem(name: "query", value: query),
            URLQueryItem(name: "sort", value: "timestamp"),
            URLQueryItem(name: "sort_dir", value: "desc"),
            URLQueryItem(name: "count", value: String(count)),
            URLQueryItem(name: "page", value: String(page)),
        ])
        guard let messages = obj["messages"] as? [String: Any] else { throw SlackError.badResponse }
        let raw = messages["matches"] as? [Any] ?? []
        let matches: [SlackMatch] = raw.compactMap { m in
            guard let data = try? JSONSerialization.data(withJSONObject: m) else { return nil }
            return try? JSONDecoder().decode(SlackMatch.self, from: data)
        }
        let pages = ((messages["paging"] as? [String: Any])?["pages"] as? Int) ?? 1
        return (matches, pages)
    }

    private func call(_ method: String, _ query: [URLQueryItem]) throws -> [String: Any] {
        guard var components = URLComponents(url: base.appendingPathComponent(method),
                                             resolvingAgainstBaseURL: false) else {
            throw SlackError.badResponse
        }
        if !query.isEmpty {
            components.queryItems = query
            // URLComponents leaves `+` alone, which a form decoder reads as a space.
            let encoded = components.percentEncodedQuery
            components.percentEncodedQuery = encoded?.replacingOccurrences(of: "+", with: "%2B")
        }
        guard let url = components.url else { throw SlackError.badResponse }
        var request = URLRequest(url: url)
        request.timeoutInterval = Self.timeout
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let (status, headers, body) = try transport(request)
        if status == 429 {
            throw SlackError.rateLimited(retryAfter: headers["retry-after"].flatMap { Int($0) })
        }
        guard status == 200 else { throw SlackError.http(status) }
        guard let obj = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any] else {
            throw SlackError.badResponse
        }
        guard obj["ok"] as? Bool == true else {
            let code = obj["error"] as? String ?? "unknown_error"
            if code == "ratelimited" {
                throw SlackError.rateLimited(retryAfter: headers["retry-after"].flatMap { Int($0) })
            }
            throw SlackError.api(code)
        }
        return obj
    }

    static var timeout: TimeInterval { (try? CoreAssets.comms())?.httpTimeoutSecs ?? 15 }

    /// The live exchange. The wait is bounded past the request's own timeout, so a
    /// session that never calls back cannot hold the tick.
    public static func urlSession(_ request: URLRequest) throws -> (status: Int, headers: [String: String], body: Data) {
        let box = Exchange()
        let task = URLSession.shared.dataTask(with: request) { data, response, error in
            box.finish(data: data, response: response as? HTTPURLResponse, error: error)
        }
        task.resume()
        guard box.done.wait(timeout: .now() + request.timeoutInterval + 5) == .success else {
            task.cancel()
            throw SlackError.transport("timed out after \(Int(request.timeoutInterval))s")
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
}
