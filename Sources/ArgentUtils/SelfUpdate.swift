import Foundation

/// Self-update for the macOS app: fast-forward the checkout, rebuild the `.app`
/// bundle, and relaunch it. The Swift port of the Linux front-end's `selfupdate`
/// module, adapted for the packaged front-end — where Linux relaunches the checkout's
/// launcher, macOS rebuilds `ArgentUtils.app` (via `scripts/build-app.sh`) and `open`s
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
            out.error = "no checkout at \(root.path) — set ARGENT_UTILS_SELF_REPO"
            return out
        }
        do {
            out.commit = try git(["rev-parse", "--short", "HEAD"])
            out.branch = try git(["rev-parse", "--abbrev-ref", "HEAD"])
            try git(["fetch", "--quiet", "origin"])
            let up = upstream()
            out.upstream = up
            out.behind = Int(try git(["rev-list", "--count", "HEAD..\(up)"]))
        } catch let e as UpdateError {
            out.error = e.message
        } catch {
            out.error = "\(error)"
        }
        return out
    }

    /// Fast-forward the checkout to its upstream; returns the new short SHA. Refuses on
    /// local changes or a diverged branch (`--ff-only`) — an update must never discard
    /// work in the checkout it runs from. Mirrors `selfupdate.pull`.
    static func pull() throws -> String {
        let dirty = try git(["status", "--porcelain", "--untracked-files=no"])
        if !dirty.isEmpty {
            throw UpdateError(message: "checkout has local changes — commit or stash them first")
        }
        try git(["fetch", "--quiet", "origin"])
        try git(["merge", "--ff-only", upstream()])
        return try git(["rev-parse", "--short", "HEAD"])
    }

    /// Rebuild `ArgentUtils.app` from the (freshly pulled) checkout via
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
        let app = root.appendingPathComponent("ArgentUtils.app")
        guard FileManager.default.fileExists(atPath: app.path) else {
            throw UpdateError(message: "ArgentUtils.app not found after rebuild")
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        p.arguments = ["-n", app.path]
        do { try p.run() } catch {
            throw UpdateError(message: "could not relaunch the app: \(error.localizedDescription)")
        }
        p.waitUntilExit()
        if p.terminationStatus != 0 {
            throw UpdateError(message: "open ArgentUtils.app exited \(p.terminationStatus)")
        }
    }

    private static func shellQuote(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }
}
