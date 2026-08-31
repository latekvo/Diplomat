import AppKit
import Foundation

/// Raising the terminal window a process is running in, through whatever sits between
/// the two.
///
/// A terminal window shows a tty directly only when nothing wraps it. tmux does: the
/// pane an agent runs in is a pty the tmux SERVER owns, so the pane's parent is a
/// daemon, and the window on screen belongs to the CLIENT attached to that pane's
/// session — a different tty, on a process the pane cannot reach through its parents.
/// Shell wrappers do the same in the small: `kiro-cli-term` and its kind run the real
/// shell in a pty one level below the window's.
///
/// So the window is found by walking rather than by looking up: from the process
/// outwards, taking the parent's tty at each step, and hopping across tmux where the
/// parent chain dead-ends at the server. Every tty passed on the way is a candidate,
/// tried nearest-first, and the first one a terminal admits to showing wins.
///
/// Under a plain shell the walk ends on its first step, which is the pre-tmux
/// behaviour exactly: the agent's own tty is the window's.
enum TerminalFocus {
    /// One process, as the walk needs it.
    struct Proc: Equatable {
        var ppid: Int
        /// Short form ("ttys012"), or "" for a process with no controlling terminal.
        var tty: String
    }

    /// A tmux pane: which pane id, and whose session it belongs to.
    struct Pane: Equatable {
        var id: String
        var session: String
    }

    /// The ttys to try, and the tmux panes to reveal first.
    struct Walk: Equatable {
        var ttys: [String] = []
        var panes: [String] = []
    }

    /// How many hops the walk takes before giving up. A pane sits three processes and
    /// one tmux hop from its window; the cap is only there so a cycle in a
    /// hand-assembled tree cannot spin.
    static let maxHops = 16

    /// Bring forward the terminal window running `tty`, or whatever wraps it. `pid` is
    /// where the walk starts when it is known — a tty alone can only name the
    /// processes sitting on it, which is a weaker start.
    ///
    /// False means no terminal admitted to showing any tty on the way out: the window
    /// is closed, the agent is not in a terminal at all, or automation is not granted.
    @discardableResult
    static func focus(tty: String, pid: Int? = nil) -> Bool {
        let walk = walk(tty: AgentProbes.shortTTY(tty), pid: pid, processes: processes(),
                        panes: panes(), clients: clients())
        guard !walk.ttys.isEmpty else { return false }
        // Before raising the window: put the agent's pane back on screen in it. The
        // client shows whichever pane its session has selected, which after an
        // unattended spawn is rarely the one being asked for.
        for pane in walk.panes { reveal(pane) }
        let paths = walk.ttys.map { "/dev/\($0)" }
        if isRunning("com.googlecode.iterm2"),
           OSAScript.runSilently(itermScript(paths)) { return true }
        if isRunning("com.apple.Terminal"),
           OSAScript.runSilently(terminalScript(paths)) { return true }
        return false
    }

    /// Close the terminal window running `tty`, or whatever wraps it — the mirror of
    /// `focus`, and the only route to the window of a run that kept no handle.
    ///
    /// Used for one thing: a run the quiescence backstop ended
    /// (`AgentState.Resolution.wedged`) —
    /// twenty minutes of a screen that has not moved, so nothing is being read and nothing
    /// is being typed. What it performs is the act the operator would: a wrapped session's
    /// window belongs to the tmux CLIENT, so closing it detaches the session exactly as a
    /// hand on that window would, rather than reaching past it to kill something the
    /// operator may share.
    ///
    /// The panes are not revealed as `focus` reveals them: selecting a pane in a window
    /// about to close shows nobody anything.
    ///
    /// False means no terminal admitted to showing any tty on the way out: the window is
    /// already gone, the agent is not in a terminal at all, or automation is not granted.
    @discardableResult
    static func close(tty: String, pid: Int? = nil) -> Bool {
        let walk = walk(tty: AgentProbes.shortTTY(tty), pid: pid, processes: processes(),
                        panes: panes(), clients: clients())
        guard !walk.ttys.isEmpty else { return false }
        let paths = walk.ttys.map { "/dev/\($0)" }
        if isRunning("com.googlecode.iterm2"),
           OSAScript.runSilently(itermCloseScript(paths)) { return true }
        if isRunning("com.apple.Terminal"),
           OSAScript.runSilently(terminalCloseScript(paths)) { return true }
        return false
    }

    /// The ordered ttys between a process and its window, nearest first. Pure —
    /// everything it walks over is passed in, so the shape of a wrapped session is
    /// testable without one.
    ///
    /// `panes` is keyed by pane tty and `clients` by session name, which is how the
    /// two tmux listings come back.
    static func walk(tty: String, pid: Int?, processes: [Int: Proc],
                     panes: [String: Pane], clients: [String: String]) -> Walk {
        var out = Walk()
        var seen = Set<String>()
        var tty = tty
        var pid = pid ?? leader(on: tty, processes)
        for _ in 0..<maxHops {
            guard !tty.isEmpty else { break }
            if seen.insert(tty).inserted { out.ttys.append(tty) }
            if let pane = panes[tty] {
                out.panes.append(pane.id)
                // A session nobody is attached to has no window to raise, and the walk
                // has nowhere else to go: the pane's own parent is the tmux server.
                guard let client = clients[pane.session], client != tty else { break }
                tty = client
                pid = leader(on: client, processes)
                continue
            }
            guard let here = pid, let parent = processes[here]?.ppid,
                  let up = processes[parent] else { break }
            pid = parent
            tty = up.tty
        }
        return out
    }

    /// The oldest process on a tty, which is the one nearest the terminal — every
    /// other process there descends from it.
    private static func leader(on tty: String, _ processes: [Int: Proc]) -> Int? {
        guard !tty.isEmpty else { return nil }
        return processes.filter { $0.value.tty == tty }.keys.min()
    }

    // MARK: - Reading the machine

    /// pid → parent and tty, for every process on the box.
    static func processes() -> [Int: Proc] {
        guard let out = run("/bin/ps", ["-axo", "pid=,ppid=,tty="]) else { return [:] }
        var table: [Int: Proc] = [:]
        for line in out.split(separator: "\n") {
            let cols = line.split(separator: " ", omittingEmptySubsequences: true)
            guard cols.count >= 2, let pid = Int(cols[0]), let ppid = Int(cols[1]) else { continue }
            // A process with no controlling terminal prints "??" — or nothing at all,
            // since `tty=` suppresses the header and the column is then blank.
            let tty = cols.count >= 3 && cols[2] != "??" ? String(cols[2]) : ""
            table[pid] = Proc(ppid: ppid, tty: tty)
        }
        return table
    }

    /// pane tty → the pane, for every pane the tmux server holds. Empty when tmux is
    /// not installed or no server is running, which is the same answer the walk wants:
    /// nothing to hop across.
    static func panes() -> [String: Pane] {
        guard let out = tmux(["list-panes", "-a", "-F",
                              "#{pane_tty}\(unit)#{pane_id}\(unit)#{session_name}"])
        else { return [:] }
        var found: [String: Pane] = [:]
        for line in out.split(separator: "\n") {
            let cols = line.components(separatedBy: unit)
            guard cols.count == 3, !cols[1].isEmpty else { continue }
            found[AgentProbes.shortTTY(cols[0])] = Pane(id: cols[1], session: cols[2])
        }
        return found
    }

    /// session name → the tty of a client attached to it. Last one wins, arbitrarily:
    /// several clients on one session are several windows showing the same screen, so
    /// any of them is the right window to raise.
    static func clients() -> [String: String] {
        guard let out = tmux(["list-clients", "-F", "#{client_session}\(unit)#{client_tty}"])
        else { return [:] }
        var found: [String: String] = [:]
        for line in out.split(separator: "\n") {
            let cols = line.components(separatedBy: unit)
            guard cols.count == 2, !cols[0].isEmpty else { continue }
            found[cols[0]] = AgentProbes.shortTTY(cols[1])
        }
        return found
    }

    /// Select `pane` in its own window and session, so the client attached to it is
    /// showing the agent when the window comes forward. Best-effort: a pane that has
    /// closed is a window that will simply show whatever it was showing.
    private static func reveal(_ pane: String) {
        _ = tmux(["select-window", "-t", pane])
        _ = tmux(["select-pane", "-t", pane])
    }

    /// Between a tmux format's fields — a unit separator cannot occur in a tty path,
    /// a pane id or a session name.
    private static let unit = "\u{1f}"

    private static func tmux(_ arguments: [String]) -> String? {
        guard let bin = binary else { return nil }
        return run(bin, arguments)
    }

    /// Where tmux is, or nil when it is not installed — resolved the same way `node`
    /// and `npm` are, because a menu-bar app inherits no shell PATH.
    static var binary: String? {
        let fm = FileManager.default
        if let env = ProcessInfo.processInfo.environment["DIPLOMAT_TMUX"] {
            return fm.fileExists(atPath: env) ? env : nil
        }
        return ["/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux"]
            .first { fm.fileExists(atPath: $0) }
    }

    private static func run(_ path: String, _ arguments: [String]) -> String? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: path)
        p.arguments = arguments
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = Pipe()
        do { try p.run() } catch { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        guard p.terminationStatus == 0 else { return nil }
        return String(data: data, encoding: .utf8)
    }

    // MARK: - tty → window

    /// Only queries an app that is ALREADY running — a bare `tell application` would
    /// LAUNCH iTerm/Terminal just to be told the tty isn't there.
    private static func isRunning(_ bundleID: String) -> Bool {
        !NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).isEmpty
    }

    /// The candidate ttys as an AppleScript list literal, in order.
    private static func list(_ paths: [String]) -> String {
        "{" + paths.map { "\"\($0.replacingOccurrences(of: "\"", with: "\\\""))\"" }
            .joined(separator: ", ") + "}"
    }

    /// A tty is not addressable the way a window id is, so these do walk the app's
    /// windows — but they only ever `activate` on the way out, after the match, so the
    /// reordering that comes with it cannot renumber the list still being walked.
    static func itermScript(_ paths: [String]) -> String {
        """
        tell application "iTerm"
            repeat with _t in \(list(paths))
                repeat with w in windows
                    repeat with t in tabs of w
                        repeat with s in sessions of t
                            if (tty of s) is (_t as string) then
                                select w
                                select t
                                tell t to select s
                                activate
                                return "ok"
                            end if
                        end repeat
                    end repeat
                end repeat
            end repeat
            error "no window on any of those ttys"
        end tell
        """
    }

    /// The close mirrors of the two above, erroring when no session sits on any candidate
    /// tty so the caller can tell "closed it" from "there was nothing there". Neither
    /// activates: the window is going away, and `activate` renumbers the index-based
    /// references the search is still walking.
    static func itermCloseScript(_ paths: [String]) -> String {
        """
        tell application "iTerm"
            repeat with _t in \(list(paths))
                repeat with w in windows
                    repeat with t in tabs of w
                        repeat with s in sessions of t
                            if (tty of s) is (_t as string) then
                                close w
                                return "ok"
                            end if
                        end repeat
                    end repeat
                end repeat
            end repeat
            error "no window on any of those ttys"
        end tell
        """
    }

    static func terminalCloseScript(_ paths: [String]) -> String {
        """
        tell application "Terminal"
            repeat with _t in \(list(paths))
                repeat with w in windows
                    repeat with t in tabs of w
                        if (tty of t) is (_t as string) then
                            close w
                            return "ok"
                        end if
                    end repeat
                end repeat
            end repeat
            error "no window on any of those ttys"
        end tell
        """
    }

    static func terminalScript(_ paths: [String]) -> String {
        """
        tell application "Terminal"
            repeat with _t in \(list(paths))
                repeat with w in windows
                    repeat with t in tabs of w
                        if (tty of t) is (_t as string) then
                            set index of w to 1
                            set frontmost of w to true
                            set selected of t to true
                            activate
                            return "ok"
                        end if
                    end repeat
                end repeat
            end repeat
            error "no window on any of those ttys"
        end tell
        """
    }
}
