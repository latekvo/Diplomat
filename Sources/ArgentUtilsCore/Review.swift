import Foundation

/// One review-depth level, hydrated from the shared `core/review.json`.
public struct ReviewDepth: Identifiable {
    public let id: String
    public let title: String
    public let blurb: String
    public let fragment: String

    public init(id: String, title: String, blurb: String, fragment: String) {
        self.id = id
        self.title = title
        self.blurb = blurb
        self.fragment = fragment
    }
}

/// Read-only access to the review-prompt model in `core/review.json`.
public enum ReviewCatalog {
    public static func depths() -> [ReviewDepth] {
        guard let r = try? CoreAssets.review() else { return [] }
        return r.depths.map { ReviewDepth(id: $0.id, title: $0.title, blurb: $0.blurb, fragment: $0.fragment) }
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
    public var depth: String          // depth id; "" -> default
    public var targetIsMine: Bool
    public var username: String
    /// The authenticated viewer login (from the Store), used as the @handle for "mine".
    public var me: String

    public var markReady: Bool        // mark perfectly-clean PRs ready for review
    public var leaveReviews: Bool     // effective only when reviewing OTHER people's PRs
    public var replyToReviews: Bool   // effective only when reviewing MY PRs

    public var includeDrafts: Bool
    public var includeReady: Bool
    public var specificPR: String

    /// The "final pass" escalation: a culminating full-E2E verdict pass. Off by default.
    public var finalPass: Bool

    public init(depth: String = "", targetIsMine: Bool = true, username: String = "",
                me: String = "", markReady: Bool = true, leaveReviews: Bool = true,
                replyToReviews: Bool = true, includeDrafts: Bool = true,
                includeReady: Bool = true, specificPR: String = "", finalPass: Bool = false) {
        self.depth = depth.isEmpty ? ReviewCatalog.defaultDepthID() : depth
        self.targetIsMine = targetIsMine
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

    /// The @handle whose PRs we go through.
    public var authorHandle: String {
        if targetIsMine { return me.isEmpty ? "me" : me }
        let u = username.trimmingCharacters(in: .whitespaces)
        return u.isEmpty ? "" : u
    }

    public var canMarkReady: Bool { targetIsMine }
    public var canLeaveReviews: Bool { !targetIsMine }
    public var canReplyToReviews: Bool { targetIsMine }
    public var effMarkReady: Bool { markReady && canMarkReady }
    public var effLeaveReviews: Bool { leaveReviews && canLeaveReviews }
    public var effReplyToReviews: Bool { replyToReviews && canReplyToReviews }

    /// With neither PR-state box ticked, we review one PR by number instead.
    public var isSinglePR: Bool { !includeDrafts && !includeReady }
    public var trimmedPR: String { specificPR.trimmingCharacters(in: .whitespaces) }

    public var isValid: Bool {
        if isSinglePR { return Int(trimmedPR) != nil }
        return !authorHandle.isEmpty
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

        var out: [String] = []

        if isSinglePR {
            out.append((scope["single"] ?? "")
                .replacingOccurrences(of: "{pr}", with: trimmedPR)
                .replacingOccurrences(of: "{owner}", with: owner)
                .replacingOccurrences(of: "{repo}", with: repo))
        } else {
            let tmpl = targetIsMine ? (scope["scopeMine"] ?? "") : (scope["scopeOther"] ?? "")
            let scopeText = tmpl
                .replacingOccurrences(of: "{prKind}", with: prKind(scope))
                .replacingOccurrences(of: "{handle}", with: authorHandle)
            out.append((scope["multi"] ?? "")
                .replacingOccurrences(of: "{scope}", with: scopeText)
                .replacingOccurrences(of: "{owner}", with: owner)
                .replacingOccurrences(of: "{repo}", with: repo))
        }

        out.append(ReviewCatalog.depth(id: depth).fragment)
        if let bar = blocks["bar"] { out.append(bar) }
        if effMarkReady, let b = blocks["markReady"] { out.append(b) }
        if effLeaveReviews, let b = blocks["leaveReviews"] { out.append(b) }
        if effReplyToReviews, let b = blocks["reply"] { out.append(b) }
        if let trailer = blocks["trailer"] { out.append(trailer) }
        if finalPass, let b = blocks["finalPass"] { out.append(b) }

        return out.joined(separator: "\n\n")
    }
}
