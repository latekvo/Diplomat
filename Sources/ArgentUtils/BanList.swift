import Foundation

// One prompt-injection ban. Written by the device-allocator daemon's
// report-prompt-injection tool to ~/.argent/pr-monitor/banned.json (a cross-process
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
            .appendingPathComponent(".argent/pr-monitor/banned.json")
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

    /// Remove a ban (the UI's un-ban button). Atomic rewrite so it can't corrupt a
    /// concurrent daemon write.
    static func unban(_ login: String) {
        var list = read()
        list.removeAll { $0.login.lowercased() == login.lowercased() }
        struct Wrap: Encodable { let banned: [BannedAuthor] }
        let enc = JSONEncoder()
        enc.outputFormatting = [.prettyPrinted]
        guard let data = try? enc.encode(Wrap(banned: list)) else { return }
        let tmp = fileURL.appendingPathExtension("tmp")
        do {
            try data.write(to: tmp)
            _ = try FileManager.default.replaceItemAt(fileURL, withItemAt: tmp)
        } catch {
            try? data.write(to: fileURL)   // fallback
        }
    }
}
