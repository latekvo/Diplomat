import Foundation
import DiplomatCore

/// The checkouts besides the repo root that the wizards accept links to (Settings →
/// OTHER REPOS), each read for the GitHub repos its remotes name.
enum LocalRepos {
    struct Checkout: Equatable {
        /// As entered; `~` is expanded only for the lookup and the spawn.
        let path: String
        /// Its GitHub remotes, the one Settings names it by first. Empty for a path that
        /// is no checkout or has no GitHub remote: listed, but no link resolves to it.
        let repos: [PRRef.LocalRepo]
    }

    /// One `git` call per path: keep it off the main actor.
    static func resolve(_ paths: [String]) -> [Checkout] {
        paths.map { raw in
            let path = (raw as NSString).expandingTildeInPath
            guard RepoPaths.isCheckout(path) else { return Checkout(path: raw, repos: []) }
            return Checkout(path: raw, repos: PRRef.LocalRepo.entries(path: path,
                                                                      remotes: remotes(of: path)))
        }
    }

    static func remotes(of path: String) -> String {
        git(["-C", path, "config", "--local", "--get-regexp", #"^remote\..*\.url$"#]) ?? ""
    }

    private static func git(_ arguments: [String]) -> String? {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/git")
        proc.arguments = arguments
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = Pipe()
        do { try proc.run() } catch { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        guard proc.terminationStatus == 0 else { return nil }
        return String(data: data, encoding: .utf8)
    }
}

extension PRRef {
    /// The hint under a single-item field whose link names a repo that is neither the
    /// selected one (`home`) nor listed in Settings. `noun` is "PR" or "issue".
    func mismatchWarning(_ noun: String, home: String, anyListed: Bool) -> String? {
        guard repoMismatch else { return nil }
        return "That \(noun) isn't in \(home)"
            + (anyListed ? " or any repo listed in Settings." : ".")
    }
}
