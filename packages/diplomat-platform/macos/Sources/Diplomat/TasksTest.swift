import Foundation
import DiplomatCore

/// `DIPLOMAT_TASKS_TEST=1`: the app's half of the Tasks tracker (the core's is pinned in
/// `DiplomatCoreSmoke`): an edit through `Store.editTask`, a sync through `Store.syncTasks`
/// and the rows, badge and errors the screen draws after each, against a stand-in `gh`
/// (`DIPLOMAT_GH`) that logs its calls and answers one canned GraphQL reply. That a sync
/// with no unarchived task calls nothing is read off that log.
///
/// The tasks and Comms directories point at scratch; no real `gh` runs.
enum TasksTest {
    @MainActor
    static func run() async -> Bool {
        var pass = true
        func check(_ name: String, _ ok: Bool, _ detail: @autoclosure () -> String = "") {
            print("\(ok ? "PASS" : "FAIL") — \(name)\(ok ? "" : " \(detail())")")
            if !ok { pass = false }
        }

        let fm = FileManager.default
        let scratch = fm.temporaryDirectory.appendingPathComponent("diplomat-taskstest-\(UUID().uuidString)")
        defer { try? fm.removeItem(at: scratch) }
        try? fm.createDirectory(at: scratch, withIntermediateDirectories: true)
        setenv("DIPLOMAT_TASKS_DIR", scratch.appendingPathComponent("tasks").path, 1)
        setenv("DIPLOMAT_COMMS_DIR", scratch.appendingPathComponent("comms").path, 1)

        let (owner, repo) = CoreAssets.repoCoordinates()
        let pr = "https://github.com/\(owner)/\(repo)/pull/1714"
        let log = scratch.appendingPathComponent("gh.log")
        let failing = scratch.appendingPathComponent("gh.fail")
        let reply = scratch.appendingPathComponent("gh.json")
        let gh = scratch.appendingPathComponent("gh")
        let node: [String: Any] = [
            "__typename": "PullRequest", "title": "feat: physical iPhone over USB", "url": pr,
            "state": "OPEN", "isDraft": false, "mergeable": "CONFLICTING",
            "commits": ["nodes": [["commit": ["statusCheckRollup": ["state": "SUCCESS"]]]]],
            "comments": ["nodes": []], "reviews": ["nodes": []],
        ]
        do {
            try JSONSerialization.data(withJSONObject: ["data": ["i0": ["issueOrPullRequest": node]]])
                .write(to: reply)
            try """
            #!/bin/sh
            echo "$1 $2" >> '\(log.path)'
            if [ -f '\(failing.path)' ]; then echo 'HTTP 502: Bad Gateway' >&2; exit 1; fi
            cat '\(reply.path)'
            """.write(to: gh, atomically: true, encoding: .utf8)
            try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: gh.path)
        } catch {
            print("FAIL — stand-in gh: \(error)")
            return false
        }
        setenv("DIPLOMAT_GH", gh.path, 1)
        func calls() -> Int {
            ((try? String(contentsOf: log, encoding: .utf8)) ?? "").split(separator: "\n").count
        }

        let store = Store()

        print("sync interval")
        for (raw, want) in [("5", 60.0), ("600", 600.0), ("inf", 300.0), ("nope", 300.0)] {
            setenv("DIPLOMAT_TASKS_SECS", raw, 1)
            check("DIPLOMAT_TASKS_SECS=\(raw) -> \(Int(want))s", Store.tasksSyncInterval == want,
                  "got \(Store.tasksSyncInterval)")
        }
        unsetenv("DIPLOMAT_TASKS_SECS")

        print("no task")
        await store.syncTasks()
        check("a sync with no task calls no gh", calls() == 0, "\(calls()) calls")
        check("and records no sync", store.tasksLastSync == nil && store.tasksSyncError == nil)

        print("create")
        let made = await store.editTask(.create(title: "Physical iPhone support",
                                                tags: ["Area:Devices", "kind:feature"], links: ["#1714"]))
        check("the edit is taken", made && store.tasksEditError == nil, store.tasksEditError ?? "")
        let first = store.taskViews.first
        check("its row is read back", store.taskViews.count == 1 && first?.task.title == "Physical iPhone support")
        check("tags are the core's normalized ones", first?.task.tags == ["area:devices", "kind:feature"],
              "\(first?.task.tags ?? [])")
        check("unsynced until a sync", first?.status == .inProgress && first?.reasons == ["#1714:unsynced"],
              "\(first?.status.rawValue ?? "-") \(first?.reasons ?? [])")
        let id = first?.task.id ?? ""

        print("sync")
        await store.syncTasks()
        let synced = store.taskViews.first
        check("one gh call", calls() == 1, "\(calls()) calls")
        check("the sync is recorded", store.tasksLastSync != nil && store.tasksSyncError == nil,
              store.tasksSyncError ?? "")
        check("the rows are re-read: blocked on the conflict",
              synced?.status == .blocked && synced?.reasons == ["#1714:conflicts"],
              "\(synced?.status.rawValue ?? "-") \(synced?.reasons ?? [])")
        check("the link is GitHub's PR now", synced?.task.links.first?.url == pr
                && synced?.task.links.first?.kind == "pr", "\(synced?.task.links ?? [])")
        check("history has the first sight", synced?.history.first?.tags == ["linked", "pr:open"],
              "\(synced?.history.first?.tags ?? [])")
        check("the badge counts it", store.blockedTaskCount == 1)

        print("refusals")
        let bad = await store.editTask(.link(id: id, url: "argent 1714"))
        check("a bad link is refused, and says why",
              !bad && store.tasksEditError?.contains("not a link") == true, store.tasksEditError ?? "nil")
        let good = await store.editTask(.note(id: id, text: "USB only for now", tags: ["decision"], url: nil))
        check("the next good edit clears it", good && store.tasksEditError == nil)
        check("and the note is history", store.taskViews.first?.history.first?.source == "note")

        print("gh failing")
        fm.createFile(atPath: failing.path, contents: Data())
        let before = store.taskViews
        let lastSync = store.tasksLastSync
        await store.syncTasks()
        check("the error is shown", store.tasksSyncError?.contains("502") == true, store.tasksSyncError ?? "nil")
        check("the last good sync stands", store.tasksLastSync == lastSync)
        check("and the rows are unchanged", store.taskViews == before)
        try? fm.removeItem(at: failing)

        print("overlap")
        let n = calls()
        async let a: Void = store.syncTasks()
        async let b: Void = store.syncTasks()
        _ = await (a, b)
        check("a sync asked for during another runs after it", calls() == n + 2, "\(calls() - n) calls")
        check("and clears the error", store.tasksSyncError == nil, store.tasksSyncError ?? "")

        print("a link edit syncs")
        let m = calls()
        await store.editTask(.link(id: id, url: "https://swmansion.slack.com/archives/C01/p1793373966186819"),
                             sync: true)
        for _ in 0..<100 where calls() == m || store.tasksSyncing {
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        check("it syncs without waiting for the timer", calls() == m + 1, "\(calls() - m) calls")
        check("the slack link is drawn", store.taskViews.first?.task.links.map(\.kind) == ["pr", "slack"])

        print("archive")
        await store.editTask(.archive(id: id))
        let k = calls()
        await store.syncTasks()
        check("an archived-only tracker calls no gh", calls() == k, "\(calls() - k) calls")
        check("the archived task is still read", store.taskViews.first?.task.archivedAt != nil)
        check("and counts toward neither the badge nor the active tasks",
              store.blockedTaskCount == 0 && store.activeTaskCount == 0)

        print(pass ? "tasks: all passed" : "tasks: FAILED")
        return pass
    }
}
