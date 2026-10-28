import Foundation

/// The triage call: one sandboxed `claude -p` per message, answering a fixed schema.
///
/// The message is untrusted, so the call gets nothing it could be talked into using.
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

    public static func prompt(settings: CommsSettings) -> String {
        guard let c = (try? CoreAssets.comms())?.classifier else { return "" }
        return CommsText.fill(c.instructions, [
            "login": settings.login.isEmpty ? "the operator" : settings.login,
            "owner": settings.owner, "repo": settings.repo,
        ])
    }

    /// The stdin block, quoted between the fence lines. The quoting holds the fence;
    /// `defang` only removes the marker spellings it knows.
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

    public static var schema: String { (try? CoreAssets.comms())?.schema ?? "" }

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

    /// Never hangs past `timeout`; every failure is a `none` whose reason starts with
    /// `failurePrefix`.
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
    /// Taken for triage but not yet handed to a front-end; the next tick hands them over
    /// first.
    public var undelivered: [CommsTickResult.Item]
    /// Per query that overflowed `maxSearchPages`, the oldest ts already reported, so one
    /// shortfall logs one `error` event.
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

    /// A missing or unreadable file reads as a first tick.
    public static func load(in dir: URL = CommsLog.dir) -> CommsState {
        guard let data = try? Data(contentsOf: url(in: dir)),
              let state = try? JSONDecoder().decode(CommsState.self, from: data) else {
            return CommsState()
        }
        return state
    }

    /// Atomic, so a crash mid-write leaves the previous state; 0600, since `undelivered`
    /// holds message text.
    public func save(in dir: URL = CommsLog.dir) {
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys]
        guard let data = try? enc.encode(self) else { return }
        CommsLog.prepare(dir)
        CommsLog.writePrivate(data, to: Self.url(in: dir))
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
    }

    public var me: CommsIdentity?
    public var items: [Item]
    public var error: String?
    /// What this tick could not read (a catch-up past the horizon, a query paged out). Not
    /// a failure, so these go to the activity feed, not `error`.
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

    /// An absent `me` or `error` is written as `null`, so a reader can tell a clean tick
    /// from a truncated answer.
    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(me, forKey: .me)
        try c.encode(items, forKey: .items)
        try c.encode(error, forKey: .error)
        try c.encode(notices, forKey: .notices)
    }
}

public enum CommsWatch {
    static let interrupted = "the tick ended before its triage answered, so it was not triaged"

    public static let noToken = "no Slack token is configured"

    /// Starts the reason of the `none` a banned sender's message gets in place of a
    /// triage: its text never reaches a model.
    public static let bannedPrefix = "sender banned: "

    static func bannedReason(_ message: CommsMessage) -> String {
        bannedPrefix + "\(message.sender) is on the prompt-injection ban list, so this was "
            + "not triaged"
    }

    /// The first tick only learns who the token is and sets the watermark to `now`, so
    /// turning Comms on triages no backlog; a watermark older than `catchUpHours` likewise
    /// moves up to `now` less `indexLagSecs`, and one `error` event names the skipped
    /// stretch. Otherwise the `comms.json` queries run, matches at or below the watermark
    /// or already `seen` drop, and the oldest `maxPerTick` are triaged. A query overflowing
    /// `maxSearchPages` is read newest first, and one `error` event names what went unread.
    ///
    /// A message from one of `settings.bannedSenders` is taken like any other but never
    /// triaged: its decision is a `none` whose reason starts with `bannedPrefix`.
    ///
    /// Each message is saved as taken before its triage runs, so a tick that never returns
    /// hands it over on the next one (triaged, or as an interrupted `none`) and never
    /// triages it twice. `deliver` sees the result before anything is logged or let go; a
    /// front-end in another process (`comms-tick`) answers false when the result did not
    /// reach it, and the messages wait for the next tick the same way.
    ///
    /// The watermark trails `now` by `indexLagSecs` because Slack's search index can return
    /// a message well after it was posted; `seen` stops one in that window being triaged
    /// twice. With pings left over past `maxPerTick`, it stops short of the first one.
    ///
    /// A Slack failure leaves the watermark in place and is the result's `error`; so is the
    /// last triage's failure, until one succeeds. An `error` event is logged only on the
    /// tick that goes from working to failing.
    public static func tick(token: String, settings: CommsSettings, now: Date = Date(),
                            dir: URL = CommsLog.dir, api: SlackAPI? = nil,
                            deliver: (CommsTickResult) -> Bool = { _ in true },
                            classify: (CommsMessage) -> CommsDecision) -> CommsTickResult {
        var state = CommsState.load(in: dir)

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
            // Not cached: a swapped token must not leave the previous owner's id deciding
            // which messages are "my own".
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
            if settings.bannedSenders.contains(message.userID) {
                state.undelivered[state.undelivered.count - 1].decision =
                    .none(bannedReason(message))
                state.save(in: dir)
                continue
            }
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

    /// Pages until one reaches the watermark, comes back short, or is the last. Stopped by
    /// `maxSearchPages`, it returns the oldest ts read: below it, to the watermark, went
    /// unread.
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
