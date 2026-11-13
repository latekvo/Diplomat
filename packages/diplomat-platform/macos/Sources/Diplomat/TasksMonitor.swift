import Foundation
import DiplomatCore

// MARK: - Tasks: the tracker's sync loop and edits, on the macOS side
//
// The core owns the files, the GitHub mirror and the status (`TaskStore`, `TaskSync`); this
// side runs a sync on a timer and reads the rows back, every file and `gh` touch off the
// main actor.

extension Store {
    /// `DIPLOMAT_TASKS_SECS` (ignored unless finite), else `tasks.json`'s `syncSecs`. Never
    /// under `minSyncSecs`, nor over a day (a huge value would overflow the nanosecond sleep).
    static var tasksSyncInterval: TimeInterval {
        let model = try? CoreAssets.tasks()
        let floor = Double(model?.minSyncSecs ?? 60)
        let secs = ProcessInfo.processInfo.environment["DIPLOMAT_TASKS_SECS"]
            .flatMap(Double.init).flatMap { $0.isFinite ? $0 : nil }
        return min(max(floor, secs ?? Double(model?.syncSecs ?? 300)), commsMaxPollInterval)
    }

    /// The header badge.
    var blockedTaskCount: Int {
        taskViews.filter { $0.task.archivedAt == nil && $0.status == .blocked }.count
    }

    var activeTaskCount: Int { taskViews.filter { $0.task.archivedAt == nil }.count }

    /// Re-reads every task, archived ones included, and labels their links off the main
    /// actor: a label is a regex match or two, too many for every redraw.
    func reloadTasks() async {
        tasksReloadGeneration += 1
        let generation = tasksReloadGeneration
        let dir = TaskStore.dir
        let me = effectiveMe
        let (read, mark) = await Task.detached(priority: .utility) {
            () -> (Result<(views: [TaskView], labels: [String: String]), Error>, TaskGrouper.Mark?) in
            (Result {
                let views = try TaskStore.views(archived: true, me: me, in: dir)
                return (views, TaskTint.labels(of: views))
            }, TaskGrouper.mark(in: dir))
        }.value
        guard generation == tasksReloadGeneration else { return }
        if mark != tasksGrouperMark { tasksGrouperMark = mark }
        switch read {
        case .success(let read):
            tasksReadError = nil
            if read.views != taskViews {
                taskLabels = read.labels
                taskViews = read.views
            }
        case .failure(let error):
            tasksReadError = error.localizedDescription
        }
    }

    /// One `TaskSync.run`, then a reload. Skipped, sync and all, while no task is
    /// unarchived; asked from disk, so a task an agent made through the CLI counts.
    ///
    /// One asked for while another runs follows it, as that one may have read the tasks
    /// before the edit that asked.
    func syncTasks() async {
        guard !tasksSyncing else {
            tasksSyncAgain = true
            return
        }
        tasksSyncing = true
        defer { tasksSyncing = false }
        repeat {
            tasksSyncAgain = false
            await syncTasksOnce()
        } while tasksSyncAgain
    }

    private func syncTasksOnce() async {
        let dir = TaskStore.dir
        let commsDir = CommsLog.dir
        let result = await Task.detached(priority: .utility) { () -> TaskSync.Result? in
            guard let tasks = try? TaskStore.tasks(in: dir),
                  tasks.contains(where: { $0.archivedAt == nil }) else { return nil }
            return await TaskSync.run(dir: dir, commsDir: commsDir)
        }.value
        if let result {
            tasksLastSync = Date()
            tasksSyncError = result.error
        }
        await reloadTasks()
    }

    /// Applies one operator edit; why the core refused it, or nil once applied. `sync`
    /// starts a sync after a good one, so a new link's state shows without waiting for the
    /// timer.
    @discardableResult
    func editTask(_ edit: TaskEdit, sync: Bool = false) async -> String? {
        let dir = TaskStore.dir
        // Read first: a dismissed goal leaves the list.
        let withdrawn = goalAsks(stoppedBy: edit)
        let refusal = await Task.detached(priority: .userInitiated) { () -> String? in
            do {
                try TaskStore.edit(edit, in: dir)
                return nil
            } catch let error as TaskError where error.saved != nil {
                // Applied: refusing it would have the operator repeat it.
                AppLog.log("tasks", error.localizedDescription)
                return nil
            } catch {
                return error.localizedDescription
            }
        }.value
        guard refusal == nil else { return refusal }
        // Not left to the sync, whose read may come before the timer's next turn.
        await reloadTasks()
        await withdrawGoalAsks(withdrawn)
        if sync { Task { [weak self] in await self?.syncTasks() } }
        return nil
    }

    /// Never stopped: a tick with no unarchived task syncs nothing, and looks for goals
    /// only once `TaskGrouper.due()`.
    func startTasksMonitor() {
        guard tasksMonitorTask == nil, !Headless.active else { return }
        tasksMonitorTask = Task { [weak self] in
            // The badge, before the first sync's GitHub round trip.
            await self?.reloadTasks()
            while !Task.isCancelled {
                await AppLog.timed("tasksSync") { await self?.syncTasks() }
                let dir = TaskStore.dir
                if await Task.detached(priority: .utility, operation: { TaskGrouper.due(in: dir) }).value {
                    await AppLog.timed("tasksGroup") { await self?.findGoals() }
                }
                let ns = UInt64(Store.tasksSyncInterval * 1_000_000_000)
                try? await Task.sleep(nanoseconds: ns)
            }
        }
    }
}

// MARK: - Goals: the grouper, the agent overlay and the automatic pass

/// What the core cannot see about a goal's items: which have an agent on them and which
/// wait in the queue, by number in the watched repo.
struct GoalOverlay: Equatable {
    var agentPRs: Set<Int> = []
    var agentIssues: Set<Int> = []
    var queuedPRs: Set<Int> = []
    var queuedIssues: Set<Int> = []

    enum Badge: Equatable { case agent, queued }

    /// A PR pill's badge, or an `add` pill's (its issue's); an issue pill shows none, its
    /// fix being drawn on the `add` pill after it.
    func badge(_ node: TaskGraph.Node, watched: (owner: String, repo: String)) -> Badge? {
        guard let ref = GitHubRef(url: node.url), ref.owner.lowercased() == watched.owner.lowercased(),
              ref.repo.lowercased() == watched.repo.lowercased() else { return nil }
        let agents: Set<Int>, queued: Set<Int>
        switch node.kind {
        case "pr":
            agents = agentPRs
            queued = queuedPRs
        case "add":
            agents = agentIssues
            queued = queuedIssues
        default:
            return nil
        }
        if agents.contains(ref.number) { return .agent }
        return queued.contains(ref.number) ? .queued : nil
    }
}

extension Store {
    /// The agents and queued work behind `GoalOverlay`, `watched`'s alone (the goals are
    /// its):
    /// - a PR has an agent while a run on it is in a blocking state (`AgentState.inFlight`'s
    ///   rule); an issue while an issue fix run's link names it (its label, `IssueWizard`'s
    ///   "Issues · #N · …" or "Issues · repo#N · …", when it has no link), or its goal fix
    ///   is spawning
    /// - queued: every queued or starting task about a PR, every `issues:` one, and every
    ///   asked-for review or fix
    static func goalOverlay(rows: [AgentRow], queued: [QueuedAgentTask], requested: [RequestedWork],
                            startingFixes: Set<Int>, watched: PRRef.LocalRepo) -> GoalOverlay {
        func number(_ item: String?) -> Int? {
            guard let split = item.flatMap(PRRef.splitItemKey), split.root == watched.key else { return nil }
            return split.number
        }
        var out = GoalOverlay(agentIssues: startingFixes)
        for row in rows where !row.record.released && AgentState.blocking.contains(row.state) {
            if row.record.prNumber != nil {
                if let n = number(row.record.item) { out.agentPRs.insert(n) }
            } else if row.record.kind == AgentTaskQueue.issuesAction {
                let n = row.record.prURL.isEmpty
                    ? issueNumber(inLabel: row.record.label, repo: watched.repo)
                    : number(PRRef.itemKey(url: row.record.prURL))
                if let n { out.agentIssues.insert(n) }
            }
        }
        let issueKey = AgentTaskQueue.issuesAction + ":"
        for task in queued {
            if task.job.prNumber != nil {
                if let n = number(task.job.item) { out.queuedPRs.insert(n) }
            } else if task.id.hasPrefix(issueKey), let n = number(String(task.id.dropFirst(issueKey.count))) {
                out.queuedIssues.insert(n)
            }
        }
        for ask in requested {
            guard let n = number(ask.item) else { continue }
            switch ask.config {
            case .review: out.queuedPRs.insert(n)
            case .issues: out.queuedIssues.insert(n)
            case .investigate: break
            }
        }
        return out
    }

    /// N of the first "Issues · #N · " in `label`, or of "Issues · <repo>#N · ", the form
    /// a label takes while several roots are loaded.
    static func issueNumber(inLabel label: String, repo: String) -> Int? {
        guard let start = label.range(of: "Issues · ") else { return nil }
        var rest = label[start.upperBound...]
        if rest.lowercased().hasPrefix(repo.lowercased() + "#") { rest = rest.dropFirst(repo.count) }
        guard rest.hasPrefix("#") else { return nil }
        rest = rest.dropFirst()
        let digits = rest.prefix(while: \.isNumber)
        guard !digits.isEmpty, rest.dropFirst(digits.count).hasPrefix(" · ") else { return nil }
        return Int(digits)
    }

    var goalOverlay: GoalOverlay {
        let (owner, repo) = CoreAssets.repoCoordinates()
        return Store.goalOverlay(rows: agentRows, queued: queuedTasks + startingTasks, requested: requestedWork,
                                 startingFixes: goalFixesStarting,
                                 watched: PRRef.LocalRepo(owner: owner, repo: repo, path: ""))
    }

    /// The operator's PRs, as item keys, whose review threads an `auto` goal wants fixed
    /// (`findings`).
    var goalFindingItems: Set<String> {
        Set(taskViews.flatMap(\.work).filter { $0.kind == .findings }.compactMap { PRRef.itemKey(url: $0.url) })
    }

    /// The automatic pass's fix of a goal issue: feature requests included, since the goal
    /// it serves is often one, and, as a sweep's, re-checked when its turn comes: one closed
    /// or assigned by then is left alone.
    func goalFixAsk(number: Int, url: String, author: String?) -> RequestedWork {
        var cfg = IssueConfig(target: .all, me: effectiveMe, includeFeatures: true)
        cfg.home = root(ofItem: PRRef.itemKey(url: url))
        return RequestedWork(number: number, url: url, author: author ?? "",
                             config: .issues(cfg.forIssue(number)))
    }

    /// The `+` press's fix: named, as the Fix-issues wizard's press names it, since the
    /// operator asked for this one.
    func goalPlusAsk(number: Int, url: String, author: String?) -> RequestedWork {
        var cfg = IssueConfig(target: .specific, me: effectiveMe, specificIssue: String(number),
                              includeFeatures: true)
        cfg.home = root(ofItem: PRRef.itemKey(url: url))
        return RequestedWork(number: number, url: url, author: author ?? "", config: .issues(cfg))
    }

    /// The goal and its `dispatched` fixes and reviews by queue key, when `edit` stops it
    /// acting (a mode other than auto, a pin, an archive).
    func goalAsks(stoppedBy edit: TaskEdit) -> (task: String, asks: [String: String])? {
        let id: String
        switch edit {
        case let .mode(task, mode?, _) where mode != TaskMode.auto.rawValue: id = task
        case let .update(task, _, _, pin?) where !pin.isEmpty: id = task
        case let .archive(task): id = task
        default: return nil
        }
        guard let task = taskViews.first(where: { $0.task.id == id })?.task else { return nil }
        var asks: [String: String] = [:]
        for key in task.dispatched.keys {
            guard let work = GoalWork.parse(key) else { continue }
            let verb = work.kind == GoalWork.Kind.fixIssue.rawValue ? AgentTaskQueue.issuesAction
                                                                     : AgentTaskQueue.reviewAction
            asks[key] = AgentTaskQueue.key(auditAction: verb, item: work.ref.key)
        }
        return asks.isEmpty ? nil : (id, asks)
    }

    /// Cancels the asks of `goal` not started yet, and has the goal forget them, so they
    /// are asked for again should it go back to auto.
    func withdrawGoalAsks(_ goal: (task: String, asks: [String: String])?) async {
        guard let goal else { return }
        let waiting = goal.asks.filter { _, queueKey in
            queuedTasks.contains { $0.id == queueKey && $0.job.requested }
        }
        guard !waiting.isEmpty else { return }
        waiting.values.forEach(cancelRequestedWork)
        if let refusal = await editTask(.withdrawn(id: goal.task, work: waiting.keys.sorted())) {
            AppLog.log("tasks", "goal \(goal.task): could not forget \(waiting.keys.sorted().joined(separator: " ")): \(refusal)")
        }
    }

    /// A goal PR's review at the review-request monitor's settings (the most thorough
    /// depth), comments only: no request's files are at hand to weigh a verdict against.
    func goalReviewAsk(_ item: GoalWork) -> RequestedWork {
        let mine = !effectiveMe.isEmpty && item.author?.lowercased() == effectiveMe.lowercased()
        var cfg = ReviewConfig(depth: "max", target: .specific, me: effectiveMe, markReady: false,
                               leaveReviews: true, replyToReviews: false, specificPR: String(item.number),
                               finalPass: false, softApprove: softApproveEnabled,
                               specificAuthor: mine ? .mine : .theirs)
        cfg.home = root(ofItem: PRRef.itemKey(url: item.url))
        return RequestedWork(number: item.number, url: item.url, author: mine ? "" : item.author ?? "",
                             config: .review(cfg))
    }

    /// Every `auto` goal's `work`: a fix and a review are asked for through the queue
    /// (`requestOne`, so the cap, the ban list and the queue switch apply), then recorded
    /// as `dispatched`, also when already asked for; a fix of an issue an agent is on is
    /// recorded without asking. `findings` needs nothing here: `isPaused` lets the my-PR
    /// review reconcile run for `goalFindingItems`. An item in no repo root is skipped,
    /// and the whole pass while the GitHub login is unknown: a review ask would take my
    /// own PR for someone else's.
    func runGoalPass() async {
        guard !effectiveMe.isEmpty else { return }
        let overlay = goalOverlay
        for view in taskViews where !view.work.isEmpty {
            var started: [String] = []
            for item in view.work where root(ofItem: PRRef.itemKey(url: item.url)) != nil {
                switch item.kind {
                case .fixIssue:
                    if !overlay.agentIssues.contains(item.number) {
                        requestOne(goalFixAsk(number: item.number, url: item.url, author: item.author))
                    }
                    started.append(item.key)
                case .review:
                    requestOne(goalReviewAsk(item))
                    started.append(item.key)
                case .findings:
                    break
                }
            }
            guard !started.isEmpty else { continue }
            if let refusal = await editTask(.dispatched(id: view.task.id, work: started)) {
                AppLog.log("tasks", "goal \(view.task.id): could not record \(started.joined(separator: " ")): \(refusal)")
            }
        }
    }

    /// The `+` pill: a fix agent for its issue now, outside the cap like the Fix-issues
    /// wizard's single-issue press, recorded as `dispatched` once started. Nil once
    /// started (or already on its way), else why not. An issue in no repo root gets none,
    /// as the automatic pass skips one.
    func startGoalFix(_ node: TaskGraph.Node, task id: String) async -> String? {
        guard let ref = GitHubRef(url: node.url) else { return "not a GitHub issue: \(node.url)" }
        let item = PRRef.itemKey(url: node.url)
        guard root(ofItem: item) != nil else {
            let root = item.flatMap(PRRef.splitItemKey)?.root ?? node.url
            return "\(root) is not a repo root, so no fix was started"
        }
        guard goalOverlay.badge(node, watched: CoreAssets.repoCoordinates()) == nil else { return nil }
        goalFixesStarting.insert(ref.number)
        defer { goalFixesStarting.remove(ref.number) }
        let outcome = await dispatchAgent(goalPlusJob(node, number: ref.number), source: .panel)
        switch outcome {
        case .spawned, .standDown: break
        case .banned: return "@\(node.author ?? "") is banned, so no fix was started"
        case .failed(let why): return "the fix agent did not start: \(why)"
        default: return "the fix agent did not start (\(outcome))"
        }
        if let work = node.work, let refusal = await editTask(.dispatched(id: id, work: [work])) {
            AppLog.log("tasks", "goal \(id): could not record \(work): \(refusal)")
        }
        return nil
    }

    /// The `+` press's agent, in its issue's root checkout.
    func goalPlusJob(_ node: TaskGraph.Node, number: Int) -> AgentJob {
        let ask = goalPlusAsk(number: number, url: node.url, author: node.author)
        let action = ask.config.action
        return AgentJob(kind: action, auditAction: action, label: ask.label(roots: roots.count),
                        prompt: ask.config.buildPrompt(), prURL: ask.issueLink(in: roots), prNumber: nil,
                        authorLogin: node.author, duty: action, workKey: "", counter: nil,
                        directory: ask.config.checkout)
    }

    /// One mode edit, then the automatic pass when the goal wants work started, without
    /// waiting for the next poll.
    func setGoalMode(_ id: String, mode: TaskMode?, reviewOnPush: Bool? = nil) async -> String? {
        if let refusal = await editTask(.mode(id: id, mode: mode?.rawValue, reviewOnPush: reviewOnPush)) {
            return refusal
        }
        if taskViews.contains(where: { $0.task.id == id && !$0.work.isEmpty }) { await runGoalPass() }
        return nil
    }

    /// One `TaskGrouper.run` off the main actor, never two at once; a run that proposed or
    /// added anything is synced, so its items' states show.
    func findGoals() async {
        guard !tasksGrouping else { return }
        tasksGrouping = true
        defer { tasksGrouping = false }
        let dir = TaskStore.dir
        let result = await Task.detached(priority: .utility) { await TaskGrouper.run(dir: dir) }.value
        if let error = result.error { AppLog.log("tasks", "finding goals failed: \(error)") }
        if result.created + result.added > 0 {
            await syncTasks()
        } else {
            await reloadTasks()
        }
    }
}
