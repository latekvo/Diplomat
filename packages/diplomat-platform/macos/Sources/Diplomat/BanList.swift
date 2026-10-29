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
struct BannedAuthor: Decodable, Equatable, Identifiable {
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

extension BannedAuthor {
    private enum CodingKeys: String, CodingKey {
        case login, reason, pr, evidence, evidenceDir, reportedBy, at, firstAt, screenshot
        case ghCaptured, source, handle, place
    }

    /// Only `login` is required: a field of the wrong type (a numeric `pr`, which the
    /// daemon once stored as given) reads as absent, and the ban still holds.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let login = try c.decode(String.self, forKey: .login)
        guard !login.isEmpty else {
            throw DecodingError.dataCorruptedError(forKey: .login, in: c,
                                                   debugDescription: "empty login")
        }
        func text(_ k: CodingKeys) -> String? { (try? c.decodeIfPresent(String.self, forKey: k)) ?? nil }
        func flag(_ k: CodingKeys) -> Bool? { (try? c.decodeIfPresent(Bool.self, forKey: k)) ?? nil }
        self.init(login: login, reason: text(.reason), pr: text(.pr), evidence: text(.evidence),
                  evidenceDir: text(.evidenceDir), reportedBy: text(.reportedBy), at: text(.at),
                  firstAt: text(.firstAt), screenshot: flag(.screenshot),
                  ghCaptured: flag(.ghCaptured), source: text(.source), handle: text(.handle),
                  place: text(.place))
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

    /// Entry by entry: one without a string `login` is skipped, the rest still read.
    /// Empty when the file is absent or unreadable (`readFile`).
    static func read() -> [BannedAuthor] {
        guard case .list(_, let entries) = readFile() else { return [] }
        return entries.compactMap { raw in
            guard let obj = raw as? [String: Any],
                  let data = try? JSONSerialization.data(withJSONObject: obj) else { return nil }
            return try? JSONDecoder().decode(BannedAuthor.self, from: data)
        }
    }

    enum File {
        case absent
        /// Present, but not a JSON object whose `banned` is an array (or absent): never
        /// written over, since that would lift every ban in it.
        case unreadable
        /// The whole object, and its `banned` entries as stored, including any `read`
        /// skips: a rewrite keeps every entry and key it does not change.
        case list([String: Any], [Any])
    }

    static func readFile() -> File {
        guard FileManager.default.fileExists(atPath: fileURL.path) else { return .absent }
        guard let data = try? Data(contentsOf: fileURL),
              let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else { return .unreadable }
        switch root["banned"] {
        case nil: return .list(root, [])
        case let entries as [Any]: return .list(root, entries)
        default: return .unreadable
        }
    }

    private static func sameLogin(_ raw: Any, _ login: String) -> Bool {
        ((raw as? [String: Any])?["login"] as? String)?.lowercased() == login.lowercased()
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

    enum Unban: Equatable {
        /// The daemon handled it, and wrote the activity line.
        case daemon
        /// No daemon could be reached: rewritten here, and the activity line is the caller's.
        case file
        /// No daemon could be reached, and the list holds no such login.
        case notBanned
        case failed(String)
    }

    /// Remove a ban (the UI's un-ban button). Through the daemon's /unban: it serializes
    /// all ban-list writes in one process, so a concurrent injection report can't be lost
    /// to our read-modify-write (an atomic rename prevents corruption, but not a lost
    /// update). Rewritten here only when the request cannot reach a daemon at all; one
    /// that refuses or does not answer in time is not written past.
    @discardableResult
    static func unban(_ login: String, timeoutSecs: Int = 5) -> Unban {
        switch DeviceAllocator.send("unban", body: ["login": login], timeoutSecs: timeoutSecs) {
        case .reply(let r) where (200..<300).contains(r.status): return .daemon
        case .reply(let r):
            return .failed(r.json?["error"]?.string ?? "the allocator daemon answered HTTP \(r.status)")
        case .unanswered:
            return .failed("the allocator daemon did not answer in time; it may still lift the ban")
        case .unreachable: break
        }
        // Never rewrite the file unless that login is actually banned - so a
        // stray/duplicate call can't clobber the list.
        guard case .list(var root, var entries) = readFile(),
              entries.contains(where: { sameLogin($0, login) }) else { return .notBanned }
        entries.removeAll { sameLogin($0, login) }
        root["banned"] = entries
        return write(root) ? .file : .failed("could not write \(fileURL.path)")
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
    private static func write(_ root: [String: Any]) -> Bool {
        guard let data = try? JSONSerialization.data(
            withJSONObject: root, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
        else { return false }
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
        /// No daemon could be reached: written here, and the activity line is the caller's.
        case file
        /// On the list already: nothing written.
        case already
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

    /// Ban a Slack sender Comms caught injecting, unless already banned. The daemon's
    /// /report-injection first (it serializes ban-list writes and keeps the evidence);
    /// only when the request cannot reach a daemon at all are the same entry and
    /// evidence folder written here. A daemon that refuses, or takes the request and does
    /// not answer in time, is not bypassed: a late write of its own would duplicate
    /// ours. Blocking: off the main actor.
    static func ban(_ b: CommsBan, reportedBy: String, timeoutSecs: Int = 10) -> BanWrite {
        guard isSlackUserID(b.userID) else {
            return .failed("\"\(b.userID)\" is not a Slack user id")
        }
        let login = CommsBan.login(b.userID)
        let before = read()
        if isBanned(login, in: before) { return .already }
        let body: [String: Any] = [
            "person": b.userID, "source": BannedAuthor.slackSource, "handle": b.handle,
            "place": b.place, "pr": b.permalink, "evidence": b.evidence, "agentName": reportedBy,
            "via": "comms",
        ]
        switch DeviceAllocator.send("report-injection", body: body, timeoutSecs: timeoutSecs) {
        case .reply(let reply):
            guard (200..<300).contains(reply.status) else {
                return .failed(reply.json?["error"]?.string
                               ?? "the allocator daemon answered HTTP \(reply.status)")
            }
            // A daemon older than Slack bans takes the id for a GitHub login: that stray
            // ban is lifted again, unless that login was banned already.
            let recorded = reply.json?["login"]?.string ?? ""
            guard recorded.lowercased() == login.lowercased() else {
                let wasThere = !recorded.isEmpty && isBanned(recorded, in: before)
                let lifted = recorded.isEmpty || wasThere || unbanViaDaemon(recorded)
                return .failed("the allocator daemon banned \"\(recorded)\" instead of \(login)"
                               + (wasThere ? ", which was banned already, so that ban stays"
                                  : lifted ? "" : ", and would not lift it")
                               + "; restart it to pick up Slack bans")
            }
            return .daemon
        case .unanswered:
            return .failed("the allocator daemon did not answer in time; it may still record "
                           + "the ban")
        case .unreachable:
            if let why = banInFile(b, login: login, reportedBy: reportedBy) { return .failed(why) }
            return .file
        }
    }

    /// `handleReportInjection`'s entry, upserted the same way: a repeat keeps its
    /// `firstAt`, and the handle and channel it had when this one names none. Returns why
    /// nothing was written, or nil.
    private static func banInFile(_ b: CommsBan, login: String, reportedBy: String) -> String? {
        var root: [String: Any] = [:]
        var entries: [Any] = []
        switch readFile() {
        case .absent: break
        case .unreadable: return "\(fileURL.path) is unreadable, so it was left as it is"
        case .list(let r, let e): (root, entries) = (r, e)
        }
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let at = iso.string(from: Date())
        let slug = at.replacingOccurrences(of: ":", with: "-").replacingOccurrences(of: ".", with: "-")
            + "-" + login.replacingOccurrences(of: ":", with: "_")
        let evidenceDir = dir.appendingPathComponent("injections").appendingPathComponent(slug)
        try? FileManager.default.createDirectory(at: evidenceDir, withIntermediateDirectories: true)
        try? b.evidence.write(to: evidenceDir.appendingPathComponent("evidence.txt"),
                              atomically: true, encoding: .utf8)
        var entry: [String: Any] = [
            "login": login, "reason": "prompt injection",
            "pr": b.permalink.isEmpty ? NSNull() : b.permalink,
            "evidence": String(b.evidence.prefix(800)), "evidenceDir": evidenceDir.path,
            "reportedBy": reportedBy, "at": at, "screenshot": false, "ghCaptured": false,
            "source": BannedAuthor.slackSource,
            "handle": b.handle.isEmpty ? NSNull() : b.handle,
            "place": b.place.isEmpty ? NSNull() : b.place,
        ]
        if let i = entries.firstIndex(where: { sameLogin($0, login) }),
           let existing = entries[i] as? [String: Any] {
            for key in ["handle", "place"] where entry[key] is NSNull {
                entry[key] = existing[key] ?? NSNull()
            }
            entry["firstAt"] = existing["firstAt"] ?? existing["at"] ?? at
            entries[i] = existing.merging(entry) { $1 }
        } else {
            entry["firstAt"] = at
            entries.append(entry)
        }
        root["banned"] = entries
        return write(root) ? nil : "could not write \(fileURL.path)"
    }
}
