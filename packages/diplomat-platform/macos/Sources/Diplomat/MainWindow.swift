import SwiftUI
import AppKit

/// How this GUI instance was started. Read once at launch and removed from the
/// environment, so nothing this process starts (a relaunch, a shell-out) inherits it.
enum LaunchMode {
    /// The variable every unattended launch sets: the login LaunchAgent, and the
    /// relaunch the 06:00 updater or the watchdog performs.
    static let backgroundVar = "DIPLOMAT_BACKGROUND"

    /// Started unattended: run with no window up, and never take focus.
    private(set) static var background = false

    static func adopt(from env: [String: String]) {
        background = env[backgroundVar] == "1"
        unsetenv(backgroundVar)
    }
}

/// The app's one window, built once and kept: closing it orders it out, so the
/// panel's state (an open wizard, a scrolled list) is there when it comes back.
@MainActor
final class MainWindow {
    static let frameName = "DiplomatMainWindow"
    static let openSettings = Notification.Name("DiplomatOpenSettings")
    static let didClose = Notification.Name("DiplomatMainWindowDidClose")

    let window: NSWindow

    init(store: Store, screen: ContentView.PanelScreen = .main) {
        let hosting = NSHostingController(rootView: MainRoot(screen: screen).environmentObject(store))
        // The window's size is the operator's, not the panel's ideal one.
        hosting.sizingOptions = []
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: MainRoot.fullWidth, height: 800),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.contentViewController = hosting
        window.title = "Diplomat"
        window.isReleasedWhenClosed = false
        window.contentMinSize = NSSize(width: MainRoot.minWidth, height: MainRoot.minHeight)
        // A headless instance neither reads nor records the live app's saved frame.
        if Headless.active || !window.setFrameUsingName(Self.frameName) {
            let visible = NSScreen.main?.visibleFrame.size ?? NSSize(width: 1440, height: 900)
            window.setContentSize(NSSize(width: min(MainRoot.fullWidth, visible.width),
                                         height: min(visible.height - 40, 1000)))
            window.center()
        }
        if !Headless.active { window.setFrameAutosaveName(Self.frameName) }
        WindowLog.track(window)
        NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification,
                                               object: window, queue: .main) { _ in
            NotificationCenter.default.post(name: MainWindow.didClose, object: nil)
        }
    }

    func show() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        WindowLog.shown(window)
    }

    func showSettings() {
        show()
        NotificationCenter.default.post(name: Self.openSettings, object: nil)
    }
}

/// Logs each open of a window once it has settled, and each close (`AppLog`). A show
/// not on screen and opaque by then is the "won't open" failure.
enum WindowLog {
    static var settle: TimeInterval = 0.4
    private static var closesKey: UInt8 = 0

    private static func closes(_ win: NSWindow) -> Int {
        objc_getAssociatedObject(win, &closesKey) as? Int ?? 0
    }

    static func track(_ win: NSWindow) {
        NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification,
                                               object: win, queue: .main) { note in
            guard let win = note.object as? NSWindow else { return }
            objc_setAssociatedObject(win, &closesKey, closes(win) + 1, .OBJC_ASSOCIATION_RETAIN)
            if !AppLog.exiting { AppLog.log("window", "close (was visible=\(win.isVisible ? 1 : 0))") }
        }
    }

    /// Call right after ordering `win` in. One closed again before it settled is not judged.
    static func shown(_ win: NSWindow) {
        let orderedIn = win.isVisible
        let closesBefore = closes(win)
        DispatchQueue.main.asyncAfter(deadline: .now() + settle) {
            if closes(win) != closesBefore {
                AppLog.log("window", "show, closed again within \(settle)s")
                return
            }
            let ok = win.isVisible && win.occlusionState.contains(.visible) && win.alphaValue >= 0.5
            AppLog.log("window", "show \(ok ? "ok" : "FAILED")"
                + (orderedIn ? "" : " (still invisible right after ordering in)")
                + " \(describe(win))")
        }
    }

    static func describe(_ w: NSWindow) -> String {
        let f = w.frame
        return "visible=\(w.isVisible ? 1 : 0) occlusion=\(w.occlusionState.contains(.visible) ? "visible" : "hidden")"
            + " alpha=\(String(format: "%.2f", w.alphaValue)) key=\(w.isKeyWindow ? 1 : 0)"
            + " activeSpace=\(w.isOnActiveSpace ? 1 : 0) level=\(w.level.rawValue)"
            + " behavior=0x\(String(w.collectionBehavior.rawValue, radix: 16))"
            + " frame=\(Int(f.minX)),\(Int(f.minY)) \(Int(f.width))x\(Int(f.height))"
            + " screen=\(w.screen?.localizedName ?? "none")"
            + " app active=\(NSApp.isActive ? 1 : 0) hidden=\(NSApp.isHidden ? 1 : 0)"
    }
}

/// The window's content: the two-column panel at its fixed width, centred, scrolling
/// when the window is shorter than it, and sideways when narrower.
struct MainRoot: View {
    /// Two columns: the left holds the monitoring lists (status, bans, sessions,
    /// devices, activity), the right every interactive surface (search, tool grid,
    /// results/lookup, action wizards).
    static let width: CGFloat = 1120
    /// Width of each column: (width − 12pt column gap − 2×10pt outer padding) / 2.
    static let columnWidth: CGFloat = (width - 12 - 20) / 2
    /// The window width that shows the whole panel. A legacy ("Show scroll bars:
    /// Always") scroller sits inside the window, so this leaves it a lane beside the panel.
    static let fullWidth: CGFloat = width
        + NSScroller.scrollerWidth(for: .regular, scrollerStyle: .legacy)
    static let minWidth: CGFloat = 480
    static let minHeight: CGFloat = 360

    /// The screen the window opens on (`DIPLOMAT_REOPEN_TEST` opens it elsewhere).
    var screen: ContentView.PanelScreen = .main

    var body: some View {
        GeometryReader { geo in
            if geo.size.width < MainRoot.fullWidth {
                ScrollView([.vertical, .horizontal]) {
                    panel.frame(width: MainRoot.width)
                }
            } else {
                ScrollView {
                    panel
                        .frame(width: MainRoot.width)
                        .frame(maxWidth: .infinity)
                }
            }
        }
    }

    private var panel: ContentView {
        ContentView(showSettings: screen == .settings, showTelemetry: screen == .telemetry,
                    showDevices: screen == .devices, showMachines: screen == .machines,
                    showComms: screen == .comms,
                    showTasks: screen == .tasks)
    }
}

/// The Dock icon: the panel header's wrench on a blue tile. A bundle built by
/// `build-app.sh` carries no icon file, so it is drawn at launch.
enum AppIcon {
    static func image() -> NSImage {
        let size = NSSize(width: 512, height: 512)
        return NSImage(size: size, flipped: false) { rect in
            let tile = rect.insetBy(dx: 50, dy: 50)
            let path = NSBezierPath(roundedRect: tile, xRadius: 90, yRadius: 90)
            NSGradient(starting: NSColor(calibratedRed: 0.25, green: 0.55, blue: 1, alpha: 1),
                       ending: NSColor(calibratedRed: 0.05, green: 0.3, blue: 0.85, alpha: 1))?
                .draw(in: path, angle: -90)
            let config = NSImage.SymbolConfiguration(pointSize: 230, weight: .semibold)
                .applying(.init(paletteColors: [.white]))
            if let glyph = NSImage(systemSymbolName: "wrench.and.screwdriver.fill",
                                   accessibilityDescription: nil)?
                .withSymbolConfiguration(config) {
                let g = glyph.size
                glyph.draw(in: NSRect(x: tile.midX - g.width / 2, y: tile.midY - g.height / 2,
                                      width: g.width, height: g.height))
            }
            return true
        }
    }
}
