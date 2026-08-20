import SwiftUI
import AppKit
import DiplomatCore

/// The settings screen — swapped in for the main panel body when the header gear
/// is tapped. Eleven cards over two columns: identity, the agent runner and its repo
/// root, what the monitors are allowed to do on their own, and the environment the
/// spawns land in. Everything persists through the Store (UserDefaults, or the
/// shared `~/.diplomat/config.json` for the knobs another process also reads).
///
/// Each row states what it does in a line; the paragraph behind that line is drawn
/// only while the header's *Explain* switch is on. The screen was previously all
/// paragraph — a page of prose with controls embedded in it, unreadable at a glance
/// and no faster to use on the hundredth visit than the first.
struct SettingsView: View {
    @EnvironmentObject var store: Store
    @Binding var isPresented: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            headerRow
            // Two columns, same layout as the main panel: identity + automation
            // behaviour on the left, appearance + environment on the right.
            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading, spacing: 10) {
                    identitySection
                    runnerSection
                    repoSection
                    autofixSection
                    limitsSection
                }
                .frame(width: PopoverRoot.columnWidth, alignment: .topLeading)

                VStack(alignment: .leading, spacing: 10) {
                    toolsSection
                    terminalSection
                    apiWatchSection
                    allocatorSection
                    meshSection
                    updateSection
                }
                .frame(width: PopoverRoot.columnWidth, alignment: .topLeading)
            }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .environment(\.settingsExplain, store.settingsExplain)
        .task {
            // Freshen the allocator status only. This used to also fire a full GitHub
            // poll on EVERY Settings open — three GraphQL searches against the shared
            // 5000 pt/hr budget (and potential agent dispatch) from a view-appear
            // hook; the monitor's own cadence + wake trigger keep the rows fresh.
            await store.refreshAllocatorInstall()
            // Cheap local git fetch/compare — off the UI thread inside the Store.
            store.refreshUpdateStatus()
            if store.meshEnabled { await store.meshTick() }
        }
    }

    // MARK: header

    private var headerRow: some View {
        HStack(spacing: 8) {
            Image(systemName: "gearshape.fill").foregroundStyle(.secondary)
            Text("Settings").font(.subheadline.bold())
            Spacer()
            explainToggle
            Button { withAnimation(.easeInOut(duration: 0.15)) { isPresented = false } } label: {
                Text("Done").bold()
            }
            .buttonStyle(.borderless)
            .keyboardShortcut(.cancelAction)
        }
    }

    /// Reveals every row's long-form paragraph at once. One switch for the screen, not
    /// a disclosure arrow per row: the paragraphs are read together, on the visit where
    /// the automation is being set up, and never again after it.
    private var explainToggle: some View {
        Toggle(isOn: Binding(
            get: { store.settingsExplain },
            set: { on in withAnimation(.easeInOut(duration: 0.15)) { store.settingsExplain = on } }
        )) {
            HStack(spacing: 4) {
                Image(systemName: "info.circle").font(.system(size: 10))
                Text("Explain").font(.caption)
            }
        }
        .toggleStyle(.switch).controlSize(.mini)
        .help("Show what each setting does, in full")
    }

    // MARK: GitHub identity

    private var trimmedOverride: String {
        store.usernameOverride.trimmingCharacters(in: .whitespaces)
    }

    private var identitySection: some View {
        let override = trimmedOverride
        let effective = override.isEmpty ? store.me : override
        let pill = StatusPill(text: effective.isEmpty ? "not signed in" : "@\(effective)",
                              tint: override.isEmpty ? .secondary : .blue,
                              symbol: override.isEmpty ? "person" : "pencil")
        return SettingsCard(symbol: "person.crop.circle.fill", title: "IDENTITY",
                            tint: .blue, pill: pill) {
            SettingRow(title: "GitHub username",
                       summary: override.isEmpty
                            ? "Blank = whoever `gh` is authenticated as."
                            : "Overriding the gh-authenticated user.",
                       detail: "Scopes the “My …” tools and the Review wizard: which PRs "
                             + "count as mine, and whose reviews the monitors owe.",
                       stacked: true) {
                identityField
            }
        }
    }

    private var identityField: some View {
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
        .padding(7)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.12)))
    }

    // MARK: Agent runner (which CLI the agents are)

    private var runnerSection: some View {
        let foreign = store.agentRunner != .claude
        let name = store.agentRunner.label
        return SettingsCard(symbol: "terminal.fill", title: "AGENT RUNNER", tint: .purple,
                            pill: StatusPill(text: name, tint: .purple)) {
            SettingRow(title: "Which CLI a spawn runs",
                       summary: runnerSummary,
                       detail: foreign
                            ? "Diplomat never holds an API key: \(name) stores its own "
                            + "credential, and *Connect a provider* opens its login wizard, "
                            + "which knows every provider in its catalog."
                            : "SPAWN AGENT picks up whatever flags your shell alias for "
                            + "`claude` gives it.",
                       stacked: true) {
                Picker("Which CLI a spawn runs", selection: $store.agentRunner) {
                    ForEach(AgentRunner.allCases, id: \.self) { Text($0.label).tag($0) }
                }
                .pickerStyle(.segmented).labelsHidden()
            }
            if foreign { modelRow(name) }
        }
    }

    private func modelRow(_ runnerName: String) -> some View {
        NestedSettings(tint: .purple) {
            HStack(spacing: 6) {
                Image(systemName: "cpu").font(.caption).foregroundStyle(.secondary)
                TextField("model — blank lets \(runnerName) choose", text: $store.agentModel)
                    .textFieldStyle(.plain).font(.callout).lineLimit(1)
                Button("Connect a provider…") { openProviderSetup() }
                    .buttonStyle(.bordered).controlSize(.small)
                    .help("Open \(runnerName)'s own login wizard: it knows every "
                          + "provider in its catalog and stores the credential "
                          + "itself. Diplomat never holds an API key.")
            }
            .padding(7)
            .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.12)))
        }
    }

    private var runnerSummary: String {
        switch store.agentRunner {
        case .claude: return "SPAWN AGENT runs `claude`."
        case .opencode: return "SPAWN AGENT runs `opencode`, on OpenCode's own model and provider."
        case .hermes: return "SPAWN AGENT runs `hermes chat --tui`, on Hermes' own model and provider."
        }
    }

    /// Hand the user to the runner's provider wizard, in a terminal window of the kind
    /// they already picked for agents — it is interactive, so it needs a real one.
    private func openProviderSetup() {
        AgentSpawner.openTerminal(command: store.agentRunner.setupCommand,
                                  terminal: store.terminal)
    }

    // MARK: Repo root (where the agents work)

    /// Trimmed the same way the resolver blanks it (`RepoPaths.storedAgentRepo`), so a
    /// newline-only paste reads as blank in the UI too — otherwise the clear button and
    /// the "Blank = …" tail would disagree with what actually resolves.
    private var trimmedRepoPath: String {
        store.repoPathOverride.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var repoSection: some View {
        // One state read per render decides the pill, the summary and its colour.
        let state = RepoPaths.agentRepoState
        let ok = state == .ok
        let pill = StatusPill(text: ok ? "checkout ok" : "check this",
                              tint: ok ? .green : .orange,
                              symbol: ok ? "checkmark.seal.fill" : "exclamationmark.triangle.fill")
        return SettingsCard(symbol: "folder.fill", title: "REPO ROOT", tint: .teal, pill: pill) {
            // A problem states itself on the face of the card; only the happy path is
            // short enough to fold into the summary line and its detail.
            SettingRow(title: "Where every spawned agent starts",
                       summary: repoSummary(state),
                       detail: ok
                            ? "Blank = the default path, \(RepoPaths.defaultAgentRepo). "
                            + "DIPLOMAT_REPO in this app's environment outranks both."
                            : nil,
                       stacked: true) {
                repoField
            }
        }
    }

    private var repoField: some View {
        HStack(spacing: 6) {
            Image(systemName: "folder").font(.caption).foregroundStyle(.secondary)
            TextField(RepoPaths.defaultAgentRepo, text: $store.repoPathOverride)
                .textFieldStyle(.plain)
                .font(.callout)
                .lineLimit(1)
            if !trimmedRepoPath.isEmpty {
                Button { store.repoPathOverride = "" } label: {
                    Image(systemName: "xmark.circle.fill")
                }
                .buttonStyle(.borderless).foregroundStyle(.secondary)
                .help("Clear — fall back to \(RepoPaths.defaultAgentRepo)")
            }
            Button("Choose…") { chooseRepoRoot() }
                .buttonStyle(.bordered).controlSize(.small)
        }
        .padding(7)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.12)))
    }

    /// The line for a given state. `state` is passed in (not re-read) so one read in
    /// `repoSection` drives both the pill and the text — they can't disagree, and the
    /// filesystem is stat'd once per render. Mirrors `settingsview._refresh_repo_ui`.
    private func repoSummary(_ state: RepoPaths.AgentRepoState) -> String {
        switch state {
        case .envShadowed:
            return "DIPLOMAT_REPO is set in this app's environment — agents run in "
                + "\(RepoPaths.agentRepoEnvOverride ?? RepoPaths.agentRepo), whatever this "
                + "field says. Unset it to use the picker again."
        case .notAbsolute:
            return "Use an absolute path — a relative one resolves against whatever "
                + "directory the spawned terminal happens to start in, not this app's."
        case .notACheckout:
            return "No git checkout at \(RepoPaths.agentRepo) — the spawn's `cd` is "
                + "best-effort, so an agent would start in your home directory instead. "
                + "Pick the clone of \(repoSlug)."
        case .ok:
            return "`cd \(RepoPaths.agentRepo)` — your local clone of \(repoSlug)."
        }
    }

    private var repoSlug: String {
        let c = CoreAssets.repoCoordinates()
        return "\(c.owner)/\(c.repo)"
    }

    /// Directory picker for the repo root. The menu-bar popover closes when the panel
    /// takes focus (it's a `.window`-style MenuBarExtra) — the pick is still stored, so
    /// reopening Settings shows the new path.
    private func chooseRepoRoot() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.message = "Choose the local checkout agents should work in"
        panel.prompt = "Use as repo root"
        panel.directoryURL = URL(fileURLWithPath: RepoPaths.agentRepo)
        NSApp.activate(ignoringOtherApps: true)
        if panel.runModal() == .OK, let url = panel.url {
            store.repoPathOverride = url.path
        }
    }

    // MARK: PR auto-fix monitor

    private var autofixSection: some View {
        SettingsCard(symbol: "bolt.fill", title: "AUTOMATIC WORK", tint: .orange,
                     pill: autofixPill) {
            conflictsRow
            pollErrorRow
            reviewRequestsRow
            if store.reviewRequestsEnabled { reviewPolicyBlock }
        }
    }

    /// How much automatic work this machine may run, and whether it can afford to. Its
    /// own card, beside the monitors rather than under them: both rows bound *every*
    /// automatic agent — the two monitors above and anything a mesh peer routes here.
    private var limitsSection: some View {
        let n = store.autoTaskLimit
        return SettingsCard(symbol: "speedometer", title: "LIMITS", tint: .orange,
                            pill: StatusPill(text: "≤ \(n) at a time", tint: .secondary)) {
            autoTaskLimitRow
            autoBudgetBlock
        }
    }

    /// The monitors' own health, on the card so it is answered before any row is read.
    /// A failing poll used to be invisible: the switches said "on", the counts froze
    /// stale, and nothing dispatched.
    private var autofixPill: StatusPill {
        let on = store.prAutofixEnabled || store.reviewRequestsEnabled
        if !on { return StatusPill(text: "manual", tint: .secondary, symbol: "hand.raised") }
        if store.autofixPollError != nil {
            return StatusPill(text: "polls failing", tint: .red,
                              symbol: "exclamationmark.triangle.fill")
        }
        guard store.autofixStatus?.isLive == true else {
            return StatusPill(text: "no monitor yet", tint: .orange, symbol: "bolt.slash.fill")
        }
        let n = store.autofixStatus?.watching ?? 0
        return StatusPill(text: "watching \(n) PR\(n == 1 ? "" : "s")", tint: .green,
                          symbol: "bolt.fill")
    }

    private var conflictsRow: some View {
        SettingRow(title: "Auto-queue fixes for my PRs",
                   summary: "Merge conflicts and new review threads.",
                   detail: "Off, the monitor still lists what it finds under Agent "
                         + "tasks — queued, for “execute now” only.") {
            switchControl("Auto-queue fixes for my PRs", $store.prAutofixEnabled)
        }
    }

    /// Shown while the monitor's polls are failing (gh auth expired, network, GraphQL
    /// errors) — the card's pill flags it, this names the error.
    @ViewBuilder
    private var pollErrorRow: some View {
        if let err = store.autofixPollError {
            HStack(alignment: .firstTextBaseline, spacing: 5) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.system(size: 9)).foregroundStyle(Color.red)
                Text("Failing since \(Fmt.clock(store.autofixPollErrorAt)) — \(err)")
                    .font(.caption2).foregroundStyle(Color.red)
                    .lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var reviewRequestsRow: some View {
        SettingRow(title: "Auto-queue reviews that request me",
                   summary: "Full E2E · max, inline comments — read-only, never their branch.",
                   detail: SettingsView.reviewRequestsDetail) {
            HStack(spacing: 6) {
                reviewedPill
                switchControl("Auto-queue reviews that request me",
                              $store.reviewRequestsEnabled)
            }
        }
    }

    private static let reviewRequestsDetail = """
        A review that never lands (agent died, window closed) is retried. Off, the \
        requests still list under Agent tasks, queued for “execute now” only.
        """

    /// Two counts that only exist once the monitor has run: how many reviews it has
    /// delivered, and how many it currently owes. Both were sentences buried mid-blurb.
    @ViewBuilder
    private var reviewedPill: some View {
        if store.unaddressedReviews > 0 {
            let n = store.unaddressedReviews
            StatusPill(text: "\(n) owed", tint: .orange, symbol: "arrow.triangle.2.circlepath")
                .help("\(n) unaddressed review\(n == 1 ? "" : "s") — the reconciler is retrying")
        } else if store.reviewRequestsHandled > 0 {
            StatusPill(text: "\(store.reviewRequestsHandled) done", tint: .secondary)
                .help("Reviews delivered so far")
        }
    }

    /// What an auto-review is allowed to submit. Nested under the switch that creates
    /// them, because none of it means anything while no auto-review runs.
    private var reviewPolicyBlock: some View {
        NestedSettings(tint: .orange) {
            SettingRow(title: "May approve / request changes",
                       summary: "Off ⇒ inline comments only; the verdict stays with you.",
                       detail: "On ⇒ a clean review may submit a verdict, except on the "
                             + "classes withheld below.") {
                switchControl("May approve / request changes", $store.autoApproveEnabled)
            }
            if store.autoApproveEnabled { verdictPolicyBlock }
            SettingRow(title: "Soft-approve clean PRs",
                       summary: "One “ran the sweep, all clean” comment — never an APPROVE.",
                       detail: "Off ⇒ a review that finds nothing says nothing. Independent "
                             + "of the verdict switch above: a soft approval is a comment, "
                             + "not a GitHub approval.") {
                switchControl("Soft-approve clean PRs", $store.softApproveEnabled)
            }
        }
    }

    /// The three configurable suppressors for the auto-review's "final pass + verdict".
    /// A PR matching any enabled chip gets comments only; otherwise it gets a verdict.
    private var verdictPolicyBlock: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text("WITHHOLD IT WHEN THE PR TOUCHES…")
                .font(.system(size: 9, weight: .bold)).foregroundStyle(.secondary).kerning(0.5)
            HStack(spacing: 5) {
                ToggleChip(label: "a SKILL", isOn: $store.verdictWithholdSkill,
                           help: "Comments only on a PR that edits a SKILL")
                ToggleChip(label: "the installer", isOn: $store.verdictWithholdInstaller,
                           help: "Comments only on a PR that edits the installer")
                ToggleChip(label: "community", isOn: $store.verdictWithholdCommunity,
                           help: "Comments only on a PR whose author is outside the org")
                Spacer(minLength: 0)
            }
        }
    }

    /// The device-wide ceiling on concurrent automatic agents. Under both monitors
    /// because it governs both — a poll of either can find any number of pending units
    /// at once, and this is what keeps them from all opening at the same moment.
    private var autoTaskLimitRow: some View {
        let n = store.autoTaskLimit
        let lo = AgentDispatchGate.minAutoTaskLimit, hi = AgentDispatchGate.maxAutoTaskLimit
        let badge: String = "\(n) task\(n == 1 ? "" : "s")"
        return SettingRow(title: "Run at most",
                          summary: SettingsView.autoTaskLimitSummary,
                          detail: SettingsView.autoTaskLimitDetail,
                          stacked: true) {
            SliderSetting(label: "Run at most",
                          value: Binding(
                              get: { Double(store.autoTaskLimit) },
                              set: { store.autoTaskLimit = Int($0.rounded()) }
                          ),
                          range: Double(lo)...Double(hi),
                          step: 1,
                          badge: badge,
                          minLabel: "\(lo)", maxLabel: "\(hi)",
                          tint: .orange)
        }
    }

    /// Both long strings are resolved before the ViewBuilder sees them: a concatenation
    /// or a ternary inside a `Text(...)` in a builder is what tips this file over the
    /// type-checker's time limit on a CI runner, while still compiling here.
    private static let autoTaskLimitSummary =
        "Across both monitors, a PR sweep's reviews, and anything a mesh peer routes here."

    private static let autoTaskLimitDetail = """
        The agent a wizard press opens on the spot is outside the cap; a review it \
        queues instead is inside. Work over the cap waits in Agent tasks, in the \
        order you put it, and starts when a bay frees — unless you switch off \
        Auto-execute queue there.
        """

    /// The rate-limit budget: whether automatic work waits when the account is running
    /// low, how sure of that it has to be, and what to keep in hand while the ledger
    /// cannot yet price a task.
    ///
    /// Under the task cap because they are the two halves of one question — the cap
    /// bounds how many automatic agents run at once, this bounds whether any of them
    /// should start at all.
    private static let autoBudgetDetail = """
        Priced from Telemetry → limit per task: against both rate-limit windows under \
        Claude Code, or — under a runner billed in money — against what one task costs \
        on the model it runs, and what your OpenRouter key and credit balance have \
        left. Higher confidence is stricter. Held work waits in Agent tasks, and \
        "execute now" overrides it. Nothing is held while the probe can't read a limit.
        """

    private var autoBudgetBlock: some View {
        VStack(alignment: .leading, spacing: 9) {
            SettingRow(title: "Hold work when the limit runs low",
                       summary: "Wait for a window to refill rather than start what won't fit.",
                       detail: SettingsView.autoBudgetDetail) {
                switchControl("Hold work when the limit runs low", $store.autoBudgetGate)
            }
            if store.autoBudgetGate { autoBudgetKnobs }
        }
    }

    private var autoBudgetKnobs: some View {
        let floorBadge: String = Telemetry.percent(store.autoBudgetFloorPct)
        let reserveBadge: String = Telemetry.money(store.autoBudgetReserveUsd)
        return NestedSettings(tint: .orange) {
            SettingRow(title: "Start one only when it fits", stacked: true) {
                Picker("Start one only when it fits", selection: $store.autoBudgetConfidence) {
                    ForEach(AgentDispatchGate.budgetConfidenceZ.keys.sorted(), id: \.self) {
                        Text("\($0)%").tag($0)
                    }
                }
                .pickerStyle(.segmented).labelsHidden().controlSize(.small)
            }
            SettingRow(title: "Keep in hand until it can be priced",
                       summary: "Of each rate-limit window, under Claude Code.",
                       stacked: true) {
                SliderSetting(label: "Keep in hand until it can be priced",
                              value: $store.autoBudgetFloorPct,
                              range: 0...100,
                              step: 5,
                              badge: floorBadge,
                              minLabel: "spend it all", maxLabel: "spend nothing",
                              tint: .orange)
            }
            // The same knob in the other currency. Both are shown whichever runner is
            // selected: the setting outlives the choice of runner, and a knob that
            // appeared and disappeared as that changed would look like it had been reset.
            SettingRow(title: "Keep on the account until it can be priced",
                       summary: "Of your OpenRouter balance, under a runner billed in money.",
                       stacked: true) {
                SliderSetting(label: "Keep on the account until it can be priced",
                              value: $store.autoBudgetReserveUsd,
                              range: 0...AgentDispatchGate.maxBudgetReserveUsd,
                              step: 5,
                              badge: reserveBadge,
                              minLabel: "spend it all", maxLabel: "spend nothing",
                              tint: .orange)
            }
        }
    }

    // MARK: Claude API-error watcher

    private var apiWatchSection: some View {
        let pill = apiWatchPill
        return SettingsCard(symbol: "exclamationmark.bubble.fill", title: "STALLED AGENTS",
                            tint: .pink, pill: pill) {
            SettingRow(title: "Auto-continue on API errors",
                       summary: "A 529 stops an agent dead; this types it back into motion.",
                       detail: SettingsView.apiWatchDetail) {
                switchControl("Auto-continue on API errors", $store.apiWatchEnabled)
            }
        }
    }

    /// Every other card's pill answers "is this doing anything" before a row is read.
    /// This one drew nothing at all until the watcher had stepped in at least once.
    private var apiWatchPill: StatusPill {
        let n = store.apiWatchContinues
        let text = n > 0 ? "\(n) continued" : ""
        if !store.apiWatchEnabled {
            return StatusPill(text: text.isEmpty ? "off" : text, tint: .secondary,
                              symbol: "bolt.slash.fill")
        }
        return StatusPill(text: text.isEmpty ? "watching" : text, tint: .green,
                          symbol: "bolt.fill")
    }

    private static let apiWatchDetail = """
        Watches every iTerm/Terminal session and sends "\(ApiErrorWatcher.continueMessage)" \
        when a Claude API error shows up. Out-of-quota stalls ("You've hit your weekly \
        limit") are left alone — nudging can't help until the limit resets. Claude Code \
        runs only: the banners it matches are Claude Code's. An OpenCode or Hermes agent \
        that hits an error reads as idle instead, frees its task-cap slot, and is \
        dispatched again by whichever monitor owed the work.
        """

    // MARK: Tool visibility

    private var toolsSection: some View {
        let shown = ToolKind.allCases.count - store.hiddenTools.count
        return SettingsCard(symbol: "square.grid.2x2.fill", title: "TOOLS",
                            tint: .indigo,
                            pill: StatusPill(text: "\(shown) of \(ToolKind.allCases.count) shown",
                                             tint: .secondary)) {
            SettingRow(title: "Cards in the panel grid",
                       detail: "The tint colours the card and every result row under it. "
                             + "Hiding the selected tool selects the first one still shown.",
                       stacked: true) {
                VStack(alignment: .leading, spacing: 5) {
                    ForEach(ToolKind.allCases) { toolRow($0) }
                }
            }
        }
    }

    /// One tool. The whole row dims while it is hidden, so which cards the grid will
    /// actually draw reads off the column without checking six switch positions.
    private func toolRow(_ kind: ToolKind) -> some View {
        let visible = !store.hiddenTools.contains(kind.rawValue)
        return HStack(spacing: 8) {
            IconBadge(symbol: kind.systemImage, tint: store.tint(for: kind))
            VStack(alignment: .leading, spacing: 1) {
                Text(kind.title).font(.caption.bold())
                Text(kind.subtitle).font(.system(size: 9)).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 6)
            ColorPicker("Tint for \(kind.title)", selection: Binding(
                get: { store.tint(for: kind) },
                set: { store.setTint($0, for: kind) }
            ), supportsOpacity: false)
                .labelsHidden()
                .frame(width: 34)
                .help("Tint for \(kind.title)")
            Toggle("Show \(kind.title) in the grid", isOn: Binding(
                get: { visible },
                set: { store.setTool(kind, visible: $0) }
            ))
                .labelsHidden()
                .toggleStyle(.switch)
                .controlSize(.small)
                .tint(store.tint(for: kind))
                .help("Show \(kind.title) in the grid")
        }
        .opacity(visible ? 1 : 0.45)
        .padding(.horizontal, 6).padding(.vertical, 4)
        .background(RoundedRectangle(cornerRadius: 7).fill(Color.gray.opacity(visible ? 0.07 : 0)))
    }

    // MARK: Terminal

    private var terminalSection: some View {
        let resolved = AgentSpawner.resolved(store.terminal).title
        return SettingsCard(symbol: "macwindow", title: "SPAWN TERMINAL", tint: .brown,
                            pill: StatusPill(text: resolved, tint: .secondary)) {
            SettingRow(title: "Window SPAWN AGENT opens",
                       summary: "iTerm is used when installed; otherwise Terminal.",
                       stacked: true) {
                Picker("Window SPAWN AGENT opens", selection: $store.terminalChoice) {
                    ForEach(SpawnTerminal.allCases) { term in
                        Text(term.title + (term.isInstalled ? "" : " (not installed)")).tag(term.rawValue)
                    }
                }
                .labelsHidden()
                .pickerStyle(.segmented)
            }
        }
    }

    // MARK: Device allocator (MCP server + skill + rule)

    private var allocatorSection: some View {
        let s = store.allocatorInstall
        return SettingsCard(symbol: "iphone.gen3", title: "DEVICE ALLOCATOR",
                            tint: .cyan, pill: allocatorPill(s)) {
            SettingRow(title: "Reserve a simulator before using it",
                       summary: allocatorSummary,
                       detail: "Installs an MCP server, a skill and an always-on rule. "
                             + "Reclaims a device when its agent dies or it sits idle for "
                             + "15 minutes.",
                       stacked: true) {
                VStack(alignment: .leading, spacing: 7) {
                    allocatorMarks(s)
                    allocatorButtons(s)
                }
            }
        }
    }

    /// "Installed" alone would be a true statement about a machine still running the
    /// copies some earlier checkout laid down, so the stale case says so and the marks
    /// below name what drifted. Amber for stale, not green: it is working, but not from
    /// this checkout. Mirrors `settingsview._refresh_allocator_ui`.
    private func allocatorPill(_ s: AllocatorInstall?) -> StatusPill {
        guard let s else { return StatusPill(text: "checking…", tint: .secondary) }
        let version = s.version ?? "?"
        if s.outdated {
            return StatusPill(text: "out of date · v\(version)", tint: .orange,
                              symbol: "exclamationmark.triangle.fill")
        }
        if s.installed {
            return StatusPill(text: "v\(version)", tint: .green, symbol: "checkmark.seal.fill")
        }
        return StatusPill(text: "not installed", tint: .secondary, symbol: "circle.dashed")
    }

    @ViewBuilder
    private func allocatorMarks(_ s: AllocatorInstall?) -> some View {
        if let s {
            let drift = s.outdated && !s.drift.isEmpty ? s.drift.joined(separator: ", ") : ""
            HStack(spacing: 4) {
                MarkChip(label: "MCP", ok: s.mcpRegistered)
                MarkChip(label: "skill", ok: s.skillInstalled)
                MarkChip(label: "rule", ok: s.ruleInstalled)
                MarkChip(label: "CLAUDE.md", ok: s.claudeMdInjected)
                if s.daemonRunning {
                    StatusPill(text: "daemon", tint: .green, symbol: "bolt.fill")
                }
                Spacer(minLength: 0)
            }
            if !drift.isEmpty {
                Text("stale: \(drift)")
                    .font(.system(size: 9, design: .monospaced)).foregroundStyle(.orange)
            }
        }
    }

    private func allocatorButtons(_ s: AllocatorInstall?) -> some View {
        HStack(spacing: 8) {
            Button { Task { await store.installAllocator() } } label: {
                Text(s?.outdated ?? false ? "Update"
                     : (s?.installed ?? false) ? "Reinstall" : "Install").bold()
            }
            .buttonStyle(.borderedProminent).controlSize(.small)
            .disabled(!allocatorReady)
            if s?.installed ?? false {
                Button { Task { await store.uninstallAllocator() } } label: { Text("Uninstall") }
                    .buttonStyle(.bordered).controlSize(.small)
            }
            Button { Task { await store.refreshAllocatorInstall() } } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless).controlSize(.small).help("Re-check status")
            Spacer(minLength: 0)
        }
    }

    private var allocatorReady: Bool {
        DeviceAllocator.packageAvailable && DeviceAllocator.nodeAvailable
    }

    private var allocatorSummary: String {
        if !DeviceAllocator.packageAvailable {
            return "⚠ Package not found at \(DeviceAllocator.packageDir). Set "
                + "DIPLOMAT_DEVICE_ALLOCATOR_DIR to point at it."
        }
        if !DeviceAllocator.nodeAvailable {
            return "⚠ Node.js not found. Install Node (or set DIPLOMAT_NODE) — the "
                + "allocator's MCP server and daemon need it to run."
        }
        return "So two agents never drive the same emulator at once."
    }

    // MARK: mesh (LAN P2P duty coordination)

    private var meshSection: some View {
        SettingsCard(symbol: "hexagon.fill", title: "MESH (LAN P2P)", tint: .mint,
                     pill: meshPill) {
            SettingRow(title: "Coordinate duties with this LAN",
                       summary: "Routes reviews, conflict fixes and audits to whichever "
                              + "machine fits the policy.",
                       detail: SettingsView.meshDetail) {
                switchControl("Coordinate duties with this LAN", $store.meshEnabled)
            }
        }
    }

    private static let meshDetail = """
        Runs a small peer-to-peer node that discovers the other Diplomat machines on \
        your LAN (UDP beacons); placement is surplus-first by default, token- and \
        platform-aware. Configure the whole mesh from the ⬡ Mesh screen (the ⬡ button \
        in the panel header). Off by default; no node opens on the network until you \
        enable it here.
        """

    private var meshPill: StatusPill {
        guard store.meshEnabled else {
            return StatusPill(text: "off", tint: .secondary, symbol: "bolt.slash.fill")
        }
        guard MeshBridge.nodeRunning(store.meshState) else {
            return store.meshState == nil
                ? StatusPill(text: "starting…", tint: .orange, symbol: "hourglass")
                : StatusPill(text: "node not running", tint: .orange, symbol: "bolt.slash.fill")
        }
        let peers = store.meshState?.peers.count ?? 0
        return StatusPill(text: "\(peers) peer\(peers == 1 ? "" : "s")", tint: .green,
                          symbol: "bolt.fill")
    }

    // MARK: applet update

    private var updateSection: some View {
        SettingsCard(symbol: "arrow.down.circle.fill", title: "UPDATE", tint: .blue,
                     pill: updatePill) {
            SettingRow(title: "This applet",
                       summary: updateDetail,
                       detail: "Pulls the latest applet from GitHub, rebuilds the "
                             + "diplomat-core prompt engine and the app bundle, and "
                             + "relaunches it in place.",
                       stacked: true) {
                HStack(spacing: 8) {
                    Button { store.updateApp() } label: { Text("Update").bold() }
                        .buttonStyle(.borderedProminent).controlSize(.small)
                        .disabled(!(store.updateState.map { !$0.isBusy } ?? false))
                    Button { store.refreshUpdateStatus() } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .buttonStyle(.borderless).controlSize(.small).help("Re-check for updates")
                    Spacer(minLength: 0)
                }
            }
        }
    }

    /// nil (before the first check) reads as "checking", matching the Linux front-end.
    private var updatePill: StatusPill {
        switch store.updateState ?? .checking {
        case .checking:
            return StatusPill(text: "checking…", tint: .secondary)
        case .updating:
            return StatusPill(text: "updating…", tint: .orange, symbol: "arrow.triangle.2.circlepath")
        case .restarting:
            return StatusPill(text: "restarting…", tint: .green, symbol: "arrow.clockwise")
        case .failed:
            return StatusPill(text: "update failed", tint: .red, symbol: "xmark.octagon.fill")
        case .idle(let r):
            if r.error != nil { return StatusPill(text: "check failed", tint: .orange,
                                                  symbol: "exclamationmark.triangle.fill") }
            guard let behind = r.behind, behind > 0 else {
                return StatusPill(text: "up to date", tint: .green, symbol: "checkmark.seal.fill")
            }
            return StatusPill(text: "\(behind) behind", tint: .blue, symbol: "arrow.down.circle.fill")
        }
    }

    /// The line under the button: what a check found, or what an update is doing.
    private var updateDetail: String {
        switch store.updateState ?? .checking {
        case .checking:
            return "comparing with origin…"
        case .updating(let step):
            return step
        case .restarting(let commit):
            return "relaunched at \(commit) — this instance is handing over"
        case .failed(let err):
            return err
        case .idle(let r):
            if let e = r.error { return e }
            // A diverged checkout still updates — via a merge, not a discard.
            let ahead = r.ahead ?? 0
            let aheadNote = ahead > 0
                ? " · \(ahead) local ahead\((r.behind ?? 0) > 0 ? " (will merge)" : "")" : ""
            return "\(r.commit ?? "?") on \(r.branch ?? "?") · upstream \(r.upstream ?? "?")\(aheadNote)"
        }
    }

    // MARK: shared control

    /// The one switch every boolean row uses, so no two of them can end up different
    /// sizes — which is exactly what the mini/small mix on this screen used to be.
    /// `title` is the row's, repeated because `labelsHidden()` drops the label from
    /// the layout but keeps it as the switch's accessible name.
    private func switchControl(_ title: String, _ isOn: Binding<Bool>) -> some View {
        Toggle(title, isOn: isOn)
            .labelsHidden()
            .toggleStyle(.switch)
            .controlSize(.small)
    }
}
