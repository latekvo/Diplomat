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
        // The same finished turn, with a background fan-out that has not reported yet: it
        // will wake this agent as a fresh user turn, so the run is not over. `ses_done`
        // above carries a delegation of its own that was delivered, which is what makes
        // this the delivery state being read rather than a row being there at all.
        let owed = staged(.hermes, prompt: fixture.owedPrompt)
        let pending = AgentSessionProbe.states(for: [owed], directory: fixture.cwd)[owed.runID]
        check("a Hermes run whose background subagent still owes it a result stays open",
              AgentRegistry.boundSession(owed.runID) == "ses_owed" && pending?.busy == true)
        // The two ways that store goes quiet, told apart on the same bound session. A
        // Hermes too old to delegate in the background has no such table and owes
        // nothing, so its turns still end; a table in a shape this build cannot read
        // proves nothing either way, and a run is never ended on that.
        func reshapeDelegations(_ sql: String) -> Bool {
            var db: OpaquePointer?
            guard sqlite3_open_v2(fixture.db, &db, SQLITE_OPEN_READWRITE, nil) == SQLITE_OK
            else {
                sqlite3_close(db)
                return false
            }
            defer { sqlite3_close(db) }
            return sqlite3_exec(db, "DROP TABLE IF EXISTS async_delegations;" + sql,
                                nil, nil, nil) == SQLITE_OK
        }
        check("a Hermes too old to delegate in the background still ends its turns",
              reshapeDelegations("")
                  && AgentSessionProbe.states(for: [owed],
                                              directory: fixture.cwd)[owed.runID]?.busy == false)
        check("a delegation table this build cannot read leaves the turn unjudged",
              reshapeDelegations("CREATE TABLE async_delegations (delegation_id TEXT);")
                  && AgentSessionProbe.states(for: [owed],
                                              directory: fixture.cwd)[owed.runID] == nil)

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
        //
        //    Which export it is asked for is the binary's major, so each stub answers
        //    only its own major's spelling and exits 1 on the other's.
        if let stub = opencodeFixture() {
            let priorPath = ProcessInfo.processInfo.environment["PATH"]
            let priorShell = ProcessInfo.processInfo.environment["SHELL"]
            setenv("SHELL", stub.shell, 1)
            // What a desktop launcher hands the app, plus an install the rc puts behind the
            // user's own: the agent runs what the shell finds, so that is what is asked.
            setenv("PATH", "\(stub.decoyDir):/usr/bin:/bin", 1)
            check("the opencode the user's shell runs prices its run, not one on the app's PATH",
                  UsageScan.opencodeTaskTokens(sessionID: "ses_ours") == 248)
            // The same path, upgraded in place: the major is remembered per file, so a
            // rewritten binary is asked again.
            check("a 2.x stub could be written",
                  write(stub.exporter, opencodeStub(version: "opencode v2.0.18",
                                                    export: "session export",
                                                    json: serviceExported)))
            check("a 2.x opencode prices its run through `session export`",
                  UsageScan.opencodeTaskTokens(sessionID: "ses_ours") == 127)
            // Put the process back: this is the only check that touches the environment,
            // and one left behind would reach whatever is written after it.
            if let priorPath { setenv("PATH", priorPath, 1) } else { unsetenv("PATH") }
            if let priorShell { setenv("SHELL", priorShell, 1) } else { unsetenv("SHELL") }
        } else {
            check("an opencode fixture could be written", false)
        }

        // 6. An OpenCode 2.x run with no pid to name it: its agent's command line carries
        //    only `--session <id>`, so the process-table scan reads the PR off the
        //    session's opening prompt, and the run is then asked of — and stopped
        //    through — that session. The terminal, tmux and the shells the spawn nests
        //    carry the TUI's words too, at lower pids and on no tty or the tmux client's
        //    (lines as a real spawn in tmux left them in `ps`, on 2.0.18).
        let wrapper = "tmux -L d new-session -s d zsh -i -c 'cd /r; zsh -i -c \"x || exit; "
            + "opencode --session ses_mesh\"; exec sh'"
        let dump = Observation.present("""
          790 ??         00:41 script -q /dev/null \(wrapper)
          791 ttys030    00:41 \(wrapper)
          792 ??         00:41 \(wrapper)
          793 ttys031    00:41 zsh -i -c x || exit; opencode --session ses_mesh
          801 ttys031    00:40 opencode --session ses_mesh
          802 ttys032    00:40 /Users/u/.npm/bin/opencode --session ses_later
          803 ttys033    00:40 opencode --session ses_other
          804 ttys034    00:40 opencode Review PR #11 in o/r
        """)
        var asked: [String] = []
        let openings = ["ses_mesh": "Review PR #9 in o/r", "ses_other": "Review PR #9 in x/y"]
        let scanned = AgentProbes.scan(dump, owner: "o", repo: "r") {
            asked.append($0)
            return openings[$0]
        }
        check("a 2.x agent is found by its session's opening prompt, on its own tty",
              scanned.agents.value == [9: "ttys031", 11: "ttys034"]
                && scanned.sessions == [9: "ses_mesh"])
        check("…only a TUI's own line names a session: no wrapper is asked about or attached",
              asked == ["ses_mesh", "ses_later", "ses_other"]
                && scanned.attached.value == ["ses_mesh", "ses_later", "ses_other"])

        func booked(_ pr: Int, _ placement: AgentState.Placement, port: Int? = nil,
                    prompt: String? = nil) -> AgentState.RunRecord {
            dispatched += 1
            let record = AgentRegistry.createRun(
                AgentState.RunRecord(runID: AgentRegistry.newRunID(now: dispatched),
                                     dispatchedAt: dispatched, prNumber: pr,
                                     placement: placement),
                prompt: prompt ?? "Review PR #\(pr) in o/r")
            AgentRegistry.stageRunner(record.runID, AgentRunner.opencode.rawValue)
            if let port { _ = AgentRegistry.stagePort(record.runID, port) }
            return record
        }
        let meshHere = booked(9, .meshHere)
        // Same PR, same prompt, booked later: the session is already held.
        let meshTwin = booked(9, .meshHere)
        // Same PR, another task: the scan's sighting on PR 9 is not its session.
        let meshOther = booked(9, .meshHere, prompt: "Resolve the conflicts on PR #9 in o/r")
        let meshOld = booked(9, .meshHere, port: 4096)
        let local = booked(9, .local)
        let untracked = AgentState.RunRecord(runID: "untracked:9", dispatchedAt: dispatched,
                                             prNumber: 9, untracked: true)
        let unseen = AgentState.RunRecord(runID: "untracked:12", dispatchedAt: dispatched,
                                          prNumber: 12, untracked: true)
        OpenCodeProbe.adopt([meshHere, meshTwin, meshOther, meshOld, local, untracked, unseen],
                            sessions: scanned.sessions,
                            attached: scanned.attached.value ?? []) { openings[$0] }
        check("a run the mesh placed here is bound to the session opened with its prompt",
              AgentRegistry.boundSession(meshHere.runID) == "ses_mesh"
                && OpenCodeProbe.serviceSession(of: meshHere) == "ses_mesh")
        check("…and no other run is bound to it, whatever PR it is on",
              AgentRegistry.boundSession(meshTwin.runID).isEmpty
                && AgentRegistry.boundSession(meshOther.runID).isEmpty)
        check("…nor is a 1.x one, nor one this applet spawned",
              AgentRegistry.boundSession(meshOld.runID).isEmpty
                && AgentRegistry.boundSession(local.runID).isEmpty)
        check("a synthesized run is given the session in memory, and asked of the service",
              OpenCodeProbe.serviceSession(of: untracked) == "ses_mesh"
                && AgentSessionProbe.serves(untracked)
                && !AgentSessionProbe.serves(unseen))
        // Which sessions a tick's ending interrupts. `meshHere` (ses_mesh) was reaped and
        // retired both; `untracked` (ses_mesh too) retired alongside it; `closed` retired
        // with no TUI left on its session; `merged` retired with its TUI still up.
        let closed = booked(13, .local)
        AgentRegistry.bindSession(closed.runID, "ses_closed")
        let merged = booked(14, .local)
        AgentRegistry.bindSession(merged.runID, "ses_merged")
        let retired = [meshHere, untracked, closed, merged]
        check("a reaped run is interrupted once, a retired one only when no TUI is attached",
              OpenCodeProbe.interrupts(reaped: [meshHere], retired: retired,
                                       attached: .present(["ses_mesh", "ses_merged"]))
                  == ["ses_mesh", "ses_closed"])
        check("…and a process table that could not be read shows no TUI",
              OpenCodeProbe.interrupts(reaped: [], retired: retired,
                                       attached: .unavailable("could not be read"))
                  == ["ses_mesh", "ses_closed", "ses_merged"])

        OpenCodeProbe.forgetAdopted([untracked.runID])
        check("…until it is retired: the next agent on its PR is not that session's",
              OpenCodeProbe.serviceSession(of: untracked) == nil
                && !AgentSessionProbe.serves(untracked))

        // A session the service gave no opening prompt for is not asked again for
        // `missMemory` — each ask of a hung service is a timeout, on every tick — and one
        // it answered is never asked again.
        var fetched = 0
        let missing = "ses_miss_\(UUID().uuidString.prefix(8))"
        let at: TimeInterval = 1_000
        func opening(after seconds: TimeInterval, answer: String?) -> String? {
            OpenCodeProbe.openingPrompt(sessionID: missing, now: at + seconds) { _ in
                fetched += 1
                return answer
            }
        }
        let first = opening(after: 0, answer: nil)
        let within = opening(after: OpenCodeProbe.missMemory - 1, answer: "Review PR #9 in o/r")
        check("a missed opening prompt is not asked again within \(Int(OpenCodeProbe.missMemory)) s",
              first == nil && within == nil && fetched == 1)
        let after = opening(after: OpenCodeProbe.missMemory, answer: "Review PR #9 in o/r")
        let later = opening(after: 86_400, answer: nil)
        check("…is asked again after it, and a hit is kept for good",
              after == "Review PR #9 in o/r" && later == after && fetched == 2)

        // One pass asks the service for the active map once, however many runs it holds.
        var gets: [String] = []
        let servicePass = OpenCodeProbe.ServicePass(
            find: { OpenCodeAPI.serviceEndpoint(Data(#"{"url": "http://127.0.0.1:1"}"#.utf8)) },
            get: { _, path in
                gets.append(path)
                if path == OpenCodeAPI.serviceActivePath { return ["data": ["ses_a": [String: Any]()]] }
                return ["data": ["time": ["idle": 5]]]
            })
        let busy = OpenCodeProbe.serviceState(sessionID: "ses_a", pass: servicePass)
        let idle = OpenCodeProbe.serviceState(sessionID: "ses_b", pass: servicePass)
        check("a pass fetches the active map once for every run it asks about",
              gets.filter { $0 == OpenCodeAPI.serviceActivePath }.count == 1
                && gets.count == 3 && busy?.busy == true && idle?.busy == false)
        // A service that does not answer the active map is asked nothing else that pass.
        gets = []
        let hungPass = OpenCodeProbe.ServicePass(
            find: { OpenCodeAPI.serviceEndpoint(Data(#"{"url": "http://127.0.0.1:1"}"#.utf8)) },
            get: { _, path in
                gets.append(path)
                return path == OpenCodeAPI.serviceActivePath ? nil : ["data": ["time": ["idle": 5]]]
            })
        let unasked = [OpenCodeProbe.serviceState(sessionID: "ses_a", pass: hungPass),
                       OpenCodeProbe.serviceState(sessionID: "ses_b", pass: hungPass)]
        check("…and a pass whose active map failed asks for no session",
              unasked.allSatisfy { $0 == nil } && gets == [OpenCodeAPI.serviceActivePath])

        print(pass ? "\nSWEEP TEST OK" : "\nSWEEP TEST FAILED")
        return pass
    }

    /// A throwaway 1.x `opencode`, the shell whose rc puts it first, and a directory
    /// holding a broken `opencode` for the app's own `PATH`. Returns that shell, the stub
    /// so a check can swap in another major, and the decoy's directory.
    ///
    /// The exported numbers are the ones the Linux suite and `DiplomatCoreSmoke` assert
    /// against too — 3 + 84 + 40 + 7 + 8 + 106, never the 59384 cache reads beside them.
    private static func opencodeFixture() -> (shell: String, exporter: URL, decoyDir: String)? {
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
        let decoyDir = dir.appendingPathComponent("system")
        guard (try? FileManager.default.createDirectory(at: decoyDir,
                                                        withIntermediateDirectories: true)) != nil
        else { return nil }
        // The rc greets, because one that does is ordinary and its greeting lands on the
        // same stdout as the answer.
        let files = [
            (exporter, opencodeStub(version: "1.4.3", export: "export", json: exported)),
            (decoyDir.appendingPathComponent("opencode"), "#!/bin/sh\nexit 1\n"),
            (shell, "#!/bin/sh\necho 'welcome back!'\nexport PATH=\(bin.path):$PATH\n"
                    + "exec /bin/sh \"$@\"\n"),
        ]
        for (url, body) in files where !write(url, body) { return nil }
        return (shell.path, exporter, decoyDir.path)
    }

    /// 2.x's export: the same numbers' first message, its tokens at the message's top
    /// level rather than under `info` — 3 + 84 + 40.
    private static let serviceExported = """
    {"info": {"id": "ses_ours"}, "messages": [
      {"type": "user", "text": "Review PR #7 in o/r"},
      {"type": "assistant",
       "tokens": {"input": 3, "output": 84, "reasoning": 9, "cache": {"read": 29000, "write": 40}}},
      {"type": "idle", "outcome": "succeeded"}
    ]}
    """

    /// An `opencode` that prints `version` for `--version` and `json` for `<export> ses_ours`
    /// — `export` on 1.x, `session export` on 2.x — and exits 1 on anything else.
    private static func opencodeStub(version: String, export: String, json: String) -> String {
        """
        #!/bin/sh
        if [ "$1" = --version ]; then echo '\(version)'; exit 0; fi
        if [ "$*" = '\(export) ses_ours' ]; then
        cat <<'JSON'
        \(json)
        JSON
        exit 0
        fi
        exit 1

        """
    }

    /// An executable file with this body, replacing whatever was there.
    private static func write(_ url: URL, _ body: String) -> Bool {
        FileManager.default.createFile(atPath: url.path, contents: Data(body.utf8),
                                       attributes: [.posixPermissions: 0o755])
    }

    /// A throwaway Hermes store: three sessions a second apart in one directory, told
    /// apart only by their opening message, plus the token counts a finished one is priced
    /// from and the delegation rows that say whether a finished turn is a finished run.
    ///
    /// Written with SQLite rather than checked in as a binary so the schema this reads is
    /// stated in the test that depends on it.
    private static func hermesFixture()
        -> (db: String, cwd: String, oursPrompt: String, donePrompt: String,
            owedPrompt: String)? {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("diplomat-sweep-\(UUID().uuidString)")
        guard (try? FileManager.default.createDirectory(at: dir,
                                                        withIntermediateDirectories: true)) != nil
        else { return nil }
        let cwd = dir.appendingPathComponent("repo").path
        let started = Date().timeIntervalSince1970 - 500
        let ours = "Review PR #7 in o/r"
        let done = "Review PR #8 in o/r"
        let owed = "Review PR #9 in o/r"
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
        CREATE TABLE async_delegations (delegation_id TEXT PRIMARY KEY,
          origin_session TEXT, parent_session_id TEXT, state TEXT, delivery_state TEXT);
        INSERT INTO sessions VALUES ('ses_theirs', '\(cwd)', \(started), 0, 0, 0, 0);
        INSERT INTO sessions VALUES ('ses_ours', '\(cwd)', \(started + 1), 100, 20, 9000, 5);
        INSERT INTO sessions VALUES ('ses_done', '\(cwd)', \(started + 2), 1, 1, 0, 0);
        INSERT INTO sessions VALUES ('ses_owed', '\(cwd)', \(started + 3), 1, 1, 0, 0);
        INSERT INTO messages (session_id, role, content, finish_reason)
          VALUES ('ses_theirs', 'user', 'something else entirely', NULL),
                 ('ses_ours', 'user', '\(ours)', NULL),
                 ('ses_ours', 'assistant', '', 'tool_calls'),
                 ('ses_done', 'user', '\(done)', NULL),
                 ('ses_done', 'assistant', 'posted', 'stop'),
                 ('ses_owed', 'user', '\(owed)', NULL),
                 ('ses_owed', 'assistant', 'dispatched', 'stop');
        INSERT INTO async_delegations
          VALUES ('deleg_done', 'ses_done', 'ses_done', 'completed', 'delivered'),
                 ('deleg_owed', '', 'ses_owed', 'running', 'pending');
        """
        guard sqlite3_exec(db, sql, nil, nil, nil) == SQLITE_OK else { return nil }
        return (dir.appendingPathComponent("state.db").path, cwd, ours, done, owed)
    }
}
