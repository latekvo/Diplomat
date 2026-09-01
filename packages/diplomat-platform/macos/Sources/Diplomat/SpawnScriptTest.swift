import Foundation

/// Self-test for everything about a spawn that can be decided without one —
/// `DIPLOMAT_SPAWN_SCRIPT_TEST=1`.
///
/// `DIPLOMAT_SPAWN_FOCUS_TEST` and `DIPLOMAT_TRACK_TEST` prove the same contracts against
/// real windows, but both need a GUI session and Automation consent, so CI cannot host
/// them. This reads the generated AppleScript instead, which needs neither and still pins
/// the properties that would silently take a terminal out of service: which window a
/// restore names, how each app's window id is addressed, and which terminal a spawn
/// resolves to.
///
///     DIPLOMAT_SPAWN_SCRIPT_TEST=1 swift run Diplomat
///
/// Opens no window and runs no AppleScript. Exit code is pass/fail, so CI can gate on it.
/// Run it a second time with `DIPLOMAT_TMUX` pointed at nothing to take the other arm of
/// the terminal ladder — the machine without tmux, where Ghostty must not be chosen.
enum SpawnScriptTest {
    static func run() -> Bool {
        var failures: [String] = []
        func check(_ name: String, _ cond: Bool, _ detail: String = "") {
            if cond { print("  ok    \(name)") }
            else { print("  FAIL  \(name) \(detail)"); failures.append(name) }
        }

        func script(_ term: SpawnTerminal, restore: String?) -> String {
            AgentSpawner.appleScript(for: term, shellCommand: "echo hi", restoreFocusTo: restore)
        }

        print("spawn script: who the restore names")

        for term in SpawnTerminal.allCases {
            let capture = term == .iterm
                ? "set _prev to id of current window"
                : "set _prev to id of front window"
            let reselect: String
            let creates: String
            switch term {
            case .ghostty:
                reselect = "activate window (first window whose id is _prev)"
                creates = "new window with configuration"
            case .iterm:
                reselect = "select (first window whose id is _prev)"
                creates = "create window with default profile"
            case .terminal:
                reselect = "set frontmost of _pw to true"
                creates = "do script \"\""
            }

            // Foreground: the operator asked to land in the new window, so nothing is
            // restored and no window is named.
            let fg = script(term, restore: nil)
            check("\(term.title) foreground activates the terminal", fg.contains("activate"))
            check("\(term.title) foreground restores nothing", !fg.contains("_prev"))

            // Another app: activating it is the whole restore — it owns no window here.
            let cross = script(term, restore: "com.apple.finder")
            check("\(term.title) cross-app activates the target app",
                  cross.contains("tell application id \"com.apple.finder\" to activate"))
            check("\(term.title) cross-app names no window", !cross.contains("_prev"))

            // The terminal itself: activating it lands on the window the spawn just
            // made, so the operator's window has to be named to come back.
            let same = script(term, restore: term.bundleID)
            check("\(term.title) same-app captures the front window", same.contains(capture))
            check("\(term.title) same-app re-selects it", same.contains(reselect))
            // Captured BEFORE the spawn's own window exists — after it, the id is the
            // agent's window and the restore is a no-op that still reads like one.
            let capturedAt = same.range(of: capture)?.lowerBound
            let createdAt = same.range(of: creates)?.lowerBound
            check("\(term.title) same-app captures before it creates",
                  capturedAt != nil && createdAt != nil && capturedAt! < createdAt!)

        }

        // The window a run is raised and reaped by. Ghostty's id is an opaque string
        // ("tab-group-6000023ec120"), so it is addressed as one; iTerm's and Terminal's
        // are numbers and `window id 999` is how those two take it. Getting this wrong
        // is an AppleScript syntax error at reap time, on a run nobody is watching.
        print("\nwindow scripts: how each app's id is addressed")
        let ghostFocus = AgentWindows.focusScript(term: .ghostty, windowID: "tab-group-1",
                                                  sessionID: "diplomat-abc")
        check("Ghostty focus quotes the id and raises by it",
              ghostFocus.contains("first window whose id is \"tab-group-1\"")
                && ghostFocus.contains("activate window"))
        let ghostClose = AgentWindows.closeScript(term: .ghostty, windowID: "tab-group-1")
        // `close` closes a SURFACE in Ghostty's dictionary and refuses a window found by
        // walking `windows` (-1708); `close window` on a resolved specifier is the verb.
        check("Ghostty close uses the window verb on a resolved specifier",
              ghostClose.contains("close window (first window whose id is \"tab-group-1\")")
                && !ghostClose.contains("repeat with w in windows"))
        // The specifier errors outright when nothing matches, where walking simply
        // matches nothing — so this one needs the `try` the other two do not.
        check("…and a window already gone is not a failed reap", ghostClose.contains("try"))
        for term in [SpawnTerminal.iterm, .terminal] {
            check("\(term.title) addresses its numeric window id bare",
                  AgentWindows.focusScript(term: term, windowID: "999", sessionID: "S")
                    .contains("window id 999"))
        }

        // Ghostty raises a new window asynchronously and finishes after the script that
        // made it has moved on, so a restore issued straight away is overtaken and the
        // background spawn ends up frontmost — the focus theft the restore exists to
        // prevent. iTerm and Terminal are the other way round on purpose (their window is
        // up by the time the call returns, and their restore goes BEFORE the five-second
        // input settle so focus is gone for a blink rather than for the whole of it), so
        // this is Ghostty's alone.
        let ghostBG = AgentSpawner.appleScript(for: .ghostty, shellCommand: "echo hi",
                                               restoreFocusTo: "com.apple.finder")
        let waitsAt = ghostBG.range(of: "delay ")?.lowerBound
        let restoresAt = ghostBG.range(of: "tell application id")?.lowerBound
        check("a background Ghostty spawn waits for its window before handing focus back",
              waitsAt != nil && restoresAt != nil && waitsAt! < restoresAt!)
        check("…and a foreground one waits for nothing, having been asked to land there",
              !AgentSpawner.appleScript(for: .ghostty, shellCommand: "echo hi")
                  .contains("delay "))

        // What a Ghostty window is actually told to run. Every layer here is load-bearing:
        // tmux is the only reader its agents have, the name is what finds and ends the
        // run, and the launcher file is what the prompt's quoting survives.
        print("\nghostty command: tmux, a named session, a staged file")
        let launcher = "/var/folders/t/diplomat-launch-1.sh"
        let cmd = AgentSpawner.ghosttyCommand(session: "diplomat-abc", launcher: launcher,
                                              tmux: "/opt/homebrew/bin/tmux")
        check("it opens a tmux session by name on the staged file",
              cmd.contains("new-session -s 'diplomat-abc'") && cmd.contains("'\(launcher)'"))
        // Not "$SHELL": the staged file invokes an interactive login shell itself, and on
        // a box whose zshrc execs tmux, one outside that one nests a second server.
        check("…under /bin/sh, not the login shell",
              cmd.contains("/bin/sh") && !cmd.contains("$SHELL"))
        check("the session name is one a reap can recognise as ours",
              AgentSpawner.ghosttySession().hasPrefix(TerminalFocus.sessionPrefix))

        // Which terminal a spawn lands on. Ghostty leads because its window is created
        // WITH its command and so cannot drop one — but only where tmux can read the
        // screen, since a run nothing can watch holds its bay until a human closes it.
        print("\nterminal ladder: tmux is what makes Ghostty offerable")
        check("Ghostty leads the fallback order",
              SpawnTerminal.allCases.map(\.rawValue) == ["ghostty", "iterm", "terminal"])
        check("only Ghostty can ever be installed-but-undriveable",
              SpawnTerminal.iterm.unavailableReason == nil
                && SpawnTerminal.terminal.unavailableReason == nil)
        if TerminalFocus.tmuxAvailable {
            check("with tmux, nothing holds Ghostty back",
                  SpawnTerminal.ghostty.unavailableReason == nil
                    && SpawnTerminal.ghostty.isUsable == SpawnTerminal.ghostty.isInstalled)
        } else {
            check("without tmux, Ghostty gives a reason instead of a window",
                  SpawnTerminal.ghostty.unavailableReason != nil
                    && !SpawnTerminal.ghostty.isUsable)
            check("…and a spawn that asked for it resolves past it",
                  AgentSpawner.resolved(.ghostty) != .ghostty)
        }
        check("an unusable preference never resolves to itself",
              SpawnTerminal.allCases.allSatisfy {
                  $0.isUsable || AgentSpawner.resolved($0) != $0
              })

        // The exit-code sentinel, run for real against a directory that is not there.
        // Retirement deletes a run's directory while its agent sits at its prompt, so
        // this write lands on a gone path every time the operator finally exits — and
        // this string is typed into their own login shell, which on a Mac is zsh.
        print("\nexit sentinel: best-effort against a deleted run directory")
        for shell in ["/bin/sh", "/bin/zsh", "/bin/bash"] {
            guard FileManager.default.isExecutableFile(atPath: shell) else { continue }
            let p = Process()
            p.executableURL = URL(fileURLWithPath: shell)
            p.arguments = ["-c", "false; " + AgentSpawner.sentinel("/nonexistent/gone/done")]
            let err = Pipe()
            p.standardError = err
            p.standardOutput = Pipe()
            try? p.run()
            let noise = err.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            check("\(shell) says nothing and exits 0 when the directory is gone",
                  noise.isEmpty && p.terminationStatus == 0,
                  String(data: noise, encoding: .utf8) ?? "")
        }
        // The other half: guarding the write must not stop it happening. `false` stands
        // in for an agent that exited 1, and 1 is what the ledger prices from.
        let landing = FileManager.default.temporaryDirectory
            .appendingPathComponent("diplomat-sentinel-\(UUID().uuidString)")
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/zsh")
        p.arguments = ["-c", "false; " + AgentSpawner.sentinel(landing.path)]
        try? p.run()
        p.waitUntilExit()
        check("…and still records the agent's own exit code where it can",
              (try? String(contentsOf: landing, encoding: .utf8)) == "1")
        try? FileManager.default.removeItem(at: landing)

        // The one-time move of an existing install onto Ghostty. iTerm is what every
        // install reads today, whether that was a decision or a default nobody touched;
        // Terminal.app was picked over an installed iTerm, which is a decision.
        print("\nterminal migration: which stored choices move")
        check("an install on iTerm moves",
              Store.terminalChoiceMigration(stored: "iterm", ghosttyUsable: true) == "ghostty")
        check("a fresh install moves",
              Store.terminalChoiceMigration(stored: nil, ghosttyUsable: true) == "ghostty")
        check("a deliberate Terminal.app stays",
              Store.terminalChoiceMigration(stored: "terminal", ghosttyUsable: true) == nil)
        check("a box that cannot drive Ghostty stays",
              Store.terminalChoiceMigration(stored: "iterm", ghosttyUsable: false) == nil)

        print(failures.isEmpty
              ? "\nSPAWN_SCRIPT_TEST OK"
              : "\nSPAWN_SCRIPT_TEST FAILED  [\(failures.joined(separator: ", "))]")
        return failures.isEmpty
    }
}
