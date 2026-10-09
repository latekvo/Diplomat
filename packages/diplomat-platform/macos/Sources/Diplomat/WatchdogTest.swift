import Foundation

/// Self-test for who brings a dead app back - `DIPLOMAT_WATCHDOG_TEST=1`.
///
/// The 06:00 updater and the watchdog decide over fixture job steps, so no git, build,
/// launch or log line reaches the machine: each must launch an app that is not running
/// unless the operator quit it, and launch nothing while one runs. The quit mark
/// round-trips through a scratch file. The launch itself - what it hands the GUI and
/// whom the singleton counts as the app - is `RelaunchTest`'s.
///
///     DIPLOMAT_WATCHDOG_TEST=1 swift run Diplomat
///
/// Starts no process and removes the scratch directory.
enum WatchdogTest {
    static func run() -> Bool {
        var failures: [String] = []
        func check(_ name: String, _ cond: Bool, _ detail: String = "") {
            if cond { print("  ok    \(name)") }
            else { print("  FAIL  \(name) \(detail)"); failures.append(name) }
        }

        let fm = FileManager.default
        let scratch = fm.temporaryDirectory.appendingPathComponent("diplomat-watchdog-\(getpid())")
        try? fm.createDirectory(at: scratch, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: scratch) }

        /// A fixture machine: what the job finds, and what it did.
        final class Machine {
            var behind = 0
            var offline = false
            var pullFails = false
            var buildFails = false
            var running = false
            var quit = false
            var launchFails = false
            var launches = 0
            var lines: [String] = []

            var host: SelfUpdate.Host {
                var h = SelfUpdate.Host()
                h.check = {
                    var r = SelfUpdate.CheckResult()
                    r.commit = "abc1234"
                    if self.offline { r.error = "could not resolve host" } else { r.behind = self.behind; r.ahead = 0 }
                    return r
                }
                h.pull = {
                    if self.pullFails { throw SelfUpdate.UpdateError(message: "checkout has local changes") }
                    return "def5678"
                }
                h.rebuild = { if self.buildFails { throw SelfUpdate.UpdateError(message: "build-app.sh: exit 1") } }
                h.appRunning = { self.running }
                h.operatorQuit = { self.quit }
                h.launch = {
                    if self.launchFails { throw SelfUpdate.UpdateError(message: "Diplomat.app not found") }
                    self.launches += 1
                }
                h.log = { self.lines.append($0) }
                return h
            }
        }
        func machine(_ set: (Machine) -> Void) -> Machine { let m = Machine(); set(m); return m }

        print("06:00 updater: a dead app comes back on every path")
        for (name, m) in [
            ("up to date", machine { _ in }),
            ("updated in place", machine { $0.behind = 2 }),
            ("origin unreachable", machine { $0.offline = true }),
            ("merge refused", machine { $0.behind = 1; $0.pullFails = true }),
        ] {
            let code = SelfUpdate.runScheduled(m.host)
            check("\(name): launched once", m.launches == 1 && code == 0, "launches \(m.launches), exit \(code)")
            check("\(name): the log says so", m.lines.last == "app not running: launched it", "\(m.lines)")
        }
        do {
            let m = machine { $0.behind = 1; $0.buildFails = true }
            let code = SelfUpdate.runScheduled(m.host)
            check("build failed: the last bundle is launched and the run still fails",
                  m.launches == 1 && code == 1, "launches \(m.launches), exit \(code)")
        }
        do {
            let m = machine { $0.launchFails = true }
            let code = SelfUpdate.runScheduled(m.host)
            check("a launch that fails is an exit 1 and a log line",
                  code == 1 && m.lines.last?.hasPrefix("app not running: launch failed: ") == true,
                  "exit \(code), \(m.lines)")
        }

        print("06:00 updater: what it leaves alone")
        for (name, m) in [
            ("up to date", machine { $0.quit = true }),
            ("updated in place", machine { $0.behind = 1; $0.quit = true }),
        ] {
            let code = SelfUpdate.runScheduled(m.host)
            check("\(name), quit by the operator: nothing launched", m.launches == 0 && code == 0,
                  "launches \(m.launches)")
            check("\(name), quit by the operator: the log says why",
                  m.lines.last == "app not running: quit by the operator, left closed", "\(m.lines)")
        }
        do {
            let m = machine { $0.running = true }
            _ = SelfUpdate.runScheduled(m.host)
            check("up to date, running: nothing launched", m.launches == 0, "launches \(m.launches)")
        }
        do {
            let m = machine { $0.behind = 1; $0.running = true }
            _ = SelfUpdate.runScheduled(m.host)
            check("updated, running: relaunched once onto the build",
                  m.launches == 1 && m.lines.last == "relaunched running app onto def5678", "\(m.lines)")
        }

        print("watchdog")
        do {
            let m = machine { _ in }
            let code = SelfUpdate.runWatchdog(m.host)
            check("a dead app is launched and logged", m.launches == 1 && code == 0
                  && m.lines == ["watchdog: app not running: launched it"], "\(m.lines)")
        }
        for (name, m) in [("running", machine { $0.running = true }), ("quit by the operator", machine { $0.quit = true })] {
            _ = SelfUpdate.runWatchdog(m.host)
            check("\(name): nothing launched, nothing logged", m.launches == 0 && m.lines.isEmpty,
                  "launches \(m.launches), \(m.lines)")
        }
        do {
            let m = machine { $0.launchFails = true }
            check("a failed launch is an exit 1", SelfUpdate.runWatchdog(m.host) == 1)
        }

        print("the quit mark")
        let realMark = OperatorQuit.url
        OperatorQuit.url = scratch.appendingPathComponent("nested/operator-quit")
        defer { OperatorQuit.url = realMark }
        check("absent at first", !OperatorQuit.isMarked)
        OperatorQuit.mark()
        check("a mark is read back, its directory made", OperatorQuit.isMarked)
        OperatorQuit.clear()
        check("a clear removes it", !OperatorQuit.isMarked)
        OperatorQuit.clear()
        check("a second clear is harmless", !OperatorQuit.isMarked)
        check("the Quit button is deliberate",
              OperatorQuit.isDeliberate(senderIsDiplomat: nil, endsSession: false))
        check("a quit sent by another app is deliberate",
              OperatorQuit.isDeliberate(senderIsDiplomat: false, endsSession: false))
        check("a quit sent by a newer Diplomat is a hand-over",
              !OperatorQuit.isDeliberate(senderIsDiplomat: true, endsSession: false))
        check("a logout's quit is not, cancelled or not",
              !OperatorQuit.isDeliberate(senderIsDiplomat: false, endsSession: true))

        print("who sent a quit")
        check("a quit sent from this binary reads as Diplomat's",
              SingleInstance.executableName(getpid()) == SingleInstance.execName)
        check("a quit sent from anything else does not",
              SingleInstance.executableName(getppid()).map { $0 != SingleInstance.execName } == true)

        if failures.isEmpty { print("watchdog: all passed") }
        else { print("watchdog: FAILED \(failures.count): \(failures.joined(separator: "; "))") }
        return failures.isEmpty
    }
}
