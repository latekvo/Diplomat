import Foundation
import AppKit

// Best-effort "click an in-use device → focus the terminal running the agent that
// holds it". The device pool only knows the owner by its MCP-forwarder PID, so we
// resolve that PID's controlling tty (which the agent's `claude` session shares),
// then bring that window forward. Two paths:
//   1. Precise — the applet itself spawned that agent, so a TrackedProcess with the
//      same tty exists; reuse ProcessMonitor.focus (window/session id).
//   2. Fallback — any other claude session; locate the tty across iTerm/Terminal.
// Either can fail (agent not in a terminal, window closed, unsupported terminal);
// the caller treats a false return as a silent no-op.
enum DeviceFocus {
    /// The controlling tty of `pid` as `ps` reports it in short form (e.g. "ttys012"),
    /// or nil when the process is gone or has no controlling terminal ("??").
    static func tty(forPid pid: Int) -> String? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/ps")
        p.arguments = ["-o", "tty=", "-p", String(pid)]
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = Pipe()
        do { try p.run() } catch { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        guard let out = String(data: data, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines),
              !out.isEmpty, out != "??" else { return nil }
        return out.hasPrefix("/dev/") ? String(out.dropFirst(5)) : out
    }

    /// Bring forward the terminal window of the agent holding `dev`. Returns false
    /// when it can't be resolved (no owner PID, PID dead, no tty, window gone).
    @discardableResult
    static func focus(_ dev: DeviceAllocation, tracked: [TrackedProcess]) -> Bool {
        guard let pid = dev.owner?.ownerPid, let t = tty(forPid: pid) else { return false }
        // Precise: an applet-spawned session with this exact tty.
        if let p = tracked.first(where: { !$0.tty.isEmpty && $0.shortTTY == t }),
           ProcessMonitor.focus(p) { return true }
        // Fallback: find the tty across the known terminals.
        return focusByTTY(t)
    }

    // MARK: tty → window

    /// Only queries an app that is ALREADY running — a bare `tell application` would
    /// LAUNCH iTerm/Terminal just to be told the tty isn't there (every sibling
    /// watcher guards this way; DeviceFocus was the one that skipped it).
    private static func isRunning(_ bundleID: String) -> Bool {
        !NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).isEmpty
    }

    private static func focusByTTY(_ tty: String) -> Bool {
        let devPath = "/dev/\(tty)"
        if isRunning("com.googlecode.iterm2"), runSilently(itermByTTY(devPath)) { return true }
        if isRunning("com.apple.Terminal"), runSilently(terminalByTTY(devPath)) { return true }
        return false
    }

    private static func itermByTTY(_ devPath: String) -> String {
        """
        tell application "iTerm"
            set _hit to false
            repeat with w in windows
                repeat with t in tabs of w
                    repeat with s in sessions of t
                        if (tty of s) is "\(devPath)" then
                            activate
                            select w
                            select t
                            tell t to select s
                            set _hit to true
                        end if
                    end repeat
                end repeat
            end repeat
            if not _hit then error "tty not found"
        end tell
        """
    }

    private static func terminalByTTY(_ devPath: String) -> String {
        """
        tell application "Terminal"
            set _hit to false
            repeat with w in windows
                repeat with t in tabs of w
                    if (tty of t) is "\(devPath)" then
                        activate
                        set index of w to 1
                        set frontmost of w to true
                        set selected of t to true
                        set _hit to true
                    end if
                end repeat
            end repeat
            if not _hit then error "tty not found"
        end tell
        """
    }

    private static func runSilently(_ script: String) -> Bool {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        p.arguments = ["-e", script]
        p.standardOutput = Pipe()
        p.standardError = Pipe()
        do { try p.run() } catch { return false }
        p.waitUntilExit()
        return p.terminationStatus == 0
    }
}
