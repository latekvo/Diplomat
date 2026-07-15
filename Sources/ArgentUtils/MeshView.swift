import SwiftUI
import ArgentUtilsCore

/// The Mesh management screen — the macOS face of Argent Mesh, and the SwiftUI
/// counterpart of the Linux front-end's `meshview.py`. One of the panel's three
/// screens (Actions · Mesh · Settings), swapped in for the main body when the header
/// ⬡ is tapped.
///
/// It renders the local node's public topology snapshot (`store.meshState`): a compact
/// wire graph of self + peers, one editable card per node (tier / token state — edits
/// apply to any node, self or peer, forwarded over the mesh so one machine configures
/// the fleet), and the duty table (which job classes route where, with a live per-duty
/// placement policy the panel edits and the mesh gossips last-writer-wins). Reads are a
/// polled file decode; the write paths call the `store.mesh*` control wrappers.
struct MeshView: View {
    @EnvironmentObject var store: Store
    @Binding var isPresented: Bool

    /// The shared mesh model (duty catalog, strategies, tier/token vocabulary). Cached in
    /// CoreAssets, so `try?` here is cheap.
    private var catalog: MeshCatalog? { try? CoreAssets.mesh() }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            header
            if let err = store.meshError { errorLine(err) }
            content
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .task { if store.meshEnabled { await store.meshTick() } }
    }

    // MARK: header

    private var header: some View {
        // Live node count + status between the title and Done, matching the Linux screen.
        let running = MeshBridge.nodeRunning(store.meshState)
        let peers = store.meshState?.peers.count ?? 0
        let (dot, label): (Color, String) = {
            if !store.meshEnabled { return (.gray, "off") }
            if store.meshState == nil { return (.orange, "starting") }
            if !running { return (.red, "node dead") }
            return (.green, "live")
        }()
        return HStack(spacing: 6) {
            Image(systemName: "hexagon.fill").foregroundStyle(.secondary)
            Text("Mesh").font(.subheadline.bold())
            if store.meshEnabled, running {
                Text("\(1 + peers)").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            }
            Spacer()
            Circle().fill(dot).frame(width: 7, height: 7)
            Text(label).font(.caption2).foregroundStyle(.secondary)
            Button { withAnimation(.easeInOut(duration: 0.15)) { isPresented = false } } label: {
                Text("Done").bold()
            }
            .buttonStyle(.borderless)
            .keyboardShortcut(.cancelAction)
        }
    }

    private func errorLine(_ msg: String) -> some View {
        Text("⚠ \(msg)")
            .font(.system(size: 10)).foregroundStyle(.red)
            .fixedSize(horizontal: false, vertical: true)
    }

    // MARK: content states

    @ViewBuilder
    private var content: some View {
        if !store.meshEnabled {
            emptyState("hexagon", "Mesh is off",
                       "Enable it in ⚙︎ Settings to coordinate duties with other machines on this LAN.",
                       action: nil)
        } else if store.meshState == nil {
            emptyState("hourglass", "Starting mesh node…",
                       "Discovering peers on the LAN.", action: nil)
        } else if !MeshBridge.nodeRunning(store.meshState) {
            emptyState("xmark.octagon", "Mesh node not running.",
                       "The node process is gone. Start it to rejoin the mesh.",
                       action: ("Start", { store.ensureMeshRunning() }))
        } else {
            live
        }
    }

    private func emptyState(_ symbol: String, _ title: String, _ detail: String,
                            action: (title: String, run: () -> Void)?) -> some View {
        VStack(spacing: 8) {
            Image(systemName: symbol).font(.system(size: 30)).foregroundStyle(.secondary)
            Text(title).font(.subheadline.bold())
            Text(detail).font(.caption).foregroundStyle(.secondary)
                .multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
            if let action {
                Button(action.title) { action.run() }
                    .buttonStyle(.borderedProminent).controlSize(.small)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 28)
    }

    // MARK: live topology

    @ViewBuilder
    private var live: some View {
        let selfNode = store.meshState?.selfNode
        let peers = store.meshState?.peers ?? []
        VStack(alignment: .leading, spacing: 8) {
            TopologyGraph(selfNode: selfNode, peers: peers, platformMeta: platformMeta)
                .frame(height: 190)
                .frame(maxWidth: .infinity)
                .padding(6)
                .background(RoundedRectangle(cornerRadius: 8).fill(Color.gray.opacity(0.07)))

            HStack(alignment: .top, spacing: 12) {
                nodesColumn(selfNode: selfNode, peers: peers)
                    .frame(maxWidth: .infinity, alignment: .topLeading)
                dutiesColumn(selfNode: selfNode, peers: peers)
                    .frame(maxWidth: .infinity, alignment: .topLeading)
            }
        }
    }

    // MARK: nodes column

    private func nodesColumn(selfNode: MeshNode?, peers: [MeshPeer]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            sectionLabel("NODES")
            if let s = selfNode {
                nodeCard(id: s.id, name: s.name, platform: s.platform, tier: s.tier,
                         tokens: s.tokens, link: nil, addr: nil)
            }
            ForEach(peers.sorted { $0.name.lowercased() < $1.name.lowercased() }, id: \.id) { p in
                nodeCard(id: p.id, name: p.name, platform: p.platform, tier: p.tier,
                         tokens: p.tokens, link: p.link, addr: p.addr, lastSeen: p.lastSeenSecsAgo)
            }
        }
        .padding(7)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.gray.opacity(0.07)))
    }

    private func nodeCard(id: String, name: String, platform: String, tier: Int, tokens: String,
                          link: String?, addr: String?, lastSeen: Double? = nil) -> some View {
        let meta = platformMeta(platform)
        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(meta.glyph).font(.system(size: 13))
                    .frame(width: 20, height: 20)
                    .background(RoundedRectangle(cornerRadius: 5).fill(meta.color.opacity(0.2)))
                Text(name).font(.caption).lineLimit(1)
                Spacer(minLength: 4)
                if link == nil {
                    badge("self", .green)
                } else {
                    let ago = lastSeen.map { " \(Int($0))s" } ?? ""
                    badge("\(link ?? "down")\(ago)", linkColor(link ?? "down"))
                }
            }
            if let addr, !addr.isEmpty {
                Text(addr).font(.system(size: 9, design: .monospaced)).foregroundStyle(.secondary)
            }
            HStack(spacing: 10) {
                tierEditor(nodeID: id, tier: tier)
                tokenEditor(nodeID: id, tokens: tokens)
                Spacer(minLength: 0)
            }
        }
        .padding(6)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.06)))
    }

    private func tierEditor(nodeID: String, tier: Int) -> some View {
        let bounds = catalog?.tierBounds ?? (min: 1, max: 5, default: 3)
        return HStack(spacing: 3) {
            Button { store.meshSetAttr(nodeID: nodeID, attrs: ["tier": max(bounds.min, tier - 1)]) } label: {
                Image(systemName: "minus").font(.system(size: 9, weight: .bold))
            }
            .buttonStyle(.borderless).disabled(tier <= bounds.min)
            Text("tier \(tier)").font(.system(size: 10, design: .monospaced))
                .help("Machine strength (1 = strongest)")
            Button { store.meshSetAttr(nodeID: nodeID, attrs: ["tier": min(bounds.max, tier + 1)]) } label: {
                Image(systemName: "plus").font(.system(size: 9, weight: .bold))
            }
            .buttonStyle(.borderless).disabled(tier >= bounds.max)
        }
    }

    private func tokenEditor(nodeID: String, tokens: String) -> some View {
        let ids = catalog?.tokens.map { $0.id } ?? ["ok", "low", "out"]
        return Picker("", selection: Binding(
            get: { tokens },
            set: { newValue in
                if newValue != tokens { store.meshSetAttr(nodeID: nodeID, attrs: ["tokens": newValue]) }
            }
        )) {
            ForEach(ids, id: \.self) { tid in
                let m = tokenMeta(tid)
                Text("\(m.glyph) \(tid)").tag(tid)
            }
        }
        .labelsHidden()
        .pickerStyle(.menu)
        .frame(width: 96)
        .help("Token budget state — the mesh routes around 'out' nodes")
    }

    // MARK: duties column

    private func dutiesColumn(selfNode: MeshNode?, peers: [MeshPeer]) -> some View {
        // id → readable name, for turning assigned ids into names.
        var idToName: [String: String] = [:]
        if let s = selfNode { idToName[s.id] = s.name }
        for p in peers { idToName[p.id] = p.name }
        let assignments = store.meshState?.assignments ?? [:]
        let overrides = store.meshState?.overrides
        return VStack(alignment: .leading, spacing: 6) {
            sectionLabel("DUTIES")
            ForEach(catalog?.duties ?? [], id: \.id) { duty in
                dutyCard(duty: duty, assignment: assignments[duty.id],
                         placement: catalog?.placement(for: duty.id, overrides: overrides)
                            ?? MeshPlacement(strategy: "weakest-first", tokenAware: true, spread: []),
                         idToName: idToName)
            }
        }
        .padding(7)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.gray.opacity(0.07)))
    }

    private func dutyCard(duty: MeshCatalog.Duty, assignment: MeshAssignment?,
                          placement: MeshPlacement, idToName: [String: String]) -> some View {
        let tint = Color(hex: duty.colorHex) ?? .gray
        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(duty.emoji).font(.system(size: 13))
                Text(duty.title).font(.caption.bold())
                Spacer()
            }
            // Assigned → names, or empty / shortfall warning.
            if let a = assignment, !a.assigned.isEmpty {
                Text("→ " + a.assigned.map { idToName[$0] ?? String($0.prefix(6)) }.joined(separator: ", "))
                    .font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(2)
            } else if (assignment?.shortfall ?? []).isEmpty {
                Text("∅ nobody").font(.system(size: 10)).foregroundStyle(.secondary)
            }
            if let short = assignment?.shortfall, !short.isEmpty {
                Text(short.map { "⚠ missing \($0.missing)×\(platformMeta($0.platform).glyph)" }
                        .joined(separator: " · "))
                    .font(.system(size: 9, weight: .semibold)).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // Spread (static — spread editing is out of scope, matching the Linux screen).
            if !placement.spread.isEmpty {
                Text("spread: " + placement.spread.map { "\($0.count)×\(platformMeta($0.platform).glyph)" }
                        .joined(separator: "+"))
                    .font(.system(size: 9, design: .monospaced)).foregroundStyle(.secondary)
            }
            // Policy editors: strategy picker + token-aware toggle.
            HStack(spacing: 6) {
                strategyPicker(dutyID: duty.id, placement: placement)
                Toggle(isOn: Binding(
                    get: { placement.tokenAware },
                    set: { editPlacement(dutyID: duty.id, current: placement, tokenAware: $0) }
                )) { Text("token-aware").font(.system(size: 9)) }
                    .toggleStyle(.switch).controlSize(.mini)
                    .help("Skip nodes that are out of tokens when routing")
            }
        }
        .padding(6)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.06)))
        .overlay(RoundedRectangle(cornerRadius: 6).stroke(tint.opacity(0.25), lineWidth: 1))
    }

    private func strategyPicker(dutyID: String, placement: MeshPlacement) -> some View {
        let strategies = catalog?.strategies ?? []
        return Picker("", selection: Binding(
            get: { placement.strategy },
            set: { editPlacement(dutyID: dutyID, current: placement, strategy: $0) }
        )) {
            ForEach(strategies, id: \.id) { s in
                Text(s.title).tag(s.id).help(s.detail)
            }
        }
        .labelsHidden()
        .pickerStyle(.menu)
        .frame(maxWidth: 150)
    }

    /// Push one placement edit to the mesh (LWW-gossiped). Spread is preserved; only the
    /// strategy / token-awareness the panel exposes can change (mirrors `_edit_placement`).
    private func editPlacement(dutyID: String, current: MeshPlacement,
                               strategy: String? = nil, tokenAware: Bool? = nil) {
        var next = current
        if let strategy { next.strategy = strategy }
        if let tokenAware { next.tokenAware = tokenAware }
        store.meshSetOverrides(duty: dutyID, placement: next)
    }

    // MARK: helpers

    private func sectionLabel(_ text: String) -> some View {
        Text(text).font(.system(size: 9, weight: .bold)).foregroundStyle(.secondary).kerning(0.5)
    }

    private func badge(_ text: String, _ color: Color) -> some View {
        Text(text).font(.system(size: 8, weight: .bold)).foregroundStyle(color)
            .padding(.horizontal, 5).padding(.vertical, 1)
            .background(Capsule().fill(color.opacity(0.15)))
    }

    /// (emoji, tint) for a platform id, from the shared model. Falls back to a neutral
    /// node glyph for an unknown platform.
    private func platformMeta(_ id: String) -> (glyph: String, color: Color) {
        if let p = catalog?.platform(id) { return (p.emoji, Color(hex: p.colorHex) ?? .gray) }
        return ("⬡", .gray)
    }

    private func tokenMeta(_ id: String) -> (glyph: String, color: Color) {
        if let t = catalog?.token(id) { return (t.emoji, Color(hex: t.colorHex) ?? .gray) }
        return ("●", .gray)
    }

    private func linkColor(_ s: String) -> Color {
        switch s {
        case "up":    return .green
        case "stale": return .orange
        case "down":  return .red
        default:      return .gray
        }
    }
}

// MARK: - Wire graph

/// A compact node-link diagram: self centred, peers on a ring, links coloured by state;
/// peer↔peer edges (from each peer's `sees` list) drawn thin/gray when both ends agree.
/// Purely presentational — the SwiftUI/`Canvas` port of `meshview.TopologyGraph`.
private struct TopologyGraph: View {
    let selfNode: MeshNode?
    let peers: [MeshPeer]
    let platformMeta: (String) -> (glyph: String, color: Color)

    var body: some View {
        Canvas { ctx, size in
            guard let selfNode else { return }
            let cx = size.width / 2, cy = size.height / 2
            let radius = min(size.width * 0.33, size.height / 2 - 30)
            var points: [CGPoint] = []
            let n = max(peers.count, 1)
            for i in 0..<peers.count {
                let ang = -Double.pi / 2 + (2 * Double.pi * Double(i) / Double(n))
                points.append(CGPoint(x: cx + radius * cos(ang), y: cy + radius * sin(ang)))
            }
            var idToPoint: [String: CGPoint] = [selfNode.id: CGPoint(x: cx, y: cy)]
            for (peer, pt) in zip(peers, points) { idToPoint[peer.id] = pt }

            // peer↔peer edges first (behind), gray; brighter when the sighting is mutual.
            var seen = Set<Set<String>>()
            for peer in peers {
                for other in peer.sees where other != selfNode.id && idToPoint[other] != nil {
                    let pair: Set<String> = [peer.id, other]
                    if seen.contains(pair) { continue }
                    seen.insert(pair)
                    let mutual = peers.first { $0.id == other }?.sees.contains(peer.id) ?? false
                    var path = Path()
                    path.move(to: idToPoint[peer.id]!)
                    path.addLine(to: idToPoint[other]!)
                    ctx.stroke(path, with: .color(.gray.opacity(mutual ? 0.6 : 0.28)), lineWidth: 1)
                }
            }
            // self→peer links, coloured by link state.
            for (peer, pt) in zip(peers, points) {
                var path = Path()
                path.move(to: CGPoint(x: cx, y: cy))
                path.addLine(to: pt)
                let color = linkColor(peer.link)
                let style = StrokeStyle(lineWidth: peer.link == "up" ? 2 : 1.6,
                                        dash: peer.link == "down" ? [4, 3] : [])
                ctx.stroke(path, with: .color(color), style: style)
            }
            // nodes on top.
            drawNode(ctx, at: CGPoint(x: cx, y: cy), node: selfNode.platform, name: selfNode.name,
                     isSelf: true, height: size.height)
            for (peer, pt) in zip(peers, points) {
                drawNode(ctx, at: pt, node: peer.platform, name: peer.name, isSelf: false,
                         height: size.height)
            }
        }
    }

    private func drawNode(_ ctx: GraphicsContext, at p: CGPoint, node platform: String,
                          name: String, isSelf: Bool, height: CGFloat) {
        let meta = platformMeta(platform)
        let r: CGFloat = isSelf ? 15 : 12
        let rect = CGRect(x: p.x - r, y: p.y - r, width: r * 2, height: r * 2)
        ctx.fill(Circle().path(in: rect), with: .color(meta.color.opacity(isSelf ? 1 : 0.82)))
        ctx.stroke(Circle().path(in: rect),
                   with: .color(isSelf ? .white : .black.opacity(0.35)), lineWidth: isSelf ? 2 : 1)
        ctx.draw(Text(meta.glyph).font(.system(size: r * 0.95)), at: p)
        // Name label: above the disc for ring nodes in the top half, else below.
        let above = !isSelf && p.y < height / 2 - 1
        let labelY = above ? p.y - r - 8 : p.y + r + 8
        // `.foregroundColor` (not `.foregroundStyle`) so this stays a `Text`, which is
        // what `GraphicsContext.draw(_:at:)` takes.
        ctx.draw(Text(name).font(.system(size: 9, weight: isSelf ? .bold : .regular))
                    .foregroundColor(isSelf ? Color(white: 0.9) : Color(white: 0.65)),
                 at: CGPoint(x: p.x, y: labelY))
    }

    private func linkColor(_ s: String) -> Color {
        switch s {
        case "up":    return .green
        case "stale": return .orange
        case "down":  return .red
        default:      return .gray
        }
    }
}
