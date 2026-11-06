import Foundation

/// The mark a deliberate quit leaves, so the unattended jobs that bring a dead app back
/// (the 06:00 updater, the watchdog) leave it closed. The next GUI launch clears it; a
/// crash, a kill, or a hand-over to a newer instance never sets it. Mirrors the Linux
/// front-end's `selfupdate.mark_operator_quit`.
enum OperatorQuit {
    /// `WatchdogTest` points it at a scratch file.
    static var url = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".diplomat/operator-quit")

    /// Space set aside for the mark. A full APFS volume refuses even an empty file, but
    /// still lets one be deleted, and the space a deletion frees takes the mark: 32 KiB
    /// was not always enough on a filled volume, 64 KiB was.
    static var reserveURL: URL { url.appendingPathExtension("reserve") }
    static let reserveBytes = 1 << 20

    static var isMarked: Bool { FileManager.default.fileExists(atPath: url.path) }

    /// The mark is the file being there; the time in it is for a human.
    static func mark() {
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        func create() -> Bool {
            let fd = open(url.path, O_WRONLY | O_CREAT | O_TRUNC, 0o644)
            guard fd >= 0 else { return false }
            let stamp = Data("\(ISO8601DateFormatter().string(from: Date()))\n".utf8)
            _ = stamp.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
            close(fd)
            return true
        }
        if create() { return }
        try? FileManager.default.removeItem(at: reserveURL)
        _ = create()
    }

    /// What every GUI launch does: lift the mark, and set space aside for the next one.
    static func clear() {
        try? FileManager.default.removeItem(at: url)
        let size = (try? FileManager.default.attributesOfItem(atPath: reserveURL.path))?[.size]
        guard (size as? Int) != reserveBytes else { return }
        try? Data(count: reserveBytes).write(to: reserveURL, options: .atomic)
    }

    /// `senderIsDiplomat` is nil for a quit this process asked for itself (the Quit
    /// button), else whether the quit Apple event came from a Diplomat process: the
    /// singleton's `terminate()` from a newer instance. `endsSession` is a logout,
    /// restart or shutdown, which asks every app to quit before it can still be
    /// cancelled; marked, a cancelled one would leave the app dead for the rest of the
    /// session. Anything else (`osascript`, Activity Monitor's Quit) is the operator.
    static func isDeliberate(senderIsDiplomat: Bool?, endsSession: Bool) -> Bool {
        senderIsDiplomat != true && !endsSession
    }
}
