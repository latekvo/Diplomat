import Foundation
import Darwin
import CoMaintainerCore

/// The macOS bridge to a local Co-Maintainer Mesh node — the counterpart of the Linux
/// front-end's `store` mesh helpers (`ensure_mesh_running_async`, `mesh.statefile`,
/// `mesh.ctl`). Two surfaces:
///   - a *viewer* of the node's public topology snapshot (`~/.argent/mesh/state.json`);
///   - a *driver* that spawns the node (`python3 -m co_maintainer.mesh --daemon`, run
///     from the checkout's `linux/` tree) and talks its synchronous control protocol
///     (one NDJSON command → one reply over a loopback TCP connection).
///
/// The mesh node itself is stdlib-only Python that runs on any OS (see the README);
/// a Swift node is future work, so macOS drives the same Python node the Linux applet
/// does. All blocking calls here are meant to run off the main thread (the Store wraps
/// them in detached tasks, like `DeviceAllocator`).

struct MeshCtlError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

enum MeshBridge {
    private static var home: URL { FileManager.default.homeDirectoryForCurrentUser }

    /// The node's state directory (`CO_MAINTAINER_MESH_DIR` override, else `~/.argent/mesh`) —
    /// matches `co_maintainer.mesh.identity.mesh_dir`.
    static var stateDir: URL {
        if let env = ProcessInfo.processInfo.environment["CO_MAINTAINER_MESH_DIR"], !env.isEmpty {
            return URL(fileURLWithPath: env)
        }
        return home.appendingPathComponent(".argent/mesh")
    }
    static var stateURL: URL { stateDir.appendingPathComponent("state.json") }

    /// Optional pre-shared join token (`CO_MAINTAINER_MESH_SECRET`), presented on every control
    /// session — mirrors `mesh.config.secret`. Empty (the default) = open mesh.
    static var secret: String { ProcessInfo.processInfo.environment["CO_MAINTAINER_MESH_SECRET"] ?? "" }

    // MARK: - viewer

    /// Decode the node's public topology snapshot; nil if the node has never run here.
    static func readState() -> MeshSnapshot? {
        guard let data = try? Data(contentsOf: stateURL) else { return nil }
        return MeshSnapshot.decode(data)
    }

    /// True when a local node appears alive: the snapshot names a live pid. Mirrors
    /// `statefile.node_running` (a suspended laptop resumes with a stale timestamp but a
    /// live pid, so we key on the pid, not freshness).
    static func nodeRunning(_ snap: MeshSnapshot? = nil) -> Bool {
        let s = snap ?? readState()
        guard let pid = s?.pid, pid > 0 else { return false }
        // kill(pid, 0): 0 ⇒ alive & ours; -1/EPERM ⇒ alive but not ours; -1/ESRCH ⇒ gone.
        if kill(pid_t(pid), 0) == 0 { return true }
        return errno == EPERM
    }

    // MARK: - node spawn

    /// Start a background mesh node iff none is already alive. Returns nil on success, or
    /// a human-readable reason it couldn't start (missing checkout / python / spawn error)
    /// for the Store to surface. Blocking — call off the main thread.
    ///
    /// `--daemon` double-forks and returns immediately (see the Python `_daemonize`), so
    /// this waits only for that quick detach, not the node's lifetime.
    @discardableResult
    static func ensureRunning() -> String? {
        if nodeRunning() { return nil }
        guard RepoPaths.checkoutPresent else {
            return "no Co-Maintainer checkout at \(RepoPaths.root.path) — set CO_MAINTAINER_SELF_REPO to point at it"
        }
        guard let python = resolvePython() else {
            return "python3 not found — install Python 3 (or set CO_MAINTAINER_PYTHON) to run a mesh node"
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = ["-m", "co_maintainer.mesh", "--daemon"]
        p.currentDirectoryURL = RepoPaths.root.appendingPathComponent("linux")
        p.standardInput = FileHandle.nullDevice
        // Discard output to null (not a Pipe) so an unread buffer can never deadlock the
        // detach — same rule as DeviceAllocator's installer shell-out.
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch {
            return "could not start mesh node: \((error as? LocalizedError)?.errorDescription ?? "\(error)")"
        }
        let watchdog = DispatchWorkItem { if p.isRunning { p.terminate() } }
        DispatchQueue.global().asyncAfter(deadline: .now() + 20, execute: watchdog)
        p.waitUntilExit()
        watchdog.cancel()
        if p.terminationStatus != 0 {
            return "mesh node failed to detach (exit \(p.terminationStatus))"
        }
        return nil
    }

    /// Find a usable `python3`: env override → Homebrew → /usr/local → the system one.
    static func resolvePython() -> String? {
        if let env = ProcessInfo.processInfo.environment["CO_MAINTAINER_PYTHON"],
           FileManager.default.fileExists(atPath: env) { return env }
        for path in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"] {
            if FileManager.default.fileExists(atPath: path) { return path }
        }
        return nil
    }

    // MARK: - control protocol (one command → one reply)

    /// Edit a node's attributes (self or a peer, forwarded over the mesh). `target` is a
    /// node id or "self". Mirrors `ctl.set_attr`.
    static func setAttr(target: String, attrs: [String: Any], port: Int) throws {
        _ = try request(["t": "set-attr", "target": target, "attrs": attrs], port: port)
    }

    /// Edit one duty's mesh-wide placement (gossiped last-writer-wins). Mirrors
    /// `ctl.set_overrides`.
    static func setOverrides(duty: String, placement: [String: Any], port: Int) throws {
        _ = try request(["t": "set-overrides", "duty": duty, "placement": placement], port: port)
    }

    /// Ask the local node to stop (used when the user disables the mesh). Mirrors `ctl.stop`.
    static func stop(port: Int) throws {
        _ = try request(["t": "stop"], port: port)
    }

    /// Mark a peer's device Personal — add its proven fingerprint to the local trusted
    /// allowlist. Mirrors `ctl.trust_device`.
    static func trust(fingerprint: String, label: String, port: Int) throws {
        _ = try request(["t": "trust", "fingerprint": fingerprint, "label": label], port: port)
    }

    /// Mark a peer's device Foreign — remove its fingerprint from the allowlist. Mirrors
    /// `ctl.untrust_device`.
    static func untrust(fingerprint: String, port: Int) throws {
        _ = try request(["t": "untrust", "fingerprint": fingerprint], port: port)
    }

    /// Hand a duty job to the mesh: the local node picks the target (per the dispatch
    /// strategy, with failover) unless `target` pins a node id, and the chosen executor
    /// spawns the agent. Returns the per-node result dicts (`status`: spawned / declined /
    /// failed + `reason`). Mirrors `ctl.dispatch`. A remote spawn can take a while to
    /// ack, hence the generous timeout.
    static func dispatch(duty: String, prompt: String, target: String? = nil, port: Int,
                         timeout: TimeInterval = 60) throws -> [[String: Any]] {
        var msg: [String: Any] = ["t": "dispatch", "duty": duty, "prompt": prompt]
        if let target { msg["target"] = target }
        let reply = try request(msg, port: port, timeout: timeout)
        return (reply["results"] as? [[String: Any]]) ?? []
    }

    /// One command, one reply, over a fresh loopback TCP connection to the node's control
    /// port — the Swift port of `mesh.ctl.request`. Blocking; throws `MeshCtlError` when
    /// the node is unreachable, silent, or answers with an error.
    @discardableResult
    static func request(_ msg: [String: Any], port: Int, timeout: TimeInterval = 5) throws -> [String: Any] {
        guard port > 0 else { throw MeshCtlError(message: "state.json has no usable tcpPort") }
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw MeshCtlError(message: "could not open a control socket") }
        defer { close(fd) }

        var tv = timeval(tv_sec: Int(timeout), tv_usec: 0)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))

        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = in_port_t(UInt16(truncatingIfNeeded: port).bigEndian)
        inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr)
        let rc = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard rc == 0 else {
            throw MeshCtlError(message: "mesh node unreachable on :\(port) (is it running?)")
        }

        // A control session opens with a ctl hello (not a peer hello), then the command.
        try sendMessage(fd, ctlHello())
        try sendMessage(fd, msg)

        let line = try recvLine(fd)
        guard !line.isEmpty,
              let reply = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else {
            throw MeshCtlError(message: "mesh node closed the control session without answering")
        }
        if (reply["t"] as? String) == "error" {
            throw MeshCtlError(message: (reply["reason"] as? String) ?? "unknown error")
        }
        return reply
    }

    private static func ctlHello() -> [String: Any] {
        var m: [String: Any] = ["t": "ctl"]
        if !secret.isEmpty { m["secret"] = secret }
        return m
    }

    /// Serialize one message to a single NDJSON line (the protocol stamps `v` on every
    /// message) and write it in full.
    private static func sendMessage(_ fd: Int32, _ msg: [String: Any]) throws {
        var m = msg
        if m["v"] == nil { m["v"] = 1 }
        var data = try JSONSerialization.data(withJSONObject: m)
        data.append(0x0A)  // NDJSON line terminator
        try data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            guard let base = raw.bindMemory(to: UInt8.self).baseAddress else { return }
            var sent = 0
            while sent < data.count {
                let n = send(fd, base + sent, data.count - sent, 0)
                if n <= 0 { throw MeshCtlError(message: "control write failed") }
                sent += n
            }
        }
    }

    /// Read one NDJSON line (up to the protocol's max) — the reply. Byte-at-a-time is
    /// fine: control replies are tiny (an echo or a status snapshot).
    private static func recvLine(_ fd: Int32, max: Int = 512 * 1024) throws -> Data {
        var out = Data()
        var byte: UInt8 = 0
        while out.count < max {
            let n = recv(fd, &byte, 1, 0)
            if n == 0 { break }                 // EOF
            if n < 0 { throw MeshCtlError(message: "mesh node read timed out") }
            if byte == 0x0A { break }           // end of line
            out.append(byte)
        }
        return out
    }
}
