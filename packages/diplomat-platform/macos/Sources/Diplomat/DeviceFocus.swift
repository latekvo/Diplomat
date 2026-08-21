import Foundation
import AppKit

// Best-effort "click an in-use device → focus the terminal running the agent that
// holds it". The device pool only knows the owner by its MCP-forwarder PID, so we
// resolve that PID's controlling tty (which the agent's session shares), then bring
// that window forward. Two paths:
//   1. Precise — the applet itself spawned that agent, so a run with the same tty has
//      a window handle recorded; raise it (`AgentWindows.focus`).
//   2. Fallback — any other agent session; walk out to its window (`TerminalFocus`).
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
        return AgentProbes.shortTTY(out)
    }

    /// Bring forward the terminal window of the agent holding `dev`. Returns false
    /// when it can't be resolved (no owner PID, PID dead, no tty, window gone).
    @discardableResult
    static func focus(_ dev: DeviceAllocation, tracked: [Store.AgentRow]) -> Bool {
        guard let pid = dev.owner?.ownerPid, let t = tty(forPid: pid) else { return false }
        // Precise: an applet-spawned session with this exact tty.
        if let handle = tracked.first(where: { $0.record.tty == t })?.window,
           AgentWindows.focus(handle) { return true }
        return TerminalFocus.focus(tty: t, pid: pid)
    }
}
