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
            .frame(width: 470)
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
        default: // "panel" — the whole content view
            ContentView().frame(height: 580)
        }
    }
}
