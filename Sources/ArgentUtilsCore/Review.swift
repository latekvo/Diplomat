import Foundation

/// One review-depth level, hydrated from the shared `core/review.json`.
public struct ReviewDepth: Identifiable {
    public let id: String
    public let title: String
    public let blurb: String
    public let fragment: String
    /// The "fix it on the branch" disposition for this depth, appended only when
    /// we may actually commit. `nil` for flag-only depths / never used review-only.
    public let onBranch: String?

    public init(id: String, title: String, blurb: String, fragment: String, onBranch: String? = nil) {
        self.id = id
        self.title = title
        self.blurb = blurb
        self.fragment = fragment
        self.onBranch = onBranch
    }
}

/// Read-only access to the review-prompt model in `core/review.json`.
public enum ReviewCatalog {
    public static func depths() -> [ReviewDepth] {
        guard let r = try? CoreAssets.review() else { return [] }
        return r.depths.map { ReviewDepth(id: $0.id, title: $0.title, blurb: $0.blurb, fragment: $0.fragment, onBranch: $0.onBranch) }
    }
    public static func defaultDepthID() -> String {
        (try? CoreAssets.review())?.defaultDepth ?? depths().first?.id ?? ""
    }
    public static func depth(id: String) -> ReviewDepth {
        let all = depths()
        if let match = all.first(where: { $0.id == id }) { return match }
        if let def = all.first(where: { $0.id == defaultDepthID() }) { return def }
        return all.first ?? ReviewDepth(id: "", title: "", blurb: "", fragment: "")
    }
}

/// Who authored a specific PR under review, when known. Selects the prompt (fix-on-
/// branch vs review-only vs author-gated) and which action toggles even apply.
public enum SpecificAuthor: Equatable {
    case unknown   // specific PR, author not polled yet / poll failed — offer everything
    case mine      // fix on the branch (CASE A)
    case theirs    // review only (CASE B)
}

/// Everything the Review-PRs wizard collects, plus the logic that turns it into
/// the prompt handed to a fresh `claude` session. Pure value type — the prompt
/// text comes from `core/review.json`; only the assembly order/conditions live
/// here, shared verbatim with the Linux front-end.
public struct ReviewConfig {
    /// Whose PRs we review — the same axis the Resolve-conflicts wizard uses.
    public typealias Target = PRTarget

    public var depth: String          // depth id; "" -> default
    public var target: Target
    public var username: String
    /// The authenticated viewer login (from the Store), used as the @handle for "mine".
    public var me: String

    public var markReady: Bool        // mark perfectly-clean PRs ready for review
    public var leaveReviews: Bool     // formal review — for OTHERS' PRs (and a specific PR)
    public var replyToReviews: Bool   // reply to others' threads — on MY PRs (and a specific PR)

    public var includeDrafts: Bool
    public var includeReady: Bool
    public var specificPR: String

    /// The "final pass" escalation: a culminating full-E2E verdict pass. Off by default.
    public var finalPass: Bool

    /// For a specific PR: whether it's mine, someone else's, or not yet determined. The
    /// wizard polls the PR's author and sets this; the monitors set it directly (they
    /// always know). Ignored unless single-PR.
    public var specificAuthor: SpecificAuthor

    public init(depth: String = "", target: Target = .mine, username: String = "",
                me: String = "", markReady: Bool = true, leaveReviews: Bool = true,
                replyToReviews: Bool = true, includeDrafts: Bool = true,
                includeReady: Bool = true, specificPR: String = "", finalPass: Bool = false,
                specificAuthor: SpecificAuthor = .unknown) {
        self.depth = depth.isEmpty ? ReviewCatalog.defaultDepthID() : depth
        self.target = target
        self.username = username
        self.me = me
        self.markReady = markReady
        self.leaveReviews = leaveReviews
        self.replyToReviews = replyToReviews
        self.includeDrafts = includeDrafts
        self.includeReady = includeReady
        self.specificPR = specificPR
        self.finalPass = finalPass
        self.specificAuthor = specificAuthor
    }

    /// The @handle whose PRs we go through (empty in single-PR mode).
    public var authorHandle: String {
        switch target {
        case .mine:
            return me.isEmpty ? "me" : me
        case .someone:
            let u = username.trimmingCharacters(in: .whitespaces)
            return u.isEmpty ? "" : u
        case .specific:
            return ""
        }
    }

    /// The review disposition: mine (fix on branch) or theirs (review only). For a
    /// whose-PRs sweep it follows the target; for a specific PR it's the polled author.
    /// `.unknown` (specific, author still pending) offers every toggle, gated prompt.
    public var disposition: SpecificAuthor {
        switch target {
        case .mine: return .mine
        case .someone: return .theirs
        case .specific: return specificAuthor
        }
    }
    // Which action toggles apply. Mine-only toggles (mark-ready, reply-to-threads) hide
    // for theirs; theirs-only toggles (formal review, final verdict) hide for mine.
    // `.unknown` (author pending) leaves all four visible.
    public var canMarkReady: Bool { disposition != .theirs }
    public var canLeaveReviews: Bool { disposition != .mine }
    public var canReplyToReviews: Bool { disposition != .theirs }
    public var canFinalPass: Bool { disposition != .mine }
    public var effMarkReady: Bool { markReady && canMarkReady }
    public var effLeaveReviews: Bool { leaveReviews && canLeaveReviews }
    public var effReplyToReviews: Bool { replyToReviews && canReplyToReviews }
    public var effFinalPass: Bool { finalPass && canFinalPass }

    /// Review exactly one PR by number/URL instead of a whose-PRs sweep.
    public var isSinglePR: Bool { target == .specific }

    /// Reviewing someone else's PRs: a hard look-don't-touch mode. We never
    /// commit or push to their branch, so the no-commit guard goes in and the
    /// commit-authoring guidance stays out. (A specific PR may be mine, so it is
    /// deliberately NOT review-only.)
    public var isReviewOnly: Bool { target == .someone }

    /// The configured target repo (owner, repo), from the shared core config.
    public var targetRepo: (owner: String, repo: String) {
        let cfg = try? CoreAssets.config()
        return (cfg?.owner ?? "software-mansion", cfg?.repo ?? "argent")
    }

    /// The single-PR field parsed as a number / URL / `owner/repo#n` shorthand,
    /// checked against the target repo.
    public var prRef: PRRef {
        let (owner, repo) = targetRepo
        return PRRef.parse(specificPR, owner: owner, repo: repo)
    }

    public var isValid: Bool {
        if isSinglePR { return prRef.isValid }
        // A whose-PRs sweep needs a handle and at least one PR-state box ticked.
        return !authorHandle.isEmpty && (includeDrafts || includeReady)
    }

    private func prKind(_ scope: [String: String]) -> String {
        switch (includeDrafts, includeReady) {
        case (true, true):  return scope["prKindBoth"] ?? ""
        case (true, false): return scope["prKindDrafts"] ?? ""
        default:            return scope["prKindReady"] ?? ""
        }
    }

    public func buildPrompt() -> String {
        let review = try? CoreAssets.review()
        let scope = review?.scope ?? [:]
        let blocks = review?.blocks ?? [:]
        let cfg = try? CoreAssets.config()
        let owner = cfg?.owner ?? "software-mansion"
        let repo = cfg?.repo ?? "argent"

        // A specific PR may be mine OR someone else's — which decides whether the agent
        // may touch the branch. When we know the author (the wizard polled it, or a
        // monitor set it) emit the mine-only / review-only prompt directly; otherwise
        // (author still pending) fall back to the author-gated CASE A/B prompt.
        if isSinglePR {
            switch specificAuthor {
            case .mine:
                return buildKnownMinePrompt(review: review, scope: scope, blocks: blocks, owner: owner, repo: repo)
            case .theirs:
                return buildKnownTheirsPrompt(review: review, scope: scope, blocks: blocks, owner: owner, repo: repo)
            case .unknown:
                return buildSpecificPrompt(review: review, scope: scope, blocks: blocks, owner: owner, repo: repo)
            }
        }

        var out: [String] = []

        let tmpl = target == .mine ? (scope["scopeMine"] ?? "") : (scope["scopeOther"] ?? "")
        let scopeText = tmpl
            .replacingOccurrences(of: "{prKind}", with: prKind(scope))
            .replacingOccurrences(of: "{handle}", with: authorHandle)
        out.append((scope["multi"] ?? "")
            .replacingOccurrences(of: "{scope}", with: scopeText)
            .replacingOccurrences(of: "{owner}", with: owner)
            .replacingOccurrences(of: "{repo}", with: repo))

        // For someone else's PRs, frame the whole task as look-don't-touch up front.
        if isReviewOnly, let b = blocks["reviewOnly"] { out.append(b) }
        let chosen = ReviewCatalog.depth(id: depth)
        out.append(chosen.fragment)
        // The depth's on-branch fix step only when we may actually commit — never
        // for someone else's branch, which we don't touch.
        if !isReviewOnly, let ob = chosen.onBranch, !ob.isEmpty { out.append(ob) }
        if let bar = blocks["bar"] { out.append(bar) }
        if effMarkReady, let b = blocks["markReady"] { out.append(b) }
        if effLeaveReviews, let b = blocks["leaveReviews"] { out.append(b) }
        if effReplyToReviews, let b = blocks["reply"] { out.append(b) }
        if let trailer = blocks["trailer"] { out.append(trailer) }
        // Commit-authoring guidance only when we might actually commit — never
        // for someone else's branch, which we don't touch.
        if !isReviewOnly, let b = blocks["noAttribution"] { out.append(b) }
        if effFinalPass, let b = blocks["finalPass"] { out.append(b) }

        return out.joined(separator: "\n\n")
    }

    /// The single-PR prompt for a PR we ALREADY know is mine: no author poll, no
    /// CASE A/B — just the mine-only disposition (review approach + bar + fix-on-branch,
    /// with reply/mark-ready gated by their toggles). Used by the auto-fix monitor.
    private func buildKnownMinePrompt(review: CoreAssets.Review?, scope: [String: String],
                                      blocks: [String: String], owner: String, repo: String) -> String {
        let specific = review?.specific ?? [:]
        let chosen = ReviewCatalog.depth(id: depth)
        let pr = prRef.numberString
        let handle = me.isEmpty ? "me" : me
        func fill(_ s: String) -> String {
            s.replacingOccurrences(of: "{pr}", with: pr)
                .replacingOccurrences(of: "{owner}", with: owner)
                .replacingOccurrences(of: "{repo}", with: repo)
                .replacingOccurrences(of: "{me}", with: handle)
        }
        var out: [String] = []
        out.append(fill(scope["single"] ?? ""))
        out.append(fill(specific["mineOnly"] ?? ""))
        // First and foremost: screen + verify + fix/dismiss + respond to every reviewer
        // finding already on the PR, BEFORE the agent's own review.
        out.append(fill(specific["reviewerFindingsFirst"] ?? ""))
        out.append(chosen.fragment)
        if let bar = blocks["bar"] { out.append(bar) }
        if let ob = chosen.onBranch, !ob.isEmpty { out.append(ob) }
        if effMarkReady, let b = blocks["markReady"] { out.append(b) }
        if let b = blocks["noAttribution"] { out.append(b) }
        if let trailer = blocks["trailer"] { out.append(trailer) }
        // No final verdict for my own PR — I don't approve my own work. The reviewer-
        // findings block above already covers replying to threads (so the separate reply
        // block is redundant here).
        return out.filter { !$0.isEmpty }.joined(separator: "\n\n")
    }

    /// The single-PR prompt for a PR we ALREADY know is someone else's (a review
    /// requested from me): no author poll, no CASE A/B — the review-only, hands-off
    /// disposition directly (leave a formal review if the toggle is on, never touch the
    /// branch). Used by the review-request monitor.
    private func buildKnownTheirsPrompt(review: CoreAssets.Review?, scope: [String: String],
                                        blocks: [String: String], owner: String, repo: String) -> String {
        let specific = review?.specific ?? [:]
        let chosen = ReviewCatalog.depth(id: depth)
        let pr = prRef.numberString
        let handle = me.isEmpty ? "me" : me
        func fill(_ s: String) -> String {
            s.replacingOccurrences(of: "{pr}", with: pr)
                .replacingOccurrences(of: "{owner}", with: owner)
                .replacingOccurrences(of: "{repo}", with: repo)
                .replacingOccurrences(of: "{me}", with: handle)
        }
        var out: [String] = []
        out.append(fill(scope["single"] ?? ""))
        out.append(fill(specific["theirsOnly"] ?? ""))
        out.append(chosen.fragment)
        if let bar = blocks["bar"] { out.append(bar) }
        if let b = blocks["reviewOnly"] { out.append(b) }
        if effLeaveReviews, let b = blocks["leaveReviews"] { out.append(b) }
        out.append(fill(specific["otherNoMarkReady"] ?? ""))
        // Deliver the approve/changes-requested verdict only when the Final-E2E toggle
        // is on (manual review). Automatic runs leave it off → no verdict, the final
        // call stays with me.
        if effFinalPass, let b = blocks["finalPass"] {
            out.append(b)
        } else if let b = blocks["noVerdict"] {
            out.append(fill(b))
        }
        if let trailer = blocks["trailer"] { out.append(trailer) }
        return out.filter { !$0.isEmpty }.joined(separator: "\n\n")
    }

    /// The single-PR (Specific PR) prompt. Because the PR may be mine or someone
    /// else's — and that decides whether the branch may be touched — this prompt
    /// tells the agent to poll the author first, then split into two mutually
    /// exclusive cases: CASE A (mine → fix on the branch, mark clean ready, reply,
    /// no AI attribution) and CASE B (theirs → review only, never touch the branch,
    /// leave a formal review, and explicitly DO NOT mark it ready). The action
    /// sub-blocks are gated by the same toggles the wizard exposes.
    private func buildSpecificPrompt(review: CoreAssets.Review?, scope: [String: String],
                                     blocks: [String: String], owner: String, repo: String) -> String {
        let specific = review?.specific ?? [:]
        let chosen = ReviewCatalog.depth(id: depth)
        let pr = prRef.numberString
        let handle = me.isEmpty ? "me" : me
        func fill(_ s: String) -> String {
            s.replacingOccurrences(of: "{pr}", with: pr)
                .replacingOccurrences(of: "{owner}", with: owner)
                .replacingOccurrences(of: "{repo}", with: repo)
                .replacingOccurrences(of: "{me}", with: handle)
        }

        var out: [String] = []
        out.append(fill(scope["single"] ?? ""))
        out.append(fill(specific["determineAuthor"] ?? ""))
        // The review work itself is the same regardless of author; only the
        // disposition differs, so the approach + bar come before the split.
        out.append(chosen.fragment)
        if let bar = blocks["bar"] { out.append(bar) }

        // CASE A — it's mine: fix it on the branch.
        out.append(fill(specific["mineHeader"] ?? ""))
        if let ob = chosen.onBranch, !ob.isEmpty { out.append(ob) }
        if effMarkReady, let b = blocks["markReady"] { out.append(b) }
        if effReplyToReviews, let b = blocks["reply"] { out.append(b) }
        if let b = blocks["noAttribution"] { out.append(b) }

        // CASE B — it's someone else's: review only, hands off the branch.
        out.append(fill(specific["otherHeader"] ?? ""))
        if let b = blocks["reviewOnly"] { out.append(b) }
        if effLeaveReviews, let b = blocks["leaveReviews"] { out.append(b) }
        out.append(fill(specific["otherNoMarkReady"] ?? ""))

        if let trailer = blocks["trailer"] { out.append(trailer) }
        if effFinalPass, let b = blocks["finalPass"] { out.append(b) }

        return out.filter { !$0.isEmpty }.joined(separator: "\n\n")
    }
}

/// Decides whether an auto-dispatched review of a review-requested PR may carry the
/// "final pass + verdict" escalation, or must stay comments-only. Pure & data-light so
/// it's unit-testable and shared verbatim with any front-end. Each flag independently
/// withholds the verdict for one class of PR; every flag defaults ON, matching the
/// intended policy: SKILL / installer / community PRs get comments only, all else a verdict.
public struct VerdictPolicy: Equatable {
    /// Withhold the verdict when the PR changes a SKILL file.
    public var withholdOnSkill: Bool
    /// Withhold the verdict when the PR changes the installer/CLI.
    public var withholdOnInstaller: Bool
    /// Withhold the verdict when the PR's author is outside the org (a community PR).
    public var withholdOnCommunity: Bool

    public init(withholdOnSkill: Bool = true, withholdOnInstaller: Bool = true,
                withholdOnCommunity: Bool = true) {
        self.withholdOnSkill = withholdOnSkill
        self.withholdOnInstaller = withholdOnInstaller
        self.withholdOnCommunity = withholdOnCommunity
    }

    /// Author associations trusted enough for an auto-verdict; anything else is "community".
    /// Matches the long-standing gate (org members, maintainers, established contributors).
    public static let trustedAssociations: Set<String> =
        ["OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"]

    public static func isCommunity(_ authorAssociation: String) -> Bool {
        !trustedAssociations.contains(authorAssociation.uppercased())
    }

    /// Human-readable reasons the verdict is withheld for this PR under this policy.
    /// Empty ⇒ the verdict is allowed.
    public func withholdReasons(files: [String], authorAssociation: String) -> [String] {
        var reasons: [String] = []
        if withholdOnSkill, files.contains(where: Filters.isSkillFile) {
            reasons.append("touches a SKILL")
        }
        if withholdOnInstaller, files.contains(where: Filters.isInstallerFile) {
            reasons.append("touches the installer")
        }
        if withholdOnCommunity, VerdictPolicy.isCommunity(authorAssociation) {
            reasons.append("community PR")
        }
        return reasons
    }

    /// The final-pass verdict is allowed only when no enabled suppressor matches.
    public func allowsVerdict(files: [String], authorAssociation: String) -> Bool {
        withholdReasons(files: files, authorAssociation: authorAssociation).isEmpty
    }
}
