import SwiftUI

// Small shared UI atoms. Each of these existed as 3-8 hand-copied blocks across
// ContentView/SettingsView that had already started drifting (font sizes, opacities,
// capsule colors); one definition freezes the drift.

/// The recurring rounded icon tile: a bold white SF Symbol on a tinted rounded
/// rectangle. `size` 22 is the row variant (font 11 / radius 5); 26 is the grid-card
/// variant (font 13 / radius 6). Pass any opacity baked into `tint`.
struct IconBadge: View {
    let symbol: String
    let tint: Color
    var size: CGFloat = 22

    var body: some View {
        Image(systemName: symbol)
            .font(.system(size: size >= 26 ? 13 : 11, weight: .bold))
            .foregroundStyle(.white)
            .frame(width: size, height: size)
            .background(tint)
            .clipShape(RoundedRectangle(cornerRadius: size >= 26 ? 6 : 5))
    }
}

/// The collapsible-section header: chevron, optional leading glyph, caps title,
/// count capsule, optional trailing caption. Tapping anywhere toggles `expanded`
/// with the shared ease-in-out.
struct SectionHeader: View {
    let title: String
    let count: Int
    @Binding var expanded: Bool
    /// Tint for the count text/capsule; `.secondary` renders the neutral gray capsule.
    var countTint: Color = .secondary
    var icon: String? = nil
    var iconTint: Color = .secondary
    var caption: String? = nil

    var body: some View {
        Button {
            withAnimation(.easeInOut(duration: 0.16)) { expanded.toggle() }
        } label: {
            HStack(spacing: 5) {
                Image(systemName: expanded ? "chevron.down" : "chevron.right")
                    .font(.system(size: 8, weight: .bold)).foregroundStyle(.secondary).frame(width: 9)
                if let icon {
                    Image(systemName: icon).font(.system(size: 9)).foregroundStyle(iconTint)
                }
                Text(title).font(.system(size: 9, weight: .bold))
                    .foregroundStyle(.secondary).kerning(0.5)
                Text("\(count)").font(.system(size: 9).monospacedDigit())
                    .foregroundStyle(countTint == .secondary ? Color.secondary : countTint)
                    .padding(.horizontal, 5).padding(.vertical, 1)
                    .background(Capsule().fill((countTint == .secondary ? Color.gray : countTint).opacity(0.15)))
                if let caption {
                    Text(caption).font(.system(size: 9)).foregroundStyle(.secondary)
                }
                Spacer()
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

/// One grid-cell card: icon badge, title/subtitle, and a caller-supplied trailing
/// view (a count for tool cards, a chevron for action cards). A real Button — the
/// cards used to be plain views with `.onTapGesture`, which gave the panel's primary
/// navigation no keyboard focus, no VoiceOver button trait, and no pressed feedback.
struct GridCard<Trailing: View>: View {
    let systemImage: String
    let title: String
    let subtitle: String
    let tint: Color
    let selected: Bool
    let action: () -> Void
    @ViewBuilder var trailing: () -> Trailing

    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                IconBadge(symbol: systemImage, tint: tint, size: 26)
                VStack(alignment: .leading, spacing: 1) {
                    Text(title).font(.caption.bold()).lineLimit(1)
                    Text(subtitle).font(.system(size: 9)).foregroundStyle(.secondary).lineLimit(2)
                }
                Spacer(minLength: 2)
                trailing()
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
        .buttonStyle(.plain)
    }
}
