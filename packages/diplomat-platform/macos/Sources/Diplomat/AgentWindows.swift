import Foundation
import DiplomatCore

/// Where a run's terminal window is, so the panel can raise it again.
///
/// The run book is a cross-language contract and carries only what both front-ends mean
/// by a run; a window id is neither — a Linux spawn is a detached process with no window
/// handle at all. So it lives beside the run rather than in it, as one more file in the
/// run directory next to `runner` / `port` / `session`. `AgentRegistry.forget` deletes
/// the directory, so a retired run takes its handle with it.
///
/// The handle is the exact way back to a session: a spawn walks away from a fully detached
/// terminal, and these three ids are what the focus AppleScript addresses. A run with no
/// handle — one the mesh node opened, or a live agent nobody dispatched — is reached the
/// inexact way instead, by walking its own process out to the window showing it
/// (`TerminalFocus`). Only a run whose agent is on another machine cannot be clicked.
///
/// Whether a run is still going is not asked here and never was a window question: the
/// answer is its agent's pid in the process table (`AgentState`), and closing a window
/// kills the session's processes, so a shut window is a pid that has gone.
enum AgentWindows {
    /// The three ids `focus` addresses a session by.
    struct Handle: Equatable, Codable {
        /// Which terminal app opened it ("ghostty" / "iterm" / "terminal").
        var terminal: String
        /// Terminal window id (string form) — the focus target.
        var windowID: String
        /// What the window's own id does not identify: iTerm's session GUID, or a
        /// Ghostty run's tmux session name, which is what reaps it. Empty for
        /// Terminal.app, which has neither.
        var sessionID: String
    }

    private static func path(_ runID: String) -> URL {
        AgentRegistry.runDir(runID).appendingPathComponent("window")
    }

    /// Record where this run's window is. Best-effort: a handle that cannot be written is
    /// a row that cannot be clicked, never a spawn that failed.
    static func stage(_ runID: String, _ handle: Handle) {
        guard let data = try? JSONEncoder().encode(handle) else { return }
        try? data.write(to: path(runID), options: .atomic)
    }

    /// This run's window handle, or nil for a run that never had one.
    static func handle(_ runID: String) -> Handle? {
        guard let data = try? Data(contentsOf: path(runID)) else { return nil }
        return try? JSONDecoder().decode(Handle.self, from: data)
    }

    /// Bring a session's terminal window to the front. Returns false when the window no
    /// longer exists (closed) or AppleScript errors — the caller then re-settles and the
    /// run's row goes with the next tick's verdict.
    @discardableResult
    static func focus(_ handle: Handle) -> Bool {
        guard !handle.windowID.isEmpty else { return false }
        let term = SpawnTerminal(rawValue: handle.terminal) ?? .iterm
        let script = focusScript(term: term, windowID: handle.windowID,
                                 sessionID: handle.sessionID)
        return OSAScript.runSilently(script)
    }

    /// Close a session's terminal window. Returns whether AppleScript accepted it.
    ///
    /// The mirror of `focus`, and used for exactly one thing: a run a BACKSTOP ended
    /// (`AgentState.reapable`), which is two verdicts. The quiescence one is twenty
    /// minutes of a screen that has not moved, so nothing is being read and nothing is
    /// being typed. The run deadline is the operator's own instruction to give up on a
    /// task at four hours — and there the agent may well be working, which is the point:
    /// they asked for the bay back anyway. A run that ends the ordinary way keeps its
    /// window either way: its agent is alive at its prompt with the whole task in
    /// context, and that is a session the operator may still want to read.
    @discardableResult
    static func close(_ handle: Handle) -> Bool {
        guard !handle.windowID.isEmpty else { return false }
        let term = SpawnTerminal(rawValue: handle.terminal) ?? .iterm
        // Closing a Ghostty window does not end what is running in it (measured on
        // 1.3.1): the window goes and the agent keeps running on a pane whose client is
        // no longer on screen, holding a bay that nothing left can retire. Its tmux
        // session is what ends the run; the window close is what clears the screen.
        //
        // Ending a session rather than detaching from it is the thing `TerminalFocus.close`
        // is written not to do, and this is the case it carves out: the session is one
        // this spawn made up a name for and put one agent in, so it is nobody's to share.
        if term == .ghostty { _ = TerminalFocus.killSession(named: handle.sessionID) }
        return OSAScript.runSilently(closeScript(term: term, windowID: handle.windowID))
    }

    /// AppleScript that closes the window with the captured id. A window the operator
    /// already closed is not a failure in any of the three.
    static func closeScript(term: SpawnTerminal, windowID: String) -> String {
        // Ghostty's `close` closes a terminal SURFACE; the window verb is `close window`,
        // and it will not take a window found by walking `windows` (-1708). So the id is
        // resolved into a specifier instead — which errors outright when nothing matches
        // it, where walking simply matches nothing. Hence the `try` only this one needs.
        if term == .ghostty {
            return """
            tell application "Ghostty"
                try
                    close window (first window whose id is "\(windowID)")
                end try
            end tell
            """
        }
        return """
        tell application "\(term.appName)"
            repeat with w in windows
                if (id of w as string) is "\(windowID)" then close w
            end repeat
        end tell
        """
    }

    /// AppleScript that selects the window with the captured id (erroring if it's gone, so
    /// the caller sees a non-zero exit). iTerm also re-selects the exact session; Terminal
    /// raises + fronts the window.
    ///
    /// The window is addressed by id rather than found by walking `windows`, and the app
    /// is activated last. `activate` returns before the app has finished coming forward,
    /// and reordering its windows renumbers the index-based references a `repeat with w in
    /// windows` is walking — so a search that activates first intermittently steps over
    /// the very window it was given the id of. A direct specifier cannot: it is resolved
    /// once, by id, against whatever order the app is in.
    static func focusScript(term: SpawnTerminal, windowID: String, sessionID: String) -> String {
        switch term {
        case .ghostty:
            // Ghostty ids are opaque strings ("tab-group-6000023ec120"), so they are
            // quoted rather than written bare the way the other two numeric ids are.
            //
            // This one cannot report a window that is gone. Ghostty's scripting bridge
            // keeps answering for a closed window — `first window whose id is …` still
            // resolves, and `activate window` still returns success — so a Ghostty focus
            // says yes to a window that is no longer on screen. Nothing downstream turns
            // on it: whether the RUN is still going is its agent's pid, never its window.
            return """
            tell application "Ghostty"
                set w to (first window whose id is "\(windowID)")
                activate window w
                activate
            end tell
            """
        case .iterm:
            return """
            tell application "iTerm"
                set w to window id \(windowID)
                select w
                repeat with t in tabs of w
                    repeat with s in sessions of t
                        if (id of s) is "\(sessionID)" then
                            select t
                            tell t to select s
                        end if
                    end repeat
                end repeat
                activate
            end tell
            """
        case .terminal:
            return """
            tell application "Terminal"
                set w to window id \(windowID)
                set index of w to 1
                set frontmost of w to true
                activate
            end tell
            """
        }
    }
}
