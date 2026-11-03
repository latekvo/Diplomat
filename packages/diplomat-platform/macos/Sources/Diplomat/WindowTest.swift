import AppKit

/// Self-test for the app window - `DIPLOMAT_WINDOW_TEST=1`.
///
/// What a launch decides before anything is on screen: an unattended start is read off
/// the environment and removed from it, and the window is a regular one that a close
/// keeps for the next open. Orders no window in, so it takes no focus; showing,
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
        check("never narrower than the panel and a legacy scroller",
              w.contentMinSize.width >= MainRoot.minWindowWidth,
              "min \(w.contentMinSize.width) < \(MainRoot.minWindowWidth)")
        check("a close keeps it for the next open", !w.isReleasedWhenClosed)
        w.close()
        check("a closed window still has the panel", w.contentViewController != nil && !w.isVisible)
        check("the last window closing does not quit the app",
              !AppDelegate().applicationShouldTerminateAfterLastWindowClosed(NSApp))
        check("a headless window records no frame in the live app's defaults",
              w.frameAutosaveName.isEmpty, "got \(w.frameAutosaveName)")

        if failures.isEmpty { print("window: all passed") }
        else { print("window: FAILED \(failures.count): \(failures.joined(separator: "; "))") }
        return failures.isEmpty
    }
}
