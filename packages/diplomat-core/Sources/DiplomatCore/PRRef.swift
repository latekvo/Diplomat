import Foundation

/// A PR reference parsed from a single-PR text field.
///
/// The field accepts a bare number (`337`, `#337`), a full GitHub PR URL
/// (`https://github.com/owner/repo/pull/337`, with any trailing path/query), or the
/// `owner/repo#337` shorthand. When the input names a repo (URL or shorthand) it's
/// checked against the configured target repo, so a link to the wrong project is
/// rejected instead of silently reviewing the wrong PR. Shared verbatim with the
/// Linux front-end (see `prref.py`).
public struct PRRef: Equatable {
    /// The extracted PR number, or nil when the input contains a usable one.
    public let number: Int?
    /// True only when the input named a repo that does NOT match the target. A bare
    /// number names no repo, so it never mismatches.
    public let repoMismatch: Bool

    public init(number: Int?, repoMismatch: Bool) {
        self.number = number
        self.repoMismatch = repoMismatch
    }

    /// A usable reference: a number was found and any named repo matched.
    public var isValid: Bool { number != nil && !repoMismatch }
    /// The bare number for prompt injection ("" when none).
    public var numberString: String { number.map(String.init) ?? "" }

    /// Parse `raw` against the expected `owner`/`repo` (compared case-insensitively).
    public static func parse(_ raw: String, owner: String, repo: String) -> PRRef {
        let s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if s.isEmpty { return PRRef(number: nil, repoMismatch: false) }

        // A GitHub PR URL anywhere in the input: github.com/OWNER/REPO/pull/N.
        // [0-9], not \d: ICU's \d matches non-ASCII digits, which Int() then rejects —
        // and the Python port matches ASCII only, so the two sides would disagree.
        if let g = firstMatch(s, #"(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/pull/([0-9]+)"#) {
            return named(owner: g[1], repo: g[2], number: g[3], wantOwner: owner, wantRepo: repo)
        }
        // The OWNER/REPO#N shorthand (the whole string).
        if let g = firstMatch(s, #"^([\w.-]+)/([\w.-]+)#([0-9]+)$"#) {
            return named(owner: g[1], repo: g[2], number: g[3], wantOwner: owner, wantRepo: repo)
        }
        // A bare number, with an optional leading '#'. ASCII digits only — Int() alone
        // also accepts "+337", which the Python port rejects.
        let bare = s.hasPrefix("#") ? String(s.dropFirst()) : s
        if !bare.isEmpty, bare.allSatisfy({ $0.isASCII && $0.isNumber }),
           let n = Int(bare), n > 0 { return PRRef(number: n, repoMismatch: false) }

        return PRRef(number: nil, repoMismatch: false)
    }

    private static func named(owner: String, repo: String, number: String,
                              wantOwner: String, wantRepo: String) -> PRRef {
        let n = Int(number).flatMap { $0 > 0 ? $0 : nil }
        let matches = owner.caseInsensitiveCompare(wantOwner) == .orderedSame
            && repo.caseInsensitiveCompare(wantRepo) == .orderedSame
        return PRRef(number: n, repoMismatch: !matches)
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
