import SwiftUI
import AppKit
import DiplomatCore

/// The Comms screen — what Diplomat saw in Slack and what it did about it.
///
/// One of the panel's screens (Actions · Comms · Mesh · Telemetry · Settings), swapped in
/// for the main body by the header's speech bubbles. It draws `store.commsItems`, the one
/// fold of Comms' own log (`CommsLog.fold`) that the Linux twin draws too: the pings on
/// the left, newest first, and on the right every one of them that put work in the queue,
/// with where that work has got to. Opening it marks every ping seen.
///
/// Read-only apart from links out: the switches are in Settings, and the queued work is
/// arranged, run or cancelled from the Agent-tasks list like any other ask.
struct CommsView: View {
    @EnvironmentObject var store: Store
    @Binding var isPresented: Bool

    /// The "seen" mark as it stood when the screen opened. Opening moves the Store's
    /// mark to now, and the highlight is against this one, so what was new on arrival
    /// stays drawn as new for the rest of the visit.
    @State private var openedSeenAt: Date?

    private static let maxPings = 40

    var body: some View {
        let items = store.commsItems
        let jobs = items.filter { $0.queueKey != nil }
        return VStack(alignment: .leading, spacing: 8) {
            header
            statusLine
            if showSetup {
                setupCard
            } else if let error = store.commsError {
                errorBanner(error)
            }
            if !items.isEmpty {
                HStack(alignment: .top, spacing: 12) {
                    pingsColumn(Array(items.prefix(CommsView.maxPings)))
                        .frame(width: PopoverRoot.columnWidth, alignment: .topLeading)
                    jobsColumn(jobs)
                        .frame(width: PopoverRoot.columnWidth, alignment: .topLeading)
                }
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .onAppear {
            if openedSeenAt == nil { openedSeenAt = store.commsSeenAt }
            store.markCommsSeen()
        }
        // A ping that lands while the screen is up has been seen too.
        .onChange(of: store.commsItems) { _ in store.markCommsSeen() }
    }

    /// The setup instructions stand in for the lists while there is no token, or while
    /// Comms is off and has never recorded anything.
    private var showSetup: Bool {
        store.commsTokenMissing || (!store.commsEnabled && store.commsItems.isEmpty)
    }

    // MARK: - Header and status

    private var header: some View {
        HStack(spacing: 6) {
            Image(systemName: "bubble.left.and.bubble.right.fill").foregroundStyle(.secondary)
            Text("Comms").font(.subheadline.bold())
            Spacer()
            Button { withAnimation(.easeInOut(duration: 0.15)) { isPresented = false } } label: {
                Text("Done").bold()
            }
            .buttonStyle(.borderless)
            .keyboardShortcut(.cancelAction)
        }
    }

    private var statusLine: some View {
        let text = statusText
        let tint: Color = !store.commsEnabled ? .secondary
            : (store.commsError == nil ? .green : .orange)
        return HStack(spacing: 6) {
            Circle().fill(tint).frame(width: 7, height: 7)
            Text(text).font(.system(size: 11)).foregroundStyle(.secondary)
            Spacer()
        }
    }

    private var statusText: String {
        guard store.commsEnabled else { return "Off: switch Comms on in Settings to watch Slack." }
        let secs = Int(Store.commsPollInterval)
        var parts: [String] = []
        if let me = store.commsIdentity {
            parts.append("Watching Slack as @\(me.handle) in workspace \(me.teamID)")
        } else {
            parts.append("Watching Slack")
        }
        parts.append("last poll \(Fmt.clock(store.commsLastPoll))")
        parts.append("every \(secs)s")
        return parts.joined(separator: " · ")
    }

    private func errorBanner(_ message: String) -> some View {
        HStack(alignment: .top, spacing: 6) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 10)).foregroundStyle(.orange)
            Text(message).font(.system(size: 11))
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .cardChrome(fill: Color.orange.opacity(0.14))
    }

    // MARK: - Setup

    private static let setupIntro = """
        Comms reads Slack with a user token (xoxp-…) of your own: the DMs you get, the \
        messages that @-mention you, and the ones carrying a keyword from Settings. Each \
        is triaged by one claude call with no tools, and an ask for a review, a fix, a \
        bump or an investigation goes into the queue like any other.
        """

    private static let setupSteps = """
        1. Create a Slack app for your workspace and give it the user token scope \
        search:read. Install it and copy the User OAuth Token.
        2. Store it in your login Keychain (it prompts for the token, so it never lands \
        in your shell history):
        """

    private static let setupAfter = """
        3. Switch Comms on in Settings and press Test. The token stays in the Keychain: \
        ~/.diplomat/config.json holds only the item's name.
        """

    private var setupCard: some View {
        let command = CommsToken.addCommand(item: AppConfig.slackTokenKeychainItem)
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: "key.fill").font(.system(size: 11)).foregroundStyle(.blue)
                Text("Connect Slack").font(.system(size: 12, weight: .semibold))
            }
            Text(CommsView.setupIntro).font(.system(size: 11)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Text(CommsView.setupSteps).font(.system(size: 11))
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 6) {
                Text(command).font(.system(size: 11, design: .monospaced))
                    .textSelection(.enabled)
                Spacer(minLength: 4)
                Button("Copy") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(command, forType: .string)
                }
                .controlSize(.small)
            }
            .cardChrome(fill: Color.gray.opacity(0.14), padding: 6)
            Text(CommsView.setupAfter).font(.system(size: 11))
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardChrome(fill: Color.blue.opacity(0.08), padding: 10)
    }

    // MARK: - Pings

    private func pingsColumn(_ items: [CommsItem]) -> some View {
        let since = openedSeenAt ?? store.commsSeenAt
        let unread = items.filter { $0.at > since }.count
        return VStack(alignment: .leading, spacing: 6) {
            columnTitle("PINGS", count: store.commsItems.count,
                        caption: unread > 0 ? "\(unread) new" : nil)
            ForEach(items, id: \.message.id) { item in
                PingRow(item: item, unread: item.at > since, me: store.commsIdentity)
            }
        }
    }

    // MARK: - Jobs

    private func jobsColumn(_ jobs: [CommsItem]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            columnTitle("JOBS", count: jobs.count, caption: nil)
            if jobs.isEmpty {
                Text("Nothing a ping asked for is queued yet.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                    .cardChrome()
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            ForEach(jobs, id: \.message.id) { item in
                JobRow(item: item, status: jobStatus(item))
            }
        }
    }

    /// Where the work a ping queued has got to. `started`/`finished` are what the log
    /// recorded; between them, and before them, the queue and the agent rows are asked,
    /// since a queued ask has no line in the log until it spawns.
    private func jobStatus(_ item: CommsItem) -> JobRow.Status {
        if item.finishedAt != nil { return .done }
        let key = item.queueKey ?? ""
        if store.startingTasks.contains(where: { $0.id == key }) { return .running }
        if item.startedAt != nil, !Store.commsNeverStarted(item) { return .running }
        if store.queuedTasks.contains(where: { $0.id == key }) { return .queued }
        return .notQueued
    }

    private func columnTitle(_ title: String, count: Int, caption: String?) -> some View {
        HStack(spacing: 5) {
            Text(title).font(.system(size: 9, weight: .bold))
                .foregroundStyle(.secondary).kerning(0.5)
            Text("\(count)").font(.system(size: 9).monospacedDigit())
                .foregroundStyle(.secondary)
                .padding(.horizontal, 5).padding(.vertical, 1)
                .background(Capsule().fill(Color.gray.opacity(0.15)))
            if let caption {
                Text(caption).font(.system(size: 9, weight: .bold)).foregroundStyle(.blue)
            }
            Spacer()
        }
    }
}

/// One ping: who, where, why it reached the operator, what they said, what triage made
/// of it and what was done.
private struct PingRow: View {
    let item: CommsItem
    let unread: Bool
    let me: CommsIdentity?

    var body: some View {
        let m = item.message
        let decision = decisionText
        let text = readableText
        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 5) {
                if unread {
                    Circle().fill(Color.blue).frame(width: 6, height: 6)
                }
                Text(m.sender).font(.system(size: 11, weight: .semibold))
                // A DM says so in its reason chip.
                if !m.isDM {
                    Text(m.place).font(.system(size: 10)).foregroundStyle(.secondary)
                }
                ForEach(reasonChips, id: \.self) { chip in
                    StatusPill(text: chip, tint: .blue)
                }
                Spacer(minLength: 4)
                Text(Fmt.clock(item.at)).font(.system(size: 9).monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            Text(text).font(.system(size: 11)).lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)
            HStack(alignment: .firstTextBaseline, spacing: 5) {
                StatusPill(text: actionWord, tint: actionTint)
                Text(decision).font(.system(size: 10)).foregroundStyle(.secondary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(item.outcome ?? "not acted on yet").font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 4)
                if let url = URL(string: m.permalink), !m.permalink.isEmpty {
                    Button("Open in Slack") { NSWorkspace.shared.open(url) }
                        .buttonStyle(.link).font(.system(size: 10))
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardChrome(fill: unread ? Color.blue.opacity(0.13) : Color.gray.opacity(0.07))
    }

    /// The text with Slack's mention markup for the operator, `<@U…>`, read as their
    /// handle.
    private var readableText: String {
        guard let me, !me.userID.isEmpty else { return item.message.text }
        return item.message.text.replacingOccurrences(of: "<@\(me.userID)>", with: "@\(me.handle)")
    }

    private var reasonChips: [String] {
        item.message.reasons.map { r -> String in
            switch r {
            case .dm:      return "DM"
            case .mention: return "@mention"
            case .keyword: return "keyword: \(item.message.keyword ?? "")"
            }
        }
    }

    private var actionWord: String {
        guard let d = item.decision else { return "triaging" }
        switch d.action {
        case .none:        return "none"
        case .bump:        return "bump #\(d.number)"
        case .review:      return "review #\(d.number)"
        case .fixIssue:    return "fix #\(d.number)"
        case .investigate: return "investigate"
        }
    }

    private var actionTint: Color {
        switch item.decision?.action {
        case .bump?:        return .orange
        case .review?:      return .purple
        case .fixIssue?:    return .green
        case .investigate?: return .teal
        case .none?, nil:   return .secondary
        }
    }

    /// The triage's summary, else its reason: the summary says what was asked, and a
    /// `none` usually has only the reason it is one.
    private var decisionText: String {
        guard let d = item.decision else { return "" }
        return d.summary.isEmpty ? d.reason : d.summary
    }
}

/// One piece of work a ping queued, and how far it has got.
private struct JobRow: View {
    enum Status {
        case queued, running, done, notQueued

        var word: String {
            switch self {
            case .queued:    return "queued"
            case .running:   return "running"
            case .done:      return "done"
            case .notQueued: return "not queued"
            }
        }

        var tint: Color {
            switch self {
            case .queued:    return .orange
            case .running:   return .blue
            case .done:      return .green
            case .notQueued: return .secondary
            }
        }
    }

    let item: CommsItem
    let status: Status

    var body: some View {
        let report = reportURL
        let times = timesText
        return VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(item.label ?? item.queueKey ?? "").font(.system(size: 11, weight: .semibold))
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 4)
                StatusPill(text: status.word, tint: status.tint)
            }
            HStack(spacing: 6) {
                Text(times).font(.system(size: 10).monospacedDigit()).foregroundStyle(.secondary)
                Spacer(minLength: 4)
                if let report {
                    Button("Report") { NSWorkspace.shared.open(report) }
                        .controlSize(.small)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardChrome()
    }

    private var timesText: String {
        var parts = ["asked \(Fmt.clock(item.at))"]
        if let s = item.startedAt { parts.append("started \(Fmt.clock(s))") }
        if let f = item.finishedAt { parts.append("finished \(Fmt.clock(f))") }
        return parts.joined(separator: " · ")
    }

    /// An investigation's report, once its agent has written it: `<reports>/<ts number>.md`,
    /// the path `InvestigateConfig` handed the agent.
    private var reportURL: URL? {
        guard item.queueKey?.hasPrefix(AgentTaskQueue.investigateAction + ":") == true,
              let n = CommsTS.number(item.message.ts) else { return nil }
        let url = CommsLog.reportsDir().appendingPathComponent("\(n).md")
        return FileManager.default.fileExists(atPath: url.path) ? url : nil
    }
}
