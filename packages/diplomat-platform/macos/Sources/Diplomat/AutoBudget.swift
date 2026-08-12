import Foundation
import DiplomatCore

/// Can this machine afford to start another automatic task right now?
///
/// The pure arithmetic is `AgentDispatchGate.budgetDecide` and the pieces it is fed;
/// this is the assembly — the telemetry ledger folded and priced, the live quota probe,
/// and the operator's three knobs. Python twin: `diplomat_runtime/autobudget.py`, which the
/// mesh node uses to answer the same question about work a peer routes in. Both read
/// the same ledger and the same config file, so a device cannot end up with one budget
/// for work it found itself and another for work it was sent.
enum AutoBudget {
    /// How long one answer is reused. The quota probe behind it is already cached for
    /// ~a minute (`Quota.ttlSecs`), but the ledger fold and the summarize pass are not,
    /// and a poll that finds eight units of owed work asks this eight times in a row
    /// about a machine whose spend cannot have moved meanwhile.
    static let ttlSecs: TimeInterval = 20

    private static var cache: (until: TimeInterval, budget: AgentDispatchGate.Budget)?
    private static let lock = NSLock()

    /// Test hook: forget the cached answer.
    static func resetCache() {
        lock.lock(); defer { lock.unlock() }
        cache = nil
    }

    /// Whether the gate is switched on at all (Settings → PR AUTO-FIX).
    static var enabled: Bool { AppConfig.autoBudgetGate }

    /// The budget verdict for one more automatic task, from live evidence.
    ///
    /// Priced against the 5-hour window the same way the Telemetry screen prices it:
    /// the ledger's finished-and-locally-run tasks, each as a share of the window it
    /// was spent from (`Telemetry.summarize`), give a mean and a spread, and the upper
    /// prediction bound on those is what one more task is required to fit inside. The
    /// 7-day window is the same tasks rescaled by the ratio of the two calibrations,
    /// since a task is a fixed number of tokens and only the divisor differs.
    ///
    /// What is LEFT comes from the probe rather than from the ledger's last sample:
    /// samples are written every 15 minutes, and a gate reading one of those would let
    /// a quarter-hour of spending — several agents' worth — go unnoticed.
    ///
    /// Never throws. A probe that cannot answer and a ledger that will not parse
    /// degrade to the same place: no measurement, and `budgetDecide`'s fail-open.
    static func decide(now: TimeInterval = Date().timeIntervalSince1970)
        -> AgentDispatchGate.Budget {
        lock.lock()
        if let cached = cache, now < cached.until {
            lock.unlock()
            return cached.budget
        }
        lock.unlock()
        let budget = decideUncached()
        lock.lock()
        cache = (now + ttlSecs, budget)
        lock.unlock()
        return budget
    }

    private static func decideUncached() -> AgentDispatchGate.Budget {
        let (sessionLeft, weekLeft) = Quota.fractionsLeft()
        var sessionCost: Double?
        var weekCost: Double?
        if let model = try? CoreAssets.telemetry() {
            // The screen's DEFAULT lookback, not its longest and not whatever range the
            // operator last flipped it to: the gate is a background decision, and the
            // one thing that makes it auditable is that "Limit per task" as the screen
            // opens is the figure it was priced from. `steps`/`binCount` are floors —
            // the series and the histogram are the screen's, and only the
            // distribution's moments and the two calibrations are read here.
            let summary = Telemetry.summarize(
                TelemetryLog.load(), now: Date().timeIntervalSince1970,
                days: Double(model.defaultRangeDays), steps: 2, binCount: 1,
                z: model.confidence.z)
            (sessionCost, weekCost) = costs(
                summary, z: AgentDispatchGate.budgetZ(AppConfig.autoBudgetConfidence),
                minSample: model.minSample)
        }
        return AgentDispatchGate.budgetDecide(
            sessionLeftPct: sessionLeft.map { 100 * $0 },
            weekLeftPct: weekLeft.map { 100 * $0 },
            sessionCostPct: sessionCost, weekCostPct: weekCost,
            floorPct: AppConfig.autoBudgetFloorPct)
    }

    /// `(session, week)` upper bounds on what one more task costs, each as a percentage
    /// of its own window, or nil where the ledger cannot price it.
    ///
    /// `summary.perTask` is already a share-of-the-5-hour-window distribution. The
    /// week's is that one scaled by `sessionLimitTokens / weekLimitTokens`: both windows
    /// are priced in tokens from the same samples (`Telemetry.calibrate`), so a task
    /// worth *t* tokens is `100·t/session` of one and `100·t/week` of the other — a
    /// constant ratio, which mean, spread and bound all carry.
    static func costs(_ summary: Telemetry.Summary, z: Double,
                      minSample: Int) -> (Double?, Double?) {
        let d = summary.perTask
        guard let session = AgentDispatchGate.taskCostBound(
            mean: d.mean, sd: d.sd, count: d.count, z: z, minSample: minSample)
        else { return (nil, nil) }
        guard let weekLimit = summary.weekLimitTokens, weekLimit > 0,
              let sessionLimit = summary.sessionLimitTokens, sessionLimit > 0
        else { return (session, nil) }   // the 5-hour window is priced, the weekly one isn't
        return (session, session * sessionLimit / weekLimit)
    }

    /// Why a refusal refused, for the activity feed: which window is short, by how much,
    /// and whether the figure it was held to was measured or is the standing floor.
    ///
    /// A clause rather than a sentence, because what is being deferred differs by caller
    /// while the arithmetic behind it is the one thing every caller must quote
    /// identically.
    static func shortfall(_ budget: AgentDispatchGate.Budget) -> String {
        let window = budget.window == AgentDispatchGate.windowWeek ? "7-day" : "5-hour"
        let left = Telemetry.percent(budget.leftPct)
        let needed = Telemetry.percent(budget.neededPct)
        if budget.measured {
            return "\(left) of the \(window) rate limit left, and a task needs "
                + "up to \(needed) of it"
        }
        return "\(left) of the \(window) rate limit left, under the \(needed) kept "
            + "in hand until the ledger can price a task"
    }
}
