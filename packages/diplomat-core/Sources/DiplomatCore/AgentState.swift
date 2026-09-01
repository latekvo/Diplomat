import Foundation

// MARK: - What every dispatched agent is doing right now
//
// Four questions used to be answered four separate times, each from its own subset of
// the same evidence: is this PR in flight, how many bays of the device's cap are full,
// what rows does the panel draw, and which record is retired. Patching one moved the
// bug into the others, and the two front-ends answered all four differently again.
//
// Here they are one function and five projections of its result:
//
//     AgentState.resolve(records:evidence:now:deadline:) -> [runID: Resolution]
//     AgentState.inFlight / capLoad / rows / retirable / reapable
//
// Everything here is PURE — no clock, no subprocess, no filesystem. The impure half is
// the front-end's probe layer, whose only job is to turn the outside world into an
// `Evidence` bundle. That split is what makes a scenario a literal instead of a machine
// in a particular state.
//
// Python twin: `diplomat_runtime/agentstate.py`. The scenario table in
// `linux/tests/test_agent_state.py` is fed through both (`test_agent_state_parity.py`,
// via `diplomat-core agent-state`), so the two front-ends cannot drift again. Reason
// strings are compared verbatim, so any text change here needs the same text there.
//
// THE ONE RULE THE WHOLE LADDER IS BUILT TO KEEP: absence of evidence never resolves to
// `.finished`. A run is finished only on positive evidence — its runner said the turn
// is over, its sentinel exists, its process was looked for in a table we actually read
// and was not there, or its mesh claim was seen and has since been released. Every
// other gap resolves to `.unknown`, which holds its bay and says so. Reading "I could
// not look" as "it is gone" is what produced already-complete verdicts on agents that
// were still working.
//
// The one deliberate exception is `runDeadline`, which ends a run on its age rather than
// on evidence about it — the outermost backstop, for the runs every other rung is
// structurally unable to end. It fires only when a caller passes one (the operator's
// switch, Settings → STALLED AGENTS) and only on a positive reading that the account
// still has tokens to spend.

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

    /// Rank for the panel, matching `AgentTaskStatus`: an outcome, then a local exit,
    /// then the sessions that want a human, then the ones that don't, then the ones
    /// nothing is known about. The two `ended` states head the rank and no front-end
    /// draws a row in one, so the list itself starts at `.awaitingInput`.
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
    /// `ended`. Wider than `occupying` by `.awaitingInput`, and the difference is the
    /// point: that session still holds the PR's context and is waiting to be typed at,
    /// so it must not get a second agent beside it even though it has given its bay
    /// back.
    public static let blocking: Set<RunState> = occupying.union([.awaitingInput])

    /// The states a run is over in, both of them positive evidence.
    ///
    /// The pass that resolves a run into one of these retires it (`retirable`), so both
    /// front-ends leave it out of the list they draw: a row for it would be on screen
    /// for one redraw and gone the next, and which redraw caught it would depend on
    /// when the poll landed. What the run leaves behind is its activity line and its
    /// ledger entry.
    public static let ended: Set<RunState> = [.merged, .finished]

    // MARK: - Timing constants

    /// How long after dispatch a run with no observed process still reads as
    /// `.starting`. The inner shell writes its pid before the agent starts, but a
    /// terminal emulator, a tmux server and the user's rc all run first — and the
    /// process table is one `ps` pass reused for several seconds, so it can predate the
    /// pid file naming what to look for. Past this the run is judged on the evidence
    /// there is: a known pid the table does not hold has ended, while a run that
    /// produced neither a pid nor a PR to scan for becomes `.unknown`, because a spawn
    /// that never landed and a pid file we have not read yet look identical from here.
    public static let spawnGrace: TimeInterval = 20

    /// How long after dispatch a live run whose screen has not shown a turn yet reads
    /// as working rather than as back at its prompt.
    ///
    /// The pid exists as soon as the inner shell runs, but the agent then has to boot,
    /// read its prompt file and draw its first status bar — and until it does, its
    /// screen is the screen of an agent that has FINISHED, the interrupt hint absent
    /// from both. Read as idle there, a run hands its bay straight back to the poll
    /// that started it, and the next dispatch of that poll is seconds behind: a cap of
    /// one, two agents.
    ///
    /// Well past the twelve seconds measured from dispatch to first status bar, because
    /// being too short is that burst while being too long only defers the next task by
    /// seconds — and only for a run whose own report never arrives, since a sentinel, a
    /// merged PR, the CLI's turn report and a runner's session each end one inside this
    /// window untouched.
    public static let firstTurnGrace: TimeInterval = 45

    /// How much younger than its own record a process may be and still be that
    /// record's agent. Pids are recycled, and a run that dispatched an hour ago cannot
    /// be a process that started a minute ago; the slack only absorbs the seconds
    /// between the dispatch stamp and the exec, plus `etime` rounding.
    public static let pidAdoptionSlack: TimeInterval = 30

    /// How long a run's screen may sit perfectly unchanged before it is called over.
    ///
    /// The backstop, for the runs the turn report cannot reach: a runner with no hooks,
    /// a spawn whose settings could not be staged, an agent wedged mid-turn with its
    /// status bar frozen. It is INDEPENDENT of that report rather than derived from it,
    /// which is the only thing that makes it a fallback — a backstop that fails whenever
    /// the primary fails is not one.
    ///
    /// Twenty minutes because a working agent's screen is never still for anywhere near
    /// that long: the CLI redraws a spinner, a token count and an elapsed timer every
    /// second it is thinking, so a pane still for this whole window — its terminal's own
    /// clock aside, see `maskClocks` — means nothing is happening in it. Long enough that
    /// a slow tool call, a long build or a human reading the window is not mistaken for a
    /// dead one.
    public static let quietTimeout: TimeInterval = 20 * 60

    /// How long a run this device executes may go on before it is called over whatever
    /// else the evidence says — the outermost backstop, offered as a switch
    /// (`AppConfig.runDeadline`) rather than applied unconditionally.
    ///
    /// Beneath `quietTimeout` sits the same argument one rung further out. The stillness
    /// clock ends a wedged run by reading its screen, so it ends nothing on a run whose
    /// screen cannot be read — a pane the multiplexer will not dump, a terminal that
    /// refuses automation, a run whose tty was never adopted. Those runs hold a bay until
    /// a human closes the window, and nothing in the ladder above says otherwise.
    ///
    /// Four hours because it has to clear the longest run that is genuinely work and not
    /// a wedge: a swarm review of a large PR, an issue reproduced from scratch, an E2E
    /// sweep. Those run in hours, not in fractions of one, so a deadline in minutes would
    /// retire working agents and this one is deliberately far past anything measured here.
    ///
    /// It is the one rung that ends a run on the CLOCK rather than on evidence about that
    /// run, which is why it is switched off by an operator who would rather a stuck bay
    /// than an early verdict — and why it asks `Evidence.tokensLeft` first. An account
    /// with nothing left to spend parks every agent it has: they sit there accumulating
    /// age while doing no work at all, and reading that as four hours of wedged run would
    /// retire the whole board on the day a limit ran out.
    public static let runDeadline: TimeInterval = 4 * 60 * 60

    /// How long a mesh origination claim may go unseen before the peer's run reads as
    /// over.
    ///
    /// Absence is only evidence once it has had time to be evidence. The claim travels
    /// the executor's link BEFORE the dispatch ack, but reaches a front-end through a
    /// file the node rewrites every couple of seconds, read by a poll of its own — and a
    /// node restart empties the book until its peers re-assert. This window outlasts all
    /// three, and is short enough that a finished run leaves the list while the operator
    /// is still looking at it.
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

    /// What an agent's own session says about it, for a runner that keeps one.
    ///
    /// The typed answer to the question `classifyActivity` otherwise has to read off a
    /// status bar. An OpenCode agent serves its session over loopback while it works
    /// (`OpenCodeAPI`) and a Hermes agent writes its own to SQLite (`HermesStore`);
    /// Claude Code serves nothing, so its runs are absent from the evidence and are
    /// still read from the screen.
    ///
    /// Only the one fact, because it is the only one this evidence can carry honestly:
    /// an OpenCode run's spend is a sum over its whole transcript and the poll reads one
    /// message. A finished run is priced from its runner's own store instead.
    public struct SessionState: Equatable {
        /// Is a turn in flight? Whichever way its runner says so.
        public var busy: Bool

        public init(busy: Bool) {
            self.busy = busy
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
        /// The agent's pid, written by the inner shell before the agent starts (see
        /// `AgentSpawner.shellCommand` for what "the agent's" rests on, and where it is
        /// instead the shell wrapping it). `nil` until the registry has read the file.
        public var pid: Int?
        public var tty: String
        /// When this device last saw the executor's claim for `workKey`, for a
        /// mesh-peer run. `nil` when it has never been seen.
        public var claimSeenAt: TimeInterval?
        /// A digest of this run's screen when it last CHANGED, with the time it
        /// changed. The memory behind `quietTimeout` — absence of motion is only
        /// measurable against when there was last some. Empty/`nil` until its screen
        /// is first read.
        public var quietDigest: String
        public var quietSince: TimeInterval?
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
                    quietDigest: String = "", quietSince: TimeInterval? = nil,
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
            self.quietDigest = quietDigest
            self.quietSince = quietSince
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
        /// run id → what that run's own agent session says about it. Only a runner
        /// that serves one appears here, so a run's absence is ordinary and reads as
        /// "ask the screen" rather than as anything about the run.
        public var sessions: Observation<[String: SessionState]>
        /// run id → `(verb, when)` the run's own CLI last reported for itself, via the
        /// hooks staged into its settings (`AgentCompletion`). The only evidence here
        /// that is a REPORT rather than an observation: everything else in this bundle
        /// is something a probe went and looked at, and this is the agent saying so
        /// itself at the instant it happened. A run is absent when it has reported
        /// nothing yet.
        public var activity: Observation<[String: TurnReport]>
        /// Does the account this device's agents draw on still have room to spend? The
        /// one item here that is about the MACHINE rather than about any run, and the
        /// precondition `runDeadline` turns on. `.unsupported` on a machine whose account
        /// publishes no limit this applet can read, which is an ordinary machine and not
        /// a broken probe.
        public var tokensLeft: Observation<Bool>

        /// Defaults are `.unavailable` rather than empty, so a caller that forgets to
        /// wire a probe gets rows reading "unknown" instead of a machine that
        /// confidently believes every agent finished.
        public init(processes: Observation<[Int: ProcInfo]> = .unavailable("not probed"),
                    sentinels: Observation<Set<String>> = .unavailable("not probed"),
                    tails: Observation<[String: String]> = .unavailable("not probed"),
                    claims: Observation<Set<String>> = .unavailable("not probed"),
                    mergedPRs: Observation<Set<Int>> = .unavailable("not probed"),
                    liveAgents: Observation<[Int: String]> = .unavailable("not probed"),
                    sessions: Observation<[String: SessionState]> = .unavailable("not probed"),
                    activity: Observation<[String: TurnReport]> = .unavailable("not probed"),
                    tokensLeft: Observation<Bool> = .unavailable("not probed")) {
            self.processes = processes
            self.sentinels = sentinels
            self.tails = tails
            self.claims = claims
            self.mergedPRs = mergedPRs
            self.liveAgents = liveAgents
            self.sessions = sessions
            self.activity = activity
            self.tokensLeft = tokensLeft
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
        /// Whether the STILLNESS BACKSTOP is what ended this run — set by that rung and
        /// by nothing else.
        ///
        /// A verdict, not a restatement of one: a run reaches `.finished` by many roads,
        /// and only this one says its agent was alive with a frozen screen. The window
        /// reaper is the consumer, and the distinction is the whole of its licence to
        /// close a terminal, so it cannot be left to be re-derived from `state` plus a
        /// matured `wentQuiet` — a clock keeps maturing while its pane is unreadable
        /// (`observeQuiescence` only advances on ticks that SAW the screen), so a run
        /// whose process left the machine during an evidence outage comes back
        /// finished-because-gone carrying twenty minutes of stillness. Reaping that
        /// closes whatever holds its tty now.
        public var wedged: Bool = false
        /// Whether the RUN DEADLINE is what ended this run — set by that rung and by
        /// nothing else.
        ///
        /// The other half of the window reaper's licence, and a separate field for the
        /// same reason `wedged` is one: a clock answers about a record whatever ended
        /// it. `pastDeadline` still returns an age for a run that a rung ABOVE the
        /// deadline ended — a sentinel, or the agent's own turn report — and that run
        /// finished the ordinary way, alive at its prompt with the task on the screen.
        /// Reaping it closes the window over the very thing the operator asked for.
        public var expired: Bool = false
        /// Whether nothing on this machine had anything to LOOK FOR — set by the one
        /// rung that answers `.unknown` about the record rather than about a probe.
        ///
        /// The deadline overrules a `.running` and exactly one `.unknown`, and the two
        /// `.unknown`s are not separable by state. "The process table could not be
        /// read" is an evidence outage, and ending a run on a clock during one retires
        /// it for being old on the single pass that saw nothing. A run with neither a
        /// pid nor a PR number is not an outage at all — every probe answered, and none
        /// of them was given anything to look for — and that answer is the same on every
        /// future tick, so without this the record holds its bay for the life of the
        /// applet.
        ///
        /// It is also what keeps such a run out of `reapable`: a window is closed on the
        /// strength of having SEEN the agent sitting in it, and this is the one ending
        /// where nothing ever did.
        public var unfindable: Bool = false

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

    // MARK: - Turn reports

    /// One verb the agent's own CLI wrote, and when. The Swift twin of
    /// `diplomat_runtime.completion` — that module owns the format; this only reads it.
    public struct TurnReport: Equatable {
        public enum Verb: String {
            /// A turn is in flight — `UserPromptSubmit` ran and no `Stop` has since.
            case busy
            /// The turn ended. The agent is alive at its prompt, and its work is done.
            case idle
            /// The session itself is over — the agent exited rather than returning to
            /// a prompt.
            case ended

            /// The reason line for a verb that ends a run, or `nil` for one that does
            /// not. Both terminal verbs, and only on a verb actually read: an absent
            /// report can never end a run.
            var overReason: String? {
                switch self {
                case .busy: return nil
                case .idle: return "its CLI reported the turn over"
                case .ended: return "its CLI reported the session ended"
                }
            }
        }

        public var verb: Verb
        public var at: TimeInterval

        public init(verb: Verb, at: TimeInterval) {
            self.verb = verb
            self.at = at
        }
    }

    /// What this run last reported about itself, or `nil` if it reports nothing.
    ///
    /// `nil` covers three ordinary cases and is never evidence about the run: the probe
    /// could not read the directory, the run was spawned without hooks (a foreign
    /// runner, or settings that would not stage), and the seconds before a fresh run's
    /// first hook fires. Each falls through to the evidence such a run always had.
    private static func reported(_ record: RunRecord, _ evidence: Evidence) -> TurnReport? {
        evidence.activity.value?[record.runID]
    }

    /// How long this run's screen has been perfectly still, once that is long enough to
    /// call it over — `nil` otherwise.
    ///
    /// A function rather than a comparison at each site because two of them ask: the
    /// resolver, to end the run, and the reaper, to close the window it was in. Those
    /// two answers agreeing is the whole contract — a window killed under a run still
    /// counted as working is the one mistake this backstop could make.
    public static func wentQuiet(_ record: RunRecord, now: TimeInterval) -> TimeInterval? {
        guard let since = record.quietSince else { return nil }
        let quiet = now - since
        return quiet >= quietTimeout ? quiet : nil
    }

    /// Refresh each run's record of when its screen last CHANGED, returning updated
    /// records.
    ///
    /// Beside `observeClaims` and for the same reason: absence is only measurable
    /// against a memory of presence, and `resolve` stays a function of its arguments
    /// alone. What is remembered is a digest rather than the screen itself — the book is
    /// rewritten every tick and read by other processes, so storing every watched pane's
    /// contents in it would be both large and pointless.
    ///
    /// A tail that could not be read updates nothing. That is what keeps a tmux server
    /// going down from looking like twenty minutes of stillness across every run at
    /// once: the clock only advances on ticks that actually SAW the screen, and it
    /// restarts from the first one that does.
    public static func observeQuiescence(_ records: [RunRecord],
                                         tails: Observation<[String: String]>,
                                         now: TimeInterval) -> [RunRecord] {
        guard let seen = tails.value else { return records }
        return records.map { r in
            guard !r.tty.isEmpty, let tail = seen[r.tty] else { return r }
            var out = r
            let digest = paneDigest(tail)
            if digest != r.quietDigest {
                out.quietDigest = digest
                out.quietSince = now
            } else if r.quietSince == nil {
                out.quietSince = now
            }
            return out
        }
    }

    /// A screen's fingerprint, for telling "unchanged" from "changed".
    ///
    /// FNV-1a 64-bit, matching `agentstate.pane_digest` exactly: both front-ends
    /// persist this into the SAME book, so the two must agree byte for byte or a
    /// hand-over restarts the stillness clock. Not a cryptographic hash, because this
    /// target is Foundation-only and builds on Linux, where `CryptoKit` does not
    /// exist — and collision resistance is not a property this needs, the question
    /// being only whether THIS pane differs from what the last tick saw of it.
    static func paneDigest(_ tail: String) -> String {
        var h: UInt64 = 0xCBF2_9CE4_8422_2325
        for byte in Array(maskClocks(tail).utf8) {
            h = (h ^ UInt64(byte)) &* 0x100_0000_01B3
        }
        return String(format: "%016llx", h)
    }

    /// A screen with every time of day blanked, matching `agentstate._CLOCK` — the
    /// pattern `[0-9]{1,2}:[0-9]{2}(:[0-9]{2})?`, replaced by `~`. That constant carries
    /// why a screen's fingerprint has to ignore a clock, and why only a clock.
    ///
    /// Scanned by hand rather than by `NSRegularExpression`: the digest goes into the one
    /// book both front-ends read, so this has to agree with Python's `re` character for
    /// character, and a shared explicit scan is what guarantees that where two regex
    /// engines only make it likely.
    static func maskClocks(_ tail: String) -> String {
        let chars = Array(tail.unicodeScalars)
        func isDigit(_ i: Int) -> Bool { i < chars.count && chars[i] >= "0" && chars[i] <= "9" }
        func isColon(_ i: Int) -> Bool { i < chars.count && chars[i] == ":" }
        // Length of the `:[0-9][0-9]` group at `i`, or 0 — minutes, then seconds.
        func pair(_ i: Int) -> Int { isColon(i) && isDigit(i + 1) && isDigit(i + 2) ? 3 : 0 }
        var out = String.UnicodeScalarView()
        var i = 0
        while i < chars.count {
            // Greedy on the leading run, as `[0-9]{1,2}` is: two digits before one.
            var head = 0
            if isDigit(i) && isDigit(i + 1) && pair(i + 2) > 0 { head = 2 }
            else if isDigit(i) && pair(i + 1) > 0 { head = 1 }
            guard head > 0 else {
                out.append(chars[i])
                i += 1
                continue
            }
            let seconds = pair(i + head + 3)
            out.append("~")
            i += head + 3 + seconds
        }
        return String(out)
    }

    // MARK: - The resolver

    /// Every run's state, from one pass of evidence. Pure.
    public static func resolve(records: [RunRecord], evidence: Evidence,
                               now: TimeInterval,
                               deadline: TimeInterval? = nil) -> [String: Resolution] {
        var out: [String: Resolution] = [:]
        for r in records {
            out[r.runID] = resolveOne(r, evidence: evidence, now: now, deadline: deadline)
        }
        return out
    }

    /// One run's state, by a fixed ladder.
    ///
    /// The order is the precedence, and each rung is either positive evidence or an
    /// explicit refusal to guess:
    ///
    /// 1. the PR landed — a terminal outcome that outranks whatever the process is doing;
    /// 2. the completion sentinel exists — the agent returned an exit code;
    /// 3. the agent's CLI reported its turn over — a report rather than an inference,
    ///    and above what follows because a run that finished is alive at its prompt and
    ///    every rung below this one sees a live process either way. A runner that keeps
    ///    a session instead of running hooks says the same thing further down, once its
    ///    pid is known alive;
    /// 4. a mesh-peer run is judged by the executor's claim, because no probe on this
    ///    machine can see a process on another one;
    /// 5. a local run is judged by its pid, and its screen only classifies a pid that is
    ///    already known to be alive;
    /// 6. the deadline, when the operator has one, and LAST: what it overrules is a
    ///    `.running` — the answer that means "this bay is spoken for and nothing here
    ///    can say when it will come back". Every other answer the ladder reaches is a
    ///    better one than a clock, and each of them is a reason this rung must not fire:
    ///    an ended run already named how it stopped, `.awaitingInput` is a session at
    ///    its prompt that gave its bay back and still holds a task worth reading, and
    ///    `.unknown` is almost always the tick where the evidence could not be read at
    ///    all — ending a run on that would retire it for being old on the one pass that
    ///    saw nothing. Its one exception is the `.unknown` that is about the record
    ///    rather than a probe (`Resolution.unfindable`), which no later pass can improve
    ///    on.
    public static func resolveOne(_ record: RunRecord, evidence: Evidence,
                                  now: TimeInterval,
                                  deadline: TimeInterval? = nil) -> Resolution {
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
        if let report = reported(record, evidence), let why = report.verb.overReason {
            return done(.finished, why)
        }
        let out = record.placement == .meshPeer
            ? resolvePeer(record, evidence: evidence, now: now, done: done)
            : resolveLocal(record, evidence: evidence, now: now, done: done)
        guard out.state == .running || out.unfindable,
              let expired = pastDeadline(record, tokens: evidence.tokensLeft, now: now,
                                         deadline: deadline)
        else { return out }
        let cutoff = ApiErrorMatch.humanInterval(deadline ?? 0)
        // The one rung that stamps `expired`; see `Resolution.expired`. The answer it
        // overruled is kept in the reason: it is what the run looked like right up to
        // the moment a clock ended it, and the only account of that anyone gets.
        var ended = done(.finished,
                         "\(out.reason); has run for "
                         + "\(ApiErrorMatch.humanInterval(expired)), "
                         + "past the \(cutoff) deadline")
        ended.expired = true
        ended.unfindable = out.unfindable
        return ended
    }

    /// How long this run has been going, once that is long enough to call it over — nil
    /// when it is not, or when nothing here may call it over at all.
    ///
    /// Asked by the resolver alone, like `wentQuiet`, and for the same reason: an age is
    /// true of a run whatever ended it, so the window reaper reads the verdict that came
    /// out of this (`Resolution.expired`) rather than asking again. The age returned is
    /// the age the reason line quotes, so the verdict and the number the operator reads
    /// cannot come apart.
    ///
    /// Five things hold it back, each a case where the clock is measuring something
    /// other than a bay that will not come back:
    ///
    /// * **no deadline** — the operator switched the backstop off;
    /// * **no token reading, or none left** — see `runDeadline`;
    /// * **a run on somebody else's machine** — the reading above is THIS account's, and
    ///   the peer's own claim already ends that run;
    /// * **a run the operator started by hand** — `capLoad` counts only `.auto`, so a
    ///   panel click holds no bay of the automatic cap and there is nothing here to hand
    ///   back. All that ending one would buy is the loss of a working agent the operator
    ///   is driving themselves;
    /// * **an untracked run** — its record comes from the scan rather than from a
    ///   dispatch, so `synthesizeUntracked` rebuilds it on the very next tick with a
    ///   fresh stamp: the bay comes back for one tick and the same agent takes it again.
    ///   Its stamp is when the scan first SAW the agent, so the age here would not even
    ///   be the run's.
    ///
    /// Every run this reaches is one `capLoad` is counting — a bay is what there is to
    /// hand back — but not the reverse, and the gap is deliberate on both sides. An
    /// untracked run holds a bay and is exempt above. So is a run on a tick whose
    /// evidence could not be read, which the caller keeps out by asking this about a
    /// `.running` verdict or an unfindable one and no other: a bay held by a run nobody
    /// could look at this pass is a bay kept.
    public static func pastDeadline(_ record: RunRecord, tokens: Observation<Bool>,
                                    now: TimeInterval,
                                    deadline: TimeInterval?) -> TimeInterval? {
        guard let cutoff = deadline, record.runsHere, !record.untracked,
              record.source == AgentDispatchGate.Source.auto.rawValue,
              tokens.value == true else { return nil }
        let age = now - record.dispatchedAt
        return age >= cutoff ? age : nil
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
    /// The pid is the identity — written by the inner shell, and naming the agent itself
    /// or the shell that wraps it, per `AgentSpawner.shellCommand`. Every rung below
    /// reads only what holds for both: the process is there, its argv is an agent's, and
    /// it is no younger than the record. Matching on it replaces reading
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
            if record.untracked {
                return resolveUntracked(record, evidence: evidence, now: now, done: done)
            }
            return resolveWithoutPid(record, evidence: evidence, now: now, age: age, done: done)
        }
        guard let proc = table[pid] else {
            // A pid the table has not caught up with, not a dead one: the pid file
            // and the table are read at different instants, and the table is one `ps`
            // pass reused for several seconds, so a pid written after that pass names a
            // process it structurally cannot hold. Read as death, a run is retired
            // seconds into its own spawn and its directory deleted under a working
            // agent. The same record one tick earlier, with no pid at all, had exactly
            // this grace.
            if age <= spawnGrace {
                return done(.starting,
                            "dispatched \(secs(age)) ago, pid \(pid) "
                            + "not in the process table yet")
            }
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
        return classifyActivity(record, evidence: evidence, now: now, done: done,
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
                                          now: TimeInterval, age: TimeInterval,
                                          done: (RunState, String) -> Resolution) -> Resolution {
        guard let live = evidence.liveAgents.value else {
            let why = evidence.liveAgents.reason.isEmpty
                ? "failed" : evidence.liveAgents.reason
            return done(.unknown, "no pid, and the agent scan \(why)")
        }
        if let pr = record.prNumber, live[pr] != nil {
            return classifyActivity(record, evidence: evidence, now: now, done: done,
                                    aliveReason: "an agent is up on PR #\(pr)")
        }
        if age <= spawnGrace {
            return done(.starting, "dispatched \(secs(age)) ago, no pid yet")
        }
        guard let pr = record.prNumber else {
            // Nothing to look for: a run with neither a pid nor a PR cannot be found by
            // either mechanism, so its absence is not evidence of anything. The one rung
            // that stamps `unfindable`; see `Resolution.unfindable`.
            var nowhere = done(.unknown, "no pid recorded \(secs(age)) after dispatch")
            nowhere.unfindable = true
            return nowhere
        }
        return done(.finished, "no agent for PR #\(pr) in the process table")
    }

    /// A run synthesized from a sighting in the process table.
    ///
    /// It has no pid of its own and no dispatch stamp to be young against, so the scan
    /// that made it is also the only thing that can end it — and something must, because
    /// the record is kept across ticks for the stillness backstop's sake: one that
    /// outlived its agent would hold that PR against a fresh agent, and a bay of the cap,
    /// for the life of the applet.
    ///
    /// An unreadable scan ends nothing, like every other rung here. It is the sole
    /// evidence about this run, so "could not look" must not read as "it is gone".
    private static func resolveUntracked(_ record: RunRecord, evidence: Evidence,
                                         now: TimeInterval,
                                         done: (RunState, String) -> Resolution) -> Resolution {
        guard let live = evidence.liveAgents.value else {
            let why = evidence.liveAgents.reason.isEmpty
                ? "failed" : evidence.liveAgents.reason
            return done(.unknown, "the agent scan \(why)")
        }
        if let pr = record.prNumber, live[pr] != nil {
            return classifyActivity(record, evidence: evidence, now: now, done: done,
                                    aliveReason: "found in process table")
        }
        return done(.finished, "gone from the process table")
    }

    /// Working, or finished its turn and waiting at the prompt?
    ///
    /// An agent is spawned into an INTERACTIVE session, so finishing its work is not
    /// exiting: it sits at the prompt until a human closes the window, and the process
    /// table shows the same live agent either way. Something has to separate the two.
    ///
    /// A run that reports its own turns never reaches here still working — the ladder
    /// above ends it — so what this rung answers for one is the other half: its CLI
    /// said a turn is in flight, which outranks anything read off a screen.
    ///
    /// For a run that reports nothing, the agent's own session is asked next, and its
    /// answer ENDS the run exactly as the CLI's own does: a runner that keeps a session
    /// and one that runs a hook are two spellings of "ask the agent". Read as merely
    /// idle, every OpenCode and Hermes run stayed in the book until somebody closed its
    /// window by hand.
    ///
    /// The screen is the last fallback, and it is an inference — it reads whether the
    /// CLI's interrupt hint was on the status bar when we looked, which is a string from
    /// someone else's UI that says nothing at all if they reword it. It is the one
    /// source here that cannot end a run: `.awaitingInput` is what a stale hint reads as.
    ///
    /// Every gap here reads as `.running`, which costs a bay rather than correctness —
    /// but it is also the one rung that fails silently, so the probe layer counts how
    /// often the tail is missing and says so out loud.
    ///
    /// The quiescence backstop is asked FIRST, ahead of every "it is working" answer,
    /// because it exists precisely to overrule one: a run whose screen has not changed
    /// in `quietTimeout` is wedged whatever its status bar still claims, and the frozen
    /// `esc to interrupt` of an agent that died mid-turn is the exact case that
    /// otherwise holds a bay until a human closes the window.
    private static func classifyActivity(_ record: RunRecord, evidence: Evidence,
                                         now: TimeInterval,
                                         done: (RunState, String) -> Resolution,
                                         aliveReason: String) -> Resolution {
        if let quiet = wentQuiet(record, now: now) {
            // The one rung that stamps `wedged`; see `Resolution.wedged`.
            var out = done(.finished,
                           "\(aliveReason); its screen has not changed in "
                           + "\(ApiErrorMatch.humanInterval(quiet))")
            out.wedged = true
            return out
        }
        if let report = reported(record, evidence), report.verb == .busy {
            return done(.running, "\(aliveReason); its CLI reported a turn in flight")
        }
        if let known = evidence.sessions.value, let session = known[record.runID] {
            if session.busy { return done(.running, "\(aliveReason); its session is mid-turn") }
            return done(.finished, "\(aliveReason); its runner reported the turn over")
        }
        guard let tails = evidence.tails.value else {
            let why = evidence.tails.reason.isEmpty ? "unavailable" : evidence.tails.reason
            return done(.running, "\(aliveReason); screen \(why)")
        }
        guard !record.tty.isEmpty, let tail = tails[record.tty] else {
            return done(.running,
                        "\(aliveReason); no screen for tty \(record.tty.isEmpty ? "?" : record.tty)")
        }
        if AgentActivity.looksBusy(tail) { return done(.running, "\(aliveReason); working") }
        // An agent that has not started its first turn shows the same bare prompt as one
        // that has finished its last, so inside `firstTurnGrace` this joins the "alive,
        // and we cannot yet tell" answers above rather than reading as idle. Only for a
        // run we dispatched: an untracked one is stamped when the scan first saw it,
        // which says nothing about when its agent started.
        let age = now - record.dispatchedAt
        if !record.untracked, age <= firstTurnGrace {
            return done(.running, "\(aliveReason); dispatched \(secs(age)) ago, no turn on screen yet")
        }
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
    /// One is made once and then kept in the book like any other run, because the
    /// stillness backstop measures a screen against the last one seen and a record
    /// re-derived every tick remembers none: its clock never leaves zero, so it can never
    /// be found wedged and its window is never closed. The dedup on PR number is what
    /// keeps the next tick from making a second.
    ///
    /// They count as automatic. An agent whose trigger is unknown spending a bay defers
    /// work; the opposite error dispatches a second agent onto a PR that has one.
    public static func synthesizeUntracked(_ records: [RunRecord],
                                           liveAgents: Observation<[Int: String]>,
                                           now: TimeInterval) -> [RunRecord] {
        guard let live = liveAgents.value else { return records }
        var out: [RunRecord] = []
        for r in records {
            // A kept record follows its PR's current sighting: the scan reports one agent
            // per PR, so an operator's second session becomes that sighting the moment the
            // first exits. Its memory of the old screen goes with it, or the new window
            // inherits the old one's stillness.
            guard r.untracked, let pr = r.prNumber, let tty = live[pr],
                  !tty.isEmpty, tty != r.tty
            else { out.append(r); continue }
            var moved = r
            moved.tty = tty
            moved.quietDigest = ""
            moved.quietSince = nil
            out.append(moved)
        }
        let known = Set(out.compactMap { $0.prNumber })
        for pr in Set(live.keys).subtracting(known).sorted() {
            out.append(RunRecord(runID: "untracked:\(pr)", dispatchedAt: now,
                                 prNumber: pr,
                                 source: AgentDispatchGate.Source.auto.rawValue,
                                 placement: .local, tty: live[pr] ?? "",
                                 untracked: true))
        }
        return out
    }

    // MARK: - The projections
    //
    // Each is a fold over the resolved map. Nothing below re-reads evidence or
    // re-derives a state, which is the whole point: the answers can disagree with each
    // other only if this file is wrong, not if one call site of five drifted.

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
    /// Age alone retires nothing here: an hour-long review is an ordinary one, and a
    /// clock that ends records ends them mid-run. The one exception is
    /// `runDeadline`, which is on unless an operator turns it off — so this is the
    /// one thing said here that a default-on switch can make untrue.
    public static func retirable(records: [RunRecord],
                                 states: [String: Resolution]) -> [RunRecord] {
        records.filter { r in
            guard let s = states[r.runID] else { return false }
            return ended.contains(s.state)
        }
    }

    /// The runs whose terminal is nobody's — ended by a CLOCK rather than by evidence
    /// that their agent stopped, so the agent may well still be sitting in the window.
    ///
    /// A projection rather than a test each front-end repeats, because it is the one
    /// destructive consequence a tick has: a window closed under a run this resolver
    /// still calls working takes the whole task's context with it.
    ///
    /// Which rung fired is ASKED (`Resolution.wedged`, `Resolution.expired`) rather than
    /// re-derived from the clocks, because a clock answers about a record whatever ended
    /// it. Both of them are still true of runs the rungs above them ended: `wentQuiet`
    /// keeps maturing across an evidence outage, and `pastDeadline` holds for the whole
    /// life of a long run that then reports its turn over the ordinary way. Those runs
    /// are absent from here — their agent is alive at its prompt holding the finished
    /// task, and the operator may still want to read it. So is a merged one, for the
    /// same reason.
    ///
    /// The deadline's own exception is the run nothing could find
    /// (`Resolution.unfindable`): a clock ended that one too, but no probe ever saw its
    /// agent, so there is no window here for the verdict to vouch for. What is left is
    /// exactly the runs this tick SAW alive: the stillness rung only classifies a
    /// process already known to be up, and the other verdict the deadline overrules is a
    /// `.running`.
    ///
    /// `runsHere` is not re-checked: both stamps already imply it — the stillness rung
    /// only runs inside `resolveLocal`, and `pastDeadline` refuses a peer.
    public static func reapable(records: [RunRecord],
                                states: [String: Resolution]) -> [RunRecord] {
        records.filter { r in
            guard let s = states[r.runID] else { return false }
            return s.wedged || (s.expired && !s.unfindable)
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
        /// Of those, the ones a backstop ended — whose window is closed as well as
        /// forgotten. See `reapable(records:states:)`.
        public var reapable: [RunRecord]
        public var freeSlots: Int
        /// The instant every verdict below was resolved against.
        public var now: TimeInterval = 0

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
                            now: TimeInterval, limit: Int,
                            deadline: TimeInterval? = nil) -> Tick {
        var recs = observeClaims(records, claims: evidence.claims, now: now)
        recs = adoptTTYs(recs, processes: evidence.processes,
                         liveAgents: evidence.liveAgents)
        recs = observeQuiescence(recs, tails: evidence.tails, now: now)
        recs = synthesizeUntracked(recs, liveAgents: evidence.liveAgents, now: now)
        let states = resolve(records: recs, evidence: evidence, now: now,
                             deadline: deadline)
        let load = capLoad(records: recs, states: states)
        return Tick(records: recs, states: states,
                    rows: rows(records: recs, states: states),
                    capLoad: load, retirable: retirable(records: recs, states: states),
                    reapable: reapable(records: recs, states: states),
                    freeSlots: freeSlots(limit: limit, occupied: load.count), now: now)
    }
}
