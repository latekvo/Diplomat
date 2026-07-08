import Foundation
import AppKit
import ArgentUtilsCore

// Watches every iTerm session + Terminal tab for a Claude CLI API-error line and, when
// one is found, sends a short "continue" message to that exact session — so an agent
// that stalled on a transient server error (e.g. overnight 529 overload) resumes on its
// own. Detection (ApiErrorMatch) runs only over the last few visible lines, so it fires
// on a session that just errored, not one that merely mentions the phrase in scrollback.
// The tty is the unifying key across both terminals.
enum ApiErrorWatcher {
    static let continueMessage = "Go on, there was a Claude API error, continue as normal"

    /// How many non-empty visible lines from the bottom we scan for the error. A tall
    /// prompt/status box under the error line can push it ~17 lines up, so 30 keeps it in
    /// view while still staying out of older scrollback.
    static let scannedTailLines = 30

    struct Session { let tty: String; let tail: String }

    /// The last visible lines of every session/tab, keyed by tty. Only queries an app
    /// that is ALREADY running — never launches iTerm/Terminal. Returns nil when any
    /// dump script FAILED (automation permission revoked, AppleEvent timeout…) —
    /// callers must treat that as "unknown", not "no sessions": acting on a silent
    /// empty result used to make the watcher inert with no signal, and would wrongly
    /// reset every backoff/liveness decision keyed on the session list.
    static func dumpSessions() -> [Session]? {
        var out: [Session] = []
        if isRunning("com.googlecode.iterm2") {
            guard let dump = run(itermDumpScript) else { return nil }
            out += parse(dump)
        }
        if isRunning("com.apple.Terminal") {
            guard let dump = run(terminalDumpScript) else { return nil }
            out += parse(dump)
        }
        return out
    }

    /// A short-lived cache over `dumpSessions`: the 8s process sweep and the 20s
    /// API-error scan each dumped EVERY session's full visible buffer over AppleEvents;
    /// sharing one dump between near-simultaneous callers halves that traffic.
    private static let cacheLock = NSLock()
    private static var cachedDump: (at: Date, sessions: [Session]?)?

    static func dumpSessionsCached(maxAge: TimeInterval = 5) -> [Session]? {
        cacheLock.lock()
        if let c = cachedDump, Date().timeIntervalSince(c.at) < maxAge {
            let s = c.sessions
            cacheLock.unlock()
            return s
        }
        cacheLock.unlock()
        let fresh = dumpSessions()
        cacheLock.lock()
        cachedDump = (Date(), fresh)
        cacheLock.unlock()
        return fresh
    }

    /// Send the continue nudge to whichever session/tab owns `tty` (submits it as the
    /// next line of input — iTerm `write text` / Terminal `do script … in tab`).
    /// Returns whether a session with that tty was actually found and written to —
    /// the caller must not count/audit a nudge that never landed.
    @discardableResult
    static func sendContinue(tty: String) -> Bool {
        let msg = escape(continueMessage)
        var sent = false
        if isRunning("com.googlecode.iterm2"), run(itermSendScript(tty: tty, msg: msg)) != nil {
            sent = true
        }
        if !sent, isRunning("com.apple.Terminal"), run(terminalSendScript(tty: tty, msg: msg)) != nil {
            sent = true
        }
        return sent
    }

    // MARK: helpers

    private static func isRunning(_ bundleID: String) -> Bool {
        !NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).isEmpty
    }

    private static let unitSep = "\u{1F}"    // between tty and its tail
    private static let recordSep = "\u{1E}"  // between sessions

    private static func parse(_ s: String) -> [Session] {
        s.components(separatedBy: recordSep).compactMap { rec in
            guard let r = rec.range(of: unitSep) else { return nil }
            let tty = String(rec[rec.startIndex..<r.lowerBound])
                .trimmingCharacters(in: .whitespacesAndNewlines)
            let tail = lastLines(String(rec[r.upperBound...]), scannedTailLines)
            return tty.isEmpty ? nil : Session(tty: tty, tail: tail)
        }
    }

    /// The last `n` non-empty visible lines — enough to catch a stall's error line even
    /// under a tall prompt/status box, without matching the phrase in older scrollback.
    private static func lastLines(_ text: String, _ n: Int) -> String {
        let lines = text.split(whereSeparator: \.isNewline).filter {
            !$0.trimmingCharacters(in: .whitespaces).isEmpty
        }
        return lines.suffix(n).joined(separator: "\n")
    }

    private static func escape(_ s: String) -> String {
        s.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
    }

    /// Runs an AppleScript; nil on ANY failure (launch, non-zero exit). A failure used
    /// to come back as "" — indistinguishable from "no sessions", hiding a revoked
    /// automation permission forever.
    private static func run(_ script: String) -> String? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        p.arguments = ["-e", script]
        let outPipe = Pipe()
        p.standardOutput = outPipe
        p.standardError = Pipe()
        do { try p.run() } catch { return nil }
        let data = outPipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        guard p.terminationStatus == 0 else { return nil }
        return String(data: data, encoding: .utf8) ?? ""
    }

    // MARK: AppleScript

    // Returns "<tty>\u{1F}<visible contents>\u{1E}" per session. ASCII 31/30 are the
    // separators (they never appear in terminal text). `contents` must be assigned to a
    // variable first — iTerm errors on `<compound> of (contents of s)` (-1728). Swift
    // trims to the last lines; the visible screen is already bounded.
    private static var itermDumpScript: String {
        """
        tell application "iTerm"
          set res to ""
          repeat with w in windows
            repeat with t in tabs of w
              repeat with s in sessions of t
                set c to ""
                try
                  set c to (contents of s) as text
                end try
                set res to res & (tty of s) & (ASCII character 31) & c & (ASCII character 30)
              end repeat
            end repeat
          end repeat
          return res
        end tell
        """
    }

    private static var terminalDumpScript: String {
        """
        tell application "Terminal"
          set res to ""
          repeat with w in windows
            repeat with t in tabs of w
              set c to ""
              try
                set c to (contents of t) as text
              end try
              set res to res & (tty of t) & (ASCII character 31) & c & (ASCII character 30)
            end repeat
          end repeat
          return res
        end tell
        """
    }

    // Both send scripts error when no session owns the tty, so the caller can tell
    // "nudge delivered" from "tty not found / window closed" via the exit status.
    private static func itermSendScript(tty: String, msg: String) -> String {
        """
        tell application "iTerm"
          set _hit to false
          repeat with w in windows
            repeat with t in tabs of w
              repeat with s in sessions of t
                if (tty of s) is "\(tty)" then
                  tell s to write text "\(msg)"
                  set _hit to true
                end if
              end repeat
            end repeat
          end repeat
          if not _hit then error "tty not found"
        end tell
        """
    }

    private static func terminalSendScript(tty: String, msg: String) -> String {
        """
        tell application "Terminal"
          set _hit to false
          repeat with w in windows
            repeat with t in tabs of w
              if (tty of t) is "\(tty)" then
                do script "\(msg)" in t
                set _hit to true
              end if
            end repeat
          end repeat
          if not _hit then error "tty not found"
        end tell
        """
    }
}
