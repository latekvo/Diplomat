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
/// - budget: only auto work is held to what is left of the rate-limit windows —
///   a human spending their own last 5% is their call to make (`decide`);
/// - mesh: only auto origination is mesh-gated — a human clicking THIS machine's
///   button has already decided placement (`decide`);
/// - counters: only a monitor's FIRST dispatch counts as auto-handled work
///   (`bumpsCounter`);
/// - label: rows a monitor found carry the "Auto · " prefix, retries are surfaced
///   the same way on both (`label`).
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
        /// An agent is already working this PR (`AgentState.inFlight`, which counts
        /// every run that is not over, tracked or not) — never double-spawn, whoever
        /// asks.
        case inFlight
        /// The author is on the prompt-injection ban list — never agent-review
        /// them, whoever asks. (Un-ban first if that is really wanted.)
        case banned
        /// Mesh: another live node originates this work (auto only).
        case standDown
        /// This device already runs its cap of concurrent automatic agents
        /// (auto only). Deferred, not dropped — see `decide`.
        case atCapacity
        /// Too little of the rate-limit windows is left to cover another automatic
        /// task (auto only). Deferred, not dropped — see `decide`.
        case unaffordable
    }

    /// The one decision both interfaces obey, in fixed precedence: ban, then
    /// in-flight, then (auto only) this device's concurrency cap, then (auto only)
    /// its rate-limit budget, then (auto only) mesh.
    ///
    /// Capacity outranks mesh so a saturated device never *originates*: the claim
    /// that routing takes has gossip side effects, and a node holding the claim for
    /// work it then refuses to start is worse than not asking. It is safe to leave
    /// the work for a later poll — every machine scans, so on a mesh a peer with
    /// room picks the same unit up, and off a mesh the reconciler retries it here on
    /// the next tick (the refusal writes no attempt record, so no backoff engages).
    ///
    /// The budget sits between the two for the same reason and with the same
    /// consequence: an account with no window left cannot finish the agent it would
    /// claim the work for, and holding the job costs nothing but the wait for the
    /// 5-hour window to refill. It ranks BELOW capacity only because capacity is the
    /// measurement already in hand — a saturated device has no slot to spend a
    /// budget on, so the probe is never worth taking.
    public static func decide(source: Source, banned: Bool, agentOnPR: Bool,
                              meshStandsDown: Bool, atCapacity: Bool,
                              unaffordable: Bool = false) -> Verdict {
        if banned { return .banned }
        if agentOnPR { return .inFlight }
        if source == .auto, atCapacity { return .atCapacity }
        if source == .auto, unaffordable { return .unaffordable }
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

    /// How many automatic agents are *working* on this device, counted in PRs (one
    /// agent per PR is what the in-flight dedup guarantees).
    ///
    /// Four inputs, because no single one of them is both complete and attributable:
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
    /// - `idlePRs` — PRs whose agent has finished its turn and is sitting at its
    ///   prompt (`AgentActivity.looksBusy` over that session's visible buffer).
    ///   Subtracted LAST, from the union, because an idle agent is idle however it was
    ///   found: a tracked `autoPRs` entry that went quiet has to leave too, or
    ///   re-adding it here would hold the very slot the subtraction is for.
    ///
    /// An agent nobody tracked therefore counts as automatic. That is the safe way to
    /// be wrong: the cost is deferring auto work behind an untracked agent for as long
    /// as it runs, where the opposite error is the burst this cap exists to stop.
    ///
    /// Idleness is subtracted rather than merely labelled because an agent is spawned
    /// into an INTERACTIVE session, which does not exit when its work is done — it
    /// waits at the prompt for a human who may not come for hours. The cap exists to
    /// bound concurrent LOAD, and a session waiting on input is spending none; left
    /// counted, a finished agent holds its slot until someone closes the window, and a
    /// machine whose every bay is held that way defers automatic work indefinitely
    /// while doing nothing.
    ///
    /// Only positive evidence of idleness qualifies (a session whose buffer was read
    /// and showed no interrupt hint): an agent whose terminal could not be read counts
    /// as working, so the failure direction stays the deferral, never the burst.
    public static func runningAutoTasks(livePRs: Set<Int>, autoPRs: Set<Int>,
                                        manualPRs: Set<Int>,
                                        idlePRs: Set<Int> = []) -> Int {
        livePRs.subtracting(manualPRs).union(autoPRs).subtracting(idlePRs).count
    }

    // MARK: - The device's rate-limit budget
    //
    // The cap above bounds how many automatic agents run at once; this bounds
    // whether any of them should start at all. A machine can have three empty bays
    // and 4% of its 5-hour window left, and spending that on an auto-review is how
    // the operator finds the limit gone the next time they sit down to work.
    //
    // What a task costs is a measurement, not a guess: the telemetry ledger prices
    // every finished agent against the window it was spent from (`Telemetry.summarize`
    // → `perTask`, a share-of-window distribution). So the question "can we afford
    // one more" has a statistical answer — and the one worth asking is about the
    // NEXT task, not about the average one. Half of all tasks cost more than the
    // mean, and the distribution is right-skewed (most small, a few enormous), so a
    // gate set at the mean would wave through the expensive tail every time.
    //
    // Hence a one-sided upper PREDICTION bound: the cost that one more task will
    // come in under, with the configured confidence. That is what `autoBudgetConfidence`
    // buys — at 95%, roughly one auto-task in twenty may still overrun what it was
    // gated on.
    //
    // Python twin: the same names in `autofix.py`.

    /// Supported confidence levels (percent) and their ONE-SIDED standard-normal
    /// quantiles. One-sided because only the upper tail is a budget question:
    /// nothing goes wrong when a task turns out cheaper than predicted. (The
    /// Telemetry screen's own band is a different statistic — a two-sided interval
    /// on the MEAN, z = 1.96 — and the two are not interchangeable.)
    public static let budgetConfidenceZ: [Int: Double] = [
        50: 0.0, 80: 0.8416, 90: 1.2816, 95: 1.6449, 99: 2.3263,
    ]

    public static let defaultBudgetConfidence = 95
    /// Share of a window to keep in hand when the ledger cannot price a task yet.
    public static let defaultBudgetFloorPct = 20.0
    /// A prediction bound needs a spread, and the sample standard deviation of one
    /// observation is 0 — which would report a single cheap task as certainty. Below
    /// this the ledger has no answer and the floor stands in, however the caller's
    /// own minimum is configured.
    public static let minBudgetSamples = 2

    public static let windowSession = "session"   // the 5-hour rate-limit window
    public static let windowWeek = "week"         // the 7-day one

    /// The configured confidence, snapped to a level `budgetConfidenceZ` has a
    /// quantile for.
    ///
    /// Rounds UP to the next supported level rather than to the nearest, so a
    /// hand-edited file lands on the stricter of the two neighbours: a value this
    /// table cannot honour should hold work back, never wave it through on a looser
    /// bound than was asked for.
    public static func clampBudgetConfidence(_ value: Int) -> Int {
        let levels = budgetConfidenceZ.keys.sorted()
        return levels.first(where: { $0 >= value }) ?? levels[levels.count - 1]
    }

    /// The one-sided normal quantile for a confidence level (percent).
    public static func budgetZ(_ confidence: Int) -> Double {
        budgetConfidenceZ[clampBudgetConfidence(confidence)] ?? 0
    }

    /// The configured floor, held to a real share of a window. 0 is allowed and
    /// means "spend it to the last drop while the ledger is still thin".
    public static func clampBudgetFloorPct(_ value: Double) -> Double {
        guard value.isFinite else { return defaultBudgetFloorPct }
        return max(0, min(100, value))
    }

    /// What one more auto-task will cost at most, as a share of the window
    /// `mean`/`sd` are shares of — the upper end of a one-sided prediction
    /// interval, `mean + z·sd·√(1 + 1/n)`.
    ///
    /// The `√(1 + 1/n)` is what makes this a bound on the NEXT observation rather
    /// than on the mean: it carries the spread of the tasks themselves plus the
    /// uncertainty in where their average sits, and so stops narrowing as the ledger
    /// fills. (The interval the Telemetry screen draws is the other one, `z·sd/√n`,
    /// and converges on the mean — a gate built from it would end up approving the
    /// average task, which by construction half of them cost more than.)
    ///
    /// nil when the ledger cannot answer: fewer finished-and-priced tasks than the
    /// caller's minimum, or a non-finite figure from an unusable one. The caller
    /// then falls back to the configured floor.
    public static func taskCostBound(mean: Double, sd: Double, count: Int,
                                     z: Double, minSample: Int) -> Double? {
        if count < max(minBudgetSamples, minSample) { return nil }
        guard mean.isFinite, sd.isFinite, z.isFinite else { return nil }
        return mean + z * sd * (1.0 + 1.0 / Double(count)).squareRoot()
    }

    /// Whether what is left of the rate-limit windows covers one more auto-task,
    /// and the arithmetic that decided it — the numbers the activity feed quotes
    /// back when work is held.
    public struct Budget: Equatable {
        public let affordable: Bool
        /// The window the verdict came from: the one with the LEAST headroom,
        /// whether it refused or not, so the same field explains an approval and a
        /// refusal. Empty when neither window had a reading and nothing was decided.
        public let window: String
        /// What that window had left, and what a task was required to fit inside,
        /// both as percentages of it.
        public let leftPct: Double
        public let neededPct: Double
        /// True when `neededPct` was priced from the ledger, false when the
        /// telemetry was too thin and the configured floor stood in for it.
        public let measured: Bool

        public init(affordable: Bool, window: String = "", leftPct: Double = 0,
                    neededPct: Double = 0, measured: Bool = false) {
            self.affordable = affordable
            self.window = window
            self.leftPct = leftPct
            self.neededPct = neededPct
            self.measured = measured
        }
    }

    /// Can one more automatic task be afforded right now?
    ///
    /// Both windows gate, because either can be the one that runs out: the 5-hour
    /// window is what stops work this afternoon, and the 7-day window is the ceiling
    /// a busy week walks into. A task has to fit inside what is left of each.
    ///
    /// A window with no cost measurement falls back to `floorPct` — "keep this much
    /// of the limit in hand" — which is the whole of the answer on a machine whose
    /// ledger has not priced a task yet.
    ///
    /// A window with **no reading at all** is skipped, and a call where neither
    /// window has one is affordable. That is deliberate: the usage probe can be
    /// switched off (`DIPLOMAT_QUOTA_PROBE=0`), logged out, or simply offline, and a
    /// gate that read silence as "no budget" would take a machine's automatic work
    /// with it every time the network dropped. The gate exists to spend a *measured*
    /// limit carefully; with nothing measured it has no opinion, and the task cap is
    /// still in front of it.
    public static func budgetDecide(sessionLeftPct: Double?, weekLeftPct: Double?,
                                    sessionCostPct: Double?, weekCostPct: Double?,
                                    floorPct: Double) -> Budget {
        var tightest: Budget?
        for (window, left, cost) in [(windowSession, sessionLeftPct, sessionCostPct),
                                     (windowWeek, weekLeftPct, weekCostPct)] {
            guard let left = left else { continue }
            let needed = cost ?? floorPct
            if let best = tightest, left - needed >= best.leftPct - best.neededPct {
                continue   // the other window is the binding one; session wins a tie
            }
            tightest = Budget(affordable: left >= needed, window: window,
                              leftPct: left, neededPct: needed, measured: cost != nil)
        }
        return tightest ?? Budget(affordable: true)
    }

    /// Panel spawns come to the front; auto spawns must never steal focus.
    public static func stealsFocus(_ source: Source) -> Bool { source == .panel }

    /// The activity/session label both interfaces produce: same core, the source
    /// prefix and retry suffix applied identically everywhere.
    ///
    /// `requested` drops the prefix for work the operator asked for by name and the
    /// queue merely chose the moment for — a review from a PR sweep. Such a job is
    /// dispatched as `.auto` in every other respect (it waits for the cap, it holds a
    /// bay while it runs), but "Auto · " answers *who decided there was work here*,
    /// and for this one that was the operator. Without it a requested review of #12
    /// and the review-reply monitor's own dispatch on #12 read as the same row.
    public static func label(source: Source, core: String, attemptNumber: Int = 1,
                             requested: Bool = false) -> String {
        let retry = attemptNumber > 1 ? " · retry \(attemptNumber)" : ""
        return (source == .auto && !requested ? "Auto · " : "") + core + retry
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
        guard !headSha.isEmpty, let ref = prRef(prURL) else { return "" }
        return "\(kind):\(ref)@\(headSha)"
    }

    /// `<host>/<owner>/<repo>#<n>` for a PR URL, or nil when it isn't one. Split out
    /// of `workKey` so the ledger key below cannot parse a URL differently from the
    /// claim key — the two identify the same unit of work and are compared against
    /// each other in the ledger.
    private static func prRef(_ prURL: String) -> String? {
        guard let u = URL(string: prURL),
              let host = u.host?.lowercased(), !host.isEmpty else { return nil }
        let parts = u.pathComponents.filter { $0 != "/" }
        guard parts.count == 4, parts[2] == "pull",
              parts[3].allSatisfy(\.isNumber), !parts[3].isEmpty else { return nil }
        return "\(host)/\(parts[0])/\(parts[1])#\(parts[3])"
    }

    /// The telemetry ledger's identity for one unit of work.
    ///
    /// The claim key when a head sha is known — same string, so the two records of
    /// one job agree — and the same shape WITHOUT `@sha` when it isn't. `workKey`
    /// deliberately returns "" there, because skipping the mesh claim is the safe
    /// degradation for a *claim*; skipping the ledger entry is not, since the work
    /// still gets dispatched and would then be missing from every figure on the
    /// screen. The cost of the fallback is that two pushes to one PR fold into one
    /// ledger task while the sha is unknown, which understates the count rather
    /// than inventing one.
    public static func ledgerKey(kind: String, prURL: String, headSha: String) -> String {
        guard let ref = prRef(prURL) else { return "" }
        return headSha.isEmpty ? "\(kind):\(ref)" : "\(kind):\(ref)@\(headSha)"
    }
}
