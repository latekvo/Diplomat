import SwiftUI
import AppKit
import DiplomatCore

// The ConflictConfig prompt builder lives in DiplomatCore (driven by
// assets/conflicts.json) and is shared verbatim with the Linux front-end. This file
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
    /// "Run on mesh" (effective only while the row is live) — checked by default,
    /// like the Linux wizards.
    @State private var useMesh = true
    /// A mesh dispatch is in flight — disables SPAWN so a second click can't
    /// double-dispatch (the Qt wizards disable the button the same way).
    @State private var meshDispatching = false

    private var config: ConflictConfig {
        ConflictConfig(
            target: target,
            username: username,
            me: store.effectiveMe,
            specificPR: specificPR)
    }

    var body: some View {
        content.wizardScroll(scrolls)
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 10) {
            titleRow
            targetRow
            contextRow
            blurbRow
            spawnButton
            if let status { WizardStatusLine(status) }
        }
        .padding(.trailing, 2)
        // Animate the contextual input row reflowing as the target changes.
        .animation(.easeInOut(duration: 0.22), value: target)
    }

    private var titleRow: some View {
        WizardTitle(systemImage: "arrow.triangle.merge", title: "Resolve conflicts", tint: tint)
    }

    private var targetRow: some View {
        WizardTargetPicker(target: $target, me: store.effectiveMe)
    }

    /// The someone-else's handle field or the single-PR number field — only the
    /// one that applies to the current target is shown.
    @ViewBuilder
    private var contextRow: some View {
        switch target {
        case .someone:
            WizardTextField(systemImage: "at", placeholder: "github username", text: $username)
                .transition(rowTransition)
        case .specific:
            VStack(alignment: .leading, spacing: 3) {
                WizardTextField(systemImage: "number", placeholder: "PR # or URL", text: $specificPR)
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

    private var blurbRow: some View {
        WizardBlurb("Merges the latest main into each PR; where that conflicts, resolves it and pushes the merge. Clean merges are left untouched.")
    }

    private var spawnButton: some View {
        WizardSpawnControls(duty: "conflicts", useMesh: $useMesh,
                            isValid: config.isValid && !meshDispatching,
                            tint: tint,
                            terminalTitle: AgentSpawner.resolved(store.terminal).title,
                            action: spawn)
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
        guard let n = specificNumber else { return nil }
        let (owner, repo) = config.targetRepo
        return "https://github.com/\(owner)/\(repo)/pull/\(n)"
    }

    /// The single PR's number (single-PR mode only) — the pipeline's dedup key.
    private var specificNumber: Int? {
        target == .specific ? config.prRef.number : nil
    }

    private func spawn() {
        let cfg = config
        // Mesh path: hand the job to the local node (it picks the executor, with
        // failover) instead of opening a terminal here — mirrors the Linux wizards.
        if MeshSpawnRow.isLive(store), useMesh {
            meshDispatching = true
            status = "Dispatching over the mesh…"
            AuditLog.log("panel", "conflicts", "\(trackingLabel) · via mesh")
            store.meshDispatch(duty: "conflicts", prompt: cfg.buildPrompt()) { results, err in
                meshDispatching = false
                status = MeshSpawn.summarize(results, error: err)
            }
            return
        }
        // Local: the SAME pipeline the auto-monitor rides — dedup, ban, tracking —
        // only the trigger (this click) and its policies (foreground, no mesh gate)
        // differ. See `AgentDispatchGate`.
        let term = AgentSpawner.resolved(store.terminal)
        let job = Store.AgentJob(kind: "conflicts", auditAction: "conflicts",
                                 label: trackingLabel, prompt: cfg.buildPrompt(),
                                 prURL: trackingPRURL, prNumber: specificNumber,
                                 authorLogin: nil, duty: "conflicts",
                                 workKey: "", counter: nil)
        status = "Launching \(term.title)…"
        Task {
            status = statusText(for: await store.dispatchAgent(job, source: .panel),
                                terminal: term.title)
        }
    }
}
