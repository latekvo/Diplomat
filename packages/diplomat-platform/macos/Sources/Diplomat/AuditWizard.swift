import SwiftUI
import AppKit
import DiplomatCore

// The AuditConfig prompt builder lives in DiplomatCore (driven by assets/audit.json)
// and is shared verbatim with the Linux front-end. This file is the macOS-specific
// renderer: the SwiftUI wizard view. It reuses the terminal spawner (AgentSpawner)
// and the SPAWN button from ReviewWizard.swift.

// MARK: - Full E2E test wizard (shown in the results area)

/// The Full-E2E-test wizard: a one-click whole-repo swarm audit. No target picker —
/// it always tests the entire repository. Two toggles escalate the scope: also
/// reproduce + fix the open BUG issues, and open a PR for every confirmed finding.
/// Rendered in the results pane when the "Full E2E test" grid card is selected.
struct AuditWizardView: View {
    @EnvironmentObject var store: Store
    private let tint = Color.indigo

    /// `scrolls: false` (headless render only) drops the ScrollView so the snapshot
    /// isn't blank (ImageRenderer can't render ScrollView content).
    private let scrolls: Bool

    init(scrolls: Bool = true, seedFixIssues: Bool? = nil, seedOpenPRs: Bool? = nil) {
        self.scrolls = scrolls
        if let v = seedFixIssues { _fixIssues = State(initialValue: v) }
        if let v = seedOpenPRs { _openPRs = State(initialValue: v) }
    }

    @State private var fixIssues = false
    @State private var openPRs = false
    @State private var status: String?

    private var config: AuditConfig {
        var cfg = AuditConfig(fixIssues: fixIssues, openPRs: openPRs)
        cfg.home = store.selectedRoot
        return cfg
    }

    var body: some View {
        content.wizardScroll(scrolls)
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 10) {
            titleRow
            blurbRow
            barRow
            toggles
            spawnButton
            if let status { WizardStatusLine(status) }
        }
        .padding(.trailing, 2)
        .animation(.easeInOut(duration: 0.22), value: fixIssues)
        .animation(.easeInOut(duration: 0.22), value: openPRs)
    }

    private var titleRow: some View {
        WizardTitle(systemImage: "ladybug.fill", title: "Full E2E test", tint: tint)
    }

    private var blurbRow: some View {
        WizardBlurb("Dispatches a massive swarm to end-to-end test the whole repo — every module, flow, build and test. By default it only finds and reports defects; nothing is changed.")
    }

    /// Always-on reminder of the non-negotiable bar — every HIGH / MEDIUM finding
    /// hard-reproduced with a 100%-certainty repro (a LOW earns one short check
    /// instead). Styled to read as a guarantee, not an option.
    private var barRow: some View {
        HStack(spacing: 6) {
            Image(systemName: "checkmark.seal.fill").font(.caption2).foregroundStyle(tint)
            Text("Every HIGH / MEDIUM finding is hard-reproduced — 100% proof, no guesses.")
                .font(.system(size: 10)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(7)
        .background(RoundedRectangle(cornerRadius: 7).fill(tint.opacity(0.10)))
    }

    /// The two scope-escalating toggles. Both are highlighted because each one lets
    /// the swarm change code / GitHub state, well beyond the default find-only run.
    private var toggles: some View {
        VStack(alignment: .leading, spacing: 8) {
            EscalationToggle(
                isOn: $openPRs,
                systemImage: "arrow.up.forward.square.fill",
                title: "Open PRs for every finding",
                help: "Deliver each confirmed finding / fix as its own focused PR. Off: read-only audit that only reports findings.")
            EscalationToggle(
                isOn: $fixIssues,
                systemImage: "ant.fill",
                title: "Also fix open bug issues",
                help: "Reproduce + fix the repo's open BUG issues too. Feature requests are always skipped.")
        }
    }

    private var spawnButton: some View {
        SpawnAgentButton(isValid: config.isValid, tint: tint,
                         terminalTitle: AgentSpawner.resolved(store.terminal).title,
                         action: spawn)
    }

    /// A short label for the ongoing-processes list, e.g. "E2E · repo · +PRs".
    private var trackingLabel: String {
        var s = "E2E · repo"
        if fixIssues { s += " · +issues" }
        if openPRs { s += " · +PRs" }
        return s
    }

    private func spawn() {
        let cfg = config
        // The SAME pipeline the auto-monitor rides — only the trigger (this click) and
        // its policies (foreground, none of the automatic-work gates) differ. Audits
        // aren't PR-scoped, so there is no dedup key. See `AgentDispatchGate`.
        let term = AgentSpawner.resolved(store.terminal)
        let job = Store.AgentJob(kind: "audit", auditAction: "audit",
                                 label: trackingLabel, prompt: cfg.buildPrompt(),
                                 prURL: nil, prNumber: nil,
                                 authorLogin: nil,
                                 counter: nil, directory: cfg.checkout)
        status = "Launching \(term.title)…"
        Task {
            status = statusText(for: await store.dispatchAgent(job, source: .panel),
                                terminal: term.title)
        }
    }
}
