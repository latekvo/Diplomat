import Foundation
import AppKit
import SwiftUI
import DiplomatCore

/// `DIPLOMAT_TASKS_TEST=1`: the app's half of the Tasks tracker (the core's is pinned in
/// `DiplomatCoreSmoke`): an edit through `Store.editTask` or an editor's `TaskDraft`, a sync
/// through `Store.syncTasks` and the rows, badge and errors the screen draws after each,
/// against a stand-in `gh` (`DIPLOMAT_GH`) that logs its calls, notes any call made while
/// another is still running, and answers one canned GraphQL reply. Then the screen itself,
/// clicked and typed into off screen: its inline editors across folds, archiving and
/// answers that come after the editor that asked has closed.
///
/// Then goals: the automatic pass's asks and records, what pauses for them, the agent
/// overlay, and a grouper run that fails.
///
/// The tasks and Comms directories point at scratch; no real `gh` or `claude` runs, and
/// no agent is spawned.
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
        let running = scratch.appendingPathComponent("gh.running")
        let overlaps = scratch.appendingPathComponent("gh.overlaps")
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
            mkdir '\(running.path)' 2>/dev/null || echo "$1 $2" >> '\(overlaps.path)'
            sleep 0.2
            rmdir '\(running.path)' 2>/dev/null
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
        check("the edit is taken", made == nil, made ?? "")
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
        check("its chip is labelled and dotted from the sync",
              store.taskLabels[pr] == "#1714"
                && TaskTint.linkState(synced?.linkStates[pr], kind: "pr")?.word == "open",
              "\(store.taskLabels[pr] ?? "-") \(synced?.linkStates ?? [:])")

        print("refusals, at the editor that made them")
        let link = TaskDraft(.link(task: id), seed: .init(text: "argent 1714"))
        let linked = await link.submit(to: store)
        check("a bad link is refused, and its editor says why",
              !linked && link.error?.contains("not a link") == true, link.error ?? "nil")
        check("and keeps what was typed", link.text == "argent 1714", link.text)
        let note = TaskDraft(.note(task: id))
        let empty = await note.submit(to: store)
        check("an empty note is refused at its own editor",
              !empty && note.error?.contains("a note needs") == true, note.error ?? "nil")
        check("leaving the link editor's error as it was", link.error?.contains("not a link") == true)
        note.text = "USB only for now"
        note.tags = "decision"
        let noted = await note.submit(to: store)
        check("a good save clears its error and its fields",
              noted && note.error == nil && note.text.isEmpty && note.tags.isEmpty,
              "\(note.error ?? "") \(note.text)")
        check("and the note is history", store.taskViews.first?.history.first?.source == "note")
        let missing = await store.editTask(.archive(id: "t-nope"))
        check("a row action's refusal is returned to it", missing?.contains("no task") == true, missing ?? "nil")
        let events = TaskStore.dir.appendingPathComponent("events.jsonl").path
        chmod(events, 0o400)
        let tagged = await store.editTask(.update(id: id, title: nil, tags: ["area:devices", "kind:feature", "half-logged"],
                                                  pinnedStatus: nil))
        chmod(events, 0o600)
        check("an edit saved without its history line is taken, not refused",
              tagged == nil && store.taskViews.first?.task.tags.contains("half-logged") == true, tagged ?? "")

        print("gh failing")
        fm.createFile(atPath: failing.path, contents: Data())
        let before = store.taskViews
        let lastSync = store.tasksLastSync ?? .distantFuture
        await store.syncTasks()
        check("the error is shown", store.tasksSyncError?.contains("502") == true, store.tasksSyncError ?? "nil")
        check("the last sync is when the failed one finished", (store.tasksLastSync ?? .distantPast) > lastSync,
              "\(String(describing: store.tasksLastSync)) after \(lastSync)")
        check("and the rows are unchanged", store.taskViews == before)
        try? fm.removeItem(at: failing)

        print("overlap")
        let n = calls()
        async let a: Void = store.syncTasks()
        async let b: Void = store.syncTasks()
        _ = await (a, b)
        check("a sync asked for during another runs after it", calls() == n + 2, "\(calls() - n) calls")
        let overlapping = (try? String(contentsOf: overlaps, encoding: .utf8)) ?? ""
        check("never beside it: no gh call starts while another runs", overlapping.isEmpty,
              "\(overlapping.split(separator: "\n").count) overlapping")
        check("and clears the error", store.tasksSyncError == nil, store.tasksSyncError ?? "")

        /// Waits out the sync an edit started.
        func settle(after count: Int) async {
            for _ in 0..<100 where calls() == count || store.tasksSyncing {
                try? await Task.sleep(nanoseconds: 50_000_000)
            }
        }

        print("a link edit syncs")
        let m = calls()
        link.text = "https://swmansion.slack.com/archives/C01/p1793373966186819"
        let relinked = await link.submit(to: store)
        await settle(after: m)
        check("the fixed link is taken, its error cleared",
              relinked && link.error == nil && link.text.isEmpty, link.error ?? link.text)
        check("it syncs without waiting for the timer", calls() == m + 1, "\(calls() - m) calls")
        check("the slack link is drawn", store.taskViews.first?.task.links.map(\.kind) == ["pr", "slack"])
        check("and labelled", store.taskLabels["https://swmansion.slack.com/archives/C01/p1793373966186819"] == "slack")

        print("archive")
        await store.editTask(.archive(id: id))
        let k = calls()
        let lastBefore = store.tasksLastSync
        await store.syncTasks()
        check("an archived-only tracker starts no sync", store.tasksLastSync == lastBefore && calls() == k,
              "\(calls() - k) calls, last sync \(String(describing: store.tasksLastSync))")
        check("the archived task is still read", store.taskViews.first?.task.archivedAt != nil)
        check("and counts toward neither the badge nor the active tasks",
              store.blockedTaskCount == 0 && store.activeTaskCount == 0)

        print("the new-task form")
        let form = TaskDraft(.create, seed: .init(text: " ", tags: "kind:chore", links: "#1714"))
        let untitled = await form.submit(to: store)
        check("an untitled task is refused at the form",
              !untitled && form.error?.contains("needs a title") == true, form.error ?? "nil")
        form.text = "Release 0.9 checklist"
        let j = calls()
        let created = await form.submit(to: store)
        await settle(after: j)
        check("a created task empties the form",
              created && form.error == nil && form.text.isEmpty && form.tags.isEmpty && form.links.isEmpty,
              "\(form.text)|\(form.tags)|\(form.links)")
        check("and syncs without waiting for the timer", calls() == j + 1, "\(calls() - j) calls")

        print("the screen's editors")
        await screen(store, check: check)

        print("goals")
        let goalsDir = scratch.appendingPathComponent("goals")
        setenv("DIPLOMAT_TASKS_DIR", goalsDir.path, 1)
        // The grouper's `claude`, were it ever reached.
        setenv("DIPLOMAT_CLAUDE", "/usr/bin/false", 1)
        let me = "octo-me"
        store.usernameOverride = ""
        store.me = me
        store.prAutofixEnabled = false
        let fixture: GoalFixture
        do {
            fixture = try writeGoalFixture(in: goalsDir, now: Date(), me: me)
        } catch {
            print("FAIL — goal fixture: \(error)")
            return false
        }
        await store.reloadTasks()
        func view(_ id: String) -> TaskView? { store.taskViews.first { $0.task.id == id } }
        func ref(_ n: Int) -> String { "\(owner)/\(repo)#\(n)" }
        func key(_ n: Int) -> String { ref(n).lowercased() }
        let labels = view(fixture.proposed)?.graph.map { $0.map(\.label) } ?? []
        check("the proposal's graph: closes, stacked and blocking PRs, and the issue no PR closes",
              labels == [["#1801", "#1810", "#1811"], ["#1802", "#1811"], ["#1803", "+"], ["#1812", "#1810", "#1811"]],
              "\(labels)")
        let wanted = view(fixture.auto)?.work.map { "\($0.kind.rawValue):\($0.number)" } ?? []
        check("the auto goal wants a fix, two reviews and the findings of the operator's PR",
              wanted == ["fix-issue:1820", "review:1821", "review:1822", "findings:1822"], "\(wanted)")
        check("a proposed and an observed goal want nothing",
              view(fixture.proposed)?.work == [] && view(fixture.observe)?.work == [])
        check("with PR auto-fix off, my-PR review fixes are paused",
              store.isPaused(.myReviews, item: key(1810)) && store.isPaused(.myReviews, item: nil))
        check("but not on a PR of an auto goal's findings, whose conflicts stay paused",
              !store.isPaused(.myReviews, item: key(1822)) && store.isPaused(.conflicts, item: key(1822)))

        print("the automatic pass")
        let asksBeforeNoRoot = store.requestedWork.count
        store.pinRoots([PRRef.LocalRepo(owner: "o", repo: "elsewhere", path: "/src/elsewhere")])
        await store.runGoalPass()
        store.pinRoots([PRRef.LocalRepo(owner: owner, repo: repo, path: RepoPaths.agentRepo)])
        check("with no root for the goal's repo, it asks for and records nothing",
              store.requestedWork.count == asksBeforeNoRoot && view(fixture.auto)?.task.dispatched.isEmpty == true,
              "\(store.requestedWork.map(\.key)) \(view(fixture.auto)?.task.dispatched.keys.sorted() ?? [])")
        let asksBefore = Set(store.requestedWork.map(\.key))
        store.goalFixesStarting = [1820]
        await store.runGoalPass()
        store.goalFixesStarting = []
        let asked = store.requestedWork.filter { !asksBefore.contains($0.key) }
        check("asks for exactly the two reviews, the fix's issue having an agent",
              asked.map(\.key) == ["review:\(key(1821))", "review:\(key(1822))"], "\(asked.map(\.key))")
        let fixAsk = store.goalFixAsk(number: 1820, url: "https://github.com/\(owner)/\(repo)/issues/1820",
                                       author: "tv-user")
        if case .issues(let cfg) = fixAsk.config {
            check("a fix is the one issue, features included, re-checked for closed and assigned when its turn comes",
                  cfg.target == .all && cfg.specificIssue == "1820" && cfg.includeFeatures && cfg.unassignedOnly
                    && !cfg.isSingleIssue && fixAsk.key == "issues:\(key(1820))",
                  "\(cfg.target) \(cfg.specificIssue) \(cfg.includeFeatures)")
        }
        if case .issues(let cfg) = store.goalPlusAsk(number: 1802, url: "", author: nil).config {
            check("the + names its issue, features included", cfg.target == .specific
                    && cfg.specificIssue == "1802" && cfg.includeFeatures && cfg.isSingleIssue,
                  "\(cfg.target) \(cfg.specificIssue) \(cfg.includeFeatures)")
        }
        let elsewhere = PRRef.LocalRepo(owner: "o", repo: "elsewhere", path: "/src/elsewhere")
        let configuredRoot = PRRef.LocalRepo(owner: owner, repo: repo, path: scratch.appendingPathComponent("configured").path)
        let plus = TaskGraph.Node(kind: "add", url: "https://github.com/\(owner)/\(repo)/issues/1899", label: "+",
                                  author: "plus-banned")
        store.pinRoots([elsewhere, configuredRoot])
        let plusJob = store.goalPlusJob(plus, number: 1899)
        check("the + works in its issue's root, not the primary's",
              plusJob.directory == configuredRoot.path, "\(plusJob.directory ?? "nil")")
        // Banned and in a scratch feed: were the root check missing, the dispatch would
        // stop at the ban and say so there, rather than open a terminal.
        let bans = scratch.appendingPathComponent("bans")
        try? fm.createDirectory(at: bans, withIntermediateDirectories: true)
        try? Data(#"{"banned":[{"login":"plus-banned","reason":"test"}]}"#.utf8)
            .write(to: bans.appendingPathComponent("banned.json"))
        setenv("DA_BAN_DIR", bans.path, 1)
        let feed = scratch.appendingPathComponent("audit")
        setenv("DIPLOMAT_AUDIT_DIR", feed.path, 1)
        store.pinRoots([elsewhere])
        let noRoot = await store.startGoalFix(plus, task: fixture.auto)
        let fed = (try? String(contentsOf: feed.appendingPathComponent("audit.jsonl"), encoding: .utf8)) ?? ""
        check("a + on an issue in no root starts nothing and says why",
              noRoot == "\(owner.lowercased())/\(repo.lowercased()) is not a repo root, so no fix was started"
                && fed.isEmpty, "\(noRoot ?? "nil") | \(fed)")
        store.pinRoots([PRRef.LocalRepo(owner: owner, repo: repo, path: RepoPaths.agentRepo)])
        let reviews = asked.compactMap { ask -> (ReviewConfig, String)? in
            if case .review(let cfg) = ask.config { return (cfg, ask.author) }
            return nil
        }
        check("the reviews are the review-request monitor's, mine reviewed as mine",
              reviews.map { "\($0.0.depth) \($0.0.specificPR) \($0.0.specificAuthor) @\($0.1)" }
                == ["max 1821 theirs @contributor-x", "max 1822 mine @"],
              "\(reviews.map { "\($0.0.depth) \($0.0.specificPR) \($0.0.specificAuthor) @\($0.1)" })")
        let recorded = view(fixture.auto)?.task.dispatched.keys.sorted() ?? []
        check("and records them dispatched", recorded == ["fix:\(ref(1820))", "review:\(ref(1821))@a1b2c3d4",
                                                           "review:\(ref(1822))@e5f6a7b8"], "\(recorded)")
        check("so the goal wants only the findings now",
              view(fixture.auto)?.work.map(\.kind) == [.findings])
        await store.runGoalPass()
        check("a second pass asks for nothing more", store.requestedWork.count == asksBefore.count + 2,
              "\(store.requestedWork.count - asksBefore.count) asks")
        let overlay = store.goalOverlay
        check("the asks show as queued", overlay.queuedIssues == [] && overlay.queuedPRs == [1821, 1822],
              "\(overlay)")
        _ = await store.setGoalMode(fixture.auto, mode: .observe)
        check("switching the goal to observe withdraws its asks not started",
              !store.requestedWork.contains { ["review:\(key(1821))", "review:\(key(1822))"].contains($0.key) }
                && !store.queuedTasks.contains { ["review:\(key(1821))", "review:\(key(1822))"].contains($0.id) },
              "\(store.requestedWork.map(\.key))")
        check("and forgets them, the fix whose agent started kept",
              view(fixture.auto)?.task.dispatched.keys.sorted() == ["fix:\(ref(1820))"],
              "\(view(fixture.auto)?.task.dispatched.keys.sorted() ?? [])")
        _ = await store.setGoalMode(fixture.auto, mode: .auto)
        await store.runGoalPass()
        check("back in auto, its reviews are asked for again",
              Set(["review:\(key(1821))", "review:\(key(1822))"]).isSubset(of: Set(store.requestedWork.map(\.key))),
              "\(store.requestedWork.map(\.key))")
        _ = await store.editTask(.archive(id: fixture.auto))
        check("archiving the goal withdraws them too",
              !store.requestedWork.contains { ["review:\(key(1821))", "review:\(key(1822))"].contains($0.key) },
              "\(store.requestedWork.map(\.key))")
        _ = await store.editTask(.unarchive(id: fixture.auto))
        _ = await store.setGoalMode(fixture.observe, mode: .auto, reviewOnPush: true)
        check("the grouper's goal, automated, asks for its PR's review",
              store.requestedWork.contains { $0.key == "review:\(key(1831))" }, "\(store.requestedWork.map(\.key))")
        _ = await store.setGoalMode(fixture.observe, mode: .dismissed)
        check("dismissing it, which hides it, withdraws that too",
              !store.requestedWork.contains { $0.key == "review:\(key(1831))" }, "\(store.requestedWork.map(\.key))")

        print("the agent overlay")
        let home = PRRef.LocalRepo(owner: owner, repo: repo, path: "")
        func url(_ pr: Int, in slug: String = "\(owner)/\(repo)") -> String { "https://github.com/\(slug)/pull/\(pr)" }
        func run(_ id: String, _ pr: Int?, _ kind: String, _ label: String, _ state: AgentState.RunState,
                 released: Bool = false, in slug: String = "\(owner)/\(repo)") -> Store.AgentRow {
            Store.AgentRow(record: AgentState.RunRecord(runID: id, dispatchedAt: 0, prNumber: pr,
                                                        prURL: pr.map { url($0, in: slug) } ?? "", kind: kind,
                                                        label: label, released: released),
                           state: state, reason: "", window: nil)
        }
        func task(_ id: String, pr: Int?, in slug: String = "\(owner)/\(repo)") -> Store.QueuedAgentTask {
            Store.QueuedAgentTask(id: id, job: Store.AgentJob(kind: "review", auditAction: "review", label: id,
                                                              prompt: "", prURL: pr.map { url($0, in: slug) },
                                                              prNumber: pr, authorLogin: nil,
                                                              counter: nil),
                                  attemptNumber: 1)
        }
        let issueURL = "https://github.com/\(owner)/\(repo)/issues/1801"
        let composed = Store.goalOverlay(
            rows: [run("a", 1821, "review", "Review · #1821 · deep", .running),
                   run("b", 1810, "review", "Review · #1810 · deep", .awaitingInput),
                   run("c", 1822, "review", "Review · #1822 · deep", .running, released: true),
                   run("d", 1811, "conflicts", "Resolve · #1811", .finished),
                   run("e", nil, "issues", "Issues · #18030 · deep", .starting),
                   run("f", nil, "issues", "Issues · \(repo)#1803 · deep", .unknown),
                   run("g", nil, "review", "Issues · #1804 · deep", .running),
                   // Another root's PR and issue under the goals' numbers: not theirs.
                   run("h", 1823, "review", "Review · widgets#1823 · deep", .running, in: "acme/widgets"),
                   run("i", nil, "issues", "Issues · widgets#1805 · deep", .running)],
            queued: [task("issues:\(key(1802))", pr: nil),
                     task("review-req:\(key(1812))", pr: 1812),
                     task("issues:acme/widgets#1806", pr: nil),
                     task("review-req:acme/widgets#1813", pr: 1813, in: "acme/widgets")],
            requested: [store.goalFixAsk(number: 1801, url: issueURL, author: nil),
                        store.goalFixAsk(number: 1807, url: "https://github.com/acme/widgets/issues/1807",
                                         author: nil)],
            startingFixes: [1830], watched: home)
        check("agents: live PR runs, issue fixes by label, spawning fixes, the goals' repo alone",
              composed.agentPRs == [1810, 1821] && composed.agentIssues == [1803, 1830, 18030], "\(composed)")
        check("queued: PR tasks, issues: tasks and asked-for fixes, the goals' repo alone",
              composed.queuedPRs == [1812] && composed.queuedIssues == [1801, 1802], "\(composed)")
        let watched = (owner: owner, repo: repo)
        func pill(_ kind: String, _ n: Int, in o: String = owner) -> TaskGraph.Node {
            TaskGraph.Node(kind: kind, url: "https://github.com/\(o)/\(repo)/\(kind == "pr" ? "pull" : "issues")/\(n)",
                           label: "#\(n)")
        }
        let badges = [pill("pr", 1821), pill("pr", 1812), pill("pr", 1822), pill("add", 1803), pill("add", 1802),
                      pill("issue", 1803), pill("pr", 1821, in: "someone-else")]
            .map { composed.badge($0, watched: watched).map { "\($0)" } ?? "-" }
        check("badges: a PR's and an add pill's, none on an issue pill or another repo's PR",
              badges == ["agent", "queued", "-", "agent", "queued", "-", "-"], "\(badges)")

        print("finding goals")
        fm.createFile(atPath: failing.path, contents: Data())
        await store.findGoals()
        try? fm.removeItem(at: failing)
        check("a failed grouper run is shown, and the chip freed",
              store.tasksGrouperMark?.error?.contains("502") == true && !store.tasksGrouping,
              store.tasksGrouperMark?.error ?? "nil")

        print(pass ? "tasks: all passed" : "tasks: FAILED")
        return pass
    }

    /// What `writeGoalFixture` made: the three goals' ids.
    struct GoalFixture {
        let proposed: String
        let auto: String
        let observe: String
    }

    struct FixtureError: Error { let message: String }

    /// Three goals in `dir`, through the real core: a grouper proposal (`TaskGrouper.apply`)
    /// whose PRs close its issues, stack on and block each other, one issue with no PR; an
    /// `auto` goal (`reviewOnPush`) with an issue to fix, someone's PR and one of `me`'s; a
    /// proposal the operator chose to observe. Their items are synced into the mirror
    /// (`TaskSync.apply`) beside whatever it holds.
    static func writeGoalFixture(in dir: URL, now: Date, me: String) throws -> GoalFixture {
        let (owner, repo) = CoreAssets.repoCoordinates()
        func gh(_ kind: String, _ n: Int) -> String {
            "https://github.com/\(owner)/\(repo)/\(kind == "pr" ? "pull" : "issues")/\(n)"
        }
        func ago(_ hours: Double) -> Date { now.addingTimeInterval(-hours * 3600) }
        struct Item {
            let kind: String, number: Int, title: String, author: String
            var state = "OPEN", draft = false, closes: [Int] = [], base: String? = nil, head: String? = nil,
                oid: String? = nil
        }
        let items = [
            Item(kind: "issue", number: 1801, title: "iPhone Duo: apps open on the wrong panel", author: "duo-fan"),
            Item(kind: "issue", number: 1802, title: "Fold gesture on the iPhone Duo", author: "mobile-dev"),
            Item(kind: "issue", number: 1803, title: "Screenshots capture one panel of the Duo", author: "qa-bot"),
            Item(kind: "pr", number: 1810, title: "feat: iPhone Duo support", author: me, closes: [1801],
                 base: "main", head: "duo-support", oid: "d00d1810"),
            Item(kind: "pr", number: 1811, title: "fix: fold gesture on the Duo", author: "mobile-dev",
                 closes: [1802], base: "duo-support", head: "duo-fold", oid: "f01d1811"),
            Item(kind: "pr", number: 1812, title: "fix: simulator-server crash on a second display", author: me,
                 state: "MERGED", base: "main", head: "second-display", oid: "5ec0d1812"),
            Item(kind: "issue", number: 1820, title: "Fire TV remote: long press", author: "tv-user"),
            Item(kind: "pr", number: 1821, title: "feat: remote key mapping", author: "contributor-x",
                 base: "main", head: "remote-keys", oid: "a1b2c3d4"),
            Item(kind: "pr", number: 1822, title: "feat: remote swipe gestures", author: me,
                 base: "main", head: "remote-swipe", oid: "e5f6a7b8"),
            Item(kind: "issue", number: 1830, title: "Network inspector on Android", author: "net-user"),
            Item(kind: "pr", number: 1831, title: "feat: Android network inspector", author: "net-dev", draft: true,
                 closes: [1830], base: "main", head: "android-net", oid: "0a1b1831"),
        ]
        let candidates = items.filter { $0.number < 1820 || $0.number >= 1830 }.map {
            TaskGrouper.Candidate(number: $0.number, kind: $0.kind, title: $0.title, author: $0.author,
                                  closes: $0.closes, url: gh($0.kind, $0.number))
        }
        let answers = [
            TaskGrouper.Answer(title: "iPhone Duo support", tags: ["area:devices"],
                               items: [1801, 1802, 1803, 1810, 1811, 1812], blocks: [(from: 1812, to: 1810)]),
            TaskGrouper.Answer(title: "Android network inspector", tags: ["area:network"], items: [1830, 1831]),
        ]
        let grouped = TaskGrouper.apply(answers, to: try TaskStore.tasks(in: dir), candidates: candidates,
                                        now: ago(30), minItems: 2, maxNew: 3,
                                        watched: (owner: owner, repo: repo))
        guard grouped.created == 2, grouped.tasks.count >= 2 else {
            throw FixtureError(message: "the grouper fixture proposed \(grouped.created) goals")
        }
        let proposed = grouped.tasks[grouped.tasks.count - 2].id
        let observe = grouped.tasks[grouped.tasks.count - 1].id
        try save(grouped.tasks, events: grouped.events, in: dir)
        try TaskStore.edit(.mode(id: observe, mode: TaskMode.observe.rawValue, reviewOnPush: nil), now: ago(20), in: dir)
        let auto = try TaskStore.edit(.create(title: "Fire TV remote input", tags: ["area:vega", "goal"],
                                              links: [gh("issue", 1820), gh("pr", 1821), gh("pr", 1822)]),
                                      now: ago(50), in: dir)
        try TaskStore.edit(.mode(id: auto.id, mode: TaskMode.auto.rawValue, reviewOnPush: true), now: ago(6), in: dir)

        var fetched: [String: TaskFetch] = [:]
        for i in items {
            let url = gh(i.kind, i.number)
            fetched[url] = .found(TaskItem(kind: i.kind, title: i.title, url: url, state: i.state, isDraft: i.draft,
                                           ci: i.kind == "pr" ? "SUCCESS" : nil,
                                           closes: i.closes.map { gh("issue", $0) },
                                           mergedAt: i.state == "MERGED" ? ago(10) : nil, author: i.author,
                                           baseRef: i.base, headRef: i.head, headOid: i.oid))
        }
        let synced = TaskSync.apply(tasks: try TaskStore.tasks(in: dir), mirror: TaskStore.mirror(in: dir),
                                    fetched: fetched, now: ago(2), excerptChars: 200)
        try save(synced.tasks, events: synced.events, in: dir)
        try CommsLog.encoder.encode(synced.mirror).write(to: dir.appendingPathComponent("mirror.json"))
        return GoalFixture(proposed: proposed, auto: auto.id, observe: observe)
    }

    /// `tasks` as the whole task file, `events` appended to the history.
    private static func save(_ tasks: [TrackedTask], events: [TaskEvent], in dir: URL) throws {
        let fm = FileManager.default
        try fm.createDirectory(at: dir, withIntermediateDirectories: true)
        try CommsLog.encoder.encode(["tasks": tasks]).write(to: dir.appendingPathComponent("tasks.json"))
        let url = dir.appendingPathComponent("events.jsonl")
        if !fm.fileExists(atPath: url.path) { fm.createFile(atPath: url.path, contents: nil) }
        var lines = Data()
        for e in events {
            lines.append(try CommsLog.encoder.encode(e))
            lines.append(UInt8(ascii: "\n"))
        }
        let handle = try FileHandle(forWritingTo: url)
        try handle.seekToEnd()
        try handle.write(contentsOf: lines)
        try handle.close()
    }

    /// The screen in a window never ordered on screen: typing goes through each field's
    /// editor, and a click straight to the view under it, at a point found from the fields
    /// around it. Every step that moves the screen is checked before the next relies on it.
    @MainActor
    private static func screen(_ store: Store,
                               check report: (String, Bool, @autoclosure () -> String) -> Void) async {
        func check(_ name: String, _ ok: Bool, _ detail: String = "") { report(name, ok, detail) }
        _ = await store.editTask(.create(title: "Editor lifecycle", tags: [], links: []))
        guard let id = store.taskViews.first(where: { $0.task.title == "Editor lifecycle" })?.task.id else {
            return check("its task is created", false, "")
        }
        let host = NSHostingView(rootView: TasksView(isPresented: .constant(true), seedExpanded: [id],
                                                     seedEditor: .init(task: id, kind: .note))
            .environmentObject(store))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 900),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        defer { window.contentView = nil }

        func settle(until done: () -> Bool = { false }) async {
            for _ in 0..<40 {
                host.layoutSubtreeIfNeeded()
                try? await Task.sleep(nanoseconds: 50_000_000)
                if done() { break }
            }
            host.layoutSubtreeIfNeeded()
        }
        func fields(_ view: NSView) -> [NSTextField] {
            ((view as? NSTextField).map { [$0] } ?? []) + view.subviews.flatMap(fields)
        }
        func field(_ placeholder: String) -> NSTextField? {
            fields(host).first { $0.placeholderString?.hasPrefix(placeholder) == true }
        }
        func shown(_ text: String) -> Bool { fields(host).contains { $0.stringValue.contains(text) } }
        func frame(_ f: NSTextField) -> NSRect { f.convert(f.bounds, to: nil) }
        func type(_ text: String, into f: NSTextField) {
            window.makeFirstResponder(f)
            f.currentEditor()?.insertText(text)
        }
        func submit(_ f: NSTextField) {
            window.makeFirstResponder(f)
            f.currentEditor()?.insertNewline(nil)
        }
        func click(_ point: NSPoint) {
            let at = host.convert(point, from: nil)
            for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
                guard let event = NSEvent.mouseEvent(with: type, location: point, modifierFlags: [],
                                                     timestamp: ProcessInfo.processInfo.systemUptime,
                                                     windowNumber: window.windowNumber, context: nil,
                                                     eventNumber: 0, clickCount: 1, pressure: 1) else { return }
                if type == .leftMouseDown { host.hitTest(at)?.mouseDown(with: event) }
                else { host.hitTest(at)?.mouseUp(with: event) }
            }
        }
        let note = "Note:", link = "A PR or issue URL", tags = "Tags, e.g. decision"
        func cancel(beside f: NSTextField) -> NSPoint { NSPoint(x: frame(f).maxX + 29, y: frame(f).midY) }

        await settle()
        // Points on the row, from its id at the right of its actions: a sync may reorder the
        // rows. A folded row hides its actions, so its header is where it was last seen.
        func actions() -> NSRect? { fields(host).first { $0.stringValue == id }.map(frame) }
        guard let noteField = field(note), let row = actions() else {
            return check("the seeded note editor is drawn", false, "")
        }
        let left = frame(noteField).minX
        var headerAbove: CGFloat = 0
        var lastHeader = NSPoint.zero
        func header() -> NSPoint {
            if let row = actions() { lastHeader = NSPoint(x: row.minX / 2, y: row.maxY + headerAbove) }
            return lastHeader
        }
        func addLink() -> NSPoint { NSPoint(x: left + 33, y: actions()?.midY ?? -1) }
        func addNote() -> NSPoint { NSPoint(x: left + 105, y: actions()?.midY ?? -1) }
        type("USB only for now", into: noteField)
        await settle()

        // The header is the first point above the actions that folds the row.
        for above in stride(from: 8.0, through: 40.0, by: 2.0) where field(note) != nil {
            click(NSPoint(x: row.minX / 2, y: row.maxY + above))
            await settle(until: { field(note) == nil })
            headerAbove = above
        }
        guard field(note) == nil else { return check("a click on the row's header folds it", false, "") }
        lastHeader = NSPoint(x: row.minX / 2, y: row.maxY + headerAbove)
        click(header())
        await settle(until: { field(note) != nil })
        check("a note typed before its row folded is there when it unfolds",
              field(note)?.stringValue == "USB only for now", field(note)?.stringValue ?? "no note editor")

        click(addLink())
        await settle(until: { field(link) != nil })
        guard let linkField = field(link) else { return check("Add link opens the link editor", false, "") }
        type("argent 1714", into: linkField)
        submit(linkField)
        await settle(until: { shown("not a link: argent 1714") })
        check("a refused link says why at its editor", shown("not a link: argent 1714"))
        click(header())
        await settle(until: { field(link) == nil })
        click(header())
        await settle(until: { field(link) != nil })
        check("the link and its refusal outlive a fold",
              field(link)?.stringValue == "argent 1714" && shown("not a link: argent 1714"),
              field(link)?.stringValue ?? "no link editor")

        _ = await store.editTask(.archive(id: id))
        await settle(until: { field(link) == nil })
        check("archived, the row leaves the list", field(link) == nil)
        _ = await store.editTask(.unarchive(id: id))
        await settle(until: { field(link) != nil })
        check("and comes back with its editor as it was",
              field(link)?.stringValue == "argent 1714" && shown("not a link: argent 1714"),
              field(link)?.stringValue ?? "no link editor")
        if let refused = field(link) { click(cancel(beside: refused)) }
        await settle(until: { field(link) == nil })
        click(addLink())
        await settle(until: { field(link) != nil })
        check("a cancelled editor's refusal leaves with it",
              field(link)?.stringValue == "" && !shown("not a link"), field(link)?.stringValue ?? "no link editor")
        if let open = field(link) { type("half a link", into: open) }
        click(addLink())
        await settle()
        check("Add link again keeps the open link editor as typed",
              field(link)?.stringValue == "half a link", field(link)?.stringValue ?? "no link editor")
        click(addNote())
        await settle(until: { field(note) != nil })
        check("Add note puts an empty note editor in its place",
              field(link) == nil && field(note)?.stringValue == "", field(note)?.stringValue ?? "no note editor")

        click(addNote())
        await settle(until: { field(note) != nil })
        guard let first = field(note), let firstTags = field(tags) else {
            return check("Add note opens the note editor", false, "")
        }
        type("asked Apple about the entitlement", into: first)
        submit(first)
        // Before the note's answer: Cancel, and a new note editor.
        click(cancel(beside: firstTags))
        click(addNote())
        await settle(until: {
            store.taskViews.first { $0.task.id == id }?.history.first?.text == "asked Apple about the entitlement"
        })
        await settle()
        check("the note is saved",
              store.taskViews.first { $0.task.id == id }?.history.first?.text == "asked Apple about the entitlement")
        check("and its answer leaves the note editor opened after it open", field(note) != nil)

        click(addLink())
        await settle(until: { field(link) != nil })
        guard let second = field(link) else { return check("Add link opens the link editor", false, "") }
        type("argent 1730", into: second)
        submit(second)
        click(cancel(beside: second))
        await settle(until: { shown("not a link: argent 1730") })
        check("a refusal whose editor closed is said on the row",
              field(link) == nil && shown("not a link: argent 1730"))
    }
}
