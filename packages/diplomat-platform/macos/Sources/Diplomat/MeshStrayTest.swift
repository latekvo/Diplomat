import DiplomatCore
import Foundation

/// Self-test for stopping a node left running while the mesh is off -
/// `DIPLOMAT_MESH_STRAY_TEST=1`.
///
/// `Store.stopStrayMeshNode` runs at every launch with the mesh off, against whatever
/// `state.json` names, so what it must NOT stop matters as much as what it must: a pid
/// the OS has since handed to another process, a port a node on another state dir has
/// since bound, a node with another identity. Stand-in nodes that speak just enough of
/// the control protocol (`status`, `stop`) to be told apart cover those. Real nodes,
/// loopback-only with no WAN transport, cover the reply a real one gives and a launch
/// with the mesh on.
///
///   DIPLOMAT_MESH_STRAY_TEST=1 swift run Diplomat
///
/// It points `SZPONTNET_DIR` and `DIPLOMAT_AUDIT_DIR` at scratch directories of its own
/// before anything reads them, so it never sees the operator's node or feed. Needs a
/// `python3`, and a checkout for the real node. Twin of
/// `diplomat-platform/linux/tests/test_mesh_stray_node.py`. Exit code is pass/fail.
@MainActor
enum MeshStrayTest {
    /// A node as far as `stopStrayNode` can tell: writes `state.json` into its
    /// `SZPONTNET_DIR` and answers `status` with its own pid and id. `stubborn`
    /// acknowledges `stop` and keeps running; `refuse` answers it with an error; `slow`
    /// writes `state.json.stopping` and exits 1.5s later.
    private static let standIn = """
        import json, os, socket, sys, time
        node_id, mode = sys.argv[1], (sys.argv[2:] or [""])[0]
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        me = {"pid": os.getpid(), "tcpPort": srv.getsockname()[1], "self": {"id": node_id}}
        path = os.path.join(os.environ["SZPONTNET_DIR"], "state.json")
        with open(path + ".tmp", "w") as f:
            json.dump(me, f)
        os.replace(path + ".tmp", path)
        while True:
            conn, _ = srv.accept()
            with conn, conn.makefile("rwb") as f:
                f.readline()
                t = json.loads(f.readline() or b"{}").get("t")
                f.write(json.dumps({"t": "state", "state": me} if t == "status"
                                   else {"t": "error", "reason": "refused"} if mode == "refuse"
                                   else {"t": "ok"}).encode() + b"\\n")
            if t == "stop" and mode == "slow":
                open(path + ".stopping", "w").close()
                time.sleep(1.5)
            if t == "stop" and mode in ("", "slow"):
                sys.exit(0)
        """

    private struct Node {
        let process: Process
        let pid: Int
        let port: Int
        let id: String
    }

    static func run() async -> Bool {
        var failures: [String] = []
        func check(_ name: String, _ condition: Bool, _ detail: @autoclosure () -> String = "") {
            if condition {
                print("  ok    \(name)")
            } else {
                let d = detail()
                print("  FAIL  \(name)\(d.isEmpty ? "" : " — \(d)")")
                failures.append(name)
            }
        }

        let fm = FileManager.default
        let scratch = fm.temporaryDirectory
            .appendingPathComponent("diplomat-meshstray-\(UUID().uuidString)")
        let ours = scratch.appendingPathComponent("mesh")
        let theirs = scratch.appendingPathComponent("other-mesh")
        let feed = scratch.appendingPathComponent("audit")
        for dir in [ours, theirs, feed] {
            try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        }
        setenv("SZPONTNET_DIR", ours.path, 1)
        setenv("DIPLOMAT_AUDIT_DIR", feed.path, 1)
        let script = scratch.appendingPathComponent("standin.py")
        try? standIn.write(to: script, atomically: true, encoding: .utf8)

        var started: [Process] = []
        defer {
            for p in started where p.isRunning { p.terminate() }
            try? fm.removeItem(at: scratch)
        }

        guard let python = MeshBridge.resolvePython() else {
            print("  FAIL  no python3 to run the stand-in nodes")
            return false
        }

        func launch(_ id: String, in dir: URL, mode: String = "") -> Node? {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: python)
            p.arguments = [script.path, id] + (mode.isEmpty ? [] : [mode])
            var env = ProcessInfo.processInfo.environment
            env["SZPONTNET_DIR"] = dir.path
            p.environment = env
            let state = dir.appendingPathComponent("state.json")
            try? fm.removeItem(at: state)
            guard (try? p.run()) != nil else { return nil }
            started.append(p)
            let deadline = Date().addingTimeInterval(10)
            while Date() < deadline {
                if let data = try? Data(contentsOf: state), let snap = MeshSnapshot.decode(data),
                   snap.pid == Int(p.processIdentifier), let port = snap.tcpPort {
                    return Node(process: p, pid: snap.pid ?? 0, port: port, id: id)
                }
                usleep(50_000)
            }
            return nil
        }

        func sleeper() -> Process {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/bin/sleep")
            p.arguments = ["60"]
            try? p.run()
            started.append(p)
            return p
        }

        /// Our `state.json`, naming whatever the case needs it to.
        func name(pid: Int, port: Int, id: String) {
            let json = #"{"pid": \#(pid), "tcpPort": \#(port), "self": {"id": "\#(id)"}}"#
            try? json.write(to: ours.appendingPathComponent("state.json"),
                            atomically: true, encoding: .utf8)
        }

        /// A loopback port nothing listens on.
        func closedPort() -> Int {
            let fd = socket(AF_INET, SOCK_STREAM, 0)
            defer { close(fd) }
            var addr = sockaddr_in()
            addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
            addr.sin_family = sa_family_t(AF_INET)
            inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr)
            var len = socklen_t(MemoryLayout<sockaddr_in>.size)
            _ = withUnsafeMutablePointer(to: &addr) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    bind(fd, $0, len) == 0 ? getsockname(fd, $0, &len) : -1
                }
            }
            return Int(UInt16(bigEndian: addr.sin_port))
        }

        func auditLines() -> [String] {
            ((try? String(contentsOf: AuditLog.fileURL, encoding: .utf8)) ?? "")
                .split(whereSeparator: \.isNewline).map(String.init)
        }

        let store = Store()
        // Read from the operator's defaults; left on, a stop below would start a node
        // before the loopback env is set.
        store.meshEnabled = false
        print("== Store.stopStrayMeshNode ==")

        check("no state.json: nothing to stop",
              await store.stopStrayMeshNode() == .none)

        guard let a = launch("n-a", in: ours) else {
            print("  FAIL  the stand-in node never wrote its state.json")
            return false
        }
        await store.settleMeshOnLaunch()
        check("a launch with the mesh off stops the node state.json names", !a.process.isRunning)
        let stopLines = auditLines().filter { $0.contains("\"mesh-stop\"") }
        check("and the audit feed says so, naming the pid",
              stopLines.count == 1 && stopLines[0].contains("pid \(a.pid)"), "\(stopLines)")

        // The common case: the file outlived its node. Whatever holds the port now is
        // not ours to send control lines to.
        do {
            let gone = Process()
            gone.executableURL = URL(fileURLWithPath: "/usr/bin/true")
            try? gone.run()
            gone.waitUntilExit()
            let listener = socket(AF_INET, SOCK_STREAM, 0)
            defer { close(listener) }
            var addr = sockaddr_in()
            addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
            addr.sin_family = sa_family_t(AF_INET)
            inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr)
            var len = socklen_t(MemoryLayout<sockaddr_in>.size)
            _ = withUnsafeMutablePointer(to: &addr) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    bind(listener, $0, len) == 0 && listen(listener, 1) == 0
                        ? getsockname(listener, $0, &len) : -1
                }
            }
            _ = fcntl(listener, F_SETFL, O_NONBLOCK)
            name(pid: Int(gone.processIdentifier), port: Int(UInt16(bigEndian: addr.sin_port)),
                 id: "n-a")
            let outcome = await store.stopStrayMeshNode()
            let dialled = accept(listener, nil, nil)
            if dialled >= 0 { close(dialled) }
            check("a dead pid dials nothing", outcome == .none && dialled < 0,
                  "outcome \(outcome), dialled \(dialled >= 0)")
        }

        let reused = sleeper()
        let reusedPid = Int(reused.processIdentifier)
        name(pid: reusedPid, port: closedPort(), id: "n-a")
        check("a pid the OS handed to another process is left alone",
              await store.stopStrayMeshNode() == .none && reused.isRunning)

        guard let b = launch("n-b", in: theirs) else {
            print("  FAIL  the other state dir's stand-in never wrote its state.json")
            return false
        }
        name(pid: reusedPid, port: b.port, id: b.id)
        check("a node another state dir runs, on the port the file names, is left alone",
              await store.stopStrayMeshNode() == .none && b.process.isRunning
                && reused.isRunning)

        name(pid: b.pid, port: b.port, id: "n-a")
        check("a node with another identity is left alone",
              await store.stopStrayMeshNode() == .none && b.process.isRunning)

        name(pid: b.pid, port: b.port, id: b.id)
        check("the same node, named by pid, port and id, is stopped",
              await store.stopStrayMeshNode() == .stopped(pid: b.pid, port: b.port)
                && !b.process.isRunning)

        guard let c = launch("n-c", in: ours, mode: "stubborn") else {
            print("  FAIL  the stubborn stand-in never wrote its state.json")
            return false
        }
        let stubborn = await store.stopStrayMeshNode(exitWait: 1)
        if case .stopFailed(let pid, _, _) = stubborn {
            check("a node that outlives its stop is not reported stopped", pid == c.pid)
        } else {
            check("a node that outlives its stop is not reported stopped", false, "got \(stubborn)")
        }
        let warnLines = auditLines().filter { $0.contains("\"warn\"") }
        check("and the audit feed says it did not stop",
              warnLines.count == 1 && warnLines[0].contains("pid \(c.pid)"), "\(warnLines)")
        c.process.terminate()

        guard let d = launch("n-d", in: ours, mode: "refuse") else {
            print("  FAIL  the refusing stand-in never wrote its state.json")
            return false
        }
        let refused = await store.stopStrayMeshNode()
        check("a node that refuses its stop is not reported stopped",
              refused == .stopFailed(pid: d.pid, port: d.port, reason: "refused")
                && d.process.isRunning, "got \(refused)")
        check("and the audit feed gives its reason",
              auditLines().last?.contains("(pid \(d.pid), :\(d.port)) did not stop: refused") == true,
              auditLines().last ?? "")
        check("nothing else was written to the feed",
              auditLines().count == 4, "\(auditLines().count) lines")
        d.process.terminate()

        // Real nodes, spawned the way the app spawns one: the stand-ins above only speak
        // the two commands, so this is what pins the reply of a real one. Loopback-only
        // on ports of its own, no WAN transport, and a HOME of its own for the activity
        // feed its host writes to.
        if RepoPaths.checkoutPresent {
            let realHome = ProcessInfo.processInfo.environment["HOME"]
            let nodeEnv = ["SZPONTNET_LOOPBACK": "1", "SZPONTNET_TOR": "0", "SZPONTNET_IROH": "0",
                           "SZPONTNET_OAUTH_PROBE": "0", "SZPONTNET_MCAST_PORT": "51811",
                           "SZPONTNET_TCP_BASE": "51812",
                           "HOME": scratch.appendingPathComponent("home").path]
            for (k, v) in nodeEnv { setenv(k, v, 1) }
            defer { if let realHome { setenv("HOME", realHome, 1) } }

            /// The node live on our state dir, other than `other`, within 30s.
            func liveNode(other: Int? = nil) async -> MeshSnapshot? {
                let deadline = Date().addingTimeInterval(30)
                while Date() < deadline {
                    if let snap = MeshBridge.readState(), MeshBridge.nodeRunning(snap),
                       snap.pid != other { return snap }
                    try? await Task.sleep(nanoseconds: 200_000_000)
                }
                return nil
            }

            try? fm.removeItem(at: ours.appendingPathComponent("state.json"))
            store.meshEnabled = true
            await store.settleMeshOnLaunch()
            let real = await liveNode()
            check("a launch with the mesh on starts a node", real != nil,
                  store.meshError ?? "no live state.json within 30s")
            if let real, let pid = real.pid, let port = real.tcpPort {
                await store.settleMeshOnLaunch()
                check("a launch with the mesh on leaves its node alone",
                      MeshBridge.nodeRunning(real), "pid \(pid) stopped")
                store.meshEnabled = false
                await store.settleMeshOnLaunch()
                check("a real node left on this state dir is stopped at launch",
                      !MeshBridge.nodeRunning(real), "pid \(pid) still running")
                check("and the audit feed says so",
                      auditLines().last?.contains("pid \(pid), :\(port)") == true
                        && auditLines().last?.contains("\"mesh-stop\"") == true)
                if MeshBridge.nodeRunning(real) { kill(pid_t(clamping: pid), SIGTERM) }
            }

            // The Settings toggle finds the stray node still alive and starts nothing, so
            // the stop is what has to.
            if let e = launch("n-e", in: ours, mode: "slow") {
                store.meshEnabled = false
                let settling = Task { await store.settleMeshOnLaunch() }
                let stopping = ours.appendingPathComponent("state.json.stopping")
                let deadline = Date().addingTimeInterval(10)
                while !fm.fileExists(atPath: stopping.path), Date() < deadline {
                    try? await Task.sleep(nanoseconds: 50_000_000)
                }
                store.meshEnabled = true
                store.ensureMeshRunning()  // the toggle's didSet does nothing headless
                await settling.value
                let next = await liveNode(other: e.pid)
                check("turning the mesh on while a stray node exits leaves a node running",
                      !e.process.isRunning && next != nil,
                      "stray running \(e.process.isRunning), \(store.meshError ?? "no new node")")
                if let pid = next?.pid { kill(pid_t(clamping: pid), SIGTERM) }
            } else {
                check("the slow stand-in wrote its state.json", false)
            }
        } else {
            print("  skip  real nodes: no checkout to run one from")
        }

        print(failures.isEmpty ? "MESH STRAY TEST OK" : "MESH STRAY TEST FAILED (\(failures.count))")
        return failures.isEmpty
    }
}
