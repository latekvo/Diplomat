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
    /// plus one per keyword.
    static var commsPollInterval: TimeInterval {
        let model = try? CoreAssets.comms()
        let floor = Double(model?.minPollSecs ?? 20)
        let secs = ProcessInfo.processInfo.environment["DIPLOMAT_COMMS_SECS"].flatMap(Double.init)
        return max(floor, secs ?? Double(model?.pollSecs ?? 60))
    }

    /// Stand in for GitHub in the Comms self-test, which must answer "is #N open"
    /// without a `gh` call. Headless only.
    static var commsLookupOverride: (@MainActor (CommsTargetKind, Int) async -> CommsTarget?)?

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

    func refreshComms() {
        let next = CommsLog.fold(CommsLog.read())
        if next != commsItems { commsItems = next }
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
    func applyComms(_ result: CommsTickResult) async {
        if let me = result.me, me != commsIdentity { commsIdentity = me }
        noteCommsHealth(result.error)
        let toggles = commsToggles
        let settings = commsSettings
        let reports = CommsLog.reportsDir()
        for item in result.items {
            let effect = CommsPlan.effect(for: item.decision, message: item.message,
                                          toggles: toggles, settings: settings,
                                          reportDir: reports)
            let act = await commsAct(effect, item)
            CommsLog.append(CommsEvent(kind: .action, id: item.message.id, outcome: act.outcome,
                                       queueKey: act.queueKey, label: act.label))
            AuditLog.log("auto", act.verb, act.feedLine)
            if commsNotify { CommsNotifier.post(item.message) }
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
    }

    private func commsAct(_ effect: CommsEffect, _ item: CommsTickResult.Item) async -> CommsActResult {
        let m = item.message
        let who = "\(m.sender) (\(m.place))"
        switch effect {
        case .none:
            let off = Store.switchedOff(item.decision.action)
            return CommsActResult(verb: "comms-ping", outcome: off ?? "ping only",
                                  feedLine: "\(m.place) from \(m.sender): \(Store.snippet(m.text))")
        case .review(let n):
            let found = await commsLookup(.pr, n)
            guard let pr = found, pr.isOpen else {
                return refused("comms-review", who, Store.notOpen("PR", n, found))
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
            let found = await commsLookup(.issue, n)
            guard let issue = found, issue.isOpen else {
                return refused("comms-fix", who, Store.notOpen("issue", n, found))
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
    private func queue(_ ask: RequestedWork, verb: String, who: String,
                       front: Bool) -> CommsActResult {
        let fresh = requestOne(ask)
        if front { moveQueuedTaskToFront(ask.key) }
        let outcome = (fresh ? "queued" : "already queued")
            + (front ? ", at the front of its band" : "")
        return CommsActResult(verb: verb, outcome: outcome, queueKey: ask.key, label: ask.label,
                              feedLine: "\(ask.label) asked by \(who) — \(outcome)")
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

    private static func notOpen(_ noun: String, _ n: Int, _ found: CommsTarget?) -> String {
        guard let found else { return "no \(noun) #\(n) in this repo, so nothing was queued" }
        return "\(noun) #\(n) is \(found.state.lowercased()), so nothing was queued"
    }

    nonisolated static func snippet(_ text: String, max: Int = 80) -> String {
        let flat = text.split(whereSeparator: \.isNewline).joined(separator: " ")
        return flat.count > max ? String(flat.prefix(max - 1)) + "…" : flat
    }

    /// The PR or issue `n`, open or not: the panel's own last fetch first, which lists
    /// open work only, then `gh` for what it does not hold — an issue a filtered list
    /// left out, a PR opened since the last refresh, or one already closed.
    private func commsLookup(_ kind: CommsTargetKind, _ n: Int) async -> CommsTarget? {
        if Headless.active, let stub = Store.commsLookupOverride { return await stub(kind, n) }
        switch kind {
        case .pr:
            if let pr = prs.first(where: { $0.number == n }) {
                return CommsTarget(number: n, url: pr.url, author: pr.author, isOpen: true, state: "OPEN")
            }
        case .issue:
            if let issue = issues.first(where: { $0.number == n }) {
                return CommsTarget(number: n, url: issue.url, author: issue.author, isOpen: true,
                                   state: "OPEN")
            }
        }
        let (owner, repo) = CoreAssets.repoCoordinates()
        let verb = kind == .pr ? "pr" : "issue"
        guard let data = try? await GH.run([verb, "view", String(n), "--repo", "\(owner)/\(repo)",
                                            "--json", "number,url,author,state"]),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let state = obj["state"] as? String else { return nil }
        let author = (obj["author"] as? [String: Any])?["login"] as? String ?? ""
        return CommsTarget(number: n, url: obj["url"] as? String ?? "", author: author,
                           isOpen: state == "OPEN", state: state)
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
        let owners = commsItems.filter { $0.queueKey == entry.id && $0.startedAt == nil }
        guard !owners.isEmpty else { return }
        for item in owners {
            CommsLog.append(CommsEvent(kind: .started, id: item.message.id, label: entry.job.label))
        }
        refreshComms()
    }

    /// A started Comms run whose label no agent row wears any more, and which is not
    /// waiting to run again, has finished: its pings record `finished`, once. Run after
    /// every publish of the agent rows.
    func noteCommsRunsEnded() {
        let open = commsItems.filter { $0.startedAt != nil && $0.finishedAt == nil }
        guard !open.isEmpty else { return }
        let running = Set(agentRows.map(\.record.label))
        let waiting = Set(queuedTasks.map(\.id) + startingTasks.map(\.id))
        let ended = open.filter {
            !running.contains($0.label ?? "") && !waiting.contains($0.queueKey ?? "")
        }
        guard !ended.isEmpty else { return }
        for item in ended {
            CommsLog.append(CommsEvent(kind: .finished, id: item.message.id, label: item.label))
        }
        refreshComms()
    }
}

/// Where the Slack token comes from. Read on each use, never cached and never written:
/// `DIPLOMAT_SLACK_TOKEN` (tests, a headless poll), else the login-Keychain item
/// Settings names, read through `security` so an unsigned build raises no Keychain
/// prompt from a background tick (`Quota`'s reasoning).
enum CommsToken {
    static let timeout: TimeInterval = 10

    static func read(item: String) -> String? {
        if let env = ProcessInfo.processInfo.environment["DIPLOMAT_SLACK_TOKEN"], !env.isEmpty {
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
    /// each would have had. Run against a scratch copy of the watermark unless
    /// `DIPLOMAT_COMMS_DIR` names one, so the live app's next tick still sees these
    /// messages as new. False when there is no token to poll with.
    @MainActor
    static func commsPoll() async -> Bool {
        let env = ProcessInfo.processInfo.environment
        if (env["DIPLOMAT_COMMS_DIR"] ?? "").isEmpty {
            let real = CommsLog.dir
            let scratch = FileManager.default.temporaryDirectory
                .appendingPathComponent("diplomat-commspoll-\(getpid())")
            try? FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)
            try? FileManager.default.copyItem(at: CommsState.url(in: real),
                                              to: CommsState.url(in: scratch))
            setenv("DIPLOMAT_COMMS_DIR", scratch.path, 1)
        }
        let dir = CommsLog.dir
        let item = AppConfig.slackTokenKeychainItem
        let fromEnv = !(env["DIPLOMAT_SLACK_TOKEN"] ?? "").isEmpty
        print("== comms poll (dry run) ==")
        print("dir:   \(dir.path)")
        print("token: " + (fromEnv ? "DIPLOMAT_SLACK_TOKEN" : "Keychain item \"\(item)\""))
        let token = await Task.detached { CommsToken.read(item: item) }.value
        guard let token else {
            print(CommsToken.missingMessage(item: item))
            return false
        }
        let store = Store()
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
        return true
    }

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
