import Foundation

/// One dense row in the results list (pure data — no UI).
public struct DisplayItem: Identifiable {
    public let id: Int
    public let badge: String      // "#337"
    public let title: String
    public let url: String
    public let line2: String      // primary metadata
    public let line3: String?     // optional detail (skills / files / labels)

    public init(id: Int, badge: String, title: String, url: String, line2: String, line3: String?) {
        self.id = id
        self.badge = badge
        self.title = title
        self.url = url
        self.line2 = line2
        self.line3 = line3
    }
}

/// Reverse-lookup result: which lists a given PR/issue number lands on.
public struct LookupResult {
    public let number: Int
    public let onLists: [ToolKind]
    public let presence: String   // human description of what the number is
    public let url: String?       // canonical url if we know it (cached)
    public var isOnAnyList: Bool { !onLists.isEmpty }

    public init(number: Int, onLists: [ToolKind], presence: String, url: String?) {
        self.number = number
        self.onLists = onLists
        self.presence = presence
        self.url = url
    }
}

/// The tools in the library. Metadata (title / subtitle / icon / colour) comes
/// from the shared `core/catalog.json`, so the macOS and Linux front-ends stay
/// in lockstep. The case set + order mirror the catalog.
public enum ToolKind: String, CaseIterable, Identifiable {
    case skillPRs, installerPRs, staleReady, unaddressedIssues, myApproved, myUnaddressed
    public var id: String { rawValue }

    private static let catalog: [String: CoreAssets.CatalogEntry] = {
        var map: [String: CoreAssets.CatalogEntry] = [:]
        for entry in (try? CoreAssets.catalog()) ?? [] { map[entry.id] = entry }
        return map
    }()
    private var entry: CoreAssets.CatalogEntry? { ToolKind.catalog[rawValue] }

    public var title: String { entry?.title ?? rawValue }
    public var subtitle: String { entry?.subtitle ?? "" }
    /// macOS SF Symbol name.
    public var systemImage: String { entry?.sfSymbol ?? "questionmark.circle" }
    /// Linux emoji glyph.
    public var emoji: String { entry?.emoji ?? "" }
    /// SwiftUI semantic colour name (purple / orange / …).
    public var colorName: String { entry?.color ?? "gray" }
    /// "#RRGGBB" default tint (used by Linux and as the macOS fallback).
    public var colorHex: String { entry?.colorHex ?? "#888888" }
}

/// The pure tool engine: filtering + dense row formatting + reverse lookup,
/// shared by the macOS Store. Mirrors the logic in the Linux store.items_for.
public enum ToolData {
    public static func items(for kind: ToolKind, prs: [OpenPR], issues: [OpenIssue], me: String) -> [DisplayItem] {
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
        case .myApproved:
            return Filters.myApprovedPRs(prs, me: me).sorted { $0.number > $1.number }.map { p in
                DisplayItem(
                    id: p.number, badge: "#\(p.number)", title: p.title, url: p.url,
                    line2: "@\(p.author) · \(Fmt.age(p.createdAt)) · approved · \(p.isDraft ? "draft" : "ready")",
                    line3: nil)
            }
        case .myUnaddressed:
            return Filters.myUnaddressedReviewPRs(prs, me: me).sorted { $0.number > $1.number }.map { p in
                let n = p.unaddressedThreads(me: me).count
                return DisplayItem(
                    id: p.number, badge: "#\(p.number)", title: p.title, url: p.url,
                    line2: "@\(p.author) · \(Fmt.age(p.createdAt)) · \(n) open thread\(n == 1 ? "" : "s")",
                    line3: nil)
            }
        }
    }

    public static func count(for kind: ToolKind, prs: [OpenPR], issues: [OpenIssue], me: String) -> Int {
        items(for: kind, prs: prs, issues: issues, me: me).count
    }

    /// Reverse lookup: which of the `visible` lists does this number appear on?
    /// Pure, cache-only, reusing the exact `items` filters so it can never
    /// disagree with what the lists show.
    public static func lookup(_ number: Int, prs: [OpenPR], issues: [OpenIssue], me: String, visible: [ToolKind]) -> LookupResult {
        let onLists = visible.filter { kind in
            items(for: kind, prs: prs, issues: issues, me: me).contains { $0.id == number }
        }
        if let pr = prs.first(where: { $0.number == number }) {
            return LookupResult(number: number, onLists: onLists,
                                presence: "open PR · @\(pr.author) · \(pr.isDraft ? "draft" : "ready")",
                                url: pr.url)
        }
        if let issue = issues.first(where: { $0.number == number }) {
            return LookupResult(number: number, onLists: onLists,
                                presence: "open issue · @\(issue.author) [\(issue.authorAssociation)]",
                                url: issue.url)
        }
        return LookupResult(number: number, onLists: onLists,
                            presence: "not in open PRs/issues (closed or unknown)",
                            url: nil)
    }
}
