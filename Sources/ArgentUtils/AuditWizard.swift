import SwiftUI
import AppKit
import ArgentUtilsCore

// The AuditConfig prompt builder lives in ArgentUtilsCore (driven by core/audit.json)
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
    /// "Run on mesh" (effective only while the row is live) — checked by default,
    /// like the Linux wizards.
    @State private var useMesh = true

    private var config: AuditConfig {
        AuditConfig(fixIssues: fixIssues, openPRs: openPRs)
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
            blurbRow
            barRow
            toggles
            spawnButton
            if let status { statusLine(status) }
        }
        .padding(.trailing, 2)
        .animation(.easeInOut(duration: 0.22), value: fixIssues)
        .animation(.easeInOut(duration: 0.22), value: openPRs)
    }

    private var titleRow: some View {
        HStack(spacing: 6) {
            Image(systemName: "ladybug.fill").foregroundStyle(tint)
            Text("Full E2E test").font(.subheadline.bold())
            Spacer()
        }
    }

    private var blurbRow: some View {
        Text("Dispatches a massive swarm to end-to-end test the whole repo — every module, flow, build and test. By default it only finds and reports defects; nothing is changed.")
            .font(.system(size: 10))
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
    }

    /// Always-on reminder of the non-negotiable bar — every finding hard-reproduced
    /// with a 100%-certainty repro. Styled to read as a guarantee, not an option.
    private var barRow: some View {
        HStack(spacing: 6) {
            Image(systemName: "checkmark.seal.fill").font(.caption2).foregroundStyle(tint)
            Text("Every finding is hard-reproduced — 100% proof of existence, no guesses.")
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
            escalationToggle(
                isOn: $openPRs,
                on: openPRs,
                icon: "arrow.up.forward.square.fill",
                title: "Open PRs for every finding",
                help: "Deliver each confirmed finding / fix as its own focused PR. Off: read-only audit that only reports findings.")
            escalationToggle(
                isOn: $fixIssues,
                on: fixIssues,
                icon: "ant.fill",
                title: "Also fix open bug issues",
                help: "Reproduce + fix the repo's open BUG issues too. Feature requests are always skipped.")
        }
    }

    private func escalationToggle(isOn: Binding<Bool>, on: Bool, icon: String,
                                  title: String, help: String) -> some View {
        Toggle(isOn: isOn) {
            HStack(spacing: 6) {
                Image(systemName: icon).foregroundStyle(.orange)
                Text(title).font(.caption.bold())
                Spacer(minLength: 0)
            }
        }
        .toggleStyle(.checkbox)
        .padding(7)
        .background(RoundedRectangle(cornerRadius: 7).fill(Color.orange.opacity(on ? 0.28 : 0.14)))
        .overlay(RoundedRectangle(cornerRadius: 7).stroke(.orange.opacity(on ? 0.9 : 0.5), lineWidth: on ? 1.4 : 1))
        .help(help)
    }

    private var spawnButton: some View {
        VStack(spacing: 6) {
            MeshSpawnRow(duty: "audit", useMesh: $useMesh)
            SpawnAgentButton(isValid: config.isValid,
                             tint: tint,
                             terminalTitle: AgentSpawner.resolved(store.terminal).title,
                             action: spawn)
        }
    }

    private func statusLine(_ msg: String) -> some View {
        Text(msg)
            .font(.system(size: 10, design: .monospaced))
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
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
        // Mesh path: hand the job to the local node (it picks the executor per the
        // audit duty's linux+macos spread, with failover) instead of spawning here.
        if MeshSpawnRow.isLive(store), useMesh {
            status = "Dispatching over the mesh…"
            AuditLog.log("panel", "audit", "\(trackingLabel) · via mesh")
            store.meshDispatch(duty: "audit", prompt: cfg.buildPrompt()) { results, err in
                status = MeshSpawn.summarize(results, error: err)
            }
            return
        }
        let preferred = store.terminal
        let term = AgentSpawner.resolved(preferred)
        let label = trackingLabel
        status = "Launching \(term.title)…"
        Task.detached {
            do {
                let result = try AgentSpawner.spawn(cfg.buildPrompt(), terminal: preferred)
                await MainActor.run {
                    store.track(kind: "audit", label: label, prURL: nil, result: result)
                    status = "Launched \(term.title) · \(Fmt.clock(Date()))"
                }
            } catch {
                let msg = (error as? LocalizedError)?.errorDescription ?? "\(error)"
                await MainActor.run { status = "Failed: \(msg)" }
            }
        }
    }
}
