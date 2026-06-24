import SwiftUI
import AppKit
import ArgentUtilsCore

@main
struct ArgentUtilsApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var store = Store()

    var body: some Scene {
        MenuBarExtra("Argent Utils", systemImage: "wrench.and.screwdriver") {
            ContentView()
                .environmentObject(store)
                .frame(width: 470, height: 600)
        }
        .menuBarExtraStyle(.window)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        let env = ProcessInfo.processInfo.environment
        let headless = env["ARGENT_UTILS_DUMP"] == "1"
            || env["ARGENT_UTILS_LOOKUP"] != nil
            || env["ARGENT_UTILS_PRINT_PROMPT"] != nil
            || env["ARGENT_UTILS_SETTINGS_DUMP"] == "1"
            || env["ARGENT_UTILS_RENDER"] != nil

        // Singleton, newest-wins: a freshly launched GUI instance kills any older
        // ones so there's never more than one wrench. Skipped in headless self-test
        // mode so a dump/lookup run can't kill the live menu-bar app.
        if !headless {
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
                let preferred = SpawnTerminal(rawValue: defaults.string(forKey: "terminalChoice") ?? "") ?? .iterm
                AgentSpawner.triggerAutomationPrompt(preferred: preferred)
            }
        }

        // Menu-bar-only: no Dock icon.
        NSApp.setActivationPolicy(.accessory)

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
        // Headless UI render: snapshot a view to PNG and exit.
        if let what = env["ARGENT_UTILS_RENDER"] {
            Task { @MainActor in Render.run(what, store: Store()); exit(0) }
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
            let cmd = AgentSpawner.shellCommand(promptFile: file)
            print("\n----- SHELL COMMAND -----")
            print(cmd)
            print("\n----- APPLESCRIPT (\(AgentSpawner.resolved(.iterm).title)) -----")
            print(AgentSpawner.appleScript(for: AgentSpawner.resolved(.iterm), shellCommand: cmd))
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
            let cmd = AgentSpawner.shellCommand(promptFile: file)
            print("\n----- SHELL COMMAND -----")
            print(cmd)
            print("\n----- APPLESCRIPT (\(AgentSpawner.resolved(.iterm).title)) -----")
            print(AgentSpawner.appleScript(for: AgentSpawner.resolved(.iterm), shellCommand: cmd))
            try? FileManager.default.removeItem(at: file)
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
