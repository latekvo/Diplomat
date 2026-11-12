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
    /// The first word of the profile's real name, else its display name: how a reply
    /// names the operator. Nil for an identity recorded before it was read.
    public var name: String?

    public init(userID: String, teamID: String, handle: String, name: String? = nil) {
        self.userID = userID
        self.teamID = teamID
        self.handle = handle
        self.name = name
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
    /// From the sender's profile: what a reply's answer is written for (`CommsAnswerer`)
    /// and the zone its times are given in. Nil when not read.
    public var senderTitle: String?
    public var senderTimezone: String?

    public init(id: String, channelID: String, channelName: String, isDM: Bool,
                userID: String, userName: String, teamID: String, text: String, ts: String,
                permalink: String, reasons: [CommsReason], keyword: String? = nil,
                senderTitle: String? = nil, senderTimezone: String? = nil) {
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
        self.senderTitle = senderTitle
        self.senderTimezone = senderTimezone
    }

    public var sender: String { userName.isEmpty ? userID : userName }

    public var place: String { isDM ? "DM" : "#\(channelName)" }

    /// A message asking for several PRs or issues is one row per target, so each has its
    /// own queue key, start and finish: the first keeps `id`, the rest are `<id>#<number>`
    /// (`<id>#<owner/repo>#<number>` outside the primary repository).
    public static let targetSeparator: Character = "#"

    /// The Slack message's own id, whichever of its targets' rows this is.
    public var messageID: String { Self.messageID(of: id) }

    public static func messageID(of id: String) -> String {
        String(id.prefix { $0 != targetSeparator })
    }

    /// A row for a target past the first: the same message again, not another one.
    public var isExtraTarget: Bool { id.contains(Self.targetSeparator) }
}

/// The raw values are the classifier schema's enum.
public enum CommsAction: String, Codable, CaseIterable, Sendable {
    case none, bump, review
    case fixIssue = "fix_issue"
    case investigate
    /// A question the project's documentation could answer: answered in Slack while the
    /// operator is away (`CommsReplier`), a ping otherwise.
    case question
    /// A hijack attempt: the sender is banned (`CommsBan`).
    case injection

    public var needsNumber: Bool {
        switch self {
        case .bump, .review, .fixIssue:                     return true
        case .none, .investigate, .question, .injection:    return false
        }
    }
}

/// One PR or issue a message asks about, and what it asks for it: `review`, `fixIssue` or
/// `bump`.
public struct CommsAsk: Codable, Equatable, Sendable {
    public let action: CommsAction
    public let number: Int
    /// Its repository as `CommsSettings.canonical` writes it; "" (the primary) is not encoded.
    public let repo: String

    public init(_ action: CommsAction, _ number: Int, repo: String = "") {
        self.action = action
        self.number = number
        self.repo = repo
    }

    public var ref: String { CommsSettings.ref(number, repo: repo) }

    private enum CodingKeys: String, CodingKey { case action, number, repo }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(try c.decode(CommsAction.self, forKey: .action), try c.decode(Int.self, forKey: .number),
                  repo: try c.decodeIfPresent(String.self, forKey: .repo) ?? "")
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(action, forKey: .action)
        try c.encode(number, forKey: .number)
        if !repo.isEmpty { try c.encode(repo, forKey: .repo) }
    }
}

/// The triage's answer, valid by construction: every initializer (memberwise, decoding,
/// `parse`) routes through one check, so a PR action without a positive number reads
/// `none`, with the reason recorded, and so does an `injection` without evidence. Whether
/// the message holds those numbers or that evidence is `checked(against:)`; whether one is
/// an OPEN PR or issue is the front-end's check against GitHub before it acts.
public struct CommsDecision: Codable, Equatable, Sendable {
    /// For work on PRs or issues, the first target's action.
    public let action: CommsAction
    /// Every PR or issue it names, each with its own action, no number twice; empty unless
    /// `action.needsNumber`. More than one lasts only until `CommsTickResult.Item.targets`
    /// splits it.
    public let targets: [CommsAsk]
    public let summary: String
    public let reason: String
    /// The injected words, quoted from the message; "" unless `action` is `injection`.
    public let evidence: String
    /// What the message is written in: a key of `replies.languages`, or anything else for
    /// English. Only ever looks up fixed words, so a message steering it steers nothing.
    public let language: String
    /// For an `investigate` or a `question`, the message's repository as in `CommsAsk.repo`;
    /// "" otherwise.
    public let repo: String

    /// The first target's number; 0 for none.
    public var number: Int { targets.first?.number ?? 0 }
    public var numbers: [Int] { targets.map(\.number) }

    public static let defaultLanguage = "en"

    /// Classifier prose is clipped to this; it is shown on one row.
    static let maxText = 240

    /// `targets` wins; without it, `numbers` (else `number`) are targets of `action`. A
    /// target whose number is not positive, or whose action needs none, is dropped, as is one
    /// whose repository and number an earlier target has.
    public init(action: CommsAction, number: Int = 0, numbers: [Int] = [],
                targets: [CommsAsk] = [], summary: String,
                reason: String, evidence: String = "", language: String = defaultLanguage,
                repo: String = "") {
        let reason = Self.clip(reason)
        let evidence = action == .injection ? Self.unquoted(evidence) : ""
        let asked = !targets.isEmpty ? targets
            : (numbers.isEmpty ? [number] : numbers).map { CommsAsk(action, $0) }
        var kept: [CommsAsk] = []
        for t in asked where t.number > 0 && t.action.needsNumber
            && !kept.contains(where: { $0.number == t.number && $0.repo == t.repo }) {
            kept.append(t)
        }
        self.summary = Self.clip(summary)
        self.language = language
        self.repo = [.investigate, .question].contains(action) ? repo : ""
        if action.needsNumber && kept.isEmpty {
            self.action = .none
            self.targets = []
            self.reason = "answered \(action.rawValue) without a positive PR or issue number"
                + " (\(Self.describe(asked, tagged: asked.contains { $0.action != action }))); \(reason)"
            self.evidence = ""
        } else if action == .injection && evidence.isEmpty {
            self.action = .none
            self.targets = []
            self.reason = "answered injection without quoting any; \(reason)"
            self.evidence = ""
        } else {
            self.targets = action.needsNumber ? kept : []
            self.action = self.targets.first?.action ?? action
            self.reason = reason
            self.evidence = evidence
        }
    }

    public static func none(_ reason: String) -> CommsDecision {
        CommsDecision(action: .none, number: 0, summary: "", reason: reason)
    }

    /// This decision without any target `message` and `earlier` do not back: one whose
    /// number neither contains, as `#N`, `/pull/N`, `/issues/N` or `N`, with no letter or
    /// digit either side, outside a reference to another repository than the target's
    /// (`ownRepoText`), one in a repository `settings` does not watch, and one outside
    /// the primary repository whose name neither contains. `none` when that leaves no
    /// number, or when it is an `injection` whose evidence `message` itself does not
    /// contain, ignoring case and runs of whitespace, as sent or as the classifier read it
    /// (`CommsText.defang`). Repositories come back as `CommsSettings.canonical` writes
    /// them. `CommsWatch.tick` applies it to every answer, passing the messages before
    /// this one in its conversation (`CommsBatch.earlier`), so a guessed number never acts
    /// and nobody is banned for words they did not write.
    public func checked(against message: CommsMessage, earlier: [CommsMessage] = [],
                        settings: CommsSettings = CommsSettings()) -> CommsDecision {
        let needle = Self.squashed(evidence)
        if action == .injection, ![message.text, CommsText.defang(message.text)].contains(where: {
            Self.squashed($0).localizedCaseInsensitiveContains(needle) }) {
            return CommsDecision(action: .none, number: 0, summary: summary,
                                 reason: "answered injection quoting words the message does "
                                     + "not contain; \(reason)")
        }
        guard action.needsNumber else {
            let about = settings.canonical(repo) ?? ""
            return about == repo ? self
                : CommsDecision(action: action, summary: summary, reason: reason,
                                evidence: evidence, language: language, repo: about)
        }
        let sent = ([message] + earlier).map(\.text)
        var texts: [String: [String]] = [:]
        var kept: [CommsAsk] = [], dropped: [CommsAsk] = []
        for t in targets {
            guard let repo = settings.canonical(t.repo) else {
                dropped.append(t)
                continue
            }
            let ask = CommsAsk(t.action, t.number, repo: repo)
            if texts[repo] == nil {
                texts[repo] = sent.map { Self.ownRepoText($0, settings: settings, repo: repo) }
            }
            let named = repo.isEmpty || sent.contains {
                CommsMatch.containsWord($0, String(repo.split(separator: "/").last ?? ""))
            }
            if kept.contains(where: { $0.number == ask.number && $0.repo == ask.repo }) { continue }
            if named, texts[repo, default: []].contains(where: {
                CommsMatch.containsWord($0, String(t.number)) }) {
                kept.append(ask)
            } else {
                dropped.append(t)
            }
        }
        guard kept != targets else { return self }
        guard !dropped.isEmpty else {
            return CommsDecision(action: action, targets: kept, summary: summary, reason: reason,
                                 language: language)
        }
        let noun = dropped.count == 1 ? "a number" : "numbers"
        let place = settings.repos.count == 1 ? "in this repository" : "in that watched repository"
        let why = "answered \(Self.describe(dropped, tagged: true)), \(noun) neither the message "
            + "nor its conversation names \(place); \(reason)"
        return CommsDecision(action: kept.isEmpty ? .none : action, targets: kept,
                             summary: summary, reason: why, language: language)
    }

    func targeting(_ t: CommsAsk) -> CommsDecision {
        CommsDecision(action: t.action, targets: [t], summary: summary, reason: reason,
                      evidence: evidence, language: language)
    }

    /// Tagged, `review #1, #2, fix_issue argent#3`: an action before the first of each run
    /// of it. Untagged, the bare numbers: `1, 2, 3`.
    private static func describe(_ targets: [CommsAsk], tagged: Bool = false) -> String {
        guard tagged else { return targets.map { String($0.number) }.joined(separator: ", ") }
        return targets.enumerated().map { i, t in
            let named = i == 0 || targets[i - 1].action != t.action
            return (named ? t.action.rawValue + " " : "") + t.ref
        }.joined(separator: ", ")
    }

    /// `text` with its references into repositories other than `repo` (a
    /// `CommsSettings.canonical` slug, the primary by default) taken out: GitHub links with
    /// their Slack or markdown label, `owner/repo#N` for another repository of `repo`'s
    /// owner or another watched one, and `name#N` for a repository a link names or another
    /// watched one. A number there is not one of `repo`'s, even when `repo` has a PR that
    /// happens to wear it. Any other `word#N` or `word/word#N` stays, for the classifier to
    /// judge: `PR#12`, `fix/login#12` and `swmansion/argent#12` cannot be told from a
    /// foreign reference here.
    public static func ownRepoText(_ text: String, settings: CommsSettings,
                                   repo target: String = "") -> String {
        let (home, name) = settings.coordinates(of: target)
        let slug = "\(home)/\(name)".lowercased()
        let url = #"(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/(?:pull|issues|discussions|commit)/\w+"#
        guard let links = try? NSRegularExpression(
            pattern: #"<\#(url)[^\s>|]*(?:\|[^>\n]*)?>"#
                + #"|\[[^\[\]\n]*\]\(\#(url)[^\s)]*\)"#
                + #"|\#(url)(?:/[\w./-]*)?(?:#[A-Za-z][\w-]*)?"#
                + #"|(?<![\w./-])([A-Za-z0-9][A-Za-z0-9-]*)/([\w.-]+)#\d+"#,
            options: [.caseInsensitive]) else { return text }
        let source = text as NSString
        let out = NSMutableString(string: text)
        var others = Set(settings.repos.compactMap { s -> String? in
            let n = String(s.split(separator: "/").last ?? "")
            return n == name.lowercased() ? nil : n
        })
        // Backwards, so each match's range still holds in `out`.
        for m in links.matches(in: text, range: NSRange(location: 0, length: source.length)).reversed() {
            let group = stride(from: 1, to: 9, by: 2).first { m.range(at: $0).location != NSNotFound } ?? 1
            let owner = source.substring(with: m.range(at: group))
            let repo = source.substring(with: m.range(at: group + 1))
            let named = "\(owner)/\(repo)".lowercased()
            let sameRepo = repo.lowercased() == name.lowercased()
            let isLink = group < 7
            let foreign = named != slug && (isLink || owner.lowercased() == home.lowercased()
                                            || settings.repos.contains(named))
            guard foreign else { continue }
            out.replaceCharacters(in: m.range, with: "")
            if isLink && !sameRepo { others.insert(repo) }
        }
        var kept = out as String
        for name in others {
            let pattern = "(?<![\\w.-])" + NSRegularExpression.escapedPattern(for: name) + "#\\d+"
            guard let re = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive])
            else { continue }
            kept = re.stringByReplacingMatches(in: kept, range: NSRange(kept.startIndex..., in: kept),
                                               withTemplate: "")
        }
        return kept
    }

    /// Evidence copied from the classifier's stdin carries the fence's `|` on every line;
    /// cut to `maxText` without an ellipsis, so a long quote is still the message's words.
    private static func unquoted(_ s: String) -> String {
        let lines = s.split(omittingEmptySubsequences: false, whereSeparator: \.isNewline).map {
            line -> Substring in
            let t = line.drop { $0 == " " || $0 == "\t" }
            guard t.hasPrefix(CommsText.quoteMarker) else { return line }
            let rest = t.dropFirst(CommsText.quoteMarker.count)
            return rest.hasPrefix(" ") ? rest.dropFirst() : rest
        }
        let t = lines.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
        return String(t.prefix(maxText)).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func squashed(_ s: String) -> String {
        s.split(whereSeparator: \.isWhitespace).joined(separator: " ")
    }

    private enum CodingKeys: String, CodingKey {
        case action, number, numbers, targets, summary, reason, evidence, language, repo
    }

    /// A target as written, each field kept only when it has the right type.
    private struct RawTarget: Decodable {
        let action: String?
        let number: Int?
        let repo: String?

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            action = try? c.decode(String.self, forKey: .action)
            number = try? c.decode(Int.self, forKey: .number)
            repo = try? c.decode(String.self, forKey: .repo)
        }
    }

    /// Field by field, so one bad field costs the decision its action instead of failing
    /// the object: an unknown action, or a number that is missing, fractional, a string or
    /// a boolean, lands in `init` as something it refuses. A target without an action
    /// takes the decision's. An older log line has `numbers`, or `number` alone, and no
    /// `repo`: the primary repository's.
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
        let targets = ((try? c.decode([RawTarget].self, forKey: .targets)) ?? []).map {
            CommsAsk($0.action.map { CommsAction(rawValue: $0) ?? .none } ?? action, $0.number ?? 0,
                     repo: $0.repo ?? "")
        }
        self.init(action: action, number: (try? c.decode(Int.self, forKey: .number)) ?? 0,
                  numbers: (try? c.decode([Int].self, forKey: .numbers)) ?? [],
                  targets: targets, summary: summary, reason: reason,
                  evidence: (try? c.decode(String.self, forKey: .evidence)) ?? "",
                  language: (try? c.decode(String.self, forKey: .language)) ?? Self.defaultLanguage,
                  repo: (try? c.decode(String.self, forKey: .repo)) ?? "")
    }

    /// `number` beside `targets`: the Linux ping row reads it.
    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(action, forKey: .action)
        try c.encode(number, forKey: .number)
        try c.encode(targets, forKey: .targets)
        try c.encode(summary, forKey: .summary)
        try c.encode(reason, forKey: .reason)
        try c.encode(evidence, forKey: .evidence)
        try c.encode(language, forKey: .language)
        if !repo.isEmpty { try c.encode(repo, forKey: .repo) }
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
        switch answerObject(in: output, key: "action") {
        case nil:                  return nil
        case .failed(let d):       return d
        case .object(let o):       return decode(o)
        }
    }

    /// A batch's answers, `count` of them, the nth answering message n + 1: the first
    /// entry of `decisions` naming it, or a `none` saying it went unanswered. Nil, as for
    /// `answer(in:)`, for output with no answer at all.
    static func answers(in output: Data, count: Int) -> [CommsDecision]? {
        let entries: [Any]
        switch answerObject(in: output, key: "decisions") {
        case nil:
            return nil
        case .failed(let d):
            return Array(repeating: d, count: count)
        case .object(let o):
            guard let list = o?["decisions"] as? [Any] else {
                return Array(repeating: .none("the classifier's answer held no decisions list"),
                             count: count)
            }
            entries = list
        }
        var out = [CommsDecision?](repeating: nil, count: count)
        for case let entry as [String: Any] in entries {
            guard let data = try? JSONSerialization.data(withJSONObject: entry),
                  let n = (try? JSONDecoder().decode(Slot.self, from: data))?.message,
                  n >= 1, n <= count, out[n - 1] == nil else { continue }
            out[n - 1] = decode(entry)
        }
        return out.map { $0 ?? .none("the classifier gave this message no answer") }
    }

    private struct Slot: Decodable { let message: Int }

    enum AnswerObject {
        /// Nil for a `result` text that is not a JSON object.
        case object([String: Any]?)
        case failed(CommsDecision)
    }

    /// The answer object in `claude`'s output; a bare object counts when it has `key`, the
    /// shape a stub classifier prints.
    static func answerObject(in output: Data, key: String) -> AnswerObject? {
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
            return .failed(.none(CommsClassifier.failurePrefix
                                 + "claude reported an error: \(clip(text))"))
        }
        if let structured = result["structured_output"] as? [String: Any] {
            return .object(structured)
        }
        if let text = result["result"] as? String {
            let parsed = try? JSONSerialization.jsonObject(with: Data(unfenced(text).utf8))
            return .object(parsed as? [String: Any])
        }
        if result[key] != nil { return .object(result) }
        return nil
    }

    private static func decode(_ object: [String: Any]?) -> CommsDecision {
        guard let object, let data = try? JSONSerialization.data(withJSONObject: object) else {
            return .none("the classifier's answer was not a JSON object")
        }
        return decode(data)
    }

    private static func decode(_ data: Data) -> CommsDecision {
        guard let d = try? JSONDecoder().decode(CommsDecision.self, from: data) else {
            return .none("the classifier's answer was not a JSON object")
        }
        // The marker is `CommsClassifier`'s alone: a reason the model wrote cannot claim it.
        guard d.reason.hasPrefix(CommsClassifier.failurePrefix) else { return d }
        return CommsDecision(action: d.action, targets: d.targets, summary: d.summary,
                             reason: "triage: " + d.reason, evidence: d.evidence,
                             language: d.language, repo: d.repo)
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
    /// Slack user ids on the ban list (`CommsBan`): their messages are never triaged.
    public var bannedSenders: [String]
    public var model: String
    /// How many new messages one tick takes.
    public var maxPerTick: Int
    /// How many messages one triage call reads.
    public var batchSize: Int
    /// A queue shorter than `batchSize` is triaged once its newest message is this old.
    public var flushAfterSecs: Int
    /// The watched repositories (the front-end's repo roots) as lowercased `owner/repo`
    /// slugs, the primary first; never empty. A bare number is the primary's.
    public private(set) var repos: [String]
    /// The operator's GitHub login; the classifier is told the message was sent to it.
    public var login: String

    /// `repos` without the blank, malformed and repeated; with none left, the one repo
    /// `owner`/`repo` name, else `assets/config.json`'s.
    public init(keywords: [String] = [], senderAllowlist: [String] = [],
                bannedSenders: [String] = [],
                model: String? = nil, maxPerTick: Int? = nil, batchSize: Int? = nil,
                flushAfterSecs: Int? = nil, owner: String? = nil, repo: String? = nil,
                repos: [String] = [], login: String = "") {
        let assets = try? CoreAssets.comms()
        let coordinates = CoreAssets.repoCoordinates()
        self.keywords = keywords
        self.senderAllowlist = senderAllowlist
        self.bannedSenders = bannedSenders
        let m = (model ?? "").trimmingCharacters(in: .whitespaces)
        self.model = m.isEmpty ? (assets?.model ?? "haiku") : m
        self.maxPerTick = max(1, maxPerTick ?? assets?.maxPerTick ?? 8)
        self.batchSize = max(1, batchSize ?? assets?.batchSize ?? 8)
        self.flushAfterSecs = max(0, flushAfterSecs ?? assets?.flushAfterSecs ?? 1800)
        var slugs: [String] = []
        for r in repos {
            let s = r.trimmingCharacters(in: .whitespaces).lowercased()
            let halves = s.split(separator: "/", omittingEmptySubsequences: false)
            if halves.count == 2, !halves[0].isEmpty, !halves[1].isEmpty, !slugs.contains(s) {
                slugs.append(s)
            }
        }
        self.repos = slugs.isEmpty
            ? ["\(owner ?? coordinates.owner)/\(repo ?? coordinates.repo)".lowercased()] : slugs
        self.login = login
    }

    public var owner: String { coordinates(of: "").owner }
    public var repo: String { coordinates(of: "").repo }

    /// A watched repository's coordinates; "" names the primary.
    public func coordinates(of slug: String) -> (owner: String, repo: String) {
        let halves = (slug.isEmpty ? repos[0] : slug).split(separator: "/")
        return (String(halves.first ?? ""), String(halves.last ?? ""))
    }

    /// How a decision writes `slug` (any case, "" for the primary): "" for the primary,
    /// its lowercased self for another watched repository, nil for one not watched.
    public func canonical(_ slug: String) -> String? {
        let s = slug.trimmingCharacters(in: .whitespaces).lowercased()
        if s.isEmpty || s == repos[0] { return "" }
        return repos.contains(s) ? s : nil
    }

    /// `#12` in the primary repository (`repo` ""), `argent#12` in another.
    public static func ref(_ number: Int, repo: String) -> String {
        repo.isEmpty ? "#\(number)" : "\(repo.split(separator: "/").last ?? "")#\(number)"
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
        case keywords, senderAllowlist, bannedSenders, model, maxPerTick, batchSize,
             flushAfterSecs, owner, repo, repos, login
        case keywordsRaw, senderAllowlistRaw
    }

    /// Every field optional. The lists may come as the raw Settings strings (`keywordsRaw`,
    /// `senderAllowlistRaw`), so the splitting stays here rather than in each front-end.
    /// Without `repos`, `owner` and `repo` name the one watched repository.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let keywords = try c.decodeIfPresent([String].self, forKey: .keywords)
            ?? Self.keywordList(try c.decodeIfPresent(String.self, forKey: .keywordsRaw) ?? "")
        let allow = try c.decodeIfPresent([String].self, forKey: .senderAllowlist)
            ?? Self.allowlist(try c.decodeIfPresent(String.self, forKey: .senderAllowlistRaw) ?? "")
        self.init(keywords: keywords, senderAllowlist: allow,
                  bannedSenders: try c.decodeIfPresent([String].self, forKey: .bannedSenders) ?? [],
                  model: try c.decodeIfPresent(String.self, forKey: .model),
                  maxPerTick: try c.decodeIfPresent(Int.self, forKey: .maxPerTick),
                  batchSize: try c.decodeIfPresent(Int.self, forKey: .batchSize),
                  flushAfterSecs: try c.decodeIfPresent(Int.self, forKey: .flushAfterSecs),
                  owner: try c.decodeIfPresent(String.self, forKey: .owner),
                  repo: try c.decodeIfPresent(String.self, forKey: .repo),
                  repos: try c.decodeIfPresent([String].self, forKey: .repos) ?? [],
                  login: try c.decodeIfPresent(String.self, forKey: .login) ?? "")
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(keywords, forKey: .keywords)
        try c.encode(senderAllowlist, forKey: .senderAllowlist)
        try c.encode(bannedSenders, forKey: .bannedSenders)
        try c.encode(model, forKey: .model)
        try c.encode(maxPerTick, forKey: .maxPerTick)
        try c.encode(batchSize, forKey: .batchSize)
        try c.encode(flushAfterSecs, forKey: .flushAfterSecs)
        try c.encode(repos, forKey: .repos)
        try c.encode(login, forKey: .login)
    }
}

/// Per-kind switches (`requests` covers reviews and issue fixes); off turns that kind
/// into a ping alone. `replies` is whether Comms writes back in Slack (`CommsReplier`) at
/// all, and the `replyTo…` switches about what: its answers to questions, and only those,
/// until the operator says otherwise.
public struct CommsToggles: Codable, Equatable, Sendable {
    public var requests: Bool
    public var bumps: Bool
    public var investigations: Bool
    public var replies: Bool
    public var replyToReviews: Bool
    public var replyToFixes: Bool
    public var replyToInvestigations: Bool
    public var replyToQuestions: Bool

    public init(requests: Bool = true, bumps: Bool = true, investigations: Bool = true,
                replies: Bool = true, replyToReviews: Bool = false, replyToFixes: Bool = false,
                replyToInvestigations: Bool = false, replyToQuestions: Bool = true) {
        self.requests = requests
        self.bumps = bumps
        self.investigations = investigations
        self.replies = replies
        self.replyToReviews = replyToReviews
        self.replyToFixes = replyToFixes
        self.replyToInvestigations = replyToInvestigations
        self.replyToQuestions = replyToQuestions
    }

    public func repliesTo(_ action: CommsAction) -> Bool {
        switch action {
        case .review:                        return replyToReviews
        case .fixIssue:                      return replyToFixes
        case .investigate:                   return replyToInvestigations
        case .question:                      return replyToQuestions
        case .none, .bump, .injection:       return false
        }
    }

    private enum CodingKeys: String, CodingKey {
        case requests, bumps, investigations, replies, replyToReviews, replyToFixes,
             replyToInvestigations, replyToQuestions
    }

    /// A missing switch reads as its default.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = CommsToggles()
        func read(_ key: CodingKeys, _ fallback: Bool) throws -> Bool {
            try c.decodeIfPresent(Bool.self, forKey: key) ?? fallback
        }
        self.init(requests: try read(.requests, d.requests),
                  bumps: try read(.bumps, d.bumps),
                  investigations: try read(.investigations, d.investigations),
                  replies: try read(.replies, d.replies),
                  replyToReviews: try read(.replyToReviews, d.replyToReviews),
                  replyToFixes: try read(.replyToFixes, d.replyToFixes),
                  replyToInvestigations: try read(.replyToInvestigations, d.replyToInvestigations),
                  replyToQuestions: try read(.replyToQuestions, d.replyToQuestions))
    }
}

/// A Settings slider that stops only at `choices`: its position is an index into them.
public enum CommsSliderStops {
    /// The stop closest to `value` on a log scale, for a value written by hand; 0 for no
    /// stops.
    public static func index(of value: Int, in choices: [Int]) -> Int {
        let v = log(Double(max(1, value)))
        return choices.indices.min {
            abs(log(Double(choices[$0])) - v) < abs(log(Double(choices[$1])) - v)
        } ?? 0
    }
}

/// What a front-end does about one triaged ping, through its ordinary queue: the ban
/// list, the task cap and the budget gate all apply.
///
/// JSON: `{"kind": "none"}`, `{"kind": "review" | "fix_issue" | "bump", "number": N,
/// "repo"?: slug}` (`repo` as `CommsAsk.repo` writes it, absent for the primary
/// repository), `{"kind": "investigate", "config": InvestigateConfig}`,
/// `{"kind": "ban", "ban": CommsBan}`.
public enum CommsEffect: Equatable, Sendable {
    case none
    /// Move whatever is already queued for #N to the front of its band; start nothing.
    case bump(Int, repo: String = "")
    /// `review` and `fixIssue` queue at the front of their band.
    case review(Int, repo: String = "")
    case fixIssue(Int, repo: String = "")
    case investigate(InvestigateConfig)
    /// Put the sender on the prompt-injection ban list.
    case ban(CommsBan)
}

extension CommsEffect: Codable {
    private enum CodingKeys: String, CodingKey { case kind, number, repo, config, ban }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let kind = try c.decode(String.self, forKey: .kind)
        func target() throws -> (Int, String) {
            (try c.decode(Int.self, forKey: .number),
             try c.decodeIfPresent(String.self, forKey: .repo) ?? "")
        }
        switch kind {
        case "none":        self = .none
        case "bump":        let t = try target(); self = .bump(t.0, repo: t.1)
        case "review":      let t = try target(); self = .review(t.0, repo: t.1)
        case "fix_issue":   let t = try target(); self = .fixIssue(t.0, repo: t.1)
        case "investigate": self = .investigate(try c.decode(InvestigateConfig.self, forKey: .config))
        case "ban":         self = .ban(try c.decode(CommsBan.self, forKey: .ban))
        default:
            throw DecodingError.dataCorruptedError(forKey: .kind, in: c,
                                                   debugDescription: "unknown effect \(kind)")
        }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        func target(_ kind: String, _ n: Int, _ repo: String) throws {
            try c.encode(kind, forKey: .kind)
            try c.encode(n, forKey: .number)
            if !repo.isEmpty { try c.encode(repo, forKey: .repo) }
        }
        switch self {
        case .none:
            try c.encode("none", forKey: .kind)
        case .bump(let n, let repo):     try target("bump", n, repo)
        case .review(let n, let repo):   try target("review", n, repo)
        case .fixIssue(let n, let repo): try target("fix_issue", n, repo)
        case .investigate(let cfg):
            try c.encode("investigate", forKey: .kind)
            try c.encode(cfg, forKey: .config)
        case .ban(let ban):
            try c.encode("ban", forKey: .kind)
            try c.encode(ban, forKey: .ban)
        }
    }
}

/// The one mapping from a triage answer to an effect. A ban has no switch: no review
/// injection site has one either.
public enum CommsPlan {
    public static func effect(for decision: CommsDecision, message: CommsMessage,
                              toggles: CommsToggles, settings: CommsSettings = CommsSettings(),
                              reportDir: URL) -> CommsEffect {
        let repo = decision.targets.first?.repo ?? ""
        switch decision.action {
        case .none, .question: return .none
        case .review:          return toggles.requests ? .review(decision.number, repo: repo) : .none
        case .fixIssue:        return toggles.requests ? .fixIssue(decision.number, repo: repo) : .none
        case .bump:            return toggles.bumps ? .bump(decision.number, repo: repo) : .none
        case .investigate:
            guard toggles.investigations else { return .none }
            return .investigate(InvestigateConfig(message: message, decision: decision,
                                                  settings: settings, reportDir: reportDir))
        case .injection:       return .ban(CommsBan(message: message, decision: decision))
        }
    }
}

/// A Slack sender's prompt-injection ban: one entry of the review ban list
/// (`~/.diplomat/pr-monitor/banned.json`, written by the device-allocator daemon), keyed by
/// `login(userID)`. The prefix keeps the two namespaces apart, since no GitHub login holds
/// a colon, so every existing ban check reads a Slack entry as a login it never sees. The
/// daemon and the Linux front-end spell the prefix themselves.
public struct CommsBan: Codable, Equatable, Sendable {
    public static let loginPrefix = "slack:"

    public static func login(_ userID: String) -> String { loginPrefix + userID }

    /// The Slack user id a ban-list login names, or nil for a GitHub one.
    public static func userID(fromLogin login: String) -> String? {
        login.hasPrefix(loginPrefix) ? String(login.dropFirst(loginPrefix.count)) : nil
    }

    /// The Slack senders on the ban list as it is now: `banned.json` in `$DA_BAN_DIR`,
    /// else `~/.diplomat/pr-monitor`, as `{"banned": [{"login": …}, …]}`.
    public static func bannedNow() -> Set<String> {
        let env = ProcessInfo.processInfo.environment["DA_BAN_DIR"] ?? ""
        let dir = env.isEmpty
            ? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".diplomat/pr-monitor")
            : URL(fileURLWithPath: (env as NSString).expandingTildeInPath)
        guard let data = try? Data(contentsOf: dir.appendingPathComponent("banned.json")),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let list = obj["banned"] as? [[String: Any]] else { return [] }
        // The daemon keeps a login as written and matches it ignoring case.
        return Set(list.compactMap { entry -> String? in
            guard let login = entry["login"] as? String,
                  login.lowercased().hasPrefix(loginPrefix) else { return nil }
            return String(login.dropFirst(loginPrefix.count)).uppercased()
        })
    }

    public var userID: String
    public var handle: String
    /// `#channel` or `DM`.
    public var place: String
    public var permalink: String
    public var evidence: String
    /// The classifier's one-sentence reason.
    public var reason: String

    public init(userID: String, handle: String, place: String, permalink: String,
                evidence: String, reason: String) {
        self.userID = userID
        self.handle = handle
        self.place = place
        self.permalink = permalink
        self.evidence = evidence
        self.reason = reason
    }

    public init(message: CommsMessage, decision: CommsDecision) {
        self.init(userID: message.userID, handle: message.sender, place: message.place,
                  permalink: message.permalink, evidence: decision.evidence,
                  reason: decision.reason)
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
    /// The sender's Slack user id: the ask's ban dimension, as `CommsBan.login(senderID)`.
    /// "" for an ask queued before it was recorded.
    public var senderID: String
    public var channel: String
    public var permalink: String
    public var text: String
    public var summary: String
    public var reportPath: String
    /// The decision's repository (`CommsDecision.repo`).
    public var owner: String
    public var repo: String
    /// That repository's checkout; nil for the primary repo root. Set by the front-end,
    /// which alone knows the paths.
    public var checkout: String?

    public static let kind = "investigate"

    public init(number: Int, messageID: String, sender: String, senderID: String = "",
                channel: String, permalink: String, text: String, summary: String,
                reportPath: String, owner: String, repo: String, checkout: String? = nil) {
        self.number = number
        self.messageID = messageID
        self.sender = sender
        self.senderID = senderID
        self.channel = channel
        self.permalink = permalink
        self.text = text
        self.summary = summary
        self.reportPath = reportPath
        self.owner = owner
        self.repo = repo
        self.checkout = checkout
    }

    public init(message: CommsMessage, decision: CommsDecision, settings: CommsSettings,
                reportDir: URL) {
        let number = CommsTS.number(message.ts) ?? 0
        // One no longer watched stays named, so the front-end refuses it as it does a review.
        let (owner, repo) = settings.coordinates(of: settings.canonical(decision.repo)
                                                     ?? decision.repo.lowercased())
        self.init(number: number, messageID: message.id, sender: message.sender,
                  senderID: message.userID, channel: message.place, permalink: message.permalink, text: message.text,
                  summary: decision.summary,
                  reportPath: reportDir.appendingPathComponent("\(number).md").path,
                  owner: owner, repo: repo)
    }

    /// `kind` and `label` are written for readers and never decoded: both follow from the rest.
    private enum CodingKeys: String, CodingKey {
        case kind, number, messageID, sender, senderID, channel, permalink, text, summary,
             reportPath, owner, repo, checkout, label
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let coordinates = CoreAssets.repoCoordinates()
        func s(_ k: CodingKeys) throws -> String { try c.decodeIfPresent(String.self, forKey: k) ?? "" }
        self.init(number: try c.decode(Int.self, forKey: .number),
                  messageID: try s(.messageID), sender: try s(.sender),
                  senderID: try s(.senderID), channel: try s(.channel),
                  permalink: try s(.permalink), text: try s(.text), summary: try s(.summary),
                  reportPath: try s(.reportPath),
                  owner: try c.decodeIfPresent(String.self, forKey: .owner) ?? coordinates.owner,
                  repo: try c.decodeIfPresent(String.self, forKey: .repo) ?? coordinates.repo,
                  checkout: try c.decodeIfPresent(String.self, forKey: .checkout))
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(Self.kind, forKey: .kind)
        try c.encode(number, forKey: .number)
        try c.encode(messageID, forKey: .messageID)
        try c.encode(sender, forKey: .sender)
        try c.encode(senderID, forKey: .senderID)
        try c.encode(channel, forKey: .channel)
        try c.encode(permalink, forKey: .permalink)
        try c.encode(text, forKey: .text)
        try c.encode(summary, forKey: .summary)
        try c.encode(reportPath, forKey: .reportPath)
        try c.encode(owner, forKey: .owner)
        try c.encode(repo, forKey: .repo)
        try c.encodeIfPresent(checkout, forKey: .checkout)
        try c.encode(label, forKey: .label)
    }

    /// The queue's ban dimension (`RequestedWork.author`): "" when no sender id was
    /// recorded.
    public var banLogin: String { senderID.isEmpty ? "" : CommsBan.login(senderID) }

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
            "senderID": CommsText.oneLine(senderID),
            "channel": CommsText.oneLine(CommsText.defang(channel)),
            "summary": CommsText.defang(summary), "text": CommsText.defang(text),
        ]
        // Without an id there is nobody the agent could name in a report.
        let injection = senderID.isEmpty ? [] : [b.injection]
        return ([b.intro, b.message, b.work] + injection + [b.rules, b.report])
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

    /// `a`, `a and b`, `a, b and c`, with `and` the language's word.
    public static func list(_ items: [String], and: String) -> String {
        items.count < 2 ? items.joined()
            : items.dropLast().joined(separator: ", ") + " \(and) " + (items.last ?? "")
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
