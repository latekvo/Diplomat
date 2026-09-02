import SwiftUI
import AppKit
import DiplomatCore

// The review-depth model and ReviewConfig prompt builder now live in
// DiplomatCore (driven by assets/review.json) and are shared verbatim with the
// Linux front-end. This file keeps only the macOS-specific bits: the terminal
// chooser, the AppleScript/iTerm spawner, and the SwiftUI wizard view.

// MARK: - Terminal choice

/// Which terminal SPAWN AGENT drives. Declaration order is the preference order
/// `resolved` falls back through: Ghostty, then iTerm, then Terminal.app, which is
/// always present and so ends every fallback chain.
///
/// Ghostty is first because its spawn is the one that cannot drop a prompt. iTerm and
/// Terminal are driven by TYPING the command into a window that already exists, which
/// is why `inputSettleDelay` is there at all; Ghostty takes the command as part of
/// creating the window, so there is no window-without-a-command moment to race.
enum SpawnTerminal: String, CaseIterable, Identifiable {
    case ghostty, iterm, terminal
    var id: String { rawValue }

    var title: String {
        switch self {
        case .ghostty: return "Ghostty"
        case .iterm: return "iTerm"
        case .terminal: return "Terminal"
        }
    }

    var bundleID: String {
        switch self {
        case .ghostty: return "com.mitchellh.ghostty"
        case .iterm: return "com.googlecode.iterm2"
        case .terminal: return "com.apple.Terminal"
        }
    }

    /// The name AppleScript addresses the app by.
    var appName: String {
        switch self {
        case .ghostty: return "Ghostty"
        case .iterm: return "iTerm"
        case .terminal: return "Terminal"
        }
    }

    var isInstalled: Bool {
        NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) != nil
    }

    /// Why an INSTALLED terminal still cannot be driven, or nil when it can.
    ///
    /// Only Ghostty has one. Its AppleScript dictionary can create a window, type into
    /// a terminal and close one, but a `terminal` exposes no visible text — there is no
    /// equivalent of iTerm's `contents of session`. Reading an agent's screen is not a
    /// nicety: it is the stillness backstop's only input, the API-error watcher's only
    /// input, and the fallback that separates a working agent from one back at its
    /// prompt. A run whose screen cannot be read resolves `.running` on every tick
    /// (`AgentState.classifyActivity`), so it holds its bay until a human closes the
    /// window — and a machine whose every bay is held that way dispatches nothing.
    ///
    /// tmux is what supplies it (`capture-pane`), exactly as it does for the whole
    /// Linux front-end, which has no scriptable terminal at all. So a Ghostty spawn
    /// runs its agent inside a tmux session, and without tmux Ghostty is not offered:
    /// falling back to a terminal that can be watched beats a fleet that cannot be.
    ///
    /// Short enough to sit inside a segmented picker's own segment; the Settings row
    /// carries the fix.
    var unavailableReason: String? {
        guard self == .ghostty, !TerminalFocus.tmuxAvailable else { return nil }
        return "needs tmux"
    }

    /// Installed AND driveable. What every resolution and every enablement asks.
    var isUsable: Bool { isInstalled && unavailableReason == nil }
}

// MARK: - Spawning a detached claude session in a terminal

/// Opens a brand-new terminal window running `claude "<prompt>"`, fully detached
/// from this applet. The prompt is written to a temp file and read back with
/// `$(cat …)` so we never have to wrestle a multi-line prompt through nested
/// shell + AppleScript quoting.
enum AgentSpawner {
    /// The local checkout the agent works in — Settings → REPO ROOT (see
    /// `RepoPaths.agentRepo` for the full resolution). Read per spawn, so changing it
    /// takes effect on the next agent without a restart. The `cd` is best-effort
    /// (`;`, not `&&`) so `claude` still starts if the path is wrong.
    static var repoPath: String { RepoPaths.agentRepo }

    /// Seconds between OPENING the terminal window and TYPING the command into it.
    /// A freshly created window's shell is still initializing, and input written
    /// immediately is silently discarded about half the time (zsh resets its line
    /// editor during startup) — the agent then never launches. The WHOLE command
    /// waits, not just its trailing newline. Every spawn call site is detached, so
    /// the in-script `delay` never blocks the UI.
    ///
    /// iTerm and Terminal only. A Ghostty window is created WITH its command, so there
    /// is no window sitting empty to race and nothing to wait for.
    static let inputSettleDelay = 5

    /// Seconds a BACKGROUND Ghostty spawn waits after creating its window before handing
    /// focus back.
    ///
    /// Ghostty finishes raising a new window after the AppleScript that made it has moved
    /// on, so an `activate` issued straight away is overtaken and the spawn ends up
    /// frontmost — the exact focus theft the restore exists to prevent. Measured on 1.3.1:
    /// at no delay Ghostty wins every time, at 0.3s the restore holds. Three times that,
    /// and still well under the `inputSettleDelay` the other two spend.
    ///
    /// The foreground spawn waits for nothing: landing in the new window is what it was
    /// asked for.
    static let ghosttyRaiseDelay = 1.0

    /// How long a Ghostty spawn waits for its tmux pane to appear before giving up on
    /// reading the tty off it. Generous against a cold `tmux` server; a spawn that
    /// overruns it loses only the head start, since the pid file lands a moment later
    /// and the tty is re-derived from it on the tick after.
    static let ghosttyPaneTimeout: TimeInterval = 5

    /// Resolve the terminal to actually drive: the preferred one if it can be driven,
    /// else the first alternative that can, else Terminal.app (always present).
    ///
    /// Driveable, not merely installed: a Ghostty on a machine without tmux resolves
    /// PAST itself, rather than to a spawn whose agents could never be watched.
    static func resolved(_ preferred: SpawnTerminal) -> SpawnTerminal {
        if preferred.isUsable { return preferred }
        return SpawnTerminal.allCases.first(where: { $0.isUsable }) ?? .terminal
    }

    /// Proactively provoke the macOS "control <terminal>" automation prompt so the
    /// user grants it up front instead of on first SPAWN. No-op once granted; runs
    /// fire-and-forget so a pending prompt never blocks startup.
    static func triggerAutomationPrompt(preferred: SpawnTerminal) {
        let term = resolved(preferred)
        // Don't wait — the prompt itself is the point, and it is modal.
        OSAScript.fireAndForget("tell application \"\(term.appName)\" to get version")
    }

    enum SpawnError: LocalizedError {
        case write(String)
        case osascript(code: Int32, stderr: String)

        var errorDescription: String? {
            switch self {
            case .write(let m): return "Couldn't stage prompt: \(m)"
            case .osascript(let code, let stderr):
                let s = stderr.trimmingCharacters(in: .whitespacesAndNewlines)
                return "osascript exited \(code): \(s.isEmpty ? "(no stderr)" : s)"
            }
        }
    }

    /// What a run's directory staged for its agent: the four paths the spawn is built
    /// out of, and which CLI is to be run in it.
    ///
    /// Everything here comes from `AgentRegistry`, which the run is registered with
    /// BEFORE the spawn — a terminal takes seconds to open, and a run booked only on
    /// success is a PR that reads free while its agent is starting.
    struct SpawnPlan {
        let promptFile: URL
        let donePath: String
        let pidPath: String
        /// Which agent CLI this run is spawned as. Resolved once by the caller and
        /// carried here rather than re-read later: the setting is what the NEXT spawn
        /// will use, so a run started under one runner and asked about after the
        /// operator switched would be interrogated through the wrong store.
        let runner: AgentRunner
        /// Where this run's OpenCode server answers, or 0 for a run that has none —
        /// every Claude Code and Hermes run, and any OpenCode run no port could be
        /// reserved for.
        let port: Int
        /// Where Claude Code finds the hooks it reports its own turn boundaries
        /// through (`AgentCompletion`), or nil for a run spawned without them. That
        /// report is the only evidence that separates a finished agent from a working
        /// one — both are the same live process at the same pid.
        var settingsPath: String? = nil
    }

    /// What a spawn produced: the handle the applet keeps so it can raise the window
    /// again, and the tty its agent runs on.
    struct SpawnResult {
        let terminal: SpawnTerminal
        let window: AgentWindows.Handle
        /// The controlling tty as `ps` spells it, which is how a run's screen is found.
        /// Known here a moment before the pid file is, so a run has a screen from its
        /// first tick rather than from whichever one adopts its pid.
        let tty: String
    }

    /// Open the terminal and run the agent on the staged prompt. The AppleScript reports
    /// the new window/session/tty back on stdout, which we capture so the spawned session
    /// can be raised again afterwards.
    ///
    /// `restoreFocusTo` (a bundle id) makes the spawn NON-focus-stealing: the window
    /// opens without activating the terminal, and focus bounces straight back to that
    /// app. The auto-fix monitor passes the frontmost app so its background spawns
    /// don't yank the user off whatever they're doing; a user-driven SPAWN passes nil
    /// so the terminal comes forward as before.
    @discardableResult
    static func spawn(_ plan: SpawnPlan, terminal preferred: SpawnTerminal,
                      restoreFocusTo restoreBID: String? = nil) throws -> SpawnResult {
        let term = resolved(preferred)
        let (wid, sid, tty) = try runSpawn(command: shellCommand(plan), terminal: term,
                                           restoreFocusTo: restoreBID)
        return SpawnResult(terminal: term,
                           window: AgentWindows.Handle(terminal: term.rawValue,
                                                       windowID: wid, sessionID: sid),
                           tty: AgentProbes.shortTTY(tty))
    }

    /// Open a new terminal window running `command`, returning the captured
    /// (windowID, sessionID, tty). The execution path shared by the real spawn and
    /// the tracking self-test (`DIPLOMAT_TRACK_TEST`). `restoreFocusTo` bounces
    /// focus back to that bundle id (background spawns) instead of activating the
    /// terminal.
    static func runSpawn(command: String, terminal term: SpawnTerminal,
                         restoreFocusTo restoreBID: String? = nil) throws -> (String, String, String) {
        if term == .ghostty { return try runGhosttySpawn(command: command, restoreFocusTo: restoreBID) }
        let captured = try runOsascriptCapturing(
            appleScript(for: term, shellCommand: command, restoreFocusTo: restoreBID))
        return parseCapture(captured)
    }

    /// The Ghostty spawn, which is a different shape from the other two rather than a
    /// different script: the window is created WITH its command, and the two things the
    /// other terminals hand back with the window id are not Ghostty's to give.
    ///
    /// The command is a tmux session on a staged launcher file, for two reasons that
    /// arrive together. tmux because Ghostty cannot be asked what a terminal is showing
    /// or which tty it is on, and `capture-pane` is the only reader an agent's screen
    /// then has; a file because `command:` is one AppleScript string holding a shell
    /// word-split command holding `"$SHELL" -i -c '…'` holding `$(cat …)`, and the
    /// prompt's own quoting does not survive that many layers. Staging the shell command
    /// verbatim in a file collapses every layer but the first.
    ///
    /// So the session name stands in for the session id, and the tty is read back off
    /// tmux instead of off the window — which makes it the AGENT'S tty, one better than
    /// what iTerm reports, since a Ghostty spawn puts nothing between the two.
    private static func runGhosttySpawn(command: String, restoreFocusTo restoreBID: String?)
            throws -> (String, String, String) {
        let session = ghosttySession()
        let launcher = try writeLauncher(ghosttyLauncher(command: command, session: session))
        let captured = try runOsascriptCapturing(
            appleScript(for: .ghostty,
                        shellCommand: ghosttyCommand(session: session, launcher: launcher.path),
                        restoreFocusTo: restoreBID))
        return (parseCapture(captured).0, session, ghosttyPaneTTY(session: session))
    }

    /// A tmux session name for one run: prefixed so a reap can only ever match a session
    /// this applet opened, and short enough to read in a window title.
    static func ghosttySession() -> String {
        TerminalFocus.sessionPrefix + UUID().uuidString.prefix(8).lowercased()
    }

    /// The window command a Ghostty spawn runs. `-s` names the session so the run can be
    /// found again by name — for its tty on the way in, to end it on the way out, and, by
    /// way of `ghosttyLauncher`, to raise its window at any point in between.
    ///
    /// `/bin/sh` rather than the operator's login shell: the staged file is the command
    /// `shellCommand` built, which invokes `"$SHELL" -i -c` itself. Running it under the
    /// login shell too would put an interactive zsh outside that one — and on a box whose
    /// zshrc execs tmux, an interactive zsh inside tmux nests a second server.
    static func ghosttyCommand(session: String, launcher: String,
                               tmux: String = TerminalFocus.binary ?? "tmux") -> String {
        "\(shq(tmux)) new-session -s \(shq(session)) /bin/sh \(shq(launcher))"
    }

    /// What the staged file runs: the agent's command, under the two tmux options that
    /// write the session's name into the window's title.
    ///
    /// That title is the only thing tying a Ghostty window to the agent inside it.
    /// Ghostty's dictionary exposes no tty, so nothing can ask which window a process is
    /// on, and the handle a spawn keeps is deleted along with the run directory the
    /// moment the run retires — which happens while agents are still working. The session
    /// name outlives it: `TerminalFocus.walk` reads the name back off the pane every time
    /// the agent is found again, so a window carrying it can be raised exactly for as long
    /// as the agent is alive (`TerminalFocus.ghosttyRaiseScript`).
    ///
    /// Set from inside the session, so the options need no target and touch no other
    /// session on the operator's server. `set-titles-string` is pinned to the name rather
    /// than left at its default, which interpolates the pane's own title: the agent
    /// rewrites that as it works, and a title that moves is not one to match on.
    static func ghosttyLauncher(command: String, session: String,
                                tmux: String = TerminalFocus.binary ?? "tmux") -> String {
        """
        \(shq(tmux)) set-option set-titles on
        \(shq(tmux)) set-option set-titles-string \(shq(session))
        \(command)
        """
    }

    /// Stage the shell command as a file for a Ghostty window to run.
    private static func writeLauncher(_ command: String) throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("diplomat-launch-\(UUID().uuidString).sh")
        do { try command.write(to: url, atomically: true, encoding: .utf8) }
        catch { throw SpawnError.write(error.localizedDescription) }
        return url
    }

    /// The tty of the pane the spawn just opened, polled because tmux takes a moment to
    /// come up inside the new window. "" when it never appears, which costs the run the
    /// head start and nothing else — the pid file lands next, and the tty comes off that.
    private static func ghosttyPaneTTY(session: String) -> String {
        let deadline = Date().addingTimeInterval(ghosttyPaneTimeout)
        repeat {
            if let tty = TerminalFocus.panes().first(where: { $0.value.session == session })?.key {
                return tty
            }
            usleep(100_000)
        } while Date() < deadline
        return ""
    }

    /// Open a terminal window on one command a *human* is meant to drive — the runner's
    /// provider-login wizard. Unlike a spawn there is no run to register and no handle
    /// worth keeping, so the captured ids are dropped and a failure to open is not
    /// raised into the Settings sheet: the visible outcome either way is a window that
    /// did or did not appear.
    static func openTerminal(command: String, terminal preferred: SpawnTerminal) {
        _ = try? runSpawn(command: command, terminal: resolved(preferred))
    }

    static func writePrompt(_ prompt: String) throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("diplomat-review-\(UUID().uuidString).txt")
        do { try prompt.write(to: url, atomically: true, encoding: .utf8) }
        catch { throw SpawnError.write(error.localizedDescription) }
        return url
    }

    /// `cd '<repo>' 2>/dev/null; "$SHELL" -i -c 'printf %s $$ > <pid>; <agent>'; { printf %s $? > '<done>'; } 2>/dev/null || :`
    ///
    /// `<agent>` is `AgentRunner.agentCommand` — `claude "$(cat '<promptfile>')"` or the
    /// OpenCode spelling of the same thing. Everything around it is identical for both,
    /// because everything around it is what a run is *identified* by.
    ///
    /// The agent runs one shell deeper, and what that shell records is its own `$$`.
    /// `AgentState` identifies the run by it, in place of matching
    /// `PR #<n> in <owner>/<repo>` against prompt text in `ps` output — which could not
    /// tell two runs on one PR apart, matched any unrelated session that mentioned the
    /// number, and matched the wrapper shell as readily as the agent.
    ///
    /// It is the AGENT'S OWN pid wherever the shell elides the fork for the last command
    /// of a `-c` string: the shell execs the agent over itself, replacing the process
    /// image without changing the pid. Every shell in current use does — measured on
    /// macOS 15.5, `$SHELL -i -c 'printf %s $$ > p; sleep 4'` records `sleep` under zsh
    /// 5.9 and under bash 5.3. The exception is bash `3.2`, the last GPLv2 release and
    /// what macOS still ships as `/bin/bash`, which forks; `$SHELL` is the operator's own
    /// and unconstrained here, so a login shell set to that one records the wrapper.
    /// Nothing downstream minds: it shares the agent's controlling terminal and start
    /// instant, its argv carries the runner word (`AgentRunner.isAgentLine`), and it
    /// exits when the agent does — every input `AgentState.resolveLocal` reads. What it
    /// is not, there, is a handle on the agent *process* — its argv, a signal sent to
    /// it — so nothing should be built on that.
    ///
    /// The agent must stay the LAST command inside those quotes, and that exec must stay
    /// the shell's own rather than the written-out `exec` keyword. Spelling it out would
    /// settle bash 3.2 too, and costs more than it settles: alias expansion applies to
    /// the first word of a simple command, so under an explicit `exec claude` the word
    /// checked is `exec`, the user's `claude` alias never expands, and the agent loses
    /// the `--dangerously-skip-permissions` that alias carries. The inner shell is
    /// interactive for the same reason — aliases do not survive into a non-interactive
    /// child. `$?` after it is the agent's own exit code either way: one process where
    /// the exec happened, and the wrapper's own status where it did not.
    ///
    /// The trailing `sentinel` writes the agent's exit code the moment it returns, so
    /// the applet can price the run even while its window stays open. It only ever
    /// fires on EXIT, though, and finishing a turn is not exiting — which is what the
    /// hooks in `plan.settingsPath` answer.
    static func shellCommand(_ plan: SpawnPlan) -> String {
        let agent = plan.runner.agentCommand(promptFile: plan.promptFile.path,
                                             model: AppConfig.agentModel, port: plan.port,
                                             settingsFile: plan.settingsPath)
        let inner = "printf %s $$ > \(shq(plan.pidPath)); \(agent)"
        return "cd \(shq(repoPath)) 2>/dev/null; \"$SHELL\" -i -c \(shq(inner)); "
            + sentinel(plan.donePath)
    }

    /// The exit-code sentinel write, best-effort.
    ///
    /// `donePath` is inside the run directory, retirement deletes that directory, and a
    /// run is retired while its agent goes on sitting at its prompt — so by the time the
    /// operator exits the session there is nowhere to write. Nothing reads it then (the
    /// run is long retired); an unguarded write only puts a shell diagnostic on the last
    /// screen of a window somebody is closing. `AgentCompletion` guards its hook writes
    /// for the same reason.
    ///
    /// A REDIRECTED GROUP rather than the `2>/dev/null` ahead of the redirect that the
    /// hooks use, because the two strings are run by different shells. A hook runs under
    /// `sh`, where a leading `2>/dev/null` is in effect before the failing open. This one
    /// is typed into the operator's own login shell, and zsh reports a redirection error
    /// on the shell's OWN stderr, which no redirection of that command can reach:
    /// measured on macOS 15.5, `zsh -c 'printf %s $? 2>/dev/null > /nope/done'` still
    /// prints `zsh:1: no such file or directory`. Wrapping the redirect silences sh,
    /// bash, dash and zsh alike, and `|| :` keeps the failure out of `$?`.
    static func sentinel(_ donePath: String) -> String {
        "{ printf %s $? > \(shq(donePath)); } 2>/dev/null || :"
    }

    /// Wrap the shell command in an "open a new window, settle, run this, and report
    /// the window id / session id / tty" script for the given terminal. The trailing
    /// `return …` line makes osascript print `wid|sid|tty` on stdout. The iTerm and
    /// Terminal variants open the window FIRST, capture the handles, `delay` for
    /// `inputSettleDelay`, and only then type the command (see the constant's doc for
    /// why). Ghostty's takes the command as part of creating the window, so it types
    /// nothing, waits for nothing, and reports the window id alone — `runGhosttySpawn`
    /// supplies the other two fields, which are not Ghostty's to give.
    ///
    /// `cmd` is the command the WINDOW runs, which for Ghostty is the tmux line
    /// `ghosttyCommand` builds rather than the agent's shell command: the quoting the
    /// other two survive by being typed does not survive being embedded here.
    ///
    /// When `restoreBID` is nil (a user pressing SPAWN AGENT) the script `activate`s
    /// the terminal so the user lands in the new window. When it's a bundle id (the
    /// auto-fix monitor) the script instead opens the window WITHOUT activating and
    /// bounces focus straight back to that app the instant the window exists — so a
    /// background spawn never steals focus. Creating an iTerm window activates iTerm
    /// on its own (there is no "open in background" flag), which is why the restore is
    /// wedged between window creation and the input-settle `delay`: focus is gone for
    /// a blink, not for the whole 5s settle. The captured handles are script-globals,
    /// so they survive leaving and re-entering the terminal's `tell` block.
    ///
    /// When the app to restore to IS the terminal, activating it restores nothing: the
    /// window just created is already that app's front window, so the operator lands on
    /// the agent's window rather than the one they were in. That is the common case —
    /// agents are dispatched from a terminal — so there the front window is captured
    /// before the spawn and re-selected after it. Restoring to any other app is
    /// answered by the activate alone.
    static func appleScript(for term: SpawnTerminal, shellCommand cmd: String,
                            restoreFocusTo restoreBID: String? = nil) -> String {
        let esc = cmd
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        let bid = (restoreBID?.isEmpty ?? true) ? nil : restoreBID
        let sameApp = bid == term.bundleID
        switch term {
        case .ghostty:
            guard let bid else {
                return """
                tell application "Ghostty"
                    activate
                    set _wid to (id of (new window with configuration {command:"\(esc)"})) as string
                end tell
                return _wid & "||"
                """
            }
            // Ghostty comes forward on its own when a window is created, so the restore
            // is the same bounce the other two need. Re-raising the operator's own
            // window is `activate window`: Ghostty's window `index` is read-only, and
            // it has no `select`.
            let prevCapture = sameApp
                ? "\n    set _prev to missing value\n    try\n        set _prev to id of front window\n    end try"
                : ""
            let prevRestore = sameApp
                ? "\ntell application \"Ghostty\"\n    if _prev is not missing value then\n        try\n            activate window (first window whose id is _prev)\n        end try\n    end if\nend tell"
                : ""
            return """
            tell application "Ghostty"\(prevCapture)
                set _wid to (id of (new window with configuration {command:"\(esc)"})) as string
                delay \(ghosttyRaiseDelay)
            end tell
            tell application id "\(escBundleID(bid))" to activate\(prevRestore)
            return _wid & "||"
            """
        case .iterm:
            guard let bid else {
                return """
                tell application "iTerm"
                    activate
                    set w to (create window with default profile)
                    set _sid to ""
                    set _tty to ""
                    tell current session of w
                        set _tty to tty
                        set _sid to id
                    end tell
                    set _wid to (id of w) as string
                    delay \(inputSettleDelay)
                    tell current session of w
                        write text "\(esc)"
                    end tell
                end tell
                return _wid & "|" & _sid & "|" & _tty
                """
            }
            let prevCapture = sameApp
                ? "\n    set _prev to missing value\n    try\n        set _prev to id of current window\n    end try"
                : ""
            let prevRestore = sameApp
                ? "\n    if _prev is not missing value then\n        try\n            select (first window whose id is _prev)\n        end try\n    end if"
                : ""
            return """
            tell application "iTerm"\(prevCapture)
                set w to (create window with default profile)
                set _sid to ""
                set _tty to ""
                tell current session of w
                    set _tty to tty
                    set _sid to id
                end tell
                set _wid to (id of w) as string
            end tell
            tell application id "\(escBundleID(bid))" to activate
            tell application "iTerm"\(prevRestore)
                delay \(inputSettleDelay)
                tell current session of w
                    write text "\(esc)"
                end tell
            end tell
            return _wid & "|" & _sid & "|" & _tty
            """
        case .terminal:
            guard let bid else {
                return """
                tell application "Terminal"
                    activate
                    set _tab to do script ""
                    set _tty to tty of _tab
                    set _wid to (id of front window) as string
                    delay \(inputSettleDelay)
                    do script "\(esc)" in _tab
                end tell
                return _wid & "||" & _tty
                """
            }
            let prevCapture = sameApp
                ? "\n    set _prev to missing value\n    try\n        set _prev to id of front window\n    end try"
                : ""
            let prevRestore = sameApp
                ? "\n    if _prev is not missing value then\n        try\n            set _pw to (first window whose id is _prev)\n            set index of _pw to 1\n            set frontmost of _pw to true\n        end try\n    end if"
                : ""
            return """
            tell application "Terminal"\(prevCapture)
                set _tab to do script ""
                set _tty to tty of _tab
                set _wid to (id of front window) as string
            end tell
            tell application id "\(escBundleID(bid))" to activate
            tell application "Terminal"\(prevRestore)
                delay \(inputSettleDelay)
                do script "\(esc)" in _tab
            end tell
            return _wid & "||" & _tty
            """
        }
    }

    /// AppleScript-string-escape a bundle id for embedding in `tell application id "…"`.
    /// Bundle ids are dotted reverse-DNS (no quotes in practice), but escape defensively.
    private static func escBundleID(_ bid: String) -> String {
        bid.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
    }

    /// Split osascript's `wid|sid|tty` line. Empty middle field is expected for
    /// Terminal.app (no stable session id). Missing fields degrade to "".
    static func parseCapture(_ s: String) -> (String, String, String) {
        let parts = s.trimmingCharacters(in: .whitespacesAndNewlines)
            .split(separator: "|", omittingEmptySubsequences: false).map(String.init)
        return (parts.count > 0 ? parts[0] : "",
                parts.count > 1 ? parts[1] : "",
                parts.count > 2 ? parts[2] : "")
    }

    /// POSIX single-quote a path for safe embedding in the shell command.
    static func shq(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    /// Run an AppleScript, returning its stdout. Throws on a non-zero exit.
    @discardableResult
    private static func runOsascriptCapturing(_ script: String) throws -> String {
        guard let outcome = OSAScript.run(script) else {
            throw SpawnError.osascript(code: -1, stderr: "could not launch osascript")
        }
        guard outcome.succeeded else {
            throw SpawnError.osascript(code: outcome.status, stderr: outcome.stderr)
        }
        return outcome.stdout
    }
}

// MARK: - Shared SPAWN AGENT button

/// The tinted "SPAWN AGENT" button shared by the Review and Resolve-conflicts
/// wizards: full-width, coloured when the config is valid and grey when not, with
/// a help string naming the terminal it will open.
struct SpawnAgentButton: View {
    let isValid: Bool
    let tint: Color
    let terminalTitle: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 6) {
                Image(systemName: "play.fill")
                Text("SPAWN AGENT").bold().kerning(0.5)
                Spacer()
                Image(systemName: "terminal.fill").font(.caption2).opacity(0.8)
            }
            .foregroundStyle(.white)
            .padding(.vertical, 8)
            .padding(.horizontal, 10)
            .frame(maxWidth: .infinity)
            .background(RoundedRectangle(cornerRadius: 7).fill(isValid ? tint : Color.gray))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!isValid)
        .help("Open a new \(terminalTitle) window running the agent runner with this prompt.")
    }
}

// MARK: - Review wizard (shown in the results area)

/// The Review-PRs wizard: target, scope, depth and action toggles, then SPAWN.
/// Rendered in the results pane when the "Review PRs" grid card is selected.
struct ReviewWizardView: View {
    @EnvironmentObject var store: Store
    private let tint = Color.pink

    /// Shared appear/disappear transition for contextual rows that are shown only
    /// where they apply (fade + slide).
    private let rowTransition: AnyTransition = .opacity.combined(with: .move(edge: .top))

    /// The results area is shorter than the wizard, so it scrolls in the app.
    /// `scrolls: false` (headless render only) drops the ScrollView so the
    /// snapshot isn't blank (ImageRenderer can't render ScrollView content).
    private let scrolls: Bool

    /// Default init for the live app. The optional `seed*` params let the headless
    /// renderer snapshot specific states (e.g. single-PR mode) without driving the
    /// UI; they default to nil, leaving each `@State`'s declared value.
    init(scrolls: Bool = true,
         seedTarget: PRTarget? = nil,
         seedSpecificPR: String? = nil,
         seedUsername: String? = nil,
         seedSpecificAuthor: SpecificAuthor? = nil,
         seedSpecificAuthorLogin: String? = nil) {
        self.scrolls = scrolls
        if let v = seedTarget { _target = State(initialValue: v) }
        if let v = seedSpecificPR { _specificPR = State(initialValue: v) }
        if let v = seedUsername { _username = State(initialValue: v) }
        if let v = seedSpecificAuthor { _specificAuthor = State(initialValue: v) }
        if let v = seedSpecificAuthorLogin { _specificAuthorLogin = State(initialValue: v) }
    }

    @State private var depthValue: Double = ReviewWizardView.defaultDepthValue()
    @State private var target: PRTarget = .mine
    @State private var username = ""
    @State private var markReady = true
    @State private var leaveReviews = true
    @State private var replyToReviews = true
    @State private var includeDrafts = true
    @State private var includeReady = true
    @State private var specificPR = ""
    @State private var finalPass = false
    /// Soft-approve a perfectly-clean PR with a friendly thank-you comment (no APPROVE
    /// action). On by default; hidden for my own PRs (I don't thank myself).
    @State private var softApprove = true
    @State private var status: String?
    /// "Run on mesh" (effective only while the row is live) — checked by default,
    /// like the Linux wizards.
    @State private var useMesh = true
    /// A mesh dispatch is in flight — disables SPAWN so a second click can't
    /// double-dispatch (the Qt wizards disable the button the same way).
    @State private var meshDispatching = false
    /// For a specific PR: the polled author disposition (mine / theirs / not-yet). Drives
    /// which action toggles show. `.unknown` while we determine it (offers all, gated).
    @State private var specificAuthor: SpecificAuthor = .unknown
    @State private var authorLoading = false
    /// The polled author login (for a specific PR) — used to check the ban list.
    @State private var specificAuthorLogin: String?

    /// The review-depth levels, loaded once from the shared core.
    private var depths: [PromptDepth] { ReviewCatalog.depths() }
    private var depthIndex: Int {
        guard !depths.isEmpty else { return 0 }
        return min(max(Int(depthValue), 0), depths.count - 1)
    }
    private var depth: PromptDepth {
        depths.isEmpty
            ? PromptDepth(id: "", title: "", blurb: "", fragment: "")
            : depths[depthIndex]
    }

    private static func defaultDepthValue() -> Double {
        let all = ReviewCatalog.depths()
        let idx = all.firstIndex(where: { $0.id == ReviewCatalog.defaultDepthID() }) ?? 0
        return Double(idx)
    }

    private var config: ReviewConfig {
        ReviewConfig(
            depth: depth.id,
            target: target,
            username: username,
            me: store.effectiveMe,
            markReady: markReady,
            leaveReviews: leaveReviews,
            replyToReviews: replyToReviews,
            includeDrafts: includeDrafts,
            includeReady: includeReady,
            specificPR: specificPR,
            finalPass: finalPass,
            softApprove: softApprove,
            specificAuthor: specificAuthor)
    }

    var body: some View {
        content.wizardScroll(scrolls)
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 10) {
            titleRow
            if let banned = bannedTargetLogin { bannedWarning(banned) }
            targetRow
            contextRow
            if target == .specific { authorHint }
            // Draft/ready scope applies to a whose-PRs sweep, not a single PR.
            if target != .specific { scopeRow }
            depthRow
            checkboxes
            // The Final-E2E verdict makes no sense for my own PRs (I don't approve my
            // own work); hidden for the mine disposition.
            if config.canFinalPass { finalPassRow }
            spawnButton
            if let status { WizardStatusLine(status) }
        }
        .padding(.trailing, 2)
        // Animate contextual rows reflowing as the target/scope/author change.
        .animation(.easeInOut(duration: 0.22), value: target)
        .animation(.easeInOut(duration: 0.22), value: specificAuthor)
        .animation(.easeInOut(duration: 0.22), value: includeDrafts)
        .animation(.easeInOut(duration: 0.22), value: includeReady)
        .onChange(of: specificPR) { _ in refreshAuthor() }
        .onChange(of: target) { _ in refreshAuthor() }
    }

    /// The author being reviewed IF they're banned for prompt injection — nil otherwise.
    /// For "someone else's PRs" it's the handle; for a specific PR it's the polled author.
    /// (My own PRs are never banned.)
    private var bannedTargetLogin: String? {
        let bans = store.bannedAuthors
        switch target {
        case .mine:
            return nil
        case .someone:
            let u = username.trimmingCharacters(in: .whitespaces)
            return BanList.isBanned(u, in: bans) ? u : nil
        case .specific:
            if let login = specificAuthorLogin, BanList.isBanned(login, in: bans) { return login }
            return nil
        }
    }

    private func bannedWarning(_ login: String) -> some View {
        WizardBanWarning(
            login: login,
            detail: "Reviewing their PRs is strongly discouraged while the ban stands.")
    }

    /// A one-line note under the single-PR field: whose PR it is once polled, so the
    /// user knows why some toggles disappeared.
    @ViewBuilder
    private var authorHint: some View {
        let (icon, text, color): (String, String, Color) = {
            if authorLoading { return ("hourglass", "Checking who authored this PR…", .secondary) }
            switch specificAuthor {
            case .mine:    return ("person.fill", "Your PR — fix-on-branch review.", .green)
            case .theirs:  return ("person.2.fill", "Someone else's PR — review only, hands off.", .orange)
            case .unknown: return ("questionmark.circle", "Enter a PR to detect whether it's yours.", .secondary)
            }
        }()
        HStack(spacing: 5) {
            Image(systemName: icon).font(.system(size: 9)).foregroundStyle(color)
            Text(text).font(.system(size: 10)).foregroundStyle(color)
        }
        .transition(rowTransition)
    }

    /// Whether the shared contextual field acts as a github-username box (someone
    /// else's), a single-PR box (specific PR), or is hidden (mine).
    private enum ContextRole { case none, username, pr }
    private var contextRole: ContextRole {
        switch target {
        case .specific: return .pr
        case .someone:  return .username
        case .mine:     return .none
        }
    }

    private var titleRow: some View {
        WizardTitle(systemImage: "checklist", title: "Review PRs", tint: tint)
    }

    private var targetRow: some View {
        WizardTargetPicker(target: $target, me: store.effectiveMe)
    }

    /// One field, one slot — the github-username box and the single-PR box share a
    /// place and never show together (see `contextRole`).
    @ViewBuilder
    private var contextRow: some View {
        switch contextRole {
        case .username:
            WizardTextField(systemImage: "at", placeholder: "github username", text: $username)
                .transition(rowTransition)
        case .pr:
            VStack(alignment: .leading, spacing: 3) {
                WizardTextField(systemImage: "number", placeholder: "PR # or URL", text: $specificPR)
                    .help("Review just this one PR — paste its number or GitHub URL.")
                if let warning = prWarning {
                    Text(warning)
                        .font(.system(size: 10))
                        .foregroundStyle(.red.opacity(0.85))
                }
            }
            .transition(rowTransition)
        case .none:
            EmptyView()
        }
    }

    /// A hint under the PR field when a pasted URL points at a different repo.
    private var prWarning: String? {
        guard config.prRef.repoMismatch else { return nil }
        let (owner, repo) = config.targetRepo
        return "That PR isn't in \(owner)/\(repo)."
    }

    private var scopeRow: some View {
        VStack(alignment: .leading, spacing: 6) {
            Toggle(isOn: $includeDrafts) { Text("Review draft PRs").font(.caption) }
            Toggle(isOn: $includeReady) { Text("Review ready-for-review PRs").font(.caption) }
        }
        .toggleStyle(.checkbox)
    }

    private var depthRow: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text("Review depth").font(.caption.bold()).foregroundStyle(.secondary)
                Spacer()
                Text(depth.title).font(.caption.bold()).foregroundStyle(.primary)
            }
            Slider(value: $depthValue,
                   in: 0...Double(max(depths.count - 1, 0)),
                   step: 1)
                .tint(tint)
            Text(depth.blurb).font(.system(size: 10)).foregroundStyle(.secondary)
        }
    }

    private var checkboxes: some View {
        VStack(alignment: .leading, spacing: 6) {
            if config.canMarkReady {
                Toggle(isOn: $markReady) {
                    Text("Mark clean PRs ready for review").font(.caption)
                }
                .help("Mark perfectly-clean PRs ready for review.")
                .transition(rowTransition)
            }
            if config.canLeaveReviews {
                Toggle(isOn: $leaveReviews) {
                    Text("Leave reviews (CLAUDE.md format)").font(.caption)
                }
                .help("Post per-line reviews on these PRs.")
                .transition(rowTransition)
            }
            if config.canReplyToReviews {
                Toggle(isOn: $replyToReviews) {
                    Text("Reply to others' review threads").font(.caption)
                }
                .help("Reply \"Fixed in <hash>\" on threads others left.")
                .transition(rowTransition)
            }
            if config.canSoftApprove {
                Toggle(isOn: $softApprove) {
                    Text("Soft-approve clean PRs (thank-you comment)").font(.caption)
                }
                .help("On a perfectly-clean PR, leave a friendly thank-you comment — never an APPROVE action.")
                .transition(rowTransition)
            }
        }
        .toggleStyle(.checkbox)
    }

    /// The escalation toggle — off by default, visually highlighted so it reads as
    /// the special "go all the way" option. Appends a final E2E + verdict block.
    private var finalPassRow: some View {
        EscalationToggle(
            isOn: $finalPass,
            systemImage: "sparkles",
            title: "Final E2E pass + verdict",
            help: "One last full-E2E pass with big swarms: approve clean PRs, request changes on real blockers.",
            fill: .yellow)
    }

    private var spawnButton: some View {
        WizardSpawnControls(duty: "review", useMesh: $useMesh,
                            isValid: config.isValid && !meshDispatching,
                            tint: tint,
                            terminalTitle: AgentSpawner.resolved(store.terminal).title,
                            // Only a single PR is a session the mesh could place: a
                            // sweep opens none, it queues one review per PR for this
                            // machine's own cap to start.
                            routable: target == .specific,
                            action: spawn)
    }

    /// A short label for the ongoing-processes list, e.g. "Review · #337 · Deep".
    /// One shape, because a single PR is the only thing this wizard spawns: a sweep is
    /// queued a PR at a time and each of those rows is labelled by its own
    /// `Store.RequestedWork`.
    private var trackingLabel: String {
        let n = config.prRef.number.map { "#\($0)" } ?? "PR"
        return "Review · \(n) · \(depth.title)"
    }

    /// The one PR this run concerns — the open-in-browser fallback when its window
    /// can't be focused.
    private var trackingPRURL: String? {
        guard let n = config.prRef.number else { return nil }
        let (owner, repo) = config.targetRepo
        return "https://github.com/\(owner)/\(repo)/pull/\(n)"
    }

    /// Poll the specific PR's author (debounced) so the wizard can hide the toggles that
    /// don't apply and pick the right mine/theirs prompt — no author-guessing left to
    /// the spawned agent.
    private func refreshAuthor() {
        guard target == .specific else { specificAuthor = .unknown; specificAuthorLogin = nil; authorLoading = false; return }
        let ref = config.prRef
        guard ref.isValid, let num = ref.number else {
            specificAuthor = .unknown; specificAuthorLogin = nil; authorLoading = false; return
        }
        let (owner, repo) = config.targetRepo
        let me = store.effectiveMe
        let pending = specificPR
        specificAuthor = .unknown        // offer all toggles while we determine
        specificAuthorLogin = nil
        authorLoading = true
        Task {
            try? await Task.sleep(nanoseconds: 400_000_000)   // debounce keystrokes
            if specificPR != pending { return }               // superseded by newer input
            let login = await Self.fetchAuthor(owner: owner, repo: repo, number: num)
            guard specificPR == pending, target == .specific else { return }
            authorLoading = false
            specificAuthorLogin = login
            if let login, !me.isEmpty {
                specificAuthor = login.lowercased() == me.lowercased() ? .mine : .theirs
            } else {
                specificAuthor = .unknown
            }
        }
    }

    /// One `gh pr view … --json author` → the author login, or nil on failure.
    private static func fetchAuthor(owner: String, repo: String, number: Int) async -> String? {
        guard let data = try? await GH.run(
            ["pr", "view", String(number), "--repo", "\(owner)/\(repo)", "--json", "author"])
        else { return nil }
        struct R: Decodable { struct A: Decodable { let login: String }; let author: A }
        return (try? JSONDecoder().decode(R.self, from: data))?.author.login
    }

    private func spawn() {
        let cfg = config
        // A single PR is one agent, and the two branches below dispatch it. A
        // whose-PRs sweep is not: it becomes one queued review per PR, so the task cap
        // decides how many run at once rather than one agent being handed every draft
        // in the repo at the same time.
        guard cfg.isSinglePR else {
            queueSweep(cfg)
            return
        }
        // Mesh path: hand the job to the local node (it picks the executor, with
        // failover) instead of opening a terminal here — mirrors the Linux wizards.
        if MeshSpawnRow.isLive(store), useMesh {
            meshDispatching = true
            status = "Dispatching over the mesh…"
            AuditLog.log("panel", "review", "\(trackingLabel) · via mesh")
            store.meshDispatch(duty: "review", prompt: cfg.buildPrompt()) { results, err in
                meshDispatching = false
                status = MeshSpawn.summarize(results, error: err)
            }
            return
        }
        // Local: the SAME pipeline the auto-monitor rides — dedup, ban, tracking —
        // only the trigger (this click) and its policies (foreground, no mesh gate)
        // differ. See `AgentDispatchGate`.
        let term = AgentSpawner.resolved(store.terminal)
        let job = Store.AgentJob(kind: "review", auditAction: "review",
                                 label: trackingLabel, prompt: cfg.buildPrompt(),
                                 prURL: trackingPRURL, prNumber: cfg.prRef.number,
                                 authorLogin: reviewedAuthorLogin, duty: "review",
                                 workKey: "", counter: nil)
        status = "Launching \(term.title)…"
        Task {
            status = statusText(for: await store.dispatchAgent(job, source: .panel),
                                terminal: term.title)
        }
    }

    /// Expand a whose-PRs sweep into one queued review per PR, and say what landed.
    ///
    /// The PRs are the panel's own last fetch — the list the operator was looking at
    /// when they pressed the button. Before the first fetch that list is empty, and
    /// queueing nothing out of it would read as "you have no drafts".
    private func queueSweep(_ cfg: ReviewConfig) {
        guard store.hasLoaded else {
            status = "PRs haven't loaded yet — refresh, then sweep."
            return
        }
        let (queued, already) = store.requestReviewSweep(cfg)
        if queued > 0 {
            let waiting = already > 0 ? " (\(already) already queued)" : ""
            status = "Queued \(queued) review\(queued == 1 ? "" : "s")\(waiting)"
                + " — they start as slots free."
        } else if already > 0 {
            status = "All \(already) are queued already."
        } else {
            status = "No open PRs in that scope."
        }
    }

    /// Whose PR this run would review, when known — the pipeline's ban dimension.
    ///
    /// Only a single PR is dispatched from here at all: a sweep queues instead, and
    /// each ask it queues carries its own PR's author. So this is the wizard's
    /// debounced poll, which may still be unknown — and nil for my own PR, which has
    /// no ban dimension to check.
    private var reviewedAuthorLogin: String? { specificAuthorLogin }
}

/// The wizard status line for one dispatch outcome — shared by all four wizards
/// so refusals read identically everywhere.
///
/// A wizard SPAWN is a `.panel` dispatch, and none of the mesh gate, the
/// automatic-task cap and the rate-limit budget applies to a human's click, so
/// `.standDown`, `.atCapacity` and `.unaffordable` are answers only a monitor gets.
/// They are spelled out rather than folded into a `default`, so adding an outcome
/// keeps failing this switch until someone decides what the wizard should say about it.
func statusText(for outcome: Store.DispatchOutcome, terminal: String) -> String {
    switch outcome {
    case .spawned: return "Launched \(terminal) · \(Fmt.clock(Date()))"
    case .inFlight: return "An agent is already on this PR — see its session above."
    case .banned: return "Author is banned for prompt injection — un-ban to review."
    case .standDown: return "Another mesh node originates this work."
    case .atCapacity: return "This machine is at its cap of concurrent automatic tasks."
    case .unaffordable: return "Too little rate limit left for automatic work."
    case .failed(let msg): return "Failed: \(msg)"
    }
}
