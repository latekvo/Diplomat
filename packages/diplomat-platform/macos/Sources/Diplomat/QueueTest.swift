import Foundation
import DiplomatCore

/// Headless self-test for the queue behind the automatic-task cap, driven by
/// `DIPLOMAT_QUEUE_TEST=1`.
///
/// The *ordering* rules are pure and pinned in `DiplomatCoreSmoke`
/// (`AgentTaskQueue`); what this covers is the wiring around them, which is where
/// a queue silently stops being one: that a refusal is captured rather than
/// dropped, that one unit offered twice in a cycle is one task, that the operator's
/// arrangement survives the poll that rebuilds the list, that work GitHub stops
/// owing falls out of it, and that a switched-off monitor's work is held rather
/// than either run or dropped.
///
/// It reaches the at-capacity branch honestly — a real `dispatchAgent` call against
/// a real cap — so it never spawns an agent, needs no `gh` auth and no terminal
/// automation, and can run on a CI runner:
///
///   DIPLOMAT_QUEUE_TEST=1 swift run Diplomat
///
/// It does read `ps` and the ban list, and the refusal it provokes writes an
/// `at-capacity` line — so it points `DIPLOMAT_AUDIT_DIR` at a scratch directory
/// first. `Headless.active` covers UserDefaults, not the shared feed, and a
/// self-test has no business in the operator's activity log.
enum QueueTest {
    /// Returns overall pass/fail so the launcher can exit non-zero — a FAIL that
    /// still exits 0 can't gate anything.
    @discardableResult
    @MainActor
    static func run() async -> Bool {
        var pass = true
        func check(_ name: String, _ ok: Bool) {
            print("\(ok ? "PASS" : "FAIL") — \(name)")
            if !ok { pass = false }
        }

        // Before ANY Store call: the at-capacity branch logs, and `AuditLog.dir` is
        // read per write, so redirecting it here is enough to keep the real feed clean.
        let feed = FileManager.default.temporaryDirectory
            .appendingPathComponent("diplomat-queuetest-\(UUID().uuidString)")
        setenv("DIPLOMAT_AUDIT_DIR", feed.path, 1)
        defer { try? FileManager.default.removeItem(at: feed) }

        let store = Store()
        // A headless Store still READS the operator's real settings — pin every one
        // this test's outcome depends on, or a machine with a monitor switched off
        // fails assertions about work that monitor owns.
        store.prAutofixEnabled = true       // headless-guarded: persists nothing, polls nothing
        store.reviewRequestsEnabled = true
        // One automatic agent already up, and a cap of one: every auto job offered
        // below is over the cap, which is the branch under test. (The `ps` scan the
        // count also consults can only add to it, never subtract, so a developer's
        // own live agents can't turn this into a spawn.)
        store.autoTaskLimit = 1
        store.processes = [
            TrackedProcess(kind: "review", label: "Auto · Review · #1", terminal: "iterm",
                           windowID: "1", sessionID: "", tty: "", donePath: "",
                           prURL: "https://github.com/software-mansion/argent/pull/1",
                           source: AgentDispatchGate.Source.auto.rawValue,
                           createdAt: Date(), done: false),
        ]

        func job(_ number: Int, action: String = "review-req", label: String? = nil,
                 counter: Store.AutoCounter = .reviewRequests) -> Store.AgentJob {
            Store.AgentJob(kind: "review", auditAction: action,
                           label: label ?? "Review-req · #\(number)", prompt: "",
                           prURL: "https://github.com/software-mansion/argent/pull/\(number)",
                           prNumber: number, authorLogin: nil, duty: "review", workKey: "",
                           counter: counter,
                           attemptStamp: Store.AttemptStamp.unresolvedReview)
        }
        func offer(_ j: Store.AgentJob, attempt: Int = 1) async -> Store.DispatchOutcome {
            await store.dispatchAgent(j, source: .auto, attemptNumber: attempt)
        }

        // 1. A refusal is queued, not dropped — the whole feature rests on the gate
        //    handing the job over instead of returning `.atCapacity` and forgetting it.
        check("an over-cap auto job is refused", await offer(job(2)) == .atCapacity)
        store.commitQueue()
        check("…and lands in the queue rather than vanishing",
              store.queuedTasks.map(\.id) == ["review-req:2"])

        // 2. One unit offered twice in a cycle is one task. The review edge-trigger and
        //    the level-triggered reconciler both reach the same PR in one poll; two
        //    rows for one agent would be a lie about what is pending, and the
        //    reconciler's offer is the one that carries the backoff-aware attempt.
        _ = await offer(job(2))
        _ = await offer(job(2), attempt: 3)
        store.commitQueue()
        check("the same unit offered twice in one cycle is one task",
              store.queuedTasks.count == 1)
        check("…dispatched under the later, backoff-aware attempt number",
              store.queuedTasks.first?.attemptNumber == 3)
        check("which is the label it will run under",
              store.queuedTasks.first.map {
                  AgentDispatchGate.label(source: .auto, core: $0.job.label,
                                          attemptNumber: $0.attemptNumber)
              } == "Auto · Review-req · #2 · retry 3")

        // 3. A PR can owe two monitors at once — a conflict and an unaddressed review
        //    are two agents' worth of work, so two rows.
        _ = await offer(job(2))
        _ = await offer(job(2, action: "conflicts", label: "Resolve · #2"))
        _ = await offer(job(4))
        store.commitQueue()
        check("one PR owing two monitors is two tasks",
              store.queuedTasks.map(\.id) == ["review-req:2", "conflicts:2", "review-req:4"])

        // 4. The drag order is the execution order, and it survives the poll that
        //    rebuilds the list — the queue is re-derived from GitHub every cycle, so an
        //    arrangement that didn't outlive one would be undone within 3 minutes.
        store.moveQueuedTask("review-req:4", onto: "review-req:2")
        check("a task dragged onto the first row runs first",
              store.queuedTasks.map(\.id) == ["review-req:4", "review-req:2", "conflicts:2"])
        _ = await offer(job(2))
        _ = await offer(job(2, action: "conflicts", label: "Resolve · #2"))
        _ = await offer(job(4))
        store.commitQueue()
        check("…and the next poll re-offers them in that order",
              store.queuedTasks.map(\.id) == ["review-req:4", "review-req:2", "conflicts:2"])

        // 5. Work GitHub stops owing drops out. A queue that outlived its evidence
        //    would offer "execute now" on a review that has already been answered.
        _ = await offer(job(4))
        store.commitQueue()
        check("a unit no longer offered leaves the queue",
              store.queuedTasks.map(\.id) == ["review-req:4"])

        // 6. Only work a monitor will offer again may be queued. The queue is a view
        //    of what the monitors owe, never a record of its own: a job no monitor
        //    owns would sit there unpauseable, unrecorded, and never re-offered — so
        //    it is refused at the door. Nothing builds one today; this is what keeps
        //    that true when something does.
        _ = await offer(Store.AgentJob(kind: "review", auditAction: "sweep",
                                       label: "Sweep · #9", prompt: "",
                                       prURL: "https://github.com/software-mansion/argent/pull/9",
                                       prNumber: 9, authorLogin: nil, duty: "review",
                                       workKey: "", counter: nil))
        store.commitQueue()
        check("work no monitor owns is not queued", store.queuedTasks.isEmpty)

        // 7. A monitor switched off still queues its work — the panel is where the
        //    operator sees what their PRs owe — but nothing automatic starts it. The
        //    drain walks `drainableTasks`, so that list is where the rule lives — a
        //    paused row appearing in it is an agent opening minutes after they
        //    switched that monitor off.
        _ = await offer(job(4))
        _ = await offer(job(6, action: "conflicts", label: "Resolve · #6",
                            counter: .conflicts))
        store.commitQueue()
        check("both monitors on ⇒ both kinds queue",
              store.queuedTasks.map(\.id) == ["review-req:4", "conflicts:6"])
        check("…and both are the drain's to run",
              store.drainableTasks.map(\.id) == ["review-req:4", "conflicts:6"])
        store.prAutofixEnabled = false
        check("switching off PR auto-fix keeps its queued work on the list",
              store.queuedTasks.map(\.id) == ["review-req:4", "conflicts:6"])
        check("…but the drain will no longer start it",
              store.drainableTasks.map(\.id) == ["review-req:4"])

        // 8. A switched-off monitor keeps FINDING work: the poll runs both monitors
        //    either way and the queue is where the off one puts what it found. This is
        //    the toggle's whole meaning now — who starts the work, not whether it is
        //    known — so a poll that skipped the off monitor would empty its rows.
        _ = await offer(job(4))
        _ = await offer(job(6, action: "conflicts", label: "Resolve · #6",
                            counter: .conflicts))
        store.commitQueue()
        check("a paused monitor's find is queued, not dropped",
              store.queuedTasks.map(\.id) == ["review-req:4", "conflicts:6"])
        store.reviewRequestsEnabled = false
        check("with both monitors off, nothing is the drain's to run",
              store.drainableTasks.isEmpty)
        check("…and the rows stay, for \"execute now\"",
              store.queuedTasks.count == 2)

        // Room on the device is not permission. With slots free and the monitor off,
        // the job must still be held — the alternative is an agent launching from a
        // monitor the operator switched off, which is the one thing the toggle buys.
        store.autoTaskLimit = 3     // one auto agent up ⇒ two slots free
        check("a paused monitor's job is held even with the device idle",
              await offer(job(8, action: "conflicts", label: "Resolve · #8",
                              counter: .conflicts)) == .atCapacity)
        store.autoTaskLimit = 1

        // 9. The empty slots the panel draws under the tasks. The seeded agent is up
        //    against a cap of one, so this device has no room; the panel must not
        //    offer a bay the gate would refuse to fill.
        store.pinAutoTasksMeasured(1)
        check("a device at its cap draws no free slots", store.freeAutoSlots == 0)
        store.autoTaskLimit = 3
        check("raising the cap opens the slots it added", store.freeAutoSlots == 2)
        store.processes = []
        store.pinAutoTasksMeasured(0)
        check("an idle device is all free slots", store.freeAutoSlots == 3)
        // Reachable two ways: an untracked agent counts as automatic, and the cap can
        // be lowered under agents that are already running.
        store.pinAutoTasksMeasured(5)
        check("more agents up than the cap allows is zero slots, never negative",
              store.freeAutoSlots == 0)

        // 10. A task the mesh runs on a peer is still a task this panel shows. Before
        //    the mesh row existed, "execute now" on peer-routed work took the queued
        //    row away and put nothing in its place, which reads exactly like the click
        //    dropping the task. The row it leaves instead lives on the executor's
        //    lease: held while the remote agent runs, gone when it finishes.
        store.processes = []
        let meshKey = "review:github.com/software-mansion/argent#77@abc123"
        var meshJob = job(77)
        meshJob.workKey = meshKey
        store.trackMeshRun(meshJob, node: "softoobox", attemptNumber: 1)
        check("a job the mesh took becomes a row, not a gap",
              store.processes.count == 1 && store.processes.first?.isMesh == true)
        check("…that names the node it runs on",
              store.processes.first?.mesh?.node == "softoobox")
        check("…under the label it would have run under here",
              store.processes.first?.label == "Auto · Review-req · #77")
        // The same key offered twice is the same run: a stand-down re-offered before
        // the in-flight check can see the row must not draw a second one.
        store.trackMeshRun(meshJob, node: "softoobox", attemptNumber: 1)
        check("one lease is one row, however often the mesh answers for it",
              store.processes.count == 1)

        // It runs elsewhere, so it spends none of THIS device's budget — a peer-routed
        // job that closed a local bay would cap the machine on work it isn't doing.
        store.autoTaskLimit = 2
        store.pinAutoTasksMeasured(0)
        check("a mesh row takes none of this device's slots", store.freeAutoSlots == 2)

        // One timeline, so each step measures from the sighting before it — the whole
        // rule is that the clock restarts every time the lease is seen, not that a row
        // expires at some age.
        let t0 = Date()
        func at(_ secs: TimeInterval) -> Date { t0.addingTimeInterval(secs) }
        // The lease is present ⇒ the agent is up, however long the row has been there.
        // An hour-long review is the normal case, not a stuck row.
        store.reconcileMeshRuns(claims: [meshKey: "n-soft-strong"], now: at(3600))
        check("a claimed key keeps its row, however old", store.processes.count == 1)
        // Absence inside the settle window is snapshot lag, not a finished run — and
        // it is measured from that last sighting, not from the dispatch.
        store.reconcileMeshRuns(claims: [:], now: at(3600 + MeshAgentRun.claimSettle - 1))
        check("an unseen claim inside the settle window does not end the row",
              store.processes.count == 1)
        // Past it, the executor has released: the run is over and there is nothing
        // left here to focus, read or retry — so the row goes, as a closed terminal's
        // does. Left behind it would also hold the PR in-flight forever.
        store.reconcileMeshRuns(claims: [:], now: at(3600 + MeshAgentRun.claimSettle + 1))
        check("a released lease takes the row with it", store.processes.isEmpty)

        // A row reloaded after a restart has no sighting behind it and an age that can
        // be hours — and for the first seconds of a launch the node's snapshot has not
        // been read yet, so the claim book looks empty whatever a peer is running. Its
        // window has to start at the first pass, or every restored row is dropped on
        // the first poll of every launch.
        store.processes = [
            TrackedProcess(kind: "review", label: "Auto · Review-req · #77",
                           terminal: "", windowID: "", sessionID: "", tty: "",
                           donePath: "",
                           prURL: "https://github.com/software-mansion/argent/pull/77",
                           mesh: .init(node: "softoobox", workKey: meshKey),
                           source: AgentDispatchGate.Source.auto.rawValue,
                           createdAt: at(-7200), done: false),
        ]
        store.reconcileMeshRuns(claims: [:], now: at(0))
        check("a row reloaded hours after its dispatch survives the first pass",
              store.processes.count == 1)
        store.reconcileMeshRuns(claims: [meshKey: "n-soft-strong"], now: at(1))
        store.reconcileMeshRuns(claims: [:], now: at(MeshAgentRun.claimSettle))
        check("…and then lives on sightings like any other",
              store.processes.count == 1)

        // The list now has two liveness sources walking it, and each must leave the
        // other's rows alone. A local session outlives the mesh reconciler…
        let local = TrackedProcess(kind: "review", label: "Review · #5", terminal: "iterm",
                                   windowID: "5", sessionID: "", tty: "", donePath: "",
                                   prURL: "https://github.com/software-mansion/argent/pull/5",
                                   createdAt: t0, done: false)
        store.processes = [local]
        store.reconcileMeshRuns(claims: [:], now: at(MeshAgentRun.claimSettle + 1))
        check("a local session is not the mesh reconciler's to remove",
              store.processes.count == 1)
        // …and a mesh row outlives the window sweep, which would otherwise read its
        // missing window, tty and sentinel — none of which a remote run has — as a
        // session that ended.
        let remote = TrackedProcess(kind: "review", label: "Auto · Review-req · #77",
                                    terminal: "", windowID: "", sessionID: "", tty: "",
                                    donePath: "",
                                    prURL: "https://github.com/software-mansion/argent/pull/77",
                                    mesh: .init(node: "softoobox", workKey: meshKey),
                                    source: AgentDispatchGate.Source.auto.rawValue,
                                    createdAt: t0.addingTimeInterval(-600), done: false)
        // Every local probe says gone: no window in the enumeration, no session dump,
        // no process on the tty. Asking at all is the bug — a machine whose whole
        // Agent-tasks list is peer-routed work would drive an AppleScript window
        // enumeration every poll to learn nothing — so the resolver counts its calls.
        var asked = 0
        let swept = ProcessMonitor.sweep([remote], now: at(0),
                                         openWindows: { _ in asked += 1; return [] },
                                         sessionTails: [:], ttyElapsed: [:])
        check("a mesh row is not the window sweep's to remove",
              swept.closedIDs.isEmpty && swept.refreshed.first?.done == false)
        check("…and the sweep never asks a terminal about one", asked == 0)
        store.processes = []

        // 11. The redirect above is the only thing between a run of this test and the
        //    operator's real activity log, so prove it caught the writes.
        check("the at-capacity lines it provoked went to the scratch feed",
              FileManager.default.fileExists(
                  atPath: feed.appendingPathComponent("audit.jsonl").path))

        print(pass ? "\nQUEUE TEST OK" : "\nQUEUE TEST FAILED")
        return pass
    }
}
