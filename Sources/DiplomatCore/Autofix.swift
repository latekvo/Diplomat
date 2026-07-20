import Foundation

// The PR auto-fix monitor's pure core: given the previous per-PR fingerprints and a
// fresh snapshot of my open PRs, decide which PRs just transitioned into a state that
// warrants dispatching an agent — a NEW merge conflict, or NEW review work (more
// unresolved threads, or a fresh CHANGES_REQUESTED verdict). Kept here (not in the
// macOS UI layer) so it's cross-platform and unit-testable; the front-end supplies the
// GitHub snapshot and performs the spawn.
//
// Deliberately edge-triggered: an event fires only on the transition, and the caller
// persists the returned fingerprints, so a persistent condition never re-dispatches.
// Review detection keys on unresolved-thread COUNT and the verdict — never on "a new
// review object appeared" — so the agent's own "Fixed in <hash>" replies (which are
// review comments authored as me) can't retrigger it.

public struct PRSnapshot: Equatable {
    public let number: Int
    public let title: String
    public let url: String
    public let isDraft: Bool
    public let mergeable: String        // "MERGEABLE" / "CONFLICTING" / "UNKNOWN"
    public let reviewDecision: String   // "" / "CHANGES_REQUESTED" / "APPROVED" / …
    public let threadsUnresolved: Int
    /// Unresolved threads I still OWE a reply on (resolvable, not resolved, last comment
    /// isn't mine) — the "My Unaddressed Reviews" signal. Drives the offline-review
    /// reconcile so we don't dispatch a fix agent for a PR where the ball is with the
    /// reviewer. `threadsUnresolved` (raw count) still drives the edge-trigger.
    public let threadsIOwe: Int
    /// Head commit sha (`headRefOid`) — the "which push" part of the mesh work key,
    /// so two nodes observing the same commit derive the same key (docs/szpontnet/12).
    public let headSha: String

    public init(number: Int, title: String, url: String, isDraft: Bool,
                mergeable: String, reviewDecision: String,
                threadsUnresolved: Int, threadsIOwe: Int = 0, headSha: String = "") {
        self.number = number
        self.title = title
        self.url = url
        self.isDraft = isDraft
        self.mergeable = mergeable
        self.reviewDecision = reviewDecision
        self.threadsUnresolved = threadsUnresolved
        self.threadsIOwe = threadsIOwe
        self.headSha = headSha
    }
}

public struct PRFingerprint: Codable, Equatable {
    public var mergeable: String
    public var reviewDecision: String
    public var threadsUnresolved: Int

    public init(mergeable: String, reviewDecision: String, threadsUnresolved: Int) {
        self.mergeable = mergeable
        self.reviewDecision = reviewDecision
        self.threadsUnresolved = threadsUnresolved
    }
}

public enum AutofixEvent: Equatable {
    case conflict(PRSnapshot)
    case review(PRSnapshot)
}

public enum AutofixDiff {
    /// Compare the prior fingerprints (keyed by PR number) against a fresh snapshot.
    /// Returns the events to act on plus the fingerprints to persist for next time.
    /// A PR with no prior entry is seeded silently (baseline — never dispatched on
    /// first sighting), so newly-opened PRs and the very first run don't fire.
    public static func compute(prior: [Int: PRFingerprint], now: [PRSnapshot])
        -> (events: [AutofixEvent], fingerprints: [Int: PRFingerprint]) {
        var events: [AutofixEvent] = []
        var fingerprints: [Int: PRFingerprint] = [:]
        for s in now {
            let p = prior[s.number]
            // GitHub returns UNKNOWN transiently while it recomputes mergeability;
            // carry the prior value forward so we neither lose nor fake a conflict.
            let mergeable = (s.mergeable == "UNKNOWN" || s.mergeable.isEmpty)
                ? (p?.mergeable ?? s.mergeable)
                : s.mergeable
            if let p = p {
                if p.mergeable != "CONFLICTING" && mergeable == "CONFLICTING" {
                    events.append(.conflict(s))
                }
                let moreThreads = s.threadsUnresolved > p.threadsUnresolved
                let nowChanges = p.reviewDecision != "CHANGES_REQUESTED"
                    && s.reviewDecision == "CHANGES_REQUESTED"
                if moreThreads || nowChanges {
                    events.append(.review(s))
                }
            }
            fingerprints[s.number] = PRFingerprint(
                mergeable: mergeable,
                reviewDecision: s.reviewDecision,
                threadsUnresolved: s.threadsUnresolved)
        }
        return (events, fingerprints)
    }
}

// MARK: - Mesh coordination for the auto-monitors (mirrors autofix.py's twin)
//
// Two machines running this monitor poll the same GitHub state as the same user, so
// each is an independent origin of the same work (docs/szpontnet/12-work-claims.md).
// The Store gates every auto dispatch with:
//   1. `standDown` — the duty is assigned to OTHER live nodes: their monitor
//      originates there, ours stands down (assignment already tracks liveness);
//   2. the ctl `claim` verb on `workKey` — origination dedup for the remaining
//      races (no assignee, takeover flaps, spread placements).
public enum AutofixMesh {
    public static let kindReviewReq = "review"        // reviews requested of me → duty "review"
    public static let kindReviewReply = "review-reply" // replies to reviews on MY PRs → duty "review"
    public static let kindConflicts = "conflicts"     // conflict fixes on MY PRs → duty "conflicts"

    /// The origination-dedup key for one unit of monitor work — the reference
    /// convention from docs/szpontnet/12: `<kind>:<host>/<owner>/<repo>#<n>@<sha>`.
    /// Derived from the PR's own URL so every node observing the same PR agrees
    /// byte-for-byte (the Python twin must produce identical strings — see the
    /// parity tests). Returns "" — claim gate skipped, the safe pre-claims
    /// degradation — when the URL doesn't look like a PR URL or the sha is unknown.
    public static func workKey(kind: String, prURL: String, headSha: String) -> String {
        guard !headSha.isEmpty,
              let u = URL(string: prURL),
              let host = u.host?.lowercased(), !host.isEmpty else { return "" }
        let parts = u.pathComponents.filter { $0 != "/" }
        guard parts.count == 4, parts[2] == "pull",
              parts[3].allSatisfy(\.isNumber), !parts[3].isEmpty else { return "" }
        return "\(kind):\(host)/\(parts[0])/\(parts[1])#\(parts[3])@\(headSha)"
    }

    /// Whether this node's auto-monitor must NOT originate `duty` work: the mesh
    /// assigns the duty to other nodes only (their own monitors originate there,
    /// with full local in-flight tracking). Returns the assigned node ids to stand
    /// down for, or nil to originate here (assigned to us, or nobody assigned — a
    /// duty nobody can take is still better handled than dropped).
    ///
    /// `assignments` is the node's state.json assignments map; the node already
    /// recomputes it on peer-down, so an assignee listed there is a live one
    /// modulo gossip lag (the claim gate covers that window).
    public static func standDown(assignments: [String: MeshAssignment],
                                 selfID: String, duty: String) -> [String]? {
        let assigned = (assignments[duty]?.assigned ?? []).filter { !$0.isEmpty }
        if assigned.isEmpty || assigned.contains(selfID) { return nil }
        return assigned
    }
}
