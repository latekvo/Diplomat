import SwiftUI
import CoMaintainerCore

/// The wizards' "⬡ Run on mesh" row — the SwiftUI counterpart of the Linux
/// front-end's `meshspawn.MeshSpawnRow`, with the same semantics: shown only while
/// the mesh can actually route (enabled AND a live local node), checked by default,
/// and SPAWN then hands the job to the mesh instead of opening a local terminal.
/// The caption previews where the duty currently routes, so the destination is
/// legible before clicking.
struct MeshSpawnRow: View {
    @EnvironmentObject var store: Store
    /// The duty this wizard dispatches ("review" / "conflicts" / "audit").
    let duty: String
    /// The wizard-owned toggle; effective only while `isLive`.
    @Binding var useMesh: Bool

    /// Mesh routing is offered only when it can route: enabled + live node. The row
    /// hides entirely otherwise (mirrors the Qt row's `_sync`).
    static func isLive(_ store: Store) -> Bool {
        store.meshEnabled && MeshBridge.nodeRunning(store.meshState)
    }

    var body: some View {
        if Self.isLive(store) {
            HStack(spacing: 8) {
                Toggle(isOn: $useMesh) {
                    HStack(spacing: 5) {
                        Image(systemName: "hexagon").font(.system(size: 10))
                        Text("Run on mesh").font(.system(size: 11, weight: .semibold))
                    }
                }
                .toggleStyle(.checkbox)
                Spacer(minLength: 4)
                Text(destination)
                    .font(.system(size: 10)).foregroundStyle(.secondary)
                    .lineLimit(1).truncationMode(.tail)
            }
            .padding(.horizontal, 9).padding(.vertical, 6)
            .background(RoundedRectangle(cornerRadius: 7).fill(Color.gray.opacity(0.08)))
            .help("Hand the job to the Co-Maintainer Mesh: the node picks the machine per this duty's "
                  + "placement policy (with failover) instead of always spawning locally.")
        }
    }

    /// "→ node, node" for the duty's current assignment, plus a "⚠ missing N×🐧"
    /// warning per unfilled platform slot — mirrors `_destination_preview`.
    private var destination: String {
        guard let state = store.meshState else { return "" }
        var names: [String: String] = [:]
        if let s = state.selfNode { names[s.id] = s.name }
        for p in state.peers { names[p.id] = p.name }
        var parts: [String] = []
        let assignment = state.assignments[duty]
        for nid in assignment?.assigned ?? [] {
            parts.append(names[nid] ?? String(nid.prefix(6)))
        }
        let catalog = try? CoreAssets.mesh()
        for sf in assignment?.shortfall ?? [] {
            let glyph = catalog?.platform(sf.platform)?.emoji ?? sf.platform
            parts.append("⚠ missing \(sf.missing)×\(glyph)")
        }
        return parts.isEmpty ? "∅ no eligible node" : "→ " + parts.joined(separator: ", ")
    }
}

enum MeshSpawn {
    /// One status-label line from the dispatch's per-slot results — mirrors the Qt
    /// row's `summarize`: "Mesh: ✓ node · ✗ node (reason)".
    static func summarize(_ results: [[String: Any]], error: String?) -> String {
        if let error { return "Mesh dispatch failed: \(error)" }
        if results.isEmpty { return "Mesh dispatch failed: no result" }
        let bits = results.map { r -> String in
            let name = (r["nodeName"] as? String)
                ?? String(((r["node"] as? String) ?? "?").prefix(6))
            if (r["status"] as? String) == "spawned" { return "✓ \(name)" }
            let reason = (r["reason"] as? String) ?? ((r["status"] as? String) ?? "?")
            return "✗ \(name) (\(reason))"
        }
        return "Mesh: " + bits.joined(separator: " · ")
    }
}
