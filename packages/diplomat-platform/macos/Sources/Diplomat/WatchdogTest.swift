import AppKit

/// Self-test for who brings a dead app back - `DIPLOMAT_WATCHDOG_TEST=1`.
///
/// The 06:00 updater and the watchdog decide over fixture job steps, so no git, build,
/// launch or log line reaches the machine: each must launch an app that is not running
/// unless the operator quit it, and launch nothing while one runs. The quit mark
/// round-trips through a scratch file. What is real: the environment `open` hands a
/// launched bundle (a throwaway one that writes it down), and whom the singleton counts
/// as the app - an idle copy of this binary under `DIPLOMAT_WATCHDOG_TEST=hold` is
/// headless and must not count, while a bundle whose executable is named like the app
/// must. Bundles are matched by path and pid, so the live app is neither counted nor
/// touched.
///
///     DIPLOMAT_WATCHDOG_TEST=1 swift run Diplomat
///
/// Ends every process it started and removes the scratch directory.
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

        print("the environment a launched GUI gets")
        func bundle(_ name: String, executable: String, script: String) -> URL {
            let app = scratch.appendingPathComponent("\(name).app")
            let macos = app.appendingPathComponent("Contents/MacOS")
            try? fm.createDirectory(at: macos, withIntermediateDirectories: true)
            fm.createFile(atPath: macos.appendingPathComponent(executable).path,
                          contents: Data(script.utf8), attributes: [.posixPermissions: 0o755])
            let plist = """
            <?xml version="1.0" encoding="UTF-8"?>
            <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
            <plist version="1.0"><dict>
            <key>CFBundleName</key><string>\(name)</string>
            <key>CFBundleIdentifier</key><string>com.ignacy.diplomat.watchdogtest.\(name)</string>
            <key>CFBundleExecutable</key><string>\(executable)</string>
            <key>CFBundlePackageType</key><string>APPL</string>
            <key>LSUIElement</key><true/>
            </dict></plist>
            """
            fm.createFile(atPath: app.appendingPathComponent("Contents/Info.plist").path,
                          contents: Data(plist.utf8))
            return app
        }
        let dump = scratch.appendingPathComponent("stays.env").path
        let stays = bundle("Stays", executable: SingleInstance.execName,
                           script: "#!/bin/sh\nenv > '\(dump).tmp' && mv '\(dump).tmp' '\(dump)'\nexec sleep 60\n")
        func staying() -> [NSRunningApplication] {
            let path = stays.resolvingSymlinksInPath().path
            return NSRunningApplication.runningApplications(
                withBundleIdentifier: "com.ignacy.diplomat.watchdogtest.Stays"
            ).filter { $0.bundleURL?.resolvingSymlinksInPath().path == path }
        }
        defer { for app in staying() { kill(app.processIdentifier, SIGKILL) } }
        setenv("DIPLOMAT_WATCHDOG", "1", 1)
        setenv("WATCHDOG_TEST_CANARY", "\(getpid())", 1)
        var launchError: String?
        do { try SelfUpdate.relaunch(stays) } catch {
            launchError = (error as? LocalizedError)?.errorDescription ?? "\(error)"
        }
        unsetenv("DIPLOMAT_WATCHDOG")
        unsetenv("WATCHDOG_TEST_CANARY")
        check("the bundle opens", launchError == nil, launchError ?? "")
        let dumpBy = Date().addingTimeInterval(10)
        while Date() < dumpBy, !fm.fileExists(atPath: dump) { usleep(50_000) }
        var handed: [String: String] = [:]
        for line in ((try? String(contentsOfFile: dump, encoding: .utf8)) ?? "").split(separator: "\n") {
            guard let eq = line.firstIndex(of: "=") else { continue }
            handed[String(line[..<eq])] = String(line[line.index(after: eq)...])
        }
        check("the rest of this process's environment reaches it",
              handed["WATCHDOG_TEST_CANARY"] == "\(getpid())", "got \(handed["WATCHDOG_TEST_CANARY"] ?? "nil")")
        check("no headless marker does: not the watchdog's, not this test's",
              !handed.isEmpty && !Headless.isActive(in: handed)
                  && handed["DIPLOMAT_WATCHDOG"] == nil && handed["DIPLOMAT_WATCHDOG_TEST"] == nil,
              "got \(handed.filter { $0.key.hasPrefix("DIPLOMAT_") })")
        check("a missing bundle is an error, not a launch",
              (try? SelfUpdate.relaunch(scratch.appendingPathComponent("Gone.app"))) == nil)

        print("whom the singleton counts as the app")
        let child = Process()
        child.executableURL = Bundle.main.executableURL
        var env = ProcessInfo.processInfo.environment
        env["DIPLOMAT_WATCHDOG_TEST"] = "hold"
        child.environment = env
        child.standardOutput = FileHandle.nullDevice
        child.standardError = FileHandle.nullDevice
        do { try child.run() } catch { check("an idle copy of this binary starts", false, "\(error)") }
        defer { if child.isRunning { child.terminate(); child.waitUntilExit() } }
        let idle = child.processIdentifier
        func listed(_ pid: pid_t) -> Bool {
            NSWorkspace.shared.runningApplications.contains { $0.processIdentifier == pid }
        }
        let listedBy = Date().addingTimeInterval(10)
        while Date() < listedBy, !listed(idle) { usleep(50_000) }
        check("the idle copy is a running application", listed(idle))
        check("its environment reads back",
              SingleInstance.environment(of: idle)["DIPLOMAT_WATCHDOG_TEST"] == "hold")
        let counted = SingleInstance.otherInstances().map(\.processIdentifier)
        check("a headless instance is not the app", !counted.contains(idle), "counted \(counted)")
        let up = staying().map(\.processIdentifier)
        check("an instance carrying no headless marker is the app",
              !up.isEmpty && up.allSatisfy { counted.contains($0) }, "counted \(counted), staying \(up)")
        check("a quit sent from this binary reads as Diplomat's",
              SingleInstance.executableName(idle) == SingleInstance.execName)
        check("a quit sent from anything else does not",
              SingleInstance.executableName(getppid()).map { $0 != SingleInstance.execName } == true)

        if failures.isEmpty { print("watchdog: all passed") }
        else { print("watchdog: FAILED \(failures.count): \(failures.joined(separator: "; "))") }
        return failures.isEmpty
    }
}
