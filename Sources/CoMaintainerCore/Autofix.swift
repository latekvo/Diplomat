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

    public init(number: Int, title: String, url: String, isDraft: Bool,
                mergeable: String, reviewDecision: String,
                threadsUnresolved: Int, threadsIOwe: Int = 0) {
        self.number = number
        self.title = title
        self.url = url
        self.isDraft = isDraft
        self.mergeable = mergeable
        self.reviewDecision = reviewDecision
        self.threadsUnresolved = threadsUnresolved
        self.threadsIOwe = threadsIOwe
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
