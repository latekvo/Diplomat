import Foundation
import CoMaintainerCore

// macOS side of the PR auto-fix monitor: fetch a snapshot of my open PRs (one GraphQL
// call via the `gh` CLI) so the shared AutofixDiff can decide what changed. The
// diffing lives in CoMaintainerCore; the spawn/track happens in Store. This file is
// just the GitHub read. The queries themselves live in `core/graphql/` with the other
// shared queries (single source of truth; a future Linux monitor reuses them as-is).
enum AutofixMonitor {
    /// One GraphQL search over my open, authored PRs in the target repo, with each
    /// PR's mergeability, review verdict, and review-thread resolution — everything the
    /// diff and the reconcilers need, in a single request (`core/graphql/monitor-prs`).
    static func fetchSnapshots(owner: String, repo: String, me: String) async throws -> [PRSnapshot] {
        let q = "repo:\(owner)/\(repo) author:\(me) is:pr is:open"
        let query = try CoreAssets.graphql("monitor-prs")
        let data = try await GH.run(["api", "graphql", "-f", "query=\(query)", "-f", "q=\(q)"])
        return try parse(data, me: me)
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

        /// I owe a review when I'm requested and that request is newer than my last review
        /// of this PR (ISO8601 strings compare chronologically). A fresh re-request (newer
        /// timestamp) re-qualifies even after I reviewed once.
        var oweReview: Bool {
            guard let r = requestedAt else { return true } // requested but no event detail → assume owed
            guard let m = myLastReviewAt else { return true }
            return r > m
        }
    }

    /// PRs that request MY review, with request/last-review timestamps (one GraphQL
    /// call, `core/graphql/review-requests`). `includeFiles` pulls each PR's changed
    /// paths for the verdict-withhold gate — a big chunk of the query's rate-limit
    /// cost, and pointless unless auto-approvals are on (verdicts are off by default),
    /// so the caller passes `false` then and the `@include` directive skips it.
    static func fetchReviewRequests(owner: String, repo: String, me: String,
                                    includeFiles: Bool = false) async throws -> [ReviewRequest] {
        let q = "repo:\(owner)/\(repo) review-requested:\(me) is:pr is:open"
        let query = try CoreAssets.graphql("review-requests")
        let data = try await GH.run(["api", "graphql", "-f", "query=\(query)", "-f", "q=\(q)",
                                     "-F", "withFiles=\(includeFiles)"])
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
                let files: FilesConn?
                let timelineItems: TL?
                let reviews: RVs?
            }
            struct Login: Decodable { let login: String? }
            struct FilesConn: Decodable { let nodes: [FileNode] }
            struct FileNode: Decodable { let path: String? }
            struct TL: Decodable { let nodes: [Ev] }
            struct Ev: Decodable { let createdAt: String?; let requestedReviewer: Login? }
            struct RVs: Decodable { let nodes: [RV] }
            struct RV: Decodable { let author: Login?; let submittedAt: String? }
        }
        let r = try JSONDecoder().decode(Resp.self, from: data)
        let lower = me.lowercased()
        return r.data.search.nodes.compactMap { n in
            guard let number = n.number else { return nil }
            let reqAt = (n.timelineItems?.nodes ?? [])
                .filter { $0.requestedReviewer?.login?.lowercased() == lower }
                .compactMap { $0.createdAt }.max()
            let myReviewAt = (n.reviews?.nodes ?? [])
                .filter { $0.author?.login?.lowercased() == lower }
                .compactMap { $0.submittedAt }.max()
            return ReviewRequest(number: number, title: n.title ?? "", url: n.url ?? "",
                                 author: n.author?.login ?? "",
                                 authorAssociation: n.authorAssociation ?? "NONE",
                                 files: (n.files?.nodes ?? []).compactMap { $0.path },
                                 requestedAt: reqAt, myLastReviewAt: myReviewAt)
        }
    }

    /// Decode the GraphQL search response into snapshots. Non-PR search nodes (no
    /// `number`) are skipped. `me` distinguishes threads I still owe a reply on (last
    /// comment isn't mine) from ones waiting on the reviewer — via the shared
    /// `ThreadTriage.owed` rule (same one the Tool 6 list uses).
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
                let reviewThreads: Threads?
            }
            struct Author: Decodable { let login: String? }
            struct Threads: Decodable { let nodes: [Thread] }
            struct Thread: Decodable {
                let isResolved: Bool
                let viewerCanResolve: Bool?
                let comments: Comments?
                struct Comments: Decodable { let nodes: [Comment] }
                struct Comment: Decodable { let author: Author? }
            }
        }
        let r = try JSONDecoder().decode(Resp.self, from: data)
        return r.data.search.nodes.compactMap { n in
            guard let number = n.number else { return nil }
            let threads = n.reviewThreads?.nodes ?? []
            let unresolved = threads.filter { !$0.isResolved }.count
            let iOwe = threads.filter { t in
                ThreadTriage.owed(isResolved: t.isResolved, viewerCanResolve: t.viewerCanResolve,
                                  lastCommentAuthor: t.comments?.nodes.last?.author?.login, me: me)
            }.count
            return PRSnapshot(
                number: number,
                title: n.title ?? "",
                url: n.url ?? "",
                isDraft: n.isDraft ?? false,
                mergeable: n.mergeable ?? "UNKNOWN",
                reviewDecision: n.reviewDecision ?? "",
                threadsUnresolved: unresolved,
                threadsIOwe: iOwe)
        }
    }
}
