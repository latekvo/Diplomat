import Foundation
import AppKit
import UserNotifications
import DiplomatCore

// MARK: - Comms: the Slack watcher, on the macOS side
//
// The core decides which messages are pings and what each answer asks for
// (`CommsWatch.tick`, `CommsPlan.effect`); this side runs the tick on a timer and turns each
// effect into the same requested asks a wizard sweep makes. Nothing here starts an agent:
// every ask waits in the queue for a bay, so the ban list, task cap, spending budget and
// disk floor all still stand between a Slack message and a terminal.

/// What GitHub says about the PR or issue a message named.
struct CommsTarget: Equatable {
    let number: Int
    let url: String
    let author: String
    let isOpen: Bool
    /// GitHub's spelling, e.g. `MERGED`.
    let state: String
}

enum CommsTargetKind { case pr, issue }

extension Store {
    /// `DIPLOMAT_COMMS_SECS` (ignored unless finite), else `comms.json`'s `pollSecs`. Never
    /// under `minPollSecs` (`search.messages` is Tier 2, and a tick is two searches plus
    /// one per keyword), nor over a day (a huge value would overflow the nanosecond sleep).
    static var commsPollInterval: TimeInterval {
        let model = try? CoreAssets.comms()
        let floor = Double(model?.minPollSecs ?? 20)
        let secs = ProcessInfo.processInfo.environment["DIPLOMAT_COMMS_SECS"]
            .flatMap(Double.init).flatMap { $0.isFinite ? $0 : nil }
        return min(max(floor, secs ?? Double(model?.pollSecs ?? 60)), commsMaxPollInterval)
    }

    static let commsMaxPollInterval: TimeInterval = 86_400

    /// The Comms self-test's stand-in for `gh pr|issue view`; consulted only headless.
    static var commsGHOverride: (@MainActor ([String]) async throws -> Data)?

    /// The most of `events.jsonl` a refresh reads on the main actor; more is read and
    /// folded off it.
    static let commsInlineReadBytes: UInt64 = 64 << 10

    /// Its `bannedSenders` are read from the ban list on each use, so a ban recorded
    /// since the last tick holds for the next.
    var commsSettings: CommsSettings {
        CommsSettings(keywords: CommsSettings.keywordList(commsKeywordsRaw),
                      senderAllowlist: CommsSettings.allowlist(commsSenderAllowlistRaw),
                      bannedSenders: BanList.slackUserIDs(in: BanList.read()),
                      model: commsModel, login: effectiveMe)
    }

    var commsToggles: CommsToggles {
        CommsToggles(requests: commsActRequests, bumps: commsActBumps,
                     investigations: commsActInvestigations)
    }

    /// Where a Slack ban's message was (`#channel`, `DM`), from the logged ping: the ban
    /// list keeps only its permalink.
    func commsPlace(of ban: BannedAuthor) -> String? {
        guard ban.isSlack, let link = ban.pr, !link.isEmpty else { return nil }
        return commsItems.first { $0.message.permalink == link }?.message.place
    }

    var commsUnread: Int { commsItems.filter { $0.at > commsSeenAt }.count }

    func markCommsSeen() {
        if commsUnread > 0 { commsSeenAt = Date() }
    }

    /// Reads only what `events.jsonl` grew by (it only grows between rotations), and
    /// nothing when inode, size and mtime are unchanged. A long read runs off the main
    /// actor and is returned; until it lands every refresh returns it, and on landing it
    /// refreshes again for whatever was written meanwhile.
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

    /// Folds straight away: what decides the next line (an owner's start, a run's end)
    /// reads `commsItems`.
    func recordComms(_ events: [CommsEvent]) {
        for e in events { CommsLog.append(e) }
        refreshComms()
    }

    // MARK: the loop

    /// Never stopped: a tick with Comms switched off returns at once.
    func startCommsMonitor() {
        guard commsMonitorTask == nil, !Headless.active else { return }
        commsMonitorTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.runCommsPollOnce()
                let ns = UInt64(Store.commsPollInterval * 1_000_000_000)
                try? await Task.sleep(nanoseconds: ns)
            }
        }
        // The loop's sleep pauses while the Mac sleeps; polling on wake catches up at once.
        if commsWakeObserver == nil {
            commsWakeObserver = NSWorkspace.shared.notificationCenter.addObserver(
                forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
            ) { [weak self] _ in
                Task { await self?.runCommsPollOnce() }
            }
        }
    }

    /// Overlap-guarded: the timer, a wake and the enable switch can each kick a tick while
    /// another is still in its triage.
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

    /// Not private: the Comms self-test hands it composed tick results.
    ///
    /// A tick that Comms was switched off during still lands its pings (the tick has
    /// already logged them), but acts on and notifies none.
    ///
    /// A logged ping with no action is acted on first, unnotified: an earlier apply that
    /// never finished (a quit or handover during a lookup) delivered it, and the
    /// watermark is past it.
    ///
    /// A sender banned before or during this apply has nothing else acted on, and no
    /// ping of theirs is notified: the ban's own notification says it.
    func applyComms(_ result: CommsTickResult) async {
        if let me = result.me, me != commsIdentity { commsIdentity = me }
        noteCommsHealth(result.error)
        for notice in result.notices { AuditLog.log("auto", "comms-notice", notice) }
        if !result.notices.isEmpty { refreshAudit() }
        let toggles = commsToggles
        let settings = commsSettings
        let reports = CommsLog.reportsDir()
        let fresh = Set(result.items.map(\.message.id))
        var bannedNow = Set(settings.bannedSenders)
        if let reading = refreshComms() { await reading.value }
        let stranded = commsItems.reversed().compactMap { i -> CommsTickResult.Item? in
            guard i.outcome == nil, !fresh.contains(i.message.id), let d = i.decision else {
                return nil
            }
            return CommsTickResult.Item(message: i.message, decision: d)
        }
        for item in stranded + result.items {
            let act: CommsActResult
            let effect = CommsPlan.effect(for: item.decision, message: item.message,
                                          toggles: toggles, settings: settings,
                                          reportDir: reports)
            let senderBanned = bannedNow.contains(item.message.userID)
            if commsEnabled, senderBanned, effect != .none, !Store.isBan(effect) {
                act = CommsActResult(verb: "comms-ping", outcome: Store.commsSenderBannedOutcome,
                                     feedLine: Store.pingLine(item.message)
                                        + " (\(Store.commsSenderBannedOutcome))")
            } else if commsEnabled {
                act = await commsAct(effect, item)
                if case .ban(let b) = effect, act.verb == "ban" { bannedNow.insert(b.userID) }
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
            if let line = act.feedLine { AuditLog.log("auto", act.verb, line) }
            let quiet = senderBanned || Store.isBan(effect)
                || item.decision.reason.hasPrefix(CommsWatch.bannedPrefix)
            if commsNotify && commsEnabled && fresh.contains(item.message.id) && !quiet {
                Notifier.post(item.message)
            }
        }
        if !result.items.isEmpty || !stranded.isEmpty { refreshAudit() }
        refreshComms()
    }

    struct CommsActResult {
        var verb: String
        var outcome: String
        var queueKey: String?
        var label: String?
        /// Nil when the activity line is already written: the daemon logs the bans it records.
        var feedLine: String?
        /// The agent already doing the asked-for work; the ping records its start.
        var running: AgentState.RunRecord?
    }

    private static func pingLine(_ m: CommsMessage) -> String {
        "\(m.place) from \(m.sender): \(Store.snippet(m.text))"
    }

    static let commsSenderBannedOutcome =
        "the sender is banned for prompt injection, so nothing was done"

    /// Starts the outcome of a ping whose sender it got banned.
    static let commsBannedOutcomePrefix = "banned "

    /// The `reportedBy` of a ban Comms records.
    static let commsReporter = "Comms triage"

    private static func isBan(_ effect: CommsEffect) -> Bool {
        if case .ban = effect { return true }
        return false
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
            // The sender is the ask's ban dimension: the dispatch gate drops it if they
            // are banned while it waits.
            return queue(RequestedWork(number: cfg.number, url: cfg.permalink,
                                       author: cfg.banLogin, config: .investigate(cfg)),
                         verb: "comms-investigate", who: who, front: false)
        case .ban(let b):
            let reporter = Store.commsReporter
            let write = await Task.detached(priority: .userInitiated) {
                BanList.ban(b, reportedBy: reporter)
            }.value
            refreshBanList()
            let name = b.handle.isEmpty ? b.userID : "@\(b.handle)"
            switch write {
            case .daemon, .file:
                let outcome = Store.commsBannedOutcomePrefix + "\(name) for prompt injection: "
                    + "\"\(Store.snippet(b.evidence, max: 120))\""
                return CommsActResult(verb: "ban", outcome: outcome,
                                      feedLine: write == .daemon ? nil : BanList.slackBanLine(b))
            case .failed(let why):
                return refused("comms-ban", who, "could not ban \(name) for prompt injection "
                               + "(\(Store.snippet(why, max: 120)))")
            }
        }
    }

    /// Work already running, or already waiting under another key, is not asked for
    /// twice: the ping takes that work's key and label, so its started and finished
    /// follow the agent that does it.
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
        // A paused monitor's find too: once a ping owns it, it drains like an ask
        // (`isHeld`) and stays queued until it runs (`commsKeptAdopted`).
        if let twin = startingTasks.first(where: { twins.contains($0.id) })
            ?? queuedTasks.first(where: { twins.contains($0.id) }) {
            commsAdopted[twin.id] = twin
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

    /// Monitor keys for the same work as the ask `key`: a review requested of me on
    /// GitHub is the review a Slack message asks for.
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

    /// Any review agent on that PR (the dispatch gate treats every agent on a PR as that
    /// PR's), or a fix of that issue, known only by the label every issue fix wears
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
        case .none, .injection:  return nil
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

    enum CommsLookup {
        case found(CommsTarget)
        /// GitHub says the repo has no such PR or issue.
        case missing
        /// No answer: gh failed, timed out or printed no PR or issue.
        case unasked(String)
    }

    /// `gh pr|issue view`'s "no such number". "Could not resolve to a Repository" shares
    /// the prefix but means the repo itself could not be seen.
    static func ghSaysNotFound(_ error: Error) -> Bool {
        guard case GHError.process(_, let stderr) = error else { return false }
        return stderr.contains("Could not resolve to") && stderr.contains("with the number of")
    }

    nonisolated static func snippet(_ text: String, max: Int = 80) -> String {
        let flat = text.split(whereSeparator: \.isNewline).joined(separator: " ")
        return flat.count > max ? String(flat.prefix(max - 1)) + "…" : flat
    }

    /// The panel's last fetch (open work only) first, then `gh` for what it lacks: an
    /// issue a filtered list left out, a PR opened since, a closed one.
    ///
    /// `gh issue view` answers a PR's number with that PR, so only an `/issues/` url is
    /// an issue.
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

    /// Logs only transitions: a bad token fails every poll, and a line per poll would bury
    /// the feed.
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

    /// Records `started` on the pings owning a dispatched queued task; a no-op for one no
    /// ping owns.
    ///
    /// A dispatch the ban gate refused leaves the queue for good (`settleRequested`): its
    /// pings say so, and who is banned.
    func commsSettled(_ entry: QueuedAgentTask, _ outcome: DispatchOutcome) {
        guard outcome.wasHandled || outcome == .banned else { return }
        // Owners are found in the fold, so a read in flight is waited for.
        if let reading = commsLogRead {
            Task { [weak self] in
                await reading.value
                self?.commsSettled(entry, outcome)
            }
            return
        }
        let owners = commsItems.filter { $0.queueKey == entry.id && Store.commsUnstarted($0) }
        guard !owners.isEmpty else { return }
        if outcome == .banned {
            let who = BanList.refusedWho(entry.job.authorLogin ?? "")
            let why = Store.commsLeftQueueOutcome + " (\(who) \(Store.commsBanRefusal))"
            recordComms(owners.filter { !Store.commsLeftQueue($0) }.map {
                CommsEvent(kind: .action, id: $0.message.id, outcome: why)
            })
            return
        }
        let label = AgentDispatchGate.label(source: .auto, core: entry.job.label,
                                            attemptNumber: entry.attemptNumber,
                                            requested: entry.job.requested)
        recordComms(owners.flatMap { Store.commsStart($0, label: label) })
    }

    /// Run after every agent-row publish. `neverRan` is this tick's FAILED runs, label to
    /// reason.
    ///
    /// A ping that took a monitor's queued work (`queue`) records `started` once an agent
    /// row is doing it: a find its monitor dispatches itself never reaches `commsSettled`.
    ///
    /// A started run whose label no agent row wears any more, and which is not waiting to
    /// run again, has finished: its pings record `finished`, once. One the tick calls
    /// FAILED never ran instead.
    func noteCommsRuns(neverRan: [String: String] = [:]) {
        if let reading = commsLogRead {
            guard !neverRan.isEmpty else { return }
            Task { [weak self] in
                await reading.value
                self?.noteCommsRuns(neverRan: neverRan)
            }
            return
        }
        let waiting = Set(queuedTasks.map(\.id) + startingTasks.map(\.id))
        var lines: [CommsEvent] = []
        for item in commsItems where Store.commsUnstarted(item) {
            guard let key = item.queueKey, !waiting.contains(key),
                  let verb = Store.commsKeyParts(key)?.verb,
                  !AgentTaskQueue.requestedActions.contains(verb),
                  let row = commsRow(doing: key) else { continue }
            lines += Store.commsStart(item, label: row.record.label,
                                      at: Date(timeIntervalSince1970: row.record.dispatchedAt))
        }
        let running = Set(agentRows.map(\.record.label))
        for item in commsItems where item.startedAt != nil && item.finishedAt == nil
            && !Store.commsNeverStarted(item) && !waiting.contains(item.queueKey ?? "") {
            let label = item.label ?? ""
            if let why = neverRan[label] {
                lines.append(CommsEvent(kind: .action, id: item.message.id,
                                        outcome: Store.commsNeverStartedOutcome
                                            + " (\(Store.snippet(why, max: 120)))"))
            } else if !running.contains(label) {
                lines.append(CommsEvent(kind: .finished, id: item.message.id, label: item.label))
            }
        }
        if !lines.isEmpty { recordComms(lines) }
    }

    /// A ping whose last run never started is waiting for one again: what retries it
    /// (the ask's next offer, the monitor's next find) is a new run.
    static func commsUnstarted(_ item: CommsItem) -> Bool {
        item.finishedAt == nil && (item.startedAt == nil || commsNeverStarted(item))
    }

    private static func commsStart(_ item: CommsItem, label: String,
                                   at: Date = Date()) -> [CommsEvent] {
        let start = CommsEvent(at: at, kind: .started, id: item.message.id, label: label)
        guard commsNeverStarted(item) || commsLeftQueue(item) else { return [start] }
        return [CommsEvent(kind: .action, id: item.message.id, outcome: commsRetriedOutcome),
                start]
    }

    static let commsRetriedOutcome = "started on a later attempt"

    /// The queue keys a ping is still waiting on.
    var commsOwnedKeys: Set<String> {
        Set(commsItems.filter { Store.commsUnstarted($0) && !Store.commsLeftQueue($0) }
            .compactMap(\.queueKey))
    }

    /// The monitor finds pings adopted (`queue`) that a commit's offers left out: each
    /// stays queued until it runs, its monitor no longer offering it (a withdrawn request,
    /// a failed spawn it has not found again). One adopted before this launch has no task
    /// here to put back, so its ping says it left the queue rather than read "queued"
    /// forever.
    func commsKeptAdopted(offered: [QueuedAgentTask]) -> [QueuedAgentTask] {
        let owned = commsOwnedKeys
        commsAdopted = commsAdopted.filter { owned.contains($0.key) }
        for task in offered where commsAdopted[task.id] != nil { commsAdopted[task.id] = task }
        let present = Set(offered.map(\.id) + startingTasks.map(\.id))
        var kept: [QueuedAgentTask] = []
        var lines: [CommsEvent] = []
        for item in commsItems where Store.commsUnstarted(item) && !Store.commsLeftQueue(item) {
            guard let key = item.queueKey, !present.contains(key),
                  let verb = Store.commsKeyParts(key)?.verb,
                  !AgentTaskQueue.requestedActions.contains(verb),
                  commsRow(doing: key) == nil else { continue }
            if let task = commsAdopted[key] {
                if !kept.contains(where: { $0.id == key }) { kept.append(task) }
            } else {
                lines.append(CommsEvent(kind: .action, id: item.message.id,
                                        outcome: Store.commsLeftQueueOutcome + " (\(key))"))
            }
        }
        if !lines.isEmpty { recordComms(lines) }
        return kept
    }

    static let commsLeftQueueOutcome = "its queued task left the queue before it ran"

    static func commsLeftQueue(_ item: CommsItem) -> Bool {
        item.outcome?.hasPrefix(commsLeftQueueOutcome) == true
    }

    static let commsBanRefusal = "is banned for prompt injection"

    /// Its queued task left the queue because the dispatch gate found a ban (`commsSettled`).
    static func commsStoppedByBan(_ item: CommsItem) -> Bool {
        commsLeftQueue(item) && item.outcome?.hasSuffix(commsBanRefusal + ")") == true
    }

    /// A ping may already have taken this failed run's row as its start (`noteCommsRuns`,
    /// `queue`'s "already running"): it says the run never ran, or the `finished` its
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

    /// Its run failed to spawn (`commsSpawnFailed`): there is none to finish.
    static func commsNeverStarted(_ item: CommsItem) -> Bool {
        item.outcome?.hasPrefix(commsNeverStartedOutcome) == true
    }
}

/// Where a read of `events.jsonl` stopped; `offset` is just past its last whole line.
struct CommsLogMark: Equatable, Sendable {
    var inode: UInt64
    var offset: UInt64
    var mtime: CommsLogReader.Time
}

struct CommsLogCache {
    var events: [CommsEvent] = []
    var mark: CommsLogMark?
}

/// `CommsLog.read`, from an offset.
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

    /// A last line still being written is left for the next read.
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

/// Read on each use, never cached or written: `DIPLOMAT_SLACK_TOKEN` (tests, a headless
/// poll), else the Keychain item Settings names, through `security` so an unsigned build
/// raises no Keychain prompt from a background tick.
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

    static func addCommand(item: String) -> String {
        "security add-generic-password -a \"$USER\" -s \(item) -w"
    }
}

/// System notifications: a Comms ping, and every new prompt-injection ban.
///
/// Only from a real .app and never headless: `UNUserNotificationCenter` aborts a process
/// with no bundle, which is what `swift run` is.
enum Notifier {
    static var available: Bool {
        !Headless.active && Bundle.main.bundleIdentifier != nil
            && Bundle.main.bundleURL.pathExtension == "app"
    }

    /// The self-tests' stand-in for the notification center: (identifier, title, body).
    /// Consulted only headless.
    static var sink: ((String, String, String) -> Void)?

    /// Not at launch: only once Comms is on with notifications wanted, or a ban needs
    /// telling (`postBan`).
    static func authorize() {
        guard available else { return }
        let center = UNUserNotificationCenter.current()
        center.delegate = Presenter.shared
        center.requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    static func post(_ m: CommsMessage) {
        add(id: "comms-\(m.id)", title: "Slack · \(m.place) from \(m.sender)",
            body: Store.snippet(m.text, max: 160))
    }

    /// Asks for authorization first, which prompts only the first time: a ban is worth
    /// a notification whether or not Comms ever asked.
    static func postBan(_ ban: BannedAuthor) {
        let who = ban.isSlack ? "Slack user \(ban.displayName)" : ban.displayName
        var parts: [String] = []
        if let pr = ban.pr, !pr.isEmpty { parts.append(ban.isSlack ? "in a Slack message" : "on \(pr)") }
        let evidence = (ban.evidence ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if !evidence.isEmpty { parts.append("\"\(Store.snippet(evidence, max: 140))\"") }
        if let by = ban.reportedBy, !by.isEmpty { parts.append("reported by \(by)") }
        let id = "ban-\(ban.login)-\(ban.at ?? "")"
        let title = "Banned \(who) for prompt injection"
        let body = parts.joined(separator: " · ")
        guard available else { return add(id: id, title: title, body: body) }
        let center = UNUserNotificationCenter.current()
        center.delegate = Presenter.shared
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            guard granted else { return }
            add(id: id, title: title, body: body)
        }
    }

    private static func add(id: String, title: String, body: String) {
        if Headless.active, let sink {
            sink(id, title, body)
            return
        }
        guard available else { return }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        let center = UNUserNotificationCenter.current()
        center.delegate = Presenter.shared
        center.add(UNNotificationRequest(identifier: id, content: content, trigger: nil))
    }

    /// Shows notifications while the popover is open: an accessory app is active exactly
    /// then, and macOS drops an active app's notifications by default.
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

/// Kept out of `DiplomatApp.swift`, whose `env[...]` reads CI takes for the list of
/// headless modes.
extension Dump {
    /// `DIPLOMAT_COMMS_POLL=1`: one real tick, printing each ping's would-be effect. Without
    /// `DIPLOMAT_COMMS_DIR` it ticks a scratch dir whose watermark is `commsPollLookback`
    /// ago, so it triages that window whether or not Comms is on and leaves the live
    /// watermark alone. False when there is no token or the tick failed.
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
        case .ban(let b):
            return "ban \(b.handle) (\(b.userID)) for prompt injection: "
                + "\"\(Store.snippet(b.evidence))\""
        }
    }
}
