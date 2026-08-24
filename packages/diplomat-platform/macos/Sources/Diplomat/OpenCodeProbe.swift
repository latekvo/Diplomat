import Foundation
import DiplomatCore

/// Dialling an OpenCode run's own server: the impure half of `OpenCodeAPI`.
///
/// The decisions — which session is this run's, and what its last message says — live in
/// `DiplomatCore.OpenCodeAPI`, shared with the Linux front-end and pinned by the core
/// smoke. What is here is only the two things a pure library cannot do: take a port, and
/// fetch a URL. They are split because DiplomatCore is built for Linux too, where
/// URLSession is a module this package does not take.
///
/// Every call blocks and every failure is `nil`. Both are deliberate: this runs on the
/// same background sweep as the `ps` and `capture-pane` shell-outs beside it, and a
/// server still starting, a window already closed and a port taken by something that is
/// not OpenCode are all "this run cannot be reached" — whose only useful consequence is
/// to read the screen instead.
enum OpenCodeProbe {
    /// Which session on this run's server is this run's, by its opening prompt.
    ///
    /// Every run has its own server but they share one session store, so the port alone
    /// narrows nothing — `GET /session` lists the machine's own recent history whichever
    /// port it is asked on. The prompt is what makes the match exact, and exact is worth the
    /// fetch: the applet runs several agents in one checkout at a time, so two sessions a
    /// second apart in the same directory is the ordinary case, not the pathological one.
    ///
    /// A run with no port serves nothing to ask, so it never matches — that is an
    /// OpenCode run the spawn could not reserve one for.
    static func bind(_ r: AgentState.RunRecord, directory: String,
                     taken: Set<String>) -> String {
        guard let port = AgentRegistry.port(r.runID),
              let prompt = try? String(contentsOf: AgentRegistry.promptPath(r.runID),
                                       encoding: .utf8),
              let listing = sessions(port: port) else { return "" }
        let found = OpenCodeAPI.candidates(listing, directory: directory,
                                           sinceMs: r.dispatchedAt * 1000,
                                           taken: taken)
        for sessionID in found.prefix(OpenCodeAPI.maxCandidates) {
            if OpenCodeAPI.isOurs(messages(port: port, sessionID: sessionID) ?? [],
                                  prompt: prompt) {
                return sessionID
            }
        }
        return ""
    }

    /// Whether that session's turn is still in flight.
    ///
    /// Both halves of the answer — see `OpenCodeAPI.stateOf` for which blind spot each
    /// of them covers.
    static func state(_ r: AgentState.RunRecord, sessionID: String) -> AgentState.SessionState? {
        guard let port = AgentRegistry.port(r.runID) else { return nil }
        let running = statuses(port: port).map {
            OpenCodeAPI.isRunning($0, sessionID: sessionID)
        }
        // Nothing is fetched to pair with a status that is not there: the answer is
        // already "ask the screen", and an unreachable port charges a full timeout.
        let last = running == nil ? [] : messages(port: port, sessionID: sessionID, limit: 1) ?? []
        return OpenCodeAPI.stateOf(last, running: running)
    }

    /// A port nothing is listening on, or nil if one cannot be had.
    ///
    /// Taken by binding zero and letting the kernel choose, then closing: the answer is a
    /// port that was genuinely free, rather than one that merely looked free. It can still
    /// be taken in the moment between here and the agent's own bind, and an OpenCode that
    /// cannot bind exits instead of choosing another port — so the caller treats nil and a
    /// lost race the same way, by spawning without a port and reading the screen.
    static func freePort() -> Int? {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        // The same interface the agent will bind, so the port this reports free is free
        // where it has to be.
        addr.sin_addr.s_addr = inet_addr(OpenCodeAPI.host)
        addr.sin_port = 0
        let bound = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                // Qualified: the session-matching `bind` above is the nearer name here.
                Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0 else { return nil }
        var out = sockaddr_in()
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        let named = withUnsafeMutablePointer(to: &out) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                getsockname(fd, $0, &len)
            }
        }
        guard named == 0 else { return nil }
        let port = Int(UInt16(bigEndian: out.sin_port))
        return port > 0 ? port : nil
    }

    /// The machine's recent sessions, newest first, as this run's server reports them.
    ///
    /// A hundred of them, not the whole store (1.4.3) — a bound a run's own session is
    /// always inside, since it is matched within seconds of being created.
    static func sessions(port: Int) -> [[String: Any]]? {
        get(port: port, path: "/session")
    }

    /// What this run's server is working on, session id → its status.
    ///
    /// Scoped to the server asked, not to the machine: unlike `GET /session`, which
    /// answers out of the shared store, this is the live state of the process holding the
    /// port — a run's own turn and its subagents', and nothing another run is doing.
    ///
    /// An idle session is simply absent, so this is a small response whatever the agent is
    /// up to. `nil` when the server could not answer, which is never "idle".
    static func statuses(port: Int) -> [String: Any]? {
        get(port: port, path: "/session/status")
    }

    /// A session's messages, oldest first. `limit` keeps only the last that many.
    ///
    /// The sweep wants one message and the binding wants the first, so both spellings are
    /// here rather than at two call sites: `limit: 1` is what stops a long review's whole
    /// transcript being pulled across on every pass.
    static func messages(port: Int, sessionID: String, limit: Int = 0) -> [[String: Any]]? {
        let suffix = limit > 0 ? "?limit=\(limit)" : ""
        return get(port: port, path: "/session/\(sessionID)/message\(suffix)")
    }

    /// One GET against a run's server, decoded to whatever shape the caller asked for —
    /// an array of objects for the two listings, one object for the statuses.
    ///
    /// A payload of the wrong shape reads as nil, like every other failure: a port the
    /// kernel handed to some other daemon between the reservation and the agent's own
    /// bind answers something, and answering something is not answering this.
    private static func get<T>(port: Int, path: String) -> T? {
        guard let url = URL(string: "http://\(OpenCodeAPI.host):\(port)\(path)") else { return nil }
        var payload: T?
        let done = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: URLRequest(url: url,
                                                    timeoutInterval: OpenCodeAPI.timeout)) { data, _, _ in
            defer { done.signal() }
            guard let data, data.count <= OpenCodeAPI.maxBytes else { return }
            payload = (try? JSONSerialization.jsonObject(with: data)) as? T
        }.resume()
        // The request carries its own timeout; the wait is bounded a little wider so a
        // sweep can never park here forever if the task never calls back.
        _ = done.wait(timeout: .now() + OpenCodeAPI.timeout + 2)
        return payload
    }
}
