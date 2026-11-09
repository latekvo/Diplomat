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

        print(pass ? "tasks: all passed" : "tasks: FAILED")
        return pass
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
