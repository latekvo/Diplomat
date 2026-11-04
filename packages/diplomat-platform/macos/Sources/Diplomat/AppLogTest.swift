import AppKit

/// Self-test for the diagnostic log - `DIPLOMAT_APPLOG_TEST=1`.
///
/// Writes into a scratch directory: the line format, rotation, timed work, a busy and a
/// waiting main-thread hang (the first sampled, the second not), the heartbeat, the
/// popover's show and hide lines, and the activity feed's mirror.
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
                && !AppLog.writesFile(mode: "popover_test", redirected: false)
                && AppLog.writesFile(mode: "popover_test", redirected: true))

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
        watch.sampleAfter = 0.8
        watch.sampleSeconds = 1
        watch.heartbeat = 1
        watch.start()
        await pause(0.5)
        let before = lines().count
        await AppLog.timed("busyProbe") { spinMain(2.0) }
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
        check("its end gives the length", busySecs.map { $0 >= 1.7 && $0 < 2.6 } == true, busy ?? "no line")
        check("and tells it was the main thread computing", busy?.contains("(busy on the main thread)") == true,
              busy ?? "")
        check("and names the sample", busy.map { samplePath != nil && $0.hasSuffix("; sample \(samplePath!)") } == true,
              busy ?? "")
        var stacks = ""
        for _ in 0..<60 where !stacks.contains("Call graph") {
            await pause(0.5)
            stacks = samplePath.flatMap { try? String(contentsOfFile: $0, encoding: .utf8) } ?? ""
        }
        check("the sample holds the main thread's stack in the hang", stacks.contains("spinMain"),
              "\(stacks.count) bytes")

        let beforeWait = lines().count
        await AppLog.timed("waitProbe") { blockMain(2.0) }
        await pause(0.5)
        let afterWait = Array(lines().dropFirst(beforeWait))
        let waiting = afterWait.first { $0.contains("main thread blocked") }
        check("a hang spent waiting is told apart", waiting?.contains("(waiting, not computing)") == true,
              waiting ?? "no line")
        let stillWaiting = afterWait.first { $0.contains("unresponsive") }
        let samples = (try? fm.contentsOfDirectory(atPath: watch.sampleDir.path)) ?? []
        check("one sample per sampleEvery", stillWaiting?.contains("in flight: waitProbe ") == true
              && stillWaiting?.contains("sampling") == false && waiting?.contains("sample") == false
              && samples.count == 1,
              "\(stillWaiting ?? "no line"); \(samples)")

        await pause(1.3)
        let beats = lines().filter { $0.contains("] alive up ") }
        let hangCount = beats.compactMap { line -> Int? in
            line.range(of: #"hangs \d+"#, options: .regularExpression).flatMap { Int(line[$0].dropFirst(6)) }
        }.reduce(0, +)
        check("heartbeats count both hangs", hangCount == 2, beats.joined(separator: "\n"))
        check("and tally the timed work", beats.contains { matches($0, #"busyProbe 1x avg [12]\.\d\ds"#) },
              beats.joined(separator: "\n"))

        print("applog: popover")
        PopoverFadeOut.showSettle = 0.2
        let wedged = WedgedWindow(contentRect: NSRect(x: -4000, y: -4000, width: 10, height: 10),
                                  styleMask: [.borderless], backing: .buffered, defer: false)
        PopoverFadeOut.enable(on: wedged)
        wedged.order(.above, relativeTo: 0)
        await pause(0.4)
        let failed = lines().last { $0.contains("] popover show") }
        check("a show that never reaches the screen is FAILED, with the window's state",
              matches(failed, #"\] popover show FAILED \(still invisible right after ordering in\) visible=0 occlusion=hidden alpha=\d\.\d\d key=0 activeSpace=\d level=0 behavior=0x[0-9a-f]+ frame=-4000,-4000 10x10 screen=none app active=\d hidden=0$"#),
              failed ?? "no line")
        wedged.order(.out, relativeTo: 0)
        let wedgedHide = lines().last { $0.contains("] popover hide") }
        check("its hide says it never showed", wedgedHide?.hasSuffix("] popover hide (was visible=0)") == true,
              wedgedHide ?? "no line")

        let offscreen = ProbeWindow(contentRect: NSRect(x: -4000, y: -4000, width: 2, height: 2),
                                    styleMask: [.borderless], backing: .buffered, defer: false)
        PopoverFadeOut.enable(on: offscreen)
        offscreen.order(.above, relativeTo: 0)
        await pause(0.4)
        let unseen = lines().last { $0.contains("] popover show") }
        check("so is one ordered in where nobody can see it",
              matches(unseen, #"\] popover show FAILED visible=1 occlusion=hidden "#), unseen ?? "no line")
        offscreen.order(.out, relativeTo: 0)

        let probe = ProbeWindow(contentRect: NSRect(x: 0, y: 0, width: 2, height: 2),
                                styleMask: [.borderless], backing: .buffered, defer: false)
        probe.alphaValue = 0
        probe.ignoresMouseEvents = true
        PopoverFadeOut.enable(on: probe)
        probe.order(.above, relativeTo: 0)
        await pause(0.4)
        let shown = lines().last { $0.contains("] popover show") }
        check("a show that lands is ok", matches(shown, #"\] popover show ok visible=1 occlusion=visible "#),
              shown ?? "no line")
        probe.order(.out, relativeTo: 0)
        await pause(0.1)
        let probeHide = lines().last { $0.contains("] popover hide") }
        check("and its hide says it showed", probeHide?.hasSuffix("] popover hide (was visible=1)") == true,
              probeHide ?? "no line")

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

    /// Keeps the main thread blocked without computing.
    static func blockMain(_ secs: Double) { usleep(UInt32(secs * 1e6)) }

    /// Drops every order-in, as the wedged popover window did: AppKit took the call and
    /// the window never reached the screen. `dynamic`, so the test's own call goes through
    /// the Objective-C method table as AppKit's does, where the swizzle sits.
    final class WedgedWindow: NSWindow {
        override dynamic func order(_ place: NSWindow.OrderingMode, relativeTo otherWin: Int) {
            if place == .out { super.order(place, relativeTo: otherWin) }
        }
    }

    final class ProbeWindow: NSWindow {}
}
