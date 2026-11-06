import AppKit

/// Self-test for the app window - `DIPLOMAT_WINDOW_TEST=1`.
///
/// What a launch decides before anything is on screen: an unattended start is read off
/// the environment and removed from it, and the window is a regular one that a close
/// keeps for the next open; and an older login agent gets the unattended mark, on a
/// scratch plist. Orders no window in, so it takes no focus; showing,
/// reopening and focus are checked by `scripts/macos-vm-test.sh` in a VM.
///
///     DIPLOMAT_WINDOW_TEST=1 swift run Diplomat
@MainActor
enum WindowTest {
    static func run() -> Bool {
        var failures: [String] = []
        func check(_ name: String, _ cond: Bool, _ detail: String = "") {
            if cond { print("  ok    \(name)") }
            else { print("  FAIL  \(name) \(detail)"); failures.append(name) }
        }

        print("launch mode: who opens the window")
        setenv(LaunchMode.backgroundVar, "1", 1)
        LaunchMode.adopt(from: ProcessInfo.processInfo.environment)
        check("DIPLOMAT_BACKGROUND=1 is an unattended start", LaunchMode.background)
        check("…and is gone from the environment, so nothing started from here inherits it",
              getenv(LaunchMode.backgroundVar) == nil)
        for value in ["0", "true", ""] {
            LaunchMode.adopt(from: [LaunchMode.backgroundVar: value])
            check("DIPLOMAT_BACKGROUND=\"\(value)\" is not", !LaunchMode.background)
        }
        LaunchMode.adopt(from: [:])
        check("no variable is an attended start", !LaunchMode.background)

        print("window: a regular one, kept across a close")
        let main = MainWindow(store: Store())
        let w = main.window
        let mask = w.styleMask
        check("titled, closable, miniaturizable and resizable",
              mask.isSuperset(of: [.titled, .closable, .miniaturizable, .resizable]), "\(mask)")
        check("not a panel", !(w is NSPanel))
        check("titled Diplomat", w.title == "Diplomat", "got \(w.title)")
        check("not up until shown", !w.isVisible)
        check("an ordinary window level, not floating", w.level == .normal, "\(w.level)")
        if let screen = NSScreen.main?.visibleFrame {
            check("the first frame fits the screen",
                  w.frame.width <= screen.width && w.frame.height <= screen.height,
                  "\(w.frame.size) on \(screen.size)")
            check("…and shows the whole panel where the screen has room",
                  screen.width < MainRoot.fullWidth
                      || w.contentRect(forFrameRect: w.frame).width >= MainRoot.fullWidth,
                  "\(w.frame.size)")
        }
        check("a narrower window is allowed, the panel scrolling sideways",
              w.contentMinSize.width < MainRoot.width && w.contentMinSize.width > 0,
              "min \(w.contentMinSize.width)")
        check("a close keeps it for the next open", !w.isReleasedWhenClosed)
        w.close()
        check("a closed window still has the panel", w.contentViewController != nil && !w.isVisible)
        check("the last window closing does not quit the app",
              !AppDelegate().applicationShouldTerminateAfterLastWindowClosed(NSApp))
        check("a headless window records no frame in the live app's defaults",
              w.frameAutosaveName.isEmpty, "got \(w.frameAutosaveName)")

        print("login agent: an older plist is marked unattended")
        let fm = FileManager.default
        let scratch = fm.temporaryDirectory.appendingPathComponent("diplomat-window-\(getpid())")
        try? fm.createDirectory(at: scratch, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: scratch) }
        let plist = scratch.appendingPathComponent("agent.plist")
        let old: [String: Any] = ["Label": Daemon.label, "ProgramArguments": ["/x/Diplomat"],
                                  "RunAtLoad": true, "EnvironmentVariables": ["KEEP": "me"]]
        try? PropertyListSerialization.data(fromPropertyList: old, format: .xml, options: 0).write(to: plist)
        check("a plist without the mark is rewritten", Daemon.markLoginStartUnattended(plist: plist))
        let read = (try? Data(contentsOf: plist)).flatMap {
            try? PropertyListSerialization.propertyList(from: $0, format: nil) as? [String: Any]
        } ?? [:]
        let env = read["EnvironmentVariables"] as? [String: String] ?? [:]
        check("…with DIPLOMAT_BACKGROUND=1", env[LaunchMode.backgroundVar] == "1", "\(env)")
        check("…and every other key kept",
              env["KEEP"] == "me" && read["Label"] as? String == Daemon.label
                  && read["RunAtLoad"] as? Bool == true
                  && read["ProgramArguments"] as? [String] == ["/x/Diplomat"], "\(read)")
        check("a marked plist is left alone", !Daemon.markLoginStartUnattended(plist: plist))
        let absent = scratch.appendingPathComponent("none.plist")
        check("no plist, no agent: nothing is written",
              !Daemon.markLoginStartUnattended(plist: absent) && !fm.fileExists(atPath: absent.path))

        if failures.isEmpty { print("window: all passed") }
        else { print("window: FAILED \(failures.count): \(failures.joined(separator: "; "))") }
        return failures.isEmpty
    }
}
