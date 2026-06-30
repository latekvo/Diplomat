import Foundation

// MARK: - Domain models

public struct OpenPR: Identifiable {
    public let number: Int
    public let title: String
    public let url: String
    public let isDraft: Bool
    public let author: String
    public let createdAt: Date
    /// Timestamp of the last "marked ready for review" event, if the PR was ever
    /// a draft that got converted. nil means it was opened ready ("born ready").
    public let readyForReviewAt: Date?
    public let files: [String]
    /// GitHub's aggregate review state: "APPROVED" / "CHANGES_REQUESTED" /
    /// "REVIEW_REQUIRED", or nil when no review is required.
    public let reviewDecision: String?
    public let reviewThreads: [ReviewThread]

    public var id: Int { number }
    /// Best-effort "has been ready since" timestamp.
    public var readyAt: Date { readyForReviewAt ?? createdAt }

    public init(number: Int, title: String, url: String, isDraft: Bool, author: String,
                createdAt: Date, readyForReviewAt: Date?, files: [String],
                reviewDecision: String?, reviewThreads: [ReviewThread]) {
        self.number = number
        self.title = title
        self.url = url
        self.isDraft = isDraft
        self.author = author
        self.createdAt = createdAt
        self.readyForReviewAt = readyForReviewAt
        self.files = files
        self.reviewDecision = reviewDecision
        self.reviewThreads = reviewThreads
    }

    /// Review threads on *my* PR that I still owe a response on: resolvable, not
    /// resolved, and whose most-recent comment isn't mine.
    public func unaddressedThreads(me: String) -> [ReviewThread] {
        reviewThreads.filter { $0.viewerCanResolve && !$0.isResolved && $0.lastCommentAuthor != me }
    }
}

/// A PR review conversation thread (one inline comment chain).
public struct ReviewThread {
    public let isResolved: Bool
    public let viewerCanResolve: Bool
    public let lastCommentAuthor: String?

    public init(isResolved: Bool, viewerCanResolve: Bool, lastCommentAuthor: String?) {
        self.isResolved = isResolved
        self.viewerCanResolve = viewerCanResolve
        self.lastCommentAuthor = lastCommentAuthor
    }
}

public struct OpenIssue: Identifiable {
    public let number: Int
    public let title: String
    public let url: String
    public let author: String
    public let authorAssociation: String
    public let createdAt: Date
    public let updatedAt: Date
    public let commentCount: Int
    public let assignees: [String]
    public let labels: [String]
    public let memberResponded: Bool

    public var id: Int { number }
    public var isExternal: Bool { !Filters.orgAssociations.contains(authorAssociation) }
    public var isAddressed: Bool { memberResponded || !assignees.isEmpty }

    public init(number: Int, title: String, url: String, author: String,
                authorAssociation: String, createdAt: Date, updatedAt: Date,
                commentCount: Int, assignees: [String], labels: [String],
                memberResponded: Bool) {
        self.number = number
        self.title = title
        self.url = url
        self.author = author
        self.authorAssociation = authorAssociation
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.commentCount = commentCount
        self.assignees = assignees
        self.labels = labels
        self.memberResponded = memberResponded
    }
}

// MARK: - Filters (the tool logic, data-driven from core/filters.json)

public enum Filters {
    private static let cfg: CoreAssets.Filters? = try? CoreAssets.filters()

    public static func isSkillFile(_ path: String) -> Bool {
        guard let suffix = cfg?.skillSuffix else { return false }
        return path.lowercased().hasSuffix(suffix)
    }
    public static var installerPrefixes: [String] { cfg?.installerPrefixes ?? [] }
    public static func isInstallerFile(_ path: String) -> Bool {
        installerPrefixes.contains { path.contains($0) }
    }
    /// Associations that count as "the team already touched this".
    public static let team: Set<String> = Set((try? CoreAssets.filters())?.team ?? [])
    /// Associations that count as "an org member authored this".
    public static let orgAssociations: Set<String> =
        Set((try? CoreAssets.filters())?.orgAssociations ?? [])

    public static func skillPRs(_ prs: [OpenPR]) -> [OpenPR] {
        prs.filter { $0.files.contains(where: isSkillFile) }
    }
    public static func installerPRs(_ prs: [OpenPR]) -> [OpenPR] {
        prs.filter { $0.files.contains(where: isInstallerFile) }
    }
    public static func staleReadyPRs(_ prs: [OpenPR], now: Date = Date()) -> [OpenPR] {
        let days = Double(cfg?.staleReadyDays ?? 10)
        return prs.filter { !$0.isDraft && now.timeIntervalSince($0.readyAt) > days * 86400 }
    }
    public static func unaddressedExternalIssues(_ issues: [OpenIssue]) -> [OpenIssue] {
        issues.filter { $0.isExternal && !$0.isAddressed }
    }
    public static func myApprovedPRs(_ prs: [OpenPR], me: String) -> [OpenPR] {
        guard !me.isEmpty else { return [] }
        let approved = cfg?.approvedDecision ?? "APPROVED"
        return prs.filter { $0.author == me && $0.reviewDecision == approved }
    }
    public static func myUnaddressedReviewPRs(_ prs: [OpenPR], me: String) -> [OpenPR] {
        guard !me.isEmpty else { return [] }
        return prs.filter { $0.author == me && !$0.unaddressedThreads(me: me).isEmpty }
    }
}

// MARK: - Tiny formatting helpers

public enum Fmt {
    public static func age(_ date: Date, now: Date = Date()) -> String {
        let s = max(0, now.timeIntervalSince(date))
        if s >= 86400 { return "\(Int(s / 86400))d" }
        if s >= 3600 { return "\(Int(s / 3600))h" }
        return "\(Int(s / 60))m"
    }
    public static func days(_ date: Date, now: Date = Date()) -> Int {
        Int(max(0, now.timeIntervalSince(date)) / 86400)
    }
    /// ".../argent-native-profiler/SKILL.md" -> "argent-native-profiler"
    public static func skillName(_ path: String) -> String {
        let parts = path.split(separator: "/")
        return parts.count >= 2 ? String(parts[parts.count - 2]) : path
    }
    public static func shortPath(_ path: String) -> String {
        path.replacingOccurrences(of: "packages/", with: "")
    }
    public static func clock(_ date: Date?) -> String {
        guard let date else { return "—" }
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f.string(from: date)
    }
}

// MARK: - GitHub API (GraphQL via the gh CLI)

public enum API {
    /// The authenticated user's GitHub login — needed to find "my" PRs.
    public static func fetchViewerLogin() async throws -> String {
        try await graphqlDecoded("viewer", withRepo: false, as: ViewerResponse.self).data.viewer.login
    }

    public static func fetchOpenPRs() async throws -> [OpenPR] {
        let resp = try await graphqlDecoded("prs", withRepo: true, as: PRResponse.self)
        return resp.data.repository.pullRequests.nodes.map { n in
            OpenPR(
                number: n.number,
                title: n.title,
                url: n.url,
                isDraft: n.isDraft,
                author: n.author?.login ?? "ghost",
                createdAt: n.createdAt,
                readyForReviewAt: n.timelineItems.nodes.compactMap { $0.createdAt }.first,
                files: n.files.nodes.map { $0.path },
                reviewDecision: n.reviewDecision,
                reviewThreads: n.reviewThreads.nodes.map { t in
                    ReviewThread(
                        isResolved: t.isResolved,
                        viewerCanResolve: t.viewerCanResolve,
                        lastCommentAuthor: t.comments.nodes.last?.author?.login
                    )
                }
            )
        }
    }

    /// The lifecycle state of a single PR — "OPEN", "CLOSED", or "MERGED". Used to
    /// mark a tracked agent session's PR as merged once it lands. One cheap
    /// `gh pr view` per tracked PR; the repo coordinates come from the shared config.
    public static func fetchPRState(number: Int) async throws -> String {
        let cfg = try? CoreAssets.config()
        let owner = cfg?.owner ?? "software-mansion"
        let repo = cfg?.repo ?? "argent"
        let data = try await GH.run(
            ["pr", "view", "\(number)", "--repo", "\(owner)/\(repo)", "--json", "state"])
        struct StateResponse: Decodable { let state: String }
        return try JSONDecoder().decode(StateResponse.self, from: data).state
    }

    public static func fetchOpenIssues() async throws -> [OpenIssue] {
        let resp = try await graphqlDecoded("issues", withRepo: true, as: IssueResponse.self)
        return resp.data.repository.issues.nodes.map { n in
            OpenIssue(
                number: n.number,
                title: n.title,
                url: n.url,
                author: n.author?.login ?? "ghost",
                authorAssociation: n.authorAssociation,
                createdAt: n.createdAt,
                updatedAt: n.updatedAt,
                commentCount: n.comments.totalCount,
                assignees: n.assignees.nodes.map { $0.login },
                labels: n.labels.nodes.map { $0.name },
                memberResponded: n.comments.nodes.contains { Filters.team.contains($0.authorAssociation) }
            )
        }
    }

    /// Run a GraphQL query and decode it, retrying once on failure. GitHub
    /// intermittently times these heavier queries out, so a single retry turns a
    /// transient blip into a non-event.
    private static func graphqlDecoded<T: Decodable>(_ queryName: String, withRepo: Bool, as: T.Type) async throws -> T {
        var lastError: Error?
        for attempt in 0..<2 {
            do {
                let data = try await GH.graphql(queryName, withRepo: withRepo)
                try checkGraphQLErrors(data)
                return try makeDecoder().decode(T.self, from: data)
            } catch {
                lastError = error
                if attempt == 0 { try? await Task.sleep(nanoseconds: 800_000_000) }
            }
        }
        throw lastError!
    }

    private static func makeDecoder() -> JSONDecoder {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .iso8601
        return d
    }

    private static func checkGraphQLErrors(_ data: Data) throws {
        if let env = try? JSONDecoder().decode(GQLEnvelope.self, from: data),
           let errs = env.errors, !errs.isEmpty {
            throw GHError.graphql(messages: errs.map { $0.message })
        }
    }
}

// MARK: - GraphQL decode shapes (private to this file)

private struct GQLEnvelope: Decodable {
    struct E: Decodable { let message: String }
    let errors: [E]?
}

private struct ViewerResponse: Decodable {
    let data: D
    struct D: Decodable { let viewer: V }
    struct V: Decodable { let login: String }
}

private struct PRResponse: Decodable {
    let data: D
    struct D: Decodable { let repository: Repo }
    struct Repo: Decodable { let pullRequests: Conn }
    struct Conn: Decodable { let nodes: [Node] }
    struct Node: Decodable {
        let number: Int
        let title: String
        let url: String
        let isDraft: Bool
        let createdAt: Date
        let reviewDecision: String?
        let author: Author?
        let files: Files
        let timelineItems: Timeline
        let reviewThreads: Threads
    }
    struct Author: Decodable { let login: String }
    struct Files: Decodable { let nodes: [PathNode] }
    struct PathNode: Decodable { let path: String }
    struct Timeline: Decodable { let nodes: [TLNode] }
    struct TLNode: Decodable { let createdAt: Date? }
    struct Threads: Decodable { let nodes: [Thread] }
    struct Thread: Decodable {
        let isResolved: Bool
        let viewerCanResolve: Bool
        let comments: ThreadComments
    }
    struct ThreadComments: Decodable {
        let nodes: [ThreadComment]
        struct ThreadComment: Decodable { let author: Author? }
    }
}

private struct IssueResponse: Decodable {
    let data: D
    struct D: Decodable { let repository: Repo }
    struct Repo: Decodable { let issues: Conn }
    struct Conn: Decodable { let nodes: [Node] }
    struct Node: Decodable {
        let number: Int
        let title: String
        let url: String
        let createdAt: Date
        let updatedAt: Date
        let authorAssociation: String
        let author: Author?
        let assignees: Logins
        let labels: Names
        let comments: Comments
    }
    struct Author: Decodable { let login: String }
    struct Logins: Decodable {
        let nodes: [Item]
        struct Item: Decodable { let login: String }
    }
    struct Names: Decodable {
        let nodes: [Item]
        struct Item: Decodable { let name: String }
    }
    struct Comments: Decodable {
        let totalCount: Int
        let nodes: [Item]
        struct Item: Decodable { let authorAssociation: String }
    }
}
