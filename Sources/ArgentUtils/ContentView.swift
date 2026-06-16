import SwiftUI
import AppKit

struct ContentView: View {
    @EnvironmentObject var store: Store

    var body: some View {
        VStack(spacing: 8) {
            header
            if let err = store.error { errorBanner(err) }
            toolGrid
            Divider()
            results
        }
        .padding(10)
        .task { if !store.hasLoaded { await store.refresh() } }
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
            Button { NSApp.terminate(nil) } label: {
                Image(systemName: "power")
            }.buttonStyle(.borderless).help("Quit")
        }
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
            ForEach(ToolKind.allCases) { kind in
                ToolCard(
                    kind: kind,
                    count: store.hasLoaded ? store.count(for: kind) : nil,
                    selected: store.selected == kind
                )
                .onTapGesture { store.selected = kind }
            }
        }
    }

    // MARK: results

    private var results: some View {
        let items = store.items(for: store.selected)
        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Image(systemName: store.selected.systemImage).foregroundStyle(store.selected.tint)
                Text(store.selected.title).font(.subheadline.bold())
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
                            ResultRow(item: item, tint: store.selected.tint)
                        }
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

// MARK: - Tool card

private struct ToolCard: View {
    let kind: ToolKind
    let count: Int?
    let selected: Bool

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: kind.systemImage)
                .font(.system(size: 13, weight: .bold))
                .foregroundStyle(.white)
                .frame(width: 26, height: 26)
                .background(kind.tint)
                .clipShape(RoundedRectangle(cornerRadius: 6))
            VStack(alignment: .leading, spacing: 1) {
                Text(kind.title).font(.caption.bold()).lineLimit(1)
                Text(kind.subtitle).font(.system(size: 9)).foregroundStyle(.secondary).lineLimit(2)
            }
            Spacer(minLength: 2)
            Text(count.map(String.init) ?? "…")
                .font(.callout.bold().monospacedDigit())
                .foregroundStyle(kind.tint)
        }
        .padding(7)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(selected ? kind.tint.opacity(0.16) : Color.gray.opacity(0.08))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(selected ? kind.tint : .clear, lineWidth: 1.2)
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
