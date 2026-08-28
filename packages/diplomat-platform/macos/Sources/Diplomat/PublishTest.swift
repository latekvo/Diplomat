import Foundation
import DiplomatCore

/// Headless self-test for what a tick hands the panel — `DIPLOMAT_PUBLISH_TEST=1`.
///
/// `Store.publish` is where a resolved tick becomes the Agent-tasks list, and the one
/// decision it makes on the way is that a run which has ENDED does not get a row: its
/// record is retired in the same pass, so a row for it would be on screen for a single
/// redraw and gone the next, and which redraw caught it would depend on when the poll
/// landed.
///
/// Nothing else covers it. The headless render reaches the list through
/// `Store.pinAgentRows`, which assigns `agentRows` directly — so the one artefact CI
/// inspects is produced by the path that BYPASSES the filter, and deleting the filter
/// left every macOS gate green. The Linux twin of the same invariant is pinned
/// (`test_autofix.py::test_a_peer_run_is_never_retired_by_this_machines_process_table`
/// exercises `Store.running_tasks`), and the two front-ends are meant to draw the same
/// list, so the coverage was one-sided across a cross-platform promise.
///
///     DIPLOMAT_PUBLISH_TEST=1 swift run Diplomat
///
/// Pure: the evidence is a literal, so no probe runs, no `ps` is read and the answer
/// does not depend on what else is running on the machine. Exit code is pass/fail.
enum PublishTest {
    /// Real CLI buffers — the interrupt hint on the status bar means mid-turn, its
    /// absence means back at the prompt.
    private static let working =
        "● Reading files…\n⏵⏵ bypass permissions on · esc to interrupt · ← for agents"
    private static let atPrompt =
        "● Posted the review.\n❯\n⏵⏵ bypass permissions on (shift+tab to cycle)"

    @discardableResult
    @MainActor
    static func run() async -> Bool {
        var pass = true
        func check(_ name: String, _ ok: Bool) {
            print("\(ok ? "PASS" : "FAIL") — \(name)")
            if !ok { pass = false }
        }

        // A headless Store persists nothing, but it still READS the operator's state —
        // and `Store()` is built before the first assertion, so the book goes to a
        // scratch directory like every other self-test's.
        let agents = FileManager.default.temporaryDirectory
            .appendingPathComponent("diplomat-publishtest-\(UUID().uuidString)")
        setenv("DIPLOMAT_AGENTS_DIR", agents.path, 1)
        defer { try? FileManager.default.removeItem(at: agents) }

        let now = Date().timeIntervalSince1970
        func rec(_ id: String, pr: Int, pid: Int?, tty: String) -> AgentState.RunRecord {
            AgentState.RunRecord(runID: id, dispatchedAt: now - 600, prNumber: pr,
                                 prURL: "https://github.com/software-mansion/argent/pull/\(pr)",
                                 kind: "review", label: "Auto · Review · #\(pr)",
                                 source: AgentDispatchGate.Source.auto.rawValue,
                                 pid: pid, tty: tty)
        }
        // Four runs across the two halves of the list: two whose agents are alive and
        // two that are over — one by a landed PR, one by a pid that has left the table.
        let live = rec("live", pr: 11, pid: 4242, tty: "ttys011")
        let idle = rec("idle", pr: 12, pid: 4243, tty: "ttys012")
        let gone = rec("gone", pr: 13, pid: 4244, tty: "ttys013")
        let landed = rec("landed", pr: 14, pid: 4245, tty: "ttys014")
        let evidence = AgentState.Evidence(
            // `gone`'s pid is missing from a table that WAS read, which is what ends it.
            processes: .present([
                4242: AgentState.ProcInfo(tty: "ttys011", elapsed: 600, isAgent: true),
                4243: AgentState.ProcInfo(tty: "ttys012", elapsed: 600, isAgent: true),
                4245: AgentState.ProcInfo(tty: "ttys014", elapsed: 600, isAgent: true),
            ]),
            sentinels: .present([]),
            tails: .present(["ttys011": working, "ttys012": atPrompt,
                             "ttys014": working]),
            claims: .present([]),
            mergedPRs: .present([14]),
            liveAgents: .present([:]),
            sessions: .present([:]),
            activity: .present([:]))
        let t = AgentState.tick(records: [live, idle, gone, landed],
                                evidence: evidence, now: now, limit: 4)

        // The fixture has to actually contain both halves, or everything below passes
        // on a tick that never had an ended run in it.
        check("the tick resolves all four runs",
              t.rows.count == 4)
        check("…two of them over — one merged, one whose pid has left the table",
              t.states["landed"]?.state == .merged && t.states["gone"]?.state == .finished)
        check("…and two not — one working, one back at its prompt",
              t.states["live"]?.state == .running
                && t.states["idle"]?.state == .awaitingInput)

        let store = Store()
        store.publishForSelfTest(Store.AgentPass(tick: t, windows: [:]))

        // The list starts at `.awaitingInput`, so the idle one leads and the working
        // one follows — and neither of the two that ended is on it at all.
        check("a run that has ended is not handed to the panel",
              store.agentRows.map(\.record.runID) == ["idle", "live"])
        check("…and each row carries the verdict that put it there",
              store.agentRows.map(\.state) == [.awaitingInput, .running]
                && store.agentRows.allSatisfy { !$0.reason.isEmpty })
        // The other half of the same call. A session at its prompt keeps its row and
        // gives its bay back, so the list is longer than the load exactly as often as a
        // finished window is left open — one row of these two, not two.
        check("…while the bays measured are the tick's, not the rows'",
              store.autoTasksMeasured == t.capLoad.count && t.capLoad == Set(["live"]))

        print(pass ? "PUBLISH TEST OK" : "PUBLISH TEST FAILED")
        return pass
    }
}
