import SwiftUI
import AppKit
import ArgentUtilsCore
import ObjectiveC

/// Sizes the menu-bar popover to its content, capped at the screen's safe area so it
/// never spills past the menu bar (top) or the Dock — the "bottom bar" (bottom). The
/// content lays out at its natural height; once that would exceed the cap the popover
/// stops growing and the content scrolls instead of running off-screen.
struct PopoverRoot: View {
    /// Fixed popover width. Two-column layout: the left column holds the lists (status,
    /// bans, sessions, devices, activity, results), the right the interactive controls
    /// (search, tool grid, action wizards).
    static let width: CGFloat = 1120
    /// Width of each of the two columns (minus the inter-column gap + outer padding).
    static let columnWidth: CGFloat = 536

    /// The content's measured natural height. Seeded with a sane default so the very
    /// first frame isn't zero-height; corrected on the first layout pass.
    @State private var contentHeight: CGFloat = 600

    /// Usable height of the display the popover currently sits on (`visibleFrame` already
    /// excludes the menu bar + Dock). Reported by `WindowCenterer` from the window's ACTUAL
    /// screen — not `NSScreen.main`, which tracks the key-window's screen and can point at
    /// a taller display than the one the popover opens on, letting the content grow past
    /// what fits and spill off the bottom.
    @State private var displayVisibleHeight: CGFloat = NSScreen.main?.visibleFrame.height ?? 800

    /// Cap the popover at the display's usable height, less a small margin so it never
    /// kisses the menu bar or the Dock. Content beyond this scrolls.
    private var cap: CGFloat { max(320, displayVisibleHeight - 12) }

    var body: some View {
        ScrollView {
            ContentView()
                .background(
                    GeometryReader { geo in
                        Color.clear.preference(key: ContentHeightKey.self, value: geo.size.height)
                    }
                )
        }
        .scrollDisabled(contentHeight <= cap)
        .frame(width: PopoverRoot.width, height: min(contentHeight, cap))
        .onPreferenceChange(ContentHeightKey.self) { h in
            if h > 1, abs(h - contentHeight) > 0.5 { contentHeight = h }
        }
        .background(PopoverWindowController(onDisplayVisibleHeight: { h in
            if abs(h - displayVisibleHeight) > 0.5 { displayVisibleHeight = h }
        }))
    }
}

/// Horizontally centers the MenuBarExtra popover on the display it opens on. The system
/// anchors the window under the status item (the wrench), which for this wide (1120px)
/// popover lands far off to one side on displays where the wrench isn't near the middle.
///
/// MenuBarExtra reuses one window and re-anchors it on every open WITHOUT re-running the
/// SwiftUI view body, so a plain update-driven recentre only fires on the first open.
/// Instead we grab the hosting window once and, off AppKit notifications that fire on every
/// show/hide (become/resign-key, occlusion) and resize, we: keep it horizontally centred on
/// its display, report that display's usable height for the content cap, and fade it in/out
/// by animating `alphaValue` (the system otherwise shows/hides it instantly).
private struct PopoverWindowController: NSViewRepresentable {
    /// Reports the usable height of the display the popover is on, so `PopoverRoot` caps
    /// its content to what actually fits there.
    var onDisplayVisibleHeight: (CGFloat) -> Void = { _ in }

    func makeCoordinator() -> Coordinator { Coordinator() }
    func makeNSView(context: Context) -> NSView {
        context.coordinator.onDisplayVisibleHeight = onDisplayVisibleHeight
        let v = NSView(frame: .zero)
        context.coordinator.attach(to: v)
        return v
    }
    func updateNSView(_ nsView: NSView, context: Context) {
        context.coordinator.onDisplayVisibleHeight = onDisplayVisibleHeight
        context.coordinator.center()
    }

    final class Coordinator {
        /// Fade-in duration. (Fade-out is driven by `PopoverFadeOut` on the window itself.)
        static let fadeInDuration = 0.1

        var onDisplayVisibleHeight: (CGFloat) -> Void = { _ in }
        private weak var view: NSView?
        private weak var observed: NSWindow?
        private var tokens: [NSObjectProtocol] = []

        func attach(to view: NSView) {
            self.view = view
            hookWindow(retries: 40)
        }

        /// Wait (a few runloop turns) for the view to land in its window, then observe it.
        private func hookWindow(retries: Int) {
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                guard let window = self.view?.window else {
                    if retries > 0 { self.hookWindow(retries: retries - 1) }
                    return
                }
                if self.observed !== window {
                    self.tokens.forEach(NotificationCenter.default.removeObserver)
                    self.observed = window
                    // Own the show/hide look: start transparent so the first appearance fades
                    // in, and delay the window's own instant hide so we can fade it OUT.
                    window.animationBehavior = .none
                    window.alphaValue = 0
                    PopoverFadeOut.enable(on: window)
                    let nc = NotificationCenter.default
                    self.tokens = [
                        // Each open: the popover takes key focus.
                        nc.addObserver(forName: NSWindow.didBecomeKeyNotification, object: window, queue: .main) {
                            [weak self] _ in self?.onShow()
                        },
                        // Show/hide (also covers opens where key focus doesn't change).
                        nc.addObserver(forName: NSWindow.didChangeOcclusionStateNotification, object: window, queue: .main) {
                            [weak self] _ in self?.onOcclusionChange()
                        },
                        // Content-height correction resizes the window; keep it centred.
                        nc.addObserver(forName: NSWindow.didResizeNotification, object: window, queue: .main) {
                            [weak self] _ in self?.center()
                        },
                    ]
                }
                self.onShow()
            }
        }

        // MARK: show / hide

        private func onShow() { center(); fadeIn() }

        private func onOcclusionChange() {
            guard let w = observed else { return }
            if w.occlusionState.contains(.visible) {
                onShow()
            } else {
                // Fully hidden: reset to transparent so the next open fades in cleanly.
                w.alphaValue = 0
            }
        }

        private func fadeIn() {
            guard let w = observed, w.alphaValue < 0.99 else { return }
            NSAnimationContext.runAnimationGroup { ctx in
                ctx.duration = Coordinator.fadeInDuration
                w.animator().alphaValue = 1
            }
        }

        // MARK: placement + sizing

        func center() {
            // Defer so we run AFTER the system has re-anchored + sized the window on show.
            DispatchQueue.main.async { [weak self] in
                guard let self,
                      let window = self.observed ?? self.view?.window,
                      let screen = window.screen ?? NSScreen.main else { return }
                // Report the display's usable height so the content caps to what fits here.
                self.onDisplayVisibleHeight(screen.visibleFrame.height)
                let targetX = PopoverPlacement.centeredX(screen: screen.frame, windowWidth: window.frame.width)
                if abs(window.frame.origin.x - targetX) > 0.5 {
                    window.setFrameOrigin(NSPoint(x: targetX, y: window.frame.origin.y))
                }
            }
        }

        deinit { tokens.forEach(NotificationCenter.default.removeObserver) }
    }
}

/// The horizontal placement math, factored out so it's testable without a live window.
enum PopoverPlacement {
    /// The x-origin that horizontally centers a `windowWidth`-wide window on `screen`
    /// (both in global/screen coordinates).
    static func centeredX(screen: CGRect, windowWidth: CGFloat) -> CGFloat {
        screen.midX - windowWidth / 2
    }
}

/// Gives the MenuBarExtra popover a fade-OUT. The system hides the window via `orderOut:`,
/// instantly, so there's nothing to animate. We swap that method's IMPLEMENTATION (method
/// swizzle — crucially NOT a class change, so the window's KVO isa stays intact and it can't
/// crash the way an `object_setClass` reclass does) with one that first animates `alphaValue`
/// to 0, THEN calls the original `orderOut:`. A per-window flag means only our popover fades;
/// any other window sharing the class hides normally.
enum PopoverFadeOut {
    static let duration: TimeInterval = 0.1
    /// `NSWindowOrderingMode.out` — the hide case of `orderWindow:relativeTo:`.
    private static let orderOut = 0
    private static var swizzled = Set<ObjectIdentifier>()
    private static var isPopoverKey: UInt8 = 0
    private static var isFadingKey: UInt8 = 0

    static func enable(on window: NSWindow) {
        // Tag this exact window; the swizzled method only fades tagged windows.
        objc_setAssociatedObject(window, &isPopoverKey, true, .OBJC_ASSOCIATION_RETAIN)

        guard let cls: AnyClass = object_getClass(window) else { return }
        let id = ObjectIdentifier(cls)
        guard !swizzled.contains(id) else { return }   // one swap per class is enough
        swizzled.insert(id)

        // MenuBarExtra shows/hides the popover through `orderWindow:relativeTo:` (never
        // `orderOut:`). `.above` (1) is the show, `.out` (0) the hide — and at hide time the
        // window is still visible at full alpha, so we fade it to 0 there, then let it order
        // out. Method swizzle (no `object_setClass`), so the window's KVO isa is untouched.
        let sel = #selector(NSWindow.order(_:relativeTo:))
        guard let method = class_getInstanceMethod(cls, sel),
              let types = method_getTypeEncoding(method) else { return }
        let originalIMP = method_getImplementation(method)
        typealias OrderWindow = @convention(c) (NSObject, Selector, Int, Int) -> Void
        let callOriginal: (NSWindow, Int, Int) -> Void = { win, place, relativeTo in
            unsafeBitCast(originalIMP, to: OrderWindow.self)(win, sel, place, relativeTo)
        }

        let block: @convention(block) (NSWindow, Int, Int) -> Void = { win, place, relativeTo in
            let ours = (objc_getAssociatedObject(win, &isPopoverKey) as? Bool) ?? false
            let fading = (objc_getAssociatedObject(win, &isFadingKey) as? Bool) ?? false
            // Only intercept OUR popover's hide, once, while it's actually visible.
            guard place == orderOut, ours, !fading, win.isVisible, win.alphaValue > 0.01 else {
                callOriginal(win, place, relativeTo)
                return
            }
            objc_setAssociatedObject(win, &isFadingKey, true, .OBJC_ASSOCIATION_RETAIN)
            NSAnimationContext.runAnimationGroup({ ctx in
                ctx.duration = duration
                win.animator().alphaValue = 0
            }, completionHandler: {
                objc_setAssociatedObject(win, &isFadingKey, false, .OBJC_ASSOCIATION_RETAIN)
                callOriginal(win, place, relativeTo)   // now actually order it out
            })
        }
        let newIMP = imp_implementationWithBlock(block)
        // Add an override on THIS class only; if it already defines the method, replace it.
        if !class_addMethod(cls, sel, newIMP, types) {
            method_setImplementation(class_getInstanceMethod(cls, sel)!, newIMP)
        }
    }
}

/// Carries the content's natural height up from a background GeometryReader so
/// `PopoverRoot` can size the window to it.
private struct ContentHeightKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}

struct ContentView: View {
    @EnvironmentObject var store: Store
    @State private var query = ""
    @State private var showingSettings: Bool

    /// `showSettings` seeds the initial settings state — used by the headless render
    /// to snapshot the Settings screen in context. Defaults to the main view.
    /// `seedPendingUnban` seeds the inline un-ban confirmation for a login (render only).
    init(showSettings: Bool = false, seedPendingUnban: String? = nil) {
        _showingSettings = State(initialValue: showSettings)
        _pendingUnban = State(initialValue: seedPendingUnban)
    }
    /// Which action wizard (if any) replaces the tool lists in the results pane.
    @State private var activeAction: ActionPanel?
    /// Rows whose last click couldn't be focused or opened — show "tracking lost".
    /// Whether the prompt-injection ban list (above the sessions) is expanded.
    @State private var bannedExpanded = true
    /// Whether the activity/audit log is expanded.
    @State private var auditExpanded = true
    /// The login whose un-ban is awaiting inline confirmation (nil = none). Kept inline
    /// in the popover rather than an NSAlert, which would open behind the menu-bar window.
    @State private var pendingUnban: String?
    @FocusState private var searchFocused: Bool

    /// The action cards in the grid that open a wizard instead of selecting a tool.
    private enum ActionPanel: Hashable { case review, conflicts, audit }

    var body: some View {
        VStack(spacing: 8) {
            header
            if showingSettings {
                // A form reads badly stretched to the full 2-column width; cap it.
                SettingsView(isPresented: $showingSettings)
                    .frame(maxWidth: 640, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                HStack(alignment: .top, spacing: 12) {
                    leftColumn.frame(width: PopoverRoot.columnWidth, alignment: .top)
                    rightColumn.frame(width: PopoverRoot.columnWidth, alignment: .top)
                }
            }
        }
        .padding(10)
        .background(cmdFCatcher)
        .animation(.easeInOut(duration: 0.18), value: store.processes)
        .task {
            // Optional: launch pre-focused on a specific number (also used for headless UI checks).
            if query.isEmpty, let pre = ProcessInfo.processInfo.environment["ARGENT_UTILS_PREFILL"], !pre.isEmpty {
                query = pre
                searchFocused = true
            }
            if !store.hasLoaded { await store.refresh() }
        }
    }

    // MARK: header

    private var header: some View {
        HStack(spacing: 6) {
            Image(systemName: "wrench.and.screwdriver.fill").foregroundStyle(.blue)
            Text("Argent Utils").font(.headline)
            Text("software-mansion/argent").font(.caption2).foregroundStyle(.secondary)
            Spacer()
            if store.isLoading {
                ProgressView().controlSize(.small)
            }
            Text("upd \(Fmt.clock(store.lastUpdated))").font(.caption2).foregroundStyle(.secondary)
            Button { Task { await store.refresh() } } label: {
                Image(systemName: "arrow.clockwise")
            }.buttonStyle(.borderless).help("Refresh")
            Button { withAnimation(.easeInOut(duration: 0.15)) { showingSettings.toggle() } } label: {
                Image(systemName: showingSettings ? "gearshape.fill" : "gearshape")
                    .foregroundStyle(showingSettings ? Color.accentColor : .primary)
            }.buttonStyle(.borderless).help("Settings")
            Button { QuitFlow.confirm() } label: {
                Image(systemName: "power")
            }.buttonStyle(.borderless).help("Quit")
        }
    }

    // MARK: auto-fix monitor status pill

    /// Top-of-panel indicator that the external PR auto-fix monitor is running. Green
    /// "active" ONLY on a fresh heartbeat (so it never lies); amber "agent offline"
    /// when enabled but nothing is running; hidden when the feature is toggled off.
    /// Tapping it jumps to Settings, where the toggle lives.
    @ViewBuilder
    private var autofixBanner: some View {
        if store.prAutofixEnabled {
            let live = store.autofixStatus?.isLive == true
            let accent = live ? Color.green : Color.orange
            Button {
                withAnimation(.easeInOut(duration: 0.15)) { showingSettings = true }
            } label: {
                HStack(spacing: 7) {
                    Image(systemName: live ? "bolt.fill" : "bolt.slash.fill")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundStyle(accent)
                    Text(live ? "Auto-fixing PRs" : "Auto-fix enabled")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(.primary)
                    if live {
                        Text("active").font(.system(size: 9, weight: .bold)).foregroundStyle(accent)
                    } else {
                        Text("· agent offline").font(.system(size: 9)).foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 4)
                    if live, let s = store.autofixStatus {
                        Text("watching \(s.watching)\(s.totalHandled > 0 ? " · fixed \(s.totalHandled)" : "")")
                            .font(.system(size: 9).monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                    Image(systemName: "gearshape").font(.system(size: 9)).foregroundStyle(.secondary.opacity(0.7))
                }
                .padding(.horizontal, 9).padding(.vertical, 6)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 7).fill(accent.opacity(0.12)))
                .overlay(RoundedRectangle(cornerRadius: 7).stroke(accent.opacity(0.35), lineWidth: 1))
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(live
                  ? "A monitor is watching your open PRs and auto-fixing conflicts & new reviews. Click to manage in Settings."
                  : "Auto-fix is enabled but no monitor is currently running. Click to manage in Settings.")
        }
    }

    // MARK: prompt-injection ban list

    /// Collapsible list of authors banned for prompt injection — they get no automated
    /// reviews. Each row links to the captured evidence and can be un-banned.
    @ViewBuilder
    private func bannedList(_ bans: [BannedAuthor]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Button {
                withAnimation(.easeInOut(duration: 0.16)) { bannedExpanded.toggle() }
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: bannedExpanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 8, weight: .bold)).foregroundStyle(.secondary).frame(width: 9)
                    Image(systemName: "hand.raised.fill").font(.system(size: 9)).foregroundStyle(.red)
                    Text("BANNED").font(.system(size: 9, weight: .bold))
                        .foregroundStyle(.secondary).kerning(0.5)
                    Text("\(bans.count)").font(.system(size: 9).monospacedDigit())
                        .foregroundStyle(.red)
                        .padding(.horizontal, 5).padding(.vertical, 1)
                        .background(Capsule().fill(Color.red.opacity(0.15)))
                    Text("prompt injection · no auto-reviews")
                        .font(.system(size: 9)).foregroundStyle(.secondary)
                    Spacer()
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if bannedExpanded {
                ForEach(bans) { ban in
                    if pendingUnban == ban.login {
                        unbanConfirmRow(ban).transition(.opacity)
                    } else {
                        BanRow(ban: ban, onUnban: {
                            withAnimation(.easeInOut(duration: 0.15)) { pendingUnban = ban.login }
                        })
                    }
                }
            }
        }
        .padding(7)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.red.opacity(0.06)))
    }

    /// Inline "Unban @X?" confirmation shown in place of the ban row when its ✕ is
    /// clicked — kept inside the popover so it can't open behind the menu-bar window.
    private func unbanConfirmRow(_ ban: BannedAuthor) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "questionmark.circle.fill")
                .font(.system(size: 11, weight: .bold)).foregroundStyle(.white)
                .frame(width: 22, height: 22)
                .background(Color.orange.opacity(0.85))
                .clipShape(RoundedRectangle(cornerRadius: 5))
            VStack(alignment: .leading, spacing: 1) {
                Text("Unban @\(ban.login)?").font(.caption.bold())
                Text("Their PRs will receive automated reviews again.")
                    .font(.system(size: 9)).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 4)
            Button("Cancel") {
                withAnimation(.easeInOut(duration: 0.15)) { pendingUnban = nil }
            }
            .buttonStyle(.plain).font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary)
            .padding(.horizontal, 9).padding(.vertical, 4)
            .background(Capsule().fill(Color.gray.opacity(0.18)))
            Button("Unban") {
                store.unban(ban.login)
                withAnimation(.easeInOut(duration: 0.15)) { pendingUnban = nil }
            }
            .buttonStyle(.plain).font(.system(size: 10, weight: .bold)).foregroundStyle(.white)
            .padding(.horizontal, 9).padding(.vertical, 4)
            .background(Capsule().fill(Color.red.opacity(0.85)))
        }
        .padding(6)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.orange.opacity(0.12)))
    }

    // MARK: activity / audit log

    /// Collapsible unified activity feed — every action, whether triggered from the panel
    /// or dispatched automatically (or reported by an agent). Newest first.
    @ViewBuilder
    private func auditList(_ entries: [AuditEntry]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Button {
                withAnimation(.easeInOut(duration: 0.16)) { auditExpanded.toggle() }
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: auditExpanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 8, weight: .bold)).foregroundStyle(.secondary).frame(width: 9)
                    Image(systemName: "list.bullet.rectangle").font(.system(size: 9)).foregroundStyle(.secondary)
                    Text("ACTIVITY").font(.system(size: 9, weight: .bold))
                        .foregroundStyle(.secondary).kerning(0.5)
                    Text("\(entries.count)").font(.system(size: 9).monospacedDigit())
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 5).padding(.vertical, 1)
                        .background(Capsule().fill(Color.gray.opacity(0.15)))
                    Spacer()
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if auditExpanded {
                ForEach(entries.prefix(30)) { entry in AuditRow(entry: entry) }
            }
        }
        .padding(7)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.gray.opacity(0.07)))
    }

    // MARK: ongoing agent sessions

    private var processList: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 5) {
                Image(systemName: "antenna.radiowaves.left.and.right")
                    .font(.system(size: 9)).foregroundStyle(.secondary)
                Text("Agent sessions").font(.system(size: 10, weight: .bold))
                    .foregroundStyle(.secondary)
                Text("\(store.processes.count)")
                    .font(.system(size: 10).monospacedDigit()).foregroundStyle(.secondary)
                Spacer()
            }
            ForEach(store.processes) { proc in
                ProcessRow(
                    proc: proc,
                    tint: processTint(proc),
                    onTap: { activate(proc) },
                    onRemove: { store.removeProcess(proc.id) }
                )
            }
        }
        .padding(7)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.gray.opacity(0.07)))
    }

    private func processTint(_ proc: TrackedProcess) -> Color {
        switch proc.kind {
        case "conflicts": return .cyan
        case "audit":     return .indigo
        default:          return .pink
        }
    }

    /// Click a tracked row: focus its window. If focus fails the window is gone and
    /// the session is dismissed by the store — nothing more to do here.
    private func activate(_ proc: TrackedProcess) {
        Task { _ = await store.activate(proc) }
    }

    // MARK: search (reverse lookup)

    private var searchBar: some View {
        HStack(spacing: 6) {
            Image(systemName: "magnifyingglass").font(.caption).foregroundStyle(.secondary)
            TextField("PR / issue #  (⌘F)", text: $query)
                .textFieldStyle(.plain)
                .font(.callout)
                .focused($searchFocused)
            if !query.isEmpty {
                Button { query = ""; searchFocused = true } label: {
                    Image(systemName: "xmark.circle.fill")
                }.buttonStyle(.borderless).foregroundStyle(.secondary).help("Clear")
            }
        }
        .padding(6)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.1)))
        .overlay(
            RoundedRectangle(cornerRadius: 6)
                .stroke(searchFocused ? Color.accentColor : .clear, lineWidth: 1)
        )
    }

    /// Invisible button whose ⌘F shortcut moves focus into the search field.
    private var cmdFCatcher: some View {
        Button("") { searchFocused = true }
            .keyboardShortcut("f", modifiers: .command)
            .opacity(0)
            .frame(width: 0, height: 0)
            .accessibilityHidden(true)
    }

    private func errorBanner(_ msg: String) -> some View {
        Text(msg)
            .font(.caption2)
            .foregroundStyle(.white)
            .padding(6)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.red.opacity(0.85))
            .clipShape(RoundedRectangle(cornerRadius: 6))
    }

    // MARK: tool library

    private var toolGrid: some View {
        LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 8) {
            ForEach(store.visibleTools) { kind in
                ToolCard(
                    kind: kind,
                    tint: store.tint(for: kind),
                    count: store.hasLoaded ? store.count(for: kind) : nil,
                    selected: store.selected == kind && activeAction == nil
                )
                .onTapGesture { activeAction = nil; store.selected = kind }
            }
            ActionCard(
                systemImage: "checklist",
                title: "Review PRs",
                subtitle: "spawn a review agent",
                tint: .pink,
                selected: activeAction == .review
            )
            .onTapGesture { activeAction = .review }
            ActionCard(
                systemImage: "arrow.triangle.merge",
                title: "Resolve conflicts",
                subtitle: "merge main, fix conflicts",
                tint: .cyan,
                selected: activeAction == .conflicts
            )
            .onTapGesture { activeAction = .conflicts }
            ActionCard(
                systemImage: "ladybug.fill",
                title: "Full E2E test",
                subtitle: "swarm-test the whole repo",
                tint: .indigo,
                selected: activeAction == .audit
            )
            .onTapGesture { activeAction = .audit }
        }
    }

    // MARK: two columns — lists on the left, interactive controls on the right

    /// Left column: status pill, ban list, agent sessions, devices, activity log, and —
    /// when no action wizard is open — the selected tool's result list / lookup.
    private var leftColumn: some View {
        VStack(alignment: .leading, spacing: 8) {
            autofixBanner
            if !store.bannedAuthors.isEmpty { bannedList(store.bannedAuthors) }
            if !store.processes.isEmpty { processList }
            if let ds = store.deviceState, !ds.devices.isEmpty {
                DevicesView(ds: ds, tracked: store.processes,
                            onKill: { key in Task { await store.killDevice(key) } })
            }
            if !store.auditEntries.isEmpty { auditList(store.auditEntries) }
            Spacer(minLength: 0)
        }
    }

    /// Right column: every interactive surface — the reverse-lookup search, the tool
    /// grid, and whatever a card opens: an action wizard, or the selected tool's
    /// info/result list. Both always render here (never in the left list column).
    private var rightColumn: some View {
        VStack(alignment: .leading, spacing: 8) {
            searchBar
            if let err = store.error { errorBanner(err) }
            toolGrid
            Divider().padding(.vertical, 1)
            if activeAction != nil {
                wizardPane
            } else {
                listResultsPane
            }
            Spacer(minLength: 0)
        }
    }

    /// The action wizard (interactive) — right column. Sizes to its own content
    /// (scrolls: false); PopoverRoot's outer scroll view handles any overflow.
    @ViewBuilder
    private var wizardPane: some View {
        switch activeAction {
        case .review:    ReviewWizardView(scrolls: false)
        case .conflicts: ConflictWizardView(scrolls: false)
        case .audit:     AuditWizardView(scrolls: false)
        case .none:      EmptyView()
        }
    }

    /// The lookup / tool-result LIST (left column) shown when no wizard is open.
    @ViewBuilder
    private var listResultsPane: some View {
        let trimmed = query.trimmingCharacters(in: .whitespaces)
        if !trimmed.isEmpty, let n = Int(trimmed) {
            lookupView(n)
                .frame(maxWidth: .infinity, alignment: .topLeading)
        } else if !trimmed.isEmpty {
            Text("Type a PR or issue number.")
                .font(.caption).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.vertical, 12)
        } else {
            toolResults
        }
    }

    private func lookupView(_ n: Int) -> some View {
        let r = store.lookup(n)
        let cfg = try? CoreAssets.config()
        let link = r.url ?? "https://github.com/\(cfg?.owner ?? "software-mansion")/\(cfg?.repo ?? "argent")/issues/\(n)"
        return VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text("#\(n)").font(.title3.bold().monospaced())
                Text(r.isOnAnyList ? "on \(r.onLists.count) list\(r.onLists.count == 1 ? "" : "s")" : "on no list")
                    .font(.caption.bold())
                    .foregroundStyle(r.isOnAnyList ? .green : .secondary)
                Spacer()
                Button { if let u = URL(string: link) { NSWorkspace.shared.open(u) } } label: {
                    Image(systemName: "arrow.up.forward.square")
                }.buttonStyle(.borderless).help("Open #\(n) on GitHub")
            }
            Text(r.presence).font(.caption).foregroundStyle(.secondary)
            VStack(spacing: 5) {
                ForEach(store.visibleTools) { kind in
                    checkRow(kind, on: r.onLists.contains(kind))
                }
            }
        }
        .padding(.top, 2)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func checkRow(_ kind: ToolKind, on: Bool) -> some View {
        let tint = store.tint(for: kind)
        return HStack(spacing: 8) {
            Image(systemName: kind.systemImage)
                .font(.system(size: 11, weight: .bold))
                .foregroundStyle(.white)
                .frame(width: 22, height: 22)
                .background(on ? tint : Color.gray.opacity(0.35))
                .clipShape(RoundedRectangle(cornerRadius: 5))
            Text(kind.title).font(.caption).foregroundStyle(on ? .primary : .secondary)
            Spacer()
            Image(systemName: on ? "checkmark.circle.fill" : "minus")
                .foregroundStyle(on ? tint : .secondary)
        }
        .padding(7)
        .background(
            RoundedRectangle(cornerRadius: 6)
                .fill(on ? tint.opacity(0.12) : Color.gray.opacity(0.05))
        )
    }

    @ViewBuilder
    private var toolResults: some View {
        // The selected tool may have been hidden in Settings; fall back to the
        // first visible one, or an empty-state hint if the user hid them all.
        if let kind = store.visibleTools.contains(store.selected) ? store.selected : store.visibleTools.first {
            let items = store.items(for: kind)
            let tint = store.tint(for: kind)
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Image(systemName: kind.systemImage).foregroundStyle(tint)
                    Text(kind.title).font(.subheadline.bold())
                    Text("\(items.count)").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    Spacer()
                }
                if items.isEmpty {
                    Text(store.isLoading ? "Loading…" : "Nothing here.")
                        .font(.caption).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.vertical, 12)
                } else {
                    VStack(spacing: 4) {
                        ForEach(items) { item in
                            if kind == .myApproved,
                               let pr = store.prs.first(where: { $0.number == item.id }) {
                                HStack(spacing: 6) {
                                    ResultRow(item: item, tint: tint)
                                    MergeButton(
                                        conflicts: pr.hasConflicts,
                                        busy: store.mergingPRs.contains(pr.number),
                                        onMerge: { Task { await store.mergePR(pr.number) } },
                                        onResolve: { Task { await store.resolveConflicts(for: pr.number) } })
                                }
                            } else {
                                ResultRow(item: item, tint: tint)
                            }
                        }
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .topLeading)
        } else {
            VStack(spacing: 6) {
                Image(systemName: "eye.slash").font(.title3).foregroundStyle(.secondary)
                Text("All tools hidden").font(.caption.bold()).foregroundStyle(.secondary)
                Text("Re-enable some under ⚙︎ Settings.").font(.caption2).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 16)
        }
    }
}

// MARK: - Tool card

private struct ToolCard: View {
    let kind: ToolKind
    let tint: Color
    let count: Int?
    let selected: Bool

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: kind.systemImage)
                .font(.system(size: 13, weight: .bold))
                .foregroundStyle(.white)
                .frame(width: 26, height: 26)
                .background(tint)
                .clipShape(RoundedRectangle(cornerRadius: 6))
            VStack(alignment: .leading, spacing: 1) {
                Text(kind.title).font(.caption.bold()).lineLimit(1)
                Text(kind.subtitle).font(.system(size: 9)).foregroundStyle(.secondary).lineLimit(2)
            }
            Spacer(minLength: 2)
            Text(count.map(String.init) ?? "…")
                .font(.callout.bold().monospacedDigit())
                .foregroundStyle(tint)
        }
        .padding(7)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(selected ? tint.opacity(0.16) : Color.gray.opacity(0.08))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(selected ? tint : .clear, lineWidth: 1.2)
        )
        .contentShape(Rectangle())
    }
}

// MARK: - Action card (grid entry that opens an action pane, e.g. Review PRs)

private struct ActionCard: View {
    let systemImage: String
    let title: String
    let subtitle: String
    let tint: Color
    let selected: Bool

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: systemImage)
                .font(.system(size: 13, weight: .bold))
                .foregroundStyle(.white)
                .frame(width: 26, height: 26)
                .background(tint)
                .clipShape(RoundedRectangle(cornerRadius: 6))
            VStack(alignment: .leading, spacing: 1) {
                Text(title).font(.caption.bold()).lineLimit(1)
                Text(subtitle).font(.system(size: 9)).foregroundStyle(.secondary).lineLimit(2)
            }
            Spacer(minLength: 2)
            Image(systemName: "chevron.right")
                .font(.system(size: 11, weight: .bold))
                .foregroundStyle(selected ? tint : .secondary)
        }
        .padding(7)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(selected ? tint.opacity(0.16) : Color.gray.opacity(0.08))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(selected ? tint : .clear, lineWidth: 1.2)
        )
        .contentShape(Rectangle())
    }
}

// MARK: - Ongoing agent-session row

private struct ProcessRow: View {
    let proc: TrackedProcess
    let tint: Color
    let onTap: () -> Void
    let onRemove: () -> Void

    /// The leading glyph, matched to the action that spawned the session.
    private var kindIcon: String {
        switch proc.kind {
        case "conflicts": return "arrow.triangle.merge"
        case "audit":     return "ladybug.fill"
        default:          return "checklist"
        }
    }

    var body: some View {
        HStack(spacing: 6) {
            Button(action: onTap) {
                HStack(spacing: 8) {
                    Image(systemName: kindIcon)
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(.white)
                        .frame(width: 22, height: 22)
                        .background(tint)
                        .clipShape(RoundedRectangle(cornerRadius: 5))
                    VStack(alignment: .leading, spacing: 1) {
                        Text(proc.label).font(.caption).lineLimit(1)
                        statusLine
                    }
                    Spacer(minLength: 4)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Bring this session's window to the front.")

            Button(action: onRemove) {
                Image(systemName: "xmark.circle.fill")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.borderless)
            .help("Stop tracking — remove from the list.")
        }
        .padding(6)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.06)))
    }

    @ViewBuilder
    private var statusLine: some View {
        // "merged" is the definitive outcome — it outranks "done" (the local claude
        // process merely exited; the PR may still be open).
        if proc.merged {
            label("merged", "arrow.triangle.merge", .purple)
        } else if proc.done {
            label("done", "checkmark.circle.fill", .green)
        } else {
            label("running", "circle.fill", .blue)
        }
    }

    private func label(_ text: String, _ symbol: String, _ color: Color) -> some View {
        HStack(spacing: 3) {
            Image(systemName: symbol).font(.system(size: 8))
            Text(text).font(.system(size: 9))
        }
        .foregroundStyle(color)
    }
}

// MARK: - Device-allocator row

// MARK: - Device allocator pool

/// The Devices section: in-use and free devices in two independently-collapsible
/// groups. In-use is expanded by default (it's what you're watching); free starts
/// collapsed. In-use rows track how long the device has been held and, on click,
/// try to focus the terminal running the agent that holds it.
struct DevicesView: View {
    let ds: DeviceState
    let tracked: [TrackedProcess]
    /// Kill a device by key (the per-row X). No-op default so the renderer can omit it.
    var onKill: (String) -> Void = { _ in }

    @State private var inUseExpanded: Bool
    @State private var freeExpanded: Bool

    /// The seed params let the headless renderer snapshot either collapse state.
    init(ds: DeviceState, tracked: [TrackedProcess], onKill: @escaping (String) -> Void = { _ in },
         seedInUseExpanded: Bool = true, seedFreeExpanded: Bool = false) {
        self.ds = ds
        self.tracked = tracked
        self.onKill = onKill
        _inUseExpanded = State(initialValue: seedInUseExpanded)
        _freeExpanded = State(initialValue: seedFreeExpanded)
    }

    /// Within a section: by platform, then name. (Cross-section busy-first ordering
    /// is gone — the split into In use / Free already conveys that.)
    private func sorted(_ d: [DeviceAllocation]) -> [DeviceAllocation] {
        d.sorted { a, b in
            if a.platform != b.platform { return a.platform < b.platform }
            return (a.name ?? "") < (b.name ?? "")
        }
    }

    var body: some View {
        let inUse = sorted(ds.devices.filter { $0.isAllocated })
        let free = sorted(ds.devices.filter { !$0.isAllocated })
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 5) {
                Image(systemName: "iphone").font(.system(size: 9)).foregroundStyle(.secondary)
                Text("Devices").font(.system(size: 10, weight: .bold)).foregroundStyle(.secondary)
                Spacer()
            }
            if !inUse.isEmpty {
                section("In use", color: .green, expanded: $inUseExpanded, devices: inUse)
            }
            if !free.isEmpty {
                section("Free", color: .secondary, expanded: $freeExpanded, devices: free)
            }
        }
        .padding(7)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.gray.opacity(0.07)))
    }

    /// One collapsible group: a tappable header (chevron + title + count pill) and,
    /// when expanded, its device rows.
    @ViewBuilder
    private func section(_ title: String, color: Color, expanded: Binding<Bool>,
                         devices: [DeviceAllocation]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Button {
                withAnimation(.easeInOut(duration: 0.16)) { expanded.wrappedValue.toggle() }
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: expanded.wrappedValue ? "chevron.down" : "chevron.right")
                        .font(.system(size: 8, weight: .bold))
                        .foregroundStyle(.secondary).frame(width: 9)
                    Text(title.uppercased()).font(.system(size: 9, weight: .bold))
                        .foregroundStyle(.secondary).kerning(0.5)
                    Text("\(devices.count)").font(.system(size: 9).monospacedDigit())
                        .foregroundStyle(color == .secondary ? Color.secondary : color)
                        .padding(.horizontal, 5).padding(.vertical, 1)
                        .background(Capsule().fill((color == .secondary ? Color.gray : color).opacity(0.15)))
                    Spacer()
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if expanded.wrappedValue {
                ForEach(devices) { dev in
                    DeviceRow(dev: dev, tracked: tracked, onKill: onKill)
                }
            }
        }
    }
}

private struct DeviceRow: View {
    let dev: DeviceAllocation
    var tracked: [TrackedProcess] = []
    var onKill: ((String) -> Void)? = nil

    /// Clickable when an owner PID exists to resolve a terminal for. The actual
    /// (possibly-failing) tty lookup runs on click, never during layout.
    private var focusable: Bool { dev.owner?.ownerPid != nil }
    /// Killable when the device is actually running (allocated or booted-but-free);
    /// a shut-down "free" device has nothing to kill.
    private var killable: Bool { onKill != nil && dev.status != "free" }

    private var platformIcon: String {
        switch dev.platform {
        case "ios":        return "apple.logo"
        case "apple-tv":   return "appletv"
        case "android":    return "candybarphone"
        case "android-tv": return "tv"
        case "vega":       return "flame"
        default:           return "square.dashed"
        }
    }
    private var platformTint: Color {
        switch dev.platform {
        case "ios", "apple-tv":     return .blue
        case "android", "android-tv": return .green
        case "vega":                return .orange
        default:                    return .gray
        }
    }

    private var statusBadge: (text: String, color: Color) {
        switch dev.status {
        case "ready":     return dev.isAllocated ? ("in use", .green) : ("free", .secondary)
        case "booting":   return ("booting", .orange)
        case "repairing": return ("repairing", .purple)
        case "error":     return ("error", .red)
        case "running-free": return ("free", .secondary)
        default:          return ("free", .secondary)
        }
    }

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: platformIcon)
                .font(.system(size: 11, weight: .bold))
                .foregroundStyle(.white)
                .frame(width: 22, height: 22)
                .background(dev.isAllocated ? platformTint : Color.gray.opacity(0.4))
                .clipShape(RoundedRectangle(cornerRadius: 5))
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 4) {
                    Text(dev.name ?? dev.handle ?? dev.key).font(.caption).lineLimit(1)
                    if let v = dev.version { Text(v).font(.system(size: 9)).foregroundStyle(.secondary) }
                    if let f = dev.format { Text(f).font(.system(size: 9)).foregroundStyle(.tertiary) }
                }
                detailLine
            }
            Spacer(minLength: 4)
            if focusable {
                Image(systemName: "macwindow")
                    .font(.system(size: 9)).foregroundStyle(.secondary.opacity(0.7))
                    .help("Focus the terminal running \(dev.owner?.agentName ?? "this agent")")
            }
            Text(statusBadge.text)
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(statusBadge.color)
                .padding(.horizontal, 6).padding(.vertical, 2)
                .background(Capsule().fill(statusBadge.color.opacity(0.14)))
            if killable {
                Button { onKill?(dev.key) } label: {
                    Image(systemName: "xmark.circle.fill").font(.system(size: 12))
                }
                .buttonStyle(.borderless).foregroundStyle(.red.opacity(0.7))
                .help("Kill this device — free it and shut the simulator/emulator down.")
            }
        }
        .padding(6)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.06)))
        .contentShape(Rectangle())
        .onTapGesture { if focusable { DeviceFocus.focus(dev, tracked: tracked) } }
    }

    @ViewBuilder
    private var detailLine: some View {
        if dev.status == "repairing" {
            label(dev.brokenReason.map { "repair: \($0)" } ?? "repair dispatched",
                  "wrench.and.screwdriver", .purple)
        } else if dev.isAllocated, let owner = dev.owner?.agentName {
            HStack(spacing: 5) {
                label(owner, "person.fill", platformTint)
                // How long this device has been held — ticks live via TimelineView so
                // it advances without waiting on a device-state change.
                if let started = dev.allocatedAt {
                    TimelineView(.periodic(from: Date(), by: 30)) { ctx in
                        label("held \(Fmt.duration(ctx.date.timeIntervalSince1970 - started / 1000))",
                              "clock", .secondary)
                    }
                }
                // Idle time, colouring toward red as it nears the 15-min auto-reclaim.
                if let m = idleMinutes {
                    Text("· idle \(m)m").font(.system(size: 9)).foregroundStyle(idleColor(m))
                }
            }
        } else {
            Text(dev.handle ?? "available")
                .font(.system(size: 9, design: .monospaced))
                .foregroundStyle(.secondary).lineLimit(1)
        }
    }

    /// Whole minutes idle (nil under a minute). The daemon floors idleMs to minutes.
    private var idleMinutes: Int? {
        guard let ms = dev.idleMs, ms >= 60_000 else { return nil }
        return Int(ms / 60_000)
    }
    private func idleColor(_ minutes: Int) -> Color {
        if minutes >= 14 { return .red }        // reclaim at 15m — imminent
        if minutes >= 10 { return .orange }
        return .secondary
    }

    private func label(_ text: String, _ symbol: String, _ color: Color) -> some View {
        HStack(spacing: 3) {
            Image(systemName: symbol).font(.system(size: 8))
            Text(text).font(.system(size: 9)).lineLimit(1)
        }
        .foregroundStyle(color)
    }
}

// MARK: - Activity row

private struct AuditRow: View {
    let entry: AuditEntry

    private var sourceColor: Color {
        switch entry.source {
        case "panel": return .blue
        case "auto":  return .green
        case "agent": return .red
        default:      return .secondary
        }
    }
    private var icon: String {
        switch entry.action {
        case "review", "review-req": return "checklist"
        case "conflicts":            return "arrow.triangle.merge"
        case "audit":                return "ladybug.fill"
        case "nudge":                return "bolt.fill"
        case "kill-device":          return "xmark.circle.fill"
        case "unban":                return "hand.raised.slash.fill"
        case "ban":                  return "hand.raised.fill"
        default:                     return "circle.fill"
        }
    }

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: icon).font(.system(size: 10)).foregroundStyle(sourceColor).frame(width: 16)
            Text(entry.detail).font(.system(size: 10)).lineLimit(2)
            Spacer(minLength: 4)
            Text(entry.source).font(.system(size: 8, weight: .bold)).foregroundStyle(sourceColor)
                .padding(.horizontal, 4).padding(.vertical, 1)
                .background(Capsule().fill(sourceColor.opacity(0.15)))
            if let d = entry.date {
                Text(Fmt.clock(d)).font(.system(size: 9, design: .monospaced)).foregroundStyle(.secondary)
            }
        }
        .padding(6)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.05)))
    }
}

// MARK: - Banned-author row

private struct BanRow: View {
    let ban: BannedAuthor
    let onUnban: () -> Void

    private var evidenceLine: String {
        let e = (ban.evidence ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return e.isEmpty ? (ban.reason ?? "prompt injection") : e
    }

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "hand.raised.slash.fill")
                .font(.system(size: 11, weight: .bold)).foregroundStyle(.white)
                .frame(width: 22, height: 22)
                .background(Color.red.opacity(0.7))
                .clipShape(RoundedRectangle(cornerRadius: 5))
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 4) {
                    Text("@\(ban.login)").font(.caption).lineLimit(1)
                    if let pr = ban.pr, !pr.isEmpty {
                        Text(pr).font(.system(size: 9)).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
                Text(evidenceLine).font(.system(size: 9)).foregroundStyle(.secondary).lineLimit(2)
            }
            Spacer(minLength: 4)
            if let dir = ban.evidenceDir, !dir.isEmpty {
                Button { NSWorkspace.shared.open(URL(fileURLWithPath: dir)) } label: {
                    Image(systemName: "doc.text.magnifyingglass")
                }
                .buttonStyle(.borderless).foregroundStyle(.secondary)
                .help("Open the captured evidence (gh content\(ban.screenshot == true ? " + screenshot" : ""))")
            }
            Button(action: onUnban) { Image(systemName: "xmark.circle.fill") }
                .buttonStyle(.borderless).foregroundStyle(.secondary).help("Un-ban @\(ban.login)")
        }
        .padding(6)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.06)))
    }
}

// MARK: - Result row

/// The per-row action on an Approved PR: green "Merge" normally, or blue "Resolve
/// conflicts" when the PR conflicts with its base. Merge shows a spinner while in flight.
private struct MergeButton: View {
    let conflicts: Bool
    let busy: Bool
    let onMerge: () -> Void
    let onResolve: () -> Void

    var body: some View {
        Button(action: conflicts ? onResolve : onMerge) {
            HStack(spacing: 4) {
                if busy {
                    ProgressView().controlSize(.mini).tint(.white)
                } else {
                    Image(systemName: conflicts ? "arrow.triangle.merge" : "checkmark.circle.fill")
                        .font(.system(size: 10, weight: .bold))
                }
                Text(conflicts ? "Resolve conflicts" : "Merge")
                    .font(.system(size: 10, weight: .semibold))
            }
            .foregroundStyle(.white)
            .padding(.vertical, 6)
            .padding(.horizontal, 9)
            .background(RoundedRectangle(cornerRadius: 6).fill(conflicts ? Color.blue : Color.green))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(busy)
        .help(conflicts ? "Merge main into this PR and resolve the conflicts."
                        : "Squash-merge this PR from here.")
    }
}

private struct ResultRow: View {
    let item: DisplayItem
    let tint: Color

    var body: some View {
        Button {
            if let u = URL(string: item.url) { NSWorkspace.shared.open(u) }
        } label: {
            HStack(alignment: .top, spacing: 6) {
                Text(item.badge)
                    .font(.caption.bold().monospaced())
                    .foregroundStyle(tint)
                    .frame(width: 40, alignment: .leading)
                VStack(alignment: .leading, spacing: 1) {
                    Text(item.title).font(.caption).lineLimit(2)
                    Text(item.line2).font(.system(size: 9)).foregroundStyle(.secondary)
                    if let l3 = item.line3 {
                        Text(l3)
                            .font(.system(size: 9, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .lineLimit(8)
                    }
                }
                Spacer(minLength: 0)
                Image(systemName: "arrow.up.forward.square")
                    .font(.system(size: 9))
                    .foregroundStyle(.tertiary)
            }
            .padding(6)
            .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.06)))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("Open #\(item.id) in browser")
    }
}
