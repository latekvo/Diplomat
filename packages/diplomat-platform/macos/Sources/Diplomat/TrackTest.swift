import Foundation
import DiplomatCore

/// Headless end-to-end self-test for the agent run book on macOS, driven by
/// `DIPLOMAT_TRACK_TEST=1`.
///
/// What each state MEANS is pure, shared with the Linux front-end and pinned by
/// `DiplomatCoreSmoke` plus the parity suite. What is on trial here is the half that
/// cannot be shared: this platform's probes (`AgentProbes` — BSD `ps`, AppleScript session
/// dumps), the window handle a spawn keeps beside its run (`AgentWindows`), and the
/// registry round-trip through a real directory. Then a live capture → registry → resolve →
/// focus → retire cycle against a throwaway terminal window it cleans up itself.
///
/// Run it via the installed `.app` binary so the live portion inherits the granted
/// "control <terminal>" automation permission:
///   DIPLOMAT_TRACK_TEST=1 /Applications/Diplomat.app/Contents/MacOS/Diplomat
enum TrackTest {
    /// Returns overall pass/fail so the launcher can exit non-zero — a FAIL that
    /// still exits 0 can't gate anything.
    @discardableResult
    static func run() async -> Bool {
        var pass = true
        func check(_ name: String, _ ok: Bool) {
            print("\(ok ? "PASS" : "FAIL") — \(name)")
            if !ok { pass = false }
        }

        // Every run below is registered for real, in a scratch book beside the operator's.
        let agents = FileManager.default.temporaryDirectory
            .appendingPathComponent("diplomat-track-agents-\(UUID().uuidString)")
        setenv("DIPLOMAT_AGENTS_DIR", agents.path, 1)
        defer { try? FileManager.default.removeItem(at: agents) }

        // 1. Capture parsing (iTerm "wid|sid|tty" and Terminal's empty-field form).
        let c1 = AgentSpawner.parseCapture("37216|ABC-DEF|/dev/ttys016\n")
        check("parseCapture iTerm wid/sid/tty", c1 == ("37216", "ABC-DEF", "/dev/ttys016"))
        let c2 = AgentSpawner.parseCapture("44||\n")
        check("parseCapture Terminal empty sid", c2 == ("44", "", ""))

        // 2. The process table, from a real BSD `ps` dump. `etime` is the column this
        //    platform has (there is no `etimes` here), and its `[[dd-]hh:]mm:ss` form is
        //    what the resolver's pid-adoption guard compares against a run's age — parsed
        //    wrong, every live agent would read as too young to be its own record's.
        check("etime parses mm:ss / hh:mm:ss / dd-hh:mm:ss",
              AgentProbes.parseElapsed("03:07") == 187
                && AgentProbes.parseElapsed("01:02:03") == 3723
                && AgentProbes.parseElapsed("2-00:00:10") == 172_810
                && AgentProbes.parseElapsed("junk") == nil)
        let dump = Observation.present("""
          701 ttys001    03:07 claude Review PR #436 in software-mansion/argent. Use the `gh` CLI.
          702 ttys002 01:02:03 claude Take PR #369 in software-mansion/argent. Use the `gh` CLI.
          703 ttys003    00:09 claude Review PR #99 in other-org/other-repo. Use the `gh` CLI.
          704 ttys004    00:30 /bin/zsh -i -c cd '/Users/x/repo' 2>/dev/null; claude "$(cat '/tmp/p')"
          705 ??         10:00 /usr/libexec/secinitd
          706 ttys005    00:11 grep PR #123 in software-mansion/argent
          """)
        let table = AgentProbes.processTable(dump).value ?? [:]
        check("every line of a real dump becomes one row", table.count == 6)
        check("a process with no controlling terminal has no screen to read",
              table[705]?.tty == "" && table[705]?.isAgent == false)
        check("the tty is kept as `ps` spells it, which is how a screen is looked up",
              table[701]?.tty == "ttys001" && table[701]?.elapsed == 187)
        // The spawning shell's own argv carries the agent's name, so it reads as an
        // agent process. That is deliberate and is what the age half of the pid-adoption
        // guard is for — a wrapper and the agent share a terminal and a lifetime, and
        // under a shell that execs the agent over the wrapper, one pid.
        check("a wrapper shell counts as an agent line", table[704]?.isAgent == true)
        check("a line that merely mentions a runner is not one", table[706]?.isAgent == false)

        // 2b. The prompt scan: the pre-registry identity mechanism, kept for the agents a
        //     pid cannot speak for — ones nobody here dispatched. The spawning shell's
        //     argv holds an unexpanded `$(cat …)` rather than the prompt, and another
        //     repo's agent is not ours.
        let scan = AgentProbes.liveAgents(dump, owner: "software-mansion",
                                          repo: "argent").value ?? [:]
        check("the scan finds this repo's agents and nothing else",
              Set(scan.keys) == [436, 369])
        check("…each on the tty its screen is read from",
              scan[436] == "ttys001" && scan[369] == "ttys002")

        // 2c. The two sources that have to meet on a tty do not spell it the same way:
        //     `ps` reports a bare device name, the terminal apps a full `/dev/…` path. One
        //     spelling wins or every screen lookup misses and no bay is ever handed back.
        check("every spelling of one tty lands on the same key",
              AgentProbes.shortTTY("/dev/ttys013") == "ttys013"
                && AgentProbes.shortTTY("ttys013") == "ttys013")

        // 3. The registry round-trip, through a real directory — the book both front-ends
        //    and the mesh node read. A mesh placement back on THIS machine is the reading
        //    a round-trip can silently lose: reloaded as a peer's it frees a bay this
        //    device is in fact using, and the cap spends it twice.
        let now = Date().timeIntervalSince1970
        var mesh = AgentState.RunRecord(
            runID: AgentRegistry.newRunID(now: now), dispatchedAt: now, prNumber: 7,
            prURL: "https://github.com/a/b/pull/7", kind: "review",
            label: "Auto · Review · #7", source: AgentDispatchGate.Source.auto.rawValue,
            placement: .meshHere, node: "softoobox",
            workKey: "review:github.com/a/b#7@sha", ledgerKey: "l7")
        mesh.pid = 4021
        mesh.tty = "ttys991"
        AgentRegistry.createRun(mesh, prompt: "review it")
        let reloaded = AgentRegistry.load().first { $0.runID == mesh.runID }
        check("a record survives the book byte for byte", reloaded == mesh)
        check("…including that the mesh put it back on this machine",
              reloaded?.placement == .meshHere && reloaded?.runsHere == true)
        check("the prompt is kept beside the run, so a restart can still price it",
              AgentRegistry.prompt(mesh.runID) == "review it")

        // 3b. The three sidecars a macOS run adds beside the shared record, and the one
        //     only this platform has: the window handle a click raises.
        AgentRegistry.stageRunner(mesh.runID, AgentRunner.opencode.rawValue)
        _ = AgentRegistry.stagePort(mesh.runID, 47_910)
        AgentWindows.stage(mesh.runID, .init(terminal: "iterm", windowID: "999",
                                             sessionID: "SID"))
        check("a run remembers which CLI ran it and where its server answers",
              AgentRegistry.runRunner(mesh.runID) == AgentRunner.opencode.rawValue
                && AgentRegistry.port(mesh.runID) == 47_910)
        check("…and where its window is", AgentWindows.handle(mesh.runID)?.windowID == "999")
        check("a run this applet never spawned has no handle staged",
              AgentWindows.handle("never-existed") == nil)
        AgentRegistry.forget([mesh.runID])
        check("forgetting a run takes its whole directory, sidecars and all",
              AgentRegistry.load().isEmpty && AgentWindows.handle(mesh.runID) == nil)

        // 4. Focus script embeds the captured ids (so it targets the right window), and
        //    addresses the window by id rather than searching for it: `activate` returns
        //    before the app has reordered its windows, which renumbers the index-based
        //    references a search is walking, and the search then steps over the very
        //    window it was given the id of.
        let fs = AgentWindows.focusScript(term: .iterm, windowID: "999", sessionID: "SID")
        check("focusScript embeds windowID + sessionID", fs.contains("999") && fs.contains("SID"))
        check("focusScript addresses the window by id, and activates last",
              fs.contains("window id 999") && !fs.contains("repeat with w in windows")
                && (fs.range(of: "activate").map { fs.range(of: "select w")!.upperBound < $0.lowerBound }
                    ?? false))

        // 4b. The walk from an agent's own tty out to the window showing it — the only
        //     route to a session this applet did not open, and the one thing between a
        //     pane and its window that a parent chain cannot cross.
        //
        //     The tree below is the real shape of a spawn on a box whose rc starts every
        //     interactive shell inside tmux, with a shell wrapper on top of it: the agent
        //     sits two ptys below its pane, and the pane's parent is the tmux SERVER, so
        //     the window is only reachable by hopping to the client attached to that
        //     pane's session and walking out from there.
        let wrapped: [Int: TerminalFocus.Proc] = [
            392: .init(ppid: 139, tty: "ttys038"),      // the agent
            139: .init(ppid: 132, tty: "ttys038"),      // the shell that exec'd it
            132: .init(ppid: 2212, tty: "ttys037"),     // the pane's own shell
            2212: .init(ppid: 1, tty: ""),              // the tmux server — a dead end
            107: .init(ppid: 100, tty: "ttys036"),      // the client attached to it
            100: .init(ppid: 99998, tty: "ttys034"),    // the window's shell
            99998: .init(ppid: 1, tty: ""),
        ]
        let panes = ["ttys037": TerminalFocus.Pane(id: "%80", session: "80")]
        let attached = ["80": "ttys036"]
        let hop = TerminalFocus.walk(tty: "ttys038", pid: 392, processes: wrapped,
                                     panes: panes, clients: attached)
        check("the walk crosses tmux and ends on the window's tty",
              hop.ttys == ["ttys038", "ttys037", "ttys036", "ttys034"]
                && hop.panes == ["%80"])
        check("…and starts from the tty alone when no pid is known",
              TerminalFocus.walk(tty: "ttys038", pid: nil, processes: wrapped,
                                 panes: panes, clients: attached) == hop)
        // A parked session has no client, so there is no window to raise and the walk
        // stops at the pane rather than wandering up the server's ancestry.
        check("a detached tmux session leads nowhere",
              TerminalFocus.walk(tty: "ttys038", pid: 392, processes: wrapped,
                                 panes: panes, clients: [:]).ttys == ["ttys038", "ttys037"])
        // Nothing wrapping the session: the agent's own tty IS the window's, which is
        // the pre-tmux shape and must still be one step.
        check("an unwrapped session is one step",
              TerminalFocus.walk(tty: "ttys001", pid: 700,
                                 processes: [700: .init(ppid: 50, tty: "ttys001"),
                                             50: .init(ppid: 1, tty: "")],
                                 panes: [:], clients: [:]).ttys == ["ttys001"])
        check("a run with no tty has nothing to walk",
              TerminalFocus.walk(tty: "", pid: nil, processes: wrapped,
                                 panes: panes, clients: attached).ttys.isEmpty)
        check("the lookup script tries every candidate, nearest first",
              TerminalFocus.itermScript(["/dev/ttys038", "/dev/ttys034"])
                .contains("{\"/dev/ttys038\", \"/dev/ttys034\"}"))
        // The reaper's route for a run with no handle. It closes and does not activate:
        // `activate` renumbers the index-based references the search is still walking,
        // which is the bug the focus script above is written around.
        for (term, script) in [("iTerm", TerminalFocus.itermCloseScript(["/dev/ttys038"])),
                               ("Terminal", TerminalFocus.terminalCloseScript(["/dev/ttys038"]))] {
            check("the \(term) close script closes the matched window and never activates",
                  script.contains("{\"/dev/ttys038\"}") && script.contains("close w")
                    && !script.contains("activate"))
        }

        //     The same walk, asked of the SCREEN rather than the window. A run this applet
        //     spawned carries the tty of the window it opened and matches a session dump
        //     outright; a run it did not carries the agent's own, which on the shape above
        //     names no window at all — and a screen that cannot be read is RUNNING for as
        //     long as the session lives, holding a bay of the automatic-task cap.
        let screens = ["ttys034": "the agent's screen", "ttys001": "an unwrapped shell"]
        let adopted = AgentProbes.adoptWrappedTails([(tty: "ttys038", pid: 392)],
                                                    into: screens, processes: wrapped,
                                                    panes: panes, clients: attached)
        check("a wrapped run reads the screen of the window showing it",
              adopted["ttys038"] == "the agent's screen" && adopted.count == 3)
        // An agent nobody dispatched has no record yet, so it arrives as a bare tty out
        // of the process scan. It is the whole reason the walk is asked of the screen:
        // its tty is ALWAYS the agent's own, never a window's.
        check("…and so does one known only by its tty",
              AgentProbes.adoptWrappedTails([(tty: "ttys038", pid: nil)], into: screens,
                                            processes: wrapped, panes: panes,
                                            clients: attached) == adopted)
        check("a run whose own tty the dump already carries is not walked",
              AgentProbes.adoptWrappedTails([(tty: "ttys001", pid: 700)], into: screens,
                                            processes: [:], panes: [:],
                                            clients: [:]) == screens)
        check("a walk that reaches no terminal adopts nothing",
              AgentProbes.adoptWrappedTails([(tty: "ttys038", pid: 392)], into: screens,
                                            processes: wrapped, panes: panes,
                                            clients: [:]) == screens)
        // Which of those bare ttys are gathered at all: an agent whose PR a record
        // already covers is walked as that record, and must not be walked twice.
        var booked = AgentState.RunRecord(runID: "booked", dispatchedAt: 0, kind: "review")
        booked.prNumber = 705
        check("only the live agents no record covers are gathered",
              AgentProbes.unbookedTTYs([booked], .present([705: "ttys038", 703: "ttys029"]))
                == ["ttys029"])
        check("a scan that failed gathers nothing",
              AgentProbes.unbookedTTYs([booked], .unavailable("no dump")).isEmpty)

        // 4c. Which rows offer the click at all. Both the button and the click itself
        //     ask this one question, so a row can never be pressable and inert.
        func rowFor(_ tty: String, handle: AgentWindows.Handle?) -> Store.AgentRow {
            var r = AgentState.RunRecord(runID: "r", dispatchedAt: 0, kind: "review")
            r.tty = tty
            return Store.AgentRow(record: r, state: .running, reason: "", window: handle)
        }
        let staged = AgentWindows.Handle(terminal: "iterm", windowID: "7", sessionID: "S")
        check("a run this applet spawned is clickable by its handle",
              rowFor("", handle: staged).isFocusable)
        check("…one nobody dispatched, by the tty its agent is on",
              rowFor("ttys029", handle: nil).isFocusable)
        check("…and one running on a peer by neither",
              !rowFor("", handle: nil).isFocusable)

        // 5. Snapshot parse computes "threads I owe" (the offline-review reconcile signal):
        //    unresolved + I-can-resolve + last comment isn't mine. Threads I already
        //    replied to (last comment == me), resolved threads, and ones I can't resolve
        //    are excluded — so we don't auto-fix a PR whose ball is with the reviewer.
        let parseJSON = """
        {"data":{"search":{"nodes":[
          {"number":100,"title":"t","url":"u/100","isDraft":false,"author":{"login":"me"},
           "mergeable":"MERGEABLE","reviewDecision":"CHANGES_REQUESTED","headRefName":"b",
           "reviewThreads":{"nodes":[
             {"isResolved":false,"viewerCanResolve":true,"comments":{"nodes":[{"author":{"login":"reviewer"}}]}},
             {"isResolved":false,"viewerCanResolve":true,"comments":{"nodes":[{"author":{"login":"me"}}]}},
             {"isResolved":true,"viewerCanResolve":true,"comments":{"nodes":[{"author":{"login":"reviewer"}}]}},
             {"isResolved":false,"viewerCanResolve":false,"comments":{"nodes":[{"author":{"login":"reviewer"}}]}}
           ]}}
        ]}}}
        """
        let parsed = (try? AutofixMonitor.parse(Data(parseJSON.utf8), me: "me"))?.first
        check("parse counts all unresolved threads", parsed?.threadsUnresolved == 3)
        check("parse counts only threads I owe a reply on", parsed?.threadsIOwe == 1)

        // 6. Live cycle against a real, self-closing terminal window.
        await liveCycle(check: check)

        print(pass ? "\nTRACK_TEST OK" : "\nTRACK_TEST FAILED")
        return pass
    }

    /// The whole mechanism against one real window: register a run, spawn a stand-in
    /// agent into it, and watch the resolver carry it from `.starting` through `.running`
    /// to `.finished` off nothing but the run's own pid file and sentinel.
    private static func liveCycle(check: (String, Bool) -> Void) async {
        let term = AgentSpawner.resolved(.ghostty)
        let now = Date().timeIntervalSince1970
        let record = AgentRegistry.createRun(
            AgentState.RunRecord(runID: AgentRegistry.newRunID(now: now), dispatchedAt: now,
                                 prNumber: 1, kind: "review", label: "self-test"),
            prompt: "self-test")
        defer { AgentRegistry.forget([record.runID]) }
        // A benign stand-in for the agent, NAMED as one: every scan that counts, adopts
        // or reaps an agent asks `AgentRunner.isAgentLine`, so a stand-in under any other
        // name would be read as a recycled pid and retired on its first pass.
        let bin = FileManager.default.temporaryDirectory
            .appendingPathComponent("diplomat-track-bin-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: bin, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: bin) }
        let stand = bin.appendingPathComponent(AgentRunner.claude.rawValue)
        // Released by the test rather than run on a clock: nothing below it is bounded —
        // the resolve loop allows fifteen passes of the whole evidence gather, and raising
        // a window walks every session the operator has open. A stand-in that outlives
        // those on a quiet machine is gone before them on a busy one, where `ps` then
        // reports no tty and the checks below read a departed agent as an unreachable one.
        // The 1500 turns cap it at five minutes if the test dies without releasing it.
        let release = bin.appendingPathComponent("release")
        guard FileManager.default.createFile(
                atPath: stand.path,
                contents: Data("""
                    #!/bin/sh
                    echo 'diplomat tracking self-test — this window closes itself'
                    n=0
                    while [ ! -f \(AgentSpawner.shq(release.path)) ] && [ "$n" -lt 1500 ]
                    do
                        sleep 0.2
                        n=$((n + 1))
                    done
                    """.utf8),
                attributes: [.posixPermissions: 0o755]) else {
            check("a stand-in agent could be staged", false)
            return
        }
        // Written the way a real spawn writes it: the inner shell records its own pid and
        // then becomes the agent, so the pid the resolver adopts is the one it would
        // adopt for a real run (`AgentSpawner.shellCommand`).
        let pidPath = AgentRegistry.pidPath(record.runID).path
        let donePath = AgentRegistry.donePath(record.runID).path
        let inner = "printf %s $$ > \(AgentSpawner.shq(pidPath)); \(AgentSpawner.shq(stand.path))"
        let cmd = "\"$SHELL\" -i -c \(AgentSpawner.shq(inner)); "
            + AgentSpawner.sentinel(donePath)
        guard let cap = try? AgentSpawner.runSpawn(command: cmd, terminal: term),
              !cap.0.isEmpty, !cap.2.isEmpty else {
            print("SKIP — live \(term.title) capture unavailable (automation not granted?)")
            return
        }
        let (wid, sid, tty) = cap
        AgentWindows.stage(record.runID, .init(terminal: term.rawValue, windowID: wid,
                                               sessionID: sid))
        var seeded = record
        seeded.tty = AgentProbes.shortTTY(tty)
        _ = AgentRegistry.save([seeded])
        check("live capture returns wid + tty", !wid.isEmpty && !tty.isEmpty)

        /// One pass of the real pipeline: real `ps`, real sentinels, real screens.
        func resolve() -> AgentState.Resolution? {
            AgentProbes.resetCache()   // each pass is its own look at the machine
            let at = Date().timeIntervalSince1970
            let records = AgentRegistry.adoptPids(AgentRegistry.load())
            let evidence = AgentProbes.gather(records: records, now: at, owner: "o", repo: "r",
                                              directory: AgentSpawner.repoPath,
                                              meshEnabled: false, meshState: nil,
                                              merged: .present([]),
                                              // This self-test spawns and watches one
                                              // real agent within seconds; the deadline
                                              // has no bearing on it.
                                              tokens: .present(false))
            return AgentState.resolve(records: records, evidence: evidence,
                                      now: at)[record.runID]
        }

        var found: AgentState.Resolution?
        for _ in 0..<15 {
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            // `.starting` is what a run reads as while nothing at all has been observed
            // of it; anything else means the pid file landed and the table was read.
            if let r = resolve(), r.state != .starting { found = r; break }
        }
        print("live: \(found.map { "\($0.state.rawValue) — \($0.reason)" } ?? "nothing resolved")")
        // Which live state it lands in is not the point and is not stable: it turns on
        // whether the stand-in's own output reads as a busy status bar, and it is a
        // `sleep`, not an agent. What is on trial is that the run was found AT ALL, off
        // nothing but the pid its own shell wrote down.
        check("a live run is found by the pid its own shell wrote",
              found.map { AgentState.blocking.contains($0.state) } == true
                && found?.reason.contains("alive") == true)
        // Raised repeatedly, because the failure this replaced was intermittent: one
        // click in three landed on a window search that had been renumbered under it.
        let handle = AgentWindows.handle(record.runID)
        check("its window is raisable from the handle its spawn kept, every time",
              handle != nil && (1...4).allSatisfy { _ in handle.map(AgentWindows.focus) == true })
        // The other route, and the only one a run without a handle has: the agent's OWN
        // tty, which is the window's only when nothing wraps the session. Under tmux, or
        // a shell wrapper that opens a pty of its own, it is a pty no terminal shows.
        let agentPID = AgentRegistry.adoptPids(AgentRegistry.load())
            .first { $0.runID == record.runID }?.pid
        let agentTTY = agentPID.flatMap(DeviceFocus.tty(forPid:)) ?? ""
        // Split from the focus below: no tty is an agent that has gone rather than a
        // route that failed, and the two together read as the second.
        check("the agent is still on a terminal to be walked from", !agentTTY.isEmpty)
        // A DIFFERENT window in front first, or the two checks below prove nothing: the
        // run's window is the one the spawn just opened, so it is frontmost already and a
        // walk that raised nothing would leave it there. Best-effort — one Ghostty window
        // on the machine means no decoy to offer.
        if term == .ghostty {
            OSAScript.runSilently("""
                tell application "Ghostty"
                    activate window (first window whose name is not "\(sid)")
                    activate
                end tell
                """)
        }
        check("…and from the agent's own process, with no handle at all",
              TerminalFocus.focus(tty: agentTTY, pid: agentPID))
        // Not merely that something came forward: the walk ends at a bare `activate` for
        // a window it cannot name, which leaves whichever was in front there — so "focus
        // returned true" and "the agent's window is up" are two claims. The session name
        // is the title the spawn wrote, so the front window's own name settles it.
        if term == .ghostty {
            var raised = false
            for _ in 0..<15 where !raised {
                raised = OSAScript.capture("tell application \"Ghostty\" to get name of front window")?
                    .trimmingCharacters(in: .whitespacesAndNewlines) == sid
                if !raised { try? await Task.sleep(nanoseconds: 200_000_000) }
            }
            check("…on the window the agent is in, not whichever was already in front", raised)
        }
        check("focus of a vanished window fails (→ the row is dismissed)",
              !AgentWindows.focus(.init(terminal: term.rawValue, windowID: "99999999",
                                        sessionID: "nope")))

        // The API-error watcher's other half, against the window this run is in: the
        // nudge has to be READ BACK off the screen it was typed into, because "the
        // terminal accepted it" and "the agent was asked to continue" are not the same
        // claim — a Ghostty run is typed into through tmux, and a tmux `send-keys` to a
        // pane that has gone still exits zero on some paths.
        check("the continue nudge lands in the run's own session",
              ApiErrorWatcher.sendContinue(tty: agentTTY))
        var echoed = false
        for _ in 0..<15 where !echoed {
            echoed = ApiErrorWatcher.dumpSessions()?.contains {
                AgentProbes.shortTTY($0.tty) == AgentProbes.shortTTY(agentTTY)
                    && $0.tail.contains(ApiErrorWatcher.continueMessage)
            } ?? false
            if !echoed { try? await Task.sleep(nanoseconds: 200_000_000) }
        }
        check("…and the dump reads it back off that screen", echoed)
        check("a nudge to a tty nothing is on reports that it landed nowhere",
              !ApiErrorWatcher.sendContinue(tty: "ttys999"))

        // Releases the stand-in, whose shell then writes the sentinel the last check waits for.
        FileManager.default.createFile(atPath: release.path, contents: nil)
        var finished = false
        for _ in 0..<25 {
            if resolve()?.state == .finished { finished = true; break }
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
        check("the sentinel its shell writes retires the run", finished)
        closeWindow(term: term, windowID: wid)

        await reapCycle(term: term, check: check)
    }

    /// The reaper's route to a window it has no handle for: the same walk as the focus
    /// above, asked to close rather than raise. A window of its own, because the walk
    /// resolves only while the process it starts from is alive, and this one has to be
    /// closed out from under that process — which the cycle above cannot allow, since a
    /// shell whose window went away never writes the sentinel it is waiting for.
    ///
    /// Alive is also what is being stood in for: what the backstop reaps is a WEDGED run,
    /// whose agent is by definition still sitting in front of a screen that stopped
    /// moving twenty minutes ago.
    private static func reapCycle(term: SpawnTerminal, check: (String, Bool) -> Void) async {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("diplomat-track-reap-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        // Deleting the directory is what releases the stand-in, so the teardown cannot
        // race the file it waits on. The 1500 turns are the leash for a close that does
        // not land: a session left detached inside tmux still ends five minutes later.
        defer { try? FileManager.default.removeItem(at: dir) }
        let keep = dir.appendingPathComponent("keep")
        FileManager.default.createFile(atPath: keep.path, contents: nil)
        let pidFile = dir.appendingPathComponent("pid")
        let inner = """
            printf %s $$ > \(AgentSpawner.shq(pidFile.path))
            n=0
            while [ -f \(AgentSpawner.shq(keep.path)) ] && [ "$n" -lt 1500 ]
            do
                sleep 0.2
                n=$((n + 1))
            done
            """
        guard let cap = try? AgentSpawner.runSpawn(
                command: "\"$SHELL\" -i -c \(AgentSpawner.shq(inner))", terminal: term),
              !cap.0.isEmpty else {
            print("SKIP — live \(term.title) capture unavailable for the reap window")
            return
        }
        var pid: Int?
        for _ in 0..<50 where pid == nil {
            pid = (try? String(contentsOf: pidFile, encoding: .utf8))
                .flatMap { Int($0.trimmingCharacters(in: .whitespacesAndNewlines)) }
            if pid == nil { try? await Task.sleep(nanoseconds: 200_000_000) }
        }
        let tty = pid.flatMap(DeviceFocus.tty(forPid:)) ?? ""
        check("the reaped agent is on a terminal to be walked from", !tty.isEmpty)
        // The same walk `close` runs internally, for the one thing its Bool cannot say:
        // which tty the window it went for is on.
        let window = TerminalFocus.walk(tty: AgentProbes.shortTTY(tty), pid: pid,
                                        processes: TerminalFocus.processes(),
                                        panes: TerminalFocus.panes(),
                                        clients: TerminalFocus.clients()).ttys.last

        let closed = TerminalFocus.close(tty: tty, pid: pid)
        check("the window closes from the agent's own tty, with no handle at all", closed)
        // The window's tty and never the agent's: closing a wrapped window ends the tmux
        // client attached to it and leaves the session — and the agent — detached, which
        // is the whole difference between closing a window and killing what is inside it.
        var emptied = false
        for _ in 0..<15 where !emptied {
            emptied = window.map { t in
                !TerminalFocus.processes().values.contains { $0.tty == t }
            } ?? false
            if !emptied { try? await Task.sleep(nanoseconds: 200_000_000) }
        }
        check("…and the window is gone, not merely reported closed", emptied)
        // On `emptied` rather than on `closed`: a terminal that answered ok and left the
        // window standing has to be tidied up too.
        if !emptied { closeWindow(term: term, windowID: cap.0) }
    }

    /// Best-effort cleanup of a throwaway window: a window the user already closed makes
    /// this fail, which is not a test failure. Closed the way the reap closes one, so a
    /// window this cannot close is one the reap could not either.
    private static func closeWindow(term: SpawnTerminal, windowID: String) {
        OSAScript.runSilently(AgentWindows.closeScript(term: term, windowID: windowID))
    }
}
