import Foundation
import SQLite3
import DiplomatCore

/// Headless self-test for what a run's OWN agent says it is doing, driven by
/// `DIPLOMAT_SWEEP_TEST=1`.
///
/// The reading of an answer is pure and pinned in `DiplomatCoreSmoke` (`OpenCodeAPI`,
/// `HermesStore`), and what that answer then decides is pinned by the shared scenario
/// table (`AgentState`). What this covers is the wiring between them, which is where the
/// answer stops being used: that each runner is asked of its own store, that the session a
/// run matched is written into its run directory so it is never matched again, that a
/// runner serving nothing is asked of nothing, and that a finished run is priced from the
/// store that ran it.
///
/// It opens no window, dials no port and needs no agent: the Hermes probe is pointed at a
/// store this file writes, and the OpenCode exporter at one it stages on a throwaway
/// shell's path. A CI runner can host it:
///
///     DIPLOMAT_SWEEP_TEST=1 swift run Diplomat
enum SweepTest {
    /// Returns overall pass/fail so the launcher can exit non-zero — a FAIL that still
    /// exits 0 can't gate anything.
    @discardableResult
    static func run() -> Bool {
        var pass = true
        func check(_ name: String, _ ok: Bool) {
            print("\(ok ? "PASS" : "FAIL") — \(name)")
            if !ok { pass = false }
        }

        // Every run is registered for real, in a scratch book: what is on trial is a
        // probe that reads a run's runner and prompt out of its own directory and writes
        // its session back there, so a fixture that skipped the registry would exercise
        // none of it.
        let agents = FileManager.default.temporaryDirectory
            .appendingPathComponent("diplomat-sweep-agents-\(UUID().uuidString)")
        setenv("DIPLOMAT_AGENTS_DIR", agents.path, 1)
        defer { try? FileManager.default.removeItem(at: agents) }

        guard let fixture = hermesFixture() else {
            check("a Hermes store fixture could be written", false)
            print("\nSWEEP TEST FAILED")
            return false
        }
        setenv("DIPLOMAT_HERMES_DB", fixture.db, 1)

        var dispatched = Date().timeIntervalSince1970 - 600
        func staged(_ runner: AgentRunner, prompt: String) -> AgentState.RunRecord {
            dispatched += 1
            let record = AgentRegistry.createRun(
                AgentState.RunRecord(runID: AgentRegistry.newRunID(now: dispatched),
                                     dispatchedAt: dispatched, prNumber: 7, kind: "review",
                                     label: "Review · #7"),
                prompt: prompt)
            AgentRegistry.stageRunner(record.runID, runner.rawValue)
            return record
        }

        // 1. Hermes, against a real store this writes — the one runner whose answer comes
        //    out of SQLite rather than a socket, so the query, the read-only open and the
        //    match are all exercised rather than stubbed.
        let ours = staged(.hermes, prompt: fixture.oursPrompt)
        let mine = AgentSessionProbe.states(for: [ours], directory: fixture.cwd)[ours.runID]
        // Two sessions a second apart in one checkout is the ordinary case under the task
        // cap, and only the prompt separates them.
        check("a Hermes run finds its own session and not the one beside it",
              AgentRegistry.boundSession(ours.runID) == "ses_ours")
        check("a Hermes turn that is mid tool call reads as working", mine?.busy == true)
        let other = staged(.hermes, prompt: fixture.donePrompt)
        let theirs = AgentSessionProbe.states(for: [other], directory: fixture.cwd)[other.runID]
        check("a Hermes turn its agent marked finished reads as back at the prompt",
              AgentRegistry.boundSession(other.runID) == "ses_done" && theirs?.busy == false)

        // 2. The match costs a fetch of a session's opening message; the run's directory
        //    is where the answer is kept, so the next tick asks a session it already
        //    knows — and so a run that ends can still be priced by it after its prompt is
        //    gone.
        try? FileManager.default.removeItem(at: AgentRegistry.promptPath(ours.runID))
        check("a bound session outlives the prompt that found it",
              AgentSessionProbe.states(for: [ours],
                                       directory: fixture.cwd)[ours.runID]?.busy == true)

        // 3. Claude Code serves nothing, so asking any store about it would be asking
        //    about somebody else's session — the runner is what decides who is asked.
        check("a Claude Code run is asked of no store at all",
              !AgentSessionProbe.serves(AgentRunner.claude.rawValue)
                  && AgentSessionProbe.states(for: [staged(.claude, prompt: fixture.oursPrompt)],
                                              directory: fixture.cwd).isEmpty)

        // 4. Pricing: input + output + cache writes, never the 9000 cache reads beside
        //    them — the per-task figure has to mean the same thing for every runner in
        //    one ledger.
        check("a finished Hermes run is priced from its own session row",
              HermesProbe.sessionTokens(sessionID: "ses_ours") == 125)
        check("a session the store has never heard of is unpriced, not free",
              HermesProbe.sessionTokens(sessionID: "ses_gone") == nil)

        // 5. OpenCode, the one runner whose price comes from a subprocess. The exporter
        //    is reached the way a spawn reaches it — through the user's shell — so a
        //    stub only an rc puts on the path is what proves it: the applet's own
        //    environment is a Dock icon's, and an install of exactly that shape is what
        //    the Settings hint tells the operator will still work.
        if let rcShell = opencodeFixture() {
            let priorPath = ProcessInfo.processInfo.environment["PATH"]
            let priorShell = ProcessInfo.processInfo.environment["SHELL"]
            setenv("SHELL", rcShell, 1)
            setenv("PATH", "/usr/bin:/bin", 1)   // what a desktop launcher hands the app
            check("an rc-only opencode still prices its run",
                  UsageScan.opencodeTaskTokens(sessionID: "ses_ours") == 248)
            // Put the process back: this is the only check that touches the environment,
            // and one left behind would reach whatever is written after it.
            if let priorPath { setenv("PATH", priorPath, 1) } else { unsetenv("PATH") }
            if let priorShell { setenv("SHELL", priorShell, 1) } else { unsetenv("SHELL") }
        } else {
            check("an opencode fixture could be written", false)
        }

        print(pass ? "\nSWEEP TEST OK" : "\nSWEEP TEST FAILED")
        return pass
    }

    /// A throwaway `opencode`, and the shell whose rc is the only thing that finds it.
    /// Returns that shell.
    ///
    /// The exported numbers are the ones the Linux suite and `DiplomatCoreSmoke` assert
    /// against too — 3 + 84 + 40 + 7 + 8 + 106, never the 59384 cache reads beside them.
    private static func opencodeFixture() -> String? {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("diplomat-export-test-\(UUID().uuidString)")
        let bin = dir.appendingPathComponent("opt")
        guard (try? FileManager.default.createDirectory(at: bin,
                                                        withIntermediateDirectories: true)) != nil
        else { return nil }
        let exported = """
        {"messages": [
          {"info": {"role": "user"}},
          {"info": {"role": "assistant",
                    "tokens": {"input": 3, "output": 84, "reasoning": 9,
                               "cache": {"read": 29000, "write": 40}}}},
          {"info": {"role": "assistant",
                    "tokens": {"input": 7, "output": 8, "reasoning": 0,
                               "cache": {"read": 30384, "write": 106}}}}
        ]}
        """
        let exporter = bin.appendingPathComponent("opencode")
        let shell = dir.appendingPathComponent("rcshell")
        // The rc greets, because one that does is ordinary and its greeting lands on the
        // same stdout as the answer.
        let files = [
            (exporter, "#!/bin/sh\ncat <<'JSON'\n\(exported)\nJSON\n"),
            (shell, "#!/bin/sh\necho 'welcome back!'\nexport PATH=\(bin.path):$PATH\n"
                    + "exec /bin/sh \"$@\"\n"),
        ]
        for (url, body) in files {
            guard FileManager.default.createFile(atPath: url.path,
                                                 contents: Data(body.utf8),
                                                 attributes: [.posixPermissions: 0o755])
            else { return nil }
        }
        return shell.path
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
        return (dir.appendingPathComponent("state.db").path, cwd, ours, done)
    }
}
