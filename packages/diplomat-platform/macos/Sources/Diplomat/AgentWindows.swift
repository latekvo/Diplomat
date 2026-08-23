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
        /// Which terminal app opened it ("iterm" / "terminal").
        var terminal: String
        /// Terminal window id (string form) — the focus target.
        var windowID: String
        /// iTerm session id (GUID); empty for Terminal.app, which has no stable one.
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
    /// The mirror of `focus`, and used for exactly one thing: a run the quiescence
    /// backstop ended (`AgentState.wentQuiet`) — twenty minutes of a byte-identical
    /// screen, so nothing is being read and nothing is being typed. A run that ends the
    /// ordinary way keeps its window: its agent is alive at its prompt with the whole
    /// task in context, and that is a session the operator may still want to read.
    @discardableResult
    static func close(_ handle: Handle) -> Bool {
        guard !handle.windowID.isEmpty else { return false }
        let term = SpawnTerminal(rawValue: handle.terminal) ?? .iterm
        return OSAScript.runSilently(closeScript(term: term, windowID: handle.windowID))
    }

    /// AppleScript that closes the window with the captured id. A window the operator
    /// already closed simply matches nothing, which is not a failure.
    static func closeScript(term: SpawnTerminal, windowID: String) -> String {
        """
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
