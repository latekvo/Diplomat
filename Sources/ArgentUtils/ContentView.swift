import SwiftUI
import AppKit
import ArgentUtilsCore

/// Sizes the menu-bar popover to its content, capped at the screen's safe area so it
/// never spills past the menu bar (top) or the Dock — the "bottom bar" (bottom). The
/// content lays out at its natural height; once that would exceed the cap the popover
/// stops growing and the content scrolls instead of running off-screen.
struct PopoverRoot: View {
    /// Fixed popover width. Widened from the original 470 to give the Devices section
    /// room for a device name, its holder, and a status badge on one line.
    static let width: CGFloat = 560

    /// The content's measured natural height. Seeded with a sane default so the very
    /// first frame isn't zero-height; corrected on the first layout pass.
    @State private var contentHeight: CGFloat = 600

    /// Usable vertical space on the menu-bar screen. `visibleFrame` already excludes
    /// the menu bar and the Dock, so capping here keeps the popover off both; a small
    /// margin stops it from kissing either edge. Falls back to a safe default.
    private var cap: CGFloat {
        let visible = NSScreen.main?.visibleFrame.height ?? 800
        return max(320, visible - 12)
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
        .frame(width: PopoverRoot.width, height: min(contentHeight, cap))
        .onPreferenceChange(ContentHeightKey.self) { h in
            if h > 1, abs(h - contentHeight) > 0.5 { contentHeight = h }
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
    @State private var showingSettings = false
    /// Which action wizard (if any) replaces the tool lists in the results pane.
    @State private var activeAction: ActionPanel?
    /// Rows whose last click couldn't be focused or opened — show "tracking lost".
    @State private var lostProcessIDs: Set<UUID> = []
    @FocusState private var searchFocused: Bool

    /// The action cards in the grid that open a wizard instead of selecting a tool.
    private enum ActionPanel: Hashable { case review, conflicts, audit }

    var body: some View {
        VStack(spacing: 8) {
            header
            // Ongoing agent sessions live at the very top, above everything else,
            // and vanish entirely when there are none.
            if !store.processes.isEmpty { processList }
            // The shared device pool + who holds what, just below the sessions.
            if let ds = store.deviceState, !ds.devices.isEmpty { devicesList(ds) }
            if showingSettings {
                SettingsView(isPresented: $showingSettings)
            } else {
                searchBar
                if let err = store.error { errorBanner(err) }
                toolGrid
                Divider()
                resultsPane
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
                    lost: lostProcessIDs.contains(proc.id),
                    onTap: { activate(proc) },
                    onRemove: {
                        lostProcessIDs.remove(proc.id)
                        store.removeProcess(proc.id)
                    }
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

    /// Click a tracked row: focus its window → else open its PR → else mark it lost.
    private func activate(_ proc: TrackedProcess) {
        lostProcessIDs.remove(proc.id)
        Task {
            if await store.activate(proc) == .lost {
                lostProcessIDs.insert(proc.id)
            }
        }
    }

    // MARK: device allocator pool

    private func devicesList(_ ds: DeviceState) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 5) {
                Image(systemName: "iphone")
                    .font(.system(size: 9)).foregroundStyle(.secondary)
                Text("Devices").font(.system(size: 10, weight: .bold))
                    .foregroundStyle(.secondary)
                Text("\(ds.allocatedCount) in use · \(ds.freeCount) free")
                    .font(.system(size: 10).monospacedDigit()).foregroundStyle(.secondary)
                Spacer()
            }
            ForEach(sortedDevices(ds.devices)) { dev in
                DeviceRow(dev: dev)
            }
        }
        .padding(7)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.gray.opacity(0.07)))
    }

    /// Busy devices first (what's in use is what you care about), then by platform+name.
    private func sortedDevices(_ d: [DeviceAllocation]) -> [DeviceAllocation] {
        d.sorted { a, b in
            if a.isAllocated != b.isAllocated { return a.isAllocated }
            if a.platform != b.platform { return a.platform < b.platform }
            return (a.name ?? "") < (b.name ?? "")
        }
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

    // MARK: results pane (lookup when searching, else the selected tool's list)

    @ViewBuilder
    private var resultsPane: some View {
        let trimmed = query.trimmingCharacters(in: .whitespaces)
        if let activeAction {
            // The wizards size to their own content (scrolls: false); PopoverRoot's
            // outer scroll view handles any overflow, so they never nest a scroller.
            switch activeAction {
            case .review:    ReviewWizardView(scrolls: false)
            case .conflicts: ConflictWizardView(scrolls: false)
            case .audit:     AuditWizardView(scrolls: false)
            }
        } else if !trimmed.isEmpty, let n = Int(trimmed) {
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
                            ResultRow(item: item, tint: tint)
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
    let lost: Bool
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
            .help(lost ? "Tracking lost — the window and PR couldn't be reached."
                       : "Bring this session's window to the front.")

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
        // "merged" is the definitive outcome — it outranks both "done" (the local
        // claude process merely exited) and a transient click-time "tracking lost".
        if proc.merged {
            label("merged", "arrow.triangle.merge", .purple)
        } else if lost {
            label("tracking lost", "questionmark.circle", .orange)
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

private struct DeviceRow: View {
    let dev: DeviceAllocation

    private var platformIcon: String { dev.platform == "ios" ? "apple.logo" : "candybarphone" }
    private var platformTint: Color { dev.platform == "ios" ? .blue : .green }

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
                }
                detailLine
            }
            Spacer(minLength: 4)
            Text(statusBadge.text)
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(statusBadge.color)
                .padding(.horizontal, 6).padding(.vertical, 2)
                .background(Capsule().fill(statusBadge.color.opacity(0.14)))
        }
        .padding(6)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.06)))
    }

    @ViewBuilder
    private var detailLine: some View {
        if dev.status == "repairing" {
            label(dev.brokenReason.map { "repair: \($0)" } ?? "repair dispatched",
                  "wrench.and.screwdriver", .purple)
        } else if dev.isAllocated, let owner = dev.owner?.agentName {
            HStack(spacing: 4) {
                label(owner, "person.fill", platformTint)
                if let idle = idleText {
                    Text(idle).font(.system(size: 9)).foregroundStyle(.secondary)
                }
            }
        } else {
            Text(dev.handle ?? "available")
                .font(.system(size: 9, design: .monospaced))
                .foregroundStyle(.secondary).lineLimit(1)
        }
    }

    private var idleText: String? {
        guard let ms = dev.idleMs, ms > 60_000 else { return nil }
        return "· idle \(Int(ms / 60_000))m"
    }

    private func label(_ text: String, _ symbol: String, _ color: Color) -> some View {
        HStack(spacing: 3) {
            Image(systemName: symbol).font(.system(size: 8))
            Text(text).font(.system(size: 9)).lineLimit(1)
        }
        .foregroundStyle(color)
    }
}

// MARK: - Result row

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
