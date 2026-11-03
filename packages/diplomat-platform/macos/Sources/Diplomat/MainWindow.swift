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

    let window: NSWindow

    init(store: Store) {
        let hosting = NSHostingController(rootView: MainRoot().environmentObject(store))
        // The window's size is the operator's, not the panel's ideal one.
        hosting.sizingOptions = []
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: MainRoot.minWindowWidth, height: 800),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.contentViewController = hosting
        window.title = "Diplomat"
        window.isReleasedWhenClosed = false
        window.contentMinSize = NSSize(width: MainRoot.minWindowWidth, height: MainRoot.minHeight)
        if !window.setFrameUsingName(Self.frameName) {
            let visible = NSScreen.main?.visibleFrame.height ?? 900
            window.setContentSize(NSSize(width: MainRoot.minWindowWidth, height: min(visible - 40, 1000)))
            window.center()
        }
        // A headless instance shares the live app's defaults domain and persists nothing.
        if !Headless.active { window.setFrameAutosaveName(Self.frameName) }
    }

    func show() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func showSettings() {
        show()
        NotificationCenter.default.post(name: Self.openSettings, object: nil)
    }
}

/// The window's content: the two-column panel at its fixed width, centred, scrolling
/// when the window is shorter than it.
struct MainRoot: View {
    /// Two columns: the left holds the monitoring lists (status, bans, sessions,
    /// devices, activity), the right every interactive surface (search, tool grid,
    /// results/lookup, action wizards).
    static let width: CGFloat = 1120
    /// Width of each column: (width − 12pt column gap − 2×10pt outer padding) / 2.
    static let columnWidth: CGFloat = (width - 12 - 20) / 2
    /// A legacy ("Show scroll bars: Always") scroller sits inside the window, so the
    /// narrowest window leaves it a lane beside the panel instead of clipping it.
    static let minWindowWidth: CGFloat = width
        + NSScroller.scrollerWidth(for: .regular, scrollerStyle: .legacy)
    static let minHeight: CGFloat = 360

    var body: some View {
        ScrollView {
            ContentView()
                .frame(width: MainRoot.width)
                .frame(maxWidth: .infinity)
        }
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
