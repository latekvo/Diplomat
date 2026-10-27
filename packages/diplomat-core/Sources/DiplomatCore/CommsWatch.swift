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
/// argument, and everything taken from the message (`input`) arrives on stdin, fenced
/// and quoted.
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

    /// The stdin block: the message and what is known about it, quoted between the fence
    /// lines, with any copy of the fence marker inside it defanged.
    public static func input(for message: CommsMessage, settings: CommsSettings) -> String {
        guard let c = (try? CoreAssets.comms())?.classifier else { return "" }
        let reasons = message.reasons.map { r -> String in
            switch r {
            case .dm:      return "DM"
            case .mention: return "@mention"
            case .keyword: return "keyword \"\(message.keyword ?? "")\""
            }
        }.joined(separator: ", ")
        return CommsText.fillFenced(c.input, [
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
        let found = LoginShell.locate("claude", shell: shell, environment: childEnvironment)
            ?? LoginShell.onPath("claude")
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

    /// This process's environment without `DIPLOMAT_SLACK_TOKEN`: what every child the
    /// watcher starts gets (`claude`, the login shell that locates it, and curl, which
    /// is handed the token on stdin).
    static var childEnvironment: [String: String] {
        var env = ProcessInfo.processInfo.environment
        env["DIPLOMAT_SLACK_TOKEN"] = nil
        return env
    }

    /// Run `exe` with `input` as stdin, waiting at most `timeout`; past it the process is
    /// stopped as `GH.run` stops one (terminate, then SIGKILL). stdin, stdout and stderr
    /// are files: a pipe the child fills blocks it, and corelibs only sees an exit once
    /// every holder of the launch socketpair has gone, so the wait is on a semaphore the
    /// termination handler signals rather than `waitUntilExit`.
    ///
    /// Those files hold the Slack token (curl's config) and DM text, and off macOS the
    /// temp dir is usually the shared /tmp, so they go in a fresh 0700 directory, each
    /// one 0600 from the moment it exists.
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

    /// A new file at `path`, 0600 from its creation, holding `contents`, open at its start.
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
    /// The last triage's failure (`CommsClassifier.failurePrefix`), nil once one
    /// succeeds: a tick with nothing to triage still ends on it.
    public var classifierError: String?
    /// Messages taken for triage and not yet handed to a front-end: the tick that took
    /// them did not return, or its result did not reach the front-end. The next tick
    /// hands them over first.
    public var undelivered: [CommsTickResult.Item]
    /// Per query whose matches did not fit in `maxSearchPages`, the oldest match's ts
    /// already reported, so one shortfall logs one `error` event.
    public var gaps: [String: String]

    public init(watermark: String? = nil, seen: [String] = [], me: CommsIdentity? = nil,
                error: String? = nil, classifierError: String? = nil,
                undelivered: [CommsTickResult.Item] = [], gaps: [String: String] = [:]) {
        self.watermark = watermark
        self.seen = seen
        self.me = me
        self.error = error
        self.classifierError = classifierError
        self.undelivered = undelivered
        self.gaps = gaps
    }

    private enum CodingKeys: String, CodingKey {
        case watermark, seen, me, error, classifierError, undelivered, gaps
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(watermark: try? c.decodeIfPresent(String.self, forKey: .watermark),
                  seen: (try? c.decodeIfPresent([String].self, forKey: .seen)) ?? [],
                  me: try? c.decodeIfPresent(CommsIdentity.self, forKey: .me),
                  error: try? c.decodeIfPresent(String.self, forKey: .error),
                  classifierError: try? c.decodeIfPresent(String.self, forKey: .classifierError),
                  undelivered: (try? c.decodeIfPresent([CommsTickResult.Item].self,
                                                       forKey: .undelivered)) ?? [],
                  gaps: (try? c.decodeIfPresent([String: String].self, forKey: .gaps)) ?? [:])
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

    /// Written whole and atomically, so a crash mid-write leaves the previous state, and
    /// 0600, since `undelivered` holds message text.
    public func save(in dir: URL = CommsLog.dir) {
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys]
        guard let data = try? enc.encode(self) else { return }
        CommsLog.prepare(dir)
        CommsLog.writePrivate(data, to: Self.url(in: dir))
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
    /// What this tick could not read (a catch-up past the horizon, a query paged out): the
    /// tick still succeeded, so these go to the activity feed rather than the error.
    public var notices: [String]

    public init(me: CommsIdentity?, items: [Item], error: String?, notices: [String] = []) {
        self.me = me
        self.items = items
        self.error = error
        self.notices = notices
    }

    private enum CodingKeys: String, CodingKey { case me, items, error, notices }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        me = try c.decodeIfPresent(CommsIdentity.self, forKey: .me)
        items = try c.decode([Item].self, forKey: .items)
        error = try c.decodeIfPresent(String.self, forKey: .error)
        notices = try c.decodeIfPresent([String].self, forKey: .notices) ?? []
    }

    /// `me` and `error` are written as `null` when absent, so a reader can tell a
    /// clean tick from a truncated answer.
    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(me, forKey: .me)
        try c.encode(items, forKey: .items)
        try c.encode(error, forKey: .error)
        try c.encode(notices, forKey: .notices)
    }
}

/// One poll of Slack: search, filter, triage, log.
public enum CommsWatch {
    /// The decision a message carries when the tick that took it ended inside its triage.
    static let interrupted = "the tick ended before its triage answered, so it was not triaged"

    /// The error a tick, or `diplomat-core comms-auth`, answers an empty token with.
    public static let noToken = "no Slack token is configured"

    /// Run one tick against the state in `dir`.
    ///
    /// The first tick only learns who the token is and sets the watermark to `now`: an
    /// operator turning Comms on is not asked to triage a backlog. Nor is one turning it
    /// back on: a watermark more than `catchUpHours` old moves up to `now` less
    /// `indexLagSecs`, and one `error` event names the stretch that was not triaged.
    /// Every other tick runs the `comms.json` queries from `CommsTS.afterDay`, keeps the
    /// matches `CommsMatch.message` turns into pings, drops those at or below the
    /// watermark or already `seen`, and triages the oldest `maxPerTick` of the rest. A
    /// query with more matches above the watermark than `maxSearchPages` pages hold is
    /// read newest first as far as they go, and one `error` event names the stretch
    /// below that went unread.
    ///
    /// A triaged message is handed over in the result and logged then, as a `ping` and
    /// its `decision` (`CommsDecision.checked` against the message). It is saved as
    /// taken before its triage runs, so a tick that never returns hands it over on the
    /// next one, as triaged or as an interrupted `none`, and never triages it twice.
    /// `deliver` is given the result before anything is logged or let go: a front-end in
    /// another process (`diplomat-core comms-tick`) answers false when the result could
    /// not reach it, and the messages wait for the next tick the same way.
    ///
    /// The watermark trails `now` by `indexLagSecs`, because Slack's search index can
    /// return a message well after it was posted; `seen` is what keeps a message inside
    /// that window from being triaged twice. With more pings waiting than `maxPerTick`,
    /// it stops short of the first one left over, so the next tick picks up there.
    ///
    /// Slack failing (no token, a bad one, a missing scope, a rate limit) leaves the
    /// watermark where it was and is the result's `error`. So is the last triage failing
    /// (`CommsClassifier.failurePrefix`), on every tick until a triage succeeds. An
    /// `error` event is logged only on the tick that goes from working to failing.
    public static func tick(token: String, settings: CommsSettings, now: Date = Date(),
                            dir: URL = CommsLog.dir, api: SlackAPI? = nil,
                            deliver: (CommsTickResult) -> Bool = { _ in true },
                            classify: (CommsMessage) -> CommsDecision) -> CommsTickResult {
        var state = CommsState.load(in: dir)

        /// Hand over and log what was triaged, and save the state with how this tick
        /// ended, logging an `error` on the transition.
        var notices: [String] = []
        func notice(_ text: String) {
            CommsLog.append(CommsEvent(kind: .error, id: "", detail: text), in: dir)
            notices.append(text)
        }
        func finish(error: String?) -> CommsTickResult {
            let error = error ?? state.classifierError
            let result = CommsTickResult(me: state.me, items: state.undelivered, error: error,
                                         notices: notices)
            if deliver(result) {
                for item in result.items {
                    CommsLog.append(CommsEvent(kind: .ping, id: item.message.id, message: item.message),
                                    in: dir)
                    CommsLog.append(CommsEvent(kind: .decision, id: item.message.id,
                                               decision: item.decision), in: dir)
                }
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
        guard !token.isEmpty else { return finish(error: noToken) }
        let api = api ?? SlackAPI(token: token)
        let me: CommsIdentity
        do {
            // Asked every tick rather than cached: a swapped token must not leave the
            // previous owner's id deciding which messages are "my own".
            me = try api.authTest()
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

        let after = CommsTS.afterDay(watermark)
        // Each query with the name an `error` event about it calls its matches by.
        var searches = [
            ("DMs", CommsText.fill(queries.dm, ["after": after])),
            ("mentions", CommsText.fill(queries.mention, ["after": after, "user": me.userID])),
        ]
        for keyword in settings.keywords {
            let k = keyword.replacingOccurrences(of: "\"", with: "")
            searches.append(("matches of \"\(k)\"",
                             CommsText.fill(queries.keyword, ["after": after, "keyword": k])))
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
        let pending = found.values
            .compactMap { CommsMatch.message(from: $0, me: me, settings: settings) }
            .compactMap { m in CommsTS.number(m.ts).map { (n: $0, m: m) } }
            .filter { $0.n > watermark && !seen.contains($0.m.id) }
            .sorted { ($0.n, $0.m.id) < ($1.n, $1.m.id) }
        let take = pending.prefix(settings.maxPerTick)
        let rest = pending.dropFirst(settings.maxPerTick)

        for (_, message) in take {
            state.seen.append(message.id)
            state.undelivered.append(CommsTickResult.Item(message: message,
                                                          decision: .none(interrupted)))
            state.save(in: dir)
            let decision = classify(message).checked(against: message)
            state.undelivered[state.undelivered.count - 1].decision = decision
            state.classifierError = decision.reason.hasPrefix(CommsClassifier.failurePrefix)
                ? decision.reason : nil
            state.save(in: dir)
        }

        let next = rest.first.map { max(watermark, min(lagged, $0.n - 1)) }
            ?? max(watermark, lagged)
        state.watermark = CommsTS.string(next)
        // An id at or below the watermark can never be offered again.
        state.seen = Array(state.seen.filter { id in
            CommsTS.number(String(id.split(separator: ":").last ?? "")).map { $0 > next } ?? false
        }.suffix(assets.seenLimit))
        return finish(error: nil)
    }

    private static func iso(_ ts: Int) -> String {
        CommsLog.isoString(Date(timeIntervalSince1970: TimeInterval(ts / 1_000_000)))
    }

    /// Every match of `query`, newest first, into `found`: pages until one reaches back
    /// to the watermark, comes back short, or is the last. Past `maxSearchPages` it
    /// stops, and returns the oldest match read: everything below it and above the
    /// watermark went unread.
    private static func collect(_ query: String, above watermark: Int,
                                into found: inout [String: SlackMatch], api: SlackAPI,
                                assets: CoreAssets.CommsModel) throws -> Int? {
        var page = 1
        while true {
            let (matches, pages) = try api.search(query: query, page: page,
                                                  count: assets.searchCount)
            for m in matches where found[m.id] == nil { found[m.id] = m }
            let oldest = matches.compactMap { CommsTS.number($0.ts) }.min()
            guard page < pages, matches.count >= assets.searchCount,
                  let oldest, oldest > watermark else { return nil }
            guard page < assets.maxSearchPages else { return oldest }
            page += 1
        }
    }
}
