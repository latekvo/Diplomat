import Foundation

/// Reading an OpenCode agent's own session — the Swift twin of `diplomat_runtime/opencodeapi.py`.
///
/// OpenCode has had two shapes of server, and a run is asked through whichever one it
/// was spawned against:
///
/// * **1.x** — a TUI given `--port` serves its own session over HTTP on loopback while it
///   works: `sessionPath`, `candidates`, `isOurs`, `isRunning` and `stateOf`.
/// * **2.x** — no TUI serves anything. Every client talks to ONE per-user background
///   service, which the spawn creates the run's session on before the TUI attaches to it
///   (`AgentRunner.agentCommand`): everything from `isServiceVersion` on.
///
/// Either way the server answers the question the applet has always had to guess at:
/// **is this run working, or back at its prompt?** — from the state its own TUI draws
/// from, rather than an inference from how a status bar happened to be drawn.
///
/// A 1.x server keeps a status per session — `busy`, `retry` or idle — and stamps each
/// message it finishes. Both are read, and a turn is over only when they agree: the
/// server is running no turn in this session AND the last thing it wrote was a finished
/// message. Each covers the other's one blind spot. The status alone calls a session idle
/// in the moment between its server coming up and its first turn starting, which would
/// retire a run seconds after it launched. The stamp alone calls a turn over between
/// every two STEPS of one: OpenCode 1.x writes an assistant message per step, each
/// stamped as it completes, and the gaps between them are short but there are hundreds of
/// them in a long review (1.4.3: 164 gaps in one 2.5-hour session, up to 757ms each) —
/// enough that a poll lands in one.
///
/// What the run SPENT is not asked here. A turn's price is per-message, so a run's is a
/// sum over its whole transcript, and this poll reads one message; a finished run is
/// priced from the CLI's export instead (`exportArguments`), once, when it ends.
///
/// The screen is still read for a run this cannot reach. `AgentState.classifyActivity`
/// takes whichever answer it gets and says which one it used.
///
/// ## Which session is this run's
///
/// A 2.x run's session is minted by the spawn and written into the run directory before
/// the agent starts, so there is nothing to find. A 1.x run's has to be found:
///
/// Every 1.x run gets its own server, but not its own session store: whichever port it is
/// asked on, `GET /session` answers out of the store OpenCode keeps per project — every
/// agent that has worked in this checkout or a worktree of it — most recently touched
/// first, and cut off at a hundred rows unless the fetch asks for more. So the fetch asks
/// for both halves of the narrowing the server can do (`sessionPath`): this run's
/// directory, which it matches exactly, and a limit one checkout's history does not
/// reach. Otherwise a busier neighbour holds every row of the answer and this run's
/// session is not in it at all.
///
/// A 1.x run is matched to its session the only way that is exact — by the prompt.
/// `candidates` narrows the list to sessions that could be this run's, and `isOurs`
/// confirms one against the prompt the applet staged.
///
/// ## Loopback, and who may ask
///
/// Both servers bind `127.0.0.1`. A 1.x run's is NOT password-protected, and that is
/// forced rather than chosen: OpenCode's server does support a password, but its own TUI
/// sends none, so a run started with `OPENCODE_SERVER_PASSWORD` set exits on
/// `Unauthorized` before doing any work (verified against 1.4.3). So the port is
/// reachable by any other user on the machine, and driving it runs commands as this user.
/// The 2.x service takes HTTP Basic auth, with the password it writes into its own
/// owner-only discovery file (`serviceEndpoint`).
///
/// Only the decisions live here. Dialling either server is the platform's job
/// (`OpenCodeProbe` on macOS), because this library is built for Linux too, where
/// URLSession is a separate module this target does not take.
public enum OpenCodeAPI {
    /// The interface a 1.x run's server binds — OpenCode's own default, restated because
    /// it is also the address the probe dials.
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

    /// What a whole session spent, from the messages the CLI's export returns
    /// (`exportArguments`). Both majors carry a message's `tokens` in one shape — under
    /// `info` in 1.x, at the message's top level in 2.x — so one sum reads either.
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

    /// The CLI arguments that print a finished session with every message it holds:
    /// `export <id>` on 1.x, `session export <id>` on 2.x, which has no top-level
    /// `export` — and 1.x has no `session export`, so the major decides, never a guess.
    public static func exportArguments(sessionID: String, service: Bool) -> [String] {
        service ? ["session", "export", sessionID] : ["export", sessionID]
    }

    // MARK: - OpenCode 2.x: the shared service

    /// Whether `opencode --version` printed a 2.x-or-later version, which is what decides
    /// every fork between the two shapes above.
    ///
    /// The first `major.minor.patch` in the output is the version — 2.x prints
    /// `opencode v2.0.18`, 1.x a bare `1.4.3`. Output this cannot read a version out of is
    /// 1.x: that is the shape every spawn had before 2.0, so a failed probe costs a 2.x
    /// install its runs rather than moving a 1.x one off the only command it accepts.
    public static func isServiceVersion(_ output: String) -> Bool {
        guard let pattern = try? NSRegularExpression(pattern: "[0-9]+\\.[0-9]+\\.[0-9]+"),
              let hit = pattern.firstMatch(in: output,
                                           range: NSRange(output.startIndex..., in: output)),
              let range = Range(hit.range, in: output),
              let major = Int(output[range].prefix { $0 != "." }) else { return false }
        return major >= 2
    }

    /// A fresh id for the session a 2.x spawn creates: `ses_diplomat_` and 32 hex digits.
    ///
    /// The id is the caller's to choose — the service's only rule is the `ses` prefix — and
    /// choosing it is what lets the run be bound to its session before the agent starts.
    /// It must be fresh per spawn: creating a session under an id that already exists
    /// answers with the existing one, and the run would attach to another run's work.
    public static func newSessionID() -> String {
        "ses_diplomat_" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
    }

    /// A model pin as the service's session body names one: `provider/model` split at the
    /// FIRST `/`, so an OpenRouter id keeps its own path, and a `#variant` after the last
    /// `#` of the rest — how OpenCode 2.x reads a `provider/model#variant` reference. A
    /// pin with no `/` is all provider and no model: the service creates the session
    /// anyway, and it is the turn that then fails, in the run's own window. nil for no pin.
    public static func modelRef(_ pin: String) -> [String: String]? {
        let pin = pin.trimmingCharacters(in: .whitespaces)
        guard !pin.isEmpty else { return nil }
        guard let slash = pin.firstIndex(of: "/") else { return ["providerID": pin, "id": ""] }
        var ref = ["providerID": String(pin[..<slash])]
        let rest = pin[pin.index(after: slash)...]
        if let hash = rest.lastIndex(of: "#") {
            ref["id"] = String(rest[..<hash])
            ref["variant"] = String(rest[rest.index(after: hash)...])
        } else {
            ref["id"] = String(rest)
        }
        return ref
    }

    /// The permission ruleset a spawned 2.x session is created with: everything allowed.
    ///
    /// The 2.x counterpart of the grant `AgentRunner.permissionValue` carries to a 1.x run,
    /// passed at creation because the service is where 2.x keeps permissions — an
    /// environment variable on the TUI reaches a client that decides nothing. An agent that
    /// stops to ask in a window nobody is watching never finishes.
    public static let allowAll: [[String: String]] = [
        ["action": "*", "resource": "*", "effect": "allow"],
    ]

    /// The body `opencode api session.create` is handed for a spawn: its id, the directory
    /// the agent works in, the allow-all ruleset, and a model only when one is pinned —
    /// unpinned, the service starts on the one its config names, which is the model the
    /// tag then says (`AgentModel`).
    public static func sessionBody(id: String, directory: String,
                                   model: String) -> [String: Any] {
        var body: [String: Any] = ["id": id, "location": ["directory": directory],
                                   "permissions": allowAll]
        if let ref = modelRef(model) { body["model"] = ref }
        return body
    }

    /// The body `opencode api session.prompt` is handed: the staged prompt, verbatim.
    public static func promptBody(_ text: String) -> [String: Any] { ["text": text] }

    /// Where the service says where it is: `$XDG_STATE_HOME/opencode/service.json`, else
    /// `~/.local/state/opencode/service.json` — the file a 2.x client writes when it
    /// starts the service, and reads to find it.
    public static func serviceFile(environment: [String: String], home: URL) -> URL {
        let root = environment["XDG_STATE_HOME"].flatMap { $0.isEmpty ? nil : $0 }
            .map { URL(fileURLWithPath: $0) }
            ?? home.appendingPathComponent(".local/state")
        return root.appendingPathComponent("opencode/service.json")
    }

    /// Where the service answers and how to be let in.
    public struct ServiceEndpoint {
        /// `http://127.0.0.1:<port>`, with no trailing slash.
        public let base: String
        /// The `Authorization` header every `/api` route wants, or nil for a service
        /// that wrote no password.
        public let authorization: String?
    }

    /// The endpoint in a service file's contents, or nil for one that names no URL.
    ///
    /// The password is HTTP Basic under the fixed user `opencode`. Without it every `/api`
    /// route answers 401, which the probe reads the way it reads any other failure.
    public static func serviceEndpoint(_ data: Data) -> ServiceEndpoint? {
        guard let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              var base = obj["url"] as? String, !base.isEmpty else { return nil }
        while base.hasSuffix("/") { base.removeLast() }
        let auth = (obj["password"] as? String).map {
            "Basic " + Data("opencode:\($0)".utf8).base64EncodedString()
        }
        return ServiceEndpoint(base: base, authorization: auth)
    }

    /// The service's path for one session, or for one of its actions (`suffix`
    /// `/interrupt`). nil for an id no path can carry.
    public static func servicePath(sessionID: String, suffix: String = "") -> String? {
        guard !sessionID.isEmpty,
              let id = sessionID.addingPercentEncoding(withAllowedCharacters: unescapedInID)
        else { return nil }
        return "/api/session/\(id)\(suffix)"
    }

    /// Every session with a turn in flight, across the whole service.
    public static let serviceActivePath = "/api/session/active"

    /// The service's path for a session's opening message: the oldest one, alone.
    public static func serviceOpeningPath(sessionID: String) -> String? {
        servicePath(sessionID: sessionID, suffix: "/message?limit=1&order=asc")
    }

    /// The text of a session's opening message, from `serviceOpeningPath`'s whole decoded
    /// answer — `{"data": [{"type": "user", "text": …}]}` on 2.0.18 — or nil for anything
    /// else, an empty list included: that is a session created and not yet prompted.
    public static func openingText(_ response: Any?) -> String? {
        guard let first = ((response as? [String: Any])?["data"] as? [[String: Any]])?.first,
              first["type"] as? String == "user" else { return nil }
        return first["text"] as? String
    }

    /// The session a 2.x TUI's command line attaches to — `--session` and then a
    /// `ses`-prefixed id — or nil for any other command line. The id stops at the first
    /// character no id has, so the shells wrapping the TUI, whose command lines still
    /// carry the quote that closed it, name the same session rather than a garbled one.
    ///
    /// A 2.x agent's argv carries this and no prompt: the prompt went to the service
    /// through `opencode api` before the TUI started (`AgentRunner.serviceCommand`). So
    /// this is what the process-table scan finds such an agent by, and the prompt it
    /// matches is asked of the service (`openingText`).
    public static func attachedSession(_ commandLine: String) -> String? {
        guard let pattern = try? NSRegularExpression(pattern: "--session\\s+(ses[0-9A-Za-z_-]*)"),
              let hit = pattern.firstMatch(in: commandLine,
                                           range: NSRange(commandLine.startIndex...,
                                                          in: commandLine)),
              let range = Range(hit.range(at: 1), in: commandLine) else { return nil }
        return String(commandLine[range])
    }

    /// RFC 3986's unreserved set: a session id is one path segment.
    private static let unescapedInID = CharacterSet(
        charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

    /// Whether a 2.x session's turn is still in flight, from `GET /api/session/<id>` and
    /// `GET /api/session/active` — each passed as the whole decoded response.
    ///
    /// `nil` — "ask the screen instead" — unless both are the `{"data": {…}}` the service
    /// answers with: a service that is down, a 401, a 404 for an unknown id, and the HTML
    /// its web app answers for anything else all land here, and none of them is "idle".
    ///
    /// Busy while the session is in the active map, which holds it continuously from the
    /// start of a turn to its end with no gap between steps (measured on 2.0.18, polled at
    /// 50 ms across a three-tool-call turn). Out of it, idle once `time.idle` is stamped,
    /// which the service does when a turn ends. Busy otherwise: a session created but
    /// whose first turn has not ended — the moment between `session.create` and the
    /// prompt starting a turn included — has not finished anything, and calling it idle
    /// would retire an agent seconds after it launched.
    public static func serviceState(session: Any?, active: Any?,
                                    sessionID: String) -> AgentState.SessionState? {
        guard let info = (session as? [String: Any])?["data"] as? [String: Any],
              let running = (active as? [String: Any])?["data"] as? [String: Any]
        else { return nil }
        if running[sessionID] != nil { return AgentState.SessionState(busy: true) }
        let time = info["time"] as? [String: Any] ?? [:]
        return AgentState.SessionState(busy: (time["idle"] as? NSNumber) == nil)
    }
}
