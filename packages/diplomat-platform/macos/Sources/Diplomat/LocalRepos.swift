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

    /// Runs `git` once per path, so callers stay off the main actor.
    static func resolve(_ paths: [String]) -> [Checkout] {
        paths.map { raw in
            let path = (raw as NSString).expandingTildeInPath
            guard RepoPaths.isCheckout(path) else { return Checkout(path: raw, repos: []) }
            let remotes = git(["-C", path, "config", "--get-regexp", #"^remote\..*\.url$"#]) ?? ""
            return Checkout(path: raw, repos: PRRef.LocalRepo.entries(path: path, remotes: remotes))
        }
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
    /// How a run's label names the item: `#337`, or `owner/repo#291` in a listed repo.
    var label: String? {
        number.map { n in repo.map { "\($0.slug)#\(n)" } ?? "#\(n)" }
    }

    /// The hint under a single-item field whose link names a repo that is neither the
    /// configured one nor listed in Settings. `noun` is "PR" or "issue".
    func mismatchWarning(_ noun: String, anyListed: Bool) -> String? {
        guard repoMismatch else { return nil }
        let (owner, repo) = CoreAssets.repoCoordinates()
        return "That \(noun) isn't in \(owner)/\(repo)"
            + (anyListed ? " or any repo listed in Settings." : ".")
    }
}
