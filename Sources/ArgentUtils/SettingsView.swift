import SwiftUI

/// The settings screen — swapped in for the main panel body when the header gear
/// is tapped. Two knobs: the GitHub handle to treat as "me", and which tool cards
/// show in the grid. Both persist via the Store (UserDefaults-backed).
struct SettingsView: View {
    @EnvironmentObject var store: Store
    @Binding var isPresented: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            headerRow
            identitySection
            toolsSection
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private var headerRow: some View {
        HStack(spacing: 6) {
            Image(systemName: "gearshape.fill").foregroundStyle(.secondary)
            Text("Settings").font(.subheadline.bold())
            Spacer()
            Button { withAnimation(.easeInOut(duration: 0.15)) { isPresented = false } } label: {
                Text("Done").bold()
            }
            .buttonStyle(.borderless)
            .keyboardShortcut(.cancelAction)
        }
    }

    // MARK: GitHub identity

    private var trimmedOverride: String {
        store.usernameOverride.trimmingCharacters(in: .whitespaces)
    }

    private var identitySection: some View {
        VStack(alignment: .leading, spacing: 6) {
            sectionLabel("GITHUB USERNAME")
            HStack(spacing: 6) {
                Image(systemName: "at").font(.caption).foregroundStyle(.secondary)
                TextField(store.me.isEmpty ? "your github handle" : store.me,
                          text: $store.usernameOverride)
                    .textFieldStyle(.plain)
                    .font(.callout)
                if !trimmedOverride.isEmpty {
                    Button { store.usernameOverride = "" } label: {
                        Image(systemName: "xmark.circle.fill")
                    }
                    .buttonStyle(.borderless).foregroundStyle(.secondary)
                    .help("Clear — fall back to the gh-authenticated user")
                }
            }
            .padding(8)
            .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.1)))

            Text(trimmedOverride.isEmpty
                 ? "Using the gh-authenticated user\(store.me.isEmpty ? "" : " (@\(store.me))"). Scopes the “My …” tools and the Review wizard."
                 : "Overriding to @\(trimmedOverride) for the “My …” tools and the Review wizard.")
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: Tool visibility

    private var toolsSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionLabel("VISIBLE TOOLS")
            ForEach(ToolKind.allCases) { kind in
                Toggle(isOn: Binding(
                    get: { !store.hiddenTools.contains(kind.rawValue) },
                    set: { store.setTool(kind, visible: $0) }
                )) {
                    HStack(spacing: 8) {
                        Image(systemName: kind.systemImage)
                            .font(.system(size: 11, weight: .bold))
                            .foregroundStyle(.white)
                            .frame(width: 22, height: 22)
                            .background(kind.tint)
                            .clipShape(RoundedRectangle(cornerRadius: 5))
                        VStack(alignment: .leading, spacing: 1) {
                            Text(kind.title).font(.caption.bold())
                            Text(kind.subtitle).font(.system(size: 9)).foregroundStyle(.secondary).lineLimit(1)
                        }
                    }
                }
                .toggleStyle(.switch)
                .tint(kind.tint)
            }
        }
    }

    private func sectionLabel(_ text: String) -> some View {
        Text(text).font(.caption2.bold()).foregroundStyle(.secondary).kerning(0.5)
    }
}
