import Foundation

/// The mark a deliberate quit leaves, so the unattended jobs that bring a dead app back
/// (the 06:00 updater, the watchdog) leave it closed. The next GUI launch clears it; a
/// crash, a kill, or a hand-over to a newer instance never sets it. Mirrors the Linux
/// front-end's `selfupdate.mark_operator_quit`.
enum OperatorQuit {
    /// `WatchdogTest` points it at a scratch file.
    static var url = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".diplomat/operator-quit")

    static var isMarked: Bool { FileManager.default.fileExists(atPath: url.path) }

    static func mark() {
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? Data("\(ISO8601DateFormatter().string(from: Date()))\n".utf8).write(to: url)
    }

    static func clear() { try? FileManager.default.removeItem(at: url) }

    /// `senderIsDiplomat` is nil for a quit this process asked for itself (the Quit
    /// button), else whether the quit Apple event came from a Diplomat process: the
    /// singleton's `terminate()` from a newer instance. Anything else is the operator:
    /// `osascript`, Activity Monitor's Quit, and a logout, whose mark the login agent's
    /// launch at the next login clears.
    static func isDeliberate(senderIsDiplomat: Bool?) -> Bool { senderIsDiplomat != true }
}
