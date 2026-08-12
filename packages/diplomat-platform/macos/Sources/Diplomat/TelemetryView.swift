import SwiftUI
import DiplomatCore

/// The Telemetry screen — what the monitors cost and what they still owe.
///
/// The macOS face of the ledger (`TelemetryLog`), and one of the panel's four
/// screens: Actions · Mesh · **Telemetry** · Settings. It reads
/// `~/.diplomat/pr-monitor/telemetry.jsonl`, folds it through the shared arithmetic
/// (`DiplomatCore.Telemetry`), and draws eight figures:
///
/// * what share of the 5-hour rate-limit window one auto-task consumes, on average;
/// * how that share is distributed, as a histogram with a fitted normal and a
///   confidence interval on the mean;
/// * what the probe measured to be left of each rate-limit window, over the lookback;
/// * how many auto-reviews were owed but unstarted, over the lookback;
/// * the same for auto-fixes;
/// * mean time from an agent starting to its exit;
/// * mean time from the monitor first seeing the work to an agent taking it;
/// * how much of this machine's Claude spend went on this repo rather than
///   everything else.
///
/// Read-only: the one control is the lookback, and flipping it recomputes from the
/// same fold. The Linux twin is `telemetryview.py`; the numbers both draw come from
/// the shared model in `assets/telemetry.json` and the shared math, so the two can
/// only differ in how they look.
struct TelemetryView: View {
    @EnvironmentObject var store: Store
    @Binding var isPresented: Bool

    /// The lookback in days. Seeded from the shared model so both platforms open on
    /// the same range.
    @State private var days: Double

    init(isPresented: Binding<Bool>) {
        self._isPresented = isPresented
        self._days = State(initialValue: Double((try? CoreAssets.telemetry())?.defaultRangeDays ?? 14))
    }

    private var model: CoreAssets.TelemetryModel? { try? CoreAssets.telemetry() }

    // MARK: - Shared model lookups

    private func metric(_ id: String) -> CoreAssets.TelemetryModel.Metric? { model?.metric(id) }
    private func tint(_ id: String) -> Color {
        Color(hex: metric(id)?.colorHex ?? "") ?? .gray
    }
    private func symbol(_ id: String) -> String { metric(id)?.sfSymbol ?? "circle" }
    private func title(_ id: String) -> String { metric(id)?.title ?? id }
    private func blurb(_ id: String) -> String { metric(id)?.blurb ?? "" }

    // MARK: - Body

    var body: some View {
        // Fold + summarize once per render: `TelemetryLog.load` caches until the file
        // changes, so a range flip re-does the arithmetic and not the parse.
        let now = Date().timeIntervalSince1970
        let summary = Telemetry.summarize(
            store.telemetryLedger, now: now, days: days,
            steps: model?.series.steps ?? 56, binCount: model?.series.bins ?? 12,
            z: model?.confidence.z ?? 1.96)
        let hasData = !store.telemetryLedger.tasks.isEmpty
            || !store.telemetryLedger.samples.isEmpty

        return VStack(alignment: .leading, spacing: 8) {
            header
            if hasData {
                HStack(alignment: .top, spacing: 12) {
                    VStack(alignment: .leading, spacing: 10) {
                        costCard(summary)
                        quotaCard(summary, now: now)
                        tokensCard(summary)
                    }
                    .frame(maxWidth: .infinity, alignment: .topLeading)
                    VStack(alignment: .leading, spacing: 10) {
                        pendingCard(summary)
                        timingCard(summary)
                    }
                    .frame(maxWidth: .infinity, alignment: .topLeading)
                }
                coverage(summary)
            } else {
                emptyState
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .task { store.refreshTelemetry() }
    }

    // MARK: - Header

    private var header: some View {
        HStack(spacing: 6) {
            Image(systemName: "chart.bar.xaxis").foregroundStyle(.secondary)
            Text("Telemetry").font(.subheadline.bold())
            Spacer()
            ForEach(model?.ranges ?? [], id: \.days) { range in
                rangeButton(range)
            }
            Button { withAnimation(.easeInOut(duration: 0.15)) { isPresented = false } } label: {
                Text("Done").bold()
            }
            .buttonStyle(.borderless)
            .keyboardShortcut(.cancelAction)
        }
    }

    private func rangeButton(_ range: CoreAssets.TelemetryModel.Range) -> some View {
        let active = Int(days) == range.days
        return Button { days = Double(range.days) } label: {
            Text(range.title)
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(active ? Color.primary : Color.secondary)
                .padding(.horizontal, 7).padding(.vertical, 2)
                .background(Capsule().fill(active ? Color.gray.opacity(0.22) : .clear))
        }
        .buttonStyle(.plain)
    }

    private var emptyState: some View {
        VStack(spacing: 8) {
            Image(systemName: "chart.bar.xaxis").font(.system(size: 30))
                .foregroundStyle(.secondary)
            Text("Nothing recorded yet.").font(.subheadline.bold())
            Text("The monitors write to the telemetry ledger as they work: what they "
                 + "find owed, when an agent takes it, how long it runs and what it "
                 + "cost. Leave Diplomat running and this fills in.")
                .font(.caption).foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 28)
    }

    // MARK: - Cards

    /// A card's heading row: tinted symbol, title, the headline number on the right —
    /// the one shape all four cards share.
    private func cardHead(_ id: String, _ value: String) -> some View {
        HStack(spacing: 6) {
            Image(systemName: symbol(id)).font(.system(size: 10, weight: .bold))
                .foregroundStyle(tint(id))
            Text(title(id).uppercased()).font(.system(size: 9, weight: .bold))
                .foregroundStyle(.secondary).kerning(1)
            Spacer(minLength: 4)
            Text(value).font(.system(size: 17, weight: .bold).monospacedDigit())
                .foregroundStyle(tint(id))
        }
    }

    private func note(_ text: String) -> some View {
        Text(text).font(.system(size: 9)).foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
    }

    @ViewBuilder
    private func costCard(_ s: Telemetry.Summary) -> some View {
        let d = s.perTask
        let priced = s.sessionLimitTokens != nil && d.count > 0
        // Built outside the ViewBuilder: a chain of ternaries over concatenated
        // strings inside `Text(...)` blows past SwiftUI's type-check budget.
        let headline: String = {
            if priced { return Telemetry.percent(d.mean) }
            if d.count == 0 && s.perTaskTokensMean > 0 {
                return Telemetry.tokens(s.perTaskTokensMean)
            }
            return "—"
        }()
        let caption: String = {
            if priced {
                return "of the 5-hour window, per task · median "
                    + "\(Telemetry.percent(d.median)) · "
                    + "\(Telemetry.percent(s.perTaskWeekMean)) of the week"
            }
            if d.count == 0 && s.perTaskTokensMean > 0 {
                return "tokens per task. The share of the limit is Claude Code's "
                    + "own — it counts only tasks that ran on it, and needs two "
                    + "quota readings from the OAuth usage probe."
            }
            return "No finished auto-task in this range yet."
        }()
        VStack(alignment: .leading, spacing: 6) {
            cardHead("limitPerTask", headline)
            note(caption)
            if priced {
                SpreadChart(dist: d, tint: tint("limitSpread")).frame(height: 150)
                Text("\(model?.confidence.title ?? "95% CI") "
                     + "\(Telemetry.percent(d.ciLow)) – \(Telemetry.percent(d.ciHigh))"
                     + "  ·  sd \(Telemetry.percent(d.sd))  ·  n=\(d.count)")
                    .font(.system(size: 9).monospaced()).foregroundStyle(.secondary)
                if d.count < (model?.minSample ?? 5) {
                    Text("Only \(d.count) finished task\(d.count == 1 ? "" : "s") — the "
                         + "curve is a guess until there are \(model?.minSample ?? 5).")
                        .font(.system(size: 9)).foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
                note(blurb("limitSpread"))
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardChrome()
    }

    /// The one card here that is measured rather than derived: what the OAuth usage
    /// probe says is left of each rate-limit window, drawn where it was sampled. It
    /// stays truthful on a machine whose window was never priced, which is exactly
    /// when `costCard` cannot show a percentage at all.
    @ViewBuilder
    private func quotaCard(_ s: Telemetry.Summary, now: Double) -> some View {
        let sessionText = s.sessionLeftPct.map { Telemetry.percent($0) } ?? "—"
        let weekText = s.weekLeftPct.map { Telemetry.percent($0) } ?? "—"
        let gaps = s.quota.filter { $0.sessionPct == nil }.count
        VStack(alignment: .leading, spacing: 6) {
            cardHead("quotaLeft", sessionText)
            note(blurb("quotaLeft"))
            if s.quota.isEmpty {
                note("No quota readings in this range. The probe uses the OAuth token "
                     + "Claude Code already holds — is it logged in on this machine?")
            } else {
                // Two readings are the fewest that make a line; one is drawn as the
                // headline alone rather than as an empty 120pt box.
                if s.quota.count > 1 {
                    QuotaChart(points: s.quota, days: days, now: now,
                               sessionTint: tint("quotaLeft"),
                               weekTint: tint("quotaWeek"))
                        .frame(height: 120)
                }
                legend([(tint("quotaLeft"), "5-hour \(sessionText)"),
                        (tint("quotaWeek"), "\(title("quotaWeek")) \(weekText)")])
                // A gap is the probe failing to answer, not the window emptying. The
                // chart breaks its line across one; saying how many keeps a blind
                // stretch from reading as a quiet one.
                if gaps > 0 {
                    note("\(gaps) reading\(gaps == 1 ? "" : "s") missing of "
                         + "\(s.quota.count) — the probe could not answer then, so the "
                         + "line breaks rather than dropping to zero.")
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardChrome()
    }

    @ViewBuilder
    private func tokensCard(_ s: Telemetry.Summary) -> some View {
        let total = s.repoTokens + s.otherTokens
        VStack(alignment: .leading, spacing: 6) {
            cardHead("tokenShare", total > 0 ? Telemetry.percent(s.repoSharePct) : "—")
            note(blurb("tokenShare"))
            if total > 0 {
                SplitBar(left: s.repoTokens, right: s.otherTokens,
                         leftColor: tint("tokenShare"), rightColor: .gray)
                    .frame(height: 18)
                legend([(tint("tokenShare"), "this repo \(Telemetry.tokens(s.repoTokens))"),
                        (.gray, "everything else \(Telemetry.tokens(s.otherTokens))")])
            } else {
                note("No Claude turns recorded in this range.")
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardChrome()
    }

    private func pendingCard(_ s: Telemetry.Summary) -> some View {
        let found: String = "\(s.queuedCount) unit\(s.queuedCount == 1 ? "" : "s") of work "
            + "found in this range, \(s.startedCount) started"
            + (s.remoteCount > 0 ? ", \(s.remoteCount) on mesh peers" : "")
            + ". Fixes stack on top of reviews, which take a free slot first, so the "
            + "top edge is everything the pool owes. Work picked up between two points "
            + "on the chart never shows as a backlog — that is the chart working, not "
            + "a gap."
        return VStack(alignment: .leading, spacing: 6) {
            cardHead("pendingWork", "\(s.pendingReviewsNow) / \(s.pendingConflictsNow)")
            note("owed right now: \(title("pendingReviews").lowercased()) / "
                 + title("pendingFixes").lowercased())
            PendingChart(points: s.pending, days: days,
                         reviewTint: tint("pendingReviews"),
                         conflictTint: tint("pendingFixes"))
                .frame(height: 160)
            legend([(tint("pendingReviews"),
                     "\(title("pendingReviews").lowercased()) (peak \(s.peakReviews))"),
                    (tint("pendingFixes"),
                     "\(title("pendingFixes").lowercased()) (peak \(s.peakConflicts))")])
            note(found)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardChrome()
    }

    private func timingCard(_ s: Telemetry.Summary) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            cardHead("startLag",
                     Telemetry.duration(s.avgWaitSecs, samples: s.waitSamples))
            note(blurb("startLag"))
            cardHead("completeTime",
                     Telemetry.duration(s.avgRunSecs, samples: s.runSamples))
            note("\(blurb("completeTime"))  Measured over \(s.runSamples) finished and "
                 + "\(s.waitSamples) started task\(s.waitSamples == 1 ? "" : "s").")
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardChrome()
    }

    /// A chart key: one swatch-coloured label per series. One label carrying several
    /// markers can only be a single colour, which makes the key claim both series are
    /// the colour of the first — the one thing a key must not do.
    private func legend(_ entries: [(Color, String)]) -> some View {
        HStack(spacing: 12) {
            ForEach(Array(entries.enumerated()), id: \.offset) { _, entry in
                Text("◼ \(entry.1)").font(.system(size: 9).monospaced())
                    .foregroundStyle(entry.0)
            }
            Spacer(minLength: 0)
        }
    }

    private func coverage(_ s: Telemetry.Summary) -> some View {
        var parts: [String] = []
        if let first = s.firstSampleAt {
            parts.append("quota readings since \(dayLabel(first))")
        }
        if let limit = s.sessionLimitTokens {
            parts.append("5-hour window priced at ≈\(Telemetry.tokens(limit)) tokens, measured")
        }
        if s.unattributedCount > 0 {
            parts.append("\(s.unattributedCount) finished task"
                         + "\(s.unattributedCount == 1 ? "" : "s") could not be matched "
                         + "to a transcript, so they carry no cost")
        }
        return Text(parts.joined(separator: " · "))
            .font(.system(size: 9)).foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
    }
}

// MARK: - Charts

/// The bell curve: a histogram of per-task cost, the fitted normal over it, and the
/// confidence interval on the mean as a shaded band.
///
/// The band is deliberately drawn *behind* the bars and the mean as a solid rule, so
/// the eye reads "the average is here, and this is how well we know it" rather than
/// mistaking the interval for the spread of the tasks themselves — which is the
/// histogram, and is much wider.
private struct SpreadChart: View {
    let dist: Telemetry.Distribution
    let tint: Color

    var body: some View {
        Canvas { ctx, size in
            guard dist.count > 0, let last = dist.bins.last else { return }
            let padL: CGFloat = 4, padR: CGFloat = 4, padT: CGFloat = 8, padB: CGFloat = 16
            let w = size.width - padL - padR
            let h = size.height - padT - padB
            let hi = last.upper > 0 ? last.upper : 1
            // The curve's peak can exceed the tallest bar (a tight distribution
            // sampled into wide bins), so both share one scale or the fit would be
            // clipped where it matters most.
            let top = max(Double(dist.bins.map(\.count).max() ?? 1),
                          dist.curve.max() ?? 0, 1)

            func xOf(_ value: Double) -> CGFloat {
                padL + w * CGFloat(min(1, max(0, value / hi)))
            }
            func yOf(_ count: Double) -> CGFloat {
                padT + h * CGFloat(1 - min(1, count / top))
            }

            // Confidence band on the mean, behind everything else so it reads as
            // context for the mean rule rather than as another series.
            if dist.ciHigh > dist.ciLow {
                let lo = xOf(dist.ciLow), hiX = xOf(dist.ciHigh)
                ctx.fill(Path(CGRect(x: lo, y: padT, width: max(1, hiX - lo), height: h)),
                         with: .color(tint.opacity(0.16)))
            }

            for bin in dist.bins where bin.count > 0 {
                let x0 = xOf(bin.lower), x1 = xOf(bin.upper), y = yOf(Double(bin.count))
                let rect = CGRect(x: x0 + 0.8, y: y, width: max(1, x1 - x0 - 1.6),
                                  height: padT + h - y)
                ctx.fill(Path(roundedRect: rect, cornerRadius: 2),
                         with: .color(tint.opacity(0.55)))
            }

            if dist.curve.count > 1 {
                var path = Path()
                for (i, value) in dist.curve.enumerated() {
                    let px = padL + w * CGFloat(i) / CGFloat(dist.curve.count - 1)
                    let py = yOf(value)
                    if i == 0 { path.move(to: CGPoint(x: px, y: py)) }
                    else { path.addLine(to: CGPoint(x: px, y: py)) }
                }
                ctx.stroke(path, with: .color(tint), lineWidth: 1.8)
            }

            // The mean, as a full-height rule.
            var rule = Path()
            rule.move(to: CGPoint(x: xOf(dist.mean), y: padT))
            rule.addLine(to: CGPoint(x: xOf(dist.mean), y: padT + h))
            ctx.stroke(rule, with: .color(.white), style: StrokeStyle(lineWidth: 1.2,
                                                                      dash: [3, 3]))

            // Axis: 0 on the left, the largest observation on the right.
            ctx.draw(axisText("0%"), at: CGPoint(x: padL, y: padT + h + 8), anchor: .leading)
            ctx.draw(axisText(Telemetry.percent(dist.max)),
                     at: CGPoint(x: padL + w, y: padT + h + 8), anchor: .trailing)
        }
    }
}

/// Owed-but-unstarted work over the lookback: reviews and conflict fixes stacked into
/// one area on a count axis with day gridlines.
///
/// Stacked rather than overlaid because the two kinds of work queue for the same
/// executors — the top edge is the whole backlog those executors owe, which is the
/// number that decides whether anything waits. Reviews are the lower band: they outrank
/// conflict fixes for a free slot (`AgentTaskQueue.band`), so the band above is exactly
/// the work waiting behind the band below.
private struct PendingChart: View {
    let points: [Telemetry.PendingPoint]
    let days: Double
    let reviewTint: Color
    let conflictTint: Color

    var body: some View {
        Canvas { ctx, size in
            guard points.count > 1, let first = points.first, let last = points.last
            else { return }
            let padL: CGFloat = 4, padR: CGFloat = 4, padT: CGFloat = 8, padB: CGFloat = 16
            let w = size.width - padL - padR
            let h = size.height - padT - padB
            let peak = points.map { $0.reviews + $0.conflicts }.max() ?? 0
            let top = max(1, peak)

            func xOf(_ i: Int) -> CGFloat {
                padL + w * CGFloat(i) / CGFloat(points.count - 1)
            }
            func yOf(_ count: Int) -> CGFloat {
                padT + h * CGFloat(1 - Double(count) / Double(top))
            }

            // Day gridlines, so a fortnight of backlog reads as a fortnight.
            let span = last.at - first.at
            if span > 0 {
                let step = max(1.0, (days / 7).rounded())
                var t = last.at
                while t > first.at {
                    let gx = padL + w * CGFloat((t - first.at) / span)
                    var grid = Path()
                    grid.move(to: CGPoint(x: gx, y: padT))
                    grid.addLine(to: CGPoint(x: gx, y: padT + h))
                    ctx.stroke(grid, with: .color(.white.opacity(0.07)), lineWidth: 1)
                    t -= step * 86_400
                }
            }

            var base = [Int](repeating: 0, count: points.count)
            for (values, color) in [(points.map(\.reviews), reviewTint),
                                    (points.map(\.conflicts), conflictTint)] {
                let stacked = zip(base, values).map { $0 + $1 }
                if values.contains(where: { $0 > 0 }) {
                    // The band between the running total below it and its own top —
                    // not a shape from the axis up, which would bury the band under it
                    // at any opacity.
                    var fill = Path()
                    fill.move(to: CGPoint(x: xOf(0), y: yOf(base[0])))
                    for (i, value) in stacked.enumerated() {
                        fill.addLine(to: CGPoint(x: xOf(i), y: yOf(value)))
                    }
                    for i in stride(from: base.count - 1, through: 0, by: -1) {
                        fill.addLine(to: CGPoint(x: xOf(i), y: yOf(base[i])))
                    }
                    fill.closeSubpath()
                    ctx.fill(fill, with: .color(color.opacity(0.22)))

                    var line = Path()
                    for (i, value) in stacked.enumerated() {
                        let p = CGPoint(x: xOf(i), y: yOf(value))
                        if i == 0 { line.move(to: p) } else { line.addLine(to: p) }
                    }
                    ctx.stroke(line, with: .color(color), lineWidth: 1.8)
                }
                base = stacked
            }

            ctx.draw(axisText(dayLabel(first.at)),
                     at: CGPoint(x: padL, y: padT + h + 8), anchor: .leading)
            ctx.draw(axisText("now"),
                     at: CGPoint(x: padL + w, y: padT + h + 8), anchor: .trailing)
            // Peak, on the count axis, so the height means something without a full
            // y-axis eating the width. It is the peak of the stack — the most ever owed
            // at one moment — which the per-series peaks in the key need not add up to.
            // A range that never owed anything says nothing rather than reporting the
            // floor the axis is held at.
            if peak > 0 {
                ctx.draw(axisText("peak \(peak) owed"),
                         at: CGPoint(x: padL + 2, y: padT + 4), anchor: .leading)
            }
        }
    }
}

/// Both rate-limit windows over the lookback, on a fixed 0-100% axis.
///
/// Nothing here is derived: these are the readings the OAuth usage probe returned,
/// drawn where they were taken. The axis is pinned to 0-100 rather than scaled to the
/// data, because "we never dropped below 60%" is the answer the chart exists to give,
/// and an auto-scaled one would show that week and a week of exhaustion as the same
/// picture.
///
/// The 5-hour window is drawn as a fill (it saws — it refills on its own cycle, so the
/// shape matters more than any one value) and the 7-day as a line over it.
private struct QuotaChart: View {
    let points: [Telemetry.QuotaPoint]
    /// The lookback the axis spans, and the instant it ends at. The axis is the
    /// whole range rather than the span of the readings, so it lines up with the
    /// owed-work chart beside it and a probe that stopped answering three days ago
    /// leaves visible empty axis instead of a line that appears to reach `now`.
    let days: Double
    let now: Double
    let sessionTint: Color
    let weekTint: Color

    /// The readings split into unbroken runs, cut at every gap. A missing reading is
    /// not a zero — it is a probe that could not answer — so the line has to stop and
    /// restart rather than dive to the floor and back, which would read as an
    /// exhausted window that recovered.
    private func runs(_ value: (Telemetry.QuotaPoint) -> Double?) -> [[(Double, Double)]] {
        var out: [[(Double, Double)]] = []
        var current: [(Double, Double)] = []
        for p in points {
            guard let v = value(p) else {
                if current.count > 1 { out.append(current) }
                current = []
                continue
            }
            current.append((p.at, v))
        }
        if current.count > 1 { out.append(current) }
        return out
    }

    var body: some View {
        Canvas { ctx, size in
            let span = days * 86_400
            guard points.count > 1, span > 0 else { return }
            let start = now - span
            let padL: CGFloat = 4, padR: CGFloat = 4, padT: CGFloat = 8, padB: CGFloat = 16
            let w = size.width - padL - padR
            let h = size.height - padT - padB

            func xOf(_ at: Double) -> CGFloat { padL + w * CGFloat((at - start) / span) }
            func yOf(_ pct: Double) -> CGFloat {
                padT + h * CGFloat(1 - min(100, max(0, pct)) / 100)
            }

            // The half-way rule, so a glance can place a run against "half spent".
            var half = Path()
            half.move(to: CGPoint(x: padL, y: yOf(50)))
            half.addLine(to: CGPoint(x: padL + w, y: yOf(50)))
            ctx.stroke(half, with: .color(.white.opacity(0.07)), lineWidth: 1)

            for run in runs({ $0.sessionPct }) {
                var fill = Path()
                fill.move(to: CGPoint(x: xOf(run[0].0), y: padT + h))
                for (at, pct) in run { fill.addLine(to: CGPoint(x: xOf(at), y: yOf(pct))) }
                fill.addLine(to: CGPoint(x: xOf(run[run.count - 1].0), y: padT + h))
                fill.closeSubpath()
                ctx.fill(fill, with: .color(sessionTint.opacity(0.30)))
            }

            for run in runs({ $0.weekPct }) {
                var line = Path()
                for (i, point) in run.enumerated() {
                    let p = CGPoint(x: xOf(point.0), y: yOf(point.1))
                    if i == 0 { line.move(to: p) } else { line.addLine(to: p) }
                }
                ctx.stroke(line, with: .color(weekTint), lineWidth: 1.8)
            }

            ctx.draw(axisText("100%"), at: CGPoint(x: padL, y: padT + 4), anchor: .leading)
            ctx.draw(axisText(dayLabel(start)),
                     at: CGPoint(x: padL, y: padT + h + 8), anchor: .leading)
            ctx.draw(axisText("now"),
                     at: CGPoint(x: padL + w, y: padT + h + 8), anchor: .trailing)
        }
    }
}

/// A single horizontal bar split between two quantities — the Diplomat share of this
/// machine's tokens against everything else.
private struct SplitBar: View {
    let left: Double
    let right: Double
    let leftColor: Color
    let rightColor: Color

    var body: some View {
        GeometryReader { geo in
            let total = max(0, left) + max(0, right)
            let split = total > 0 ? geo.size.width * max(0, left) / total : 0
            HStack(spacing: 0) {
                Rectangle().fill(leftColor).frame(width: split)
                Rectangle().fill(rightColor.opacity(0.45))
            }
            .clipShape(RoundedRectangle(cornerRadius: 5))
        }
    }
}

/// Chart axis labels, one styling for every tick on both charts. `.foregroundColor`
/// (not `.foregroundStyle`) so this stays a `Text`, which is what
/// `GraphicsContext.draw(_:at:)` takes.
private func axisText(_ s: String) -> Text {
    Text(s).font(.system(size: 8)).foregroundColor(Color(white: 0.55))
}

/// Month names, spelled out rather than left to a `DateFormatter` — that follows the
/// process locale, so on a machine set to anything but English the chart axis would
/// come out in a different language from every other word on the screen.
private let months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

private func dayLabel(_ epoch: Double) -> String {
    let date = Date(timeIntervalSince1970: epoch)
    let parts = Calendar.current.dateComponents([.day, .month], from: date)
    guard let day = parts.day, let month = parts.month, (1...12).contains(month) else {
        return ""
    }
    return "\(day) \(months[month - 1])"
}
