import Foundation

/// A PR or issue reference parsed from a wizard's single-item text field.
///
/// The field accepts a bare number (`337`, `#337`), a full GitHub URL
/// (`https://github.com/owner/repo/pull/337`, with any trailing path/query), or the
/// `owner/repo#337` shorthand. When the input names a repo (URL or shorthand) it's
/// checked against the configured target repo and then the local checkouts listed in
/// Settings, so a link to an unknown project is rejected instead of silently reviewing
/// the wrong PR. Shared verbatim with the shared runtime (see `prref.py`).
public struct PRRef: Equatable {
    /// A local checkout a link may target besides the configured repo: one GitHub remote
    /// of it (`owner`/`repo`) and where it is cloned (`path`).
    public struct LocalRepo: Equatable, Hashable, Codable {
        public let owner: String
        public let repo: String
        public let path: String

        public init(owner: String, repo: String, path: String) {
            self.owner = owner
            self.repo = repo
            self.path = path
        }

        public var slug: String { "\(owner)/\(repo)" }
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
    /// The listed checkout the input named, or nil for the configured repo (which a bare
    /// number always means).
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

    /// Parse `raw` against the expected `owner`/`repo`, then `also` in order (all
    /// compared case-insensitively). `kind` picks the URL path segment a pasted link must
    /// carry — `pull` for a PR field, `issues` for an issue one.
    public static func parse(_ raw: String, owner: String, repo: String,
                             kind: Kind = .pull, also: [LocalRepo] = []) -> PRRef {
        let s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if s.isEmpty { return PRRef(number: nil, repoMismatch: false) }

        // A GitHub URL anywhere in the input: github.com/OWNER/REPO/<kind>/N.
        // [0-9], not \d: ICU's \d matches non-ASCII digits, which Int() then rejects —
        // and the Python port matches ASCII only, so the two sides would disagree.
        if let g = firstMatch(s, #"(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/"# + kind.rawValue + #"/([0-9]+)"#) {
            return named(owner: g[1], repo: g[2], number: g[3], wantOwner: owner, wantRepo: repo, also: also)
        }
        // The OWNER/REPO#N shorthand (the whole string).
        if let g = firstMatch(s, #"^([\w.-]+)/([\w.-]+)#([0-9]+)$"#) {
            return named(owner: g[1], repo: g[2], number: g[3], wantOwner: owner, wantRepo: repo, also: also)
        }
        // A bare number, with an optional leading '#'. ASCII digits only — Int() alone
        // also accepts "+337", which the Python port rejects.
        let bare = s.hasPrefix("#") ? String(s.dropFirst()) : s
        if !bare.isEmpty, bare.allSatisfy({ $0.isASCII && $0.isNumber }),
           let n = Int(bare), n > 0 { return PRRef(number: n, repoMismatch: false) }

        return PRRef(number: nil, repoMismatch: false)
    }

    private static func named(owner: String, repo: String, number: String,
                              wantOwner: String, wantRepo: String,
                              also: [LocalRepo]) -> PRRef {
        let n = Int(number).flatMap { $0 > 0 ? $0 : nil }
        func same(_ o: String, _ r: String) -> Bool {
            owner.caseInsensitiveCompare(o) == .orderedSame
                && repo.caseInsensitiveCompare(r) == .orderedSame
        }
        if same(wantOwner, wantRepo) { return PRRef(number: n, repoMismatch: false) }
        if let local = also.first(where: { same($0.owner, $0.repo) }) {
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
