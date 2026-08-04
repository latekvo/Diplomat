import Foundation
import AppKit
import DiplomatCore

// Tracking for the detached `claude` sessions the Review / Resolve-conflicts
// wizards spawn. A spawn opens a fully detached terminal window, so there is no
// child process to wait on; instead we capture three OS-level handles at spawn
// time — the terminal window id, the session id, and the controlling tty — plus a
// sentinel "done" file the shell touches when `claude` returns. From those we can
// (a) tell whether the session finished (the sentinel) and whether its window
// still exists (AppleScript window-id query — a closed window dismisses the row),
// and (b) bring its window back to the front on demand (AppleScript by window id).

// MARK: - Tracked process model

/// One dispatched agent task shown in the applet's ongoing-processes list: a
/// session this machine spawned, or — with `mesh` set — work it handed to a mesh
/// node, which has no local session behind it at all.
///
/// Persisted in UserDefaults so the list survives an applet restart (the daemon is
/// rebuilt/relaunched often); the references it survives on outlive this process
/// either way — a session's tty / window / sentinel are OS-level, and a mesh run's
/// lease is held by the peer executing it.
struct TrackedProcess: Identifiable, Codable, Equatable {
    /// Where a row's agent actually runs, when it is not a terminal on this machine.
    ///
    /// A mesh-routed job leaves no local handle behind — no window, no tty, no
    /// sentinel — so a row for one carries the two things that DO identify it: the
    /// node the mesh placed it on, and the work key whose lease says it is still
    /// going (`MeshAgentRun`).
    struct MeshRun: Codable, Equatable {
        /// The executor's node name, as the dispatch reported it. Empty when the
        /// mesh answered without naming one — the row then says only that it is on
        /// the mesh, which is still more than the work vanishing.
        var node: String
        /// The origination lease this run holds while its agent lives.
        var workKey: String
        /// Whether the mesh placed this run back on THIS machine — the best node it
        /// could find was the one that asked. Such a run is a `claude` process here
        /// like any other, so it spends this device's automatic-task budget; only the
        /// terminal was opened by the node rather than by the applet.
        var onThisMachine: Bool

        init(node: String, workKey: String, onThisMachine: Bool) {
            self.node = node
            self.workKey = workKey
            self.onThisMachine = onThisMachine
        }

        /// Tolerant decode, like the row that holds it: a run persisted before the
        /// placement was recorded reads as a peer's. That is the reading which frees
        /// a slot rather than holding one, and it self-corrects on the first sweep —
        /// `ps` sees a local agent whoever started it.
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            node = try c.decode(String.self, forKey: .node)
            workKey = try c.decode(String.self, forKey: .workKey)
            onThisMachine = try c.decodeIfPresent(Bool.self, forKey: .onThisMachine) ?? false
        }
    }

    let id: UUID
    /// "review" or "conflicts" — drives the row's icon/tint.
    var kind: String
    /// Human label, e.g. "Review · #337 · Deep" or "Resolve · my PRs".
    var label: String
    /// The terminal it runs in ("iterm" / "terminal"), for the focus AppleScript.
    /// Empty on a mesh row, which has no terminal on this machine.
    var terminal: String
    /// Terminal window id (string form) — the focus target.
    var windowID: String
    /// iTerm session id (GUID); empty for Terminal.app.
    var sessionID: String
    /// Controlling tty, e.g. "/dev/ttys016" — the liveness probe.
    var tty: String
    /// Sentinel file the shell writes when `claude` returns (`…; printf … > done`).
    var donePath: String
    /// Set when the mesh placed this work on a node instead of a local terminal; nil
    /// for every session this machine spawned itself. The one test that tells the
    /// two apart, because everything else about the row reads the same.
    var mesh: MeshRun?
    /// The single PR this session concerns, if any — dedups agents per PR (the
    /// in-flight checks) and drives the merged-status probe.
    var prURL: String?
    /// Which trigger opened this session: "panel" (a click) or "auto" (a monitor).
    /// The automatic-task cap has to tell the two apart — a click spends none of the
    /// automatic budget, while a monitor dispatch is exactly what the budget is for,
    /// and `ps` cannot distinguish them.
    var source: String
    var createdAt: Date
    /// Recomputed by the poller: true once `claude` has returned (sentinel present)
    /// or the window is gone. Persisted only as a cache; the next poll corrects it.
    var done: Bool
    /// Recomputed by a full refresh (the "Update"): true once this session's PR has
    /// been MERGED on GitHub. A definitive, terminal outcome that outranks `done`
    /// (which only means the local `claude` process exited). Persisted as a cache;
    /// the next refresh corrects it. Always false for sessions with no PR.
    var merged: Bool
    /// Recomputed by the poller from the session's terminal buffer: true when the agent
    /// has finished its turn and is idling at the prompt (no "esc to interrupt" hint on
    /// the live status bar) rather than actively working. Meaningful only while `!done`;
    /// persisted only as a cache, the next poll corrects it.
    var awaitingInput: Bool

    /// `source` defaults to a panel spawn: the only callers that leave it out are the
    /// fixture rows (the headless render, the tracking self-test), and a fixture stands
    /// in for a session the operator opened. The real dispatch pipeline always passes
    /// its own. (A record decoded from an older build defaults the other way — see the
    /// decoder below, which is answering a different question.)
    init(id: UUID = UUID(), kind: String, label: String, terminal: String,
         windowID: String, sessionID: String, tty: String, donePath: String,
         prURL: String?, mesh: MeshRun? = nil,
         source: String = AgentDispatchGate.Source.panel.rawValue,
         createdAt: Date = Date(),
         done: Bool = false, merged: Bool = false, awaitingInput: Bool = false) {
        self.id = id
        self.kind = kind
        self.label = label
        self.terminal = terminal
        self.windowID = windowID
        self.sessionID = sessionID
        self.tty = tty
        self.donePath = donePath
        self.prURL = prURL
        self.mesh = mesh
        self.source = source
        self.createdAt = createdAt
        self.done = done
        self.merged = merged
        self.awaitingInput = awaitingInput
    }

    /// Tolerant decode: the recomputed cache flags (`done`, `merged`) may be absent
    /// in a record persisted by an older build, so default them to false rather than
    /// failing the whole list's decode.
    ///
    /// A record written before sessions carried a `source` defaults to "auto", the
    /// conservative reading for the automatic-task cap: an agent whose trigger is
    /// unknown counts against the budget rather than being exempted from it. The
    /// alternative under-counts on exactly one launch — the first after an upgrade,
    /// with agents already running.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(UUID.self, forKey: .id)
        kind = try c.decode(String.self, forKey: .kind)
        label = try c.decode(String.self, forKey: .label)
        terminal = try c.decode(String.self, forKey: .terminal)
        windowID = try c.decode(String.self, forKey: .windowID)
        sessionID = try c.decode(String.self, forKey: .sessionID)
        tty = try c.decode(String.self, forKey: .tty)
        donePath = try c.decode(String.self, forKey: .donePath)
        prURL = try c.decodeIfPresent(String.self, forKey: .prURL)
        mesh = try c.decodeIfPresent(MeshRun.self, forKey: .mesh)
        source = try c.decodeIfPresent(String.self, forKey: .source)
            ?? AgentDispatchGate.Source.auto.rawValue
        createdAt = try c.decode(Date.self, forKey: .createdAt)
        done = try c.decodeIfPresent(Bool.self, forKey: .done) ?? false
        merged = try c.decodeIfPresent(Bool.self, forKey: .merged) ?? false
        awaitingInput = try c.decodeIfPresent(Bool.self, forKey: .awaitingInput) ?? false
    }

    /// This row stands for work running on a mesh node, not a session on this
    /// machine — so nothing local can be probed, focused or killed for it.
    var isMesh: Bool { mesh != nil }

    /// Does this row's agent run on THIS machine? True for every session the applet
    /// spawned, and for a mesh placement that landed back here; false only for a
    /// peer's. What the automatic-task cap counts — the cap is about how much this
    /// device runs at once, not about who dispatched it.
    var runsHere: Bool { mesh.map(\.onThisMachine) ?? true }

    /// The tty as `ps` reports it (no `/dev/` prefix), or "" when untracked.
    var shortTTY: String {
        tty.hasPrefix("/dev/") ? String(tty.dropFirst(5)) : tty
    }

    /// The PR number this session concerns, parsed from `prURL` (…/pull/<n>), or nil
    /// when the session isn't tied to a single PR. The merge-status probe key.
    var prNumber: Int? {
        guard let prURL, let r = prURL.range(of: "/pull/") else { return nil }
        let digits = prURL[r.upperBound...].prefix { $0.isNumber }
        return Int(digits)
    }
}

// MARK: - Liveness + window focus

/// Stateless helpers that probe whether a tracked session is still alive and bring
/// its window forward. All work happens through `ps` (liveness) and `osascript`
/// (focus); there is no long-lived handle to the detached terminal.
enum ProcessMonitor {
    /// A just-spawned session may not show in `ps` for a beat; don't call it done
    /// inside this window even if the tty probe misses.
    static let graceInterval: TimeInterval = 5

    /// How much younger than its row a tty's oldest process may be and still count
    /// as that session's own shell (spawn wrote `createdAt` a settle-delay after the
    /// shell started, so the genuine shell always PREdates the row — the slack only
    /// absorbs clock/etime rounding). Anything younger is a squatter on a recycled
    /// pty name.
    static let ttyAdoptionSlack: TimeInterval = 10

    /// Every controlling tty currently backing a live process (e.g. "ttys016").
    /// One `ps` call covers the whole list of tracked sessions.
    static func aliveTTYs() -> Set<String> {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/ps")
        proc.arguments = ["-A", "-o", "tty="]
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = Pipe()
        do { try proc.run() } catch { return [] }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        guard let out = String(data: data, encoding: .utf8) else { return [] }
        var set = Set<String>()
        for line in out.split(separator: "\n") {
            let t = line.trimmingCharacters(in: .whitespaces)
            if !t.isEmpty && t != "??" { set.insert(t) }
        }
        return set
    }

    /// The result of one liveness sweep: the same sessions with `done` recomputed,
    /// plus the ids whose terminal window/tab has been *closed* (their tty is gone)
    /// — those get dropped from the list entirely rather than lingering as "done".
    struct Sweep {
        var refreshed: [TrackedProcess]
        var closedIDs: Set<UUID>
    }

    /// The open window ids of a terminal app, or nil when we can't tell (osascript
    /// errored — e.g. automation permission not yet granted). Each agent is spawned
    /// into its OWN window, so a window id maps 1:1 to a session; membership is the
    /// authoritative "is this session's terminal still open?" test — the same handle
    /// `focus` targets, so liveness and focus can never disagree. `is running` is a
    /// no-launch predicate, so polling never resurrects a quit terminal; a running app
    /// with no windows returns an empty set (all its sessions are gone).
    static func openWindowIDs(term: SpawnTerminal) -> Set<String>? {
        let app = term.appName
        let script = """
        if application "\(app)" is running then
            tell application "\(app)"
                set _ids to {}
                repeat with w in windows
                    set end of _ids to (id of w as string)
                end repeat
                set AppleScript's text item delimiters to linefeed
                return _ids as text
            end tell
        end if
        return ""
        """
        guard let s = OSAScript.capture(script) else { return nil } // couldn't query → unknown
        var set = Set<String>()
        for line in s.split(separator: "\n") {
            let t = line.trimmingCharacters(in: .whitespaces)
            if !t.isEmpty { set.insert(t) }
        }
        return set
    }

    /// Recompute liveness of each session against the set of open terminal windows.
    /// A session is `done` when the `claude` sentinel exists OR its window is gone; it
    /// is *terminal-closed* (returned in `closedIDs`, to be dropped from the list)
    /// specifically when its bound window is gone past the grace window. When a
    /// terminal app can't be queried (resolver returns nil) its sessions are left
    /// alone — we never dismiss on an inconclusive probe. `openWindows` is injectable
    /// for deterministic tests; it defaults to the live `openWindowIDs`.
    /// `sessionTails` (tty → the session's visible terminal buffer) drives the
    /// running-vs-awaiting-input classification of live sessions AND corroborates
    /// window-gone verdicts (a tty still listed among the terminal's sessions means
    /// the session lives, whatever happened to its window id); pass `nil` when the
    /// dump failed — it then can't veto anything. Injectable for deterministic tests.
    /// `ttyElapsed` (tty → its longest-lived process's elapsed seconds) is the
    /// second corroboration layer; nil = probe `ps` lazily. Injectable for tests.
    ///
    /// Mesh rows are passed through untouched: their agent runs on another machine,
    /// so every probe here would be asking this one about a process it does not
    /// have — and answering "no window, no tty, no shell" would dismiss a live run.
    /// Their liveness is the executor's claim instead (`Store.reconcileMeshRuns`).
    static func sweep(_ procs: [TrackedProcess], now: Date = Date(),
                      openWindows: ((SpawnTerminal) -> Set<String>?)? = nil,
                      sessionTails: [String: String]? = nil,
                      ttyElapsed: [String: TimeInterval]? = nil) -> Sweep {
        let local = procs.filter { !$0.isMesh }
        guard !local.isEmpty else { return Sweep(refreshed: procs, closedIDs: []) }
        let resolve = openWindows ?? { openWindowIDs(term: $0) }
        let fm = FileManager.default
        // One window-id query per distinct terminal app (nil = couldn't determine).
        var openByTerm: [String: Set<String>?] = [:]
        for t in Set(local.map { $0.terminal }) {
            openByTerm[t] = resolve(SpawnTerminal(rawValue: t) ?? .iterm)
        }
        // Probed at most once per sweep, and only when a window-gone verdict needs
        // corroborating.
        var elapsedProbe = ttyElapsed
        var closed = Set<UUID>()
        // Every probe below asks the OS about a process on THIS machine, so only the
        // local sessions walk it; the mesh rows are stitched back into place at the
        // end, in their original order and exactly as they came in.
        let swept = local.map { p -> TrackedProcess in
            var p = p
            let sentinel = !p.donePath.isEmpty && fm.fileExists(atPath: p.donePath)
            // Is this session's terminal window really gone? The window-id enumeration
            // alone is a poor witness, in BOTH directions: it can drop a window whose
            // session lives on (dragging a window into another as a tab destroys the
            // window identity but keeps the session; a transient AppleScript miss
            // looks identical — removing the row then loses in-flight dedup and the
            // monitor double-dispatches, 2026-07-20), and it can keep listing a window
            // the user closed for as long as iTerm's undo-close grace revives it. So
            // for rows with a captured tty the session evidence decides:
            // - The session dump still lists the tty → the terminal itself says the
            //   session exists (maybe under another window id) — ALIVE.
            // - The tty still hosts a process as old as the row → the session's own
            //   shell (born before the row) is still running — ALIVE. The age gate
            //   matters: freed pty NAMES are recycled within seconds on a busy box, so
            //   "any process on the tty" would adopt strangers; a process predating
            //   the row can't be a squatter on a later-freed pty.
            // - Neither, and the tty probe itself worked → the shell is dead, so the
            //   session is gone whatever the window list claims — CLOSED.
            // Rows with no captured tty fall back to window-id membership alone, and
            // an unqueryable terminal app (enumeration nil) never auto-removes.
            var windowClosed = false
            if now.timeIntervalSince(p.createdAt) > graceInterval {
                let aliveByDump = !p.tty.isEmpty && sessionTails?[p.tty] != nil
                var aliveByTTY = false
                var ttyProbeWorked = false
                if !p.tty.isEmpty, !aliveByDump {
                    if elapsedProbe == nil { elapsedProbe = ttyProcessElapsed() }
                    ttyProbeWorked = !(elapsedProbe?.isEmpty ?? true)
                    if let oldest = elapsedProbe?[p.shortTTY] {
                        aliveByTTY = oldest >= now.timeIntervalSince(p.createdAt) - ttyAdoptionSlack
                    }
                }
                if aliveByDump || aliveByTTY {
                    windowClosed = false
                } else if !p.tty.isEmpty, ttyProbeWorked {
                    windowClosed = true
                } else if !p.windowID.isEmpty, let open = openByTerm[p.terminal] ?? nil {
                    windowClosed = !open.contains(p.windowID)
                }
            }
            if windowClosed { closed.insert(p.id) }
            p.done = sentinel || windowClosed
            // A still-live session is "awaiting input" when its terminal shows the CLI back
            // at the prompt (no interrupt hint). Only assert it when we actually captured
            // that session's buffer — absent evidence, leave it reading as running.
            if let tails = sessionTails {
                if p.done {
                    p.awaitingInput = false
                } else if let tail = tails[p.tty] {
                    p.awaitingInput = !AgentActivity.looksBusy(tail)
                } else {
                    p.awaitingInput = false
                }
            }
            return p
        }
        let byID = Dictionary(swept.map { ($0.id, $0) }, uniquingKeysWith: { _, last in last })
        return Sweep(refreshed: procs.map { byID[$0.id] ?? $0 }, closedIDs: closed)
    }

    /// Back-compat convenience: just the `done`-recomputed sessions (drops the
    /// terminal-closed classification). Used by the self-test's live cycle.
    static func refreshed(_ procs: [TrackedProcess], now: Date = Date()) -> [TrackedProcess] {
        sweep(procs, now: now).refreshed
    }

    /// True when the session's tty is still backed by a live process — i.e. its
    /// window is still open and can be focused.
    static func isWindowAlive(_ p: TrackedProcess) -> Bool {
        guard !p.tty.isEmpty else { return false }
        return aliveTTYs().contains(p.shortTTY)
    }

    /// Elapsed age (seconds) of the longest-lived process on each tty, from one
    /// `ps -axo etime=,tty=` pass. `etime` is used (not `lstart`) because its
    /// `[[dd-]hh:]mm:ss` form is locale-independent.
    static func ttyProcessElapsed() -> [String: TimeInterval] {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/ps")
        proc.arguments = ["-axo", "etime=,tty="]
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = Pipe()
        do { try proc.run() } catch { return [:] }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        guard let out = String(data: data, encoding: .utf8) else { return [:] }
        var map: [String: TimeInterval] = [:]
        for line in out.split(separator: "\n") {
            let cols = line.split(separator: " ", omittingEmptySubsequences: true)
            guard cols.count >= 2, let secs = parseElapsed(String(cols[0])) else { continue }
            let tty = String(cols[1])
            guard tty != "??" else { continue }
            map[tty] = max(map[tty] ?? 0, secs)
        }
        return map
    }

    /// Parse `ps` etime (`[[dd-]hh:]mm:ss`) into seconds; nil when malformed.
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

    /// PR numbers of `claude` agents currently alive anywhere on this machine, read
    /// straight from `ps` argvs: every single-PR prompt the applet dispatches opens
    /// with "… PR #<n> in <owner>/<repo> …" and `claude` receives the whole prompt
    /// as one argument, so a live agent is visible no matter what happened to our
    /// row tracking (applet restart, a swept row, a window merged into a tab). The
    /// monitor's in-flight dedup ORs this in so it can never double-dispatch onto a
    /// PR that demonstrably already has an agent. `psOutput` is injectable for
    /// deterministic tests; nil = run `ps`.
    static func liveAgentPRNumbers(owner: String, repo: String,
                                   psOutput: String? = nil) -> Set<Int> {
        let dump = psOutput ?? fullCommands()
        guard !dump.isEmpty,
              let re = try? NSRegularExpression(
                pattern: "PR #(\\d+) in \(NSRegularExpression.escapedPattern(for: "\(owner)/\(repo)"))")
        else { return [] }
        var out = Set<Int>()
        for line in dump.split(separator: "\n") {
            // Only a real agent process carries the phrase: the spawning shell's
            // argv holds the unexpanded `$(cat …)`, not the prompt text.
            guard line.contains("claude") else { continue }
            let s = String(line)
            let range = NSRange(s.startIndex..., in: s)
            for m in re.matches(in: s, range: range) {
                if let r = Range(m.range(at: 1), in: s), let n = Int(s[r]) { out.insert(n) }
            }
        }
        return out
    }

    /// Full argv of every process (`ps -axo command=`), one per line.
    private static func fullCommands() -> String {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/ps")
        proc.arguments = ["-axo", "command="]
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = Pipe()
        do { try proc.run() } catch { return "" }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        return String(data: data, encoding: .utf8) ?? ""
    }

    /// Bring the session's terminal window to the front. Returns false when the
    /// window no longer exists (closed) or AppleScript errors — the caller then
    /// re-sweeps and dismisses the dead row.
    @discardableResult
    static func focus(_ p: TrackedProcess) -> Bool {
        guard !p.windowID.isEmpty else { return false }
        let term = SpawnTerminal(rawValue: p.terminal) ?? .iterm
        let script = focusScript(term: term, windowID: p.windowID, sessionID: p.sessionID)
        return OSAScript.runSilently(script)
    }

    /// AppleScript that selects the window with the captured id (erroring if it's
    /// gone, so the caller sees a non-zero exit). iTerm also re-selects the exact
    /// session; Terminal raises + fronts the window.
    static func focusScript(term: SpawnTerminal, windowID: String, sessionID: String) -> String {
        switch term {
        case .iterm:
            return """
            tell application "iTerm"
                activate
                set _found to false
                repeat with w in windows
                    if (id of w as string) is "\(windowID)" then
                        select w
                        set _found to true
                        repeat with t in tabs of w
                            repeat with s in sessions of t
                                if (id of s) is "\(sessionID)" then
                                    select t
                                    tell t to select s
                                end if
                            end repeat
                        end repeat
                    end if
                end repeat
                if not _found then error "window gone"
            end tell
            """
        case .terminal:
            return """
            tell application "Terminal"
                activate
                set _found to false
                repeat with w in windows
                    if (id of w as string) is "\(windowID)" then
                        set index of w to 1
                        set frontmost of w to true
                        set _found to true
                    end if
                end repeat
                if not _found then error "window gone"
            end tell
            """
        }
    }
}
