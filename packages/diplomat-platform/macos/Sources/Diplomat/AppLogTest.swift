import AppKit

/// Self-test for the diagnostic log - `DIPLOMAT_APPLOG_TEST=1`.
///
/// Writes into a scratch directory: the line format, rotation, timed work, a busy and a
/// waiting main-thread hang (the first sampled, the second not), the heartbeat, the
/// window's show and close lines, and the activity feed's mirror.
///
///     DIPLOMAT_APPLOG_TEST=1 swift run Diplomat
///
/// Needs a window server. Its windows are transparent, 2 pt or off-screen, and never key.
@MainActor
enum AppLogTest {
    static func run() async -> Bool {
        var failures: [String] = []
        func check(_ name: String, _ cond: Bool, _ detail: @autoclosure () -> String = "") {
            if cond { print("  ok    \(name)") }
            else { print("  FAIL  \(name) \(detail())"); failures.append(name) }
        }
        let fm = FileManager.default
        let scratch = fm.temporaryDirectory.appendingPathComponent("applog-test-\(getpid())")
        try? fm.removeItem(at: scratch)
        AppLog.dirOverride = scratch
        AuditLog.dirOverride = scratch
        defer { try? fm.removeItem(at: scratch) }
        func lines() -> [String] {
            ((try? String(contentsOf: AppLog.url, encoding: .utf8)) ?? "").split(separator: "\n").map(String.init)
        }
        func matches(_ s: String?, _ pattern: String) -> Bool {
            s?.range(of: pattern, options: .regularExpression) != nil
        }
        func pause(_ secs: Double) async { try? await Task.sleep(nanoseconds: UInt64(secs * 1e9)) }

        print("applog: the line")
        AppLog.log("test", "hello")
        check("a line is a local time to the millisecond, the pid and mode, the category, the message",
              matches(lines().last, #"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}([+-]\d\d:\d\d|Z) \[\#(getpid()) applog_test\] test hello$"#),
              lines().last ?? "nothing written")

        check("the live app and the unattended jobs write the file; a self-test only when redirected",
              AppLog.writesFile(mode: nil, redirected: false)
                && AppLog.writesFile(mode: "self_update", redirected: false)
                && AppLog.writesFile(mode: "watchdog", redirected: false)
                && !AppLog.writesFile(mode: "reopen_test", redirected: false)
                && AppLog.writesFile(mode: "reopen_test", redirected: true))
        AppLog.dirOverride = nil
        setenv("DIPLOMAT_LOG_DIR", "", 1)
        let emptyRedirects = AppLog.isRedirected
        setenv("DIPLOMAT_LOG_DIR", scratch.path, 1)
        let setRedirects = AppLog.isRedirected
        unsetenv("DIPLOMAT_LOG_DIR")
        AppLog.dirOverride = scratch
        check("an empty DIPLOMAT_LOG_DIR leaves the file where it is, and is not a redirect",
              !emptyRedirects && setRedirects)

        print("applog: rotation")
        let cap = AppLog.maxBytes
        AppLog.maxBytes = 4096
        for i in 0..<200 { AppLog.log("test", "filler \(i)") }
        AppLog.maxBytes = cap
        let live = lines()
        let rotated = ((try? String(contentsOfFile: AppLog.url.path + ".1", encoding: .utf8)) ?? "")
            .split(separator: "\n").map(String.init)
        let size = (try? fm.attributesOfItem(atPath: AppLog.url.path)[.size] as? Int) ?? -1
        let rotatedSize = (try? fm.attributesOfItem(atPath: AppLog.url.path + ".1")[.size] as? Int) ?? -1
        check("neither file passes its cap", size > 0 && size <= 4096 && rotatedSize > 4000 && rotatedSize <= 4096,
              "live \(size), .1 \(rotatedSize) bytes")
        check("the newest line is in the live file", live.last?.hasSuffix(" filler 199") == true)
        let firstLive = live.first.flatMap { Int($0.split(separator: " ").last ?? "") }
        let lastRotated = rotated.last.flatMap { Int($0.split(separator: " ").last ?? "") }
        check(".1 ends on the line just before the live file starts",
              firstLive != nil && lastRotated.map { $0 + 1 } == firstLive,
              "live starts \(firstLive.map(String.init) ?? "?"), .1 ends \(lastRotated.map(String.init) ?? "?")")

        print("applog: timed work")
        var during = ""
        await AppLog.timed("probe", slowAfter: 0.1) {
            during = AppLog.inFlight()
            try? await Task.sleep(nanoseconds: 200_000_000)
        }
        check("work is in flight while it runs", during.hasPrefix("probe "), during)
        check("and gone once it ends", AppLog.inFlight() == "nothing", AppLog.inFlight())
        check("slow work gets its own line", matches(lines().last, #"\] timing probe took 0\.2\ds$"#), lines().last ?? "")
        await AppLog.timed("quick", slowAfter: 10) {}
        check("quick work does not", lines().last?.contains("quick") == false, lines().last ?? "")
        let tallies = AppLog.drainTallies()
        check("both are tallied for the heartbeat", tallies["probe"]?.count == 1 && tallies["quick"]?.count == 1,
              "\(tallies)")

        print("applog: main-thread hangs")
        let watch = HangWatch.shared
        watch.threshold = 0.3
        // Wide margins: a ping can go out up to one interval (plus timer slack) into a hang.
        watch.sampleAfter = 1.5
        watch.sampleSeconds = 1
        watch.keepSamples = 3
        watch.heartbeat = 1
        try? fm.createDirectory(at: watch.sampleDir, withIntermediateDirectories: true)
        for i in 1...4 {
            fm.createFile(atPath: watch.sampleDir.appendingPathComponent("2000-01-01T00-00-0\(i)-1.txt").path,
                          contents: Data())
        }
        func sampleFiles() -> [String] {
            ((try? fm.contentsOfDirectory(atPath: watch.sampleDir.path)) ?? []).sorted()
        }
        func realSamples() -> [String] { sampleFiles().filter { !$0.hasPrefix("2000-") } }
        AppLog.startSession()
        let session = lines()
        check("a session opens with the launch line",
              session.contains { matches($0, #"\] app launch /.+ modified \d{4}-.+, macOS .+, parent .+ \(\d+\)$"#) },
              session.suffix(3).joined(separator: "\n"))
        check("and the displays", session.contains { matches($0, #"\] system displays: .+ primary"#) },
              session.suffix(3).joined(separator: "\n"))
        await pause(0.5)

        let beforeShort = lines().count
        await AppLog.timed("shortProbe") { spinMain(1.0) }
        await pause(0.5)
        let short = lines().dropFirst(beforeShort).first { $0.contains("main thread blocked") }
        check("a short hang names the work that was in flight while it was late, ended since",
              short?.contains("; in flight: shortProbe ") == true, short ?? "no line")

        let before = lines().count
        var spun = 0.0
        await AppLog.timed("busyProbe") { spun = spinMain(until: { realSamples().count == 1 }, atMost: 20) }
        await pause(0.5)
        let afterBusy = Array(lines().dropFirst(before))
        let ongoing = afterBusy.first { $0.contains("unresponsive") }
        check("a hang past sampleAfter is logged while it lasts, with the work in flight",
              matches(ongoing, #"\] hang main thread unresponsive for \d\.\d\ds so far; in flight: busyProbe "#),
              ongoing ?? "no line")
        let samplePath = ongoing?.components(separatedBy: "; sampling to ").dropFirst().first
        check("and sampled", samplePath?.hasPrefix(watch.sampleDir.path) == true, ongoing ?? "")
        let busy = afterBusy.first { $0.contains("main thread blocked") }
        let busySecs = busy.flatMap { Double($0.components(separatedBy: "blocked ").last?.prefix(4) ?? "") }
        check("its end gives the length, up to one interval short",
              busySecs.map { $0 > spun - watch.interval - 0.15 && $0 <= spun + 0.15 } == true,
              "\(busy ?? "no line"); spun \(spun)")
        check("and tells it was the main thread computing", busy?.contains("(busy on the main thread)") == true,
              busy ?? "")
        check("and names the sample", busy.map { samplePath != nil && $0.hasSuffix("; sample \(samplePath!)") } == true,
              busy ?? "")
        check("a heartbeat during it says a hang is going on",
              afterBusy.contains { matches($0, #"\] alive up .*, one going on for \d"#) },
              afterBusy.filter { $0.contains("alive") }.joined(separator: "\n"))
        var stacks = ""
        for _ in 0..<60 where !stacks.contains("Call graph") {
            stacks = samplePath.flatMap { try? String(contentsOfFile: $0, encoding: .utf8) } ?? ""
            if !stacks.contains("Call graph") { await pause(0.5) }
        }
        check("the sample holds the main thread's stack in the hang", stacks.contains("spinMain"),
              "\(stacks.count) bytes")
        check("only the newest keepSamples samples are kept",
              sampleFiles().count == 3 && sampleFiles().prefix(2) == ["2000-01-01T00-00-03-1.txt", "2000-01-01T00-00-04-1.txt"],
              "\(sampleFiles())")

        let beforeWait = lines().count
        await AppLog.timed("waitProbe") { blockMain(3.0) }
        await pause(0.5)
        let afterWait = Array(lines().dropFirst(beforeWait))
        let waiting = afterWait.first { $0.contains("main thread blocked") }
        check("a hang spent waiting is told apart", waiting?.contains("(waiting, not computing)") == true,
              waiting ?? "no line")
        let stillWaiting = afterWait.first { $0.contains("unresponsive") }
        check("one sample per sampleEvery", stillWaiting?.contains("in flight: waitProbe ") == true
              && stillWaiting?.contains("sampling") == false && waiting?.contains("sample") == false
              && realSamples().count == 1,
              "\(stillWaiting ?? "no line"); \(sampleFiles())")

        await pause(1.3)
        let beats = lines().filter { $0.contains("] alive up ") }
        let hangCount = beats.compactMap { line -> Int? in
            line.range(of: #"hangs \d+"#, options: .regularExpression).flatMap { Int(line[$0].dropFirst(6)) }
        }.reduce(0, +)
        check("heartbeats count all three hangs", hangCount == 3, beats.joined(separator: "\n"))
        check("and tally the timed work", beats.contains { matches($0, #"busyProbe 1x avg \d+\.\d+s"#) },
              beats.joined(separator: "\n"))

        print("applog: window")
        WindowLog.settle = 0.2
        func lastShow() -> String? { lines().last { $0.contains("] window show") } }
        func lastClose() -> String? { lines().last { $0.contains("] window close") } }
        func testWindow(_ cls: NSWindow.Type, _ rect: NSRect) -> NSWindow {
            let w = cls.init(contentRect: rect, styleMask: [.borderless], backing: .buffered, defer: false)
            w.isReleasedWhenClosed = false
            WindowLog.track(w)
            return w
        }
        let wedged = testWindow(WedgedWindow.self, NSRect(x: -4000, y: -4000, width: 10, height: 10))
        wedged.order(.above, relativeTo: 0)
        WindowLog.shown(wedged)
        await pause(0.4)
        check("a show that never reaches the screen is FAILED, with the window's state",
              matches(lastShow(), #"\] window show FAILED \(still invisible right after ordering in\) visible=0 occlusion=hidden alpha=\d\.\d\d key=0 activeSpace=\d level=0 behavior=0x[0-9a-f]+ frame=-4000,-4000 10x10 screen=none app active=\d hidden=0$"#),
              lastShow() ?? "no line")
        wedged.close()
        check("its close says it never showed", lastClose()?.hasSuffix("] window close (was visible=0)") == true,
              lastClose() ?? "no line")

        let offscreen = testWindow(NSWindow.self, NSRect(x: -4000, y: -4000, width: 2, height: 2))
        offscreen.order(.above, relativeTo: 0)
        WindowLog.shown(offscreen)
        await pause(0.4)
        check("so is one ordered in where nobody can see it",
              matches(lastShow(), #"\] window show FAILED visible=1 occlusion=hidden "#), lastShow() ?? "no line")
        offscreen.close()

        // 2 pt in the corner, never key and click-through.
        let probe = testWindow(NSWindow.self, NSRect(x: 0, y: 0, width: 2, height: 2))
        probe.ignoresMouseEvents = true
        probe.level = .popUpMenu
        // A locked screen covers every window, so nothing can be seen to land.
        let locked = (CGSessionCopyCurrentDictionary() as? [String: Any])?["CGSSessionScreenIsLocked"] as? Bool == true
        func onScreen(_ name: String, _ cond: Bool, _ detail: String) {
            if locked { print("  skip  \(name) (the screen is locked)") } else { check(name, cond, detail) }
        }
        func showProbe(alpha: CGFloat) {
            probe.alphaValue = alpha
            probe.order(.above, relativeTo: 0)
            WindowLog.shown(probe)
        }
        showProbe(alpha: 0)
        await pause(0.4)
        onScreen("and one left transparent",
                 matches(lastShow(), #"\] window show FAILED visible=1 occlusion=visible alpha=0\.00 "#), lastShow() ?? "no line")
        probe.close()
        await pause(0.1)

        showProbe(alpha: 0.6)
        await pause(0.4)
        onScreen("a show that lands is ok",
                 matches(lastShow(), #"\] window show ok visible=1 occlusion=visible alpha=0\.60 "#), lastShow() ?? "no line")
        probe.close()
        check("and its close says it showed", lastClose()?.hasSuffix("] window close (was visible=1)") == true,
              lastClose() ?? "no line")

        showProbe(alpha: 0.6)
        await pause(0.05)
        probe.close()
        await pause(0.4)
        check("one closed before it settled is not judged",
              lastShow()?.hasSuffix("] window show, closed again within 0.2s") == true, lastShow() ?? "no line")

        let closesBefore = lines().filter { $0.contains("] window close") }.count
        AppLog.exiting = true
        offscreen.order(.above, relativeTo: 0)
        offscreen.close()
        AppLog.exiting = false
        check("a close while the app quits is not logged",
              lines().filter { $0.contains("] window close") }.count == closesBefore)

        print("applog: the activity feed")
        AuditLog.log("panel", "probe", "a detail")
        let activity = lines().last { $0.contains("] activity ") }
        check("an activity line is mirrored", activity?.hasSuffix("] activity panel probe: a detail") == true,
              activity ?? "no line")

        print(failures.isEmpty ? "applog: all passed" : "applog: \(failures.count) FAILED: \(failures)")
        return failures.isEmpty
    }

    /// Keeps the main thread computing for `secs`; the name is what the sample must show.
    static func spinMain(_ secs: Double) {
        let end = AppLog.uptime + secs
        var x = 0.0
        while AppLog.uptime < end { x += sin(x) }
        if x.isNaN { print(x) }
    }

    /// Keeps the main thread computing until `done` (asked every 50 ms) or `atMost`
    /// seconds, and returns how long it ran.
    static func spinMain(until done: () -> Bool, atMost: Double) -> Double {
        let start = AppLog.uptime
        while AppLog.uptime - start < atMost {
            spinMain(0.05)
            if done() { break }
        }
        return AppLog.uptime - start
    }

    /// Keeps the main thread blocked without computing.
    static func blockMain(_ secs: Double) { usleep(UInt32(secs * 1e6)) }

    /// Drops every order-in, as the wedged popover window of 2026-11-04 did: AppKit took
    /// the call and the window never reached the screen.
    final class WedgedWindow: NSWindow {
        override func order(_ place: NSWindow.OrderingMode, relativeTo otherWin: Int) {
            if place == .out { super.order(place, relativeTo: otherWin) }
        }
    }
}
