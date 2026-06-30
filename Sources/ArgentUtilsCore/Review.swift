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

    public init(depth: String = "", target: Target = .mine, username: String = "",
                me: String = "", markReady: Bool = true, leaveReviews: Bool = true,
                replyToReviews: Bool = true, includeDrafts: Bool = true,
                includeReady: Bool = true, specificPR: String = "", finalPass: Bool = false) {
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

    // A specific PR may be mine or someone's, so all three actions are offered.
    public var canMarkReady: Bool { target != .someone }
    public var canLeaveReviews: Bool { target != .mine }
    public var canReplyToReviews: Bool { target != .someone }
    public var effMarkReady: Bool { markReady && canMarkReady }
    public var effLeaveReviews: Bool { leaveReviews && canLeaveReviews }
    public var effReplyToReviews: Bool { replyToReviews && canReplyToReviews }

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

        // A specific PR may be mine OR someone else's — which decides whether the
        // agent is allowed to touch the branch. We can't know the author up front,
        // so we hand the agent an author-gated prompt instead of guessing.
        if isSinglePR {
            return buildSpecificPrompt(review: review, scope: scope, blocks: blocks,
                                       owner: owner, repo: repo)
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
        if finalPass, let b = blocks["finalPass"] { out.append(b) }

        return out.joined(separator: "\n\n")
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
        if markReady, let b = blocks["markReady"] { out.append(b) }
        if replyToReviews, let b = blocks["reply"] { out.append(b) }
        if let b = blocks["noAttribution"] { out.append(b) }

        // CASE B — it's someone else's: review only, hands off the branch.
        out.append(fill(specific["otherHeader"] ?? ""))
        if let b = blocks["reviewOnly"] { out.append(b) }
        if leaveReviews, let b = blocks["leaveReviews"] { out.append(b) }
        out.append(fill(specific["otherNoMarkReady"] ?? ""))

        if let trailer = blocks["trailer"] { out.append(trailer) }
        if finalPass, let b = blocks["finalPass"] { out.append(b) }

        return out.filter { !$0.isEmpty }.joined(separator: "\n\n")
    }
}
