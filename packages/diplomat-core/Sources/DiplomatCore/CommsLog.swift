import Foundation

/// One line of Comms' own log, `events.jsonl`. Every line shares `at` (ISO 8601),
/// `kind` and `id` (`<channelID>:<ts>`; `""` for an `error`):
///
/// - `ping`     + `message`                         — the tick, per new message, as it hands it over
/// - `decision` + `decision`                        — the tick, right after that ping; one per
///                                                    target, each extra target's id `<id>#<number>`
/// - `action`   + `outcome`, `queueKey`?, `label`?  — the front-end, once it has acted
/// - `started`  + `label`                           — the front-end, when the ask's agent spawned
/// - `finished` + `label`                           — the front-end, when that run left the agent list
/// - `reply`    + `outcome`, `detail`?              — `CommsReplier`: `sending` with the text before it
///                                                    posts, then `sent` or `failed: …`; `skipped: …`
///                                                    for an away question it would not answer
/// - `error`    + `detail`                          — the tick, on going from a good tick to a failing
///                                                    one, and for messages it skipped unread
///
/// The Linux front-end's Python appender writes the same shape: one JSON object per line,
/// appended to a 0600 file while holding `CommsLog.lockURL`.
public struct CommsEvent: Codable, Equatable, Sendable {
    public enum Kind: String, Codable, Sendable {
        case ping, decision, action, started, finished, error, reply
    }

    public var at: Date
    public var kind: Kind
    public var id: String
    public var message: CommsMessage?
    public var decision: CommsDecision?
    public var outcome: String?
    public var queueKey: String?
    public var label: String?
    public var detail: String?

    public init(at: Date = Date(), kind: Kind, id: String, message: CommsMessage? = nil,
                decision: CommsDecision? = nil, outcome: String? = nil,
                queueKey: String? = nil, label: String? = nil, detail: String? = nil) {
        self.at = at
        self.kind = kind
        self.id = id
        self.message = message
        self.decision = decision
        self.outcome = outcome
        self.queueKey = queueKey
        self.label = label
        self.detail = detail
    }
}

/// One Comms-screen row: a ping and everything since.
public struct CommsItem: Codable, Equatable, Sendable {
    public var message: CommsMessage
    public var decision: CommsDecision?
    public var outcome: String?
    public var queueKey: String?
    public var label: String?
    public var startedAt: Date?
    public var finishedAt: Date?
    /// The text Comms wrote back in Slack, and the last `reply` line's outcome; `reply`
    /// is nil for a skipped one.
    public var reply: String?
    public var replyOutcome: String?
    /// When the ping was recorded.
    public var at: Date

    public init(message: CommsMessage, at: Date) {
        self.message = message
        self.at = at
    }
}

/// `dir` holds `state.json` (`CommsWatch`'s watermark), `events.jsonl` (this log),
/// `events.lock` and `reports/` (investigation reports): directories 0700, files 0600.
public enum CommsLog {
    public static var dir: URL {
        if let env = ProcessInfo.processInfo.environment["DIPLOMAT_COMMS_DIR"], !env.isEmpty {
            return URL(fileURLWithPath: env, isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".diplomat/comms", isDirectory: true)
    }

    public static func eventsURL(in dir: URL = dir) -> URL {
        dir.appendingPathComponent("events.jsonl")
    }

    public static func reportsDir(in dir: URL = dir) -> URL {
        dir.appendingPathComponent("reports", isDirectory: true)
    }

    /// Every writer of `events.jsonl`, the Linux Python appender included, holds this
    /// (`flock`, exclusive) around an append or a rotation: a line appended mid-rotation
    /// would land in the replaced file.
    public static func lockURL(in dir: URL = dir) -> URL {
        dir.appendingPathComponent("events.lock")
    }

    static var maxBytes: Int { (try? CoreAssets.comms())?.maxLogBytes ?? 2 << 20 }

    /// Both made 0700 even if they already exist (`PrivateFiles.prepare`).
    public static func prepare(_ dir: URL = dir) {
        PrivateFiles.prepare(dir)
        PrivateFiles.prepare(reportsDir(in: dir))
    }

    /// Under the lock (`PrivateFiles.append`); false when the line is not all on disk.
    @discardableResult
    public static func append(_ event: CommsEvent, in dir: URL = dir) -> Bool {
        guard var line = try? encoder.encode(event) else { return false }
        line.append(UInt8(ascii: "\n"))
        prepare(dir)
        return PrivateFiles.locked(lockURL(in: dir)) {
            PrivateFiles.append(line, to: eventsURL(in: dir), maxBytes: maxBytes)
        } ?? false
    }

    /// Oldest first; a torn or foreign line costs only that line.
    public static func read(in dir: URL = dir) -> [CommsEvent] {
        guard let data = try? Data(contentsOf: eventsURL(in: dir)) else { return [] }
        let d = decoder
        return data.split(separator: UInt8(ascii: "\n")).compactMap {
            try? d.decode(CommsEvent.self, from: Data($0))
        }
    }

    /// The Comms screen's rows, newest ping first: the one projection both front-ends
    /// draw (Linux via `diplomat-core comms-feed`). An event with no ping before it is
    /// dropped, having no message to draw on; a later `action` overwrites an earlier one.
    /// A `decision` for an extra target (`CommsMessage.isExtraTarget`) starts its row, a
    /// copy of its message's beside it.
    public static func fold(_ events: [CommsEvent]) -> [CommsItem] {
        var items: [String: (index: Int, item: CommsItem)] = [:]
        for (i, e) in events.enumerated() {
            switch e.kind {
            case .ping:
                if items[e.id] == nil, let m = e.message { items[e.id] = (i, CommsItem(message: m, at: e.at)) }
            case .decision:
                guard let d = e.decision else { break }
                if items[e.id] == nil,
                   let first = items[CommsMessage.messageID(of: e.id)] {
                    var message = first.item.message
                    message.id = e.id
                    items[e.id] = (first.index, CommsItem(message: message, at: first.item.at))
                }
                items[e.id]?.item.decision = d
            case .action:
                items[e.id]?.item.outcome = e.outcome
                if let k = e.queueKey { items[e.id]?.item.queueKey = k }
                if let l = e.label { items[e.id]?.item.label = l }
            case .started:
                items[e.id]?.item.startedAt = e.at
                if let l = e.label { items[e.id]?.item.label = l }
            case .finished:
                items[e.id]?.item.finishedAt = e.at
            case .reply:
                items[e.id]?.item.replyOutcome = e.outcome
                if let d = e.detail { items[e.id]?.item.reply = d }
            case .error:
                break
            }
        }
        return items.values
            .sorted {
                ($0.item.at, $0.index) == ($1.item.at, $1.index)
                    ? $0.item.message.id < $1.item.message.id
                    : ($0.item.at, $0.index) > ($1.item.at, $1.index)
            }
            .map(\.item)
    }

    // MARK: - JSON

    /// `decoder` takes optional fractional seconds and any offset, so Python's
    /// `datetime.isoformat()` lines read back.
    public static var encoder: JSONEncoder {
        let e = JSONEncoder()
        e.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        e.dateEncodingStrategy = .custom { date, enc in
            var c = enc.singleValueContainer()
            try c.encode(isoString(date))
        }
        return e
    }

    public static var decoder: JSONDecoder {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .custom { dec in
            let c = try dec.singleValueContainer()
            let s = try c.decode(String.self)
            guard let date = parseISO(s) else {
                throw DecodingError.dataCorruptedError(in: c, debugDescription: "not ISO 8601: \(s)")
            }
            return date
        }
        return d
    }

    public static func isoString(_ date: Date) -> String {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f.string(from: date)
    }

    public static func parseISO(_ s: String) -> Date? {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: s) { return d }
        f.formatOptions = [.withInternetDateTime]
        return f.date(from: s)
    }
}
