import AppKit
import Darwin
import os

/// The applet's diagnostic log, `~/Library/Logs/diplomat.log`: what happened, when, and
/// how long it took. One line per event:
///
///     2026-11-04T16:11:24.023+01:00 [51344] window show FAILED visible=0 occlusion=hidden ...
///
/// The bracket is the pid, plus the mode for a headless run (`self_update`, `watchdog`).
/// Every line also goes to the unified log, subsystem `com.ignacy.diplomat`, where it
/// interleaves with WindowServer's and AppKit's own:
/// `log show --predicate 'subsystem == "com.ignacy.diplomat"' --last 1h`.
///
/// Past `maxBytes` the file moves to `diplomat.log.1`, replacing the one before. A
/// headless run other than the 06:00 updater and the watchdog (a self-test, render or
/// dump) writes only the unified log unless it redirects the file.
enum AppLog {
    static let subsystem = "com.ignacy.diplomat"

    /// Set by `AppLogTest`; wins over the environment.
    static var dirOverride: URL?

    static var dir: URL {
        if let dirOverride { return dirOverride }
        if let envDir { return URL(fileURLWithPath: envDir) }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs")
    }
    private static var envDir: String? {
        ProcessInfo.processInfo.environment["DIPLOMAT_LOG_DIR"].flatMap { $0.isEmpty ? nil : $0 }
    }
    /// Whether the file goes somewhere other than `~/Library/Logs`; an empty
    /// `DIPLOMAT_LOG_DIR` does not count.
    static var isRedirected: Bool { dirOverride != nil || envDir != nil }
    static var url: URL { dir.appendingPathComponent("diplomat.log") }
    static var maxBytes = 8 << 20

    /// The headless mode this process runs in, lowercased without its prefix.
    static let mode: String? = {
        let env = ProcessInfo.processInfo.environment
        return Headless.modes.keys.sorted()
            .first { name in env[name].map { Headless.isActive(in: [name: $0]) } ?? false }
            .map { $0.replacingOccurrences(of: "DIPLOMAT_", with: "").lowercased() }
    }()
    private static let tag = mode.map { "\(getpid()) \($0)" } ?? "\(getpid())"

    private static var writesFile: Bool {
        writesFile(mode: mode, redirected: isRedirected)
    }

    /// The live app and the two unattended jobs write the file; a self-test or render
    /// only when it redirected the file.
    static func writesFile(mode: String?, redirected: Bool) -> Bool {
        redirected || mode == nil || mode == "self_update" || mode == "watchdog"
    }

    private static let lock = NSLock()
    private static let stamp: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSSZZZZZ"
        return f
    }()

    /// Append one line. Callable from any thread; never throws.
    static func log(_ category: String, _ message: String) {
        Logger(subsystem: subsystem, category: category).notice("\(message, privacy: .public)")
        guard writesFile else { return }
        lock.lock(); defer { lock.unlock() }
        let line = "\(stamp.string(from: Date())) [\(tag)] \(category) \(message)\n"
        append(Data(line.utf8))
    }

    private static func append(_ data: Data) {
        let path = url.path
        func openLog() -> Int32 { open(path, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC, 0o644) }
        var fd = openLog()
        if fd < 0 {
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            fd = openLog()
        }
        guard fd >= 0 else { return }
        var st = stat()
        var atPath = stat()
        if fstat(fd, &st) == 0, Int(st.st_size) + data.count > maxBytes {
            close(fd)
            // Another process may have rotated since this one opened: renaming its fresh
            // file over `.1` would throw away the full one there.
            if stat(path, &atPath) == 0, atPath.st_ino == st.st_ino { rename(path, path + ".1") }
            fd = openLog()
            guard fd >= 0 else { return }
        }
        defer { close(fd) }
        _ = data.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
    }

    // MARK: - Timed work

    /// Seconds since boot, not counting sleep, so nothing measured spans a lid close.
    static var uptime: Double { Double(DispatchTime.now().uptimeNanoseconds) / 1e9 }

    private struct Running { let name: String; let since: Double }
    struct Tally: Equatable { var count = 0, total = 0.0, worst = 0.0 }
    private static var running: [Int: Running] = [:]
    private static var nextID = 0
    private static var tallies: [String: Tally] = [:]

    /// Runs `body` as the named activity: listed as in flight by a hang or heartbeat
    /// line while it runs, tallied into the next heartbeat, and logged on its own when
    /// it takes `slowAfter` or longer.
    static func timed<T>(_ name: String, slowAfter: Double = 10, _ body: () async -> T) async -> T {
        let (id, start) = begin(name)
        let result = await body()
        let secs = end(id, name, since: start)
        if secs >= slowAfter { log("timing", "\(name) took \(duration(secs))") }
        return result
    }

    private static func begin(_ name: String) -> (Int, Double) {
        lock.lock(); defer { lock.unlock() }
        let id = nextID
        nextID += 1
        let start = uptime
        running[id] = Running(name: name, since: start)
        return (id, start)
    }

    private static func end(_ id: Int, _ name: String, since start: Double) -> Double {
        lock.lock(); defer { lock.unlock() }
        let secs = uptime - start
        running[id] = nil
        var t = tallies[name, default: Tally()]
        t.count += 1; t.total += secs; t.worst = max(t.worst, secs)
        tallies[name] = t
        return secs
    }

    /// What is running now, oldest first: `refresh 3.2s, apiErrorScan 0.4s`.
    static func inFlight() -> String {
        let now = uptime
        lock.lock(); defer { lock.unlock() }
        let list = running.values.sorted { $0.since < $1.since }
        return list.isEmpty ? "nothing" : list.map { "\($0.name) \(duration(now - $0.since))" }
            .joined(separator: ", ")
    }

    /// Every activity's tally since the last call, which resets them.
    static func drainTallies() -> [String: Tally] {
        lock.lock(); defer { lock.unlock() }
        let drained = tallies
        tallies = [:]
        return drained
    }

    static func duration(_ secs: Double) -> String {
        if secs < 10 { return String(format: "%.2fs", secs) }
        if secs < 60 { return String(format: "%.1fs", secs) }
        let s = Int(secs.rounded())
        return s < 3600 ? "\(s / 60)m\(s % 60)s" : "\(s / 3600)h\(s % 3600 / 60)m"
    }

    // MARK: - The GUI session

    private static var observers: [NSObjectProtocol] = []

    /// Set once the app is quitting, when AppKit orders every window out on its way down.
    /// Main thread only.
    static var exiting = false

    /// The live app's launch line, its system-event lines and the hang watch. GUI only.
    @MainActor static func startSession() {
        let exe = Bundle.main.executablePath ?? CommandLine.arguments[0]
        let modified = (try? FileManager.default.attributesOfItem(atPath: exe)[.modificationDate] as? Date)
            .map { stamp.string(from: $0) } ?? "?"
        let parent = SingleInstance.executableName(getppid()) ?? "?"
        log("app", "launch \(exe) modified \(modified), macOS \(ProcessInfo.processInfo.operatingSystemVersionString), "
            + "parent \(parent) (\(getppid()))")

        let ws = NSWorkspace.shared.notificationCenter
        let workspace: [(Notification.Name, String)] = [
            (NSWorkspace.willSleepNotification, "sleep"),
            (NSWorkspace.didWakeNotification, "wake"),
            (NSWorkspace.screensDidSleepNotification, "displays asleep"),
            (NSWorkspace.screensDidWakeNotification, "displays awake"),
            (NSWorkspace.sessionDidResignActiveNotification, "session inactive"),
            (NSWorkspace.sessionDidBecomeActiveNotification, "session active"),
            (NSWorkspace.activeSpaceDidChangeNotification, "space changed"),
        ]
        for (name, text) in workspace {
            observers.append(ws.addObserver(forName: name, object: nil, queue: nil) { _ in log("system", text) })
        }
        let dnc = DistributedNotificationCenter.default()
        for (name, text) in [("com.apple.screenIsLocked", "locked"), ("com.apple.screenIsUnlocked", "unlocked")] {
            observers.append(dnc.addObserver(forName: Notification.Name(name), object: nil, queue: nil) { _ in
                log("system", text)
            })
        }
        observers.append(NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main
        ) { _ in log("system", "displays changed: \(screens())") })
        log("system", "displays: \(screens())")

        HangWatch.shared.start()
    }

    /// `Built-in 1512x982 @0,0 primary, DELL 1920x1080 @0,982`; the primary one holds the menu bar.
    static func screens() -> String {
        NSScreen.screens.enumerated().map { i, s in
            let f = s.frame
            return "\(s.localizedName) \(Int(f.width))x\(Int(f.height)) @\(Int(f.minX)),\(Int(f.minY))"
                + (i == 0 ? " primary" : "")
        }.joined(separator: ", ")
    }
}
