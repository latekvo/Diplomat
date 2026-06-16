import SwiftUI

/// One dense row in the results list.
struct DisplayItem: Identifiable {
    let id: Int
    let badge: String      // "#337"
    let title: String
    let url: String
    let line2: String      // primary metadata
    let line3: String?     // optional detail (skills / files / labels)
}

/// The four tools in the library. Each gets a unique SF Symbol + tint so they
/// read at a glance.
enum ToolKind: String, CaseIterable, Identifiable {
    case skillPRs, installerPRs, staleReady, unaddressedIssues
    var id: String { rawValue }

    var title: String {
        switch self {
        case .skillPRs: return "SKILL.md PRs"
        case .installerPRs: return "Installer/CLI PRs"
        case .staleReady: return "Stale Ready >10d"
        case .unaddressedIssues: return "Unaddressed Issues"
        }
    }
    var subtitle: String {
        switch self {
        case .skillPRs: return "open PRs editing a SKILL.md"
        case .installerPRs: return "open PRs in argent-installer / argent-cli"
        case .staleReady: return "non-draft, ready >10 days"
        case .unaddressedIssues: return "external, no team reply/assignee"
        }
    }
    var systemImage: String {
        switch self {
        case .skillPRs: return "book.closed.fill"
        case .installerPRs: return "shippingbox.fill"
        case .staleReady: return "hourglass.tophalf.filled"
        case .unaddressedIssues: return "exclamationmark.bubble.fill"
        }
    }
    var tint: Color {
        switch self {
        case .skillPRs: return .purple
        case .installerPRs: return .orange
        case .staleReady: return .red
        case .unaddressedIssues: return .teal
        }
    }
}

@MainActor
final class Store: ObservableObject {
    @Published var prs: [OpenPR] = []
    @Published var issues: [OpenIssue] = []
    @Published var isLoading = false
    @Published var error: String?
    @Published var lastUpdated: Date?
    @Published var selected: ToolKind = .skillPRs
    @Published var hasLoaded = false

    func refresh() async {
        isLoading = true
        error = nil
        do {
            async let p = API.fetchOpenPRs()
            async let i = API.fetchOpenIssues()
            let (pp, ii) = try await (p, i)
            prs = pp
            issues = ii
            lastUpdated = Date()
            hasLoaded = true
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? "\(error)"
        }
        isLoading = false
    }

    func count(for kind: ToolKind) -> Int { items(for: kind).count }

    func items(for kind: ToolKind) -> [DisplayItem] {
        switch kind {
        case .skillPRs:
            return Filters.skillPRs(prs).sorted { $0.number > $1.number }.map { p in
                let skills = p.files.filter(Filters.isSkillFile).map(Fmt.skillName).joined(separator: ", ")
                return DisplayItem(
                    id: p.number, badge: "#\(p.number)", title: p.title, url: p.url,
                    line2: "@\(p.author) · \(Fmt.age(p.createdAt)) · \(p.isDraft ? "draft" : "ready")",
                    line3: "skills: \(skills)")
            }
        case .installerPRs:
            return Filters.installerPRs(prs).sorted { $0.number > $1.number }.map { p in
                let fs = p.files.filter(Filters.isInstallerFile)
                return DisplayItem(
                    id: p.number, badge: "#\(p.number)", title: p.title, url: p.url,
                    line2: "@\(p.author) · \(Fmt.age(p.createdAt)) · \(fs.count) file\(fs.count == 1 ? "" : "s")",
                    line3: fs.map(Fmt.shortPath).joined(separator: "\n"))
            }
        case .staleReady:
            return Filters.staleReadyPRs(prs).sorted { $0.readyAt < $1.readyAt }.map { p in
                let d = Fmt.days(p.readyAt)
                return DisplayItem(
                    id: p.number, badge: "#\(p.number)", title: p.title, url: p.url,
                    line2: "@\(p.author) · ready \(d)d · \(p.readyForReviewAt == nil ? "born-ready" : "converted")",
                    line3: nil)
            }
        case .unaddressedIssues:
            return Filters.unaddressedExternalIssues(issues).sorted { $0.createdAt < $1.createdAt }.map { i in
                DisplayItem(
                    id: i.number, badge: "#\(i.number)", title: i.title, url: i.url,
                    line2: "@\(i.author) [\(i.authorAssociation)] · \(Fmt.age(i.createdAt)) · \(i.commentCount)c",
                    line3: i.labels.isEmpty ? nil : "labels: \(i.labels.joined(separator: ", "))")
            }
        }
    }
}
