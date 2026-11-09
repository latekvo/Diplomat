import Foundation

/// One triage call's messages: `messages` to answer, oldest first, and `context`, earlier
/// pings of the same conversations, already triaged, shown to the classifier but never
/// answered.
public struct CommsBatch: Equatable, Sendable {
    public var messages: [CommsMessage]
    public var context: [CommsMessage]

    public init(messages: [CommsMessage], context: [CommsMessage] = []) {
        self.messages = messages
        self.context = context
    }

    /// What the classifier may have read `message` against: the batch's other messages and
    /// its context posted before it in the same conversation. `CommsDecision.checked`
    /// takes a number named there.
    public func earlier(than message: CommsMessage) -> [CommsMessage] {
        let at = CommsTS.number(message.ts) ?? 0
        return (context + messages).filter {
            $0.channelID == message.channelID && (CommsTS.number($0.ts) ?? .max) < at
        }
    }
}

/// The triage call: one sandboxed `claude -p` per batch, answering a fixed schema.
///
/// The messages are untrusted, so the call gets nothing it could be talked into using.
/// `--tools ""` alone still loads the operator's MCP servers (a Slack one can post), hence
/// `--strict-mcp-config` with no config; `--disable-slash-commands`, `--setting-sources ""`
/// (no hooks or permissions added back) and `--no-session-persistence` close the rest. The
/// binary runs directly (no alias) in the temp directory, so no CLAUDE.md is read.
///
/// The instructions (`prompt`) are the argument; everything from the message (`input`)
/// arrives on stdin, fenced and quoted.
public enum CommsClassifier {
    /// Starts the reason of a decision no answer came back for (no `claude`, a timeout, a
    /// crash, an error result). `CommsWatch.tick` reports those as the tick's error, since
    /// every later ping would fail the same way.
    public static let failurePrefix = "classifier failed: "

    /// `classifier.instructions`, its repository wording `classifier.repos.one`'s for one
    /// watched repository and `many`'s for several.
    public static func prompt(settings: CommsSettings) -> String {
        guard let c = (try? CoreAssets.comms())?.classifier else { return "" }
        let names = settings.repos.map { String($0.split(separator: "/").last ?? "") }
        let base = ["owner": settings.owner, "repo": settings.repo,
                    "list": CommsText.list(settings.repos, and: "and"),
                    "example": names.dropFirst().first ?? ""]
        var values = (settings.repos.count > 1 ? c.repos.many : c.repos.one)
            .mapValues { CommsText.fill($0, base) }
        values["login"] = settings.login.isEmpty ? "the operator" : settings.login
        values["owner"] = settings.owner
        values["repo"] = settings.repo
        return CommsText.fill(c.instructions, values)
    }

    /// The stdin: one block per message, context first, each its label line and the
    /// message quoted between fence lines. The quoting holds each fence, so no message can
    /// write a label or another message's header; `defang` only removes the marker
    /// spellings it knows. A DM's `Where` carries its conversation id: every DM would
    /// otherwise read the same.
    public static func input(for batch: CommsBatch, settings: CommsSettings) -> String {
        guard let c = (try? CoreAssets.comms())?.classifier else { return "" }
        let blocks = batch.context.map { block(c, c.contextLabel, $0, settings) }
            + batch.messages.enumerated().map { i, m in
                block(c, CommsText.fill(c.messageLabel, ["n": String(i + 1)]), m, settings)
            }
        return blocks.joined(separator: "\n\n")
    }

    private static func block(_ c: CoreAssets.CommsModel.Classifier, _ label: String,
                              _ message: CommsMessage, _ settings: CommsSettings) -> String {
        let reasons = message.reasons.map { r -> String in
            switch r {
            case .dm:      return "DM"
            case .mention: return "@mention"
            case .keyword: return "keyword \"\(message.keyword ?? "")\""
            }
        }.joined(separator: ", ")
        let sent = CommsTS.number(message.ts).map {
            CommsLog.isoString(Date(timeIntervalSince1970: Double($0 / 1_000_000)))
        } ?? message.ts
        return CommsText.fillFenced(c.input, [
            "label": label,
            "login": settings.login.isEmpty ? "the operator" : settings.login,
            "sender": CommsText.defang(message.sender),
            "channel": CommsText.defang(message.isDM ? "DM \(message.channelID)" : message.place),
            "sent": sent,
            "reasons": CommsText.defang(reasons),
            "text": CommsText.defang(message.text),
        ])
    }

    /// `classifier.schema`; with several watched repositories, each decision and each of
    /// its targets also names one of them as `repo`.
    public static func schema(settings: CommsSettings) -> String {
        let fixed = (try? CoreAssets.comms())?.schema ?? ""
        guard settings.repos.count > 1,
              var root = (try? JSONSerialization.jsonObject(with: Data(fixed.utf8))) as? [String: Any],
              var props = root["properties"] as? [String: Any],
              var decisions = props["decisions"] as? [String: Any],
              var decision = decisions["items"] as? [String: Any],
              var fields = decision["properties"] as? [String: Any],
              var targets = fields["targets"] as? [String: Any],
              var target = targets["items"] as? [String: Any],
              var targetFields = target["properties"] as? [String: Any] else { return fixed }
        let repo: [String: Any] = ["type": "string", "enum": settings.repos]
        func requiring(_ object: [String: Any]) -> [String] {
            ((object["required"] as? [String]) ?? []) + ["repo"]
        }
        targetFields["repo"] = repo
        target["properties"] = targetFields
        target["required"] = requiring(target)
        targets["items"] = target
        fields["targets"] = targets
        fields["repo"] = repo
        decision["properties"] = fields
        decision["required"] = requiring(decision)
        decisions["items"] = decision
        props["decisions"] = decisions
        root["properties"] = props
        guard let data = try? JSONSerialization.data(withJSONObject: root, options: [.sortedKeys])
        else { return fixed }
        return String(decoding: data, as: UTF8.self)
    }

    public static func arguments(settings: CommsSettings) -> [String] {
        ["-p", "--model", settings.model,
         "--tools", "",
         "--strict-mcp-config",
         "--disable-slash-commands",
         "--setting-sources", "",
         "--no-session-persistence",
         "--output-format", "json",
         "--json-schema", schema(settings: settings),
         prompt(settings: settings)]
    }

    // Only hits are cached, so an install made after a miss is seen.
    private static let lock = NSLock()
    private static var cached: (path: String, at: Date)?
    private static let resolveTTL: TimeInterval = 60

    public static func binary(now: Date = Date()) -> String? {
        if let env = ProcessInfo.processInfo.environment["DIPLOMAT_CLAUDE"], !env.isEmpty {
            return env
        }
        lock.lock()
        defer { lock.unlock() }
        if let c = cached, now.timeIntervalSince(c.at) < resolveTTL,
           FileManager.default.isExecutableFile(atPath: c.path) {
            return c.path
        }
        let shell = ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh"
        let found = LoginShell.locate("claude", shell: shell, environment: childEnvironment)
            ?? LoginShell.onPath("claude")
        cached = found.map { ($0, now) }
        return found
    }

    /// How long one call over `messages` may take: its answer grows with them.
    public static func callBudget(messages: Int, model: CoreAssets.CommsModel) -> TimeInterval {
        max(model.classifierTimeoutSecs, model.classifierSecsPerMessage * Double(messages))
    }

    /// One decision per message of `batch`, in order. Never hangs past `timeout`; a call
    /// that fails is a `none` for every message, whose reason starts with `failurePrefix`.
    public static func run(_ batch: CommsBatch, settings: CommsSettings,
                           binary: String? = nil, timeout: TimeInterval? = nil) -> [CommsDecision] {
        let count = batch.messages.count
        func failed(_ why: String) -> [CommsDecision] {
            Array(repeating: .none(failurePrefix + why), count: count)
        }
        guard let model = try? CoreAssets.comms() else {
            return failed("assets/comms.json could not be read")
        }
        guard let exe = binary ?? self.binary() else {
            return failed("the claude CLI was not found (set DIPLOMAT_CLAUDE to its path)")
        }
        let budget = timeout ?? callBudget(messages: count, model: model)
        switch execute(exe, arguments(settings: settings),
                       input: Data(input(for: batch, settings: settings).utf8),
                       timeout: budget) {
        case .failedToStart(let why):
            return failed("claude could not be started: \(why)")
        case .timedOut:
            return failed("claude did not answer within \(Int(budget))s")
        case .exited(let code, let out, let err):
            if let decisions = CommsDecision.answers(in: out, count: count) { return decisions }
            let tail = String(String(decoding: err, as: UTF8.self)
                .trimmingCharacters(in: .whitespacesAndNewlines).suffix(200))
            return failed("claude exited \(code) with no answer" + (tail.isEmpty ? "" : ": \(tail)"))
        }
    }

    enum Outcome {
        case failedToStart(String)
        case timedOut
        case exited(Int32, Data, Data)
    }

    /// Every child the watcher starts (`claude`, its login-shell lookup, curl) gets this;
    /// curl is handed the token on stdin instead.
    static var childEnvironment: [String: String] {
        var env = ProcessInfo.processInfo.environment
        env["DIPLOMAT_SLACK_TOKEN"] = nil
        return env
    }

    /// Run `exe` for at most `timeout`, then stop it as `GH.run` does. All three streams
    /// are files, since a pipe the child fills blocks it; the wait is a semaphore the
    /// termination handler signals, since corelibs' `waitUntilExit` lasts until every
    /// holder of the launch socketpair has gone.
    ///
    /// The files hold the Slack token (curl's config) and DM text, and off macOS the temp
    /// dir is usually the shared /tmp: a fresh 0700 directory, each file 0600 from creation.
    static func execute(_ exe: String, _ args: [String], input: Data,
                        timeout: TimeInterval) -> Outcome {
        let fm = FileManager.default
        var template = Array(fm.temporaryDirectory
            .appendingPathComponent("diplomat-comms-XXXXXX").path.utf8CString)
        let made = template.withUnsafeMutableBufferPointer { buf in
            buf.baseAddress.flatMap { mkdtemp($0) }.map { String(cString: $0) }
        }
        guard let dir = made else { return .failedToStart("could not stage its stdin") }
        defer { try? fm.removeItem(atPath: dir) }
        let inPath = dir + "/in", outPath = dir + "/out", errPath = dir + "/err"
        guard let stdin = privateFile(inPath, input),
              let stdout = privateFile(outPath, Data()),
              let stderr = privateFile(errPath, Data()) else {
            return .failedToStart("could not stage its stdin")
        }
        defer {
            try? stdin.close()
            try? stdout.close()
            try? stderr.close()
        }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: exe)
        proc.arguments = args
        proc.environment = childEnvironment
        proc.currentDirectoryURL = fm.temporaryDirectory
        proc.standardInput = stdin
        proc.standardOutput = stdout
        proc.standardError = stderr
        let exited = DispatchSemaphore(value: 0)
        proc.terminationHandler = { _ in exited.signal() }
        do { try proc.run() } catch {
            proc.terminationHandler = nil
            return .failedToStart(error.localizedDescription)
        }
        if exited.wait(timeout: .now() + timeout) == .timedOut {
            GH.stop(proc)
            return .timedOut
        }
        return .exited(proc.terminationStatus,
                       (try? Data(contentsOf: URL(fileURLWithPath: outPath))) ?? Data(),
                       (try? Data(contentsOf: URL(fileURLWithPath: errPath))) ?? Data())
    }

    private static func privateFile(_ path: String, _ contents: Data) -> FileHandle? {
        let fd = open(path, O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC, 0o600)
        guard fd >= 0 else { return nil }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        do {
            try handle.write(contentsOf: contents)
            try handle.seek(toOffset: 0)
        } catch {
            return nil
        }
        return handle
    }
}

public struct CommsState: Codable, Equatable, Sendable {
    /// The Slack ts at or below which nothing is looked at again; nil before the first tick.
    public var watermark: String?
    /// Ids already turned into pings above the watermark, newest last.
    public var seen: [String]
    public var me: CommsIdentity?
    /// The last tick's failure, so an `error` event fires on the transition only.
    public var error: String?
    /// The last triage's failure, kept until one succeeds: a tick with nothing to triage
    /// still ends on it.
    public var classifierError: String?
    /// Taken, but not yet handed to a front-end as pings; the next tick hands them over
    /// first.
    public var unannounced: [CommsMessage]
    /// Pinged and waiting for triage, oldest first.
    public var queue: [CommsMessage]
    /// Triaged (or taken for triage) but not yet handed to a front-end; the next tick
    /// hands them over first.
    public var undelivered: [CommsTickResult.Item]
    /// Per query that overflowed `maxSearchPages`, the oldest ts already reported, so one
    /// shortfall logs one `error` event.
    public var gaps: [String: String]
    /// When a tick last had Claude Code refresh its Slack login (Unix seconds), so a login
    /// that stays dead costs one `claude mcp list` per `loginRefreshSecs`, not per tick.
    public var loginRefreshedAt: Double?

    public init(watermark: String? = nil, seen: [String] = [], me: CommsIdentity? = nil,
                error: String? = nil, classifierError: String? = nil,
                unannounced: [CommsMessage] = [], queue: [CommsMessage] = [],
                undelivered: [CommsTickResult.Item] = [], gaps: [String: String] = [:],
                loginRefreshedAt: Double? = nil) {
        self.watermark = watermark
        self.seen = seen
        self.me = me
        self.error = error
        self.classifierError = classifierError
        self.unannounced = unannounced
        self.queue = queue
        self.undelivered = undelivered
        self.gaps = gaps
        self.loginRefreshedAt = loginRefreshedAt
    }

    private enum CodingKeys: String, CodingKey {
        case watermark, seen, me, error, classifierError, unannounced, queue, undelivered, gaps,
             loginRefreshedAt
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(watermark: try? c.decodeIfPresent(String.self, forKey: .watermark),
                  seen: (try? c.decodeIfPresent([String].self, forKey: .seen)) ?? [],
                  me: try? c.decodeIfPresent(CommsIdentity.self, forKey: .me),
                  error: try? c.decodeIfPresent(String.self, forKey: .error),
                  classifierError: try? c.decodeIfPresent(String.self, forKey: .classifierError),
                  unannounced: (try? c.decodeIfPresent([CommsMessage].self,
                                                       forKey: .unannounced)) ?? [],
                  queue: (try? c.decodeIfPresent([CommsMessage].self, forKey: .queue)) ?? [],
                  undelivered: (try? c.decodeIfPresent([CommsTickResult.Item].self,
                                                       forKey: .undelivered)) ?? [],
                  gaps: (try? c.decodeIfPresent([String: String].self, forKey: .gaps)) ?? [:],
                  loginRefreshedAt: try? c.decodeIfPresent(Double.self, forKey: .loginRefreshedAt))
    }

    public static func url(in dir: URL = CommsLog.dir) -> URL {
        dir.appendingPathComponent("state.json")
    }

    /// A missing or unreadable file reads as a first tick.
    public static func load(in dir: URL = CommsLog.dir) -> CommsState {
        guard let data = try? Data(contentsOf: url(in: dir)),
              let state = try? JSONDecoder().decode(CommsState.self, from: data) else {
            return CommsState()
        }
        return state
    }

    /// Atomic, so a crash mid-write leaves the previous state; 0600, since the queue and
    /// what is undelivered hold message text.
    public func save(in dir: URL = CommsLog.dir) {
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys]
        guard let data = try? enc.encode(self) else { return }
        CommsLog.prepare(dir)
        PrivateFiles.writeWhole(data, to: Self.url(in: dir))
    }
}

public struct CommsTickResult: Codable, Equatable, Sendable {
    public struct Item: Codable, Equatable, Sendable {
        public var message: CommsMessage
        public var decision: CommsDecision

        public init(message: CommsMessage, decision: CommsDecision) {
            self.message = message
            self.decision = decision
        }

        /// One item per target of `decision`, at most `maxTargets` of them, so each has its
        /// own action, row in the log and queue entry; last first, as each goes to the
        /// front of its band and the first asked for should run first. A decision with one
        /// target or none is one item, as it stands. A row past the first is
        /// `<id>#<number>`, or `<id>#<owner/repo>#<number>` outside the primary repository.
        public static func targets(_ message: CommsMessage, _ decision: CommsDecision,
                            maxTargets: Int) -> [Item] {
            guard decision.numbers.count > 1 else {
                return [Item(message: message, decision: decision)]
            }
            let kept = decision.targets.prefix(max(1, maxTargets))
            return kept.enumerated().reversed().map { i, t in
                var row = message
                if i > 0 {
                    let mark = String(CommsMessage.targetSeparator)
                    row.id += mark + (t.repo.isEmpty ? "" : t.repo + mark) + String(t.number)
                }
                return Item(message: row, decision: decision.targeting(t))
            }
        }
    }

    public var me: CommsIdentity?
    /// New pings, oldest first: each is triaged later, in `items` of this tick or a later one.
    public var pings: [CommsMessage]
    /// Triage answers.
    public var items: [Item]
    public var error: String?
    /// What this tick could not read (a catch-up past the horizon, a query paged out). Not
    /// a failure, so these go to the activity feed, not `error`.
    public var notices: [String]

    public init(me: CommsIdentity?, pings: [CommsMessage] = [], items: [Item], error: String?,
                notices: [String] = []) {
        self.me = me
        self.pings = pings
        self.items = items
        self.error = error
        self.notices = notices
    }

    private enum CodingKeys: String, CodingKey { case me, pings, items, error, notices }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        me = try c.decodeIfPresent(CommsIdentity.self, forKey: .me)
        pings = try c.decodeIfPresent([CommsMessage].self, forKey: .pings) ?? []
        items = try c.decode([Item].self, forKey: .items)
        error = try c.decodeIfPresent(String.self, forKey: .error)
        notices = try c.decodeIfPresent([String].self, forKey: .notices) ?? []
    }

    /// An absent `me` or `error` is written as `null`, so a reader can tell a clean tick
    /// from a truncated answer.
    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(me, forKey: .me)
        try c.encode(pings, forKey: .pings)
        try c.encode(items, forKey: .items)
        try c.encode(error, forKey: .error)
        try c.encode(notices, forKey: .notices)
    }
}

public enum CommsWatch {
    static let interrupted = "the tick ended before its triage answered, so it was not triaged"

    /// Starts the reason of the `none` a banned sender's message gets in place of a
    /// triage: its text never reaches a model.
    public static let bannedPrefix = "sender banned: "

    static func bannedReason(_ message: CommsMessage) -> String {
        bannedPrefix + "\(message.sender) is on the prompt-injection ban list, so this was "
            + "not triaged"
    }

    /// The first tick only learns who the login is and sets the watermark to `now`, so
    /// turning Comms on triages no backlog; a watermark older than `catchUpHours` likewise
    /// moves up to `now` less `indexLagSecs`, and one `error` event names the skipped
    /// stretch. Otherwise the `comms.json` queries run, matches at or below the watermark
    /// or already `seen` drop, and the oldest `maxPerTick` whose senders' profiles admit them
    /// are taken: each is a ping at once, and joins the queue for triage. All Slack calls
    /// past signing in share `slackBudgetSecs`. A query overflowing `maxSearchPages` is read
    /// newest first, and one `error` event names what went unread.
    ///
    /// The queue is triaged in batches, one `classify` call each (`flush`): its oldest
    /// `batchSize` once it holds that many or its newest message is
    /// `settings.flushAfterSecs` old; at most one batch a tick. A tick whose Slack calls fail
    /// triages nothing: offline, the classifier would fail too, and spend the batch. An
    /// answer naming several PRs or issues is handed over as one item per target
    /// (`CommsTickResult.Item.targets`).
    ///
    /// A queued message from one of `settings.bannedSenders` is never triaged: its decision
    /// is a `none` whose reason starts with `bannedPrefix`, on the tick it is taken or the
    /// first after the ban.
    ///
    /// Each batch is saved as taken before its triage runs, so a tick that never returns
    /// hands it over on the next one (triaged, or as an interrupted `none`) and never
    /// triages it twice. `deliver` sees the result before anything is logged or let go; a
    /// front-end in another process (`comms-tick`) answers false when the result did not
    /// reach it, and the pings and answers wait for the next tick the same way.
    ///
    /// The watermark trails `now` by `indexLagSecs` because Slack's search index can return
    /// a message well after it was posted; `seen` stops one in that window being triaged
    /// twice. With pings left over past `maxPerTick`, it stops short of the first one.
    ///
    /// Slack is reached under Claude Code's own Slack login (`SlackAPI.signIn`), unless
    /// `api` is given; an expired or refused login has Claude Code refresh it, at most
    /// once per `loginRefreshSecs`.
    ///
    /// A Slack failure leaves the watermark in place and is the result's `error`; so is the
    /// last triage's failure, until one succeeds. An `error` event is logged only on the
    /// tick that goes from working to failing.
    public static func tick(settings: CommsSettings, now: Date = Date(),
                            dir: URL = CommsLog.dir, api: SlackAPI? = nil,
                            deliver: (CommsTickResult) -> Bool = { _ in true },
                            classify: (CommsBatch) -> [CommsDecision]) -> CommsTickResult {
        var state = CommsState.load(in: dir)

        var notices: [String] = []
        func notice(_ text: String) {
            CommsLog.append(CommsEvent(kind: .error, id: "", detail: text), in: dir)
            notices.append(text)
        }
        func finish(error: String?) -> CommsTickResult {
            let error = error ?? state.classifierError
            let result = CommsTickResult(me: state.me, pings: state.unannounced,
                                         items: state.undelivered, error: error, notices: notices)
            if deliver(result) {
                for message in result.pings {
                    CommsLog.append(CommsEvent(kind: .ping, id: message.id, message: message), in: dir)
                }
                for item in result.items {
                    CommsLog.append(CommsEvent(kind: .decision, id: item.message.id,
                                               decision: item.decision), in: dir)
                }
                state.unannounced = []
                state.undelivered = []
            }
            if let error, state.error == nil {
                CommsLog.append(CommsEvent(kind: .error, id: "", detail: error), in: dir)
            }
            state.error = error
            state.save(in: dir)
            return result
        }

        guard let assets = try? CoreAssets.comms() else {
            return finish(error: "assets/comms.json could not be read")
        }
        let injected = api
        let api: SlackAPI
        let me: CommsIdentity
        do {
            // Not cached: a swapped login must not leave the previous owner's id deciding
            // which messages are "my own".
            if let given = injected {
                (api, me) = (given, try given.authTest())
            } else {
                let due = state.loginRefreshedAt.map {
                    now.timeIntervalSince1970 - $0 >= Double(assets.loginRefreshSecs)
                } ?? true
                let signedIn = try SlackAPI.signIn(refresh: due ? {
                    state.loginRefreshedAt = now.timeIntervalSince1970
                    SlackToken.refresh(timeout: Double(assets.loginRefreshTimeoutSecs))
                } : nil)
                (api, me) = (signedIn.api, signedIn.me.identity)
            }
            api.deadline = Date().addingTimeInterval(Double(assets.slackBudgetSecs))
            state.me = me
        } catch {
            return finish(error: error.localizedDescription)
        }
        guard var watermark = state.watermark.flatMap(CommsTS.number) else {
            state.watermark = CommsTS.string(CommsTS.number(now))
            return finish(error: nil)
        }
        let lagged = CommsTS.number(now) - assets.indexLagSecs * 1_000_000
        if watermark < CommsTS.number(now) - assets.catchUpHours * 3_600 * 1_000_000 {
            notice("Comms had read Slack only up to \(iso(watermark)), more than "
                + "\(assets.catchUpHours) hours ago; messages before \(iso(lagged)) "
                + "were not triaged")
            watermark = max(watermark, lagged)
            state.watermark = CommsTS.string(watermark)
            state.gaps = [:]
        }
        let queries = assets.queries

        var searches = [
            ("DMs", queries.dm.filled([:])),
            ("mentions", queries.mention.filled(["user": me.userID])),
        ]
        for keyword in settings.keywords {
            let k = keyword.replacingOccurrences(of: "\"", with: "")
            // Slack expands a `*` only outside quotes, so a glob goes word by word.
            let query = k.contains("*")
                ? CoreAssets.CommsModel.Query(
                    keywords: k.split(separator: " ").flatMap {
                        queries.wildcard.filled(["keyword": String($0)]).keywords
                    },
                    filters: queries.wildcard.filters)
                : queries.keyword.filled(["keyword": k])
            searches.append(("matches of \"\(k)\"", query))
        }
        var found: [String: SlackMatch] = [:]
        var gaps: [String: Int] = [:]
        do {
            for (label, query) in searches {
                gaps[label] = try collect(query, above: watermark, into: &found, api: api,
                                          assets: assets)
            }
        } catch {
            return finish(error: error.localizedDescription)
        }
        for (label, oldest) in gaps.sorted(by: { $0.key < $1.key })
        where state.gaps[label] != CommsTS.string(oldest) {
            notice("Slack holds more \(label) than one poll reads "
                + "(\(assets.maxSearchPages) pages of \(assets.searchCount)); those from "
                + "\(iso(watermark)) to \(iso(oldest)) were not triaged")
        }
        state.gaps = gaps.mapValues(CommsTS.string)

        let seen = Set(state.seen)
        let fresh = found.values
            .compactMap { m in CommsTS.number(m.ts).map { (n: $0, m: m) } }
            .filter { $0.n > watermark && !seen.contains($0.m.id) }
            .sorted { ($0.n, $0.m.id) < ($1.n, $1.m.id) }
        // Oldest first, reading senders' profiles only until one ping past `maxPerTick` is
        // found or `maxProfileReads` are spent; `unread` is where that walk stopped.
        var senders: [String: SlackProfile?] = [:]
        var pending: [(n: Int, m: CommsMessage)] = []
        var unread: Int?
        do {
            for (n, match) in fresh where pending.count <= settings.maxPerTick {
                let user = match.user
                if !user.isEmpty, !match.isBot, user != me.userID, senders[user] == nil {
                    guard senders.count < assets.maxProfileReads else {
                        unread = n
                        break
                    }
                    senders[user] = .some(try api.profile(user))
                }
                if let m = CommsMatch.message(from: match, sender: senders[user] ?? nil, me: me,
                                              settings: settings) {
                    pending.append((n, m))
                }
            }
        } catch {
            return finish(error: error.localizedDescription)
        }
        let take = pending.prefix(settings.maxPerTick)
        let rest = pending.dropFirst(settings.maxPerTick)

        for (_, message) in take {
            state.seen.append(message.id)
            state.unannounced.append(message)
            state.queue.append(message)
        }
        let next = [rest.first?.n, unread].compactMap { $0 }.min()
            .map { max(watermark, min(lagged, $0 - 1)) } ?? max(watermark, lagged)
        state.watermark = CommsTS.string(next)
        // An id at or below the watermark can never be offered again.
        state.seen = Array(state.seen.filter { id in
            CommsTS.number(String(id.split(separator: ":").last ?? "")).map { $0 > next } ?? false
        }.suffix(assets.seenLimit))
        state.save(in: dir)
        flush(&state, settings: settings, now: now, dir: dir, assets: assets, notice: notice, classify: classify)
        return finish(error: nil)
    }

    /// Triages the queue's due batch, if one is: see `tick`. A batch with an `injection`
    /// among its answers is triaged again one message per call, each with only its own
    /// conversation's context, so no sender is banned, nor anything asked, on the strength
    /// of another batch message's text.
    private static func flush(_ state: inout CommsState, settings: CommsSettings, now: Date,
                              dir: URL, assets: CoreAssets.CommsModel, notice: (String) -> Void,
                              classify: (CommsBatch) -> [CommsDecision]) {
        func ts(_ m: CommsMessage) -> Int { CommsTS.number(m.ts) ?? 0 }
        state.queue.sort { (ts($0), $0.id) < (ts($1), $1.id) }
        for m in state.queue where settings.bannedSenders.contains(m.userID) {
            state.undelivered.append(CommsTickResult.Item(message: m,
                                                          decision: .none(bannedReason(m))))
        }
        state.queue.removeAll { settings.bannedSenders.contains($0.userID) }
        // A message stamped ahead of this clock is age 0, not too young for a flush at 0.
        let stale = state.queue.last.map {
            max(0, CommsTS.number(now) - ts($0)) >= settings.flushAfterSecs * 1_000_000
        } ?? false
        guard state.queue.count >= settings.batchSize || stale else {
            state.save(in: dir)
            return
        }
        let messages = Array(state.queue.prefix(settings.batchSize))
        state.queue.removeFirst(messages.count)
        let at = state.undelivered.count
        state.undelivered += messages.map {
            CommsTickResult.Item(message: $0, decision: .none(interrupted))
        }
        state.save(in: dir)

        let batch = CommsBatch(messages: messages,
                               context: context(for: messages, in: dir, settings: settings,
                                                assets: assets))
        var decisions = triage(batch, settings: settings, classify)
        if messages.count > 1, decisions.contains(where: { $0.action == .injection }) {
            decisions = messages.map { m in
                triage(CommsBatch(messages: [m],
                                  context: batch.context.filter { $0.channelID == m.channelID }),
                       settings: settings, classify)[0]
            }
        }
        state.undelivered.replaceSubrange(at..., with: zip(messages, decisions).flatMap {
            CommsTickResult.Item.targets($0, $1, maxTargets: assets.maxTargets)
        })
        for decision in decisions where decision.numbers.count > assets.maxTargets {
            let left = decision.targets.dropFirst(assets.maxTargets).map(\.ref)
            notice("A Slack message named more than the \(assets.maxTargets) a message can act on, "
                   + "so nothing was done for \(left.joined(separator: ", ")).")
        }
        state.classifierError = decisions.first {
            $0.reason.hasPrefix(CommsClassifier.failurePrefix)
        }?.reason
        state.save(in: dir)
    }

    /// One answer per message of `batch`, each `checked` against it and what came before
    /// it; a missing answer is a `none`.
    private static func triage(_ batch: CommsBatch, settings: CommsSettings,
                               _ classify: (CommsBatch) -> [CommsDecision]) -> [CommsDecision] {
        let answers = classify(batch)
        return batch.messages.enumerated().map { i, message in
            let answer = i < answers.count ? answers[i]
                : .none("the classifier gave this message no answer")
            let decision = answer.checked(against: message, earlier: batch.earlier(than: message),
                                          settings: settings)
            // The marker is the tick's alone: a reason the model wrote cannot claim it.
            guard decision.reason.hasPrefix(bannedPrefix) else { return decision }
            return CommsDecision(action: decision.action, targets: decision.targets,
                                 summary: decision.summary, reason: "triage: " + decision.reason,
                                 evidence: decision.evidence, language: decision.language)
        }
    }

    /// Per conversation of `messages`, its newest `contextMessages` pings already triaged,
    /// at most `contextHours` older than the oldest of `messages`; oldest first. A banned
    /// sender's, or one triaged as an injection, is left out: its text reaches no model
    /// again.
    public static func context(for messages: [CommsMessage], in dir: URL, settings: CommsSettings,
                        assets: CoreAssets.CommsModel) -> [CommsMessage] {
        let places = Set(messages.map(\.channelID)), ids = Set(messages.map(\.id))
        guard assets.contextMessages > 0,
              let oldest = messages.compactMap({ CommsTS.number($0.ts) }).min() else { return [] }
        let since = oldest - assets.contextHours * 3_600 * 1_000_000
        func order(_ a: CommsMessage, _ b: CommsMessage) -> Bool {
            (CommsTS.number(a.ts) ?? 0, a.id) < (CommsTS.number(b.ts) ?? 0, b.id)
        }
        let earlier = CommsLog.fold(CommsLog.read(in: dir)).compactMap { item -> CommsMessage? in
            guard let d = item.decision, d.action != .injection,
                  !d.reason.hasPrefix(bannedPrefix), !item.message.isExtraTarget,
                  places.contains(item.message.channelID), !ids.contains(item.message.id),
                  !settings.bannedSenders.contains(item.message.userID),
                  let n = CommsTS.number(item.message.ts), n >= since else { return nil }
            return item.message
        }
        return Dictionary(grouping: earlier, by: \.channelID).values
            .flatMap { $0.sorted(by: order).suffix(assets.contextMessages) }
            .sorted(by: order)
    }

    private static func iso(_ ts: Int) -> String {
        CommsLog.isoString(Date(timeIntervalSince1970: TimeInterval(ts / 1_000_000)))
    }

    /// Pages until one reaches the watermark or comes back short. Stopped by
    /// `maxSearchPages`, it returns the oldest ts read: below it, to the watermark, went
    /// unread. Slack's `after` is whole seconds; the exact cut is the caller's.
    private static func collect(_ query: CoreAssets.CommsModel.Query, above watermark: Int,
                                into found: inout [String: SlackMatch], api: SlackAPI,
                                assets: CoreAssets.CommsModel) throws -> Int? {
        var page = 1
        while true {
            let matches = try api.search(query, after: watermark / 1_000_000, page: page,
                                         count: assets.searchCount)
            for m in matches where found[m.id] == nil { found[m.id] = m }
            let oldest = matches.compactMap { CommsTS.number($0.ts) }.min()
            guard matches.count >= assets.searchCount,
                  let oldest, oldest > watermark else { return nil }
            guard page < assets.maxSearchPages else { return oldest }
            page += 1
        }
    }
}
