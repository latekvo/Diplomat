import Foundation

/// Reading a Hermes agent's own session — the Swift twin of `diplomat_runtime/hermesstore.py`.
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
///   The end of a turn is the end of the run only once nothing is owed to it, which is
///   what the `delegating` half of `stateOf` carries.
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

    /// The `delivery_state` of a background subagent's result the agent has not been
    /// handed yet — Hermes' own spelling, matched verbatim by the query that reads it.
    /// It covers both halves of "outstanding": a child still working, and a finished
    /// child whose result is queued to wake the agent.
    public static let undelivered = "pending"

    /// What the session says: mid-turn, or back at its prompt with nothing owed to it.
    ///
    /// `nil` when there is no message to read — a session created but not yet written to.
    /// That is not "idle": a run whose turn has not started has not finished either, and
    /// saying so would retire an agent seconds after it launched.
    ///
    /// Anything that is not a finished assistant message is a turn in flight, which is the
    /// right reading of all three ways that happens: the agent is mid tool call, a tool
    /// result is waiting to be answered, or the query has not been picked up yet.
    ///
    /// `delegating` is whether a background subagent still owes this session a result:
    /// `delegate_task(background=true)` hands the turn back and reports later as a fresh
    /// user turn, so a turn that ended with one outstanding is not a run that ended. A
    /// `nil` there is a store that could not say, and it takes the answer with it —
    /// ending a run on a delegation nobody could read is the mistake this half exists to
    /// stop. See the Python twin `hermesstore.delegating` for what fills it.
    public static func stateOf(role: String?, finishReason: String?,
                               delegating: Bool?) -> AgentState.SessionState? {
        guard let role else { return nil }
        let over = role == "assistant" && turnOver.contains(finishReason ?? "")
        if !over { return AgentState.SessionState(busy: true) }
        guard let delegating else { return nil }
        return AgentState.SessionState(busy: delegating)
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

    /// What one session cost in dollars, from the same row.
    ///
    /// The other unit a task can be priced in, and the one an OpenRouter-billed run is
    /// actually held to. Tokens alone cannot answer it: the same hundred thousand
    /// tokens are cents on a small model and dollars on a frontier one, which is why
    /// the model is read alongside the money and travels with it into the ledger.
    ///
    /// Hermes prices each session itself, against the provider's published rates for
    /// the model it ran on, and settles that figure when the provider reports the real
    /// one: `actual` is preferred where it exists and `estimated` answers until it
    /// does.
    ///
    /// nil where there is nothing to read — a session row that has not been priced yet,
    /// or a Hermes build older than the columns. That is a completion recorded without
    /// a price, exactly as an unattributable transcript is, and the gate falls back to
    /// its reserve rather than to a made-up figure.
    public static func sessionPrice(actual: Double?, estimated: Double?) -> Double? {
        [actual, estimated].first { ($0 ?? 0) > 0 } ?? nil
    }
}
