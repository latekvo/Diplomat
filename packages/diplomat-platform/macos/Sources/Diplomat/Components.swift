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
// piece of that layout lives here once, so two wizards cannot disagree about it -
// the kind of divergence that reads as a design choice and is not one. Each is a
// concrete little View, deliberately not a generic scaffold: SwiftUI type-checks a
// ViewBuilder
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
/// the audit's escalations orange. The alphas are deliberately NOT a parameter: a
/// toggle that reads fainter than its neighbour is drift, not design.
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

// MARK: - Settings chrome

// Settings is a form, and a form is the same three shapes over and over: a titled
// card, a named row with a control on its right, and a small status token. They live
// here for the reason the atoms above do — and because the twin screen in
// `settingsview.py` is built from the same three, so a change of shape has one
// obvious counterpart on the other side rather than thirty scattered ones.

/// Whether the rows are showing their long-form explanation. Set once by `SettingsView`
/// from the header's *Explain* switch; read by every `SettingRow` beneath it, which is
/// why it travels in the environment rather than through thirty initialisers.
private struct SettingsExplainKey: EnvironmentKey { static let defaultValue = false }

extension EnvironmentValues {
    var settingsExplain: Bool {
        get { self[SettingsExplainKey.self] }
        set { self[SettingsExplainKey.self] = newValue }
    }
}

/// A small state token: an optional glyph and a word, in a tint that carries the
/// state on its own. Used for everything Settings reports rather than sets — the
/// monitors' live/idle, the allocator's version, the mesh's peer count.
struct StatusPill: View {
    let text: String
    let tint: Color
    var symbol: String? = nil

    var body: some View {
        HStack(spacing: 3) {
            if let symbol {
                Image(systemName: symbol).font(.system(size: 8, weight: .bold))
            }
            Text(text).font(.system(size: 9, weight: .bold))
        }
        .foregroundStyle(tint)
        .padding(.horizontal, 6).padding(.vertical, 2)
        .background(Capsule().fill(tint.opacity(0.14)))
    }
}

/// A ✓/✗ token for one part of a multi-part install — green when present, red when
/// missing. The allocator's four pieces used to be one monospaced run of "MCP ✓ ·
/// skill ✓ · rule ✗", which reads as a filename until you parse it word by word.
struct MarkChip: View {
    let label: String
    let ok: Bool

    var body: some View {
        HStack(spacing: 2) {
            Image(systemName: ok ? "checkmark" : "xmark").font(.system(size: 7, weight: .black))
            Text(label).font(.system(size: 9, weight: .semibold))
        }
        .foregroundStyle(ok ? Color.green : Color.red)
        .padding(.horizontal, 5).padding(.vertical, 2)
        .background(Capsule().fill((ok ? Color.green : Color.red).opacity(0.12)))
    }
}

/// One block of Settings: a tinted glyph, a caps title, an optional state pill, and
/// the rows under them on a soft card.
///
/// `pill` is a concrete optional rather than a second `@ViewBuilder` — one builder per
/// container is what keeps the SwiftUI type-checker inside its time budget on a CI
/// runner, the same constraint the wizard chrome above is shaped by.
struct SettingsCard<Content: View>: View {
    let symbol: String
    let title: String
    var tint: Color = .secondary
    var pill: StatusPill? = nil
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Image(systemName: symbol)
                    .font(.system(size: 9, weight: .bold)).foregroundStyle(tint)
                    .frame(width: 17, height: 17)
                    .background(RoundedRectangle(cornerRadius: 5).fill(tint.opacity(0.16)))
                Text(title).font(.system(size: 10, weight: .heavy)).kerning(0.6)
                    .foregroundStyle(.secondary)
                Spacer(minLength: 6)
                pill
            }
            VStack(alignment: .leading, spacing: 10) { content() }
        }
        .padding(.horizontal, 10).padding(.vertical, 9)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.gray.opacity(0.11)))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.gray.opacity(0.22), lineWidth: 1))
    }
}

/// One setting: its name, the control that sets it, and — under both — an optional
/// one-line summary. `detail` is the long-form paragraph, drawn only while the
/// header's *Explain* switch is on, so the screen defaults to something you can scan
/// and still holds every word of what a knob does.
struct SettingRow<Control: View>: View {
    let title: String
    var summary: String? = nil
    var detail: String? = nil
    /// Put the control under the title instead of beside it — for the wide ones
    /// (a text field, a segmented picker) that have no room in a trailing slot.
    var stacked: Bool = false
    @ViewBuilder var control: () -> Control

    @Environment(\.settingsExplain) private var explain

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            if stacked {
                Text(title).font(.system(size: 11, weight: .semibold))
                control()
            } else {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(title).font(.system(size: 11, weight: .semibold))
                    Spacer(minLength: 8)
                    control()
                }
            }
            // Under the control either way: half of these lines report what the
            // control currently resolves to (which handle is in force, where `cd`
            // will land), and a consequence reads wrong above its cause.
            if let summary {
                Text(summary).font(.system(size: 10)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if explain, let detail {
                Text(detail).font(.system(size: 10)).foregroundStyle(.secondary)
                    .padding(.horizontal, 7).padding(.vertical, 5)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.09)))
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

/// Settings that exist only while the switch above them is on, indented behind a
/// tinted rail — so the dependency is drawn rather than left to be inferred from an
/// indent, which is all that distinguished the nested verdict policy before.
struct NestedSettings<Content: View>: View {
    var tint: Color = .accentColor
    @ViewBuilder var content: () -> Content

    var body: some View {
        HStack(alignment: .top, spacing: 9) {
            RoundedRectangle(cornerRadius: 1).fill(tint.opacity(0.4)).frame(width: 2)
            VStack(alignment: .leading, spacing: 9) { content() }
        }
        .padding(.leading, 1)
    }
}

/// A capsule that fills with its tint while on — the multi-select counterpart of a
/// switch, for a set of flags short enough to name in a word each.
struct ToggleChip: View {
    let label: String
    @Binding var isOn: Bool
    var tint: Color = .orange
    var help: String = ""

    var body: some View {
        Button { isOn.toggle() } label: {
            HStack(spacing: 4) {
                Image(systemName: isOn ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 9, weight: .bold))
                Text(label).font(.system(size: 10, weight: .semibold))
            }
            .foregroundStyle(isOn ? tint : Color.secondary)
            .padding(.horizontal, 8).padding(.vertical, 4)
            .background(Capsule().fill(tint.opacity(isOn ? 0.18 : 0.07)))
            .overlay(Capsule().stroke(tint.opacity(isOn ? 0.7 : 0.22), lineWidth: 1))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .help(help)
    }
}

/// A slider with the value in a badge beside it and the two ends of the range named
/// underneath. Replaces the steppers, which show a number without showing the range
/// it lives in — so the only way to learn a cap of 16 existed was to click sixteen times.
struct SliderSetting: View {
    @Binding var value: Double
    let range: ClosedRange<Double>
    /// A step draws a tick per stop, which is worth it only where the stops are few
    /// enough to aim at — nil keeps the track clean and leaves rounding to the binding.
    var step: Double? = nil
    /// The current value, already formatted — "4 tasks", "20%".
    let badge: String
    let minLabel: String
    let maxLabel: String
    var tint: Color = .accentColor

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            HStack(spacing: 8) {
                slider.controlSize(.small).tint(tint)
                Text(badge).font(.system(size: 10, weight: .bold).monospacedDigit())
                    .foregroundStyle(tint)
                    .frame(minWidth: 52, alignment: .trailing)
            }
            HStack {
                Text(minLabel).font(.system(size: 9)).foregroundStyle(.secondary)
                Spacer()
                Text(maxLabel).font(.system(size: 9)).foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private var slider: some View {
        if let step {
            Slider(value: $value, in: range, step: step)
        } else {
            Slider(value: $value, in: range)
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
