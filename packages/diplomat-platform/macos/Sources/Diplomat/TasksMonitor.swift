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
    /// The agents and queued work behind `GoalOverlay`:
    /// - a PR has an agent while a run on it is in a blocking state (`AgentState.inFlight`'s
    ///   rule); an issue while an issue fix run's label (`RequestedWork.label`,
    ///   `IssueWizard`: "Issues · #N · …") names it, or its goal fix is spawning
    /// - queued: every queued or starting task about a PR, every `issues:N` one, and every
    ///   asked-for review or fix
    static func goalOverlay(rows: [AgentRow], queued: [QueuedAgentTask], requested: [RequestedWork],
                            startingFixes: Set<Int>) -> GoalOverlay {
        var out = GoalOverlay(agentIssues: startingFixes)
        for row in rows where !row.record.released && AgentState.blocking.contains(row.state) {
            if let n = row.record.prNumber {
                out.agentPRs.insert(n)
            } else if row.record.kind == AgentTaskQueue.issuesAction,
                      let n = issueNumber(inLabel: row.record.label) {
                out.agentIssues.insert(n)
            }
        }
        let issueKey = AgentTaskQueue.issuesAction + ":"
        for task in queued {
            if let n = task.job.prNumber {
                out.queuedPRs.insert(n)
            } else if task.id.hasPrefix(issueKey), let n = Int(task.id.dropFirst(issueKey.count)) {
                out.queuedIssues.insert(n)
            }
        }
        for ask in requested {
            switch ask.config {
            case .review: out.queuedPRs.insert(ask.number)
            case .issues: out.queuedIssues.insert(ask.number)
            case .investigate: break
            }
        }
        return out
    }

    /// N of the first "Issues · #N · " in `label`.
    static func issueNumber(inLabel label: String) -> Int? {
        guard let start = label.range(of: "Issues · #") else { return nil }
        let rest = label[start.upperBound...]
        let digits = rest.prefix(while: \.isNumber)
        guard !digits.isEmpty, rest.dropFirst(digits.count).hasPrefix(" · ") else { return nil }
        return Int(digits)
    }

    var goalOverlay: GoalOverlay {
        Store.goalOverlay(rows: agentRows, queued: queuedTasks + startingTasks, requested: requestedWork,
                          startingFixes: goalFixesStarting)
    }

    /// The operator's PRs whose review threads an `auto` goal wants fixed (`findings`).
    var goalFindingPRs: Set<Int> {
        Set(taskViews.flatMap(\.work).filter { $0.kind == .findings }.map(\.number))
    }

    /// A goal issue's fix: that issue, named, feature requests included, since the goal it
    /// serves is often one.
    func goalFixAsk(number: Int, url: String, author: String?) -> RequestedWork {
        RequestedWork(number: number, url: url, author: author ?? "",
                      config: .issues(IssueConfig(target: .specific, me: effectiveMe,
                                                  specificIssue: String(number), includeFeatures: true)))
    }

    /// A goal PR's review at the review-request monitor's settings (the most thorough
    /// depth), comments only: no request's files are at hand to weigh a verdict against.
    func goalReviewAsk(_ item: GoalWork) -> RequestedWork {
        let mine = !effectiveMe.isEmpty && item.author?.lowercased() == effectiveMe.lowercased()
        let cfg = ReviewConfig(depth: "max", target: .specific, me: effectiveMe, markReady: false,
                               leaveReviews: true, replyToReviews: false, specificPR: String(item.number),
                               finalPass: false, softApprove: softApproveEnabled,
                               specificAuthor: mine ? .mine : .theirs)
        return RequestedWork(number: item.number, url: item.url, author: mine ? "" : item.author ?? "",
                             config: .review(cfg))
    }

    /// Every `auto` goal's `work`: a fix and a review are asked for through the queue
    /// (`requestOne`, so the cap, the ban list and the queue switch apply), then recorded
    /// as `dispatched`, also when already asked for; a fix of an issue an agent is on is
    /// recorded without asking. `findings` needs nothing here: `isPaused` lets the my-PR
    /// review reconcile run for `goalFindingPRs`.
    func runGoalPass() async {
        let overlay = goalOverlay
        for view in taskViews where !view.work.isEmpty {
            var started: [String] = []
            for item in view.work {
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
    /// started (or already on its way), else why not.
    func startGoalFix(_ node: TaskGraph.Node, task id: String) async -> String? {
        guard let ref = GitHubRef(url: node.url) else { return "not a GitHub issue: \(node.url)" }
        guard goalOverlay.badge(node, watched: CoreAssets.repoCoordinates()) == nil else { return nil }
        goalFixesStarting.insert(ref.number)
        defer { goalFixesStarting.remove(ref.number) }
        let ask = goalFixAsk(number: ref.number, url: node.url, author: node.author)
        let action = ask.config.action
        let job = AgentJob(kind: action, auditAction: action, label: ask.label,
                           prompt: ask.config.buildPrompt(), prURL: nil, prNumber: nil,
                           authorLogin: node.author, duty: action, workKey: "", counter: nil)
        let outcome = await dispatchAgent(job, source: .panel)
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
