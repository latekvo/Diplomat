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

    @Published var usernameOverride: String {
        didSet { UserDefaults.standard.set(usernameOverride, forKey: Keys.usernameOverride) }
    }
    @Published var hiddenTools: Set<String> {
        didSet { UserDefaults.standard.set(Array(hiddenTools), forKey: Keys.hiddenTools) }
    }
    @Published var colorOverrides: [String: String] {
        didSet { UserDefaults.standard.set(colorOverrides, forKey: Keys.colorOverrides) }
    }
    @Published var terminalChoice: String {
        didSet { UserDefaults.standard.set(terminalChoice, forKey: Keys.terminalChoice) }
    }
    /// Whether the in-process PR auto-fix monitor is on. Persisted; when turned on we
    /// kick an immediate poll rather than waiting for the next tick.
    @Published var prAutofixEnabled: Bool {
        didSet {
            UserDefaults.standard.set(prAutofixEnabled, forKey: Keys.prAutofixEnabled)
            if prAutofixEnabled && !oldValue { Task { await runAutofixPollOnce() } }
        }
    }

    /// Whether to auto-dispatch a full-E2E review when someone requests my review on a
    /// PR (someone else's PR → review-only, leave comments). Persisted; kicks a poll on
    /// enable. Independent of `prAutofixEnabled`.
    @Published var reviewRequestsEnabled: Bool {
        didSet {
            UserDefaults.standard.set(reviewRequestsEnabled, forKey: Keys.reviewRequestsEnabled)
            if reviewRequestsEnabled && !oldValue { Task { await runAutofixPollOnce() } }
        }
    }

    /// Latest state from the auto-fix monitor's own poll (nil until the first). Drives
    /// the top-of-panel status pill; freshness (`isLive`) decides active vs. offline.
    @Published var autofixStatus: AutofixStatus?

    /// Authors banned for prompt injection (read from the daemon's banned.json). They
    /// receive no automated reviews, and appear in the "Banned" list above the sessions.
    @Published var bannedAuthors: [BannedAuthor] = []

    /// Whether the Claude-API-error terminal watcher is on: it nudges any agent that
    /// stalls on a transient server error to continue. Persisted; kicks a scan on enable.
    @Published var apiWatchEnabled: Bool {
        didSet {
            UserDefaults.standard.set(apiWatchEnabled, forKey: Keys.apiWatchEnabled)
            if apiWatchEnabled && !oldValue { Task { await runApiErrorScanOnce() } }
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
        static let reviewRequestedSeen = "reviewRequestedSeen"
        static let reviewRequestsHandled = "reviewRequestsHandled"
        static let apiWatchEnabled = "apiWatchEnabled"
        static let apiWatchContinues = "apiWatchContinues"
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
        apiWatchEnabled = defaults.object(forKey: Keys.apiWatchEnabled) as? Bool ?? true
        processes = Store.loadProcesses()
        if hiddenTools.contains(selected.rawValue),
           let first = ToolKind.allCases.first(where: { !hiddenTools.contains($0.rawValue) }) {
            selected = first
        }

        let env = ProcessInfo.processInfo.environment
        // Match the app's full headless set (ArgentUtilsApp) so self-test modes
        // (render, dumps, track-test) don't start polls or shell `node` for the
        // allocator status during a one-shot check.
        let headless = env["ARGENT_UTILS_DUMP"] == "1"
            || env["ARGENT_UTILS_LOOKUP"] != nil
            || env["ARGENT_UTILS_RENDER"] != nil
            || env["ARGENT_UTILS_PRINT_PROMPT"] != nil
            || env["ARGENT_UTILS_SETTINGS_DUMP"] == "1"
            || env["ARGENT_UTILS_TRACK_TEST"] == "1"
            || env["ARGENT_UTILS_DEVICE_DUMP"] == "1"
            || env["ARGENT_UTILS_AUTOFIX_POLL"] == "1"
            || env["ARGENT_UTILS_APIWATCH_SCAN"] == "1"
        if !headless {
            startAutoRefresh()
            startProcessPoll()
            startAutofixMonitor()
            startApiErrorWatcher()
            refreshBanList()
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
        _ = await Task.detached(priority: .userInitiated) { DeviceAllocator.killDevice(key: key) }.value
        await refreshDeviceState()
    }

    /// Shell the installer's `--check` (Node startup, ~100-300ms) off-main and
    /// publish the result. Called at startup, when Settings opens, and post-install.
    func refreshAllocatorInstall() async {
        allocatorInstall = await Task.detached(priority: .utility) { DeviceAllocator.check() }.value
    }

    func installAllocator() async {
        allocatorInstall = await Task.detached(priority: .utility) { DeviceAllocator.install() }.value
        await refreshDeviceState()
    }

    func uninstallAllocator() async {
        allocatorInstall = await Task.detached(priority: .utility) { DeviceAllocator.uninstall() }.value
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
    enum FocusOutcome { case focused, openedPR, lost }

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
        if ProcessInfo.processInfo.environment["ARGENT_UTILS_RENDER"] != nil { return }
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

    /// Register a freshly spawned agent session for tracking.
    func track(kind: String, label: String, prURL: String?, result: AgentSpawner.SpawnResult) {
        let p = TrackedProcess(kind: kind, label: label,
                               terminal: result.terminal.rawValue,
                               windowID: result.windowID, sessionID: result.sessionID,
                               tty: result.tty, donePath: result.donePath, prURL: prURL)
        processes.append(p)
    }

    /// Remove one tracked session from the list (the row's ✕ button).
    func removeProcess(_ id: UUID) {
        processes.removeAll { $0.id == id }
    }

    // MARK: PR auto-fix monitor

    /// How often the monitor polls GitHub. 3 min by default; override for testing.
    static var autofixPollInterval: TimeInterval {
        let secs = ProcessInfo.processInfo.environment["ARGENT_UTILS_AUTOFIX_SECS"].flatMap(Double.init)
        return max(30, secs ?? 3 * 60)
    }
    private var autofixMonitorTask: Task<Void, Never>?

    private func startAutofixMonitor() {
        guard autofixMonitorTask == nil else { return }
        autofixMonitorTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.runAutofixPollOnce()
                let ns = UInt64(Store.autofixPollInterval * 1_000_000_000)
                try? await Task.sleep(nanoseconds: ns)
            }
        }
    }

    /// One poll: fetch my open PRs, diff against saved fingerprints, and dispatch an
    /// agent for each PR that just gained a conflict or new review work. No-op when
    /// the feature is off or our login isn't known yet.
    func runAutofixPollOnce() async {
        if me.isEmpty { await fetchMe() }
        guard !effectiveMe.isEmpty else { return }
        let (owner, repo) = coreRepo
        if prAutofixEnabled { await pollMyPRs(owner: owner, repo: repo) }
        if reviewRequestsEnabled { await pollReviewRequests(owner: owner, repo: repo) }
    }

    /// My own PRs: dispatch on new conflicts / new review work (edge-triggered).
    private func pollMyPRs(owner: String, repo: String) async {
        let snaps: [PRSnapshot]
        do {
            snaps = try await AutofixMonitor.fetchSnapshots(owner: owner, repo: repo,
                                                            me: effectiveMe, role: .author)
        } catch {
            return   // transient gh/network error — leave state as-is, retry next tick
        }
        let (events, fingerprints) = AutofixDiff.compute(prior: loadAutofixFingerprints(), now: snaps)
        for event in events { await dispatchAutofix(event) }
        saveAutofixFingerprints(fingerprints)
        autofixStatus = AutofixStatus(
            updatedAt: Date(), watching: snaps.count,
            conflictsHandled: autofixConflictsHandled, reviewsHandled: autofixReviewsHandled)
    }

    /// PRs that request MY review: when one NEWLY enters the requested set, dispatch the
    /// most comprehensive review of it (someone else's PR → review-only, leave comments).
    /// First run seeds the set silently so outstanding requests aren't retro-swarmed.
    private func pollReviewRequests(owner: String, repo: String) async {
        let snaps: [PRSnapshot]
        do {
            snaps = try await AutofixMonitor.fetchSnapshots(owner: owner, repo: repo,
                                                            me: effectiveMe, role: .reviewRequested)
        } catch {
            return
        }
        let current = Set(snaps.map { $0.number })
        guard let prior = loadReviewRequestedSeen() else {
            saveReviewRequestedSeen(current)   // first run — baseline, no dispatch
            return
        }
        let banned = BanList.read()
        for snap in snaps where !prior.contains(snap.number) {
            // A prompt-injection-banned author gets no automated review from me.
            if BanList.isBanned(snap.author, in: banned) { continue }
            await dispatchReviewRequest(snap)
        }
        saveReviewRequestedSeen(current)   // seen (incl. banned) so it won't retro-fire
    }

    /// Re-read the prompt-injection ban list (cheap local file). Publishes on change.
    func refreshBanList() {
        let next = BanList.read()
        if next != bannedAuthors { bannedAuthors = next }
    }
    /// Remove a ban (the UI's un-ban button) and refresh.
    func unban(_ login: String) {
        BanList.unban(login)
        refreshBanList()
    }

    /// Spawn the most-comprehensive Review action (Full E2E ×2 + final verdict, leaving
    /// formal per-line comments) on a PR someone asked me to review — hands off the branch.
    private func dispatchReviewRequest(_ snap: PRSnapshot) async {
        if processes.contains(where: { $0.prURL == snap.url && !$0.done }) { return }
        // Most-comprehensive review (max depth = Full E2E ×2) leaving formal inline
        // comments, but NO auto-verdict — the final approve/changes-requested is the
        // user's (specificAuthor .theirs emits the neutral-COMMENT, no-verdict prompt).
        let prompt = ReviewConfig(depth: "max", target: .specific, me: effectiveMe,
                                  markReady: false, leaveReviews: true, replyToReviews: false,
                                  specificPR: String(snap.number),
                                  specificAuthor: .theirs).buildPrompt()
        let preferred = terminal
        do {
            let result = try await Task.detached(priority: .userInitiated) {
                try AgentSpawner.spawn(prompt, terminal: preferred)
            }.value
            track(kind: "review", label: "Auto · Review-req · #\(snap.number)",
                  prURL: snap.url, result: result)
            reviewRequestsHandled += 1
        } catch { }
    }

    var reviewRequestsHandled: Int {
        get { UserDefaults.standard.integer(forKey: Keys.reviewRequestsHandled) }
        set { UserDefaults.standard.set(newValue, forKey: Keys.reviewRequestsHandled) }
    }
    /// nil ⇒ never polled (first run); a set (possibly empty) ⇒ the last poll's requests.
    private func loadReviewRequestedSeen() -> Set<Int>? {
        guard let arr = UserDefaults.standard.array(forKey: Keys.reviewRequestedSeen) as? [Int] else {
            return nil
        }
        return Set(arr)
    }
    private func saveReviewRequestedSeen(_ set: Set<Int>) {
        UserDefaults.standard.set(Array(set), forKey: Keys.reviewRequestedSeen)
    }

    /// Spawn the appropriate action-button agent for a detected transition and track
    /// it, mirroring exactly what the Resolve-conflicts / Review wizards do (Deep depth,
    /// don't-mark-ready / no-formal-review / reply-"Fixed in <hash>").
    private func dispatchAutofix(_ event: AutofixEvent) async {
        let kind: String
        let snap: PRSnapshot
        let prompt: String
        let label: String
        switch event {
        case .conflict(let s):
            kind = "conflicts"; snap = s
            prompt = ConflictConfig(target: .specific, me: effectiveMe,
                                    specificPR: String(s.number)).buildPrompt()
            label = "Auto · Resolve · #\(s.number)"
        case .review(let s):
            kind = "review"; snap = s
            prompt = ReviewConfig(depth: "deep", target: .specific, me: effectiveMe,
                                  markReady: false, leaveReviews: false, replyToReviews: true,
                                  specificPR: String(s.number), specificAuthor: .mine).buildPrompt()
            label = "Auto · Review · #\(s.number)"
        }
        // Never pile a second agent on a PR that already has one running.
        if processes.contains(where: { $0.prURL == snap.url && !$0.done }) { return }
        let preferred = terminal
        do {
            let result = try await Task.detached(priority: .userInitiated) {
                try AgentSpawner.spawn(prompt, terminal: preferred)
            }.value
            track(kind: kind, label: label, prURL: snap.url, result: result)
            if case .conflict = event { autofixConflictsHandled += 1 } else { autofixReviewsHandled += 1 }
        } catch {
            // Spawn failed (e.g. terminal-automation permission not granted). Skip; the
            // fingerprint is still recorded, so it won't loop — the user can trigger the
            // action card manually.
        }
    }

    private var coreRepo: (owner: String, repo: String) {
        let cfg = try? CoreAssets.config()
        return (cfg?.owner ?? "software-mansion", cfg?.repo ?? "argent")
    }

    // Persisted so restarts don't re-dispatch, and the pill's counts survive.
    private var autofixConflictsHandled: Int {
        get { UserDefaults.standard.integer(forKey: Keys.autofixConflicts) }
        set { UserDefaults.standard.set(newValue, forKey: Keys.autofixConflicts) }
    }
    private var autofixReviewsHandled: Int {
        get { UserDefaults.standard.integer(forKey: Keys.autofixReviews) }
        set { UserDefaults.standard.set(newValue, forKey: Keys.autofixReviews) }
    }
    private func loadAutofixFingerprints() -> [Int: PRFingerprint] {
        guard let data = UserDefaults.standard.data(forKey: Keys.autofixFingerprints),
              let decoded = try? JSONDecoder().decode([String: PRFingerprint].self, from: data)
        else { return [:] }
        return Dictionary(uniqueKeysWithValues: decoded.compactMap { k, v in Int(k).map { ($0, v) } })
    }
    private func saveAutofixFingerprints(_ fps: [Int: PRFingerprint]) {
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
    /// Don't re-nudge the same tty within this window (avoids spamming while the error
    /// text is still on screen before the agent produces new output).
    static let apiWatchCooldown: TimeInterval = 120
    private var apiWatchTask: Task<Void, Never>?
    private var apiErrorCooldown: [String: Date] = [:]

    /// Count of nudges sent, for the Settings display.
    var apiWatchContinues: Int {
        get { UserDefaults.standard.integer(forKey: Keys.apiWatchContinues) }
        set { UserDefaults.standard.set(newValue, forKey: Keys.apiWatchContinues) }
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

    /// One scan: read every terminal's last visible lines and, for any showing a Claude
    /// API error (outside its cooldown), send the continue nudge to that exact session.
    func runApiErrorScanOnce() async {
        guard apiWatchEnabled else { return }
        let sessions = await Task.detached(priority: .utility) { ApiErrorWatcher.dumpSessions() }.value
        let now = Date()
        for s in sessions where ApiErrorMatch.looksLikeApiError(s.tail) {
            if let last = apiErrorCooldown[s.tty], now.timeIntervalSince(last) < Store.apiWatchCooldown {
                continue
            }
            apiErrorCooldown[s.tty] = now
            let tty = s.tty
            await Task.detached(priority: .userInitiated) { ApiErrorWatcher.sendContinue(tty: tty) }.value
            apiWatchContinues += 1
        }
    }

    private func startProcessPoll() {
        guard processPollTask == nil else { return }
        processPollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refreshProcessStatuses()
                await self?.refreshDeviceState()
                self?.refreshBanList()
                let ns = UInt64(Store.processPollInterval * 1_000_000_000)
                try? await Task.sleep(nanoseconds: ns)
            }
        }
    }

    /// Re-derive each session's `done` flag off the main thread (one `ps` call),
    /// then merge the flags back by id so a concurrent add/remove isn't clobbered.
    func refreshProcessStatuses() async {
        let snapshot = processes
        guard !snapshot.isEmpty else { return }
        let refreshed = await Task.detached(priority: .utility) {
            ProcessMonitor.refreshed(snapshot)
        }.value
        var doneByID: [UUID: Bool] = [:]
        for p in refreshed { doneByID[p.id] = p.done }
        var next = processes
        var changed = false
        for i in next.indices {
            if let d = doneByID[next[i].id], next[i].done != d {
                next[i].done = d
                changed = true
            }
        }
        if changed { processes = next }
    }

    /// Click a tracked row: focus its terminal window; if that's not possible open
    /// the PR; if there's no PR either, report that tracking is lost. The osascript
    /// focus runs off the main thread so the popover never hitches.
    func activate(_ p: TrackedProcess) async -> FocusOutcome {
        let focused = await Task.detached(priority: .userInitiated) {
            ProcessMonitor.focus(p)
        }.value
        if focused { return .focused }
        if let s = p.prURL, let u = URL(string: s) {
            NSWorkspace.shared.open(u)
            return .openedPR
        }
        return .lost
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
