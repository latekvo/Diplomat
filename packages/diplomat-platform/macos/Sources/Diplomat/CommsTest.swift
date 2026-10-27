import Foundation
import DiplomatCore

/// Headless self-test for what the app does with a Comms tick, driven by
/// `DIPLOMAT_COMMS_TEST=1`.
///
/// Which messages are pings and what an answer asks for are the core's, and pinned in
/// `DiplomatCoreSmoke`. What this covers is the half only the app has: each effect turned
/// into queue work (or deliberately not), where in the queue it lands, what the log, the
/// feed and the screen are told, and the `started`/`finished` lines a queued ask earns.
/// The tick is composed here and handed to `applyComms`, the same function a live poll
/// hands Slack's answer to, so no Slack call, no `claude` and no `gh` is made: GitHub's
/// answer about each PR and issue is `Store.commsLookupOverride`.
///
/// Spawns nothing: nothing in `applyComms` dispatches, and the queue it fills is drained
/// only by the poll a headless Store never starts. The feed, Comms' own log, the run book
/// and the shared config are pointed at scratch directories first.
enum CommsTest {
    @discardableResult
    @MainActor
    static func run() async -> Bool {
        var pass = true
        func check(_ name: String, _ ok: Bool) {
            print("\(ok ? "PASS" : "FAIL") — \(name)")
            if !ok { pass = false }
        }

        let scratch = FileManager.default.temporaryDirectory
            .appendingPathComponent("diplomat-commstest-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: scratch) }
        setenv("DIPLOMAT_AUDIT_DIR", scratch.appendingPathComponent("audit").path, 1)
        setenv("DIPLOMAT_COMMS_DIR", scratch.appendingPathComponent("comms").path, 1)
        setenv("DIPLOMAT_AGENTS_DIR", scratch.appendingPathComponent("agents").path, 1)
        setenv("DIPLOMAT_CONFIG", scratch.appendingPathComponent("config.json").path, 1)
        setenv("DIPLOMAT_QUOTA_PROBE", "0", 1)
        setenv(DiskFloor.envOverride, "0", 1)
        AgentProbes.pinDump(.present(""))
        defer { AgentProbes.pinDump(nil) }

        // GitHub, as far as this test is concerned. Numbers far above the real repo's,
        // so no saved arrangement of the operator's names any of them.
        let targets: [Int: CommsTarget] = [
            90010: CommsTarget(number: 90010, url: "https://github.com/o/r/pull/90010",
                               author: "alice", isOpen: true, state: "OPEN"),
            90011: CommsTarget(number: 90011, url: "https://github.com/o/r/pull/90011",
                               author: "latekvo", isOpen: true, state: "OPEN"),
            90020: CommsTarget(number: 90020, url: "https://github.com/o/r/issues/90020",
                               author: "bob", isOpen: true, state: "OPEN"),
            90030: CommsTarget(number: 90030, url: "https://github.com/o/r/pull/90030",
                               author: "Mallory", isOpen: true, state: "OPEN"),
            90031: CommsTarget(number: 90031, url: "https://github.com/o/r/issues/90031",
                               author: "mallory", isOpen: true, state: "OPEN"),
            90098: CommsTarget(number: 90098, url: "https://github.com/o/r/pull/90098",
                               author: "carol", isOpen: false, state: "MERGED"),
        ]
        var lookups: [Int] = []
        Store.commsLookupOverride = { _, n in
            lookups.append(n)
            return targets[n]
        }
        defer { Store.commsLookupOverride = nil }

        let store = Store()
        // A headless Store still READS the operator's settings and their stored asks:
        // pin every one an assertion below depends on. Nothing here is persisted.
        store.requestedWork = []
        store.queuedTasks = []
        store.usernameOverride = "latekvo"
        store.commsActRequests = true
        store.commsActBumps = true
        store.commsActInvestigations = true
        store.commsNotify = false
        store.commsKeywordsRaw = ""
        store.commsSenderAllowlistRaw = ""
        store.commsModel = ""
        store.commsSeenAt = Date().addingTimeInterval(-3600)
        store.bannedAuthors = [BannedAuthor(login: "mallory")]
        store.refreshComms()

        var serial = 0
        let base = CommsTS.number(Date())
        func item(_ action: CommsAction, _ number: Int, summary: String = "",
                  text: String = "") -> CommsTickResult.Item {
            serial += 1
            let ts = CommsTS.string(base + serial)
            let m = CommsMessage(id: "D0ALICE:\(ts)", channelID: "D0ALICE", channelName: "U0ALICE",
                                 isDM: true, userID: "U0ALICE", userName: "alice",
                                 teamID: "T0", text: text.isEmpty ? "msg \(serial)" : text, ts: ts,
                                 permalink: "https://x.slack.com/archives/D0ALICE/p\(base + serial)",
                                 reasons: [.dm], keyword: nil)
            return .init(message: m, decision: CommsDecision(action: action, number: number,
                                                             summary: summary, reason: "r"))
        }
        // What `CommsWatch.tick` writes before it returns: each ping, then its triage.
        func apply(_ items: [CommsTickResult.Item], error: String? = nil) async {
            for it in items {
                CommsLog.append(CommsEvent(kind: .ping, id: it.message.id, message: it.message))
                CommsLog.append(CommsEvent(kind: .decision, id: it.message.id, decision: it.decision))
            }
            await store.applyComms(CommsTickResult(
                me: CommsIdentity(userID: "U0ME", teamID: "T0", handle: "latekvo"),
                items: items, error: error))
        }
        func ids() -> [String] { store.queuedTasks.map(\.id) }
        func band(_ b: Int) -> [String] { ids().filter { AgentTaskQueue.band($0) == b } }
        func outcome(_ it: CommsTickResult.Item) -> String {
            store.commsItems.first { $0.message.id == it.message.id }?.outcome ?? "<none>"
        }
        func folded(_ it: CommsTickResult.Item) -> CommsItem? {
            store.commsItems.first { $0.message.id == it.message.id }
        }
        func feed(_ verb: String) -> [AuditEntry] { AuditLog.read().filter { $0.action == verb } }
        func review(_ n: Int) -> Store.RequestedWork {
            Store.RequestedWork(number: n, url: "https://github.com/o/r/pull/\(n)", author: "",
                          config: .review(ReviewConfig(target: .specific, me: "latekvo",
                                                       specificPR: String(n))))
        }
        func fix(_ n: Int) -> Store.RequestedWork {
            Store.RequestedWork(number: n, url: "https://github.com/o/r/issues/\(n)", author: "",
                          config: .issues(IssueConfig(target: .specific, me: "latekvo",
                                                      specificIssue: String(n))))
        }

        // Work already waiting, as a sweep queues it: what "the front of its band" is
        // measured against.
        store.requestOne(review(90001))
        store.requestOne(review(90002))
        store.requestOne(fix(90004))
        check("the fixture queues two reviews and a fix", ids() == ["review:90001", "review:90002",
                                                                   "issues:90004"])

        // MARK: review
        let askReview = item(.review, 90010, summary: "Review the server PR")
        await apply([askReview])
        check("a review ask lands first in the review band, ahead of a sweep's",
              band(1) == ["review:90010", "review:90001", "review:90002"])
        check("…as a stored ask, so a poll re-offers it",
              store.requestedWork.contains { $0.key == "review:90010" })
        let reviewAsk = store.requestedWork.first { $0.key == "review:90010" }
        check("…carrying someone else's PR author as its ban dimension",
              reviewAsk?.author == "alice")
        check("…and its outcome, key and label are what the screen folds",
              folded(askReview)?.outcome == "queued, at the front of its band"
                && folded(askReview)?.queueKey == "review:90010"
                && folded(askReview)?.label == reviewAsk?.label)
        check("…in one activity line", feed("comms-review").count == 1)

        let askMine = item(.review, 90011)
        await apply([askMine])
        check("a review of my own PR carries no author to ban-check",
              store.requestedWork.first { $0.key == "review:90011" }?.author == "")

        let again = item(.review, 90010)
        await apply([again])
        check("asking again for queued work leaves one row",
              ids().filter { $0 == "review:90010" }.count == 1
                && outcome(again) == "already queued, at the front of its band")

        // MARK: fix
        let askFix = item(.fixIssue, 90020)
        await apply([askFix])
        check("a fix ask lands first in the issue band",
              band(2) == ["issues:90020", "issues:90004"])
        check("…behind every review", ids().firstIndex(of: "issues:90020")!
                > ids().firstIndex(of: "review:90002")!)
        check("…carrying the issue's author", store.requestedWork.first {
            $0.key == "issues:90020" }?.author == "bob")

        // MARK: bump
        store.requestOne(review(90003))
        store.requestOne(review(190003))
        store.requestOne(fix(90003))
        let beforeBump = ids()
        let askBump = item(.bump, 90003)
        await apply([askBump])
        check("a bump moves each queued item for that number to the front of its band",
              band(1).first == "review:90003" && band(2).first == "issues:90003")
        check("…and nothing whose number merely ends in it", band(1).last == "review:190003")
        check("…adding no work of its own", Set(ids()) == Set(beforeBump)
                && folded(askBump)?.queueKey == nil)
        let askBumpNothing = item(.bump, 90077)
        let beforeIdle = ids()
        await apply([askBumpNothing])
        check("a bump of nothing queued changes nothing and says so",
              ids() == beforeIdle && outcome(askBumpNothing) == "nothing queued for #90077")

        // MARK: investigate
        let askInvestigate = item(.investigate, 0, summary: "iOS build crashes on launch",
                                  text: "the iOS build crashes on launch")
        await apply([askInvestigate])
        let invKey = "investigate:\(CommsTS.number(askInvestigate.message.ts)!)"
        check("an investigation lands in its own band, after the fixes",
              band(3) == [invKey] && ids().last == invKey)
        let stored = Store.decodeRequestedWork(try? JSONEncoder().encode(store.requestedWork))
        let reloaded = stored.first { $0.key == invKey }
        let live = store.requestedWork.first { $0.key == invKey }
        check("…and survives the encode a restart reads back",
              reloaded != nil && reloaded?.label == live?.label
                && reloaded?.config.buildPrompt() == live?.config.buildPrompt())
        if case .investigate(let cfg)? = reloaded?.config {
            check("…as an investigation of that message",
                  cfg.messageID == askInvestigate.message.id
                    && cfg.summary == "iOS build crashes on launch"
                    && reloaded?.label.hasPrefix("Investigate · Slack · alice") == true)
        } else {
            check("…as an investigation of that message", false)
        }

        // MARK: refusals
        lookups = []
        let snapshot = ids()
        let askGone = item(.review, 90099)
        let askMerged = item(.review, 90098)
        let askBanned = item(.review, 90030)
        let askBannedIssue = item(.fixIssue, 90031)
        await apply([askGone, askMerged, askBanned, askBannedIssue])
        check("each named target was asked about", lookups == [90099, 90098, 90030, 90031])
        check("an unknown PR, a merged PR and a banned author's PR or issue queue nothing",
              ids() == snapshot)
        check("…an unknown PR says so", outcome(askGone) == "no PR #90099 in this repo, so nothing was queued")
        check("…a merged one says so", outcome(askMerged) == "PR #90098 is merged, so nothing was queued")
        check("…a banned author's says so, whatever the case of the login",
              outcome(askBanned) == "@Mallory is banned, so #90030 was not queued"
                && outcome(askBannedIssue) == "@mallory is banned, so #90031 was not queued")
        check("…and none of the four left a queue key", [askGone, askMerged, askBanned,
                                                          askBannedIssue].allSatisfy {
            folded($0)?.queueKey == nil })

        // MARK: switched off
        store.commsActRequests = false
        store.commsActBumps = false
        store.commsActInvestigations = false
        let offReview = item(.review, 90010)
        let offFix = item(.fixIssue, 90020)
        let offBump = item(.bump, 90002)
        let offInvestigate = item(.investigate, 0, summary: "another crash")
        let pingsBefore = feed("comms-ping").count
        lookups = []
        let offSnapshot = ids()
        await apply([offReview, offFix, offBump, offInvestigate])
        check("with every action switched off, the queue is untouched, order included",
              ids() == offSnapshot)
        check("…GitHub is not even asked", lookups.isEmpty)
        check("…each is still a ping, with its own activity line",
              feed("comms-ping").count == pingsBefore + 4)
        check("…saying which switch held it",
              outcome(offReview) == "review and fix asks are switched off, so ping only"
                && outcome(offFix) == "review and fix asks are switched off, so ping only"
                && outcome(offBump) == "bumps are switched off, so ping only"
                && outcome(offInvestigate) == "investigations are switched off, so ping only")
        store.commsActRequests = true
        store.commsActBumps = true
        store.commsActInvestigations = true

        // MARK: one line per ping
        let actions = CommsLog.read().filter { $0.kind == .action }
        let commsLines = AuditLog.read(limit: 1000).filter { $0.action.hasPrefix("comms-") }
        check("one action event and one activity line per ping applied",
              actions.count == serial && commsLines.count == serial)
        check("every ping is unread until the screen is opened", store.commsUnread == serial)
        store.markCommsSeen()
        check("…and none after", store.commsUnread == 0)

        // MARK: health
        await apply([], error: "invalid_auth")
        await apply([], error: "invalid_auth")
        check("a failing poll is published and logged once, not once per tick",
              store.commsError == "invalid_auth" && feed("comms-error").count == 1)
        await apply([])
        check("…and its recovery once", store.commsError == nil
                && feed("comms-recovered").count == 1)

        // MARK: started / finished
        guard let entry = store.queuedTasks.first(where: { $0.id == "review:90010" }) else {
            check("the review ask is still queued for the dispatch half", false)
            print(pass ? "COMMS TEST OK" : "COMMS TEST FAILED")
            return pass
        }
        store.commsSettled(entry, .atCapacity)
        store.commsSettled(entry, .banned)
        check("a dispatch that started nothing records no start", folded(askReview)?.startedAt == nil)
        store.commsSettled(entry, .spawned(terminal: "iterm"))
        store.commsSettled(entry, .spawned(terminal: "iterm"))
        let starts = CommsLog.read().filter { $0.kind == .started }
        check("a spawn records one start for every ping that asked for it",
              Set(starts.map(\.id)) == [askReview.message.id, again.message.id]
                && starts.count == 2 && folded(askReview)?.startedAt != nil)
        check("…under the label the run wears", starts.allSatisfy { $0.label == entry.job.label })

        store.noteCommsRunsEnded()
        check("a run still in the queue has not finished", folded(askReview)?.finishedAt == nil)
        store.queuedTasks.removeAll { $0.id == "review:90010" }
        let record = AgentState.RunRecord(runID: "r1", dispatchedAt: Date().timeIntervalSince1970,
                                          prNumber: 90010, prURL: entry.job.prURL ?? "",
                                          kind: "review", label: entry.job.label,
                                          source: AgentDispatchGate.Source.auto.rawValue)
        store.pinAgentRows([Store.AgentRow(record: record, state: .running, reason: "", window: nil)])
        store.noteCommsRunsEnded()
        check("a run an agent row still wears has not finished", folded(askReview)?.finishedAt == nil)

        let quiet = AgentState.Evidence(processes: .present([:]), sentinels: .present([]),
                                        tails: .present([:]), claims: .present([]),
                                        mergedPRs: .present([]), liveAgents: .present([:]),
                                        sessions: .present([:]), activity: .present([:]))
        let tick = AgentState.tick(records: [], evidence: quiet, now: Date().timeIntervalSince1970,
                                   limit: 4)
        store.publishForSelfTest(Store.AgentPass(tick: tick, windows: [:], attached: .present([])))
        let finishes = CommsLog.read().filter { $0.kind == .finished }
        check("the publish that drops its row records it finished, once per ping",
              finishes.count == 2 && folded(askReview)?.finishedAt != nil)
        store.publishForSelfTest(Store.AgentPass(tick: tick, windows: [:], attached: .present([])))
        check("…and a later publish records nothing more",
              CommsLog.read().filter { $0.kind == .finished }.count == 2)

        print(pass ? "COMMS TEST OK" : "COMMS TEST FAILED")
        return pass
    }
}
