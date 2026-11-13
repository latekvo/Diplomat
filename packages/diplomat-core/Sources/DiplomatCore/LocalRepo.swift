import Foundation

extension PRRef.LocalRepo {
    /// The lowercased host and `owner`/`repo` a git remote URL names: the scp form
    /// (`git@github.com:o/r`) or a URL (`https`/`http`/`ssh`/`git`, any user and port), with
    /// or without `.git`; nil off github.com and GitLab (`GitLabHosts.contains`). A GitLab
    /// owner is the whole namespace. Same patterns as `localrepos.py`.
    public static func coordinates(remoteURL: String, gitlabHosts: Set<String> = [])
        -> (host: String, owner: String, repo: String)? {
        let s = remoteURL.trimmingCharacters(in: .whitespacesAndNewlines)
        let github = #"^(?:(?:https?|ssh|git)://(?:[^@/]+@)?github\.com(?::[0-9]+)?/|[^@/:]+@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$"#
        if let g = groups(s, github) { return (PRRef.LocalRepo.github, g[0], g[1]) }
        let gitlab = #"^(?:(?:https?|ssh|git)://(?:[^@/]+@)?([A-Za-z0-9.-]+)(?::[0-9]+)?/|[^@/:]+@([A-Za-z0-9.-]+):)((?:[\w.-]+/)*[\w.-]+)/([\w.-]+?)(?:\.git)?/?$"#
        guard let g = groups(s, gitlab) else { return nil }
        let host = (g[0].isEmpty ? g[1] : g[0]).lowercased()
        guard GitLabHosts.contains(host, configured: gitlabHosts) else { return nil }
        return (host, g[2], g[3])
    }

    /// Capture groups 1...n, "" for one that did not take part; case-insensitive.
    private static func groups(_ s: String, _ pattern: String) -> [String]? {
        guard let re = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]),
              let m = re.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) else { return nil }
        return (1..<m.numberOfRanges).map { i in
            Range(m.range(at: i), in: s).map { String(s[$0]) } ?? ""
        }
    }

    /// Every GitHub or GitLab remote of the checkout at `path`, from `git config --local
    /// --get-regexp '^remote\..*\.url$'` output, each once (by host and slug). A fork clone
    /// carries both the fork and the project it forked, and a link to either belongs in
    /// that checkout. GitHub ones rank first, as in `RepoRoots.resolve`, so a host-less
    /// `owner/repo#N` names the GitHub repo; Settings names the checkout by the first.
    /// Within each host, `upstream` comes first, then `origin`, then the rest in config
    /// order. With `only`, just the remotes it names, ranked the same.
    /// Python twin: `localrepos.entries`.
    public static func entries(path: String, remotes: String, only: [String] = [],
                               gitlabHosts: Set<String> = []) -> [PRRef.LocalRepo] {
        var named: [(remote: String, repo: PRRef.LocalRepo)] = []
        for line in remotes.split(separator: "\n") {
            let parts = line.split(separator: " ", maxSplits: 1).map(String.init)
            guard parts.count == 2, parts[0].hasPrefix("remote."), parts[0].hasSuffix(".url"),
                  let c = coordinates(remoteURL: parts[1], gitlabHosts: gitlabHosts) else { continue }
            let remote = String(parts[0].dropFirst("remote.".count).dropLast(".url".count))
            if !only.isEmpty, !only.contains(remote) { continue }
            named.append((remote, PRRef.LocalRepo(owner: c.owner, repo: c.repo, path: path,
                                                  host: c.host)))
        }
        let rank = { (e: (offset: Int, element: (remote: String, repo: PRRef.LocalRepo))) in
            (e.element.repo.isGitLab ? 1 : 0, ["upstream", "origin"].firstIndex(of: e.element.remote) ?? 2,
             e.offset)
        }
        var seen = Set<String>()
        return named.enumerated()
            .sorted { rank($0) < rank($1) }
            .map(\.element.repo)
            .filter { seen.insert($0.identity).inserted }
    }

    var identity: String { "\(host)/\(slug.lowercased())" }

    /// The coordinates a config with this home targets: the home's, else
    /// `assets/config.json`'s.
    static func coordinates(home: PRRef.LocalRepo?) -> (owner: String, repo: String) {
        home.map { ($0.owner, $0.repo) } ?? CoreAssets.repoCoordinates()
    }

    /// The host a config with this home targets: the home's, else github.com
    /// (`assets/config.json`'s).
    static func host(home: PRRef.LocalRepo?) -> String {
        home?.host ?? PRRef.LocalRepo.github
    }

    /// `gitlab.json`'s paragraph for a prompt whose target is `repo`, nil off GitLab.
    static func gitlabNote(_ repo: PRRef.LocalRepo?) -> String? {
        guard let repo, repo.isGitLab, let note = try? CoreAssets.gitlab().promptNote else { return nil }
        return note.replacingOccurrences(of: "{host}", with: repo.host)
            .replacingOccurrences(of: "{owner}", with: repo.owner)
            .replacingOccurrences(of: "{repo}", with: repo.repo)
    }
}

/// The hosts a git remote reads as GitLab on: gitlab.com, `configured`
/// (`GLab.configuredHosts`) and any `gitlab.*` host; never github.com, whatever glab lists.
/// Python twin: `localrepos.gitlab_hosts`.
public enum GitLabHosts {
    public static func contains(_ host: String, configured: Set<String>) -> Bool {
        let h = host.lowercased()
        if h == PRRef.LocalRepo.github { return false }
        if h == "gitlab.com" || configured.contains(where: { $0.lowercased() == h }) { return true }
        let labels = h.split(separator: ".", omittingEmptySubsequences: false)
        return labels.count >= 2 && labels[0] == "gitlab" && !labels.contains(where: \.isEmpty)
    }

    /// The hosts a glab `config.yml` lists: each bare `key:` line under column-0 `hosts:`
    /// at its first child's indent, `:port` dropped.
    public static func parse(config: String) -> Set<String> {
        var out = Set<String>()
        var inHosts = false
        var indent: Int?
        let key = try? NSRegularExpression(pattern: #"^\s+([A-Za-z0-9.-]+)(?::[0-9]+)?:\s*$"#)
        for raw in config.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = String(raw)
            let body = line.trimmingCharacters(in: .whitespaces)
            if body.isEmpty || body.hasPrefix("#") { continue }
            let depth = line.prefix { $0 == " " || $0 == "\t" }.count
            if depth == 0 {
                inHosts = body == "hosts:"
                indent = nil
                continue
            }
            guard inHosts else { continue }
            if indent == nil { indent = depth }
            guard depth == indent,
                  let m = key?.firstMatch(in: line, range: NSRange(line.startIndex..., in: line)),
                  let r = Range(m.range(at: 1), in: line) else { continue }
            out.insert(line[r].lowercased())
        }
        return out
    }
}

/// The repo roots the operator loaded, each read as the one GitHub or GitLab repo every
/// automated job works there. Python twin: `localrepos.resolve_roots`.
public enum RepoRoots {
    /// Each of `paths` (a leading `~` expanded) as the repo it is a checkout of, in order.
    ///
    /// `remotes` maps an expanded path to its `git config --local --get-regexp
    /// '^remote\..*\.url$'` output; a path missing from it is no checkout. A path with a
    /// GitHub remote resolves among those alone: it names the configured repo when its
    /// `upstream` or `origin` (any GitHub remote, with neither) is that repo, or a repo of
    /// that name under another owner (a fork's `origin`, or a URL from before the repo
    /// moved), and otherwise its first GitHub remote (`LocalRepo.entries` order). Another
    /// remote naming the configured repo is a sibling fetched from, not what the checkout
    /// is. A path with no GitHub remote names its first GitLab remote; one with neither is
    /// skipped, and a repo two paths name keeps the first. When nothing
    /// resolves, the first path stands for the configured repo (`guessed` tells when that
    /// is a guess among several).
    public static func resolve(paths: [String], remotes: [String: String],
                               configured: (owner: String, repo: String),
                               gitlabHosts: Set<String> = []) -> [PRRef.LocalRepo] {
        let want = "\(configured.owner)/\(configured.repo)".lowercased()
        var seen = Set<String>()
        var out: [PRRef.LocalRepo] = []
        for raw in paths {
            let path = expand(raw)
            guard let config = remotes[path] else { continue }
            let entries = PRRef.LocalRepo.entries(path: path, remotes: config,
                                                  gitlabHosts: gitlabHosts)
            let ownRemotes = PRRef.LocalRepo.entries(path: path, remotes: config,
                                                     only: ["upstream", "origin"],
                                                     gitlabHosts: gitlabHosts)
            let hub = entries.filter { !$0.isGitLab }
            let github = ownRemotes.filter { !$0.isGitLab }
            let own = github.isEmpty ? hub : github
            let renamed = own.contains { $0.repo.lowercased() == configured.repo.lowercased() }
                ? PRRef.LocalRepo(owner: configured.owner, repo: configured.repo, path: path) : nil
            guard let pick = own.first(where: { $0.slug.lowercased() == want })
                    ?? renamed ?? entries.first,
                  seen.insert(pick.identity).inserted else { continue }
            out.append(pick)
        }
        if out.isEmpty, let first = paths.first {
            return [PRRef.LocalRepo(owner: configured.owner, repo: configured.repo,
                                    path: expand(first))]
        }
        return out
    }

    /// Whether every one of `paths` has a GitHub or GitLab remote in `remotes`, as
    /// `resolve` reads them: false while one is missing, git failed on it, or it has
    /// neither. Python twin: `localrepos.all_resolve`.
    public static func allResolve(paths: [String], remotes: [String: String],
                                  gitlabHosts: Set<String> = []) -> Bool {
        paths.allSatisfy { resolves($0, remotes, gitlabHosts) }
    }

    /// Whether `resolve`'s first root is the first path's own repo: always with one path,
    /// else only while the first path has a GitHub or GitLab remote. Without one the next
    /// path's repo stands first, so "the primary" names another repo. Python twin:
    /// `localrepos.primary_resolves`.
    public static func primaryResolves(paths: [String], remotes: [String: String],
                                       gitlabHosts: Set<String> = []) -> Bool {
        guard paths.count >= 2 else { return true }
        return resolves(paths[0], remotes, gitlabHosts)
    }

    /// Whether `resolve` fell back on several paths none of which resolved: its one root
    /// is then a guess that the first path checks out the configured repo, possibly a
    /// checkout of another. Python twin: `localrepos.guessed`.
    public static func guessed(paths: [String], remotes: [String: String],
                               gitlabHosts: Set<String> = []) -> Bool {
        paths.count > 1 && !paths.contains { resolves($0, remotes, gitlabHosts) }
    }

    private static func resolves(_ path: String, _ remotes: [String: String],
                                 _ gitlabHosts: Set<String>) -> Bool {
        let p = expand(path)
        return !PRRef.LocalRepo.entries(path: p, remotes: remotes[p] ?? "",
                                        gitlabHosts: gitlabHosts).isEmpty
    }

    /// `path` with a leading `~` (alone or before a `/`) replaced by `$HOME`, as Python's
    /// `os.path.expanduser` does, and otherwise byte for byte as given.
    public static func expand(_ path: String) -> String {
        guard path == "~" || path.hasPrefix("~/") else { return path }
        let home = ProcessInfo.processInfo.environment["HOME"] ?? NSHomeDirectory()
        return home + path.dropFirst()
    }
}
