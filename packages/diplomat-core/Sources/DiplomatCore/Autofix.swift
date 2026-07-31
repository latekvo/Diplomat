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
    /// so two nodes observing the same commit derive the same key (szpontnet-spec/docs/12).
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

// MARK: - Unified dispatch gate (one workflow, two triggers)

/// The SPAWN buttons and the auto-monitors are two TRIGGERS for the very same
/// workflow: run one agent job. Everything from "run X (on PR #n)" onward — the
/// ban check, in-flight dedup, mesh coordination, spawn focus, activity label,
/// counters — is decided HERE, once, so the interfaces cannot drift apart.
/// Triggers stay thin: a click, or a poll's backoff decision. (2026-07-20: the
/// drift was not hypothetical — dedup lived only on some paths, dupes followed.)
///
/// The intended trigger asymmetries, in full (anything else is a bug):
/// - focus: a panel spawn brings the terminal forward, an auto spawn never steals
///   focus (`stealsFocus`);
/// - capacity: only auto work is held to the device's automatic-task cap — a
///   human's click is one deliberate agent, not a monitor emptying its queue
///   (`decide`);
/// - mesh: only auto origination is mesh-gated — a human clicking THIS machine's
///   button has already decided placement (`decide`);
/// - counters: only a monitor's FIRST dispatch counts as auto-handled work
///   (`bumpsCounter`);
/// - label: auto rows carry the "Auto · " prefix, retries are surfaced the same
///   way on both (`label`).
///
/// Python twin: `autofix.dispatch_decide` etc. — keep byte-equivalent semantics
/// (see the parity tests on both sides).
public enum AgentDispatchGate {
    public enum Source: String {
        case panel, auto
    }

    public enum Verdict: Equatable {
        /// Run it.
        case proceed
        /// An agent is already working this PR (tracked row or a live `claude`
        /// visible in `ps`) — never double-spawn, whoever asks.
        case inFlight
        /// The author is on the prompt-injection ban list — never agent-review
        /// them, whoever asks. (Un-ban first if that is really wanted.)
        case banned
        /// Mesh: another live node originates this work (auto only).
        case standDown
        /// This device already runs its cap of concurrent automatic agents
        /// (auto only). Deferred, not dropped — see `decide`.
        case atCapacity
    }

    /// The one decision both interfaces obey, in fixed precedence: ban, then
    /// in-flight, then (auto only) this device's concurrency cap, then (auto only)
    /// mesh.
    ///
    /// Capacity outranks mesh so a saturated device never *originates*: the claim
    /// that routing takes has gossip side effects, and a node holding the claim for
    /// work it then refuses to start is worse than not asking. It is safe to leave
    /// the work for a later poll — every machine scans, so on a mesh a peer with
    /// room picks the same unit up, and off a mesh the reconciler retries it here on
    /// the next tick (the refusal writes no attempt record, so no backoff engages).
    public static func decide(source: Source, banned: Bool, agentOnPR: Bool,
                              meshStandsDown: Bool, atCapacity: Bool) -> Verdict {
        if banned { return .banned }
        if agentOnPR { return .inFlight }
        if source == .auto, atCapacity { return .atCapacity }
        if source == .auto, meshStandsDown { return .standDown }
        return .proceed
    }

    // MARK: - The device's automatic-task cap
    //
    // A poll finds every unit of pending work at once — N conflicted PRs, N reviews
    // owed — and, before this cap existed, dispatched all of them in one pass: N
    // terminal windows, N `claude` sessions, one machine. The cap is the device's,
    // not the monitor's: it bounds how many automatic agents Diplomat has RUNNING
    // here, so it holds across the review monitor, the conflict reconciler and work
    // a mesh peer routes in (the node asks its host before it spawns).

    public static let defaultAutoTaskLimit = 2
    public static let minAutoTaskLimit = 1
    public static let maxAutoTaskLimit = 16

    /// The configured cap, held inside the range the UI offers. A stored 0 would
    /// silently stop all automatic work while both monitor toggles still read "on",
    /// so the floor is 1 — pausing is what those toggles are for.
    public static func clampAutoTaskLimit(_ value: Int) -> Int {
        max(minAutoTaskLimit, min(maxAutoTaskLimit, value))
    }

    /// How many automatic agents are running on this device, counted in PRs (one
    /// agent per PR is what the in-flight dedup guarantees).
    ///
    /// Three inputs, because no single one of them is both complete and attributable:
    ///
    /// - `livePRs` — PRs with a live `claude` visible in `ps`. The ground truth, and
    ///   the only evidence that survives an applet restart, but it cannot say who
    ///   started an agent;
    /// - `manualPRs` — PRs whose live agent this applet tracked as a *panel* spawn.
    ///   Subtracted, because a click is the operator's own act and never spends the
    ///   automatic budget;
    /// - `autoPRs` — PRs with a tracked auto agent. Added, because a just-spawned
    ///   agent takes a moment to appear in `ps` and would otherwise be counted zero
    ///   times by the very poll that started it.
    ///
    /// An agent nobody tracked therefore counts as automatic. That is the safe way to
    /// be wrong: the cost is deferring auto work behind an untracked agent for as long
    /// as it runs, where the opposite error is the burst this cap exists to stop.
    public static func runningAutoTasks(livePRs: Set<Int>, autoPRs: Set<Int>,
                                        manualPRs: Set<Int>) -> Int {
        livePRs.subtracting(manualPRs).union(autoPRs).count
    }

    /// Panel spawns come to the front; auto spawns must never steal focus.
    public static func stealsFocus(_ source: Source) -> Bool { source == .panel }

    /// The activity/session label both interfaces produce: same core, the source
    /// prefix and retry suffix applied identically everywhere.
    public static func label(source: Source, core: String, attemptNumber: Int = 1) -> String {
        let retry = attemptNumber > 1 ? " · retry \(attemptNumber)" : ""
        return (source == .auto ? "Auto · " : "") + core + retry
    }

    /// Auto-handled counters bump only on a monitor's first dispatch — a retry is
    /// not new work handled, and a manual run is the user's own action.
    public static func bumpsCounter(source: Source, attemptNumber: Int) -> Bool {
        source == .auto && attemptNumber == 1
    }
}

// MARK: - Mesh coordination for the auto-monitors (mirrors autofix.py's twin)
//
// Two machines running this monitor poll the same GitHub state as the same user, so
// each is an independent origin of the same work (szpontnet-spec/docs/12-work-claims.md).
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
    /// convention from szpontnet-spec/docs/12: `<kind>:<host>/<owner>/<repo>#<n>@<sha>`.
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
}
