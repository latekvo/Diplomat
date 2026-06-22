import SwiftUI
import AppKit

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
        switch what.lowercased() {
        case "settings":
            SettingsView(isPresented: .constant(true)).frame(height: 560)
        case "wizard":
            ActionsPanel(startExpanded: true)
        default: // "panel" — the whole content view
            ContentView().frame(height: 580)
        }
    }
}
