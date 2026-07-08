import SwiftUI
import AppKit
import ArgentUtilsCore

/// macOS UI mapping for a tool's tint. The catalog (`core/catalog.json`) carries
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

    /// Persist a settings value — EXCEPT in render mode. A headless render seeds this
    /// Store with preview values through the same persisted properties the GUI uses,
    /// and it shares the live app's defaults domain: an unguarded write would silently
    /// flip the user's real settings (a past render turned the auto-approve opt-in ON).
    /// Every settings didSet must go through here.
    private func persist(_ value: Any?, forKey key: String) {
        guard !Headless.isRender else { return }
        UserDefaults.standard.set(value, forKey: key)
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
    /// Whether the in-process PR auto-fix monitor is on. Persisted; when turned on we
    /// kick an immediate poll rather than waiting for the next tick.
    @Published var prAutofixEnabled: Bool {
        didSet {
            persist(prAutofixEnabled, forKey: Keys.prAutofixEnabled)
            if prAutofixEnabled && !oldValue && !Headless.isRender { Task { await runAutofixPollOnce() } }
        }
    }

    /// Whether to auto-dispatch a full-E2E review when someone requests my review on a
    /// PR (someone else's PR → review-only, leave comments). Persisted; kicks a poll on
    /// enable. Independent of `prAutofixEnabled`.
    @Published var reviewRequestsEnabled: Bool {
        didSet {
            persist(reviewRequestsEnabled, forKey: Keys.reviewRequestsEnabled)
            if reviewRequestsEnabled && !oldValue && !Headless.isRender { Task { await runAutofixPollOnce() } }
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

    /// Master switch for auto-approvals: whether an auto-dispatched review may EVER submit
    /// a verdict (APPROVE / request changes) on my behalf. Default OFF — every auto-review
    /// leaves comments only and the final call stays with me until I opt in. The per-class
    /// withhold flags below only matter when this is on.
    @Published var autoApproveEnabled: Bool {
        didSet { persist(autoApproveEnabled, forKey: Keys.autoApproveEnabled) }
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
            if apiWatchEnabled && !oldValue && !Headless.isRender { Task { await runApiErrorScanOnce() } }
        }
    }

    /// The dispatched agent sessions shown in the ongoing-processes list. Persisted
    /// so the list survives an applet restart — the tty/window/sentinel handles are
    /// OS-level and outlive this process.
    @Published var processes: [TrackedProcess] {
        didSet { persistProcesses() }
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
        static let verdictWithholdSkill = "verdictWithholdSkill"
        static let verdictWithholdInstaller = "verdictWithholdInstaller"
        static let verdictWithholdCommunity = "verdictWithholdCommunity"
        static let apiWatchEnabled = "apiWatchEnabled"
        static let apiWatchContinues = "apiWatchContinues"
        static let myConflictAttempts = "myConflictAttempts"
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
    /// `ARGENT_UTILS_REFRESH_SECS` (clamped to ≥5s) for tuning/testing.
    static var autoRefreshInterval: TimeInterval {
        let secs = ProcessInfo.processInfo.environment["ARGENT_UTILS_REFRESH_SECS"].flatMap(Double.init)
        return max(5, secs ?? 5 * 60)
    }
    private var autoRefreshTask: Task<Void, Never>?

    init() {
        let defaults = UserDefaults.standard
        usernameOverride = defaults.string(forKey: Keys.usernameOverride) ?? ""
        hiddenTools = Set(defaults.stringArray(forKey: Keys.hiddenTools) ?? [])
        colorOverrides = (defaults.dictionary(forKey: Keys.colorOverrides) as? [String: String]) ?? [:]
        terminalChoice = defaults.string(forKey: Keys.terminalChoice)
            ?? (SpawnTerminal.iterm.isInstalled ? SpawnTerminal.iterm.rawValue : SpawnTerminal.terminal.rawValue)
        // Default ON (absent key ⇒ true): the pill only lights up on a live heartbeat,
        // so defaulting on can't falsely claim "active" when no monitor is running.
        prAutofixEnabled = defaults.object(forKey: Keys.prAutofixEnabled) as? Bool ?? true
        reviewRequestsEnabled = defaults.object(forKey: Keys.reviewRequestsEnabled) as? Bool ?? true
        // Auto-approvals OFF by default — an auto-review never submits a verdict on my
        // behalf until I explicitly opt in.
        autoApproveEnabled = defaults.object(forKey: Keys.autoApproveEnabled) as? Bool ?? false
        verdictWithholdSkill = defaults.object(forKey: Keys.verdictWithholdSkill) as? Bool ?? true
        verdictWithholdInstaller = defaults.object(forKey: Keys.verdictWithholdInstaller) as? Bool ?? true
        verdictWithholdCommunity = defaults.object(forKey: Keys.verdictWithholdCommunity) as? Bool ?? true
        apiWatchEnabled = defaults.object(forKey: Keys.apiWatchEnabled) as? Bool ?? true
        processes = Store.loadProcesses()
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
            refreshBanList()
            refreshAudit()
            Task { await fetchMe() }
            Task { await refreshDeviceState() }
            Task { await refreshAllocatorInstall() }
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

    /// Shell the installer's `--check` (Node startup, ~100-300ms) off-main and
    /// publish the result. Called at startup, when Settings opens, and post-install.
    func refreshAllocatorInstall() async {
        allocatorInstall = await Task.detached(priority: .utility) { DeviceAllocator.check() }.value
    }

    func installAllocator() async {
        allocatorInstall = await Task.detached(priority: .utility) { DeviceAllocator.install() }.value
        AuditLog.log("panel", "allocator-install",
                     "Installed device allocator (ok: \(allocatorInstall?.installed == true))")
        refreshAudit()
        await refreshDeviceState()
    }

    func uninstallAllocator() async {
        allocatorInstall = await Task.detached(priority: .utility) { DeviceAllocator.uninstall() }.value
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
    /// with `ARGENT_UTILS_PROC_POLL_SECS` (clamped ≥2s) for tuning/testing.
    static var processPollInterval: TimeInterval {
        let secs = ProcessInfo.processInfo.environment["ARGENT_UTILS_PROC_POLL_SECS"].flatMap(Double.init)
        return max(2, secs ?? 8)
    }
    private var processPollTask: Task<Void, Never>?

    private func persistProcesses() {
        // A headless render shares the live app's defaults domain; never let seeded
        // preview rows overwrite the user's real tracked-process list.
        guard !Headless.isRender else { return }
        if let data = try? JSONEncoder().encode(processes) {
            UserDefaults.standard.set(data, forKey: Keys.processes)
        }
    }
    private static func loadProcesses() -> [TrackedProcess] {
        guard let data = UserDefaults.standard.data(forKey: Keys.processes),
              let decoded = try? JSONDecoder().decode([TrackedProcess].self, from: data)
        else { return [] }
        return decoded
    }

    /// Register a freshly spawned agent session for tracking, and record it in the audit
    /// log. `source` is "panel" (a wizard SPAWN) or "auto" (a monitor dispatch).
    func track(kind: String, label: String, prURL: String?, result: AgentSpawner.SpawnResult,
               source: String = "panel") {
        let p = TrackedProcess(kind: kind, label: label,
                               terminal: result.terminal.rawValue,
                               windowID: result.windowID, sessionID: result.sessionID,
                               tty: result.tty, donePath: result.donePath, prURL: prURL)
        processes.append(p)
        AuditLog.log(source, kind, label)
    }

    /// Remove one tracked session from the list (the row's ✕ button).
    func removeProcess(_ id: UUID) {
        processes.removeAll { $0.id == id }
    }

    // MARK: PR auto-fix monitor

    /// How often the monitor polls GitHub. 3 min by default — the GraphQL rate limit
    /// (5000 points/hr) is real and these searches aren't cheap, so a tight cadence blows
    /// the budget. Responsiveness comes from the immediate poll on wake / on enable, not
    /// from a fast steady cadence. Override for testing.
    static var autofixPollInterval: TimeInterval {
        let secs = ProcessInfo.processInfo.environment["ARGENT_UTILS_AUTOFIX_SECS"].flatMap(Double.init)
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
    /// agent for each PR that just gained a conflict or new review work. No-op when
    /// the feature is off or our login isn't known yet.
    func runAutofixPollOnce() async {
        guard !autofixPollInFlight else { return }
        autofixPollInFlight = true
        defer { autofixPollInFlight = false }
        if me.isEmpty { await fetchMe() }
        guard !effectiveMe.isEmpty else { return }
        let (owner, repo) = coreRepo
        pollErrorThisCycle = nil
        if prAutofixEnabled { await pollMyPRs(owner: owner, repo: repo) }
        if reviewRequestsEnabled { await pollReviewRequests(owner: owner, repo: repo) }
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
            let inFlight = processes.contains(where: { $0.prURL == s.url && !$0.done })
            let decision = ReviewReconcile.decide(prior: attempts[key], stamp: "unresolved",
                                                  inFlight: inFlight, banned: false, now: now)
            if case .dispatch(let attemptNumber) = decision {
                if await dispatchMyReview(s, attemptNumber: attemptNumber) {
                    attempts[key] = ReviewAttempt(requestedAt: "unresolved",
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
    /// it gets a Resolve-conflicts agent — with `ReviewReconcile` retry backoff, so a
    /// spawn that failed (e.g. terminal-automation permission revoked) is retried instead
    /// of being silently consumed forever, and a conflict that already existed when the
    /// monitor first saw the PR (which the edge-trigger baselines) still gets an agent.
    /// The record is pruned once the PR stops conflicting.
    private func reconcileMyConflicts(snaps: [PRSnapshot], now: Date) async {
        var attempts = loadMyConflictAttempts()
        let conflicted = snaps.filter { $0.mergeable == "CONFLICTING" }
        for s in conflicted {
            let key = String(s.number)
            let inFlight = processes.contains(where: { $0.prURL == s.url && !$0.done })
            let decision = ReviewReconcile.decide(prior: attempts[key], stamp: "conflicting",
                                                  inFlight: inFlight, banned: false, now: now)
            if case .dispatch(let attemptNumber) = decision {
                if await dispatchConflictFix(number: s.number, url: s.url,
                                             attemptNumber: attemptNumber, source: "auto") {
                    attempts[key] = ReviewAttempt(requestedAt: "conflicting",
                                                  lastDispatchedAt: now, attempts: attemptNumber)
                }
            }
        }
        let keys = Set(conflicted.map { String($0.number) })
        attempts = attempts.filter { keys.contains($0.key) }
        saveMyConflictAttempts(attempts)
    }

    private func loadMyConflictAttempts() -> [String: ReviewAttempt] {
        guard let data = UserDefaults.standard.data(forKey: Keys.myConflictAttempts),
              let decoded = try? JSONDecoder().decode([String: ReviewAttempt].self, from: data)
        else { return [:] }
        return decoded
    }
    private func saveMyConflictAttempts(_ map: [String: ReviewAttempt]) {
        guard !Headless.isRender else { return }
        if let data = try? JSONEncoder().encode(map) {
            UserDefaults.standard.set(data, forKey: Keys.myConflictAttempts)
        }
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
        func inFlight(_ r: AutofixMonitor.ReviewRequest) -> Bool {
            processes.contains(where: { $0.prURL == r.url && !$0.done })
        }
        for r in owed {
            let key = String(r.number)
            let stamp = r.requestedAt ?? "-"
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
    /// Remove a ban (the UI's un-ban button) and refresh. When the daemon handled the
    /// unban it also wrote the audit entry — don't double-log.
    func unban(_ login: String) {
        let viaDaemon = BanList.unban(login)
        if !viaDaemon { AuditLog.log("panel", "unban", "Un-banned @\(login)") }
        refreshAudit()
        refreshBanList()
    }

    /// Spawn the most-comprehensive Review action (Full E2E ×2, formal per-line comments,
    /// no auto-verdict) on a PR someone asked me to review — hands off the branch.
    /// `attemptNumber` ≥2 means this is a retry of a review a previous agent left
    /// unaddressed; it's surfaced in the label/audit so the re-dispatch is visible.
    @discardableResult
    private func dispatchReviewRequest(_ r: AutofixMonitor.ReviewRequest, attemptNumber: Int = 1) async -> Bool {
        // Auto-approvals must be enabled AND no configured suppressor may match (SKILL /
        // installer / community PR) for an auto-review to submit a verdict. Otherwise it's
        // comments-only and the final call stays with me.
        let reasons = verdictPolicy.withholdReasons(files: r.files, authorAssociation: r.authorAssociation)
        let verdict = autoApproveEnabled && reasons.isEmpty
        let prompt = ReviewConfig(depth: "max", target: .specific, me: effectiveMe,
                                  markReady: false, leaveReviews: true, replyToReviews: false,
                                  specificPR: String(r.number), finalPass: verdict,
                                  specificAuthor: .theirs).buildPrompt()
        let preferred = terminal
        do {
            let result = try await Task.detached(priority: .userInitiated) {
                try AgentSpawner.spawn(prompt, terminal: preferred)
            }.value
            let tag: String
            if verdict {
                tag = " +verdict"
            } else if !autoApproveEnabled {
                tag = " −verdict (auto-approvals off)"
            } else {
                tag = " −verdict (\(reasons.joined(separator: ", ")))"
            }
            let retry = attemptNumber > 1 ? " · retry \(attemptNumber)" : ""
            track(kind: "review", label: "Auto · Review-req · #\(r.number) (@\(r.author))\(tag)\(retry)",
                  prURL: r.url, result: result, source: "auto")
            // Retries of the same review are re-dispatches, not new reviews handled —
            // count each review once or the Settings tally overclaims.
            if attemptNumber == 1 { reviewRequestsHandled += 1 }
            return true
        } catch {
            AuditLog.log("auto", "spawn-failed",
                         "Review-req #\(r.number) failed to spawn: \((error as? LocalizedError)?.errorDescription ?? "\(error)")")
            refreshAudit()
            return false
        }
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
        guard !Headless.isRender else { return }
        if let data = try? JSONEncoder().encode(map) {
            UserDefaults.standard.set(data, forKey: Keys.reviewReqAttempts)
        }
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

    /// Spawn a Resolve-conflicts agent for one PR — shared by the conflicts reconciler
    /// ("auto") and the panel's per-row button ("panel"). `resolvingPRs` is inserted
    /// BEFORE the (seconds-long) spawn await, so a double-click or an overlapping poll
    /// can't launch two agents on the same PR; the in-flight process check dedups
    /// against agents that are already tracked. Returns whether an agent launched.
    @discardableResult
    private func dispatchConflictFix(number: Int, url: String,
                                     attemptNumber: Int = 1, source: String) async -> Bool {
        if resolvingPRs.contains(number) { return false }
        if processes.contains(where: { $0.prURL == url && !$0.done }) { return false }
        resolvingPRs.insert(number)
        defer { resolvingPRs.remove(number) }
        let prompt = ConflictConfig(target: .specific, me: effectiveMe,
                                    specificPR: String(number)).buildPrompt()
        let preferred = terminal
        do {
            let result = try await Task.detached(priority: .userInitiated) {
                try AgentSpawner.spawn(prompt, terminal: preferred)
            }.value
            let retry = attemptNumber > 1 ? " · retry \(attemptNumber)" : ""
            let label = source == "auto" ? "Auto · Resolve · #\(number)\(retry)" : "Resolve · #\(number)"
            track(kind: "conflicts", label: label, prURL: url, result: result, source: source)
            if attemptNumber == 1 { autofixConflictsHandled += 1 }
            return true
        } catch {
            // Spawn failed (e.g. terminal-automation permission revoked). Audit it —
            // this used to be completely silent — and let the reconciler retry with
            // backoff.
            AuditLog.log(source, "spawn-failed",
                         "Resolve #\(number) failed to spawn: \((error as? LocalizedError)?.errorDescription ?? "\(error)")")
            refreshAudit()
            return false
        }
    }

    /// Spawn the Deep, fix-on-branch review agent for one of MY PRs (reply "Fixed in
    /// <hash>", don't mark ready, no formal review) and track it. Returns whether the spawn
    /// succeeded — so the reconciler only starts its retry backoff once an agent actually
    /// launched. `attemptNumber` ≥2 (a reconcile retry) is surfaced in the label/audit.
    @discardableResult
    private func dispatchMyReview(_ s: PRSnapshot, attemptNumber: Int = 1) async -> Bool {
        // Never pile a second agent on a PR that already has one running.
        if processes.contains(where: { $0.prURL == s.url && !$0.done }) { return false }
        let prompt = ReviewConfig(depth: "deep", target: .specific, me: effectiveMe,
                                  markReady: false, leaveReviews: false, replyToReviews: true,
                                  specificPR: String(s.number), specificAuthor: .mine).buildPrompt()
        let preferred = terminal
        do {
            let result = try await Task.detached(priority: .userInitiated) {
                try AgentSpawner.spawn(prompt, terminal: preferred)
            }.value
            let retry = attemptNumber > 1 ? " · retry \(attemptNumber)" : ""
            track(kind: "review", label: "Auto · Review · #\(s.number)\(retry)",
                  prURL: s.url, result: result, source: "auto")
            if attemptNumber == 1 { autofixReviewsHandled += 1 }
            return true
        } catch {
            AuditLog.log("auto", "spawn-failed",
                         "Review #\(s.number) failed to spawn: \((error as? LocalizedError)?.errorDescription ?? "\(error)")")
            refreshAudit()
            return false
        }
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
        guard !Headless.isRender else { return }
        if let data = try? JSONEncoder().encode(map) {
            UserDefaults.standard.set(data, forKey: Keys.myReviewAttempts)
        }
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
    /// conflicts) — the same single-PR conflict run the reconciler dispatches.
    func resolveConflicts(for number: Int) async {
        let (owner, repo) = coreRepo
        let url = "https://github.com/\(owner)/\(repo)/pull/\(number)"
        if await dispatchConflictFix(number: number, url: url, source: "panel") == false,
           !resolvingPRs.contains(number),
           !processes.contains(where: { $0.prURL == url && !$0.done }) {
            self.error = "Resolve #\(number) failed to spawn — see the activity log."
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
        guard !Headless.isRender else { return }
        let keyed = Dictionary(uniqueKeysWithValues: fps.map { (String($0.key), $0.value) })
        if let data = try? JSONEncoder().encode(keyed) {
            UserDefaults.standard.set(data, forKey: Keys.autofixFingerprints)
        }
    }

    // MARK: Claude API-error terminal watcher

    /// How often to scan terminals for a stalled agent. 20s by default; env-overridable.
    static var apiWatchInterval: TimeInterval {
        let secs = ProcessInfo.processInfo.environment["ARGENT_UTILS_APIWATCH_SECS"].flatMap(Double.init)
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
        for s in sessions where ApiErrorMatch.looksLikeApiError(s.tail) {
            erroring.insert(s.tty)
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
                "Continued a stalled agent (API error) on \(tty); next retry in ≥ \(Store.humanInterval(next))")
        }
        // Keep backoff ONLY for currently-erroring ttys: an on-screen session that
        // stopped erroring has recovered (reset to base), and a CLOSED session's entry
        // must not linger — macOS recycles tty numbers, so a stale 3h window would
        // suppress nudges to an unrelated new session on the same tty.
        apiErrorBackoff = apiErrorBackoff.filter { erroring.contains($0.key) }
    }

    private func startProcessPoll() {
        guard processPollTask == nil else { return }
        processPollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refreshProcessStatuses()
                await self?.refreshDeviceState()
                self?.refreshBanList()
                self?.refreshAudit()
                let ns = UInt64(Store.processPollInterval * 1_000_000_000)
                try? await Task.sleep(nanoseconds: ns)
            }
        }
    }

    /// Re-derive each session's `done` flag off the main thread (one `ps` call), drop
    /// any whose terminal window/tab was closed, then merge the rest back by id so a
    /// concurrent add/remove isn't clobbered.
    func refreshProcessStatuses() async {
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
