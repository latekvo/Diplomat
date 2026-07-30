import Foundation

/// Whose PRs a wizard acts on: my own, another user's, or one specific PR by
/// number/URL. Shared by the Review and Resolve-conflicts wizards (and mirrored in
/// the Linux front-end's `prtarget.py`).
public enum PRTarget: Int, CaseIterable, Identifiable {
    case mine, someone, specific
    public var id: Int { rawValue }
    public var title: String {
        switch self {
        case .mine:     return "Mine"
        case .someone:  return "Someone else's"
        case .specific: return "Specific PR"
        }
    }
}
