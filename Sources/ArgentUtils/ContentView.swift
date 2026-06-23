import SwiftUI
import AppKit
import ArgentUtilsCore

struct ContentView: View {
    @EnvironmentObject var store: Store
    @State private var query = ""
    @State private var showingSettings = false
    @State private var showingReviewWizard = false
    @FocusState private var searchFocused: Bool

    var body: some View {
        VStack(spacing: 8) {
            header
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
                    selected: store.selected == kind && !showingReviewWizard
                )
                .onTapGesture { showingReviewWizard = false; store.selected = kind }
            }
            ActionCard(
                systemImage: "checklist",
                title: "Review PRs",
                subtitle: "spawn a review agent",
                tint: .pink,
                selected: showingReviewWizard
            )
            .onTapGesture { showingReviewWizard = true }
        }
    }

    // MARK: results pane (lookup when searching, else the selected tool's list)

    @ViewBuilder
    private var resultsPane: some View {
        let trimmed = query.trimmingCharacters(in: .whitespaces)
        if showingReviewWizard {
            ReviewWizardView()
        } else if !trimmed.isEmpty, let n = Int(trimmed) {
            ScrollView { lookupView(n) }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        } else if !trimmed.isEmpty {
            Text("Type a PR or issue number.")
                .font(.caption).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
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
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    ScrollView {
                        VStack(spacing: 4) {
                            ForEach(items) { item in
                                ResultRow(item: item, tint: tint)
                            }
                        }
                    }
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        } else {
            VStack(spacing: 6) {
                Image(systemName: "eye.slash").font(.title3).foregroundStyle(.secondary)
                Text("All tools hidden").font(.caption.bold()).foregroundStyle(.secondary)
                Text("Re-enable some under ⚙︎ Settings.").font(.caption2).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
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
