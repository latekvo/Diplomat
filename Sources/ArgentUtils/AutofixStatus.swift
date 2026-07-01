import Foundation

// The PR auto-fix monitor runs OUTSIDE the applet (a Claude session watching my open
// PRs and dispatching conflict-resolve / review-fix agents). It writes a heartbeat to
// ~/.argent/pr-monitor/status.json each tick; the applet reads it to show whether
// auto-fixing is actually live — the "active" pill only lights up on a FRESH heartbeat,
// so it never claims active when nothing is running. The Settings toggle writes
// control.json, which the monitor honors, so turning it off truly pauses dispatching.

struct AutofixStatus: Decodable, Equatable {
    var updatedAt: Date?
    var enabled: Bool
    var watching: Int
    var conflictsResolved: Int
    var reviewsAddressed: Int

    /// A fresh heartbeat ⇒ the monitor process is alive right now. The tick cadence is
    /// ~10 min; allow 2.5× before we call it offline.
    var isLive: Bool {
        guard let updatedAt else { return false }
        return Date().timeIntervalSince(updatedAt) < 25 * 60
    }

    /// Total agents the monitor has dispatched this session (conflicts + reviews).
    var totalFixed: Int { conflictsResolved + reviewsAddressed }

    enum CodingKeys: String, CodingKey {
        case updatedAt, enabled, watching, conflictsResolved, reviewsAddressed
    }

    init(updatedAt: Date?, enabled: Bool, watching: Int,
         conflictsResolved: Int, reviewsAddressed: Int) {
        self.updatedAt = updatedAt
        self.enabled = enabled
        self.watching = watching
        self.conflictsResolved = conflictsResolved
        self.reviewsAddressed = reviewsAddressed
    }

    /// Tolerant decode: any missing field defaults rather than failing the whole read.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        enabled = (try? c.decode(Bool.self, forKey: .enabled)) ?? true
        watching = (try? c.decode(Int.self, forKey: .watching)) ?? 0
        conflictsResolved = (try? c.decode(Int.self, forKey: .conflictsResolved)) ?? 0
        reviewsAddressed = (try? c.decode(Int.self, forKey: .reviewsAddressed)) ?? 0
        if let s = try? c.decode(String.self, forKey: .updatedAt) {
            updatedAt = ISO8601DateFormatter().date(from: s)
        }
    }
}

enum Autofix {
    static var dir: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".argent/pr-monitor")
    }
    static var statusURL: URL { dir.appendingPathComponent("status.json") }
    static var controlURL: URL { dir.appendingPathComponent("control.json") }

    static func readStatus() -> AutofixStatus? {
        guard let data = try? Data(contentsOf: statusURL) else { return nil }
        return try? JSONDecoder().decode(AutofixStatus.self, from: data)
    }

    /// Write the enable flag the monitor reads before dispatching. Best-effort.
    static func writeEnabled(_ enabled: Bool) {
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let json = "{\"enabled\":\(enabled ? "true" : "false")}\n"
        try? json.data(using: .utf8)?.write(to: controlURL)
    }
}
