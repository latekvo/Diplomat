import Foundation

/// Read-only access to the Fix-issues prompt model in `assets/issues.json`.
public enum IssueCatalog {
    public static func depths() -> [PromptDepth] {
        guard let i = try? CoreAssets.issues() else { return [] }
        return i.depths.map {
            PromptDepth(id: $0.id, title: $0.title, blurb: $0.blurb, fragment: $0.fragment)
        }
    }
    public static func defaultDepthID() -> String {
        (try? CoreAssets.issues())?.defaultDepth ?? depths().first?.id ?? ""
    }
    public static func depth(id: String) -> PromptDepth {
        PromptDepth.resolve(depths(), id: id, defaultID: defaultDepthID())
    }
}

/// Everything the "Fix issues" wizard collects, plus the logic that turns it into
/// the prompt handed to a fresh `claude` session. Pure value type — the prompt text
/// comes from `assets/issues.json`; only the assembly order/conditions live here,
/// shared verbatim with the Linux front-end. The one thing `buildPrompt` looks up
/// off the machine is which model the agent runs on (`AgentModel`), which the
/// attribution tag names.
///
/// **Every prompt this builds works ONE issue** — `specificIssue` names it. A scope
/// is never handed to an agent as a scope: the front-end enumerates it against the
/// repo's open issues (`Filters.sweptIssues`) and queues one run per issue, each
/// built from `forIssue`. So the three axes below are read by different readers:
///
///   - `target` / `username` / `unassignedOnly` — WHICH issues the sweep picks up
///     (all / mine / one user's / the community's / the org's / one specific issue,
///     narrowed to the ones nobody has claimed). The front-end reads these; the
///     prompt reads them only to know whether this issue was swept, and so whether
///     it has to re-check the state the sweep selected on;
///   - `depth` — how hard the one issue is proven;
///   - `assignToMe` / `openPRs` / `commentOnIssue` / `includeFeatures` — what the run
///     may DO about what it finds.
public struct IssueConfig: Codable, Equatable {
    public var depth: String          // depth id; "" -> default
    public var target: IssueTarget
    public var username: String       // the "someone else's" handle
    /// The authenticated viewer login (from the Store), used as the @handle for "mine".
    public var me: String
    /// The one issue this run works. Typed by hand under the `.specific` scope, and
    /// filled in per issue by `forIssue` for every other one.
    public var specificIssue: String

    /// Skip every issue that already has an assignee — somebody is on it already.
    public var unassignedOnly: Bool
    /// Claim the issue (assign it to me) before starting on it, and hand it back
    /// if the run abandons it.
    public var assignToMe: Bool
    /// Deliver the fix as its own draft PR. Off ⇒ nothing reaches the remote.
    public var openPRs: Bool
    /// Report the outcome on the issue itself.
    public var commentOnIssue: Bool
    /// The one escalation: also take on feature requests, not just bug reports.
    public var includeFeatures: Bool

    public init(depth: String = "", target: IssueTarget = .all, username: String = "",
                me: String = "", specificIssue: String = "", unassignedOnly: Bool = true,
                assignToMe: Bool = true, openPRs: Bool = true,
                commentOnIssue: Bool = true, includeFeatures: Bool = false) {
        self.depth = depth.isEmpty ? IssueCatalog.defaultDepthID() : depth
        self.target = target
        self.username = username
        self.me = me
        self.specificIssue = specificIssue
        self.unassignedOnly = unassignedOnly
        self.assignToMe = assignToMe
        self.openPRs = openPRs
        self.commentOnIssue = commentOnIssue
        self.includeFeatures = includeFeatures
    }

    /// The @handle whose issues we sweep — empty for every scope that names no one
    /// person (all / contributors / members / one specific issue).
    public var authorHandle: String {
        switch target {
        case .mine:
            return me.isEmpty ? "me" : me
        case .someone:
            let u = username.trimmingCharacters(in: .whitespaces)
            return u.isEmpty ? "" : u
        default:
            return ""
        }
    }

    /// Fix exactly one issue named by hand, instead of sweeping a scope.
    public var isSingleIssue: Bool { target == .specific }

    /// Whether the wizard offers the unassigned filter at all. It only means
    /// something for a sweep: a specific issue was named by hand, so filtering it
    /// back out would just be a run that does nothing.
    public var canFilterUnassigned: Bool { !isSingleIssue }

    /// The configured target repo (owner, repo), from the shared core config.
    public var targetRepo: (owner: String, repo: String) {
        CoreAssets.repoCoordinates()
    }

    /// The single-issue field parsed as a number / URL / `owner/repo#n` shorthand,
    /// checked against the target repo.
    public var issueRef: PRRef {
        let (owner, repo) = targetRepo
        return PRRef.parse(specificIssue, owner: owner, repo: repo, kind: .issues)
    }

    public var isValid: Bool {
        if isSingleIssue { return issueRef.isValid }
        // A scope that names a person needs that person; the rest need nothing.
        return target.needsHandle ? !authorHandle.isEmpty : true
    }

    /// The login whose open issues this sweep expands into one queued fix each, or ""
    /// when the scope names nobody in particular (all / contributors / members) or
    /// there is nothing to expand (one named issue, or my own issues before the
    /// viewer login has resolved).
    ///
    /// Not `authorHandle`, which falls back to the literal "me" for the prompt to
    /// address: matched against real issue authors, where the account called "me"
    /// has opened nothing.
    public var sweepAuthor: String {
        switch target {
        case .mine: return me.trimmingCharacters(in: .whitespaces)
        case .someone: return username.trimmingCharacters(in: .whitespaces)
        default: return ""
        }
    }

    /// This sweep, narrowed to one of the issues it covers — the config behind one
    /// queued fix.
    ///
    /// Same depth and same action toggles, because they are what the operator chose;
    /// only the issue is added. The scope is deliberately KEPT rather than collapsed
    /// to `.specific`: it is what still says this issue was swept rather than named,
    /// and so that the run re-checks the state the sweep selected on before working
    /// an issue whose turn came hours later.
    public func forIssue(_ number: Int) -> IssueConfig {
        var out = self
        out.specificIssue = String(number)
        return out
    }

    public func buildPrompt() -> String {
        let issues = try? CoreAssets.issues()
        let scope = issues?.scope ?? [:]
        let blocks = issues?.blocks ?? [:]
        let (owner, repo) = CoreAssets.repoCoordinates()

        func fill(_ s: String) -> String {
            s.replacingOccurrences(of: "{issue}", with: issueRef.numberString)
                .replacingOccurrences(of: "{owner}", with: owner)
                .replacingOccurrences(of: "{repo}", with: repo)
        }

        var out: [String] = []

        // 1. The one issue in front of the agent.
        out.append(fill(scope["single"] ?? ""))

        // 2. What a swept issue has to be re-checked for, its turn having come long
        //    after the sweep chose it: closed since, and claimed since.
        if !isSingleIssue {
            if let b = blocks["swept"] { out.append(fill(b)) }
            if unassignedOnly, let b = blocks["unassigned"] { out.append(fill(b)) }
        }

        // 3. Whether it is this run's business at all — a bug, or a feature too.
        if includeFeatures {
            if let b = blocks["includeFeatures"] { out.append(fill(b)) }
        } else if let b = blocks["bugsOnly"] {
            out.append(fill(b))
        }

        // 4. Claim it before working it, so a second agent doesn't take the same one.
        if assignToMe, let b = blocks["assignSelf"] { out.append(fill(b)) }

        // 5. How hard to prove it, and the bar that proof is held to.
        out.append(IssueCatalog.depth(id: depth).fragment)
        if let b = blocks["bar"] { out.append(fill(b)) }
        if let b = blocks["cannotReproduce"] { out.append(fill(b)) }

        // 6. Where the fix goes, and who hears about it.
        if openPRs {
            if let b = blocks["openPRs"] { out.append(fill(b)) }
        } else if let b = blocks["noPRs"] {
            out.append(fill(b))
        }
        if commentOnIssue, let b = blocks["comment"] { out.append(fill(b)) }
        // Commit-authoring guidance only when we might actually commit.
        if openPRs, let b = blocks["noAttribution"] { out.append(fill(b)) }
        // The attribution tag only when this run posts something to wear it.
        if openPRs || commentOnIssue, let b = blocks["diplomatTag"] {
            out.append(AgentModel.fillTag(fill(b), model: AgentModel.detected()))
        }
        if let b = blocks["summary"] { out.append(fill(b)) }

        return out.filter { !$0.isEmpty }.joined(separator: "\n\n")
    }
}
