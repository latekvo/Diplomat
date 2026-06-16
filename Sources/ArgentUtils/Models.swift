import Foundation

// MARK: - Domain models

struct OpenPR: Identifiable {
    let number: Int
    let title: String
    let url: String
    let isDraft: Bool
    let author: String
    let createdAt: Date
    /// Timestamp of the last "marked ready for review" event, if the PR was ever
    /// a draft that got converted. nil means it was opened ready ("born ready").
    let readyForReviewAt: Date?
    let files: [String]

    var id: Int { number }
    /// Best-effort "has been ready since" timestamp.
    var readyAt: Date { readyForReviewAt ?? createdAt }
}

struct OpenIssue: Identifiable {
    let number: Int
    let title: String
    let url: String
    let author: String
    let authorAssociation: String
    let createdAt: Date
    let updatedAt: Date
    let commentCount: Int
    let assignees: [String]
    let labels: [String]
    /// True if anyone with write-ish association (member/owner/collaborator) commented.
    let memberResponded: Bool

    var id: Int { number }
    var isExternal: Bool { !["MEMBER", "OWNER"].contains(authorAssociation) }
    var isAddressed: Bool { memberResponded || !assignees.isEmpty }
}

// MARK: - Filters (the actual tool logic, kept pure & testable)

enum Filters {
    static func isSkillFile(_ path: String) -> Bool { path.lowercased().hasSuffix("skill.md") }

    /// "Argent installer CLI code" — interpreted broadly as the installer package
    /// plus the shell CLI package. Tweak here if you want it narrower.
    static let installerPrefixes = ["packages/argent-installer/", "packages/argent-cli/"]
    static func isInstallerFile(_ path: String) -> Bool {
        installerPrefixes.contains { path.contains($0) }
    }

    /// Associations that count as "the team already touched this".
    static let team: Set<String> = ["MEMBER", "OWNER", "COLLABORATOR"]

    static func skillPRs(_ prs: [OpenPR]) -> [OpenPR] {
        prs.filter { $0.files.contains(where: isSkillFile) }
    }
    static func installerPRs(_ prs: [OpenPR]) -> [OpenPR] {
        prs.filter { $0.files.contains(where: isInstallerFile) }
    }
    static func staleReadyPRs(_ prs: [OpenPR], now: Date = Date()) -> [OpenPR] {
        prs.filter { !$0.isDraft && now.timeIntervalSince($0.readyAt) > 10 * 86400 }
    }
    static func unaddressedExternalIssues(_ issues: [OpenIssue]) -> [OpenIssue] {
        issues.filter { $0.isExternal && !$0.isAddressed }
    }
}

// MARK: - Tiny formatting helpers

enum Fmt {
    static func age(_ date: Date, now: Date = Date()) -> String {
        let s = max(0, now.timeIntervalSince(date))
        if s >= 86400 { return "\(Int(s / 86400))d" }
        if s >= 3600 { return "\(Int(s / 3600))h" }
        return "\(Int(s / 60))m"
    }
    static func days(_ date: Date, now: Date = Date()) -> Int {
        Int(max(0, now.timeIntervalSince(date)) / 86400)
    }
    /// ".../argent-native-profiler/SKILL.md" -> "argent-native-profiler"
    static func skillName(_ path: String) -> String {
        let parts = path.split(separator: "/")
        return parts.count >= 2 ? String(parts[parts.count - 2]) : path
    }
    static func shortPath(_ path: String) -> String {
        path.replacingOccurrences(of: "packages/", with: "")
    }
    static func clock(_ date: Date?) -> String {
        guard let date else { return "—" }
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f.string(from: date)
    }
}

// MARK: - GitHub API (GraphQL via the gh CLI)

enum API {
    static func fetchOpenPRs() async throws -> [OpenPR] {
        let q = """
        { repository(owner: "\(GH.owner)", name: "\(GH.repo)") {
            pullRequests(states: OPEN, first: 100, orderBy: {field: CREATED_AT, direction: DESC}) {
              nodes {
                number title url isDraft createdAt
                author { login }
                files(first: 100) { nodes { path } }
                timelineItems(itemTypes: [READY_FOR_REVIEW_EVENT], last: 1) {
                  nodes { __typename ... on ReadyForReviewEvent { createdAt } }
                }
              }
            }
        } }
        """
        let data = try await GH.graphql(q)
        try checkGraphQLErrors(data)
        let resp = try makeDecoder().decode(PRResponse.self, from: data)
        return resp.data.repository.pullRequests.nodes.map { n in
            OpenPR(
                number: n.number,
                title: n.title,
                url: n.url,
                isDraft: n.isDraft,
                author: n.author?.login ?? "ghost",
                createdAt: n.createdAt,
                readyForReviewAt: n.timelineItems.nodes.compactMap { $0.createdAt }.first,
                files: n.files.nodes.map { $0.path }
            )
        }
    }

    static func fetchOpenIssues() async throws -> [OpenIssue] {
        let q = """
        { repository(owner: "\(GH.owner)", name: "\(GH.repo)") {
            issues(states: OPEN, first: 100, orderBy: {field: CREATED_AT, direction: DESC}) {
              nodes {
                number title url createdAt updatedAt authorAssociation
                author { login }
                assignees(first: 10) { nodes { login } }
                labels(first: 20) { nodes { name } }
                comments(first: 100) { totalCount nodes { authorAssociation } }
              }
            }
        } }
        """
        let data = try await GH.graphql(q)
        try checkGraphQLErrors(data)
        let resp = try makeDecoder().decode(IssueResponse.self, from: data)
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
        let author: Author?
        let files: Files
        let timelineItems: Timeline
    }
    struct Author: Decodable { let login: String }
    struct Files: Decodable { let nodes: [PathNode] }
    struct PathNode: Decodable { let path: String }
    struct Timeline: Decodable { let nodes: [TLNode] }
    struct TLNode: Decodable { let createdAt: Date? }
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
