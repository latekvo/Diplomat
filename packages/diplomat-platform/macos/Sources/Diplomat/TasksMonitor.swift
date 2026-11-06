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
        let read = await Task.detached(priority: .utility) {
            () -> Result<(views: [TaskView], labels: [String: String]), Error> in
            Result {
                let views = try TaskStore.views(archived: true, in: dir)
                return (views, TaskTint.labels(of: views))
            }
        }.value
        guard generation == tasksReloadGeneration else { return }
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

    /// Never stopped: a tick with no unarchived task syncs nothing.
    func startTasksMonitor() {
        guard tasksMonitorTask == nil, !Headless.active else { return }
        tasksMonitorTask = Task { [weak self] in
            // The badge, before the first sync's GitHub round trip.
            await self?.reloadTasks()
            while !Task.isCancelled {
                await AppLog.timed("tasksSync") { await self?.syncTasks() }
                let ns = UInt64(Store.tasksSyncInterval * 1_000_000_000)
                try? await Task.sleep(nanoseconds: ns)
            }
        }
    }
}
