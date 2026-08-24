import SwiftUI
import AppKit
import DiplomatCore

// The IssueConfig prompt builder lives in DiplomatCore (driven by assets/issues.json)
// and is shared verbatim with the Linux front-end. This file is the macOS-specific
// renderer: the SwiftUI wizard view. It reuses the terminal spawner (AgentSpawner)
// and the SPAWN button from ReviewWizard.swift.

// MARK: - Fix-issues wizard (shown in the results area)

/// The Fix-issues wizard: pick which of the repo's open issues to work — all of
/// them, mine, one person's, everything the community filed, everything the org
/// filed, or one specific issue — narrow that to the ones nobody has claimed, and
/// SPAWN. One named issue opens a detached agent; a scope queues one such agent per
/// issue it covers. Rendered in the results pane when the "Fix issues" grid card is
/// selected.
struct IssueWizardView: View {
    @EnvironmentObject var store: Store
    private let tint = Color.mint

    /// Shared appear/disappear transition for contextual rows shown only where they
    /// apply (fade + slide).
    private let rowTransition: AnyTransition = .opacity.combined(with: .move(edge: .top))

    /// `scrolls: false` (headless render only) drops the ScrollView so the snapshot
    /// isn't blank (ImageRenderer can't render ScrollView content). The seed params
    /// let the renderer snapshot every wizard state (scope, issue field, username
    /// field, repo-mismatch warning) — same pattern as ReviewWizardView.
    private let scrolls: Bool
    init(scrolls: Bool = true, seedTarget: IssueTarget? = nil,
         seedSpecificIssue: String? = nil, seedUsername: String? = nil,
         seedIncludeFeatures: Bool? = nil) {
        self.scrolls = scrolls
        if let v = seedTarget { _target = State(initialValue: v) }
        if let v = seedSpecificIssue { _specificIssue = State(initialValue: v) }
        if let v = seedUsername { _username = State(initialValue: v) }
        if let v = seedIncludeFeatures { _includeFeatures = State(initialValue: v) }
    }

    @State private var depthValue: Double = IssueWizardView.defaultDepthValue()
    @State private var target: IssueTarget = .all
    @State private var username = ""
    @State private var specificIssue = ""
    @State private var unassignedOnly = true
    @State private var assignToMe = true
    @State private var openPRs = true
    @State private var commentOnIssue = true
    @State private var includeFeatures = false
    @State private var status: String?
    /// "Run on mesh" (effective only while the row is live) — checked by default,
    /// like the Linux wizards.
    @State private var useMesh = true
    /// A mesh dispatch is in flight — disables SPAWN so a second click can't
    /// double-dispatch (the Qt wizards disable the button the same way).
    @State private var meshDispatching = false

    /// The Fix-issues depth levels, loaded from the shared core.
    private var depths: [PromptDepth] { IssueCatalog.depths() }
    private var depthIndex: Int {
        guard !depths.isEmpty else { return 0 }
        return min(max(Int(depthValue), 0), depths.count - 1)
    }
    private var depth: PromptDepth {
        depths.isEmpty
            ? PromptDepth(id: "", title: "", blurb: "", fragment: "")
            : depths[depthIndex]
    }

    private static func defaultDepthValue() -> Double {
        let all = IssueCatalog.depths()
        let idx = all.firstIndex(where: { $0.id == IssueCatalog.defaultDepthID() }) ?? 0
        return Double(idx)
    }

    private var config: IssueConfig {
        IssueConfig(
            depth: depth.id,
            target: target,
            username: username,
            me: store.effectiveMe,
            specificIssue: specificIssue,
            unassignedOnly: unassignedOnly,
            assignToMe: assignToMe,
            openPRs: openPRs,
            commentOnIssue: commentOnIssue,
            includeFeatures: includeFeatures)
    }

    var body: some View {
        content.wizardScroll(scrolls)
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 10) {
            titleRow
            if let banned = bannedTargetLogin { bannedWarning(banned) }
            scopeRow
            contextRow
            blurbRow
            depthRow
            checkboxes
            featuresRow
            spawnButton
            if let status { WizardStatusLine(status) }
        }
        .padding(.trailing, 2)
        // Animate the contextual input row and the filter reflowing as the scope changes.
        .animation(.easeInOut(duration: 0.22), value: target)
        .animation(.easeInOut(duration: 0.22), value: includeFeatures)
    }

    private var titleRow: some View {
        WizardTitle(systemImage: "wrench.and.screwdriver.fill", title: "Fix issues", tint: tint)
    }

    /// The @handle this run would work the issues of IF they're banned for prompt
    /// injection — nil otherwise. Only the one scope that names a person can be
    /// banned; an issue sweep across a whole association names nobody to check.
    private var bannedTargetLogin: String? {
        guard target == .someone else { return nil }
        let u = username.trimmingCharacters(in: .whitespaces)
        return BanList.isBanned(u, in: store.bannedAuthors) ? u : nil
    }

    private func bannedWarning(_ login: String) -> some View {
        WizardBanWarning(
            login: login,
            detail: "Working their issues is strongly discouraged while the ban stands.")
    }

    /// Which issues: six scopes, so a menu rather than the segmented control the
    /// three-way whose-PRs pickers use — six segments in this column are unreadable.
    private var scopeRow: some View {
        Picker("", selection: $target) {
            ForEach(IssueTarget.allCases) { t in
                Text(t.title).tag(t)
            }
        }
        .labelsHidden()
        .pickerStyle(.menu)
    }

    /// The someone-else's handle field, the single-issue number field, or the
    /// @handle caption for "mine" — only the one that applies to the current scope.
    @ViewBuilder
    private var contextRow: some View {
        switch target {
        case .someone:
            WizardTextField(systemImage: "at", placeholder: "github username", text: $username)
                .transition(rowTransition)
        case .specific:
            VStack(alignment: .leading, spacing: 3) {
                WizardTextField(systemImage: "number", placeholder: "issue # or URL",
                                text: $specificIssue)
                    .help("Fix just this one issue — paste its number or GitHub URL.")
                if let warning = issueWarning {
                    Text(warning)
                        .font(.system(size: 10))
                        .foregroundStyle(.red.opacity(0.85))
                }
            }
            .transition(rowTransition)
        case .mine:
            if !store.effectiveMe.isEmpty {
                Text("issues opened by @\(store.effectiveMe)")
                    .font(.caption2).foregroundStyle(.secondary)
                    .transition(rowTransition)
            }
        default:
            EmptyView()
        }
    }

    /// A hint under the issue field when a pasted URL points at a different repo.
    private var issueWarning: String? {
        guard config.issueRef.repoMismatch else { return nil }
        let (owner, repo) = config.targetRepo
        return "That issue isn't in \(owner)/\(repo)."
    }

    private var blurbRow: some View {
        WizardBlurb("One agent per issue: it reproduces that issue, fixes it, and re-runs the same reproduction to prove the fix lands. Anything it can't reproduce is reported, never guessed at.")
    }

    private var depthRow: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text("Fix depth").font(.caption.bold()).foregroundStyle(.secondary)
                Spacer()
                Text(depth.title).font(.caption.bold()).foregroundStyle(.primary)
            }
            Slider(value: $depthValue,
                   in: 0...Double(max(depths.count - 1, 0)),
                   step: 1)
                .tint(tint)
            Text(depth.blurb).font(.system(size: 10)).foregroundStyle(.secondary)
        }
    }

    private var checkboxes: some View {
        VStack(alignment: .leading, spacing: 6) {
            if config.canFilterUnassigned {
                Toggle(isOn: $unassignedOnly) {
                    Text("Only unassigned issues").font(.caption)
                }
                .help("Skip every issue that already has an assignee — somebody is on it already.")
                .transition(rowTransition)
            }
            Toggle(isOn: $assignToMe) {
                Text("Assign each issue to me while working it").font(.caption)
            }
            .help("Claim the issue on GitHub before starting, and hand it back if the run abandons it — what stops a second agent taking the same one.")
            Toggle(isOn: $openPRs) {
                Text("Open a draft PR per fix").font(.caption)
            }
            .help("Off: nothing reaches the remote — each fix is left in the working tree and reported.")
            Toggle(isOn: $commentOnIssue) {
                Text("Comment the outcome on the issue").font(.caption)
            }
            .help("One comment per issue actually worked: what was reproduced, the cause, and where the fix is.")
        }
        .toggleStyle(.checkbox)
    }

    /// The escalation toggle — off by default, visually highlighted so it reads as
    /// the one option that lets the run build something nobody has signed off on.
    private var featuresRow: some View {
        EscalationToggle(
            isOn: $includeFeatures,
            systemImage: "lightbulb.fill",
            title: "Also take on feature requests",
            help: "Off: only real bug reports are worked — every feature request, question and wishlist item is skipped.")
    }

    private var spawnButton: some View {
        WizardSpawnControls(duty: "issues", useMesh: $useMesh,
                            isValid: config.isValid && !meshDispatching,
                            tint: tint,
                            terminalTitle: AgentSpawner.resolved(store.terminal).title,
                            // Only a named issue is a session the mesh could place: a
                            // sweep opens none, it queues one fix per issue for this
                            // machine's own cap to start.
                            routable: target == .specific,
                            action: spawn)
    }

    /// A short label for the ongoing-processes list, e.g. "Issues · #421 · Deep · swarm the fix".
    /// One shape, because one named issue is the only thing this wizard spawns: a
    /// sweep is queued an issue at a time and each of those rows is labelled by its own
    /// `Store.RequestedWork`.
    private var trackingLabel: String {
        let n = config.issueRef.number.map { "#\($0)" } ?? "issue"
        return "Issues · \(n) · \(depth.title)"
    }

    private func spawn() {
        let cfg = config
        // One named issue is one agent, and the two branches below dispatch it. A
        // scope is not: it becomes one queued fix per issue, so the task cap decides
        // how many run at once rather than one agent being handed every open issue in
        // the repo at the same time.
        guard cfg.isSingleIssue else {
            queueSweep(cfg)
            return
        }
        // Mesh path: hand the job to the local node (it picks the executor, with
        // failover) instead of opening a terminal here — mirrors the Linux wizards.
        if MeshSpawnRow.isLive(store), useMesh {
            meshDispatching = true
            status = "Dispatching over the mesh…"
            AuditLog.log("panel", "issues", "\(trackingLabel) · via mesh")
            store.meshDispatch(duty: "issues", prompt: cfg.buildPrompt()) { results, err in
                meshDispatching = false
                status = MeshSpawn.summarize(results, error: err)
            }
            return
        }
        // Local: the SAME pipeline the auto-monitor rides — ban check, tracking —
        // only the trigger (this click) and its policies (foreground, no mesh gate)
        // differ. An issue run is not PR-scoped, so there is no dedup key; what keeps
        // two agents off one issue is the assign-to-me claim, which every machine can
        // see. See `AgentDispatchGate`. Nor is there an author to ban-check: whoever
        // filed a hand-named issue is not the handle typed into the scope picker, and
        // this wizard never fetches it — a swept issue's ask carries its own author.
        let term = AgentSpawner.resolved(store.terminal)
        let job = Store.AgentJob(kind: "issues", auditAction: "issues",
                                 label: trackingLabel, prompt: cfg.buildPrompt(),
                                 prURL: nil, prNumber: nil,
                                 authorLogin: nil, duty: "issues",
                                 workKey: "", counter: nil)
        status = "Launching \(term.title)…"
        Task {
            status = statusText(for: await store.dispatchAgent(job, source: .panel),
                                terminal: term.title)
        }
    }

    /// Expand a scope into one queued fix per issue, and say what landed.
    ///
    /// The issues are the panel's own last fetch — the list the operator was looking
    /// at when they pressed the button. Before the first fetch that list is empty, and
    /// queueing nothing out of it would read as "there are no open issues".
    private func queueSweep(_ cfg: IssueConfig) {
        guard store.hasLoaded else {
            status = "Issues haven't loaded yet — refresh, then sweep."
            return
        }
        let (queued, already) = store.requestIssueSweep(cfg)
        if queued > 0 {
            let waiting = already > 0 ? " (\(already) already queued)" : ""
            status = "Queued \(queued) fix\(queued == 1 ? "" : "es")\(waiting)"
                + " — they start as slots free."
        } else if already > 0 {
            status = "All \(already) are queued already."
        } else {
            status = "No open issues in that scope."
        }
    }
}
