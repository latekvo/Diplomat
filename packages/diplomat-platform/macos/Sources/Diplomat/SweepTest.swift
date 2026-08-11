import Foundation
import DiplomatCore

/// Headless self-test for what the sweep decides about a live session — is it working,
/// or back at its prompt? — driven by `DIPLOMAT_SWEEP_TEST=1`.
///
/// The reading of an answer is pure and pinned in `DiplomatCoreSmoke` (`OpenCodeAPI`);
/// what this covers is the wiring around it, which is where the answer stops being used:
/// that a run's own session outranks its window, that a run without one still falls back
/// to the window, and that the match a run pays for once is written onto the row so it is
/// never paid for again.
///
/// Both probes are injected, so it opens no window, dials no port and needs no agent — a
/// CI runner can host it:
///
///     DIPLOMAT_SWEEP_TEST=1 swift run Diplomat
enum SweepTest {
    /// The live status bar of a real Claude Code pane mid-turn, and of one back at its
    /// prompt. Verbatim, because the whole point of the marker is that it is someone
    /// else's string — a buffer we composed would only prove we agree with ourselves.
    static let working = "● Reading files…\n⏵⏵ bypass permissions on · esc to interrupt · ←"
    static let atPrompt = "● Posted the review.\n❯\n⏵⏵ bypass permissions on (shift+tab to cycle)"

    /// Returns overall pass/fail so the launcher can exit non-zero — a FAIL that still
    /// exits 0 can't gate anything.
    @discardableResult
    static func run() -> Bool {
        var pass = true
        func check(_ name: String, _ ok: Bool) {
            print("\(ok ? "PASS" : "FAIL") — \(name)")
            if !ok { pass = false }
        }

        // A row old enough to be past the spawn grace, on a tty the terminal still lists
        // — so the window-gone branch never fires and `awaitingInput` is what varies.
        func row(port: Int = 0, agentSessionID: String = "") -> TrackedProcess {
            TrackedProcess(kind: "review", label: "Review · #7", terminal: "iterm",
                           windowID: "1", sessionID: "", tty: "/dev/ttys001",
                           donePath: "", prURL: nil,
                           createdAt: Date(timeIntervalSinceNow: -600),
                           port: port, agentSessionID: agentSessionID)
        }
        func sweep(_ p: TrackedProcess, tail: String,
                   session: OpenCodeProbe.AgentSession?) -> TrackedProcess {
            ProcessMonitor.sweep([p], openWindows: { _ in ["1"] },
                                 sessionTails: ["/dev/ttys001": tail],
                                 ttyElapsed: ["ttys001": 900],
                                 agentSessions: { procs in
                                     guard let session, let first = procs.first else { return [:] }
                                     return [first.id: session]
                                 }).refreshed[0]
        }
        func answer(_ busy: Bool, _ id: String = "ses_ours") -> OpenCodeProbe.AgentSession {
            OpenCodeProbe.AgentSession(sessionID: id,
                                       state: AgentState.SessionState(busy: busy))
        }

        // 1. The two disagreeing cases, which are the whole reason to ask the agent: they
        //    are what a redrawn or reworded status bar looks like, and each is a mistake
        //    the applet used to make with no way to tell it was making one.
        check("a session mid-turn holds its bay though its window looks idle",
              sweep(row(port: 47_910), tail: atPrompt, session: answer(true))
                  .awaitingInput == false)
        check("a session that finished its turn gives its bay back though the hint is stale",
              sweep(row(port: 47_910), tail: working, session: answer(false))
                  .awaitingInput == true)

        // 2. The match costs a fetch of a session's opening message; the row is where the
        //    answer is kept so the next sweep asks a session it already knows.
        check("the session a run matched is written onto its row",
              sweep(row(port: 47_910), tail: working, session: answer(false))
                  .agentSessionID == "ses_ours")

        // 3. Every Claude Code run is this one, as is an OpenCode run no port could be
        //    reserved for. Reaching for an answer that is not there must cost the older
        //    evidence, never the verdict.
        check("a run with no session of its own is still read off its window",
              sweep(row(), tail: working, session: nil).awaitingInput == false)
        check("…and reads as idle when that window is back at its prompt",
              sweep(row(), tail: atPrompt, session: nil).awaitingInput == true)

        // 4. A window whose buffer could not be captured at all: neither probe answered,
        //    so nothing may be asserted. Left as it came in — running.
        let unread = ProcessMonitor.sweep([row()], openWindows: { _ in ["1"] },
                                          ttyElapsed: ["ttys001": 900],
                                          agentSessions: { _ in [:] }).refreshed[0]
        check("a run neither probe could reach keeps reading as running",
              unread.awaitingInput == false)

        print(pass ? "\nSWEEP TEST OK" : "\nSWEEP TEST FAILED")
        return pass
    }
}
