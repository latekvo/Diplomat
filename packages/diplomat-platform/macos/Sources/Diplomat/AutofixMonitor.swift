import Foundation
import DiplomatCore

// macOS side of the PR auto-fix monitor: fetch a snapshot of my open PRs (one GraphQL
// call via the `gh` CLI, or `glab` on a GitLab root) so the shared AutofixDiff can decide
// what changed. The diffing lives in DiplomatCore; the spawn/track happens in Store. This
// file is just the read. The queries themselves live in `assets/graphql/` with the other
// shared queries (single source of truth; the Linux monitor reuses them as-is). A GitLab
// answer is put in its GitHub twin's shape (`GitLab`), so one parser reads both.
enum AutofixMonitor {
    /// One GraphQL search over my open, authored PRs in `root`, with each PR's
    /// mergeability, review verdict, and review-thread resolution — everything the diff
    /// and the reconcilers need, in a single request (`assets/graphql/monitor-prs`,
    /// `gitlab-monitor-prs` on GitLab).
    static func fetchSnapshots(root: PRRef.LocalRepo, me: String) async throws -> [PRSnapshot] {
        let data: Data
        if root.isGitLab {
            data = try GitLab.monitorPRs(try await GLab.graphql(
                "gitlab-monitor-prs", host: root.host,
                variables: [("path", root.slug), ("author", me)]))
        } else {
            let q = "repo:\(root.owner)/\(root.repo) author:\(me) is:pr is:open"
            data = try await GH.graphql("monitor-prs", repo: nil, variables: [("q", q)])
        }
        return try parse(data, me: me)
    }

    /// The items (`PRRef.itemKey`) of the PRs this repo has recently merged or closed:
    /// what the queue is re-checked against (`assets/graphql/closed-prs`).
    ///
    /// Not scoped to me, to my queue, or to a monitor's verb: any PR can be sitting in
    /// the queue (a sweep asks for whoever's PRs the operator aimed it at), and a
    /// search cannot be given a list of numbers to ask about. So it asks the cheap
    /// question the other way round — who closed lately — and the caller intersects.
    static func fetchClosedPRs(root: PRRef.LocalRepo) async throws -> Set<String> {
        let data: Data
        if root.isGitLab {
            data = try GitLab.closedPRs(try await GLab.graphql(
                "gitlab-closed-prs", host: root.host, variables: [("path", root.slug)]))
        } else {
            let q = "repo:\(root.owner)/\(root.repo) is:pr is:closed sort:updated-desc"
            let query = try CoreAssets.graphql("closed-prs")
            data = try await GH.run(["api", "graphql", "-f", "query=\(query)", "-f", "q=\(q)"])
        }
        struct Resp: Decodable {
            let data: D
            struct D: Decodable { let search: S }
            struct S: Decodable { let nodes: [Node] }
            struct Node: Decodable { let number: Int? }
        }
        let r = try JSONDecoder().decode(Resp.self, from: data)
        return Set(r.data.search.nodes.compactMap(\.number).map {
            PRRef.itemKey(owner: root.owner, repo: root.repo, number: $0)
        })
    }

    /// A PR that has requested my review, with the timestamps needed to decide whether I
    /// still owe a review — robustly, without depending on observing a "request removed"
    /// transition (which a re-request can slip past).
    struct ReviewRequest {
        let number: Int
        let title: String
        let url: String
        let author: String
        let authorAssociation: String // OWNER / MEMBER / COLLABORATOR / CONTRIBUTOR / NONE / …
        let files: [String]         // changed file paths (for skill/installer verdict gating)
        let requestedAt: String?    // latest "review requested from me" (ISO8601)
        let myLastReviewAt: String? // my latest review submission (ISO8601)
        let headSha: String         // head commit sha — the mesh work key's "@sha" part
        let myLastCommentAt: String? // my latest top-level comment (ISO8601)

        /// The most recent time I responded to this PR — a formal review submission OR a
        /// top-level comment, whichever is later: a top-level comment of mine answers the
        /// request too, and a review-only signal reads one answered that way as forever-owed. ISO8601 strings compare chronologically, so `max` picks the latest.
        var myLastResponseAt: String? {
            [myLastReviewAt, myLastCommentAt].compactMap { $0 }.max()
        }

        /// I owe a review when I'm requested and that request is newer than my last response
        /// to this PR (review or comment). A fresh re-request (newer timestamp) re-qualifies
        /// even after I responded once.
        var oweReview: Bool {
            guard let r = requestedAt else { return true } // requested but no event detail → assume owed
            guard let last = myLastResponseAt else { return true }
            return r > last
        }
    }

    /// PRs that request MY review, with request/last-review timestamps (one GraphQL
    /// call, `assets/graphql/review-requests`, `gitlab-review-requests` on GitLab).
    /// `includeFiles` pulls each PR's changed paths for the verdict-withhold gate — a big
    /// chunk of the query's rate-limit cost, and pointless unless auto-approvals are on
    /// (verdicts are off by default), so the caller passes `false` then and the
    /// `@include` directive skips it.
    static func fetchReviewRequests(root: PRRef.LocalRepo, me: String,
                                    includeFiles: Bool = false) async throws -> [ReviewRequest] {
        let data: Data
        if root.isGitLab {
            data = try GitLab.reviewRequests(try await GLab.graphql(
                "gitlab-review-requests", host: root.host,
                variables: [("path", root.slug), ("reviewer", me)],
                typedVariables: [("withFiles", "\(includeFiles)")]))
        } else {
            let q = "repo:\(root.owner)/\(root.repo) review-requested:\(me) is:pr is:open"
            data = try await GH.graphql("review-requests", repo: nil,
                                        variables: [("q", q)],
                                        typedVariables: [("withFiles", "\(includeFiles)")])
        }
        struct Resp: Decodable {
            let data: D
            struct D: Decodable { let search: S }
            struct S: Decodable { let nodes: [Node] }
            struct Node: Decodable {
                let number: Int?
                let title: String?
                let url: String?
                let author: Login?
                let authorAssociation: String?
                let headRefOid: String?
                let files: FilesConn?
                let timelineItems: TL?
                let reviews: RVs?
                let comments: CVs?
            }
            struct Login: Decodable { let login: String? }
            struct FilesConn: Decodable { let nodes: [FileNode] }
            struct FileNode: Decodable { let path: String? }
            struct TL: Decodable { let nodes: [Ev] }
            struct Ev: Decodable { let createdAt: String?; let requestedReviewer: Login? }
            struct RVs: Decodable { let nodes: [RV] }
            struct RV: Decodable { let author: Login?; let submittedAt: String? }
            struct CVs: Decodable { let nodes: [CV] }
            struct CV: Decodable { let author: Login?; let createdAt: String? }
        }
        let r = try JSONDecoder().decode(Resp.self, from: data)
        let lower = me.lowercased()
        return r.data.search.nodes.compactMap { n -> ReviewRequest? in
            guard let number = n.number else { return nil }
            let reqAt = (n.timelineItems?.nodes ?? [])
                .filter { $0.requestedReviewer?.login?.lowercased() == lower }
                .compactMap { $0.createdAt }.max()
            let myReviewAt = (n.reviews?.nodes ?? [])
                .filter { $0.author?.login?.lowercased() == lower }
                .compactMap { $0.submittedAt }.max()
            let myCommentAt = (n.comments?.nodes ?? [])
                .filter { $0.author?.login?.lowercased() == lower }
                .compactMap { $0.createdAt }.max()
            return ReviewRequest(number: number, title: n.title ?? "", url: n.url ?? "",
                                 author: n.author?.login ?? "",
                                 authorAssociation: n.authorAssociation ?? "NONE",
                                 files: (n.files?.nodes ?? []).compactMap { $0.path },
                                 requestedAt: reqAt, myLastReviewAt: myReviewAt,
                                 headSha: n.headRefOid ?? "",
                                 myLastCommentAt: myCommentAt)
        }
    }

    /// Decode the GraphQL search response into snapshots. Non-PR search nodes (no
    /// `number`) are skipped. `me` distinguishes threads I still owe a reply on from ones
    /// waiting on the reviewer — via the shared `ThreadTriage.owed` rule (same one the
    /// Tool 6 list uses).
    static func parse(_ data: Data, me: String = "") throws -> [PRSnapshot] {
        struct Resp: Decodable {
            let data: DataField
            struct DataField: Decodable { let search: Search }
            struct Search: Decodable { let nodes: [Node] }
            struct Node: Decodable {
                let number: Int?
                let title: String?
                let url: String?
                let isDraft: Bool?
                let mergeable: String?
                let reviewDecision: String?
                let headRefOid: String?
                let reviewThreads: Threads?
            }
            struct Author: Decodable { let login: String? }
            struct Threads: Decodable { let nodes: [Thread] }
            struct Thread: Decodable {
                let isResolved: Bool
                let viewerCanResolve: Bool?
                let comments: Comments?
                struct Comments: Decodable { let totalCount: Int?; let nodes: [Comment] }
                struct Comment: Decodable { let author: Author?; let body: String? }
            }
        }
        let r = try JSONDecoder().decode(Resp.self, from: data)
        return r.data.search.nodes.compactMap { n -> PRSnapshot? in
            guard let number = n.number else { return nil }
            let threads = n.reviewThreads?.nodes ?? []
            let unresolved = threads.filter { !$0.isResolved }.count
            let iOwe = threads.filter { t in
                ThreadTriage.owed(isResolved: t.isResolved, viewerCanResolve: t.viewerCanResolve,
                                  lastCommentAuthor: t.comments?.nodes.last?.author?.login, me: me,
                                  unansweredRunFinding: ThreadTriage.isUnansweredRunFinding(
                                      commentCount: t.comments?.totalCount,
                                      lastCommentBody: t.comments?.nodes.last?.body))
            }.count
            return PRSnapshot(
                number: number,
                title: n.title ?? "",
                url: n.url ?? "",
                isDraft: n.isDraft ?? false,
                mergeable: n.mergeable ?? "UNKNOWN",
                reviewDecision: n.reviewDecision ?? "",
                threadsUnresolved: unresolved,
                threadsIOwe: iOwe,
                headSha: n.headRefOid ?? "")
        }
    }
}
