import Foundation

/// Self-update for the macOS app: fast-forward the checkout, rebuild the `.app`
/// bundle, and relaunch it. The Swift port of the Linux front-end's `selfupdate`
/// module, adapted for the packaged front-end — where Linux relaunches the checkout's
/// launcher, macOS rebuilds `CoMaintainer.app` (via `scripts/build-app.sh`) and `open`s
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
            out.error = "no checkout at \(root.path) — set CO_MAINTAINER_SELF_REPO"
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
            : ["-c", "user.name=Co-Maintainer updater", "-c", "user.email=co-maintainer@localhost"]
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

    /// Rebuild `CoMaintainer.app` from the (freshly pulled) checkout via
    /// `scripts/build-app.sh`. Run through a login shell so the Swift toolchain is on
    /// PATH even when the app was started from launchd with a minimal environment.
    static func rebuild() throws {
        let script = root.appendingPathComponent("scripts/build-app.sh").path
        guard FileManager.default.fileExists(atPath: script) else {
            throw UpdateError(message: "scripts/build-app.sh not found in \(root.path)")
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
    /// this instance once it's up, so the caller only reports "restarting…" and waits to
    /// be replaced. Mirrors `selfupdate.relaunch`.
    static func relaunch() throws {
        let app = root.appendingPathComponent("CoMaintainer.app")
        guard FileManager.default.fileExists(atPath: app.path) else {
            throw UpdateError(message: "CoMaintainer.app not found after rebuild")
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        p.arguments = ["-n", app.path]
        do { try p.run() } catch {
            throw UpdateError(message: "could not relaunch the app: \(error.localizedDescription)")
        }
        p.waitUntilExit()
        if p.terminationStatus != 0 {
            throw UpdateError(message: "open CoMaintainer.app exited \(p.terminationStatus)")
        }
    }

    private static func shellQuote(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    // MARK: - unattended (6AM launchd) path

    /// Headless daily update for the launchd 6AM job. Never throws; returns an exit code.
    ///
    /// Fetches, and if behind, merges upstream and rebuilds — then relaunches the app only
    /// if one is actually running (so it never pops a menu-bar app onto a login session that
    /// isn't showing one). Quiet no-op when already current; a conflict or unreachable origin
    /// is logged and left for a human rather than retried destructively. Mirrors
    /// `selfupdate.run_scheduled`.
    static func runScheduled() -> Int32 {
        let st = check()
        if let e = st.error { schedLog("skip: cannot reach origin (\(e))"); return 0 }
        guard let behind = st.behind, behind > 0 else {
            let extra = (st.ahead ?? 0) > 0 ? " (\(st.ahead!) local ahead)" : ""
            schedLog("up to date at \(st.commit ?? "?")\(extra)")
            return 0
        }
        schedLog("\(behind) behind at \(st.commit ?? "?") — merging \(st.upstream ?? "origin/main")")
        let commit: String
        do {
            commit = try pull()
        } catch {
            schedLog("skip: \((error as? LocalizedError)?.errorDescription ?? "\(error)")")
            return 0
        }
        schedLog("merged to \(commit) — rebuilding the app")
        do {
            try rebuild()
        } catch {
            schedLog("build failed: \((error as? LocalizedError)?.errorDescription ?? "\(error)")")
            return 1
        }
        if SingleInstance.isRunning() {
            do {
                try relaunch()
                schedLog("relaunched running app onto \(commit)")
            } catch {
                schedLog("relaunch failed: \((error as? LocalizedError)?.errorDescription ?? "\(error)")")
                return 1
            }
        } else {
            schedLog("updated to \(commit) in place (app not running)")
        }
        return 0
    }

    /// Append a timestamped line to the auto-update log (best-effort).
    private static func schedLog(_ message: String) {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs")
        let url = dir.appendingPathComponent("co-maintainer-autoupdate.log")
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
