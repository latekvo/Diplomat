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
        let panes = panes(), clients = clients()
        let walk = walk(tty: AgentProbes.shortTTY(tty), pid: pid, processes: processes(),
                        panes: panes, clients: clients)
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
        return raiseGhostty(walk, panes: panes, clients: clients)
    }

    /// The last route out, for a tty neither scriptable terminal admitted to showing.
    ///
    /// Ghostty answers no question about which window is on which tty, so there is
    /// nothing here to match the way `itermScript` matches on one. The window's TITLE
    /// stands in: a spawn tells tmux to write the session's name there
    /// (`AgentSpawner.ghosttyLauncher`), and that name is on the pane this walk has just
    /// come through. So a run this applet opened is raised exactly whether or not it
    /// still has the handle its run directory held — which retirement deletes out from
    /// under an agent that is still working, and which is the whole reason this path is
    /// reached for a spawn of our own at all.
    ///
    /// The bare activate is what remains for a session this applet did not name, and for
    /// one opened before its window was titled. It brings Ghostty forward without picking
    /// a window, which on a machine with several open is the wrong one more often than
    /// not — but the alternative is reporting no window for an agent plainly sitting in one.
    ///
    /// Only for a pane a client is attached to. An unattached session is on nobody's
    /// screen, and raising an app over one would report a window that does not exist.
    private static func raiseGhostty(_ walk: Walk, panes: [String: Pane],
                                     clients: [String: String]) -> Bool {
        let attached = walk.ttys.contains { tty in
            guard let session = panes[tty]?.session else { return false }
            return clients[session] != nil
        }
        guard attached, isRunning("com.mitchellh.ghostty") else { return false }
        if let session = ownSession(on: walk.ttys, panes),
           OSAScript.runSilently(ghosttyRaiseScript(session: session)) { return true }
        return OSAScript.runSilently("tell application \"Ghostty\" to activate")
    }

    /// Raise the Ghostty window titled `session`, where a spawn had tmux write the name.
    ///
    /// The specifier errors (-1719) when no window carries the title, which is what sends
    /// a session opened before titles on to the activate above rather than reporting a
    /// raise that did not happen.
    static func ghosttyRaiseScript(session: String) -> String {
        """
        tell application "Ghostty"
            activate window (first window whose name is "\(session)")
            activate
        end tell
        """
    }

    /// Close the terminal window running `tty`, or whatever wraps it — the mirror of
    /// `focus`, and the only route to the window of a run that kept no handle.
    ///
    /// The window reaper is what calls it in earnest, for a run a BACKSTOP ended
    /// (`AgentState.reapable`) — twenty minutes of a screen that has not moved, so
    /// nothing is being read and nothing is being typed, or the four-hour deadline the
    /// operator left on, where the agent may well be working and the bay was asked for
    /// anyway. (`TrackTest` also closes the throwaway window it opened.)
    /// What it performs is the act the operator would: a wrapped session's
    /// window belongs to the tmux CLIENT, so closing it detaches the session exactly as a
    /// hand on that window would, rather than reaching past it to kill something the
    /// operator may share.
    ///
    /// The panes are not revealed as `focus` reveals them: selecting a pane in a window
    /// about to close shows nobody anything.
    ///
    /// The last resort ends a tmux session outright, which is the one thing the paragraph
    /// above says not to do — and it is allowed only for a session THIS APPLET named
    /// (`sessionPrefix`). Ghostty tells nobody which tty a window is on, so a Ghostty run
    /// that kept no handle has no window to walk to and no other way to be ended; a run
    /// this applet opened is alone in a session it made up a name for, so ending it can
    /// reach nothing the operator shares. An agent found sitting in the operator's own
    /// tmux session is still left exactly as it was.
    ///
    /// False means no terminal admitted to showing any tty on the way out: the window is
    /// already gone, the agent is not in a terminal at all, or automation is not granted.
    @discardableResult
    static func close(tty: String, pid: Int? = nil) -> Bool {
        let panes = panes()
        let walk = walk(tty: AgentProbes.shortTTY(tty), pid: pid, processes: processes(),
                        panes: panes, clients: clients())
        guard !walk.ttys.isEmpty else { return false }
        let paths = walk.ttys.map { "/dev/\($0)" }
        if isRunning("com.googlecode.iterm2"),
           OSAScript.runSilently(itermCloseScript(paths)) { return true }
        if isRunning("com.apple.Terminal"),
           OSAScript.runSilently(terminalCloseScript(paths)) { return true }
        return ownSession(on: walk.ttys, panes).map(endGhostty(session:)) ?? false
    }

    /// End a Ghostty run this applet named: the session, which takes the agent with it,
    /// and then the window, which ending the session does not. Closing a Ghostty window
    /// leaves what runs in it running, and the reverse holds too — kill the session and
    /// the window stays, showing a dead shell for as long as the operator leaves it there.
    /// Both halves are the reap; only together are they the act a hand on that window
    /// would perform.
    ///
    /// The window is addressable at all because the spawn titled it
    /// (`AgentSpawner.ghosttyLauncher`), which is what a run with no handle has instead of
    /// a window id. One opened before titles loses its session and keeps its window, which
    /// is what every run through here used to get.
    ///
    /// Answers on the kill. The window is what the operator sees, but the agent is what
    /// the backstop was ending, and a run whose agent is gone must be priced rather than
    /// held back over a window that would not close.
    private static func endGhostty(session: String) -> Bool {
        let killed = killSession(named: session)
        OSAScript.runSilently(ghosttyCloseScript(session: session))
        return killed
    }

    /// Close the Ghostty window titled `session`. `close window` is the verb — plain
    /// `close` closes a surface — and the specifier errors when no window carries the
    /// title, which for a window the operator shut first is not a failed reap.
    static func ghosttyCloseScript(session: String) -> String {
        """
        tell application "Ghostty"
            try
                close window (first window whose name is "\(session)")
            end try
        end tell
        """
    }

    /// The name of a tmux session this applet opened, if one of these ttys is a pane in
    /// one. Prefix-matched, which is why the prefix is not something an operator would
    /// type: it is the whole permission to end a session rather than detach from it.
    private static func ownSession(on ttys: [String], _ panes: [String: Pane]) -> String? {
        ttys.compactMap { panes[$0]?.session }.first { $0.hasPrefix(sessionPrefix) }
    }

    /// What every tmux session this applet opens is named for. `AgentSpawner` builds the
    /// names; `ownSession` is why they are recognisable.
    static let sessionPrefix = "diplomat-"

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
    static func panes() -> [String: Pane] { readPanes() ?? [:] }

    /// The same listing, with "tmux would not answer" kept apart from "tmux has nothing"
    /// — see `walkTables`, the one caller that has to tell them apart.
    private static func readPanes() -> [String: Pane]? {
        guard let out = tmux(["list-panes", "-a", "-F",
                              "#{pane_tty}\(unit)#{pane_id}\(unit)#{session_name}"])
        else { return nil }
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
    static func clients() -> [String: String] { readClients() ?? [:] }

    /// The same listing, failure kept apart from emptiness — see `walkTables`.
    private static func readClients() -> [String: String]? {
        guard let out = tmux(["list-clients", "-F", "#{client_session}\(unit)#{client_tty}"])
        else { return nil }
        var found: [String: String] = [:]
        for line in out.split(separator: "\n") {
            let cols = line.components(separatedBy: unit)
            guard cols.count == 2, !cols[0].isEmpty else { continue }
            found[cols[0]] = AgentProbes.shortTTY(cols[1])
        }
        return found
    }

    /// Both listings `walk` hops across, or nil when a LIVE tmux server would not answer.
    ///
    /// `panes()` and `clients()` answer `[:]` on any failure, which is right where the
    /// answer is which window to raise — a walk with nothing to hop across simply stops
    /// — and wrong where it decides whether a line of text is typed into somebody's
    /// shell. There, one failed `list-clients` empties the agent-tty set, which reads
    /// identically to "no agent is up" and prunes the API-error watcher's backoff and
    /// idle-confirmation state for every session in it. Not knowing has to mean not
    /// typing, and it must not also mean forgetting.
    ///
    /// The nil is narrow on purpose. Both listings fail against a machine with no tmux
    /// and against one whose server has shut down, and neither is a failure: there is
    /// genuinely nothing to hop across. `paneScreens` separates them the same way, for
    /// the same reason — a server with no panes shuts itself down, so an empty answer
    /// from a LIVE server is a failed command rather than an empty machine.
    static func walkTables() -> (panes: [String: Pane], clients: [String: String])? {
        guard binary != nil else { return ([:], [:]) }
        guard let panes = readPanes(), let clients = readClients() else {
            return serverRunning() ? nil : ([:], [:])
        }
        return (panes, clients)
    }

    /// Select `pane` in its own window and session, so the client attached to it is
    /// showing the agent when the window comes forward. Best-effort: a pane that has
    /// closed is a window that will simply show whatever it was showing.
    private static func reveal(_ pane: String) {
        _ = tmux(["select-window", "-t", pane])
        _ = tmux(["select-pane", "-t", pane])
    }

    /// Whether tmux can be driven at all. What `SpawnTerminal.unavailableReason` asks
    /// before offering Ghostty, whose agents are only readable through it.
    static var tmuxAvailable: Bool { binary != nil }

    /// Every tmux pane's visible screen, keyed by the pane's own tty.
    ///
    /// The Swift twin of `tmuxwatch.dump_panes`, and it earns a place on a platform whose
    /// terminals are already scriptable because Ghostty is not one of them: its dictionary
    /// exposes no visible text and no tty, so `capture-pane` is the only way to read what
    /// a Ghostty agent has on screen. Folding it into the same dump the iTerm and Terminal
    /// scripts feed keeps every consumer — the API-error scan, the panel's screen tails,
    /// the stillness backstop — asking one question keyed one way.
    ///
    /// The pane's tty IS the agent's under a Ghostty spawn, because tmux is the window's
    /// command there and nothing else wraps it. Under iTerm it is not the window's, which
    /// is what `AgentProbes.adoptWrappedTails` is for; both ttys land in the dump, and
    /// they are different keys, so neither displaces the other.
    ///
    /// Empty when tmux is absent or no server is running — ordinary inert states, not
    /// failures. nil only when a server is up and the listing still came back empty,
    /// which the callers must read as "we could not look" rather than "nothing is there".
    ///
    /// `shownOn` is the ttys a scriptable terminal has already reported; see
    /// `panesToCapture` for what it excludes and why.
    static func paneScreens(shownOn: Set<String>) -> [(tty: String, screen: String)]? {
        guard binary != nil else { return [] }
        let table = panes()
        // A server with no panes shuts itself down, so an empty listing against a live
        // server is a failed command, not an empty machine.
        if table.isEmpty { return serverRunning() ? nil : [] }
        var out: [(tty: String, screen: String)] = []
        for (tty, pane) in panesToCapture(table, processes: processes(),
                                          attachedTo: clients(), shownOn: shownOn) {
            // A pane that closed between the listing and the capture is skipped, not
            // recorded empty: an empty screen reads as one with nothing on it.
            guard let screen = tmux(["capture-pane", "-p", "-t", pane.id]) else { continue }
            out.append((tty: tty, screen: screen))
        }
        return out
    }

    /// Which panes `paneScreens` captures: the ones no scriptable terminal is showing.
    /// A pane already in the dump under its window's tty would be a second entry for one
    /// screen, and both spellings pass the "is an agent behind this?" gate — so a stalled
    /// agent would be nudged twice and audited twice.
    ///
    /// The whole walk and not the pane's own client, because the client's tty is not the
    /// one a terminal reports being on: a shell wrapper (`kiro-cli-term` and its kind)
    /// runs the real shell in a pty of its own, so the client sits one below the window.
    /// Comparing those two would match nothing on exactly the machine this is for.
    ///
    /// A session with no client is kept: nothing is showing it, so `capture-pane` is its
    /// only reader. Ordered by tty so a dump is stable across ticks. Pure, so the rule is
    /// decidable without a tmux server — the way `walk` is decidable without one.
    static func panesToCapture(_ table: [String: Pane], processes: [Int: Proc],
                               attachedTo: [String: String],
                               shownOn: Set<String>) -> [(tty: String, pane: Pane)] {
        table.filter { tty, _ in
            guard !tty.isEmpty else { return false }
            return !walk(tty: tty, pid: nil, processes: processes, panes: table,
                         clients: attachedTo).ttys.contains(where: shownOn.contains)
        }
        .map { (tty: $0.key, pane: $0.value) }
        .sorted { $0.tty < $1.tty }
    }

    /// Type `text` into the tmux pane on `tty` and submit it. Returns whether a pane on
    /// that tty took it, so a caller never counts a nudge that landed nowhere.
    ///
    /// The twin of `tmuxwatch.send_continue`. `-l` sends the text literally, so a message
    /// containing tmux key names is not read as keys.
    static func sendLine(tty: String, text: String) -> Bool {
        guard let pane = panes()[AgentProbes.shortTTY(tty)] else { return false }
        guard tmux(["send-keys", "-t", pane.id, "-l", text]) != nil else { return false }
        return tmux(["send-keys", "-t", pane.id, "Enter"]) != nil
    }

    /// End the tmux session by name, taking the agent running in it with it.
    ///
    /// The Ghostty half of a reap, and the reason one is needed: closing a Ghostty window
    /// does not end what is running in it. Measured on Ghostty 1.3.1 — the window goes,
    /// the session stays, and the agent keeps running headless on a pane whose client is
    /// no longer on screen, holding a bay that nothing left can retire. iTerm and Terminal
    /// take a session's processes down with its window, so only this path needs it.
    ///
    /// `=` is tmux's exact-match target prefix. Without it a name is matched as a prefix
    /// and then as a pattern, so reaping `diplomat-a1b2` could take the operator's own
    /// session with it.
    static func killSession(named session: String) -> Bool {
        guard !session.isEmpty else { return false }
        return tmux(["kill-session", "-t", "=" + session]) != nil
    }

    /// Whether a tmux server is up. Separates "no panes because nothing is running" from
    /// "no panes because the command failed" — `tmuxwatch._server_running`.
    private static func serverRunning() -> Bool { tmux(["has-session"]) != nil }

    /// Between a tmux format's fields — a unit separator cannot occur in a tty path,
    /// a pane id or a session name.
    private static let unit = "\u{1f}"

    /// A client told to assume UTF-8: without `-u`, one started with no `$TMUX` and no
    /// UTF-8 in `LC_ALL`/`LC_CTYPE`/`LANG` - what launchd gives the app - has every
    /// control byte in a command's output rewritten as `_`, `unit` included.
    private static func tmux(_ arguments: [String]) -> String? {
        guard let bin = binary else { return nil }
        return run(bin, ["-u"] + arguments)
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
