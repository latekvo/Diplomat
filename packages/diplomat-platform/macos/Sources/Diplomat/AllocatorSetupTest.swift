import Foundation
import SwiftUI

/// Self-test for the launch-time allocator decision — `DIPLOMAT_ALLOCATOR_TEST=1`.
///
/// The device allocator is not a one-time install: everything `--install` writes is a
/// copy of something in this checkout (the skill, the always-on rule, the CLAUDE.md
/// coercion block, the MCP registration), so a `git pull` moves the originals and
/// leaves the copies behind. Refreshing them on launch is what `needsInstall` is for
/// — and the reason it needs a test is the case next door: a user who removed the
/// allocator in Settings must not find it back after a restart.
///
///     DIPLOMAT_ALLOCATOR_TEST=1 swift run Diplomat
///
/// Pure decision logic: no installer is shelled, no `~/.claude*` is read, and the only
/// file written is a scratch `DIPLOMAT_CONFIG`. The second half decodes real installer
/// payloads, because the decision is only as good as the fields it reads — a
/// silently-dropped `outdated` would leave every machine stale while this table still
/// passed. Then the daemon's idle limits are read back the way Settings shows them, and
/// the Devices screen's inputs decoded: snapshots with no, a full, and a garbled remote
/// pool, and the daemon's replies to the remote-pool calls. No daemon is called.
///
/// The decision half is the twin of `diplomat-platform/linux/tests/test_allocator_update.py`. Exit code is pass/fail, so CI can
/// gate on it.
enum AllocatorSetupTest {
    static func run() -> Bool {
        var failures: [String] = []
        func check(_ name: String, _ cond: Bool, _ detail: String = "") {
            if cond { print("  ok    \(name)") }
            else { print("  FAIL  \(name) \(detail)"); failures.append(name) }
        }

        func status(installed: Bool, outdated: Bool = false) -> AllocatorInstall {
            var s = AllocatorInstall()
            s.installed = installed
            s.outdated = outdated
            s.drift = outdated ? ["skill"] : []
            return s
        }

        print("allocator: the launch-time install decision")

        // (case, status, setupDone, expected)
        let cases: [(String, AllocatorInstall, Bool, Bool)] = [
            ("first run: nothing installed, nothing settled",
             status(installed: false), false, true),
            ("settled uninstall: the user took it off in Settings",
             status(installed: false), true, false),
            ("steady state: installed and current",
             status(installed: true), true, false),
            ("installed by something else, and current — adopt it rather than reinstall",
             status(installed: true), false, false),
            ("stale: the checkout moved on from what is deployed",
             status(installed: true, outdated: true), true, true),
            ("stale on a machine that has not settled yet",
             status(installed: true, outdated: true), false, true),
        ]
        for (name, s, setupDone, expected) in cases {
            let got = DeviceAllocator.needsInstall(status: s, setupDone: setupDone)
            check(name, got == expected, "got \(got), expected \(expected)")
        }

        // A check that could not run at all (`.unknown`: node missing, or output that
        // would not parse) is a first run until the setup settles. Retrying is right —
        // that is how a machine with no node yet gets set up once one arrives — but
        // only until then, or an uninstalled machine with a broken check would be
        // reinstalled on every single launch.
        check("an unknown status retries before setup settles",
              DeviceAllocator.needsInstall(status: .unknown, setupDone: false))
        check("an unknown status is left alone once setup is settled",
              !DeviceAllocator.needsInstall(status: .unknown, setupDone: true))

        // ---- the fields the decision reads, decoded from real installer output ----

        func decode(_ json: String, _ name: String) -> AllocatorInstall? {
            guard let data = json.data(using: .utf8),
                  let parsed = try? JSONDecoder().decode(AllocatorInstall.self, from: data)
            else { check("decodes \(name)", false, "decode threw"); return nil }
            return parsed
        }

        if let stale = decode("""
        {"mcpRegistered":true,"skillInstalled":true,"ruleInstalled":true,
         "claudeMdInjected":true,"daemonRunning":true,"installed":true,
         "version":"0.1.0","outdated":true,"drift":["skill","mcp"]}
        """, "a stale --check") {
            check("a stale check decodes as outdated", stale.outdated)
            check("a stale check carries what drifted", stale.drift == ["skill", "mcp"],
                  "got \(stale.drift)")
            check("a stale check carries the version", stale.version == "0.1.0",
                  "got \(stale.version ?? "nil")")
            check("a stale check asks to be reinstalled",
                  DeviceAllocator.needsInstall(status: stale, setupDone: true))
        }

        // An installer that predates drift detection: the .app can outrun its
        // checkout (a copy kept elsewhere while the source moves). Reading
        // "current" as a positive flag would make that machine reinstall on every
        // launch forever; deriving it from `installed && !outdated` makes the old
        // installer's silence mean "installed, nothing known to be wrong".
        if let old = decode("""
        {"mcpRegistered":true,"skillInstalled":true,"ruleInstalled":true,
         "claudeMdInjected":true,"daemonRunning":true,"installed":true}
        """, "an installer too old to report drift") {
            check("an old installer's silence is not drift", !old.outdated)
            check("an old installer's version is absent, not a lie", old.version == nil)
            check("an old install is left alone",
                  !DeviceAllocator.needsInstall(status: old, setupDone: true))
        }

        // ---- the daemon's idle limits, as Settings reads them from the shared file ----

        let scratch = FileManager.default.temporaryDirectory
            .appendingPathComponent("diplomat-allocator-test-\(UUID().uuidString)")
        let previousConfig = ProcessInfo.processInfo.environment["DIPLOMAT_CONFIG"]
        setenv("DIPLOMAT_CONFIG", scratch.appendingPathComponent("config.json").path, 1)
        defer {
            if let previousConfig { setenv("DIPLOMAT_CONFIG", previousConfig, 1) }
            else { unsetenv("DIPLOMAT_CONFIG") }
            try? FileManager.default.removeItem(at: scratch)
        }
        /// Both limits read back from a file holding `free` and `leased` as raw JSON
        /// values. The keys are spelled out, so they pin what the daemon reads too.
        func idleLimits(_ free: String?, _ leased: String?) -> (Int, Int) {
            var fields: [String] = []
            if let free { fields.append(#""deviceIdleFreeMinutes":\#(free)"#) }
            if let leased { fields.append(#""deviceIdleLeasedMinutes":\#(leased)"#) }
            let json = "{" + fields.joined(separator: ",") + "}"
            try? FileManager.default.createDirectory(at: scratch,
                                                     withIntermediateDirectories: true)
            try? json.write(to: AppConfig.url, atomically: true, encoding: .utf8)
            return (AppConfig.deviceIdleFreeMinutes, AppConfig.deviceIdleLeasedMinutes)
        }

        // (case, free in the file, leased in the file, expected free, expected leased)
        let idleCases: [(String, String?, String?, Int, Int)] = [
            ("absent keys read as the defaults", nil, nil, 5, 30),
            ("0 is never, not absent", "0", "0", 0, 0),
            ("in-range values are kept", "12", "45", 12, 45),
            ("a hand-edited bool is not 1 minute", "true", "false", 5, 30),
            ("a string is not a number", #""10""#, #""60""#, 5, 30),
            ("a negative limit is the default, not never", "-1", "-30", 5, 30),
            ("past the slider's range clamps to its end", "61", "999", 60, 240),
        ]
        for (name, free, leased, wantFree, wantLeased) in idleCases {
            let got = idleLimits(free, leased)
            check("idle limits: \(name)", got == (wantFree, wantLeased),
                  "got \(got), expected (\(wantFree), \(wantLeased))")
        }

        // ---- the daemon's snapshot, with and without a remote pool ----

        func snapshot(_ json: String) -> DeviceState? {
            json.data(using: .utf8).flatMap { try? JSONDecoder().decode(DeviceState.self, from: $0) }
        }
        let row = #"{"key":"ios:A","platform":"ios","name":"iPhone","status":"free"}"#

        let plain = snapshot(#"{"updatedAt":"x","daemonPid":812,"devices":[\#(row)]}"#)
        check("a snapshot without a remote pool decodes", plain?.devices.count == 1)
        check("no remote key reads as no remote pool", plain != nil && plain?.remote == nil)
        check("a row without a source is local", plain?.devices.first?.isRemote == false)
        check("the daemon's pid is read", plain?.daemonPid == 812, "got \(String(describing: plain?.daemonPid))")

        let signingIn = snapshot(#"""
        {"devices":[\#(row),
          {"key":"p:7","platform":"android","source":"remote","handle":"127.0.0.1:41207",
           "status":"booting","owner":{"agentName":"e2e","ownerPid":42}}],
         "remote":{"provider":"lab-pool","state":"signing-in","loginUrl":"https://h/login",
                   "zone":"b","count":3,"retryAt":"2026-10-27T18:05:00.000Z"}}
        """#)
        let r = signingIn?.remote
        check("a remote status decodes", r?.provider == "lab-pool" && r?.state == "signing-in")
        check("loginUrl is read", r?.loginUrl == "https://h/login")
        check("an ISO retryAt with milliseconds is read",
              r?.retryAt == Date(timeIntervalSince1970: 1_793_124_300), "got \(String(describing: r?.retryAt))")
        check("unknown keys are kept in key order, known ones are not repeated",
              r?.extras == [.init(key: "count", value: "3"), .init(key: "zone", value: "b")],
              "got \(String(describing: r?.extras))")
        check("a remote row is a remote lease", signingIn?.devices.last?.isRemote == true)

        let garbage = snapshot(#"{"daemonPid":"x","devices":[\#(row),{"key":"b","platform":"ios","status":7}],"remote":"broken"}"#)
        check("an unreadable row costs only itself", garbage?.devices.map(\.key) == ["ios:A"],
              "got \(String(describing: garbage?.devices.map(\.key)))")
        check("an unreadable daemonPid is dropped, not fatal", garbage != nil && garbage?.daemonPid == nil)
        check("a non-object remote is an unknown state, not an absent pool",
              garbage?.remote?.state == "unknown"
                && garbage?.remote?.extras == [.init(key: "remote", value: "broken")])
        check("a null remote is no pool", snapshot(#"{"devices":[],"remote":null}"#)?.remote == nil)

        let odd = snapshot(#"{"devices":[],"remote":{"state":5,"provider":7,"retryAt":"soon","reason":null}}"#)?.remote
        check("a non-string state reads as unknown", odd?.state == "unknown")
        check("a non-string provider is no name", odd != nil && odd?.provider == nil)
        check("an unparseable retryAt is shown as it says",
              odd?.retryAt == nil && odd?.extras == [.init(key: "retryAt", value: "soon")],
              "got \(String(describing: odd?.extras))")
        check("a null reason is no reason", odd != nil && odd?.reason == nil)
        check("an epoch-millisecond retryAt is read",
              snapshot(#"{"devices":[],"remote":{"state":"x","retryAt":1793124300000}}"#)?.remote?.retryAt
                == Date(timeIntervalSince1970: 1_793_124_300))

        // ---- the daemon's replies to the remote-pool calls ----

        func reply(_ body: String, _ code: Int) -> DaemonReply? {
            DeviceAllocator.parseReply(Data("\(body)\n\(code)".utf8))
        }
        check("a reply carries its status and body",
              reply(#"{"ok":true}"#, 200) == DaemonReply(status: 200, json: .object(["ok": .bool(true)])))
        check("output without a status line is no reply", DeviceAllocator.parseReply(Data("{}".utf8)) == nil)
        check("no answer is said so",
              DeviceAllocator.signInReply(nil) == .reason("the allocator daemon did not answer"))
        check("a daemon without the route says it has no remote pool",
              DeviceAllocator.devicesReply(reply(#"{"error":"unknown route /remote/devices"}"#, 404))
                == .reason("this allocator has no remote device pool — update it"))
        check("a signed-in reply is ok", DeviceAllocator.signInReply(reply(#"{"ok":true}"#, 200)) == .ok)
        check("a refused sign-in carries its reason",
              DeviceAllocator.signInReply(reply(#"{"ok":false,"reason":"timed out"}"#, 200)) == .reason("timed out"))
        check("a daemon error is a reason, not a success",
              DeviceAllocator.signInReply(reply(#"{"error":"boom"}"#, 500)) == .reason("boom"))
        check("a pool listing keeps object rows and stringifies numbers",
              DeviceAllocator.devicesReply(reply(#"{"devices":[{"key":"k","name":"S24","version":14,"state":"ready"},"junk"]}"#, 200))
                == .devices([RemotePoolDevice(id: "k", platform: nil, name: "S24", version: "14",
                                              apiVersion: nil, format: nil, state: "ready")]))
        check("a pool that cannot list says why",
              DeviceAllocator.devicesReply(reply(#"{"reason":"not signed in"}"#, 200)) == .reason("not signed in"))
        check("a listing that is not a list is a reason",
              DeviceAllocator.devicesReply(reply(#"{"devices":"x"}"#, 200)) == .reason("the pool sent no device list"))

        // ---- idle time, coloured toward the leased limit ----

        check("idle colour: red a minute before the limit", DeviceAllocation.idleColor(29, limit: 30) == .red)
        check("idle colour: orange from two thirds", DeviceAllocation.idleColor(20, limit: 30) == .orange)
        check("idle colour: quiet below that", DeviceAllocation.idleColor(14, limit: 30) == .secondary)
        check("idle colour: never with no limit", DeviceAllocation.idleColor(500, limit: 0) == .secondary)

        print(failures.isEmpty
            ? "\nALLOCATOR TEST OK"
            : "\nALLOCATOR TEST FAILED: \(failures.joined(separator: ", "))")
        return failures.isEmpty
    }
}
