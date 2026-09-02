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
/// - `done`    — the agent exited, carrying the tokens its own transcript accounts
///               for and, for a runner the provider bills in money, what it charged
///               and the model it charged for. Timed from its completion sentinel, or
///               from that transcript's last turn where the run left no sentinel the
///               applet can read.
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
/// against the Python twin in `diplomat_runtime/telemetry.py`. Keep the two in step.
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
        /// Which CLI ran it (an `AgentRunner.rawValue`), or empty for a task recorded
        /// before there was a choice of one.
        public var runner: String
        /// What the provider charged for it, for a runner billed in money, and the
        /// model whose rates it was charged at — in the ledger's own spelling of that
        /// id, which is the runner's, so tasks group by it without anything having to
        /// translate between how two tools write the same model.
        public var usd: Double?
        public var model: String

        /// Whether the tokens it spent came out of the account the quota probe reads.
        ///
        /// Only Claude Code's do; every other runner is billed by whichever provider it
        /// is logged into. So a foreign task is worth a token count of its own and is
        /// worth nothing at all as a share of a five-hour window it never drew on — and
        /// left in, it would drag that share down for every task beside it.
        public var anthropic: Bool {
            runner.isEmpty || runner == AgentRunner.claude.rawValue
        }

        /// Seconds from an agent starting to its exit.
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
                                          remote: false, tokens: nil, runner: "",
                                          usd: nil, model: "")
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
                let agentRunner = obj["runner"] as? String ?? ""
                let ranOn = obj["model"] as? String ?? ""
                if task.doneAt == nil {
                    task.doneAt = at
                    task.tokens = number(obj["tokens"])
                    task.runner = agentRunner
                    task.usd = number(obj["usd"])
                    task.model = ranOn
                } else {
                    // A retry appends a SECOND completion under the same key. The
                    // instants stay first-wins, but the price is taken from
                    // whichever attempt could be attributed at all — otherwise a
                    // task whose first attempt was never tied back to a transcript
                    // stays unpriced however many times it is re-run. Its runner
                    // travels with it: that is what says whether those tokens came
                    // out of the Anthropic window.
                    //
                    // The two prices fill independently, because a runner can report
                    // one and not the other — a session row written before the
                    // provider returned a cost carries tokens and no money.
                    if !((task.tokens ?? 0) > 0), let later = number(obj["tokens"]),
                       later > 0 {
                        task.tokens = later
                        task.runner = agentRunner
                    }
                    if !((task.usd ?? 0) > 0), let later = number(obj["usd"]),
                       later > 0 {
                        task.usd = later
                        task.model = ranOn
                        if task.runner.isEmpty { task.runner = agentRunner }
                    }
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

    /// Each task as a percentage of one rate-limit window, or nothing at all while
    /// that window has no price. Empty rather than zeroed: a share of a window nobody
    /// has measured is a made-up number, and the screen says so instead of drawing it.
    static func shares(_ taskTokens: [Double], limit: Double?) -> [Double] {
        guard let limit, limit > 0 else { return [] }
        return taskTokens.map { 100 * $0 / limit }
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

    /// `span` widens the histogram's top edge past the largest observation, so two
    /// distributions drawn on one axis share their bin edges. Without it each scales
    /// to its own maximum, and the same task lands in a different bin on each.
    public static func distribution(_ values: [Double], binCount: Int, z: Double,
                                    span: Double? = nil) -> Distribution {
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
        let hi = Swift.max(span ?? 0, sorted.last ?? 0, 1e-9)
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

    // MARK: - Finished-work series

    /// One bar of the finished-work chart.
    public struct FinishedPoint: Equatable {
        /// When the bucket opens. It runs to the next point's `at`, and the last one
        /// to `now`.
        public let at: Double
        public let count: Int
    }

    /// How many tasks finished in each equal bucket of the lookback, oldest first.
    ///
    /// Bucketed by when a task ENDED, not when it started or was queued: a run that
    /// spanned two buckets is one delivery, and it happened at its exit. Work the mesh
    /// placed on a peer is counted like any other — it was delivered — even though its
    /// cost belongs to that peer and is kept out of every per-task figure.
    ///
    /// The buckets are laid backwards from `now` rather than forwards from the start
    /// of the range, so the newest bar covers a whole bucket like every other one.
    /// Laid forwards, that bar would hold whatever fraction of a bucket the range ends
    /// on and read as a slump that is only a short bar. Every shipped range divides
    /// exactly (`bucketHours` in the shared model), so nothing spills off the far end
    /// either.
    public static func finishedSeries(_ tasks: [Task], now: Double, days: Double,
                                      bucketHours: Double) -> [FinishedPoint] {
        guard days > 0, bucketHours > 0 else { return [] }
        let width = bucketHours * 3600
        let count = Int((days * 86_400 / width).rounded(.up))
        let start = now - Double(count) * width
        var counts = [Int](repeating: 0, count: count)
        for task in tasks {
            guard let done = task.doneAt, done >= start, done <= now else { continue }
            // A task that finished exactly at `now` lands one past the last edge.
            counts[Swift.min(Int((done - start) / width), count - 1)] += 1
        }
        return counts.enumerated().map {
            FinishedPoint(at: start + Double($0.offset) * width, count: $0.element)
        }
    }

    // MARK: - Rate-limit windows over time

    /// How long a quota reading stays an answer to "what is left right now" — the
    /// silence the chart draws through, and the age past which the headline reads
    /// "—". One bound for both, or a line that has broken ends up captioned with a
    /// figure.
    public static var quotaFreshSecs: Double {
        let model = try? CoreAssets.telemetry()
        return (model?.quotaFreshSamples ?? 4) * (model?.sampleIntervalSecs ?? 900)
    }

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

        /// What a task costs as a share of the 5-hour window, and the same tasks as
        /// a share of the 7-day one, both in percent. Two distributions rather than
        /// one and a ratio: each window is priced from its own quota readings
        /// (`calibrate`), so either can be measurable while the other is not, and the
        /// screen draws them on one axis — whichever hump sits further right is the
        /// ceiling that runs out first. They share bin edges, so a bin means the same
        /// slice of the axis on both.
        public let perTask: Distribution
        public let perTaskWeek: Distribution
        /// Mean RAW tokens per task. Independent of the quota probe, so it is what
        /// the screen shows while the window has no price yet — an unanchored
        /// number, but a measured one.
        public let perTaskTokensMean: Double

        /// What a task costs in DOLLARS, for a runner the provider bills in money.
        /// Built from one model's runs only (`perTaskUsdModel`), which is what makes
        /// the figure mean anything: rates differ by two orders of magnitude across
        /// models, so a distribution mixing them describes no task that ever ran.
        public let perTaskUsd: Distribution
        /// The model `perTaskUsd` is priced for — the most recent one to run, spelled
        /// as the runner that ran it spells it. Empty when nothing in range was
        /// billed in money.
        public let perTaskUsdModel: String

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

        /// Tasks that FINISHED inside the range, bucketed for the bar chart, with the
        /// total and the tallest bucket derived from those same bars — so the headline
        /// count and the chart under it can never be counting two different things.
        public let finished: [FinishedPoint]
        public let finishedCount: Int
        public let peakFinished: Int
        /// How wide one of those buckets is, carried out with them so the chart's
        /// caption names the same slice of time its bars were counted over.
        public let bucketHours: Double

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
                                 steps: Int, binCount: Int, z: Double,
                                 bucketHours: Double) -> Summary {
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

        let priced = local.filter { ($0.tokens ?? 0) > 0 }
        // Two lists, because the two figures ask different questions: what a task cost
        // is a token count whoever billed it, while a share of a window is the
        // account's and only its own tasks may be measured against it.
        let charged = priced.filter(\.anthropic).compactMap(\.tokens)
        let pct = shares(charged, limit: sessionLimit)
        let pctWeek = shares(charged, limit: weekLimit)
        // One axis for both histograms, so the distance between the humps is readable
        // as what it is: how much more of a 5-hour window a task eats than of a week.
        let span = Swift.max(pct.max() ?? 0, pctWeek.max() ?? 0)

        // The dollar half of the same question. Restricted to the model that ran most
        // recently, because a switch of model is a switch of rates: keeping the older
        // model's runs in would price the next task against rates it will not be
        // charged at, and dropping them narrows the sample until the new model has a
        // history of its own — which holds work to the standing reserve meanwhile, the
        // conservative direction of the two. Ties go to the first seen, as they do in
        // the Python twin.
        let billed = local.filter { ($0.usd ?? 0) > 0 }
        var latest: Task?
        for task in billed {
            guard let best = latest else { latest = task; continue }
            if (task.startedAt ?? 0) > (best.startedAt ?? 0) { latest = task }
        }
        let usdModel = latest?.model ?? ""
        let usd = billed.filter { $0.model == usdModel }.compactMap(\.usd)

        let series = pendingSeries(ledger.tasks, now: now, days: days, steps: steps)
        let finished = finishedSeries(ledger.tasks, now: now, days: days,
                                      bucketHours: bucketHours)
        let quota = quotaSeries(ledger.samples, now: now, days: days)
        let fresh = quotaFreshSecs
        let (repo, other) = tokenSplit(samples)
        let total = repo + other

        return Summary(
            sessionLimitTokens: sessionLimit,
            weekLimitTokens: weekLimit,
            perTask: distribution(pct, binCount: binCount, z: z, span: span),
            perTaskWeek: distribution(pctWeek, binCount: binCount, z: z, span: span),
            perTaskTokensMean: priced.isEmpty
                ? 0 : priced.compactMap(\.tokens).reduce(0, +) / Double(priced.count),
            perTaskUsd: distribution(usd, binCount: binCount, z: z),
            perTaskUsdModel: usdModel,
            avgRunSecs: runs.isEmpty ? 0 : runs.reduce(0, +) / Double(runs.count),
            avgWaitSecs: waits.isEmpty ? 0 : waits.reduce(0, +) / Double(waits.count),
            runSamples: runs.count,
            waitSamples: waits.count,
            quota: quota,
            // The last reading that CARRIED a value, not the last sample — but no
            // older than `quotaFreshSecs`. The card prints this as what is left now
            // and never says how old it is, so past that bound no number beats a
            // stale one.
            sessionLeftPct: quota.last(where: {
                $0.sessionPct != nil && now - $0.at <= fresh })?.sessionPct,
            weekLeftPct: quota.last(where: {
                $0.weekPct != nil && now - $0.at <= fresh })?.weekPct,
            pending: series,
            pendingReviewsNow: series.last?.reviews ?? 0,
            pendingConflictsNow: series.last?.conflicts ?? 0,
            peakReviews: series.map(\.reviews).max() ?? 0,
            peakConflicts: series.map(\.conflicts).max() ?? 0,
            finished: finished,
            finishedCount: finished.reduce(0) { $0 + $1.count },
            peakFinished: finished.map(\.count).max() ?? 0,
            bucketHours: bucketHours,
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

    /// "4h" — how wide one bar of the finished-work chart is, as both screens caption
    /// it. A bucket that is not a whole number of hours falls through to the shared
    /// duration spelling ("30m 00s") rather than truncating to "0h".
    public static func bucketLabel(_ hours: Double) -> String {
        if hours.isFinite, hours > 0, hours == hours.rounded(.down) {
            return "\(Int(hours))h"
        }
        return duration(hours * 3600)
    }

    /// A percentage at the precision it deserves: sub-1% figures keep two decimals
    /// (an auto-review really can cost 0.35% of a window), everything else one.
    public static func percent(_ value: Double) -> String {
        guard value.isFinite else { return "—" }
        if value > 0, value < 1 { return String(format: "%.2f%%", value) }
        return String(format: "%.1f%%", value)
    }

    /// "$12.40" / "$0.068" — dollar figures, which run from a fraction of a cent for
    /// one task to a three-figure balance. Sub-dollar amounts keep three places for
    /// the reason sub-1% figures keep two: that is the range a single task lands in,
    /// and rounding it to cents would print most of them as the same number.
    public static func money(_ value: Double) -> String {
        guard value.isFinite, value >= 0 else { return "—" }
        if value > 0, value < 1 { return String(format: "$%.3f", value) }
        return String(format: "$%.2f", value)
    }

    /// "1.2M" / "834k" / "512" — token counts, which run to eight figures.
    public static func tokens(_ value: Double) -> String {
        guard value.isFinite, value > 0 else { return "0" }
        if value >= 1_000_000 { return String(format: "%.1fM", value / 1_000_000) }
        if value >= 1_000 { return String(format: "%.0fk", value / 1_000) }
        return String(Int(value.rounded()))
    }
}
