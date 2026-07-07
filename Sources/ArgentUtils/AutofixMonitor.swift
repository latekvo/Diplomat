import Foundation
import ArgentUtilsCore

// macOS side of the PR auto-fix monitor: fetch a snapshot of my open PRs (one GraphQL
// call via the `gh` CLI) so the shared AutofixDiff can decide what changed. The
// diffing lives in ArgentUtilsCore; the spawn/track happens in Store. This file is
// just the GitHub read.
enum AutofixMonitor {
    /// Which open PRs to fetch: the ones I authored (auto-fix), or the ones that have
    /// requested my review (auto-review).
    enum Role {
        case author, reviewRequested
        func qualifier(_ me: String) -> String {
            switch self {
            case .author: return "author:\(me)"
            case .reviewRequested: return "review-requested:\(me)"
            }
        }
    }

    /// One GraphQL search over my open PRs in the target repo (by `role`), with each
    /// PR's mergeability, review verdict, and review-thread resolution — everything the
    /// diff needs, in a single request.
    static func fetchSnapshots(owner: String, repo: String, me: String,
                               role: Role = .author) async throws -> [PRSnapshot] {
        let q = "repo:\(owner)/\(repo) \(role.qualifier(me)) is:pr is:open"
        let query = """
        query($q: String!) {
          search(query: $q, type: ISSUE, first: 100) {
            nodes {
              ... on PullRequest {
                number
                title
                url
                isDraft
                author { login }
                mergeable
                reviewDecision
                headRefName
                reviewThreads(first: 100) {
                  nodes {
                    isResolved
                    viewerCanResolve
                    comments(last: 1) { nodes { author { login } } }
                  }
                }
              }
            }
          }
        }
        """
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
        let headRef: String
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

        var touchesSkill: Bool { files.contains(where: Filters.isSkillFile) }
        var touchesInstaller: Bool { files.contains(where: Filters.isInstallerFile) }
        /// Authored from outside the org (unknown/first-time/outside author).
        var isCommunity: Bool { VerdictPolicy.isCommunity(authorAssociation) }
    }

    /// PRs that request MY review, with request/last-review timestamps (one GraphQL call).
    static func fetchReviewRequests(owner: String, repo: String, me: String) async throws -> [ReviewRequest] {
        let q = "repo:\(owner)/\(repo) review-requested:\(me) is:pr is:open"
        let query = """
        query($q: String!) {
          search(query: $q, type: ISSUE, first: 50) {
            nodes {
              ... on PullRequest {
                number
                title
                url
                headRefName
                author { login }
                authorAssociation
                files(first: 100) { nodes { path } }
                timelineItems(itemTypes: [REVIEW_REQUESTED_EVENT], last: 40) {
                  nodes { ... on ReviewRequestedEvent {
                    createdAt
                    requestedReviewer { __typename ... on User { login } }
                  } }
                }
                reviews(last: 40) { nodes { author { login } submittedAt } }
              }
            }
          }
        }
        """
        let data = try await GH.run(["api", "graphql", "-f", "query=\(query)", "-f", "q=\(q)"])
        struct Resp: Decodable {
            let data: D
            struct D: Decodable { let search: S }
            struct S: Decodable { let nodes: [Node] }
            struct Node: Decodable {
                let number: Int?
                let title: String?
                let url: String?
                let headRefName: String?
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
                                 headRef: n.headRefName ?? "", author: n.author?.login ?? "",
                                 authorAssociation: n.authorAssociation ?? "NONE",
                                 files: (n.files?.nodes ?? []).compactMap { $0.path },
                                 requestedAt: reqAt, myLastReviewAt: myReviewAt)
        }
    }

    /// Decode the GraphQL search response into snapshots. Non-PR search nodes (no
    /// `number`) are skipped. `me` distinguishes threads I still owe a reply on (last
    /// comment isn't mine) from ones waiting on the reviewer.
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
                let author: Author?
                let mergeable: String?
                let reviewDecision: String?
                let headRefName: String?
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
        let lowerMe = me.lowercased()
        return r.data.search.nodes.compactMap { n in
            guard let number = n.number else { return nil }
            let threads = n.reviewThreads?.nodes ?? []
            let unresolved = threads.filter { !$0.isResolved }.count
            // Threads I owe a reply on: unresolved, I can resolve them, and the latest
            // comment isn't mine (so the reviewer is waiting on me, not the other way).
            let iOwe = threads.filter { t in
                guard !t.isResolved, t.viewerCanResolve ?? true else { return false }
                let last = t.comments?.nodes.last?.author?.login?.lowercased()
                return last != lowerMe
            }.count
            return PRSnapshot(
                number: number,
                title: n.title ?? "",
                url: n.url ?? "",
                headRef: n.headRefName ?? "",
                isDraft: n.isDraft ?? false,
                author: n.author?.login ?? "",
                mergeable: n.mergeable ?? "UNKNOWN",
                reviewDecision: n.reviewDecision ?? "",
                threadsUnresolved: unresolved,
                threadsIOwe: iOwe)
        }
    }
}
