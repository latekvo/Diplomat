import Foundation

/// Whether a finished review run's review went through GitHub's lifecycle.
///
/// The prompt has the agent open one review PENDING, put every comment inside it, and
/// submit it (`assets/review.json` → `leaveReviews`). Two ways of not doing that look like
/// success from here: a review left PENDING is visible to nobody but me, and findings
/// posted one at a time each become a review of their own. When a review run retires, the
/// store reads my reviews on its PR (`assets/graphql/my-reviews`) and logs what `findings`
/// names. Mirrored by `reviewaudit.py`.
public enum ReviewAudit {
    public struct PullRequest: Decodable, Equatable {
        public struct Login: Decodable, Equatable { public let login: String? }
        public struct Reviews: Decodable, Equatable { public let nodes: [Review?] }
        public struct Review: Decodable, Equatable {
            public let state: String?
            public let createdAt: String?
            public let submittedAt: String?
            public let comments: Comments?
        }
        public struct Comments: Decodable, Equatable { public let nodes: [Comment?] }
        public struct Comment: Decodable, Equatable { public let replyTo: Reply? }
        public struct Reply: Decodable, Equatable { public let id: String? }
        public let author: Login?
        public let reviews: Reviews?
    }

    /// `(owner, repo, number)` for a github.com PR URL, else nil — gh's GraphQL answers
    /// for its default host only.
    public static func coordinates(_ prURL: String) -> (owner: String, repo: String, number: Int)? {
        let s = prURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let re = try? NSRegularExpression(
                pattern: #"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/pull/([0-9]+)/?$"#),
              let m = re.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)),
              let o = Range(m.range(at: 1), in: s), let r = Range(m.range(at: 2), in: s),
              let n = Range(m.range(at: 3), in: s), let number = Int(s[n])
        else { return nil }
        return (String(s[o]), String(s[r]), number)
    }

    private static func at(_ iso: String?) -> TimeInterval? {
        guard let iso else { return nil }
        return ISO8601DateFormatter().date(from: iso)?.timeIntervalSince1970
    }

    /// What the reviews of mine on one PR say a run that started at `since` left wrong,
    /// as sentences for the activity feed.
    ///
    /// Only what the run itself made is judged: a PENDING review opened earlier may be my
    /// own draft in the browser. A split is only judged on someone else's PR, the only
    /// kind the prompt has a run review rather than reply on.
    public static func findings(_ pr: PullRequest, me: String, since: TimeInterval) -> [String] {
        let reviews = (pr.reviews?.nodes ?? []).compactMap { $0 }
        var out: [String] = []
        if reviews.contains(where: { $0.state == "PENDING" && (at($0.createdAt) ?? 0) >= since }) {
            out.append("left its review PENDING — never submitted, so only I can see it")
        }
        let author = (pr.author?.login ?? "").lowercased()
        if !author.isEmpty, author != me.lowercased() {
            let opened = reviews.filter { rv in
                (at(rv.submittedAt) ?? 0) >= since
                    && (rv.comments?.nodes ?? []).contains { $0 != nil && $0?.replyTo == nil }
            }.count
            if opened > 1 {
                out.append("posted its findings as \(opened) separate reviews instead of one")
            }
        }
        return out
    }

    /// Read my reviews on `prURL` and return `findings`. `[]` for a URL that names no
    /// github.com PR. Throws when GitHub cannot be asked.
    public static func audit(prURL: String, me: String, since: TimeInterval) async throws -> [String] {
        guard let c = coordinates(prURL), !me.isEmpty else { return [] }
        let data = try await GH.graphql("my-reviews", withRepo: false,
                                        variables: [("owner", c.owner), ("name", c.repo), ("me", me)],
                                        typedVariables: [("number", String(c.number))])
        struct Resp: Decodable {
            let data: D?
            let errors: [Err]?
            struct D: Decodable { let repository: Repo? }
            struct Repo: Decodable { let pullRequest: PullRequest? }
            struct Err: Decodable { let message: String? }
        }
        let resp = try JSONDecoder().decode(Resp.self, from: data)
        if let errs = resp.errors, !errs.isEmpty {
            throw GHError.graphql(messages: errs.map { $0.message ?? "?" })
        }
        guard let pr = resp.data?.repository?.pullRequest else { return [] }
        return findings(pr, me: me, since: since)
    }
}
