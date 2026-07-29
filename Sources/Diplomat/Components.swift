import SwiftUI
import DiplomatCore

// Small shared UI atoms. Each of these existed as 3-8 hand-copied blocks across
// ContentView/SettingsView and the three spawn wizards that had already started
// drifting (font sizes, opacities, capsule colors); one definition freezes the drift.

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

/// A left-to-right flow layout that wraps to the next line when the row is full — used
/// for the Activity filter chips, whose count and width vary with the feed. SwiftUI has
/// no built-in wrapping stack; this is the minimal `Layout` conformance (macOS 13+).
struct FlowLayout: Layout {
    var spacing: CGFloat = 4

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout Void) -> CGSize {
        let maxWidth = proposal.width ?? .infinity
        var rowWidth: CGFloat = 0, rowHeight: CGFloat = 0
        var totalWidth: CGFloat = 0, totalHeight: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            // Wrap: this subview would overflow the current row.
            if rowWidth > 0, rowWidth + spacing + size.width > maxWidth {
                totalWidth = max(totalWidth, rowWidth)
                totalHeight += rowHeight + spacing
                rowWidth = 0
                rowHeight = 0
            }
            rowWidth += (rowWidth > 0 ? spacing : 0) + size.width
            rowHeight = max(rowHeight, size.height)
        }
        totalWidth = max(totalWidth, rowWidth)
        totalHeight += rowHeight
        return CGSize(width: totalWidth, height: totalHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout Void) {
        var x = bounds.minX, y = bounds.minY, rowHeight: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > bounds.minX, x + size.width > bounds.maxX {
                x = bounds.minX
                y += rowHeight + spacing
                rowHeight = 0
            }
            view.place(at: CGPoint(x: x, y: y), anchor: .topLeading,
                       proposal: ProposedViewSize(size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
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

// MARK: - Spawn-wizard chrome

// The Review / Resolve-conflicts / Full-E2E wizards are three renderers over one
// layout: title, contextual rows, a mesh row + SPAWN button, a status line. Each
// of the pieces below was hand-copied into two or three of them, and the copies
// had begun to disagree (the escalation toggle's fill alpha differed between the
// audit's two toggles and the review's final-pass row). Each is a concrete little
// View, deliberately not a generic scaffold: SwiftUI type-checks a ViewBuilder
// body as one expression, and the app target only compiles in macOS CI, so a
// clever wrapper that builds locally can still time out there.

/// A wizard's heading: the tool's glyph in its tint, then the name.
struct WizardTitle: View {
    let systemImage: String
    let title: String
    let tint: Color

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: systemImage).foregroundStyle(tint)
            Text(title).font(.subheadline.bold())
            Spacer()
        }
    }
}

/// The grey explainer paragraph under a wizard's heading or a toggle.
struct WizardBlurb: View {
    let text: String

    init(_ text: String) { self.text = text }

    var body: some View {
        Text(text)
            .font(.system(size: 10))
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
    }
}

/// The monospaced line under SPAWN that reports what the click did.
struct WizardStatusLine: View {
    let message: String

    init(_ message: String) { self.message = message }

    var body: some View {
        Text(message)
            .font(.system(size: 10, design: .monospaced))
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// One boxed text field with a leading glyph — the "github username" and
/// "PR # or URL" inputs, which share a slot and never show together.
struct WizardTextField: View {
    let systemImage: String
    let placeholder: String
    @Binding var text: String

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: systemImage).font(.caption2).foregroundStyle(.secondary)
            TextField(placeholder, text: $text)
                .textFieldStyle(.plain)
                .font(.callout)
        }
        .padding(6)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.1)))
    }
}

/// The whose-PRs segmented picker, plus the @handle caption shown for "mine".
/// Shared by the Review and Resolve-conflicts wizards, which sweep the same axis.
struct WizardTargetPicker: View {
    @Binding var target: PRTarget
    /// The authenticated viewer login; the caption is omitted while it is empty.
    let me: String

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Picker("", selection: $target) {
                ForEach(PRTarget.allCases) { t in
                    Text(t.title).tag(t)
                }
            }
            .labelsHidden()
            .pickerStyle(.segmented)

            if target == .mine, !me.isEmpty {
                Text("PRs authored by @\(me)")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
    }
}

/// A scope-escalating checkbox: highlighted because ticking it lets the swarm
/// change code or GitHub state, well beyond the default read-only run. The box
/// deepens and its border thickens while on.
///
/// `fill` is the only per-wizard difference — the review's final pass reads yellow,
/// the audit's escalations orange. The alphas are shared: they used to differ by
/// 0.02 between the two copies, which was drift, not design.
struct EscalationToggle: View {
    @Binding var isOn: Bool
    let systemImage: String
    let title: String
    let help: String
    var fill: Color = .orange

    var body: some View {
        Toggle(isOn: $isOn) {
            HStack(spacing: 6) {
                Image(systemName: systemImage).foregroundStyle(.orange)
                Text(title).font(.caption.bold())
                Spacer(minLength: 0)
            }
        }
        .toggleStyle(.checkbox)
        .padding(7)
        .background(RoundedRectangle(cornerRadius: 7).fill(fill.opacity(isOn ? 0.28 : 0.14)))
        .overlay(RoundedRectangle(cornerRadius: 7).stroke(.orange.opacity(isOn ? 0.9 : 0.5),
                                                          lineWidth: isOn ? 1.4 : 1))
        .help(help)
    }
}

/// The dispatch controls every wizard ends with: the mesh-routing row (which
/// hides itself unless a local node is live) above the SPAWN button.
struct WizardSpawnControls: View {
    let duty: String
    @Binding var useMesh: Bool
    /// SPAWN is live only for a valid config, and never while a mesh dispatch is
    /// already in flight — a second click would double-dispatch.
    let isValid: Bool
    let tint: Color
    let terminalTitle: String
    let action: () -> Void

    var body: some View {
        VStack(spacing: 6) {
            MeshSpawnRow(duty: duty, useMesh: $useMesh)
            SpawnAgentButton(isValid: isValid, tint: tint,
                             terminalTitle: terminalTitle, action: action)
        }
    }
}

extension View {
    /// The panel's card chrome: padded content on a soft rounded tint. Every grouped
    /// block in the popover wears it — the activity feed, the ongoing-sessions list, the
    /// device pool, the mesh topology/nodes/duties — so the corner radius and the tint
    /// live in one place instead of once per card.
    ///
    /// `fill` is the whole colour, opacity included, because the one non-default card
    /// (the ban list) differs in both hue and strength.
    func cardChrome(fill: Color = .gray.opacity(0.07), padding: CGFloat = 7) -> some View {
        self.padding(padding)
            .background(RoundedRectangle(cornerRadius: 8).fill(fill))
    }

    /// Wrap a wizard body in the results pane's ScrollView.
    ///
    /// `scrolls: false` is the headless renderer's escape hatch: `ImageRenderer`
    /// cannot render ScrollView content, so a snapshot of a scrolling wizard comes
    /// out blank.
    @ViewBuilder
    func wizardScroll(_ scrolls: Bool) -> some View {
        Group {
            if scrolls { ScrollView { self } } else { self }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
    }
}
