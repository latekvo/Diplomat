import Foundation

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
/// Pure decision logic: no installer is shelled, no `~/.claude*` is read, nothing is
/// written. The second half decodes real installer payloads, because the decision is
/// only as good as the fields it reads — a silently-dropped `outdated` would leave
/// every machine stale while this table still passed.
///
/// Twin of `linux/tests/test_allocator_update.py`. Exit code is pass/fail, so CI can
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
        // checkout (it may sit in /Applications while the source moves). Reading
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

        print(failures.isEmpty
            ? "\nALLOCATOR TEST OK"
            : "\nALLOCATOR TEST FAILED: \(failures.joined(separator: ", "))")
        return failures.isEmpty
    }
}
