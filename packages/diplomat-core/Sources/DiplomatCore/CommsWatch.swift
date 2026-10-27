import Foundation

/// The triage call: one `claude -p` per message, sandboxed, answering a fixed schema.
///
/// The message is untrusted text written by somebody else, so the call is given
/// nothing it could be talked into using. `--tools ""` removes the built-in tools, but
/// on its own still loads every MCP server the operator has configured — a Slack one
/// that can post among them — so `--strict-mcp-config` (with no config) drops those,
/// `--disable-slash-commands` the commands, `--setting-sources ""` the settings that
/// could add hooks or permissions back, and `--no-session-persistence` leaves no
/// transcript. What is left answers through `StructuredOutput` and nothing else. The
/// binary is run directly, never through an alias, and in the temp directory, so no
/// project's CLAUDE.md is read either.
///
/// The prompt is split along the trust line: the instructions (`prompt`) are the
/// argument, and everything taken from the message (`input`) arrives on stdin, fenced.
public enum CommsClassifier {
    /// What a decision's reason starts with when no answer came back at all — no
    /// `claude`, a timeout, a crash, an error result. `CommsWatch.tick` reports those as
    /// the tick's error, since every ping after them would be triaged the same way.
    public static let failurePrefix = "classifier failed: "

    /// The instructions, with the repo and the operator filled in.
    public static func prompt(settings: CommsSettings) -> String {
        guard let c = (try? CoreAssets.comms())?.classifier else { return "" }
        return CommsText.fill(c.instructions, [
            "login": settings.login.isEmpty ? "the operator" : settings.login,
            "owner": settings.owner, "repo": settings.repo,
        ])
    }

    /// The stdin block: the message and what is known about it, between the fence lines,
    /// with any copy of the fence marker inside it defanged.
    public static func input(for message: CommsMessage, settings: CommsSettings) -> String {
        guard let c = (try? CoreAssets.comms())?.classifier else { return "" }
        let reasons = message.reasons.map { r -> String in
            switch r {
            case .dm:      return "DM"
            case .mention: return "@mention"
            case .keyword: return "keyword \"\(message.keyword ?? "")\""
            }
        }.joined(separator: ", ")
        return CommsText.fill(c.input, [
            "login": settings.login.isEmpty ? "the operator" : settings.login,
            "sender": CommsText.defang(message.sender),
            "channel": CommsText.defang(message.place),
            "reasons": CommsText.defang(reasons),
            "text": CommsText.defang(message.text),
        ])
    }

    /// The `--json-schema` argument (`comms.json` → `classifier.schema`).
    public static var schema: String { (try? CoreAssets.comms())?.schema ?? "" }

    /// Everything after the binary: the sandbox flags, the schema, the prompt.
    public static func arguments(settings: CommsSettings) -> [String] {
        ["-p", "--model", settings.model,
         "--tools", "",
         "--strict-mcp-config",
         "--disable-slash-commands",
         "--setting-sources", "",
         "--no-session-persistence",
         "--output-format", "json",
         "--json-schema", schema,
         prompt(settings: settings)]
    }

    // Only a found binary is cached, so an install made after a miss is seen.
    private static let lock = NSLock()
    private static var cached: (path: String, at: Date)?
    private static let resolveTTL: TimeInterval = 60

    /// The `claude` executable: `$DIPLOMAT_CLAUDE`, else where the user's login shell
    /// locates it (`LoginShell.locate`, past any alias), else this process's `PATH`.
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
        let found = LoginShell.locate("claude", shell: shell) ?? LoginShell.onPath("claude")
        cached = found.map { ($0, now) }
        return found
    }

    /// Triage `message`. Never throws and never hangs past `timeout` (default
    /// `classifierTimeoutSecs`): every way it can fail is a `none` whose reason starts
    /// with `failurePrefix`.
    public static func run(_ message: CommsMessage, settings: CommsSettings,
                           binary: String? = nil, timeout: TimeInterval? = nil) -> CommsDecision {
        guard let model = try? CoreAssets.comms() else {
            return .none(failurePrefix + "assets/comms.json could not be read")
        }
        guard let exe = binary ?? self.binary() else {
            return .none(failurePrefix + "the claude CLI was not found (set DIPLOMAT_CLAUDE "
                         + "to its path)")
        }
        let budget = timeout ?? model.classifierTimeoutSecs
        switch execute(exe, arguments(settings: settings),
                       input: Data(input(for: message, settings: settings).utf8),
                       timeout: budget) {
        case .failedToStart(let why):
            return .none(failurePrefix + "claude could not be started: \(why)")
        case .timedOut:
            return .none(failurePrefix + "claude did not answer within \(Int(budget))s")
        case .exited(let code, let out, let err):
            if let decision = CommsDecision.answer(in: out) { return decision }
            let tail = String(String(decoding: err, as: UTF8.self)
                .trimmingCharacters(in: .whitespacesAndNewlines).suffix(200))
            return .none(failurePrefix + "claude exited \(code) with no answer"
                         + (tail.isEmpty ? "" : ": \(tail)"))
        }
    }

    enum Outcome {
        case failedToStart(String)
        case timedOut
        case exited(Int32, Data, Data)
    }

    /// Run `exe` with `input` as stdin, waiting at most `timeout`; past it the process is
    /// stopped as `GH.run` stops one (terminate, then SIGKILL). stdin, stdout and stderr
    /// are files: a pipe the child fills blocks it, and corelibs only sees an exit once
    /// every holder of the launch socketpair has gone, so the wait is on a semaphore the
    /// termination handler signals rather than `waitUntilExit`.
    static func execute(_ exe: String, _ args: [String], input: Data,
                        timeout: TimeInterval) -> Outcome {
        let fm = FileManager.default
        let base = fm.temporaryDirectory.appendingPathComponent("diplomat-comms-\(UUID().uuidString)")
        let inURL = base.appendingPathExtension("in")
        let outURL = base.appendingPathExtension("out")
        let errURL = base.appendingPathExtension("err")
        guard fm.createFile(atPath: inURL.path, contents: input, attributes: [.posixPermissions: 0o600]),
              fm.createFile(atPath: outURL.path, contents: nil),
              fm.createFile(atPath: errURL.path, contents: nil),
              let stdin = FileHandle(forReadingAtPath: inURL.path),
              let stdout = FileHandle(forWritingAtPath: outURL.path),
              let stderr = FileHandle(forWritingAtPath: errURL.path) else {
            return .failedToStart("could not stage its stdin")
        }
        defer {
            try? stdin.close()
            try? stdout.close()
            try? stderr.close()
            for url in [inURL, outURL, errURL] { try? fm.removeItem(at: url) }
        }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: exe)
        proc.arguments = args
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
                       (try? Data(contentsOf: outURL)) ?? Data(),
                       (try? Data(contentsOf: errURL)) ?? Data())
    }
}

/// What the tick remembers between runs (`state.json` in `CommsLog.dir`).
public struct CommsState: Codable, Equatable, Sendable {
    /// The Slack ts at or below which nothing is looked at again. Nil until the first tick.
    public var watermark: String?
    /// Ids already turned into pings above the watermark, newest last.
    public var seen: [String]
    /// `auth.test`'s answer on the last tick that reached Slack.
    public var me: CommsIdentity?
    /// The failure the last tick ended on, nil after a good one: what makes an `error`
    /// event fire on the transition only.
    public var error: String?

    public init(watermark: String? = nil, seen: [String] = [], me: CommsIdentity? = nil,
                error: String? = nil) {
        self.watermark = watermark
        self.seen = seen
        self.me = me
        self.error = error
    }

    private enum CodingKeys: String, CodingKey { case watermark, seen, me, error }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(watermark: try? c.decodeIfPresent(String.self, forKey: .watermark),
                  seen: (try? c.decodeIfPresent([String].self, forKey: .seen)) ?? [],
                  me: try? c.decodeIfPresent(CommsIdentity.self, forKey: .me),
                  error: try? c.decodeIfPresent(String.self, forKey: .error))
    }

    public static func url(in dir: URL = CommsLog.dir) -> URL {
        dir.appendingPathComponent("state.json")
    }

    /// The saved state; a missing or unreadable file reads as a first tick.
    public static func load(in dir: URL = CommsLog.dir) -> CommsState {
        guard let data = try? Data(contentsOf: url(in: dir)),
              let state = try? JSONDecoder().decode(CommsState.self, from: data) else {
            return CommsState()
        }
        return state
    }

    /// Written whole and atomically, so a crash mid-write leaves the previous state.
    public func save(in dir: URL = CommsLog.dir) {
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys]
        guard let data = try? enc.encode(self) else { return }
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try? data.write(to: Self.url(in: dir), options: .atomic)
    }
}

/// What one tick found: who the token is, the new pings with their triage, and what
/// went wrong (Slack, or the classifier), if anything.
public struct CommsTickResult: Codable, Equatable, Sendable {
    public struct Item: Codable, Equatable, Sendable {
        public var message: CommsMessage
        public var decision: CommsDecision

        public init(message: CommsMessage, decision: CommsDecision) {
            self.message = message
            self.decision = decision
        }
    }

    public var me: CommsIdentity?
    public var items: [Item]
    public var error: String?

    public init(me: CommsIdentity?, items: [Item], error: String?) {
        self.me = me
        self.items = items
        self.error = error
    }

    private enum CodingKeys: String, CodingKey { case me, items, error }

    /// `me` and `error` are written as `null` when absent, so a reader can tell a
    /// clean tick from a truncated answer.
    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(me, forKey: .me)
        try c.encode(items, forKey: .items)
        try c.encode(error, forKey: .error)
    }
}

/// One poll of Slack: search, filter, triage, log.
public enum CommsWatch {
    /// Run one tick against the state in `dir`.
    ///
    /// The first tick only learns who the token is and sets the watermark to `now`: an
    /// operator turning Comms on is not asked to triage a backlog. Every later tick runs
    /// the `comms.json` queries from the day before the watermark, keeps the matches
    /// `CommsMatch.message` turns into pings, drops those at or below the watermark or
    /// already `seen`, and triages the oldest `maxPerTick` of the rest, each logged as a
    /// `ping` before `classify` is called and a `decision` after.
    ///
    /// The watermark trails `now` by `indexLagSecs`, because Slack's search index can
    /// return a message well after it was posted; `seen` is what keeps a message inside
    /// that window from being triaged twice. With more pings waiting than `maxPerTick`,
    /// it stops short of the first one left over, so the next tick picks up there.
    ///
    /// Slack failing (no token, a bad one, a missing scope, a rate limit) leaves the
    /// watermark where it was and is the result's `error`; so is the classifier failing
    /// (`CommsClassifier.failurePrefix`). An `error` event is logged only on the tick
    /// that goes from working to failing.
    public static func tick(token: String, settings: CommsSettings, now: Date = Date(),
                            dir: URL = CommsLog.dir, api: SlackAPI? = nil,
                            classify: (CommsMessage) -> CommsDecision) -> CommsTickResult {
        var state = CommsState.load(in: dir)

        /// Save the state with how this tick ended, logging an `error` on the transition.
        func finish(_ items: [CommsTickResult.Item], error: String?) -> CommsTickResult {
            if let error, state.error == nil {
                CommsLog.append(CommsEvent(at: now, kind: .error, id: "", detail: error), in: dir)
            }
            state.error = error
            state.save(in: dir)
            return CommsTickResult(me: state.me, items: items, error: error)
        }

        guard let assets = try? CoreAssets.comms() else {
            return finish([], error: "assets/comms.json could not be read")
        }
        guard !token.isEmpty else { return finish([], error: "no Slack token is configured") }
        let api = api ?? SlackAPI(token: token)
        let me: CommsIdentity
        do {
            // Asked every tick rather than cached: a swapped token must not leave the
            // previous owner's id deciding which messages are "my own".
            me = try api.authTest()
            state.me = me
        } catch {
            return finish([], error: error.localizedDescription)
        }
        guard let watermark = state.watermark.flatMap(CommsTS.number) else {
            state.watermark = CommsTS.string(CommsTS.number(now))
            return finish([], error: nil)
        }
        let queries = assets.queries

        let after = CommsTS.afterDay(watermark)
        var searches = [CommsText.fill(queries.dm, ["after": after]),
                        CommsText.fill(queries.mention, ["after": after, "user": me.userID])]
        for keyword in settings.keywords {
            let k = keyword.replacingOccurrences(of: "\"", with: "")
            searches.append(CommsText.fill(queries.keyword, ["after": after, "keyword": k]))
        }
        var found: [String: SlackMatch] = [:]
        do {
            for query in searches {
                try collect(query, above: watermark, into: &found, api: api, assets: assets)
            }
        } catch {
            return finish([], error: error.localizedDescription)
        }

        let seen = Set(state.seen)
        let pending = found.values
            .compactMap { CommsMatch.message(from: $0, me: me, settings: settings) }
            .compactMap { m in CommsTS.number(m.ts).map { (n: $0, m: m) } }
            .filter { $0.n > watermark && !seen.contains($0.m.id) }
            .sorted { ($0.n, $0.m.id) < ($1.n, $1.m.id) }
        let take = pending.prefix(settings.maxPerTick)
        let rest = pending.dropFirst(settings.maxPerTick)

        var items: [CommsTickResult.Item] = []
        var classifierError: String?
        for (_, message) in take {
            CommsLog.append(CommsEvent(at: now, kind: .ping, id: message.id, message: message), in: dir)
            // Recorded before the triage runs: a crash inside it costs this ping its
            // decision, never a second ping.
            state.seen.append(message.id)
            state.save(in: dir)
            let decision = classify(message)
            CommsLog.append(CommsEvent(at: now, kind: .decision, id: message.id,
                                       decision: decision), in: dir)
            if decision.reason.hasPrefix(CommsClassifier.failurePrefix) {
                classifierError = classifierError ?? decision.reason
            }
            items.append(CommsTickResult.Item(message: message, decision: decision))
        }

        let lagged = CommsTS.number(now) - assets.indexLagSecs * 1_000_000
        let next = rest.first.map { max(watermark, min(lagged, $0.n - 1)) }
            ?? max(watermark, lagged)
        state.watermark = CommsTS.string(next)
        // An id at or below the watermark can never be offered again.
        state.seen = Array(state.seen.filter { id in
            CommsTS.number(String(id.split(separator: ":").last ?? "")).map { $0 > next } ?? false
        }.suffix(assets.seenLimit))
        return finish(items, error: classifierError)
    }

    /// Every match of `query`, newest first, into `found`: pages until one reaches back
    /// to the watermark, comes back short, or `maxSearchPages` have been read.
    private static func collect(_ query: String, above watermark: Int,
                                into found: inout [String: SlackMatch], api: SlackAPI,
                                assets: CoreAssets.CommsModel) throws {
        var page = 1
        while true {
            let (matches, pages) = try api.search(query: query, page: page,
                                                  count: assets.searchCount)
            for m in matches where found[m.id] == nil { found[m.id] = m }
            let oldest = matches.compactMap { CommsTS.number($0.ts) }.min()
            guard page < min(pages, assets.maxSearchPages), matches.count >= assets.searchCount,
                  let oldest, oldest > watermark else { return }
            page += 1
        }
    }
}
