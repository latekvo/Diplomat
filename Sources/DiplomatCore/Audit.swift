import Foundation

/// Everything the "Full E2E test" action collects, plus the logic that turns it
/// into the prompt handed to a fresh `claude` session. Pure value type — the prompt
/// text comes from `core/audit.json`; only the assembly order/conditions live here,
/// shared verbatim with the Linux front-end.
///
/// Unlike Review / Resolve-conflicts there is no whose-PRs axis: the audit always
/// targets the whole repository. Two independent toggles gate the optional scope:
///   - `fixIssues` — also reproduce + fix the repo's OPEN BUG issues (never feature
///     requests), in addition to auditing the existing code.
///   - `openPRs`   — open a focused PR for every confirmed finding / fix. When off
///     the run is a read-only audit that only reports its hard-reproduced findings.
public struct AuditConfig {
    /// Also reproduce + fix the repo's open BUG issues (feature requests excluded).
    public var fixIssues: Bool
    /// Open a PR for every confirmed finding / fix. Off ⇒ read-only audit.
    public var openPRs: Bool

    public init(fixIssues: Bool = false, openPRs: Bool = false) {
        self.fixIssues = fixIssues
        self.openPRs = openPRs
    }

    /// The configured target repo (owner, repo), from the shared core config.
    public var targetRepo: (owner: String, repo: String) {
        CoreAssets.repoCoordinates()
    }

    /// A whole-repo audit needs no user input, so it is always spawnable.
    public var isValid: Bool { true }

    public func buildPrompt() -> String {
        let blocks = (try? CoreAssets.audit())?.blocks ?? [:]
        let (owner, repo) = targetRepo
        func fill(_ s: String) -> String {
            s.replacingOccurrences(of: "{owner}", with: owner)
                .replacingOccurrences(of: "{repo}", with: repo)
        }

        var out: [String] = []
        if let intro = blocks["intro"] { out.append(fill(intro)) }
        if let bar = blocks["bar"] { out.append(fill(bar)) }
        // Always: classify every finding H/M/L (drives the report + the Low<20-LOC PR gate).
        if let classify = blocks["classify"] { out.append(fill(classify)) }
        // Optional: also reproduce + fix the repo's open BUG issues.
        if fixIssues, let b = blocks["issues"] { out.append(fill(b)) }
        // Delivery: open a PR per fix, or stay read-only and just report.
        if openPRs {
            if let b = blocks["openPRs"] { out.append(fill(b)) }
            if let b = blocks["noAttribution"] { out.append(fill(b)) }
            // PRs/comments this run opens carry the Diplomat attribution tag.
            if let b = blocks["diplomatTag"] { out.append(fill(b)) }
        } else if let b = blocks["readOnly"] {
            out.append(fill(b))
        }
        if let s = blocks["summary"] { out.append(fill(s)) }

        return out.joined(separator: "\n\n")
    }
}
