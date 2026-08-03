import Foundation
import DiplomatCore

/// Headless self-test for the queue behind the automatic-task cap, driven by
/// `DIPLOMAT_QUEUE_TEST=1`.
///
/// The *ordering* rules are pure and pinned in `DiplomatCoreSmoke`
/// (`AgentTaskQueue`); what this covers is the wiring around them, which is where
/// a queue silently stops being one: that a refusal is captured rather than
/// dropped, that one unit offered twice in a cycle is one task, that the operator's
/// arrangement survives the poll that rebuilds the list, and that the list empties
/// when nothing is watching for the work any more.
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

        // 6. A monitor switched off takes its queued work with it. That toggle is how
        //    the operator pauses this work; unpruned, the drain still spawns an agent
        //    per queued row of that monitor, minutes after they switched it off.
        _ = await offer(job(4))
        _ = await offer(job(6, action: "conflicts", label: "Resolve · #6",
                            counter: .conflicts))
        store.commitQueue()
        check("both monitors on ⇒ both kinds queue",
              store.queuedTasks.map(\.id) == ["review-req:4", "conflicts:6"])
        store.prAutofixEnabled = false
        store.pruneQueueToEnabledMonitors()
        check("switching off PR auto-fix drops its queued work, not the other monitor's",
              store.queuedTasks.map(\.id) == ["review-req:4"])

        // 7. Nothing polls ⇒ nothing is pending. Rows offering to run work that no
        //    monitor is watching for would outlive the feature that produced them.
        store.reviewRequestsEnabled = false
        await store.runAutofixPollOnce()
        check("turning both monitors off empties the queue", store.queuedTasks.isEmpty)

        // 8. The redirect above is the only thing between a run of this test and the
        //    operator's real activity log, so prove it caught the writes.
        check("the at-capacity lines it provoked went to the scratch feed",
              FileManager.default.fileExists(
                  atPath: feed.appendingPathComponent("audit.jsonl").path))

        print(pass ? "\nQUEUE TEST OK" : "\nQUEUE TEST FAILED")
        return pass
    }
}
