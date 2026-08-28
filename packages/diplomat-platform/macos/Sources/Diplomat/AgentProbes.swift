import Foundation
import DiplomatCore

/// The impure half of agent-state detection: the outside world, typed. Swift twin of
/// `diplomat_app/probes.py`.
///
/// `AgentState` decides; this is the only thing that looks. Every probe returns an
/// `Observation`, so a failure to look reaches the resolver as a failure to look rather
/// than as an empty answer — which is the whole reason the resolver can refuse to guess.
///
/// The split is also what makes the resolver testable: a scenario is a literal because
/// nothing below is in the call path.
///
/// Nothing here throws. A probe that cannot answer says so and the tick continues; the
/// worst outcome of a broken probe is rows that read "unknown" and a cap that holds its
/// bays, never an agent declared finished.
enum AgentProbes {
    /// How long a probe's answer is reused. The resolver re-runs for every question the
    /// applet asks — deliberately, so no answer is ever stale — which makes THIS the only
    /// place the cost is paid. Short enough that a poll never acts on an old machine, long
    /// enough that one cycle spawns one `ps` and one AppleScript dump rather than a dozen.
    static let cacheSecs: TimeInterval = 5

    private static let lock = NSLock()
    private static var psCache: (at: TimeInterval, dump: Observation<String>)?
    private static var pinnedPS: Observation<String>?
    private static var tailsCache: (at: TimeInterval, answer: Observation<[String: String]>)?
    private static var sessionsCache: (at: TimeInterval, runs: Set<String>,
                                       answer: Observation<[String: AgentState.SessionState]>)?

    /// One probe's standing: what it last said, and for how long.
    ///
    /// A probe that goes quiet is the failure mode with no symptom of its own — the applet
    /// keeps drawing rows and simply believes something untrue — so the fact is kept and
    /// reported rather than left to be inferred from behaviour.
    struct Health {
        var name: String
        var reason = ""
        /// `false` while the probe is answering; the count is what it is failing on.
        var failing = false
        var consecutiveFailures = 0

        /// Has this probe failed for long enough to be worth saying out loud?
        ///
        /// Only `.unavailable` counts. A machine without a mesh add-on, or with no run that
        /// serves a session, is an ordinary machine, and warning about it every few minutes
        /// would be noise that trains the operator to ignore the channel.
        var silent: Bool { failing && consecutiveFailures >= Health.silentAfter }

        /// Consecutive failed ticks before a probe is called silent. The panel resolves
        /// every few seconds, so this is under a minute — long enough that a revoked-then-
        /// regranted automation permission or a momentary `ps` failure passes unremarked.
        static let silentAfter = 10
    }

    private static var healthByName: [String: Health] = [:]

    /// How many agent screens have been read, and how many of them showed a CLI's
    /// interrupt hint. The hints are literal strings from someone else's UI
    /// (`AgentActivity.busyMarkers`), and if they ever stop matching, every agent reads as
    /// idle at once: the cap empties and the monitors burst. Nothing else would say so —
    /// the applet would look like it was working perfectly — so the ratio is counted.
    private static var tailsRead = 0
    private static var markerSeen = 0

    /// Every probe's standing, in a stable order.
    static func health() -> [Health] {
        lock.lock(); defer { lock.unlock() }
        return healthByName.keys.sorted().compactMap { healthByName[$0] }
    }

    /// `(screens read, screens that showed the interrupt hint)`.
    static func markerStats() -> (read: Int, seen: Int) {
        lock.lock(); defer { lock.unlock() }
        return (tailsRead, markerSeen)
    }

    /// Stand in for what this machine's `ps` says — for a self-test whose fixture
    /// controls the run book but not the box it is running on.
    ///
    /// Emptying the registry is not enough to control the answer, because neither the cap
    /// load nor the row list is a fold over records alone: `AgentState.synthesizeUntracked`
    /// turns every agent this scan finds with no record of its own into an occupying
    /// `untracked:<pr>` one. So on the machine the applet is developed on, whose ordinary
    /// state is several agents up, an assertion about which bays a placement spends could
    /// not pass — and read as a regression in the very accounting it exists to catch.
    ///
    /// `.present("")` is the honest fixture for that: a machine that WAS looked at and had
    /// nothing on it, which is a different answer from a scan that failed and one the
    /// resolver already distinguishes. `nil` — every path but a self-test — is the machine
    /// itself.
    ///
    /// Deliberately outside `resetCache`: the caches are this machine, and dropping them is
    /// how a self-test asks to look at it again. This is the fixture standing in its place,
    /// and it outlives every such look.
    static func pinDump(_ dump: Observation<String>?) {
        lock.lock(); defer { lock.unlock() }
        pinnedPS = dump
    }

    /// Drop every probe cache and every counter — for self-tests that change the machine
    /// between assertions inside one cache window.
    static func resetCache() {
        lock.lock(); defer { lock.unlock() }
        psCache = nil
        tailsCache = nil
        sessionsCache = nil
        tailsRead = 0
        markerSeen = 0
        healthByName.removeAll()
    }

    /// Record how a probe answered, and pass the answer through.
    private static func note<T>(_ name: String, _ obs: Observation<T>) -> Observation<T> {
        lock.lock(); defer { lock.unlock() }
        var h = healthByName[name] ?? Health(name: name)
        h.reason = obs.reason
        switch obs {
        case .present:
            h.failing = false
            h.consecutiveFailures = 0
        case .unavailable:
            h.failing = true
            h.consecutiveFailures += 1
        case .unsupported:
            h.failing = false
            h.consecutiveFailures = 0
        }
        healthByName[name] = h
        return obs
    }

    // MARK: - The process table

    /// One `ps` pass, briefly cached, as `pid tty etime args…` lines.
    ///
    /// `etime` rather than Linux's `etimes`: BSD `ps` has no whole-seconds keyword and
    /// exits non-zero with `keyword not found` when asked for one, so every tick would
    /// resolve `unavailable` and every local run would sit at `unknown` holding its bay.
    private static func psDump(now: TimeInterval) -> Observation<String> {
        lock.lock()
        if let pinned = pinnedPS {
            lock.unlock()
            return pinned
        }
        if let cached = psCache, now - cached.at < cacheSecs {
            lock.unlock()
            return cached.dump
        }
        lock.unlock()
        let out = run("/bin/ps", ["-axo", "pid=,tty=,etime=,command="])
        let obs: Observation<String> = out.map(Observation.present)
            ?? .unavailable("could not be read")
        lock.lock()
        psCache = (now, obs)
        lock.unlock()
        return obs
    }

    /// pid → what the process table says about it.
    ///
    /// `etime` is what the resolver's pid-adoption guard compares against a run's age; the
    /// argv decides `isAgent`, using the same loose "the line mentions a runner's CLI" test
    /// the legacy scan used, because a wrapper shell and an agent both carrying the word is
    /// exactly what the age half of the guard is for.
    static func processTable(_ dump: Observation<String>) -> Observation<[Int: AgentState.ProcInfo]> {
        guard let text = dump.value else { return .unavailable(dump.reason) }
        var table: [Int: AgentState.ProcInfo] = [:]
        for line in text.split(separator: "\n") {
            guard let cols = columns(String(line)), let pid = Int(cols.pid),
                  let elapsed = parseElapsed(cols.elapsed) else { continue }
            table[pid] = AgentState.ProcInfo(tty: cols.tty, elapsed: elapsed,
                                             isAgent: AgentRunner.isAgentLine(cols.args))
        }
        return .present(table)
    }

    /// PR number → the tty of an agent visible in `ps` by its prompt text.
    ///
    /// The pre-registry identity mechanism, kept for the two questions a pid cannot answer:
    /// agents this applet has no record of at all (`AgentState.synthesizeUntracked`), and
    /// records whose agent has no pid to match — a placement the mesh routed back here,
    /// where the NODE opened the terminal and wrote the pid file into a run directory this
    /// applet never created.
    ///
    /// It cannot tell two runs on one PR apart and it matches any session that merely
    /// mentions the number, which is why it decides nothing that a pid can decide.
    ///
    /// The tty rides along because it is the only handle such an agent has: without it
    /// nothing can read its screen, so it would count as working until its window closed
    /// however long ago it finished. First sighting of a PR wins — a set of PR numbers is
    /// all this scan can honestly produce.
    static func liveAgents(_ dump: Observation<String>, owner: String,
                           repo: String) -> Observation<[Int: String]> {
        guard let text = dump.value else { return .unavailable(dump.reason) }
        guard let re = try? NSRegularExpression(
            pattern: "PR #(\\d+) in \(NSRegularExpression.escapedPattern(for: "\(owner)/\(repo)"))")
        else { return .unavailable("the prompt pattern would not compile") }
        var out: [Int: String] = [:]
        for line in text.split(separator: "\n") {
            let s = String(line)
            // Only a real agent process carries the phrase: the spawning shell's argv
            // holds the unexpanded `$(cat …)`, not the prompt text.
            guard AgentRunner.isAgentLine(s), let cols = columns(s) else { continue }
            for m in re.matches(in: cols.args, range: NSRange(cols.args.startIndex...,
                                                              in: cols.args)) {
                guard let r = Range(m.range(at: 1), in: cols.args),
                      let pr = Int(cols.args[r]) else { continue }
                if out[pr] == nil { out[pr] = cols.tty }
            }
        }
        return .present(out)
    }

    /// One `ps` line as its four columns. The command is whatever is left, so a path with
    /// spaces in it stays whole.
    private static func columns(_ line: String) -> (pid: String, tty: String, elapsed: String,
                                                    args: String)? {
        let parts = line.split(separator: " ", maxSplits: 3,
                               omittingEmptySubsequences: true).map(String.init)
        guard parts.count == 4 else { return nil }
        // "??" is `ps` for "no controlling terminal", which is not a tty a screen can be
        // read from — the empty string the resolver already treats as "no screen".
        return (parts[0], parts[1] == "??" ? "" : parts[1], parts[2], parts[3])
    }

    /// Parse `ps` etime (`[[dd-]hh:]mm:ss`) into seconds; nil when malformed. `etime` is
    /// used (not `lstart`) because its form is locale-independent.
    static func parseElapsed(_ s: String) -> TimeInterval? {
        let dayParts = s.split(separator: "-", omittingEmptySubsequences: false)
        guard dayParts.count <= 2 else { return nil }
        let days = dayParts.count == 2 ? Int(dayParts[0]) : 0
        guard let days else { return nil }
        let clock = dayParts.count == 2 ? dayParts[1] : dayParts[0]
        let fields = clock.split(separator: ":", omittingEmptySubsequences: false)
        guard (1...3).contains(fields.count) else { return nil }
        var secs = 0
        for f in fields {
            guard let v = Int(f), v >= 0 else { return nil }
            secs = secs * 60 + v
        }
        return TimeInterval(days * 86_400 + secs)
    }

    // MARK: - Screens

    /// tty → the visible buffer of the terminal session on it.
    ///
    /// One AppleScript dump of every open session, shared with the API-error scan through
    /// `ApiErrorWatcher`'s own cache, so asking costs no extra AppleEvent traffic.
    ///
    /// A dump that FAILED is `.unavailable`, never an empty map: automation permission can
    /// be revoked and an AppleEvent can time out, and read as "we looked and found no
    /// sessions" either one would take a bay back from every live agent at once.
    ///
    /// The marker tally counts only the screens belonging to a run this applet is
    /// tracking. The dump carries every terminal window on the machine, and the ratio it
    /// is kept for is "how often does an AGENT's status bar show its interrupt hint" —
    /// counting the operator's own shells would bury the answer under windows that could
    /// never have shown one.
    static func paneTails(_ records: [AgentState.RunRecord], unbooked: [String],
                          now: TimeInterval) -> Observation<[String: String]> {
        lock.lock()
        if let cached = tailsCache, now - cached.at < cacheSecs {
            lock.unlock()
            return cached.answer
        }
        lock.unlock()
        let answer: Observation<[String: String]>
        if let sessions = ApiErrorWatcher.dumpSessionsCached() {
            var tails: [String: String] = [:]
            for s in sessions {
                let key = shortTTY(s.tty)
                guard !key.isEmpty, tails[key] == nil else { continue }
                tails[key] = s.tail
            }
            let wanted = records.map { (tty: $0.tty, pid: $0.pid) }
                + unbooked.map { (tty: $0, pid: Int?.none) }
            if wanted.contains(where: { !$0.tty.isEmpty && tails[$0.tty] == nil }) {
                tails = adoptWrappedTails(wanted, into: tails,
                                          processes: TerminalFocus.processes(),
                                          panes: TerminalFocus.panes(),
                                          clients: TerminalFocus.clients())
            }
            let ours = Set(records.map(\.tty)).subtracting([""]).compactMap { tails[$0] }
            lock.lock()
            tailsRead += ours.count
            markerSeen += ours.filter(AgentActivity.looksBusy).count
            lock.unlock()
            answer = .present(tails)
        } else {
            answer = .unavailable("are unreadable (the terminals would not answer)")
        }
        lock.lock()
        tailsCache = (now, answer)
        lock.unlock()
        return answer
    }

    /// Key each run's own tty to the tail of whatever terminal session is really showing
    /// it, for the runs sitting behind a wrapper.
    ///
    /// A terminal reports the tty it opened; `ps` reports the one the agent ended up on.
    /// For a run this applet spawned they are the same, because the spawn captures the
    /// window's tty as it opens it. For a run it did not — one a peer's node started, one
    /// the operator opened by hand — the only tty known is the agent's own, read out of
    /// the process table, and on a box whose shells wrap themselves in tmux that tty
    /// belongs to no window at all. Its screen is then unreadable for as long as the
    /// session lives, which resolves to RUNNING, and RUNNING holds a bay of the
    /// automatic-task cap: one finished agent nobody dispatched starves the machine.
    ///
    /// The route out is the walk a clicked row already takes (`TerminalFocus`), asked of
    /// the screen rather than the window. It is the same question, and asking it twice in
    /// two ways is how two answers drift.
    ///
    /// Tails are read from the dump rather than from the result, so a run can only adopt a
    /// screen a terminal actually reported — never one another run just adopted.
    /// The ttys of live agents no record covers — the untracked runs `AgentState.tick`
    /// will invent once this evidence is resolved.
    ///
    /// Gathered HERE because the probe is the only place their tty is known before they
    /// exist: an untracked run is synthesized from this same scan a step later, by which
    /// time the screens have already been read. Left out, the one kind of run whose tty
    /// is always the agent's own — the kind the walk above exists for — is the one kind
    /// never walked, and it holds its bay of the cap for the life of its session.
    ///
    /// Selected by `synthesizeUntracked`'s own rule, on PR number, so these are exactly
    /// the ttys the records about to be made will carry.
    static func unbookedTTYs(_ records: [AgentState.RunRecord],
                             _ liveAgents: Observation<[Int: String]>) -> [String] {
        guard let scan = liveAgents.value else { return [] }
        let known = Set(records.compactMap(\.prNumber))
        return scan.filter { !known.contains($0.key) }.map(\.value)
    }

    static func adoptWrappedTails(_ agents: [(tty: String, pid: Int?)],
                                  into tails: [String: String],
                                  processes: [Int: TerminalFocus.Proc],
                                  panes: [String: TerminalFocus.Pane],
                                  clients: [String: String]) -> [String: String] {
        var out = tails
        for a in agents where !a.tty.isEmpty && out[a.tty] == nil {
            let walk = TerminalFocus.walk(tty: a.tty, pid: a.pid, processes: processes,
                                          panes: panes, clients: clients)
            if let tail = walk.ttys.lazy.compactMap({ tails[$0] }).first { out[a.tty] = tail }
        }
        return out
    }

    /// One spelling of a tty, because the two sources that have to meet on it do not
    /// agree: `ps` reports a bare device name and the terminal apps report the full
    /// `/dev/…` path. A record's tty is whatever `ps` calls it, so that is the spelling
    /// everything is keyed by.
    static func shortTTY(_ raw: String) -> String {
        let t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return t.hasPrefix("/dev/") ? String(t.dropFirst(5)) : t
    }

    // MARK: - The agents' own sessions

    /// run id → what that run's own agent says it is doing.
    ///
    /// Positive evidence where the screen gives an inference: a turn the runner itself
    /// marks finished, rather than whether someone else's status bar happened to have its
    /// interrupt hint drawn when we looked.
    ///
    /// A run missing from the answer is a run this cannot reach: every Claude Code run, an
    /// OpenCode run spawned without a port, one whose server has not come up yet, one
    /// whose session has not been written to yet. The resolver reads its screen instead,
    /// so absence here costs the older evidence and never a verdict.
    ///
    /// `.unsupported` when no tracked run has such a session at all — a machine running
    /// Claude Code is an ordinary machine, not one whose probe has gone quiet.
    ///
    /// Cached for the same window as the other probes, and for a sharper reason: this one
    /// dials a socket, and the resolver re-runs for every question the applet asks — the
    /// poll's, the cap's, and one per dispatch. Uncached, a burst of those inside one
    /// window pays a full per-run timeout each for the same unresponsive port.
    static func agentSessions(_ records: [AgentState.RunRecord], directory: String,
                              now: TimeInterval) -> Observation<[String: AgentState.SessionState]> {
        let asking = records.filter { AgentSessionProbe.serves(AgentRegistry.runRunner($0.runID)) }
        guard !asking.isEmpty else {
            return .unsupported("are unavailable (no run serves a session of its own)")
        }
        let key = Set(asking.map(\.runID))
        lock.lock()
        if let cached = sessionsCache, cached.runs == key, now - cached.at < cacheSecs {
            lock.unlock()
            return cached.answer
        }
        lock.unlock()
        let answer = Observation.present(AgentSessionProbe.states(for: asking,
                                                                  directory: directory))
        lock.lock()
        sessionsCache = (now, key, answer)
        lock.unlock()
        return answer
    }

    // MARK: - The mesh

    /// The work keys currently claimed anywhere on the mesh.
    ///
    /// `.unsupported` when no node has ever run here, `.unavailable` when one has and is
    /// not answering — a peer's run must not be retired because the local node is down,
    /// which is the difference the resolver reads.
    static func meshClaims(enabled: Bool, snapshot: MeshSnapshot?) -> Observation<Set<String>> {
        guard enabled else {
            return .unsupported("are unavailable (the mesh is switched off)")
        }
        guard let snapshot else {
            return .unsupported("are unavailable (no mesh node has run here)")
        }
        guard MeshBridge.nodeRunning(snapshot) else {
            return .unavailable("are unavailable (the mesh node is not running)")
        }
        return .present(Set(snapshot.claims.keys))
    }

    // MARK: - GitHub

    /// Which of these PRs GitHub calls MERGED — the one terminal outcome that outranks
    /// anything a process is doing.
    ///
    /// One `gh` call per PR, so this belongs on the slow refresh, not the process poll. A
    /// PR whose probe fails is simply absent from the answer; a partial answer is still
    /// positive evidence about the PRs it covers.
    static func mergedPRs(_ numbers: Set<Int>) async -> Observation<Set<Int>> {
        guard !numbers.isEmpty else { return .present([]) }
        var merged = Set<Int>()
        for n in numbers.sorted() {
            if let state = try? await API.fetchPRState(number: n), state == "MERGED" {
                merged.insert(n)
            }
        }
        return .present(merged)
    }

    // MARK: - One pass

    /// One pass of every cheap probe.
    ///
    /// `merged` is passed in rather than probed here: it costs a `gh` call per PR and
    /// belongs to the slow refresh, so the fast tick carries forward whatever the last one
    /// found (`.unavailable` until the first).
    static func gather(records: [AgentState.RunRecord], now: TimeInterval,
                       owner: String, repo: String, directory: String,
                       meshEnabled: Bool, meshState: MeshSnapshot?,
                       merged: Observation<Set<Int>>) -> AgentState.Evidence {
        let dump = psDump(now: now)
        let table = note("processes", processTable(dump))
        let scan = note("agent scan", liveAgents(dump, owner: owner, repo: repo))
        // Whose screens are worth counting is decided from the process table, not from the
        // records as they arrived: a run's tty lives on its agent process, and a run
        // spawned since the last tick has not adopted one yet.
        let lookedUp = AgentState.adoptTTYs(records, processes: table, liveAgents: scan)
        return AgentState.Evidence(
            processes: table,
            sentinels: note("sentinels", AgentRegistry.sentinels(records)),
            tails: note("screens", paneTails(lookedUp, unbooked: unbookedTTYs(lookedUp, scan),
                                             now: now)),
            claims: note("mesh claims", meshClaims(enabled: meshEnabled, snapshot: meshState)),
            mergedPRs: merged,
            liveAgents: scan,
            sessions: note("agent sessions",
                           agentSessions(records, directory: directory, now: now)),
            activity: note("turn reports", AgentRegistry.activity(records)))
    }

    /// Run a command, returning its stdout — nil on any failure, which every caller reads
    /// as "could not look".
    private static func run(_ path: String, _ arguments: [String]) -> String? {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: path)
        proc.arguments = arguments
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = Pipe()
        do { try proc.run() } catch { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        guard proc.terminationStatus == 0 else { return nil }
        return String(data: data, encoding: .utf8)
    }
}
