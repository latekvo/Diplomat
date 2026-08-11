import Foundation
import SQLite3
import DiplomatCore

/// Headless self-test for what the sweep decides about a live session — is it working,
/// or back at its prompt? — driven by `DIPLOMAT_SWEEP_TEST=1`.
///
/// The reading of an answer is pure and pinned in `DiplomatCoreSmoke` (`OpenCodeAPI`,
/// `HermesStore`); what this covers is the wiring around it, which is where the answer
/// stops being used: that a run's own session outranks its window, that a run without one
/// still falls back to the window, that the match a run pays for once is written onto the
/// row so it is never paid for again, and that each runner is asked of its own store.
///
/// It opens no window, dials no port and needs no agent: the OpenCode probe is injected,
/// and the Hermes one is pointed at a store this file writes. A CI runner can host it:
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
        func row(runner: AgentRunner = .claude, port: Int = 0,
                 agentSessionID: String = "") -> TrackedProcess {
            TrackedProcess(kind: "review", label: "Review · #7", terminal: "iterm",
                           windowID: "1", sessionID: "", tty: "/dev/ttys001",
                           donePath: "", prURL: nil,
                           createdAt: Date(timeIntervalSinceNow: -600),
                           runner: runner.rawValue, port: port,
                           agentSessionID: agentSessionID)
        }
        func sweep(_ p: TrackedProcess, tail: String,
                   session: AgentSessionProbe.AgentSession?) -> TrackedProcess {
            ProcessMonitor.sweep([p], openWindows: { _ in ["1"] },
                                 sessionTails: ["/dev/ttys001": tail],
                                 ttyElapsed: ["ttys001": 900],
                                 agentSessions: { procs in
                                     guard let session, let first = procs.first else { return [:] }
                                     return [first.id: session]
                                 }).refreshed[0]
        }
        func answer(_ busy: Bool, _ id: String = "ses_ours") -> AgentSessionProbe.AgentSession {
            AgentSessionProbe.AgentSession(sessionID: id,
                                           state: AgentState.SessionState(busy: busy))
        }

        // 1. The two disagreeing cases, which are the whole reason to ask the agent: they
        //    are what a redrawn or reworded status bar looks like, and each is a mistake
        //    the applet used to make with no way to tell it was making one.
        check("a session mid-turn holds its bay though its window looks idle",
              sweep(row(runner: .opencode, port: 47_910), tail: atPrompt, session: answer(true))
                  .awaitingInput == false)
        check("a session that finished its turn gives its bay back though the hint is stale",
              sweep(row(runner: .opencode, port: 47_910), tail: working, session: answer(false))
                  .awaitingInput == true)

        // 2. The match costs a fetch of a session's opening message; the row is where the
        //    answer is kept so the next sweep asks a session it already knows.
        check("the session a run matched is written onto its row",
              sweep(row(runner: .opencode, port: 47_910), tail: working, session: answer(false))
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

        // 5. Hermes, against a real store this writes — the one runner whose answer comes
        //    out of SQLite rather than a socket, so the query, the read-only open and the
        //    match are all exercised rather than stubbed.
        guard let fixture = hermesFixture() else {
            check("a Hermes store fixture could be written", false)
            print("\nSWEEP TEST FAILED")
            return false
        }
        setenv("DIPLOMAT_HERMES_DB", fixture.db, 1)
        func staged(_ runner: AgentRunner, prompt: String) -> TrackedProcess {
            var p = row(runner: runner)
            p.promptFile = prompt
            return p
        }
        let ours = staged(.hermes, prompt: fixture.oursPrompt)
        let mine = AgentSessionProbe.states(for: [ours], directory: fixture.cwd)[ours.id]
        // Two sessions a second apart in one checkout is the ordinary case under the task
        // cap, and only the prompt separates them.
        check("a Hermes run finds its own session and not the one beside it",
              mine?.sessionID == "ses_ours")
        check("a Hermes turn that is mid tool call reads as working",
              mine?.state.busy == true)
        let other = staged(.hermes, prompt: fixture.donePrompt)
        let theirs = AgentSessionProbe.states(for: [other], directory: fixture.cwd)[other.id]
        check("a Hermes turn its agent marked finished reads as back at the prompt",
              theirs?.sessionID == "ses_done" && theirs?.state.busy == false)
        // Input + output + cache writes, never the 9000 cache reads beside them: the
        // per-task figure has to mean the same thing for every runner in one ledger.
        check("a finished Hermes run is priced from its own session row",
              HermesProbe.sessionTokens(sessionID: "ses_ours") == 125)
        check("a session the store has never heard of is unpriced, not free",
              HermesProbe.sessionTokens(sessionID: "ses_gone") == nil)

        // 6. Claude Code serves nothing, so asking any store about it would be asking
        //    about somebody else's session — the runner is what decides who is asked.
        check("a Claude Code run is asked of no store at all",
              AgentSessionProbe.states(for: [staged(.claude, prompt: fixture.oursPrompt)],
                                       directory: fixture.cwd).isEmpty)

        print(pass ? "\nSWEEP TEST OK" : "\nSWEEP TEST FAILED")
        return pass
    }

    /// A throwaway Hermes store: two sessions a second apart in one directory, told apart
    /// only by their opening message, plus the token counts a finished one is priced from.
    ///
    /// Written with SQLite rather than checked in as a binary so the schema this reads is
    /// stated in the test that depends on it.
    private static func hermesFixture()
        -> (db: String, cwd: String, oursPrompt: String, donePrompt: String)? {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("diplomat-sweep-\(UUID().uuidString)")
        guard (try? FileManager.default.createDirectory(at: dir,
                                                        withIntermediateDirectories: true)) != nil
        else { return nil }
        let cwd = dir.appendingPathComponent("repo").path
        let started = Date().timeIntervalSince1970 - 500
        let ours = "Review PR #7 in o/r"
        let done = "Review PR #8 in o/r"
        var db: OpaquePointer?
        guard sqlite3_open_v2(dir.appendingPathComponent("state.db").path, &db,
                              SQLITE_OPEN_CREATE | SQLITE_OPEN_READWRITE, nil) == SQLITE_OK
        else {
            sqlite3_close(db)
            return nil
        }
        defer { sqlite3_close(db) }
        let sql = """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, started_at REAL,
          input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
          cache_write_tokens INTEGER);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
          role TEXT, content TEXT, finish_reason TEXT);
        INSERT INTO sessions VALUES ('ses_theirs', '\(cwd)', \(started), 0, 0, 0, 0);
        INSERT INTO sessions VALUES ('ses_ours', '\(cwd)', \(started + 1), 100, 20, 9000, 5);
        INSERT INTO sessions VALUES ('ses_done', '\(cwd)', \(started + 2), 1, 1, 0, 0);
        INSERT INTO messages (session_id, role, content, finish_reason)
          VALUES ('ses_theirs', 'user', 'something else entirely', NULL),
                 ('ses_ours', 'user', '\(ours)', NULL),
                 ('ses_ours', 'assistant', '', 'tool_calls'),
                 ('ses_done', 'user', '\(done)', NULL),
                 ('ses_done', 'assistant', 'posted', 'stop');
        """
        guard sqlite3_exec(db, sql, nil, nil, nil) == SQLITE_OK else { return nil }
        let oursFile = dir.appendingPathComponent("ours.txt")
        let doneFile = dir.appendingPathComponent("done.txt")
        guard (try? ours.write(to: oursFile, atomically: true, encoding: .utf8)) != nil,
              (try? done.write(to: doneFile, atomically: true, encoding: .utf8)) != nil
        else { return nil }
        return (dir.appendingPathComponent("state.db").path, cwd,
                oursFile.path, doneFile.path)
    }
}
