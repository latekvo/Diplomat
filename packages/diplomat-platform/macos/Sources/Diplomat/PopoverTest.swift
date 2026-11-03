import AppKit
import SwiftUI

/// Self-test for what dismissing the menu-bar popover does - `DIPLOMAT_POPOVER_TEST=1`.
///
/// Two halves. The app's own MenuBarExtra is opened and closed through its status-bar
/// button, which proves a real dismissal posts `PopoverFadeOut.didHide`. Then popovers
/// hosted in off-screen windows are hidden through the same `orderWindow(.out)` and
/// shown again, and the screen they come back on is read off the window's height:
/// Settings returns to the panel, Devices stays, and Settings stays while a modal (the
/// repo-root folder picker) is what hid it.
///
///     DIPLOMAT_POPOVER_TEST=1 swift run Diplomat
///
/// Needs a window server; it shows the popover on screen, so never run it on a
/// machine someone is working at.
@MainActor
enum PopoverTest {
    static func run() async -> Bool {
        var failures: [String] = []
        func check(_ name: String, _ cond: Bool, _ detail: String = "") {
            if cond { print("  ok    \(name)") }
            else { print("  FAIL  \(name) \(detail)"); failures.append(name) }
        }
        // Uncapped, so a screen's height is its content's and two screens never tie at
        // the display's limit.
        setenv("DIPLOMAT_POPOVER_CAP", "10000", 1)
        let store = Store()

        print("popover: a real menu-bar dismissal")
        var hides: [ObjectIdentifier] = []
        let token = NotificationCenter.default.addObserver(
            forName: PopoverFadeOut.didHide, object: nil, queue: nil) { note in
            if let w = note.object as? NSWindow { hides.append(ObjectIdentifier(w)) }
        }
        defer { NotificationCenter.default.removeObserver(token) }
        if let button = statusButton() {
            button.performClick(nil)
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            let popover = NSApp.windows.first { $0.isVisible && $0.frame.width >= PopoverRoot.width }
            check("the status button opens the popover", popover != nil)
            button.performClick(nil)
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            check("the popover is gone after a second press", popover?.isVisible == false)
            check("its dismissal posts didHide",
                  popover.map { hides.contains(ObjectIdentifier($0)) } ?? false,
                  "posted \(hides.count)")
        } else {
            check("the app has a status-bar button", false)
        }

        print("popover: the screen it reopens on")
        let mainHeight = await height(of: host(.main, store))
        let settingsHeight = await height(of: host(.settings, store))
        let devicesHeight = await height(of: host(.devices, store))
        check("Settings and the panel differ in height, so the height names the screen",
              abs(settingsHeight - mainHeight) > 40,
              "settings \(settingsHeight), main \(mainHeight)")
        check("Devices differs from the panel too", abs(devicesHeight - mainHeight) > 40,
              "devices \(devicesHeight), main \(mainHeight)")

        let settings = host(.settings, store)
        _ = await height(of: settings)
        settings.order(.out, relativeTo: 0)
        let reopened = await height(of: settings)
        check("Settings reopens on the panel", abs(reopened - mainHeight) < 2,
              "reopened \(reopened), main \(mainHeight)")

        let devices = host(.devices, store)
        _ = await height(of: devices)
        devices.order(.out, relativeTo: 0)
        let devicesReopened = await height(of: devices)
        check("Devices reopens on Devices", abs(devicesReopened - devicesHeight) < 2,
              "reopened \(devicesReopened), devices \(devicesHeight)")

        let picking = host(.settings, store)
        _ = await height(of: picking)
        let modal = OffscreenWindow(contentRect: NSRect(x: -4000, y: -4000, width: 10, height: 10),
                                    styleMask: [.titled], backing: .buffered, defer: false)
        let timer = Timer(timeInterval: 0.2, repeats: false) { _ in
            MainActor.assumeIsolated {
                picking.order(.out, relativeTo: 0)
                NSApp.stopModal()
            }
        }
        RunLoop.main.add(timer, forMode: .modalPanel)
        NSApp.runModal(for: modal)
        modal.orderOut(nil)
        let pickedReopened = await height(of: picking)
        check("Settings hidden by a modal reopens on Settings",
              abs(pickedReopened - settingsHeight) < 2,
              "reopened \(pickedReopened), settings \(settingsHeight)")

        print(failures.isEmpty ? "popover: all passed" : "popover: \(failures.count) FAILED")
        return failures.isEmpty
    }

    /// Never dragged back on screen by AppKit, unlike a plain window.
    private final class OffscreenWindow: NSWindow {
        override func constrainFrameRect(_ frameRect: NSRect, to screen: NSScreen?) -> NSRect {
            frameRect
        }
    }

    private static func host(_ screen: ContentView.PanelScreen, _ store: Store) -> NSWindow {
        let hosting = NSHostingController(rootView: PopoverRoot(screen: screen).environmentObject(store))
        let window = OffscreenWindow(contentViewController: hosting)
        window.setFrameOrigin(NSPoint(x: -6000, y: -6000))
        return window
    }

    /// Orders the window in and lets the content-height pass settle.
    private static func height(of window: NSWindow) async -> CGFloat {
        window.orderFrontRegardless()
        try? await Task.sleep(nanoseconds: 1_500_000_000)
        return window.frame.height
    }

    private static func statusButton() -> NSStatusBarButton? {
        for window in NSApp.windows {
            var queue = [window.contentView].compactMap { $0 }
            while let view = queue.popLast() {
                if let button = view as? NSStatusBarButton { return button }
                queue.append(contentsOf: view.subviews)
            }
        }
        return nil
    }
}
