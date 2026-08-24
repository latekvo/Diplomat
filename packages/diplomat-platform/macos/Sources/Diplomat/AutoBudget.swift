import Foundation
import DiplomatCore

/// Can this machine afford to start another automatic task right now?
///
/// The pure arithmetic is `AgentDispatchGate.budgetDecide` and the pieces it is fed;
/// this is the assembly — the telemetry ledger folded and priced, the live probe, and
/// the operator's knobs. Python twin: `diplomat_runtime/autobudget.py`, which the mesh
/// node uses to answer the same question about work a peer routes in — work this app
/// never sees, and which is paid for out of this machine's account just the same. Both
/// read the same ledger and the same config file, so a device cannot end up with one
/// budget for work it found itself and another for work it was sent.
///
/// **Which currency.** What an agent spends depends on what runs it, so the runner
/// picks the ceilings and the unit both sides of the comparison are in: Claude Code
/// draws on Anthropic rate-limit windows, published only as a percentage; every other
/// runner is billed in money by whichever provider it is logged into. Neither reading
/// substitutes for the other — a Hermes task held against a Claude window is gated on a
/// limit it never touches, and a machine not logged into Claude Code at all would have
/// no gate whatsoever.
enum AutoBudget {
    /// How long one answer is reused. The probe behind it is already cached for ~a
    /// minute (`Quota.ttlSecs`, `Spend.ttlSecs`), but the ledger fold and the summarize
    /// pass are not, and a poll that finds eight units of owed work asks this eight
    /// times in a row about a machine whose spend cannot have moved meanwhile.
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
        let budget = AppConfig.agentRunner == .claude ? decideClaude() : decideMoney()
        lock.lock()
        cache = (now + ttlSecs, budget)
        lock.unlock()
        return budget
    }

    /// The ledger, summarized the way the Telemetry screen summarizes it.
    ///
    /// The screen's DEFAULT lookback, not its longest and not whatever range the
    /// operator last flipped it to: the gate is a background decision, and the one
    /// thing that makes it auditable is that "Limit per task" as the screen opens is
    /// the figure it was priced from. `steps`/`binCount` are floors — the series and
    /// the histogram are the screen's, and only the distributions' moments and the two
    /// calibrations are read here.
    private static func summary() -> (Telemetry.Summary, Int)? {
        guard let model = try? CoreAssets.telemetry() else { return nil }
        return (Telemetry.summarize(TelemetryLog.load(),
                                    now: Date().timeIntervalSince1970,
                                    days: Double(model.defaultRangeDays), steps: 2,
                                    binCount: 1, z: model.confidence.z),
                model.minSample)
    }

    /// The verdict for a machine spending Anthropic rate-limit windows.
    ///
    /// Priced against both windows the same way the Telemetry screen prices them: the
    /// ledger's finished-and-locally-run tasks, each as a share of the window it was
    /// spent from, give a mean and a spread per window, and the upper prediction bound
    /// on those is what one more task is required to fit inside.
    ///
    /// What is LEFT comes from the probe rather than from the ledger's last sample:
    /// samples are written every 15 minutes, and a gate reading one of those would let
    /// a quarter-hour of spending — several agents' worth — go unnoticed.
    private static func decideClaude() -> AgentDispatchGate.Budget {
        let (sessionLeft, weekLeft) = Quota.fractionsLeft()
        var sessionCost: Double?
        var weekCost: Double?
        if let (s, minSample) = summary() {
            (sessionCost, weekCost) = costs(
                s, z: AgentDispatchGate.budgetZ(AppConfig.autoBudgetConfidence),
                minSample: minSample)
        }
        return AgentDispatchGate.budgetDecide(
            [(AgentDispatchGate.windowSession, sessionLeft.map { 100 * $0 }, sessionCost),
             (AgentDispatchGate.windowWeek, weekLeft.map { 100 * $0 }, weekCost)],
            floor: AppConfig.autoBudgetFloorPct, unit: AgentDispatchGate.unitPct)
    }

    /// The verdict for a machine whose agents are billed in money.
    ///
    /// The same statistic in the other currency: the ledger's finished tasks, each at
    /// what the provider charged for it, bound the cost of one more, and what is left
    /// is what the account has on each of its two ceilings (`Spend`). Both are dollars
    /// already, so unlike the windows above neither needs converting into the other's
    /// terms — and the same task cost gates both, since a dollar spent is a dollar off
    /// each of them.
    ///
    /// The key's own cap is listed first, so it is the one named when both bind
    /// equally: it is the ceiling that refills on its own, and naming it tells the
    /// operator to wait rather than to go and top the account up.
    ///
    /// **A ledger with no billed task at all decides nothing.** The reserve exists to
    /// hold work back while a machine that spends money has not yet been measured, and
    /// applying it to a machine that spends none — a runner pointed at a local model,
    /// or one Diplomat cannot price — would hold that machine's work against an account
    /// it never draws on, purely because a key for that account is on disk. So the
    /// standing reserve engages only once something has actually been charged here;
    /// before that this is the same fail-open a silent probe gets, and the task cap is
    /// still in front of it.
    private static func decideMoney() -> AgentDispatchGate.Budget {
        let unbilled = AgentDispatchGate.Budget(affordable: true,
                                                unit: AgentDispatchGate.unitUsd)
        // A ledger that will not parse cannot show the evidence the reserve needs
        // either, so it lands where an unbilled machine does rather than on a floor it
        // was never shown to owe.
        guard let (s, minSample) = summary(), s.perTaskUsd.count > 0 else { return unbilled }
        let d = s.perTaskUsd
        let cost = AgentDispatchGate.taskCostBound(
            mean: d.mean, sd: d.sd, count: d.count,
            z: AgentDispatchGate.budgetZ(AppConfig.autoBudgetConfidence),
            minSample: minSample)
        let balance = Spend.balance()
        return AgentDispatchGate.budgetDecide(
            [(AgentDispatchGate.windowKey, balance.keyLeft, cost),
             (AgentDispatchGate.windowCredits, balance.creditLeft, cost)],
            floor: AppConfig.autoBudgetReserveUsd, unit: AgentDispatchGate.unitUsd)
    }

    /// `(session, week)` upper bounds on what one more task costs, each as a percentage
    /// of its own window, or nil where the ledger cannot price that window.
    ///
    /// One bound per window from that window's own distribution — the same pair the
    /// Telemetry screen draws. Each is priced from its own quota readings, so a week
    /// the samples cannot price leaves the 5-hour gate measured, and the reverse.
    static func costs(_ summary: Telemetry.Summary, z: Double,
                      minSample: Int) -> (Double?, Double?) {
        let s = summary.perTask, w = summary.perTaskWeek
        return (AgentDispatchGate.taskCostBound(mean: s.mean, sd: s.sd, count: s.count,
                                                z: z, minSample: minSample),
                AgentDispatchGate.taskCostBound(mean: w.mean, sd: w.sd, count: w.count,
                                                z: z, minSample: minSample))
    }

    /// What each ceiling is called in the one line the feed prints about it.
    private static let ceilings = [
        AgentDispatchGate.windowSession: "5-hour rate limit",
        AgentDispatchGate.windowWeek: "7-day rate limit",
        AgentDispatchGate.windowKey: "OpenRouter key limit",
        AgentDispatchGate.windowCredits: "OpenRouter credit balance",
    ]

    /// Why a refusal refused, for the activity feed: which ceiling is short, by how
    /// much, and whether the figure it was held to was measured or is the standing
    /// floor.
    ///
    /// A clause rather than a sentence, because what is being deferred differs by caller
    /// while the arithmetic behind it is the one thing every caller must quote
    /// identically.
    static func shortfall(_ budget: AgentDispatchGate.Budget) -> String {
        let ceiling = ceilings[budget.window] ?? "limit"
        let fmt: (Double) -> String =
            budget.unit == AgentDispatchGate.unitUsd ? Telemetry.money : Telemetry.percent
        let left = fmt(budget.left)
        let needed = fmt(budget.needed)
        if budget.measured {
            return "\(left) of the \(ceiling) left, and a task needs "
                + "up to \(needed) of it"
        }
        return "\(left) of the \(ceiling) left, under the \(needed) kept "
            + "in hand until the ledger can price a task"
    }
}
