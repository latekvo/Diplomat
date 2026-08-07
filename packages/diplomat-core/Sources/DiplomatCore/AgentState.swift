import Foundation

// MARK: - What every dispatched agent is doing right now
//
// Four questions used to be answered four separate times, each from its own subset of
// the same evidence: is this PR in flight, how many bays of the device's cap are full,
// what rows does the panel draw, and which record is retired. Patching one moved the
// bug into the others, and the two front-ends answered all four differently again.
//
// Here they are one function and four projections of its result:
//
//     AgentState.resolve(records:evidence:now:) -> [runID: Resolution]
//     AgentState.inFlight / capLoad / rows / retirable
//
// Everything here is PURE — no clock, no subprocess, no filesystem. The impure half is
// the front-end's probe layer, whose only job is to turn the outside world into an
// `Evidence` bundle. That split is what makes a scenario a literal instead of a machine
// in a particular state.
//
// Python twin: `diplomat_app/agentstate.py`. The scenario table in
// `linux/tests/test_agent_state.py` is fed through both (`test_agent_state_parity.py`,
// via `diplomat-core agent-state`), so the two front-ends cannot drift again. Reason
// strings are compared verbatim, so any text change here needs the same text there.
//
// THE ONE RULE THE WHOLE LADDER IS BUILT TO KEEP: absence of evidence never resolves to
// `.finished`. A run is finished only on positive evidence — its sentinel exists, its
// process was looked for in a table we actually read and was not there, or its mesh
// claim was seen and has since been released. Every other gap resolves to `.unknown`,
// which holds its bay and says so. Reading "I could not look" as "it is gone" is what
// produced already-complete verdicts on agents that were still working.

/// One probe's answer: a value, or a named reason there isn't one.
///
/// The type exists because the two collapse in every collection: an empty session dump
/// means both "no terminal is open" and "automation was refused", and an empty `ps` set
/// means both "no agents" and "the dump would not decode". Callers then cannot degrade
/// differently for the two, so they degrade wrongly for one of them.
public enum Observation<T> {
    case present(T)
    /// A probe that should have answered could not — the terminal refused automation,
    /// the process table would not decode, the mesh node is down.
    case unavailable(String)
    /// This platform has no such probe at all. Never a defect, and never a reason to
    /// warn about a probe having gone silent.
    case unsupported(String)

    public var value: T? {
        if case .present(let v) = self { return v }
        return nil
    }

    public var isPresent: Bool { value != nil }

    /// The wording a resolution reason uses when this observation is why it could not
    /// decide.
    public var reason: String {
        switch self {
        case .present:              return ""
        case .unavailable(let r):   return r
        case .unsupported(let r):   return r
        }
    }
}

public enum AgentState {

    // MARK: - The states a run can be in

    public enum RunState: String, CaseIterable {
        /// The PR landed — terminal, and it outranks whatever the process is doing.
        case merged
        /// Positive evidence the agent ended.
        case finished
        /// Alive, and its screen shows it back at the prompt.
        case awaitingInput = "awaiting_input"
        /// Alive, and either working or unreadable.
        case running
        /// Dispatched so recently that nothing could have observed it yet.
        case starting
        /// The evidence this run turns on was unavailable.
        case unknown
    }

    /// Reading order for the panel, matching `AgentTaskStatus`: a finished outcome
    /// first because it is the only row asking to be read, then the sessions, then the
    /// ones nothing is known about.
    public static let stateOrder: [RunState] = [
        .merged, .finished, .awaitingInput, .running, .starting, .unknown,
    ]

    /// States in which a run still holds a bay of the device's automatic-task cap.
    ///
    /// `.awaitingInput` is deliberately absent. The cap bounds concurrent LOAD, and a
    /// session sitting at its prompt is spending none — left counted, a machine whose
    /// finished windows are all still open defers automatic work indefinitely while
    /// doing nothing. `.unknown` is deliberately present: a bay released on missing
    /// evidence is exactly the burst this cap exists to stop.
    public static let occupying: Set<RunState> = [.running, .starting, .unknown]

    /// States that block a second dispatch onto the same PR — every state that is not
    /// over. Wider than `occupying` by `.awaitingInput`, and the difference is the
    /// point: that session still holds the PR's context and is waiting to be typed at,
    /// so it must not get a second agent beside it even though it has given its bay
    /// back.
    public static let blocking: Set<RunState> = occupying.union([.awaitingInput])

    // MARK: - Timing constants

    /// How long after dispatch a run with no observed process still reads as
    /// `.starting`. The inner shell writes its pid before it execs the agent, but a
    /// terminal emulator, a tmux server and the user's rc all run first. Past this the
    /// run is not called finished — it becomes `.unknown`, because a spawn that never
    /// landed and a pid file we have not read yet look identical from here.
    public static let spawnGrace: TimeInterval = 20

    /// How much younger than its own record a process may be and still be that
    /// record's agent. Pids are recycled, and a run that dispatched an hour ago cannot
    /// be a process that started a minute ago; the slack only absorbs the seconds
    /// between the dispatch stamp and the exec, plus `etime` rounding.
    public static let pidAdoptionSlack: TimeInterval = 30

    /// How long a mesh origination claim may go unseen before the peer's run reads as
    /// over. Same value and same reasoning as `MeshAgentRun.claimSettle`, which this
    /// replaces on both platforms.
    public static let claimSettle: TimeInterval = 45

    // MARK: - Placements and sources

    public enum Placement: String {
        /// This applet opened the terminal.
        case local
        /// The mesh placed the run back on this machine.
        case meshHere = "mesh-here"
        /// The run is a process on somebody else's box.
        case meshPeer = "mesh-peer"
    }

    // MARK: - Inputs

    /// One live process, as the process-table probe reports it.
    public struct ProcInfo: Equatable {
        public var tty: String
        /// Seconds since the process started (`ps` `etime`), for the adoption guard.
        public var elapsed: TimeInterval
        /// Whether its argv still looks like an agent — the second half of the guard,
        /// so a recycled pid belonging to something else can never be adopted.
        public var isAgent: Bool

        public init(tty: String, elapsed: TimeInterval, isAgent: Bool) {
            self.tty = tty
            self.elapsed = elapsed
            self.isAgent = isAgent
        }
    }

    /// One dispatched agent run, as the registry persists it.
    ///
    /// Identity is `runID`, not the PR: two runs on one PR are two records, an applet
    /// restart keeps them both, and nothing has to be inferred from the wording of a
    /// prompt.
    public struct RunRecord: Equatable {
        public var runID: String
        public var dispatchedAt: TimeInterval
        public var prNumber: Int?
        public var prURL: String
        public var kind: String
        public var label: String
        public var source: String
        public var placement: Placement
        public var node: String
        public var workKey: String
        public var ledgerKey: String
        /// The agent's real pid, written by the inner shell before it execs (see
        /// `AgentSpawner`). `nil` until the registry has read the pid file.
        public var pid: Int?
        public var tty: String
        /// When this device last saw the executor's claim for `workKey`, for a
        /// mesh-peer run. `nil` when it has never been seen.
        public var claimSeenAt: TimeInterval?
        /// True for a run nothing dispatched — a live agent found in the process table
        /// with no record behind it. It gets a row and blocks a second dispatch, but
        /// carries no label, no ledger key and no start time.
        public var untracked: Bool

        public init(runID: String, dispatchedAt: TimeInterval, prNumber: Int? = nil,
                    prURL: String = "", kind: String = "", label: String = "",
                    source: String = AgentDispatchGate.Source.auto.rawValue,
                    placement: Placement = .local, node: String = "",
                    workKey: String = "", ledgerKey: String = "", pid: Int? = nil,
                    tty: String = "", claimSeenAt: TimeInterval? = nil,
                    untracked: Bool = false) {
            self.runID = runID
            self.dispatchedAt = dispatchedAt
            self.prNumber = prNumber
            self.prURL = prURL
            self.kind = kind
            self.label = label
            self.source = source
            self.placement = placement
            self.node = node
            self.workKey = workKey
            self.ledgerKey = ledgerKey
            self.pid = pid
            self.tty = tty
            self.claimSeenAt = claimSeenAt
            self.untracked = untracked
        }

        /// Does this run's agent execute on THIS machine? What the device's cap counts
        /// — the cap bounds what this box runs, not what it dispatched.
        public var runsHere: Bool { placement != .meshPeer }
    }

    /// Everything the outside world had to say this tick, each part able to say it had
    /// nothing to say.
    public struct Evidence {
        /// pid → what the process table says about it.
        public var processes: Observation<[Int: ProcInfo]>
        /// The run ids whose completion sentinel exists.
        public var sentinels: Observation<Set<String>>
        /// tty → that session's visible buffer.
        public var tails: Observation<[String: String]>
        /// Work keys currently claimed somewhere on the mesh.
        public var claims: Observation<Set<String>>
        /// PR numbers GitHub reports as MERGED.
        public var mergedPRs: Observation<Set<Int>>
        /// PR number → the tty of an agent found by its prompt text in the process
        /// table. The pre-registry identity mechanism, and still the only evidence
        /// about a run whose terminal this applet did not open — a mesh placement that
        /// landed back here, whose pid file belongs to the node that spawned it.
        public var liveAgents: Observation<[Int: String]>

        /// Defaults are `.unavailable` rather than empty, so a caller that forgets to
        /// wire a probe gets rows reading "unknown" instead of a machine that
        /// confidently believes every agent finished.
        public init(processes: Observation<[Int: ProcInfo]> = .unavailable("not probed"),
                    sentinels: Observation<Set<String>> = .unavailable("not probed"),
                    tails: Observation<[String: String]> = .unavailable("not probed"),
                    claims: Observation<Set<String>> = .unavailable("not probed"),
                    mergedPRs: Observation<Set<Int>> = .unavailable("not probed"),
                    liveAgents: Observation<[Int: String]> = .unavailable("not probed")) {
            self.processes = processes
            self.sentinels = sentinels
            self.tails = tails
            self.claims = claims
            self.mergedPRs = mergedPRs
            self.liveAgents = liveAgents
        }
    }

    // MARK: - Output

    /// What one run resolved to, and the single fact that decided it.
    ///
    /// `reason` is not decoration: it is what the debug dump prints, and it is how a
    /// wrong verdict is diagnosed in one read instead of by re-deriving the ladder by
    /// hand. Every rung writes one.
    public struct Resolution: Equatable {
        public var runID: String
        public var state: RunState
        public var reason: String

        public var occupying: Bool { AgentState.occupying.contains(state) }
    }

    // MARK: - Claim sightings

    /// Refresh each mesh-peer run's claim sighting, returning updated records.
    ///
    /// Split out of `resolve` because it is the one input that is a memory rather than
    /// an observation: absence only becomes evidence relative to when the claim was
    /// last present, so somebody has to remember that. Keeping it a separate pure step
    /// means `resolve` stays a function of its arguments alone.
    ///
    /// An unavailable claim book updates nothing — a node we could not read must not
    /// age out a peer's run.
    public static func observeClaims(_ records: [RunRecord],
                                     claims: Observation<Set<String>>,
                                     now: TimeInterval) -> [RunRecord] {
        guard let live = claims.value else { return records }
        return records.map { r in
            guard r.placement == .meshPeer, !r.workKey.isEmpty,
                  live.contains(r.workKey) else { return r }
            var out = r
            out.claimSeenAt = now
            return out
        }
    }

    // MARK: - The resolver

    /// Every run's state, from one pass of evidence. Pure.
    public static func resolve(records: [RunRecord], evidence: Evidence,
                               now: TimeInterval) -> [String: Resolution] {
        var out: [String: Resolution] = [:]
        for r in records { out[r.runID] = resolveOne(r, evidence: evidence, now: now) }
        return out
    }

    /// One run's state, by a fixed ladder.
    ///
    /// The order is the precedence, and each rung is either positive evidence or an
    /// explicit refusal to guess:
    ///
    /// 1. the PR landed — a terminal outcome that outranks whatever the process is doing;
    /// 2. the completion sentinel exists — the agent returned an exit code;
    /// 3. a mesh-peer run is judged by the executor's claim, because no probe on this
    ///    machine can see a process on another one;
    /// 4. a local run is judged by its pid, and its screen only classifies a pid that is
    ///    already known to be alive.
    public static func resolveOne(_ record: RunRecord, evidence: Evidence,
                                  now: TimeInterval) -> Resolution {
        func done(_ state: RunState, _ reason: String) -> Resolution {
            Resolution(runID: record.runID, state: state, reason: reason)
        }
        if let merged = evidence.mergedPRs.value, let pr = record.prNumber,
           merged.contains(pr) {
            return done(.merged, "PR #\(pr) is merged")
        }
        if let sentinels = evidence.sentinels.value, sentinels.contains(record.runID) {
            return done(.finished, "completion sentinel present")
        }
        if record.placement == .meshPeer {
            return resolvePeer(record, evidence: evidence, now: now, done: done)
        }
        return resolveLocal(record, evidence: evidence, now: now, done: done)
    }

    /// A run on somebody else's machine, judged by the origination lease.
    ///
    /// The executor claims the work key when it spawns the agent and releases it when
    /// the agent exits (szpontnet-spec/docs/12), and the claim is republished in every
    /// snapshot the local node writes. So a claimed key is a running agent — and this
    /// is the only evidence there is, because `ps` on this box structurally cannot see
    /// a process on that one. Judging a peer's run by a local process table, as the
    /// Linux front-end used to, retires every peer run the moment its grace expires.
    private static func resolvePeer(_ record: RunRecord, evidence: Evidence,
                                    now: TimeInterval,
                                    done: (RunState, String) -> Resolution) -> Resolution {
        guard let live = evidence.claims.value else {
            let why = evidence.claims.reason.isEmpty ? "unavailable" : evidence.claims.reason
            return done(.unknown, "mesh claims \(why)")
        }
        if !record.workKey.isEmpty, live.contains(record.workKey) {
            return done(.running, "claim held on \(record.node.isEmpty ? "a peer" : record.node)")
        }
        // Absence is only evidence once it has had time to be evidence. A run whose
        // claim has never been seen counts from its dispatch, which covers both the lag
        // before the first snapshot carries the key and the executor that deduped our
        // dispatch against an agent of its own and so never took a lease at all.
        let since = now - (record.claimSeenAt ?? record.dispatchedAt)
        if since < claimSettle {
            if record.claimSeenAt == nil {
                return done(.starting, "dispatched \(secs(since)) ago, claim not seen yet")
            }
            return done(.running, "claim last seen \(secs(since)) ago")
        }
        return done(.finished, "claim released \(secs(since)) ago")
    }

    /// A run whose agent is a process on this machine.
    ///
    /// The pid is the identity — written by the inner shell before it execs the agent,
    /// so it is the agent's own, not a wrapper's. Matching on it replaces reading
    /// `PR #<n> in <owner>/<repo>` out of a prompt in `ps` output, which could not tell
    /// two runs on one PR apart and matched any unrelated session that mentioned the
    /// number.
    private static func resolveLocal(_ record: RunRecord, evidence: Evidence,
                                     now: TimeInterval,
                                     done: (RunState, String) -> Resolution) -> Resolution {
        guard let table = evidence.processes.value else {
            let why = evidence.processes.reason.isEmpty
                ? "unavailable" : evidence.processes.reason
            return done(.unknown, "process table \(why)")
        }
        let age = now - record.dispatchedAt
        guard let pid = record.pid else {
            // An untracked run IS its process-table sighting, so it has no pid of its
            // own and no dispatch stamp to be young against; it is alive by construction.
            if record.untracked {
                return classifyActivity(record, evidence: evidence, done: done,
                                        aliveReason: "found in process table")
            }
            return resolveWithoutPid(record, evidence: evidence, age: age, done: done)
        }
        guard let proc = table[pid] else {
            return done(.finished, "pid \(pid) absent from the process table")
        }
        if !proc.isAgent {
            return done(.finished, "pid \(pid) was recycled by another process")
        }
        // A recycled pid can also be re-taken by another agent. The genuine one started
        // just after this record did, so anything materially younger is a stranger.
        if proc.elapsed < age - pidAdoptionSlack {
            return done(.finished,
                        "pid \(pid) is \(secs(proc.elapsed)) old but the run is \(secs(age)) old")
        }
        return classifyActivity(record, evidence: evidence, done: done,
                                aliveReason: "pid \(pid) alive")
    }

    /// A run this applet booked but has no pid for.
    ///
    /// Two things produce one. A spawn whose shell has not written its pid file yet —
    /// the ordinary first seconds of a run. And a placement the mesh routed back to
    /// this machine, where the NODE opened the terminal, so the pid file it wrote
    /// belongs to a run directory this applet never created and never will.
    ///
    /// The second is why this rung is not simply "unknown until a pid appears". A
    /// mesh-here run has no pid ever, so that answer would hold its bay and refuse its
    /// PR a fresh agent for the rest of the applet's life — the exact wedge this
    /// module exists to remove, arriving by a different road. Seen in production the
    /// first time the monitors ran: two conflict fixes the mesh placed back here, both
    /// reading "unknown", both bays held, nothing able to retire either.
    ///
    /// So the fallback is the pre-registry evidence: the agent's own prompt in the
    /// process table. It cannot tell two runs on one PR apart, which is exactly why it
    /// is the fallback and not the identity — but "an agent for this PR is up" and "no
    /// agent for this PR is up" are both positive answers, and the second is what
    /// finally ends the run.
    private static func resolveWithoutPid(_ record: RunRecord, evidence: Evidence,
                                          age: TimeInterval,
                                          done: (RunState, String) -> Resolution) -> Resolution {
        guard let live = evidence.liveAgents.value else {
            let why = evidence.liveAgents.reason.isEmpty
                ? "failed" : evidence.liveAgents.reason
            return done(.unknown, "no pid, and the agent scan \(why)")
        }
        if let pr = record.prNumber, live[pr] != nil {
            return classifyActivity(record, evidence: evidence, done: done,
                                    aliveReason: "an agent is up on PR #\(pr)")
        }
        if age <= spawnGrace {
            return done(.starting, "dispatched \(secs(age)) ago, no pid yet")
        }
        guard let pr = record.prNumber else {
            // Nothing to look for: a run with neither a pid nor a PR cannot be found by
            // either mechanism, so its absence is not evidence of anything.
            return done(.unknown, "no pid recorded \(secs(age)) after dispatch")
        }
        return done(.finished, "no agent for PR #\(pr) in the process table")
    }

    /// Working, or finished its turn and waiting at the prompt?
    ///
    /// An agent is spawned into an INTERACTIVE session, so finishing its work is not
    /// exiting: it sits at the prompt until a human closes the window, and the process
    /// table shows the same live agent either way. Its own visible buffer is the only
    /// thing that separates the two.
    ///
    /// Every gap here reads as `.running`, which costs a bay rather than correctness —
    /// but it is also the one rung that fails silently, so the probe layer counts how
    /// often the tail is missing and says so out loud.
    private static func classifyActivity(_ record: RunRecord, evidence: Evidence,
                                         done: (RunState, String) -> Resolution,
                                         aliveReason: String) -> Resolution {
        guard let tails = evidence.tails.value else {
            let why = evidence.tails.reason.isEmpty ? "unavailable" : evidence.tails.reason
            return done(.running, "\(aliveReason); screen \(why)")
        }
        guard !record.tty.isEmpty, let tail = tails[record.tty] else {
            return done(.running,
                        "\(aliveReason); no screen for tty \(record.tty.isEmpty ? "?" : record.tty)")
        }
        if AgentActivity.looksBusy(tail) { return done(.running, "\(aliveReason); working") }
        return done(.awaitingInput, "\(aliveReason); at the prompt")
    }

    /// Seconds as the reason strings spell them, matching Python's `f"{x:.0f}"` so the
    /// parity diff compares text rather than tolerances.
    private static func secs(_ v: TimeInterval) -> String {
        String(format: "%.0fs", v)
    }

    // MARK: - Untracked agents

    /// Fill in each run's tty, from whichever source can reach its agent.
    ///
    /// Nothing tells the applet a run's tty at spawn time — it opens a terminal and walks
    /// away — so the only place it exists is on the agent process itself. Without it a run
    /// has no screen, and no screen means it reads as working from the moment it starts
    /// until the moment its window closes: exactly the "still running" verdict on an agent
    /// that finished hours ago.
    ///
    /// Two sources, because two kinds of run reach their agent differently. A run with a
    /// pid takes the tty off that process, which is exact. A run WITHOUT one — a placement
    /// the mesh routed back here, where the node opened the terminal — has only the prompt
    /// scan, which is looser but is the same evidence that says it is alive at all.
    ///
    /// A tty is adopted once and then left alone: it is a property of the process, and a
    /// process does not change ttys.
    public static func adoptTTYs(_ records: [RunRecord],
                                 processes: Observation<[Int: ProcInfo]>,
                                 liveAgents: Observation<[Int: String]>) -> [RunRecord] {
        let table = processes.value ?? [:]
        let scan = liveAgents.value ?? [:]
        return records.map { r in
            guard r.tty.isEmpty else { return r }
            let found: String
            if let pid = r.pid, let proc = table[pid] {
                found = proc.tty
            } else if let pr = r.prNumber {
                found = scan[pr] ?? ""
            } else {
                found = ""
            }
            guard !found.isEmpty else { return r }
            var out = r
            out.tty = found
            return out
        }
    }

    /// Records for live agents nobody dispatched, so they are deduped against and drawn
    /// rather than merely subtracted from a slot count.
    ///
    /// `liveAgents` maps a PR number to the tty its agent runs on. The tty is what lets
    /// one of these be classified as working or idle at all — without it every untracked
    /// agent would read as running and hold a bay until its window closed, which is the
    /// state the cap exists to prevent.
    ///
    /// Three things produce one: an applet upgraded while agents ran, an agent a peer's
    /// node started on this box, and a session the operator opened by hand. They are
    /// found the old way — the prompt's `PR #<n> in <owner>/<repo>` in the process table
    /// — which is why they are a *fallback* and not the identity mechanism: that scan
    /// cannot tell two runs on one PR apart, so at most one record per PR is made.
    ///
    /// They count as automatic. An agent whose trigger is unknown spending a bay defers
    /// work; the opposite error dispatches a second agent onto a PR that has one.
    public static func synthesizeUntracked(_ records: [RunRecord],
                                           liveAgents: Observation<[Int: String]>,
                                           now: TimeInterval) -> [RunRecord] {
        guard let live = liveAgents.value else { return records }
        let known = Set(records.compactMap { $0.prNumber })
        var out = records
        for pr in Set(live.keys).subtracting(known).sorted() {
            out.append(RunRecord(runID: "untracked:\(pr)", dispatchedAt: now,
                                 prNumber: pr,
                                 source: AgentDispatchGate.Source.auto.rawValue,
                                 placement: .local, tty: live[pr] ?? "",
                                 untracked: true))
        }
        return out
    }

    // MARK: - The four projections
    //
    // Each is a fold over the resolved map. Nothing below re-reads evidence or
    // re-derives a state, which is the whole point: the four answers can disagree with
    // each other only if this file is wrong, not if one of four call sites drifted.

    /// Does this PR already have an agent, for the dispatch gate's dedup?
    public static func inFlight(records: [RunRecord], states: [String: Resolution],
                                prNumber: Int) -> Bool {
        records.contains { r in
            guard r.prNumber == prNumber, let s = states[r.runID] else { return false }
            return blocking.contains(s.state)
        }
    }

    /// The run ids holding a bay of this device's automatic-task cap.
    ///
    /// Counted by where a run EXECUTES and who triggered it: a peer's agent spends the
    /// peer's budget, and a panel click is the operator's own act and spends none of the
    /// automatic one.
    public static func capLoad(records: [RunRecord],
                               states: [String: Resolution]) -> Set<String> {
        Set(records.filter { r in
            guard r.runsHere, r.source == AgentDispatchGate.Source.auto.rawValue,
                  let s = states[r.runID] else { return false }
            return s.occupying
        }.map(\.runID))
    }

    /// Every run the panel draws, in reading order: by state, then oldest first.
    ///
    /// Every run — both sources, both platforms, tracked and not. The front-ends used to
    /// disagree about this (Linux hid panel spawns and drew untracked agents, macOS did
    /// the reverse), which meant the list and the cap were answering different questions.
    public static func rows(records: [RunRecord],
                            states: [String: Resolution]) -> [(RunRecord, Resolution)] {
        let rank = Dictionary(uniqueKeysWithValues: stateOrder.enumerated().map { ($1, $0) })
        return records.compactMap { r in states[r.runID].map { (r, $0) } }
            .sorted { a, b in
                let (ra, rb) = (rank[a.1.state] ?? 0, rank[b.1.state] ?? 0)
                if ra != rb { return ra < rb }
                if a.0.dispatchedAt != b.0.dispatchedAt {
                    return a.0.dispatchedAt < b.0.dispatchedAt
                }
                return a.0.runID < b.0.runID
            }
    }

    /// The runs whose agent has ended — what the registry drops and what the telemetry
    /// ledger prices.
    ///
    /// Only `.merged` and `.finished`, both of which are positive evidence. A record is
    /// never retired by its own age: an hour-long review is an ordinary one, and a clock
    /// that ends records ends them mid-run.
    public static func retirable(records: [RunRecord],
                                 states: [String: Resolution]) -> [RunRecord] {
        records.filter { r in
            guard let s = states[r.runID] else { return false }
            return s.state == .merged || s.state == .finished
        }
    }

    /// Bays of the cap with nothing in them. Clamped, because a lowered cap and
    /// untracked agents can both put more agents on the box than the cap allows.
    public static func freeSlots(limit: Int, occupied: Int) -> Int {
        max(0, limit - occupied)
    }

    // MARK: - One tick

    /// Everything one pass of evidence produced. What a caller reads instead of
    /// re-deriving any of it.
    public struct Tick {
        /// The records as the pipeline left them — claim sightings refreshed,
        /// untracked agents synthesized. The caller persists these.
        public var records: [RunRecord]
        public var states: [String: Resolution]
        public var rows: [(RunRecord, Resolution)]
        public var capLoad: Set<String>
        public var retirable: [RunRecord]
        public var freeSlots: Int

        public func inFlight(prNumber: Int) -> Bool {
            AgentState.inFlight(records: records, states: states, prNumber: prNumber)
        }
    }

    /// Fold one pass of evidence into every answer, in the one order that is correct.
    ///
    /// The order is the reason this is a function rather than a convention each caller
    /// repeats: claims are observed and ttys adopted BEFORE resolving, so both count this
    /// tick rather than a tick late; and untracked agents are synthesized AFTER, so a live
    /// agent that already has a record is not drawn twice — and so it keeps the tty the
    /// scan found it on rather than having one adopted for a pid it does not have. Both
    /// front-ends and the parity CLI go through here, so neither can get the sequence
    /// subtly different from the other.
    public static func tick(records: [RunRecord], evidence: Evidence,
                            now: TimeInterval, limit: Int) -> Tick {
        var recs = observeClaims(records, claims: evidence.claims, now: now)
        recs = adoptTTYs(recs, processes: evidence.processes,
                         liveAgents: evidence.liveAgents)
        recs = synthesizeUntracked(recs, liveAgents: evidence.liveAgents, now: now)
        let states = resolve(records: recs, evidence: evidence, now: now)
        let load = capLoad(records: recs, states: states)
        return Tick(records: recs, states: states,
                    rows: rows(records: recs, states: states),
                    capLoad: load, retirable: retirable(records: recs, states: states),
                    freeSlots: freeSlots(limit: limit, occupied: load.count))
    }
}
