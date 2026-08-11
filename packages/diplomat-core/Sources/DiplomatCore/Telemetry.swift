import Foundation

/// The Telemetry screen's arithmetic: fold the append-only ledger into per-task
/// records, then reduce those into the figures both front-ends render.
///
/// **Why a ledger and not counters.** Four of the seven figures are about *time* —
/// how long work waited, how long it ran, how much was owed on each of the last
/// fourteen days — and a counter can't be asked what it read last Tuesday. So the
/// monitors append events (`~/.diplomat/pr-monitor/telemetry.jsonl`, one JSON
/// object per line, `O_APPEND` like the activity feed) and everything the screen
/// shows is derived here, on read. Nothing in the file is a summary; a bug in this
/// arithmetic is fixed by editing this file, not by re-gathering weeks of data.
///
/// The event vocabulary, per unit of auto-work (keyed by the mesh work key, which
/// is already the identity two machines agree on — see `AutofixMesh.workKey`):
///
/// - `queued`  — a poll saw this work owed for the first time.
/// - `started` — an agent was dispatched for it (`remote` when the mesh placed it
///               on a peer, in which case the tokens are that machine's, not ours).
/// - `done`    — the completion sentinel fired, carrying the tokens the agent's own
///               transcript accounts for.
/// - `cleared` — a poll no longer sees it owed and we never started it (someone
///               replied by hand, the PR closed, a peer took it).
///
/// plus one `sample` per poll carrying the account's remaining quota fractions and
/// this machine's cumulative Claude token counters, split monitored-repo vs
/// everything else.
///
/// The quota fractions are a measurement, and the screen draws them as one
/// (`quotaSeries`): both rate-limit windows over the lookback, resets and all. They
/// do double duty as the thing that makes *tokens* comparable to a limit — the pair
/// (quota consumed, tokens spent) over an interval prices the window in tokens, and
/// a task's own token count then converts to a share of it. See `calibrate`.
///
/// Pure and `Foundation`-only, so it builds on Linux and is driven by
/// `diplomat-core telemetry` — which is how `test_telemetry_parity.py` diffs it
/// against the Python twin in `diplomat_app/telemetry.py`. Keep the two in step.
public enum Telemetry {

    // MARK: - Ledger

    /// One unit of auto-work, folded from every event carrying its key.
    public struct Task: Equatable {
        public let key: String
        /// The mesh duty the work belongs to: `review` or `conflicts`.
        public var duty: String
        public var pr: Int
        /// First time a poll saw it owed. Nil for work dispatched by a trigger that
        /// never observed it as owed (a wizard click), which the screen ignores.
        public var queuedAt: Double?
        public var startedAt: Double?
        public var doneAt: Double?
        public var clearedAt: Double?
        /// The mesh placed it on a peer: it consumed that machine's quota, so it
        /// counts toward the owed/started tallies but never toward the token spread.
        public var remote: Bool
        /// Tokens the agent's own transcript accounts for (input + output + cache
        /// creation, matching `usage._tokenCost`). Nil when the transcript couldn't
        /// be tied back to the run.
        public var tokens: Double?

        /// Seconds from an agent starting to its completion sentinel.
        public var runSecs: Double? {
            guard let s = startedAt, let d = doneAt, d >= s else { return nil }
            return d - s
        }
        /// Seconds the work sat owed before an agent took it.
        public var waitSecs: Double? {
            guard let q = queuedAt, let s = startedAt, s >= q else { return nil }
            return s - q
        }
        /// Whether it was owed (and unstarted) at `t`. An unfinished task stays
        /// pending to the end of the range on purpose: it *was* owed for that whole
        /// stretch, including any span where the applet was off and nothing polled.
        public func pending(at t: Double) -> Bool {
            guard let q = queuedAt, q <= t else { return false }
            if let s = startedAt, s <= t { return false }
            if let c = clearedAt, c <= t { return false }
            return true
        }
    }

    /// One poll's reading of the account's quota and this machine's token counters.
    /// `repoTokens`/`otherTokens` are cumulative and monotonic within a run of the
    /// scanner; a drop means its cursor file was lost and the counters restarted,
    /// which every consumer here treats as a segment boundary rather than a delta.
    public struct Sample: Equatable {
        public let at: Double
        /// Fraction of the 5-hour session window still unspent, 1 = untouched. Nil
        /// when the OAuth quota probe had nothing to say.
        public let sessionLeft: Double?
        public let weekLeft: Double?
        public let repoTokens: Double
        public let otherTokens: Double
    }

    /// Everything the ledger holds, in file order.
    public struct Ledger: Equatable {
        public var tasks: [Task] = []
        public var samples: [Sample] = []

        /// The empty ledger a front-end starts from before its first fold.
        public init(tasks: [Task] = [], samples: [Sample] = []) {
            self.tasks = tasks
            self.samples = samples
        }
    }

    /// Fold raw ledger lines into tasks and samples. Unparseable lines, unknown
    /// event verbs and events with no `at`/`key` are skipped: the file is appended
    /// to by two platforms and a partially-written tail is normal, so one bad line
    /// must cost that line and nothing else.
    ///
    /// Repeat events for one key are first-wins on every instant. That is what
    /// makes a retry read correctly: attempt 2 appends a second `started`, and the
    /// wait we report is still the wait until work actually began.
    public static func fold(lines: [String]) -> Ledger {
        var order: [String] = []
        var byKey: [String: Task] = [:]
        var samples: [Sample] = []

        for line in lines {
            let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty, let data = trimmed.data(using: .utf8),
                  let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  let at = number(obj["at"]), let ev = obj["ev"] as? String
            else { continue }

            if ev == "sample" {
                samples.append(Sample(at: at,
                                      sessionLeft: number(obj["sessionLeft"]),
                                      weekLeft: number(obj["weekLeft"]),
                                      repoTokens: number(obj["repoTokens"]) ?? 0,
                                      otherTokens: number(obj["otherTokens"]) ?? 0))
                continue
            }
            guard let key = obj["key"] as? String, !key.isEmpty else { continue }
            let fresh = byKey[key] == nil
            var task = byKey[key] ?? Task(key: key, duty: "", pr: 0, queuedAt: nil,
                                          startedAt: nil, doneAt: nil, clearedAt: nil,
                                          remote: false, tokens: nil)
            if let duty = obj["duty"] as? String, !duty.isEmpty { task.duty = duty }
            if let pr = number(obj["pr"]), pr > 0 { task.pr = Int(pr) }
            let known: Bool
            switch ev {
            case "queued":
                known = true
                if task.queuedAt == nil { task.queuedAt = at }
            case "started":
                known = true
                if task.startedAt == nil {
                    task.startedAt = at
                    task.remote = obj["remote"] as? Bool ?? false
                }
            case "done":
                known = true
                if task.doneAt == nil {
                    task.doneAt = at
                    task.tokens = number(obj["tokens"])
                }
            case "cleared":
                known = true
                if task.clearedAt == nil { task.clearedAt = at }
            default:
                known = false
            }
            // A verb this build doesn't understand — a newer platform's event, or a
            // corrupted line — must not conjure a timestamp-less task. Such a row
            // changes no figure, but it makes the key look already-recorded to
            // `observeOwed`, which would then never queue that work at all.
            guard known else { continue }
            if fresh { order.append(key) }
            byKey[key] = task
        }
        // Samples arrive in append order, which is chronological — but two processes
        // (the applet and a mesh node) append to one file, so a slow write can land
        // out of order. Every consumer below walks consecutive pairs, so sort once.
        samples.sort { $0.at < $1.at }
        return Ledger(tasks: order.compactMap { byKey[$0] }, samples: samples)
    }

    /// JSON numbers arrive as `NSNumber`; a hand-edited file can hold a numeric
    /// string. Non-finite values (which `JSONSerialization` will not produce but a
    /// concatenated file can carry through `Infinity`) are rejected, because one of
    /// them anywhere downstream turns every mean into `nan`.
    private static func number(_ raw: Any?) -> Double? {
        var v: Double
        if let n = raw as? NSNumber { v = n.doubleValue }
        else if let s = raw as? String, let d = Double(s) { v = d }
        else { return nil }
        return v.isFinite ? v : nil
    }

    // MARK: - Calibration: what a rate-limit window is worth, in tokens

    /// Tokens per 100% of a rate-limit window, measured from consecutive samples.
    ///
    /// Anthropic publishes a *utilization percentage*, never a token budget, and the
    /// budget is dynamic — so the only honest way to say "this task ate 4% of the
    /// window" is to price the window from what actually happened: over an interval
    /// the account spent `dUtil` of its window and this machine logged `dTokens`, so
    /// the whole window is worth `dTokens / dUtil`. Summing numerator and
    /// denominator across every usable interval weights long intervals more heavily,
    /// which is what you want — a 30-second interval's rounding error should not
    /// count as much as an hour's.
    ///
    /// Intervals are skipped when the window RESET between the two samples (quota
    /// went up, so `dUtil <= 0`) or when nothing was spent, since neither prices
    /// anything. Returns nil when no interval survives — the screen then falls back
    /// to the caller's heuristic ceiling and says the figure is an estimate.
    public static func calibrate(_ samples: [Sample], session: Bool) -> Double? {
        var tokens = 0.0, util = 0.0
        for i in 1..<max(samples.count, 1) {
            let a = samples[i - 1], b = samples[i]
            guard let left0 = session ? a.sessionLeft : a.weekLeft,
                  let left1 = session ? b.sessionLeft : b.weekLeft else { continue }
            let dUtil = left0 - left1
            guard dUtil > 0 else { continue }
            let dTokens = (b.repoTokens + b.otherTokens) - (a.repoTokens + a.otherTokens)
            guard dTokens > 0 else { continue }   // also drops a counter reset
            tokens += dTokens
            util += dUtil
        }
        guard util > 0, tokens > 0 else { return nil }
        return tokens / util
    }

    // MARK: - Distribution (the bell curve)

    public struct Bin: Equatable {
        public let lower: Double
        public let upper: Double
        public let count: Int
    }

    /// A histogram of one metric with a normal fitted over it and a confidence
    /// interval on the mean.
    ///
    /// The interval is on the MEAN, not on a single task: `z x sd / sqrt(n)`. That
    /// is the question a budget is planned against ("what does a task cost on
    /// average, and how well do we know that"), and it is the one that keeps
    /// narrowing as the ledger fills. `sd` is the sample standard deviation (n-1),
    /// so a single observation reports 0 rather than pretending to a spread.
    public struct Distribution: Equatable {
        public let count: Int
        public let mean: Double
        public let sd: Double
        public let stderr: Double
        public let ciLow: Double
        public let ciHigh: Double
        public let min: Double
        public let max: Double
        /// The median — printed beside the mean because this distribution is
        /// right-skewed in practice (most tasks are small, a few are enormous), and
        /// a mean well above the median is the reader's cue that it is.
        public let median: Double
        public let bins: [Bin]
        /// The fitted normal, sampled across the histogram's span and scaled to
        /// counts (density x n x binWidth) so it can be drawn straight over the bars.
        public let curve: [Double]

        public static let empty = Distribution(
            count: 0, mean: 0, sd: 0, stderr: 0, ciLow: 0, ciHigh: 0,
            min: 0, max: 0, median: 0, bins: [], curve: [])
    }

    /// Points the fitted curve is sampled at, per bin. Fixed so both platforms draw
    /// the same polyline.
    public static let curveResolution = 4

    public static func distribution(_ values: [Double], binCount: Int, z: Double) -> Distribution {
        guard !values.isEmpty else { return .empty }
        let n = Double(values.count)
        let mean = values.reduce(0, +) / n
        let variance = values.count > 1
            ? values.reduce(0) { $0 + ($1 - mean) * ($1 - mean) } / (n - 1)
            : 0
        let sd = variance.squareRoot()
        let stderr = values.count > 1 ? sd / n.squareRoot() : 0
        let sorted = values.sorted()
        let mid = sorted.count / 2
        let median = sorted.count % 2 == 1
            ? sorted[mid]
            : (sorted[mid - 1] + sorted[mid]) / 2

        // Bins run from 0, not from the smallest observation: this is a share of a
        // budget, so how close the mass sits to zero is the point of looking.
        let hi = Swift.max(sorted.last ?? 0, 1e-9)
        let width = hi / Double(binCount)
        var counts = [Int](repeating: 0, count: binCount)
        for v in values {
            // The top edge belongs to the last bin; without this the maximum lands
            // in bin `binCount` and is dropped from its own histogram.
            let idx = Swift.min(binCount - 1, Swift.max(0, Int((v / width).rounded(.down))))
            counts[idx] += 1
        }
        let bins = (0..<binCount).map {
            Bin(lower: Double($0) * width, upper: Double($0 + 1) * width, count: counts[$0])
        }

        var curve: [Double] = []
        let points = binCount * curveResolution
        if sd > 0 {
            for i in 0...points {
                let x = hi * Double(i) / Double(points)
                let zx = (x - mean) / sd
                let density = exp(-0.5 * zx * zx) / (sd * (2 * Double.pi).squareRoot())
                curve.append(density * n * width)
            }
        }
        return Distribution(count: values.count, mean: mean, sd: sd, stderr: stderr,
                            ciLow: mean - z * stderr, ciHigh: mean + z * stderr,
                            min: sorted.first ?? 0, max: sorted.last ?? 0,
                            median: median, bins: bins, curve: curve)
    }

    // MARK: - Pending-over-time series

    public struct PendingPoint: Equatable {
        public let at: Double
        public let reviews: Int
        public let conflicts: Int
    }

    /// How much work was owed but unstarted, sampled at `steps` evenly spaced
    /// instants ending at `now`. Split by duty, because the two answer different
    /// questions: reviews pile up when peers are waiting on you, conflict fixes when
    /// your own branches are rotting against main.
    public static func pendingSeries(_ tasks: [Task], now: Double, days: Double,
                                     steps: Int) -> [PendingPoint] {
        guard steps > 1, days > 0 else { return [] }
        let span = days * 86_400
        let start = now - span
        return (0..<steps).map { i in
            let t = start + span * Double(i) / Double(steps - 1)
            var reviews = 0, conflicts = 0
            for task in tasks where task.pending(at: t) {
                if task.duty == "conflicts" { conflicts += 1 } else { reviews += 1 }
            }
            return PendingPoint(at: t, reviews: reviews, conflicts: conflicts)
        }
    }

    // MARK: - Rate-limit windows over time

    /// One quota reading: what fraction of each rate-limit window was still unspent.
    public struct QuotaPoint: Equatable {
        public let at: Double
        /// Percent of the 5-hour window left, or nil when that reading is missing —
        /// the probe was offline, or Claude Code was logged out. Nil is NOT zero, and
        /// a chart must break its line rather than draw a plunge to the floor.
        public let sessionPct: Double?
        public let weekPct: Double?
    }

    /// The quota readings inside the range, oldest first.
    ///
    /// Unlike the pending series this is NOT resampled onto a fixed grid: these are
    /// measurements, taken every `sampleIntervalSecs`, and the 5-hour window's
    /// sawtooth is the shape worth seeing. Interpolating it onto 56 evenly spaced
    /// instants would smooth away the resets that give it its meaning.
    public static func quotaSeries(_ samples: [Sample], now: Double,
                                   days: Double) -> [QuotaPoint] {
        let start = now - days * 86_400
        return samples.filter { $0.at >= start && $0.at <= now }.map {
            QuotaPoint(at: $0.at,
                       sessionPct: $0.sessionLeft.map { 100 * $0 },
                       weekPct: $0.weekLeft.map { 100 * $0 })
        }
    }

    // MARK: - Token split

    /// Cumulative-counter deltas across the samples given, split monitored-repo vs
    /// everything else. A counter that went DOWN between two samples means the
    /// scanner's cursor file was lost and it restarted from zero, so that pair
    /// contributes nothing rather than a huge negative.
    public static func tokenSplit(_ samples: [Sample]) -> (repo: Double, other: Double) {
        var repo = 0.0, other = 0.0
        for i in 1..<max(samples.count, 1) {
            let a = samples[i - 1], b = samples[i]
            if b.repoTokens >= a.repoTokens { repo += b.repoTokens - a.repoTokens }
            if b.otherTokens >= a.otherTokens { other += b.otherTokens - a.otherTokens }
        }
        return (repo, other)
    }

    // MARK: - The whole screen, in one value

    public struct Summary: Equatable {
        /// Tokens per 100% of the 5-hour session window, and of the 7-day week.
        /// Nil until enough samples exist to price the window, in which case every
        /// percentage below is empty rather than guessed: Anthropic's limits are
        /// dynamic and account-specific, so a hardcoded ceiling would be a made-up
        /// number wearing a real one's clothes.
        public let sessionLimitTokens: Double?
        public let weekLimitTokens: Double?

        /// Share of the session window one task consumes, in percent.
        public let perTask: Distribution
        /// The same tasks against the 7-day window — one number, since the shape is
        /// the shape of `perTask` rescaled.
        public let perTaskWeekMean: Double
        /// Mean RAW tokens per task. Independent of the quota probe, so it is what
        /// the screen shows while the window has no price yet — an unanchored
        /// number, but a measured one.
        public let perTaskTokensMean: Double

        public let avgRunSecs: Double
        public let avgWaitSecs: Double
        public let runSamples: Int
        public let waitSamples: Int

        /// Every quota reading in the range, plus the latest of each window — what
        /// is left right now, which is the number the reader checks first.
        public let quota: [QuotaPoint]
        public let sessionLeftPct: Double?
        public let weekLeftPct: Double?

        public let pending: [PendingPoint]
        /// Owed right now, and the worst it got over the range.
        public let pendingReviewsNow: Int
        public let pendingConflictsNow: Int
        public let peakReviews: Int
        public let peakConflicts: Int

        public let repoTokens: Double
        public let otherTokens: Double
        /// Share of tokens spent in the repo the agents work in, in percent. 0 when
        /// nothing was spent at all.
        public let repoSharePct: Double

        /// Work counts over the range, for the "what this is measured over" line.
        public let queuedCount: Int
        public let startedCount: Int
        public let doneCount: Int
        public let remoteCount: Int
        /// Finished tasks whose transcript could not be tied back to the run, so
        /// they carry no tokens and sit out the spread.
        public let unattributedCount: Int

        /// Oldest and newest poll sample in the whole ledger (not just the range),
        /// so the screen can say how far back the data actually goes.
        public let firstSampleAt: Double?
        public let lastSampleAt: Double?
    }

    /// Reduce a folded ledger to everything the screen shows. `now` is injected so
    /// the two implementations — and the tests — agree on where the range ends.
    public static func summarize(_ ledger: Ledger, now: Double, days: Double,
                                 steps: Int, binCount: Int, z: Double) -> Summary {
        let start = now - days * 86_400
        // The token counters are cumulative, so what the range spent is the rise
        // since the last reading taken BEFORE it opened. Starting from the first
        // reading INSIDE it drops everything spent between those two — a whole
        // sample interval, which on a bursty day is a sixth of what a 1-day range
        // is being asked about.
        let inside = ledger.samples.indices.filter {
            ledger.samples[$0].at >= start && ledger.samples[$0].at <= now
        }
        let samples = inside.isEmpty
            ? []
            : Array(ledger.samples[max(0, inside[0] - 1)...inside[inside.count - 1]])

        // What a rate-limit window is worth in tokens is a property of the ACCOUNT,
        // not of the lookback the reader happens to have selected — so it is priced
        // from every sample in the ledger. That also means flipping to 7d doesn't
        // blank the percentages on a machine whose quota readings only began last
        // week, and that a short range borrows the precision of a long history.
        let sessionLimit = calibrate(ledger.samples, session: true)
        let weekLimit = calibrate(ledger.samples, session: false)

        // A task belongs to the range by when it STARTED — that is the instant its
        // tokens were spent, and it keeps a task from moving between ranges as its
        // agent runs.
        let inRange = ledger.tasks.filter { t in
            guard let s = t.startedAt else { return false }
            return s >= start && s <= now
        }
        let local = inRange.filter { !$0.remote }
        let runs = local.compactMap(\.runSecs)
        let waits = inRange.compactMap(\.waitSecs)

        let priced = local.compactMap(\.tokens).filter { $0 > 0 }
        var pct: [Double] = []
        if let limit = sessionLimit, limit > 0 {
            pct = priced.map { 100 * $0 / limit }
        }
        var weekMean = 0.0
        if let limit = weekLimit, limit > 0, !priced.isEmpty {
            weekMean = priced.reduce(0) { $0 + 100 * $1 / limit } / Double(priced.count)
        }

        let series = pendingSeries(ledger.tasks, now: now, days: days, steps: steps)
        let quota = quotaSeries(ledger.samples, now: now, days: days)
        let (repo, other) = tokenSplit(samples)
        let total = repo + other

        return Summary(
            sessionLimitTokens: sessionLimit,
            weekLimitTokens: weekLimit,
            perTask: distribution(pct, binCount: binCount, z: z),
            perTaskWeekMean: weekMean,
            perTaskTokensMean: priced.isEmpty
                ? 0 : priced.reduce(0, +) / Double(priced.count),
            avgRunSecs: runs.isEmpty ? 0 : runs.reduce(0, +) / Double(runs.count),
            avgWaitSecs: waits.isEmpty ? 0 : waits.reduce(0, +) / Double(waits.count),
            runSamples: runs.count,
            waitSamples: waits.count,
            quota: quota,
            // The LAST reading that actually carried a value, not the last sample:
            // a probe that has been down for an hour must not blank a figure it
            // measured perfectly well an hour ago.
            sessionLeftPct: quota.last(where: { $0.sessionPct != nil })?.sessionPct,
            weekLeftPct: quota.last(where: { $0.weekPct != nil })?.weekPct,
            pending: series,
            pendingReviewsNow: series.last?.reviews ?? 0,
            pendingConflictsNow: series.last?.conflicts ?? 0,
            peakReviews: series.map(\.reviews).max() ?? 0,
            peakConflicts: series.map(\.conflicts).max() ?? 0,
            repoTokens: repo,
            otherTokens: other,
            repoSharePct: total > 0 ? 100 * repo / total : 0,
            queuedCount: ledger.tasks.filter {
                guard let q = $0.queuedAt else { return false }
                return q >= start && q <= now
            }.count,
            startedCount: inRange.count,
            doneCount: local.filter { $0.doneAt != nil }.count,
            remoteCount: inRange.filter(\.remote).count,
            unattributedCount: local.filter { $0.doneAt != nil && ($0.tokens ?? 0) <= 0 }.count,
            firstSampleAt: ledger.samples.first?.at,
            lastSampleAt: ledger.samples.last?.at)
    }

    // MARK: - Formatting shared by both screens

    /// "4m 20s" / "1h 05m" / "—" for an empty sample. The screens print durations in
    /// exactly one place each, and this is it, so the two can't disagree about
    /// whether 90 minutes reads "1h 30m" or "90m".
    public static func duration(_ secs: Double, samples: Int = 1) -> String {
        guard samples > 0, secs.isFinite, secs > 0 else { return "—" }
        let total = Int(secs.rounded())
        if total < 60 { return "\(total)s" }
        if total < 3600 { return "\(total / 60)m \(String(format: "%02d", total % 60))s" }
        return "\(total / 3600)h \(String(format: "%02d", (total % 3600) / 60))m"
    }

    /// A percentage at the precision it deserves: sub-1% figures keep two decimals
    /// (an auto-review really can cost 0.35% of a window), everything else one.
    public static func percent(_ value: Double) -> String {
        guard value.isFinite else { return "—" }
        if value > 0, value < 1 { return String(format: "%.2f%%", value) }
        return String(format: "%.1f%%", value)
    }

    /// "1.2M" / "834k" / "512" — token counts, which run to eight figures.
    public static func tokens(_ value: Double) -> String {
        guard value.isFinite, value > 0 else { return "0" }
        if value >= 1_000_000 { return String(format: "%.1fM", value / 1_000_000) }
        if value >= 1_000 { return String(format: "%.0fk", value / 1_000) }
        return String(Int(value.rounded()))
    }
}
