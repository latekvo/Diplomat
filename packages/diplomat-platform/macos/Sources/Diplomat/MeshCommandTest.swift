import DiplomatCore
import Foundation

/// Self-test for the shared mesh-control routine — `DIPLOMAT_MESH_CMD_TEST=1`.
///
/// Every mesh edit the panel offers (set an attribute, trust/untrust a device, lift a
/// ban, set the default trust, re-place a duty, pick the preferred WAN transport, link
/// to a pasted id) runs through `Store.meshCommand`. Its three steps are all
/// load-bearing and none of them is visible on screen when it goes missing: skip the
/// `meshTick()` and the panel keeps rendering pre-edit state, skip the
/// `meshError` assignment and a *rejected* edit is indistinguishable from an applied one,
/// run the round-trip on the main actor and the popover freezes for the socket timeout.
/// So drive the routine with closures that succeed, throw a `MeshCtlError`, and throw
/// something else — then drive every real command and check the same properties.
///
///   SZPONTNET_DIR=$(mktemp -d) DIPLOMAT_SELF_REPO=/nonexistent \
///     DIPLOMAT_MESH_CMD_TEST=1 swift run Diplomat
///
/// No node is contacted or started: the commands run against a snapshot with no control
/// port, which `MeshBridge.request` rejects before it opens a socket. The two env vars are
/// required (the test refuses to run without them) — `SZPONTNET_DIR` so the
/// `meshTick()` re-read sees a known-empty state dir instead of the developer's live node,
/// and `DIPLOMAT_SELF_REPO` so a broken headless guard cannot double-fork a real daemon.
/// Twin of `diplomat-platform/linux/tests/test_mesh_store_commands.py`.
///
/// Exit code is pass/fail, so CI can gate on it.
@MainActor
enum MeshCommandTest {
    /// A box the detached closure can write to. The closure runs off the main actor, so
    /// what it records is read back only after the command has fully settled.
    private final class Probe: @unchecked Sendable {
        var ports: [Int] = []
        var ranOffMain = false
    }

    /// An error that is NOT a `LocalizedError`, to pin the `"\(error)"` fallback.
    private struct PlainError: Error {}

    /// What `MeshBridge.request` throws when the snapshot names no control port. Driving
    /// the commands through this path exercises the real bridge call without a node.
    private static let noPortMessage = "state.json has no usable tcpPort"

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

        print("== Store.meshCommand ==")

        // The mesh dir has to be a scratch path, or `meshTick()` re-reads the developer's
        // live node and every "the topology was re-read" check below stops discriminating.
        guard ProcessInfo.processInfo.environment["SZPONTNET_DIR"] != nil,
              MeshBridge.readState() == nil else {
            print("  FAIL  needs SZPONTNET_DIR pointing at a directory with no state.json"
                + " (got \(MeshBridge.stateDir.path))")
            return false
        }
        // And `DIPLOMAT_SELF_REPO` has to point somewhere that is NOT a checkout. That
        // makes the test hermetic: if the headless guard on the mesh toggle ever breaks,
        // `ensureRunning` reports a missing checkout instead of double-forking a real node
        // that outlives this process holding a scratch state dir. It also turns the spawn
        // attempt into something observable — `meshError` — rather than a race with a
        // daemon's first state.json write.
        guard !RepoPaths.checkoutPresent else {
            print("  FAIL  needs DIPLOMAT_SELF_REPO pointing at a non-checkout, so a broken"
                + " guard cannot spawn a real node (got \(RepoPaths.root.path))")
            return false
        }

        // 0. The two knobs this app and the node must resolve identically. Both live in
        //    SzpontNet's namespace, and the node honours the pre-rename spelling when the
        //    current one is unset — so this side has to as well. Disagree on `DIR` and the
        //    panel renders a topology nobody writes; disagree on `SECRET` and every
        //    control session below is refused by the node this app just started, with the
        //    node perfectly healthy. Twin of `szpontnet-core/tests/test_env.py`.
        do {
            let scratchDir = ProcessInfo.processInfo.environment["SZPONTNET_DIR"]!
            defer {  // this test's own preconditions depend on DIR; put it back
                setenv("SZPONTNET_DIR", scratchDir, 1)
                unsetenv("DIPLOMAT_MESH_DIR")
                unsetenv("SZPONTNET_SECRET")
            }

            unsetenv("SZPONTNET_SECRET")
            unsetenv("DIPLOMAT_MESH_SECRET")
            check("no token set is an open mesh", MeshBridge.secret.isEmpty)

            setenv("DIPLOMAT_MESH_SECRET", "from-an-old-profile", 1)
            check("a token under the pre-rename name still fences this app",
                  MeshBridge.secret == "from-an-old-profile", "got '\(MeshBridge.secret)'")

            setenv("SZPONTNET_SECRET", "current", 1)
            check("the current name wins when both are set",
                  MeshBridge.secret == "current", "got '\(MeshBridge.secret)'")
            unsetenv("DIPLOMAT_MESH_SECRET")

            unsetenv("SZPONTNET_DIR")
            setenv("DIPLOMAT_MESH_DIR", "/var/tmp/old-spelling", 1)
            check("a state dir under the pre-rename name is honoured too",
                  MeshBridge.stateDir.path == "/var/tmp/old-spelling",
                  "got \(MeshBridge.stateDir.path)")
        }

        // Enabling the mesh is what un-gates `meshTick()`. In a headless mode it must
        // persist nothing and start no node — `Headless.active` guards both in the Store.
        // Probe with the OPPOSITE of whatever is already stored, so the check can't pass
        // just because the stored value happens to match what we are about to set.
        func stored() -> Bool? { UserDefaults.standard.object(forKey: "meshEnabled") as? Bool }
        func show(_ v: Bool?) -> String { v.map { "\($0)" } ?? "unset" }
        let before = stored()
        let store = Store()
        store.meshEnabled = !(before ?? false)
        check("toggling the mesh in a headless mode persists nothing", stored() == before,
              "meshEnabled went \(show(before)) → \(show(stored())) in the defaults domain")

        // Force a real off→on transition (that is what auto-starts a node in the live app)
        // and give the spawn task a moment. With the guard in place nothing is attempted;
        // without it, `ensureRunning` reports the missing checkout into `meshError`.
        store.meshEnabled = false
        store.meshEnabled = true
        try? await Task.sleep(nanoseconds: 500_000_000)
        check("enabling the mesh in a headless mode starts no node", store.meshError == nil,
              "spawn attempted: \(store.meshError ?? "")")
        store.meshError = nil

        /// Seed a topology so there is something for the refresh to clear, and so the
        /// command has a port to forward.
        func seed(port: Int) {
            let json = """
            {"pid": \(getpid()), "tcpPort": \(port), "v": 1,
             "self": {"id": "n-selftest", "name": "selftest", "platform": "macos",
                      "tier": 2, "tokens": "ok", "sees": []},
             "peers": [], "assignments": {}}
            """
            store.meshState = MeshSnapshot.decode(json.data(using: .utf8)!)
        }

        /// Wait for the screen to settle: the command fires a detached round-trip and
        /// returns, so poll for the main-actor continuation that clears the seeded
        /// snapshot (proof `meshTick()` ran against the empty scratch dir).
        func settle() async -> Bool {
            let deadline = Date().addingTimeInterval(5)
            while Date() < deadline {
                if store.meshState == nil { return true }
                try? await Task.sleep(nanoseconds: 10_000_000)
            }
            return false
        }

        // 1. Success: the seeded port reaches the closure off the main actor, a stale
        //    error clears, and the topology is re-read.
        let probe = Probe()
        seed(port: 40881)
        store.meshError = "a stale error from an earlier edit"
        store.meshCommand { port in
            probe.ports.append(port)
            probe.ranOffMain = !Thread.isMainThread
        }
        check("a successful command settles", await settle())
        check("the command forwards the node's control port", probe.ports == [40881],
              "got \(probe.ports)")
        check("the round-trip runs off the main actor", probe.ranOffMain)
        check("success clears a previous error", store.meshError == nil,
              "got \(store.meshError ?? "nil")")

        // 2. A rejected edit: the MeshCtlError's own message is what the screen shows,
        //    and the topology is re-read anyway so the screen can't keep the attempt.
        seed(port: 40882)
        store.meshCommand { port in
            probe.ports.append(port)
            throw MeshCtlError(message: "node refused: not running")
        }
        check("a rejected command settles (topology re-read)", await settle())
        check("a rejected edit surfaces the reason",
              store.meshError == "node refused: not running", "got \(store.meshError ?? "nil")")

        // 3. Anything that is not a MeshCtlError still has to produce *some* message
        //    rather than reading as success — the `?? "\(error)"` fallback.
        seed(port: 40883)
        store.meshCommand { port in
            probe.ports.append(port)
            throw PlainError()
        }
        check("a non-localized failure settles", await settle())
        check("a non-localized failure still reports something",
              store.meshError?.contains("PlainError") == true, "got \(store.meshError ?? "nil")")

        check("each command forwarded the port current at call time",
              probe.ports == [40881, 40882, 40883], "got \(probe.ports)")

        // 4. Every real command. A snapshot with no control port makes every
        //    `MeshBridge` call fail before it opens a socket, so this exercises the real
        //    bridge path — and proves each command actually goes through the routine
        //    (an edit that skipped it would leave `meshError` or `meshState` untouched).
        let commands: [(String, () -> Void)] = [
            ("meshSetAttr", { store.meshSetAttr(nodeID: "self", attrs: ["tier": 3]) }),
            ("meshSetTrust(trust)", { store.meshSetTrust(fingerprint: "ff11", label: "box", trusted: true) }),
            ("meshSetTrust(untrust)", { store.meshSetTrust(fingerprint: "ff11", label: "", trusted: false) }),
            ("meshUnban", { store.meshUnban(fingerprint: "ee22", node: "n-flaky") }),
            ("meshSetDefaultTrust", { store.meshSetDefaultTrust(level: "personal") }),
            ("meshSetOverrides", { store.meshSetOverrides(duty: "review", placement: MeshPlacement(strategy: "weakest-first", tokenAware: true, spread: [])) }),
            ("meshSetWan", { store.meshSetWan(transport: "tor") }),
            ("meshConnect", { store.meshConnect(address: String(repeating: "3f", count: 32)) }),
        ]
        for (name, fire) in commands {
            seed(port: 0)
            store.meshError = nil
            fire()
            let settled = await settle()
            check("\(name) settles the screen", settled)
            check("\(name) surfaces the bridge's error", store.meshError == noPortMessage,
                  "got \(store.meshError ?? "nil")")
        }

        print(failures.isEmpty
            ? "\nMESH CMD TEST OK"
            : "\nMESH CMD TEST FAILED: \(failures.joined(separator: ", "))")
        return failures.isEmpty
    }
}
