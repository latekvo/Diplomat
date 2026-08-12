import Foundation

/// Tells a *running* agent CLI session apart from one that has finished its turn and is
/// idling at the prompt ("awaiting input"). Pure & data-light so it's unit-tested and
/// shared verbatim; the front-end feeds it the session's visible terminal buffer.
///
/// The signal is the CLI's own live status bar: while a turn is in flight it shows an
/// interrupt hint (alongside the working spinner); the instant the turn ends and it
/// returns to the prompt, that hint is gone. So the presence of the interrupt hint *on
/// the live status bar* means busy; its absence means the agent is waiting on the user.
public enum AgentActivity {
    /// The interrupt hint a CLI renders only while a turn is actively running — one
    /// spelling per runner. Claude Code writes "esc to interrupt", OpenCode "esc
    /// interrupt", and Hermes "Ctrl+C to interrupt…". No string here contains another,
    /// so a pane is read against all of them: nothing can ask a pane which CLI drew it,
    /// and a runner missing from this list is one whose agents read as idle the whole
    /// time they work — their bays go back to the task cap and the monitors dispatch
    /// over the top of them.
    public static let busyMarkers = ["esc to interrupt", "esc interrupt",
                                     "ctrl+c to interrupt"]

    /// How many non-empty lines up from the bottom to inspect. The live status/hint bar is
    /// always the last line or two; scanning only this tail avoids matching the very same
    /// hint left behind in scrollback from an earlier turn (which would falsely read busy).
    public static let scannedTailLines = 5

    /// True when the visible buffer shows the CLI actively working (its interrupt hint is
    /// on the live status bar). False ⇒ the turn ended and it's back at the prompt, i.e.
    /// awaiting input.
    public static func looksBusy(_ visible: String) -> Bool {
        let lines = visible
            .split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        return lines.suffix(scannedTailLines).contains { line in
            let lower = line.lowercased()
            return busyMarkers.contains { lower.contains($0) }
        }
    }
}
