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
                mergeable
                reviewDecision
                headRefName
                reviewThreads(first: 100) { nodes { isResolved } }
              }
            }
          }
        }
        """
        let data = try await GH.run(["api", "graphql", "-f", "query=\(query)", "-f", "q=\(q)"])
        return try parse(data)
    }

    /// Decode the GraphQL search response into snapshots. Non-PR search nodes (no
    /// `number`) are skipped.
    static func parse(_ data: Data) throws -> [PRSnapshot] {
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
                let headRefName: String?
                let reviewThreads: Threads?
            }
            struct Threads: Decodable { let nodes: [Thread] }
            struct Thread: Decodable { let isResolved: Bool }
        }
        let r = try JSONDecoder().decode(Resp.self, from: data)
        return r.data.search.nodes.compactMap { n in
            guard let number = n.number else { return nil }
            let unresolved = (n.reviewThreads?.nodes ?? []).filter { !$0.isResolved }.count
            return PRSnapshot(
                number: number,
                title: n.title ?? "",
                url: n.url ?? "",
                headRef: n.headRefName ?? "",
                isDraft: n.isDraft ?? false,
                mergeable: n.mergeable ?? "UNKNOWN",
                reviewDecision: n.reviewDecision ?? "",
                threadsUnresolved: unresolved)
        }
    }
}
