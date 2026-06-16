import SwiftUI
import AppKit

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
        let headless = env["ARGENT_UTILS_DUMP"] == "1" || env["ARGENT_UTILS_LOOKUP"] != nil

        // Singleton, newest-wins: a freshly launched GUI instance kills any older
        // ones so there's never more than one wrench. Skipped in headless self-test
        // mode so a dump/lookup run can't kill the live menu-bar app.
        if !headless {
            SingleInstance.terminateOthers()
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
            let prs = try await API.fetchOpenPRs()
            let issues = try await API.fetchOpenIssues()
            print("== open PRs: \(prs.count) · open issues: \(issues.count) ==\n")

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
        } catch {
            let msg = (error as? LocalizedError)?.errorDescription ?? "\(error)"
            print("DUMP ERROR: \(msg)")
        }
    }

    /// Exercises the reverse-lookup path through the real Store logic.
    static func lookup(_ n: Int) async {
        do {
            let prs = try await API.fetchOpenPRs()
            let issues = try await API.fetchOpenIssues()
            let r = await MainActor.run { () -> LookupResult in
                let s = Store()
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
}
