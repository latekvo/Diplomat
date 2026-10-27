import Foundation

// MARK: - Comms: the Slack watcher's model
//
// Diplomat watches the operator's Slack for messages addressed to them — a DM, an
// @-mention, or one of their keywords — records each as a *ping*, has one sandboxed
// `claude` call triage it into a fixed JSON shape (`CommsClassifier`), and maps the
// validated answer onto the queue (`CommsPlan`). This file holds the values and the
// pure rules; the Slack reads are `SlackAPI`, the tick that drives both is `CommsWatch`.
// Both front-ends act on what these decide; the Linux one reaches them through the
// `diplomat-core` CLI (`comms-tick`, `comms-feed`, `comms-effect`), never by
// reimplementing them.

/// Why a message reached the operator. A message can carry several (a DM that also
/// names a keyword).
public enum CommsReason: String, Codable, CaseIterable, Sendable {
    case dm, mention, keyword
}

/// Who the token belongs to, as `auth.test` answers it.
public struct CommsIdentity: Codable, Equatable, Sendable {
    public var userID: String
    public var teamID: String
    public var handle: String

    public init(userID: String, teamID: String, handle: String) {
        self.userID = userID
        self.teamID = teamID
        self.handle = handle
    }
}

/// One Slack message addressed to the operator. `id` is `<channelID>:<ts>`, unique
/// across the workspace, and is what every event about it is keyed by.
public struct CommsMessage: Codable, Equatable, Sendable {
    public var id: String
    public var channelID: String
    /// The channel's name; for a DM, whatever Slack names it (the other user's id).
    public var channelName: String
    public var isDM: Bool
    public var userID: String
    public var userName: String
    public var teamID: String
    public var text: String
    public var ts: String
    public var permalink: String
    public var reasons: [CommsReason]
    /// The keyword that matched, when `reasons` holds `.keyword`.
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

    /// The sender as a person reads them: the handle, or the user id when Slack gave none.
    public var sender: String { userName.isEmpty ? userID : userName }

    /// Where it was said: `DM`, or `#channel`.
    public var place: String { isDM ? "DM" : "#\(channelName)" }
}

/// What the triage may answer. The raw values are the classifier schema's enum.
public enum CommsAction: String, Codable, CaseIterable, Sendable {
    case none, bump, review
    case fixIssue = "fix_issue"
    case investigate

    /// Whether the action is about one PR or issue, and so is meaningless without its number.
    public var needsNumber: Bool {
        switch self {
        case .bump, .review, .fixIssue: return true
        case .none, .investigate:       return false
        }
    }
}

/// The triage's answer, valid by construction: every initializer — the memberwise one,
/// JSON decoding, `parse` — routes through the same check, so a decision that names a
/// PR action without a positive number cannot exist. It reads `none` instead, with the
/// reason recorded. Whether the message holds that number is `checked(against:)`;
/// whether it names an OPEN PR or issue of the repo is the front-end's check, made
/// against GitHub before it acts.
public struct CommsDecision: Codable, Equatable, Sendable {
    public let action: CommsAction
    /// The PR or issue number for `review` / `fix_issue` / `bump`; 0 otherwise.
    public let number: Int
    public let summary: String
    public let reason: String

    /// How much of the classifier's prose is kept. It is shown on one row.
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

    /// A `none` that says why nothing was decided.
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

    /// Decoded field by field, so one bad field costs the decision its action rather than
    /// failing the whole object: an action outside the enum, or a number that is missing,
    /// fractional, a string or a boolean, all land in `init` as something it refuses.
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

    /// The decision out of what `claude -p --output-format json` printed. That is a JSON
    /// array of events whose `type: "result"` element carries the answer, or (older CLIs)
    /// that one result object alone. `structured_output` is preferred — it is the object
    /// the schema constrained — and `result`, the same answer as text, parsed when it is
    /// absent. Never throws: output with no answer in it is a `none` saying so.
    public static func parse(classifierOutput: Data) -> CommsDecision {
        answer(in: classifierOutput) ?? .none("the classifier's output held no answer")
    }

    /// `parse`, with nil for output that holds no answer at all — which `CommsClassifier`
    /// tells apart from a bad answer, since only the first is the classifier failing.
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

    /// `text` without a Markdown code fence around it, which a model answering in prose
    /// sometimes adds.
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

/// What a front-end passes `CommsWatch.tick`: the operator's Comms settings plus the
/// repo the triage is about.
public struct CommsSettings: Codable, Equatable, Sendable {
    public var keywords: [String]
    /// Slack user ids or handles; empty admits any member of the operator's workspace.
    public var senderAllowlist: [String]
    public var model: String
    public var maxPerTick: Int
    public var owner: String
    public var repo: String
    /// The operator's GitHub login, which the classifier is told the message was sent to.
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

    /// The Settings field's keywords: comma- or newline-separated, so one keyword may be
    /// a phrase. Blank entries and repeats drop out.
    public static func keywordList(_ raw: String) -> [String] {
        unique(raw.split(whereSeparator: { $0 == "," || $0.isNewline }))
    }

    /// The Settings field's sender allowlist: comma- or whitespace-separated, a leading
    /// `@` dropped.
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

    /// Every field optional. The two lists may come as the raw Settings strings
    /// (`keywordsRaw`, `senderAllowlistRaw`) so a front-end hands over what the operator
    /// typed and the splitting stays here.
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

/// The operator's three per-kind switches: asked-for reviews and issue fixes, bumps,
/// and investigations. Off turns that kind into a ping alone.
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

    /// A missing switch reads as its Settings default, on.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(requests: try c.decodeIfPresent(Bool.self, forKey: .requests) ?? true,
                  bumps: try c.decodeIfPresent(Bool.self, forKey: .bumps) ?? true,
                  investigations: try c.decodeIfPresent(Bool.self, forKey: .investigations) ?? true)
    }
}

/// What a front-end does about one triaged ping. Everything it does still goes through
/// its ordinary queue — the ban list, the task cap and the budget gate all apply.
///
/// JSON: `{"kind": "none"}`, `{"kind": "review" | "fix_issue" | "bump", "number": N}`,
/// `{"kind": "investigate", "config": InvestigateConfig}`.
public enum CommsEffect: Equatable, Sendable {
    case none
    /// Move whatever is already queued for #N to the front of its band; start nothing.
    case bump(Int)
    /// Queue a requested review of open PR N, at the front of its band.
    case review(Int)
    /// Queue a requested fix of open issue N, at the front of its band.
    case fixIssue(Int)
    /// Queue an investigation agent.
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

/// The one mapping from a triage answer to what a front-end does about it.
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

/// Slack timestamps as numbers. A `ts` is `<seconds>.<microseconds>` — six fraction
/// digits — and unique per channel, so its digits are a number that orders messages
/// and names an investigation in the queue's numbering space.
public enum CommsTS {
    /// `"1508284197.000015"` → `1508284197000015`; nil for anything that is not two runs
    /// of digits around one dot. A fraction shorter than six digits is read as
    /// microseconds padded on the right, a longer one cut to six.
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

    /// The `after:` date for a search that must reach back to `watermark`. `after:` is
    /// day-granular and exclusive, and Slack may read its date in the operator's own
    /// timezone, so the date is the day before the watermark's day at UTC-12, the
    /// westernmost offset: under any offset, the local midnight that ends that date is
    /// at or before the watermark.
    public static func afterDay(_ watermark: Int) -> String {
        let day = (watermark / 1_000_000 - 12 * 3_600) / 86_400 - 1
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: Date(timeIntervalSince1970: TimeInterval(day * 86_400)))
    }
}

/// One investigation: the config a queued `investigate` ask carries, and the prompt
/// its agent is given (`assets/comms.json` → `investigate`).
///
/// JSON carries `"kind": "investigate"`, so the same object is a `build-prompt` payload
/// and the Linux queue's `RequestedWork.config` as it stands.
public struct InvestigateConfig: Codable, Equatable, Sendable {
    /// The message's ts as a number (`CommsTS.number`): the ask's number in the queue.
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

    /// The investigation a triaged ping asks for; its report lands in `reportDir`
    /// as `<number>.md`.
    public init(message: CommsMessage, decision: CommsDecision, settings: CommsSettings,
                reportDir: URL) {
        let number = CommsTS.number(message.ts) ?? 0
        self.init(number: number, messageID: message.id, sender: message.sender,
                  channel: message.place, permalink: message.permalink, text: message.text,
                  summary: decision.summary,
                  reportPath: reportDir.appendingPathComponent("\(number).md").path,
                  owner: settings.owner, repo: settings.repo)
    }

    /// `kind` and `label` are written for the reader's sake and never read back: both
    /// follow from the rest.
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

    /// The queue row and activity-feed label: `Investigate · Slack · <sender> · <summary>`,
    /// the summary cut to `labelSummaryMax` characters.
    public var label: String {
        let cap = (try? CoreAssets.comms())?.labelSummaryMax ?? 48
        let s = summary.trimmingCharacters(in: .whitespacesAndNewlines)
        let clipped = s.count > cap ? String(s.prefix(cap - 1)) + "…" : s
        return "Investigate · Slack · \(sender) · \(clipped)"
    }

    public func buildPrompt() -> String {
        guard let b = (try? CoreAssets.comms())?.investigate else { return "" }
        let values = [
            "owner": owner, "repo": repo, "permalink": permalink, "reportPath": reportPath,
            "sender": CommsText.defang(sender), "channel": CommsText.defang(channel),
            "summary": CommsText.defang(summary), "text": CommsText.defang(text),
        ]
        return [b.intro, b.message, b.work, b.rules, b.report]
            .map { CommsText.fill($0, values) }
            .joined(separator: "\n\n")
    }
}

/// Text handling shared by both prompts that quote a Slack message.
public enum CommsText {
    /// The words both prompts fence a message between (`BEGIN …` / `END …`).
    public static let fenceMarker = "UNTRUSTED SLACK MESSAGE"

    /// `text` with every copy of the fence marker replaced, so a message cannot close
    /// its own fence early and have what follows read as the prompt's own words. A copy
    /// is the marker's letters in order with anything but letters and digits between
    /// them (whitespace of any kind, punctuation, zero-width or combining characters),
    /// in any case, and in fullwidth, accented or Cyrillic/Greek look-alike letters.
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

    /// `template` with each `{name}` replaced by `values[name]`, in one pass: a value
    /// is never scanned again, so a message that contains `{reportPath}` stays as typed
    /// instead of being filled in. Unknown names are left as they are.
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
