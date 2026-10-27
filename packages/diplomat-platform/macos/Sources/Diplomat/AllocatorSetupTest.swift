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
/// The decision half is pure logic, and no `~/.claude*` is read. The second half decodes
/// real installer payloads, because the decision is only as good as the fields it reads —
/// a silently-dropped `outdated` would leave every machine stale while this table still
/// passed. Then the daemon's idle limits are read back the way Settings shows them, and
/// the Devices screen's inputs decoded: snapshots with no, a full, and a garbled remote
/// pool, and the daemon's replies to the remote-pool calls. Last, the Devices screen's
/// actions run through a real `Store` against a stand-in `install.js` and a daemon
/// directory with no daemon in it, all in a scratch dir with the feed and config.
///
/// The decision half is the twin of `diplomat-platform/linux/tests/test_allocator_update.py`. Exit code is pass/fail, so CI can
/// gate on it.
enum AllocatorSetupTest {
    @MainActor
    static func run() async -> Bool {
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
                == .reason("this allocator has no remote device pool — update it, then restart its daemon"))
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

        // ---- the Devices screen's actions, through a Store and a stand-in installer ----

        print("allocator: the Devices screen's actions")
        let fm = FileManager.default
        let pkg = scratch.appendingPathComponent("pkg")
        let calls = pkg.appendingPathComponent("calls.log")
        let replies = pkg.appendingPathComponent("replies.json")
        try? fm.createDirectory(at: pkg.appendingPathComponent("src"), withIntermediateDirectories: true)
        // Prints the reply set for its argument, after the delay set for it, and records
        // that it was asked.
        try? #"""
        const fs = require('fs'), path = require('path');
        const arg = process.argv[2];
        fs.appendFileSync(path.join(__dirname, '..', 'calls.log'), arg + '\n');
        const replies = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'replies.json'), 'utf8'));
        setTimeout(() => process.stdout.write(JSON.stringify(replies[arg] || {})),
                   (replies.delayMs || {})[arg] || 0);
        """#.write(to: pkg.appendingPathComponent("src/install.js"), atomically: true, encoding: .utf8)
        func answer(_ byArg: [String: String], delayMs: [String: Int] = [:]) {
            var fields = byArg.map { "\"\($0.key)\":\($0.value)" }
            let delays = delayMs.map { "\"\($0.key)\":\($0.value)" }.joined(separator: ",")
            fields.append("\"delayMs\":{\(delays)}")
            try? "{\(fields.joined(separator: ","))}".write(to: replies, atomically: true, encoding: .utf8)
            try? fm.removeItem(at: calls)
        }
        func asked() -> [String] {
            ((try? String(contentsOf: calls, encoding: .utf8)) ?? "").split(separator: "\n").map(String.init)
        }
        let parts = #""mcpRegistered":true,"skillInstalled":true,"ruleInstalled":true,"claudeMdInjected":true,"version":"0.1.0","drift":[],"outdated":false"#
        // What `install.js --start-daemon` prints: no `installed`.
        let startReply = #"{"action":"start-daemon",\#(parts),"daemonRunning":true}"#
        let checkReply = #"{"action":"check",\#(parts),"daemonRunning":true,"installed":true}"#
        for (name, value) in [
            ("DIPLOMAT_DEVICE_ALLOCATOR_DIR", pkg.path),
            ("DA_BASE_DIR", scratch.appendingPathComponent("da").path),
            ("DIPLOMAT_AUDIT_DIR", scratch.appendingPathComponent("feed").path),
            ("DIPLOMAT_AGENTS_DIR", scratch.appendingPathComponent("agents").path),
            ("DIPLOMAT_CLAUDE_DIR", scratch.appendingPathComponent("claude").path),
            ("DIPLOMAT_QUOTA_PROBE", "0"),
        ] { setenv(name, value, 1) }
        try? #"{"deviceRemoteProvider":"~/operator/own-pool.mjs"}"#
            .write(to: AppConfig.url, atomically: true, encoding: .utf8)
        let store = Store()
        func feed() -> [String] {
            AuditLog.read().filter { DevicesScreen.eventActions.contains($0.action) }.map(\.detail)
        }
        func settle() async { try? await Task.sleep(nanoseconds: 300_000_000) }

        let start = decode(startReply, "a --start-daemon reply")
        check("a --start-daemon reply does not report the install", start?.reportsInstalled == false)
        if DeviceAllocator.nodeAvailable {
            answer(["--start-daemon": startReply])
            store.allocatorInstall = decode(checkReply, "a --check reply")
            await store.startAllocatorDaemon()
            check("Start daemon keeps an installed allocator installed",
                  store.allocatorInstall?.installed == true && store.allocatorInstall?.daemonRunning == true,
                  "got \(String(describing: store.allocatorInstall))")
            check("…and asked the installer to start it", asked() == ["--start-daemon"], "got \(asked())")

            answer(["--restart-daemon": #"{"action":"restart-daemon",\#(parts),"daemonRunning":true,"installed":true}"#])
            await store.restartAllocatorDaemon()
            await settle()
            check("Restart daemon asks the installer for a restart", asked() == ["--restart-daemon"],
                  "got \(asked())")
            check("…and the Recent card says it worked",
                  feed().first == "Restarted the device-allocator daemon (ok: true)", "got \(feed())")
            answer(["--restart-daemon": #"{"action":"restart-daemon","error":"the running daemon (pid 9) did not exit within 60 s",\#(parts),"daemonRunning":false,"installed":true}"#])
            await store.restartAllocatorDaemon()
            await settle()
            check("a restart whose old daemon never left says so on the Recent card",
                  feed().first == "Restarted the device-allocator daemon (ok: false): the running "
                    + "daemon (pid 9) did not exit within 60 s", "got \(feed())")

            // Mid-restart no daemon answers: a --check then reads "not running", which
            // would offer a Start daemon that leaves a second daemon behind.
            let running = #"{"action":"restart-daemon",\#(parts),"daemonRunning":true,"installed":true}"#
            let notRunning = #"{"action":"check",\#(parts),"daemonRunning":false,"installed":true}"#
            answer(["--restart-daemon": running, "--check": notRunning, "--start-daemon": startReply],
                   delayMs: ["--restart-daemon": 1500])
            let restart = Task { await store.restartAllocatorDaemon() }
            try? await Task.sleep(nanoseconds: 400_000_000)
            check("a restart in flight marks the allocator busy", store.allocatorBusy)
            await store.refreshAllocatorInstall()
            await store.startAllocatorDaemon()
            check("…which holds the poll and refuses a start", asked() == ["--restart-daemon"],
                  "got \(asked())")
            await restart.value
            check("…until it is over", !store.allocatorBusy
                    && store.allocatorInstall?.daemonRunning == true,
                  "got busy \(store.allocatorBusy), \(String(describing: store.allocatorInstall))")

            answer(["--restart-daemon": running, "--check": notRunning], delayMs: ["--check": 1500])
            let poll = Task { await store.refreshAllocatorInstall() }
            try? await Task.sleep(nanoseconds: 400_000_000)
            await store.restartAllocatorDaemon()
            await poll.value
            check("a --check that a restart overlapped is dropped",
                  asked().sorted() == ["--check", "--restart-daemon"]
                    && store.allocatorInstall?.daemonRunning == true,
                  "asked \(asked()), got \(String(describing: store.allocatorInstall))")
        } else {
            print("  skip  the installer calls: no node on this machine")
        }

        // No daemon answers in the scratch DA_BASE_DIR, so Repaired fails.
        await store.markDeviceRepaired("appletv:ATV1")
        await settle()
        check("a failed Repaired shows on the Recent card",
              feed().first == "Repaired FAILED for device appletv:ATV1", "got \(feed())")

        // ---- the provider, which config.json holds and is also set by hand ----

        check("the provider is read from config.json", store.deviceRemoteProvider == "~/operator/own-pool.mjs")
        try? #"{"deviceRemoteProvider":"~/by/hand.mjs"}"#.write(to: AppConfig.url, atomically: true, encoding: .utf8)
        store.reloadDeviceConfig()
        check("a provider set by hand is picked up on reload", store.deviceRemoteProvider == "~/by/hand.mjs")
        check("a write that lands says so", AppConfig.set("probeKey", "x") && AppConfig.string("probeKey") == "x")
        let blocker = scratch.appendingPathComponent("not-a-dir")
        try? "".write(to: blocker, atomically: true, encoding: .utf8)
        setenv("DIPLOMAT_CONFIG", blocker.appendingPathComponent("config.json").path, 1)
        check("a write that cannot land says so", !AppConfig.set(AppConfig.deviceRemoteProviderKey, "~/x.mjs"))
        setenv("DIPLOMAT_CONFIG", scratch.appendingPathComponent("config.json").path, 1)

        // ---- the idle limits, which config.json holds too ----

        store.writesDeviceConfig = true
        try? #"{"deviceIdleLeasedMinutes":90,"deviceIdleFreeMinutes":7}"#
            .write(to: AppConfig.url, atomically: true, encoding: .utf8)
        store.reloadDeviceConfig()
        check("idle limits set by hand are picked up on reload",
              store.deviceIdleLeasedMinutes == 90 && store.deviceIdleFreeMinutes == 7,
              "got \(store.deviceIdleLeasedMinutes), \(store.deviceIdleFreeMinutes)")
        try? #"{"deviceIdleLeasedMinutes":999}"#.write(to: AppConfig.url, atomically: true, encoding: .utf8)
        store.reloadDeviceConfig()
        check("…read as the daemon reads them, and not written back",
              store.deviceIdleLeasedMinutes == 240
                && (try? String(contentsOf: AppConfig.url, encoding: .utf8)) == #"{"deviceIdleLeasedMinutes":999}"#)
        setenv("DIPLOMAT_CONFIG", blocker.appendingPathComponent("config.json").path, 1)
        store.deviceIdleLeasedMinutes = 60
        check("an idle limit that cannot be saved says so", store.deviceIdleSaveFailed)
        setenv("DIPLOMAT_CONFIG", scratch.appendingPathComponent("config.json").path, 1)
        store.deviceIdleLeasedMinutes = 45
        check("…until a change lands", !store.deviceIdleSaveFailed && AppConfig.deviceIdleLeasedMinutes == 45)
        store.writesDeviceConfig = false

        // ---- the remote card over a snapshot from a daemon with no remote pool ----

        var stopped = AllocatorInstall()
        stopped.installed = true
        var answering = stopped
        answering.daemonRunning = true
        let whenStopped = AllocatorUI.predatesPool(stopped)
        check("a stopped daemon is offered a start, not a restart",
              whenStopped.action == .start && !whenStopped.note.contains("running allocator"),
              "got \(whenStopped)")
        check("a daemon that answers is offered a restart",
              AllocatorUI.predatesPool(answering) == (AllocatorUI.predatesPoolNote, .restart))
        check("before the first check, neither",
              AllocatorUI.predatesPool(nil).action == nil
                && !AllocatorUI.predatesPool(nil).note.contains("running"))

        // A render seeds a real Store, which read the operator's own config.json.
        for mode in ["devices-screen", "devices-screen-off", "devices-screen-remote", "devices-screen-remote-away"] {
            store.setDeviceRemoteProvider("~/operator/own-pool.mjs")
            Render.seedDevicesScreen(mode, store: store)
            check("the \(mode) render shows no provider of the operator's",
                  !store.deviceRemoteProvider.contains("operator"), "got \(store.deviceRemoteProvider)")
        }

        // ---- the --check poll runs only while the screen's window is on screen ----

        if DeviceAllocator.nodeAvailable, let screen = NSScreen.main {
            answer(["--check": checkReply])
            let view = DevicesScreen(isPresented: .constant(true), live: true, pollNanoseconds: 150_000_000)
                .environmentObject(store)
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 400, height: 300),
                                  styleMask: .borderless, backing: .buffered, defer: false)
            window.contentView = NSHostingView(rootView: view)
            // Two points of it on screen: enough to count as visible, too little to notice.
            window.setFrameOrigin(NSPoint(x: screen.frame.maxX - 2, y: screen.frame.minY))
            window.isReleasedWhenClosed = false
            func polls(within secs: Double) async -> Int {
                let before = asked().count
                try? await Task.sleep(nanoseconds: UInt64(secs * 1e9))
                return asked().count - before
            }
            window.orderFrontRegardless()
            let shown = await polls(within: 1.5)
            if shown == 0 && !window.occlusionState.contains(.visible) {
                print("  skip  the poll's visibility: no window is ever visible here")
            } else {
                check("the screen polls --check while its window is up", shown >= 2, "polled \(shown)")
                window.orderOut(nil)
                _ = await polls(within: 0.5)
                let hidden = await polls(within: 1.0)
                check("…and not once it is hidden", hidden == 0, "polled \(hidden) times")
                window.orderFrontRegardless()
                check("…and again once it is back", await polls(within: 1.0) >= 1)
            }
            window.orderOut(nil)
            window.contentView = nil
        }

        print(failures.isEmpty
            ? "\nALLOCATOR TEST OK"
            : "\nALLOCATOR TEST FAILED: \(failures.joined(separator: ", "))")
        return failures.isEmpty
    }
}
