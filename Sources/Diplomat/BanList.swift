import Foundation

// One prompt-injection ban. Written by the device-allocator daemon's
// report-prompt-injection tool to ~/.diplomat/pr-monitor/banned.json (a cross-process
// file, since any agent on the machine can report). The applet reads it to skip
// auto-reviewing that author's PRs and to show the ban list.
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

    var id: String { login }
}

enum BanList {
    static var fileURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".diplomat/pr-monitor/banned.json")
    }

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
        struct Wrap: Encodable { let banned: [BannedAuthor] }
        let enc = JSONEncoder()
        enc.outputFormatting = [.prettyPrinted]
        guard let data = try? enc.encode(Wrap(banned: list)) else { return false }
        let tmp = fileURL.appendingPathExtension("tmp")
        do {
            try data.write(to: tmp)
            _ = try FileManager.default.replaceItemAt(fileURL, withItemAt: tmp)
        } catch {
            try? data.write(to: fileURL, options: .atomic)   // fallback, still atomic
        }
        return false
    }

    private static var daemonSocket: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".diplomat/device-allocator/daemon.sock")
    }

    /// POST /unban over the daemon's unix socket (via curl — URLSession has no unix-
    /// socket support). True only when the daemon answered 200.
    private static func unbanViaDaemon(_ login: String) -> Bool {
        guard FileManager.default.fileExists(atPath: daemonSocket.path),
              let body = try? JSONSerialization.data(withJSONObject: ["login": login]),
              let json = String(data: body, encoding: .utf8) else { return false }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/curl")
        p.arguments = ["-sf", "--max-time", "5", "--unix-socket", daemonSocket.path,
                       "-X", "POST", "http://localhost/unban",
                       "-H", "content-type: application/json", "-d", json]
        p.standardOutput = Pipe()
        p.standardError = Pipe()
        do { try p.run() } catch { return false }
        p.waitUntilExit()
        return p.terminationStatus == 0
    }
}
