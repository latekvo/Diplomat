import Foundation
import DiplomatCore

// One prompt-injection ban. Written by the device-allocator daemon's
// report-prompt-injection tool to ~/.diplomat/pr-monitor/banned.json (a cross-process
// file, since any agent on the machine can report). The applet reads it to skip
// auto-reviewing that author's PRs, to skip triaging a banned Slack sender's messages,
// and to show the ban list.
//
// A Slack sender's entry is keyed `slack:<user id>` (`CommsBan.login`), carries
// `source: "slack"` and the sender's `handle`, and its `pr` is the message's permalink.
struct BannedAuthor: Codable, Equatable, Identifiable {
    let login: String
    var reason: String?
    var pr: String?
    var evidence: String?
    var evidenceDir: String?
    var reportedBy: String?
    var at: String?
    var firstAt: String?
    var screenshot: Bool?
    var ghCaptured: Bool?
    var source: String?
    var handle: String?
    /// A Slack ban's `#channel` or `DM`.
    var place: String?

    var id: String { login }

    static let slackSource = "slack"

    /// The Slack user id this entry bans, or nil for a GitHub author.
    var slackUserID: String? { CommsBan.userID(fromLogin: login) }

    var isSlack: Bool { slackUserID != nil }

    /// `@login` for a GitHub author; `@handle` for a Slack sender, or the user id when
    /// no handle was recorded.
    var displayName: String {
        guard let id = slackUserID else { return "@\(login)" }
        let h = (handle ?? "").trimmingCharacters(in: .whitespaces)
        return h.isEmpty ? id : "@\(h)"
    }

    var atDate: Date? { at.flatMap(AuditLog.parseDate) }
    var firstAtDate: Date? { firstAt.flatMap(AuditLog.parseDate) }

    /// The first ban's time, when this entry is a repeat (the daemon keeps `firstAt`).
    var firstBannedIfRepeat: Date? {
        guard let first = firstAtDate, let last = atDate, last.timeIntervalSince(first) >= 1 else {
            return nil
        }
        return first
    }

    /// "just now", "12m ago"; "" with no `at`.
    func ago(now: Date = Date()) -> String {
        guard let at = atDate else { return "" }
        let d = Fmt.duration(now.timeIntervalSince(at))
        return d == "just now" ? d : "\(d) ago"
    }

    static func absolute(_ date: Date) -> String {
        let f = DateFormatter()
        f.dateFormat = "d MMM yyyy, HH:mm:ss"
        return f.string(from: date)
    }

    /// Where the injection was: the PR (a URL, or `owner/repo#N`) or the Slack message.
    var whereURL: URL? {
        guard let pr = pr?.trimmingCharacters(in: .whitespaces), !pr.isEmpty else { return nil }
        if pr.hasPrefix("https://") || pr.hasPrefix("http://") { return URL(string: pr) }
        let parts = pr.split(separator: "#")
        guard parts.count == 2, parts[0].contains("/"), Int(parts[1]) != nil else { return nil }
        return URL(string: "https://github.com/\(parts[0])/pull/\(parts[1])")
    }
}

enum BanList {
    /// The daemon's `BAN_DIR`, relocated by `DA_BAN_DIR` as in `src/paths.js`.
    static var dir: URL {
        if let env = ProcessInfo.processInfo.environment["DA_BAN_DIR"], !env.isEmpty {
            return URL(fileURLWithPath: (env as NSString).expandingTildeInPath)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".diplomat/pr-monitor")
    }

    static var fileURL: URL { dir.appendingPathComponent("banned.json") }

    static func read() -> [BannedAuthor] {
        guard let data = try? Data(contentsOf: fileURL) else { return [] }
        struct Wrap: Decodable { let banned: [BannedAuthor] }
        return (try? JSONDecoder().decode(Wrap.self, from: data))?.banned ?? []
    }

    /// Whether `login` is banned (case-insensitive) in the given list.
    static func isBanned(_ login: String, in list: [BannedAuthor]) -> Bool {
        guard !login.isEmpty else { return false }
        let l = login.lowercased()
        return list.contains { $0.login.lowercased() == l }
    }

    /// The Slack user ids `list` bans: `CommsSettings.bannedSenders`.
    static func slackUserIDs(in list: [BannedAuthor]) -> [String] {
        list.compactMap(\.slackUserID)
    }

    /// Who a dispatch refused for a ban is about, for the feed and the panel.
    static func refusedWho(_ login: String) -> String {
        CommsBan.userID(fromLogin: login) == nil ? "the PR's author" : "the sender"
    }

    /// Remove a ban (the UI's un-ban button). Prefers the daemon's /unban route — the
    /// daemon serializes all ban-list writes in one process, so a concurrent injection
    /// report can't be lost to our read-modify-write (an atomic rename prevents
    /// corruption, but not a lost update). Falls back to a direct atomic rewrite when
    /// no daemon is running. Returns true when the daemon handled it (it also writes
    /// the audit entry then).
    @discardableResult
    static func unban(_ login: String) -> Bool {
        if unbanViaDaemon(login) { return true }
        var list = read()
        let l = login.lowercased()
        // No-op (never rewrite the file) unless that login is actually banned — so a
        // stray/duplicate call can't clobber the list.
        guard list.contains(where: { $0.login.lowercased() == l }) else { return false }
        list.removeAll { $0.login.lowercased() == l }
        _ = write(list)
        return false
    }

    /// POST /unban to the device-allocator daemon (which owns the ban list). True
    /// only when it answered 2xx.
    ///
    /// The socket path comes from `DeviceAllocator`, never a second copy here: a
    /// path that drifted from the daemon's would leave un-banning silently posting
    /// where nothing listens.
    private static func unbanViaDaemon(_ login: String) -> Bool {
        DeviceAllocator.post("unban", body: ["login": login], timeoutSecs: 5)
    }

    @discardableResult
    private static func write(_ list: [BannedAuthor]) -> Bool {
        struct Wrap: Encodable { let banned: [BannedAuthor] }
        let enc = JSONEncoder()
        enc.outputFormatting = [.prettyPrinted]
        guard let data = try? enc.encode(Wrap(banned: list)) else { return false }
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let tmp = fileURL.appendingPathExtension("tmp")
        do {
            try data.write(to: tmp)
            _ = try FileManager.default.replaceItemAt(fileURL, withItemAt: tmp)
            return true
        } catch {
            try? FileManager.default.removeItem(at: tmp)
            return (try? data.write(to: fileURL, options: .atomic)) != nil   // still atomic
        }
    }

    // MARK: banning a Slack sender

    enum BanWrite: Equatable {
        /// The daemon recorded it and wrote its activity line.
        case daemon
        /// No daemon answered: written here, and the activity line is the caller's.
        case file
        case failed(String)
    }

    /// The daemon's own check on a Slack user id.
    static func isSlackUserID(_ id: String) -> Bool {
        id.range(of: "^[UW][A-Z0-9]{2,}$", options: .regularExpression) != nil
    }

    /// The activity line the daemon writes for a Comms ban (`banLine` in daemon.js).
    static func slackBanLine(_ b: CommsBan) -> String {
        let who = "Slack user " + (b.handle.isEmpty ? "" : "@\(b.handle) ") + "(\(b.userID))"
        let place = b.permalink.isEmpty ? "" : " (\(b.permalink))"
        let flat = b.evidence.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        let quote = flat.isEmpty ? "" : ": \"\(flat.prefix(160))\""
        return "Banned \(who) for prompt injection\(place)\(quote) - flagged by Comms triage"
    }

    /// Ban a Slack sender Comms caught injecting. The daemon's /report-injection
    /// first (it serializes ban-list writes and keeps the evidence); when no daemon
    /// answers at all, the same entry and evidence folder are written here. A daemon
    /// that answers and refuses is not bypassed. Blocking: off the main actor.
    static func ban(_ b: CommsBan, reportedBy: String) -> BanWrite {
        guard isSlackUserID(b.userID) else {
            return .failed("\"\(b.userID)\" is not a Slack user id")
        }
        let login = CommsBan.login(b.userID)
        let body: [String: Any] = [
            "person": b.userID, "source": BannedAuthor.slackSource, "handle": b.handle,
            "place": b.place, "pr": b.permalink, "evidence": b.evidence, "agentName": reportedBy,
            "via": "comms",
        ]
        if let reply = DeviceAllocator.call("report-injection", body: body, timeoutSecs: 10) {
            guard (200..<300).contains(reply.status) else {
                return .failed(reply.json?["error"]?.string
                               ?? "the allocator daemon answered HTTP \(reply.status)")
            }
            // A daemon older than Slack bans takes the id for a GitHub login.
            let recorded = reply.json?["login"]?.string ?? ""
            guard recorded.lowercased() == login.lowercased() else {
                return .failed("the allocator daemon banned \"\(recorded)\" instead of \(login); "
                               + "restart it to pick up Slack bans")
            }
            return .daemon
        }
        return banInFile(b, login: login, reportedBy: reportedBy) ? .file
            : .failed("could not write \(fileURL.path)")
    }

    /// `handleReportInjection`'s entry, upserted the same way: a repeat keeps its `firstAt`.
    private static func banInFile(_ b: CommsBan, login: String, reportedBy: String) -> Bool {
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let at = iso.string(from: Date())
        let slug = at.replacingOccurrences(of: ":", with: "-").replacingOccurrences(of: ".", with: "-")
            + "-" + login.replacingOccurrences(of: ":", with: "_")
        let evidenceDir = dir.appendingPathComponent("injections").appendingPathComponent(slug)
        try? FileManager.default.createDirectory(at: evidenceDir, withIntermediateDirectories: true)
        try? b.evidence.write(to: evidenceDir.appendingPathComponent("evidence.txt"),
                              atomically: true, encoding: .utf8)
        var entry = BannedAuthor(login: login, reason: "prompt injection", pr: b.permalink,
                                 evidence: String(b.evidence.prefix(800)),
                                 evidenceDir: evidenceDir.path, reportedBy: reportedBy, at: at,
                                 firstAt: at, screenshot: false, ghCaptured: false,
                                 source: BannedAuthor.slackSource,
                                 handle: b.handle.isEmpty ? nil : b.handle,
                                 place: b.place.isEmpty ? nil : b.place)
        var list = read()
        if let i = list.firstIndex(where: { $0.login.lowercased() == login.lowercased() }) {
            entry.firstAt = list[i].firstAt ?? list[i].at ?? at
            list[i] = entry
        } else {
            list.append(entry)
        }
        return write(list)
    }
}
