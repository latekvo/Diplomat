import Foundation
import DiplomatCore

/// Dialling an OpenCode run's server — a 1.x run's own, or the 2.x shared service: the
/// impure half of `OpenCodeAPI`.
///
/// The decisions — which session is this run's, and what its server says of it — live
/// in `DiplomatCore.OpenCodeAPI`, shared with the Linux front-end and pinned by the core
/// smoke. What is here is only the two things a pure library cannot do: take a port, and
/// fetch a URL. They are split because DiplomatCore is built for Linux too, where
/// URLSession is a module this package does not take.
///
/// Every call blocks and every failure is `nil`. Both are deliberate: this runs on the
/// same background sweep as the `ps` and `capture-pane` shell-outs beside it, and a
/// server still starting, a window already closed and a port taken by something that is
/// not OpenCode are all "this run cannot be reached" — whose only useful consequence is
/// to read the screen instead.
///
/// Which server a run is asked through is told by what its spawn staged: a 1.x run has a
/// port, a 2.x run a bound session and none (`AgentRegistry.serviceSession`).
enum OpenCodeProbe {
    /// Which session on this run's server is this run's, by its opening prompt.
    ///
    /// Every run has its own server but they share one session store, so the port alone
    /// narrows nothing — the same shared history answers whichever port it is asked on.
    /// The directory narrows it to this checkout, and the server does that much itself
    /// (`sessions(port:directory:)`). The prompt is what makes the match exact, and exact
    /// is worth the fetch: the applet runs several agents in one checkout at a time, so two
    /// sessions a second apart in the same directory is the ordinary case, not the
    /// pathological one.
    ///
    /// A run with no port serves nothing to ask, so it never matches here — a 1.x run the
    /// spawn could not reserve one for, or a 2.x run, bound at spawn or by `adopt`.
    static func bind(_ r: AgentState.RunRecord, directory: String,
                     taken: Set<String>) -> String {
        guard let port = AgentRegistry.port(r.runID),
              let prompt = try? String(contentsOf: AgentRegistry.promptPath(r.runID),
                                       encoding: .utf8),
              let listing = sessions(port: port, directory: directory) else { return "" }
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
    /// A bound run with no port is a 2.x run, asked of the shared service
    /// (`serviceState`). A 1.x run is asked both halves of the answer — see
    /// `OpenCodeAPI.stateOf` for which blind spot each of them covers.
    static func state(_ r: AgentState.RunRecord, sessionID: String,
                      pass: ServicePass) -> AgentState.SessionState? {
        guard let port = AgentRegistry.port(r.runID) else {
            return serviceState(sessionID: sessionID, pass: pass)
        }
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

    /// That directory's sessions, most recently touched first, as this run's server
    /// reports them.
    ///
    /// The filter is the server's own and is the same comparison `OpenCodeAPI.candidates`
    /// makes over the answer — exact string equality on the session's directory — so it
    /// changes nothing about which sessions match, only how many rows the answer has to
    /// hold them.
    ///
    /// Ordered by last touch, so what `OpenCodeAPI.sessionLimit` cuts is the least
    /// recently touched — every one of which a session created since the run was
    /// dispatched outranks.
    ///
    /// It has to be a directory OpenCode has worked in: one it has no project or sandbox
    /// for answers empty however many sessions the store holds against it — a checkout
    /// deleted since, say. A run's own never is, because the server being asked is the
    /// one running in it.
    static func sessions(port: Int, directory: String) -> [[String: Any]]? {
        guard let path = OpenCodeAPI.sessionPath(directory: directory) else { return nil }
        return get(port: port, path: path)
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
        return send(URLRequest(url: url, timeoutInterval: OpenCodeAPI.timeout),
                    requireOK: false) as? T
    }

    // MARK: - OpenCode 2.x: the shared service

    /// One probe pass's view of the service: the discovery file is read and the active map
    /// fetched at most once per pass, not once per run.
    final class ServicePass {
        private let find: () -> OpenCodeAPI.ServiceEndpoint?
        private let get: (OpenCodeAPI.ServiceEndpoint, String) -> Any?
        private var endpointRead = false
        private var endpoint: OpenCodeAPI.ServiceEndpoint?
        private var activeFetched = false
        private var active: Any?

        /// `find` and `get` are the self-test's seams.
        init(find: @escaping () -> OpenCodeAPI.ServiceEndpoint? = { OpenCodeProbe.service() },
             get: @escaping (OpenCodeAPI.ServiceEndpoint, String) -> Any? = {
                 OpenCodeProbe.call($0, $1)
             }) {
            self.find = find
            self.get = get
        }

        func fetch(_ path: String) -> Any? {
            if !endpointRead { endpoint = find(); endpointRead = true }
            return endpoint.flatMap { get($0, path) }
        }

        func activeMap() -> Any? {
            if !activeFetched {
                active = fetch(OpenCodeAPI.serviceActivePath)
                activeFetched = true
            }
            return active
        }
    }

    /// Whether a 2.x session's turn is still in flight (`OpenCodeAPI.serviceState`).
    ///
    /// Without the pass's active map no session is asked for: the answer is already "read
    /// the screen", so a hung service costs a pass one timeout rather than two per run.
    static func serviceState(sessionID: String, pass: ServicePass) -> AgentState.SessionState? {
        guard let active = pass.activeMap(),
              let path = OpenCodeAPI.servicePath(sessionID: sessionID),
              let session = pass.fetch(path) else { return nil }
        return OpenCodeAPI.serviceState(session: session, active: active, sessionID: sessionID)
    }

    /// Stop a 2.x session's turn, best-effort. Returns whether the service answered the
    /// request — which it does for a session with no turn to stop too (2.0.18 answers
    /// `{"interrupted": false}`), so `true` is "delivered", not "something was running".
    ///
    /// Closing a 2.x run's window ends its TUI and nothing else: the turn runs in the
    /// shared service, and one whose TUI was killed mid-turn went on to finish its tool
    /// call twenty seconds later (measured on 2.0.18). So this is what ends a 2.x agent
    /// (`interrupts`).
    @discardableResult
    static func interrupt(sessionID: String) -> Bool {
        guard let service = service(),
              let path = OpenCodeAPI.servicePath(sessionID: sessionID, suffix: "/interrupt")
        else { return false }
        return call(service, path, method: "POST") != nil
    }

    // An opening message never changes, so a hit is kept for the process's life. A miss is
    // kept for `missMemory`: a Diplomat spawn prompts its session before its TUI exists,
    // so a miss is ordinarily a service that did not answer, and re-asking every tick
    // costs a timeout per TUI.
    private static let openingLock = NSLock()
    private static var openings: [String: String] = [:]
    private static var misses: [String: TimeInterval] = [:]

    /// How long a session the service gave no opening prompt for goes unasked.
    static let missMemory: TimeInterval = 30

    /// The prompt a 2.x session was opened with, or nil when the service cannot say.
    ///
    /// What the process-table scan matches a 2.x agent by, whose own command line names
    /// only its session (`OpenCodeAPI.attachedSession`).
    ///
    /// `now` and `fetch` are the sweep self-test's seams.
    static func openingPrompt(sessionID: String,
                              now: TimeInterval = Date().timeIntervalSince1970,
                              fetch: (String) -> String? = fetchOpening) -> String? {
        openingLock.lock()
        let known = openings[sessionID]
        let missed = misses[sessionID]
        openingLock.unlock()
        if let known { return known }
        if let missed, now - missed < missMemory { return nil }
        let text = fetch(sessionID)
        openingLock.lock()
        if let text {
            openings[sessionID] = text
            misses[sessionID] = nil
        } else {
            misses = misses.filter { now - $0.value < missMemory }
            misses[sessionID] = now
        }
        openingLock.unlock()
        return text
    }

    private static func fetchOpening(_ sessionID: String) -> String? {
        guard let service = service(),
              let path = OpenCodeAPI.serviceOpeningPath(sessionID: sessionID) else { return nil }
        return OpenCodeAPI.openingText(call(service, path))
    }

    // Sessions of untracked runs, which have no run directory to bind one into, by run
    // id. Kept until the run is retired (`forgetAdopted`) so it can still be stopped once
    // its TUI, and so its sighting, is gone.
    private static let adoptedLock = NSLock()
    private static var adopted: [String: String] = [:]

    /// Give runs this applet did not spawn here — mesh-placed, released, or never booked —
    /// the 2.x session the process-table scan found, so they are asked of the service,
    /// priced and interrupted like a spawned run (`serviceSession(of:)`). A spawned run
    /// binds its session at spawn.
    ///
    /// An untracked run has no run directory: it is given the session remembered here -
    /// the one the scan found on its PR, or for a released run, which keeps its agent's
    /// pid, the one at that pid (the PR's sighting prefers any other agent). A mesh-placed
    /// one is bound in its run directory to the first `attached` session no run holds
    /// whose opening prompt is exactly its staged one — `bind`'s exact match — and only
    /// while it has no port (a 1.x run) and no session yet.
    ///
    /// `openingPrompt` is the sweep self-test's seam.
    static func adopt(_ records: [AgentState.RunRecord], sessions: [Int: String],
                      byPID: [Int: String], attached: [String],
                      openingPrompt: (String) -> String? = {
                          OpenCodeProbe.openingPrompt(sessionID: $0)
                      }) {
        var taken = Set(records.map { AgentRegistry.boundSession($0.runID) }
            .filter { !$0.isEmpty })
        for r in records {
            if r.untracked {
                let session = r.pid == nil ? r.prNumber.flatMap { sessions[$0] }
                    : r.pid.flatMap { byPID[$0] }
                guard let session else { continue }
                adoptedLock.lock()
                adopted[r.runID] = session
                adoptedLock.unlock()
            } else if r.placement == .meshHere,
                      AgentRegistry.runRunner(r.runID) == AgentRunner.opencode.rawValue,
                      AgentRegistry.port(r.runID) == nil,
                      AgentRegistry.boundSession(r.runID).isEmpty,
                      let prompt = try? String(contentsOf: AgentRegistry.promptPath(r.runID),
                                               encoding: .utf8),
                      let session = attached.first(where: {
                          !taken.contains($0) && openingPrompt($0) == prompt
                      }) {
                AgentRegistry.bindSession(r.runID, session)
                taken.insert(session)
            }
        }
    }

    /// Drop what `adopt` remembered for runs that have been retired: a synthesized run's id
    /// is its PR's, and the next agent seen on that PR is not the one that session was.
    static func forgetAdopted(_ runIDs: Set<String>) {
        adoptedLock.lock()
        for id in runIDs { adopted[id] = nil }
        adoptedLock.unlock()
    }

    /// The 2.x session a run works in, bound or adopted; nil for any other run.
    static func serviceSession(of record: AgentState.RunRecord) -> String? {
        if let bound = AgentRegistry.serviceSession(record.runID) { return bound }
        adoptedLock.lock()
        defer { adoptedLock.unlock() }
        return adopted[record.runID]
    }

    /// The 2.x sessions to interrupt as one tick's ended runs are closed and retired.
    ///
    /// A reaped run always: its window is being closed, and the turn outlives it. Any
    /// other retired run only when no TUI attached to its session is in the process table
    /// — its window was closed by hand or its TUI died, and the turn would run on unseen.
    /// One whose TUI is still up (retired because its PR merged, say) is left working, as
    /// a 1.x or Claude Code agent is. An unreadable process table shows no TUI.
    static func interrupts(reaped: [AgentState.RunRecord], retired: [AgentState.RunRecord],
                           attached: Observation<[String]>) -> [String] {
        let closed = Set(reaped.map(\.runID))
        let live = attached.value ?? []
        var out: [String] = []
        for r in reaped + retired {
            guard let session = serviceSession(of: r), !out.contains(session) else { continue }
            if !closed.contains(r.runID), live.contains(session) { continue }
            out.append(session)
        }
        return out
    }

    /// Where the service is, per its discovery file (`OpenCodeCLI.serviceFile`).
    private static func service() -> OpenCodeAPI.ServiceEndpoint? {
        (try? Data(contentsOf: OpenCodeCLI.serviceFile())).flatMap(OpenCodeAPI.serviceEndpoint)
    }

    /// One request against the service, decoded — nil for anything but a 200 of JSON.
    ///
    /// Unlike a 1.x port, the status matters: every `/api` route answers a 401 without the
    /// password and a 404 for an unknown session, each with a JSON body, and the web app
    /// answers any other path with a 200 of HTML.
    private static func call(_ service: OpenCodeAPI.ServiceEndpoint, _ path: String,
                             method: String = "GET") -> Any? {
        guard let url = URL(string: service.base + path) else { return nil }
        var request = URLRequest(url: url, timeoutInterval: OpenCodeAPI.timeout)
        request.httpMethod = method
        if let auth = service.authorization {
            request.setValue(auth, forHTTPHeaderField: "Authorization")
        }
        return send(request, requireOK: true)
    }

    private static func send(_ request: URLRequest, requireOK: Bool) -> Any? {
        var payload: Any?
        let done = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { data, response, _ in
            defer { done.signal() }
            if requireOK, (response as? HTTPURLResponse)?.statusCode != 200 { return }
            guard let data, data.count <= OpenCodeAPI.maxBytes else { return }
            payload = try? JSONSerialization.jsonObject(with: data)
        }.resume()
        // The request carries its own timeout; the wait is bounded a little wider so a
        // sweep can never park here forever if the task never calls back.
        _ = done.wait(timeout: .now() + OpenCodeAPI.timeout + 2)
        return payload
    }
}
