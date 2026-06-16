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
        // Menu-bar-only: no Dock icon.
        NSApp.setActivationPolicy(.accessory)

        // Headless self-test: `ARGENT_UTILS_DUMP=1 swift run` runs the real
        // fetch+filter pipeline, prints results, and exits. Used for verification.
        if ProcessInfo.processInfo.environment["ARGENT_UTILS_DUMP"] == "1" {
            Task { await Dump.run(); exit(0) }
        }
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
}
