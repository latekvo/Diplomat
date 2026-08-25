import Foundation

/// Reading an OpenCode agent's own session — the Swift twin of `diplomat_runtime/opencodeapi.py`.
///
/// An OpenCode TUI given `--port` serves its session over HTTP on loopback while it
/// works, and that server answers the question the applet has always had to guess at:
/// **is this run working, or back at its prompt?** It keeps a status per session —
/// `busy`, `retry` or idle — the same one its own TUI draws from, and it stamps each
/// message it finishes. Neither is an inference from how a status bar happened to be
/// drawn.
///
/// Both are read, and a turn is over only when they agree: the server is running no
/// turn in this session AND the last thing it wrote was a finished message. Each covers
/// the other's one blind spot. The status alone calls a session idle in the moment
/// between its server coming up and its first turn starting, which would retire a run
/// seconds after it launched. The stamp alone calls a turn over between every two STEPS
/// of one: OpenCode writes an assistant message per step, each stamped as it completes,
/// and the gaps between them are short but there are hundreds of them in a long review
/// (1.4.3: 164 gaps in one 2.5-hour session, up to 757ms each) — enough that a poll
/// lands in one.
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
/// Every run gets its own server, but not its own session store: whichever port it is
/// asked on, `GET /session` answers out of the store OpenCode keeps per project — every
/// agent that has worked in this checkout or a worktree of it — most recently touched
/// first, and cut off at a hundred rows unless the fetch asks for more. So the fetch asks
/// for both halves of the narrowing the server can do (`sessionPath`): this run's
/// directory, which it matches exactly, and a limit one checkout's history does not
/// reach. Otherwise a busier neighbour holds every row of the answer and this run's
/// session is not in it at all.
///
/// A run is matched to its session the only way that is exact — by the prompt.
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
    /// binding fetch is `sessionLimit` session rows at some 500 bytes each — under a
    /// sixteenth of this between them — but a message carries its tool output inline, and
    /// one agent that cats a large file would otherwise pull it through this probe on
    /// every tick forever.
    public static let maxBytes = 8 * 1024 * 1024

    /// Most sessions a listing may hold. The server cuts the least recently touched, so
    /// this is how many of one checkout's sessions must be touched between a run's
    /// dispatch and its binding for its own to be cut too — far past what the task cap
    /// can produce in the seconds that takes.
    public static let sessionLimit = 1000

    /// Most sessions considered when matching a run to its own. Ordinarily there is one;
    /// the cap only bites when a run never binds at all, where it is what stops a
    /// fruitless search costing one message fetch per stale session on every tick.
    public static let maxCandidates = 4

    /// Where to ask for one directory's sessions, or nil for no directory.
    ///
    /// `?directory=` narrows the listing only while it has a value: sent empty it is not
    /// a filter that matches nothing but no filter at all, and the shared store comes
    /// back whole and cut to the limit — the answer the parameter is here to avoid. So an
    /// empty directory is nothing to ask, and reads as a server that would not answer.
    ///
    /// Neither parameter is in the OpenAPI document the server publishes; both are read
    /// off what a 1.4.3 server answers (`?limit=abc` is a 400, `?limit=0` is zero rows
    /// rather than no limit, a trailing slash on the directory matches nothing).
    public static func sessionPath(directory: String) -> String? {
        guard !directory.isEmpty,
              let filter = directory.addingPercentEncoding(
                  withAllowedCharacters: unescapedInDirectory) else { return nil }
        return "/session?directory=\(filter)&limit=\(sessionLimit)"
    }

    /// RFC 3986's unreserved set plus the path separator — what `urllib.parse.quote`
    /// leaves alone with `safe="/"`.
    private static let unescapedInDirectory = CharacterSet(
        charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~/")

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

    /// Is a turn in flight in this session, per `GET /session/status`?
    ///
    /// A session the server is not working on is absent from the map, so absence is the
    /// ordinary way to be idle. An entry that names itself `idle` is read as idle too,
    /// rather than as "present, therefore busy" — the two spellings mean one thing, and
    /// the resolver must not hold a run open because its server chose the other.
    ///
    /// Every other entry is a turn in flight, `retry` included: an agent waiting out a
    /// provider's backoff is not back at its prompt and nothing may be dispatched over
    /// it. An entry of a shape this does not know is one too — being listed at all is
    /// the server tracking the session, and only the two readings above are safe to end
    /// a run on.
    public static func isRunning(_ statuses: [String: Any], sessionID: String) -> Bool {
        guard let entry = statuses[sessionID] else { return false }
        return (entry as? [String: Any])?["type"] as? String != "idle"
    }

    /// Whether this session's turn is still in flight — from its server's status and its
    /// last message together, which is the whole of what makes the answer safe.
    ///
    /// `nil` — "ask the screen instead" — for either half being missing: a status the
    /// server would not report, and a session created but not yet written to. Neither is
    /// "idle". A run whose turn has not started has not finished either, and saying so
    /// would retire an agent seconds after it launched.
    ///
    /// Busy while the server says a turn is running, and busy again for a last message
    /// with no completion stamp. It takes both to call a turn over: the status is what
    /// holds a run open across the sub-second gaps between the steps of one turn, and the
    /// stamp is what holds it open before its first turn has begun.
    public static func stateOf(_ messages: [[String: Any]],
                               running: Bool?) -> AgentState.SessionState? {
        guard let running, let last = messages.last,
              let info = last["info"] as? [String: Any] else { return nil }
        let time = info["time"] as? [String: Any] ?? [:]
        return AgentState.SessionState(busy: running || (time["completed"] as? NSNumber) == nil)
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
