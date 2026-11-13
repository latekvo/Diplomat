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
/// owing falls out of it, and that a switched-off monitor's work — or, with the
/// queue's own switch off, anyone's — is held rather than either run or dropped. And,
/// being the harness with a real book on a pinned machine, that two settles of that
/// book never overlap.
///
/// It reaches the at-capacity branch honestly — a real `dispatchAgent` call against
/// a real cap — and a dispatch that gets past it is refused its terminal
/// (`AgentSpawner.refuseHeadless`), so it never spawns an agent, needs no `gh` auth and
/// no terminal automation, and can run on a CI runner:
///
///   DIPLOMAT_QUEUE_TEST=1 swift run Diplomat
///
/// It reads the ban list, and the refusal it provokes writes an `at-capacity` line —
/// so it points `DIPLOMAT_AUDIT_DIR` at a scratch directory first. `Headless.active`
/// covers UserDefaults, not the shared feed, and a self-test has no business in the
/// operator's activity log. The run book and the process table are stood in for by the
/// same reasoning: a fixture that controls the records but not the evidence they are
/// resolved against is a fixture whose answers depend on what else the developer
/// happens to be running.
enum QueueTest {
    /// The queue key of `verb` on argent's PR or issue `n`, the repo every fixture names.
    static func key(_ verb: String, _ n: Int) -> String {
        AgentTaskQueue.key(auditAction: verb, item: "software-mansion/argent#\(n)")
    }

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
        // And the disk floor, or every dispatch here measures the developer's own disk.
        setenv(DiskFloor.envOverride, "0", 1)
        // And the run book, for the same reason one layer down: every agent this test
        // stands up is a real record, and one written into the operator's book would be
        // an agent their panel draws and their cap holds a bay for.
        let agents = FileManager.default.temporaryDirectory
            .appendingPathComponent("diplomat-queuetest-agents-\(UUID().uuidString)")
        setenv("DIPLOMAT_AGENTS_DIR", agents.path, 1)
        defer { try? FileManager.default.removeItem(at: agents) }
        // And the machine the book is resolved against. `emptyBook()` below controls the
        // records; it cannot control `ps`, and the cap load is not a fold over records
        // alone — `AgentState.synthesizeUntracked` makes every live agent with no record
        // an occupying `untracked:<pr>` one. So on the machine this is written on, whose
        // ordinary state is agents up, the two placement assertions below counted
        // whichever of the developer's own sessions was mid-turn or had an unreadable
        // screen — a verdict the box decides, not the fixture. Present-and-empty, not
        // unavailable: a machine that was looked at and had nothing on it, which is what
        // every assertion here means by a quiet box.
        AgentProbes.pinDump(.present(""))
        defer { AgentProbes.pinDump(nil) }

        /// Stand one automatic agent up, the way a dispatch books it: a record with no
        /// pid yet, which is exactly what a spawn nothing has observed yet looks like —
        /// and which holds a bay of the cap from the moment it is written. Only for
        /// `AgentState.spawnGrace`, though: past it the bay frees, and a section whose
        /// polls may outlast it dates it `ahead` to keep its work queued.
        func bookAgent(_ number: Int, label: String = "", ahead: TimeInterval = 0) {
            let now = Date().timeIntervalSince1970
            AgentRegistry.createRun(
                AgentState.RunRecord(
                    runID: AgentRegistry.newRunID(now: now), dispatchedAt: now + ahead,
                    prNumber: number,
                    prURL: "https://github.com/software-mansion/argent/pull/\(number)",
                    kind: "review",
                    label: label.isEmpty ? "Auto · Review · #\(number)" : label,
                    source: AgentDispatchGate.Source.auto.rawValue),
                prompt: "")
        }
        func emptyBook() { AgentRegistry.forget(Set(AgentRegistry.load().map(\.runID))) }

        let store = Store()
        // A headless Store still READS the operator's real settings — pin every one
        // this test's outcome depends on, or a machine with a monitor switched off
        // fails assertions about work that monitor owns.
        let argent = PRRef.LocalRepo(owner: "software-mansion", repo: "argent",
                                     path: AgentSpawner.repoPath)
        store.pinRoots([argent])
        store.selectedRepo = ""
        store.prAutofixEnabled = true       // headless-guarded: persists nothing, polls nothing
        store.reviewRequestsEnabled = true
        store.reviewAllowlistRaw = ""       // every review-req offered below carries no author

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
        bookAgent(1)

        func job(_ number: Int, action: String = "review-req", label: String? = nil,
                 author: String? = nil,
                 counter: Store.AutoCounter? = .reviewRequests, url: String? = nil) -> Store.AgentJob {
            Store.AgentJob(kind: "review", auditAction: action,
                           label: label ?? "Review-req · #\(number)", prompt: "",
                           prURL: url ?? "https://github.com/software-mansion/argent/pull/\(number)",
                           prNumber: number, authorLogin: author, duty: "review", workKey: "",
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
              store.queuedTasks.map(\.id) == [key("review-req", 2)])

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
              store.queuedTasks.map(\.id)
                  == [key("review-req", 2), key("review-req", 4), key("conflicts", 2)])
        // …and the conflict fix is last though it was offered second: the band
        // outranks the order the monitors found things in (`AgentTaskQueue.band`).

        // 4. The drag order is the execution order, and it survives the poll that
        //    rebuilds the list — the queue is re-derived from GitHub every cycle, so an
        //    arrangement that didn't outlive one would be undone within 3 minutes.
        store.moveQueuedTask(key("review-req", 4), onto: key("review-req", 2))
        check("a task dragged onto the first row runs first",
              store.queuedTasks.map(\.id)
                  == [key("review-req", 4), key("review-req", 2), key("conflicts", 2)])
        _ = await offer(job(2))
        _ = await offer(job(2, action: "conflicts", label: "Resolve · #2"))
        _ = await offer(job(4))
        store.commitQueue()
        check("…and the next poll re-offers them in that order",
              store.queuedTasks.map(\.id)
                  == [key("review-req", 4), key("review-req", 2), key("conflicts", 2)])

        // 5. Work GitHub stops owing drops out. A queue that outlived its evidence
        //    would offer "execute now" on a review that has already been answered.
        _ = await offer(job(4))
        store.commitQueue()
        check("a unit no longer offered leaves the queue",
              store.queuedTasks.map(\.id) == [key("review-req", 4)])

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
              store.queuedTasks.map(\.id) == [key("review-req", 4), key("conflicts", 6)])
        check("…and both are the drain's to run",
              store.drainableTasks.map(\.id) == [key("review-req", 4), key("conflicts", 6)])
        store.prAutofixEnabled = false
        check("switching off PR auto-fix keeps its queued work on the list",
              store.queuedTasks.map(\.id) == [key("review-req", 4), key("conflicts", 6)])
        check("…but the drain will no longer start it",
              store.drainableTasks.map(\.id) == [key("review-req", 4)])

        // 8. A switched-off monitor keeps FINDING work: the poll runs both monitors
        //    either way and the queue is where the off one puts what it found. This is
        //    the toggle's whole meaning now — who starts the work, not whether it is
        //    known — so a poll that skipped the off monitor would empty its rows.
        _ = await offer(job(4))
        _ = await offer(job(6, action: "conflicts", label: "Resolve · #6",
                            counter: .conflicts))
        store.commitQueue()
        check("a paused monitor's find is queued, not dropped",
              store.queuedTasks.map(\.id) == [key("review-req", 4), key("conflicts", 6)])
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

        // 8b. The switch over the queue itself, which is what neither monitor toggle
        //     can be: neither speaks for a review the operator asked for, so without
        //     this one nothing stops a fifty-PR sweep emptying into agents a bay at
        //     a time.
        store.prAutofixEnabled = true
        store.reviewRequestsEnabled = true
        store.queueAutoRun = false
        check("a switched-off queue is nothing the drain may run, both monitors on",
              store.drainableTasks.isEmpty)
        check("…and its rows stay, for \"execute now\"", store.queuedTasks.count == 2)
        // It holds at the DISPATCH too, not only at the drain: this offer meets a free
        // bay, so without that hold it would start without ever reaching the queue.
        store.autoTaskLimit = 3
        check("…and a find that meets a free bay is held rather than started",
              await offer(job(8, action: "conflicts", label: "Resolve · #8",
                              counter: .conflicts)) == .atCapacity)
        store.autoTaskLimit = 1
        store.queueAutoRun = true
        check("switched back on, the queue is the drain's again",
              store.drainableTasks.map(\.id) == [key("review-req", 4), key("conflicts", 6)])

        // 9. The empty slots the panel draws under the tasks. The seeded agent is up
        //    against a cap of one, so this device has no room; the panel must not
        //    offer a bay the gate would refuse to fill.
        store.pinAutoTasksMeasured(1)
        check("a device at its cap draws no free slots", store.freeAutoSlots == 0)
        store.autoTaskLimit = 3
        check("raising the cap opens the slots it added", store.freeAutoSlots == 2)
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
        bookAgent(1)
        func offerBoth() async {
            _ = await offer(job(4))
            _ = await offer(job(6, action: "conflicts", label: "Resolve · #6",
                                counter: .conflicts))
        }
        await offerBoth()
        store.commitQueue()
        check("two tasks queued, ready to start",
              store.queuedTasks.map(\.id) == [key("review-req", 4), key("conflicts", 6)])
        let starting = store.queuedTasks[0]
        store.beginStarting(starting)
        check("a task being started leaves the queue…",
              store.queuedTasks.map(\.id) == [key("conflicts", 6)])
        check("…for the band that keeps it a row while it spawns",
              store.startingTasks.map(\.id) == [key("review-req", 4)])
        check("…and the drain no longer sees it either",
              store.drainableTasks.map(\.id) == [key("conflicts", 6)])

        // The work stays owed until the spawn answers and the attempt is recorded, so
        // a poll committing mid-dispatch re-offers it. Published as queued as well, it
        // would be two rows for one task — the second promising a start that is
        // already under way.
        await offerBoth()
        store.commitQueue()
        check("a poll landing mid-dispatch does not put the row back in the queue",
              store.queuedTasks.map(\.id) == [key("conflicts", 6)])
        check("…and it is still starting", store.startingTasks.map(\.id) == [key("review-req", 4)])

        // A start that comes to nothing is re-offered by the next poll, so its key has
        // to keep its PLACE in the arrangement, rather than coming back at the end of a
        // queue the operator ordered by hand.
        store.endStarting(starting.id)
        check("the spawn answering ends the starting band", store.startingTasks.isEmpty)
        await offerBoth()
        store.commitQueue()
        check("a start that did not take comes back where it was, not at the back",
              store.queuedTasks.map(\.id) == [key("review-req", 4), key("conflicts", 6)])

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
        store.endStarting(key("review-req", 4))

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
        check("#7 is queued", store.queuedTasks.map(\.id) == [key("review-req", 7)])
        bookAgent(7, label: "Auto · Review-req · #7")
        await store.executeQueuedTask(key("review-req", 7))
        check("a refused start leaves no row spinning behind it",
              store.startingTasks.isEmpty && store.queuedTasks.isEmpty)
        check("…and says why nothing opened",
              store.error?.contains("already on this PR") == true)
        store.error = nil

        // 11. A task the mesh runs on a peer is still a task this panel shows. Before
        //    the mesh row existed, "execute now" on peer-routed work took the queued
        //    row away and put nothing in its place, which reads exactly like the click
        //    dropping the task. What it leaves instead is a record like any other,
        //    which the resolver then holds for as long as the executor's lease.
        //
        //    How long that is, is pure and pinned by the shared scenario table
        //    (`AgentState`, `test_agent_state.py`) — what is on trial here is that the
        //    record the mesh path writes carries what the resolver needs to do it.
        emptyBook()
        let meshKey = "review:github.com/software-mansion/argent#77@abc123"
        var meshJob = job(77)
        meshJob.workKey = meshKey
        store.trackMeshRun(meshJob, node: "softoobox", attemptNumber: 1)
        let peer = AgentRegistry.load().first
        check("a job the mesh took becomes a record, not a gap",
              AgentRegistry.load().count == 1 && peer?.placement == .meshPeer)
        check("…that names the node it runs on", peer?.node == "softoobox")
        check("…under the label it would have run under here",
              peer?.label == "Auto · Review-req · #77")
        // The lease is the only evidence about a peer's run that ever crosses the
        // machine boundary; without the key there is nothing to resolve it against.
        check("…keyed by the lease the executor claims it under", peer?.workKey == meshKey)
        // Its process is on another machine, so no store here holds its session. A
        // runner recorded would point the session probe at somebody else's.
        check("…and names no runner, because the run is not ours to ask",
              AgentRegistry.runRunner(peer?.runID ?? "").isEmpty)

        // It runs elsewhere, so it spends none of THIS device's budget — a peer-routed
        // job that closed a local bay would cap the machine on work it isn't doing.
        check("a peer's run holds none of this device's bays",
              await store.agentTick().tick.capLoad.isEmpty)

        // 11b. The other placement the mesh can make: back onto the machine that asked.
        //    That agent is a process HERE, so it spends a bay from the moment it is
        //    placed. Waiting for `ps` to notice is what let a poll dispatch its whole
        //    backlog into one cap — every gate in the burst measured the same machine,
        //    seconds before any of its new agents were visible.
        emptyBook()
        var homeJob = job(78)
        homeJob.workKey = "review:github.com/software-mansion/argent#78@abc123"
        store.trackMeshRun(homeJob, node: "softoobox", attemptNumber: 1, onThisMachine: true)
        let home = AgentRegistry.load().first
        check("a mesh run placed back here spends one of this device's bays",
              await store.agentTick().tick.capLoad == Set([home?.runID ?? ""]))
        check("…and is still a mesh run, held by its lease like any other",
              home?.placement == .meshHere && home?.workKey == homeJob.workKey)
        // The node spawns through the same seam a local dispatch does, so this run is
        // under the configured runner — and that is what decides which store it is
        // asked of and priced from. Left blank it would be asked of none and priced
        // off `~/.claude`, which holds no transcript of a foreign runner's work.
        check("…under the runner that spawned it, so it can be asked and priced",
              AgentRegistry.runRunner(home?.runID ?? "") == AppConfig.agentRunner.rawValue)
        emptyBook()

        // 12. The refresh the drain makes before it starts anything. A queued task
        //    carries the verdict of the poll that staged it, and by the time a bay
        //    frees an agent working the same branch has been and gone — so the list is
        //    re-checked against THIS cycle's fetch, and work already done leaves it
        //    instead of spawning. (At capacity throughout, so the drain refreshes and
        //    then returns without starting anything.)
        store.autoTaskLimit = 1
        store.prAutofixEnabled = true
        store.reviewRequestsEnabled = true
        bookAgent(1)
        _ = await offer(job(21, action: "conflicts", label: "Resolve · #21",
                            counter: .conflicts))
        _ = await offer(job(22, action: "conflicts", label: "Resolve · #22",
                            counter: .conflicts))
        _ = await offer(job(23))
        store.commitQueue()
        check("three tasks queued, two of them conflict fixes",
              store.queuedTasks.map(\.id)
                  == [key("review-req", 23), key("conflicts", 21), key("conflicts", 22)])
        func snap(_ number: Int, mergeable: String, iOwe: Int = 0) -> PRSnapshot {
            PRSnapshot(number: number, title: "t",
                       url: "https://github.com/software-mansion/argent/pull/\(number)",
                       isDraft: false, mergeable: mergeable, reviewDecision: "",
                       threadsUnresolved: 0, threadsIOwe: iOwe, headSha: "")
        }
        // #21 is still conflicting; #22 came out of conflict while it waited.
        await store.drainQueuedTasks(snaps: [snap(21, mergeable: "CONFLICTING"),
                                             snap(22, mergeable: "MERGEABLE")],
                                     closed: [])
        check("a conflict fix the branch no longer needs leaves the list",
              store.queuedTasks.map(\.id) == [key("review-req", 23), key("conflicts", 21)])
        check("…and one it still needs keeps its place",
              store.queuedTasks.contains { $0.id == key("conflicts", 21) })
        // A review requested of me is not in that fetch at all. Read as answered, every
        // review request on the panel would vanish the first time the drain ran.
        check("a review request is not retired by a fetch that cannot see it",
              store.queuedTasks.contains { $0.id == key("review-req", 23) })
        // What does retire it is its PR leaving the open state — the one answer that
        // reaches a verb the my-PRs fetch cannot speak for. #21 stays: it is the
        // arrangement, not the closure, that decides the rest of the list.
        await store.drainQueuedTasks(snaps: [snap(21, mergeable: "CONFLICTING")],
                                     closed: ["software-mansion/argent#23"])
        check("…and one whose PR has closed leaves the list all the same",
              store.queuedTasks.map(\.id) == [key("conflicts", 21)])
        emptyBook()
        store.queuedTasks = []

        // 14. The work the operator asks for. A whose-PRs sweep is queued one PR at a
        //    time — and a Fix-issues scope one ISSUE at a time — rather than handed to a
        //    single agent, and these are the only queued tasks nothing on GitHub would
        //    re-offer, so the list that remembers the ask has to survive a commit and
        //    the dispatch that answers one has to take it off. (Seeded empty: a headless
        //    Store reads the operator's real list, and a developer mid-sweep would
        //    otherwise fail every count below.)
        //
        //    The band, the key and the arrangement are pinned in DiplomatCoreSmoke; what
        //    is here is the wiring those rules hang off.
        store.requestedWork = []
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
        let fanOut = store.requestReviewSweep(sweep)
        check("a sweep queues one review per PR in scope, not one agent for all",
              fanOut.queued == 2 && fanOut.already == 0
                  && store.queuedTasks.map(\.id) == [key("review", 31), key("review", 32)])
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
        store.offerRequestedWork()
        store.commitQueue()
        check("a poll re-offers every ask nothing has started",
              store.queuedTasks.map(\.id) == [key("review", 31), key("review", 32), key("review", 33)])

        // …and the band puts them behind what GitHub is already owed, ahead of the
        // conflict fix another agent's run may make unnecessary. Back over the cap
        // first: a sweep queues without asking it, but a monitor's find reaches the
        // queue only by being refused, and the section above left the bay free.
        emptyBook()
        bookAgent(1)
        _ = await offer(job(35))
        _ = await offer(job(36, action: "conflicts", label: "Resolve · #36",
                            counter: .conflicts))
        store.offerRequestedWork()
        store.commitQueue()
        check("a requested review waits behind a monitor's find and ahead of a conflict fix",
              store.queuedTasks.map(\.id)
                  == [key("review-req", 35), key("review", 31), key("review", 32),
                      key("review", 33), key("conflicts", 36)])

        // Cancel is the way out of an ask the sweep should never have caught — nothing
        // GitHub does retires one while its PR is open, so a mis-aimed sweep would
        // otherwise be a day of agents nobody can call off.
        store.cancelRequestedWork(key("review", 32))
        store.offerRequestedWork()
        store.commitQueue()
        check("a cancelled ask does not come back on the next poll",
              store.requestedWork.map(\.number) == [31, 33]
                  && !store.queuedTasks.contains { $0.id == key("review", 32) })
        // Cancel refuses a row no ask stands behind. The monitor's find has to be
        // offered again to be in the queue at all: the commit above rebuilt it from
        // that cycle's offers, and that cycle offered only the asks.
        _ = await offer(job(35))
        store.offerRequestedWork()
        store.commitQueue()
        store.cancelRequestedWork(key("review-req", 35))
        check("…and a monitor's row is not cancellable at all",
              store.queuedTasks.contains { $0.id == key("review-req", 35) })

        // The other way out, and the only one nobody pressed: #33 landed while it
        // waited. The ask has to go with the row — the row is rebuilt from the ask on
        // every poll, so a list that kept it would offer the same review straight back.
        // (Still at capacity, so the drain sweeps and then returns without spawning.)
        await store.drainQueuedTasks(snaps: [], closed: ["software-mansion/argent#33"])
        check("a swept PR that landed before its turn takes its ask with it",
              store.requestedWork.map(\.number) == [31]
                  && !store.queuedTasks.contains { $0.id == key("review", 33) })
        store.offerRequestedWork()
        store.commitQueue()
        check("…so the next poll has nothing left to offer for it",
              !store.queuedTasks.contains { $0.id == key("review", 33) })

        // The dispatch is what answers an ask. #31's PR gains an agent, so the dispatch
        // is refused and the ask stands; a refusal that dropped it would silently
        // abandon the review.
        bookAgent(31)
        await store.executeQueuedTask(key("review", 31))
        check("an ask refused because the PR is busy is still asked for",
              store.requestedWork.map(\.number) == [31])
        store.error = nil
        emptyBook()
        store.requestedWork = []
        store.queuedTasks = []

        // 15. A Fix-issues scope is the same fan-out over the other numbering space.
        //    What is worth pinning here is that the two never collide: issue #31 and
        //    PR #31 are unrelated work, and only the queue verb says which a row is.
        func openIssue(_ number: Int, author: String = "alice",
                       assignees: [String] = []) -> OpenIssue {
            OpenIssue(number: number, title: "issue \(number)",
                      url: "https://github.com/software-mansion/argent/issues/\(number)",
                      author: author, authorAssociation: "MEMBER", createdAt: Date(),
                      updatedAt: Date(), commentCount: 0, assignees: assignees,
                      labels: [], memberResponded: false)
        }
        store.issues = [openIssue(31), openIssue(41), openIssue(42, assignees: ["bob"])]
        let issueFanOut = store.requestIssueSweep(IssueConfig(depth: "deep", me: "alice"))
        check("a scope queues one fix per unclaimed issue, not one agent for all",
              issueFanOut.queued == 2 && issueFanOut.already == 0
                  && store.queuedTasks.map(\.id) == [key("issues", 31), key("issues", 41)])
        check("…each labelled as the operator's own fix of that one issue",
              store.queuedTasks.map(\.job.label) == ["Issues · #31 · deep",
                                                     "Issues · #41 · deep"]
                  && store.queuedTasks.allSatisfy(\.job.requested))
        check("…and none of them PR-scoped, so issue #31 cannot dedup against PR #31, each "
              + "carrying its issue's link: \(store.queuedTasks.map(\.job.prURL))",
              store.queuedTasks.allSatisfy { $0.job.prNumber == nil && $0.job.item == nil }
                  && store.queuedTasks.map(\.job.prURL) == [31, 41].map {
                      "https://github.com/software-mansion/argent/issues/\($0)"
                  })

        // The two asks live in one list, told apart by their key alone — so a review of
        // PR #31 queued alongside is a second row, and cancelling one leaves the other.
        _ = store.requestReviewSweep(ReviewConfig(depth: "deep", target: .mine, me: "alice",
                                                  includeDrafts: true, includeReady: false))
        check("a review of the PR with the same number is a row of its own",
              store.requestedWork.map(\.key).sorted()
                  == [key("issues", 31), key("issues", 41), key("review", 31), key("review", 32)])
        store.cancelRequestedWork(key("issues", 31))
        check("…and cancelling the fix leaves the review of that number standing",
              store.requestedWork.map(\.key).sorted()
                  == [key("issues", 41), key("review", 31), key("review", 32)])
        store.requestedWork = []
        store.queuedTasks = []
        store.issues = []
        store.prs = []

        // 15b. The new-community-issue monitor queues each outsider's issue filed after
        //      its mark once, as the same ask a sweep of the community leaves.
        let filedBase = Date()
        func filed(_ number: Int, _ assoc: String = "NONE", minutes: Double = 1,
                   author: String = "") -> OpenIssue {
            let at = filedBase.addingTimeInterval(minutes * 60)
            return OpenIssue(number: number, title: "issue \(number)",
                             url: "https://github.com/software-mansion/argent/issues/\(number)",
                             author: author.isEmpty ? "outsider\(number)" : author,
                             authorAssociation: assoc, createdAt: at, updatedAt: at,
                             commentCount: 0, assignees: [], labels: [],
                             memberResponded: false)
        }
        store.communityIssuesEnabled = true
        store.queueCommunityIssues([filed(70, "MEMBER", minutes: 0)], root: argent, me: store.effectiveMe)
        check("the first community scan queues none of the backlog",
              store.requestedWork.isEmpty)
        store.queueCommunityIssues([filed(70, "MEMBER", minutes: 0), filed(71)], root: argent, me: store.effectiveMe)
        check("an outsider's issue filed after it is queued as a fix of that one issue",
              store.requestedWork.map(\.key) == [key("issues", 71)]
                  && store.requestedWork.first?.author == "outsider71"
                  && store.queuedTasks.map(\.job.label) == ["Issues · #71 · deep"]
                  && store.queuedTasks.first?.job.prompt.contains("issue #71") == true)
        check("…which re-checks its issue when its turn comes, as a swept fix does",
              store.queuedTasks.first?.job.prompt.contains("CAME OUT OF A SWEEP") == true)
        store.cancelRequestedWork(key("issues", 71))
        store.queueCommunityIssues([filed(70, "MEMBER", minutes: 0), filed(71)], root: argent, me: store.effectiveMe)
        check("…and only once, even after its ask is gone", store.requestedWork.isEmpty)
        store.communityIssuesEnabled = false
        store.communityIssuesEnabled = true
        store.queueCommunityIssues([filed(72, minutes: 2)], root: argent, me: store.effectiveMe)
        check("switched off and on, the next scan queues what was filed meanwhile not at all",
              store.requestedWork.isEmpty)
        store.queueCommunityIssues([filed(72, minutes: 2), filed(73, minutes: 3)], root: argent, me: store.effectiveMe)
        check("…and what is filed after it as before",
              store.requestedWork.map(\.key) == [key("issues", 73)])
        store.requestedWork = []
        store.queuedTasks = []

        // 16. Settles do not overlap: the tick is a suspension point, and a settle
        //     arriving across it would retire the same finished run again. The display
        //     refresh yields to one under way; every other caller waits for it. The
        //     first settle is held past its tick, so what the refresh meets is under way
        //     by construction: a yield to the scheduler does not order two tasks.
        emptyBook()
        let over = AgentRegistry.createRun(
            AgentState.RunRecord(
                runID: AgentRegistry.newRunID(now: Date().timeIntervalSince1970),
                dispatchedAt: Date().timeIntervalSince1970 - 600, prNumber: 21,
                prURL: "https://github.com/software-mansion/argent/pull/21",
                kind: "review", label: "Auto · Review · #21",
                source: AgentDispatchGate.Source.auto.rawValue, pid: 4242),
            prompt: "")!
        let reached = Latch(), released = Latch(), raced = Latch()
        Store.settleGate = {
            if reached.isOpen { raced.open() } else { reached.open() }
            await released.wait()
        }
        let settle = Task { await store.settleAgents() }
        await reached.wait()
        // Through a task with a deadline: a refresh that waits behind the settle instead
        // of yielding to it is a failure here, not a hang.
        let refreshed = Latch()
        Task { await store.refreshAutoTaskCount(); refreshed.open() }
        let yielded = await refreshed.opened(within: 2)
        check("a display refresh yields to the settle under way",
              yielded && AgentRegistry.load().map(\.runID) == [over.runID])
        // The gate is still up, so a second settle that ticked past the first would
        // reach it.
        let second = Task { await store.settleAgents() }
        let overlapped = await raced.opened(within: 2)
        check("a settle arriving while one is under way does not tick until it ends", !overlapped)
        Store.settleGate = nil
        released.open()
        _ = await (settle.value, second.value)
        let retired = AuditLog.read().filter {
            $0.action == "retire" && $0.detail.hasPrefix("Auto · Review · #21")
        }
        check("a settle arriving while one is under way waits, so a finished run is retired once",
              retired.count == 1 && AgentRegistry.load().isEmpty)

        // 17. The write-back merges into the book on disk rather than replacing it with
        //     the tick's copy. A spawn that registered while the tick resolved is in the
        //     book and not in the copy, so the copy written back is an agent nothing
        //     counts: a bay of the cap the machine can spend twice.
        bookAgent(21)
        let ticked = Latch(), written = Latch()
        Store.settleGate = { ticked.open(); await written.wait() }
        let across = Task { await store.settleAgents() }
        await ticked.wait()
        bookAgent(22)
        Store.settleGate = nil
        written.open()
        await across.value
        check("a run registered while the tick resolved is in the book after the write-back",
              AgentRegistry.load().compactMap(\.prNumber) == [21, 22])
        // 18. The auto-review allowlist, from the line the operator types to the verdict
        //    the gate returns — the setting is blank for every other section here, and
        //    that state is what those sections rest on.
        //
        //    Every offer below is over the cap, so `.atCapacity` is what "the list did
        //    not hold this" looks like: the two verdicts are what tell the author test
        //    apart from the bay test, and the allowlist outranking capacity is what
        //    makes them distinguishable at all.
        emptyBook()
        bookAgent(1)
        store.autoTaskLimit = 1
        store.queuedTasks = []
        check("a blank list holds nobody — an unknown author is refused for the bay",
              await offer(job(51, author: "bob")) == .atCapacity)
        // Blank is everyone, and blank is what an applet that has never seen this row
        // reads — the default lives in the defaults load, not in the property, so it
        // takes a Store built with the key absent to pin it.
        let storedRaw = UserDefaults.standard.string(forKey: "reviewAllowlistRaw")
        UserDefaults.standard.removeObject(forKey: "reviewAllowlistRaw")
        check("an applet that has never seen the setting reviews everyone",
              Store().reviewAllowlist.isEmpty)
        if let storedRaw { UserDefaults.standard.set(storedRaw, forKey: "reviewAllowlistRaw") }

        store.reviewAllowlistRaw = "alice, @carol"
        check("with a list set, an author outside it is held for the list instead",
              await offer(job(52, author: "bob")) == .notAllowed)
        check("…and one on it is still only ever held for the bay",
              await offer(job(53, author: "ALICE")) == .atCapacity)
        check("…including through the @ the operator may have typed",
              await offer(job(54, author: "carol")) == .atCapacity)
        // The counter is the whole test. The list speaks for the review monitor's finds
        // and nothing else, so the same author's conflict fix runs — and so does a
        // sweep, which an `source == .auto` test would wrongly have caught.
        check("a conflict fix by an unlisted author is not the list's to hold",
              await offer(job(55, action: "conflicts", label: "Resolve · #55",
                              author: "bob", counter: .conflicts)) == .atCapacity)
        check("…nor is work no monitor owns",
              await offer(job(56, action: "sweep", label: "Review · #56",
                              author: "bob", counter: nil)) == .atCapacity)
        // A held find is dropped, not queued: nothing re-offers it while the list
        // stands, so a row would promise an "execute now" the gate refuses again.
        store.commitQueue()
        check("the held find left no row behind it",
              !store.queuedTasks.contains { $0.id == key("review-req", 52) })
        store.reviewAllowlistRaw = ""
        store.queuedTasks = []
        emptyBook()

        // 19. A finished run is a finished task only if its agent answered. One whose
        //     transcript holds its prompt and then an interrupt spent nothing and did
        //     nothing, so retiring it leaves its ledger entry open. Twin of
        //     test_telemetry.py's never-answered test.
        let claude = feed.appendingPathComponent("claude")
        setenv("DIPLOMAT_CLAUDE_DIR", claude.path, 1)
        func transcript(_ name: String, _ records: [String]) {
            let dir = claude.appendingPathComponent("projects/q")
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            try? (records.joined(separator: "\n") + "\n")
                .write(to: dir.appendingPathComponent(name), atomically: true, encoding: .utf8)
        }
        transcript("cut.jsonl", [#"{"type":"user","message":{"content":"price #31"}}"#,
                                 #"{"type":"user","message":{"content":"[Request interrupted by user]"}}"#])
        transcript("ran.jsonl", [#"{"type":"user","message":{"content":"price #32"}}"#,
                                 #"{"type":"assistant","cwd":"/x","message":{"usage":{"input_tokens":700,"output_tokens":0,"cache_creation_input_tokens":0}}}"#])
        for n in [31, 32] {
            AgentRegistry.createRun(
                AgentState.RunRecord(
                    runID: AgentRegistry.newRunID(now: Date().timeIntervalSince1970),
                    dispatchedAt: Date().timeIntervalSince1970 - 600, prNumber: n,
                    kind: "review", label: "Auto · Review · #\(n)",
                    ledgerKey: "review:q#\(n)@aa", pid: 4242),
                prompt: "price #\(n)")
        }
        _ = await store.settleAgents()
        let settled = Dictionary(TelemetryLog.load().tasks.map { ($0.key, $0) },
                                 uniquingKeysWith: { a, _ in a })
        check("a retired run whose agent never answered is not a finished task",
              AgentRegistry.load().isEmpty && settled["review:q#31@aa"] == nil)
        check("…while one that answered is, priced from its transcript",
              settled["review:q#32@aa"]?.tokens == 700)
        unsetenv("DIPLOMAT_CLAUDE_DIR")

        // 20. A freed bay, or an ask, brings the next poll forward: the queue drains only
        //     at the top of one. Never sooner than the floor after the last poll started,
        //     and one poll for every bay that frees while it waits. `gh` is a stand-in
        //     that logs each call and names no login, so a poll stops at its viewer
        //     read and every line it logs is one poll.
        let ghLog = feed.appendingPathComponent("gh.log")
        let ghStub = feed.appendingPathComponent("gh")
        try? "#!/bin/sh\necho call >> '\(ghLog.path)'\necho '{\"data\":{\"viewer\":{\"login\":\"\"}}}'\n"
            .write(to: ghStub, atomically: true, encoding: .utf8)
        chmod(ghStub.path, 0o755)
        let realGH = ProcessInfo.processInfo.environment["DIPLOMAT_GH"]
        setenv("DIPLOMAT_GH", ghStub.path, 1)
        func polls() -> Int {
            ((try? String(contentsOf: ghLog, encoding: .utf8)) ?? "")
                .split(separator: "\n").count
        }
        /// When the poll count reached `n`, or nil if it had not inside `seconds`.
        func pollNumber(_ n: Int, within seconds: Double) async -> Date? {
            let deadline = Date().addingTimeInterval(seconds)
            while polls() < n, Date() < deadline {
                try? await Task.sleep(nanoseconds: 20_000_000)
            }
            return polls() >= n ? Date() : nil
        }
        func ask(_ number: Int) {
            store.requestOne(Store.RequestedWork(
                number: number, url: "https://github.com/software-mansion/argent/pull/\(number)",
                author: "", config: .review(sweep.forPR(number))))
        }
        emptyBook()
        store.requestedWork = []
        store.queuedTasks = []
        store.usernameOverride = ""
        store.queueAutoRun = true
        store.autoTaskLimit = 3
        let floor: TimeInterval = 3
        store.earlyPollFloor = floor
        store.pollsEarly = true
        for n in 61...63 { bookAgent(n) }
        _ = await store.settleAgents()
        ask(64)
        check("an ask with no bay free brings no poll forward",
              await pollNumber(1, within: 1) == nil)

        let started = Date()
        await store.runAutofixPollOnce()
        // Each agent ends the way a real one does - its process gone, past the spawn
        // grace - and is retired by the settle that sees it.
        for _ in 61...63 {
            guard let run = AgentRegistry.load().first(where: { $0.pid == nil }) else { break }
            AgentRegistry.update {
                $0.map { r in
                    guard r.runID == run.runID else { return r }
                    var gone = r
                    gone.pid = 4242
                    gone.dispatchedAt -= 600
                    return gone
                }
            }
            _ = await store.settleAgents()
        }
        check("three bays freed and their runs retired",
              AgentRegistry.load().isEmpty && store.autoTasksMeasured == 0)
        let early = await pollNumber(2, within: floor + 3)
        check("a freed bay brings the next poll forward to the floor",
              early.map { $0.timeIntervalSince(started) >= floor } == true)
        check("…not to the loop's interval",
              early.map { $0.timeIntervalSince(started) < floor + 3 } == true)
        check("three bays freed while it waited are one poll, not three",
              await pollNumber(3, within: 2 * floor + 1) == nil)
        let tick = Date().addingTimeInterval(store.secondsUntilPoll(gap: Store.autofixPollInterval))
        check("the loop's next tick is an interval after that poll, not after the one before",
              early.map { abs(tick.timeIntervalSince($0) - Store.autofixPollInterval) < 1 } == true)

        // The ask arriving with room is the other trigger. The last poll started over a
        // floor ago, so nothing holds it.
        let asked = Date()
        ask(65)
        let forAsk = await pollNumber(3, within: 2)
        check("an ask with a bay free brings the next poll forward",
              forAsk.map { $0.timeIntervalSince(asked) < 2 } == true)

        // So is the operator raising the cap over a machine that is full.
        bookAgent(66)
        _ = await store.settleAgents()
        store.autoTaskLimit = 1
        ask(67)
        let raised = Date()
        store.autoTaskLimit = 2
        let forLimit = await pollNumber(4, within: floor + 2)
        check("a raised cap brings the next poll forward",
              forLimit.map { $0.timeIntervalSince(raised) < floor + 2 } == true)

        store.pollsEarly = false
        emptyBook()
        store.requestedWork = []
        store.queuedTasks = []
        if let realGH { setenv("DIPLOMAT_GH", realGH, 1) } else { unsetenv("DIPLOMAT_GH") }

        // 21. Two repo roots. One poll reads both, and the same number in each is two
        //     pieces of work: two queue rows, two ledger entries, each run in its own
        //     checkout. `gh` is a stand-in answering per repo: one PR number conflicting in both,
        //     and another one's review asked in both.
        let rootA = PRRef.LocalRepo(owner: "o1", repo: "alpha", path: feed.appendingPathComponent("alpha").path)
        let rootB = PRRef.LocalRepo(owner: "o2", repo: "beta", path: feed.appendingPathComponent("beta").path)
        store.pinRoots([rootA, rootB])
        store.selectedRepo = ""
        let multiLog = feed.appendingPathComponent("gh-multi.log")
        let multiStub = feed.appendingPathComponent("gh-multi")
        func monitorPR(_ slug: String, _ sha: String) -> String {
            #"{"data":{"search":{"nodes":[{"number":90007,"title":"t","url":"https://github.com/\#(slug)/pull/90007","isDraft":false,"mergeable":"CONFLICTING","reviewDecision":"","headRefOid":"\#(sha)","reviewThreads":{"nodes":[]}}]}}}"#
        }
        func requested(_ slug: String, _ sha: String) -> String {
            #"{"data":{"search":{"nodes":[{"number":90005,"title":"t","url":"https://github.com/\#(slug)/pull/90005","author":{"login":"qt-outsider"},"authorAssociation":"NONE","headRefOid":"\#(sha)"}]}}}"#
        }
        let none = #"{"data":{"search":{"nodes":[]}}}"#
        let stubScript = """
            #!/bin/sh
            echo "$*" | grep -o 'q=repo:[^ ]* [^ ]*' >> '\(multiLog.path)'
            case "$*" in
              *"q=repo:o1/alpha author:"*) echo '\(monitorPR("o1/alpha", "a7"))' ;;
              *"q=repo:o2/beta author:"*) echo '\(monitorPR("o2/beta", "b7"))' ;;
              *"q=repo:o1/alpha review-requested:"*) echo '\(requested("o1/alpha", "a5"))' ;;
              *"q=repo:o2/beta review-requested:"*) echo '\(requested("o2/beta", "b5"))' ;;
              *"is:closed"*) echo '\(none)' ;;
              *) echo '{"data":{"viewer":{"login":"alice"}}}' ;;
            esac
            """
        try? stubScript.write(to: multiStub, atomically: true, encoding: .utf8)
        chmod(multiStub.path, 0o755)
        setenv("DIPLOMAT_GH", multiStub.path, 1)
        emptyBook()
        store.requestedWork = []
        store.queuedTasks = []
        store.usernameOverride = "alice"
        store.communityIssuesEnabled = false
        store.autoTaskLimit = 1
        bookAgent(90, ahead: 86_400)
        _ = await store.settleAgents()
        // A poll the early-poll section above brought forward may still be finishing;
        // this one only counts once the stand-in has been asked.
        for _ in 0..<50 {
            await store.runAutofixPollOnce()
            if FileManager.default.fileExists(atPath: multiLog.path) { break }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        let multiAsked = ((try? String(contentsOf: multiLog, encoding: .utf8)) ?? "")
        check("one poll asks every root for my PRs, closed PRs and review requests",
              ["o1/alpha author:alice", "o2/beta author:alice",
               "o1/alpha is:pr", "o2/beta is:pr",
               "o1/alpha review-requested:alice", "o2/beta review-requested:alice"]
                  .allSatisfy { multiAsked.contains("q=repo:" + $0) })
        let rows = Dictionary(store.queuedTasks.map { ($0.id, $0.job) },
                              uniquingKeysWith: { a, _ in a })
        let conflictA = AgentTaskQueue.key(auditAction: "conflicts", item: "o1/alpha#90007")
        let conflictB = AgentTaskQueue.key(auditAction: "conflicts", item: "o2/beta#90007")
        let reviewA = AgentTaskQueue.key(auditAction: "review-req", item: "o1/alpha#90005")
        let reviewB = AgentTaskQueue.key(auditAction: "review-req", item: "o2/beta#90005")
        check("the same number in two roots is two queued rows per verb",
              Set(rows.keys) == [conflictA, conflictB, reviewA, reviewB])
        check("…each working in its own root's checkout",
              rows[conflictA]?.directory == rootA.path && rows[conflictB]?.directory == rootB.path
                  && rows[reviewA]?.directory == rootA.path && rows[reviewB]?.directory == rootB.path)
        check("…its prompt aimed at that root's repo",
              rows[conflictB]?.prompt.contains("o2/beta") == true
                  && rows[conflictB]?.prompt.contains("o1/alpha") == false)
        check("…and labelled with the repo, there being more than one root",
              rows[conflictA]?.label == "Resolve · alpha#90007" && rows[conflictB]?.label == "Resolve · beta#90007")
        let pending = Set(TelemetryLog.load().tasks
            .filter { $0.queuedAt != nil && $0.clearedAt == nil }.map(\.key))
        let owedKeys = [
            AutofixMesh.ledgerKey(kind: AutofixMesh.kindConflicts,
                                  prURL: "https://github.com/o1/alpha/pull/90007", headSha: "a7"),
            AutofixMesh.ledgerKey(kind: AutofixMesh.kindConflicts,
                                  prURL: "https://github.com/o2/beta/pull/90007", headSha: "b7"),
            AutofixMesh.ledgerKey(kind: AutofixMesh.kindReviewReq,
                                  prURL: "https://github.com/o1/alpha/pull/90005", headSha: "a5"),
            AutofixMesh.ledgerKey(kind: AutofixMesh.kindReviewReq,
                                  prURL: "https://github.com/o2/beta/pull/90005", headSha: "b5"),
        ]
        check("the ledger holds every root's owed work, none cleared by another root's read",
              owedKeys.allSatisfy { pending.contains($0) })
        store.queuedTasks = []
        emptyBook()
        if let realGH { setenv("DIPLOMAT_GH", realGH, 1) } else { unsetenv("DIPLOMAT_GH") }

        // 22. Each root keeps its own community-issue mark: a root's first scan starts
        //     from then, whatever another root's mark says.
        func filedIn(_ root: PRRef.LocalRepo, _ number: Int, minutes: Double) -> OpenIssue {
            let at = filedBase.addingTimeInterval(minutes * 60)
            return OpenIssue(number: number, title: "issue \(number)",
                             url: "https://github.com/\(root.slug)/issues/\(number)",
                             author: "outsider\(number)", authorAssociation: "NONE",
                             createdAt: at, updatedAt: at, commentCount: 0, assignees: [],
                             labels: [], memberResponded: false)
        }
        store.requestedWork = []
        store.communityIssuesEnabled = true
        store.queueCommunityIssues([filedIn(rootA, 80, minutes: 10)], root: rootA, me: store.effectiveMe)
        store.queueCommunityIssues([filedIn(rootB, 80, minutes: 11)], root: rootB, me: store.effectiveMe)
        check("a root's first community scan queues none of its backlog, though filed after "
              + "another root's mark", store.requestedWork.isEmpty)
        store.queueCommunityIssues([filedIn(rootA, 80, minutes: 10), filedIn(rootA, 81, minutes: 12)],
                                   root: rootA, me: store.effectiveMe)
        store.queueCommunityIssues([filedIn(rootB, 80, minutes: 11), filedIn(rootB, 82, minutes: 13)],
                                   root: rootB, me: store.effectiveMe)
        let fixA = AgentTaskQueue.key(auditAction: "issues", item: "o1/alpha#81")
        let fixB = AgentTaskQueue.key(auditAction: "issues", item: "o2/beta#82")
        check("…and each root's later issue is queued as a fix in that root",
              store.requestedWork.map(\.key) == [fixA, fixB]
                  && store.requestedWork.map(\.repo) == ["o1/alpha", "o2/beta"]
                  && store.queuedTasks.first { $0.id == fixB }?.job.directory == rootB.path)
        store.requestedWork = []
        store.queuedTasks = []
        store.pinRoots([rootA])
        store.queueCommunityIssues([filedIn(rootA, 81, minutes: 12)], root: rootA, me: store.effectiveMe)
        store.pinRoots([rootA, rootB])
        store.queueCommunityIssues([filedIn(rootB, 83, minutes: 14), filedIn(rootB, 84, minutes: 15),
                                    filedIn(rootB, 85, minutes: 16)], root: rootB, me: store.effectiveMe)
        check("a root removed and added back queues none of what was filed meanwhile",
              store.requestedWork.isEmpty)
        store.pinRoots([rootA], complete: false)
        store.queueCommunityIssues([filedIn(rootA, 81, minutes: 12)], root: rootA, me: store.effectiveMe)
        store.pinRoots([rootA, rootB])
        store.queueCommunityIssues([filedIn(rootB, 85, minutes: 16), filedIn(rootB, 86, minutes: 17)],
                                   root: rootB, me: store.effectiveMe)
        check("a root whose path failed to resolve for a poll keeps its mark: what it filed meanwhile is queued",
              store.requestedWork.map(\.key) == [AgentTaskQueue.key(auditAction: "issues", item: "o2/beta#86")])
        store.communityIssuesEnabled = false
        store.requestedWork = []
        store.queuedTasks = []

        // 23. An ask is re-homed from its repo each time it is offered, and dropped (said
        //     in the feed) once that repo is no longer a root.
        var rootSweep = ReviewConfig(depth: "deep", target: .mine, me: "alice",
                                     includeDrafts: true, includeReady: false)
        rootSweep.home = rootB
        store.requestOne(Store.RequestedWork(number: 9, url: "https://github.com/o2/beta/pull/9",
                                             author: "", config: .review(rootSweep.forPR(9))))
        rootSweep.home = rootA
        store.requestOne(Store.RequestedWork(number: 9, url: "https://github.com/o1/alpha/pull/9",
                                             author: "", config: .review(rootSweep.forPR(9))))
        let askA = AgentTaskQueue.key(auditAction: "review", item: "o1/alpha#9")
        let askB = AgentTaskQueue.key(auditAction: "review", item: "o2/beta#9")
        check("an ask on each root's #9 is a row of its own, working in that root",
              store.queuedTasks.map(\.id).sorted() == [askA, askB]
                  && store.queuedTasks.first { $0.id == askB }?.job.directory == rootB.path)
        store.pinRoots([rootA], complete: false)
        store.offerRequestedWork()
        store.commitQueue()
        check("while a root path fails to resolve, an ask in a repo missing from the roots "
              + "is kept but not offered",
              store.requestedWork.map(\.key).sorted() == [askA, askB].sorted()
                  && store.queuedTasks.map(\.id) == [askA])
        store.pinRoots([rootA])
        store.offerRequestedWork()
        let feedText = (try? String(contentsOf: feed.appendingPathComponent("audit.jsonl"),
                                    encoding: .utf8))?
            .replacingOccurrences(of: "\\/", with: "/") ?? ""
        check("removing a root drops its asks and keeps the others'",
              store.requestedWork.map(\.key) == [askA])
        check("…and says so in the feed",
              feedText.contains(" - o2/beta is no longer a repo root, not run"))
        store.pinRoots([rootA, rootB])
        store.requestedWork = []
        store.queuedTasks = []

        // 24. Only the configured repo's root's work is the mesh's to place, the primary
        //     root being another repo here.
        func routed(_ directory: String?) -> Store.AgentJob {
            Store.AgentJob(kind: "review", auditAction: "review-req", label: "", prompt: "",
                           prURL: nil, prNumber: nil, authorLogin: nil, duty: "review",
                           workKey: "review-req:github.com/o2/beta#5@b5", counter: nil,
                           directory: directory)
        }
        let (cfgOwner, cfgRepo) = CoreAssets.repoCoordinates()
        let configuredRoot = PRRef.LocalRepo(owner: cfgOwner.uppercased(), repo: cfgRepo,
                                             path: feed.appendingPathComponent("configured").path)
        store.pinRoots([rootA, configuredRoot])
        check("a job in the configured repo's root may go over the mesh",
              store.meshRoutable(routed(configuredRoot.path)))
        check("…one in the primary root, another repo, may not, nor one naming no checkout",
              !store.meshRoutable(routed(rootA.path)) && !store.meshRoutable(routed(nil)))
        check("…nor one in an OTHER REPOS checkout",
              !store.meshRoutable(routed(feed.appendingPathComponent("other").path)))
        check("a wizard run goes over the mesh only from the configured repo's root",
              store.meshRoutable(checkout: configuredRoot.path) && !store.meshRoutable(checkout: rootA.path))
        store.pinRoots([configuredRoot, rootB])
        check("with the configured repo the primary root, a job naming no checkout may",
              store.meshRoutable(routed(nil)) && !store.meshRoutable(routed(rootB.path)))
        store.pinRoots([rootA, rootB])
        check("with the configured repo in no root, nothing may",
              !store.meshRoutable(routed(rootA.path)) && !store.meshRoutable(routed(nil)))
        let gitlabNamesake = PRRef.LocalRepo(owner: cfgOwner, repo: cfgRepo,
                                             path: feed.appendingPathComponent("namesake").path,
                                             host: "gitlab.example.com")
        store.pinRoots([gitlabNamesake, rootB])
        check("a GitLab project of the configured repo's name is not the configured repo",
              !store.meshRoutable(routed(nil)) && !store.meshRoutable(checkout: gitlabNamesake.path))
        store.pinRoots([rootA, rootB])

        // 24a. "Me" in a GitLab root: glab's user on its host, cached only once read.
        let glabStub = feed.appendingPathComponent("glab")
        let glabCalls = feed.appendingPathComponent("glab-calls")
        // A merge-status recheck answers from glab-rest, logging its path; the rest from glab-out.
        try? ("#!/bin/sh\necho x >> '\(glabCalls.path)'\n"
              + "case \"$*\" in *with_merge_status_recheck*) echo \"$3 $4\" >> '\(feed.path)/glab-rechecks'\n"
              + "  cat '\(feed.path)/glab-rest'; exit \"$(cat '\(feed.path)/glab-rest-code')\" ;; esac\n"
              + "printf '%s\\n' \"$@\" > '\(feed.path)/glab-argv'\n"
              + "cat '\(feed.path)/glab-out'\nexit 0\n")
            .write(to: glabStub, atomically: true, encoding: .utf8)
        chmod(glabStub.path, 0o755)
        setenv("DIPLOMAT_GLAB", glabStub.path, 1)
        func glabAnswers(_ s: String) {
            try? s.write(to: feed.appendingPathComponent("glab-out"), atomically: true, encoding: .utf8)
        }
        func glabCallCount() -> Int {
            ((try? String(contentsOf: glabCalls, encoding: .utf8)) ?? "").split(separator: "\n").count
        }
        func meIn(_ root: PRRef.LocalRepo) async -> String {
            do { return try await store.me(for: root) }
            catch { return "error: \((error as? LocalizedError)?.errorDescription ?? "\(error)")" }
        }
        glabAnswers(#"{"data":{"currentUser":null}}"#)
        let onGitLab = PRRef.LocalRepo(owner: "g/sub", repo: "p", path: "", host: "gitlab.example.com")
        let signedOut = await meIn(onGitLab)
        check("a GitLab host glab is not signed in to has no me: \(signedOut)",
              signedOut == "error: not signed in to gitlab.example.com"
                && store.knownMe(for: onGitLab).isEmpty)
        glabAnswers(#"{"data":{"currentUser":{"username":"gl-me"}}}"#)
        let glFirst = await meIn(onGitLab)
        let glSecond = await meIn(PRRef.LocalRepo(owner: "x", repo: "y", path: "", host: "gitlab.example.com"))
        check("…once signed in, glab's user, asked once per host: \(glFirst) \(glabCallCount())",
              glFirst == "gl-me" && glSecond == "gl-me" && glabCallCount() == 2
                && store.knownMe(for: onGitLab) == "gl-me")
        let overrideBefore = store.usernameOverride
        store.usernameOverride = "alice"
        check("…while a GitHub root's me stays the GitHub one",
              await meIn(rootA) == "alice" && store.knownMe(for: rootA) == "alice"
                && store.knownMe(for: nil) == "alice")
        // A link to a GitLab item from the GitHub root shown runs as glab's user; a sweep as
        // the shown root's me.
        let rootsBefore = store.roots
        store.pinRoots([rootA, onGitLab])
        let mrLink = "https://gitlab.example.com/g/sub/p/-/merge_requests/3"
        let wizardMes = [store.wizardRun(ReviewConfig(target: .specific, specificPR: mrLink)).me,
                         store.wizardRun(IssueConfig(target: .specific, specificIssue: "g/sub/p#4")).me,
                         store.wizardRun(ConflictConfig(target: .specific, specificPR: mrLink)).me,
                         store.wizardRun(ReviewConfig(target: .mine, specificPR: mrLink)).me]
        check("…and a wizard's run takes the me of the root it targets: \(wizardMes)",
              store.selectedRoot == rootA && wizardMes == ["gl-me", "gl-me", "gl-me", "alice"])
        store.pinRoots(rootsBefore)
        store.usernameOverride = overrideBefore

        // 24a'. The monitors' GitLab reads, on the shared parity fixtures.
        let fixtures = (try? CoreAssets.assetsDir())?.deletingLastPathComponent()
            .appendingPathComponent("Tests/fixtures/gitlab")
        func fixture(_ name: String) -> String {
            fixtures.flatMap { try? String(contentsOf: $0.appendingPathComponent(name), encoding: .utf8) } ?? ""
        }
        func searchNodes(_ name: String) -> Int {
            let obj = (try? JSONSerialization.jsonObject(with: Data(fixture(name).utf8))) as? [String: Any]
            let search = (obj?["data"] as? [String: Any])?["search"] as? [String: Any]
            return (search?["nodes"] as? [Any])?.count ?? -1
        }
        func glabArgv() -> [String] {
            ((try? String(contentsOf: feed.appendingPathComponent("glab-argv"), encoding: .utf8)) ?? "")
                .split(separator: "\n").map(String.init)
        }
        let rechecks = feed.appendingPathComponent("glab-rechecks")
        func glabRest(_ s: String, code: Int = 0) {
            try? s.write(to: feed.appendingPathComponent("glab-rest"), atomically: true, encoding: .utf8)
            try? "\(code)".write(to: feed.appendingPathComponent("glab-rest-code"), atomically: true,
                                 encoding: .utf8)
            try? FileManager.default.removeItem(at: rechecks)
        }
        func recheckCalls() -> [String] {
            ((try? String(contentsOf: rechecks, encoding: .utf8)) ?? "").split(separator: "\n").map(String.init)
        }
        let graphqlOnly = ((try? AutofixMonitor.parse(GitLab.monitorPRs(Data(fixture("monitor-prs.input.json").utf8))))
            ?? []).map { ($0.number, $0.mergeable) }
        let unchecked = graphqlOnly.filter { $0.1 == "UNKNOWN" }.map(\.0)
        glabRest(#"{"iid":0,"has_conflicts":true,"detailed_merge_status":"conflict"}"#)
        glabAnswers(fixture("monitor-prs.input.json"))
        let snaps = (try? await AutofixMonitor.fetchSnapshots(root: onGitLab, me: "gl-me")) ?? []
        check("a GitLab root's open merge requests are snapshotted: \(snaps.count)",
              !snaps.isEmpty && snaps.count == searchNodes("monitor-prs.expected.json")
                && glabArgv().contains("path=g/sub/p") && glabArgv().contains("author=gl-me"))
        // GitLab checks a merge status only when asked, and no GraphQL read asks.
        check("…each one GitLab has not checked read again through the REST read that rechecks "
              + "it, whose definite answer stands: \(recheckCalls()) \(snaps.map { "\($0.number) \($0.mergeable)" })",
              !unchecked.isEmpty
                && recheckCalls() == unchecked.map {
                    "gitlab.example.com projects/g%2Fsub%2Fp/merge_requests/\($0)?with_merge_status_recheck=true"
                }
                && snaps.map { ($0.number, $0.mergeable) }.elementsEqual(graphqlOnly.map {
                    ($0.0, $0.1 == "UNKNOWN" ? "CONFLICTING" : $0.1)
                }, by: { $0 == $1 }))
        glabRest(#"{"iid":0,"has_conflicts":false,"detailed_merge_status":"checking"}"#)
        let stillChecking = (try? await AutofixMonitor.fetchSnapshots(root: onGitLab, me: "gl-me")) ?? []
        glabRest("", code: 1)
        let recheckFailed = (try? await AutofixMonitor.fetchSnapshots(root: onGitLab, me: "gl-me")) ?? []
        check("…while one still checking, or whose recheck fails, reads unknown, the root read all the same",
              [stillChecking, recheckFailed].allSatisfy {
                  $0.map { ($0.number, $0.mergeable) }.elementsEqual(graphqlOnly, by: { $0 == $1 })
              } && recheckCalls().count == unchecked.count)
        glabAnswers(fixture("closed-prs.input.json"))
        let closedMRs = (try? await AutofixMonitor.fetchClosedPRs(root: onGitLab)) ?? []
        check("…its closed ones keyed by its host and nested slug: \(closedMRs.sorted())",
              !closedMRs.isEmpty && closedMRs.count == searchNodes("closed-prs.expected.json")
                && closedMRs.allSatisfy { $0.hasPrefix("gitlab.example.com/g/sub/p#") })
        glabAnswers(fixture("review-requests.input.json"))
        let asks = (try? await AutofixMonitor.fetchReviewRequests(root: onGitLab, me: "gl-me",
                                                                 includeFiles: true)) ?? []
        check("…and its review requests: \(asks.count)",
              !asks.isEmpty && asks.count == searchNodes("review-requests.expected.json")
                && glabArgv().contains("reviewer=gl-me") && glabArgv().contains("withFiles=true"))
        glabAnswers(fixture("review-requests-window.input.json"))
        let windowed = (try? await AutofixMonitor.fetchReviewRequests(root: onGitLab, me: "carol")) ?? []
        check("…a request no note in the window names dates from its oldest note, so my comment "
              + "there answers it, and from the merge request with no notes: "
              + "\(windowed.map { "\($0.number) \($0.requestedAt ?? "nil") \($0.oweReview)" })",
              windowed.map(\.number) == [31, 30]
                && windowed.map(\.requestedAt) == ["2026-11-06T08:00:00Z", "2026-11-02T07:00:00Z"]
                && windowed.map(\.oweReview) == [false, true])
        glabAnswers(#"{"data":{"project":{"mergeRequest":{"state":"merged","author":null}}}}"#)
        let landed = await AgentProbes.mergedPRs(["gitlab.example.com/g/sub/p#7"], roots: [rootA, onGitLab])
        check("a GitLab root's merged merge request is asked of glab on its host",
              { if case .present(let s) = landed { return s == ["gitlab.example.com/g/sub/p#7"] }; return false }()
                && glabArgv().contains("gitlab.example.com")
                && glabArgv().contains("iid=7"))
        // gh and glab both answer MERGED, so any wrong ask shows.
        let mergedGH = feed.appendingPathComponent("gh-merged")
        let ghArgv = feed.appendingPathComponent("gh-argv")
        try? ("#!/bin/sh\nprintf '%s ' \"$@\" >> '\(ghArgv.path)'\necho >> '\(ghArgv.path)'\n"
              + "echo '{\"state\":\"MERGED\"}'\n").write(to: mergedGH, atomically: true, encoding: .utf8)
        chmod(mergedGH.path, 0o755)
        let ghBefore = ProcessInfo.processInfo.environment["DIPLOMAT_GH"]
        setenv("DIPLOMAT_GH", mergedGH.path, 1)
        let callsBefore = glabCallCount()
        let strayKey = await AgentProbes.mergedPRs(["gitlab.example.com/g/gone#7", "localhost/g/gone#7",
                                                    "o/gone#7"], roots: [rootA])
        if let ghBefore { setenv("DIPLOMAT_GH", ghBefore, 1) } else { unsetenv("DIPLOMAT_GH") }
        let ghAsked = ((try? String(contentsOf: ghArgv, encoding: .utf8)) ?? "")
            .split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }
        check("…while a GitLab key no root has, a dotless host's too, is asked of no host, so it "
              + "is never merged; a GitHub one is asked of GitHub: \(strayKey) \(ghAsked)",
              { if case .present(let s) = strayKey { return s == ["o/gone#7"] }; return false }()
                && glabCallCount() == callsBefore
                && ghAsked == ["pr view 7 --repo o/gone --json state"])

        // 24a''. An unchecked GitLab merge request keeps its queued conflict fix (GitLab
        //        has not looked yet); a GitHub PR's UNKNOWN drops it.
        store.pinRoots([rootA, onGitLab])
        store.requestedWork = []
        store.queuedTasks = []
        store.prAutofixEnabled = true
        store.autoTaskLimit = 1
        bookAgent(1)
        let mrURL = "https://gitlab.example.com/g/sub/p/-/merge_requests/8"
        let alphaURL = "https://github.com/o1/alpha/pull/8"
        _ = await offer(job(8, action: "conflicts", label: "Resolve · p#8", counter: .conflicts, url: mrURL))
        _ = await offer(job(8, action: "conflicts", label: "Resolve · alpha#8", counter: .conflicts,
                            url: alphaURL))
        store.commitQueue()
        let mrFix = AgentTaskQueue.key(auditAction: "conflicts", item: "gitlab.example.com/g/sub/p#8")
        let alphaFix = AgentTaskQueue.key(auditAction: "conflicts", item: "o1/alpha#8")
        func unknown(_ url: String) -> PRSnapshot {
            PRSnapshot(number: 8, title: "t", url: url, isDraft: false, mergeable: "UNKNOWN",
                       reviewDecision: "", threadsUnresolved: 0)
        }
        func fixes() -> Set<String> { Set(store.queuedTasks.map(\.id)).intersection([mrFix, alphaFix]) }
        let queuedBoth = fixes() == [mrFix, alphaFix]
        await store.drainQueuedTasks(snaps: [unknown(mrURL), unknown(alphaURL)], closed: [],
                                     read: [rootA, onGitLab])
        check("a conflict fix on a GitLab merge request reading unchecked stays queued, one on a "
              + "GitHub PR reading UNKNOWN leaves: \(store.queuedTasks.map(\.id))",
              queuedBoth && fixes() == [mrFix])
        // The cycle's commit keeps what the cycle offered.
        let otherMR = "https://gitlab.example.com/g/sub/p/-/merge_requests/9"
        let otherFix = AgentTaskQueue.key(auditAction: "conflicts", item: "gitlab.example.com/g/sub/p#9")
        let cycle = [unknown(mrURL), unknown(alphaURL),
                     PRSnapshot(number: 9, title: "t", url: otherMR, isDraft: false, mergeable: "UNKNOWN",
                                reviewDecision: "", threadsUnresolved: 0)]
        await store.reconcileMyConflicts(snaps: cycle, now: Date())
        store.commitQueue()
        check("…and through the cycle's commit, while an unchecked merge request with no fix queued "
              + "gets none: \(store.queuedTasks.map(\.id))",
              fixes() == [mrFix] && !store.queuedTasks.contains { $0.id == otherFix })
        emptyBook()
        store.queuedTasks = []

        // 24a'''. A GitLab root whose glab user is unknown is held, not run as nobody; its
        //         mark waits for the next read.
        let signedOutRoot = PRRef.LocalRepo(owner: "g", repo: "lab", path: "/w/lab",
                                            host: "gitlab.signedout.example")
        store.pinRoots([signedOutRoot])
        store.requestedWork = []
        store.communityIssuesEnabled = true
        store.queueCommunityIssues([filedIn(signedOutRoot, 1, minutes: -60 * 24 * 60)], root: signedOutRoot,
                                   me: "")
        // One answer for every query: the issues read succeeds, the viewer read finds no user.
        glabAnswers(fixture("issues.input.json"))
        await store.pollCommunityIssues([signedOutRoot])
        let heldAsks = store.requestedWork.map(\.key)
        store.pinGitLabMe("lab-me", host: signedOutRoot.host)
        await store.pollCommunityIssues([signedOutRoot])
        let fixes = store.requestedWork.compactMap { w -> String? in
            if case .issues(let cfg) = w.config { return "\(w.key) \(cfg.me)" }
            return nil
        }
        // A later section counts the feed's root-read lines from the start: drop these.
        let auditFeed = feed.appendingPathComponent("audit.jsonl")
        let auditLines = ((try? String(contentsOf: auditFeed, encoding: .utf8)) ?? "").split(separator: "\n")
        func isLab(_ line: Substring) -> Bool {
            let obj = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any]
            return (obj?["detail"] as? String)?.hasPrefix("Issues of g/lab ") == true
        }
        let labLines = auditLines.filter(isLab).map { line in
            ((try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any])?["detail"] as? String ?? ""
        }
        check("a GitLab root whose glab user is unknown queues no community fix, logged as unread, "
              + "and keeps its mark: \(heldAsks) then \(fixes)",
              heldAsks.isEmpty && !fixes.isEmpty && fixes.allSatisfy { $0.hasSuffix(" lab-me") }
                && labLines.first?.contains("not signed in to gitlab.signedout.example") == true)
        try? (auditLines.filter { !isLab($0) }.joined(separator: "\n") + "\n")
            .write(to: auditFeed, atomically: true, encoding: .utf8)
        store.requestedWork = []
        store.queuedTasks = []
        store.communityIssuesEnabled = false
        store.pinRoots([rootA, rootB])
        unsetenv("DIPLOMAT_GLAB")

        // 24b. A review-request record outlives a poll its root's path failed to resolve
        //      for: o/other#7's run concluded long ago and the request is still owed.
        //      A poll of o/r alone keeps what `retainedReviewReqAttempts` returns; the
        //      next poll, of both roots, decides on that.
        let polledR = PRRef.LocalRepo(owner: "o", repo: "r", path: "/src/r")
        let reqAt = "2026-11-01T10:00:00Z"
        let concludedRec = ReviewAttempt(
            requestedAt: reqAt,
            lastDispatchedAt: Date().addingTimeInterval(-ReviewReconcile.retryMaxBackoff - 600),
            attempts: 1, concludedAt: Date().addingTimeInterval(-ReviewReconcile.retryMaxBackoff))
        func afterTwoPolls(middleComplete: Bool) -> ReviewReconcile.Decision {
            let kept = Store.retainedReviewReqAttempts(["o/other#7": concludedRec], owed: [:],
                                                       polled: [polledR], complete: middleComplete,
                                                       now: Date())
            return ReviewReconcile.decide(prior: kept["o/other#7"], stamp: reqAt, inFlight: false,
                                          banned: false, now: Date())
        }
        check("a concluded review request in a root that failed to resolve for a poll is not reviewed again",
              afterTwoPolls(middleComplete: false) == .skipConcluded)
        check("…while one in a root gone from a complete set of roots is dropped",
              afterTwoPolls(middleComplete: true) == .dispatch(attemptNumber: 1))
        check("…and one in a polled root that no longer owes it is dropped, resolved or not",
              Store.retainedReviewReqAttempts(["o/r#7": concludedRec], owed: [:], polled: [polledR],
                                              complete: false, now: Date()).isEmpty)

        // 25. The selected root decides which lists the right column reads.
        func rootPR(_ root: PRRef.LocalRepo, _ number: Int) -> OpenPR {
            OpenPR(number: number, title: "PR \(number)",
                   url: "https://github.com/\(root.slug)/pull/\(number)",
                   isDraft: true, author: "alice", createdAt: Date(),
                   readyForReviewAt: nil, files: [], reviewDecision: nil, reviewThreads: [])
        }
        store.seedPanelLists(key: "o1/alpha", prs: [rootPR(rootA, 1)], issues: [])
        store.seedPanelLists(key: "o2/beta", prs: [rootPR(rootB, 2), rootPR(rootB, 3)], issues: [])
        check("with none chosen, the panel shows the primary root's PRs",
              store.prs.map(\.url) == ["https://github.com/o1/alpha/pull/1"])
        store.selectedRepo = "o2/beta"
        check("choosing a root shows its PRs",
              store.selectedRoot == rootB && store.prs.map(\.number) == [2, 3])
        let swept = store.requestReviewSweep(ReviewConfig(depth: "deep", target: .mine, me: "alice",
                                                          includeDrafts: true, includeReady: false))
        check("…and a sweep asks for that root's PRs",
              swept.queued == 2 && store.requestedWork.map(\.key)
                  == [AgentTaskQueue.key(auditAction: "review", item: "o2/beta#2"),
                      AgentTaskQueue.key(auditAction: "review", item: "o2/beta#3")])
        store.selectedRepo = "o9/gone"
        check("a chosen root that is gone reads as the primary",
              store.selectedRoot == rootA && store.prs.map(\.number) == [1])
        let twinGH = PRRef.LocalRepo(owner: "acme", repo: "widget",
                                     path: feed.appendingPathComponent("gh-widget").path)
        let twinGL = PRRef.LocalRepo(owner: "acme", repo: "widget",
                                     path: feed.appendingPathComponent("gl-widget").path,
                                     host: "gitlab.example.com")
        let twinMR = "https://gitlab.example.com/acme/widget/-/merge_requests/7"
        store.requestedWork = []
        store.queuedTasks = []
        store.pinRoots([rootA, twinGH, twinGL])
        store.seedPanelLists(key: twinGH.key, prs: [rootPR(twinGH, 7)], issues: [])
        store.seedPanelLists(key: twinGL.key, prs: [OpenPR(number: 7, title: "MR 7", url: twinMR,
                                                           isDraft: true, author: "alice", createdAt: Date(),
                                                           readyForReviewAt: nil, files: [],
                                                           reviewDecision: nil, reviewThreads: [])],
                             issues: [])
        store.selectedRepo = twinGL.key
        check("a GitLab root of a GitHub root's slug is chosen by its key, and shows its own list",
              store.selectedRoot == twinGL && store.prs.map(\.url) == [twinMR])
        let twinSwept = store.requestReviewSweep(ReviewConfig(depth: "deep", target: .mine, me: "alice",
                                                              includeDrafts: true, includeReady: false))
        store.selectedRepo = twinGH.key
        let ghSwept = store.requestReviewSweep(ReviewConfig(depth: "deep", target: .mine, me: "alice",
                                                            includeDrafts: true, includeReady: false))
        let twinKeys = ["review:gitlab.example.com/acme/widget#7", "review:acme/widget#7"]
        check("…each of the two asks for its own PR of one number, run in its own checkout: "
              + "\(twinSwept.queued) \(ghSwept.queued) \(store.requestedWork.map(\.key)) "
              + "\(store.queuedTasks.map { "\($0.id) \($0.job.directory ?? "nil")" })",
              twinSwept.queued == 1 && ghSwept.queued == 1 && store.selectedRoot == twinGH
                  && store.requestedWork.map(\.key) == twinKeys
                  && twinKeys.map { key in store.queuedTasks.first { $0.id == key }?.job.directory }
                      == [twinGL.path, twinGH.path])
        check("…and the picker names the GitLab one by its host",
              store.roots.map { PRRef.rootLabel($0, roots: store.roots) }
                  == ["o1/alpha", "acme/widget", "gitlab.example.com/acme/widget"])
        store.pinRoots([rootA, rootB])
        store.selectedRepo = ""
        store.requestedWork = []
        store.queuedTasks = []

        // 26. What a one-repo build stored by bare number reads as the primary root's.
        check("a legacy bare-number key reads as the primary's item, a keyed one as itself",
              Store.migrated(["5": 1, "o2/beta#6": 2], primary: "o1/alpha")
                  == ["o1/alpha#5": 1, "o2/beta#6": 2])
        check("…and an already-keyed entry wins over its legacy twin",
              Store.migrated(["5": 1, "o1/alpha#5": 9], primary: "o1/alpha") == ["o1/alpha#5": 9])
        let legacyMark = Date(timeIntervalSince1970: 1_700_000_000)
        check("a legacy single community mark is the primary root's",
              Store.communityMarks(legacyMark, primary: "o1/alpha") == ["o1/alpha": legacyMark]
                  && Store.communityMarks(["o2/beta": legacyMark], primary: "o1/alpha")
                      == ["o2/beta": legacyMark])
        let legacyAsks = [
            Store.RequestedWork(number: 4, url: "https://github.com/o1/alpha/pull/4",
                                author: "", config: .review(sweep.forPR(4))),
            Store.RequestedWork(number: 5, url: "https://github.com/O2/Beta/pull/5",
                                author: "", config: .review(sweep.forPR(5))),
            Store.RequestedWork(number: 6, url: "", author: "", config: .review(sweep.forPR(6))),
        ]
        var legacyJSON = (try? JSONSerialization.jsonObject(
            with: JSONEncoder().encode(legacyAsks))) as? [[String: Any]] ?? []
        legacyJSON = legacyJSON.map { var e = $0; e["repo"] = nil; return e }
        let decoded = Store.decodeRequestedWork(
            try? JSONSerialization.data(withJSONObject: legacyJSON), configured: "O1/Alpha")
        check("an ask stored without its repo is its link's, else the configured repo's",
              decoded.map(\.repo) == ["o1/alpha", "o2/beta", "o1/alpha"]
                  && decoded.first?.key == AgentTaskQueue.key(auditAction: "review", item: "o1/alpha#4"))

        // 27. Several root paths none of which resolved: the one root is a guess, so the
        //     monitor poll stands down before any fetch, and the mesh takes nothing.
        //     Then one root's unreadable issues: logged once per streak, failing no poll.
        let rootsLog = feed.appendingPathComponent("gh-roots.log")
        let issuesFail = feed.appendingPathComponent("gh-roots.fail")
        let rootsStub = feed.appendingPathComponent("gh-roots")
        let noIssues = #"{"data":{"repository":{"issues":{"nodes":[]}}}}"#
        try? """
            #!/bin/sh
            echo "$*" >> '\(rootsLog.path)'
            case "$*" in
              *"issues(states: OPEN"*"name=alpha"*)
                if [ -f '\(issuesFail.path)' ]; then echo 'HTTP 404: Could not resolve to a Repository' >&2; exit 1; fi
                echo '\(noIssues)' ;;
              *"issues(states: OPEN"*)
                if [ -f '\(issuesFail.path)-all' ]; then echo 'HTTP 404: Could not resolve to a Repository' >&2; exit 1; fi
                echo '\(noIssues)' ;;
              *"q=repo:"*) echo '\(none)' ;;
              *) echo '{"data":{"viewer":{"login":"alice"}}}' ;;
            esac
            """.write(to: rootsStub, atomically: true, encoding: .utf8)
        chmod(rootsStub.path, 0o755)
        setenv("DIPLOMAT_GH", rootsStub.path, 1)
        store.requestedWork = []
        store.queuedTasks = []
        store.autofixPollError = nil
        let guessedMessage = "no repo root in Settings resolves to a GitHub or GitLab checkout"
        store.pinRoots([configuredRoot], complete: false, primary: false, guessed: true)
        for _ in 0..<50 {
            await store.runAutofixPollOnce()
            if store.autofixPollError == guessedMessage { break }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        let guessedAsked = (try? String(contentsOf: rootsLog, encoding: .utf8)) ?? ""
        check("no root path resolving stands the monitor poll down, fetching nothing",
              store.autofixPollError == guessedMessage && !guessedAsked.contains("q=repo:")
                  && !guessedAsked.contains("issues(states: OPEN") && store.queuedTasks.isEmpty)
        check("…and the mesh takes nothing, the configured repo's root being a guess",
              !store.meshRoutable(checkout: configuredRoot.path))
        store.pinRoots([configuredRoot])
        check("…which it does once that root resolves", store.meshRoutable(checkout: configuredRoot.path))

        func unreadLines() -> [(action: String, detail: String)] {
            let text = (try? String(contentsOf: feed.appendingPathComponent("audit.jsonl"),
                                    encoding: .utf8)) ?? ""
            return text.split(separator: "\n").compactMap { line in
                guard let obj = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any],
                      let action = obj["action"] as? String, action.hasPrefix("root-read-"),
                      let detail = obj["detail"] as? String else { return nil }
                return (action, detail)
            }
        }
        store.pinRoots([rootA, rootB])
        store.communityIssuesEnabled = true
        store.autofixPollError = nil
        FileManager.default.createFile(atPath: issuesFail.path, contents: Data())
        await store.runAutofixPollOnce()
        await store.runAutofixPollOnce()
        let failing = unreadLines()
        check("one root's unreadable issues fail no poll, so the other roots' drain and commit go on",
              store.autofixPollError == nil)
        check("…and are logged once for the streak, naming the root and why",
              failing.map(\.action) == ["root-read-failed"]
                  && failing.first?.detail.hasPrefix("Issues of o1/alpha unreadable, its work waits: ")
                      == true
                  && failing.first?.detail.contains("Could not resolve") == true)
        try? FileManager.default.removeItem(at: issuesFail)
        await store.runAutofixPollOnce()
        await store.runAutofixPollOnce()
        check("…and their recovery once",
              unreadLines().map(\.action) == ["root-read-failed", "root-read-recovered"]
                  && unreadLines().last?.detail == "Issues of o1/alpha readable again")
        FileManager.default.createFile(atPath: issuesFail.path, contents: Data())
        FileManager.default.createFile(atPath: issuesFail.path + "-all", contents: Data())
        await store.runAutofixPollOnce()
        check("…nor do they in every root", store.autofixPollError == nil
                  && unreadLines().map(\.action).filter { $0 == "root-read-failed" }.count == 3)
        try? FileManager.default.removeItem(at: issuesFail)
        try? FileManager.default.removeItem(atPath: issuesFail.path + "-all")
        await store.runAutofixPollOnce()
        store.communityIssuesEnabled = false
        store.autofixPollError = nil
        store.requestedWork = []
        store.queuedTasks = []
        if let realGH { setenv("DIPLOMAT_GH", realGH, 1) } else { unsetenv("DIPLOMAT_GH") }
        store.pinRoots([argent])

        // 28. A root whose PRs or review requests gh cannot read holds only its own work:
        //     its rows, fingerprints and attempt records stand untouched, as for a root
        //     that failed to resolve, and only a read failing in every root fails the
        //     poll. `gh` answers each root's search from a file, or fails it while a
        //     `fail-<what>-<repo>` flag exists. At capacity, so finds queue.
        let heldDir = feed.appendingPathComponent("gh-held")
        try? FileManager.default.createDirectory(at: heldDir, withIntermediateDirectories: true)
        let heldLog = heldDir.appendingPathComponent("log")
        let heldStub = heldDir.appendingPathComponent("gh")
        try? """
            #!/bin/sh
            echo "$*" | grep -o 'q=repo:[^ ]* [^ ]*' >> '\(heldLog.path)'
            case "$*" in *"issues(states: OPEN"*)
              echo "$*" | grep -o 'name=[a-z]*' | sed 's/^/issues-/' >> '\(heldLog.path)'
              case "$*" in *"name=Beta"*)
                if [ -f '\(heldDir.path)'/fail-issues-Beta ]; then
                  echo 'HTTP 404: Could not resolve to a Repository' >&2; exit 1
                fi ;;
              esac
              echo '\(noIssues)'; exit 0 ;;
            esac
            for slug in o1/alpha o2/beta O2/Beta; do
              for what in author review-requested; do
                case "$*" in *"q=repo:$slug $what:"*)
                  name=${slug#*/}
                  if [ -f '\(heldDir.path)'/fail-$what-$name ]; then
                    echo 'HTTP 422: The listed users and repositories cannot be searched' >&2; exit 1
                  fi
                  if [ -f '\(heldDir.path)'/$what-$name.json ]; then cat '\(heldDir.path)'/$what-$name.json
                  else echo '\(none)'; fi
                  exit 0 ;;
                esac
              done
            done
            case "$*" in *"q=repo:"*) echo '\(none)' ;; *) echo '{"data":{"viewer":{"login":"alice"}}}' ;; esac
            """.write(to: heldStub, atomically: true, encoding: .utf8)
        chmod(heldStub.path, 0o755)
        func heldAnswer(_ what: String, _ repo: String, _ json: String?) {
            let file = heldDir.appendingPathComponent("\(what)-\(repo).json")
            if let json { try? json.write(to: file, atomically: true, encoding: .utf8) }
            else { try? FileManager.default.removeItem(at: file) }
        }
        func heldFail(_ what: String, _ repo: String, _ on: Bool) {
            let flag = heldDir.appendingPathComponent("fail-\(what)-\(repo)").path
            if on { FileManager.default.createFile(atPath: flag, contents: Data()) }
            else { try? FileManager.default.removeItem(atPath: flag) }
        }
        func conflictingPR(_ slug: String, _ number: Int, _ sha: String) -> String {
            #"{"data":{"search":{"nodes":[{"number":\#(number),"title":"t","url":"https://github.com/\#(slug)/pull/\#(number)","isDraft":false,"mergeable":"CONFLICTING","reviewDecision":"","headRefOid":"\#(sha)","reviewThreads":{"nodes":[]}}]}}}"#
        }
        func requestedPR(_ slug: String, _ number: Int) -> String {
            #"{"data":{"search":{"nodes":[{"number":\#(number),"title":"t","url":"https://github.com/\#(slug)/pull/\#(number)","author":{"login":"qt-outsider"},"authorAssociation":"NONE","headRefOid":"r\#(number)"}]}}}"#
        }
        func heldRecords<V: Decodable>(_ key: String, as: V.Type) -> [String: V] {
            store.headlessJSON?[key].flatMap { try? JSONDecoder().decode([String: V].self, from: $0) } ?? [:]
        }
        let heldAsk = (prAutofix: store.prAutofixEnabled, reviewReqs: store.reviewRequestsEnabled)
        store.prAutofixEnabled = true
        store.reviewRequestsEnabled = true
        store.communityIssuesEnabled = false
        store.headlessJSON = [:]
        store.pinRoots([rootA, rootB])
        store.requestedWork = []
        store.queuedTasks = []
        store.autofixPollError = nil
        emptyBook()
        store.autoTaskLimit = 1
        bookAgent(90, ahead: 86_400)
        _ = await store.settleAgents()
        setenv("DIPLOMAT_GH", heldStub.path, 1)
        heldAnswer("author", "alpha", conflictingPR("o1/alpha", 90007, "a7"))
        heldAnswer("author", "beta", conflictingPR("o2/beta", 90007, "b7"))
        await store.runAutofixPollOnce()
        let heldRow = AgentTaskQueue.key(auditAction: "conflicts", item: "o2/beta#90007")
        let heldPrint = heldRecords("autofixFingerprints", as: PRFingerprint.self)["o2/beta#90007"]
        check("both roots' conflicts queue while both read",
              Set(store.queuedTasks.map(\.id)).isSuperset(of: [
                  AgentTaskQueue.key(auditAction: "conflicts", item: "o1/alpha#90007"), heldRow])
                  && heldPrint != nil)
        let record = ReviewAttempt(requestedAt: Store.AttemptStamp.conflicting,
                                   lastDispatchedAt: Date(), attempts: 1)
        store.headlessJSON?["myConflictAttempts"] = try? JSONEncoder().encode(["o2/beta#90007": record])
        store.headlessJSON?["myReviewAttempts"] = try? JSONEncoder().encode(["o2/beta#90007": record])
        // A concluded run whose request is still owed: dropping its record reviews again.
        let concludedLongAgo = ReviewAttempt(
            requestedAt: "2026-01-02T00:00:00Z",
            lastDispatchedAt: Date().addingTimeInterval(-ReviewReconcile.retryMaxBackoff - 600),
            attempts: 1, concludedAt: Date().addingTimeInterval(-ReviewReconcile.retryMaxBackoff - 60))
        store.headlessJSON?["reviewReqAttempts"] = try? JSONEncoder().encode(["o2/beta#90006": concludedLongAgo])
        heldFail("author", "beta", true)
        heldAnswer("author", "alpha", conflictingPR("o1/alpha", 90008, "a8"))
        let heldLinesBefore = unreadLines().count
        try? FileManager.default.removeItem(at: heldLog)
        store.communityIssuesEnabled = true
        await store.runAutofixPollOnce()
        await store.runAutofixPollOnce()
        store.communityIssuesEnabled = false
        let heldAsked = (try? String(contentsOf: heldLog, encoding: .utf8)) ?? ""
        check("one root's unreadable PRs fail no poll", store.autofixPollError == nil)
        check("…its queued row stands, and the other root's new conflict queues",
              store.queuedTasks.map(\.id).contains(heldRow)
                  && store.queuedTasks.map(\.id).contains(
                      AgentTaskQueue.key(auditAction: "conflicts", item: "o1/alpha#90008")))
        check("…its fingerprint and both attempt records are kept",
              heldRecords("autofixFingerprints", as: PRFingerprint.self)["o2/beta#90007"] == heldPrint
                  && heldRecords("myConflictAttempts", as: ReviewAttempt.self)["o2/beta#90007"] == record
                  && heldRecords("myReviewAttempts", as: ReviewAttempt.self)["o2/beta#90007"] == record)
        check("…its review requests and issues are not asked, the other root's are",
              heldAsked.contains("q=repo:o1/alpha review-requested:")
                  && !heldAsked.contains("q=repo:o2/beta review-requested:")
                  && heldAsked.contains("issues-name=alpha") && !heldAsked.contains("issues-name=beta"))
        check("…and its review-request records are kept",
              heldRecords("reviewReqAttempts", as: ReviewAttempt.self)["o2/beta#90006"] == concludedLongAgo)
        let heldFailed = unreadLines().dropFirst(heldLinesBefore)
        check("…and it is logged once for the streak, naming the root and why",
              heldFailed.map(\.action) == ["root-read-failed"]
                  && heldFailed.first?.detail.hasPrefix("PRs of o2/beta unreadable, its work waits: ") == true
                  && heldFailed.first?.detail.contains("cannot be searched") == true)
        // A free bay and a disk below its floor: the drain would take a row it tried
        // off the queue and re-stage it into a cycle that does not offer it, so a row
        // that stands was never tried. Nothing spawns past the disk floor.
        heldAnswer("author", "alpha", nil)
        setenv(DiskFloor.envOverride, "99999999999999999999", 1)
        if Store.agentDisk()?.low == true {
            store.autoTaskLimit = 2
            await store.runAutofixPollOnce()
            check("…and the drain does not start it while a bay is free",
                  store.queuedTasks.map(\.id).contains(heldRow))
            store.autoTaskLimit = 1
        } else {
            check("the disk floor holds every dispatch for the drain check", false)
        }
        setenv(DiskFloor.envOverride, "0", 1)
        heldFail("author", "alpha", true)
        try? FileManager.default.removeItem(at: heldLog)
        await store.runAutofixPollOnce()
        let outageAsked = (try? String(contentsOf: heldLog, encoding: .utf8)) ?? ""
        check("my PRs unreadable in every root fail the poll",
              store.autofixPollError?.contains("cannot be searched") == true)
        check("…and every root's review requests are still asked",
              outageAsked.contains("q=repo:o1/alpha review-requested:")
                  && outageAsked.contains("q=repo:o2/beta review-requested:"))
        heldFail("author", "alpha", false)
        heldFail("author", "beta", false)
        heldAnswer("author", "beta", nil)
        store.autofixPollError = nil
        store.queuedTasks = []

        // The review-request twin, its reads failing while the PRs read.
        heldAnswer("review-requested", "beta", requestedPR("o2/beta", 90010))
        await store.runAutofixPollOnce()
        let heldReq = AgentTaskQueue.key(auditAction: "review-req", item: "o2/beta#90010")
        check("a root's review request queues while it reads",
              store.queuedTasks.map(\.id).contains(heldReq))
        store.headlessJSON?["reviewReqAttempts"] = try? JSONEncoder().encode(["o2/beta#90006": concludedLongAgo])
        heldAnswer("review-requested", "alpha", requestedPR("o1/alpha", 90009))
        heldFail("review-requested", "beta", true)
        await store.runAutofixPollOnce()
        check("one root's unreadable review requests fail no poll, the other root's queue",
              store.autofixPollError == nil && store.queuedTasks.map(\.id).contains(
                  AgentTaskQueue.key(auditAction: "review-req", item: "o1/alpha#90009")))
        check("…and its concluded record is kept",
              heldRecords("reviewReqAttempts", as: ReviewAttempt.self)["o2/beta#90006"] == concludedLongAgo)
        check("…and its queued review request stands, that root being held",
              store.queuedTasks.map(\.id).contains(heldReq))
        check("…logged as that root's review requests",
              unreadLines().last?.action == "root-read-failed"
                  && unreadLines().last?.detail.hasPrefix("Review requests of o2/beta unreadable, its work waits: ")
                      == true)
        heldFail("review-requested", "alpha", true)
        await store.runAutofixPollOnce()
        check("review requests unreadable in every root fail the poll",
              store.autofixPollError?.contains("cannot be searched") == true)

        // A root's slug is cased as its remote spells it; item keys are lowercase.
        heldFail("review-requested", "alpha", false)
        heldFail("review-requested", "beta", false)
        heldAnswer("review-requested", "alpha", nil)
        heldAnswer("review-requested", "beta", nil)
        let mixed = PRRef.LocalRepo(owner: "O2", repo: "Beta", path: rootB.path)
        store.pinRoots([rootA, mixed])
        store.autofixPollError = nil
        store.queuedTasks = []
        heldAnswer("author", "Beta", conflictingPR("O2/Beta", 90011, "m1"))
        await store.runAutofixPollOnce()
        let mixedRow = AgentTaskQueue.key(auditAction: "conflicts", item: "o2/beta#90011")
        check("a mixed-case root's conflict queues while it reads",
              store.queuedTasks.map(\.id).contains(mixedRow))
        store.headlessJSON?["reviewReqAttempts"] = try? JSONEncoder().encode(["o2/beta#90006": concludedLongAgo])
        heldFail("author", "Beta", true)
        await store.runAutofixPollOnce()
        check("a held root is known whatever its slug's case: its row stands, its records are kept",
              store.autofixPollError == nil && store.queuedTasks.map(\.id).contains(mixedRow)
                  && heldRecords("reviewReqAttempts", as: ReviewAttempt.self)["o2/beta#90006"]
                      == concludedLongAgo)
        var mixedAsk = ReviewConfig(depth: "deep", target: .mine, me: "alice",
                                    includeDrafts: true, includeReady: false)
        mixedAsk.home = mixed
        store.requestOne(Store.RequestedWork(number: 90012, url: "https://github.com/O2/Beta/pull/90012",
                                             author: "", config: .review(mixedAsk.forPR(90012))))
        let mixedAskKey = AgentTaskQueue.key(auditAction: "review", item: "o2/beta#90012")
        let mixedAskQueued = store.queuedTasks.map(\.id).contains(mixedAskKey)
        await store.drainQueuedTasks(snaps: [], closed: ["o2/beta#90012"], read: [rootA, mixed])
        check("…and a read one is priced whatever its case",
              mixedAskQueued && !store.queuedTasks.map(\.id).contains(mixedAskKey))
        heldFail("author", "Beta", false)
        heldAnswer("author", "Beta", nil)

        // 29. The drain starts nothing in a root just taken out of Settings: it runs at
        //     the top of the poll, before the offer and the commit drop its rows. A free
        //     bay and a disk below its floor, so a task the drain tried would write a
        //     low-disk line instead of spawning.
        func feedDetails(_ action: String) -> [String] {
            let text = (try? String(contentsOf: feed.appendingPathComponent("audit.jsonl"),
                                    encoding: .utf8)) ?? ""
            return text.split(separator: "\n").compactMap { line in
                guard let obj = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any],
                      obj["action"] as? String == action else { return nil }
                return obj["detail"] as? String
            }
        }
        store.pinRoots([rootA, rootB])
        store.requestedWork = []
        store.queuedTasks = []
        store.autofixPollError = nil
        heldAnswer("review-requested", "beta", requestedPR("o2/beta", 7))
        let autoRun = store.queueAutoRun
        store.queueAutoRun = false
        var goneIssue = IssueConfig(target: .contributors, me: "alice")
        goneIssue.home = rootB
        store.requestOne(Store.RequestedWork(number: 11, url: "https://github.com/o2/beta/issues/11",
                                             author: "outsider11", config: .issues(goneIssue.forIssue(11)),
                                             repo: "o2/beta"))
        await store.runAutofixPollOnce()
        check("an ask and a review request in a root queue while the queue is off",
              Set(store.queuedTasks.map(\.id)) == [
                  AgentTaskQueue.key(auditAction: "issues", item: "o2/beta#11"),
                  AgentTaskQueue.key(auditAction: "review-req", item: "o2/beta#7")])
        store.pinRoots([rootA])
        store.queueAutoRun = true
        setenv(DiskFloor.envOverride, "99999999999999999999", 1)
        if Store.agentDisk()?.low == true {
            let lowDiskBefore = feedDetails("low-disk").count
            store.autoTaskLimit = 2
            await store.runAutofixPollOnce()
            store.autoTaskLimit = 1
            check("the drain starts nothing in a root just taken out of Settings",
                  feedDetails("low-disk").count == lowDiskBefore)
            check("…its rows and its ask are dropped, and said",
                  store.queuedTasks.isEmpty && store.requestedWork.isEmpty
                      && feedDetails("queue-drop").last?
                          .hasSuffix("o2/beta is no longer a repo root, not run") == true)
        } else {
            check("the disk floor holds every dispatch for the removed-root check", false)
        }
        setenv(DiskFloor.envOverride, "0", 1)
        store.queueAutoRun = autoRun
        heldAnswer("review-requested", "beta", nil)

        // 30. A queued review request waits while its root's review requests stay
        //     unreadable: the hold its failed read gives it lasts a cycle, the marker
        //     the whole streak. A mixed-case root, item keys being lowercase.
        store.pinRoots([rootA, mixed])
        store.requestedWork = []
        store.queuedTasks = []
        store.autofixPollError = nil
        store.queueAutoRun = false
        heldAnswer("review-requested", "Beta", requestedPR("O2/Beta", 7))
        await store.runAutofixPollOnce()
        let waitingReq = AgentTaskQueue.key(auditAction: "review-req", item: "o2/beta#7")
        check("a mixed-case root's review request queues while the queue is off",
              store.queuedTasks.map(\.id).contains(waitingReq))
        heldFail("review-requested", "Beta", true)
        await store.runAutofixPollOnce()
        check("…and stands while that root's review requests are unreadable",
              store.queuedTasks.map(\.id).contains(waitingReq))
        store.queueAutoRun = true
        setenv(DiskFloor.envOverride, "99999999999999999999", 1)
        if Store.agentDisk()?.low == true {
            let lowDiskBefore = feedDetails("low-disk").count
            store.autoTaskLimit = 2
            await store.runAutofixPollOnce()
            check("…and a later cycle's drain does not start it while the read still fails",
                  store.queuedTasks.map(\.id).contains(waitingReq)
                      && feedDetails("low-disk").count == lowDiskBefore)
            heldFail("review-requested", "Beta", false)
            await store.runAutofixPollOnce()
            check("…until the read succeeds again, when it is dispatched",
                  feedDetails("low-disk").count == lowDiskBefore + 1
                      && store.queuedTasks.map(\.id).contains(waitingReq))
            store.autoTaskLimit = 1
        } else {
            check("the disk floor holds every dispatch for the waiting-request check", false)
        }
        setenv(DiskFloor.envOverride, "0", 1)
        store.queueAutoRun = autoRun
        heldFail("review-requested", "Beta", false)
        heldAnswer("review-requested", "Beta", nil)
        store.queuedTasks = []

        // 31. A root taken out of Settings loses its unread markers, so added back it
        //     logs its next failed read.
        func issuesUnread() -> Int {
            feedDetails("root-read-failed").filter { $0.hasPrefix("Issues of O2/Beta unreadable") }.count
        }
        let unreadBefore = issuesUnread()
        store.communityIssuesEnabled = true
        heldFail("issues", "Beta", true)
        await store.runAutofixPollOnce()
        await store.runAutofixPollOnce()
        check("a mixed-case root's failed read is logged once while it stays a root",
              issuesUnread() == unreadBefore + 1)
        store.pinRoots([rootA])
        await store.runAutofixPollOnce()
        store.pinRoots([rootA, mixed])
        await store.runAutofixPollOnce()
        check("a root's failed read is logged again once it is taken out and added back",
              issuesUnread() == unreadBefore + 2)
        heldFail("issues", "Beta", false)
        await store.runAutofixPollOnce()
        store.communityIssuesEnabled = false

        // 32. An investigation, about no repo's item, is never outside the roots read.
        store.requestedWork = []
        store.queuedTasks = []
        let invNumber = 9_100_001
        let investigation = InvestigateConfig(
            number: invNumber, messageID: "qt-inv", sender: "qt", channel: "qt", permalink: "",
            text: "t", summary: "s", reportPath: feed.appendingPathComponent("inv.md").path,
            owner: "O2", repo: "Beta")
        store.requestOne(Store.RequestedWork(number: invNumber, url: "", author: "",
                                             config: .investigate(investigation), repo: "o2/beta"))
        let invKey = AgentTaskQueue.key(auditAction: InvestigateConfig.kind, item: String(invNumber))
        let invQueued = store.queuedTasks.map(\.id) == [invKey]
        store.queueAutoRun = true
        setenv(DiskFloor.envOverride, "99999999999999999999", 1)
        if Store.agentDisk()?.low == true {
            store.autoTaskLimit = 2
            await store.drainQueuedTasks(snaps: [], closed: [], read: [rootA, mixed])
            store.autoTaskLimit = 1
            check("the drain tries an investigation whatever roots it read",
                  invQueued && store.queuedTasks.isEmpty)
        } else {
            check("the disk floor holds every dispatch for the investigation check", false)
        }
        setenv(DiskFloor.envOverride, "0", 1)

        // 33. An investigation, or an ask stored without its home, would still run in the
        //     primary root: one whose repo left the roots is neither started nor kept, and
        //     its drop is said.
        store.pinRoots([rootA, mixed])
        store.requestedWork = []
        store.queuedTasks = []
        let goneNumber = 9_100_002
        let goneInvestigation = InvestigateConfig(
            number: goneNumber, messageID: "qt-inv2", sender: "qt", channel: "qt", permalink: "",
            text: "t", summary: "s", reportPath: feed.appendingPathComponent("inv2.md").path,
            owner: "o2", repo: "beta")
        store.requestOne(Store.RequestedWork(number: goneNumber, url: "", author: "",
                                             config: .investigate(goneInvestigation), repo: "o2/beta"))
        let goneInvKey = AgentTaskQueue.key(auditAction: InvestigateConfig.kind, item: String(goneNumber))
        store.pinRoots([rootA])
        setenv(DiskFloor.envOverride, "99999999999999999999", 1)
        if Store.agentDisk()?.low == true {
            store.autoTaskLimit = 2
            await store.drainQueuedTasks(snaps: [], closed: [], read: [rootA])
            store.autoTaskLimit = 1
            check("the drain starts no investigation asked in a root just taken out",
                  store.queuedTasks.map(\.id) == [goneInvKey])
        } else {
            check("the disk floor holds every dispatch for the gone-investigation check", false)
        }
        setenv(DiskFloor.envOverride, "0", 1)
        let homeless = ReviewConfig(depth: "deep", target: .mine, me: "alice",
                                    includeDrafts: true, includeReady: false)
        store.requestedWork.append(Store.RequestedWork(
            number: 90013, url: "https://github.com/o2/beta/pull/90013", author: "",
            config: .review(homeless.forPR(90013)), repo: "o2/beta"))
        let dropsBefore = feedDetails("queue-drop").count
        store.offerRequestedWork()
        store.commitQueue()
        let goneDrops = feedDetails("queue-drop").dropFirst(dropsBefore)
        check("…and the offer drops it and an ask stored without its home, saying so",
              store.requestedWork.isEmpty && store.queuedTasks.isEmpty && goneDrops.count == 2
                  && goneDrops.allSatisfy { $0.hasSuffix(" - o2/beta is no longer a repo root, not run") })
        let legacyNumber = 9_100_003
        let legacyInvestigation = InvestigateConfig(
            number: legacyNumber, messageID: "qt-inv3", sender: "qt", channel: "qt", permalink: "",
            text: "t", summary: "s", reportPath: feed.appendingPathComponent("inv3.md").path,
            owner: "o2", repo: "beta")
        store.requestOne(Store.RequestedWork(number: legacyNumber, url: "", author: "",
                                             config: .investigate(legacyInvestigation)))
        store.offerRequestedWork()
        store.commitQueue()
        let legacyInvKey = AgentTaskQueue.key(auditAction: InvestigateConfig.kind, item: String(legacyNumber))
        check("…but one stored naming no repo runs in the primary root, kept",
              store.requestedWork.map(\.key) == [legacyInvKey]
                  && store.queuedTasks.map(\.id) == [legacyInvKey]
                  && feedDetails("queue-drop").count == dropsBefore + 2)

        // 34. Past a free bay a dispatch reaches the terminal, which no self-test may
        //     open (`AgentSpawner.refuseHeadless`): it fails, counted, and no agent runs.
        emptyBook()
        store.requestedWork = []
        store.queuedTasks = []
        store.queueAutoRun = true
        store.autoTaskLimit = 1
        let refusedBefore = AgentSpawner.refusedTerminals
        let freeBay = await store.dispatchAgent(job(91, author: "qt-outsider", counter: nil),
                                                source: .auto)
        // A terminal answers its AppleScript before the agent's shell starts.
        try? await Task.sleep(nanoseconds: 3_000_000_000)
        let probe = Process()
        probe.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
        probe.arguments = ["-f", agents.path]
        let found = Pipe()
        probe.standardOutput = found
        try? probe.run()
        probe.waitUntilExit()
        let spawned = String(decoding: found.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            .split(separator: "\n").count
        check("a dispatch past a free bay fails, its terminal refused once, and starts no agent",
              freeBay == .failed(AgentSpawner.SpawnError.headless.localizedDescription)
                  && AgentSpawner.refusedTerminals == refusedBefore + 1 && spawned == 0
                  && AgentRegistry.load().isEmpty)
        store.queueAutoRun = autoRun
        store.prAutofixEnabled = heldAsk.prAutofix
        store.reviewRequestsEnabled = heldAsk.reviewReqs
        store.headlessJSON = nil
        store.autofixPollError = nil
        store.requestedWork = []
        store.queuedTasks = []
        emptyBook()
        if let realGH { setenv("DIPLOMAT_GH", realGH, 1) } else { unsetenv("DIPLOMAT_GH") }
        store.pinRoots([argent])

        // 13. The redirect above is the only thing between a run of this test and the
        //    operator's real activity log, so prove it caught the writes.
        check("the at-capacity lines it provoked went to the scratch feed",
              FileManager.default.fileExists(
                  atPath: feed.appendingPathComponent("audit.jsonl").path))

        print(pass ? "\nQUEUE TEST OK" : "\nQUEUE TEST FAILED")
        return pass
    }
}

/// A one-shot signal on the main actor: `wait` returns once `open` has been called, at
/// once if it already was.
@MainActor
private final class Latch {
    private(set) var isOpen = false
    private var waiters: [CheckedContinuation<Void, Never>] = []

    func open() {
        isOpen = true
        let woken = waiters
        waiters = []
        for w in woken { w.resume() }
    }

    func wait() async {
        if isOpen { return }
        await withCheckedContinuation { waiters.append($0) }
    }

    /// Whether it opened inside `seconds`, polled while the main actor keeps turning.
    func opened(within seconds: Double) async -> Bool {
        let deadline = Date().addingTimeInterval(seconds)
        while !isOpen, Date() < deadline {
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        return isOpen
    }
}
