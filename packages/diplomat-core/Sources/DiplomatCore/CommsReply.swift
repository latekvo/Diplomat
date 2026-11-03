import Foundation

/// Work a ping put in the queue, or found there or already under way, as the front-end
/// that acted on it sees it: what an acknowledgement names and estimates.
public struct CommsQueuedWork: Codable, Equatable, Sendable {
    /// `review`, `fix_issue` or `investigate`: a key of `replies.what` and `runSecs`.
    public var kind: String
    /// The PR or issue; unused for an investigation.
    public var number: Int
    /// Tasks waiting ahead of it, and how many bays run them at once.
    public var ahead: Int
    public var bays: Int
    /// When its agent started, for work already under way.
    public var startedAt: Date?
    /// A measured run time for this kind; `replies.runSecs` when nil.
    public var runSecs: Double?

    public init(kind: String, number: Int, ahead: Int = 0, bays: Int = 1,
                startedAt: Date? = nil, runSecs: Double? = nil) {
        self.kind = kind
        self.number = number
        self.ahead = ahead
        self.bays = bays
        self.startedAt = startedAt
        self.runSecs = runSecs
    }
}

/// What a front-end hands `CommsReplier.reply` once it has acted on a triaged ping
/// (Linux: `comms-reply`'s stdin).
public struct CommsReplyRequest: Codable, Sendable {
    public var message: CommsMessage
    public var decision: CommsDecision
    /// Set only when the ping's work is queued or running.
    public var queued: CommsQueuedWork?
    /// The operator's session has been locked for `replies.awayAfterSecs` (`CommsAway`).
    public var away: Bool
    public var toggles: CommsToggles
    public var settings: CommsSettings

    public init(message: CommsMessage, decision: CommsDecision, queued: CommsQueuedWork? = nil,
                away: Bool, toggles: CommsToggles = CommsToggles(),
                settings: CommsSettings = CommsSettings()) {
        self.message = message
        self.decision = decision
        self.queued = queued
        self.away = away
        self.toggles = toggles
        self.settings = settings
    }

    private enum CodingKeys: String, CodingKey { case message, decision, queued, away, toggles, settings }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(message: try c.decode(CommsMessage.self, forKey: .message),
                  decision: try c.decode(CommsDecision.self, forKey: .decision),
                  queued: try c.decodeIfPresent(CommsQueuedWork.self, forKey: .queued),
                  away: try c.decodeIfPresent(Bool.self, forKey: .away) ?? false,
                  toggles: try c.decodeIfPresent(CommsToggles.self, forKey: .toggles) ?? CommsToggles(),
                  settings: try c.decodeIfPresent(CommsSettings.self, forKey: .settings)
                      ?? CommsSettings())
    }
}

/// `outcome` is the `reply` line logged last (`sent`, `failed: …`, `skipped: …`), nil
/// when no reply was due.
public struct CommsReplyResult: Codable, Equatable, Sendable {
    public var outcome: String?
    public var text: String?

    public init(outcome: String? = nil, text: String? = nil) {
        self.outcome = outcome
        self.text = text
    }

    private enum CodingKeys: String, CodingKey { case outcome, text }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(outcome, forKey: .outcome)
        try c.encode(text, forKey: .text)
    }
}

/// Whether the operator is away: their session locked for `replies.awayAfterSecs`. The
/// front-ends read the lock (macOS `CGSSessionScreenIsLocked` and the lock
/// notifications, Linux logind's `LockedHint`), never idle time or display sleep, which
/// a `caffeinate` holds off.
public enum CommsAway {
    /// `DIPLOMAT_COMMS_AWAY=1|0` forces the answer either way (tests, a dry run).
    public static var override: Bool? {
        switch ProcessInfo.processInfo.environment["DIPLOMAT_COMMS_AWAY"] {
        case "1"?: return true
        case "0"?: return false
        default:   return nil
        }
    }

    public static func isAway(lockedSince: Date?, now: Date = Date()) -> Bool {
        if let forced = override { return forced }
        guard let since = lockedSince,
              let secs = (try? CoreAssets.comms())?.replies.awayAfterSecs else { return false }
        return now.timeIntervalSince(since) >= Double(secs)
    }
}

/// Comms' replies in Slack (`assets/comms.json` → `replies`): an acknowledgement of
/// queued work, and while the operator is away an answer to a question their project's
/// documentation settles. Each goes to the conversation the message was written in, and
/// nowhere else, whatever the message or any model says.
public enum CommsReplier {
    public static func reply(_ r: CommsReplyRequest, now: Date = Date(), dir: URL = CommsLog.dir,
                             api: SlackAPI? = nil,
                             answer: ((CommsAnswerer.Question) -> CommsAnswer)? = nil)
        -> CommsReplyResult {
        guard let assets = try? CoreAssets.comms(), r.toggles.replies,
              r.decision.action != .injection,
              !r.decision.reason.hasPrefix(CommsWatch.bannedPrefix),
              !r.settings.bannedSenders.contains(r.message.userID),
              let to = destination(for: r.message) else { return CommsReplyResult() }
        let items = CommsLog.fold(CommsLog.read(in: dir))
        guard !items.contains(where: { $0.message.id == r.message.id && $0.replyOutcome != nil })
        else { return CommsReplyResult() }
        let me = CommsState.load(in: dir).me
        let name = me?.name ?? me.map { $0.handle } ?? ""
        let who = name.isEmpty ? "The operator" : name
        let body: String
        if let q = r.queued, [.review, .fixIssue, .investigate].contains(r.decision.action) {
            body = acknowledgement(q, away: r.away, name: who, sender: r.message, now: now,
                                   assets: assets)
        } else if r.decision.action == .question, r.away {
            let question = CommsAnswerer.Question(
                message: r.message,
                context: CommsWatch.context(for: [r.message], in: dir, settings: r.settings,
                                            assets: assets),
                documentation: CommsKnowledge.load(owner: r.settings.owner, repo: r.settings.repo,
                                                   dir: dir, now: now))
            let a = (answer ?? { CommsAnswerer.run($0, settings: r.settings, name: who) })(question)
            let text = sanitize(a.text, settings: r.settings, assets: assets)
            guard a.answers, !text.isEmpty else {
                let outcome = "skipped: " + (a.reason.isEmpty ? "no answer" : a.reason)
                CommsLog.append(CommsEvent(kind: .reply, id: r.message.id, outcome: outcome), in: dir)
                return CommsReplyResult(outcome: outcome)
            }
            body = CommsText.fill(assets.replies.answeredAway, ["name": who, "answer": text])
        } else {
            return CommsReplyResult()
        }
        let text = assets.replies.prefix + body
        // Logged first, so no failure after this point sends a second one.
        CommsLog.append(CommsEvent(kind: .reply, id: r.message.id, outcome: "sending", detail: text),
                        in: dir)
        let outcome: String
        do {
            let slack = try api ?? SlackAPI.signIn().api
            slack.deadline = Date().addingTimeInterval(Double(assets.slackBudgetSecs))
            try slack.post(channel: to.channel, threadTS: to.threadTS, text: text)
            outcome = "sent"
        } catch {
            outcome = "failed: " + error.localizedDescription
        }
        CommsLog.append(CommsEvent(kind: .reply, id: r.message.id, outcome: outcome, detail: text),
                        in: dir)
        return CommsReplyResult(outcome: outcome, text: text)
    }

    /// The message's own channel, in its thread: the thread the permalink names (only one
    /// in the same channel), else a new one under a channel message; a DM message outside
    /// a thread is answered in the DM. Nil for an id Slack would not have written.
    public static func destination(for m: CommsMessage) -> (channel: String, threadTS: String?)? {
        guard isConversationID(m.channelID), CommsTS.number(m.ts) != nil else { return nil }
        if let thread = threadTS(in: m.permalink, channel: m.channelID) {
            return (m.channelID, thread)
        }
        return (m.channelID, m.isDM ? nil : m.ts)
    }

    static func isConversationID(_ id: String) -> Bool {
        guard let first = id.first, "CDG".contains(first), id.count >= 9, id.count <= 20 else {
            return false
        }
        return id.allSatisfy { $0.isASCII && ($0.isUppercase || $0.isNumber) }
    }

    private static func threadTS(in permalink: String, channel: String) -> String? {
        guard let url = URLComponents(string: permalink),
              url.path.split(separator: "/").dropFirst().first.map(String.init) == channel,
              let ts = url.queryItems?.first(where: { $0.name == "thread_ts" })?.value,
              CommsTS.number(ts) != nil else { return nil }
        return ts
    }

    static func acknowledgement(_ q: CommsQueuedWork, away: Bool, name: String,
                                sender: CommsMessage, now: Date,
                                assets: CoreAssets.CommsModel) -> String {
        let r = assets.replies
        let what = CommsText.fill(r.what[q.kind] ?? "it", ["number": String(q.number)])
        let template = q.startedAt == nil ? (away ? r.queuedAway : r.queued)
            : (away ? r.runningAway : r.running)
        let due = eta(q, now: now, assets: assets)
        return CommsText.fill(template, [
            "name": name, "what": what,
            "eta": etaText(due, now: now, timezone: sender.senderTimezone, roundMins: r.roundMins),
        ])
    }

    /// One run of this kind for every full set of bays ahead of it, then its own; work
    /// under way is due one run after it started, and never sooner than ten minutes on.
    public static func eta(_ q: CommsQueuedWork, now: Date, assets: CoreAssets.CommsModel) -> Date {
        let run = q.runSecs.flatMap { $0.isFinite && $0 > 0 ? $0 : nil }
            ?? Double(assets.replies.runSecs[q.kind] ?? 2700)
        if let started = q.startedAt {
            return max(now.addingTimeInterval(600), started.addingTimeInterval(run))
        }
        let bays = max(1, q.bays), ahead = max(0, q.ahead)
        return now.addingTimeInterval(run * Double(1 + (ahead + bays - 1) / bays))
    }

    /// `15:40 today`, `09:05 tomorrow`, or `Thu 6 Nov 11:20`, rounded up to `roundMins`,
    /// in `timezone` (the sender's), else this machine's.
    public static func etaText(_ date: Date, now: Date, timezone: String?, roundMins: Int) -> String {
        let step = Double(max(1, roundMins) * 60)
        let rounded = Date(timeIntervalSince1970: (date.timeIntervalSince1970 / step).rounded(.up) * step)
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = timezone.flatMap(TimeZone.init(identifier:)) ?? .current
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = cal.timeZone
        f.dateFormat = "HH:mm"
        let clock = f.string(from: rounded)
        let days = cal.dateComponents([.day], from: cal.startOfDay(for: now),
                                      to: cal.startOfDay(for: rounded)).day ?? 0
        switch days {
        case ...0: return "\(clock) today"
        case 1:    return "\(clock) tomorrow"
        default:
            f.dateFormat = "EEE d MMM HH:mm"
            return f.string(from: rounded)
        }
    }

    /// The answer as one paragraph of at most `answer.maxChars`, with nothing Slack would
    /// turn into a mention, a broadcast or a link the project's own prefixes do not start.
    public static func sanitize(_ text: String, settings: CommsSettings,
                                assets: CoreAssets.CommsModel) -> String {
        var s = text.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        func sub(_ pattern: String, _ template: String) {
            guard let re = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive])
            else { return }
            s = re.stringByReplacingMatches(in: s, range: NSRange(s.startIndex..., in: s),
                                            withTemplate: template)
        }
        sub("<(https?://[^|>\\s]+)(\\|[^>]*)?>", "$1")
        sub("<[^>]*>", "")
        sub("(?<![\\p{L}\\p{N}._%+-])@(?=[\\p{L}\\p{N}])", "")
        let prefixes = assets.answer.linkPrefixes.map {
            CommsText.fill($0, ["owner": settings.owner, "repo": settings.repo]).lowercased()
        }
        if let re = try? NSRegularExpression(pattern: "(?:https?://|www\\.)[^\\s<>()\\[\\]]+",
                                             options: [.caseInsensitive]) {
            for m in re.matches(in: s, range: NSRange(s.startIndex..., in: s)).reversed() {
                guard let r = Range(m.range, in: s) else { continue }
                let link = s[r].lowercased()
                let allowed = prefixes.contains { p in
                    link == p || link.hasPrefix(p.hasSuffix("/") ? p : p + "/")
                        || link.hasPrefix(p + "#") || link.hasPrefix(p + "?")
                }
                if !allowed { s.replaceSubrange(r, with: "[link removed]") }
            }
        }
        s = s.trimmingCharacters(in: .whitespaces)
        let cap = assets.answer.maxChars
        guard s.count > cap else { return s }
        let cut = s.prefix(cap - 1)
        let word = cut.lastIndex(of: " ").map { cut[..<$0] } ?? cut
        return word.trimmingCharacters(in: .whitespaces) + "…"
    }
}

/// The away answer's verdict.
public struct CommsAnswer: Equatable, Sendable {
    /// Every check held and the model chose to answer.
    public var answers: Bool
    public var text: String
    public var reason: String

    public init(answers: Bool, text: String, reason: String) {
        self.answers = answers
        self.text = text
        self.reason = reason
    }

    public static func declined(_ reason: String) -> CommsAnswer {
        CommsAnswer(answers: false, text: "", reason: reason)
    }
}

/// The away answer: one `claude -p` sandboxed as `CommsClassifier` is, given the
/// documentation and the fenced question, answering `answer.schema`.
public enum CommsAnswerer {
    public struct Question: Sendable {
        public var message: CommsMessage
        /// Earlier pings of its conversation, oldest first.
        public var context: [CommsMessage]
        public var documentation: String

        public init(message: CommsMessage, context: [CommsMessage] = [], documentation: String) {
            self.message = message
            self.context = context
            self.documentation = documentation
        }
    }

    public static func prompt(settings: CommsSettings, name: String) -> String {
        guard let a = (try? CoreAssets.comms())?.answer else { return "" }
        return CommsText.fill(a.instructions, ["name": name, "owner": settings.owner,
                                               "repo": settings.repo])
    }

    public static func input(for q: Question) -> String {
        guard let a = (try? CoreAssets.comms())?.answer else { return "" }
        let docs = CommsText.fill(a.documentation, ["documentation": q.documentation])
        let blocks = q.context.map { block(a, a.contextLabel, $0) }
            + [block(a, a.questionLabel, q.message)]
        return ([docs] + blocks).joined(separator: "\n\n")
    }

    private static func block(_ a: CoreAssets.CommsModel.Answer, _ label: String,
                              _ m: CommsMessage) -> String {
        let sent = CommsTS.number(m.ts).map {
            CommsLog.isoString(Date(timeIntervalSince1970: Double($0 / 1_000_000)))
        } ?? m.ts
        return CommsText.fillFenced(a.input, [
            "label": label,
            "sender": CommsText.defang(m.sender),
            "title": CommsText.defang(m.senderTitle ?? ""),
            "channel": CommsText.defang(m.isDM ? "DM" : m.place),
            "sent": sent,
            "text": CommsText.defang(m.text),
        ])
    }

    public static func arguments(settings: CommsSettings, name: String) -> [String] {
        let model = (try? CoreAssets.comms())
        return ["-p", "--model", model?.answer.model ?? "sonnet",
                "--tools", "",
                "--strict-mcp-config",
                "--disable-slash-commands",
                "--setting-sources", "",
                "--no-session-persistence",
                "--output-format", "json",
                "--json-schema", model?.answerSchema ?? "",
                prompt(settings: settings, name: name)]
    }

    public static func run(_ q: Question, settings: CommsSettings, name: String,
                           binary: String? = nil) -> CommsAnswer {
        guard let model = try? CoreAssets.comms() else {
            return .declined("assets/comms.json could not be read")
        }
        guard !q.documentation.isEmpty else {
            return .declined("no documentation of \(settings.owner)/\(settings.repo) could be read")
        }
        guard let exe = binary ?? CommsClassifier.binary() else {
            return .declined("the claude CLI was not found")
        }
        switch CommsClassifier.execute(exe, arguments(settings: settings, name: name),
                                       input: Data(input(for: q).utf8),
                                       timeout: model.answer.timeoutSecs) {
        case .failedToStart(let why): return .declined("claude could not be started: \(why)")
        case .timedOut:               return .declined("claude did not answer in time")
        case .exited(_, let out, _):  return parse(out)
        }
    }

    /// Only a JSON object holding all three checks true and `answer` true answers.
    public static func parse(_ output: Data) -> CommsAnswer {
        let object: [String: Any]
        switch CommsDecision.answerObject(in: output, key: "answer") {
        case nil:                    return .declined("claude's output held no answer")
        case .failed(let d):         return .declined(d.reason)
        case .object(let o?):        object = o
        case .object(nil):           return .declined("claude's answer was not a JSON object")
        }
        let reason = (object["reason"] as? String) ?? ""
        let checks = ["straightforward", "answerable", "applies", "answer"]
        guard checks.allSatisfy({ object[$0] as? Bool == true }),
              let text = object["text"] as? String,
              !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return .declined(reason)
        }
        return CommsAnswer(answers: true, text: text, reason: reason)
    }
}

/// The project's documentation, as the away answer reads it: the repo's Markdown under
/// `answer.knowledge.paths`, fetched with `gh` and cached in `<dir>/knowledge`.
public enum CommsKnowledge {
    public typealias Fetch = (_ args: [String]) -> Data?

    public static func load(owner: String, repo: String, dir: URL = CommsLog.dir,
                            now: Date = Date(), fetch: Fetch? = nil) -> String {
        guard let k = (try? CoreAssets.comms())?.answer.knowledge else { return "" }
        let folder = dir.appendingPathComponent("knowledge", isDirectory: true)
        let file = folder.appendingPathComponent("\(owner)__\(repo).md")
        let cached = try? String(contentsOf: file, encoding: .utf8)
        if let cached, !cached.isEmpty,
           let at = (try? FileManager.default.attributesOfItem(atPath: file.path))?[.modificationDate]
               as? Date, now.timeIntervalSince(at) < k.refreshHours * 3600 {
            return cached
        }
        guard let fresh = read(owner: owner, repo: repo, k, fetch: fetch ?? gh), !fresh.isEmpty else {
            return cached ?? ""
        }
        CommsLog.prepare(dir)
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true,
                                                 attributes: [.posixPermissions: 0o700])
        CommsLog.writePrivate(Data(fresh.utf8), to: file)
        return fresh
    }

    /// Nil when the tree could not be listed.
    static func read(owner: String, repo: String, _ k: CoreAssets.CommsModel.Answer.Knowledge,
                     fetch: Fetch) -> String? {
        guard let tree = fetch(["api", "repos/\(owner)/\(repo)/git/trees/HEAD?recursive=1",
                                "--jq", ".tree[] | select(.type == \"blob\") | .path"]) else {
            return nil
        }
        let paths = String(decoding: tree, as: UTF8.self).split(separator: "\n").map(String.init)
            .filter { p in
                (p.hasSuffix(".md") || p.hasSuffix(".mdx"))
                    && k.paths.contains { $0.hasSuffix("/") ? p.hasPrefix($0) : p == $0 }
                    && !k.exclude.contains { p.hasPrefix($0) }
            }
            .sorted()
            .prefix(k.maxFiles)
        var out = "", total = 0
        for path in paths {
            guard let data = fetch(["api", "repos/\(owner)/\(repo)/contents/\(path)",
                                    "-H", "Accept: application/vnd.github.raw"]) else { continue }
            let text = String(decoding: data, as: UTF8.self)
            let section = "=== \(path) ===\n\(text.trimmingCharacters(in: .whitespacesAndNewlines))\n\n"
            guard total + section.utf8.count <= k.maxBytes else { break }
            out += section
            total += section.utf8.count
        }
        return out
    }

    private static func gh(_ args: [String]) -> Data? {
        guard let exe = try? GH.ghPath(),
              case .exited(0, let out, _) = CommsClassifier.execute(
                exe, args, input: Data(), timeout: 30) else { return nil }
        return out
    }
}
