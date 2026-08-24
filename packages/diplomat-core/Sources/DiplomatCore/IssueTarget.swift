import Foundation

/// Which of the repo's open issues a Fix-issues run works on (mirrored in the shared
/// runtime's `issuetarget.py`).
///
/// Wider than the whose-PRs axis the Review and Resolve-conflicts wizards share
/// (`PRTarget`), because an issue's AUTHOR ASSOCIATION is a scope in its own right:
/// "everything the community filed" and "everything the org filed" are the two cuts
/// a triage sweep actually wants, and neither of them is one @handle.
///
/// Read by the front-end, which enumerates the scope itself (`Filters.sweptIssues`)
/// and queues one run per issue; the prompt an agent gets names one issue and never
/// a scope.
public enum IssueTarget: Int, CaseIterable, Identifiable, Codable {
    case all, mine, someone, contributors, members, specific

    public var id: Int { rawValue }

    public var title: String {
        switch self {
        case .all:          return "All open issues"
        case .mine:         return "Mine"
        case .someone:      return "Someone else's"
        case .contributors: return "Contributors"
        case .members:      return "Org members"
        case .specific:     return "Specific issue"
        }
    }

    /// The wire spelling shared with the `build-prompt` CLI and the Python twin, so
    /// one vocabulary covers the config in memory and the payload between front-ends.
    public var wireName: String {
        switch self {
        case .all:          return "all"
        case .mine:         return "mine"
        case .someone:      return "someone"
        case .contributors: return "contributors"
        case .members:      return "members"
        case .specific:     return "specific"
        }
    }

    /// Whether this scope names one person, and so needs a @handle to be usable.
    public var needsHandle: Bool { self == .mine || self == .someone }
}
