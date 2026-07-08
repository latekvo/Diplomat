import SwiftUI
import AppKit
import ArgentUtilsCore

// The ConflictConfig prompt builder lives in ArgentUtilsCore (driven by
// core/conflicts.json) and is shared verbatim with the Linux front-end. This file
// is the macOS-specific renderer: the SwiftUI wizard view. It reuses the terminal
// spawner (AgentSpawner) and SPAWN button from ReviewWizard.swift.

// MARK: - Resolve-conflicts wizard (shown in the results area)

/// The Resolve-conflicts wizard: pick whose PRs to sweep (mine / someone else's /
/// one specific PR), then SPAWN a detached agent that merges main into each and
/// resolves any conflicts. Rendered in the results pane when the "Resolve
/// conflicts" grid card is selected.
struct ConflictWizardView: View {
    @EnvironmentObject var store: Store
    private let tint = Color.cyan

    /// Shared appear/disappear transition for the contextual input row.
    private let rowTransition: AnyTransition = .opacity.combined(with: .move(edge: .top))

    /// `scrolls: false` (headless render only) drops the ScrollView so the snapshot
    /// isn't blank (ImageRenderer can't render ScrollView content). The seed params
    /// let the renderer snapshot every wizard state (target, PR field, username
    /// field, repo-mismatch warning) — same pattern as ReviewWizardView.
    private let scrolls: Bool
    init(scrolls: Bool = true, seedTarget: ConflictConfig.Target? = nil,
         seedSpecificPR: String? = nil, seedUsername: String? = nil) {
        self.scrolls = scrolls
        _target = State(initialValue: seedTarget ?? .mine)
        _specificPR = State(initialValue: seedSpecificPR ?? "")
        _username = State(initialValue: seedUsername ?? "")
    }

    @State private var target: ConflictConfig.Target
    @State private var username: String
    @State private var specificPR: String
    @State private var status: String?

    private var config: ConflictConfig {
        ConflictConfig(
            target: target,
            username: username,
            me: store.effectiveMe,
            specificPR: specificPR)
    }

    var body: some View {
        Group {
            if scrolls { ScrollView { content } } else { content }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 10) {
            titleRow
            targetRow
            contextRow
            blurbRow
            spawnButton
            if let status { statusLine(status) }
        }
        .padding(.trailing, 2)
        // Animate the contextual input row reflowing as the target changes.
        .animation(.easeInOut(duration: 0.22), value: target)
    }

    private var titleRow: some View {
        HStack(spacing: 6) {
            Image(systemName: "arrow.triangle.merge").foregroundStyle(tint)
            Text("Resolve conflicts").font(.subheadline.bold())
            Spacer()
        }
    }

    private var targetRow: some View {
        VStack(alignment: .leading, spacing: 5) {
            Picker("", selection: $target) {
                ForEach(ConflictConfig.Target.allCases) { t in
                    Text(t.title).tag(t)
                }
            }
            .labelsHidden()
            .pickerStyle(.segmented)

            if target == .mine, !store.effectiveMe.isEmpty {
                Text("PRs authored by @\(store.effectiveMe)")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
    }

    /// The someone-else's handle field or the single-PR number field — only the
    /// one that applies to the current target is shown.
    @ViewBuilder
    private var contextRow: some View {
        switch target {
        case .someone:
            inputField(icon: "at", placeholder: "github username", text: $username)
                .transition(rowTransition)
        case .specific:
            VStack(alignment: .leading, spacing: 3) {
                inputField(icon: "number", placeholder: "PR # or URL", text: $specificPR)
                    .help("Update just this one PR — paste its number or GitHub URL.")
                if let warning = prWarning {
                    Text(warning)
                        .font(.system(size: 10))
                        .foregroundStyle(.red.opacity(0.85))
                }
            }
            .transition(rowTransition)
        case .mine:
            EmptyView()
        }
    }

    /// A hint under the PR field when a pasted URL points at a different repo.
    private var prWarning: String? {
        guard config.prRef.repoMismatch else { return nil }
        let (owner, repo) = config.targetRepo
        return "That PR isn't in \(owner)/\(repo)."
    }

    private func inputField(icon: String, placeholder: String, text: Binding<String>) -> some View {
        HStack(spacing: 6) {
            Image(systemName: icon).font(.caption2).foregroundStyle(.secondary)
            TextField(placeholder, text: text)
                .textFieldStyle(.plain)
                .font(.callout)
        }
        .padding(6)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.1)))
    }

    private var blurbRow: some View {
        Text("Merges the latest main into each PR; where that conflicts, resolves it and pushes the merge. Clean merges are left untouched.")
            .font(.system(size: 10))
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
    }

    private var spawnButton: some View {
        SpawnAgentButton(isValid: config.isValid,
                         tint: tint,
                         terminalTitle: AgentSpawner.resolved(store.terminal).title,
                         action: spawn)
    }

    private func statusLine(_ msg: String) -> some View {
        Text(msg)
            .font(.system(size: 10, design: .monospaced))
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// A short label for the ongoing-processes list, e.g. "Resolve · #337".
    private var trackingLabel: String {
        switch target {
        case .mine: return "Resolve · my PRs"
        case .someone:
            let u = username.trimmingCharacters(in: .whitespaces)
            return "Resolve · @\(u.isEmpty ? "user" : u)"
        case .specific:
            let n = config.prRef.number.map { "#\($0)" } ?? "PR"
            return "Resolve · \(n)"
        }
    }

    /// The one PR this run concerns (single-PR mode only) — the open-in-browser
    /// fallback when its window can't be focused.
    private var trackingPRURL: String? {
        guard target == .specific, let n = config.prRef.number else { return nil }
        let (owner, repo) = config.targetRepo
        return "https://github.com/\(owner)/\(repo)/pull/\(n)"
    }

    private func spawn() {
        let cfg = config
        let preferred = store.terminal
        let term = AgentSpawner.resolved(preferred)
        let label = trackingLabel
        let prURL = trackingPRURL
        status = "Launching \(term.title)…"
        Task.detached {
            do {
                let result = try AgentSpawner.spawn(cfg.buildPrompt(), terminal: preferred)
                await MainActor.run {
                    store.track(kind: "conflicts", label: label, prURL: prURL, result: result)
                    status = "Launched \(term.title) · \(Fmt.clock(Date()))"
                }
            } catch {
                let msg = (error as? LocalizedError)?.errorDescription ?? "\(error)"
                await MainActor.run { status = "Failed: \(msg)" }
            }
        }
    }
}
