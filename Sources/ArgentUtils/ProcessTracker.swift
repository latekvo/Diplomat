import Foundation
import AppKit

// Tracking for the detached `claude` sessions the Review / Resolve-conflicts
// wizards spawn. A spawn opens a fully detached terminal window, so there is no
// child process to wait on; instead we capture three OS-level handles at spawn
// time — the terminal window id, the session id, and the controlling tty — plus a
// sentinel "done" file the shell touches when `claude` returns. From those we can
// (a) tell whether the session is still alive (poll the tty + the sentinel) and
// (b) bring its window back to the front on demand (AppleScript by window id).

// MARK: - Tracked process model

/// One dispatched agent session shown in the applet's ongoing-processes list.
/// Persisted in UserDefaults so the list survives an applet restart (the daemon
/// is rebuilt/relaunched often) — the tty / window / sentinel references are all
/// OS-level and outlive this process.
struct TrackedProcess: Identifiable, Codable, Equatable {
    let id: UUID
    /// "review" or "conflicts" — drives the row's icon/tint.
    var kind: String
    /// Human label, e.g. "Review · #337 · Deep" or "Resolve · my PRs".
    var label: String
    /// The terminal it runs in ("iterm" / "terminal"), for the focus AppleScript.
    var terminal: String
    /// Terminal window id (string form) — the focus target.
    var windowID: String
    /// iTerm session id (GUID); empty for Terminal.app.
    var sessionID: String
    /// Controlling tty, e.g. "/dev/ttys016" — the liveness probe.
    var tty: String
    /// Sentinel file the shell writes when `claude` returns (`…; printf … > done`).
    var donePath: String
    /// The single PR this session concerns, if any — the open-in-browser fallback
    /// when its window can't be focused.
    var prURL: String?
    var createdAt: Date
    /// Recomputed by the poller: true once `claude` has returned (sentinel present)
    /// or the window is gone. Persisted only as a cache; the next poll corrects it.
    var done: Bool
    /// Recomputed by a full refresh (the "Update"): true once this session's PR has
    /// been MERGED on GitHub. A definitive, terminal outcome that outranks `done`
    /// (which only means the local `claude` process exited). Persisted as a cache;
    /// the next refresh corrects it. Always false for sessions with no PR.
    var merged: Bool

    init(id: UUID = UUID(), kind: String, label: String, terminal: String,
         windowID: String, sessionID: String, tty: String, donePath: String,
         prURL: String?, createdAt: Date = Date(), done: Bool = false,
         merged: Bool = false) {
        self.id = id
        self.kind = kind
        self.label = label
        self.terminal = terminal
        self.windowID = windowID
        self.sessionID = sessionID
        self.tty = tty
        self.donePath = donePath
        self.prURL = prURL
        self.createdAt = createdAt
        self.done = done
        self.merged = merged
    }

    /// Tolerant decode: the recomputed cache flags (`done`, `merged`) may be absent
    /// in a record persisted by an older build, so default them to false rather than
    /// failing the whole list's decode.
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
        createdAt = try c.decode(Date.self, forKey: .createdAt)
        done = try c.decodeIfPresent(Bool.self, forKey: .done) ?? false
        merged = try c.decodeIfPresent(Bool.self, forKey: .merged) ?? false
    }

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
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        proc.arguments = ["-e", script]
        let out = Pipe()
        proc.standardOutput = out
        proc.standardError = Pipe()
        do { try proc.run() } catch { return nil }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        guard proc.terminationStatus == 0 else { return nil } // couldn't query → unknown
        let s = String(data: data, encoding: .utf8) ?? ""
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
    static func sweep(_ procs: [TrackedProcess], now: Date = Date(),
                      openWindows: ((SpawnTerminal) -> Set<String>?)? = nil) -> Sweep {
        guard !procs.isEmpty else { return Sweep(refreshed: procs, closedIDs: []) }
        let resolve = openWindows ?? { openWindowIDs(term: $0) }
        let fm = FileManager.default
        // One window-id query per distinct terminal app (nil = couldn't determine).
        var openByTerm: [String: Set<String>?] = [:]
        for t in Set(procs.map { $0.terminal }) {
            openByTerm[t] = resolve(SpawnTerminal(rawValue: t) ?? .iterm)
        }
        var closed = Set<UUID>()
        let out = procs.map { p -> TrackedProcess in
            var p = p
            let sentinel = !p.donePath.isEmpty && fm.fileExists(atPath: p.donePath)
            var windowClosed = false
            if !p.windowID.isEmpty,
               now.timeIntervalSince(p.createdAt) > graceInterval,
               let open = openByTerm[p.terminal] ?? nil {
                windowClosed = !open.contains(p.windowID)
            }
            if windowClosed { closed.insert(p.id) }
            p.done = sentinel || windowClosed
            return p
        }
        return Sweep(refreshed: out, closedIDs: closed)
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

    /// Bring the session's terminal window to the front. Returns false when the
    /// window no longer exists (closed) or AppleScript errors — the caller then
    /// falls back to opening the PR, then to a "tracking lost" notice.
    @discardableResult
    static func focus(_ p: TrackedProcess) -> Bool {
        guard !p.windowID.isEmpty else { return false }
        let term = SpawnTerminal(rawValue: p.terminal) ?? .iterm
        let script = focusScript(term: term, windowID: p.windowID, sessionID: p.sessionID)
        return runOsascriptSilently(script)
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

    /// Run an AppleScript, discard output, return whether it exited 0.
    private static func runOsascriptSilently(_ script: String) -> Bool {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        proc.arguments = ["-e", script]
        proc.standardOutput = Pipe()
        proc.standardError = Pipe()
        do { try proc.run() } catch { return false }
        proc.waitUntilExit()
        return proc.terminationStatus == 0
    }
}
