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

    /// JSON-encoded twin of `persist`, for the Codable caches (tracked processes, the
    /// per-PR attempt maps, the auto-fix fingerprints). Same headless rule; an encode
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

    /// The dispatched agent tasks shown in the ongoing-processes list: the sessions
    /// this machine spawned, plus the work it handed to a mesh node (`mesh` set —
    /// same row, different liveness). Persisted so the list survives an applet
    /// restart, which each kind outlives for its own reason: a session's
    /// tty/window/sentinel handles are OS-level, and a mesh run's lease is held by
    /// the peer executing it.
    @Published var processes: [TrackedProcess] {
        didSet { persistProcesses() }
    }

    /// The folded telemetry ledger the Telemetry screen draws. Republished when a
    /// sample lands or an agent finishes, so an open screen follows the ledger
    /// without a timer of its own.
    @Published var telemetryLedger = Telemetry.Ledger()

    /// One auto-dispatched agent whose completion is still to be recorded: the
    /// ledger key to record against, the sentinel to watch, and the prompt that
    /// identifies the agent's transcript.
    ///
    /// Deliberately in memory and NOT in `TrackedProcess`: that struct is persisted
    /// to UserDefaults on every mutation, and a prompt is kilobytes of text. An
    /// applet restart therefore forfeits the cost of the agents it had in flight,
    /// which the screen reports as unattributed rather than as free.
    ///
    /// It also holds the sentinel path itself rather than looking it up in
    /// `processes`, because a tracked row is removed the moment its terminal window
    /// closes — and an agent that finished and then had its window closed inside one
    /// poll would otherwise take its completion with it.
    private struct TelemetryRun {
        let key: String
        let prompt: String
        let donePath: String
        let at: Double
    }
    private var telemetryInflight: [UUID: TelemetryRun] = [:]

    /// A run whose sentinel never appears (window killed, machine slept) is given
    /// up on after this long, so a stuck entry can't accumulate forever. Matches
    /// the Linux applet's in-flight TTL.
    private static let telemetryRunTTL: TimeInterval = 2 * 60 * 60

    /// Automatic work nothing has started yet — held by the task cap, or by its own
    /// monitor being switched off — in the order it will run. The other half of the
    /// panel's Agent-tasks list.
    ///
    /// Deliberately NOT persisted. A deferral writes no attempt record precisely so
    /// that every poll re-offers everything GitHub still owes, which means the queue
    /// is rebuilt from live evidence every 3 minutes; a stored copy would only ever
    /// be a staler answer to a question already being re-asked, and would hand
    /// "execute now" a prompt assembled against a PR that has since moved on. What
    /// IS persisted is `queuedTaskOrder` — the operator's arrangement, the one thing
    /// a poll cannot reconstruct.
    @Published var queuedTasks: [QueuedAgentTask] = []

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
        static let processes = "trackedProcesses"
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
        static let allocatorSetupDone = "allocatorSetupDone"
        static let queuedTaskOrder = "queuedTaskOrder"
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
    var terminal: SpawnTerminal { SpawnTerminal(rawValue: terminalChoice) ?? .iterm }
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

    init() {
        Store.migrateLegacyDefaultsIfNeeded()
        MeshBridge.migrateLegacyStateDirIfNeeded()
        let defaults = UserDefaults.standard
        usernameOverride = defaults.string(forKey: Keys.usernameOverride) ?? ""
        // SKILL.md + Installer/CLI tools ship hidden (absent key ⇒ default); any
        // Settings toggle persists the explicit set from then on.
        hiddenTools = Set(defaults.stringArray(forKey: Keys.hiddenTools)
            ?? [ToolKind.skillPRs.rawValue, ToolKind.installerPRs.rawValue])
        colorOverrides = (defaults.dictionary(forKey: Keys.colorOverrides) as? [String: String]) ?? [:]
        terminalChoice = defaults.string(forKey: Keys.terminalChoice)
            ?? (SpawnTerminal.iterm.isInstalled ? SpawnTerminal.iterm.rawValue : SpawnTerminal.terminal.rawValue)
        repoPathOverride = AppConfig.string(AppConfig.repoRootKey)
        autoTaskLimit = AppConfig.autoTaskLimit
        // Default ON (absent key ⇒ true): the pill only lights up on a live heartbeat,
        // so defaulting on can't falsely claim "active" when no monitor is running.
        prAutofixEnabled = defaults.object(forKey: Keys.prAutofixEnabled) as? Bool ?? true
        reviewRequestsEnabled = defaults.object(forKey: Keys.reviewRequestsEnabled) as? Bool ?? true
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
        processes = Store.loadProcesses()
        queuedTaskOrder = defaults.stringArray(forKey: Keys.queuedTaskOrder) ?? []
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
        // A full refresh is also where we re-check whether any tracked session's PR
        // has since been merged. Best-effort and after the main load so a PR-state
        // hiccup never blocks the tool data or clobbers its error.
        await refreshMergedStatuses()
    }

    /// Re-check, off the back of an Update, whether any tracked session's PR has been
    /// merged on GitHub, and flip its `merged` flag. Best-effort: a failed probe just
    /// leaves that row unchanged. Only sessions tied to a PR that isn't already known
    /// merged are queried, so the cost is one `gh pr view` per still-open tracked PR.
    func refreshMergedStatuses() async {
        let targets = processes.filter { !$0.merged && $0.prNumber != nil }
        guard !targets.isEmpty else { return }
        var nowMerged: Set<UUID> = []
        for p in targets {
            guard let n = p.prNumber else { continue }
            if let state = try? await API.fetchPRState(number: n), state == "MERGED" {
                nowMerged.insert(p.id)
            }
        }
        guard !nowMerged.isEmpty else { return }
        var next = processes
        var changed = false
        for i in next.indices where nowMerged.contains(next[i].id) && !next[i].merged {
            next[i].merged = true
            changed = true
        }
        if changed { processes = next }
    }

    // MARK: tracked agent sessions

    /// Outcome of clicking a tracked process row.
    enum FocusOutcome { case focused, dismissed }

    /// How often the ongoing-processes list re-checks liveness. Default 8s; override
    /// with `DIPLOMAT_PROC_POLL_SECS` (clamped ≥2s) for tuning/testing.
    static var processPollInterval: TimeInterval {
        let secs = ProcessInfo.processInfo.environment["DIPLOMAT_PROC_POLL_SECS"].flatMap(Double.init)
        return max(2, secs ?? 8)
    }
    private var processPollTask: Task<Void, Never>?

    private func persistProcesses() {
        persistJSON(processes, forKey: Keys.processes)
    }
    private static func loadProcesses() -> [TrackedProcess] {
        guard let data = UserDefaults.standard.data(forKey: Keys.processes),
              let decoded = try? JSONDecoder().decode([TrackedProcess].self, from: data)
        else { return [] }
        return decoded
    }

    /// Register a freshly spawned agent session for tracking, and record it in the audit
    /// log. `source` is "panel" (a wizard SPAWN) or "auto" (a monitor dispatch).
    ///
    /// `kind` drives the tracked-session row's tint; `auditAction` (defaulting to `kind`)
    /// is the verb written to the activity feed. They're decoupled so a review-reply agent
    /// can log a distinct `review-reply` action — feeding the Activity filter its own
    /// "Replies" category — while still rendering as a plain review session.
    ///
    /// `ledgerKey` + `prompt` are supplied only for a monitor dispatch, and are what
    /// lets the sentinel that ends this session be turned into a telemetry
    /// completion with a cost attached.
    func track(kind: String, label: String, prURL: String?, result: AgentSpawner.SpawnResult,
               source: String = "panel", auditAction: String? = nil,
               ledgerKey: String = "", prompt: String = "") {
        let p = TrackedProcess(kind: kind, label: label,
                               terminal: result.terminal.rawValue,
                               windowID: result.windowID, sessionID: result.sessionID,
                               tty: result.tty, donePath: result.donePath, prURL: prURL,
                               source: source)
        if !ledgerKey.isEmpty, !p.donePath.isEmpty {
            telemetryInflight[p.id] = TelemetryRun(key: ledgerKey, prompt: prompt,
                                                   donePath: p.donePath,
                                                   at: Date().timeIntervalSince1970)
        }
        processes.append(p)
        AuditLog.log(source, auditAction ?? kind, label)
    }

    /// Remove one tracked session from the list (the row's ✕ button).
    func removeProcess(_ id: UUID) {
        processes.removeAll { $0.id == id }
    }

    // MARK: mesh rows (work this device handed to a peer)

    /// Register a unit of work the mesh is running elsewhere, so the panel shows it
    /// as a task in flight rather than as nothing at all.
    ///
    /// A mesh dispatch consumes the queued row and leaves no session behind it, so
    /// without this the machine that originated the work has no trace of it — and
    /// "execute now" on a peer-routed task is indistinguishable from a click that
    /// silently dropped it. The row is the same row a local session gets — same
    /// label, same kind, same place in the list — and says where it runs.
    ///
    /// Keyed by the work key, not the row id: one lease is one agent, so a second
    /// dispatch of a key already on the list (a stand-down re-offered before the
    /// in-flight check can see the row) updates that row instead of adding a twin.
    ///
    /// Not private: the queue self-test builds a row here rather than through a real
    /// dispatch, which would need a live mesh node and a peer willing to run it.
    func trackMeshRun(_ job: AgentJob, node: String, attemptNumber: Int,
                      onThisMachine: Bool = false) {
        // The key IS the row's identity here, so a job without one has no row to be:
        // every keyless row would answer to every other's lease. (`routeViaMesh` only
        // dispatches keyed jobs, so nothing in the app arrives here without one.)
        guard !job.workKey.isEmpty else { return }
        let run = TrackedProcess.MeshRun(node: node, workKey: job.workKey,
                                         onThisMachine: onThisMachine)
        if let i = processes.firstIndex(where: { $0.mesh?.workKey == job.workKey }) {
            processes[i].mesh = run
            return
        }
        processes.append(TrackedProcess(
            kind: job.kind,
            label: AgentDispatchGate.label(source: .auto, core: job.label,
                                           attemptNumber: attemptNumber),
            terminal: "", windowID: "", sessionID: "", tty: "", donePath: "",
            prURL: job.prURL, mesh: run,
            source: AgentDispatchGate.Source.auto.rawValue))
        meshClaimSeen[job.workKey] = Date()
    }

    /// When each live mesh row's lease was last seen in the local node's snapshot.
    /// In memory only: it is re-seeded from the row's own age on the first pass after
    /// a restart, and persisting it would mean rewriting every row on every tick just
    /// to record that nothing changed.
    private var meshClaimSeen: [String: Date] = [:]

    /// Re-derive the mesh rows' liveness from the origination leases the local node
    /// publishes, and drop the ones whose agent has finished.
    ///
    /// Dropped, not left reading "done": a finished remote run is the case the sweep
    /// already calls terminal-closed — there is no window to focus, no output to
    /// read, nothing left this machine can do with the row — and the feed keeps the
    /// record. Left on the list they would also hold the PR in-flight against the
    /// monitors, so a run that failed on a peer could never be retried anywhere.
    ///
    /// Not private: the queue self-test drives it with a synthetic claim book, which
    /// is the only way to exercise the lifecycle without a live mesh.
    func reconcileMeshRuns(claims: [String: String], now: Date = Date()) {
        let live = processes.filter(\.isMesh).compactMap { $0.mesh?.workKey }
        guard !live.isEmpty else {
            if !meshClaimSeen.isEmpty { meshClaimSeen = [:] }
            return
        }
        var finished = Set<String>()
        for key in live {
            if claims[key] != nil { meshClaimSeen[key] = now; continue }
            // A row restored from disk has no sighting behind it, so this pass becomes
            // its first and the window runs from here. Its `createdAt` must NOT stand
            // in: a row for an hour-long review reloads already older than any window,
            // and `meshState` is still nil for the first couple of seconds after
            // launch — so an age-based clock would drop every restored row on the
            // first poll, before the node's snapshot could vouch for a single one.
            let seen = meshClaimSeen[key] ?? now
            if MeshAgentRun.finished(sinceSeen: now.timeIntervalSince(seen)) {
                finished.insert(key)
            } else {
                meshClaimSeen[key] = seen
            }
        }
        if !finished.isEmpty {
            processes.removeAll { $0.mesh.map { finished.contains($0.workKey) } ?? false }
        }
        // Keys of rows that have left the list (finished here, or dismissed) stop
        // being tracked — the map is bounded by the list it describes.
        meshClaimSeen = meshClaimSeen.filter { key, _ in
            live.contains(key) && !finished.contains(key)
        }
    }

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
            // The queue first, in the operator's order: a slot that freed since the
            // last cycle belongs to work already waiting for it, not to whichever PR
            // this poll's fetch happens to return first.
            //
            // Only on evidence a cycle actually confirmed. The queue survives a failed
            // cycle deliberately (see `stagedQueue`), but surviving is not the same as
            // being current: while `gh` is down the list freezes, and a drain that kept
            // firing from it would spawn agents at work answered by hand hours ago.
            if autofixPollError == nil { await drainQueuedTasks() }
            // Start this cycle's staging empty. A commit clears it too, so this is
            // what discards the offers of a cycle that failed part-way and never
            // committed — they are re-offered by the cycle that succeeds.
            stagedQueue = []
            await pollMyPRs(owner: owner, repo: repo)
            await pollReviewRequests(owner: owner, repo: repo)
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

    /// My own PRs: dispatch on new conflicts / new review work. Edge-triggered for the
    /// real-time case (a transition observed live), plus a level-triggered reconcile pass
    /// so a review that landed while we were offline — and so was already present the first
    /// time we saw the PR (which the edge-trigger silently baselines) — still gets an agent.
    private func pollMyPRs(owner: String, repo: String) async {
        let snaps: [PRSnapshot]
        do {
            snaps = try await AutofixMonitor.fetchSnapshots(owner: owner, repo: repo, me: effectiveMe)
        } catch {
            notePollFailure(error)   // leave state as-is, retry next tick
            return
        }
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
        let liveRefs = await livePRAgents()
        for s in owed {
            let key = String(s.number)
            let inFlight = processes.contains(where: { $0.prURL == s.url && !$0.done })
                || liveRefs.contains(s.number)
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
        let liveRefs = await livePRAgents()
        for s in conflicted {
            let key = String(s.number)
            let inFlight = processes.contains(where: { $0.prURL == s.url && !$0.done })
                || liveRefs.contains(s.number)
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
        let liveRefs = await livePRAgents()
        func inFlight(_ r: AutofixMonitor.ReviewRequest) -> Bool {
            processes.contains(where: { $0.prURL == r.url && !$0.done })
                || liveRefs.contains(r.number)
        }
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
                prior: attempts[key], stamp: stamp, inFlight: inFlight(r),
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
        unaddressedReviews = owed.filter { !inFlight($0) && !BanList.isBanned($0.author, in: banned) }.count
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

    /// PR numbers with a live `claude` agent visible in `ps` right now — the
    /// tracking-independent half of the monitors' in-flight dedup. The tracked-row
    /// check alone is fragile: rows die with an applet hiccup or a swept window id
    /// while the agent itself keeps running, and the retry backoff (minutes) is far
    /// shorter than an agent's runtime (an hour), so any tracking slip used to
    /// guarantee a duplicate dispatch onto a PR that already had an agent.
    private func livePRAgents() async -> Set<Int> {
        if let c = liveAgentsCache, Date().timeIntervalSince(c.at) < 5 { return c.refs }
        let (owner, repo) = coreRepo
        let refs = await Task.detached(priority: .utility) {
            ProcessMonitor.liveAgentPRNumbers(owner: owner, repo: repo)
        }.value
        liveAgentsCache = (Date(), refs)
        return refs
    }
    /// Brief cache over the `ps` scan so one poll cycle (reconcilers + each
    /// dispatch gate) costs one subprocess, mirroring the Linux store.
    private var liveAgentsCache: (at: Date, refs: Set<Int>)?

    /// How many automatic agents are up on this device right now — the number the cap
    /// is compared against (`AgentDispatchGate.runningAutoTasks`).
    ///
    /// The tracked rows say WHO started each agent, the `ps` scan says which are
    /// really alive; neither alone is enough, so the pure helper combines them.
    ///
    /// A row counts by where its agent RUNS (`runsHere`), not by who dispatched it. A
    /// peer's is not counted: this cap is how many agents THIS machine runs, and
    /// counting them would spend the device's budget on work it is not doing, shrinking
    /// the panel by a free slot for every job routed away. A placement the mesh landed
    /// back here IS counted, from the moment it is made — `ps` alone cannot do that job,
    /// because it takes seconds to see a new agent and a poll dispatches its whole
    /// backlog in one pass, which is exactly when a cap of two started six agents.
    @discardableResult
    private func autoTasksRunning() async -> Int {
        var autoPRs = Set<Int>(), manualPRs = Set<Int>()
        for p in processes where !p.done && p.runsHere {
            guard let n = p.prNumber else { continue }
            if p.source == AgentDispatchGate.Source.panel.rawValue {
                manualPRs.insert(n)
            } else {
                autoPRs.insert(n)
            }
        }
        let n = AgentDispatchGate.runningAutoTasks(livePRs: await livePRAgents(),
                                                   autoPRs: autoPRs,
                                                   manualPRs: manualPRs)
        // Assigned only on a change, like every other published value the 8-second
        // sweep re-derives: `@Published` fires on assignment, not on difference, so
        // an unconditional write would redraw the panel on every tick of an idle
        // machine.
        if autoTasksMeasured != n { autoTasksMeasured = n }
        return n
    }

    /// The last count `autoTasksRunning` measured, published so the panel can draw
    /// the device's free slots without a `ps` scan of its own. Refreshed by every
    /// capacity decision and by the session sweep, so a finished agent frees its bay
    /// on the panel within one sweep rather than at the next 3-minute poll.
    @Published private(set) var autoTasksMeasured = 0

    /// Re-measure for the display alone. The sweep calls it on every tick, including
    /// the ticks where nothing is tracked: an agent can be alive in `ps` with no row
    /// behind it (an applet restart loses the rows, not the agents), and that is
    /// exactly when a wrongly-drawn free bay would be most misleading.
    func refreshAutoTaskCount() async { await autoTasksRunning() }

    /// Pin the measurement, for headless self-tests only. The real one scans `ps` on
    /// whatever machine is running the test, so an assertion about free slots would
    /// otherwise pass or fail on how many agents the developer happens to have open.
    func pinAutoTasksMeasured(_ n: Int) {
        guard Headless.active else { return }
        autoTasksMeasured = n
    }

    /// Slots of this device's cap with nothing in them, as the panel draws them.
    ///
    /// Counted against the higher of the last measurement and what the tracked rows
    /// themselves say. Each is only a lower bound on the truth — the measurement can
    /// predate a spawn this very poll made, the rows miss agents nobody tracked — and
    /// between two lower bounds the larger is the safer: it errs towards drawing one
    /// bay fewer, never towards offering a slot the gate would refuse.
    ///
    /// Work that is starting is ADDED to that, not folded into it: its spawn has
    /// registered with neither source, so it is one neither bound can account for.
    /// Taking the higher of the two and stopping there would lose it whenever the
    /// measurement is already the higher — and drawn as free, its bay would put a row
    /// that is launching next to the empty slot it is launching into, which is one row
    /// more than the cap allows.
    var freeAutoSlots: Int {
        let tracked = Set(processes.filter {
            !$0.done && $0.runsHere && $0.source != AgentDispatchGate.Source.panel.rawValue
        }.compactMap(\.prNumber)).count
        return AgentTaskQueue.freeSlots(
            limit: autoTaskLimit,
            running: max(autoTasksMeasured, tracked) + startingTasks.count
        )
    }

    /// Whether the "deferring auto work" note has been logged for the current
    /// at-capacity episode.
    private var capacityLogged = false

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
        // Only PR-scoped work with a monitor behind it can be queued: a task nothing
        // can name is one the next poll cannot recognise as the same one, and a task
        // no monitor owns is one nothing would re-offer — the queue would be the only
        // record of it, which is precisely what this list is not. (Every automatic job
        // is both; the sweeps that are neither are panel-only, and a click is uncapped.)
        guard let number = job.prNumber, job.counter != nil else { return }
        let entry = QueuedAgentTask(
            id: AgentTaskQueue.key(auditAction: job.auditAction, prNumber: number),
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
    /// Capacity is re-counted per task because each spawn fills a slot. A spawn
    /// failure stops the drain: it means terminal automation is broken, not that this
    /// one task was unlucky, and each entry is taken off the list before it is tried —
    /// so walking the whole queue into the same failure would clear the panel of every
    /// queued row at once, for a reason none of them caused.
    private func drainQueuedTasks() async {
        for entry in drainableTasks {
            // The list moves under this loop: it awaits a spawn per task, and an
            // "execute now" during one of those takes its row off the queue and
            // starts it there and then. Re-reading the queue is what keeps the drain
            // from dispatching that same task a second time when it reaches it.
            guard queuedTasks.contains(where: { $0.id == entry.id }) else { continue }
            guard await autoTasksRunning() < autoTaskLimit else { return }
            // Finding room here is what re-arms the saturation notice. The gate's own
            // reset sits behind the capacity measurement this path skips, so without
            // this the feed would carry one `at-capacity` line for an unbounded run of
            // saturate-and-drain episodes instead of one apiece.
            capacityLogged = false
            if case .failed = await runQueuedTask(entry) { return }
        }
    }

    /// Whether the monitor that owns this work is switched off.
    ///
    /// A switched-off monitor still finds its work and still queues it — what your
    /// PRs owe is worth seeing whether or not this machine is set to act on it — but
    /// nothing automatic starts it. It waits for "execute now", or for the toggle to
    /// come back on. That is the whole difference the two toggles make now: they
    /// decide who starts the work, not whether it is known.
    func isPaused(_ counter: AutoCounter?) -> Bool {
        switch counter {
        case .reviewRequests:        return !reviewRequestsEnabled
        case .myReviews, .conflicts: return !prAutofixEnabled
        // Unreachable: a job with no monitor behind it is never queued (`stageQueued`).
        // Answering "not paused" keeps the unreachable case from being the one that
        // silently holds work back.
        case nil:                    return false
        }
    }

    /// The queued tasks the drain may start, in the operator's order — everything
    /// whose monitor is still on.
    ///
    /// Not private: this is the seam the queue self-test drives. Asserting on the
    /// list the drain walks is how the "a paused monitor's work is held, not run"
    /// rule gets a test at all — driving the drain itself would end in a real spawn.
    var drainableTasks: [QueuedAgentTask] {
        queuedTasks.filter { !isPaused($0.job.counter) }
    }

    /// Dispatch one queued task past the capacity check its caller already made, and
    /// record the attempt its monitor would have recorded.
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
    /// `startingTasks` runs in the same actor turn as the row that replaces it
    /// (`track` / `trackMeshRun` are called inside `dispatchAgent`, which then returns
    /// without suspending again). Between them the task is a row the whole way: never
    /// drawn twice, and never missing.
    @discardableResult
    private func runQueuedTask(_ entry: QueuedAgentTask) async -> DispatchOutcome {
        beginStarting(entry)
        let outcome = await dispatchAgent(entry.job, source: .auto,
                                          attemptNumber: entry.attemptNumber,
                                          bypassCapacity: true)
        endStarting(entry.id)
        if outcome.wasHandled { recordQueuedAttempt(entry) }
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

    /// The queued row's "execute now": start this task immediately, past the cap.
    ///
    /// It stays AUTO work — same `Auto · ` label, same auto-handled counter, mesh
    /// routing still applies, and once running it occupies a slot like any other
    /// automatic agent, so the rest of the queue waits behind it. Of the five
    /// asymmetries the gate draws between a click and a monitor tick (focus, capacity,
    /// mesh, counters, label) this borrows exactly one: the cap, which is the only one
    /// the operator is overriding.
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
        switch await runQueuedTask(entry) {
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
        case .atCapacity:
            break   // unreachable: the run bypasses the cap the operator overrode
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
        var kind: String            // tracked-row tint: "review" | "conflicts" | "audit"
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
    }

    enum AutoCounter { case reviewRequests, myReviews, conflicts }

    /// One unit of automatic work nothing has started yet: the whole job, held by the
    /// device's task cap or by its own monitor being switched off, until a slot frees
    /// or the operator runs it. Rebuilt from live evidence on each poll — see
    /// `queuedTasks`.
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
        case failed(String)
        var didSpawn: Bool { if case .spawned = self { return true }; return false }
        /// The work is now being handled — spawned locally OR stood down to a peer
        /// whose agent already owns it. This is the signal to record the attempt and
        /// start the retry backoff, mirroring the Python reference which treats
        /// `("spawned", VERDICT_STAND_DOWN)` as handled. `.failed` deliberately does
        /// NOT count (a transient spawn error retries next poll); nor do `.inFlight`
        /// / `.banned` / `.atCapacity`. Using `.didSpawn` here instead would
        /// re-dispatch peer-owned work to the mesh on every poll, the backoff never
        /// engaging — and counting `.atCapacity` as handled would drop deferred work
        /// into a 5m–3h cooldown instead of offering it again the moment an agent
        /// finishes.
        var wasHandled: Bool {
            switch self {
            case .spawned, .standDown: return true
            case .inFlight, .banned, .atCapacity, .failed: return false
            }
        }
    }

    /// Run one agent job through the shared gate (`AgentDispatchGate` — the pure,
    /// smoke-tested decision both platforms mirror) and, on `.proceed`, spawn +
    /// track it. `resolvingPRs` is taken for the whole await span of any PR-scoped
    /// job, so a double-click or an overlapping poll can't race two spawns onto
    /// one PR (it also drives the panel row's spinner). In-flight evidence is the
    /// tracked rows OR a live `claude` visible in `ps` — the ground-truth floor
    /// that also catches agents whose local bookkeeping was lost and mesh jobs
    /// that landed on this very machine.
    ///
    /// An AUTO job is additionally capped at `autoTaskLimit` concurrent agents on
    /// this device (`autoTasksRunning`), and held outright while its own monitor is
    /// switched off (`isPaused`); a panel click is subject to neither. Either refusal
    /// queues the job (`stageQueued`), which is what the panel's Agent-tasks list
    /// shows as *queued*.
    ///
    /// `bypassCapacity` is for the two callers that have already answered the
    /// capacity question themselves: the queue drain (which counted the free slot it
    /// is filling, and skips paused work by construction) and "execute now" (where
    /// the operator is overriding both holds deliberately). It skips the measurement —
    /// not just its verdict — so neither pays for a second `ps` scan, and so a forced
    /// run cannot re-queue itself.
    @discardableResult
    func dispatchAgent(_ job: AgentJob, source: AgentDispatchGate.Source,
                       attemptNumber: Int = 1,
                       bypassCapacity: Bool = false) async -> DispatchOutcome {
        if let n = job.prNumber {
            if resolvingPRs.contains(n) { return .inFlight }
            resolvingPRs.insert(n)
        }
        defer { if let n = job.prNumber { resolvingPRs.remove(n) } }
        let banned = job.authorLogin.map { BanList.isBanned($0, in: BanList.read()) } ?? false
        var agentOnPR = false
        if let url = job.prURL {
            agentOnPR = processes.contains { $0.prURL == url && !$0.done }
            if !agentOnPR, let n = job.prNumber {
                agentOnPR = await livePRAgents().contains(n)
            }
        }
        // Measured only for an auto job that would otherwise run: the count costs a
        // `ps` scan, a panel click is never capped, and an in-flight PR spawns
        // nothing either way — so in both of those the answer would be discarded.
        // Finding room is also what re-arms the "deferring" note.
        var atCapacity = false, paused = false
        if source == .auto, !agentOnPR, !bypassCapacity {
            let full = await autoTasksRunning() >= autoTaskLimit
            if !full { capacityLogged = false }
            // A switched-off monitor has no room for its own work, whatever the
            // device's. Modelled as capacity because the answer is the same one in
            // every respect that matters here — hold the job, write no attempt
            // record, re-offer it next poll — which keeps a toggle that only this
            // front-end has out of the dispatch gate both front-ends mirror.
            paused = isPaused(job.counter)
            atCapacity = full || paused
        }
        switch AgentDispatchGate.decide(source: source, banned: banned,
                                        agentOnPR: agentOnPR, meshStandsDown: false,
                                        atCapacity: atCapacity) {
        case .atCapacity:
            // A paused monitor is not a saturated device: it queues silently, because
            // the operator switched it off on purpose and the row says the rest.
            if !paused { logAtCapacity() }
            stageQueued(job, attemptNumber: attemptNumber)
            return .atCapacity
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
                AuditLog.log(source.rawValue, job.auditAction,
                             AgentDispatchGate.label(source: source, core: job.label,
                                                     attemptNumber: attemptNumber))
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
                return .spawned(terminal: "mesh")
            case .local:
                break               // fall through to a local tracked spawn
            }
        }
        let preferred = terminal
        let restoreBID = AgentDispatchGate.stealsFocus(source) ? nil : frontmostAppBundleID
        let prompt = job.prompt
        do {
            let result = try await Task.detached(priority: .userInitiated) {
                try AgentSpawner.spawn(prompt, terminal: preferred, restoreFocusTo: restoreBID)
            }.value
            let tracked = source == .auto ? job.ledgerKey : ""
            track(kind: job.kind,
                  label: AgentDispatchGate.label(source: source, core: job.label,
                                                 attemptNumber: attemptNumber),
                  prURL: job.prURL, result: result, source: source.rawValue,
                  auditAction: job.auditAction, ledgerKey: tracked, prompt: job.prompt)
            bumpAutoCounter(job, source: source, attemptNumber: attemptNumber)
            recordTelemetryStart(job, source: source, remote: false,
                                 attemptNumber: attemptNumber)
            return .spawned(terminal: result.terminal.rawValue)
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
        case .spawned, .banned, .standDown, .atCapacity:
            // `.standDown` / `.atCapacity` are answers only a monitor gets — neither
            // the mesh gate nor the automatic-task cap applies to a click.
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

    /// Per-tty last erroring tail — the idle-confirmation gate. A session is nudged only
    /// once its erroring tail has stopped changing between two consecutive scans, i.e. it
    /// is genuinely stalled rather than actively producing output that merely mentions an
    /// API error (e.g. a session developing/logging error strings, or one that already
    /// recovered and moved on while the error line is still on screen). Pruned alongside
    /// `apiErrorBackoff` to currently-erroring ttys.
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

    /// One scan: read every terminal's last visible lines and, for any showing a Claude
    /// API error (outside its cooldown), send the continue nudge to that exact session.
    func runApiErrorScanOnce() async {
        guard apiWatchEnabled, !apiScanInFlight else { return }
        apiScanInFlight = true
        defer { apiScanInFlight = false }
        // nil = the dump itself failed (automation permission revoked, AppleEvent
        // timeout) — skip the whole scan rather than treating it as "no sessions",
        // which would wrongly clear every backoff and hide the breakage.
        let dump = await Task.detached(priority: .utility) { ApiErrorWatcher.dumpSessionsCached() }.value
        guard let sessions = dump else { return }
        let now = Date()
        var erroring = Set<String>()
        for s in sessions {
            // Out-of-quota banners return false here (looksLikeApiError ignores them):
            // a quota-limited agent can't progress until its window resets, so nudging
            // it is pointless — only transient server/connectivity errors are nudged.
            guard ApiErrorMatch.looksLikeApiError(s.tail) else { continue }
            erroring.insert(s.tty)
            // Idle-confirmation (ApiErrorMatch.isConfirmedStall): only nudge a session
            // whose erroring tail is UNCHANGED since the previous scan. An actively-working
            // session (output still scrolling — one merely printing/discussing an API-error
            // string, or a CLI mid auto-retry with a live countdown) changes between scans
            // and must not be treated as stalled; a genuinely stuck session's tail is
            // static. Costs one extra scan (~apiWatchInterval) of latency on a real stall —
            // nothing against a feature meant for overnight overload stalls.
            let stalled = ApiErrorMatch.isConfirmedStall(previousTail: apiErrorSeenTail[s.tty],
                                                         currentTail: s.tail)
            apiErrorSeenTail[s.tty] = s.tail
            guard stalled else { continue }
            // Still inside this session's current backoff window — hold off.
            if let b = apiErrorBackoff[s.tty], now < b.nextAllowed { continue }
            let tty = s.tty
            let sent = await Task.detached(priority: .userInitiated) {
                ApiErrorWatcher.sendContinue(tty: tty)
            }.value
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
                "Continued a stalled agent (API error) on \(tty); "
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
                await self?.refreshProcessStatuses()
                await self?.refreshAutoTaskCount()
                await self?.refreshDeviceState()
                self?.refreshBanList()
                self?.refreshAudit()
                await self?.runTelemetrySampleOnce()
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

    /// Turn finished agents into ledger completions, costed from their own Claude
    /// transcripts.
    ///
    /// Driven by the completion sentinel rather than the tracked row's `done` flag,
    /// which also goes true when a terminal window is closed — that is a session we
    /// stopped being able to watch, not a run whose duration means anything. The
    /// sentinel's mtime is when `claude` actually exited; `now` is whenever a poll got
    /// round to looking, up to a poll period later, and would inflate every run time.
    private func reconcileTelemetryCompletions() async {
        guard !telemetryInflight.isEmpty else { return }
        let now = Date().timeIntervalSince1970
        var finished: [(key: String, prompt: String, at: Double, done: Double)] = []
        for (id, run) in telemetryInflight {
            guard let attrs = try? FileManager.default.attributesOfItem(atPath: run.donePath),
                  let mtime = (attrs[.modificationDate] as? Date)?.timeIntervalSince1970
            else {
                // No sentinel yet. Past the TTL there never will be one — the window
                // was killed, or the machine slept through the run — and there is
                // nothing honest left to record.
                if now - run.at > Store.telemetryRunTTL { telemetryInflight[id] = nil }
                continue
            }
            telemetryInflight[id] = nil
            finished.append((run.key, run.prompt, run.at, mtime))
        }
        guard !finished.isEmpty else { return }
        // Scanning transcripts walks ~/.claude, so it stays off the main actor. The
        // batch is bound to a `let` first because capturing a mutable var in
        // concurrently-executing code is an error under the Swift 5.10 toolchain the
        // macOS CI job builds with — 6.x proves the mutation is finished and accepts
        // it, so this only ever fails away from the machine it was written on.
        let completions = finished
        await Task.detached(priority: .utility) {
            for f in completions {
                let tokens = UsageScan.taskTokens(prompt: f.prompt, startedAt: f.at,
                                                  endedAt: f.done)
                TelemetryLog.done(key: f.key, at: f.done, tokens: tokens)
            }
        }.value
        refreshTelemetry()
    }

    /// Take one quota/token reading for the ledger, off the main actor.
    ///
    /// Rides the process poll but answers to its own pacing: the share of this
    /// machine's tokens that goes on the monitored repo is worth knowing whether or
    /// not the monitors are enabled, and pricing the rate-limit window needs an
    /// unbroken sample series regardless. `TelemetryLog.sampleDue` does the pacing, so calling
    /// this more often than the sample interval is free.
    func runTelemetrySampleOnce() async {
        await reconcileTelemetryCompletions()
        guard TelemetryLog.sampleDue() else { return }
        await Task.detached(priority: .utility) {
            let quota = Quota.fractionsLeft()
            let totals = UsageScan.totals()
            TelemetryLog.sample(sessionLeft: quota.session, weekLeft: quota.week,
                                repoTokens: totals.repo, otherTokens: totals.other)
        }.value
        refreshTelemetry()
    }

    /// Re-derive each session's `done` flag off the main thread (one `ps` call), drop
    /// any whose terminal window/tab was closed, then merge the rest back by id so a
    /// concurrent add/remove isn't clobbered.
    func refreshProcessStatuses() async {
        // The mesh rows first: they are the ones the sweep below cannot speak for, and
        // dropping a finished one before the sweep keeps the two liveness sources from
        // reporting on the same tick's list in different states. With the mesh off
        // there is no claim book to consult, so every row settles out — a machine that
        // has stopped watching the mesh must not keep drawing its runs as live.
        reconcileMeshRuns(claims: meshEnabled ? (meshState?.claims ?? [:]) : [:])
        let snapshot = processes
        guard !snapshot.isEmpty else { return }
        let sweep = await Task.detached(priority: .utility) {
            // One osascript dump of every session's visible buffer (tty → tail) lets the
            // sweep tell a working agent from one idling at the prompt (awaiting input).
            // Cached/shared with the API-error scan; nil (dump failed) degrades to "no
            // tails" — the sweep then can't compute awaiting-input but still sweeps.
            let sessions = ApiErrorWatcher.dumpSessionsCached() ?? []
            let tails = Dictionary(sessions.map { ($0.tty, $0.tail) },
                                   uniquingKeysWith: { first, _ in first })
            return ProcessMonitor.sweep(snapshot, sessionTails: tails)
        }.value
        var stateByID: [UUID: (done: Bool, awaiting: Bool)] = [:]
        for p in sweep.refreshed { stateByID[p.id] = (p.done, p.awaitingInput) }
        var next = processes
        var changed = false
        // The terminal was closed → the session is no longer something we can monitor;
        // remove it from the list instead of leaving a dead "done" row.
        if !sweep.closedIDs.isEmpty {
            let before = next.count
            next.removeAll { sweep.closedIDs.contains($0.id) }
            if next.count != before { changed = true }
        }
        for i in next.indices {
            guard let s = stateByID[next[i].id] else { continue }
            if next[i].done != s.done { next[i].done = s.done; changed = true }
            if next[i].awaitingInput != s.awaiting { next[i].awaitingInput = s.awaiting; changed = true }
        }
        if changed { processes = next }
    }

    /// Click a tracked row: bring its terminal window to the front. If that fails the
    /// window is gone, so re-run the sweep to dismiss the dead row immediately rather
    /// than leaving it to linger (or falling back to opening the browser). The
    /// osascript focus runs off the main thread so the popover never hitches.
    func activate(_ p: TrackedProcess) async -> FocusOutcome {
        let focused = await Task.detached(priority: .userInitiated) {
            ProcessMonitor.focus(p)
        }.value
        if focused { return .focused }
        await refreshProcessStatuses()
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
