import AppKit

/// Self-test for the relaunch that ends a self-update - `DIPLOMAT_RELAUNCH_TEST=1`.
///
/// `open -n` exits 0 once LaunchServices has taken the request, so a bundle whose
/// executable dies at once is "opened" all the same; judged on that status, the Update
/// button would show "restarting…" and the 06:00 job log "relaunched" over an app that
/// never came up. The verdict is the launched instance instead, so two bundles are laid
/// out in a scratch directory - one whose executable exits, one that stays up - and the
/// relaunch is asked about each, and about the staying one again with its instance up
/// and an executable that ends: the old instance must not pass for the new one, and the
/// failure must name it as what runs, where the first failure names nothing. Bundles
/// are matched by path, so the live app is neither counted nor touched. The staying
/// one writes down the environment it got:
/// `open` passes its own on, so it must carry no headless marker - the
/// `DIPLOMAT_SELF_UPDATE=1` the 06:00 job runs under, set on this process for the
/// relaunch's duration, and this test's own - while everything else reaches it.
///
/// The 06:00 job outlives that verdict only if the instance it launched spares it, so
/// the singleton's victims are checked too: they must leave out a headless instance (an
/// idle copy of this binary under `DIPLOMAT_RELAUNCH_TEST=hold`) and keep one carrying
/// no headless marker (the staying bundle, named like the app).
///
///     DIPLOMAT_RELAUNCH_TEST=1 swift run Diplomat
///
/// Ends every process it started and removes the scratch directory; terminates nothing
/// else.
enum RelaunchTest {
    static func run() -> Bool {
        var failures: [String] = []
        func check(_ name: String, _ cond: Bool, _ detail: String = "") {
            if cond { print("  ok    \(name)") }
            else { print("  FAIL  \(name) \(detail)"); failures.append(name) }
        }

        let fm = FileManager.default
        let scratch = fm.temporaryDirectory.appendingPathComponent("diplomat-relaunch-\(getpid())")
        defer { try? fm.removeItem(at: scratch) }

        /// A menu-bar-only bundle at `<scratch>/<name>.app` whose executable is `script`.
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
            <key>CFBundleIdentifier</key><string>com.ignacy.diplomat.relaunchtest.\(name)</string>
            <key>CFBundleExecutable</key><string>\(executable)</string>
            <key>CFBundlePackageType</key><string>APPL</string>
            <key>LSUIElement</key><true/>
            </dict></plist>
            """
            fm.createFile(atPath: app.appendingPathComponent("Contents/Info.plist").path,
                          contents: Data(plist.utf8))
            return app
        }
        func relaunch(_ app: URL) -> String? {
            do { try SelfUpdate.relaunch(app) } catch {
                return (error as? LocalizedError)?.errorDescription ?? "\(error)"
            }
            return nil
        }

        print("relaunch: the instance open started is the verdict")
        let exits = bundle("Exits", executable: "Exits", script: "#!/bin/sh\nexit 7\n")
        let refused = relaunch(exits)
        check("a bundle whose executable exits at launch is a failed relaunch", refused != nil)
        check("the failure says nothing is running",
              refused?.contains("no instance of Exits.app is running") == true,
              "got \(refused ?? "nil")")

        let dump = scratch.appendingPathComponent("stays.env").path
        let stays = bundle("Stays", executable: SingleInstance.execName,
                           script: "#!/bin/sh\nenv > '\(dump).tmp' && mv '\(dump).tmp' '\(dump)'\nexec sleep 60\n")
        defer { for app in SelfUpdate.instances(of: stays) { kill(app.processIdentifier, SIGKILL) } }
        setenv("DIPLOMAT_SELF_UPDATE", "1", 1)
        setenv("RELAUNCH_TEST_CANARY", "\(getpid())", 1)
        let accepted = relaunch(stays)
        unsetenv("DIPLOMAT_SELF_UPDATE")
        unsetenv("RELAUNCH_TEST_CANARY")
        check("a bundle that stays up is a relaunch", accepted == nil, accepted ?? "")
        let staying = SelfUpdate.instances(of: stays).map(\.processIdentifier)
        check("one instance of it is up", staying.count == 1, "pids \(staying)")

        print("relaunch: the environment the instance gets")
        let dumpBy = Date().addingTimeInterval(5)
        while Date() < dumpBy, !fm.fileExists(atPath: dump) { usleep(50_000) }
        var handed: [String: String] = [:]
        for line in ((try? String(contentsOfFile: dump, encoding: .utf8)) ?? "").split(separator: "\n") {
            guard let eq = line.firstIndex(of: "=") else { continue }
            handed[String(line[..<eq])] = String(line[line.index(after: eq)...])
        }
        check("the rest of this process's environment reaches it",
              handed["RELAUNCH_TEST_CANARY"] == "\(getpid())", "got \(handed["RELAUNCH_TEST_CANARY"] ?? "nil")")
        check("no headless marker does: not the updater's, not this test's",
              !handed.isEmpty && !Headless.isActive(in: handed)
                  && handed["DIPLOMAT_SELF_UPDATE"] == nil && handed["DIPLOMAT_RELAUNCH_TEST"] == nil,
              "got \(handed.filter { $0.key.hasPrefix("DIPLOMAT_") })")

        print("singleton: whom a fresh instance would terminate")
        let child = Process()
        child.executableURL = Bundle.main.executableURL
        var env = ProcessInfo.processInfo.environment
        env["DIPLOMAT_RELAUNCH_TEST"] = "hold"
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
        let read = SingleInstance.environment(of: idle)["DIPLOMAT_RELAUNCH_TEST"]
        check("its environment reads back", read == "hold", "got \(read ?? "nil")")
        check("this process's own environment reads back",
              SingleInstance.environment(of: getpid())["DIPLOMAT_RELAUNCH_TEST"] == "1")
        let victims = SingleInstance.otherInstances().map(\.processIdentifier)
        check("a headless instance is spared", !victims.contains(idle), "victims \(victims)")
        check("an instance carrying no headless marker is a victim",
              !staying.isEmpty && staying.allSatisfy { victims.contains($0) },
              "victims \(victims), staying \(staying)")

        print("relaunch: an instance up before the launch is not the new one")
        let exec = stays.appendingPathComponent("Contents/MacOS/\(SingleInstance.execName)")
        fm.createFile(atPath: exec.path, contents: Data("#!/bin/sh\nsleep 0.5\nexit 0\n".utf8),
                      attributes: [.posixPermissions: 0o755])
        let ended = relaunch(stays)
        check("a new instance that ends is a failed relaunch, the old one notwithstanding",
              ended != nil)
        check("the failure says the old build is what runs",
              ended?.contains("the running app is still the old build") == true,
              "got \(ended ?? "nil")")
        check("the old instance is what is up",
              SelfUpdate.instances(of: stays).map(\.processIdentifier) == staying,
              "up \(SelfUpdate.instances(of: stays).map(\.processIdentifier)), staying \(staying)")

        if failures.isEmpty { print("relaunch: all passed") }
        else { print("relaunch: FAILED \(failures.count): \(failures.joined(separator: "; "))") }
        return failures.isEmpty
    }
}
