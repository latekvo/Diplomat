import Foundation
import AppKit
import UserNotifications
import DiplomatCore

// MARK: - Comms: the Slack watcher, on the macOS side
//
// The core decides everything that can be decided without this app: which messages are
// pings, what the triage answered, and what that answer asks for (`CommsWatch.tick`,
// `CommsPlan.effect`). What is left here is what only the app can do — find the token,
// run the tick on a timer, turn each effect into queue work through the same requested
// asks a wizard sweep makes, and say what it did: an `action` line in Comms' own log,
// one line in the activity feed, a notification.
//
// Nothing here starts an agent. Every ask goes into the queue and waits there for a bay,
// so the ban list, the task cap, the spending budget and the disk floor all still stand
// between a Slack message and a terminal.

/// What GitHub says about the PR or issue a message named.
struct CommsTarget: Equatable {
    let number: Int
    let url: String
    let author: String
    let isOpen: Bool
    /// GitHub's word for the state, for the outcome line when it is not open.
    let state: String
}

enum CommsTargetKind { case pr, issue }

extension Store {
    /// The tick period: `DIPLOMAT_COMMS_SECS`, else `comms.json`'s `pollSecs`, never
    /// under its `minPollSecs` — `search.messages` is Tier 2, and a tick is two searches
    /// plus one per keyword — nor over a day, past which the loop's nanosecond sleep
    /// would overflow. A value that is not a finite number is ignored.
    static var commsPollInterval: TimeInterval {
        let model = try? CoreAssets.comms()
        let floor = Double(model?.minPollSecs ?? 20)
        let secs = ProcessInfo.processInfo.environment["DIPLOMAT_COMMS_SECS"]
            .flatMap(Double.init).flatMap { $0.isFinite ? $0 : nil }
        return min(max(floor, secs ?? Double(model?.pollSecs ?? 60)), commsMaxPollInterval)
    }

    static let commsMaxPollInterval: TimeInterval = 86_400

    /// Stand in for `gh` in the Comms self-test: handed the arguments of a `gh pr view` or
    /// `gh issue view`, answers with what gh would print, or throws what gh would.
    /// Headless only.
    static var commsGHOverride: (@MainActor ([String]) async throws -> Data)?

    /// The largest stretch of `events.jsonl` a refresh reads on the main actor. More than
    /// that — the first read of a long log, or the one after a rotation — is read and
    /// folded off it.
    static let commsInlineReadBytes: UInt64 = 64 << 10

    /// What `CommsWatch.tick` is handed: the Settings fields, and the operator's login
    /// for the triage prompt.
    var commsSettings: CommsSettings {
        CommsSettings(keywords: CommsSettings.keywordList(commsKeywordsRaw),
                      senderAllowlist: CommsSettings.allowlist(commsSenderAllowlistRaw),
                      model: commsModel, login: effectiveMe)
    }

    var commsToggles: CommsToggles {
        CommsToggles(requests: commsActRequests, bumps: commsActBumps,
                     investigations: commsActInvestigations)
    }

    /// Pings recorded since the operator last opened the screen: the header badge.
    var commsUnread: Int { commsItems.filter { $0.at > commsSeenAt }.count }

    func markCommsSeen() {
        if commsUnread > 0 { commsSeenAt = Date() }
    }

    /// Bring `commsItems` up to date with `events.jsonl`.
    ///
    /// The log only ever grows between rotations, so a refresh reads just the lines added
    /// since the last one, and a file whose inode, size and modification time are all
    /// unchanged is not read at all. A long read is done off the main actor and returned;
    /// until it lands, further refreshes return it too, and it refreshes again on landing
    /// for whatever was written meanwhile.
    @discardableResult
    func refreshComms() -> Task<Void, Never>? {
        if let reading = commsLogRead { return reading }
        let url = CommsLog.eventsURL()
        guard let now = CommsLogReader.stamp(url) else {
            commsLog = CommsLogCache()
            if !commsItems.isEmpty { commsItems = [] }
            return nil
        }
        let from: UInt64
        if let mark = commsLog.mark, mark.inode == now.inode, now.size > mark.offset {
            from = mark.offset
        } else if let mark = commsLog.mark, mark.inode == now.inode,
                  now.size == mark.offset, now.mtime == mark.mtime {
            return nil
        } else {
            // First read, a rotation (a new inode), or a file that shrank or was
            // rewritten in place.
            from = 0
        }
        let base = from == 0 ? [] : commsLog.events
        if now.size - from <= Store.commsInlineReadBytes {
            if let read = CommsLogReader.read(url, from: from) {
                absorbCommsLog(base + read.events, CommsLog.fold(base + read.events), read.mark)
            }
            return nil
        }
        let reading = Task { [weak self] in
            let read = await Task.detached(priority: .utility) {
                () -> ([CommsEvent], [CommsItem], CommsLogMark)? in
                guard let read = CommsLogReader.read(url, from: from) else { return nil }
                let all = base + read.events
                return (all, CommsLog.fold(all), read.mark)
            }.value
            guard let self else { return }
            self.commsLogRead = nil
            if let read { self.absorbCommsLog(read.0, read.1, read.2) }
            self.refreshComms()
        }
        commsLogRead = reading
        return reading
    }

    private func absorbCommsLog(_ events: [CommsEvent], _ items: [CommsItem], _ mark: CommsLogMark) {
        commsLog = CommsLogCache(events: events, mark: mark)
        if items != commsItems { commsItems = items }
    }

    /// Append one of the app's own lines, and fold it in straight away: what decides
    /// the next line (an owner's start, a run's end) reads `commsItems`.
    func recordComms(_ events: [CommsEvent]) {
        for e in events { CommsLog.append(e) }
        refreshComms()
    }

    // MARK: the loop

    /// Started at launch and left running: a tick with Comms switched off returns at
    /// once, which is cheaper than starting and stopping a task on every toggle.
    func startCommsMonitor() {
        guard commsMonitorTask == nil, !Headless.active else { return }
        commsMonitorTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.runCommsPollOnce()
                let ns = UInt64(Store.commsPollInterval * 1_000_000_000)
                try? await Task.sleep(nanoseconds: ns)
            }
        }
        // The loop's sleep stops while the Mac sleeps, so a message sent overnight would
        // wait for the first tick after wake; asking on wake answers it straight away.
        if commsWakeObserver == nil {
            commsWakeObserver = NSWorkspace.shared.notificationCenter.addObserver(
                forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
            ) { [weak self] _ in
                Task { await self?.runCommsPollOnce() }
            }
        }
    }

    /// One tick: find the token, poll Slack and triage off the main actor, then act on
    /// what came back. Overlap-guarded like the monitor poll, because the timer, a wake
    /// and the enable switch can all kick one while another is still in its triage.
    func runCommsPollOnce() async {
        guard commsEnabled, !Headless.active, !commsPollInFlight else { return }
        commsPollInFlight = true
        defer { commsPollInFlight = false }
        let item = AppConfig.slackTokenKeychainItem
        let token = await Task.detached(priority: .utility) { CommsToken.read(item: item) }.value
        guard let token else {
            commsTokenMissing = true
            noteCommsHealth(CommsToken.missingMessage(item: item))
            return
        }
        commsTokenMissing = false
        let settings = commsSettings
        let result = await Task.detached(priority: .utility) {
            CommsWatch.tick(token: token, settings: settings,
                            classify: { CommsClassifier.run($0, settings: settings) })
        }.value
        commsLastPoll = Date()
        await applyComms(result)
    }

    /// The Settings "Test" button: `auth.test` with the token as it is stored now.
    func testSlackToken() async {
        guard !commsTesting else { return }
        commsTesting = true
        defer { commsTesting = false }
        let item = AppConfig.slackTokenKeychainItem
        let answer: Result<CommsIdentity, Error>? = await Task.detached(priority: .userInitiated) {
            guard let token = CommsToken.read(item: item) else { return nil }
            return Result { try SlackAPI(token: token).authTest() }
        }.value
        switch answer {
        case nil:
            commsTestResult = CommsToken.missingMessage(item: item)
        case .success(let me)?:
            commsIdentity = me
            commsTestResult = "Token is @\(me.handle) (\(me.userID)) in workspace \(me.teamID)"
        case .failure(let error)?:
            commsTestResult = (error as? LocalizedError)?.errorDescription ?? "\(error)"
        }
    }

    // MARK: acting on a tick

    /// Everything a tick found, applied: the identity and health it reported, then each
    /// ping's effect in the order the tick triaged them. Not private: the Comms self-test
    /// hands it a tick result it composed, which is the whole of this file's behaviour
    /// short of Slack and `claude`.
    ///
    /// A tick the operator switched Comms off during still lands its pings, which the
    /// tick has already logged, but acts on none of them and notifies nothing.
    func applyComms(_ result: CommsTickResult) async {
        if let me = result.me, me != commsIdentity { commsIdentity = me }
        noteCommsHealth(result.error)
        for notice in result.notices { AuditLog.log("auto", "comms-notice", notice) }
        if !result.notices.isEmpty { refreshAudit() }
        let toggles = commsToggles
        let settings = commsSettings
        let reports = CommsLog.reportsDir()
        for item in result.items {
            let act: CommsActResult
            if commsEnabled {
                let effect = CommsPlan.effect(for: item.decision, message: item.message,
                                              toggles: toggles, settings: settings,
                                              reportDir: reports)
                act = await commsAct(effect, item)
            } else {
                act = CommsActResult(verb: "comms-ping",
                                     outcome: "Comms was switched off, so ping only",
                                     feedLine: Store.pingLine(item.message))
            }
            var lines = [CommsEvent(kind: .action, id: item.message.id, outcome: act.outcome,
                                    queueKey: act.queueKey, label: act.label)]
            if let run = act.running {
                lines.append(CommsEvent(at: Date(timeIntervalSince1970: run.dispatchedAt),
                                        kind: .started, id: item.message.id, label: run.label))
            }
            // Folded before the next item's lookup suspends: a dispatch of this ask in
            // that gap finds its owner here (`commsSettled`).
            recordComms(lines)
            AuditLog.log("auto", act.verb, act.feedLine)
            if commsNotify && commsEnabled { CommsNotifier.post(item.message) }
        }
        if !result.items.isEmpty { refreshAudit() }
        refreshComms()
    }

    /// What one effect did: the outcome the screen shows under the ping, the queue key
    /// of the work that answers it (none for a bump, which asks for no work of its own),
    /// and the one activity line it is written as.
    struct CommsActResult {
        var verb: String
        var outcome: String
        var queueKey: String?
        var label: String?
        var feedLine: String
        /// The agent already doing what the ping asked for, whose start the ping records.
        var running: AgentState.RunRecord?
    }

    private static func pingLine(_ m: CommsMessage) -> String {
        "\(m.place) from \(m.sender): \(Store.snippet(m.text))"
    }

    private func commsAct(_ effect: CommsEffect, _ item: CommsTickResult.Item) async -> CommsActResult {
        let m = item.message
        let who = "\(m.sender) (\(m.place))"
        switch effect {
        case .none:
            let off = Store.switchedOff(item.decision.action)
            return CommsActResult(verb: "comms-ping", outcome: off ?? "ping only",
                                  feedLine: Store.pingLine(m))
        case .review(let n):
            let pr: CommsTarget
            switch await commsLookup(.pr, n) {
            case .found(let t) where t.isOpen: pr = t
            case let other: return refused("comms-review", who, Store.notOpen("PR", n, other))
            }
            let mine = !effectiveMe.isEmpty && pr.author.lowercased() == effectiveMe.lowercased()
            if !mine, BanList.isBanned(pr.author, in: bannedAuthors) {
                return refused("comms-review", who, "@\(pr.author) is banned, so #\(n) was not queued")
            }
            let cfg = ReviewConfig(target: .specific, me: effectiveMe, specificPR: String(n),
                                   specificAuthor: mine ? .mine : .theirs)
            return queue(RequestedWork(number: n, url: pr.url, author: mine ? "" : pr.author,
                                       config: .review(cfg)),
                         verb: "comms-review", who: who, front: true)
        case .fixIssue(let n):
            let issue: CommsTarget
            switch await commsLookup(.issue, n) {
            case .found(let t) where t.isOpen: issue = t
            case let other: return refused("comms-fix", who, Store.notOpen("issue", n, other))
            }
            if BanList.isBanned(issue.author, in: bannedAuthors) {
                return refused("comms-fix", who, "@\(issue.author) is banned, so #\(n) was not queued")
            }
            let cfg = IssueConfig(target: .specific, me: effectiveMe, specificIssue: String(n))
            return queue(RequestedWork(number: n, url: issue.url, author: issue.author,
                                       config: .issues(cfg)),
                         verb: "comms-fix", who: who, front: true)
        case .bump(let n):
            let keys = queuedTasks.map(\.id).filter { $0.hasSuffix(":\(n)") }
            for key in keys { moveQueuedTaskToFront(key) }
            let outcome = keys.isEmpty ? "nothing queued for #\(n)"
                : "moved \(keys.joined(separator: ", ")) to the front of its band"
            return CommsActResult(verb: "comms-bump", outcome: outcome,
                                  feedLine: "Bump #\(n) asked by \(who) — \(outcome)")
        case .investigate(let cfg):
            return queue(RequestedWork(number: cfg.number, url: cfg.permalink, author: "",
                                       config: .investigate(cfg)),
                         verb: "comms-investigate", who: who, front: false)
        }
    }

    /// Queue `ask`, and with `front` move it to the front of its band — where a person
    /// asking for it by name puts it, ahead of a sweep's fifty.
    ///
    /// Work that is already running, or already waiting under another key, is not asked
    /// for twice: the ping takes that work's key and label instead, so its started and
    /// finished follow the agent that does it.
    private func queue(_ ask: RequestedWork, verb: String, who: String,
                       front: Bool) -> CommsActResult {
        if let row = commsRow(doing: ask.key) {
            let outcome = "already running"
            return CommsActResult(verb: verb, outcome: outcome, queueKey: ask.key,
                                  label: row.record.label,
                                  feedLine: "\(row.record.label) asked by \(who) — \(outcome)",
                                  running: row.record)
        }
        let suffix = front ? ", at the front of its band" : ""
        let twins = Store.commsTwinKeys(ask.key)
        // Only a find that will run: a paused monitor's waits for "execute now" or its
        // toggle, where an ask is never paused (`isPaused`).
        if let twin = startingTasks.first(where: { twins.contains($0.id) })
            ?? queuedTasks.first(where: { twins.contains($0.id) && !isPaused($0.job.counter) }) {
            if front { moveQueuedTaskToFront(twin.id) }
            let outcome = "already queued" + suffix
            return CommsActResult(verb: verb, outcome: outcome, queueKey: twin.id,
                                  label: twin.job.label,
                                  feedLine: "\(twin.job.label) asked by \(who) — \(outcome)")
        }
        let fresh = requestOne(ask)
        if front { moveQueuedTaskToFront(ask.key) }
        let outcome = (fresh ? "queued" : "already queued") + suffix
        return CommsActResult(verb: verb, outcome: outcome, queueKey: ask.key, label: ask.label,
                              feedLine: "\(ask.label) asked by \(who) — \(outcome)")
    }

    /// The keys a monitor queues the same work under as the ask `key`: a review requested
    /// of me on GitHub is the review a Slack message asks for.
    private static func commsTwinKeys(_ key: String) -> [String] {
        guard let parts = commsKeyParts(key), parts.verb == AgentTaskQueue.reviewAction else {
            return []
        }
        return [AgentTaskQueue.key(auditAction: reviewRequestAction, number: parts.number)]
    }

    private static let reviewRequestAction = "review-req"

    private static func commsKeyParts(_ key: String) -> (verb: String, number: Int)? {
        let parts = key.split(separator: ":", maxSplits: 1)
        guard parts.count == 2, let n = Int(parts[1]) else { return nil }
        return (String(parts[0]), n)
    }

    /// The agent row doing the work `key` names, if one is up: any review agent on that PR
    /// (the dispatch gate treats every agent on a PR as that PR's), or a fix of that
    /// issue, which is about no PR and is known by the label every issue fix wears
    /// (`RequestedWork.label`, `IssueWizard`).
    private func commsRow(doing key: String) -> AgentRow? {
        guard let parts = Store.commsKeyParts(key) else { return nil }
        let n = parts.number
        switch parts.verb {
        case AgentTaskQueue.reviewAction, Store.reviewRequestAction:
            return agentRows.first {
                $0.record.prNumber == n && $0.record.kind == AgentTaskQueue.reviewAction
            }
        case AgentTaskQueue.issuesAction:
            return agentRows.first {
                $0.record.kind == AgentTaskQueue.issuesAction
                    && $0.record.label.contains("Issues · #\(n) · ")
            }
        default:
            return nil
        }
    }

    private func refused(_ verb: String, _ who: String, _ why: String) -> CommsActResult {
        CommsActResult(verb: verb, outcome: why, feedLine: "Asked by \(who): \(why)")
    }

    private static func switchedOff(_ action: CommsAction) -> String? {
        switch action {
        case .review, .fixIssue: return "review and fix asks are switched off, so ping only"
        case .bump:              return "bumps are switched off, so ping only"
        case .investigate:       return "investigations are switched off, so ping only"
        case .none:              return nil
        }
    }

    private static func notOpen(_ noun: String, _ n: Int, _ lookup: CommsLookup) -> String {
        switch lookup {
        case .found(let t): return "\(noun) #\(n) is \(t.state.lowercased()), so nothing was queued"
        case .missing:      return "no \(noun) #\(n) in this repo, so nothing was queued"
        case .unasked(let why):
            return "couldn't ask GitHub about \(noun) #\(n) (\(why)), so nothing was queued"
        }
    }

    /// What asking GitHub about a PR or an issue came to.
    enum CommsLookup {
        case found(CommsTarget)
        /// GitHub answered that the repo has no such PR or issue.
        case missing
        /// GitHub did not answer: gh failed, timed out or printed no PR or issue.
        case unasked(String)
    }

    /// Whether gh failed because GitHub knows no such number - how `gh pr view` and
    /// `gh issue view` both say it - rather than because it could not be asked.
    static func ghSaysNotFound(_ error: Error) -> Bool {
        guard case GHError.process(_, let stderr) = error else { return false }
        return stderr.contains("Could not resolve to")
    }

    nonisolated static func snippet(_ text: String, max: Int = 80) -> String {
        let flat = text.split(whereSeparator: \.isNewline).joined(separator: " ")
        return flat.count > max ? String(flat.prefix(max - 1)) + "…" : flat
    }

    /// The PR or issue `n`, open or not: the panel's own last fetch first, which lists
    /// open work only, then `gh` for what it does not hold — an issue a filtered list
    /// left out, a PR opened since the last refresh, or one already closed.
    ///
    /// `gh issue view` answers a PR's number with that PR, so an answer is an issue only
    /// when its url says so; a PR's number is no issue.
    private func commsLookup(_ kind: CommsTargetKind, _ n: Int) async -> CommsLookup {
        switch kind {
        case .pr:
            if let pr = prs.first(where: { $0.number == n }) {
                return .found(CommsTarget(number: n, url: pr.url, author: pr.author, isOpen: true,
                                          state: "OPEN"))
            }
        case .issue:
            if let issue = issues.first(where: { $0.number == n }) {
                return .found(CommsTarget(number: n, url: issue.url, author: issue.author,
                                          isOpen: true, state: "OPEN"))
            }
        }
        let (owner, repo) = CoreAssets.repoCoordinates()
        let verb = kind == .pr ? "pr" : "issue"
        let args = [verb, "view", String(n), "--repo", "\(owner)/\(repo)",
                    "--json", "number,url,author,state"]
        let data: Data
        do {
            if Headless.active, let stub = Store.commsGHOverride {
                data = try await stub(args)
            } else {
                data = try await GH.run(args)
            }
        } catch {
            if Store.ghSaysNotFound(error) { return .missing }
            let why = (error as? LocalizedError)?.errorDescription ?? "\(error)"
            return .unasked(Store.snippet(why, max: 120))
        }
        guard let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let state = obj["state"] as? String else {
            return .unasked("gh printed no PR or issue")
        }
        let url = obj["url"] as? String ?? ""
        if kind == .issue, !url.contains("/issues/") { return .missing }
        let author = (obj["author"] as? [String: Any])?["login"] as? String ?? ""
        return .found(CommsTarget(number: n, url: url, author: author, isOpen: state == "OPEN",
                                  state: state))
    }

    /// Publish the poll's health, and log only its transitions: a failing token fails
    /// every minute, and one line a minute would bury the feed.
    func noteCommsHealth(_ error: String?) {
        if let error {
            if commsError == nil {
                AuditLog.log("auto", "comms-error", "Comms poll failing: \(error.prefix(160))")
                refreshAudit()
            }
            if commsError != error { commsError = error }
        } else if commsError != nil {
            AuditLog.log("auto", "comms-recovered", "Comms polls succeeding again")
            refreshAudit()
            commsError = nil
        }
    }

    // MARK: started / finished

    /// An ask Comms queued has been dispatched: its pings record `started`. Called from
    /// `settleRequested` for every requested ask, so it is a no-op for the rest.
    func commsSettled(_ entry: QueuedAgentTask, _ outcome: DispatchOutcome) {
        guard outcome.wasHandled else { return }
        // Owners are found in the fold, so one still being read is waited for.
        if let reading = commsLogRead {
            Task { [weak self] in
                await reading.value
                self?.commsSettled(entry, outcome)
            }
            return
        }
        let owners = commsItems.filter { $0.queueKey == entry.id && $0.startedAt == nil }
        guard !owners.isEmpty else { return }
        recordComms(owners.map {
            CommsEvent(kind: .started, id: $0.message.id, label: entry.job.label)
        })
    }

    /// What the agent rows say about the work Comms' pings point at, after every publish
    /// of them.
    ///
    /// A ping that took a monitor's queued work (`queue`) records `started` once an agent
    /// row is doing it: a monitor dispatches its own finds, so `commsSettled`, which hears
    /// only the asks', never sees that one start.
    ///
    /// A started run whose label no agent row wears any more, and which is not waiting to
    /// run again, has finished: its pings record `finished`, once.
    func noteCommsRuns() {
        guard commsLogRead == nil else { return }
        let waiting = Set(queuedTasks.map(\.id) + startingTasks.map(\.id))
        var lines: [CommsEvent] = []
        for item in commsItems where item.startedAt == nil && item.finishedAt == nil {
            guard let key = item.queueKey, !waiting.contains(key),
                  let verb = Store.commsKeyParts(key)?.verb,
                  !AgentTaskQueue.requestedActions.contains(verb),
                  let row = commsRow(doing: key) else { continue }
            lines.append(CommsEvent(at: Date(timeIntervalSince1970: row.record.dispatchedAt),
                                    kind: .started, id: item.message.id, label: row.record.label))
        }
        let running = Set(agentRows.map(\.record.label))
        for item in commsItems where item.startedAt != nil && item.finishedAt == nil
            && !Store.commsNeverStarted(item)
            && !running.contains(item.label ?? "") && !waiting.contains(item.queueKey ?? "") {
            lines.append(CommsEvent(kind: .finished, id: item.message.id, label: item.label))
        }
        if !lines.isEmpty { recordComms(lines) }
    }

    /// The run wearing `label` failed to spawn. An agent row may already have shown it
    /// starting, and a ping taken that start for its own (`noteCommsRuns`, or `queue`'s
    /// "already running"): those pings say it never ran, where the `finished` its
    /// vanished row would earn reads as work done.
    func commsSpawnFailed(label: String, why: String) {
        if let reading = commsLogRead {
            Task { [weak self] in
                await reading.value
                self?.commsSpawnFailed(label: label, why: why)
            }
            return
        }
        let outcome = Store.commsNeverStartedOutcome + " (\(Store.snippet(why, max: 120)))"
        let lines = commsItems
            .filter { $0.label == label && $0.startedAt != nil && $0.finishedAt == nil
                && !Store.commsNeverStarted($0) }
            .map { CommsEvent(kind: .action, id: $0.message.id, outcome: outcome) }
        if !lines.isEmpty { recordComms(lines) }
    }

    static let commsNeverStartedOutcome = "its agent never started, so nothing ran"

    /// A ping whose run failed to spawn (`commsSpawnFailed`): there is no run to finish.
    static func commsNeverStarted(_ item: CommsItem) -> Bool {
        item.outcome?.hasPrefix(commsNeverStartedOutcome) == true
    }
}

/// Where a read of `events.jsonl` stopped: which file it was (its inode), the offset
/// just past its last whole line, and the file's modification time then.
struct CommsLogMark: Equatable, Sendable {
    var inode: UInt64
    var offset: UInt64
    var mtime: CommsLogReader.Time
}

/// The events read so far and where reading stopped (`Store.refreshComms`).
struct CommsLogCache {
    var events: [CommsEvent] = []
    var mark: CommsLogMark?
}

/// Reads `events.jsonl` from an offset, the way `CommsLog.read` reads all of it.
enum CommsLogReader {
    struct Time: Equatable, Sendable {
        var sec: Int
        var nsec: Int
    }

    struct Stamp: Equatable {
        var inode: UInt64
        var size: UInt64
        var mtime: Time
    }

    static func stamp(_ url: URL) -> Stamp? {
        var st = stat()
        guard stat(url.path, &st) == 0 else { return nil }
        return stamp(st)
    }

    private static func stamp(_ st: stat) -> Stamp {
        Stamp(inode: UInt64(st.st_ino), size: UInt64(max(0, st.st_size)),
              mtime: Time(sec: Int(st.st_mtimespec.tv_sec), nsec: Int(st.st_mtimespec.tv_nsec)))
    }

    /// Every event that decodes from `offset` to the last newline, and the mark there. A
    /// line still being written is left for the next read.
    static func read(_ url: URL, from offset: UInt64) -> (events: [CommsEvent], mark: CommsLogMark)? {
        guard let handle = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? handle.close() }
        var st = stat()
        guard fstat(handle.fileDescriptor, &st) == 0 else { return nil }
        let at = stamp(st)
        guard at.size >= offset else { return nil }
        guard (try? handle.seek(toOffset: offset)) != nil,
              let data = try? handle.read(upToCount: Int(at.size - offset)) ?? Data() else {
            return nil
        }
        let end = data.lastIndex(of: UInt8(ascii: "\n")).map { data.index(after: $0) }
            ?? data.startIndex
        let d = CommsLog.decoder
        let events = data[data.startIndex..<end].split(separator: UInt8(ascii: "\n")).compactMap {
            try? d.decode(CommsEvent.self, from: Data($0))
        }
        let consumed = UInt64(data.distance(from: data.startIndex, to: end))
        return (events, CommsLogMark(inode: at.inode, offset: offset + consumed, mtime: at.mtime))
    }
}

/// Where the Slack token comes from. Read on each use, never cached and never written:
/// `DIPLOMAT_SLACK_TOKEN` (tests, a headless poll), else the login-Keychain item
/// Settings names, read through `security` so an unsigned build raises no Keychain
/// prompt from a background tick (`Quota`'s reasoning).
enum CommsToken {
    static let timeout: TimeInterval = 10

    static let envVar = "DIPLOMAT_SLACK_TOKEN"

    static func read(item: String) -> String? {
        if let env = ProcessInfo.processInfo.environment[envVar], !env.isEmpty {
            return env
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/security")
        p.arguments = ["find-generic-password", "-s", item, "-w"]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        let exited = DispatchSemaphore(value: 0)
        p.terminationHandler = { _ in exited.signal() }
        do { try p.run() } catch {
            p.terminationHandler = nil
            return nil
        }
        // A token is far under a pipe's buffer, so the child never blocks on writing it
        // and the read below can wait for the exit.
        if exited.wait(timeout: .now() + timeout) == .timedOut {
            p.terminate()
            return nil
        }
        guard p.terminationStatus == 0 else { return nil }
        let token = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return token.isEmpty ? nil : token
    }

    static func missingMessage(item: String) -> String {
        "No Slack token in the Keychain item \"\(item)\""
    }

    /// The command that stores one, for the setup instructions.
    static func addCommand(item: String) -> String {
        "security add-generic-password -a \"$USER\" -s \(item) -w"
    }
}

/// macOS notifications for new pings. Only from a real app bundle and never in a headless
/// mode: `UNUserNotificationCenter` aborts the process when there is no bundle to
/// attribute a notification to, which is what `swift run` is.
enum CommsNotifier {
    static var available: Bool {
        !Headless.active && Bundle.main.bundleIdentifier != nil
            && Bundle.main.bundleURL.pathExtension == "app"
    }

    /// Asked when Comms is first switched on with notifications wanted, not at launch:
    /// a permission prompt for a feature nobody has enabled is noise.
    static func authorize() {
        guard available else { return }
        let center = UNUserNotificationCenter.current()
        center.delegate = Presenter.shared
        center.requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    static func post(_ m: CommsMessage) {
        guard available else { return }
        let content = UNMutableNotificationContent()
        content.title = "Slack · \(m.place) from \(m.sender)"
        content.body = Store.snippet(m.text, max: 160)
        let center = UNUserNotificationCenter.current()
        center.delegate = Presenter.shared
        center.add(UNNotificationRequest(identifier: "comms-\(m.id)", content: content, trigger: nil))
    }

    /// Shows a notification while the popover is open too: an accessory app is the
    /// active one exactly then, and macOS drops an active app's notifications by default.
    private final class Presenter: NSObject, UNUserNotificationCenterDelegate {
        static let shared = Presenter()

        func userNotificationCenter(_ center: UNUserNotificationCenter,
                                    willPresent notification: UNNotification,
                                    withCompletionHandler completionHandler:
                                        @escaping (UNNotificationPresentationOptions) -> Void) {
            completionHandler([.banner, .sound])
        }
    }
}

/// The Comms dry run, `DIPLOMAT_COMMS_POLL=1`. Kept out of `DiplomatApp.swift`, whose
/// `env[...]` reads CI takes for the list of headless modes.
extension Dump {
    /// One Comms tick for real, applied to nothing: the Settings the panel would hand it,
    /// the token it would find, Slack's answer, the triage's answer, and the queue effect
    /// each would have had. Unless `DIPLOMAT_COMMS_DIR` names a directory to tick, it
    /// ticks a scratch one whose watermark is `commsPollLookback` ago with nothing seen,
    /// so it triages what reached the operator in that time whether or not Comms is on,
    /// and the live app's own watermark is untouched. False when there is no token or
    /// the tick failed.
    @MainActor
    static func commsPoll() async -> Bool {
        let env = ProcessInfo.processInfo.environment
        var scratch: URL?
        if (env["DIPLOMAT_COMMS_DIR"] ?? "").isEmpty {
            let dir = FileManager.default.temporaryDirectory
                .appendingPathComponent("diplomat-commspoll-\(getpid())")
            let since = CommsTS.number(Date()) - Int(commsPollLookback) * 1_000_000
            CommsState(watermark: CommsTS.string(since)).save(in: dir)
            setenv("DIPLOMAT_COMMS_DIR", dir.path, 1)
            scratch = dir
        }
        defer { if let scratch { try? FileManager.default.removeItem(at: scratch) } }
        let dir = CommsLog.dir
        let item = AppConfig.slackTokenKeychainItem
        let fromEnv = !(env["DIPLOMAT_SLACK_TOKEN"] ?? "").isEmpty
        print("== comms poll (dry run) ==")
        print("dir:   \(dir.path)"
              + (scratch == nil ? "" : " (scratch: the last \(Int(commsPollLookback / 60)) min)"))
        print("token: " + (fromEnv ? "DIPLOMAT_SLACK_TOKEN" : "Keychain item \"\(item)\""))
        let token = await Task.detached { CommsToken.read(item: item) }.value
        guard let token else {
            print(CommsToken.missingMessage(item: item))
            return false
        }
        let store = Store()
        // The login the live app's triage prompt names; a headless Store has not fetched it.
        await store.fetchMe()
        let settings = store.commsSettings
        let toggles = store.commsToggles
        let model = settings.model.isEmpty ? "(default)" : settings.model
        print("keywords: \(settings.keywords)  senders: \(settings.senderAllowlist)  model: \(model)")
        let result = await Task.detached {
            CommsWatch.tick(token: token, settings: settings,
                            classify: { CommsClassifier.run($0, settings: settings) })
        }.value
        if let me = result.me { print("as:    @\(me.handle) (\(me.userID)) in \(me.teamID)") }
        if let error = result.error { print("error: \(error)") }
        print("\(result.items.count) new ping(s)\n")
        let reports = CommsLog.reportsDir()
        for it in result.items {
            let m = it.message
            let d = it.decision
            let effect = CommsPlan.effect(for: d, message: m, toggles: toggles,
                                          settings: settings, reportDir: reports)
            let reasons = m.reasons.map(\.rawValue).joined(separator: ",")
            print("- \(m.place) from \(m.sender) [\(reasons)] ts=\(m.ts)")
            print("    text:     \(Store.snippet(m.text, max: 120))")
            print("    decision: \(d.action.rawValue) #\(d.number)  \"\(d.summary)\"  (\(d.reason))")
            print("    would:    \(commsEffectLine(effect))")
        }
        print("\n(dry run: nothing was queued, logged to the feed, or notified)")
        return result.error == nil
    }

    static let commsPollLookback: TimeInterval = 3600

    private static func commsEffectLine(_ effect: CommsEffect) -> String {
        switch effect {
        case .none:            return "nothing (ping only)"
        case .bump(let n):     return "move what is queued for #\(n) to the front of its band"
        case .review(let n):   return "queue a review of PR #\(n), if open, at the front of its band"
        case .fixIssue(let n): return "queue a fix of issue #\(n), if open, at the front of its band"
        case .investigate(let cfg):
            return "queue \"\(cfg.label)\" (investigate:\(cfg.number)), report \(cfg.reportPath)"
        }
    }
}
