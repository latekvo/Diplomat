import Foundation

/// Self-update for the macOS app: fast-forward the checkout, rebuild the `.app`
/// bundle, and relaunch it. The Swift port of the Linux front-end's `selfupdate`
/// module, adapted for the packaged front-end — where Linux relaunches the checkout's
/// launcher, macOS rebuilds `Diplomat.app` (via `install/build-app.sh`) and `open`s
/// it; the newest-wins singleton (`SingleInstance`) then terminates this instance, so
/// a successful update ends with this process about to be replaced.
///
/// Everything here is synchronous and shell-based; the Store wraps it in detached tasks
/// (`refreshUpdateStatus` / `updateApp`) the same way it wraps the allocator installer.
enum SelfUpdate {
    struct UpdateError: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    /// Where the checkout stands vs its upstream — the payload behind the Settings
    /// "UPDATE" section. Mirrors `selfupdate.check`'s dict.
    struct CheckResult: Equatable {
        var commit: String?
        var branch: String?
        var upstream: String?
        var behind: Int?
        var ahead: Int?
        var error: String?
    }

    private static var root: URL { RepoPaths.root }

    // MARK: - git plumbing

    /// Run `git -C <root> …`, returning trimmed stdout; throws `UpdateError` (last stderr
    /// line) on a non-zero exit. Mirrors `selfupdate._git`.
    @discardableResult
    private static func git(_ args: [String], timeout: TimeInterval = 120) throws -> String {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/git")
        p.arguments = ["-C", root.path] + args
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        do { try p.run() } catch {
            throw UpdateError(message: "git \(args.first ?? ""): \(error.localizedDescription)")
        }
        let watchdog = DispatchWorkItem { if p.isRunning { p.terminate() } }
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: watchdog)
        let outData = out.fileHandleForReading.readDataToEndOfFile()
        let errData = err.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        watchdog.cancel()
        let stdout = String(data: outData, encoding: .utf8) ?? ""
        if p.terminationStatus != 0 {
            let stderr = String(data: errData, encoding: .utf8) ?? ""
            let lines = (stderr.isEmpty ? stdout : stderr)
                .split(whereSeparator: \.isNewline)
            let detail = lines.last.map(String.init) ?? "exit \(p.terminationStatus)"
            throw UpdateError(message: "git \(args.first ?? ""): \(detail)")
        }
        return stdout.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// The ref we update to: the branch's upstream, else `origin/main`.
    private static func upstream() -> String {
        (try? git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])) ?? "origin/main"
    }

    // MARK: - public surface

    /// Fetch origin and report where the checkout stands vs its upstream. Never throws —
    /// an unreachable remote still yields the local commit plus an `error` string.
    static func check() -> CheckResult {
        var out = CheckResult()
        guard RepoPaths.checkoutPresent else {
            out.error = "no checkout at \(root.path) — set DIPLOMAT_SELF_REPO"
            return out
        }
        do {
            out.commit = try git(["rev-parse", "--short", "HEAD"])
            out.branch = try git(["rev-parse", "--abbrev-ref", "HEAD"])
            try git(["fetch", "--quiet", "origin"])
            let up = upstream()
            out.upstream = up
            // left = commits only on HEAD (ahead), right = only on upstream (behind).
            let counts = try git(["rev-list", "--left-right", "--count", "HEAD...\(up)"])
                .split(whereSeparator: \.isWhitespace)
            if counts.count == 2 {
                out.ahead = Int(counts[0])
                out.behind = Int(counts[1])
            }
        } catch let e as UpdateError {
            out.error = e.message
        } catch {
            out.error = "\(error)"
        }
        return out
    }

    /// Whether a committer name+email is configured (a merge commit needs one).
    private static func hasGitIdentity() -> Bool {
        for key in ["user.name", "user.email"] {
            let v = (try? git(["config", "--get", key])) ?? ""
            if v.isEmpty { return false }
        }
        return true
    }

    /// Integrate the checkout's upstream; returns the resulting short SHA. Fast-forwards
    /// when strictly behind, and creates a merge commit when the checkout has diverged
    /// (local commits origin doesn't have) — so an update still lands when you're *ahead*,
    /// which `--ff-only` refused to do. A real conflict is never resolved unattended: the
    /// merge is aborted, the checkout left as it was, and a readable error says it needs a
    /// manual merge. Uncommitted local changes still block outright. Mirrors `selfupdate.pull`.
    static func pull() throws -> String {
        let dirty = try git(["status", "--porcelain", "--untracked-files=no"])
        if !dirty.isEmpty {
            throw UpdateError(message: "checkout has local changes — commit or stash them first")
        }
        try git(["fetch", "--quiet", "origin"])
        let up = upstream()
        // Give the auto-merge a committer identity if the environment has none (a stripped
        // launchd service env might), but never override the user's own when it's set.
        let ident = hasGitIdentity()
            ? []
            : ["-c", "user.name=Diplomat updater", "-c", "user.email=diplomat@localhost"]
        do {
            try git(ident + ["merge", "--no-edit", up])
        } catch let e as UpdateError {
            // Leave nothing half-merged behind, whatever went wrong.
            _ = try? git(["merge", "--abort"])
            if e.message.lowercased().contains("conflict") {
                throw UpdateError(message: "update conflicts with your local commits — merge origin "
                    + "by hand in the checkout, then update again")
            }
            throw e
        }
        return try git(["rev-parse", "--short", "HEAD"])
    }

    /// Rebuild `Diplomat.app` from the (freshly pulled) checkout via this package's
    /// `install/build-app.sh`. Run through a login shell so the Swift toolchain is on
    /// PATH even when the app was started from launchd with a minimal environment.
    static func rebuild() throws {
        let script = RepoPaths.installDir.appendingPathComponent("build-app.sh").path
        guard FileManager.default.fileExists(atPath: script) else {
            throw UpdateError(message: "build-app.sh not found at \(script)")
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = ["-lc", "exec \(shellQuote(script))"]
        p.currentDirectoryURL = root
        let err = Pipe()
        p.standardOutput = FileHandle.nullDevice
        p.standardError = err
        do { try p.run() } catch {
            throw UpdateError(message: "build-app.sh: \(error.localizedDescription)")
        }
        // A cold release build can take a while.
        let watchdog = DispatchWorkItem { if p.isRunning { p.terminate() } }
        DispatchQueue.global().asyncAfter(deadline: .now() + 1800, execute: watchdog)
        let errData = err.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        watchdog.cancel()
        if p.terminationStatus != 0 {
            let lines = (String(data: errData, encoding: .utf8) ?? "").split(whereSeparator: \.isNewline)
            throw UpdateError(message: "build-app.sh: \(lines.last.map(String.init) ?? "exit \(p.terminationStatus)")")
        }
    }

    /// Launch the freshly-built bundle detached; its newest-wins singleton terminates
    /// any GUI instance still running, so a caller that is one only reports
    /// "restarting…" and waits to be replaced. Mirrors `selfupdate.relaunch`.
    ///
    /// `open` passes its environment to the instance, so it gets this one with every
    /// headless marker removed: launched by the 06:00 updater or the watchdog with its
    /// marker intact, the instance would be another headless job, not the GUI.
    ///
    /// The default `app` is beside the macOS package, where `build-app.sh` writes it.
    static func relaunch(_ app: URL = RepoPaths.macosPackage.appendingPathComponent("Diplomat.app")) throws {
        let name = app.lastPathComponent
        guard FileManager.default.fileExists(atPath: app.path) else {
            throw UpdateError(message: "\(name) not found at \(app.path)")
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        p.arguments = ["-n", app.path]
        p.environment = Headless.stripped(ProcessInfo.processInfo.environment)
        do { try p.run() } catch {
            throw UpdateError(message: "could not relaunch the app: \(error.localizedDescription)")
        }
        p.waitUntilExit()
        if p.terminationStatus != 0 {
            throw UpdateError(message: "open \(name) exited \(p.terminationStatus)")
        }
    }

    private static func shellQuote(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    // MARK: - unattended (launchd) paths

    /// What the unattended paths do to the machine. The real steps by default;
    /// `WatchdogTest` swaps in fixtures.
    struct Host {
        var check: () -> CheckResult = SelfUpdate.check
        var pull: () throws -> String = SelfUpdate.pull
        var rebuild: () throws -> Void = SelfUpdate.rebuild
        var appRunning: () -> Bool = SingleInstance.isRunning
        var operatorQuit: () -> Bool = { OperatorQuit.isMarked }
        var launch: () throws -> Void = { try SelfUpdate.relaunch() }
        var log: (String) -> Void = SelfUpdate.schedLog
    }

    /// Headless daily update for the launchd 6AM job. Never throws; returns an exit code.
    ///
    /// Fetches, and if behind, merges upstream and rebuilds, then relaunches a running
    /// app onto the new build. On every other path an app that is not running is
    /// launched unless the operator quit it (`revive`). A conflict or unreachable origin
    /// is logged and left for a human rather than retried destructively. Mirrors
    /// `selfupdate.run_scheduled`.
    static func runScheduled(_ host: Host = Host()) -> Int32 {
        let st = host.check()
        if let e = st.error {
            host.log("skip: cannot reach origin (\(e))")
            return revive(host, quietly: false)
        }
        guard let behind = st.behind, behind > 0 else {
            let extra = (st.ahead ?? 0) > 0 ? " (\(st.ahead!) local ahead)" : ""
            host.log("up to date at \(st.commit ?? "?")\(extra)")
            return revive(host, quietly: false)
        }
        host.log("\(behind) behind at \(st.commit ?? "?") — merging \(st.upstream ?? "origin/main")")
        let commit: String
        do {
            commit = try host.pull()
        } catch {
            host.log("skip: \((error as? LocalizedError)?.errorDescription ?? "\(error)")")
            return revive(host, quietly: false)
        }
        host.log("merged to \(commit) — rebuilding the app")
        do {
            try host.rebuild()
        } catch {
            host.log("build failed: \((error as? LocalizedError)?.errorDescription ?? "\(error)")")
            // build-app.sh replaces the bundle only after a clean compile, so the last
            // good one is still there to bring back.
            _ = revive(host, quietly: false)
            return 1
        }
        guard host.appRunning() else {
            host.log("updated to \(commit) in place")
            return revive(host, quietly: false)
        }
        do {
            try host.launch()
            host.log("relaunched running app onto \(commit)")
        } catch {
            host.log("relaunch failed: \((error as? LocalizedError)?.errorDescription ?? "\(error)")")
            return 1
        }
        return 0
    }

    /// The launchd liveness check, every few minutes. Logs only a launch, so a healthy
    /// or deliberately closed app adds nothing to the log it shares with the updater.
    static func runWatchdog(_ host: Host = Host()) -> Int32 {
        var tagged = host
        tagged.log = { host.log("watchdog: \($0)") }
        return revive(tagged, quietly: true)
    }

    /// Launch the app if no GUI instance is running and the operator did not quit it.
    ///
    /// It terminates nothing and acts only on zero instances, so it cannot contend with
    /// the singleton or a relaunch: both start the new instance before the old one goes,
    /// so there is always one to see. A launch racing another start ends, through the
    /// singleton, with one instance, and nothing restarts the one it ended.
    static func revive(_ host: Host, quietly: Bool) -> Int32 {
        guard !host.appRunning() else { return 0 }
        guard !host.operatorQuit() else {
            if !quietly { host.log("app not running: quit by the operator, left closed") }
            return 0
        }
        do {
            try host.launch()
            host.log("app not running: launched it")
            return 0
        } catch {
            host.log("app not running: launch failed: \((error as? LocalizedError)?.errorDescription ?? "\(error)")")
            return 1
        }
    }

    /// Append a timestamped line to the auto-update log (best-effort).
    static func schedLog(_ message: String) {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs")
        let url = dir.appendingPathComponent("diplomat-autoupdate.log")
        let line = "\(ISO8601DateFormatter().string(from: Date())) \(message)\n"
        guard let data = line.data(using: .utf8) else { return }
        if let fh = try? FileHandle(forWritingTo: url) {
            defer { try? fh.close() }
            _ = try? fh.seekToEnd()
            try? fh.write(contentsOf: data)
        } else {
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            try? data.write(to: url)
        }
    }
}
