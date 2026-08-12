import Foundation

/// Reading an OpenCode agent's own session — the Swift twin of `diplomat_app/opencodeapi.py`.
///
/// An OpenCode TUI given `--port` serves its session over HTTP on loopback while it
/// works, and that server answers the two questions the applet has always had to guess
/// at:
///
/// * **is this run working, or back at its prompt?** — its last message carries a
///   completion stamp, set the instant the turn ends. A stamp is positive evidence the
///   turn is over; its absence is positive evidence it is still in flight. Neither is an
///   inference from how a status bar happened to be drawn.
///
/// What the run SPENT is not asked here. A turn's price is per-message, so a run's is a
/// sum over its whole transcript, and this poll reads one message; a finished run is
/// priced from `opencode export` instead, once, when it ends.
///
/// The screen is still read for a run this cannot reach. `AgentState.classifyActivity`
/// takes whichever answer it gets and says which one it used.
///
/// ## Which session is this run's
///
/// Every run gets its own server, but not its own session store: OpenCode keeps one
/// global store, so `GET /session` on any port answers with the machine's own history
/// rather than this run's — its hundred most recent sessions, newest first. So a
/// run is matched to its session the only way that is exact — by the prompt.
/// `candidates` narrows the list to sessions that could be this run's, and `isOurs`
/// confirms one against the prompt the applet staged.
///
/// ## Loopback, and unauthenticated
///
/// The server binds `127.0.0.1`. It is NOT password-protected, and that is forced rather
/// than chosen: OpenCode's server does support a password, but its own TUI sends none, so
/// a run started with `OPENCODE_SERVER_PASSWORD` set exits on `Unauthorized` before doing
/// any work (verified against 1.4.3). So the port is reachable by any other user on the
/// machine, and driving it runs commands as this user.
///
/// Only the decisions live here. Dialling the port is the platform's job (`OpenCodeProbe`
/// on macOS), because this library is built for Linux too, where URLSession is a separate
/// module this target does not take.
public enum OpenCodeAPI {
    /// The interface a run's server binds — OpenCode's own default, restated because it
    /// is also the address the probe dials.
    public static let host = "127.0.0.1"

    /// Per-request budget. This runs on the panel's tick, once per OpenCode run, so it
    /// has to fail faster than the tick rather than hold it up: a wedged server must cost
    /// one unavailable answer, not a frozen panel.
    public static let timeout: TimeInterval = 2.0

    /// Most a single response may be. The last-message poll is one message and the
    /// binding fetch is a session seconds old, so both are small — but a message carries
    /// its tool output inline, and one agent that cats a large file would otherwise pull
    /// it through this probe on every tick forever.
    public static let maxBytes = 8 * 1024 * 1024

    /// Most sessions considered when matching a run to its own. Ordinarily there is one;
    /// the cap only bites when a run never binds at all, where it is what stops a
    /// fruitless search costing one message fetch per stale session on every tick.
    public static let maxCandidates = 4

    /// Sessions that could be this run's, oldest first.
    ///
    /// Three filters, each of which a run's own session always passes: it is in the
    /// directory the agent was spawned into, it was created no earlier than the run was
    /// dispatched, and it has not already been claimed by another run. What survives is
    /// ordinarily one session; `isOurs` settles the rest.
    public static func candidates(_ sessions: [[String: Any]], directory: String,
                                  sinceMs: Double, taken: Set<String>) -> [String] {
        var found: [(Double, String)] = []
        for s in sessions {
            guard let id = s["id"] as? String, !taken.contains(id) else { continue }
            guard s["directory"] as? String == directory else { continue }
            guard let time = s["time"] as? [String: Any],
                  let created = (time["created"] as? NSNumber)?.doubleValue,
                  created >= sinceMs else { continue }
            found.append((created, id))
        }
        return found.sorted { $0 < $1 }.map { $0.1 }
    }

    /// Is this the session our prompt was submitted to?
    ///
    /// `--prompt` lands verbatim as the opening user message, so this is an equality test
    /// rather than a resemblance one. It is what makes the match exact when two runs are
    /// working in the same checkout at the same time — the case the directory and
    /// dispatch-time filters cannot separate, and the case the applet's own task cap
    /// makes ordinary rather than rare.
    public static func isOurs(_ messages: [[String: Any]], prompt: String) -> Bool {
        guard let first = messages.first,
              let info = first["info"] as? [String: Any],
              info["role"] as? String == "user" else { return false }
        let parts = first["parts"] as? [[String: Any]] ?? []
        let text = parts.filter { $0["type"] as? String == "text" }
            .map { $0["text"] as? String ?? "" }
            .joined()
        return text == prompt
    }

    /// What the last message says: working or done, and what it cost.
    ///
    /// `nil` when there is no message to read — a session created but not yet written to.
    /// That is not "idle": a run whose turn has not started has not finished either, and
    /// saying so would retire an agent seconds after it launched.
    ///
    /// A message with no completion stamp is a turn in flight. That covers a provider
    /// retry as well as ordinary work, which is the right reading of both: the agent is
    /// not back at its prompt and nothing else may be dispatched over it.
    public static func stateOf(_ messages: [[String: Any]]) -> AgentState.SessionState? {
        guard let last = messages.last,
              let info = last["info"] as? [String: Any] else { return nil }
        let time = info["time"] as? [String: Any] ?? [:]
        return AgentState.SessionState(busy: (time["completed"] as? NSNumber) == nil)
    }

    /// What a whole session spent, from the messages `opencode export` returns.
    ///
    /// Every message, because OpenCode reports a turn's price per message: reading only
    /// the last would price a two-hour review at whatever its closing sentence cost.
    ///
    /// Input, output and cache *writes*, never cache reads. Cache reads are huge and
    /// cheap and the transcript scan leaves them out for Claude Code, so counting them
    /// would make the per-task figure on the telemetry screen mean one thing for one
    /// runner and another for the other.
    public static func sessionTokens(_ messages: [[String: Any]]) -> Double {
        messages.reduce(0) { total, message in
            let info = message["info"] as? [String: Any] ?? message
            guard let tokens = info["tokens"] as? [String: Any] else { return total }
            let cache = tokens["cache"] as? [String: Any] ?? [:]
            return [tokens["input"], tokens["output"], cache["write"]]
                .reduce(total) { sum, value in
                    guard let n = (value as? NSNumber)?.doubleValue, n >= 0 else { return sum }
                    return sum + n
                }
        }
    }
}
