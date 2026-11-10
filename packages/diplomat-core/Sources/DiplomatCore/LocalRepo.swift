import Foundation

extension PRRef.LocalRepo {
    /// The GitHub `owner`/`repo` a git remote URL names, or nil for any other host: the scp
    /// form (`git@github.com:o/r`) or a URL (`https`/`http`/`ssh`/`git`, any user and
    /// port), with or without `.git`. Same pattern as `localrepos.py`.
    public static func coordinates(remoteURL: String) -> (owner: String, repo: String)? {
        let s = remoteURL.trimmingCharacters(in: .whitespacesAndNewlines)
        let pattern = #"^(?:(?:https?|ssh|git)://(?:[^@/]+@)?github\.com(?::[0-9]+)?/|[^@/:]+@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$"#
        guard let re = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]),
              let m = re.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)),
              let o = Range(m.range(at: 1), in: s), let r = Range(m.range(at: 2), in: s)
        else { return nil }
        return (String(s[o]), String(s[r]))
    }

    /// Every GitHub remote of the checkout at `path`, from `git config --local
    /// --get-regexp '^remote\..*\.url$'` output, each once. A fork clone carries both the fork and the
    /// project it forked, and a link to either belongs in that checkout. `upstream` comes
    /// first, then `origin`, then the rest in config order: the first entry is the one
    /// Settings names the checkout by.
    public static func entries(path: String, remotes: String) -> [PRRef.LocalRepo] {
        var named: [(remote: String, repo: PRRef.LocalRepo)] = []
        for line in remotes.split(separator: "\n") {
            let parts = line.split(separator: " ", maxSplits: 1).map(String.init)
            guard parts.count == 2, parts[0].hasPrefix("remote."), parts[0].hasSuffix(".url"),
                  let c = coordinates(remoteURL: parts[1]) else { continue }
            let remote = String(parts[0].dropFirst("remote.".count).dropLast(".url".count))
            named.append((remote, PRRef.LocalRepo(owner: c.owner, repo: c.repo, path: path)))
        }
        let rank = { (remote: String) in ["upstream", "origin"].firstIndex(of: remote) ?? 2 }
        var seen = Set<String>()
        return named.enumerated()
            .sorted { (rank($0.element.remote), $0.offset) < (rank($1.element.remote), $1.offset) }
            .map(\.element.repo)
            .filter { seen.insert($0.slug.lowercased()).inserted }
    }

    /// The coordinates a config with this home targets: the home's, else
    /// `assets/config.json`'s.
    static func coordinates(home: PRRef.LocalRepo?) -> (owner: String, repo: String) {
        home.map { ($0.owner, $0.repo) } ?? CoreAssets.repoCoordinates()
    }
}

/// The repo roots the operator loaded, each read as the one GitHub repo every automated
/// job works there. Python twin: `localrepos.resolve_roots`.
public enum RepoRoots {
    /// Each of `paths` (a leading `~` expanded) as the repo it is a checkout of, in order.
    ///
    /// `remotes` maps an expanded path to its `git config --local --get-regexp
    /// '^remote\..*\.url$'` output; a path missing from it is no checkout. A path names
    /// the configured repo when one of its remotes is that repo, or a repo of that name
    /// under another owner (a fork's `origin`, or a URL from before the repo moved), and
    /// otherwise its first remote (`LocalRepo.entries` order). A path with no GitHub
    /// remote is skipped, and a repo two paths name keeps the first. When nothing
    /// resolves, the first path stands for the configured repo, which is how a single
    /// missing or remote-less checkout has always been worked.
    public static func resolve(paths: [String], remotes: [String: String],
                               configured: (owner: String, repo: String)) -> [PRRef.LocalRepo] {
        let want = "\(configured.owner)/\(configured.repo)".lowercased()
        var seen = Set<String>()
        var out: [PRRef.LocalRepo] = []
        for raw in paths {
            let path = expand(raw)
            guard let config = remotes[path] else { continue }
            let entries = PRRef.LocalRepo.entries(path: path, remotes: config)
            let renamed = entries.contains { $0.repo.lowercased() == configured.repo.lowercased() }
                ? PRRef.LocalRepo(owner: configured.owner, repo: configured.repo, path: path) : nil
            guard let pick = entries.first(where: { $0.slug.lowercased() == want })
                    ?? renamed ?? entries.first,
                  seen.insert(pick.slug.lowercased()).inserted else { continue }
            out.append(pick)
        }
        if out.isEmpty, let first = paths.first {
            return [PRRef.LocalRepo(owner: configured.owner, repo: configured.repo,
                                    path: expand(first))]
        }
        return out
    }

    /// Whether every one of `paths` has a GitHub remote in `remotes`, as `resolve` reads
    /// them: false while one is missing, git failed on it, or it has no GitHub remote.
    /// Python twin: `localrepos.all_resolve`.
    public static func allResolve(paths: [String], remotes: [String: String]) -> Bool {
        paths.allSatisfy {
            !PRRef.LocalRepo.entries(path: expand($0), remotes: remotes[expand($0)] ?? "").isEmpty
        }
    }

    /// `path` with a leading `~` (alone or before a `/`) replaced by `$HOME`, as Python's
    /// `os.path.expanduser` does, and otherwise byte for byte as given.
    public static func expand(_ path: String) -> String {
        guard path == "~" || path.hasPrefix("~/") else { return path }
        let home = ProcessInfo.processInfo.environment["HOME"] ?? NSHomeDirectory()
        return home + path.dropFirst()
    }
}
