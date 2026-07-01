import SwiftUI
import AppKit
import ArgentUtilsCore

/// Headless UI render: snapshot a view to a PNG with `ImageRenderer`, no menu-bar
/// popover required. A deterministic "headless UI check" in the spirit of the
/// existing dump/lookup self-tests. Driven by `ARGENT_UTILS_RENDER=<what>` and
/// `ARGENT_UTILS_RENDER_OUT=<path>` (defaults under the temp dir).
@MainActor
enum Render {
    static func run(_ what: String, store: Store) {
        let out = ProcessInfo.processInfo.environment["ARGENT_UTILS_RENDER_OUT"]
            ?? FileManager.default.temporaryDirectory.appendingPathComponent("argent-utils-\(what).png").path

        let body = view(for: what, store: store)
        let content = body
            .environmentObject(store)
            .frame(width: PopoverRoot.width)
            .padding(10)
            .background(Color(nsColor: .windowBackgroundColor))

        let renderer = ImageRenderer(content: content)
        renderer.scale = 2
        guard let cg = renderer.cgImage else { print("RENDER ERROR: nil cgImage"); return }
        let rep = NSBitmapImageRep(cgImage: cg)
        guard let data = rep.representation(using: .png, properties: [:]) else {
            print("RENDER ERROR: PNG encode failed"); return
        }
        do {
            try data.write(to: URL(fileURLWithPath: out))
            print("rendered \(what) -> \(out)  (\(cg.width)x\(cg.height))")
        } catch {
            print("RENDER ERROR: \(error)")
        }
    }

    @ViewBuilder
    private static func view(for what: String, store: Store) -> some View {
        let w = what.lowercased()
        switch w {
        case "settings-live":
            // The whole panel with Settings open AND sessions + devices seeded —
            // proves both are hidden while Settings is shown (regression guard).
            let _ = seedProcessesIfNeeded("procs", store: store)
            let _ = seedDeviceState(store)
            ContentView(showSettings: true)
        case "settings":
            SettingsView(isPresented: .constant(true)).frame(height: 560)
        case let s where s.hasPrefix("wizard"):
            // Suffix-driven states: "wizard" (mine), "-other" (someone else's →
            // handle field), "-specific" (specific PR → PR field), "-wrong"
            // (specific PR with a URL pointing at another repo → warning).
            let wrong = s.contains("wrong")
            let specific = wrong || s.contains("specific") || s.contains("single")
            let other = s.contains("other")
            let target: PRTarget = specific ? .specific : (other ? .someone : .mine)
            let pr = wrong ? "https://github.com/some-org/other-repo/pull/42"
                           : "https://github.com/software-mansion/argent/pull/337"
            ReviewWizardView(scrolls: false,
                             seedTarget: target,
                             seedSpecificPR: specific ? pr : nil,
                             seedUsername: other ? "octocat" : nil)
                .frame(height: 560)
        case "conflicts":
            ConflictWizardView(scrolls: false).frame(height: 560)
        case let s where s.hasPrefix("audit"):
            // Suffix-driven toggles: "-issues" pre-checks fix-open-issues, "-prs"
            // pre-checks open-PRs, "-all" both — so each state can be eyeballed.
            AuditWizardView(scrolls: false,
                            seedFixIssues: s.contains("issues") || s.contains("all"),
                            seedOpenPRs: s.contains("prs") || s.contains("all"))
                .frame(height: 560)
        case let s where s.hasPrefix("devices"):
            // Seed a synthetic device pool (and optionally sessions) so the Devices
            // section can be eyeballed: allocated iOS + booting Android (with held
            // durations), a device under repair, and free devices. Natural height.
            // "devices-open" renders the section standalone with BOTH groups expanded
            // (so the collapsed-by-default Free rows are visible); plain "devices"
            // shows the whole panel with Free collapsed as it ships.
            let _ = seedProcessesIfNeeded(s, store: store)
            let _ = seedDeviceState(store)
            if s.contains("open"), let ds = store.deviceState {
                DevicesView(ds: ds, tracked: [],
                            seedInUseExpanded: true, seedFreeExpanded: true)
            } else {
                ContentView()
            }
        case let s where s.hasPrefix("natural"):
            // No forced height — the rendered PNG's height IS ContentView's natural
            // height, proving the content sizes to its content (what PopoverRoot caps).
            let _ = seedProcessesIfNeeded(s, store: store)
            ContentView()
        default: // "panel" — the whole content view; "panel-procs" seeds the
                 // ongoing-sessions list (persist is suppressed in render mode).
            let _ = seedProcessesIfNeeded(what, store: store)
            let _ = seedAutofix(store)
            ContentView().frame(height: 580)
        }
    }

    /// For `ARGENT_UTILS_RENDER=panel-procs`, inject a couple of fake tracked
    /// sessions so the ongoing-sessions list can be eyeballed. No-op otherwise.
    @MainActor
    private static func seedProcessesIfNeeded(_ what: String, store: Store) -> Bool {
        guard what.lowercased().contains("proc") else { return false }
        store.processes = [
            TrackedProcess(kind: "review", label: "Review · #337 · Deep", terminal: "iterm",
                           windowID: "1", sessionID: "a", tty: "/dev/ttys991", donePath: "",
                           prURL: "https://github.com/software-mansion/argent/pull/337",
                           createdAt: Date(), done: false),
            TrackedProcess(kind: "conflicts", label: "Resolve · my PRs", terminal: "iterm",
                           windowID: "2", sessionID: "b", tty: "/dev/ttys992", donePath: "",
                           prURL: nil, createdAt: Date(), done: true),
            TrackedProcess(kind: "review", label: "Review · #312 · Standard", terminal: "iterm",
                           windowID: "3", sessionID: "c", tty: "/dev/ttys993", donePath: "",
                           prURL: "https://github.com/software-mansion/argent/pull/312",
                           createdAt: Date(), done: true, merged: true),
        ]
        return true
    }

    /// A LIVE auto-fix heartbeat so the top-of-panel status pill renders "active".
    @MainActor
    private static func seedAutofix(_ store: Store) {
        store.prAutofixEnabled = true
        store.autofixStatus = AutofixStatus(
            updatedAt: Date(), enabled: true, watching: 28,
            conflictsResolved: 3, reviewsAddressed: 2)
    }

    /// Synthetic device-allocator state for `ARGENT_UTILS_RENDER=devices`.
    /// In-use devices get an `allocatedAt` in the recent past so the "held" duration
    /// renders; free devices populate the (collapsed-by-default) Free section.
    @MainActor
    private static func seedDeviceState(_ store: Store) {
        seedAutofix(store)
        let nowMs = Date().timeIntervalSince1970 * 1000
        func ago(_ minutes: Double) -> Double { nowMs - minutes * 60_000 }
        store.deviceState = DeviceState(devices: [
            DeviceAllocation(
                key: "ios:99AD", platform: "ios", name: "iPhone 16 Pro Max", version: "18.5",
                apiVersion: "18", handle: "99AD1D87-DA5F", status: "ready",
                owner: DeviceOwner(agentName: "bluesky e2e", ownerPid: 4242),
                allocatedAt: ago(12), idleMs: 240_000, brokenReason: nil, repairLog: nil, format: "phone"),
            DeviceAllocation(
                key: "android:Pixel_6_API_34", platform: "android", name: "Pixel_6_API_34",
                version: "14", apiVersion: "34", handle: "emulator-5554", status: "booting",
                owner: DeviceOwner(agentName: "checkout flow", ownerPid: 4310),
                allocatedAt: ago(83), idleMs: nil, brokenReason: nil, repairLog: nil, format: "phone"),
            DeviceAllocation(
                key: "appletv:ATV1", platform: "apple-tv", name: "Apple TV 4K", version: "17.5",
                apiVersion: "17", handle: nil, status: "repairing",
                owner: DeviceOwner(agentName: "repair", ownerPid: nil),
                allocatedAt: nil, idleMs: nil, brokenReason: "boot timeout", repairLog: "/tmp/r.log", format: nil),
            DeviceAllocation(
                key: "ios:FREE1", platform: "ios", name: "iPad Pro", version: "18.5",
                apiVersion: "18", handle: nil, status: "free",
                owner: nil, allocatedAt: nil, idleMs: nil, brokenReason: nil, repairLog: nil, format: "tablet"),
            DeviceAllocation(
                key: "android:Pixel_7_API_35", platform: "android", name: "Pixel_7_API_35",
                version: "15", apiVersion: "35", handle: nil, status: "free",
                owner: nil, allocatedAt: nil, idleMs: nil, brokenReason: nil, repairLog: nil, format: "phone"),
        ])
    }
}
