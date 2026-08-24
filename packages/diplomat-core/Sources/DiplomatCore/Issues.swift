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
/// Three axes, deliberately independent:
///   - `target`  — WHICH issues (all / mine / one user's / the community's / the
///     org's / one specific issue), the widest of the three because an issue's
///     author association is a scope of its own (see `IssueTarget`);
///   - `unassignedOnly` — narrowed to the ones nobody has claimed, so a sweep picks
///     up only what is actually going spare (moot for one specific issue);
///   - `assignToMe` / `openPRs` / `commentOnIssue` / `includeFeatures` — what the
///     run may DO about what it finds.
public struct IssueConfig: Codable, Equatable {
    public var depth: String          // depth id; "" -> default
    public var target: IssueTarget
    public var username: String       // the "someone else's" handle
    /// The authenticated viewer login (from the Store), used as the @handle for "mine".
    public var me: String
    public var specificIssue: String

    /// Skip every issue that already has an assignee — somebody is on it already.
    public var unassignedOnly: Bool
    /// Claim each issue (assign it to me) before starting on it, and hand it back
    /// if the run abandons it.
    public var assignToMe: Bool
    /// Deliver each fix as its own draft PR. Off ⇒ nothing reaches the remote.
    public var openPRs: Bool
    /// Report the outcome on the issue itself, one comment per issue worked.
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

    /// Fix exactly one issue by number/URL instead of sweeping a scope.
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

    /// The author associations that count as "inside the organisation", spelled the
    /// way the prompt names them ("MEMBER or OWNER"). Data-driven from the shared
    /// `filters.json`, so the contributors/members split the prompt describes is the
    /// same one the Unaddressed-Issues tool card filters by.
    private var orgList: String {
        let all = (try? CoreAssets.filters())?.orgAssociations ?? []
        switch all.count {
        case 0:  return "MEMBER or OWNER"
        case 1:  return all[0]
        default: return all.dropLast().joined(separator: ", ") + " or " + all[all.count - 1]
        }
    }

    private func scopeText(_ scope: [String: String]) -> String {
        let key: String
        switch target {
        case .all:          key = "scopeAll"
        case .mine:         key = "scopeMine"
        case .someone:      key = "scopeUser"
        case .contributors: key = "scopeContributors"
        case .members:      key = "scopeMembers"
        case .specific:     key = ""
        }
        return (scope[key] ?? "")
            .replacingOccurrences(of: "{handle}", with: authorHandle)
            .replacingOccurrences(of: "{orgList}", with: orgList)
    }

    /// How the prompt tells the agent to list the issues. The association scopes go
    /// through the REST endpoint because `gh issue list --json` cannot answer that
    /// field; every other scope takes the simpler `gh issue list`, narrowed by the
    /// `no:assignee` search qualifier when the unassigned filter is on.
    private func enumerateText(_ enumerate: [String: String]) -> String {
        if target.needsAuthorAssociation { return enumerate["association"] ?? "" }
        let search = unassignedOnly
            ? (enumerate["searchUnassigned"] ?? "")
            : (enumerate["searchNone"] ?? "")
        return (enumerate["plain"] ?? "").replacingOccurrences(of: "{searchFlag}", with: search)
    }

    public func buildPrompt() -> String {
        let issues = try? CoreAssets.issues()
        let scope = issues?.scope ?? [:]
        let enumerate = issues?.enumerate ?? [:]
        let blocks = issues?.blocks ?? [:]
        let (owner, repo) = CoreAssets.repoCoordinates()

        func fill(_ s: String) -> String {
            s.replacingOccurrences(of: "{issue}", with: issueRef.numberString)
                .replacingOccurrences(of: "{owner}", with: owner)
                .replacingOccurrences(of: "{repo}", with: repo)
        }

        var out: [String] = []

        // 1. What is in front of the agent: one issue, or a scope it has to enumerate.
        if isSingleIssue {
            out.append(fill(scope["single"] ?? ""))
        } else {
            out.append(fill((scope["multi"] ?? "")
                .replacingOccurrences(of: "{scope}", with: scopeText(scope))))
            out.append(fill(enumerateText(enumerate)))
            // 2. …narrowed to what nobody has claimed.
            if unassignedOnly, let b = blocks["unassigned"] { out.append(fill(b)) }
        }

        // 3. Which of those are this run's business at all — bugs, or features too.
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
