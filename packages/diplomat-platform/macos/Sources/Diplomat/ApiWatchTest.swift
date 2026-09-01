import Foundation
import DiplomatCore

/// Headless self-test for who the API-error watcher may type into —
/// `DIPLOMAT_APIWATCH_TEST=1`.
///
/// The watcher reads every iTerm session and Terminal tab, and what it does with a
/// match is submit a line of input. In an agent's session that is a user turn; in a
/// plain shell it is a command, run by that shell. A shell reaches a matching tail for
/// entirely innocent reasons — a `cat` of a log holding a banner, a `git diff` of the
/// matcher's own tests — and nothing on the screen tells those from the CLI's own
/// line, so the process behind the tty is what decides.
///
/// Two halves, because the filter is wrong in either direction. It must keep the
/// watcher off a shell; it must also still reach an agent that a terminal shows
/// INDIRECTLY, which on a box whose shells wrap themselves in tmux is every agent —
/// the session the dump reports is the window's, and the agent sits ptys below it.
///
///     DIPLOMAT_APIWATCH_TEST=1 swift run Diplomat
///
/// Pure: every session, tail and process is a literal, so no terminal is read, nothing
/// is typed, and the answer does not depend on what else is running. Exit code is
/// pass/fail.
enum ApiWatchTest {
    /// A real Claude Code banner, the shape the matcher is written against.
    private static let banner = "⏺ API Error: 529 Overloaded.\n? for shortcuts"

    /// Every nudge line the run wrote to the scratch feed. A nudge that never landed
    /// must leave none: an audited nudge is one the operator can go and read on a
    /// screen.
    private static func nudges(_ feed: URL) -> [String] {
        let path = feed.appendingPathComponent("audit.jsonl").path
        guard let text = try? String(contentsOfFile: path, encoding: .utf8) else { return [] }
        return text.split(separator: "\n").filter { $0.contains("\"nudge\"") }.map(String.init)
    }

    @discardableResult
    @MainActor
    static func run() async -> Bool {
        var pass = true
        func check(_ name: String, _ ok: Bool) {
            print("\(ok ? "PASS" : "FAIL") — \(name)")
            if !ok { pass = false }
        }

        // Before any Store call: a nudge writes an audit line, and a self-test has no
        // business in the operator's activity feed.
        let feed = FileManager.default.temporaryDirectory
            .appendingPathComponent("diplomat-apiwatchtest-\(UUID().uuidString)")
        setenv("DIPLOMAT_AUDIT_DIR", feed.path, 1)
        defer { try? FileManager.default.removeItem(at: feed) }

        let store = Store()
        var sent: [String] = []
        // Five sessions, one scan's worth: an agent stalled on the banner, a plain shell
        // showing the SAME tail, an agent whose screen is still moving, an agent out of
        // quota, and an agent whose window has closed under the send. Only the first two
        // of those five are written to at all, and only the first is written to twice.
        func sessions(_ working: String) -> [ApiErrorWatcher.Session] {
            [ApiErrorWatcher.Session(tty: "/dev/ttys011", tail: banner),
             ApiErrorWatcher.Session(tty: "/dev/ttys099", tail: banner),
             ApiErrorWatcher.Session(tty: "/dev/ttys012", tail: banner + working),
             ApiErrorWatcher.Session(tty: "/dev/ttys013",
                                     tail: "You've hit your weekly limit."),
             ApiErrorWatcher.Session(tty: "/dev/ttys014", tail: banner)]
        }
        let agentTTYs: Set<String> = ["ttys011", "ttys012", "ttys013", "ttys014"]
        func scan(_ working: String, at now: Date) async {
            await store.apiErrorScanStep(sessions: sessions(working), agentTTYs: agentTTYs,
                                         now: now) { tty in
                sent.append(tty)
                return tty != "/dev/ttys014"  // no session owns it any more
            }
        }

        let t0 = Date()
        await scan(" retry 1", at: t0)
        check("no session is nudged on a first sighting", sent.isEmpty)

        await scan(" retry 2", at: t0.addingTimeInterval(20))
        check("a stalled agent is nudged once its screen has stopped moving",
              sent == ["/dev/ttys011", "/dev/ttys014"])
        check("…and the shell showing the very same tail is not",
              !sent.contains("/dev/ttys099"))
        check("…nor the agent still working, nor the one out of quota",
              !sent.contains("/dev/ttys012") && !sent.contains("/dev/ttys013"))
        let logged = nudges(feed)
        check("…and the feed carries the one nudge that landed, and only it",
              logged.count == 1 && logged[0].contains("ttys011"))

        await scan(" retry 3", at: t0.addingTimeInterval(40))
        check("a nudged agent is left alone inside its backoff window, and one whose "
              + "nudge never landed is tried again",
              sent == ["/dev/ttys011", "/dev/ttys014", "/dev/ttys014"])

        // What an empty agent-tty set COSTS, which is why the probe above must never
        // answer with one it did not mean. The step prunes backoff and idle-confirmation
        // state to the ttys it was handed, so a scan handed none forgets every session:
        // ttys011 is inside a 2m window here, and two scans later it is nudged again.
        await store.apiErrorScanStep(sessions: sessions(" retry 4"), agentTTYs: [],
                                     now: t0.addingTimeInterval(60)) { tty in
            sent.append(tty); return true
        }
        await scan(" retry 5", at: t0.addingTimeInterval(80))
        await scan(" retry 5", at: t0.addingTimeInterval(100))
        check("a scan handed no ttys at all forgets the backoff of every session in it",
              sent.filter { $0 == "/dev/ttys011" }.count == 2)

        // The same scans with only the empty one dropped. ttys011 stays inside the
        // backoff its t+20 nudge started, so the second nudge above is what forgetting
        // cost and not what this sequence would have done anyway.
        let kept = Store()
        var keptSent: [String] = []
        for at in [0.0, 20, 40, 80, 100] {
            await kept.apiErrorScanStep(sessions: sessions(" retry 5"), agentTTYs: agentTTYs,
                                        now: t0.addingTimeInterval(at)) { tty in
                keptSent.append(tty)
                return tty != "/dev/ttys014"
            }
        }
        check("…and with nothing forgotten it is nudged once, inside that same window",
              keptSent.filter { $0 == "/dev/ttys011" }.count == 1)

        // The other direction, over the tree a spawn leaves on a box whose ~/.zshrc execs
        // tmux: the window's session runs a wrapper, the wrapper runs a tmux client, and
        // the agent is in a pane the tmux SERVER owns — so the agent's own tty appears in
        // no terminal's dump, and its parents dead-end at a daemon. A filter comparing the
        // dump's tty against the agent's would nudge nobody at all.
        let procs: [Int: AgentState.ProcInfo] = [
            2000: AgentState.ProcInfo(tty: "ttys034", elapsed: 900, isAgent: false),
            2001: AgentState.ProcInfo(tty: "ttys036", elapsed: 900, isAgent: false),
            2002: AgentState.ProcInfo(tty: "ttys036", elapsed: 900, isAgent: false),
            2300: AgentState.ProcInfo(tty: "ttys037", elapsed: 600, isAgent: false),
            2301: AgentState.ProcInfo(tty: "ttys038", elapsed: 600, isAgent: true),
            2400: AgentState.ProcInfo(tty: "ttys099", elapsed: 900, isAgent: false),
        ]
        let processes: [Int: TerminalFocus.Proc] = [
            2000: TerminalFocus.Proc(ppid: 1, tty: "ttys034"),     // the window's login
            2001: TerminalFocus.Proc(ppid: 2000, tty: "ttys036"),  // a shell wrapper
            2002: TerminalFocus.Proc(ppid: 2001, tty: "ttys036"),  // the tmux client
            2200: TerminalFocus.Proc(ppid: 1, tty: ""),            // the tmux server
            2300: TerminalFocus.Proc(ppid: 2200, tty: "ttys037"),  // the pane's shell
            2301: TerminalFocus.Proc(ppid: 2300, tty: "ttys038"),  // the agent
            2400: TerminalFocus.Proc(ppid: 1, tty: "ttys099"),     // somebody's shell
        ]
        let reachable = AgentProbes.ttysRunningAnAgent(
            procs: procs, processes: processes,
            panes: ["ttys037": TerminalFocus.Pane(id: "%80", session: "1")],
            clients: ["1": "ttys036"])
        check("a wrapped agent is reached through the tty its window is dumped as",
              reachable.contains("ttys034"))
        check("…by the whole way out, and no further",
              reachable == ["ttys038", "ttys037", "ttys036", "ttys034"])

        // What the filter answers when it could not be computed. An empty set and an
        // unavailable reading are both "type into nobody", but only the second also says
        // "and forget nothing": `apiErrorScanStep` prunes backoff and idle-confirmation
        // state to the ttys it is handed, and `runApiErrorScanOnce` returns before that
        // on an unavailable reading. On a box where every agent is behind tmux, one
        // failed `list-clients` between two 20s scans would otherwise reset an escalated
        // 3h backoff to its 2m base.
        print("")
        let agentUp = Observation.present(procs)
        var walked = 0
        func tables() -> (panes: [String: TerminalFocus.Pane], clients: [String: String])? {
            walked += 1
            return (["ttys037": TerminalFocus.Pane(id: "%80", session: "1")], ["1": "ttys036"])
        }
        check("a tmux that would not answer is unavailable, not an empty set",
              AgentProbes.ttysRunningAnAgent(procs: agentUp, processes: { processes },
                                            tmux: { nil }).value == nil)
        check("…and one that does answer walks the agent out to its window",
              AgentProbes.ttysRunningAnAgent(procs: agentUp, processes: { processes },
                                            tmux: tables).value == reachable)
        check("an unreadable process table is unavailable, as it always was",
              AgentProbes.ttysRunningAnAgent(procs: .unavailable("exited 1"),
                                            processes: { processes },
                                            tmux: tables).value == nil)
        check("a box with no agent up is an empty set, and tmux is never asked",
              AgentProbes.ttysRunningAnAgent(
                  procs: .present([2400: AgentState.ProcInfo(tty: "ttys099", elapsed: 9,
                                                             isAgent: false)]),
                  processes: { processes }, tmux: tables).value == [] && walked == 1)

        // And that the nil above is a state a real tmux can actually be in. The listings
        // and the liveness probe are separate calls, so a stand-in that fails one and
        // answers the other is the transient hiccup itself, not a machine without tmux.
        let fake = FileManager.default.temporaryDirectory
            .appendingPathComponent("diplomat-faketmux-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: fake, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: fake) }
        let previousTmux = ProcessInfo.processInfo.environment["DIPLOMAT_TMUX"]
        defer {
            if let previousTmux { setenv("DIPLOMAT_TMUX", previousTmux, 1) }
            else { unsetenv("DIPLOMAT_TMUX") }
        }
        func fakeTmux(_ body: String) -> String {
            let path = fake.appendingPathComponent("tmux-\(UUID().uuidString)").path
            try? ("#!/bin/sh\ncase \"$1\" in\n\(body)\nesac\n")
                .write(toFile: path, atomically: true, encoding: .utf8)
            try? FileManager.default.setAttributes([.posixPermissions: 0o755],
                                                  ofItemAtPath: path)
            setenv("DIPLOMAT_TMUX", path, 1)
            return path
        }
        let u = "\u{1f}"
        _ = fakeTmux("has-session) exit 0 ;;\nlist-panes) printf 'ttys037\(u)%%80\(u)1\\n' ;;"
                     + "\nlist-clients) exit 1 ;;")
        check("a live server that will not list its clients reads as unwalkable",
              TerminalFocus.walkTables() == nil)
        _ = fakeTmux("has-session) exit 1 ;;\n*) exit 1 ;;")
        check("…and a machine whose server has shut down reads as nothing to hop across",
              TerminalFocus.walkTables()?.panes.isEmpty == true)
        _ = fakeTmux("has-session) exit 0 ;;\nlist-panes) printf 'ttys037\(u)%%80\(u)1\\n' ;;"
                     + "\nlist-clients) printf '1\(u)ttys036\\n' ;;")
        check("…and a server that answers both is read whole",
              TerminalFocus.walkTables()?.clients == ["1": "ttys036"])
        setenv("DIPLOMAT_TMUX", "/nonexistent-tmux", 1)
        check("a machine with no tmux at all is nothing to hop across, not a failure",
              TerminalFocus.walkTables()?.panes.isEmpty == true)

        // Which panes the dump adds on top of what the terminals reported. The same box
        // as above, plus a Ghostty run — a session on a client no scriptable app can be
        // asked about, which is the only reader that agent's screen has.
        let panes = ["ttys037": TerminalFocus.Pane(id: "%80", session: "1"),
                     "ttys050": TerminalFocus.Pane(id: "%91", session: "diplomat-a1b2")]
        let ghostly = processes.merging([
            2500: TerminalFocus.Proc(ppid: 2200, tty: "ttys050"),  // the Ghostty pane
            2600: TerminalFocus.Proc(ppid: 2599, tty: "ttys051"),  // the tmux client
            2599: TerminalFocus.Proc(ppid: 1, tty: ""),            // Ghostty itself
        ]) { a, _ in a }
        let attached = ["1": "ttys036", "diplomat-a1b2": "ttys051"]
        func captured(_ shownOn: Set<String>) -> [String] {
            TerminalFocus.panesToCapture(panes, processes: ghostly, attachedTo: attached,
                                         shownOn: shownOn).map(\.tty)
        }
        // ttys034 is the iTerm session's own tty — two ptys above the pane, because the
        // client sits under a shell wrapper. Matching the client's tty instead would
        // find nothing here and dump this screen a second time.
        check("a pane an iTerm window already showed is not dumped again",
              captured(["ttys034", "ttys099"]) == ["ttys050"])
        check("…and with no terminal answering at all, both are",
              captured([]) == ["ttys037", "ttys050"])
        // What that duplicate would cost: the watcher nudges per entry, so one stalled
        // agent would be typed into twice and audited twice.
        check("the Ghostty pane is never the one dropped",
              captured(["ttys034", "ttys051"]).contains("ttys050") == false
                && captured(["ttys034"]) == ["ttys050"])

        print(pass ? "APIWATCH TEST OK" : "APIWATCH TEST FAILED")
        return pass
    }
}
