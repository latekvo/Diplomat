import Foundation

// A lightweight, cross-process action log shown in the panel. Every action — whether the
// user triggered it from the panel or the applet dispatched it automatically — appends
// one JSON line to ~/.argent/pr-monitor/audit.jsonl. The daemon appends here too (bans /
// terminations), so the panel shows a single unified activity feed. Appends use a real
// O_APPEND file descriptor, which keeps small concurrent writes atomic across processes
// (FileHandle's seek-then-write is NOT — it raced the daemon's appends).

struct AuditEntry: Codable, Equatable, Identifiable {
    let at: String        // ISO8601
    let source: String    // "panel" (user) | "auto" (monitor) | "agent" (agent-reported)
    let action: String    // short verb: review, resolve, audit, review-req, nudge, kill-device, unban, ban
    let detail: String

    var id: String { at + "\u{1F}" + action + "\u{1F}" + detail }
    var date: Date? { AuditLog.parseDate(at) }
}

enum AuditLog {
    static var dir: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".argent/pr-monitor")
    }
    static var fileURL: URL { dir.appendingPathComponent("audit.jsonl") }

    private static let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter(); return f
    }()
    /// The daemon stamps with JS `toISOString()`, which carries fractional seconds —
    /// a plain ISO8601DateFormatter rejects those, so every daemon row silently lost
    /// its time column. Cached (formatters are expensive to build per access).
    private static let isoFractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    static func parseDate(_ s: String) -> Date? {
        iso.date(from: s) ?? isoFractional.date(from: s)
    }

    /// Append one action. Best-effort; never throws into the caller.
    static func log(_ source: String, _ action: String, _ detail: String) {
        let entry: [String: String] = ["at": iso.string(from: Date()), "source": source,
                                       "action": action, "detail": detail]
        guard let data = try? JSONSerialization.data(withJSONObject: entry),
              var line = String(data: data, encoding: .utf8) else { return }
        line += "\n"
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        // O_APPEND: the kernel appends atomically, so a concurrent daemon append can't
        // be overwritten; O_CREAT closes the create/exists race too.
        let fd = open(fileURL.path, O_WRONLY | O_APPEND | O_CREAT, 0o644)
        guard fd >= 0 else { return }
        defer { close(fd) }
        _ = line.data(using: .utf8)?.withUnsafeBytes { buf in
            write(fd, buf.baseAddress, buf.count)
        }
    }

    /// The most recent `limit` entries, newest first. Reads only the file's tail —
    /// the log grows forever (nothing rotates it) and this runs on the panel's 8s
    /// poll, so a full-file read would eventually hitch the UI by construction.
    static func read(limit: Int = 200) -> [AuditEntry] {
        guard let h = try? FileHandle(forReadingFrom: fileURL) else { return [] }
        defer { try? h.close() }
        let tailBytes: UInt64 = 256 * 1024   // generously > 200 lines
        let size = (try? h.seekToEnd()) ?? 0
        let start = size > tailBytes ? size - tailBytes : 0
        try? h.seek(toOffset: start)
        guard var data = try? h.readToEnd() else { return [] }
        // A mid-file start lands mid-line — and possibly mid-UTF-8-character, which
        // strict String decoding rejects WHOLESALE (the audit lines are full of
        // multibyte "·"/"≥"). Drop up to the first newline on the raw BYTES, before
        // decoding, so a partial leading sequence can't blank the entire feed.
        if start > 0, let nl = data.firstIndex(of: UInt8(ascii: "\n")) {
            data = data.subdata(in: data.index(after: nl)..<data.endIndex)
        }
        guard let text = String(data: data, encoding: .utf8) else { return [] }
        let dec = JSONDecoder()
        let entries = text.split(whereSeparator: \.isNewline)
            .suffix(limit)
            .compactMap { try? dec.decode(AuditEntry.self, from: Data($0.utf8)) }
        return entries.reversed()
    }
}
