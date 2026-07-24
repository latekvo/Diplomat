import SwiftUI
import AppKit
import DiplomatCore
import ObjectiveC

/// Sizes the menu-bar popover to its content, capped at the screen's safe area so it
/// never spills past the menu bar (top) or the Dock — the "bottom bar" (bottom). The
/// content lays out at its natural height; once that would exceed the cap the popover
/// stops growing and the content scrolls instead of running off-screen.
struct PopoverRoot: View {
    /// Fixed popover width. Two-column layout: the left column holds the monitoring
    /// lists (status, bans, sessions, devices, activity), the right every interactive
    /// surface (search, tool grid, results/lookup, action wizards).
    static let width: CGFloat = 1120
    /// Width of each of the two columns: (width − 12pt column gap − 2×10pt outer
    /// padding) / 2. The old 536 left 16pt of slack, so the columns sat inset from
    /// the header's edges.
    static let columnWidth: CGFloat = (width - 12 - 20) / 2

    /// The content's measured natural height. Seeded with a sane default so the very
    /// first frame isn't zero-height; corrected on the first layout pass.
    @State private var contentHeight: CGFloat = 600

    /// Usable height of the display the popover currently sits on (`visibleFrame` already
    /// excludes the menu bar + Dock). Reported by `WindowCenterer` from the window's ACTUAL
    /// screen — not `NSScreen.main`, which tracks the key-window's screen and can point at
    /// a taller display than the one the popover opens on, letting the content grow past
    /// what fits and spill off the bottom.
    @State private var displayVisibleHeight: CGFloat = NSScreen.main?.visibleFrame.height ?? 800

    /// The user's scroller preference ("Show scroll bars: Always" ⇒ `.legacy`), tracked
    /// live because it can be flipped in System Settings while we run.
    @State private var scrollerStyle = NSScroller.preferredScrollerStyle

    /// Cap the popover at the display's usable height, less a small margin so it never
    /// kisses the menu bar or the Dock. Content beyond this scrolls.
    /// `DIPLOMAT_POPOVER_CAP` forces a small cap so the scrolling state is
    /// reproducible in the `popover` render self-test.
    private var cap: CGFloat {
        if let s = ProcessInfo.processInfo.environment["DIPLOMAT_POPOVER_CAP"],
           let v = Double(s) { return CGFloat(v) }
        return max(320, displayVisibleHeight - 12)
    }

    /// Legacy (always-on) scroll bars sit INSIDE the window and steal width from the
    /// viewport: the fixed 1120pt content then gets centre-clipped ~8pt per side and the
    /// panel visually loses its left margin. When the content actually scrolls under a
    /// legacy scroller, widen the window by the scroller's width so the content keeps
    /// its full lane and the scroller gets its own.
    private var scrollerInset: CGFloat {
        guard contentHeight > cap, scrollerStyle == .legacy else { return 0 }
        return NSScroller.scrollerWidth(for: .regular, scrollerStyle: .legacy)
    }

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
        .frame(width: PopoverRoot.width + scrollerInset, height: min(contentHeight, cap))
        .onPreferenceChange(ContentHeightKey.self) { h in
            if h > 1, abs(h - contentHeight) > 0.5 { contentHeight = h }
            // Re-read alongside the height: the init-time read can predate AppKit
            // having the real preference (it reports .overlay very early in launch).
            let style = NSScroller.preferredScrollerStyle
            if style != scrollerStyle { scrollerStyle = style }
        }
        .onReceive(NotificationCenter.default.publisher(
            for: NSScroller.preferredScrollerStyleDidChangeNotification)) { _ in
            scrollerStyle = NSScroller.preferredScrollerStyle
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
    /// Which of the three screens the body shows: Actions ("main"), Settings, or Mesh.
    @State private var screen: PanelScreen

    /// The panel's three interchangeable body screens.
    enum PanelScreen { case main, settings, mesh }

    /// `showSettings` seeds the initial screen — used by the headless render
    /// to snapshot the Settings screen in context. Defaults to the main view.
    /// `seedPendingUnban` seeds the inline un-ban confirmation for a login (render only).
    /// `seedMutedAudit` pre-mutes Activity filter categories so the filtered feed can be
    /// snapshotted (render only).
    init(showSettings: Bool = false, seedPendingUnban: String? = nil,
         seedMutedAudit: Set<AuditCategory> = []) {
        _screen = State(initialValue: showSettings ? .settings : .main)
        _pendingUnban = State(initialValue: seedPendingUnban)
        _mutedAuditCategories = State(initialValue: seedMutedAudit)
    }

    /// Binding that reads true while a given screen is up and returns to main when it's
    /// dismissed — bridges the child screens' `isPresented: Binding<Bool>` to `screen`.
    private func presenting(_ s: PanelScreen) -> Binding<Bool> {
        Binding(get: { screen == s }, set: { if !$0 { screen = .main } })
    }
    /// Which action wizard (if any) replaces the tool lists in the results pane.
    @State private var activeAction: ActionPanel?
    /// Whether the prompt-injection ban list (above the sessions) is expanded.
    @State private var bannedExpanded = true
    /// Whether the activity/audit log is expanded.
    @State private var auditExpanded = true
    /// Activity categories the user has toggled OFF via the filter chips. Empty ⇒ show
    /// everything (the default), so a category that only appears later still shows until
    /// it's explicitly muted. Persisted for the popover's lifetime, reset on relaunch.
    @State private var mutedAuditCategories: Set<AuditCategory> = []
    /// The login whose un-ban is awaiting inline confirmation (nil = none). Kept inline
    /// in the popover rather than an NSAlert, which would open behind the menu-bar window.
    @State private var pendingUnban: String?
    @FocusState private var searchFocused: Bool

    /// The action cards in the grid that open a wizard instead of selecting a tool.
    private enum ActionPanel: Hashable { case review, conflicts, audit }

    var body: some View {
        VStack(spacing: 8) {
            header
            switch screen {
            case .settings:
                // Settings uses the same two-column layout as the main panel below.
                SettingsView(isPresented: presenting(.settings))
                    .frame(maxWidth: .infinity, alignment: .leading)
            case .mesh:
                MeshView(isPresented: presenting(.mesh))
                    .frame(maxWidth: .infinity, alignment: .leading)
            case .main:
                HStack(alignment: .top, spacing: 12) {
                    leftColumn.frame(width: PopoverRoot.columnWidth, alignment: .top)
                    rightColumn.frame(width: PopoverRoot.columnWidth, alignment: .top)
                }
            }
        }
        .padding(10)
        .background(cmdFCatcher)
        .animation(.easeInOut(duration: 0.18), value: store.processes)
        .onChange(of: store.bannedAuthors) { bans in
            // The pending "Unban @X?" confirmation must not outlive the ban itself
            // (un-banned elsewhere / list rewritten) — a stale login would open the
            // confirm unprompted if that author were ever re-banned.
            if let p = pendingUnban, !bans.contains(where: { $0.login == p }) {
                pendingUnban = nil
            }
        }
        .task {
            // Optional: launch pre-focused on a specific number (also used for headless UI checks).
            if query.isEmpty, let pre = ProcessInfo.processInfo.environment["DIPLOMAT_PREFILL"], !pre.isEmpty {
                query = pre
                searchFocused = true
            }
            if !store.hasLoaded { await store.refresh() }
        }
    }

    // MARK: header

    private var header: some View {
        let (owner, repo) = CoreAssets.repoCoordinates()
        return HStack(spacing: 6) {
            Image(systemName: "wrench.and.screwdriver.fill").foregroundStyle(.blue)
            Text("Diplomat").font(.headline)
            Text("\(owner)/\(repo)").font(.caption2).foregroundStyle(.secondary)
            Spacer()
            if store.isLoading {
                ProgressView().controlSize(.small)
            }
            Text("upd \(Fmt.clock(store.lastUpdated))").font(.caption2).foregroundStyle(.secondary)
            Button { Task { await store.refresh() } } label: {
                Image(systemName: "arrow.clockwise")
            }.buttonStyle(.borderless).help("Refresh")
            Button { withAnimation(.easeInOut(duration: 0.15)) { screen = screen == .mesh ? .main : .mesh } } label: {
                Image(systemName: screen == .mesh ? "hexagon.fill" : "hexagon")
                    .foregroundStyle(screen == .mesh ? Color.accentColor : .primary)
            }.buttonStyle(.borderless).help("Mesh management")
            Button { withAnimation(.easeInOut(duration: 0.15)) { screen = screen == .settings ? .main : .settings } } label: {
                Image(systemName: screen == .settings ? "gearshape.fill" : "gearshape")
                    .foregroundStyle(screen == .settings ? Color.accentColor : .primary)
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
                withAnimation(.easeInOut(duration: 0.15)) { screen = .settings }
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
            SectionHeader(title: "BANNED", count: bans.count, expanded: $bannedExpanded,
                          countTint: .red, icon: "hand.raised.fill", iconTint: .red,
                          caption: "prompt injection · no auto-reviews")
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
            IconBadge(symbol: "questionmark.circle.fill", tint: Color.orange.opacity(0.85))
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
    /// or dispatched automatically (or reported by an agent). Newest first, filterable by
    /// activity type via the toggle chips.
    @ViewBuilder
    private func auditList(_ entries: [AuditEntry]) -> some View {
        // Per-category counts over the whole feed drive both the chips and their badges.
        let counts = Dictionary(grouping: entries) { AuditCategory.of(action: $0.action) }
            .mapValues(\.count)
        let present = AuditCategory.displayOrder.filter { counts[$0] != nil }
        let visible = entries.filter { !mutedAuditCategories.contains(AuditCategory.of(action: $0.action)) }
        VStack(alignment: .leading, spacing: 4) {
            SectionHeader(title: "ACTIVITY", count: visible.count, expanded: $auditExpanded,
                          icon: "list.bullet.rectangle",
                          caption: mutedAuditCategories.isEmpty ? nil : "filtered")
            if auditExpanded {
                // Only worth filtering when there's more than one type in view.
                if present.count > 1 { auditFilterChips(present, counts: counts) }
                ForEach(visible.prefix(30)) { entry in AuditRow(entry: entry) }
            }
        }
        .padding(7)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.gray.opacity(0.07)))
    }

    /// Wrapping row of toggle chips, one per activity type present in the feed. A lit chip
    /// (tinted) includes that type; tapping mutes it (dimmed) and drops its rows.
    @ViewBuilder
    private func auditFilterChips(_ present: [AuditCategory], counts: [AuditCategory: Int]) -> some View {
        FlowLayout(spacing: 4) {
            ForEach(present, id: \.self) { cat in
                let on = !mutedAuditCategories.contains(cat)
                Button {
                    if on { mutedAuditCategories.insert(cat) } else { mutedAuditCategories.remove(cat) }
                } label: {
                    HStack(spacing: 3) {
                        Image(systemName: cat.symbol).font(.system(size: 8, weight: .bold))
                        Text(cat.title).font(.system(size: 9, weight: .medium))
                        Text("\(counts[cat] ?? 0)").font(.system(size: 8).monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .foregroundStyle(on ? auditCategoryTint(cat) : Color.secondary)
                    .background(Capsule().fill((on ? auditCategoryTint(cat) : Color.gray).opacity(on ? 0.16 : 0.08)))
                    .overlay(Capsule().stroke(on ? auditCategoryTint(cat).opacity(0.5) : .clear, lineWidth: 1))
                    .opacity(on ? 1 : 0.55)
                    .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .help(on ? "Hide \(cat.title.lowercased())" : "Show \(cat.title.lowercased())")
            }
        }
        .padding(.bottom, 2)
    }

    /// Chip tint per activity type. Mirrors the tool/session palette so a category reads
    /// the same color it does elsewhere in the panel (reviews pink, conflicts cyan, …).
    private func auditCategoryTint(_ cat: AuditCategory) -> Color {
        switch cat {
        case .review:     return .pink
        case .reply:      return .purple
        case .conflicts:  return .cyan
        case .audit:      return .indigo
        case .apiRestart: return .orange
        case .quota:      return .yellow
        case .merge:      return .green
        case .bans:       return .red
        case .mesh:       return .teal
        case .system:     return .gray
        }
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
                let tint = store.tint(for: kind)
                GridCard(systemImage: kind.systemImage, title: kind.title,
                         subtitle: kind.subtitle, tint: tint,
                         selected: store.selected == kind && activeAction == nil,
                         action: { activeAction = nil; store.selected = kind }) {
                    Text(store.hasLoaded ? String(store.count(for: kind)) : "…")
                        .font(.callout.bold().monospacedDigit())
                        .foregroundStyle(tint)
                }
            }
            actionCard("checklist", "Review PRs", "spawn a review agent", .pink, .review)
            actionCard("arrow.triangle.merge", "Resolve conflicts", "merge main, fix conflicts",
                       .cyan, .conflicts)
            actionCard("ladybug.fill", "Full E2E test", "swarm-test the whole repo",
                       .indigo, .audit)
        }
    }

    private func actionCard(_ symbol: String, _ title: String, _ subtitle: String,
                            _ tint: Color, _ panel: ActionPanel) -> some View {
        GridCard(systemImage: symbol, title: title, subtitle: subtitle, tint: tint,
                 selected: activeAction == panel, action: { activeAction = panel }) {
            Image(systemName: "chevron.right")
                .font(.system(size: 11, weight: .bold))
                .foregroundStyle(activeAction == panel ? tint : .secondary)
        }
    }

    // MARK: two columns — lists on the left, interactive controls on the right

    /// Left column: the monitoring lists — status pill, ban list, agent sessions,
    /// devices, activity log. (Results/lookup live in the right column.)
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

    /// The lookup / tool-result list (right column) shown when no wizard is open.
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
        let (owner, repo) = CoreAssets.repoCoordinates()
        let link = r.url ?? "https://github.com/\(owner)/\(repo)/issues/\(n)"
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
            IconBadge(symbol: kind.systemImage, tint: on ? tint : Color.gray.opacity(0.35))
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
                                        // Busy while merging OR while a Resolve spawn is in
                                        // flight — the Resolve variant used to have no busy
                                        // state at all, so a double-click raced two agents.
                                        busy: store.mergingPRs.contains(pr.number)
                                            || store.resolvingPRs.contains(pr.number),
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
                    IconBadge(symbol: kindIcon, tint: tint)
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
        // process merely exited; the PR may still be open). A live session that has
        // finished its turn and is idling at the prompt reads "awaiting input" (amber,
        // it needs you) rather than "running".
        if proc.merged {
            label("merged", "arrow.triangle.merge", .purple)
        } else if proc.done {
            label("done", "checkmark.circle.fill", .green)
        } else if proc.awaitingInput {
            label("awaiting input", "ellipsis.circle.fill", .orange)
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

    /// One collapsible group: the shared section header and, when expanded, its
    /// device rows.
    @ViewBuilder
    private func section(_ title: String, color: Color, expanded: Binding<Bool>,
                         devices: [DeviceAllocation]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            SectionHeader(title: title.uppercased(), count: devices.count,
                          expanded: expanded, countTint: color)
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
            IconBadge(symbol: platformIcon,
                      tint: dev.isAllocated ? platformTint : Color.gray.opacity(0.4))
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
        .onTapGesture {
            // Off-main: the focus path is one `ps` + up to two synchronous osascript
            // round-trips — running it in the gesture handler froze the popover
            // (its tracked-row sibling has always detached for the same reason).
            guard focusable else { return }
            let d = dev, t = tracked
            Task.detached(priority: .userInitiated) { DeviceFocus.focus(d, tracked: t) }
        }
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
        return clampedInt(ms / 60_000)
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
        case "review-reply":         return "arrowshape.turn.up.left.fill"
        case "conflicts":            return "arrow.triangle.merge"
        case "audit":                return "ladybug.fill"
        case "nudge":                return "bolt.fill"
        case "quota-stall":          return "hourglass"
        case "merge":                return "checkmark.seal.fill"
        case "kill-device":          return "xmark.circle.fill"
        case "unban":                return "hand.raised.slash.fill"
        case "ban":                  return "hand.raised.fill"
        case "repair-done":          return "wrench.and.screwdriver"
        case "allocator-install", "allocator-uninstall": return "shippingbox.fill"
        case "merge-failed", "spawn-failed", "poll-failed", "warn":
            return "exclamationmark.triangle.fill"
        case "poll-recovered":       return "checkmark.circle.fill"
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
            IconBadge(symbol: "hand.raised.slash.fill", tint: Color.red.opacity(0.7))
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
