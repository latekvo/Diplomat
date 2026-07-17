import Foundation

/// Bridge to the local device-allocator daemon + installer (the Node package under
/// `device-allocator/`). The applet is a *viewer* of the daemon's public state file
/// and a *driver* of its installer — it never allocates devices itself.
///
/// Two surfaces:
///   - `readState()` decodes the daemon's `~/.argent/device-allocator/state.json`
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
    }

    private enum CodingKeys: String, CodingKey {
        case mcpRegistered, skillInstalled, ruleInstalled, claudeMdInjected, daemonRunning, installed
    }

    /// Unknown until the first check completes (so the UI can say "checking…").
    static let unknown = AllocatorInstall()
}

enum DeviceAllocator {
    /// Where the Node package lives. Overridable for non-standard checkouts; defaults
    /// to the user's repo path (this is a personal, single-checkout setup).
    static var packageDir: String {
        if let env = ProcessInfo.processInfo.environment["CO_MAINTAINER_DEVICE_ALLOCATOR_DIR"], !env.isEmpty {
            return env
        }
        return home.appendingPathComponent("dev/co-maintainer-applet/device-allocator").path
    }

    private static var home: URL { FileManager.default.homeDirectoryForCurrentUser }
    static var installJS: String { packageDir + "/src/install.js" }
    static var stateURL: URL {
        home.appendingPathComponent(".argent/device-allocator/state.json")
    }
    static var socketPath: String {
        home.appendingPathComponent(".argent/device-allocator/daemon.sock").path
    }

    /// Ask the daemon to force-kill a device by key (free any allocation + shut the
    /// sim/emulator down). Backs the panel's per-device X. Uses curl over the daemon's
    /// unix socket. Best-effort; returns whether the request succeeded.
    @discardableResult
    static func killDevice(key: String) -> Bool {
        guard FileManager.default.fileExists(atPath: socketPath) else { return false }
        let payload = (try? JSONSerialization.data(withJSONObject: ["key": key]))
            .flatMap { String(data: $0, encoding: .utf8) } ?? "{\"key\":\"\(key)\"}"
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/curl")
        p.arguments = ["-s", "--max-time", "25", "--unix-socket", socketPath,
                       "-X", "POST", "http://localhost/kill",
                       "-H", "content-type: application/json", "-d", payload]
        p.standardOutput = Pipe()
        p.standardError = Pipe()
        do { try p.run() } catch { return false }
        p.waitUntilExit()
        return p.terminationStatus == 0
    }

    /// True when the package is actually present on disk (so the UI can offer install).
    static var packageAvailable: Bool {
        FileManager.default.fileExists(atPath: installJS)
    }

    /// True when a usable `node` can be found (the installer/daemon need it).
    static var nodeAvailable: Bool { resolveNode() != nil }

    // MARK: state

    static func readState() -> DeviceState? {
        guard let data = try? Data(contentsOf: stateURL) else { return nil }
        return try? JSONDecoder().decode(DeviceState.self, from: data)
    }

    // MARK: installer (blocking — call off the main thread)

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
        if let env = ProcessInfo.processInfo.environment["CO_MAINTAINER_NODE"],
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
}
