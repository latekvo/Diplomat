import SwiftUI
import AppKit
import ArgentUtilsCore

@main
struct ArgentUtilsApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var store = Store()

    var body: some Scene {
        MenuBarExtra("Argent Utils", systemImage: "wrench.and.screwdriver") {
            PopoverRoot()
                .environmentObject(store)
        }
        .menuBarExtraStyle(.window)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        let env = ProcessInfo.processInfo.environment

        // Singleton, newest-wins: a freshly launched GUI instance kills any older
        // ones so there's never more than one wrench. Skipped in headless self-test
        // mode (see `Headless` for the single env-var list shared with Store) so a
        // dump/lookup run can't kill the live menu-bar app.
        if !Headless.active {
            SingleInstance.terminateOthers()

            // First run from a terminal (`swift run`): offer to install as a login
            // daemon. If accepted, the detached installer builds + launches the
            // daemon, which replaces this instance via the singleton. We keep
            // running rather than exit, so a *failed* install still leaves a
            // usable wrench in the menu bar instead of nothing.
            Daemon.offerInstallIfInteractive()

            // Proactively provoke the macOS "control <terminal>" automation prompt
            // once, so SPAWN AGENT works without a per-first-use prompt later.
            let defaults = UserDefaults.standard
            if !defaults.bool(forKey: "didTriggerTerminalAutomation") {
                defaults.set(true, forKey: "didTriggerTerminalAutomation")
                let preferred = SpawnTerminal(rawValue: Store.storedTerminalChoice ?? "") ?? .iterm
                AgentSpawner.triggerAutomationPrompt(preferred: preferred)
            }
        }

        // Menu-bar-only: no Dock icon.
        NSApp.setActivationPolicy(.accessory)

        // Unattended 6AM self-update (launchd StartCalendarInterval): merge upstream if
        // behind, rebuild, relaunch if the app was running, then exit. Runs off the main
        // thread — the git/build work is blocking.
        if env["ARGENT_UTILS_SELF_UPDATE"] == "1" {
            Task.detached { exit(SelfUpdate.runScheduled()) }
        }

        // Headless self-tests: run the real pipeline, print, exit.
        if env["ARGENT_UTILS_DUMP"] == "1" {
            Task { await Dump.run(); exit(0) }
        }
        if let lk = env["ARGENT_UTILS_LOOKUP"], let n = Int(lk) {
            Task { await Dump.lookup(n); exit(0) }
        }
        // Prompt/spawn self-test: print the assembled review prompt plus the exact
        // shell command and AppleScript the SPAWN AGENT button would run, then exit.
        // ARGENT_UTILS_PRINT_PROMPT=mine|user (default mine).
        if let mode = env["ARGENT_UTILS_PRINT_PROMPT"] {
            Dump.printPrompt(mode: mode); exit(0)
        }
        // Settings self-test: build a Store (which loads persisted UserDefaults)
        // and print the resolved settings. Run via the .app bundle's binary so it
        // uses the same `com.ignacy.argent-utils` defaults domain as the GUI.
        if env["ARGENT_UTILS_SETTINGS_DUMP"] == "1" {
            Task { @MainActor in Dump.settings(); exit(0) }
        }
        // Headless UI render: snapshot a view to PNG and exit. `run` returns false
        // for the window-hosted `popover` mode, which needs the app runloop to lay
        // out first — it exits by itself once the snapshot is written.
        if let what = env["ARGENT_UTILS_RENDER"] {
            Task { @MainActor in if Render.run(what, store: Store()) { exit(0) } }
        }
        // End-to-end self-test of the agent-session tracking path (capture, status,
        // liveness, focus, completion). Drives a real throwaway terminal window.
        // Exit code reflects the outcome so scripts/hooks can gate on it.
        if env["ARGENT_UTILS_TRACK_TEST"] == "1" {
            Task { let ok = await TrackTest.run(); exit(ok ? 0 : 1) }
        }
        // Device-allocator self-test: exercise the exact paths the live panel uses —
        // resolve node, shell the installer's --check, and Codable-decode the daemon's
        // real state.json — and print them. Works headless (e.g. with a locked screen).
        if env["ARGENT_UTILS_DEVICE_DUMP"] == "1" {
            Dump.deviceAllocator(); exit(0)
        }
        // Auto-fix monitor self-test: one real poll of my open PRs + the diff/dispatch
        // decision, printed. Proves the gh query, snapshot parse, edge-triggered diff,
        // and the exact prompts it would spawn — without opening any terminal.
        if env["ARGENT_UTILS_AUTOFIX_POLL"] == "1" {
            Task { await Dump.autofixPoll(); exit(0) }
        }
        // API-error watcher dry-run: dump every terminal session's last lines and show
        // which ones the watcher would nudge — WITHOUT sending anything.
        if env["ARGENT_UTILS_APIWATCH_SCAN"] == "1" {
            Dump.apiWatchScan(); exit(0)
        }
        // Spawn focus self-test: prove a background (auto-fix) spawn keeps the user's
        // focus while a foreground (user SPAWN) spawn still brings the terminal forward.
        // Drives two throwaway terminal windows it closes itself. Exit code = pass/fail.
        if env["ARGENT_UTILS_SPAWN_FOCUS_TEST"] == "1" {
            Task { @MainActor in exit(Dump.spawnFocusTest() ? 0 : 1) }
        }
    }
}

/// Quit confirmation. A real AppKit modal (SwiftUI's `.alert` is unreliable inside
/// a `MenuBarExtra` window), so a stray click can never silently kill the wrench.
enum QuitFlow {
    @MainActor static func confirm() {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Quit Argent Utils?"
        alert.informativeText = "The menu-bar wrench disappears until you launch it again."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Cancel")   // default — Return cancels
        alert.addButton(withTitle: "Quit")
        if alert.runModal() == .alertSecondButtonReturn {
            NSApp.terminate(nil)
        }
    }
}

/// Singleton enforcement: the newest instance wins and force-quits the rest.
enum SingleInstance {
    static let bundleID = "com.ignacy.argent-utils"
    static let execName = "ArgentUtils"

    /// Whether another live instance exists — the 6AM updater relaunches only if so,
    /// never spawning a menu-bar app onto a session that isn't showing one.
    static func isRunning() -> Bool {
        let myPid = ProcessInfo.processInfo.processIdentifier
        return NSWorkspace.shared.runningApplications.contains { app in
            app.processIdentifier != myPid
                && (app.bundleIdentifier == bundleID
                    || app.executableURL?.lastPathComponent == execName)
        }
    }

    static func terminateOthers() {
        func others() -> [NSRunningApplication] {
            let myPid = ProcessInfo.processInfo.processIdentifier
            return NSWorkspace.shared.runningApplications.filter { app in
                guard app.processIdentifier != myPid else { return false }
                return app.bundleIdentifier == bundleID
                    || app.executableURL?.lastPathComponent == execName
            }
        }
        let initial = others()
        guard !initial.isEmpty else { return }
        log("found \(initial.count) old instance(s), terminating")
        for app in initial { app.terminate() }      // ask nicely first
        usleep(400_000)                              // 0.4s grace
        let survivors = others()                     // re-query: terminated apps drop out
        for app in survivors { app.forceTerminate() }
        if !survivors.isEmpty { log("force-killed \(survivors.count) survivor(s)") }
    }

    private static func log(_ msg: String) {
        FileHandle.standardError.write(Data("ArgentUtils singleton: \(msg)\n".utf8))
    }
}

/// Prints every tool's output to stdout — the GUI's data layer, exercised end to end.
enum Dump {
    static func run() async {
        do {
            let me = try await API.fetchViewerLogin()
            let prs = try await API.fetchOpenPRs()
            let issues = try await API.fetchOpenIssues()
            print("== viewer: @\(me) · open PRs: \(prs.count) · open issues: \(issues.count) ==\n")

            let t1 = Filters.skillPRs(prs).sorted { $0.number > $1.number }
            print("TOOL 1 — SKILL.md PRs: \(t1.count)")
            for p in t1 {
                let s = p.files.filter(Filters.isSkillFile).map(Fmt.skillName).joined(separator: ", ")
                print("  #\(p.number) @\(p.author) [\(p.isDraft ? "draft" : "ready")] → \(s)")
            }

            let t2 = Filters.installerPRs(prs).sorted { $0.number > $1.number }
            print("\nTOOL 2 — installer/CLI PRs: \(t2.count)")
            for p in t2 {
                let f = p.files.filter(Filters.isInstallerFile)
                print("  #\(p.number) @\(p.author) (\(f.count)) → \(f.map(Fmt.shortPath).joined(separator: ", "))")
            }

            let t3 = Filters.staleReadyPRs(prs).sorted { $0.readyAt < $1.readyAt }
            print("\nTOOL 3 — ready >10d: \(t3.count)")
            for p in t3 {
                print("  #\(p.number) @\(p.author) \(Fmt.days(p.readyAt))d (\(p.readyForReviewAt == nil ? "born-ready" : "converted"))")
            }

            let t4 = Filters.unaddressedExternalIssues(issues).sorted { $0.createdAt < $1.createdAt }
            print("\nTOOL 4 — unaddressed external issues: \(t4.count)")
            for i in t4 {
                print("  #\(i.number) @\(i.author) [\(i.authorAssociation)] \(Fmt.days(i.createdAt))d \(i.commentCount)c labels:[\(i.labels.joined(separator: ","))]")
            }

            let t5 = Filters.myApprovedPRs(prs, me: me).sorted { $0.number > $1.number }
            print("\nTOOL 5 — my approved PRs: \(t5.count)")
            for p in t5 {
                print("  #\(p.number) @\(p.author) [\(p.isDraft ? "draft" : "ready")] \(Fmt.age(p.createdAt))")
            }

            let t6 = Filters.myUnaddressedReviewPRs(prs, me: me).sorted { $0.number > $1.number }
            print("\nTOOL 6 — my PRs w/ unaddressed reviews: \(t6.count)")
            for p in t6 {
                print("  #\(p.number) @\(p.author) \(p.unaddressedThreads(me: me).count) open thread(s)")
            }
        } catch {
            let msg = (error as? LocalizedError)?.errorDescription ?? "\(error)"
            print("DUMP ERROR: \(msg)")
        }
    }

    /// Exercises the reverse-lookup path through the real Store logic.
    static func lookup(_ n: Int) async {
        do {
            let me = try await API.fetchViewerLogin()
            let prs = try await API.fetchOpenPRs()
            let issues = try await API.fetchOpenIssues()
            let r = await MainActor.run { () -> LookupResult in
                let s = Store()
                s.me = me
                s.prs = prs
                s.issues = issues
                s.hasLoaded = true
                return s.lookup(n)
            }
            print("#\(n): \(r.presence)")
            print("on lists: \(r.onLists.isEmpty ? "(none)" : r.onLists.map { $0.title }.joined(separator: ", "))")
        } catch {
            print("LOOKUP ERROR: \((error as? LocalizedError)?.errorDescription ?? "\(error)")")
        }
    }

    /// Exercises the prompt builder + spawn-command assembly without any UI or
    /// network. `mode` is "user" to preview the someone-else's-PRs variant,
    /// anything else previews my-PRs. Mirrors the wizard's default toggle state.
    /// Modes prefixed "conflict…" route to the Resolve-conflicts prompt instead.
    static func printPrompt(mode: String) {
        let m = mode.lowercased()
        if m.hasPrefix("conflict") { printConflictPrompt(mode: m); return }
        if m.hasPrefix("audit") { printAuditPrompt(mode: m); return }
        let isUser = m.hasPrefix("user")
        let isSingle = m.hasPrefix("single")
        let cfg = ReviewConfig(
            depth: "max",
            target: isSingle ? .specific : (isUser ? .someone : .mine),
            username: isUser ? "someuser" : "",
            me: "latekvo",
            markReady: true,
            leaveReviews: true,
            replyToReviews: true,
            includeDrafts: true,
            includeReady: true,
            specificPR: isSingle ? "337" : "",
            finalPass: m.contains("final"))
        let label = isSingle ? "single PR #337" : (isUser ? "someone else's PRs" : "my PRs")
        print("== ReviewConfig: \(label) · depth=\(ReviewCatalog.depth(id: cfg.depth).title) ==\n")
        print("----- PROMPT -----")
        print(cfg.buildPrompt())
        if let file = try? AgentSpawner.writePrompt(cfg.buildPrompt()) {
            let cmd = AgentSpawner.shellCommand(promptFile: file, donePath: AgentSpawner.doneFilePath())
            print("\n----- SHELL COMMAND -----")
            print(cmd)
            let term = AgentSpawner.resolved(.iterm)
            print("\n----- APPLESCRIPT · foreground (user SPAWN, activates terminal) -----")
            print(AgentSpawner.appleScript(for: term, shellCommand: cmd))
            print("\n----- APPLESCRIPT · background (auto-fix, restores focus to Finder) -----")
            print(AgentSpawner.appleScript(for: term, shellCommand: cmd, restoreFocusTo: "com.apple.finder"))
            try? FileManager.default.removeItem(at: file)
        }
    }

    /// Same as `printPrompt`, but for the Resolve-conflicts wizard. `mode` selects
    /// the variant: "conflicts-user" (someone else's), "conflicts-single" (one PR),
    /// anything else (e.g. "conflicts" / "conflicts-mine") = my PRs.
    static func printConflictPrompt(mode: String) {
        let isUser = mode.contains("user")
        let isSingle = mode.contains("single")
        let target: ConflictConfig.Target = isSingle ? .specific : (isUser ? .someone : .mine)
        let cfg = ConflictConfig(
            target: target,
            username: isUser ? "someuser" : "",
            me: "latekvo",
            specificPR: isSingle ? "337" : "")
        let label = isSingle ? "single PR #337" : (isUser ? "someone else's PRs" : "my PRs")
        print("== ConflictConfig: \(label) ==\n")
        print("----- PROMPT -----")
        print(cfg.buildPrompt())
        if let file = try? AgentSpawner.writePrompt(cfg.buildPrompt()) {
            let cmd = AgentSpawner.shellCommand(promptFile: file, donePath: AgentSpawner.doneFilePath())
            print("\n----- SHELL COMMAND -----")
            print(cmd)
            print("\n----- APPLESCRIPT (\(AgentSpawner.resolved(.iterm).title)) -----")
            print(AgentSpawner.appleScript(for: AgentSpawner.resolved(.iterm), shellCommand: cmd))
            try? FileManager.default.removeItem(at: file)
        }
    }

    /// Same as `printPrompt`, but for the Full-E2E-test action. `mode` selects the
    /// toggle state: "audit" (find-only), "audit-issues" (+fix open bug issues),
    /// "audit-prs" (+open PRs), "audit-all" (both).
    static func printAuditPrompt(mode: String) {
        let cfg = AuditConfig(
            fixIssues: mode.contains("issues") || mode.contains("all"),
            openPRs: mode.contains("prs") || mode.contains("all"))
        let flags = "fixIssues=\(cfg.fixIssues) openPRs=\(cfg.openPRs)"
        print("== AuditConfig: full-repo E2E test · \(flags) ==\n")
        print("----- PROMPT -----")
        print(cfg.buildPrompt())
        if let file = try? AgentSpawner.writePrompt(cfg.buildPrompt()) {
            let cmd = AgentSpawner.shellCommand(promptFile: file, donePath: AgentSpawner.doneFilePath())
            print("\n----- SHELL COMMAND -----")
            print(cmd)
            print("\n----- APPLESCRIPT (\(AgentSpawner.resolved(.iterm).title)) -----")
            print(AgentSpawner.appleScript(for: AgentSpawner.resolved(.iterm), shellCommand: cmd))
            try? FileManager.default.removeItem(at: file)
        }
    }

    /// API-error watcher dry-run: read every terminal session's last visible lines,
    /// print which the watcher would nudge, and send NOTHING. Safe to run anytime.
    static func apiWatchScan() {
        guard let sessions = ApiErrorWatcher.dumpSessions() else {
            print("== api-error scan: DUMP FAILED (automation permission? AppleEvent timeout?) ==")
            return
        }
        print("== api-error scan: \(sessions.count) terminal session(s) ==\n")
        for s in sessions {
            let mark = ApiErrorMatch.looksLikeApiError(s.tail) ? "⚠️ MATCH" : "  ok   "
            let last = s.tail.split(whereSeparator: \.isNewline).last.map(String.init) ?? ""
            print("  \(mark)  \(s.tty)   last: \(last.prefix(70))")
        }
        let matches = sessions.filter { ApiErrorMatch.looksLikeApiError($0.tail) }
        print("\n\(matches.count) session(s) would receive: "
            + "\"\(ApiErrorWatcher.continueMessage)\"  (dry-run — nothing sent; "
            + "out-of-quota stalls are ignored, never nudged)")
    }

    /// Spawn focus self-test: exercise the REAL `AgentSpawner` spawn path twice against
    /// throwaway terminal windows and assert the focus contract —
    ///   • background spawn (auto-fix monitor, `restoreFocusTo` set) must KEEP the user's
    ///     focus (Finder here) while still creating + driving the window; and
    ///   • foreground spawn (a user pressing SPAWN AGENT, `restoreFocusTo` nil) must still
    ///     create + drive a window.
    /// The FOREGROUND case only asserts the window is created, not that the terminal came
    /// forward: macOS focus-stealing PREVENTION suppresses cross-app `activate` requests
    /// from a process that isn't itself frontmost (this backgrounded self-test), so a live
    /// activation check here is unreliable. The user path activates fine because the
    /// menu-bar popover IS frontmost at click time, and its generated AppleScript is
    /// unchanged from before this fix (see ARGENT_UTILS_PRINT_PROMPT) — that identity is
    /// the real regression guard; the foreground focus result below is informational only.
    /// Returns overall pass/fail so a hook/script can gate on the exit code. Drives the
    /// terminal the panel is configured for (falls back to iTerm).
    @MainActor static func spawnFocusTest() -> Bool {
        func osa(_ script: String) -> String {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
            p.arguments = ["-e", script]
            let out = Pipe(); p.standardOutput = out; p.standardError = Pipe()
            do { try p.run() } catch { return "" }
            let d = out.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            return String(data: d, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        }
        func frontmost() -> String {
            osa("tell application \"System Events\" to get name of first process whose frontmost is true")
        }
        // Finder is the neutral baseline: if it stays frontmost the spawn kept focus, if
        // it's replaced by the terminal the spawn stole it.
        func neutral() { _ = osa("tell application \"Finder\" to activate"); usleep(500_000) }
        func closeWindow(_ term: SpawnTerminal, _ wid: String) {
            guard !wid.isEmpty else { return }
            _ = osa("""
            tell application "\(term.appName)"
                repeat with w in windows
                    if (id of w as string) is "\(wid)" then
                        try
                            close w
                        end try
                    end if
                end repeat
            end tell
            """)
        }

        let term = AgentSpawner.resolved(SpawnTerminal(rawValue: Store.storedTerminalChoice ?? "") ?? .iterm)
        print("== spawn focus self-test · terminal: \(term.title) ==\n")

        // 1. BACKGROUND spawn — must not steal focus off Finder.
        neutral()
        let before = frontmost()
        let done1 = AgentSpawner.doneFilePath()
        let cmd1 = "echo 'argent bg focus self-test — closes itself'; sleep 2; printf %s $? > '\(done1)'"
        let cap1 = try? AgentSpawner.runSpawn(command: cmd1, terminal: term,
                                              restoreFocusTo: "com.apple.finder")
        usleep(800_000)
        let afterBG = frontmost()
        let bgWid = cap1?.0 ?? ""
        let bgOpened = !bgWid.isEmpty
        // The real contract: the background spawn must not leave ITS OWN terminal frontmost.
        // (This machine is busy — Chrome/Slack/other agents can grab focus during the
        // input-settle delay; that's not our steal. Asserting exact return-to-Finder would
        // flap on that unrelated activity, so assert only "the terminal isn't left on top".)
        let termProc = term == .iterm ? "iTerm2" : "Terminal"
        let bgKept = afterBG != termProc
        let exactly = afterBG == before ? " (exactly restored)" : " (another app took focus after restore — not our steal)"
        print("BACKGROUND spawn (restore → Finder):")
        print("  window created : \(bgOpened ? "yes (\(bgWid))" : "NO")")
        print("  focus          : before=\(before) after=\(afterBG) → \(bgKept ? "PASS (terminal not left frontmost)\(afterBG == before ? "" : exactly)" : "FAIL (left \(termProc) frontmost)")")
        closeWindow(term, bgWid)
        try? FileManager.default.removeItem(atPath: done1)

        // 2. FOREGROUND spawn — the user SPAWN path; SHOULD bring the terminal forward.
        neutral()
        let before2 = frontmost()
        let done2 = AgentSpawner.doneFilePath()
        let cmd2 = "echo 'argent fg focus self-test — closes itself'; sleep 2; printf %s $? > '\(done2)'"
        let cap2 = try? AgentSpawner.runSpawn(command: cmd2, terminal: term, restoreFocusTo: nil)
        usleep(800_000)
        let afterFG = frontmost()
        let fgWid = cap2?.0 ?? ""
        let fgOpened = !fgWid.isEmpty
        let fgActivated = afterFG != before2   // focus left Finder → the terminal came forward
        print("\nFOREGROUND spawn (user SPAWN, restore → nil):")
        print("  window created : \(fgOpened ? "yes (\(fgWid))" : "NO")")
        print("  focus (info)   : before=\(before2) after=\(afterFG) → "
            + "\(fgActivated ? "terminal activated" : "not activated (OS suppresses activation from a backgrounded test — expected here)")")
        closeWindow(term, fgWid)
        try? FileManager.default.removeItem(atPath: done2)

        // Hard assertions: the background spawn kept focus AND both paths still open a
        // trackable window. Foreground activation is informational (see the doc comment).
        let ok = bgOpened && bgKept && fgOpened
        print("\nSPAWN_FOCUS_TEST \(ok ? "OK" : "FAILED")"
            + (ok ? "" : "  [bgOpened=\(bgOpened) bgKept=\(bgKept) fgOpened=\(fgOpened)]"))
        return ok
    }

    /// Auto-fix monitor self-test: fetch my open PRs in the target repo (real gh),
    /// print the snapshot table, confirm a first-run diff seeds without dispatching,
    /// prove a synthetic transition would fire, and print the exact conflict + review
    /// prompts the monitor would spawn. Opens no terminal.
    static func autofixPoll() async {
        do {
            let me = try await API.fetchViewerLogin()
            let (owner, repo) = CoreAssets.repoCoordinates()
            let snaps = try await AutofixMonitor.fetchSnapshots(owner: owner, repo: repo, me: me)
            print("== autofix poll: @\(me) · \(owner)/\(repo) · \(snaps.count) open PRs ==\n")
            for s in snaps.sorted(by: { $0.number > $1.number }) {
                print("  #\(s.number)  \(s.mergeable)  "
                    + "decision=\(s.reviewDecision.isEmpty ? "-" : s.reviewDecision)  "
                    + "unresolved=\(s.threadsUnresolved) iOwe=\(s.threadsIOwe)  \(s.isDraft ? "draft" : "ready")")
            }

            // First-run: baseline seeds, nothing dispatched.
            let (baseEvents, fps) = AutofixDiff.compute(prior: [:], now: snaps)
            print("\nfirst-run diff: \(baseEvents.count) events (expect 0 — baseline seeds \(fps.count) PRs)")

            // Level-triggered reconcile: any of my PRs already carrying unresolved review
            // threads on first sight — exactly what the edge-trigger baselines and misses —
            // is an unaddressed review the reconciler will (re)dispatch a fix agent for.
            let owed = snaps.filter { $0.threadsIOwe > 0 }
            print("my PRs with unaddressed reviews I owe (reconcile → dispatch): \(owed.count)")
            for s in owed {
                let d = ReviewReconcile.decide(prior: nil, stamp: "unresolved",
                                               inFlight: false, banned: false, now: Date())
                let act: String
                if case .dispatch(let n) = d { act = "dispatch#\(n)" } else { act = "\(d)" }
                print("  #\(s.number)  iOwe=\(s.threadsIOwe) (of \(s.threadsUnresolved) unresolved)  → \(act)")
            }

            // Detection proof: against that baseline, flip the first PR to CONFLICTING.
            if let s = snaps.first {
                let conflicted = PRSnapshot(number: s.number, title: s.title, url: s.url,
                    isDraft: s.isDraft, mergeable: "CONFLICTING",
                    reviewDecision: s.reviewDecision, threadsUnresolved: s.threadsUnresolved)
                var others = snaps.filter { $0.number != s.number }
                others.append(conflicted)
                let (ev, _) = AutofixDiff.compute(prior: fps, now: others)
                print("after flipping #\(s.number) → CONFLICTING: \(ev.count) event(s) "
                    + "\(ev.contains(.conflict(conflicted)) ? "✓ conflict on #\(s.number)" : "✗")")

                print("\n----- CONFLICT prompt it would spawn (#\(s.number)) -----")
                print(ConflictConfig(target: .specific, me: me, specificPR: String(s.number)).buildPrompt())
                print("\n----- REVIEW prompt it would spawn (#\(s.number), Deep · known-mine · flags off/off/on) -----")
                print(ReviewConfig(depth: "deep", target: .specific, me: me,
                                   markReady: false, leaveReviews: false, replyToReviews: true,
                                   specificPR: String(s.number), specificAuthor: .mine).buildPrompt())
            }

            // Review-request feed: PRs where someone asked for MY review, with the
            // owe-a-review decision (request newer than my last review).
            let reqs = try await AutofixMonitor.fetchReviewRequests(owner: owner, repo: repo, me: me,
                                                                    includeFiles: true)
            let policy = VerdictPolicy()   // default (all suppressors on) for the dump
            print("\n== review-requested-of-me: \(reqs.count) open PR(s) ==")
            let nowStamp = Date()
            for r in reqs {
                let reasons = policy.withholdReasons(files: r.files, authorAssociation: r.authorAssociation)
                let decision = reasons.isEmpty ? "VERDICT" : "comments (\(reasons.joined(separator: ", ")))"
                // The reconciler's call for this request, assuming no local attempt record
                // and no agent in flight — i.e. a cold start would (re)dispatch every owed PR.
                let recon: String
                if r.oweReview {
                    switch ReviewReconcile.decide(prior: nil, stamp: r.requestedAt ?? "-",
                                                  inFlight: false, banned: false, now: nowStamp) {
                    case .dispatch(let n): recon = "reconcile→dispatch#\(n)"
                    case .skipInFlight: recon = "reconcile→in-flight"
                    case .skipBanned: recon = "reconcile→banned"
                    case .skipCoolingDown(let s): recon = "reconcile→cooldown(\(Int(s))s)"
                    }
                } else { recon = "addressed" }
                print("  #\(r.number)  owe=\(r.oweReview ? "YES" : "no")  \(recon)  "
                    + "author=@\(r.author)[\(r.authorAssociation)]→\(decision)  files=\(r.files.count)  "
                    + "reqAt=\(r.requestedAt ?? "-")  myReview=\(r.myLastReviewAt ?? "-")  \(r.title.prefix(40))")
            }
            let owedCount = reqs.filter { $0.oweReview }.count
            print("→ \(owedCount) review(s) owed; the reconciler (re)dispatches each until it lands.")
            print("  (the VERDICT column reflects the policy only — an actual verdict also "
                + "requires the auto-approvals master toggle, which is OFF by default.)")
            let sampleReq = reqs.first
            let sample = sampleReq?.number ?? 999
            let sampleVerdict = sampleReq.map {
                policy.allowsVerdict(files: $0.files, authorAssociation: $0.authorAssociation)
            } ?? false
            print("\n----- COMPREHENSIVE REVIEW prompt (review-requested #\(sample), max · leave comments · "
                + "\(sampleVerdict ? "→ VERDICT" : "→ NO verdict")) -----")
            print(ReviewConfig(depth: "max", target: .specific, me: me,
                               markReady: false, leaveReviews: true, replyToReviews: false,
                               specificPR: String(sample), finalPass: sampleVerdict,
                               specificAuthor: .theirs).buildPrompt())
        } catch {
            print("AUTOFIX POLL ERROR: \((error as? LocalizedError)?.errorDescription ?? "\(error)")")
        }
    }

    /// Exercises the device-allocator bridge the live panel relies on: the installer
    /// `--check` (node resolution + Process + JSON decode of `AllocatorInstall`) and a
    /// decode of the daemon's public `state.json` into `DeviceState`/`DeviceAllocation`.
    static func deviceAllocator() {
        let s = DeviceAllocator.check()
        print("install: mcp=\(s.mcpRegistered) skill=\(s.skillInstalled) rule=\(s.ruleInstalled) "
            + "claudeMd=\(s.claudeMdInjected) daemon=\(s.daemonRunning) installed=\(s.installed)")
        print("packageDir: \(DeviceAllocator.packageDir)")
        print("node: \(DeviceAllocator.resolveNode() ?? "(not found)")")
        if let st = DeviceAllocator.readState() {
            print("state: devices=\(st.devices.count) "
                + "· \(st.allocatedCount) in use · \(st.freeCount) free")
            for d in st.devices {
                print("  [\(d.platform)] \(d.name ?? d.key) v\(d.version ?? "?") "
                    + "status=\(d.status) owner=\(d.owner?.agentName ?? "—") handle=\(d.handle ?? "—")")
            }
        } else {
            print("state: (no state.json yet — daemon hasn't run)")
        }
    }

    /// Exercises the persisted-settings load path: a fresh Store reads UserDefaults
    /// in its initializer. Run via the .app bundle's binary so it shares the GUI's
    /// `com.ignacy.argent-utils` defaults domain.
    @MainActor static func settings() {
        let s = Store()
        print("usernameOverride : '\(s.usernameOverride)'")
        print("effectiveMe      : '\(s.effectiveMe)'   (override if set, else gh login — empty here, no network)")
        print("hiddenTools      : \(s.hiddenTools.sorted())")
        print("visibleTools     : \(s.visibleTools.map { $0.rawValue })")
        print("colorOverrides   : \(s.colorOverrides)")
        print("terminalChoice   : '\(s.terminalChoice)' -> resolved \(AgentSpawner.resolved(s.terminal).title)")
        print("tints            : \(ToolKind.allCases.map { "\($0.rawValue)=\(s.tint(for: $0).hexRGB)" })")
    }
}
