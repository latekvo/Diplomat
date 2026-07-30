import Foundation

/// Bridge to the local device-allocator daemon + installer (the Node package under
/// `device-allocator/`). The applet is a *viewer* of the daemon's public state file
/// and a *driver* of its installer — it never allocates devices itself.
///
/// Two surfaces:
///   - `readState()` decodes the daemon's `~/.diplomat/device-allocator/state.json`
///     (the live pool + who holds what), polled by the Store.
///   - `check()/install()/uninstall()` shell the package's `install.js` so the
///     Settings screen can show install status and one-click (un)install the MCP
///     server + skill + always-on rule.

// MARK: - state model (mirrors the daemon's public snapshot)

struct DeviceOwner: Decodable, Equatable {
    let agentName: String?
    let ownerPid: Int?
}

struct DeviceAllocation: Decodable, Identifiable, Equatable {
    let key: String
    let platform: String
    let name: String?
    let version: String?
    let apiVersion: String?
    let handle: String?
    let status: String
    let owner: DeviceOwner?
    let allocatedAt: Double?
    let idleMs: Double?
    let brokenReason: String?
    let repairLog: String?
    let format: String?

    var id: String { key }
    var isAllocated: Bool { owner?.ownerPid != nil || status == "repairing" }
}

struct DeviceState: Decodable, Equatable {
    // Only `devices` is decoded: the daemon also writes `updatedAt`/`daemonPid`,
    // but those change every poll and would defeat the "publish only on change"
    // guard, so we deliberately ignore them (unknown keys are dropped on decode).
    let devices: [DeviceAllocation]

    var allocatedCount: Int { devices.filter { $0.isAllocated }.count }
    var freeCount: Int { devices.count - allocatedCount }
}

// MARK: - installer status

struct AllocatorInstall: Decodable, Equatable {
    var mcpRegistered = false
    var skillInstalled = false
    var ruleInstalled = false
    var claudeMdInjected = false
    var daemonRunning = false
    var installed = false
    /// The installed package's version, for display. `nil` until a check answers.
    var version: String?
    /// An install whose deployed copies no longer match this checkout (the installer
    /// compares them by content). Never true for a machine that isn't installed —
    /// so a deliberate uninstall is never mistaken for damage to repair.
    var outdated = false
    /// Which artifacts drifted (`skill`, `rule`, `claudeMd`, `mcp`) — shown beside
    /// the status so "out of date" says what, not just that.
    var drift: [String] = []

    init() {}

    /// Tolerant decode: any missing key (e.g. an older `--uninstall` output that
    /// omitted `installed`, or an error payload) defaults to false rather than
    /// failing the whole decode and discarding the result.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        mcpRegistered = (try? c.decode(Bool.self, forKey: .mcpRegistered)) ?? false
        skillInstalled = (try? c.decode(Bool.self, forKey: .skillInstalled)) ?? false
        ruleInstalled = (try? c.decode(Bool.self, forKey: .ruleInstalled)) ?? false
        claudeMdInjected = (try? c.decode(Bool.self, forKey: .claudeMdInjected)) ?? false
        daemonRunning = (try? c.decode(Bool.self, forKey: .daemonRunning)) ?? false
        installed = (try? c.decode(Bool.self, forKey: .installed)) ?? false
        version = try? c.decode(String.self, forKey: .version)
        outdated = (try? c.decode(Bool.self, forKey: .outdated)) ?? false
        drift = (try? c.decode([String].self, forKey: .drift)) ?? []
    }

    private enum CodingKeys: String, CodingKey {
        case mcpRegistered, skillInstalled, ruleInstalled, claudeMdInjected, daemonRunning
        case installed, version, outdated, drift
    }

    /// Unknown until the first check completes (so the UI can say "checking…").
    static let unknown = AllocatorInstall()
}

enum DeviceAllocator {
    /// Where the Node package lives: a sibling of this app inside the same checkout.
    /// Overridable for a layout that keeps them apart.
    ///
    /// Resolved through `RepoPaths.root` rather than hardcoded to `~/dev/diplomat`,
    /// which is the twin of the Linux bridge deriving it from its own file's path: an
    /// app run out of a worktree, a differently-named clone, or a checkout moved
    /// anywhere else must drive *its own* installer, not one belonging to whatever
    /// happens to sit at the conventional path.
    static var packageDir: String {
        if let env = ProcessInfo.processInfo.environment["DIPLOMAT_DEVICE_ALLOCATOR_DIR"], !env.isEmpty {
            return env
        }
        return RepoPaths.packages.appendingPathComponent("device-allocator").path
    }

    private static var home: URL { FileManager.default.homeDirectoryForCurrentUser }
    static var installJS: String { packageDir + "/src/install.js" }
    static var nodeModulesDir: String { packageDir + "/node_modules" }
    static var stateURL: URL {
        home.appendingPathComponent(".diplomat/device-allocator/state.json")
    }
    static var socketPath: String {
        home.appendingPathComponent(".diplomat/device-allocator/daemon.sock").path
    }

    /// POST a JSON body to one of the daemon's endpoints over its unix socket, and
    /// report whether the daemon answered 2xx. The transport is `curl` because
    /// `URLSession` cannot speak to a unix socket at all.
    ///
    /// `-f` is the load-bearing flag: without it curl exits 0 for any *completed*
    /// HTTP transaction, including a 4xx/5xx — so a refused request would read as
    /// success and the audit feed would assert an action that never happened (a
    /// device that left the pool between the poll snapshot and the click answers
    /// 404). With `-sf`, terminationStatus follows the HTTP status.
    ///
    /// Returns false rather than throwing when the daemon isn't running, the body
    /// won't encode, or curl can't be launched: every caller is a best-effort
    /// side-channel to a daemon that may simply not be installed.
    static func post(_ endpoint: String, body: [String: String], timeoutSecs: Int) -> Bool {
        guard FileManager.default.fileExists(atPath: socketPath),
              let data = try? JSONSerialization.data(withJSONObject: body),
              let json = String(data: data, encoding: .utf8) else { return false }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/curl")
        p.arguments = ["-sf", "--max-time", "\(timeoutSecs)", "--unix-socket", socketPath,
                       "-X", "POST", "http://localhost/\(endpoint)",
                       "-H", "content-type: application/json", "-d", json]
        p.standardOutput = Pipe()
        p.standardError = Pipe()
        do { try p.run() } catch { return false }
        p.waitUntilExit()
        return p.terminationStatus == 0
    }

    /// Ask the daemon to force-kill a device by key (free any allocation + shut the
    /// sim/emulator down). Backs the panel's per-device X. Best-effort; returns
    /// whether the request succeeded.
    @discardableResult
    static func killDevice(key: String) -> Bool {
        // Generous timeout: the daemon shuts a real simulator/emulator down inline.
        post("kill", body: ["key": key], timeoutSecs: 25)
    }

    /// True when the package is actually present on disk (so the UI can offer install).
    static var packageAvailable: Bool {
        FileManager.default.fileExists(atPath: installJS)
    }

    /// True when a usable `node` can be found (the installer/daemon need it).
    static var nodeAvailable: Bool { resolveNode() != nil }

    /// True once the MCP server's one runtime dependency is present. The daemon needs
    /// no deps, but `mcp.js` imports `@modelcontextprotocol/sdk`, so without this the
    /// installer registers a server that dies the moment Claude Code spawns it —
    /// which looks from the outside like an allocator that installed fine and then
    /// simply never appears.
    static var depsInstalled: Bool {
        FileManager.default.fileExists(
            atPath: nodeModulesDir + "/@modelcontextprotocol/sdk")
    }

    /// Fetch the package's `node_modules` if they aren't there yet. No-op once
    /// present. Blocking (call off the main thread); returns whether the deps ended
    /// up available. Twin of `deviceallocator.ensure_deps` on Linux.
    @discardableResult
    static func ensureDeps() -> Bool {
        guard packageAvailable else { return false }
        if depsInstalled { return true }
        guard let npm = resolveNpm() else { return false }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: npm)
        p.arguments = ["install", "--omit=dev", "--no-audit", "--no-fund"]
        p.currentDirectoryURL = URL(fileURLWithPath: packageDir)
        // npm's own shebang is `env node`; the app may be launched with a PATH that
        // has none, so put the node we resolved in front of whatever it inherited.
        var env = ProcessInfo.processInfo.environment
        if let node = resolveNode() {
            let dir = (node as NSString).deletingLastPathComponent
            env["PATH"] = dir + ":" + (env["PATH"] ?? "")
        }
        p.environment = env
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return false }
        let watchdog = DispatchWorkItem { if p.isRunning { p.terminate() } }
        DispatchQueue.global().asyncAfter(deadline: .now() + 300, execute: watchdog)
        p.waitUntilExit()
        watchdog.cancel()
        return depsInstalled
    }

    // MARK: state

    static func readState() -> DeviceState? {
        guard let data = try? Data(contentsOf: stateURL) else { return nil }
        return try? JSONDecoder().decode(DeviceState.self, from: data)
    }

    // MARK: installer (blocking — call off the main thread)

    /// Whether a launch should run `--install`, given what `--check` reported and
    /// whether this machine's setup has already been settled.
    ///
    /// One routine because it is one decision made in two places (this app and the
    /// Linux applet, whose `deviceallocator.needs_install` is its twin) covering three
    /// situations that look alike and must not be confused:
    ///
    /// - **First run** — nothing installed, nothing settled. Install. A failure leaves
    ///   `setupDone` false, so the next launch retries; that is how a machine with no
    ///   node yet eventually gets set up.
    /// - **Stale** — installed, but the deployed skill/rule/CLAUDE.md/registration no
    ///   longer match this checkout. Re-install: `--install` rewrites every artifact,
    ///   so it is also the repair.
    /// - **Settled uninstall** — the user removed it in Settings. Leave it alone. This
    ///   is the one an "is everything in place?" check gets wrong, and getting it
    ///   wrong means silently reinstalling something the user deliberately took off.
    ///
    /// Derives current from `installed && !outdated` rather than a positive flag: an
    /// installer predating drift detection reports neither, and keying off a missing
    /// flag would reinstall on every launch forever.
    static func needsInstall(status: AllocatorInstall, setupDone: Bool) -> Bool {
        if setupDone && !status.installed { return false }
        return !(status.installed && !status.outdated)
    }

    static func check() -> AllocatorInstall { runInstaller("--check") }
    static func install() -> AllocatorInstall { runInstaller("--install") }
    static func uninstall() -> AllocatorInstall { runInstaller("--uninstall") }

    private static func runInstaller(_ arg: String) -> AllocatorInstall {
        guard packageAvailable, let node = resolveNode() else { return .unknown }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: node)
        p.arguments = [installJS, arg]
        let outPipe = Pipe()
        p.standardOutput = outPipe
        // Discard stderr to null (not a Pipe): an unread stderr Pipe could fill its
        // ~64KB buffer and deadlock the child, blocking our readToEnd forever.
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return .unknown }
        // Bound the wait so a hung node can never deadlock the caller.
        let watchdog = DispatchWorkItem { if p.isRunning { p.terminate() } }
        DispatchQueue.global().asyncAfter(deadline: .now() + 90, execute: watchdog)
        let data = outPipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        watchdog.cancel()
        guard let parsed = try? JSONDecoder().decode(AllocatorInstall.self, from: data) else {
            return .unknown
        }
        return parsed
    }

    /// Find a usable `node` without depending on the (possibly empty) launch-agent
    /// PATH: env override → newest nvm install → Homebrew → /usr/local → /usr.
    static func resolveNode() -> String? {
        if let env = ProcessInfo.processInfo.environment["DIPLOMAT_NODE"],
           FileManager.default.fileExists(atPath: env) { return env }
        let fm = FileManager.default
        let nvm = home.appendingPathComponent(".nvm/versions/node")
        if let versions = try? fm.contentsOfDirectory(atPath: nvm.path) {
            // Highest version dir wins (numeric-aware sort on the leading vMAJOR.MINOR.PATCH).
            let sorted = versions.sorted { a, b in
                a.compare(b, options: .numeric) == .orderedAscending
            }
            for v in sorted.reversed() {
                let candidate = nvm.appendingPathComponent("\(v)/bin/node").path
                if fm.fileExists(atPath: candidate) { return candidate }
            }
        }
        for path in ["/opt/homebrew/bin/node", "/usr/local/bin/node", "/usr/bin/node"] {
            if fm.fileExists(atPath: path) { return path }
        }
        return nil
    }

    /// Find `npm` the same way we find `node`; npm normally sits beside it.
    static func resolveNpm() -> String? {
        let fm = FileManager.default
        if let env = ProcessInfo.processInfo.environment["DIPLOMAT_NPM"],
           fm.fileExists(atPath: env) { return env }
        if let node = resolveNode() {
            let beside = (node as NSString).deletingLastPathComponent + "/npm"
            if fm.fileExists(atPath: beside) { return beside }
        }
        for path in ["/opt/homebrew/bin/npm", "/usr/local/bin/npm", "/usr/bin/npm"] {
            if fm.fileExists(atPath: path) { return path }
        }
        return nil
    }
}
