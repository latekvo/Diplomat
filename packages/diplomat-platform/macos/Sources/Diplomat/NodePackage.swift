import Foundation
import DiplomatCore

/// One of the Node packages beside this app that run a daemon on a unix socket and ship
/// an installer with the device allocator's interface (`src/install.js --check`,
/// `--install`, `--uninstall`, `--start-daemon`, `--restart-daemon`, each printing
/// `AllocatorInstall`'s JSON): the device allocator and the remote-machines MCP.
struct NodePackage {
    /// Its folder under `packages/`, and its state directory's under `~/.diplomat`.
    let name: String
    /// Overrides where the package lives.
    let dirEnv: String
    /// Relocates its state directory, as the package's own `src/paths.js` does.
    let baseEnv: String

    static let deviceAllocator = NodePackage(name: "device-allocator",
                                             dirEnv: "DIPLOMAT_DEVICE_ALLOCATOR_DIR",
                                             baseEnv: "DA_BASE_DIR")
    static let remoteMachines = NodePackage(name: "remote-machines",
                                            dirEnv: "DIPLOMAT_REMOTE_MACHINES_DIR",
                                            baseEnv: "RM_BASE_DIR")

    /// A sibling of this app inside the same checkout, or `dirEnv`, as long as
    /// `daemon-core` sits beside the overriding package (each imports it by relative path).
    ///
    /// Resolved through `RepoPaths.root` rather than hardcoded to `~/dev/diplomat`,
    /// which is the twin of the Linux bridge deriving it from its own file's path: an
    /// app run out of a worktree, a differently-named clone, or a checkout moved
    /// anywhere else must drive *its own* installer, not one belonging to whatever
    /// happens to sit at the conventional path.
    var packageDir: String {
        if let env = ProcessInfo.processInfo.environment[dirEnv], !env.isEmpty { return env }
        return RepoPaths.packages.appendingPathComponent(name).path
    }

    private static var home: URL { FileManager.default.homeDirectoryForCurrentUser }
    var installJS: String { packageDir + "/src/install.js" }
    var nodeModulesDir: String { packageDir + "/node_modules" }
    var baseDir: URL {
        if let env = ProcessInfo.processInfo.environment[baseEnv], !env.isEmpty {
            return URL(fileURLWithPath: (env as NSString).expandingTildeInPath)
        }
        return Self.home.appendingPathComponent(".diplomat/\(name)")
    }
    var socketPath: String { baseDir.appendingPathComponent("daemon.sock").path }

    // MARK: daemon socket

    enum Sent: Equatable {
        case reply(DaemonReply)
        /// The request never reached a daemon: no socket, nothing listening on it, or no
        /// curl to send it with.
        case unreachable
        /// Sent, but no reply came (a timeout, a dropped connection): the daemon may
        /// still act on it.
        case unanswered
    }

    /// POST `body` to the daemon. Any HTTP status is a reply. `curl`, as `URLSession`
    /// cannot speak to a unix socket. Blocking for up to `timeoutSecs`, so off the main
    /// actor; each call is its own curl, so a long one holds up no other.
    func send(_ endpoint: String, body: [String: Any] = [:], timeoutSecs: Int) -> Sent {
        guard FileManager.default.fileExists(atPath: socketPath),
              let data = try? JSONSerialization.data(withJSONObject: body),
              let json = String(data: data, encoding: .utf8) else { return .unreachable }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/curl")
        // The status goes on a last line of its own, after the body.
        p.arguments = ["-s", "--max-time", "\(timeoutSecs)", "--unix-socket", socketPath,
                       "-X", "POST", "http://localhost/\(endpoint)",
                       "-H", "content-type: application/json", "-d", json,
                       "-w", "\n%{http_code}"]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return .unreachable }
        let raw = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        // curl's 7 is "failed to connect": a stale socket file, or one nothing listens on.
        if p.terminationStatus == 7 { return .unreachable }
        guard p.terminationStatus == 0, let reply = Self.parseReply(raw) else { return .unanswered }
        return .reply(reply)
    }

    /// curl's `-w "\n%{http_code}"` output split back into the status and the body.
    static func parseReply(_ raw: Data) -> DaemonReply? {
        guard let nl = raw.lastIndex(of: UInt8(ascii: "\n")),
              let code = Int(String(decoding: raw[raw.index(after: nl)...], as: UTF8.self)),
              code > 0 else { return nil }
        return DaemonReply(status: code,
                           json: try? JSONDecoder().decode(JSONValue.self, from: raw[..<nl]))
    }

    // MARK: installer (blocking — call off the main thread)

    /// True when the package is actually present on disk (so the UI can offer install).
    var packageAvailable: Bool { FileManager.default.fileExists(atPath: installJS) }

    /// True once the MCP server's one runtime dependency is present. A daemon needs no
    /// deps, but `mcp.js` imports `@modelcontextprotocol/sdk`, so without this the
    /// installer registers a server that dies the moment Claude Code spawns it —
    /// which looks from the outside like a package that installed fine and then
    /// simply never appears.
    var depsInstalled: Bool {
        FileManager.default.fileExists(atPath: nodeModulesDir + "/@modelcontextprotocol/sdk")
    }

    /// Fetch the package's `node_modules` if they aren't there yet. No-op once
    /// present. Returns whether the deps ended up available. Twin of
    /// `deviceallocator.ensure_deps` on Linux.
    @discardableResult
    func ensureDeps() -> Bool {
        guard packageAvailable else { return false }
        if depsInstalled { return true }
        guard let npm = Self.resolveNpm() else { return false }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: npm)
        p.arguments = ["install", "--omit=dev", "--no-audit", "--no-fund"]
        p.currentDirectoryURL = URL(fileURLWithPath: packageDir)
        // npm's own shebang is `env node`; the app may be launched with a PATH that
        // has none, so put the node we resolved in front of whatever it inherited.
        var env = ProcessInfo.processInfo.environment
        if let node = Self.resolveNode() {
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

    func runInstaller(_ arg: String) -> AllocatorInstall {
        guard packageAvailable, let node = Self.resolveNode() else { return .unknown }
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
