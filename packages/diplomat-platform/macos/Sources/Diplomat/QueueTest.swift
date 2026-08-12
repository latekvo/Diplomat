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
        // Same reason, one layer further out: an auto dispatch that is NOT over the cap
        // asks the rate-limit budget, and that probe would otherwise spend the
        // developer's own OAuth token on a live request to Anthropic — and answer
        // differently depending on how much of their window was left when they ran the
        // suite. Off, it reports no reading, which the gate treats as no opinion.
        setenv("DIPLOMAT_QUOTA_PROBE", "0", 1)

        let store = Store()
        // A headless Store still READS the operator's real settings — pin every one
        // this test's outcome depends on, or a machine with a monitor switched off
        // fails assertions about work that monitor owns.
        store.prAutofixEnabled = true       // headless-guarded: persists nothing, polls nothing
        store.reviewRequestsEnabled = true
        // The budget's own knobs come from the shared config file, so an operator who
        // raised their floor would otherwise change what this suite asserts. Left ON
        // deliberately: with the probe off above, every dispatch below runs through the
        // real gate and its fail-open rather than around them.
        store.autoBudgetGate = true
        store.autoBudgetConfidence = AgentDispatchGate.defaultBudgetConfidence
        store.autoBudgetFloorPct = AgentDispatchGate.defaultBudgetFloorPct
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
              store.queuedTasks.map(\.id) == ["review-req:2", "review-req:4", "conflicts:2"])
        // …and the conflict fix is last though it was offered second: the band
        // outranks the order the monitors found things in (`AgentTaskQueue.band`).

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

        // 10. Starting a task takes seconds — a `ps` scan, a mesh round-trip, an
        //    AppleScript terminal — and for all of them it belongs to neither list.
        //    Held in neither, "execute now" reads as the click DELETING the row: it
        //    goes on the press, and a session appears in its place later, which is
        //    indistinguishable from a task that was dropped.
        //
        //    The cap is one again with one agent up, so every offer below queues. The
        //    commit is what clears section 8's last offer, which was deliberately left
        //    staged: uncommitted, it would land in the first commit this section makes.
        store.autoTaskLimit = 1
        store.prAutofixEnabled = true
        store.reviewRequestsEnabled = true
        store.commitQueue()
        store.processes = [
            TrackedProcess(kind: "review", label: "Auto · Review · #1", terminal: "iterm",
                           windowID: "1", sessionID: "", tty: "", donePath: "",
                           prURL: "https://github.com/software-mansion/argent/pull/1",
                           source: AgentDispatchGate.Source.auto.rawValue,
                           createdAt: Date(), done: false),
        ]
        func offerBoth() async {
            _ = await offer(job(4))
            _ = await offer(job(6, action: "conflicts", label: "Resolve · #6",
                                counter: .conflicts))
        }
        await offerBoth()
        store.commitQueue()
        check("two tasks queued, ready to start",
              store.queuedTasks.map(\.id) == ["review-req:4", "conflicts:6"])
        let starting = store.queuedTasks[0]
        store.beginStarting(starting)
        check("a task being started leaves the queue…",
              store.queuedTasks.map(\.id) == ["conflicts:6"])
        check("…for the band that keeps it a row while it spawns",
              store.startingTasks.map(\.id) == ["review-req:4"])
        check("…and the drain no longer sees it either",
              store.drainableTasks.map(\.id) == ["conflicts:6"])

        // The work stays owed until the spawn answers and the attempt is recorded, so
        // a poll committing mid-dispatch re-offers it. Published as queued as well, it
        // would be two rows for one task — the second promising a start that is
        // already under way.
        await offerBoth()
        store.commitQueue()
        check("a poll landing mid-dispatch does not put the row back in the queue",
              store.queuedTasks.map(\.id) == ["conflicts:6"])
        check("…and it is still starting", store.startingTasks.map(\.id) == ["review-req:4"])

        // A start that comes to nothing is re-offered by the next poll, so its key has
        // to keep its PLACE in the arrangement, rather than coming back at the end of a
        // queue the operator ordered by hand.
        store.endStarting(starting.id)
        check("the spawn answering ends the starting band", store.startingTasks.isEmpty)
        await offerBoth()
        store.commitQueue()
        check("a start that did not take comes back where it was, not at the back",
              store.queuedTasks.map(\.id) == ["review-req:4", "conflicts:6"])

        // Its bay is spoken for from the moment it starts. Drawn free, the panel would
        // stand a row that is launching next to the empty slot it is launching into —
        // one row more than the cap allows. (Raised after the offers above: with room
        // to spare they would spawn rather than queue. Re-pinned too — every offer
        // re-measures, and the `ps` scan behind that is the developer's own machine.)
        store.autoTaskLimit = 3
        store.pinAutoTasksMeasured(1)
        check("one agent up under a cap of three leaves two bays", store.freeAutoSlots == 2)
        store.beginStarting(store.queuedTasks[0])
        check("…and a task starting takes one of them", store.freeAutoSlots == 1)
        store.endStarting("review-req:4")

        // The `ps` count and the rows are each a lower bound on what is running, and a
        // starting task is outside BOTH — nothing has registered its spawn anywhere. So
        // it is added to the higher of them rather than counted among them: one agent
        // alive with no row behind it (the applet restart the measurement is there for)
        // is enough to put `ps` ahead, and folded in there the starting task would
        // vanish into the same number and hand its bay back as free.
        store.pinAutoTasksMeasured(2)
        check("a `ps` count ahead of the rows is what the bays are drawn from",
              store.freeAutoSlots == 1)
        let unrowed = store.queuedTasks[0]
        store.beginStarting(unrowed)
        check("…and a task starting takes one on top of it, not one of it",
              store.freeAutoSlots == 0)
        store.endStarting(unrowed.id)

        // The whole click, honestly. #7 is queued, and by the time the operator gets to
        // it an agent is already on that PR: the dispatch is refused, and a refusal
        // that left its row starting would be a task spinning at a spawn that will
        // never answer.
        store.autoTaskLimit = 1
        _ = await offer(job(7))
        store.commitQueue()
        check("#7 is queued", store.queuedTasks.map(\.id) == ["review-req:7"])
        store.processes.append(
            TrackedProcess(kind: "review", label: "Auto · Review-req · #7", terminal: "iterm",
                           windowID: "7", sessionID: "", tty: "", donePath: "",
                           prURL: "https://github.com/software-mansion/argent/pull/7",
                           source: AgentDispatchGate.Source.auto.rawValue,
                           createdAt: Date(), done: false))
        await store.executeQueuedTask("review-req:7")
        check("a refused start leaves no row spinning behind it",
              store.startingTasks.isEmpty && store.queuedTasks.isEmpty)
        check("…and says why nothing opened",
              store.error?.contains("already on this PR") == true)
        store.error = nil
        store.processes = []

        // 11. A task the mesh runs on a peer is still a task this panel shows. Before
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

        // 10b. The other placement the mesh can make: back onto the machine that asked.
        //    That agent is a `claude` process HERE, so it spends a slot from the moment
        //    it is placed. Waiting for `ps` to notice is what let a poll dispatch its
        //    whole backlog into one cap — every gate in the burst measured the same
        //    machine, seconds before any of its new agents were visible.
        store.processes = []
        var homeJob = job(78)
        homeJob.workKey = "review:github.com/software-mansion/argent#78@abc123"
        store.trackMeshRun(homeJob, node: "softoobox", attemptNumber: 1, onThisMachine: true)
        check("a mesh run placed back here spends one of this device's slots",
              store.freeAutoSlots == 1)
        check("…and is still a mesh row, held by its lease like any other",
              store.processes.first?.isMesh == true)
        // Back to the peer-routed row, and to a sighting as fresh as its dispatch: the
        // lifecycle below is one timeline over THAT key, and it starts here.
        store.processes = []
        store.trackMeshRun(meshJob, node: "softoobox", attemptNumber: 1)

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
                           mesh: .init(node: "softoobox", workKey: meshKey,
                                       onThisMachine: false),
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
                                    mesh: .init(node: "softoobox", workKey: meshKey,
                                                onThisMachine: false),
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

        // 12. The refresh the drain makes before it starts anything. A queued task
        //    carries the verdict of the poll that staged it, and by the time a bay
        //    frees an agent working the same branch has been and gone — so the list is
        //    re-checked against THIS cycle's fetch, and work already done leaves it
        //    instead of spawning. (At capacity throughout, so the drain refreshes and
        //    then returns without starting anything.)
        store.autoTaskLimit = 1
        store.prAutofixEnabled = true
        store.reviewRequestsEnabled = true
        store.processes = [
            TrackedProcess(kind: "review", label: "Auto · Review · #1", terminal: "iterm",
                           windowID: "1", sessionID: "", tty: "", donePath: "",
                           prURL: "https://github.com/software-mansion/argent/pull/1",
                           source: AgentDispatchGate.Source.auto.rawValue,
                           createdAt: Date(), done: false),
        ]
        _ = await offer(job(21, action: "conflicts", label: "Resolve · #21",
                            counter: .conflicts))
        _ = await offer(job(22, action: "conflicts", label: "Resolve · #22",
                            counter: .conflicts))
        _ = await offer(job(23))
        store.commitQueue()
        check("three tasks queued, two of them conflict fixes",
              store.queuedTasks.map(\.id) == ["review-req:23", "conflicts:21", "conflicts:22"])
        func snap(_ number: Int, mergeable: String, iOwe: Int = 0) -> PRSnapshot {
            PRSnapshot(number: number, title: "t",
                       url: "https://github.com/software-mansion/argent/pull/\(number)",
                       isDraft: false, mergeable: mergeable, reviewDecision: "",
                       threadsUnresolved: 0, threadsIOwe: iOwe, headSha: "")
        }
        // #21 is still conflicting; #22 came out of conflict while it waited.
        await store.drainQueuedTasks(snaps: [snap(21, mergeable: "CONFLICTING"),
                                             snap(22, mergeable: "MERGEABLE")])
        check("a conflict fix the branch no longer needs leaves the list",
              store.queuedTasks.map(\.id) == ["review-req:23", "conflicts:21"])
        check("…and one it still needs keeps its place",
              store.queuedTasks.contains { $0.id == "conflicts:21" })
        // A review requested of me is not in that fetch at all. Read as answered, every
        // review request on the panel would vanish the first time the drain ran.
        check("a review request is not retired by a fetch that cannot see it",
              store.queuedTasks.contains { $0.id == "review-req:23" })
        store.processes = []
        store.queuedTasks = []

        // 14. The reviews the operator asks for. A whose-PRs sweep is queued one PR at a
        //    time rather than handed to a single agent, and these are the only queued
        //    tasks nothing on GitHub would re-offer — so the list that remembers the ask
        //    has to survive a commit, and the dispatch that answers one has to take it
        //    off. (Seeded empty: a headless Store reads the operator's real list, and a
        //    developer mid-sweep would otherwise fail every count below.)
        //
        //    The band, the key and the arrangement are pinned in DiplomatCoreSmoke; what
        //    is here is the wiring those rules hang off.
        store.requestedReviews = []
        store.queuedTasks = []
        func openPR(_ number: Int, author: String = "alice", draft: Bool = true) -> OpenPR {
            OpenPR(number: number, title: "PR \(number)",
                   url: "https://github.com/software-mansion/argent/pull/\(number)",
                   isDraft: draft, author: author, createdAt: Date(),
                   readyForReviewAt: nil, files: [], reviewDecision: nil, reviewThreads: [])
        }
        store.prs = [openPR(31), openPR(32), openPR(33, draft: false), openPR(34, author: "bob")]
        let sweep = ReviewConfig(depth: "deep", target: .mine, me: "alice",
                                 includeDrafts: true, includeReady: false)
        let swept = store.requestReviewSweep(sweep)
        check("a sweep queues one review per PR in scope, not one agent for all",
              swept.queued == 2 && swept.already == 0
                  && store.queuedTasks.map(\.id) == ["review:31", "review:32"])
        check("…each scoped to its own PR",
              store.queuedTasks.map(\.job.prNumber) == [31, 32])
        check("…owned by no monitor, so no toggle pauses it and no counter counts it",
              store.queuedTasks.allSatisfy {
                  $0.job.counter == nil && $0.job.ledgerKey.isEmpty && !store.isPaused($0.job.counter)
              })
        check("…and labelled as the operator's own, not as a monitor's find",
              store.queuedTasks.first.map {
                  AgentDispatchGate.label(source: .auto, core: $0.job.label,
                                          attemptNumber: $0.attemptNumber,
                                          requested: $0.job.requested)
              } == "Review · #31 · deep")

        // Sweeping again over an overlapping scope adds only what is new: the queue is
        // keyed by PR, so a second ask would be one row that dispatches twice.
        var wider = sweep
        wider.includeReady = true
        let again = store.requestReviewSweep(wider)
        check("a second sweep asks only for what is not already queued",
              again.queued == 1 && again.already == 2)

        // Nothing on GitHub says a PR was swept, so the ask itself is what re-offers
        // these — a commit built from the monitors alone would empty the panel of them.
        store.offerRequestedReviews()
        store.commitQueue()
        check("a poll re-offers every ask nothing has started",
              store.queuedTasks.map(\.id) == ["review:31", "review:32", "review:33"])

        // …and the band puts them behind what GitHub is already owed, ahead of the
        // conflict fix another agent's run may make unnecessary.
        _ = await offer(job(35))
        _ = await offer(job(36, action: "conflicts", label: "Resolve · #36",
                            counter: .conflicts))
        store.offerRequestedReviews()
        store.commitQueue()
        check("a requested review waits behind a monitor's find and ahead of a conflict fix",
              store.queuedTasks.map(\.id)
                  == ["review-req:35", "review:31", "review:32", "review:33", "conflicts:36"])

        // Cancel is the only way out: nothing else retires an ask, so a mis-aimed sweep
        // would otherwise be a day of agents nobody can call off.
        store.cancelRequestedReview("review:32")
        store.offerRequestedReviews()
        store.commitQueue()
        check("a cancelled ask does not come back on the next poll",
              store.requestedReviews.map(\.number) == [31, 33]
                  && !store.queuedTasks.contains { $0.id == "review:32" })
        store.cancelRequestedReview("review-req:35")
        check("…and a monitor's row is not cancellable at all",
              store.queuedTasks.contains { $0.id == "review-req:35" })

        // The dispatch is what answers an ask. #31's PR gains an agent, so the dispatch
        // is refused and the ask stands; a refusal that dropped it would silently
        // abandon the review.
        store.processes.append(
            TrackedProcess(kind: "review", label: "Auto · Review · #31", terminal: "iterm",
                           windowID: "31", sessionID: "", tty: "", donePath: "",
                           prURL: "https://github.com/software-mansion/argent/pull/31",
                           source: AgentDispatchGate.Source.auto.rawValue,
                           createdAt: Date(), done: false))
        await store.executeQueuedTask("review:31")
        check("an ask refused because the PR is busy is still asked for",
              store.requestedReviews.map(\.number) == [31, 33])
        store.error = nil
        store.processes = []
        store.requestedReviews = []
        store.queuedTasks = []

        // 13. The redirect above is the only thing between a run of this test and the
        //    operator's real activity log, so prove it caught the writes.
        check("the at-capacity lines it provoked went to the scratch feed",
              FileManager.default.fileExists(
                  atPath: feed.appendingPathComponent("audit.jsonl").path))

        print(pass ? "\nQUEUE TEST OK" : "\nQUEUE TEST FAILED")
        return pass
    }
}
