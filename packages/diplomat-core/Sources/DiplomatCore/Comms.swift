import Foundation

// MARK: - Comms: the Slack watcher's model
//
// Each Slack message addressed to the operator (DM, @-mention, keyword) is a *ping*; one
// sandboxed `claude` call triages it (`CommsClassifier`) and `CommsPlan` maps the answer
// onto the queue. Slack reads are `SlackAPI`, the tick is `CommsWatch`. The Linux
// front-end reaches these rules through the `diplomat-core` CLI (`comms-tick`,
// `comms-feed`, `comms-effect`), never by reimplementing them.

/// Why a message reached the operator; a message can carry several.
public enum CommsReason: String, Codable, CaseIterable, Sendable {
    case dm, mention, keyword
}

/// Who the Slack login belongs to, from their profile (`SlackProfile.identity`).
public struct CommsIdentity: Codable, Equatable, Sendable {
    public var userID: String
    /// The workspace as `SlackProfile.workspace` names it, not a Slack team id.
    public var teamID: String
    public var handle: String

    public init(userID: String, teamID: String, handle: String) {
        self.userID = userID
        self.teamID = teamID
        self.handle = handle
    }
}

/// `id` is `<channelID>:<ts>`: workspace-unique, and the key of every event about it.
public struct CommsMessage: Codable, Equatable, Sendable {
    public var id: String
    public var channelID: String
    /// For a DM, whatever Slack names it (the other user's id).
    public var channelName: String
    public var isDM: Bool
    public var userID: String
    public var userName: String
    public var teamID: String
    public var text: String
    public var ts: String
    public var permalink: String
    public var reasons: [CommsReason]
    public var keyword: String?

    public init(id: String, channelID: String, channelName: String, isDM: Bool,
                userID: String, userName: String, teamID: String, text: String, ts: String,
                permalink: String, reasons: [CommsReason], keyword: String? = nil) {
        self.id = id
        self.channelID = channelID
        self.channelName = channelName
        self.isDM = isDM
        self.userID = userID
        self.userName = userName
        self.teamID = teamID
        self.text = text
        self.ts = ts
        self.permalink = permalink
        self.reasons = reasons
        self.keyword = keyword
    }

    public var sender: String { userName.isEmpty ? userID : userName }

    public var place: String { isDM ? "DM" : "#\(channelName)" }
}

/// The raw values are the classifier schema's enum.
public enum CommsAction: String, Codable, CaseIterable, Sendable {
    case none, bump, review
    case fixIssue = "fix_issue"
    case investigate

    public var needsNumber: Bool {
        switch self {
        case .bump, .review, .fixIssue: return true
        case .none, .investigate:       return false
        }
    }
}

/// The triage's answer, valid by construction: every initializer (memberwise, decoding,
/// `parse`) routes through one check, so a PR action without a positive number reads
/// `none`, with the reason recorded. Whether the message holds that number is
/// `checked(against:)`; whether it is an OPEN PR or issue is the front-end's check
/// against GitHub before it acts.
public struct CommsDecision: Codable, Equatable, Sendable {
    public let action: CommsAction
    /// 0 unless `action.needsNumber`.
    public let number: Int
    public let summary: String
    public let reason: String

    /// Classifier prose is clipped to this; it is shown on one row.
    static let maxText = 240

    public init(action: CommsAction, number: Int, summary: String, reason: String) {
        let summary = Self.clip(summary)
        let reason = Self.clip(reason)
        if action.needsNumber && number <= 0 {
            self.action = .none
            self.number = 0
            self.summary = summary
            self.reason = "answered \(action.rawValue) without a positive PR or issue number"
                + " (\(number)); \(reason)"
        } else {
            self.action = action
            self.number = action.needsNumber ? number : 0
            self.summary = summary
            self.reason = reason
        }
    }

    public static func none(_ reason: String) -> CommsDecision {
        CommsDecision(action: .none, number: 0, summary: "", reason: reason)
    }

    /// This decision, or `none` when it names a number `message` does not contain: as
    /// `#N`, `/pull/N`, `/issues/N` or `N`, with no letter or digit either side.
    /// `CommsWatch.tick` applies it to every answer, so a guessed number never acts.
    public func checked(against message: CommsMessage) -> CommsDecision {
        guard action.needsNumber, !CommsMatch.containsWord(message.text, String(number)) else {
            return self
        }
        return CommsDecision(action: .none, number: 0, summary: summary,
                             reason: "answered \(action.rawValue) #\(number), a number the "
                                 + "message does not contain; \(reason)")
    }

    private enum CodingKeys: String, CodingKey { case action, number, summary, reason }

    /// Field by field, so one bad field costs the decision its action instead of failing
    /// the object: an unknown action, or a number that is missing, fractional, a string or
    /// a boolean, lands in `init` as something it refuses.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let summary = (try? c.decode(String.self, forKey: .summary)) ?? ""
        let reason = (try? c.decode(String.self, forKey: .reason)) ?? ""
        let raw = (try? c.decode(String.self, forKey: .action)) ?? ""
        guard let action = CommsAction(rawValue: raw) else {
            self.init(action: .none, number: 0, summary: summary,
                      reason: "answered an action outside the schema (\"\(raw)\"); \(reason)")
            return
        }
        self.init(action: action, number: (try? c.decode(Int.self, forKey: .number)) ?? 0,
                  summary: summary, reason: reason)
    }

    /// `claude -p --output-format json` prints a JSON array of events whose
    /// `type: "result"` element carries the answer, or (older CLIs) that object alone.
    /// `structured_output`, the schema-constrained object, wins over `result`, the same
    /// answer as text.
    public static func parse(classifierOutput: Data) -> CommsDecision {
        answer(in: classifierOutput) ?? .none("the classifier's output held no answer")
    }

    /// Nil for output with no answer at all: `CommsClassifier` counts only that, not a
    /// bad answer, as the classifier failing.
    static func answer(in output: Data) -> CommsDecision? {
        guard let root = try? JSONSerialization.jsonObject(with: output) else { return nil }
        let result: [String: Any]?
        if let events = root as? [Any] {
            result = events.reversed().lazy.compactMap { $0 as? [String: Any] }
                .first { $0["type"] as? String == "result" }
        } else {
            result = root as? [String: Any]
        }
        guard let result else { return nil }
        if result["is_error"] as? Bool == true {
            let text = (result["result"] as? String) ?? (result["subtype"] as? String) ?? "?"
            return .none(CommsClassifier.failurePrefix + "claude reported an error: \(clip(text))")
        }
        if let structured = result["structured_output"] as? [String: Any],
           let data = try? JSONSerialization.data(withJSONObject: structured) {
            return decode(data)
        }
        if let text = result["result"] as? String {
            return decode(Data(unfenced(text).utf8))
        }
        // A bare decision object, the shape a stub classifier prints.
        if result["action"] != nil, let data = try? JSONSerialization.data(withJSONObject: result) {
            return decode(data)
        }
        return nil
    }

    private static func decode(_ data: Data) -> CommsDecision {
        (try? JSONDecoder().decode(CommsDecision.self, from: data))
            ?? .none("the classifier's answer was not a JSON object")
    }

    /// A model answering in prose sometimes wraps it in a Markdown code fence.
    private static func unfenced(_ text: String) -> String {
        var s = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard s.hasPrefix("```") else { return s }
        if let nl = s.firstIndex(of: "\n") { s = String(s[s.index(after: nl)...]) }
        if s.hasSuffix("```") { s = String(s.dropLast(3)) }
        return s
    }

    private static func clip(_ s: String) -> String {
        let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
        return t.count > maxText ? String(t.prefix(maxText - 1)) + "…" : t
    }
}

/// What a front-end passes `CommsWatch.tick`.
public struct CommsSettings: Codable, Equatable, Sendable {
    public var keywords: [String]
    /// Slack user ids or handles; empty admits any member of the operator's workspace.
    public var senderAllowlist: [String]
    public var model: String
    public var maxPerTick: Int
    public var owner: String
    public var repo: String
    /// The operator's GitHub login; the classifier is told the message was sent to it.
    public var login: String

    public init(keywords: [String] = [], senderAllowlist: [String] = [],
                model: String? = nil, maxPerTick: Int? = nil,
                owner: String? = nil, repo: String? = nil, login: String = "") {
        let assets = try? CoreAssets.comms()
        let coordinates = CoreAssets.repoCoordinates()
        self.keywords = keywords
        self.senderAllowlist = senderAllowlist
        let m = (model ?? "").trimmingCharacters(in: .whitespaces)
        self.model = m.isEmpty ? (assets?.model ?? "haiku") : m
        self.maxPerTick = max(1, maxPerTick ?? assets?.maxPerTick ?? 8)
        self.owner = owner ?? coordinates.owner
        self.repo = repo ?? coordinates.repo
        self.login = login
    }

    /// Not split on spaces: a keyword may be a phrase.
    public static func keywordList(_ raw: String) -> [String] {
        unique(raw.split(whereSeparator: { $0 == "," || $0.isNewline }))
    }

    public static func allowlist(_ raw: String) -> [String] {
        unique(raw.split(whereSeparator: { $0 == "," || $0.isWhitespace }).map {
            $0.hasPrefix("@") ? $0.dropFirst() : $0
        })
    }

    private static func unique<S: StringProtocol>(_ parts: [S]) -> [String] {
        var out: [String] = []
        for p in parts {
            let t = p.trimmingCharacters(in: .whitespaces)
            if !t.isEmpty && !out.contains(t) { out.append(t) }
        }
        return out
    }

    private enum CodingKeys: String, CodingKey {
        case keywords, senderAllowlist, model, maxPerTick, owner, repo, login
        case keywordsRaw, senderAllowlistRaw
    }

    /// Every field optional. The lists may come as the raw Settings strings (`keywordsRaw`,
    /// `senderAllowlistRaw`), so the splitting stays here rather than in each front-end.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let keywords = try c.decodeIfPresent([String].self, forKey: .keywords)
            ?? Self.keywordList(try c.decodeIfPresent(String.self, forKey: .keywordsRaw) ?? "")
        let allow = try c.decodeIfPresent([String].self, forKey: .senderAllowlist)
            ?? Self.allowlist(try c.decodeIfPresent(String.self, forKey: .senderAllowlistRaw) ?? "")
        self.init(keywords: keywords, senderAllowlist: allow,
                  model: try c.decodeIfPresent(String.self, forKey: .model),
                  maxPerTick: try c.decodeIfPresent(Int.self, forKey: .maxPerTick),
                  owner: try c.decodeIfPresent(String.self, forKey: .owner),
                  repo: try c.decodeIfPresent(String.self, forKey: .repo),
                  login: try c.decodeIfPresent(String.self, forKey: .login) ?? "")
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(keywords, forKey: .keywords)
        try c.encode(senderAllowlist, forKey: .senderAllowlist)
        try c.encode(model, forKey: .model)
        try c.encode(maxPerTick, forKey: .maxPerTick)
        try c.encode(owner, forKey: .owner)
        try c.encode(repo, forKey: .repo)
        try c.encode(login, forKey: .login)
    }
}

/// Per-kind switches (`requests` covers reviews and issue fixes); off turns that kind
/// into a ping alone.
public struct CommsToggles: Codable, Equatable, Sendable {
    public var requests: Bool
    public var bumps: Bool
    public var investigations: Bool

    public init(requests: Bool = true, bumps: Bool = true, investigations: Bool = true) {
        self.requests = requests
        self.bumps = bumps
        self.investigations = investigations
    }

    private enum CodingKeys: String, CodingKey { case requests, bumps, investigations }

    /// A missing switch reads as on.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(requests: try c.decodeIfPresent(Bool.self, forKey: .requests) ?? true,
                  bumps: try c.decodeIfPresent(Bool.self, forKey: .bumps) ?? true,
                  investigations: try c.decodeIfPresent(Bool.self, forKey: .investigations) ?? true)
    }
}

/// What a front-end does about one triaged ping, through its ordinary queue: the ban
/// list, the task cap and the budget gate all apply.
///
/// JSON: `{"kind": "none"}`, `{"kind": "review" | "fix_issue" | "bump", "number": N}`,
/// `{"kind": "investigate", "config": InvestigateConfig}`.
public enum CommsEffect: Equatable, Sendable {
    case none
    /// Move whatever is already queued for #N to the front of its band; start nothing.
    case bump(Int)
    /// `review` and `fixIssue` queue at the front of their band.
    case review(Int)
    case fixIssue(Int)
    case investigate(InvestigateConfig)
}

extension CommsEffect: Codable {
    private enum CodingKeys: String, CodingKey { case kind, number, config }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let kind = try c.decode(String.self, forKey: .kind)
        switch kind {
        case "none":        self = .none
        case "bump":        self = .bump(try c.decode(Int.self, forKey: .number))
        case "review":      self = .review(try c.decode(Int.self, forKey: .number))
        case "fix_issue":   self = .fixIssue(try c.decode(Int.self, forKey: .number))
        case "investigate": self = .investigate(try c.decode(InvestigateConfig.self, forKey: .config))
        default:
            throw DecodingError.dataCorruptedError(forKey: .kind, in: c,
                                                   debugDescription: "unknown effect \(kind)")
        }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .none:
            try c.encode("none", forKey: .kind)
        case .bump(let n):
            try c.encode("bump", forKey: .kind)
            try c.encode(n, forKey: .number)
        case .review(let n):
            try c.encode("review", forKey: .kind)
            try c.encode(n, forKey: .number)
        case .fixIssue(let n):
            try c.encode("fix_issue", forKey: .kind)
            try c.encode(n, forKey: .number)
        case .investigate(let cfg):
            try c.encode("investigate", forKey: .kind)
            try c.encode(cfg, forKey: .config)
        }
    }
}

/// The one mapping from a triage answer to an effect.
public enum CommsPlan {
    public static func effect(for decision: CommsDecision, message: CommsMessage,
                              toggles: CommsToggles, settings: CommsSettings = CommsSettings(),
                              reportDir: URL) -> CommsEffect {
        switch decision.action {
        case .none:        return .none
        case .review:      return toggles.requests ? .review(decision.number) : .none
        case .fixIssue:    return toggles.requests ? .fixIssue(decision.number) : .none
        case .bump:        return toggles.bumps ? .bump(decision.number) : .none
        case .investigate:
            guard toggles.investigations else { return .none }
            return .investigate(InvestigateConfig(message: message, decision: decision,
                                                  settings: settings, reportDir: reportDir))
        }
    }
}

/// A `ts` is `<seconds>.<microseconds>` and unique per channel, so its digits order
/// messages and number an investigation in the queue.
public enum CommsTS {
    /// `"1508284197.000015"` → `1508284197000015`; nil unless two runs of digits around one
    /// dot. A short fraction is padded on the right to six digits, a long one cut to six.
    public static func number(_ ts: String) -> Int? {
        let parts = ts.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 2, !parts[0].isEmpty, !parts[1].isEmpty,
              parts.allSatisfy({ $0.allSatisfy { $0.isASCII && $0.isNumber } }),
              parts[0].count <= 12,
              let secs = Int(parts[0]) else { return nil }
        let frac = String(parts[1].prefix(6)).padding(toLength: 6, withPad: "0", startingAt: 0)
        guard let micros = Int(frac) else { return nil }
        return secs * 1_000_000 + micros
    }

    /// The inverse of `number`.
    public static func string(_ n: Int) -> String {
        let micros = String(n % 1_000_000)
        return "\(n / 1_000_000)." + String(repeating: "0", count: 6 - micros.count) + micros
    }

    /// `date` in the same units.
    public static func number(_ date: Date) -> Int {
        Int((date.timeIntervalSince1970 * 1_000_000).rounded(.down))
    }
}

/// A queued `investigate` ask and its agent's prompt (`assets/comms.json` → `investigate`).
///
/// JSON carries `"kind": "investigate"`, so the same object is a `build-prompt` payload
/// and the Linux queue's `RequestedWork.config` as it stands.
public struct InvestigateConfig: Codable, Equatable, Sendable {
    /// `CommsTS.number` of the message's ts: the ask's number in the queue.
    public var number: Int
    public var messageID: String
    public var sender: String
    public var channel: String
    public var permalink: String
    public var text: String
    public var summary: String
    public var reportPath: String
    public var owner: String
    public var repo: String

    public static let kind = "investigate"

    public init(number: Int, messageID: String, sender: String, channel: String,
                permalink: String, text: String, summary: String, reportPath: String,
                owner: String, repo: String) {
        self.number = number
        self.messageID = messageID
        self.sender = sender
        self.channel = channel
        self.permalink = permalink
        self.text = text
        self.summary = summary
        self.reportPath = reportPath
        self.owner = owner
        self.repo = repo
    }

    public init(message: CommsMessage, decision: CommsDecision, settings: CommsSettings,
                reportDir: URL) {
        let number = CommsTS.number(message.ts) ?? 0
        self.init(number: number, messageID: message.id, sender: message.sender,
                  channel: message.place, permalink: message.permalink, text: message.text,
                  summary: decision.summary,
                  reportPath: reportDir.appendingPathComponent("\(number).md").path,
                  owner: settings.owner, repo: settings.repo)
    }

    /// `kind` and `label` are written for readers and never decoded: both follow from the rest.
    private enum CodingKeys: String, CodingKey {
        case kind, number, messageID, sender, channel, permalink, text, summary,
             reportPath, owner, repo, label
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let coordinates = CoreAssets.repoCoordinates()
        func s(_ k: CodingKeys) throws -> String { try c.decodeIfPresent(String.self, forKey: k) ?? "" }
        self.init(number: try c.decode(Int.self, forKey: .number),
                  messageID: try s(.messageID), sender: try s(.sender), channel: try s(.channel),
                  permalink: try s(.permalink), text: try s(.text), summary: try s(.summary),
                  reportPath: try s(.reportPath),
                  owner: try c.decodeIfPresent(String.self, forKey: .owner) ?? coordinates.owner,
                  repo: try c.decodeIfPresent(String.self, forKey: .repo) ?? coordinates.repo)
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(Self.kind, forKey: .kind)
        try c.encode(number, forKey: .number)
        try c.encode(messageID, forKey: .messageID)
        try c.encode(sender, forKey: .sender)
        try c.encode(channel, forKey: .channel)
        try c.encode(permalink, forKey: .permalink)
        try c.encode(text, forKey: .text)
        try c.encode(summary, forKey: .summary)
        try c.encode(reportPath, forKey: .reportPath)
        try c.encode(owner, forKey: .owner)
        try c.encode(repo, forKey: .repo)
        try c.encode(label, forKey: .label)
    }

    /// The queue row and activity-feed label. Both front-ends tell runs apart by label, so
    /// it ends in the message's local time: one sender's two asks with the same summary
    /// would otherwise wear one.
    public var label: String {
        let cap = (try? CoreAssets.comms())?.labelSummaryMax ?? 48
        let s = summary.trimmingCharacters(in: .whitespacesAndNewlines)
        let clipped = s.count > cap ? String(s.prefix(cap - 1)) + "…" : s
        let sent = Date(timeIntervalSince1970: TimeInterval(number) / 1_000_000)
        return "Investigate · Slack · \(sender) · \(clipped) · \(Self.clock.string(from: sent))"
    }

    private static let clock: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "HH:mm:ss"
        return f
    }()

    public func buildPrompt() -> String {
        guard let b = (try? CoreAssets.comms())?.investigate else { return "" }
        // Sender and channel sit outside the fence, so they must stay on one line.
        let values = [
            "owner": owner, "repo": repo, "permalink": permalink, "reportPath": reportPath,
            "sender": CommsText.oneLine(CommsText.defang(sender)),
            "channel": CommsText.oneLine(CommsText.defang(channel)),
            "summary": CommsText.defang(summary), "text": CommsText.defang(text),
        ]
        return [b.intro, b.message, b.work, b.rules, b.report]
            .map { CommsText.fillFenced($0, values) }
            .joined(separator: "\n\n")
    }
}

/// Text handling shared by both prompts that quote a Slack message.
public enum CommsText {
    /// Fence lines are `BEGIN <marker>` / `END <marker>`.
    public static let fenceMarker = "UNTRUSTED SLACK MESSAGE"

    /// Starts every line inside a fence, so no line of a message can be a fence line,
    /// however it is spelled.
    public static let quoteMarker = "|"

    /// `fill`, with every line between a `BEGIN` and an `END` fence line prefixed with
    /// `quoteMarker` and a space (an empty line, the marker alone). Any line break in a
    /// value starts a quoted line.
    public static func fillFenced(_ template: String, _ values: [String: String]) -> String {
        let begin = "BEGIN " + fenceMarker, end = "END " + fenceMarker
        var out: [String] = []
        var inside: [Substring]?
        for line in template.split(separator: "\n", omittingEmptySubsequences: false) {
            if var body = inside {
                if line == end {
                    out.append(quote(fill(body.joined(separator: "\n"), values)))
                    out.append(String(line))
                    inside = nil
                } else {
                    body.append(line)
                    inside = body
                }
            } else {
                out.append(fill(String(line), values))
                if line == begin { inside = [] }
            }
        }
        if let body = inside { out.append(quote(fill(body.joined(separator: "\n"), values))) }
        return out.joined(separator: "\n")
    }

    private static func quote(_ text: String) -> String {
        text.split(omittingEmptySubsequences: false, whereSeparator: \.isNewline)
            .map { $0.isEmpty ? quoteMarker : quoteMarker + " " + $0 }
            .joined(separator: "\n")
    }

    /// For a value set outside a fence.
    public static func oneLine(_ text: String) -> String {
        text.split(whereSeparator: \.isNewline).joined(separator: " ")
    }

    /// Replaces copies of the fence marker. Best-effort only: the quoting in `fillFenced`
    /// is what holds the fence. Catches the marker's letters in order with only
    /// non-alphanumerics between, in any case, fullwidth, accented, or as a `lookalikes`
    /// letter; other look-alikes pass.
    public static func defang(_ text: String) -> String {
        let scalars = Array(text.unicodeScalars)
        // The letters and digits of `text`, folded, each with the scalar it came from.
        var skeleton: [Unicode.Scalar] = []
        var origin: [Int] = []
        for (i, s) in scalars.enumerated() {
            for f in fold(s) {
                skeleton.append(f)
                origin.append(i)
            }
        }
        let marker = Array(fenceMarker.unicodeScalars.filter { $0 != " " })
        var cuts: [(from: Int, to: Int)] = []
        var k = 0
        while k + marker.count <= skeleton.count {
            if skeleton[k..<k + marker.count].elementsEqual(marker) {
                cuts.append((origin[k], origin[k + marker.count - 1]))
                k += marker.count
            } else {
                k += 1
            }
        }
        guard !cuts.isEmpty else { return text }
        var out = String.UnicodeScalarView()
        var next = 0
        for cut in cuts {
            out.append(contentsOf: scalars[next..<cut.from])
            out.append(contentsOf: "[fence marker removed]".unicodeScalars)
            next = cut.to + 1
        }
        out.append(contentsOf: scalars[next...])
        return String(out)
    }

    /// `s` as the uppercase ASCII letters or digits it reads as; empty for anything else.
    private static func fold(_ s: Unicode.Scalar) -> [Unicode.Scalar] {
        if let latin = lookalikes[s.value] { return [latin] }
        // Compatibility decomposition turns fullwidth and styled letters into ASCII, and
        // an accented one into its base letter plus combining marks, which drop out.
        let base = s.isASCII ? String(s) : String(s).decomposedStringWithCompatibilityMapping
        return base.uppercased().unicodeScalars.filter {
            $0.isASCII && ("A"..."Z" ~= $0 || "0"..."9" ~= $0)
        }
    }

    /// Cyrillic and Greek letters drawn like a Latin capital, by code point.
    private static let lookalikes: [UInt32: Unicode.Scalar] = {
        let pairs: [(String, Unicode.Scalar)] = [
            ("Аа", "A"), ("В", "B"), ("Сс", "C"), ("ЕеЁё", "E"), ("Нн", "H"), ("ІіЇї", "I"),
            ("Јј", "J"), ("Кк", "K"), ("Мм", "M"), ("Оо", "O"), ("Рр", "P"), ("Ѕѕ", "S"),
            ("Тт", "T"), ("Хх", "X"), ("Уу", "Y"), ("Ԁԁ", "D"), ("Ԍ", "G"), ("Ӏ", "L"),
            ("Α", "A"), ("Β", "B"), ("Ε", "E"), ("Ζ", "Z"), ("Η", "H"), ("Ιι", "I"),
            ("Κκ", "K"), ("Μ", "M"), ("Ν", "N"), ("Οο", "O"), ("Ρρ", "P"), ("Ττ", "T"),
            ("Υυ", "Y"), ("Χχ", "X"),
        ]
        var map: [UInt32: Unicode.Scalar] = [:]
        for (from, to) in pairs {
            for s in from.unicodeScalars { map[s.value] = to }
        }
        return map
    }()

    /// Each `{name}` replaced by `values[name]` in one pass: a value is never rescanned, so
    /// a message containing `{reportPath}` stays as typed. Unknown names are left as is.
    public static func fill(_ template: String, _ values: [String: String]) -> String {
        var out = ""
        var rest = Substring(template)
        while let open = rest.firstIndex(of: "{") {
            out += rest[..<open]
            let after = rest[rest.index(after: open)...]
            if let close = after.firstIndex(of: "}"),
               let value = values[String(after[..<close])] {
                out += value
                rest = after[after.index(after: close)...]
            } else {
                out += "{"
                rest = after
            }
        }
        return out + rest
    }
}
