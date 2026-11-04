import SwiftUI
import AppKit
import DiplomatCore

/// The Comms screen: what Diplomat saw in Slack and what it did about it.
///
/// Draws `store.commsItems`, the same `CommsLog.fold` the Linux twin draws. Read-only
/// apart from links out: the switches are in Settings, and the queued work is managed
/// from the Agent-tasks list like any other ask.
struct CommsView: View {
    @EnvironmentObject var store: Store
    @Binding var isPresented: Bool

    /// The seen mark as of opening, which moves the Store's to now: highlighting against
    /// this keeps what was new on arrival drawn as new for the visit.
    @State private var openedSeenAt: Date?
    /// Pings drawn whole, by message id.
    @State private var expanded: Set<String>
    /// Whether the pings read before this visit are listed.
    @State private var earlierOpen: Bool

    private static let maxPings = 40

    /// Seeded only by the headless renders.
    init(isPresented: Binding<Bool>, seedExpanded: Set<String> = [],
         seedEarlierOpen: Bool = false) {
        _isPresented = isPresented
        _expanded = State(initialValue: seedExpanded)
        _earlierOpen = State(initialValue: seedEarlierOpen)
    }

    var body: some View {
        let items = store.commsItems
        let jobs = items.filter { $0.queueKey != nil }
        let senders = store.bannedAuthors.filter(\.isSlack)
        return VStack(alignment: .leading, spacing: 8) {
            header
            statusLine
            if let error = store.commsError {
                errorBanner(error)
            }
            if !senders.isEmpty { bannedStrip(senders) }
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
        .onChange(of: store.commsItems) { _ in store.markCommsSeen() }
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
            parts.append("Watching Slack as " + Store.commsWho(me))
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

    // MARK: - Banned senders

    private func bannedStrip(_ bans: [BannedAuthor]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 5) {
                Image(systemName: "hand.raised.fill").font(.system(size: 9, weight: .bold))
                    .foregroundStyle(.red)
                Text("BANNED SENDERS").font(.system(size: 9, weight: .bold))
                    .foregroundStyle(.red).kerning(0.5)
                Text("\(bans.count)").font(.system(size: 9).monospacedDigit())
                    .foregroundStyle(.red)
                    .padding(.horizontal, 5).padding(.vertical, 1)
                    .background(Capsule().fill(Color.red.opacity(0.15)))
                Text("prompt injection · their messages are not triaged · un-ban in the panel's Banned list")
                    .font(.system(size: 9)).foregroundStyle(.secondary).lineLimit(1)
                Spacer(minLength: 0)
            }
            ForEach(bans) { ban in
                BannedSenderRow(ban: ban, place: store.commsPlace(of: ban))
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardChrome(fill: Color.red.opacity(0.08), padding: 6)
    }

    // MARK: - Pings

    /// Pings new on this visit first, then the rest behind one "Earlier" line.
    private func pingsColumn(_ items: [CommsItem]) -> some View {
        let since = openedSeenAt ?? store.commsSeenAt
        let unread = items.filter { $0.at > since }
        let read = items.filter { $0.at <= since }
        let caption: String? = unread.isEmpty ? nil : "\(unread.count) new"
        return VStack(alignment: .leading, spacing: 3) {
            columnTitle("PINGS", count: store.commsItems.count, caption: caption)
            ForEach(unread, id: \.message.id) { item in
                pingRow(item, unread: true)
            }
            if !read.isEmpty {
                earlierLine(read.count)
                if earlierOpen {
                    ForEach(read, id: \.message.id) { item in
                        pingRow(item, unread: false)
                    }
                }
            }
        }
    }

    private func pingRow(_ item: CommsItem, unread: Bool) -> some View {
        let id = item.message.id
        return PingRow(item: item, unread: unread, me: store.commsIdentity,
                       expanded: expanded.contains(id)) {
            if expanded.contains(id) { expanded.remove(id) } else { expanded.insert(id) }
        }
    }

    private func earlierLine(_ count: Int) -> some View {
        let chevron = earlierOpen ? "chevron.down" : "chevron.right"
        return HStack(spacing: 5) {
            Image(systemName: chevron).font(.system(size: 8, weight: .bold))
                .frame(width: 8)
            Text("Earlier").font(.system(size: 10, weight: .semibold))
            Text("· \(count)").font(.system(size: 10).monospacedDigit())
            Spacer(minLength: 0)
        }
        .foregroundStyle(.secondary)
        .padding(.horizontal, 5).padding(.vertical, 3)
        .contentShape(Rectangle())
        .onTapGesture { earlierOpen.toggle() }
        .accessibilityAddTraits(.isButton)
    }

    // MARK: - Jobs

    private func jobsColumn(_ jobs: [CommsItem]) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            columnTitle("JOBS", count: jobs.count, caption: nil)
            if jobs.isEmpty {
                Text("Nothing a ping asked for is queued yet.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                    .cardChrome(padding: 5)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            ForEach(jobs, id: \.message.id) { item in
                JobRow(item: item, status: jobStatus(item))
            }
        }
    }

    /// The log has no line for a queued ask until it spawns, so the queue is asked too.
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

/// One line: who, where, what triage made of it, the message, any reply, when. Tapped,
/// it unfolds into the whole message, the decision, the outcome and the reply.
private struct PingRow: View {
    let item: CommsItem
    let unread: Bool
    let me: CommsIdentity?
    let expanded: Bool
    let toggle: () -> Void

    var body: some View {
        let m = item.message
        let fill: Color = banned ? Color.red.opacity(0.10)
            : (unread ? Color.blue.opacity(0.13) : Color.gray.opacity(0.07))
        return VStack(alignment: .leading, spacing: 5) {
            line
            if expanded { details(m) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardChrome(fill: fill, padding: 5)
    }

    private var line: some View {
        let m = item.message
        let flat = Store.snippet(readableText, max: 400)
        let reply = replyPill
        return HStack(spacing: 5) {
            Circle().fill(unread ? Color.blue : Color.clear).frame(width: 6, height: 6)
            Text(m.sender).font(.system(size: 11, weight: .semibold)).lineLimit(1)
                .fixedSize()
            Text(m.place).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
                .fixedSize()
            StatusPill(text: actionWord, tint: actionTint).fixedSize()
            if banned {
                StatusPill(text: "BANNED", tint: .red, symbol: "hand.raised.fill").fixedSize()
            }
            Text(flat).font(.system(size: 11)).lineLimit(1).truncationMode(.tail)
                .frame(maxWidth: .infinity, alignment: .leading)
                .layoutPriority(-1)
            if let reply {
                StatusPill(text: reply.word, tint: reply.tint,
                           symbol: "arrowshape.turn.up.left.fill")
                    .fixedSize()
                    .help(item.reply ?? reply.word)
            }
            Text(Fmt.clock(item.at)).font(.system(size: 9).monospacedDigit())
                .foregroundStyle(.secondary).fixedSize()
        }
        .contentShape(Rectangle())
        .onTapGesture(perform: toggle)
        .accessibilityAddTraits(.isButton)
    }

    private func details(_ m: CommsMessage) -> some View {
        let decision = decisionText
        let outcome = item.outcome ?? "not acted on yet"
        let replyLine = replyDetail
        return VStack(alignment: .leading, spacing: 4) {
            Text(readableText).font(.system(size: 11))
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            HStack(alignment: .firstTextBaseline, spacing: 5) {
                ForEach(reasonChips, id: \.self) { chip in
                    StatusPill(text: chip, tint: .blue)
                }
                Text(decision).font(.system(size: 10)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text(outcome).font(.system(size: 10)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let replyLine {
                Text(replyLine).font(.system(size: 10))
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            if let url = URL(string: m.permalink), !m.permalink.isEmpty {
                Button("Open in Slack") { NSWorkspace.shared.open(url) }
                    .buttonStyle(.link).font(.system(size: 10))
            }
        }
        .padding(.leading, 11)
    }

    private var fromBannedSender: Bool {
        item.decision?.reason.hasPrefix(CommsWatch.bannedPrefix) == true
    }

    /// The ping that got its sender banned, one a banned sender sent, or an ask whose
    /// run a ban stopped.
    private var banned: Bool {
        let outcome = item.outcome ?? ""
        return fromBannedSender || outcome == Store.commsSenderBannedOutcome
            || (item.decision?.action == .injection
                && (outcome.hasPrefix(Store.commsBannedOutcomePrefix)
                    || outcome.hasSuffix(Store.commsAlreadyBannedSuffix)))
            || Store.commsStoppedByBan(item)
    }

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

    /// Nil when no reply was due; a skipped one keeps its reason for `details`.
    private var replyPill: (word: String, tint: Color)? {
        guard let outcome = item.replyOutcome else { return nil }
        if outcome == "sent" { return ("replied", .green) }
        if outcome == "sending" { return ("replying", .orange) }
        if outcome.hasPrefix("failed") { return ("reply failed", .red) }
        if outcome.hasPrefix("skipped") { return ("not answered", .secondary) }
        return nil
    }

    private var replyDetail: String? {
        guard let outcome = item.replyOutcome else { return nil }
        guard let text = item.reply, !text.isEmpty else { return "No reply: " + outcome }
        let head = outcome == "sent" ? "Replied: " : "Reply (" + outcome + "): "
        return head + text
    }

    private var actionWord: String {
        guard let d = item.decision else { return "triaging" }
        if fromBannedSender { return "banned sender" }
        switch d.action {
        case .none:        return "none"
        case .bump:        return "bump #\(d.number)"
        case .review:      return "review #\(d.number)"
        case .fixIssue:    return "fix #\(d.number)"
        case .investigate: return "investigate"
        case .question:    return "question"
        case .injection:   return "injection"
        }
    }

    private var actionTint: Color {
        if fromBannedSender { return .red }
        switch item.decision?.action {
        case .bump?:        return .orange
        case .review?:      return .purple
        case .fixIssue?:    return .green
        case .investigate?: return .teal
        case .question?:    return .indigo
        case .injection?:   return .red
        case .none?, nil:   return .secondary
        }
    }

    /// A `none` usually has only a reason; an injection shows the words it quoted.
    private var decisionText: String {
        guard let d = item.decision else { return "" }
        if d.action == .injection { return "\"\(d.evidence)\" - \(d.reason)" }
        if fromBannedSender { return d.reason }
        return d.summary.isEmpty ? d.reason : d.summary
    }
}

/// One Slack sender on the ban list: who, when, where and the words that got them banned.
private struct BannedSenderRow: View {
    let ban: BannedAuthor
    let place: String?

    var body: some View {
        let id = ban.slackUserID ?? ""
        let when = ban.ago()
        let absolute = ban.atDate.map(BannedAuthor.absolute) ?? "unknown time"
        let evidence = (ban.evidence ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let quoted = evidence.isEmpty ? "" : "\"" + Store.snippet(evidence, max: 300) + "\""
        let link = ban.whereURL
        return HStack(alignment: .firstTextBaseline, spacing: 5) {
            Text(ban.displayName).font(.system(size: 11, weight: .semibold)).fixedSize()
            Text(id).font(.system(size: 10, design: .monospaced)).foregroundStyle(.secondary)
                .fixedSize()
            if let place {
                Text(place).font(.system(size: 10)).foregroundStyle(.secondary).fixedSize()
            }
            Text("banned \(when)").font(.system(size: 10, weight: .semibold))
                .foregroundStyle(.red)
                .fixedSize()
                .help("Banned \(absolute)")
            Text(quoted).font(.system(size: 10)).lineLimit(1).truncationMode(.tail)
                .frame(maxWidth: .infinity, alignment: .leading)
                .layoutPriority(-1)
                .help(evidence)
            if let link {
                Button("Open in Slack") { NSWorkspace.shared.open(link) }
                    .buttonStyle(.link).font(.system(size: 10))
                    .fixedSize()
            }
        }
    }
}

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
        return HStack(spacing: 6) {
            Text(item.label ?? item.queueKey ?? "").font(.system(size: 11, weight: .semibold))
                .lineLimit(1).truncationMode(.tail)
                .frame(maxWidth: .infinity, alignment: .leading)
            StatusPill(text: status.word, tint: status.tint).fixedSize()
            if let report {
                Button("Report") { NSWorkspace.shared.open(report) }
                    .buttonStyle(.link).font(.system(size: 10))
                    .fixedSize()
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardChrome(padding: 5)
        .help(times)
    }

    private var timesText: String {
        var parts = ["asked \(Fmt.clock(item.at))"]
        if let s = item.startedAt { parts.append("started \(Fmt.clock(s))") }
        if let f = item.finishedAt { parts.append("finished \(Fmt.clock(f))") }
        return parts.joined(separator: " · ")
    }

    /// Must match the report path `InvestigateConfig` hands the agent.
    private var reportURL: URL? {
        guard item.queueKey?.hasPrefix(AgentTaskQueue.investigateAction + ":") == true,
              let n = CommsTS.number(item.message.ts) else { return nil }
        let url = CommsLog.reportsDir().appendingPathComponent("\(n).md")
        return FileManager.default.fileExists(atPath: url.path) ? url : nil
    }
}
