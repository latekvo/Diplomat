import Foundation
import ArgentUtilsCore

/// Headless end-to-end self-test for the agent-session tracking path, driven by
/// `ARGENT_UTILS_TRACK_TEST=1`. It exercises the *real* code the applet uses —
/// `parseCapture`, the `ProcessMonitor` status/liveness/focus logic, the
/// persistence round-trip — and then a live capture → liveness → focus →
/// auto-complete cycle against a throwaway terminal window it cleans up itself.
///
/// Run it via the installed `.app` binary so the live portion inherits the granted
/// "control <terminal>" automation permission:
///   ARGENT_UTILS_TRACK_TEST=1 /Applications/ArgentUtils.app/Contents/MacOS/ArgentUtils
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

        // 1. Capture parsing (iTerm "wid|sid|tty" and Terminal's empty-field form).
        let c1 = AgentSpawner.parseCapture("37216|ABC-DEF|/dev/ttys016\n")
        check("parseCapture iTerm wid/sid/tty", c1 == ("37216", "ABC-DEF", "/dev/ttys016"))
        let c2 = AgentSpawner.parseCapture("44||\n")
        check("parseCapture Terminal empty sid", c2 == ("44", "", ""))

        // 2. Liveness + terminal-closed logic, driven by an injected open-window set so
        //    it's deterministic (no live terminal needed). A session is `done` when its
        //    sentinel exists OR its window is gone; it is *terminal-closed* (removable)
        //    only when its window is gone past the grace window and the app was
        //    actually queryable.
        let old = Date().addingTimeInterval(-60)
        let sentinel = NSTemporaryDirectory() + "argent-tracktest-\(UUID().uuidString)"
        try? "0".write(toFile: sentinel, atomically: true, encoding: .utf8)
        func proc(wid: String = "OPEN", term: String = "iterm", done: String = "",
                  at: Date) -> TrackedProcess {
            TrackedProcess(kind: "review", label: "x", terminal: term, windowID: wid,
                           sessionID: "", tty: "", donePath: done, prURL: nil, createdAt: at)
        }
        // iTerm reports one open window "OPEN"; Terminal is unqueryable (nil).
        let windows: (SpawnTerminal) -> Set<String>? = { $0 == .iterm ? ["OPEN"] : nil }
        let a = proc(wid: "OPEN", done: sentinel, at: old)  // sentinel + window open
        let b = proc(wid: "GONE", at: old)                  // window gone, past grace
        let c = proc(wid: "OPEN", at: old)                  // window open, running
        let d = proc(wid: "GONE", at: Date())               // window gone, within grace
        let e = proc(wid: "GONE", term: "terminal", at: old) // app unqueryable (nil)
        let sw = ProcessMonitor.sweep([a, b, c, d, e], openWindows: windows)
        var done: [UUID: Bool] = [:]
        for p in sw.refreshed { done[p.id] = p.done }
        check("sentinel + window open → done, not removed",
              done[a.id] == true && !sw.closedIDs.contains(a.id))
        check("window gone past grace → done + removed",
              done[b.id] == true && sw.closedIDs.contains(b.id))
        check("window open, no sentinel → running, not removed",
              done[c.id] == false && !sw.closedIDs.contains(c.id))
        check("window gone within grace → not yet removed", !sw.closedIDs.contains(d.id))
        check("terminal app unqueryable → never auto-removed (fail-safe)",
              !sw.closedIDs.contains(e.id))
        try? FileManager.default.removeItem(atPath: sentinel)

        // 2c. Activity classification through the real sweep: a live session's terminal
        //     buffer decides running vs awaiting-input, a done session never reads
        //     awaiting-input (done-gating wins over an idle buffer), and a session whose
        //     buffer we couldn't capture conservatively stays "running".
        let busyBuf = "✻ Reticulating…\n──\n❯\n──\n  ⏵⏵ bypass permissions on · esc to interrupt · ← for agents"
        let idleBuf = "✻ Cooked for 3s\n──\n❯ mark threads resolved\n──\n  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
        func liveProc(wid: String, tty: String) -> TrackedProcess {
            TrackedProcess(kind: "review", label: "x", terminal: "iterm", windowID: wid,
                           sessionID: "", tty: tty, donePath: "", prURL: nil, createdAt: old)
        }
        let pBusy = liveProc(wid: "OPEN", tty: "/dev/ttysA")     // alive, working
        let pIdle = liveProc(wid: "OPEN", tty: "/dev/ttysB")     // alive, at the prompt
        let pGone = liveProc(wid: "GONE", tty: "/dev/ttysB")     // window closed → done, idle buffer ignored
        let pNoTail = liveProc(wid: "OPEN", tty: "/dev/ttysZ")   // alive but buffer not captured
        let actTails = ["/dev/ttysA": busyBuf, "/dev/ttysB": idleBuf]
        let actSweep = ProcessMonitor.sweep([pBusy, pIdle, pGone, pNoTail],
                                            openWindows: windows, sessionTails: actTails)
        var awaiting: [UUID: Bool] = [:]
        for p in actSweep.refreshed { awaiting[p.id] = p.awaitingInput }
        check("busy buffer → running (awaitingInput false)", awaiting[pBusy.id] == false)
        check("idle buffer → awaiting input (awaitingInput true)", awaiting[pIdle.id] == true)
        check("done session never reads awaiting-input", awaiting[pGone.id] == false)
        check("no captured buffer → stays running (conservative)", awaiting[pNoTail.id] == false)

        // 2d. Live classification (informational): the REAL production dump + predicate
        //     against whatever terminals are open right now, for eyeballing.
        let liveSessions = (ApiErrorWatcher.dumpSessions() ?? [])
            .filter { $0.tail.lowercased().contains("bypass permissions") }
        let liveBusy = liveSessions.filter { AgentActivity.looksBusy($0.tail) }.count
        print("live: \(liveSessions.count) claude session(s) — \(liveBusy) running, "
            + "\(liveSessions.count - liveBusy) awaiting input")

        // 3. Persistence round-trip (the exact path Store uses for UserDefaults).
        let sample = proc(wid: "9", at: old)
        let roundTrip = (try? JSONEncoder().encode([sample]))
            .flatMap { try? JSONDecoder().decode([TrackedProcess].self, from: $0) }
        check("persistence round-trip preserves the record", roundTrip?.first == sample)

        // 4. Focus script embeds the captured ids (so it targets the right window).
        let fs = ProcessMonitor.focusScript(term: .iterm, windowID: "999", sessionID: "SID")
        check("focusScript embeds windowID + sessionID", fs.contains("999") && fs.contains("SID"))

        // 4b. prNumber parsing — the key the merged-status probe runs on.
        func pn(_ url: String?) -> Int? {
            TrackedProcess(kind: "review", label: "x", terminal: "iterm", windowID: "1",
                           sessionID: "", tty: "", donePath: "", prURL: url).prNumber
        }
        check("prNumber parses …/pull/<n>",
              pn("https://github.com/software-mansion/argent/pull/337") == 337)
        check("prNumber parses …/pull/<n>/files",
              pn("https://github.com/software-mansion/argent/pull/42/files") == 42)
        check("prNumber nil when no PR url", pn(nil) == nil)

        // 4c. Snapshot parse computes "threads I owe" (the offline-review reconcile signal):
        //     unresolved + I-can-resolve + last comment isn't mine. Threads I already
        //     replied to (last comment == me), resolved threads, and ones I can't resolve
        //     are excluded — so we don't auto-fix a PR whose ball is with the reviewer.
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

        // 5. Live cycle against a real, self-closing terminal window.
        await liveCycle(check: check)

        print(pass ? "\nTRACK_TEST OK" : "\nTRACK_TEST FAILED")
        return pass
    }

    private static func liveCycle(check: (String, Bool) -> Void) async {
        let term = AgentSpawner.resolved(.iterm)
        let done = AgentSpawner.doneFilePath()
        // A benign stand-in for the claude command: announce, wait briefly, write the
        // same completion sentinel a real run would.
        let cmd = "echo 'argent tracking self-test — this window closes itself'; "
            + "sleep 8; printf %s $? > '\(done)'"
        guard let cap = try? AgentSpawner.runSpawn(command: cmd, terminal: term),
              !cap.0.isEmpty, !cap.2.isEmpty else {
            print("SKIP — live \(term.title) capture unavailable (automation not granted?)")
            return
        }
        let (wid, sid, tty) = cap
        let p = TrackedProcess(kind: "review", label: "self-test", terminal: term.rawValue,
                               windowID: wid, sessionID: sid, tty: tty, donePath: done, prURL: nil)
        check("live capture returns wid + tty", !wid.isEmpty && !tty.isEmpty)

        try? await Task.sleep(nanoseconds: 1_500_000_000)  // let the shell appear in ps
        check("live window alive (ps sees its tty)", ProcessMonitor.isWindowAlive(p))
        check("live focus succeeds", ProcessMonitor.focus(p))

        let bogus = TrackedProcess(kind: "review", label: "bogus", terminal: term.rawValue,
                                   windowID: "99999999", sessionID: "nope",
                                   tty: "/dev/ttysZZZ", donePath: "", prURL: nil)
        check("focus of a vanished window fails (→ fallback)", !ProcessMonitor.focus(bogus))

        var completed = false
        for _ in 0..<25 {
            if ProcessMonitor.refreshed([p]).first?.done == true { completed = true; break }
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
        check("live session auto-completes (sentinel lands)", completed)

        closeWindow(term: term, windowID: wid)   // tidy up the throwaway window
        // Once the window closes, its tty leaves `ps`; the sweep should classify the
        // session as terminal-closed (→ auto-removed from the list) within a poll or two.
        var removed = false
        for _ in 0..<10 {
            if ProcessMonitor.sweep([p]).closedIDs.contains(p.id) { removed = true; break }
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
        check("closed window → session flagged for auto-removal", removed)
        try? FileManager.default.removeItem(atPath: done)
    }

    private static func closeWindow(term: SpawnTerminal, windowID: String) {
        let app = term.appName
        let script = """
        tell application "\(app)"
            repeat with w in windows
                if (id of w as string) is "\(windowID)" then close w
            end repeat
        end tell
        """
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        proc.arguments = ["-e", script]
        proc.standardOutput = Pipe()
        proc.standardError = Pipe()
        try? proc.run()
        proc.waitUntilExit()
    }
}
