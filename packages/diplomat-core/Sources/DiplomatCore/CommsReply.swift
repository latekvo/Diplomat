import Foundation

/// Work a ping put in the queue, or found there or already under way, as the front-end
/// that acted on it sees it: what an acknowledgement names and estimates.
public struct CommsQueuedWork: Codable, Equatable, Sendable {
    /// `review`, `fix_issue` or `investigate`: a key of `replies.languages`' `what` and of `replies.runSecs`.
    public var kind: String
    /// The PR or issue; unused for an investigation.
    public var number: Int
    /// Tasks waiting ahead of it, and how many bays run them at once.
    public var ahead: Int
    public var bays: Int
    /// When its agent started, for work already under way.
    public var startedAt: Date?

    public init(kind: String, number: Int, ahead: Int = 0, bays: Int = 1, startedAt: Date? = nil) {
        self.kind = kind
        self.number = number
        self.ahead = ahead
        self.bays = bays
        self.startedAt = startedAt
    }
}

/// What a front-end hands `CommsReplier.reply` once it has acted on a triaged ping
/// (Linux: `comms-reply`'s stdin).
public struct CommsReplyRequest: Codable, Sendable {
    public var message: CommsMessage
    public var decision: CommsDecision
    /// Set only when the ping's work is queued or running.
    public var queued: CommsQueuedWork?
    /// The rest of what the same message asked for (`merged`).
    public var alsoQueued: [CommsQueuedWork]
    /// The operator's session has been locked for their away setting (`CommsAway`).
    public var away: Bool
    public var toggles: CommsToggles
    public var settings: CommsSettings

    public init(message: CommsMessage, decision: CommsDecision, queued: CommsQueuedWork? = nil,
                alsoQueued: [CommsQueuedWork] = [], away: Bool, toggles: CommsToggles = CommsToggles(),
                settings: CommsSettings = CommsSettings()) {
        self.message = message
        self.decision = decision
        self.queued = queued
        self.alsoQueued = alsoQueued
        self.away = away
        self.toggles = toggles
        self.settings = settings
    }

    private enum CodingKeys: String, CodingKey {
        case message, decision, queued, alsoQueued, away, toggles, settings
    }

    /// One request per Slack message, in the order `requests` first names each: a message
    /// that asked for several gets one reply. A row's targets arrive last first
    /// (`CommsTickResult.Item.targets`), and the merged work reads in the message's order.
    public static func merged(_ requests: [CommsReplyRequest]) -> [CommsReplyRequest] {
        var order: [String] = []
        var groups: [String: [CommsReplyRequest]] = [:]
        for r in requests {
            let id = r.message.messageID
            if groups[id] == nil { order.append(id) }
            groups[id, default: []].append(r)
        }
        return order.compactMap { id in
            guard var out = groups[id]?.first else { return nil }
            let work = (groups[id] ?? []).reversed().flatMap { [$0.queued].compactMap { $0 } + $0.alsoQueued }
            out.message.id = id
            out.queued = work.first
            out.alsoQueued = Array(work.dropFirst())
            return out
        }
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(message: try c.decode(CommsMessage.self, forKey: .message),
                  decision: try c.decode(CommsDecision.self, forKey: .decision),
                  queued: try c.decodeIfPresent(CommsQueuedWork.self, forKey: .queued),
                  alsoQueued: try c.decodeIfPresent([CommsQueuedWork].self, forKey: .alsoQueued) ?? [],
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

/// Whether the operator is away: their session locked for `afterSecs`, their setting. The
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

    public static func isAway(lockedSince: Date?, afterSecs: Int, now: Date = Date()) -> Bool {
        if let forced = override { return forced }
        guard let since = lockedSince else { return false }
        return now.timeIntervalSince(since) >= Double(afterSecs)
    }
}

/// Comms' replies in Slack (`assets/comms.json` → `replies`), only while the operator is
/// away: an acknowledgement of queued work, and an answer to a question their project's
/// documentation settles. Each goes to the conversation the message was written in, and
/// nowhere else, whatever the message or any model says.
public enum CommsReplier {
    public static func reply(_ r: CommsReplyRequest, now: Date = Date(), dir: URL = CommsLog.dir,
                             api: SlackAPI? = nil,
                             answer: ((CommsAnswerer.Question) -> CommsAnswer)? = nil)
        -> CommsReplyResult {
        // The request's ban list was read when its ping was applied, and a reply can wait
        // minutes behind others or on its answer: the file is asked again before each step.
        func barred() -> Bool {
            r.settings.bannedSenders.contains(r.message.userID)
                || CommsBan.bannedNow().contains(r.message.userID.uppercased())
        }
        guard r.away, let assets = try? CoreAssets.comms(), r.toggles.replies,
              r.toggles.repliesTo(r.decision.action), !barred(),
              let words = assets.replies.language(r.decision.language),
              let to = destination(for: r.message) else { return CommsReplyResult() }
        // The raw lines, not the fold: rotation can cut a ping whose reply survives.
        let id = r.message.messageID
        guard !CommsLog.read(in: dir).contains(where: { $0.kind == .reply && $0.id == id })
        else { return CommsReplyResult() }
        let me = CommsState.load(in: dir).me
        let name = me?.name ?? me.map { $0.handle } ?? ""
        let who = name.isEmpty ? words.operator : name
        let body: String
        if let q = r.queued, [.review, .fixIssue, .investigate].contains(r.decision.action) {
            body = acknowledgement([q] + r.alsoQueued, name: who, sender: r.message,
                                   now: now, words: words, assets: assets)
        } else if r.decision.action == .question {
            var settings = r.settings
            settings.bannedSenders += CommsBan.bannedNow()
            let asked = CommsTS.number(r.message.ts) ?? 0
            let question = CommsAnswerer.Question(
                message: r.message,
                context: CommsWatch.context(for: [r.message], in: dir, settings: settings,
                                            assets: assets)
                    .filter { (CommsTS.number($0.ts) ?? 0) < asked },
                documentation: CommsKnowledge.load(owner: r.settings.owner, repo: r.settings.repo,
                                                   dir: dir, now: now))
            let a = (answer ?? { CommsAnswerer.run($0, settings: r.settings, name: who) })(question)
            let text = sanitize(a.text, settings: r.settings, assets: assets)
            guard a.answers, !text.isEmpty else {
                let outcome = "skipped: " + (a.answers ? "nothing was left once mentions and links were taken out"
                                             : a.reason.isEmpty ? "no answer" : a.reason)
                CommsLog.append(CommsEvent(kind: .reply, id: id, outcome: outcome), in: dir)
                return CommsReplyResult(outcome: outcome)
            }
            body = CommsText.fill(words.answeredAway, ["name": who, "answer": text])
        } else {
            return CommsReplyResult()
        }
        let text = assets.replies.prefix + body
        if barred() {
            let outcome = "skipped: the sender was banned before it went out"
            CommsLog.append(CommsEvent(kind: .reply, id: id, outcome: outcome), in: dir)
            return CommsReplyResult(outcome: outcome)
        }
        // On disk first, so no failure after this point sends a second one; unrecorded, a
        // later tick would find nothing and post it again.
        guard CommsLog.append(CommsEvent(kind: .reply, id: id, outcome: "sending",
                                         detail: text), in: dir) else {
            return CommsReplyResult(outcome: "failed: the Comms log could not be written, so nothing was sent")
        }
        let outcome: String
        do {
            let slack = try api ?? SlackAPI.signIn(refresh: {
                SlackToken.refresh(timeout: Double(assets.loginRefreshTimeoutSecs))
            }).api
            slack.deadline = Date().addingTimeInterval(Double(assets.slackBudgetSecs))
            try slack.post(channel: to.channel, threadTS: to.threadTS, text: text)
            outcome = "sent"
        } catch {
            outcome = "failed: " + error.localizedDescription
        }
        CommsLog.append(CommsEvent(kind: .reply, id: id, outcome: outcome, detail: text), in: dir)
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

    /// `work` is everything one message asked for, all of one kind. Queued targets went to
    /// the front of their band one after the other, so the last of them runs behind the
    /// rest and whatever the longest wait was ahead of.
    static func acknowledgement(_ work: [CommsQueuedWork], name: String,
                                sender: CommsMessage, now: Date,
                                words: CoreAssets.CommsModel.Replies.Language,
                                assets: CoreAssets.CommsModel) -> String {
        let r = assets.replies
        let kind = work.first?.kind ?? ""
        let what: String
        if work.count == 1 {
            what = CommsText.fill(words.what[kind] ?? "", ["number": String(work[0].number)])
        } else {
            let tags = work.map { "#\($0.number)" }
            let numbers = tags.dropLast().joined(separator: ", ")
                + " \(words.and) " + (tags.last ?? "")
            what = CommsText.fill(words.whatMany[kind] ?? "", ["numbers": numbers])
        }
        let allRunning = work.allSatisfy { $0.startedAt != nil }
        let template = allRunning ? words.runningAway : words.queuedAway
        var last = work.filter { $0.startedAt == nil }.max { $0.ahead < $1.ahead }
        last?.ahead += work.filter { $0.startedAt == nil }.count - 1
        let due = (work.filter { $0.startedAt != nil } + (last.map { [$0] } ?? []))
            .map { eta($0, now: now, assets: assets) }.max() ?? now
        return CommsText.fill(template, [
            "name": name, "what": what,
            "eta": etaText(due, now: now, timezone: sender.senderTimezone, roundMins: r.roundMins,
                           words: words),
        ])
    }

    /// One run of this kind for each set of bays ahead of it, a partial set counting as a
    /// whole, then its own; work under way is due one run after it started, and never
    /// sooner than ten minutes on.
    public static func eta(_ q: CommsQueuedWork, now: Date, assets: CoreAssets.CommsModel) -> Date {
        let run = Double(assets.replies.runSecs[q.kind] ?? 2700)
        if let started = q.startedAt {
            return max(now.addingTimeInterval(600), started.addingTimeInterval(run))
        }
        let bays = max(1, q.bays), ahead = max(0, q.ahead)
        return now.addingTimeInterval(run * Double(1 + (ahead + bays - 1) / bays))
    }

    /// `15:40 today`, `09:05 tomorrow`, or `Thu 6 Nov 11:20`, as `words` (English by
    /// default) write them, rounded up to `roundMins`, in `timezone` (the sender's), else
    /// this machine's.
    public static func etaText(_ date: Date, now: Date, timezone: String?, roundMins: Int,
                               words: CoreAssets.CommsModel.Replies.Language? = nil) -> String {
        guard let words = words ?? (try? CoreAssets.comms())?.replies
            .language(CommsDecision.defaultLanguage) else { return "" }
        let step = Double(max(1, roundMins) * 60)
        let rounded = Date(timeIntervalSince1970: (date.timeIntervalSince1970 / step).rounded(.up) * step)
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = timezone.flatMap(TimeZone.init(identifier:)) ?? .current
        let f = DateFormatter()
        f.locale = Locale(identifier: words.locale)
        f.timeZone = cal.timeZone
        f.dateFormat = "HH:mm"
        let clock = f.string(from: rounded)
        let days = cal.dateComponents([.day], from: cal.startOfDay(for: now),
                                      to: cal.startOfDay(for: rounded)).day ?? 0
        switch days {
        case ...0: return CommsText.fill(words.today, ["clock": clock])
        case 1:    return CommsText.fill(words.tomorrow, ["clock": clock])
        default:
            f.dateFormat = "EEE d MMM HH:mm"
            return f.string(from: rounded)
        }
    }

    /// The answer as one paragraph of at most `answer.maxChars`, with nothing Slack would
    /// turn into a mention, a broadcast or a link the project's own prefixes do not start.
    public static func sanitize(_ text: String, settings: CommsSettings,
                                assets: CoreAssets.CommsModel) -> String {
        var s = text
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
                // A dot segment or a backslash can walk a link out from under its prefix.
                let allowed = !link.contains("/..") && !link.contains("%2e") && !link.contains("\\")
                    && prefixes.contains { p in
                        link == p || link.hasPrefix(p.hasSuffix("/") ? p : p + "/")
                            || link.hasPrefix(p + "#") || link.hasPrefix(p + "?")
                    }
                if !allowed { s.replaceSubrange(r, with: "[link removed]") }
            }
        }
        s = s.split(whereSeparator: \.isWhitespace).joined(separator: " ")
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
    /// `gh` with these arguments, given at most `timeout` seconds.
    public typealias Fetch = (_ args: [String], _ timeout: Double) -> Data?

    public static func load(owner: String, repo: String, dir: URL = CommsLog.dir,
                            now: Date = Date(), clock: () -> Date = Date.init,
                            fetch: Fetch? = nil) -> String {
        guard let k = (try? CoreAssets.comms())?.answer.knowledge else { return "" }
        let folder = dir.appendingPathComponent("knowledge", isDirectory: true)
        let file = folder.appendingPathComponent("\(owner)__\(repo).md")
        let cached = try? String(contentsOf: file, encoding: .utf8)
        if let cached, !cached.isEmpty,
           let at = (try? FileManager.default.attributesOfItem(atPath: file.path))?[.modificationDate]
               as? Date, now.timeIntervalSince(at) < k.refreshHours * 3600 {
            return cached
        }
        let deadline = clock().addingTimeInterval(k.fetchSecs)
        guard let fresh = read(owner: owner, repo: repo, k, deadline: deadline, clock: clock,
                               fetch: fetch ?? gh), !fresh.isEmpty else {
            return cached ?? ""
        }
        CommsLog.prepare(dir)
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true,
                                                 attributes: [.posixPermissions: 0o700])
        CommsLog.writePrivate(Data(fresh.utf8), to: file)
        return fresh
    }

    /// Nil unless the tree and every file in it were read before `deadline`: a partial set
    /// must not be cached as the whole.
    static func read(owner: String, repo: String, _ k: CoreAssets.CommsModel.Answer.Knowledge,
                     deadline: Date, clock: () -> Date, fetch: Fetch) -> String? {
        func call(_ args: [String]) -> Data?? {
            let left = deadline.timeIntervalSince(clock())
            guard left > 0 else { return .none }
            return .some(fetch(args, min(30, left)))
        }
        guard case let tree?? = call(["api", "repos/\(owner)/\(repo)/git/trees/HEAD?recursive=1",
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
            guard let got = call(["api", "repos/\(owner)/\(repo)/contents/\(path)",
                                  "-H", "Accept: application/vnd.github.raw"]) else { return nil }
            guard let data = got else { return nil }
            let text = String(decoding: data, as: UTF8.self)
            let section = "=== \(path) ===\n\(text.trimmingCharacters(in: .whitespacesAndNewlines))\n\n"
            guard total + section.utf8.count <= k.maxBytes else { break }
            out += section
            total += section.utf8.count
        }
        return out
    }

    private static func gh(_ args: [String], _ timeout: Double) -> Data? {
        guard let exe = try? GH.ghPath(),
              case .exited(0, let out, _) = CommsClassifier.execute(
                exe, args, input: Data(), timeout: timeout) else { return nil }
        return out
    }
}
