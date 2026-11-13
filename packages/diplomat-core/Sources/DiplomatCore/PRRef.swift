import Foundation

/// A PR or issue reference parsed from a wizard's single-item text field.
///
/// The field accepts a bare number (`337`, `#337`, `!337` on a GitLab home), a full GitHub
/// or GitLab URL (`https://github.com/owner/repo/pull/337`,
/// `https://<host>/group/sub/proj/-/merge_requests/337`, any trailing path/query), or the
/// `owner/repo#337` shorthand (`group/sub/proj!337` too). When the input
/// names a repo (URL or shorthand) it's checked against the home repo (the root the wizard
/// works on, else the configured repo) and then the local checkouts listed in Settings, so
/// a link to an unknown project is rejected instead of silently reviewing the wrong PR.
/// Shared verbatim with the shared runtime (see `prref.py`).
public struct PRRef: Equatable {
    /// A local checkout, a repo root or one a link may target besides the home repo: one
    /// GitHub or GitLab remote of it (`host`, `owner`/`repo`) and where it is cloned
    /// (`path`). A GitLab `owner` is the project's whole namespace and may hold `/`.
    public struct LocalRepo: Equatable, Hashable, Codable {
        public static let github = "github.com"

        public let owner: String
        public let repo: String
        public let path: String
        /// Lowercased.
        public let host: String

        public init(owner: String, repo: String, path: String, host: String = LocalRepo.github) {
            self.owner = owner
            self.repo = repo
            self.path = path
            self.host = host.lowercased()
        }

        public var slug: String { "\(owner)/\(repo)" }
        public var isGitLab: Bool { host != LocalRepo.github }

        public func webURL(_ kind: Kind, number: Int) -> String {
            guard isGitLab else { return "https://github.com/\(slug)/\(kind.rawValue)/\(number)" }
            return "https://\(host)/\(slug)/-/\(kind == .pull ? "merge_requests" : "issues")/\(number)"
        }

        private enum CodingKeys: String, CodingKey { case owner, repo, path, host }

        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            self.init(owner: try c.decode(String.self, forKey: .owner),
                      repo: try c.decode(String.self, forKey: .repo),
                      path: try c.decode(String.self, forKey: .path),
                      host: try c.decodeIfPresent(String.self, forKey: .host) ?? LocalRepo.github)
        }

        /// `host` only off GitHub, so a GitHub repo encodes byte for byte without it.
        public func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(owner, forKey: .owner)
            try c.encode(repo, forKey: .repo)
            try c.encode(path, forKey: .path)
            if isGitLab { try c.encode(host, forKey: .host) }
        }
    }

    /// Which kind of GitHub item the field is for, and so which URL path segment a
    /// pasted link has to carry. A PR link and an issue link differ by that segment
    /// alone, and accepting either everywhere would let an issue URL through the
    /// Review wizard as a PR number.
    public enum Kind: String {
        case pull, issues
    }

    /// The extracted PR number, or nil when the input contains a usable one.
    public let number: Int?
    /// True only when the input named a repo that does NOT match the target. A bare
    /// number names no repo, so it never mismatches.
    public let repoMismatch: Bool
    /// The listed checkout the input named, or nil for the home repo (which a bare number
    /// always means).
    public let repo: LocalRepo?

    public init(number: Int?, repoMismatch: Bool, repo: LocalRepo? = nil) {
        self.number = number
        self.repoMismatch = repoMismatch
        self.repo = repo
    }

    /// A usable reference: a number was found and any named repo matched.
    public var isValid: Bool { number != nil && !repoMismatch }
    /// The bare number for prompt injection ("" when none).
    public var numberString: String { number.map(String.init) ?? "" }

    /// Parse `raw` against the expected `owner`/`repo` on `host`, then `also` in order (all
    /// compared case-insensitively). `kind` picks the URL path segment a pasted link must
    /// carry — `pull` (`merge_requests` on GitLab) for a PR field, `issues` for an issue
    /// one. A URL matches a repo on its own host only; the shorthand names no host.
    public static func parse(_ raw: String, owner: String, repo: String,
                             host: String = LocalRepo.github,
                             kind: Kind = .pull, also: [LocalRepo] = []) -> PRRef {
        let s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if s.isEmpty { return PRRef(number: nil, repoMismatch: false) }
        let want = (host: host.lowercased(), owner: owner, repo: repo)

        // A GitHub URL anywhere in the input: github.com/OWNER/REPO/<kind>/N.
        // [0-9], not \d: ICU's \d matches non-ASCII digits, which Int() then rejects —
        // and the Python port matches ASCII only, so the two sides would disagree.
        if let g = firstMatch(s, #"(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/"# + kind.rawValue + #"/([0-9]+)"#) {
            return named(host: LocalRepo.github, owner: g[1], repo: g[2], number: g[3],
                         want: want, also: also)
        }
        // A GitLab URL anywhere in the input: HOST/NS.../PROJ/-/<segment>/N.
        let segment = kind == .pull ? "merge_requests" : "(?:issues|work_items)"
        if let g = firstMatch(s, #"(?:https?://)?([A-Za-z0-9.-]+)(?::[0-9]+)?/((?:[\w.-]+/)*[\w.-]+)/([\w.-]+)/-/"#
                                 + segment + #"/([0-9]+)"#) {
            return named(host: g[1].lowercased(), owner: g[2], repo: g[3], number: g[4],
                         want: want, also: also)
        }
        // The OWNER/REPO#N shorthand (the whole string), OWNER of any depth; NS/PROJ!N is
        // GitLab's for a merge request.
        let mark = kind == .pull ? "[#!]" : "#"
        if let g = firstMatch(s, #"^((?:[\w.-]+/)*[\w.-]+)/([\w.-]+)"# + mark + #"([0-9]+)$"#) {
            return named(host: nil, owner: g[1], repo: g[2], number: g[3], want: want, also: also)
        }
        // A bare number, optionally after '#' (or '!' on a GitLab home). ASCII digits only —
        // Int() alone also accepts "+337", which the Python port rejects.
        let bang = kind == .pull && want.host != LocalRepo.github
        let bare = s.hasPrefix("#") || (bang && s.hasPrefix("!")) ? String(s.dropFirst()) : s
        if !bare.isEmpty, bare.allSatisfy({ $0.isASCII && $0.isNumber }),
           let n = Int(bare), n > 0 { return PRRef(number: n, repoMismatch: false) }

        return PRRef(number: nil, repoMismatch: false)
    }

    /// A nil `host` (the shorthand) matches any host.
    private static func named(host: String?, owner: String, repo: String, number: String,
                              want: (host: String, owner: String, repo: String),
                              also: [LocalRepo]) -> PRRef {
        let n = Int(number).flatMap { $0 > 0 ? $0 : nil }
        func same(_ h: String, _ o: String, _ r: String) -> Bool {
            (host.map { $0 == h } ?? true)
                && owner.caseInsensitiveCompare(o) == .orderedSame
                && repo.caseInsensitiveCompare(r) == .orderedSame
        }
        if same(want.host, want.owner, want.repo) { return PRRef(number: n, repoMismatch: false) }
        if let local = also.first(where: { same($0.host, $0.owner, $0.repo) }) {
            return PRRef(number: n, repoMismatch: false, repo: local)
        }
        return PRRef(number: n, repoMismatch: true)
    }

    /// The capture groups (index 0 = whole match, 1…n = groups) of the first match
    /// of `pattern` in `s`, or nil when it doesn't match.
    private static func firstMatch(_ s: String, _ pattern: String) -> [String]? {
        guard let re = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]) else { return nil }
        let full = NSRange(s.startIndex..., in: s)
        guard let m = re.firstMatch(in: s, options: [], range: full) else { return nil }
        return (0..<m.numberOfRanges).map { i in
            Range(m.range(at: i), in: s).map { String(s[$0]) } ?? ""
        }
    }
}

// MARK: - Item identity across repos

extension PRRef {
    /// One PR or issue across every repo this machine works: `owner/repo#n`, lowercased.
    /// GitHub numbers PRs and issues in one space per repo, so the key carries no kind;
    /// GitLab's iids number them separately, and the queue tells the two apart by verb. The
    /// key names no host. Python twin: `prref.item_key`.
    public static func itemKey(owner: String, repo: String, number: Int) -> String {
        "\(owner.lowercased())/\(repo.lowercased())#\(number)"
    }

    /// The item key of a github.com PR or issue URL, or a GitLab merge request or issue one
    /// (GitLab also serves issues at `work_items`), any trailing path or query; nil for
    /// anything else.
    public static func itemKey(url: String) -> String? {
        let s = url.trimmingCharacters(in: .whitespacesAndNewlines)
        if let g = firstMatch(s, #"^(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/(?:pull|issues)/([0-9]+)(?:[/?#]|$)"#),
           let n = Int(g[3]), n > 0 {
            return itemKey(owner: g[1], repo: g[2], number: n)
        }
        guard let g = firstMatch(s, #"^(?:https?://)?[A-Za-z0-9.-]+(?::[0-9]+)?/((?:[\w.-]+/)*[\w.-]+)/([\w.-]+)/-/(?:merge_requests|issues|work_items)/([0-9]+)(?:[/?#]|$)"#),
              let n = Int(g[3]), n > 0 else { return nil }
        return itemKey(owner: g[1], repo: g[2], number: n)
    }

    /// An item key's slug and number, or nil when `key` is not one. The slug's owner may be
    /// a nested GitLab namespace.
    public static func splitItemKey(_ key: String) -> (slug: String, number: Int)? {
        guard let hash = key.lastIndex(of: "#") else { return nil }
        let slug = String(key[..<hash])
        let digits = String(key[key.index(after: hash)...])
        let segments = slug.split(separator: "/", omittingEmptySubsequences: false)
        guard segments.count >= 2, !segments.contains(where: \.isEmpty),
              isDigits(digits), let n = Int(digits), n > 0 else { return nil }
        return (slug, n)
    }

    public static func splitSlug(_ slug: String) -> (owner: String, repo: String)? {
        guard let slash = slug.lastIndex(of: "/") else { return nil }
        return (String(slug[..<slash]), String(slug[slug.index(after: slash)...]))
    }

    /// A key persisted by a single-repo build, a bare `"N"`, as the item key of `N` in the
    /// `primary` root (its slug); any other key unchanged.
    public static func migrateItemKey(_ key: String, primary: String) -> String {
        isDigits(key) ? "\(primary.lowercased())#\(key)" : key
    }

    /// A queue key persisted by a single-repo build, `"verb:N"`, as `"verb:<primary>#N"`;
    /// any other key unchanged. An investigation's key is numbered by a Slack ts, never
    /// by a repo, so it is never migrated.
    public static func migrateQueueKey(_ key: String, primary: String) -> String {
        guard let colon = key.firstIndex(of: ":") else { return key }
        let verb = String(key[..<colon])
        let rest = String(key[key.index(after: colon)...])
        guard verb != AgentTaskQueue.investigateAction, isDigits(rest) else { return key }
        return "\(verb):\(migrateItemKey(rest, primary: primary))"
    }

    /// How a job label names an item: `#N` while one root is loaded, `<repo>#N` (the repo
    /// name alone) once several are.
    public static func itemLabel(item: String, roots: Int) -> String {
        guard let (slug, n) = splitItemKey(item) else { return item }
        guard roots > 1, let repo = slug.split(separator: "/").last else { return "#\(n)" }
        return "\(repo)#\(n)"
    }

    private static func isDigits(_ s: String) -> Bool {
        !s.isEmpty && s.allSatisfy { $0.isASCII && $0.isNumber }
    }
}
