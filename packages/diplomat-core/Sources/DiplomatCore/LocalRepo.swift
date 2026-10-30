import Foundation

extension PRRef.LocalRepo {
    /// The GitHub `owner`/`repo` a git remote URL names, or nil for any other host. Takes
    /// the scp form (`git@github.com:o/r.git`) and the `https://` / `ssh://` URL forms,
    /// with or without `.git`.
    public static func coordinates(remoteURL: String) -> (owner: String, repo: String)? {
        let s = remoteURL.trimmingCharacters(in: .whitespacesAndNewlines)
        let pattern = #"^(?:(?:https?|ssh|git)://(?:[^@/]+@)?github\.com(?::[0-9]+)?/|[^@/:]+@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$"#
        guard let re = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]),
              let m = re.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)),
              let o = Range(m.range(at: 1), in: s), let r = Range(m.range(at: 2), in: s)
        else { return nil }
        return (String(s[o]), String(s[r]))
    }

    /// Every GitHub remote of the checkout at `path`, from `git config --get-regexp
    /// '^remote\..*\.url$'` output, each once. A fork clone carries both the fork and the
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
}
