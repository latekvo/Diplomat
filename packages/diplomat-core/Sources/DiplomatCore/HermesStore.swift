import Foundation

/// Reading a Hermes agent's own session — the Swift twin of `diplomat_app/hermesstore.py`.
///
/// The same two questions `OpenCodeAPI` answers, from a different place. Hermes serves no
/// per-run port — its `serve` is one machine-level gateway, not a server per agent — but
/// it writes every session and every message to SQLite at `~/.hermes/state.db` as it
/// goes, mid-turn, and that is enough for both:
///
/// * **is this run working, or back at its prompt?** — the session's last message. An
///   assistant message whose `finish_reason` is in `turnOver` ended the turn; anything
///   else means one is still in flight. Positive evidence either way, rather than an
///   inference from whether Hermes' status bar happened to read `ready` when we looked.
/// * **what did it cost?** — the session row carries running totals, so a finished run is
///   priced by the agent that ran it. Cumulative, unlike OpenCode's per-message figures,
///   so it is simply read.
///
/// Which session is a run's own is settled by the prompt, for the same reason it is under
/// OpenCode: the store is machine-wide, so nothing else separates two agents working in
/// one checkout. `hermes chat -q` stores the query verbatim as the session's opening user
/// message, which `isOurs` compares against.
///
/// Only the decisions live here. Opening the database is the platform's job
/// (`HermesProbe` on macOS), because this library is built for Linux too, where SQLite is
/// a system library this target does not take.
public enum HermesStore {
    /// How long a query may wait on the agent's own writer before giving up. This runs on
    /// the panel's tick, so it has to fail faster than the tick rather than hold it up.
    public static let busyTimeout: TimeInterval = 2.0

    /// `finish_reason` values that mean the turn is over rather than continuing.
    /// `tool_calls` is deliberately absent: the agent asked for a tool and is waiting on
    /// it, which is the middle of a turn and not the end of one.
    public static let turnOver: Set<String> = ["stop", "end_turn", "length",
                                               "content_filter", "error"]

    /// What the session's last message says: mid-turn, or back at the prompt.
    ///
    /// `nil` when there is no message to read — a session created but not yet written to.
    /// That is not "idle": a run whose turn has not started has not finished either, and
    /// saying so would retire an agent seconds after it launched.
    ///
    /// Anything that is not a finished assistant message is a turn in flight, which is the
    /// right reading of all three ways that happens: the agent is mid tool call, a tool
    /// result is waiting to be answered, or the query has not been picked up yet.
    public static func stateOf(role: String?, finishReason: String?) -> AgentState.SessionState? {
        guard let role else { return nil }
        let over = role == "assistant" && turnOver.contains(finishReason ?? "")
        return AgentState.SessionState(busy: !over)
    }

    /// Is this the session our prompt was submitted to, judged by its opening message?
    ///
    /// `-q` stores the query verbatim, so this is an equality test rather than a
    /// resemblance one — the same exactness `OpenCodeAPI.isOurs` gets from `--prompt`.
    public static func isOurs(role: String?, content: String?, prompt: String) -> Bool {
        role == "user" && (content ?? "") == prompt
    }

    /// What one session spent, from the running totals on its row.
    ///
    /// Input, output and cache *writes*, never cache reads — the same three the Claude
    /// Code transcript scan and `OpenCodeAPI.sessionTokens` sum, so one ledger holds every
    /// runner in one unit.
    public static func sessionTokens(input: Int?, output: Int?, cacheWrite: Int?) -> Double {
        [input, output, cacheWrite].reduce(0) { sum, value in
            guard let n = value, n >= 0 else { return sum }
            return sum + Double(n)
        }
    }
}
