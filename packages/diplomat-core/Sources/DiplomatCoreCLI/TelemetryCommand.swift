import DiplomatCore
import Foundation

/// `diplomat-core telemetry` — fold a ledger fixture and print the whole Telemetry
/// screen's arithmetic as JSON.
///
/// Same reason `tool-data` exists: `Telemetry` (here) and `diplomat_app/telemetry.py`
/// (the Linux applet) are two implementations of one calculation, and neither can
/// delegate to the other — the screen recomputes on every range flip and on every
/// poll, so a subprocess per repaint is not an option. A drift in either would be
/// invisible, and invisible in the worst way: both screens keep rendering, they just
/// quietly disagree about what a task costs. `test_telemetry_parity.py` drives both
/// over one fixture and diffs this output.
///
/// Input:
/// ```
/// { "now": 1784000000.0, "days": 14, "steps": 56, "bins": 12, "z": 1.96,
///   "lines": ["{\"at\": …, \"ev\": \"queued\", …}", …] }
/// ```
/// Output: the folded tasks, then every figure the screen shows — including the
/// FORMATTED strings, so the duration/percent/token spellings are pinned too.
/// Floats are rounded to `places` decimals on both sides: the two run the same
/// arithmetic in different languages, and comparing raw doubles would fail on the
/// last bit of an `exp` without anything being wrong.
enum TelemetryCommand {
    /// Decimal places every emitted float is rounded to. Six is far finer than
    /// anything rendered (the screen shows one or two) and far coarser than the
    /// double rounding the two runtimes can disagree about.
    static let places = 6.0

    static func run(_ obj: [String: Any]) {
        let now = (obj["now"] as? NSNumber)?.doubleValue ?? 0
        let days = (obj["days"] as? NSNumber)?.doubleValue ?? 14
        let steps = (obj["steps"] as? NSNumber)?.intValue ?? 56
        let bins = (obj["bins"] as? NSNumber)?.intValue ?? 12
        let z = (obj["z"] as? NSNumber)?.doubleValue ?? 1.96
        let lines = obj["lines"] as? [String] ?? []

        let ledger = Telemetry.fold(lines: lines)
        let s = Telemetry.summarize(ledger, now: now, days: days, steps: steps,
                                    binCount: bins, z: z)

        let out: [String: Any] = [
            "tasks": ledger.tasks.map { t -> [String: Any] in
                [
                    "key": t.key, "duty": t.duty, "pr": t.pr,
                    "queuedAt": opt(t.queuedAt), "startedAt": opt(t.startedAt),
                    "doneAt": opt(t.doneAt), "clearedAt": opt(t.clearedAt),
                    "remote": t.remote, "tokens": opt(t.tokens),
                    "runSecs": opt(t.runSecs), "waitSecs": opt(t.waitSecs),
                ]
            },
            "sampleCount": ledger.samples.count,
            "sessionLimitTokens": opt(s.sessionLimitTokens),
            "weekLimitTokens": opt(s.weekLimitTokens),
            "perTask": [
                "count": s.perTask.count,
                "mean": r(s.perTask.mean), "sd": r(s.perTask.sd), "stderr": r(s.perTask.stderr),
                "ciLow": r(s.perTask.ciLow), "ciHigh": r(s.perTask.ciHigh),
                "min": r(s.perTask.min), "max": r(s.perTask.max), "median": r(s.perTask.median),
                "bins": s.perTask.bins.map { ["lower": r($0.lower), "upper": r($0.upper),
                                              "count": $0.count] },
                "curve": s.perTask.curve.map { r($0) },
            ],
            "perTaskWeekMean": r(s.perTaskWeekMean),
            "perTaskTokensMean": r(s.perTaskTokensMean),
            "avgRunSecs": r(s.avgRunSecs), "avgWaitSecs": r(s.avgWaitSecs),
            "runSamples": s.runSamples, "waitSamples": s.waitSamples,
            "quota": s.quota.map { ["at": r($0.at), "sessionPct": opt($0.sessionPct),
                                    "weekPct": opt($0.weekPct)] },
            "sessionLeftPct": opt(s.sessionLeftPct),
            "weekLeftPct": opt(s.weekLeftPct),
            "pending": s.pending.map { ["at": r($0.at), "reviews": $0.reviews,
                                        "conflicts": $0.conflicts] },
            "pendingReviewsNow": s.pendingReviewsNow,
            "pendingConflictsNow": s.pendingConflictsNow,
            "peakReviews": s.peakReviews, "peakConflicts": s.peakConflicts,
            "repoTokens": r(s.repoTokens), "otherTokens": r(s.otherTokens),
            "repoSharePct": r(s.repoSharePct),
            "queuedCount": s.queuedCount, "startedCount": s.startedCount,
            "doneCount": s.doneCount, "remoteCount": s.remoteCount,
            "unattributedCount": s.unattributedCount,
            "firstSampleAt": opt(s.firstSampleAt), "lastSampleAt": opt(s.lastSampleAt),
            "format": [
                "run": Telemetry.duration(s.avgRunSecs, samples: s.runSamples),
                "wait": Telemetry.duration(s.avgWaitSecs, samples: s.waitSamples),
                "mean": Telemetry.percent(s.perTask.mean),
                "ciLow": Telemetry.percent(s.perTask.ciLow),
                "ciHigh": Telemetry.percent(s.perTask.ciHigh),
                "weekMean": Telemetry.percent(s.perTaskWeekMean),
                "share": Telemetry.percent(s.repoSharePct),
                "perTaskTokens": Telemetry.tokens(s.perTaskTokensMean),
                "repoTokens": Telemetry.tokens(s.repoTokens),
                "otherTokens": Telemetry.tokens(s.otherTokens),
            ],
        ]
        guard let data = try? JSONSerialization.data(
            withJSONObject: out, options: [.sortedKeys, .prettyPrinted]) else {
            die("could not serialise telemetry", 1)
        }
        FileHandle.standardOutput.write(data)
    }

    private static func r(_ v: Double) -> Double {
        guard v.isFinite else { return 0 }
        let scale = pow(10.0, places)
        return (v * scale).rounded() / scale
    }

    /// JSON has no optional; `null` is what both sides compare against.
    private static func opt(_ v: Double?) -> Any {
        guard let v else { return NSNull() }
        return r(v)
    }
}
