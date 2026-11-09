import Foundation

/// The remote-machines daemon's files as the Machines screens read them
/// (`packages/remote-machines`): the snapshot (`state.json`), the usage history
/// (`history.json`), and the operator's exclusions in config.json. Decode and pure logic
/// only, so it is checked by the core smoke test on Linux too.

// MARK: - state.json

public struct RemoteMachine: Decodable, Equatable, Identifiable {
    public struct Load: Decodable, Equatable {
        public let last1m: Double?
        public let last5m: Double?
        public let last15m: Double?
    }
    public struct Memory: Decodable, Equatable {
        public let totalBytes: Double?
        public let availableBytes: Double?
        /// `normal`, `warning` or `critical` on macOS; null elsewhere.
        public let pressure: String?
    }
    public struct Disk: Decodable, Equatable {
        public let totalBytes: Double?
        public let freeBytes: Double?
    }
    public struct GPU: Decodable, Equatable { public let name: String? }
    public struct OS: Decodable, Equatable {
        public let name: String?
        public let kernel: String?
        public let arch: String?
    }
    public struct CPU: Decodable, Equatable {
        public let model: String?
        public let perfCores: Double?
    }
    /// `until` nil = excluded for good. `by`: the settings entries that name this machine,
    /// which can be a name it is reached by under neither its target nor its aliases.
    public struct Exclusion: Decodable, Equatable {
        public let until: String?
        public let by: [String]

        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            until = try c.decodeIfPresent(String.self, forKey: .until)
            by = (try? c.decodeIfPresent([String].self, forKey: .by)) ?? []
        }

        private enum CodingKeys: String, CodingKey { case until, by }
    }
    /// The fractions the daemon ranks and warns by: `cpu` is the 1-minute load per core
    /// (above 1 when overloaded), `memory` the share of RAM not available.
    public struct Usage: Decodable, Equatable {
        public let cpu: Double?
        public let memory: Double?
    }

    public let id: String
    /// nil for this machine (the snapshot's `local`).
    public let target: String?
    public let aliases: [String]?
    public let hostname: String?
    public let user: String?
    public let port: Double?
    public let sources: [String]?
    public let status: String
    public let error: String?
    public let machineName: String?
    public let cores: Double?
    public let sshSessions: Double?
    public let load: Load?
    public let memory: Memory?
    public let disk: Disk?
    public let gpus: [GPU]?
    public let os: OS?
    public let cpu: CPU?
    public let probedAt: String?
    public let excluded: Exclusion?
    /// Only while `status` is `ok`.
    public let spareCores: Double?
    public let usage: Usage?
    public let warnings: [String]?

    /// What the operator's exclusions match it by.
    public var names: [String] { (target.map { [$0] } ?? []) + (aliases ?? []) + (excluded?.by ?? []) }
    public var isExcludedForGood: Bool { excluded != nil && excluded?.until == nil }
    public var pausedUntil: Date? { excluded?.until.flatMap(MachinesSnapshot.date) }

    /// Why a machine the daemon tried could not be read; nil when it was, or was not tried.
    public var problem: String? {
        status == "ok" || status == "excluded" ? nil : "⚠ \(status): \(error ?? "not read")"
    }
}

public struct MachinesSnapshot: Decodable, Equatable {
    public struct Daemon: Decodable, Equatable {
        public let pid: Double?
        public let startedAt: String?
        /// Agent sessions attached.
        public let sessions: Double?
    }

    public let updatedAt: String?
    public let discoveredAt: String?
    public let probeIntervalMs: Double?
    /// Absent from an older daemon's snapshot.
    public let daemon: Daemon?
    public let local: RemoteMachine?
    /// Best first: the load-balancing order.
    public let machines: [RemoteMachine]
    /// Paused machines first, then the ones excluded for good.
    public let excluded: [RemoteMachine]
    public let unavailable: [RemoteMachine]

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        func string(_ k: CodingKeys) -> String? { (try? c.decodeIfPresent(String.self, forKey: k)) ?? nil }
        func rows(_ k: CodingKeys) -> [RemoteMachine] {
            ((try? c.decodeIfPresent([Lossy<RemoteMachine>].self, forKey: k)) ?? nil)?
                .compactMap(\.value) ?? []
        }
        updatedAt = string(.updatedAt)
        discoveredAt = string(.discoveredAt)
        probeIntervalMs = (try? c.decodeIfPresent(Double.self, forKey: .probeIntervalMs)) ?? nil
        daemon = (try? c.decodeIfPresent(Daemon.self, forKey: .daemon)) ?? nil
        local = (try? c.decodeIfPresent(RemoteMachine.self, forKey: .local)) ?? nil
        machines = rows(.machines)
        excluded = rows(.excluded)
        unavailable = rows(.unavailable)
    }

    private enum CodingKeys: String, CodingKey {
        case updatedAt, discoveredAt, probeIntervalMs, daemon, local, machines, excluded, unavailable
    }

    private struct Lossy<T: Decodable>: Decodable {
        let value: T?
        init(from decoder: Decoder) throws { value = try? T(from: decoder) }
    }

    /// An ISO-8601 instant, as JS `toISOString()` writes it or without the milliseconds.
    public static func date(_ s: String) -> Date? {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: s) { return d }
        f.formatOptions = [.withInternetDateTime]
        return f.date(from: s)
    }

    /// `ISO8601-UTC.mmmZ`, the form the daemon writes and reads.
    public static func isoString(_ d: Date) -> String {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f.string(from: d)
    }

    // MARK: unavailable

    static let statusWords: [String: String] = [
        "auth-failed": "SSH refused our keys",
        "unreachable": "not answering",
        "untrusted": "host key never accepted",
        "key-changed": "host key changed since it was accepted",
        "proxied": "behind a ProxyJump or ProxyCommand (not probed)",
        "error": "probe failed",
    ]

    /// The `unavailable` list as `packages/remote-machines/src/format.js` words it: named
    /// per status, except an address only known_hosts remembers (usually a lease long
    /// handed on) and an untrusted mDNS-only advert, which are counted. The latter's name
    /// is chosen by whatever peer advertises it, so it is never shown.
    public func unavailableSummary(maxNames: Int = 8) -> [String] {
        var groups: [(status: String, targets: [String])] = []
        var stale = 0, strangers = 0
        for u in unavailable {
            let only = { (src: String) in u.sources == [src] }
            if u.status == "unreachable" && only("known-hosts") { stale += 1 }
            else if u.status == "untrusted" && only("mdns") { strangers += 1 }
            else if let i = groups.firstIndex(where: { $0.status == u.status }) {
                groups[i].targets.append(u.target ?? u.id)
            } else {
                groups.append((u.status, [u.target ?? u.id]))
            }
        }
        var parts = groups.map { g -> String in
            let more = g.targets.count > maxNames ? " and \(g.targets.count - maxNames) more" : ""
            return "\(Self.statusWords[g.status] ?? g.status): "
                + g.targets.prefix(maxNames).joined(separator: ", ") + more
        }
        if stale > 0 { parts.append("\(stale) old known_hosts addresses not answering") }
        if strangers > 0 {
            parts.append("\(strangers) other machines advertising SSH on the network, never vetted")
        }
        return parts
    }

    /// `format.js`'s sentence for the machines no panel shows, or nil when there are none.
    public func unavailableLine() -> String? {
        let parts = unavailableSummary()
        guard !parts.isEmpty else { return nil }
        return "Not usable (\(unavailable.count)) - " + parts.joined(separator: "; ") + "."
    }
}

/// The Machines screens' wording, rounding half up like the daemon's JS `Math.round`.
public enum MachinesText {
    static func halfUp(_ x: Double) -> Int { clampedInt((x + 0.5).rounded(.down)) }

    /// `12 s ago`, `14 min ago`, `3 h ago`, `2 d ago`.
    public static func ago(seconds: Double) -> String {
        let s = max(0, halfUp(seconds))
        if s < 120 { return "\(s) s ago" }
        if s < 7200 { return "\(halfUp(Double(s) / 60)) min ago" }
        if s < 172_800 { return "\(halfUp(Double(s) / 3600)) h ago" }
        return "\(halfUp(Double(s) / 86400)) d ago"
    }

    public static func percent(_ v: Double) -> String { "\(halfUp(v * 100))%" }

    /// For a snapshot without a `daemon` key, whose writer reads no usage and ignores
    /// exclusions.
    public static let daemonPredatesScreen =
        "The daemon predates this screen; restart it to read usage and apply exclusions."
}

/// Refresh: one POST to the daemon's socket.
public enum MachinesScan {
    public static let endpoint = "scan"
    /// Discovery and every probe, answered once they finish. No `ownerPid`: that would
    /// attach the caller as an agent session and keep the daemon from ever going idle.
    public static let body: [String: Bool] = ["full": true]
    /// As long as the daemon's own client waits for a full scan.
    public static let timeoutSecs = 120
    public static let action = "machines-scan"

    /// `failure` is the daemon's `error`, else `HTTP <status>`, or nil when it succeeded.
    public static func auditText(failure: String?) -> String {
        "Rescanned the remote machines" + (failure.map { " FAILED: \($0)" } ?? "")
    }
    public static let unanswered = "the daemon did not answer"
}

/// How a usage fraction is coloured: `over` where the daemon warns, `high` from 75 %.
public enum UsageTone: Equatable {
    case normal, high, over

    public static func cpu(_ v: Double) -> UsageTone { v > 1 ? .over : v >= 0.75 ? .high : .normal }
    public static func memory(_ v: Double) -> UsageTone { v > 0.9 ? .over : v >= 0.75 ? .high : .normal }
}

// MARK: - history.json

public struct MachineHistory: Decodable, Equatable {
    public struct Sample: Equatable {
        /// Epoch milliseconds.
        public let at: Double
        public let cpu: Double?
        public let memory: Double?
        public let ssh: Double?
    }

    public let intervalMs: Double
    /// Oldest first, keyed by machine id.
    public let machines: [String: [Sample]]

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        intervalMs = (try? c.decode(Double.self, forKey: .intervalMs)) ?? 60_000
        let raw = try c.decode([String: [[Double?]]].self, forKey: .machines)
        machines = raw.mapValues { rows in
            rows.compactMap { r in
                guard let at = r.first ?? nil else { return nil }
                func field(_ i: Int) -> Double? { i < r.count ? r[i] : nil }
                return Sample(at: at, cpu: field(1), memory: field(2), ssh: field(3))
            }
        }
    }

    private enum CodingKeys: String, CodingKey { case intervalMs, machines }

    /// One pixel column of a chart: the mean CPU and RAM and the most SSH sessions of the
    /// samples in it. `breaksBefore` when the gap since the previous sample drawn is long
    /// enough that the daemon was not reading (it runs only while agents are attached).
    public struct Column: Equatable {
        public let index: Int
        public let cpu: Double?
        public let memory: Double?
        public let ssh: Double?
        public let breaksBefore: Bool
    }

    /// `samples` within [from, to] (epoch ms) folded into `width` columns, skipping empty
    /// ones; a gap over `gapMs` between consecutive samples breaks the line.
    public static func columns(_ samples: [Sample], from: Double, to: Double, width: Int,
                               gapMs: Double) -> [Column] {
        guard width > 0, to > from else { return [] }
        var out: [Column] = []
        var lastAt: Double?
        var i = 0
        let inRange = samples.filter { $0.at >= from && $0.at <= to }
        while i < inRange.count {
            let col = min(width - 1, Int(Double(width) * (inRange[i].at - from) / (to - from)))
            let breaks = lastAt.map { inRange[i].at - $0 > gapMs } ?? true
            var cpu: [Double] = [], mem: [Double] = [], ssh: [Double] = []
            while i < inRange.count,
                  min(width - 1, Int(Double(width) * (inRange[i].at - from) / (to - from))) == col {
                let s = inRange[i]
                if let v = s.cpu, v.isFinite { cpu.append(v) }
                if let v = s.memory, v.isFinite { mem.append(v) }
                if let v = s.ssh, v.isFinite { ssh.append(v) }
                lastAt = s.at
                i += 1
            }
            func mean(_ a: [Double]) -> Double? { a.isEmpty ? nil : a.reduce(0, +) / Double(a.count) }
            out.append(Column(index: col, cpu: mean(cpu), memory: mean(mem), ssh: ssh.max(),
                              breaksBefore: breaks))
        }
        return out
    }
}

// MARK: - exclusions (config.json)

/// `remoteMachineExclude` (names out of the pool for good) and `remoteMachineExcludeUntil`
/// (name -> ISO time it stays out until), as the Machines screens edit them.
public struct MachineExclusions: Equatable {
    public enum Action: Equatable {
        case pause(TimeInterval), exclude, resume, include
    }

    /// Same keys on the Linux side.
    public static let excludeKey = "remoteMachineExclude"
    public static let untilKey = "remoteMachineExcludeUntil"

    public var exclude: [String]
    public var until: [String: String]

    public init(exclude: [String], until: [String: String]) {
        self.exclude = exclude
        self.until = until
    }

    /// From a parsed config.json; entries that are not strings are dropped.
    public init(config: [String: Any]) {
        exclude = (config[Self.excludeKey] as? [Any])?.compactMap { $0 as? String } ?? []
        until = (config[Self.untilKey] as? [String: Any])?.compactMapValues { $0 as? String } ?? [:]
    }

    /// The config.json edits that turn `current` into this, one per changed key, each
    /// applied to the file as it then stands and written before the next. The list goes
    /// first, so an exclusion is never briefly lifted between the two writes. An emptied
    /// key is removed.
    public func edits(from current: MachineExclusions) -> [([String: Any]) -> [String: Any]] {
        func setting(_ key: String, _ value: Any, empty: Bool) -> ([String: Any]) -> [String: Any] {
            { config in
                var next = config
                if empty { next.removeValue(forKey: key) } else { next[key] = value }
                return next
            }
        }
        var out: [([String: Any]) -> [String: Any]] = []
        if exclude != current.exclude {
            out.append(setting(Self.excludeKey, exclude, empty: exclude.isEmpty))
        }
        if until != current.until { out.append(setting(Self.untilKey, until, empty: until.isEmpty)) }
        return out
    }

    /// `action` on the machine called `target` (also known as `names`, matched without
    /// case). Every edit also drops the pauses that have run out, keeping a time it cannot
    /// parse, which the daemon's `Date.parse` may still honour.
    public func applying(_ action: Action, target: String, names: [String],
                         now: Date) -> MachineExclusions {
        let lowered = Set((names + [target]).map { $0.lowercased() })
        let isOurs = { (name: String) in lowered.contains(name.lowercased()) }
        var next = self
        switch action {
        case .pause(let d):
            next.until[target] = MachinesSnapshot.isoString(now.addingTimeInterval(d))
        case .exclude:
            if !next.exclude.contains(where: { $0.lowercased() == target.lowercased() }) {
                next.exclude.append(target)
            }
            next.until = next.until.filter { !isOurs($0.key) }
        case .resume:
            next.until = next.until.filter { !isOurs($0.key) }
        case .include:
            next.exclude.removeAll(where: isOurs)
            next.until = next.until.filter { !isOurs($0.key) }
        }
        next.until = next.until.filter { _, at in
            MachinesSnapshot.date(at).map { $0 > now } ?? true
        }
        return next
    }
}
