import SwiftUI
import AppKit
import DiplomatCore

/// macOS UI mapping for a tool's tint. The catalog (`assets/catalog.json`) carries
/// a semantic colour *name* (so the macOS app keeps its native SwiftUI look) and
/// a `#RRGGBB` fallback shared with the Linux front-end.
extension ToolKind {
    var tint: Color {
        switch colorName {
        case "purple": return .purple
        case "orange": return .orange
        case "red": return .red
        case "teal": return .teal
        case "green": return .green
        case "indigo": return .indigo
        case "pink": return .pink
        case "blue": return .blue
        default: return Color(hex: colorHex) ?? .gray
        }
    }
}

@MainActor
final class Store: ObservableObject {
    @Published var prs: [OpenPR] = []
    @Published var issues: [OpenIssue] = []
    @Published var isLoading = false
    @Published var error: String?
    @Published var lastUpdated: Date?
    @Published var selected: ToolKind = .skillPRs
    @Published var hasLoaded = false
    /// The authenticated user's login, used to scope the "my PRs" tools.
    @Published var me = ""

    /// Live device-allocator state (the shared pool + who holds what), read from the
    /// daemon's public state file. Nil until the daemon has run at least once.
    @Published var deviceState: DeviceState?
    /// Whether the device-allocator MCP server + skill + rule are installed.
    /// Nil until the first `--check` completes (so the UI can show "checking…").
    @Published var allocatorInstall: AllocatorInstall?

    // MARK: persisted settings

    /// Persist a settings value — EXCEPT in a one-shot headless mode. A self-test seeds
    /// this Store through the same persisted properties the GUI uses, and it shares the
    /// live app's defaults domain: an unguarded write would silently flip the user's real
    /// settings (a past render turned the auto-approve opt-in ON) or hand a monitor a
    /// dry-run's edge-trigger state. Every UserDefaults-backed settings didSet must go
    /// through here or `persistJSON`. (The one exception is `repoPathOverride`, which
    /// lives in the shared `AppConfig` file, not UserDefaults — its didSet applies the
    /// same guard inline.)
    private func persist(_ value: Any?, forKey key: String) {
        guard !Headless.active else { return }
        UserDefaults.standard.set(value, forKey: key)
    }

    /// JSON-encoded twin of `persist`, for the Codable caches (the per-PR attempt maps,
    /// the auto-fix fingerprints). Same headless rule; an encode
    /// failure leaves the previous value in place rather than clearing it.
    private func persistJSON<T: Encodable>(_ value: T, forKey key: String) {
        guard !Headless.active else { return }
        guard let data = try? JSONEncoder().encode(value) else { return }
        UserDefaults.standard.set(data, forKey: key)
    }

    @Published var usernameOverride: String {
        didSet { persist(usernameOverride, forKey: Keys.usernameOverride) }
    }
    @Published var hiddenTools: Set<String> {
        didSet { persist(Array(hiddenTools), forKey: Keys.hiddenTools) }
    }
    @Published var colorOverrides: [String: String] {
        didSet { persist(colorOverrides, forKey: Keys.colorOverrides) }
    }
    @Published var terminalChoice: String {
        didSet { persist(terminalChoice, forKey: Keys.terminalChoice) }
    }
    /// The repo root every spawned agent `cd`s into (Settings → REPO ROOT). Empty ⇒
    /// fall back to `~/dev/<repo>`; `DIPLOMAT_REPO` outranks both. Stored raw (a typed
    /// `~/…` is expanded at use), so the field shows back exactly what was entered.
    ///
    /// The one setting NOT in UserDefaults: a mesh node spawns agents from its own
    /// stdlib-only process and can't read them, so it lives in the shared
    /// `~/.diplomat/config.json` (see `AppConfig`). Headless-guarded like the rest.
    @Published var repoPathOverride: String {
        didSet {
            guard !Headless.active else { return }
            AppConfig.set(AppConfig.repoRootKey, repoPathOverride)
        }
    }
    /// Which agent CLI a spawn runs (Settings → AGENT RUNNER). In `AppConfig` rather
    /// than UserDefaults for the same reason the repo root is: a mesh node spawns
    /// agents from a process with no UserDefaults to ask.
    @Published var agentRunner: AgentRunner {
        didSet {
            guard !Headless.active else { return }
            AppConfig.set(AppConfig.agentRunnerKey, agentRunner.rawValue)
        }
    }
    /// The model the selected runner is pinned to; empty leaves the choice to that
    /// runner's own picker. A model id, never a credential — those live in the
    /// runner's provider store.
    @Published var agentModel: String {
        didSet {
            guard !Headless.active else { return }
            AppConfig.set(AppConfig.agentModelKey, agentModel)
        }
    }
    /// Whether the in-process PR auto-fix monitor is on. Persisted; when turned on we
    /// kick an immediate poll rather than waiting for the next tick.
    @Published var prAutofixEnabled: Bool {
        didSet {
            persist(prAutofixEnabled, forKey: Keys.prAutofixEnabled)
            if prAutofixEnabled && !oldValue && !Headless.active { Task { await runAutofixPollOnce() } }
        }
    }

    /// Whether to auto-dispatch a full-E2E review when someone requests my review on a
    /// PR (someone else's PR → review-only, leave comments). Persisted; kicks a poll on
    /// enable. Independent of `prAutofixEnabled`.
    @Published var reviewRequestsEnabled: Bool {
        didSet {
            persist(reviewRequestsEnabled, forKey: Keys.reviewRequestsEnabled)
            if reviewRequestsEnabled && !oldValue && !Headless.active { Task { await runAutofixPollOnce() } }
        }
    }

    /// Whether a free bay starts the next queued task by itself. On by default.
    ///
    /// Off, nothing automatic starts on this machine: the monitors keep finding work
    /// and every find queues, including the reviews a sweep asked for — which the
    /// monitor toggles do not speak for. Rows then move on "execute now" only.
    /// Persisted; kicks a poll on enable, since the drain runs at the top of one.
    @Published var queueAutoRun: Bool {
        didSet {
            persist(queueAutoRun, forKey: Keys.queueAutoRun)
            if queueAutoRun && !oldValue && !Headless.active { Task { await runAutofixPollOnce() } }
        }
    }

    /// How many automatic agents this machine runs at once — 2 by default.
    ///
    /// The monitors are level-triggered over everything GitHub currently owes, so
    /// without a cap one poll of a busy day dispatches every pending unit in a single
    /// pass: a terminal window and a `claude` session per conflicted PR and per owed
    /// review, all at once, on one machine. Work over the cap is not dropped — the
    /// poll that refuses it writes no attempt record, so the next tick offers it again
    /// as soon as an agent finishes.
    ///
    /// The second setting NOT in UserDefaults, for the same reason as the repo root: a
    /// mesh peer can route work here, and the node that spawns it is a separate
    /// Qt-less Python process (see `AppConfig`). Headless-guarded like the rest.
    @Published var autoTaskLimit: Int {
        didSet {
            // Normalise in memory as well as on disk. Clamping only on the way out
            // would leave the gate comparing against a number the file — and so the
            // mesh node behind it — does not have. (Assigning here does not re-enter
            // this observer.)
            let clamped = AgentDispatchGate.clampAutoTaskLimit(autoTaskLimit)
            if clamped != autoTaskLimit { autoTaskLimit = clamped }
            guard !Headless.active else { return }
            AppConfig.setInt(AppConfig.autoTaskLimitKey, clamped)
        }
    }

    /// The spending budget's four knobs — same file, same reason as the cap above:
    /// a mesh node spends this machine's limit on work this app never sees.
    ///
    /// Each normalises in memory as well as on disk, so the gate here and the node
    /// behind the file are never comparing against different numbers. Changing any of
    /// them drops the cached verdict, because it was computed under the old ones and
    /// would otherwise stand for another 20 seconds.
    @Published var autoBudgetGate: Bool {
        didSet {
            AutoBudget.resetCache()
            guard !Headless.active else { return }
            AppConfig.setBool(AppConfig.autoBudgetGateKey, autoBudgetGate)
        }
    }

    /// The run deadline's switch (Settings → STALLED AGENTS). Beside the budget knobs
    /// because it lives in the same shared file, and read straight back out of
    /// `AppConfig.runDeadline` by the tick rather than from here, so the node's file and
    /// the resolver cannot disagree about it.
    @Published var runDeadlineEnabled: Bool {
        didSet {
            guard !Headless.active else { return }
            AppConfig.setBool(AppConfig.runDeadlineKey, runDeadlineEnabled)
        }
    }

    @Published var autoBudgetConfidence: Int {
        didSet {
            let clamped = AgentDispatchGate.clampBudgetConfidence(autoBudgetConfidence)
            if clamped != autoBudgetConfidence { autoBudgetConfidence = clamped }
            AutoBudget.resetCache()
            guard !Headless.active else { return }
            AppConfig.setInt(AppConfig.autoBudgetConfidenceKey, clamped)
        }
    }

    @Published var autoBudgetFloorPct: Double {
        didSet {
            let clamped = AgentDispatchGate.clampBudgetFloorPct(autoBudgetFloorPct)
            if clamped != autoBudgetFloorPct { autoBudgetFloorPct = clamped }
            AutoBudget.resetCache()
            guard !Headless.active else { return }
            AppConfig.setDouble(AppConfig.autoBudgetFloorPctKey, clamped)
        }
    }

    @Published var autoBudgetReserveUsd: Double {
        didSet {
            let clamped = AgentDispatchGate.clampBudgetReserveUsd(autoBudgetReserveUsd)
            if clamped != autoBudgetReserveUsd { autoBudgetReserveUsd = clamped }
            AutoBudget.resetCache()
            guard !Headless.active else { return }
            AppConfig.setDouble(AppConfig.autoBudgetReserveUsdKey, clamped)
        }
    }

    /// Latest state from the auto-fix monitor's own poll (nil until the first). Drives
    /// the top-of-panel status pill; freshness (`isLive`) decides active vs. offline.
    @Published var autofixStatus: AutofixStatus?

    /// How many reviews I currently owe (someone requested my review and the request is
    /// newer than my last review) but have no agent on them right now — the "unaddressed"
    /// reviews the reconciler keeps retrying until they land. Refreshed each review poll.
    @Published var unaddressedReviews: Int = 0

    /// Authors banned for prompt injection (read from the daemon's banned.json). They
    /// receive no automated reviews, and appear in the "Banned" list above the sessions.
    @Published var bannedAuthors: [BannedAuthor] = []

    /// Recent actions (panel-triggered + automatic + agent-reported), newest first — the
    /// unified activity feed shown in the panel.
    @Published var auditEntries: [AuditEntry] = []

    /// Live Diplomat Mesh topology (the local node's `state.json` snapshot; nil until a node
    /// has run here) and the last control-edit error surfaced to the Mesh screen. Polled on
    /// a tight cadence while the mesh is enabled, so it fires far more often than the data
    /// refresh — `MeshSnapshot`'s equality ignores per-write liveness drift so an idle mesh
    /// doesn't churn.
    @Published var meshState: MeshSnapshot?
    @Published var meshError: String?

    /// Fingerprints of newly-seen mesh devices the user has already decided on (marked
    /// Personal, or explicitly "Keep Foreign") — so the one-time "New device" prompt on a
    /// peer card doesn't re-nag. The node stays the source of truth for actual trust; this
    /// only suppresses the prompt. Persisted locally (this machine's UI state).
    @Published var meshAckedDevices: Set<String> {
        didSet { persist(Array(meshAckedDevices), forKey: Keys.meshAckedDevices) }
    }

    /// Whether Settings draws each row's long-form explanation (the header's *Explain*
    /// switch). Off by default: the paragraphs answer questions a first read raises and
    /// are noise on every read after it. Persisted, so the answer to "do I want these"
    /// is given once rather than on every visit.
    @Published var settingsExplain: Bool {
        didSet { persist(settingsExplain, forKey: Keys.settingsExplain) }
    }

    /// Whether the "marked Personal — trust the other side too" reminder is suppressed
    /// (the modal's "Don't show again"). Persisted locally; default off (shown once per
    /// promotion until the user opts out).
    @Published var meshTrustReminderSuppressed: Bool {
        didSet { persist(meshTrustReminderSuppressed, forKey: Keys.meshTrustReminderSuppressed) }
    }

    /// Whether this machine joins the LAN P2P mesh. Opt-in and OFF by default — the app
    /// never opens a node on the network unasked; enabling it in Settings auto-starts one.
    @Published var meshEnabled: Bool {
        didSet {
            persist(meshEnabled, forKey: Keys.meshEnabled)
            guard !Headless.active, meshEnabled != oldValue else { return }
            if meshEnabled { ensureMeshRunning() } else { stopMesh() }
        }
    }

    /// Self-update progress for the Settings "UPDATE" section. Nil until the first check.
    @Published var updateState: AppUpdateState?

    /// Master switch for auto-approvals: whether an auto-dispatched review may EVER submit
    /// a verdict (APPROVE / request changes) on my behalf. Default OFF — every auto-review
    /// leaves comments only and the final call stays with me until I opt in. The per-class
    /// withhold flags below only matter when this is on.
    @Published var autoApproveEnabled: Bool {
        didSet { persist(autoApproveEnabled, forKey: Keys.autoApproveEnabled) }
    }

    /// Soft-approvals: when an auto-review that is NOT submitting a real verdict finds a PR
    /// perfectly clean, it leaves a friendly "ran the sweep, all clean, thanks for
    /// contributing" comment — never an APPROVE action. Default ON. Independent of
    /// `autoApproveEnabled`: it's what a comments-only review does on a clean PR instead of
    /// staying silent. Moot on any PR that gets a real verdict (that takes precedence).
    @Published var softApproveEnabled: Bool {
        didSet { persist(softApproveEnabled, forKey: Keys.softApproveEnabled) }
    }

    /// Auto-review verdict policy: each flag independently withholds the "final pass +
    /// verdict" escalation for one class of review-requested PR (SKILL / installer /
    /// community). All default ON. Persisted; no poll kick needed (only affects the next
    /// dispatch). Combined into a `VerdictPolicy` via `verdictPolicy`. Only consulted when
    /// `autoApproveEnabled` is on.
    @Published var verdictWithholdSkill: Bool {
        didSet { persist(verdictWithholdSkill, forKey: Keys.verdictWithholdSkill) }
    }
    @Published var verdictWithholdInstaller: Bool {
        didSet { persist(verdictWithholdInstaller, forKey: Keys.verdictWithholdInstaller) }
    }
    @Published var verdictWithholdCommunity: Bool {
        didSet { persist(verdictWithholdCommunity, forKey: Keys.verdictWithholdCommunity) }
    }

    /// The verdict policy assembled from the three settings toggles.
    var verdictPolicy: VerdictPolicy {
        VerdictPolicy(withholdOnSkill: verdictWithholdSkill,
                      withholdOnInstaller: verdictWithholdInstaller,
                      withholdOnCommunity: verdictWithholdCommunity)
    }

    /// Whether the Claude-API-error terminal watcher is on: it nudges any agent that
    /// stalls on a transient server error to continue. Persisted; kicks a scan on enable.
    @Published var apiWatchEnabled: Bool {
        didSet {
            persist(apiWatchEnabled, forKey: Keys.apiWatchEnabled)
            if apiWatchEnabled && !oldValue && !Headless.active { Task { await runApiErrorScanOnce() } }
        }
    }

    /// Every dispatched agent run the panel draws, in reading order: the sessions this
    /// machine spawned, the work it handed to a mesh node, and any live agent nobody
    /// dispatched at all.
    ///
    /// A projection of the last tick (`agentTick`), never a book of its own: the book is
    /// `~/.diplomat/agents/runs.json`, which is where the list survives a restart and
    /// where the Linux front-end and the mesh node read it from.
    @Published private(set) var agentRows: [AgentRow] = []

    /// One row of the Agent-tasks list: the record, what this tick resolved it to, and
    /// the window handle — nil for a run this applet did not open a window for.
    struct AgentRow: Identifiable, Equatable {
        var record: AgentState.RunRecord
        var state: AgentState.RunState
        /// The one fact that decided the state. Drawn beside `unknown`, which is the
        /// state where "which probe went quiet" is the difference between two entirely
        /// different things to go and fix.
        var reason: String
        var window: AgentWindows.Handle?

        var id: String { record.runID }
        var status: AgentTaskStatus { AgentTaskStatus.of(state) }

        /// Whether a click can reach a terminal: the handle this applet's own spawn kept,
        /// or the agent's own process to walk out from (`TerminalFocus`). A run on a peer
        /// has neither, and is the one row that draws and cannot be clicked.
        ///
        /// One place, because the two callers disagree visibly: the row stops being a
        /// button, or it stays one and does nothing when pressed.
        var isFocusable: Bool { window != nil || !record.tty.isEmpty }
    }

    /// The folded telemetry ledger the Telemetry screen draws. Republished when a
    /// sample lands or an agent finishes, so an open screen follows the ledger
    /// without a timer of its own.
    @Published var telemetryLedger = Telemetry.Ledger()

    /// Automatic work nothing has started yet — held by the task cap, or by its own
    /// monitor being switched off — in the order it will run. The other half of the
    /// panel's Agent-tasks list.
    ///
    /// Deliberately NOT persisted. A deferral writes no attempt record precisely so
    /// that every poll re-offers everything GitHub still owes, which means the queue
    /// is rebuilt from live evidence every 3 minutes; a stored copy would only ever
    /// be a staler answer to a question already being re-asked, and would hand
    /// "execute now" a prompt assembled against a PR that has since moved on. What
    /// IS persisted is `queuedTaskOrder` — the operator's arrangement — and
    /// `requestedWork`, the sweeps the operator asked for, both being things a
    /// poll cannot reconstruct.
    @Published var queuedTasks: [QueuedAgentTask] = []

    /// The work the operator has asked for by sweeping a scope and nothing has
    /// started yet, in the order their sweeps asked for it.
    ///
    /// Persisted, alone among the things the queue is built from, because it is the
    /// only one GitHub cannot answer for: a poll can always re-derive that a PR
    /// conflicts or that a thread is waiting on me, but nothing about a PR records
    /// that somebody swept it into a review, nor about an issue that somebody swept
    /// it into a fix. Losing this list on a restart would silently drop the rest of a
    /// fifty-item sweep, which is exactly the run long enough to be interrupted.
    var requestedWork: [RequestedWork] {
        didSet { persistJSON(requestedWork, forKey: Keys.requestedWork) }
    }

    /// Queued work whose dispatch is under way: it has left the queue and its spawn
    /// has not answered yet.
    ///
    /// That span is seconds long — a `ps` scan, a mesh round-trip, an AppleScript
    /// terminal — and for all of it the task is neither queued nor yet a session. It
    /// is held here so that it is a ROW throughout, saying what the click just did to
    /// it, rather than a gap where the operator's task used to be.
    ///
    /// In memory only, like the queue it comes from: a start that outlived the applet
    /// would be a spawn nothing is waiting on, and the work is re-offered by the next
    /// poll either way.
    @Published private(set) var startingTasks: [QueuedAgentTask] = []

    /// This poll's deferrals, published as `queuedTasks` only once the whole cycle
    /// has succeeded: a failed fetch means "we no longer know what is owed", which
    /// is not the same as "nothing is owed", and must not empty the list.
    private var stagedQueue: [QueuedAgentTask] = []

    /// The operator's drag order, by queue key. Pruned to what is still offered on
    /// every commit, so it can't grow past the work it describes. Held in memory and
    /// mirrored to disk, like every other persisted setting — a read-through property
    /// would lose the arrangement for a whole session in the modes where writes are
    /// suppressed.
    private var queuedTaskOrder: [String] {
        didSet { persist(queuedTaskOrder, forKey: Keys.queuedTaskOrder) }
    }

    private enum Keys {
        static let usernameOverride = "usernameOverride"
        static let hiddenTools = "hiddenTools"
        static let colorOverrides = "colorOverrides"
        static let terminalChoice = "terminalChoice"
        static let prAutofixEnabled = "prAutofixEnabled"
        static let autofixFingerprints = "autofixFingerprints"
        static let autofixConflicts = "autofixConflictsHandled"
        static let autofixReviews = "autofixReviewsHandled"
        static let reviewRequestsEnabled = "reviewRequestsEnabled"
        static let reviewReqAttempts = "reviewReqAttempts"
        static let myReviewAttempts = "myReviewAttempts"
        static let reviewRequestsHandled = "reviewRequestsHandled"
        static let autoApproveEnabled = "autoApproveEnabled"
        static let softApproveEnabled = "softApproveEnabled"
        static let verdictWithholdSkill = "verdictWithholdSkill"
        static let verdictWithholdInstaller = "verdictWithholdInstaller"
        static let verdictWithholdCommunity = "verdictWithholdCommunity"
        static let apiWatchEnabled = "apiWatchEnabled"
        static let apiWatchContinues = "apiWatchContinues"
        static let myConflictAttempts = "myConflictAttempts"
        static let meshEnabled = "meshEnabled"
        static let meshAckedDevices = "meshAckedDevices"
        static let meshTrustReminderSuppressed = "meshTrustReminderSuppressed"
        static let settingsExplain = "settingsExplain"
        static let allocatorSetupDone = "allocatorSetupDone"
        static let queuedTaskOrder = "queuedTaskOrder"
        static let queueAutoRun = "queueAutoRun"
        /// The name this list has always been stored under. Kept when it widened
        /// from reviews alone to every sweep, so the asks a Review-PRs sweep left
        /// standing outlive the upgrade that widened it (`RequestedWork` decodes a
        /// row with no verb as the review it was written as).
        static let requestedWork = "requestedReviews"
    }

    /// The persisted terminal choice, readable before a Store exists (the AppDelegate's
    /// first-launch automation prompt) — single-sourced so a key rename can't desync it.
    static var storedTerminalChoice: String? {
        UserDefaults.standard.string(forKey: Keys.terminalChoice)
    }

    /// The handle to treat as "me": the user's override if set, else the gh login.
    var effectiveMe: String {
        let o = usernameOverride.trimmingCharacters(in: .whitespaces)
        return o.isEmpty ? me : o
    }

    /// A tool's tint: the user's override if set & valid, else its catalog default.
    func tint(for kind: ToolKind) -> Color {
        if let hex = colorOverrides[kind.rawValue], let c = Color(hex: hex) { return c }
        return kind.tint
    }
    func setTint(_ color: Color, for kind: ToolKind) {
        colorOverrides[kind.rawValue] = color.hexRGB
    }
    var terminal: SpawnTerminal { SpawnTerminal(rawValue: terminalChoice) ?? .ghostty }
    var visibleTools: [ToolKind] {
        ToolKind.allCases.filter { !hiddenTools.contains($0.rawValue) }
    }
    func setTool(_ kind: ToolKind, visible: Bool) {
        if visible {
            hiddenTools.remove(kind.rawValue)
        } else {
            hiddenTools.insert(kind.rawValue)
            if selected == kind, let first = visibleTools.first { selected = first }
        }
    }

    /// How often the data auto-refreshes. Defaults to 5 minutes; override with
    /// `DIPLOMAT_REFRESH_SECS` (clamped to ≥5s) for tuning/testing.
    static var autoRefreshInterval: TimeInterval {
        let secs = ProcessInfo.processInfo.environment["DIPLOMAT_REFRESH_SECS"].flatMap(Double.init)
        return max(5, secs ?? 5 * 60)
    }
    private var autoRefreshTask: Task<Void, Never>?

    /// One-time carry-over of the pre-rename defaults domain. The Diplomat rename
    /// changed the bundle id (com.ignacy.argent-utils → com.ignacy.diplomat), which
    /// points UserDefaults at a FRESH domain — without this, the first post-rename
    /// launch silently resets every preference. Most dangerously the monitor
    /// toggles, which default ON: an operator who explicitly disabled them (e.g.
    /// after the 2026-07-20 duplicate-dispatch incident) would have them re-enable
    /// themselves. Copies only keys the new domain doesn't already have, so a
    /// setting changed post-rename is never clobbered, and runs once (marker key).
    static func migrateLegacyDefaultsIfNeeded() {
        let marker = "legacyDefaultsMigrated"
        let std = UserDefaults.standard
        guard !std.bool(forKey: marker) else { return }
        if let legacy = std.persistentDomain(forName: "com.ignacy.argent-utils") {
            for (key, value) in legacy where std.object(forKey: key) == nil {
                std.set(value, forKey: key)
            }
        }
        std.set(true, forKey: marker)
    }

    /// One-time move of the spawn terminal to Ghostty, for an install that predates it.
    ///
    /// The stored choice is what an operator picked out of a two-way picker, so on every
    /// existing install it reads "iterm" — whether that was a decision or a default they
    /// never touched. Without this, a picker that grew a third option would leave every
    /// running install on the terminal the third option was added to replace.
    ///
    /// Only a choice of iTerm is moved, and only onto a box that can drive Ghostty.
    /// Terminal.app was picked over an installed iTerm, which is a decision against a
    /// default rather than the absence of one, and is left alone. Runs once either way: a
    /// box with no Ghostty right now keeps its terminal and is not asked again, because a
    /// second ask cannot be told from overriding the operator's own switch back.
    static func migrateTerminalChoiceIfNeeded() {
        let marker = "ghosttyTerminalDefaultMigrated"
        let std = UserDefaults.standard
        guard !std.bool(forKey: marker) else { return }
        std.set(true, forKey: marker)
        guard let moved = terminalChoiceMigration(stored: std.string(forKey: Keys.terminalChoice),
                                                  ghosttyUsable: SpawnTerminal.ghostty.isUsable)
        else { return }
        std.set(moved, forKey: Keys.terminalChoice)
    }

    /// What the migration above decides, without the defaults it decides it about — so
    /// which choices it moves is checkable without writing to the operator's own.
    /// nil leaves the stored choice alone.
    nonisolated static func terminalChoiceMigration(stored: String?, ghosttyUsable: Bool) -> String? {
        guard ghosttyUsable, stored == nil || stored == SpawnTerminal.iterm.rawValue
        else { return nil }
        return SpawnTerminal.ghostty.rawValue
    }

    init() {
        Store.migrateLegacyDefaultsIfNeeded()
        Store.migrateTerminalChoiceIfNeeded()
        MeshBridge.migrateLegacyStateDirIfNeeded()
        let defaults = UserDefaults.standard
        usernameOverride = defaults.string(forKey: Keys.usernameOverride) ?? ""
        // SKILL.md + Installer/CLI tools ship hidden (absent key ⇒ default); any
        // Settings toggle persists the explicit set from then on.
        hiddenTools = Set(defaults.stringArray(forKey: Keys.hiddenTools)
            ?? [ToolKind.skillPRs.rawValue, ToolKind.installerPRs.rawValue])
        colorOverrides = (defaults.dictionary(forKey: Keys.colorOverrides) as? [String: String]) ?? [:]
        // The whole preference ladder, so a fresh install lands on the terminal a spawn
        // would have resolved to anyway.
        terminalChoice = defaults.string(forKey: Keys.terminalChoice)
            ?? AgentSpawner.resolved(.ghostty).rawValue
        repoPathOverride = AppConfig.string(AppConfig.repoRootKey)
        agentRunner = AppConfig.agentRunner
        agentModel = AppConfig.agentModel
        autoTaskLimit = AppConfig.autoTaskLimit
        autoBudgetGate = AppConfig.autoBudgetGate
        runDeadlineEnabled = AppConfig.runDeadline != nil
        autoBudgetConfidence = AppConfig.autoBudgetConfidence
        autoBudgetFloorPct = AppConfig.autoBudgetFloorPct
        autoBudgetReserveUsd = AppConfig.autoBudgetReserveUsd
        // Default ON (absent key ⇒ true): the pill only lights up on a live heartbeat,
        // so defaulting on can't falsely claim "active" when no monitor is running.
        prAutofixEnabled = defaults.object(forKey: Keys.prAutofixEnabled) as? Bool ?? true
        reviewRequestsEnabled = defaults.object(forKey: Keys.reviewRequestsEnabled) as? Bool ?? true
        queueAutoRun = defaults.object(forKey: Keys.queueAutoRun) as? Bool ?? true
        // Auto-approvals OFF by default — an auto-review never submits a verdict on my
        // behalf until I explicitly opt in.
        autoApproveEnabled = defaults.object(forKey: Keys.autoApproveEnabled) as? Bool ?? false
        // Soft-approvals ON by default (absent key ⇒ true): a clean comments-only review
        // still leaves a friendly thank-you note — no APPROVE action, so nothing is submitted
        // on my behalf.
        softApproveEnabled = defaults.object(forKey: Keys.softApproveEnabled) as? Bool ?? true
        verdictWithholdSkill = defaults.object(forKey: Keys.verdictWithholdSkill) as? Bool ?? true
        verdictWithholdInstaller = defaults.object(forKey: Keys.verdictWithholdInstaller) as? Bool ?? true
        verdictWithholdCommunity = defaults.object(forKey: Keys.verdictWithholdCommunity) as? Bool ?? true
        apiWatchEnabled = defaults.object(forKey: Keys.apiWatchEnabled) as? Bool ?? true
        // Mesh is opt-in and OFF by default (absent key ⇒ false): no node opens on the
        // network until the user enables it in Settings.
        meshEnabled = defaults.object(forKey: Keys.meshEnabled) as? Bool ?? false
        meshAckedDevices = Set(defaults.stringArray(forKey: Keys.meshAckedDevices) ?? [])
        meshTrustReminderSuppressed = defaults.bool(forKey: Keys.meshTrustReminderSuppressed)
        settingsExplain = defaults.bool(forKey: Keys.settingsExplain)
        queuedTaskOrder = defaults.stringArray(forKey: Keys.queuedTaskOrder) ?? []
        requestedWork = Store.loadRequestedWork()
        if hiddenTools.contains(selected.rawValue),
           let first = ToolKind.allCases.first(where: { !hiddenTools.contains($0.rawValue) }) {
            selected = first
        }

        // One-shot self-test modes (render, dumps, track-test) must not start polls
        // or shell `node` for the allocator status — see `Headless` for the single
        // env-var list shared with the AppDelegate.
        if !Headless.active {
            startAutoRefresh()
            startProcessPoll()
            startAutofixMonitor()
            startApiErrorWatcher()
            startMeshPoll()
            refreshBanList()
            refreshAudit()
            Task { await fetchMe() }
            Task { await refreshDeviceState() }
            // Installs the device allocator on first run and refreshes a stale one
            // afterwards; publishes the status either way, so it stands in for the
            // plain `refreshAllocatorInstall()` this used to do here.
            Task { await ensureAllocatorInstalled() }
            // Auto-start a node on launch if the user has previously opted into the mesh
            // (mirrors the Linux applet's ensure-running-on-start).
            if meshEnabled { ensureMeshRunning() }
        }
    }

    // MARK: device allocator

    /// Re-read the device-allocator's public state file (cheap) so the Devices
    /// section stays live. Off-main read; publish only on change to avoid redraws.
    func refreshDeviceState() async {
        let next = await Task.detached(priority: .utility) { DeviceAllocator.readState() }.value
        if next != deviceState { deviceState = next }
    }

    /// Force-kill a device (the panel's per-device X): free it + shut it down, then
    /// refresh so the row updates.
    func killDevice(_ key: String) async {
        let ok = await Task.detached(priority: .userInitiated) { DeviceAllocator.killDevice(key: key) }.value
        // Log AFTER the call, with the real outcome — the audit feed must not
        // assert a kill that actually failed.
        AuditLog.log("panel", "kill-device", ok ? "Killed device \(key)" : "Kill FAILED for device \(key)")
        await refreshDeviceState()
    }

    /// Whether the allocator's setup has been settled on this machine — installed, or
    /// deliberately uninstalled in Settings. Gates the first-run auto-install so it
    /// never re-installs behind a user who removed it. Plain UserDefaults rather than
    /// `@Published`: no view renders it, and a publish here would invalidate the whole
    /// panel from a background launch task. Twin of `Store.allocator_setup_done`.
    var allocatorSetupDone: Bool {
        get { UserDefaults.standard.bool(forKey: Keys.allocatorSetupDone) }
        set { persist(newValue, forKey: Keys.allocatorSetupDone) }
    }

    /// Shell the installer's `--check` (Node startup, ~100-300ms) off-main and
    /// publish the result. Called at startup, when Settings opens, and post-install.
    func refreshAllocatorInstall() async {
        allocatorInstall = await Task.detached(priority: .utility) { DeviceAllocator.check() }.value
    }

    /// Install the device-allocator MCP on first run, and keep an existing install
    /// current afterwards. Called on every launch; twin of the Linux applet's
    /// `ensure_allocator_installed_async`.
    ///
    /// Everything the installer writes — the skill, the always-on rule, the CLAUDE.md
    /// block, the MCP registration — is a *copy* of something in this checkout, and a
    /// `git pull` moves the originals alone. So an install is not a one-time event:
    /// without this, a machine set up once keeps coercing its agents with whatever
    /// text shipped that day.
    ///
    /// Which of the three situations this is — first run, stale, or an install the
    /// user deliberately removed — is `DeviceAllocator.needsInstall`, shared with its
    /// Linux twin so the two applets can't drift on the one question where being
    /// wrong reinstalls something behind the user's back.
    func ensureAllocatorInstalled() async {
        guard DeviceAllocator.packageAvailable else { return }
        let status = await Task.detached(priority: .utility) { DeviceAllocator.check() }.value
        if !DeviceAllocator.needsInstall(status: status, setupDone: allocatorSetupDone) {
            allocatorInstall = status
            if status.installed { allocatorSetupDone = true }
            return
        }
        // A first install or a stale one — the same act either way, since `--install`
        // rewrites every artifact and is therefore also the repair.
        let reason = status.installed ? "update (stale: \(status.drift.joined(separator: ", ")))"
                                      : "first-run install"
        let result = await Task.detached(priority: .utility) { () -> AllocatorInstall in
            DeviceAllocator.ensureDeps()
            return DeviceAllocator.install()
        }.value
        allocatorInstall = result
        if result.installed { allocatorSetupDone = true }
        AuditLog.log("panel", "allocator-install",
                     "Device allocator \(reason) (ok: \(result.installed))")
        refreshAudit()
        await refreshDeviceState()
    }

    func installAllocator() async {
        allocatorInstall = await Task.detached(priority: .utility) { () -> AllocatorInstall in
            // Deps first: the installer would otherwise register an MCP server that
            // dies on spawn for want of its one runtime dependency.
            DeviceAllocator.ensureDeps()
            return DeviceAllocator.install()
        }.value
        allocatorSetupDone = true
        AuditLog.log("panel", "allocator-install",
                     "Installed device allocator (ok: \(allocatorInstall?.installed == true))")
        refreshAudit()
        await refreshDeviceState()
    }

    func uninstallAllocator() async {
        allocatorInstall = await Task.detached(priority: .utility) { DeviceAllocator.uninstall() }.value
        // An explicit uninstall is a settled choice — the launch-time install must
        // not put it back on the next start.
        allocatorSetupDone = true
        AuditLog.log("panel", "allocator-uninstall", "Uninstalled device allocator")
        refreshAudit()
        await refreshDeviceState()
    }

    func fetchMe() async {
        guard me.isEmpty, let login = try? await API.fetchViewerLogin() else { return }
        me = login
    }

    func startAutoRefresh() {
        guard autoRefreshTask == nil else { return }
        autoRefreshTask = Task { [weak self] in
            let ns = UInt64(Store.autoRefreshInterval * 1_000_000_000)
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: ns)
                if Task.isCancelled { break }
                await self?.refresh()
            }
        }
    }

    func refresh() async {
        isLoading = true
        error = nil
        do {
            async let m = API.fetchViewerLogin()
            async let p = API.fetchOpenPRs()
            async let i = API.fetchOpenIssues()
            let (mm, pp, ii) = try await (m, p, i)
            me = mm
            prs = pp
            issues = ii
            lastUpdated = Date()
            hasLoaded = true
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? "\(error)"
        }
        isLoading = false
        // A full refresh is also where we re-ask which of the tracked PRs have landed.
        // Best-effort and after the main load so a PR-state hiccup never blocks the
        // tool data or clobbers its error.
        await refreshMergedStatuses()
        await refreshTokenBudget()
    }

    /// Ask GitHub which of the tracked PRs have landed — the one terminal outcome that
    /// outranks whatever a process is doing.
    ///
    /// On the slow refresh, not the 8-second tick: it costs a `gh` call per PR. The answer
    /// is carried forward by the fast ticks in between.
    ///
    /// Only the runs this applet dispatched. "Merged" ends a run so it can be priced and
    /// its bay handed back, and a synthesized one has nothing to price and is manifestly
    /// still in the process table — asked about, a landed PR whose agent is still sitting
    /// in its window would retire that record and have the next tick synthesize it
    /// straight back, one `gh` call and one audit line per tick. What ends one of those is
    /// the scan that made it.
    func refreshMergedStatuses() async {
        mergedPRs = await AgentProbes.mergedPRs(
            Set(AgentRegistry.load().filter { !$0.untracked }.compactMap(\.prNumber)))
    }

    /// Ask whether the account still has room to spend — the precondition on the
    /// resolver's run deadline.
    ///
    /// On the slow refresh, not the 8-second tick, for the reason the merged statuses
    /// are: this one dials an endpoint over HTTPS, and the tick runs on the panel's
    /// repaint as well as on the poll. The ticks in between carry the answer forward.
    ///
    /// With the deadline switched off the endpoint is left alone: it is one small
    /// per-account bucket shared by every Claude Code session on the box, and the
    /// telemetry sampler already loses readings to it, so spending a round of it on a
    /// value `AgentState.pastDeadline` cannot consult is pure contention.
    func refreshTokenBudget() async {
        guard AppConfig.runDeadline != nil else {
            // Dropped rather than carried: nothing refreshes this while the switch is
            // off, and switching it back on reaches the deadline on the panel's 8-second
            // tick, a poll interval before this runs again. Kept across that gap, the
            // reading arms the backstop off a balance as old as the switch was off for.
            tokensLeft = .unavailable("not probed with the deadline switched off")
            return
        }
        // Off the book on disk rather than off the last tick: this runs on the slow
        // refresh, where a tick may be seconds old, and a spawn since then is a run
        // whose currency the reading would otherwise miss.
        let runners = AgentRegistry.runners(of: AgentRegistry.load())
        tokensLeft = await Task.detached(priority: .utility) {
            AgentProbes.tokensLeft(runners)
        }.value
    }

    // MARK: tracked agent runs

    /// Outcome of clicking an agent row.
    enum FocusOutcome { case focused, dismissed }

    /// How often the agent rows are re-resolved. Default 8s; override with
    /// `DIPLOMAT_PROC_POLL_SECS` (clamped ≥2s) for tuning/testing.
    static var processPollInterval: TimeInterval {
        let secs = ProcessInfo.processInfo.environment["DIPLOMAT_PROC_POLL_SECS"].flatMap(Double.init)
        return max(2, secs ?? 8)
    }
    private var processPollTask: Task<Void, Never>?

    /// Register a run, then spawn its agent into it. Returns the terminal that opened.
    ///
    /// The record is written BEFORE the spawn, not after. A terminal takes seconds to
    /// open and the poll that dispatched it can ask about the same PR again inside that
    /// window; a run booked only on success is a PR that reads free while its agent is
    /// starting.
    ///
    /// The agent's shell writes its own pid into the run directory before handing over
    /// to the agent, so what identifies this run afterwards is that pid rather than the
    /// wording of its prompt (`AgentSpawner.shellCommand`).
    ///
    /// Which runner is spawned is written down here rather than re-read later: the setting
    /// is what the NEXT spawn will use, so a run started under one runner and asked about
    /// after the operator switched would be interrogated through the wrong store. An
    /// OpenCode run also gets a port reserved for its own server. A port that cannot be had
    /// is not a failure to spawn — the run goes ahead without one and is read off its
    /// screen, exactly as a Claude Code run is.
    ///
    /// `kind` drives the row's tint; `auditAction` (defaulting to `kind`) is the verb
    /// written to the activity feed. They're decoupled so a review-reply agent can log a
    /// distinct `review-reply` action — feeding the Activity filter its own "Replies"
    /// category — while still rendering as a plain review session.
    @discardableResult
    func spawnTracked(kind: String, label: String, prURL: String?, prNumber: Int?,
                      prompt: String, source: String, auditAction: String? = nil,
                      ledgerKey: String = "", terminal preferred: SpawnTerminal,
                      restoreFocusTo restoreBID: String? = nil) async throws -> SpawnTerminal {
        let now = Date().timeIntervalSince1970
        let record = AgentRegistry.createRun(
            AgentState.RunRecord(runID: AgentRegistry.newRunID(now: now), dispatchedAt: now,
                                 prNumber: prNumber, prURL: prURL ?? "", kind: kind,
                                 label: label, source: source, placement: .local,
                                 ledgerKey: ledgerKey),
            prompt: prompt)
        let runner = AppConfig.agentRunner
        AgentRegistry.stageRunner(record.runID, runner.rawValue)
        var port = 0
        if runner == .opencode, let free = OpenCodeProbe.freePort(),
           AgentRegistry.stagePort(record.runID, free) {
            port = free
        }
        let plan = AgentSpawner.SpawnPlan(
            promptFile: AgentRegistry.promptPath(record.runID),
            donePath: AgentRegistry.donePath(record.runID).path,
            pidPath: AgentRegistry.pidPath(record.runID).path,
            runner: runner, port: port,
            settingsPath: AgentRegistry.stageHooks(record.runID))
        do {
            // Detached: the spawn's `osascript` blocks for `inputSettleDelay` seconds,
            // and this actor draws the panel.
            let result = try await Task.detached(priority: .userInitiated) {
                try AgentSpawner.spawn(plan, terminal: preferred, restoreFocusTo: restoreBID)
            }.value
            AgentWindows.stage(record.runID, result.window)
            // The tty the spawn captured is the window's, which is the agent's: known a
            // moment before the pid file is, so the run has a screen from its very first
            // tick instead of from whichever one adopts its pid.
            var seeded = record
            seeded.tty = result.tty
            Store.persistRunChanges([seeded])
            AuditLog.log(source, auditAction ?? kind, label)
            return result.terminal
        } catch {
            // Nothing is running, so the record would be a bay held for an agent that
            // never started — and, being a record, one nothing can ever retire.
            AgentRegistry.forget([record.runID])
            throw error
        }
    }

    /// Stop tracking one run — the row's ✕.
    ///
    /// Drops it from the book and deletes its run directory. Only offered for a run this
    /// applet booked: a live agent nobody dispatched is re-derived from the process table
    /// every tick, so forgetting one would redraw it a second later.
    ///
    /// Priced on the way out: this is the one way a run leaves the book without being
    /// retired, and retirement is what would otherwise close its ledger entry.
    func forgetRun(_ runID: String) {
        let priced = Store.pricingInputs(AgentRegistry.load().filter { $0.runID == runID })
        AgentRegistry.forget([runID])
        Task {
            await settleLedger(priced)
            await settleAgents()
        }
    }

    // MARK: mesh runs (work this device handed to the mesh)

    /// Book a run the mesh placed, which this applet did not spawn itself.
    ///
    /// There is no pid to record: the executor opened the terminal. A placement that
    /// landed back HERE is still a process on this box, so it spends a bay and the
    /// untracked scan will find it; one on a peer is judged only by the executor's
    /// origination claim, which is the sole evidence that crosses the machine boundary.
    ///
    /// Which runner it is under is recorded for a landing HERE and only there. The node
    /// spawns through the same seam this applet does, so the answer is the same one and the
    /// run is asked of the right store and priced by it. A run on a PEER is a process on
    /// another machine: our stores hold nothing about it, and a runner written here would
    /// point its probe at a session that is somebody else's.
    ///
    /// Not private: the queue self-test books a run here rather than through a real
    /// dispatch, which would need a live mesh node and a peer willing to run it.
    func trackMeshRun(_ job: AgentJob, node: String, attemptNumber: Int,
                      onThisMachine: Bool = false) {
        // The work key IS a mesh run's identity — it is what the executor's origination
        // claim is published under, and the only evidence that ever crosses the machine
        // boundary. A run without one could never be resolved against a claim book.
        // (`routeViaMesh` only dispatches keyed jobs, so nothing in the app arrives here
        // without one.)
        guard !job.workKey.isEmpty else { return }
        let now = Date().timeIntervalSince1970
        let record = AgentRegistry.createRun(
            AgentState.RunRecord(
                runID: AgentRegistry.newRunID(now: now), dispatchedAt: now,
                prNumber: job.prNumber, prURL: job.prURL ?? "", kind: job.kind,
                label: AgentDispatchGate.label(source: .auto, core: job.label,
                                               attemptNumber: attemptNumber,
                                               requested: job.requested),
                source: AgentDispatchGate.Source.auto.rawValue,
                placement: onThisMachine ? .meshHere : .meshPeer,
                node: node, workKey: job.workKey, ledgerKey: job.ledgerKey),
            prompt: job.prompt)
        if onThisMachine {
            AgentRegistry.stageRunner(record.runID, AppConfig.agentRunner.rawValue)
        }
    }

    // MARK: - the one tick every agent question is a projection of

    /// Everything one pass of evidence produced, plus what the panel needs to click a row.
    struct AgentPass {
        var tick: AgentState.Tick
        /// Window handles for the runs that have one, read in the same pass so a repaint
        /// never touches the disk.
        var windows: [String: AgentWindows.Handle]
    }

    /// Resolve every registered run against one pass of evidence. READ-ONLY.
    ///
    /// This is the single place the applet asks what its agents are doing. The dedup, the
    /// cap, the panel rows and the retirement are all projections of the result
    /// (`DiplomatCore.AgentState`), so they cannot come to disagree — which is what four
    /// independent re-derivations of the same question did.
    ///
    /// Nothing here writes, retires or logs. The panel asks for the rows and the free slots
    /// on every repaint, so a read with consequences means a screenshot retires records and
    /// a redraw writes to the operator's activity feed. What the consequences are, and when
    /// they happen, is `settleAgents`.
    ///
    /// Deliberately not cached either. The expensive part is the I/O, and that is already
    /// cached where it happens (`AgentProbes`), so resolving again costs a file read and
    /// some set arithmetic. A cache here would instead buy a staleness window in which an
    /// agent that just finished still holds its bay — the exact class of wrong answer this
    /// whole mechanism exists to remove.
    ///
    /// The probes shell out, run AppleScript and dial sockets, so the whole pass is done
    /// off the main actor.
    func agentTick() async -> AgentPass {
        let limit = autoTaskLimit
        let (owner, repo) = coreRepo
        let (mesh, snapshot, merged) = (meshEnabled, meshState, mergedPRs)
        let (tokens, deadline) = (tokensLeft, AppConfig.runDeadline)
        let directory = AgentSpawner.repoPath
        return await Task.detached(priority: .userInitiated) {
            let now = Date().timeIntervalSince1970
            let records = AgentRegistry.adoptPids(AgentRegistry.load())
            let evidence = AgentProbes.gather(records: records, now: now, owner: owner,
                                              repo: repo, directory: directory,
                                              meshEnabled: mesh, meshState: snapshot,
                                              merged: merged, tokens: tokens)
            let tick = AgentState.tick(records: records, evidence: evidence, now: now,
                                       limit: limit, deadline: deadline)
            var windows: [String: AgentWindows.Handle] = [:]
            for r in tick.records where !r.untracked {
                windows[r.runID] = AgentWindows.handle(r.runID)
            }
            return AgentPass(tick: tick, windows: windows)
        }.value
    }

    /// One tick, and the consequences of it: publish the rows, write back what was learned,
    /// retire what has ended, and report a probe that has gone quiet.
    ///
    /// Called from the process poll and from the display refresh — the two ticks that are
    /// meant to move the world on — and from nowhere that merely draws.
    @discardableResult
    func settleAgents() async -> AgentPass {
        let pass = await agentTick()
        Store.persistRunChanges(pass.tick.records)
        publish(pass)
        await retireFinished(pass.tick)
        noteSilentProbes()
        return pass
    }

    /// The rows and the cap load this pass produced.
    ///
    /// Runs that are over are left out (`AgentState.ended`), so the list this publishes
    /// starts at `.awaitingInput`.
    private func publish(_ pass: AgentPass) {
        let rows = pass.tick.rows
            .filter { !AgentState.ended.contains($0.1.state) }
            .map { AgentRow(record: $0.0, state: $0.1.state, reason: $0.1.reason,
                            window: pass.windows[$0.0.runID] ?? nil) }
        // Assigned only on a change, like every other value the 8-second poll re-derives:
        // `@Published` fires on assignment, not on difference, so an unconditional write
        // would redraw the panel on every tick of an idle machine.
        if agentRows != rows { agentRows = rows }
        if autoTasksMeasured != pass.tick.capLoad.count {
            autoTasksMeasured = pass.tick.capLoad.count
        }
    }

    /// Write back the three things a tick learns about a run: the pid its shell has since
    /// written, the tty its process turned out to be on, and when its mesh claim was last
    /// seen.
    ///
    /// Merged into whatever is on disk NOW rather than replacing the book with this tick's
    /// copy of it. A spawn that registered while this tick was resolving would otherwise be
    /// dropped — an agent nothing counts, which is a bay of the cap the machine can then
    /// spend twice.
    ///
    /// A synthesized row is APPENDED, having none on disk to merge into — that memory is
    /// the whole reason one is kept rather than re-derived from the process table every
    /// tick. Only a synthesized one: a tracked record missing from the book was retired
    /// while this tick resolved, and writing it back would raise the dead.
    private static func persistRunChanges(_ learned: [AgentState.RunRecord]) {
        var fresh = Dictionary(learned.map { ($0.runID, $0) },
                               uniquingKeysWith: { _, last in last })
        var out: [AgentState.RunRecord] = []
        var changed = false
        for r in AgentRegistry.load() {
            guard let f = fresh.removeValue(forKey: r.runID) else { out.append(r); continue }
            var merged = r
            if merged.pid == nil { merged.pid = f.pid }
            // The fresher one wins here, unlike the pid: a synthesized run's tty follows
            // whichever agent its PR's sighting currently names.
            if !f.tty.isEmpty { merged.tty = f.tty }
            if let seen = f.claimSeenAt { merged.claimSeenAt = seen }
            // Taken wholesale, unlike the three above: this pair is the only thing a
            // tick learns by comparing itself to the LAST one, so it is the only one
            // worthless unless written down. Unpersisted, every tick re-reads a screen
            // it has no memory of, the stillness clock restarts at zero, and the
            // twenty-minute backstop can never elapse however long an agent sits wedged.
            merged.quietDigest = f.quietDigest
            merged.quietSince = f.quietSince
            changed = changed || merged != r
            out.append(merged)
        }
        // What the drain above left in `fresh`: the rows with no line on disk to merge
        // into. Taken from `learned` rather than from the dictionary, for a stable order.
        let added = learned.filter { $0.untracked && fresh[$0.runID] != nil }
        out.append(contentsOf: added)
        if changed || !added.isEmpty { AgentRegistry.save(out) }
    }

    /// Price what has ended and drop it from the book.
    ///
    /// Only on positive evidence that the agent ended, or on the one clock the operator
    /// switched on for the runs no evidence reaches (Settings → STALLED AGENTS). The prompt
    /// comes out of the run directory, so an applet that restarted mid-agent can still
    /// attribute the run to its transcript; the in-memory list could not, and every such
    /// run landed in the ledger unpriced.
    ///
    /// A synthesized run is dropped here too, and priced by nothing: it has no ledger key,
    /// and no dispatch time, prompt or transcript to be priced from. Dropping it is the
    /// whole of what it needs, and it does need it: a record kept so the stillness
    /// backstop has a memory must not outlive the agent it remembers.
    ///
    /// A run whose window would not close is kept instead. Every reapable run is one
    /// this very tick saw ALIVE — the stillness rung only classifies a process already
    /// known to be up, and the deadline overrules the verdict that says so — so a refused
    /// close means the agent is still working in a window nothing here can reach.
    /// Retiring it writes a completion into the ledger against that agent and deletes the
    /// directory holding the prompt and the exit stamp that would have priced it
    /// properly.
    ///
    /// Keeping the record is also what re-arms the backstop. Retired, the run comes back
    /// as an UNTRACKED row — which `pastDeadline` refuses by name — so the deadline
    /// fired once against a window it could not close and then never again.
    ///
    /// The record does not linger: the reaper stamps the refusal, both backstops go
    /// quiet for one of their own periods (`AgentState.reapCooling`), and the run is a
    /// running one holding its bay again until the next attempt. When its agent finally
    /// leaves, no backstop stamps the verdict at all and it retires and prices on the
    /// ordinary road.
    private func retireFinished(_ t: AgentState.Tick) async {
        let refused = reapWedgedWindows(t)
        let gone = t.retirable.filter { !refused.contains($0.runID) }
        guard !gone.isEmpty else { return }
        let priced = Store.pricingInputs(gone)
        // Forgetting deletes every trace a run leaves — record, directory, prompt,
        // handle — so a retirement that was wrong is otherwise just a row that stopped
        // being there. This line is the only thing that says which rung decided.
        for r in gone {
            AuditLog.log(r.source, "retire",
                         "\(r.label.isEmpty ? r.runID : r.label) — \(t.states[r.runID]?.reason ?? "no verdict")")
        }
        AgentRegistry.forget(Set(gone.map(\.runID)))
        await settleLedger(priced)
    }

    /// Close the terminal of every run a backstop ended — the stillness clock, or the
    /// operator's run deadline.
    ///
    /// Only those, and the verdict is asked rather than reconstructed
    /// (`AgentState.reapable`). A run that finished the ordinary way keeps its window —
    /// its agent is alive at its prompt holding the whole task, and the operator may
    /// still want to read it. One whose screen has not changed in twenty minutes is
    /// nobody's, and so is one that has been going for four hours with nothing able to
    /// say it ever stopped.
    ///
    /// "`.finished`, and its stillness clock is past the timeout" is a WIDER set than
    /// that, because the clock advances only on ticks that saw the screen and so keeps
    /// maturing while nobody can look. An evidence outage that outlasts `quietTimeout`
    /// with the agent exiting inside it produces exactly one tick where a run is
    /// finished-because-its-pid-is-gone while carrying twenty-plus minutes of stillness
    /// — and `ttys<nnn>` is recycled freely, so the walk out of that tty may reach
    /// somebody else's terminal window by then.
    ///
    /// Closing it is not decoration on either verdict: an agent left alive is found again
    /// by the prompt scan the moment its record is retired, and comes straight back as an
    /// untracked row holding the same bay and the same PR, stripped of its label and with
    /// a completion recorded against it. So the window closing is what licenses the
    /// retirement, and a close that reached nothing withholds it.
    ///
    /// Returns the runs whose window would NOT close, and stamps the refusal on each so
    /// both backstops wait out a period before ending it again. `retireFinished` keeps
    /// those rather than pricing them; see there for why, and `AgentState.reapCooling`
    /// for what the stamp buys.
    ///
    /// A run nobody dispatched is reaped like any other: a wedged session is just as dead
    /// whether or not this applet opened it. Its window is reached the same two ways a
    /// clicked row's is (`activate`) — the handle a spawn recorded, else the agent's own
    /// process walked out to whatever terminal is showing it. A synthesized run has no run
    /// directory, so it never has a handle and the walk is its only route; a run the mesh
    /// placed back here is in the same position.
    private func reapWedgedWindows(_ t: AgentState.Tick) -> Set<String> {
        var refused: Set<String> = []
        for record in t.reapable {
            let byHandle = AgentWindows.handle(record.runID).map(AgentWindows.close) ?? false
            guard byHandle || TerminalFocus.close(tty: record.tty, pid: record.pid)
            else {
                refused.insert(record.runID)
                // Once per episode: the stamp is nil only before the first refusal.
                // A window nothing can close is retried for the life of the app, and
                // the operator needs its held bay explained once, not once per period.
                if record.reapRefusedAt == nil {
                    let who = record.label.isEmpty ? record.runID : record.label
                    AuditLog.log("auto", "kill-device",
                                 "could not close \(who)'s window — keeping the run "
                                 + "and trying again")
                }
                continue
            }
            let who = record.label.isEmpty ? record.runID : record.label
            let reason = t.states[record.runID]?.reason ?? ""
            let why = reason.components(separatedBy: "; ").last ?? ""
            AuditLog.log("auto", "kill-device", "closed \(who)'s window — \(why)")
        }
        if !refused.isEmpty {
            AgentRegistry.save(AgentRegistry.load().map { r -> AgentState.RunRecord in
                guard refused.contains(r.runID) else { return r }
                var stamped = r
                stamped.reapRefusedAt = t.now
                return stamped
            })
        }
        return refused
    }

    /// What pricing a finished run needs, read off disk while its run directory still exists.
    private struct Completion: Sendable {
        let key: String, prompt: String, runner: String
        let at: TimeInterval, done: TimeInterval, session: String
    }

    /// Read every pricing input for these runs. MUST be called before `forget` deletes
    /// their directories — all of it lives in there, and a record that is gone can never
    /// be retired, so an entry left open here stays open for good.
    ///
    /// The sentinel's mtime is when the agent actually exited; now() is whenever a poll got
    /// round to looking, which is up to a poll period later and would inflate every recorded
    /// run time. A run the mesh placed leaves no sentinel here — the executor writes its own
    /// — so that one is dated from the poll, which is the best this machine has.
    private static func pricingInputs(_ records: [AgentState.RunRecord]) -> [Completion] {
        let now = Date().timeIntervalSince1970
        return records.filter { !$0.ledgerKey.isEmpty }.map {
            Completion(key: $0.ledgerKey, prompt: AgentRegistry.prompt($0.runID),
                       runner: AgentRegistry.runRunner($0.runID),
                       at: $0.dispatchedAt, done: AgentRegistry.finishedAt($0.runID) ?? now,
                       session: AgentRegistry.boundSession($0.runID))
        }
    }

    /// Close these runs' ledger entries, pricing each from whatever ran it.
    private func settleLedger(_ completions: [Completion]) async {
        guard !completions.isEmpty else { return }
        // Scanning transcripts walks ~/.claude, so it stays off the main actor. The batch
        // is bound to a `let` first because capturing a mutable var in concurrently-
        // executing code is an error under the Swift 5.10 toolchain the macOS CI job builds
        // with — 6.x proves the mutation is finished and accepts it, so this only ever
        // fails away from the machine it was written on.
        let batch = completions
        await Task.detached(priority: .utility) {
            for f in batch {
                // Which transcript prices a run depends on what ran it, and the run says
                // which by what it left behind. A matched session under a foreign runner is
                // priced by that runner's own store — OpenCode through its exporter, Hermes
                // from the session row it keeps running totals on. Everything else is a
                // Claude Code run, found in ~/.claude by the prompt it opened with.
                let tokens: Double?
                var usd: Double?
                var model = ""
                switch (f.session.isEmpty, AgentRunner(rawValue: f.runner)) {
                case (false, .hermes):
                    tokens = HermesProbe.sessionTokens(sessionID: f.session)
                    // Hermes prices its own sessions in dollars, which is the unit the
                    // run was actually billed in — the tokens above buy a comparison
                    // with every other runner, this buys the gate that pauses before
                    // the money runs out.
                    (usd, model) = HermesProbe.sessionPrice(sessionID: f.session)
                case (false, .opencode):
                    tokens = UsageScan.opencodeTaskTokens(sessionID: f.session)
                default:
                    tokens = UsageScan.taskTokens(prompt: f.prompt, startedAt: f.at,
                                                  endedAt: f.done)
                }
                TelemetryLog.done(key: f.key, at: f.done, tokens: tokens,
                                  runner: f.runner, usd: usd, model: model)
            }
        }.value
        refreshTelemetry()
    }

    /// Say out loud when a probe has stopped answering.
    ///
    /// This is the failure with no symptom of its own. Every other bug here shows up as a
    /// wrong row; a probe going quiet shows up as rows that are merely *less certain*,
    /// which looks exactly like an applet working correctly. Left unsaid, the operator sees
    /// agents pile up holding bays and has no way to know that the reason is an automation
    /// permission revoked an hour ago.
    ///
    /// Once per probe per episode, cleared when it answers again — the same shape as the
    /// at-capacity note, for the same reason.
    private func noteSilentProbes() {
        for h in AgentProbes.health() {
            let was = probeWarned[h.name] ?? false
            if h.silent, !was {
                probeWarned[h.name] = true
                AuditLog.log("auto", "probe-silent",
                             "Agent \(h.name) \(h.reason.isEmpty ? "cannot be read" : h.reason)"
                             + " — agent rows fall back to whatever weaker evidence is left"
                             + " and keep their slots until it answers again")
                refreshAudit()
            } else if !h.silent, was {
                probeWarned[h.name] = false
                AuditLog.log("auto", "probe-recovered", "Agent \(h.name) readable again")
                refreshAudit()
            }
        }
        noteStaleBusyMarker()
        noteBlindBudgetGate()
    }

    /// Say out loud when the budget gate has nothing left to gate on.
    ///
    /// What is left of the rate-limit windows comes from one probe, and `budgetDecide`
    /// SKIPS a ceiling it cannot read — with neither window readable every task is
    /// affordable. So a probe that stops answering does not fail the gate closed or
    /// loudly: it stops gating, while the toggle still reads on and nothing else looks
    /// wrong. A stale `.credentials.json` did exactly that here for four days, ending
    /// in a night of agents dispatched into an exhausted weekly window.
    ///
    /// Only what was measured is stated: rounds asked, none answered. Whether that is a
    /// dead credential, a revoked login or an endpoint outage is the operator's to find
    /// out, and the wording must not guess.
    private func noteBlindBudgetGate() {
        let (rounds, readings) = Quota.probeStats()
        guard readings == 0, rounds >= Store.quotaSample, !quotaWarned,
              AutoBudget.enabled else { return }
        quotaWarned = true
        AuditLog.log("auto", "warn",
                     "Asked what is left of the rate-limit windows \(rounds) times "
                     + "without one answer — the automatic budget is gating on nothing, "
                     + "and auto work will dispatch whatever the limits have left")
        refreshAudit()
    }

    /// How many quota probe rounds must come back empty before a blind budget gate is
    /// called out. Low, because unlike `markerSample` there is no innocent machine that
    /// produces this reading: a probe that is switched off or logged out never rounds at
    /// all, so every round counted here is one that asked and was refused. Matches the
    /// Linux applet's threshold.
    private static let quotaSample = 20

    /// Say out loud when no CLI's interrupt hint has ever once matched.
    ///
    /// Telling a working agent from one waiting at its prompt rests on literal strings
    /// borrowed from someone else's UI (`AgentActivity.busyMarkers`). If the runner in use
    /// rewords its status bar, every agent reads as idle at once: every bay of the cap
    /// frees, and the monitors dispatch a burst onto a machine that is already full.
    /// Nothing else on this screen would look wrong.
    ///
    /// So a machine that has read plenty of agent screens and never seen a hint on any of
    /// them is reported. It is not proof — every agent really can be idle — which is why
    /// the threshold is high and the wording says what was measured rather than what it
    /// means.
    private func noteStaleBusyMarker() {
        let (read, seen) = AgentProbes.markerStats()
        guard seen == 0, read >= Store.markerSample, !markerWarned else { return }
        markerWarned = true
        let markers = AgentActivity.busyMarkers.map { "“\($0)”" }.joined(separator: " / ")
        AuditLog.log("auto", "warn",
                     "Read \(read) agent screens without once seeing \(markers) — if the CLI "
                     + "reworded it, every agent now reads as idle and the task cap will not "
                     + "hold")
        refreshAudit()
    }

    /// How many agent screens must be read with no interrupt hint on any of them before the
    /// marker is called stale. High, because every agent on a quiet machine really can be
    /// at its prompt. Matches the Linux applet's threshold.
    private static let markerSample = 40

    /// Which probes have had their silence reported, and whether the stale-marker and
    /// blind-budget warnings have been given. All latch once per episode; the probe one
    /// clears when that probe answers again, the other two do not — a machine that has
    /// read that many screens without a single hint, or asked that many times without a
    /// single answer, says so once and then stops.
    private var probeWarned: [String: Bool] = [:]
    private var markerWarned = false
    private var quotaWarned = false

    /// Which of the tracked PRs GitHub calls MERGED, carried forward by the fast ticks
    /// between the slow refreshes that probe it. `.unavailable` until the first, which
    /// reads as "nothing is known about any PR" rather than as "none of them landed".
    private var mergedPRs: Observation<Set<Int>> = .unavailable("have not been probed yet")

    /// Whether the account still has room to spend, carried forward the same way and for
    /// the same reason: the probe behind it dials an endpoint, and the tick runs on every
    /// repaint.
    private var tokensLeft: Observation<Bool> = .unavailable("has not been probed yet")

    // MARK: PR auto-fix monitor

    /// How often the monitor polls GitHub. 3 min by default — the GraphQL rate limit
    /// (5000 points/hr) is real and these searches aren't cheap, so a tight cadence blows
    /// the budget. Responsiveness comes from the immediate poll on wake / on enable, not
    /// from a fast steady cadence. Override for testing.
    static var autofixPollInterval: TimeInterval {
        let secs = ProcessInfo.processInfo.environment["DIPLOMAT_AUTOFIX_SECS"].flatMap(Double.init)
        return max(60, secs ?? 3 * 60)
    }
    private var autofixMonitorTask: Task<Void, Never>?
    private var wakeObserver: NSObjectProtocol?

    private func startAutofixMonitor() {
        guard autofixMonitorTask == nil else { return }
        autofixMonitorTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.runAutofixPollOnce()
                let ns = UInt64(Store.autofixPollInterval * 1_000_000_000)
                try? await Task.sleep(nanoseconds: ns)
            }
        }
        // The poll loop's sleep is suspended while the Mac sleeps, so a review that arrives
        // overnight would otherwise wait until the next tick after wake (once cost #462 an
        // hour). Poll immediately on wake so we catch up the moment we're back.
        if wakeObserver == nil {
            wakeObserver = NSWorkspace.shared.notificationCenter.addObserver(
                forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
            ) { [weak self] _ in
                Task { await self?.runAutofixPollOnce() }
            }
        }
    }

    /// Guards `runAutofixPollOnce` against overlap. The poll suspends at every gh
    /// fetch and agent spawn, and its dedup state (in-flight processes, attempt
    /// records, fingerprints) is only committed after those suspensions — so two
    /// interleaved polls (timer tick + wake + Settings-open + toggle-enable all kick
    /// one) could each see "no agent on #N" and double-dispatch. @MainActor makes
    /// this flag race-free.
    private var autofixPollInFlight = false

    /// Work keys a peer's agent already owns, so the "claimed elsewhere" note is
    /// logged once per key rather than every poll (szpontnet-spec/docs/12).
    private var meshSuppressedLogged: Set<String> = []

    /// Set when the last monitor poll cycle failed (gh/auth/network), so persistent
    /// breakage is visible in Settings instead of silently freezing stale counts.
    /// Cleared by the next fully-successful cycle.
    @Published var autofixPollError: String?
    @Published var autofixPollErrorAt: Date?
    /// Failure recorded by the sub-polls during the current cycle; evaluated once at
    /// the end of `runAutofixPollOnce` (so one failing sub-poll can't be masked — or
    /// re-audit-logged every tick — by the other succeeding).
    private var pollErrorThisCycle: String?

    private func notePollFailure(_ error: Error) {
        pollErrorThisCycle = (error as? LocalizedError)?.errorDescription ?? "\(error)"
    }

    /// One poll: fetch my open PRs, diff against saved fingerprints, and dispatch an
    /// agent for each PR that just gained a conflict or new review work. No-op until
    /// our login is known.
    ///
    /// Both monitors poll on every cycle, switched on or off. A switched-off one
    /// still finds what it would have done and queues it (`isPaused`) — the panel is
    /// where the operator sees what their PRs owe, and that question does not go away
    /// with the toggle that answers it automatically. Nothing of a paused monitor's is
    /// started here; only "execute now" starts it.
    func runAutofixPollOnce() async {
        guard !autofixPollInFlight else { return }
        autofixPollInFlight = true
        defer { autofixPollInFlight = false }
        pollErrorThisCycle = nil
        if me.isEmpty { await fetchMe() }
        if effectiveMe.isEmpty {
            // The most common total-breakage mode (gh missing/unauthenticated) used to
            // bail before the failure surfacing — the toggles said "on" and nothing
            // ever polled, silently.
            pollErrorThisCycle = "GitHub login unknown — is `gh` installed and authenticated?"
        } else {
            let (owner, repo) = coreRepo
            // One fetch of my PRs per cycle, taken before anything acts on it: the
            // drain re-checks the waiting queue against it and the reconcilers below
            // diff from the same list. Fetching it here rather than inside the monitor
            // is what lets the queue be checked at all — the drain runs first, and the
            // whole point of the check is that it reads THIS cycle's evidence, not the
            // one the queue was built from.
            let snaps = await fetchMySnapshots(owner: owner, repo: repo)
            // And which PRs have left the open state — the one thing the fetch above
            // cannot answer, because it lists what is open and the queue has to be
            // checked against what is not. Every verb answers to it, the operator's
            // own ask included (`AgentTaskQueue.stillOwed`).
            let closed = await fetchClosedPRs(owner: owner, repo: repo)
            // The queue first, in the operator's order: a slot that freed since the
            // last cycle belongs to work already waiting for it, not to whichever PR
            // this poll's fetch happens to return first.
            //
            // Only on evidence a cycle actually confirmed. The queue survives a failed
            // cycle deliberately (see `stagedQueue`), but surviving is not the same as
            // being current: while `gh` is down the list freezes, and a drain that kept
            // firing from it would spawn agents at work answered by hand hours ago. A
            // fetch that failed just now is the same blindness one cycle earlier, so it
            // holds the drain too — either of them, since a missing answer is not a
            // pass.
            if autofixPollError == nil, let snaps, let closed {
                await drainQueuedTasks(snaps: snaps, closed: closed)
            }
            // Start this cycle's staging empty. A commit clears it too, so this is
            // what discards the offers of a cycle that failed part-way and never
            // committed — they are re-offered by the cycle that succeeds.
            stagedQueue = []
            if let snaps { await pollMyPRs(snaps: snaps) }
            await pollReviewRequests(owner: owner, repo: repo)
            // Last, so the monitors' finds keep the places they were found in: the
            // operator's own sweep bands behind them anyway, and a fifty-item ask
            // offered first would otherwise decide the arrangement of a queue it is
            // meant to wait in.
            offerRequestedWork()
            // A cycle that failed part-way knows what it fetched, not what is owed —
            // committing then would drop every task the failing half would have
            // re-offered, and with it the operator's arrangement of them.
            if pollErrorThisCycle == nil { commitQueue() }
        }
        if let e = pollErrorThisCycle {
            // Audit only the transition into failure, not every 3-minute tick.
            if autofixPollError == nil {
                AuditLog.log("auto", "poll-failed", "Monitor poll failing: \(e.prefix(120))")
                refreshAudit()
            }
            autofixPollError = e
            autofixPollErrorAt = Date()
        } else if autofixPollError != nil {
            AuditLog.log("auto", "poll-recovered", "Monitor polls succeeding again")
            refreshAudit()
            autofixPollError = nil
            autofixPollErrorAt = nil
        }
    }

    /// This cycle's read of my open PRs, or `nil` if the read failed — in which case
    /// the failure is already noted and every consumer of it stands down.
    private func fetchMySnapshots(owner: String, repo: String) async -> [PRSnapshot]? {
        do {
            return try await AutofixMonitor.fetchSnapshots(owner: owner, repo: repo, me: effectiveMe)
        } catch {
            notePollFailure(error)   // leave state as-is, retry next tick
            return nil
        }
    }

    /// This cycle's read of the PRs that have merged or closed, or `nil` if the read
    /// failed — which is not the same as an empty set, and is why the failure stands
    /// the drain down rather than letting it read every PR as open.
    private func fetchClosedPRs(owner: String, repo: String) async -> Set<Int>? {
        do {
            return try await AutofixMonitor.fetchClosedPRs(owner: owner, repo: repo)
        } catch {
            notePollFailure(error)
            return nil
        }
    }

    /// My own PRs: dispatch on new conflicts / new review work. Edge-triggered for the
    /// real-time case (a transition observed live), plus a level-triggered reconcile pass
    /// so a review that landed while we were offline — and so was already present the first
    /// time we saw the PR (which the edge-trigger silently baselines) — still gets an agent.
    private func pollMyPRs(snaps: [PRSnapshot]) async {
        let (events, fingerprints) = AutofixDiff.compute(prior: loadAutofixFingerprints(), now: snaps)
        for event in events { await dispatchAutofix(event) }
        saveAutofixFingerprints(fingerprints)
        // Record what is owed BEFORE reconciling, so a unit of work is queued in the
        // ledger before the same poll can start it — otherwise the first dispatch of
        // every item would look like it came from nowhere and its time-to-start would
        // be unmeasurable.
        TelemetryLog.observeOwed(
            kind: AutofixMesh.kindReviewReply, duty: "review",
            owed: Dictionary(snaps.filter { $0.threadsIOwe > 0 }.map {
                (AutofixMesh.ledgerKey(kind: AutofixMesh.kindReviewReply,
                                       prURL: $0.url, headSha: $0.headSha), $0.number)
            }, uniquingKeysWith: { first, _ in first }))
        TelemetryLog.observeOwed(
            kind: AutofixMesh.kindConflicts, duty: "conflicts",
            owed: Dictionary(snaps.filter { $0.mergeable == "CONFLICTING" }.map {
                (AutofixMesh.ledgerKey(kind: AutofixMesh.kindConflicts,
                                       prURL: $0.url, headSha: $0.headSha), $0.number)
            }, uniquingKeysWith: { first, _ in first }))
        await reconcileMyReviews(snaps: snaps, now: Date())
        await reconcileMyConflicts(snaps: snaps, now: Date())
        autofixStatus = AutofixStatus(
            updatedAt: Date(), watching: snaps.count,
            conflictsHandled: autofixConflictsHandled, reviewsHandled: autofixReviewsHandled)
    }

    /// Level-triggered safety net for reviews received on MY PRs: any PR of mine that
    /// currently carries unresolved review threads but has no agent on it is an unaddressed
    /// review — (re)dispatch a fix agent as soon as it's possible, deduped by in-flight +
    /// retry backoff (`ReviewReconcile`) so it never loops. This catches exactly what the
    /// edge-trigger misses: a review already present when we first saw the PR (landed while
    /// offline / a PR opened before the monitor was watching / a spawn that failed). When
    /// the threads get resolved the PR drops out and its record is pruned.
    private func reconcileMyReviews(snaps: [PRSnapshot], now: Date) async {
        var attempts = loadMyReviewAttempts()
        let owed = snaps.filter { $0.threadsIOwe > 0 }
        for s in owed {
            let key = String(s.number)
            let inFlight = await self.inFlight(s.number)
            let decision = ReviewReconcile.decide(prior: attempts[key],
                                                  stamp: AttemptStamp.unresolvedReview,
                                                  inFlight: inFlight, banned: false, now: now)
            if case .dispatch(let attemptNumber) = decision {
                if await dispatchMyReview(s, attemptNumber: attemptNumber) {
                    attempts[key] = ReviewAttempt(requestedAt: AttemptStamp.unresolvedReview,
                                                  lastDispatchedAt: now, attempts: attemptNumber)
                }
            }
        }
        let owedKeys = Set(owed.map { String($0.number) })
        attempts = attempts.filter { owedKeys.contains($0.key) }
        saveMyReviewAttempts(attempts)
    }

    /// Level-triggered reconcile for conflicts on MY PRs, mirroring `reconcileMyReviews`:
    /// any PR of mine that GitHub currently reports CONFLICTING and that has no agent on
    /// it gets a Resolve-conflicts agent. A spawn that failed (e.g. terminal-automation
    /// permission revoked) leaves no attempt record, so it retries on every poll tick
    /// until an agent launches (and is audit-logged each time); the `ReviewReconcile`
    /// backoff then paces re-dispatches of a conflict a launched agent didn't clear. A
    /// conflict that already existed when the monitor first saw the PR (which the
    /// edge-trigger baselines) still gets an agent. The record is pruned once the PR is
    /// known mergeable again.
    private func reconcileMyConflicts(snaps: [PRSnapshot], now: Date) async {
        var attempts = loadMyConflictAttempts()
        let conflicted = snaps.filter { $0.mergeable == "CONFLICTING" }
        for s in conflicted {
            let key = String(s.number)
            let inFlight = await self.inFlight(s.number)
            let decision = ReviewReconcile.decide(prior: attempts[key],
                                                  stamp: AttemptStamp.conflicting,
                                                  inFlight: inFlight, banned: false, now: now)
            if case .dispatch(let attemptNumber) = decision {
                if await dispatchConflictFix(number: s.number, url: s.url,
                                             attemptNumber: attemptNumber, source: .auto,
                                             headSha: s.headSha).wasHandled {
                    attempts[key] = ReviewAttempt(requestedAt: AttemptStamp.conflicting,
                                                  lastDispatchedAt: now, attempts: attemptNumber)
                }
            }
        }
        // Prune only when the PR is known NOT conflicting. GitHub transiently reports
        // UNKNOWN while recomputing mergeability (after any push to main) — pruning on
        // that flap would reset the backoff and double-count the same conflict when it
        // comes back as CONFLICTING a poll later.
        let keepKeys = Set(snaps.filter { $0.mergeable != "MERGEABLE" }.map { String($0.number) })
        attempts = attempts.filter { keepKeys.contains($0.key) }
        saveMyConflictAttempts(attempts)
    }

    private func loadMyConflictAttempts() -> [String: ReviewAttempt] {
        guard let data = UserDefaults.standard.data(forKey: Keys.myConflictAttempts),
              let decoded = try? JSONDecoder().decode([String: ReviewAttempt].self, from: data)
        else { return [:] }
        return decoded
    }
    private func saveMyConflictAttempts(_ map: [String: ReviewAttempt]) {
        persistJSON(map, forKey: Keys.myConflictAttempts)
    }

    /// PRs that request MY review: dispatch the most-comprehensive review whenever I OWE
    /// one — i.e. the latest "review requested from me" is newer than my last review of
    /// that PR. Robust to re-requests (a fresh request re-qualifies even after I reviewed
    /// once) and does NOT depend on observing a "request removed" transition, which a
    /// re-request can slip past.
    ///
    /// Crucially, the local "we dispatched an agent" record no longer suppresses a review
    /// *forever*: a dispatched agent can die, hit an API error, or have its window closed
    /// without ever leaving a review, in which case GitHub still shows the review owed and
    /// no agent is running — an *unaddressed* review. `ReviewReconcile` re-dispatches those
    /// as soon as it's possible (no in-flight agent, retry backoff elapsed), so a slip
    /// never leaves a review permanently unanswered.
    private func pollReviewRequests(owner: String, repo: String) async {
        let reqs: [AutofixMonitor.ReviewRequest]
        do {
            // Only pull changed-file paths (a big slice of the query cost) when auto-approvals
            // are on — they're only used to gate the verdict, which is off by default.
            reqs = try await AutofixMonitor.fetchReviewRequests(owner: owner, repo: repo,
                                                                me: effectiveMe,
                                                                includeFiles: autoApproveEnabled)
        } catch {
            notePollFailure(error)
            return
        }
        let banned = BanList.read()
        let now = Date()
        var attempts = loadReviewReqAttempts()   // prNumber -> our attempt record
        let owed = reqs.filter { $0.oweReview }
        // Before dispatching, so the ledger has a queue instant to measure the
        // time-to-start against. A banned author's request is owed by GitHub's
        // reckoning but will never be dispatched, so it is left out — counting it
        // would show a review pending forever that nothing is meant to pick up.
        TelemetryLog.observeOwed(
            kind: AutofixMesh.kindReviewReq, duty: "review",
            owed: Dictionary(owed.filter { !BanList.isBanned($0.author, in: banned) }.map {
                (AutofixMesh.ledgerKey(kind: AutofixMesh.kindReviewReq,
                                       prURL: $0.url, headSha: $0.headSha), $0.number)
            }, uniquingKeysWith: { first, _ in first }))
        for r in owed {
            let key = String(r.number)
            let stamp = AttemptStamp.reviewRequest(r)
            let decision = ReviewReconcile.decide(
                prior: attempts[key], stamp: stamp, inFlight: await inFlight(r.number),
                banned: BanList.isBanned(r.author, in: banned), now: now)
            switch decision {
            case .skipBanned, .skipInFlight, .skipCoolingDown:
                continue
            case .dispatch(let attemptNumber):
                // Record the attempt (start the retry backoff) only if an agent actually
                // launched — a transient spawn failure should retry next tick, not sit out
                // a 5m–3h cooldown while the review stays unanswered.
                if await dispatchReviewRequest(r, attemptNumber: attemptNumber) {
                    attempts[key] = ReviewAttempt(requestedAt: stamp, lastDispatchedAt: now,
                                                  attempts: attemptNumber)
                }
            }
        }
        // Keep each dispatch record until it ages past the backoff ceiling — NOT the moment
        // the review lands. A force-push dismisses my review (briefly un-owing it) then
        // re-requests; retaining the record across that flap lets `reRequestCooldown`
        // recognise the re-request as churn instead of a fresh request. Aged-out records are
        // dropped so the store can't grow unbounded and a real future re-request is fresh.
        attempts = attempts.filter {
            now.timeIntervalSince($0.value.lastDispatchedAt) < ReviewReconcile.retryMaxBackoff
        }
        saveReviewReqAttempts(attempts)
        // Reviews still owed with no agent on them AFTER this poll — the ones a freshly
        // spawned agent didn't cover (cooling down between retries, or a spawn that failed).
        // Excludes banned authors, which we never auto-review.
        var unaddressed = 0
        for r in owed where !BanList.isBanned(r.author, in: banned) {
            if await !inFlight(r.number) { unaddressed += 1 }
        }
        unaddressedReviews = unaddressed
    }

    /// Re-read the prompt-injection ban list (cheap local file). Publishes on change.
    func refreshBanList() {
        let next = BanList.read()
        if next != bannedAuthors { bannedAuthors = next }
    }
    /// Re-read the audit log's tail. The file IO runs off-main (the log grows without
    /// bound, and this fires on the 8s panel poll); publishes on change.
    func refreshAudit() {
        Task { [weak self] in
            let next = await Task.detached(priority: .utility) { AuditLog.read() }.value
            guard let self else { return }
            if next != self.auditEntries { self.auditEntries = next }
        }
    }
    /// Remove a ban (the UI's un-ban button) and refresh. The daemon round-trip
    /// (curl over the unix socket, up to 5s against a wedged daemon) runs off-main
    /// so the popover can't freeze. When the daemon handled the unban it also wrote
    /// the audit entry — don't double-log.
    func unban(_ login: String) {
        Task { [weak self] in
            let viaDaemon = await Task.detached(priority: .userInitiated) {
                BanList.unban(login)
            }.value
            guard let self else { return }
            if !viaDaemon { AuditLog.log("panel", "unban", "Un-banned @\(login)") }
            self.refreshAudit()
            self.refreshBanList()
        }
    }

    /// Does this PR already have an agent, for the monitors' dedup?
    ///
    /// Every state that is not over counts, including one waiting at its prompt (that
    /// session holds the PR's context) and one nothing is known about — releasing a PR on
    /// missing evidence is how two agents end up on it.
    private func inFlight(_ prNumber: Int) async -> Bool {
        await agentTick().tick.inFlight(prNumber: prNumber)
    }

    /// How many bays of this device's cap the last tick found held, published so the panel
    /// can draw the free slots without resolving again.
    @Published private(set) var autoTasksMeasured = 0

    /// Re-resolve for the display alone, and act on what it finds.
    ///
    /// The panel calls it on its own tick, including the ticks where nothing is registered:
    /// an agent can be alive with no record behind it (one this applet never spawned), and
    /// that is exactly when a wrongly-drawn free bay would be most misleading.
    func refreshAutoTaskCount() async { await settleAgents() }

    /// Pin the measurement, for headless self-tests only. The real one reads `ps` on
    /// whatever machine is running the test, so an assertion about free slots would
    /// otherwise pass or fail on how many agents the developer happens to have open.
    func pinAutoTasksMeasured(_ n: Int) {
        guard Headless.active else { return }
        autoTasksMeasured = n
    }

    /// Pin the rows, for headless snapshots only. Resolved for real, a render would
    /// draw whichever of the developer's own agents and terminals happen to be up when
    /// the picture is taken.
    func pinAgentRows(_ rows: [AgentRow]) {
        guard Headless.active else { return }
        agentRows = rows
    }

    /// Drive `publish` over a tick a self-test composed itself.
    ///
    /// `pinAgentRows` above assigns the list directly, which is what a render needs and
    /// is also why no render covers `publish`: the filter that keeps an ended run off the
    /// panel is on a path the one artefact CI inspects never takes. This is the way in
    /// for the check that does (`PublishTest`).
    func publishForSelfTest(_ pass: AgentPass) {
        guard Headless.active else { return }
        publish(pass)
    }

    /// Slots of this device's cap with nothing in them, as the panel draws them.
    ///
    /// Work that is starting holds one. Its spawn has not registered anywhere yet, but the
    /// bay is spoken for — and drawn as free it would put a row that is launching next to
    /// the empty slot it is launching into, which is one row more than the cap allows.
    var freeAutoSlots: Int {
        AgentTaskQueue.freeSlots(limit: autoTaskLimit,
                                 running: autoTasksMeasured + startingTasks.count)
    }

    /// Whether the "deferring auto work" note has been logged for the current
    /// at-capacity episode, and for the current out-of-budget one. Two flags, not one:
    /// a machine can saturate and drain several times over inside a single spell of
    /// having nothing left to spend, and each episode is worth one line of its own.
    private var capacityLogged = false
    private var budgetLogged = false

    /// Note that automatic work is being held for want of budget — once per episode,
    /// like `logAtCapacity`, and cleared the moment a dispatch finds room again.
    /// Without that, a machine sitting under its floor would write one of these per
    /// owed PR per poll, for hours.
    private func logUnaffordable(_ budget: AgentDispatchGate.Budget) {
        guard !budgetLogged else { return }
        budgetLogged = true
        AuditLog.log("auto", "no-budget",
                     "Deferring auto work — " + AutoBudget.shortfall(budget))
        refreshAudit()
    }

    /// Note that automatic work is being held back — once per episode, not once per
    /// PR per poll. Cleared the moment a dispatch finds room again, so the feed gets
    /// one line when the device saturates and another when it drains.
    private func logAtCapacity() {
        guard !capacityLogged else { return }
        capacityLogged = true
        let limit = autoTaskLimit
        AuditLog.log("auto", "at-capacity",
                     "Deferring auto work — this machine already runs its cap of "
                     + "\(limit) automatic task\(limit == 1 ? "" : "s")")
        refreshAudit()
    }

    /// The `ReviewAttempt.requestedAt` stamp each monitor files its dispatches
    /// under. The two level-triggered reconcilers have no GitHub timestamp to use —
    /// the PR simply is or isn't in the state they watch — so a constant stands in.
    ///
    /// Single-sourced because two places write the same stamp: the reconciler when it
    /// dispatches, and the queue when it runs a dispatch the cap deferred
    /// (`AgentJob.attemptStamp`). Two spellings of "conflicting" would
    /// not fail anything loudly — `ReviewReconcile` would just read the queue's
    /// record as a *different* request and hold the retry for the 1h re-request
    /// cooldown instead of the 5m→3h ladder.
    enum AttemptStamp {
        static let unresolvedReview = "unresolved"
        static let conflicting = "conflicting"
        /// A review request has a real timestamp; `"-"` is the unknown-stamp
        /// sentinel `ReviewReconcile` already documents.
        static func reviewRequest(_ r: AutofixMonitor.ReviewRequest) -> String {
            r.requestedAt ?? "-"
        }
    }

    // MARK: - The queue behind the cap
    //
    // A refusal writes no attempt record, so every poll re-offers whatever GitHub
    // still owes: that is where the queue's contents come from, and why nothing here
    // is a second copy of monitor state. What the queue adds is a list the panel can
    // show and the operator can arrange, drained in THEIR order at the top of a cycle,
    // before the monitors go looking for more.
    //
    // Two holds put work here. The device's cap holds work it has no slot for, and
    // the drain releases it as slots free. A switched-off monitor holds its own work
    // indefinitely: it is queued so the panel can show what the PRs owe, and only a
    // click ("execute now") or the toggle coming back on starts it.

    /// Remember one at-capacity refusal as a queued task. Called from the single
    /// dispatch gate, so every deferral is queued exactly once however it was
    /// triggered — the two reconcilers, the review-request monitor, or the review
    /// edge-trigger.
    ///
    /// A second offer of the same key within one poll replaces the first: the
    /// reconcilers run after the edge-trigger and carry the backoff-aware attempt
    /// number, so theirs is the job that should run.
    private func stageQueued(_ job: AgentJob, attemptNumber: Int) {
        // Only PR-scoped work with a monitor behind it is queued by a REFUSAL: a task
        // nothing can name is one the next poll cannot recognise as the same one, and a
        // task no monitor owns is one nothing would re-offer from here — so the refusal
        // would be the only record of it, and a poll that never happened would lose it.
        // (Every automatic job is both. Work the operator asked for is neither, and
        // is re-offered from the list that remembers the ask instead —
        // `offerRequestedWork`. A wizard click is uncapped and never refused.)
        guard let number = job.prNumber, job.counter != nil else { return }
        let entry = QueuedAgentTask(
            id: AgentTaskQueue.key(auditAction: job.auditAction, number: number),
            job: job, attemptNumber: attemptNumber)
        if let i = stagedQueue.firstIndex(where: { $0.id == entry.id }) {
            stagedQueue[i] = entry
        } else {
            stagedQueue.append(entry)
        }
    }

    /// Publish this poll's deferrals as the queue, arranged by the operator's saved
    /// order. Called only after a fully successful cycle — see `stagedQueue`.
    ///
    /// Not private: `QueueTest` commits a cycle directly, which is the only way to
    /// exercise this without a live GitHub fetch.
    ///
    /// Committing also ENDS the cycle. Leaving the staging behind would carry this
    /// poll's offers into the next one, where they would read as work still owed —
    /// so a task would never leave the queue once it entered.
    func commitQueue() {
        let staged = stagedQueue
        stagedQueue = []
        let ordered = AgentTaskQueue.order(offered: staged.map(\.id), saved: queuedTaskOrder)
        queuedTaskOrder = ordered
        // Work already starting is still offered — the attempt record that stops it
        // being offered is written when its spawn answers — so a poll landing mid-
        // dispatch would draw it a second time, back in the queue it just left. It
        // keeps its place in the saved ARRANGEMENT, because a start that fails is
        // re-offered, and dropping the key here would send it to the back.
        let starting = Set(startingTasks.map(\.id))
        queuedTasks = ordered.compactMap { id in
            starting.contains(id) ? nil : staged.first { $0.id == id }
        }
    }

    /// Run the queue down into whatever room this device has, in the operator's
    /// order. This is what makes the drag order mean anything: it runs at the TOP of
    /// a poll, before the monitors offer their own finds, so a slot that freed since
    /// the last cycle goes to the work already waiting for it rather than to whatever
    /// this poll's fetch happens to list first.
    ///
    /// The list is re-checked against `snaps` — this cycle's read of my PRs — and
    /// against `closed`, the PRs that have left the open state, before any of it is
    /// run: a queued task carries the verdict of the poll that staged it, which is as
    /// old as a whole poll period by the time a bay frees, and what filled that bay in
    /// the meantime was an agent working one of these same branches — or the author,
    /// landing the PR. Work the fetch no longer owes, and work on a PR that is no
    /// longer there to work on, leaves the list instead of spawning
    /// (`AgentTaskQueue.stillOwed`).
    ///
    /// That pass covers the whole queue, not the part the drain reaches: a row
    /// standing for work somebody already did is wrong on the panel exactly as it is
    /// wrong to start, and it is the rows of a machine with no room — which returns
    /// below on its first entry — that sit there longest.
    ///
    /// Capacity is re-counted per task because each spawn fills a slot. A spawn
    /// failure stops the drain: it means terminal automation is broken, not that this
    /// one task was unlucky, and each entry is taken off the list before it is tried —
    /// so walking the whole queue into the same failure would clear the panel of every
    /// queued row at once, for a reason none of them caused.
    ///
    /// Not private: the refresh pass is what `QueueTest` drives. It runs before the
    /// capacity guard and starts nothing by itself, so a self-test at capacity — the
    /// state that whole test sets up — exercises it without a spawn.
    func drainQueuedTasks(snaps: [PRSnapshot], closed: Set<Int>) async {
        let conflicting = Set(snaps.filter { $0.mergeable == "CONFLICTING" }.map(\.number))
        let owingReply = Set(snaps.filter { $0.threadsIOwe > 0 }.map(\.number))
        // Dropped here rather than left for this cycle's commit to omit: the commit is
        // the far side of two monitor runs, and a row already known to be answered
        // should not be sitting in the list underneath them, one "execute now" away
        // from spawning. Paused work is swept too — a switched-off monitor's row is
        // still a claim about what the PR owes.
        //
        // A task with no PR to ask about stands: that is a Fix-issues ask, numbered
        // in the issue space, and pricing issue #421 against the PRs closed this cycle
        // would retire it for something that happened to the PR of the same number.
        let answered = queuedTasks.filter { entry in
            guard let number = entry.job.prNumber else { return false }
            return !AgentTaskQueue.stillOwed(auditAction: entry.job.auditAction,
                                             prNumber: number,
                                             conflicting: conflicting,
                                             owingReply: owingReply,
                                             closed: closed)
        }
        for entry in answered {
            // An ask outlives its row by design — the row is rebuilt from it on every
            // poll — so it has to be forgotten as well as dropped, or this same cycle
            // offers it straight back. Logged because it is the one retirement neither
            // a dispatch nor the operator is behind: a row that vanished in silence
            // reads exactly like the review having run.
            if entry.job.requested {
                forgetRequested(entry.id)
                AuditLog.log("panel", "queue-drop",
                             "\(entry.job.label) — PR no longer open, not run")
            }
            dropQueuedTask(entry.id)
        }
        for entry in drainableTasks {
            // The list moves under this loop: it awaits a spawn per task, and an
            // "execute now" during one of those takes its row off the queue and
            // starts it there and then. Re-reading the queue is what keeps the drain
            // from dispatching that same task a second time when it reaches it.
            guard queuedTasks.contains(where: { $0.id == entry.id }) else { continue }
            guard await agentTick().tick.capLoad.count < autoTaskLimit else { return }
            // Finding room here is what re-arms the saturation notice. The gate's own
            // reset sits behind the capacity measurement this path skips, so without
            // this the feed would carry one `at-capacity` line for an unbounded run of
            // saturate-and-drain episodes instead of one apiece.
            capacityLogged = false
            switch await runQueuedTask(entry, forced: false) {
            case .failed: return
            // Every remaining entry would be priced against the same windows and get
            // the same answer, and the dispatch has already re-staged this one for the
            // commit at the end of the cycle. Draining on would cost a round of
            // refusals to no end.
            case .unaffordable: return
            default: break
            }
        }
    }

    // MARK: - the work the operator asks for
    //
    // A Review-PRs or Fix-issues sweep is expanded here into one queued task per PR /
    // per issue, rather than handed to a single agent as "review every draft PR of
    // mine" or "fix every open issue". On a repo with fifty of either that agent is
    // fifty jobs in one session: one context, one terminal, one machine, and no way to
    // see where it has got to or to stop it halfway. Split, each item is a row of the
    // Agent-tasks list, gets a bay of the task cap to itself, and runs when the cap
    // has room — behind the monitors' finds, ahead of the conflict fixes
    // (`AgentTaskQueue.band`).

    /// The config behind one ask, and so which of the two sweeps asked for it.
    ///
    /// Everything downstream reads an ask through `action` / `noun` / `depth` /
    /// `buildPrompt`, so the queue, the panel and the drain never branch on which kind
    /// it is — the one place that does is `requestedTask`, where a PR is dedup-able
    /// work and an issue is not.
    enum RequestedConfig: Equatable {
        case review(ReviewConfig)
        case issues(IssueConfig)

        /// The queue verb this ask waits under, which is also its `AgentJob.kind`,
        /// its mesh duty and the verb its dispatch writes to the activity feed.
        var action: String {
            switch self {
            case .review: return AgentTaskQueue.reviewAction
            case .issues: return AgentTaskQueue.issuesAction
            }
        }

        /// What the row calls itself — the wizard's own word for the work.
        var noun: String {
            switch self {
            case .review: return "Review"
            case .issues: return "Issues"
            }
        }

        var depth: String {
            switch self {
            case .review(let cfg): return cfg.depth
            case .issues(let cfg): return cfg.depth
            }
        }

        func buildPrompt() -> String {
            switch self {
            case .review(let cfg): return cfg.buildPrompt()
            case .issues(let cfg): return cfg.buildPrompt()
            }
        }
    }

    /// One PR a Review-PRs sweep asked to have reviewed, or one issue a Fix-issues
    /// sweep asked to have fixed, waiting for a free slot.
    ///
    /// This is the only work in the queue the applet has to REMEMBER. Everything else
    /// there is a monitor's find, re-derived from GitHub on every poll — but a PR
    /// records nothing about somebody having wanted it reviewed, nor an issue about
    /// somebody having swept it, so if this list is lost the ask is lost with it.
    /// Hence the whole config rather than a number: the prompt for each is assembled
    /// from it when the task is offered, exactly as the wizard would have assembled it
    /// at the moment of the click.
    struct RequestedWork: Codable, Equatable {
        /// The PR number, or the issue number — two numbering spaces, told apart by
        /// the verb in `key` and nowhere else.
        let number: Int
        let url: String
        /// Whose PR / issue — the pipeline's ban dimension. Empty where the sweep
        /// names nobody (my own PRs, an association scope).
        let author: String
        let config: RequestedConfig

        /// This ask's identity everywhere it has one: the queued task's id, the row
        /// the panel draws it as, and what `forgetRequested` drops it by.
        var key: String {
            AgentTaskQueue.key(auditAction: config.action, number: number)
        }

        /// The row this task wears in the panel and the activity feed. Carries the
        /// depth because that is the choice a sweep is worth re-reading later: the
        /// same PR queued from a `max` sweep and from a `quick` one are different jobs.
        var label: String { "\(config.noun) · #\(number) · \(config.depth)" }

        private enum CodingKeys: String, CodingKey { case number, url, author, action, config }

        init(number: Int, url: String, author: String, config: RequestedConfig) {
            self.number = number
            self.url = url
            self.author = author
            self.config = config
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            number = try c.decode(Int.self, forKey: .number)
            url = try c.decodeIfPresent(String.self, forKey: .url) ?? ""
            author = try c.decodeIfPresent(String.self, forKey: .author) ?? ""
            // No verb means a row written while this list held reviews alone, whose
            // `config` is the bare `ReviewConfig` it was stored as.
            let action = try c.decodeIfPresent(String.self, forKey: .action)
            if action == AgentTaskQueue.issuesAction {
                config = .issues(try c.decode(IssueConfig.self, forKey: .config))
            } else {
                config = .review(try c.decode(ReviewConfig.self, forKey: .config))
            }
        }

        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(number, forKey: .number)
            try c.encode(url, forKey: .url)
            try c.encode(author, forKey: .author)
            try c.encode(config.action, forKey: .action)
            switch config {
            case .review(let cfg): try c.encode(cfg, forKey: .config)
            case .issues(let cfg): try c.encode(cfg, forKey: .config)
            }
        }
    }

    /// One stored ask, decoded so that a row which will not read costs only itself.
    ///
    /// Decoding the array whole would make any single bad row empty the list, and the
    /// stored element is an entire `ReviewConfig` / `IssueConfig` — shared types that
    /// gain fields. The first release to add one would silently drop every ask a sweep
    /// left standing, which is the loss this list is persisted to prevent.
    private struct StoredAsk: Decodable {
        let work: RequestedWork?

        init(from decoder: Decoder) throws {
            work = try? decoder.singleValueContainer().decode(RequestedWork.self)
        }
    }

    private static func loadRequestedWork() -> [RequestedWork] {
        guard let data = UserDefaults.standard.data(forKey: Keys.requestedWork),
              let rows = try? JSONDecoder().decode([StoredAsk].self, from: data)
        else { return [] }
        return rows.compactMap(\.work)
    }

    /// Queue one review per PR `cfg` sweeps. Returns `(queued, already)`.
    ///
    /// The PRs come from the panel's own last fetch rather than a fresh one: it is the
    /// list the operator was looking at when they pressed the button, which is the list
    /// they meant. One that merges or closes before its turn comes is dropped by the
    /// drain (`AgentTaskQueue.stillOwed`); one the sweep should not have caught in the
    /// first place is what **cancel** is for.
    @discardableResult
    func requestReviewSweep(_ cfg: ReviewConfig) -> (queued: Int, already: Int) {
        let targets = Filters.sweptPRs(prs, author: cfg.sweepAuthor,
                                       includeDrafts: cfg.includeDrafts,
                                       includeReady: cfg.includeReady)
        return request(targets.map { pr in
            RequestedWork(number: pr.number, url: pr.url,
                          // The ban dimension, and only someone else's PR has one.
                          author: cfg.disposition == .theirs ? pr.author : "",
                          config: .review(cfg.forPR(pr.number)))
        })
    }

    /// Queue one fix per issue `cfg` sweeps. Returns `(queued, already)`.
    ///
    /// The issues come from the panel's own last fetch, for the same reason the PRs
    /// above do. What the drain cannot do for these is retire them: the monitor poll
    /// reads PRs, so nothing there notices an issue closing under a waiting ask. The
    /// swept prompt covers it instead — it re-reads the issue's state and stops if it
    /// has been dealt with since — and **cancel** is the way out of an ask the sweep
    /// should not have caught.
    @discardableResult
    func requestIssueSweep(_ cfg: IssueConfig) -> (queued: Int, already: Int) {
        let targets = Filters.sweptIssues(issues, target: cfg.target,
                                          author: cfg.sweepAuthor,
                                          unassignedOnly: cfg.unassignedOnly)
        return request(targets.map { issue in
            RequestedWork(number: issue.number, url: issue.url,
                          // The ban dimension. Only a scope that names one person has
                          // one, matching the login the wizard would ban-check.
                          author: cfg.target == .someone ? issue.author : "",
                          config: .issues(cfg.forIssue(issue.number)))
        })
    }

    /// Store and publish whichever of `asks` is not already queued, and say how many
    /// of each there were.
    ///
    /// An item already waiting keeps the ask it has instead of gaining a second: the
    /// queue is keyed by the ask, so two would be one row that dispatches twice (and
    /// the second dispatch would find the first agent still on it). That makes
    /// pressing SPAWN twice, or sweeping a scope that overlaps an earlier one,
    /// idempotent rather than a way to double up.
    private func request(_ asks: [RequestedWork]) -> (queued: Int, already: Int) {
        let known = Set(requestedWork.map(\.key))
        let fresh = asks.filter { !known.contains($0.key) }
        guard !fresh.isEmpty else { return (0, asks.count) }
        requestedWork += fresh
        publishRequested(fresh)
        return (fresh.count, asks.count - fresh.count)
    }

    /// One stored ask as the queued task it stands for.
    ///
    /// Carries no `counter`, no `workKey` and no `ledgerKey`: no monitor owns it (so no
    /// toggle pauses it and no auto-handled counter counts it), and the telemetry
    /// ledger measures the monitors, not the operator.
    private func requestedTask(_ entry: RequestedWork) -> QueuedAgentTask {
        let action = entry.config.action
        // A fix is deliberately NOT PR-scoped. The dispatch pipeline's dedup is
        // PR-shaped throughout — the in-flight check matches a `/pull/<n>` URL and
        // keys on a PR number — so handing it an issue number would collide with the
        // PR that happens to share it. What keeps two agents off one issue instead is
        // the assignee claim, which every machine can see rather than just this one.
        let isPR = action == AgentTaskQueue.reviewAction
        return QueuedAgentTask(
            id: entry.key,
            job: AgentJob(kind: action, auditAction: action,
                          label: entry.label, prompt: entry.config.buildPrompt(),
                          prURL: isPR ? entry.url : nil,
                          prNumber: isPR ? entry.number : nil,
                          authorLogin: entry.author.isEmpty ? nil : entry.author,
                          duty: action, workKey: "", counter: nil),
            attemptNumber: 1)
    }

    /// Put freshly asked-for work into the queue there and then.
    ///
    /// Without this it would appear on the next poll, up to a poll period after the
    /// press — and a press that leaves the panel exactly as it was reads as a press
    /// that did nothing. Re-arranged through the same `AgentTaskQueue.order` the commit
    /// uses, so the rows land in their band and in the operator's arrangement, not
    /// merely at the end.
    private func publishRequested(_ entries: [RequestedWork]) {
        let tasks = queuedTasks + entries.map(requestedTask)
        let ordered = AgentTaskQueue.order(offered: tasks.map(\.id), saved: queuedTaskOrder)
        queuedTaskOrder = ordered
        queuedTasks = ordered.compactMap { id in tasks.first { $0.id == id } }
    }

    /// Offer every ask nothing has started, alongside the monitors' own finds.
    ///
    /// This is the list's whole job in a poll. A monitor re-offers its work by finding
    /// it on GitHub again; nothing on GitHub says a PR or an issue was swept, so what
    /// re-offers these is the ask itself, until the dispatch that starts one takes it
    /// off (`settleRequested`), the operator cancels it, or — for a review — the drain
    /// finds its PR closed (`drainQueuedTasks`).
    ///
    /// Not private: the queue self-test commits a cycle directly, which is the only way
    /// to exercise the offer without a live GitHub fetch.
    func offerRequestedWork() {
        for entry in requestedWork { stagedQueue.append(requestedTask(entry)) }
    }

    /// Take an ask off the list once its dispatch has answered for it.
    ///
    /// Started (here or on a peer that already owns the work) is the obvious one. A BAN
    /// is the other: the agent would refuse to run for as long as the ban stands, and a
    /// row nothing will ever start is a row that lies about what this machine is going
    /// to do. Every other outcome leaves the ask alone — an in-flight PR, a window with
    /// no budget left and a terminal that failed to open are all reasons to try again
    /// next poll, which is exactly what staying in the list means.
    private func settleRequested(_ entry: QueuedAgentTask, _ outcome: DispatchOutcome) {
        guard entry.job.requested else { return }
        switch outcome {
        case .spawned, .standDown, .banned: forgetRequested(entry.id)
        case .inFlight, .atCapacity, .unaffordable, .failed: break
        }
    }

    /// Drop one ask, by the queue key that is its identity.
    private func forgetRequested(_ key: String) {
        requestedWork.removeAll { $0.key == key }
    }

    /// The queued row's "cancel": drop an ask the operator has changed their mind
    /// about, without starting it.
    ///
    /// A sweep is the one thing in this list that can be asked for by the fifty, and
    /// the only one GitHub does not answer for: a monitor's row leaves when the work is
    /// no longer owed, while an ask on a PR that is still open stands until it runs —
    /// and a fix stands whatever happens to its issue, the poll reading PRs alone.
    /// Without a way back out, a mis-aimed sweep is a day of agents nobody can call off.
    func cancelRequestedWork(_ id: String) {
        guard let entry = queuedTasks.first(where: { $0.id == id }), entry.job.requested
        else { return }
        forgetRequested(entry.id)
        dropQueuedTask(id)
        AuditLog.log("panel", "queue-cancel", "\(entry.job.label) — cancelled, not run")
        refreshAudit()
    }

    /// Whether the monitor that owns this work is switched off.
    ///
    /// A switched-off monitor still finds its work and still queues it — what your
    /// PRs owe is worth seeing whether or not this machine is set to act on it — but
    /// nothing automatic starts it. It waits for "execute now", or for the toggle to
    /// come back on. That is the whole difference the two toggles make: they decide
    /// who starts the work, not whether it is known.
    func isPaused(_ counter: AutoCounter?) -> Bool {
        switch counter {
        case .reviewRequests:        return !reviewRequestsEnabled
        case .myReviews, .conflicts: return !prAutofixEnabled
        // A review the operator asked for: no monitor owns it, so neither toggle
        // speaks for it. Switching the monitors off says what this machine may go
        // looking for, not that it should stop doing what it was told — for an ask,
        // that is what `queueAutoRun` says.
        case nil:                    return false
        }
    }

    /// The queued tasks the drain may start, in the operator's order — everything
    /// whose monitor is still on, plus the reviews the operator asked for, which no
    /// monitor speaks for either way. Empty while `queueAutoRun` is off, including
    /// the asks: that switch is over the queue itself, not over what fills it.
    ///
    /// Not private: this is the seam the queue self-test drives. Asserting on the
    /// list the drain walks is how the "a paused monitor's work is held, not run"
    /// rule gets a test at all — driving the drain itself would end in a real spawn.
    var drainableTasks: [QueuedAgentTask] {
        guard queueAutoRun else { return [] }
        return queuedTasks.filter { !isPaused($0.job.counter) }
    }

    /// Dispatch one queued task past the capacity check its caller already made, and
    /// record the attempt its monitor would have recorded.
    ///
    /// `forced` is the operator's "execute now", and is the only thing that also
    /// overrides the spending budget. The drain does not: it is the machine starting
    /// its own automatic work, and a task that could not be afforded when it was found
    /// is not afforded by having waited in a list.
    ///
    /// Dispatched as `.auto` whatever put it in the queue, including a review the
    /// operator asked for. That is not about who wanted the work but about what
    /// starting it costs: this dispatch spends a bay of the device's cap and its share
    /// of whatever pays for it, and the gate's `.panel` branch is for the agent a
    /// click opens *now*, outside the cap entirely. What the operator's ask does change
    /// is the label — see `AgentDispatchGate.label`.
    ///
    /// That record is not bookkeeping polish: the whole retry ladder hangs off it. A
    /// queued dispatch that wrote none would look, to the very next poll after the
    /// agent exits, exactly like work never attempted — so an agent that finishes
    /// without clearing the conflict or leaving the review would be re-dispatched
    /// three minutes later, and again, with no backoff ever engaging.
    ///
    /// This is also the one place a task crosses from the queue into the starting
    /// band, whether the drain reached it or the operator clicked. The move out of
    /// `queuedTasks` runs before the first suspension, so the panel shows the click
    /// landing and a second click finds nothing to start; the move out of
    /// `startingTasks` runs only once the row that replaces it is already published
    /// (`dispatchAgent` books the run — `spawnTracked` or `trackMeshRun` — and awaits
    /// `settleAgents` before it returns, and `endStarting` resumes on that continuation
    /// without suspending again). Between them the task is a row the whole way: never
    /// drawn twice, and never missing.
    @discardableResult
    private func runQueuedTask(_ entry: QueuedAgentTask,
                               forced: Bool) async -> DispatchOutcome {
        beginStarting(entry)
        let outcome = await dispatchAgent(entry.job, source: .auto,
                                          attemptNumber: entry.attemptNumber,
                                          bypassCapacity: true, bypassBudget: forced)
        endStarting(entry.id)
        if outcome.wasHandled { recordQueuedAttempt(entry) }
        settleRequested(entry, outcome)
        return outcome
    }

    /// Move one task out of the queue and into the starting band, and back out of it
    /// when its dispatch answers.
    ///
    /// Not private: these are the halves of the transition the queue self-test drives.
    /// The middle of a real one is a suspension inside `dispatchAgent`, which a test
    /// could only observe by racing the spawn it is suspended on.
    ///
    /// The band is keyed by queue key like the queue itself — one task cannot be
    /// starting twice, and the panel draws its list `ForEach` that key.
    func beginStarting(_ entry: QueuedAgentTask) {
        queuedTasks.removeAll { $0.id == entry.id }
        guard !startingTasks.contains(where: { $0.id == entry.id }) else { return }
        startingTasks.append(entry)
    }

    func endStarting(_ id: String) {
        startingTasks.removeAll { $0.id == id }
    }

    /// Take one task off the queue without starting it — the work it stands for is
    /// already done.
    ///
    /// Its place in the saved arrangement is left alone: that list drops keys nothing
    /// offers on the next commit (`AgentTaskQueue.order`), and a task that turns out
    /// to be owed after all should come back where the operator put it, not at the
    /// end.
    func dropQueuedTask(_ id: String) {
        queuedTasks.removeAll { $0.id == id }
    }

    /// Write the retry-backoff record for a task the queue dispatched, into the same
    /// per-monitor ledger that monitor writes itself: `AgentJob.counter` names the
    /// ledger, `attemptStamp` is the stamp that monitor compares against.
    private func recordQueuedAttempt(_ entry: QueuedAgentTask) {
        guard let number = entry.job.prNumber, let counter = entry.job.counter else { return }
        let key = String(number)
        let record = ReviewAttempt(requestedAt: entry.job.attemptStamp,
                                   lastDispatchedAt: Date(), attempts: entry.attemptNumber)
        switch counter {
        case .reviewRequests:
            var map = loadReviewReqAttempts(); map[key] = record; saveReviewReqAttempts(map)
        case .myReviews:
            var map = loadMyReviewAttempts(); map[key] = record; saveMyReviewAttempts(map)
        case .conflicts:
            var map = loadMyConflictAttempts(); map[key] = record; saveMyConflictAttempts(map)
        }
    }

    /// The queued row's "execute now": start this task immediately, past the two holds
    /// that are the machine's own judgement.
    ///
    /// It stays AUTO work — same `Auto · ` label, same auto-handled counter, mesh
    /// routing still applies, and once running it occupies a slot like any other
    /// automatic agent, so the rest of the queue waits behind it. Of the six
    /// asymmetries the gate draws between a click and a monitor tick (focus, capacity,
    /// budget, mesh, counters, label) this borrows exactly two: the cap and the
    /// spending budget, which are the two the operator is overriding. Both are
    /// estimates of what this machine should do next, and the operator looking at the
    /// row knows something they do not.
    ///
    /// The ROW answers the click at once — `runQueuedTask` moves it into the starting
    /// band before it suspends, so it reads as starting from the frame after the
    /// press, through the seconds the mesh round-trip and the terminal spawn take.
    /// The FEED line is written from the outcome instead, never ahead of it: this is
    /// an auto job, so a mesh peer can own the work, the PR can have gained an agent
    /// since the list was built, and the spawn can fail — announcing "started" before
    /// asking would report a launch that never happened in all three.
    func executeQueuedTask(_ id: String) async {
        guard let entry = queuedTasks.first(where: { $0.id == id }) else { return }
        switch await runQueuedTask(entry, forced: true) {
        case .spawned:
            AuditLog.log("panel", "queue-run",
                         "\(entry.job.label) — started ahead of the task cap")
        // A mesh peer's agent owns the work — which is a task now running, not a
        // click that did nothing, and `dispatchAgent` has already put the row that
        // says so where the queued one was. The router logs the peer's name.
        case .standDown:
            break
        // The rest are all logged by the step that decided them, but a feed line is
        // not an answer to a click: the row vanished and nothing opened, so say why
        // in the panel, as the per-row Resolve button does (`resolveConflicts`).
        case .failed:
            error = "\(entry.job.label) failed to spawn — see the activity log."
        case .inFlight:
            error = "\(entry.job.label): an agent is already on this PR."
        case .banned:
            error = "\(entry.job.label): the PR's author is banned (un-ban to review)."
        case .atCapacity, .unaffordable:
            break   // unreachable: the run bypasses both holds the operator overrode
        }
        refreshAudit()
    }

    /// Reorder the queue by drag: `id` lands where it was dropped relative to
    /// `target`. The arrangement is persisted, so it survives both the poll that
    /// rebuilds the list and the restart that empties it.
    func moveQueuedTask(_ id: String, onto target: String) {
        let current = queuedTasks
        let ordered = AgentTaskQueue.reorder(current.map(\.id), moving: id, onto: target)
        queuedTaskOrder = ordered
        queuedTasks = ordered.compactMap { key in current.first { $0.id == key } }
    }

    /// The app the user is currently working in, so a background (auto-fix) spawn can
    /// bounce focus straight back to it instead of yanking them into a new terminal
    /// window. Read on the main actor (Store is @MainActor). nil when there is no
    /// resolvable frontmost app — the spawn then behaves like a foreground one.
    private var frontmostAppBundleID: String? {
        NSWorkspace.shared.frontmostApplication?.bundleIdentifier
    }

    /// Where an auto job went. Both mesh outcomes name the node whose agent has the
    /// work (empty when the mesh answered without naming one) — the panel draws a row
    /// for it either way, so "some peer took it" can never look like "it vanished".
    ///
    /// `onThisMachine` says the placement came back to us: the mesh's best node was
    /// the one that asked, which makes the run local in everything but who opened its
    /// terminal.
    enum MeshRoute {
        case standDown(node: String), spawned(node: String, onThisMachine: Bool), local
    }

    /// Route an AUTO job through the mesh (szpontnet-spec/docs/12): claim-gated dispatch
    /// to the best-surplus node. Mirrors the Linux store's `_route_via_mesh`.
    ///
    /// Every machine scans GitHub independently, but the mesh runs each unit of work
    /// **once** — `MeshBridge.dispatch` claims the work key and places the run on the
    /// best node; the EXECUTOR holds that claim for its agent's lifetime, so a
    /// concurrent or repeat scan is suppressed and a node death frees it for
    /// failover. No node stands down on a duty ASSIGNMENT anymore — that deferred to
    /// a node that might not be scanning at all, silently dropping the work.
    ///
    /// `.spawned` (the mesh took it), `.standDown` (a peer's agent owns it), or
    /// `.local` to fall through to a LOCAL tracked spawn — the fail-open path when
    /// the mesh is unavailable, so a wedged node never drops the operator's work.
    private func routeViaMesh(_ job: AgentJob) async -> MeshRoute {
        guard meshEnabled, !job.workKey.isEmpty,
              let snap = meshState, MeshBridge.nodeRunning(snap) else { return .local }
        let port = snap.tcpPort ?? 0
        let (duty, prompt, workKey) = (job.duty, job.prompt, job.workKey)
        let results: [[String: Any]]? = await Task.detached(priority: .userInitiated) {
            try? MeshBridge.dispatch(duty: duty, prompt: prompt, workKey: workKey, port: port)
        }.value
        guard let results, !results.isEmpty else { return .local }  // unreachable → fail-open
        let statuses = results.map { ($0["status"] as? String) ?? "failed" }
        if statuses.allSatisfy({ $0 == "suppressed" }) {
            logMeshSuppressed(workKey, results)
            return .standDown(node: executorName(results, status: "suppressed"))
        }
        if statuses.allSatisfy({ $0 == "spawned" || $0 == "suppressed" }) {
            // Ran on the mesh (the node logs where). The name comes back with the
            // dispatch and nowhere else: the claim book identifies the owner by node
            // id, and a snapshot carrying it is seconds away at best.
            //
            // The id beside that name is what says whether the executor is us. Both
            // sides of the comparison are already in hand — the result names its
            // executor, the snapshot names this node — so a placement that came home
            // can be booked as the local agent it is.
            let me = snap.selfNode?.id
            let here = me != nil && results.contains {
                ($0["status"] as? String) == "spawned" && ($0["node"] as? String) == me
            }
            return .spawned(node: executorName(results, status: "spawned"),
                            onThisMachine: here)
        }
        return .local  // declined/failed on every slot → fall through to a local spawn
    }

    /// The node the panel names for this dispatch: the first slot with the deciding
    /// status that came back with a name. Empty when the mesh named none — a row that
    /// says only "on mesh" still beats work that disappears.
    ///
    /// One name for what a multi-platform placement can spread over several slots. It
    /// is the executors' shared work key that the row is really tracking, and exactly
    /// one node holds that lease at a time, so a second name would be a second row for
    /// one unit of work.
    private func executorName(_ results: [[String: Any]], status: String) -> String {
        let named = results.first {
            ($0["status"] as? String) == status && ($0["nodeName"] as? String)?.isEmpty == false
        }
        return (named?["nodeName"] as? String) ?? ""
    }

    /// A peer's agent owns this work — note it once per key, not per poll.
    private func logMeshSuppressed(_ workKey: String, _ results: [[String: Any]]) {
        if meshSuppressedLogged.contains(workKey) { return }
        if meshSuppressedLogged.count > 256 { meshSuppressedLogged.removeAll() }
        meshSuppressedLogged.insert(workKey)
        let owner = results.compactMap { $0["nodeName"] as? String }.first ?? "a peer"
        AuditLog.log("auto", "mesh-suppressed", "Work claimed by \(owner) — running there")
        refreshAudit()
    }

    /// Auto-handled counters bump only on a monitor's FIRST dispatch (a retry is not
    /// new work; a manual run is the user's own action). Shared by both spawn paths.
    private func bumpAutoCounter(_ job: AgentJob, source: AgentDispatchGate.Source,
                                 attemptNumber: Int) {
        guard AgentDispatchGate.bumpsCounter(source: source, attemptNumber: attemptNumber)
        else { return }
        switch job.counter {
        case .reviewRequests: reviewRequestsHandled += 1
        case .myReviews: autofixReviewsHandled += 1
        case .conflicts: autofixConflictsHandled += 1
        case nil: break
        }
    }

    // MARK: - The one dispatch pipeline (buttons and monitors are triggers, not paths)

    /// One agent job, whoever triggers it. The trigger supplies WHAT to run (config
    /// → prompt, labels, PR identity); the pipeline owns everything that HAPPENS —
    /// the ban check, in-flight dedup, mesh policy, spawn, tracking, counters — so
    /// a button click and a monitor tick cannot behave differently by accident.
    struct AgentJob: Equatable {
        var kind: String            // agent-row tint: "review" | "issues" | "conflicts" | "audit"
        var auditAction: String     // activity-feed verb
        var label: String           // label core (source prefix / retry suffix added by the gate)
        var prompt: String
        var prURL: String?          // nil = not PR-scoped (sweeps, audits) → no PR dedup possible
        var prNumber: Int?
        var authorLogin: String?    // whose PR we'd be reviewing — the ban dimension (nil = none)
        var duty: String            // mesh duty, for auto-origination gating
        var workKey: String         // mesh claim key ("" = no claim)
        /// Telemetry ledger identity ("" = not tracked). Equal to `workKey` whenever
        /// that exists, but carried separately because it survives an unknown head
        /// sha — where skipping the mesh *claim* is safe and skipping the ledger
        /// entry would drop dispatched work off every figure on the screen.
        var ledgerKey: String = ""
        var counter: AutoCounter?   // which auto-handled tally a monitor dispatch feeds
        /// The stamp the monitor that owns this job records against the PR when an
        /// agent launches (`ReviewAttempt.requestedAt`) — the request timestamp for a
        /// review request, a constant for the two level-triggered reconcilers.
        /// Carried on the job so a dispatch the *queue* runs later starts the same
        /// retry backoff the reconciler's own dispatch would have. Read only on that
        /// path: a panel spawn keeps no attempt record, and a job with no monitor
        /// behind it (the sweeps) has no stamp to carry.
        var attemptStamp: String = ""

        /// Whether the operator asked for this exact unit of work, as opposed to a
        /// monitor having found it. Read off the verb, which already distinguishes
        /// them: the monitors dispatch under `review-req` and `review-reply`, while a
        /// plain `review` or `issues` is a wizard spawn — a click, or one item of the
        /// sweep a click queued. It decides the label (`AgentDispatchGate.label`) and,
        /// in the panel, which queued rows can be cancelled.
        var requested: Bool { AgentTaskQueue.requestedActions.contains(auditAction) }
    }

    enum AutoCounter { case reviewRequests, myReviews, conflicts }

    /// One unit of automatic work nothing has started yet: the whole job, held by the
    /// device's task cap, by the spending budget, or by a switch the operator set
    /// (its own monitor, or the queue itself), until a slot frees or the operator runs
    /// it. Rebuilt from live evidence on each poll — see `queuedTasks`.
    struct QueuedAgentTask: Identifiable, Equatable {
        /// `AgentTaskQueue.key` — stable across polls and applet restarts, which is
        /// what lets the operator's drag order outlive the list itself.
        let id: String
        var job: AgentJob
        /// The attempt number the monitor would have dispatched under, so a queued
        /// retry keeps its place on the 5m→3h backoff ladder instead of restarting it.
        var attemptNumber: Int
    }

    /// What one dispatch did — wizards surface it as status text; monitors only
    /// care whether it spawned.
    enum DispatchOutcome: Equatable {
        case spawned(terminal: String)
        case inFlight
        case banned
        case standDown
        case atCapacity
        case unaffordable
        case failed(String)
        var didSpawn: Bool { if case .spawned = self { return true }; return false }
        /// The work is now being handled — spawned locally OR stood down to a peer
        /// whose agent already owns it. This is the signal to record the attempt and
        /// start the retry backoff, mirroring the Python reference which treats
        /// `("spawned", VERDICT_STAND_DOWN)` as handled. `.failed` deliberately does
        /// NOT count (a transient spawn error retries next poll); nor do `.inFlight`
        /// / `.banned` / `.atCapacity` / `.unaffordable`. Using `.didSpawn` here
        /// instead would re-dispatch peer-owned work to the mesh on every poll, the
        /// backoff never engaging — and counting either deferral as handled would drop
        /// held work into a 5m–3h cooldown instead of offering it again the moment an
        /// agent finishes or the window refills.
        var wasHandled: Bool {
            switch self {
            case .spawned, .standDown: return true
            case .inFlight, .banned, .atCapacity, .unaffordable, .failed: return false
            }
        }
    }

    /// Run one agent job through the shared gate (`AgentDispatchGate` — the pure,
    /// smoke-tested decision both platforms mirror) and, on `.proceed`, spawn +
    /// track it. `resolvingPRs` is taken for the whole await span of any PR-scoped
    /// job, so a double-click or an overlapping poll can't race two spawns onto
    /// one PR (it also drives the panel row's spinner). In-flight evidence is one
    /// tick's verdict on every registered run plus every agent visible in `ps` with
    /// no record behind it, so it also catches agents whose local bookkeeping was
    /// lost and mesh jobs that landed on this very machine.
    ///
    /// An AUTO job is additionally capped at `autoTaskLimit` concurrent agents on
    /// this device (`AgentState.capLoad`), held outright while its own monitor is
    /// switched off (`isPaused`) or the queue is (`queueAutoRun`), and held again
    /// when what is left of the limits it spends against will not cover it
    /// (`AutoBudget`);
    /// a panel click is subject to none of them. Every one of those refusals queues
    /// the job (`stageQueued`), which is what the panel's Agent-tasks list shows as
    /// *queued*.
    ///
    /// `bypassCapacity` is for the two callers that have already answered the
    /// capacity question themselves: the queue drain (which counted the free slot it
    /// is filling, and skips paused work by construction) and "execute now" (where
    /// the operator is overriding both holds deliberately). It skips the measurement —
    /// not just its verdict — so neither pays for a second `ps` scan, and so a forced
    /// run cannot re-queue itself.
    ///
    /// `bypassBudget` is only the second of those. The drain runs the machine's own
    /// automatic work, and work that could not be afforded when it was found cannot be
    /// afforded by having waited in a list; only the operator pressing "execute now"
    /// overrides the budget, exactly as only they override the cap.
    @discardableResult
    func dispatchAgent(_ job: AgentJob, source: AgentDispatchGate.Source,
                       attemptNumber: Int = 1,
                       bypassCapacity: Bool = false,
                       bypassBudget: Bool = false) async -> DispatchOutcome {
        if let n = job.prNumber {
            if resolvingPRs.contains(n) { return .inFlight }
            resolvingPRs.insert(n)
        }
        defer { if let n = job.prNumber { resolvingPRs.remove(n) } }
        let banned = job.authorLogin.map { BanList.isBanned($0, in: BanList.read()) } ?? false
        var agentOnPR = false
        if let n = job.prNumber { agentOnPR = await inFlight(n) }
        // Measured only for an auto job that would otherwise run: the count costs a
        // `ps` scan, a panel click is never capped, and an in-flight PR spawns
        // nothing either way — so in both of those the answer would be discarded.
        // Finding room is also what re-arms the "deferring" note.
        var atCapacity = false, paused = false
        if source == .auto, !agentOnPR, !bypassCapacity {
            let full = await agentTick().tick.capLoad.count >= autoTaskLimit
            if !full { capacityLogged = false }
            // A switched-off monitor has no room for its own work, whatever the
            // device's, and neither has a switched-off queue — for anything. Both are
            // modelled as capacity because the answer is the same one in every respect
            // that matters here — hold the job, write no attempt record, re-offer it
            // next poll — which keeps two toggles that only this front-end has out of
            // the dispatch gate both front-ends mirror. The queue switch has to hold
            // HERE and not only at the drain: a find that meets a free bay never
            // reaches the queue at all.
            paused = isPaused(job.counter) || !queueAutoRun
            atCapacity = full || paused
        }
        // Measured after capacity and under the same conditions: a device with no free
        // bay has nothing to spend a budget on, so the probe and the ledger fold are
        // work that would be thrown away. The drain reaches here with `bypassCapacity`
        // set and this one clear — that is the whole difference between deferring work
        // and forcing it.
        var budget = AgentDispatchGate.Budget(affordable: true)
        if source == .auto, !agentOnPR, !bypassBudget, !atCapacity, AutoBudget.enabled {
            budget = AutoBudget.decide()
            if budget.affordable { budgetLogged = false }
        }
        switch AgentDispatchGate.decide(source: source, banned: banned,
                                        agentOnPR: agentOnPR, meshStandsDown: false,
                                        atCapacity: atCapacity,
                                        unaffordable: !budget.affordable) {
        case .atCapacity:
            // A paused monitor is not a saturated device: it queues silently, because
            // the operator switched it off on purpose and the row says the rest.
            if !paused { logAtCapacity() }
            stageQueued(job, attemptNumber: attemptNumber)
            return .atCapacity
        case .unaffordable:
            logUnaffordable(budget)
            stageQueued(job, attemptNumber: attemptNumber)
            return .unaffordable
        case .banned:
            AuditLog.log(source.rawValue, "ban-skip",
                         "\(job.label) — author is banned (un-ban to review)")
            refreshAudit()
            return .banned
        case .inFlight:
            // A monitor tick hitting a busy PR is routine (stays silent); a click
            // deserves an answer for why nothing opened.
            if source == .panel {
                AuditLog.log("panel", "in-flight",
                             "\(job.label) — an agent is already on this PR")
                refreshAudit()
            }
            return .inFlight
        case .standDown:
            return .standDown
        case .proceed:
            break
        }
        // What this dispatch is called wherever it is named: the activity line it writes,
        // and — while it runs — the row it wears in the Agent-tasks list. One string, so a
        // retry cannot read as a first attempt in one of them.
        let rowLabel = AgentDispatchGate.label(source: source, core: job.label,
                                               attemptNumber: attemptNumber,
                                               requested: job.requested)
        // An AUTO job on a live mesh runs on the best-surplus node via claim-gated
        // dispatch (every machine scans; the mesh runs it once and dedups via the
        // executor's claim). A manual spawn — or a wedged/absent mesh — runs and is
        // tracked locally instead (fail-open).
        if source == .auto {
            switch await routeViaMesh(job) {
            case .standDown(let node):
                // A peer's agent owns it (logged once by the router). The work is
                // running, just not here — so it gets a row like any other running
                // agent, saying whose.
                trackMeshRun(job, node: node, attemptNumber: attemptNumber)
                return .standDown
            case .spawned(let node, let onThisMachine):
                AuditLog.log(source.rawValue, job.auditAction, rowLabel)
                // Booked wherever the mesh put it, before the next job of this poll asks
                // how many agents are running — left unbooked, every dispatch of a burst
                // measured the same empty machine and the cap held back nothing at all.
                trackMeshRun(job, node: node, attemptNumber: attemptNumber,
                             onThisMachine: onThisMachine)
                bumpAutoCounter(job, source: source, attemptNumber: attemptNumber)
                // A mesh placement on a PEER spends that peer's quota and leaves no
                // sentinel here, so it is flagged remote — counted as work started and
                // taken off the backlog, kept out of the per-task cost and run-time
                // figures. One the mesh placed back here spent ours, and is priced
                // like any other agent that ran on this machine.
                recordTelemetryStart(job, source: source, remote: !onThisMachine,
                                     attemptNumber: attemptNumber)
                refreshAudit()
                await settleAgents()
                return .spawned(terminal: "mesh")
            case .local:
                break               // fall through to a local tracked spawn
            }
        }
        do {
            let opened = try await spawnTracked(
                kind: job.kind, label: rowLabel, prURL: job.prURL, prNumber: job.prNumber,
                prompt: job.prompt, source: source.rawValue, auditAction: job.auditAction,
                ledgerKey: source == .auto ? job.ledgerKey : "", terminal: terminal,
                restoreFocusTo: AgentDispatchGate.stealsFocus(source) ? nil
                    : frontmostAppBundleID)
            bumpAutoCounter(job, source: source, attemptNumber: attemptNumber)
            recordTelemetryStart(job, source: source, remote: false,
                                 attemptNumber: attemptNumber)
            await settleAgents()
            return .spawned(terminal: opened.rawValue)
        } catch {
            let msg = (error as? LocalizedError)?.errorDescription ?? "\(error)"
            AuditLog.log(source.rawValue, "spawn-failed",
                         "\(job.label) failed to spawn: \(msg)")
            refreshAudit()
            return .failed(msg)
        }
    }

    /// Review a PR someone asked me to review (most-comprehensive depth, formal
    /// per-line comments; verdict only under the auto-approve policy) — the
    /// review-request monitor's job builder. `attemptNumber` ≥2 means a retry of a
    /// review a previous agent left unaddressed.
    @discardableResult
    private func dispatchReviewRequest(_ r: AutofixMonitor.ReviewRequest, attemptNumber: Int = 1) async -> Bool {
        // Auto-approvals must be enabled AND no configured suppressor may match (SKILL /
        // installer / community PR) for an auto-review to submit a verdict. Otherwise it's
        // comments-only and the final call stays with me.
        let reasons = verdictPolicy.withholdReasons(files: r.files, authorAssociation: r.authorAssociation)
        let verdict = autoApproveEnabled && reasons.isEmpty
        // Without a real verdict, a clean review still soft-approves (friendly comment, no
        // APPROVE) unless the user turned that off too. Moot when `verdict` is true.
        let soft = softApproveEnabled
        let tag: String
        if verdict {
            tag = " +verdict"
        } else {
            let why = !autoApproveEnabled ? "auto-approvals off" : reasons.joined(separator: ", ")
            tag = soft ? " ~soft-approve (\(why))" : " −verdict (\(why))"
        }
        let prompt = ReviewConfig(depth: "max", target: .specific, me: effectiveMe,
                                  markReady: false, leaveReviews: true, replyToReviews: false,
                                  specificPR: String(r.number), finalPass: verdict,
                                  softApprove: soft, specificAuthor: .theirs).buildPrompt()
        let job = AgentJob(kind: "review", auditAction: "review-req",
                           label: "Review-req · #\(r.number) (@\(r.author))\(tag)",
                           prompt: prompt, prURL: r.url, prNumber: r.number,
                           authorLogin: r.author, duty: "review",
                           workKey: AutofixMesh.workKey(kind: AutofixMesh.kindReviewReq,
                                                        prURL: r.url, headSha: r.headSha),
                           ledgerKey: AutofixMesh.ledgerKey(kind: AutofixMesh.kindReviewReq,
                                                            prURL: r.url, headSha: r.headSha),
                           counter: .reviewRequests, attemptStamp: AttemptStamp.reviewRequest(r))
        return await dispatchAgent(job, source: .auto, attemptNumber: attemptNumber).wasHandled
    }

    var reviewRequestsHandled: Int {
        get { UserDefaults.standard.integer(forKey: Keys.reviewRequestsHandled) }
        set { persist(newValue, forKey: Keys.reviewRequestsHandled) }
    }
    /// prNumber(String) -> our attempt record (request stamp, last dispatch, attempt count).
    /// Persisted as JSON so the retry backoff survives an applet restart.
    private func loadReviewReqAttempts() -> [String: ReviewAttempt] {
        guard let data = UserDefaults.standard.data(forKey: Keys.reviewReqAttempts),
              let decoded = try? JSONDecoder().decode([String: ReviewAttempt].self, from: data)
        else { return [:] }
        return decoded
    }
    private func saveReviewReqAttempts(_ map: [String: ReviewAttempt]) {
        persistJSON(map, forKey: Keys.reviewReqAttempts)
    }

    /// Spawn the appropriate action-button agent for a detected transition and track
    /// it, mirroring exactly what the Resolve-conflicts / Review wizards do (Deep depth,
    /// don't-mark-ready / no-formal-review / reply-"Fixed in <hash>").
    private func dispatchAutofix(_ event: AutofixEvent) async {
        switch event {
        case .review(let s):
            _ = await dispatchMyReview(s)   // shared with the offline-review reconciler
        case .conflict:
            // Conflicts are handled by the level-triggered `reconcileMyConflicts` (same
            // poll sees the CONFLICTING state, so nothing is slower) — which also covers
            // conflicts that predate the baseline and retries failed spawns with backoff.
            break
        }
    }

    /// Resolve the conflicts on one PR — the job builder shared by the conflicts
    /// reconciler (`.auto`) and the panel's per-row button (`.panel`). Everything
    /// else (dedup, mesh policy, focus, label, counter) is the pipeline's.
    @discardableResult
    private func dispatchConflictFix(number: Int, url: String,
                                     attemptNumber: Int = 1,
                                     source: AgentDispatchGate.Source,
                                     headSha: String = "") async -> DispatchOutcome {
        let prompt = ConflictConfig(target: .specific, me: effectiveMe,
                                    specificPR: String(number)).buildPrompt()
        let job = AgentJob(kind: "conflicts", auditAction: "conflicts",
                           label: "Resolve · #\(number)",
                           prompt: prompt, prURL: url, prNumber: number,
                           authorLogin: nil, duty: "conflicts",
                           workKey: AutofixMesh.workKey(kind: AutofixMesh.kindConflicts,
                                                        prURL: url, headSha: headSha),
                           ledgerKey: AutofixMesh.ledgerKey(kind: AutofixMesh.kindConflicts,
                                                            prURL: url, headSha: headSha),
                           counter: .conflicts, attemptStamp: AttemptStamp.conflicting)
        return await dispatchAgent(job, source: source, attemptNumber: attemptNumber)
    }

    /// Reply-to-reviews on one of MY PRs (Deep, fix-on-branch, "Fixed in <hash>",
    /// no formal review) — the my-reviews monitor's job builder. Returns whether an
    /// agent launched, so the reconciler only starts its backoff on a real spawn.
    @discardableResult
    private func dispatchMyReview(_ s: PRSnapshot, attemptNumber: Int = 1) async -> Bool {
        let prompt = ReviewConfig(depth: "deep", target: .specific, me: effectiveMe,
                                  markReady: false, leaveReviews: false, replyToReviews: true,
                                  specificPR: String(s.number), specificAuthor: .mine).buildPrompt()
        let job = AgentJob(kind: "review", auditAction: "review-reply",
                           label: "Review · #\(s.number)",
                           prompt: prompt, prURL: s.url, prNumber: s.number,
                           authorLogin: nil, duty: "review",
                           workKey: AutofixMesh.workKey(kind: AutofixMesh.kindReviewReply,
                                                        prURL: s.url, headSha: s.headSha),
                           ledgerKey: AutofixMesh.ledgerKey(kind: AutofixMesh.kindReviewReply,
                                                            prURL: s.url, headSha: s.headSha),
                           counter: .myReviews, attemptStamp: AttemptStamp.unresolvedReview)
        return await dispatchAgent(job, source: .auto, attemptNumber: attemptNumber).wasHandled
    }

    /// prNumber(String) -> our attempt record for reviews received on my own PRs (unresolved
    /// threads). Persisted as JSON so the retry backoff survives an applet restart.
    private func loadMyReviewAttempts() -> [String: ReviewAttempt] {
        guard let data = UserDefaults.standard.data(forKey: Keys.myReviewAttempts),
              let decoded = try? JSONDecoder().decode([String: ReviewAttempt].self, from: data)
        else { return [:] }
        return decoded
    }
    private func saveMyReviewAttempts(_ map: [String: ReviewAttempt]) {
        persistJSON(map, forKey: Keys.myReviewAttempts)
    }

    private var coreRepo: (owner: String, repo: String) {
        CoreAssets.repoCoordinates()
    }

    // MARK: - Approved-PR actions (merge / resolve conflicts from the panel)

    /// PRs currently being merged (drives the row button's spinner + guards double-taps).
    @Published var mergingPRs: Set<Int> = []
    /// PRs with a Resolve-conflicts spawn in flight — same double-tap guard as
    /// `mergingPRs`, inserted before the seconds-long spawn await (see
    /// `dispatchConflictFix`).
    @Published var resolvingPRs: Set<Int> = []

    /// Merge an approved PR straight from the applet — squash, matching the repo's
    /// convention — instead of opening the website. Refreshes on success so the PR
    /// drops off the Approved list; surfaces any error (e.g. checks still pending).
    func mergePR(_ number: Int) async {
        guard !mergingPRs.contains(number) else { return }
        let (owner, repo) = coreRepo
        mergingPRs.insert(number)
        defer { mergingPRs.remove(number) }
        do {
            _ = try await GH.run(["pr", "merge", "\(number)", "--repo", "\(owner)/\(repo)", "--squash"])
            AuditLog.log("panel", "merge", "Merged #\(number)")
            refreshAudit()
            await refresh()
        } catch {
            let msg = (error as? LocalizedError)?.errorDescription ?? "\(error)"
            self.error = "Merge #\(number) failed: \(msg)"
            AuditLog.log("panel", "merge-failed", "Merge #\(number) failed: \(msg.prefix(120))")
            refreshAudit()
        }
    }

    /// Dispatch a Resolve-conflicts agent for one PR (the blue button shown when a PR
    /// conflicts) — the very same job the reconciler dispatches, through the same
    /// pipeline; only the trigger (a click) differs.
    func resolveConflicts(for number: Int) async {
        let (owner, repo) = coreRepo
        let url = "https://github.com/\(owner)/\(repo)/pull/\(number)"
        switch await dispatchConflictFix(number: number, url: url, source: .panel) {
        case .failed:
            self.error = "Resolve #\(number) failed to spawn — see the activity log."
        case .inFlight:
            self.error = "Resolve #\(number): an agent is already on this PR."
        case .spawned, .banned, .standDown, .atCapacity, .unaffordable:
            // The last three are answers only a monitor gets — none of the mesh gate,
            // the automatic-task cap and the spending budget applies to a click.
            break
        }
    }

    // Persisted so restarts don't re-dispatch, and the pill's counts survive.
    private var autofixConflictsHandled: Int {
        get { UserDefaults.standard.integer(forKey: Keys.autofixConflicts) }
        set { persist(newValue, forKey: Keys.autofixConflicts) }
    }
    private var autofixReviewsHandled: Int {
        get { UserDefaults.standard.integer(forKey: Keys.autofixReviews) }
        set { persist(newValue, forKey: Keys.autofixReviews) }
    }
    private func loadAutofixFingerprints() -> [Int: PRFingerprint] {
        guard let data = UserDefaults.standard.data(forKey: Keys.autofixFingerprints),
              let decoded = try? JSONDecoder().decode([String: PRFingerprint].self, from: data)
        else { return [:] }
        return Dictionary(uniqueKeysWithValues: decoded.compactMap { k, v in Int(k).map { ($0, v) } })
    }
    private func saveAutofixFingerprints(_ fps: [Int: PRFingerprint]) {
        // JSON object keys must be strings; `loadAutofixFingerprints` parses them back to Int.
        persistJSON(Dictionary(uniqueKeysWithValues: fps.map { (String($0.key), $0.value) }),
                    forKey: Keys.autofixFingerprints)
    }

    // MARK: Claude API-error terminal watcher

    /// How often to scan terminals for a stalled agent. 20s by default; env-overridable.
    static var apiWatchInterval: TimeInterval {
        let secs = ProcessInfo.processInfo.environment["DIPLOMAT_APIWATCH_SECS"].flatMap(Double.init)
        return max(5, secs ?? 20)
    }
    /// Base delay before re-nudging the same tty. Doubles on every successive retry to a
    /// session that keeps erroring (exponential backoff), so an agent stuck on a persistent
    /// overload isn't hammered every two minutes forever.
    static let apiWatchCooldown: TimeInterval = 120
    /// Backoff ceiling: never wait longer than this between retries to one session.
    static let apiWatchMaxBackoff: TimeInterval = 3 * 60 * 60   // 3h
    private var apiWatchTask: Task<Void, Never>?

    /// Per-tty backoff state: when the next nudge is allowed, and the interval that got us
    /// there (doubled to schedule the one after). Cleared when the session recovers.
    private struct ApiBackoff { var nextAllowed: Date; var interval: TimeInterval }
    private var apiErrorBackoff: [String: ApiBackoff] = [:]

    /// Per-tty last erroring tail — the previous-scan half of
    /// `ApiErrorMatch.isConfirmedStall`, whose doc comment carries what that gate can and
    /// cannot separate. Pruned alongside `apiErrorBackoff` to currently-erroring ttys.
    private var apiErrorSeenTail: [String: String] = [:]

    /// Compact "2m" / "45m" / "3h" for the audit line.
    static func humanInterval(_ s: TimeInterval) -> String {
        if s >= 3600 { return "\(Int((s / 3600).rounded()))h" }
        if s >= 60 { return "\(Int((s / 60).rounded()))m" }
        return "\(Int(s))s"
    }

    /// Count of nudges sent, for the Settings display.
    var apiWatchContinues: Int {
        get { UserDefaults.standard.integer(forKey: Keys.apiWatchContinues) }
        set { persist(newValue, forKey: Keys.apiWatchContinues) }
    }

    private func startApiErrorWatcher() {
        guard apiWatchTask == nil else { return }
        apiWatchTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.runApiErrorScanOnce()
                let ns = UInt64(Store.apiWatchInterval * 1_000_000_000)
                try? await Task.sleep(nanoseconds: ns)
            }
        }
    }

    /// Serializes overlapping scans (same shape as the autofix poll guard: the backoff
    /// map is read before and written after detached awaits).
    private var apiScanInFlight = false

    /// One scan: read every terminal's last visible lines and, for any session an agent
    /// is running in that shows a Claude API error (outside its cooldown), send the
    /// continue nudge to that exact session.
    func runApiErrorScanOnce() async {
        guard apiWatchEnabled, !apiScanInFlight else { return }
        apiScanInFlight = true
        defer { apiScanInFlight = false }
        // nil = the dump itself failed (automation permission revoked, AppleEvent
        // timeout) — skip the whole scan rather than treating it as "no sessions",
        // which would wrongly clear every backoff and hide the breakage.
        let dump = await Task.detached(priority: .utility) { ApiErrorWatcher.dumpSessionsCached() }.value
        guard let sessions = dump else { return }
        // The other half of "may this session be written to". Unreadable evidence —
        // the process table, or the tmux listings the walk out of a pane needs — skips
        // the scan for the same reason a failed dump does, and more: the answer decides
        // whether a line of text is typed into somebody's shell, so not knowing has to
        // mean not typing. Skipping also leaves the step's backoff and idle-confirmation
        // state alone, which an empty set would have pruned.
        let ttys = await Task.detached(priority: .utility) {
            AgentProbes.ttysRunningAnAgent(now: Date().timeIntervalSince1970)
        }.value
        guard let agentTTYs = ttys.value else { return }
        await apiErrorScanStep(sessions: sessions, agentTTYs: agentTTYs, now: Date()) { tty in
            await Task.detached(priority: .userInitiated) {
                ApiErrorWatcher.sendContinue(tty: tty)
            }.value
        }
    }

    /// The scan's whole decision — which sessions may be written to, which of those are
    /// confirmed stalled, and which are still inside a backoff — over evidence already
    /// read, with the writing left to `send`.
    ///
    /// Split out so it can be driven without a terminal: everything above is AppleEvents
    /// and `ps`, and everything below types into somebody's session. `ApiWatchTest` is
    /// the check that drives it.
    func apiErrorScanStep(sessions: [ApiErrorWatcher.Session], agentTTYs: Set<String>,
                          now: Date, send: (String) async -> Bool) async {
        var erroring = Set<String>()
        for s in sessions {
            // A session no agent is running in is left alone whatever it shows. The
            // nudge is submitted as a line of input, so in a plain shell it is a command
            // — and a shell can show a matching tail for entirely innocent reasons.
            guard agentTTYs.contains(AgentProbes.shortTTY(s.tty)) else { continue }
            // Out-of-quota banners return false here (looksLikeApiError ignores them):
            // a quota-limited agent can't progress until its window resets, so nudging
            // it is pointless — only transient failures are nudged.
            guard ApiErrorMatch.looksLikeApiError(s.tail) else { continue }
            erroring.insert(s.tty)
            // Idle-confirmation (ApiErrorMatch.isConfirmedStall): only nudge a session
            // whose erroring tail is UNCHANGED since the previous scan. Costs one extra
            // scan (~apiWatchInterval) of latency on a real stall — nothing against a
            // feature meant for overnight overload stalls.
            let stalled = ApiErrorMatch.isConfirmedStall(previousTail: apiErrorSeenTail[s.tty],
                                                         currentTail: s.tail)
            apiErrorSeenTail[s.tty] = s.tail
            guard stalled else { continue }
            // Still inside this session's current backoff window — hold off.
            if let b = apiErrorBackoff[s.tty], now < b.nextAllowed { continue }
            let sent = await send(s.tty)
            // Only count/audit a nudge that actually landed — the send scripts now
            // report whether any session owned the tty.
            guard sent else { continue }
            apiWatchContinues += 1
            // Schedule the next retry: double the prior interval (base on first hit),
            // capped at the 3h ceiling.
            let next = apiErrorBackoff[s.tty].map { min($0.interval * 2, Store.apiWatchMaxBackoff) }
                ?? Store.apiWatchCooldown
            apiErrorBackoff[s.tty] = ApiBackoff(nextAllowed: now.addingTimeInterval(next), interval: next)
            AuditLog.log("auto", "nudge",
                "Continued a stalled agent (API error) on \(s.tty); "
                + "next retry in ≥ \(Store.humanInterval(next))")
        }
        // Keep backoff state ONLY for currently-erroring ttys: an on-screen session
        // that stopped erroring has recovered (reset to base), and a CLOSED session's
        // entry must not linger — macOS recycles tty numbers, so stale state would
        // misgate an unrelated new session on the same tty.
        apiErrorBackoff = apiErrorBackoff.filter { erroring.contains($0.key) }
        // Same pruning for the idle-confirmation tails: a tty that stopped erroring (or
        // closed — macOS recycles tty numbers) must start fresh, needing a new two-scan
        // confirmation before it can be nudged again.
        apiErrorSeenTail = apiErrorSeenTail.filter { erroring.contains($0.key) }
    }

    private func startProcessPoll() {
        guard processPollTask == nil else { return }
        processPollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refreshAutoTaskCount()
                await self?.refreshDeviceState()
                self?.refreshBanList()
                self?.refreshAudit()
                self?.runTelemetrySampleOnce()
                let ns = UInt64(Store.processPollInterval * 1_000_000_000)
                try? await Task.sleep(nanoseconds: ns)
            }
        }
    }

    // MARK: - Telemetry

    /// Re-fold the ledger for an open Telemetry screen. Cheap on a repaint:
    /// `TelemetryLog.load` caches the fold until the file actually changes.
    func refreshTelemetry() {
        let next = TelemetryLog.load()
        if next != telemetryLedger { telemetryLedger = next }
    }

    /// Record a monitor dispatch in the ledger. A wizard click is deliberately not
    /// recorded: the screen measures the MONITORS, and an operator's own click has no
    /// queue instant to be late against.
    private func recordTelemetryStart(_ job: AgentJob, source: AgentDispatchGate.Source,
                                      remote: Bool, attemptNumber: Int) {
        guard source == .auto, !job.ledgerKey.isEmpty else { return }
        TelemetryLog.started(key: job.ledgerKey, remote: remote, attempt: attemptNumber)
        refreshTelemetry()
    }

    /// True while a sample worker is out. The insisting probe can keep one alive for
    /// minutes — far longer than the gap between process polls — and until it writes,
    /// `TelemetryLog.sampleDue` still says a sample is owed, so without this the ledger
    /// would collect a burst of samples for one turn.
    private var takingTelemetrySample = false

    /// Take one quota/token reading for the ledger, off the main actor.
    ///
    /// Rides the process poll but answers to its own pacing: the share of this
    /// machine's tokens that goes on the monitored repo is worth knowing whether or
    /// not the monitors are enabled, and pricing the rate-limit window needs an
    /// unbroken sample series regardless. `TelemetryLog.sampleDue` does the pacing, so calling
    /// this more often than the sample interval is free.
    ///
    /// The worker is launched rather than awaited: its probe insists
    /// (`Quota.fractionsLeft`), and the poll loop this rides has agent rows and device
    /// state to refresh every few seconds. It runs on a plain background queue rather
    /// than as a `Task`, because insisting means waiting out the endpoint's bucket —
    /// minutes of blocking, which must not sit on one of the cooperative pool's few
    /// threads.
    func runTelemetrySampleOnce() {
        guard !takingTelemetrySample, TelemetryLog.sampleDue() else { return }
        takingTelemetrySample = true
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let quota = Quota.fractionsLeft(insist: true)
            let totals = UsageScan.totals()
            TelemetryLog.sample(sessionLeft: quota.session, weekLeft: quota.week,
                                repoTokens: totals.repo, otherTokens: totals.other)
            let store = self
            Task { @MainActor in store?.finishTelemetrySample() }
        }
    }

    /// The sample is on disk: let the next turn take one, and re-fold for an open
    /// screen.
    private func finishTelemetrySample() {
        takingTelemetrySample = false
        refreshTelemetry()
    }

    /// Click a row: bring its terminal window to the front.
    ///
    /// The handle is exact, and belongs to a run this applet spawned. Failing it — a run
    /// nobody dispatched, one whose handle never landed — the agent's own process is
    /// walked out to whatever window is showing it (`TerminalFocus`), which is the only
    /// route to a live agent this applet did not open.
    ///
    /// A run on a peer has neither and dismisses rather than pretending. If both fail the
    /// window is gone, so the tick is re-run to drop the dead row immediately rather than
    /// leaving it to linger. The AppleScript runs off the main thread so the popover
    /// never hitches.
    func activate(_ row: AgentRow) async -> FocusOutcome {
        guard row.isFocusable else { return .dismissed }
        let (handle, tty, pid) = (row.window, row.record.tty, row.record.pid)
        let focused = await Task.detached(priority: .userInitiated) {
            if let handle, AgentWindows.focus(handle) { return true }
            return TerminalFocus.focus(tty: tty, pid: pid)
        }.value
        if focused { return .focused }
        await settleAgents()
        return .dismissed
    }

    // MARK: - Diplomat Mesh (LAN P2P topology)

    /// How often the mesh topology snapshot is re-read while enabled. 2s by default so the
    /// screen feels live; the read is a cheap file decode and the poll no-ops when the mesh
    /// is off. Env-overridable for tests.
    static var meshPollInterval: TimeInterval {
        let secs = ProcessInfo.processInfo.environment["DIPLOMAT_MESH_POLL_SECS"].flatMap(Double.init)
        return max(1, secs ?? 2)
    }
    private var meshPollTask: Task<Void, Never>?

    private func startMeshPoll() {
        guard meshPollTask == nil else { return }
        meshPollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.meshTick()
                let ns = UInt64(Store.meshPollInterval * 1_000_000_000)
                try? await Task.sleep(nanoseconds: ns)
            }
        }
    }

    /// Re-read the local node's public topology snapshot and publish on a meaningful
    /// change. No-ops (and costs nothing) when the mesh is disabled — and in render
    /// mode, where it would clobber a seeded mesh fixture with the real state.json.
    func meshTick() async {
        guard meshEnabled, !Headless.isRender else { return }
        let next = await Task.detached(priority: .utility) { MeshBridge.readState() }.value
        if next != meshState { meshState = next }
    }

    /// Start a background mesh node if none is alive (the Mesh screen's "Start" button and
    /// the Settings toggle both call this). A spawn failure lands in `meshError`.
    func ensureMeshRunning() {
        Task { [weak self] in
            let err = await Task.detached(priority: .utility) { MeshBridge.ensureRunning() }.value
            guard let self else { return }
            if let err { self.meshError = err }
            await self.meshTick()
        }
    }

    /// Ask the local node to stop and drop the topology (used when the user disables the
    /// mesh). Best-effort — an already-dead node is fine.
    func stopMesh() {
        let port = meshState?.tcpPort ?? 0
        Task { [weak self] in
            _ = await Task.detached(priority: .utility) { () -> Bool in
                if port > 0 { try? MeshBridge.stop(port: port) }
                return true
            }.value
            guard let self else { return }
            self.meshState = nil
            self.meshError = nil
        }
    }

    /// Run one mesh control round-trip off the main actor, then settle the screen:
    /// any `MeshCtlError` becomes `meshError` (the mesh screen renders it) and the
    /// topology is re-read so the edit shows immediately.
    ///
    /// Every step is load-bearing, which is why the five commands below share it
    /// rather than each spelling it out: without the `meshTick()` the screen keeps
    /// showing pre-edit state, and without the `meshError` assignment a rejected edit
    /// looks like it worked. Driven directly
    /// by `MeshCommandTest`, which is why it isn't private. Twin of the Linux
    /// `store._mesh_command`.
    func meshCommand(_ body: @escaping (Int) throws -> Void) {
        let port = meshState?.tcpPort ?? 0
        Task { [weak self] in
            let err: String? = await Task.detached(priority: .userInitiated) {
                do { try body(port); return nil }
                catch { return (error as? LocalizedError)?.errorDescription ?? "\(error)" }
            }.value
            guard let self else { return }
            self.meshError = err
            await self.meshTick()
        }
    }

    /// Edit a node's attributes (self or a peer, forwarded over the mesh). Runs the control
    /// round-trip off-main; a `MeshCtlError` lands in `meshError` for the screen.
    func meshSetAttr(nodeID: String, attrs: [String: Any]) {
        meshCommand { try MeshBridge.setAttr(target: nodeID, attrs: attrs, port: $0) }
    }

    /// Mark a peer's device Personal (trust) or Foreign (untrust) — add/remove its proven
    /// fingerprint from the local allowlist. Mirrors the Linux `store.mesh_trust`/`mesh_untrust`.
    func meshSetTrust(fingerprint: String, label: String, trusted: Bool) {
        meshCommand { port in
            if trusted { try MeshBridge.trust(fingerprint: fingerprint, label: label, port: port) }
            else { try MeshBridge.untrust(fingerprint: fingerprint, port: port) }
        }
    }

    /// Lift a ban on a peer's device — it was marked banned after accepting a
    /// SzpontRequest and failing to deliver it (szpontnet-spec/docs/13#the-ban), or
    /// manually. It returns to Foreign; promote via the trust toggle if it's yours.
    /// (Mirrors the Linux store's `mesh_unban`.)
    func meshUnban(fingerprint: String, node: String) {
        meshCommand { try MeshBridge.unban(fingerprint: fingerprint, node: node, port: $0) }
    }

    /// Set the trust level applied to UNKNOWN (unlisted) devices — the mesh screen's
    /// default-trust toggle. `level` is "personal" or "foreign". Runs the control
    /// round-trip off-main; a `MeshCtlError` lands in `meshError` for the screen.
    func meshSetDefaultTrust(level: String) {
        meshCommand { try MeshBridge.setDefaultTrust(level: level, port: $0) }
    }

    /// Pick the mesh's preferred WAN transport (gossiped last-writer-wins, like a
    /// placement edit). It orders the transports a WAN dial tries, so it lands on the
    /// next dial each node makes — no live link is moved. Mirrors the Linux store's
    /// `mesh_set_wan`.
    func meshSetWan(transport: String) {
        meshCommand { try MeshBridge.setWan(transport: transport, port: $0) }
    }

    /// Link to a peer's WAN id (an iroh endpoint id or an onion), reaching a machine
    /// this one may never have met on the LAN. The address shape picks the transport;
    /// a refusal (an id we can't dial, a transport not running here) lands in
    /// `meshError` for the screen. Mirrors the Linux store's `mesh_connect`.
    func meshConnect(address: String) {
        meshCommand { try MeshBridge.connect(address: address, port: $0) }
    }

    /// Record that the user has decided on a newly-seen device (Personal or Keep Foreign),
    /// so its one-time "New device" prompt stops showing. UI-local; does not change trust.
    func meshAckDevice(fingerprint: String) {
        guard !fingerprint.isEmpty else { return }
        meshAckedDevices.insert(fingerprint)
    }

    /// Hand a duty job to the mesh — the wizards' "Run on mesh" path (mirrors the Linux
    /// store's `mesh_dispatch`). The local node picks the executor per the dispatch
    /// strategy and walks failover candidates; the per-slot result dicts (or a transport
    /// error) land in `completion` on the main actor, and the activity feed re-reads so
    /// the node's mesh-dispatch entries appear immediately.
    func meshDispatch(duty: String, prompt: String,
                      completion: @escaping ([[String: Any]], String?) -> Void) {
        let port = meshState?.tcpPort ?? 0
        Task { [weak self] in
            let outcome: ([[String: Any]], String?) = await Task.detached(priority: .userInitiated) {
                do { return (try MeshBridge.dispatch(duty: duty, prompt: prompt, port: port), nil) }
                catch { return ([], (error as? LocalizedError)?.errorDescription ?? "\(error)") }
            }.value
            guard let self else { return }
            completion(outcome.0, outcome.1)
            self.refreshAudit()
            await self.meshTick()
        }
    }

    /// Edit one duty's mesh-wide placement (gossiped last-writer-wins).
    func meshSetOverrides(duty: String, placement: MeshPlacement) {
        let obj = placement.jsonObject()
        meshCommand { try MeshBridge.setOverrides(duty: duty, placement: obj, port: $0) }
    }

    // MARK: - self-update

    /// Fetch origin and compare HEAD to upstream, off the UI thread. Guards against
    /// re-entry while an update is already in flight.
    func refreshUpdateStatus() {
        switch updateState {
        case .checking, .updating, .restarting: return
        default: break
        }
        updateState = .checking
        Task { [weak self] in
            let result = await Task.detached(priority: .utility) { SelfUpdate.check() }.value
            self?.updateState = .idle(result)
        }
    }

    /// Pull the checkout, rebuild `Diplomat.app`, relaunch it. The relaunched instance
    /// terminates this one (newest-wins singleton), so a successful run ends in `.restarting`
    /// with this process about to be replaced; only a failure leaves state to interact with.
    func updateApp() {
        switch updateState {
        case .updating, .restarting: return
        default: break
        }
        updateState = .updating(step: "pulling from origin…")
        Task { [weak self] in
            do {
                let commit = try await Task.detached(priority: .userInitiated) { try SelfUpdate.pull() }.value
                self?.updateState = .updating(step: "building the app at \(commit)…")
                try await Task.detached(priority: .userInitiated) { try SelfUpdate.rebuild() }.value
                self?.updateState = .updating(step: "relaunching…")
                try await Task.detached(priority: .userInitiated) { try SelfUpdate.relaunch() }.value
                self?.updateState = .restarting(commit: commit)
            } catch {
                self?.updateState = .failed((error as? LocalizedError)?.errorDescription ?? "\(error)")
            }
        }
    }

    // MARK: tool data (delegated to the shared core engine)

    func count(for kind: ToolKind) -> Int {
        ToolData.count(for: kind, prs: prs, issues: issues, me: effectiveMe)
    }
    func items(for kind: ToolKind) -> [DisplayItem] {
        ToolData.items(for: kind, prs: prs, issues: issues, me: effectiveMe)
    }
    func lookup(_ number: Int) -> LookupResult {
        ToolData.lookup(number, prs: prs, issues: issues, me: effectiveMe, visible: visibleTools)
    }
}

/// The self-update flow's UI state, mirroring the phases of the Linux front-end's
/// `update_state` dict: checking → idle(result) → updating(step) → restarting, or failed.
enum AppUpdateState: Equatable {
    case checking
    case idle(SelfUpdate.CheckResult)
    case updating(step: String)
    case restarting(commit: String)
    case failed(String)

    /// True while a check or an update is in flight — the Update button is disabled then.
    var isBusy: Bool {
        switch self {
        case .checking, .updating, .restarting: return true
        case .idle, .failed: return false
        }
    }
}
