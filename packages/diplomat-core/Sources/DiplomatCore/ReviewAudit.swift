import Foundation

/// Whether a finished review run's review went through GitHub's lifecycle.
///
/// The prompt has the agent open one review PENDING, put every comment inside it, and
/// submit it (`assets/review.json` → `reviewLifecycle`). Two ways of not doing that look like
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
        public struct Comment: Decodable, Equatable {
            public let body: String?
            public let replyTo: Reply?
        }
        public struct Reply: Decodable, Equatable { public let id: String? }
        public let author: Login?
        public let reviews: Reviews?
    }

    /// What every comment a run posts opens with (`assets/review.json` → `diplomatTag`).
    public static let tag = #"\[[Diplomat]("#

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
    /// Comments tell a run's review from mine, since a run tags every one. A PENDING review
    /// is the run's when every comment in it is tagged — however old, as a run continues a
    /// tagged one an earlier run left — or when it is empty and was opened since the run
    /// started. One with an untagged comment is my draft, which the run is told to leave
    /// alone. A split counts the reviews submitted since the run started that open a tagged
    /// thread, and is not judged on a PR of my own, where a run replies rather than reviews.
    public static func findings(_ pr: PullRequest, me: String, since: TimeInterval) -> [String] {
        let reviews = (pr.reviews?.nodes ?? []).compactMap { $0 }
        var out: [String] = []
        func comments(_ rv: PullRequest.Review) -> [PullRequest.Comment] {
            (rv.comments?.nodes ?? []).compactMap { $0 }
        }
        func tagged(_ c: PullRequest.Comment) -> Bool {
            (c.body ?? "").drop(while: \.isWhitespace).hasPrefix(tag)
        }
        if reviews.contains(where: { rv in
            rv.state == "PENDING" && (comments(rv).isEmpty ? (at(rv.createdAt) ?? 0) >= since
                                                           : comments(rv).allSatisfy(tagged))
        }) {
            out.append("left its review PENDING — never submitted, so only I can see it")
        }
        let author = (pr.author?.login ?? "").lowercased()
        if !author.isEmpty, author != me.lowercased() {
            let opened = reviews.filter { rv in
                (at(rv.submittedAt) ?? 0) >= since
                    && comments(rv).contains { $0.replyTo == nil && tagged($0) }
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
        guard let c = coordinates(prURL) else { return [] }
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
