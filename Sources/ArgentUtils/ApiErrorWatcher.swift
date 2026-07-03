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
    /// that is ALREADY running — never launches iTerm/Terminal.
    static func dumpSessions() -> [Session] {
        var out: [Session] = []
        if isRunning("com.googlecode.iterm2") { out += parse(run(itermDumpScript)) }
        if isRunning("com.apple.Terminal") { out += parse(run(terminalDumpScript)) }
        return out
    }

    /// Send the continue nudge to whichever session/tab owns `tty` (submits it as the
    /// next line of input — iTerm `write text` / Terminal `do script … in tab`).
    static func sendContinue(tty: String) {
        let msg = escape(continueMessage)
        if isRunning("com.googlecode.iterm2") { _ = run(itermSendScript(tty: tty, msg: msg)) }
        if isRunning("com.apple.Terminal") { _ = run(terminalSendScript(tty: tty, msg: msg)) }
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

    @discardableResult
    private static func run(_ script: String) -> String {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        p.arguments = ["-e", script]
        let outPipe = Pipe()
        p.standardOutput = outPipe
        p.standardError = Pipe()
        do { try p.run() } catch { return "" }
        let data = outPipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
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

    private static func itermSendScript(tty: String, msg: String) -> String {
        """
        tell application "iTerm"
          repeat with w in windows
            repeat with t in tabs of w
              repeat with s in sessions of t
                if (tty of s) is "\(tty)" then
                  tell s to write text "\(msg)"
                end if
              end repeat
            end repeat
          end repeat
        end tell
        """
    }

    private static func terminalSendScript(tty: String, msg: String) -> String {
        """
        tell application "Terminal"
          repeat with w in windows
            repeat with t in tabs of w
              if (tty of t) is "\(tty)" then
                do script "\(msg)" in t
              end if
            end repeat
          end repeat
        end tell
        """
    }
}
