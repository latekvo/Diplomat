import SwiftUI
import DiplomatCore

/// The Mesh management screen — the macOS face of Diplomat Mesh, and the SwiftUI
/// counterpart of the Linux front-end's `meshview.py`. One of the panel's three
/// screens (Actions · Mesh · Settings), swapped in for the main body when the header
/// ⬡ is tapped.
///
/// It renders the local node's public topology snapshot (`store.meshState`): a compact
/// wire graph of self + peers, one editable card per node (machine strength in words +
/// an auto-measured token budget + a Personal/Foreign trust toggle — edits apply to any
/// node, self or peer, forwarded over the mesh so one machine configures the fleet), and
/// the duty table (which job classes route where, with a live per-duty placement policy
/// the panel edits and the mesh gossips last-writer-wins). Reads are a polled file decode;
/// the write paths call the `store.mesh*` control wrappers.
struct MeshView: View {
    @EnvironmentObject var store: Store
    @Binding var isPresented: Bool

    /// Set to a device's name to raise the one-time reminder after the user marks a
    /// new device Personal — trust is directional, so the *other* machine must trust
    /// this one back for the reverse direction to work.
    @State private var trustReminderPeer: String?
    /// The reminder modal's "Don't show again" checkbox (applied on dismiss).
    @State private var suppressTrustReminder = false

    /// `seedTrustReminder` pre-opens the reminder modal for a headless render self-test
    /// (`DIPLOMAT_RENDER=mesh-reminder`); nil in normal use.
    init(isPresented: Binding<Bool>, seedTrustReminder: String? = nil) {
        self._isPresented = isPresented
        self._trustReminderPeer = State(initialValue: seedTrustReminder)
    }

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
        // An in-popover overlay, NOT a `.alert`/`.sheet`: a system dialog steals key-window
        // focus from the MenuBarExtra popover, which then auto-closes on dismiss (the "OK
        // closes the applet" bug). Drawing the modal inside the same window keeps the
        // popover open and lets it carry a "Don't show again" checkbox.
        .overlay {
            if let name = trustReminderPeer { trustReminderModal(name) }
        }
    }

    // MARK: trust reminder modal

    /// The "marked Personal — trust the other side too" reminder, as an in-popover card
    /// over a dimming scrim. Dismiss (OK or tap-outside) applies the "Don't show again"
    /// choice. Trust is one-directional, so this nudges the user to set the reverse.
    private func trustReminderModal(_ peerName: String) -> some View {
        ZStack {
            Color.black.opacity(0.28).ignoresSafeArea()
                .onTapGesture { dismissTrustReminder() }
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 6) {
                    Image(systemName: "checkmark.shield.fill")
                        .foregroundStyle(trustColor("personal"))
                    Text("Marked as Personal").font(.headline)
                }
                Text("“\(peerName)” can now run your requests directly on this machine.\n\n"
                     + "Trust is one-directional: for it to run *your* requests, set this "
                     + "machine to Personal on that device too.")
                    .font(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Toggle(isOn: $suppressTrustReminder) {
                    Text("Don't show again").font(.callout)
                }
                .toggleStyle(.checkbox)
                HStack {
                    Spacer()
                    Button("OK") { dismissTrustReminder() }
                        .buttonStyle(.borderedProminent)
                        .keyboardShortcut(.defaultAction)
                }
            }
            .padding(16)
            .frame(width: 340)
            .background(RoundedRectangle(cornerRadius: 12).fill(Color(nsColor: .windowBackgroundColor)))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.gray.opacity(0.35), lineWidth: 1))
            .shadow(color: .black.opacity(0.3), radius: 20, y: 6)
        }
    }

    /// Close the reminder, persisting the "Don't show again" choice if it was checked.
    private func dismissTrustReminder() {
        if suppressTrustReminder { store.meshTrustReminderSuppressed = true }
        suppressTrustReminder = false
        trustReminderPeer = nil
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
        let linking = store.meshState?.linking ?? 0
        VStack(alignment: .leading, spacing: 8) {
            // The node can't send a single beacon — the OS is blocking its LAN
            // discovery, so peers can never find this machine and a dropped link
            // won't re-form. Shout it; without this the mesh just looks empty.
            if store.meshState?.beaconBlocked == true {
                discoverabilityBanner
            }
            // Still finding the fleet? Keep an animated, elapsed-timed banner up so a
            // slow first link reads as "scanning", not a frozen empty graph.
            if peers.isEmpty || linking > 0 {
                scanBanner(scanText(uptime: selfNode?.uptimeSecs, linking: linking))
            }
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

    private func scanText(uptime: Double?, linking: Int) -> String {
        if linking > 0 { return "Linking to \(linking) machine\(linking == 1 ? "" : "s")…" }
        let elapsed = uptime.map { " (\(fmtDur($0)))" } ?? ""
        return "Scanning the LAN for machines\(elapsed)…"
    }

    /// The major-issue banner for a node whose every beacon send fails. It shows the
    /// node's OWN diagnosis (snapshot `beaconBlockReason`), not a fixed guess: a Local
    /// Network / firewall gate (the fixable common case — "Open" jumps to the Local
    /// Network pane) versus a genuinely downed network stack (nothing to grant, so no
    /// button). The old banner always told the user to "allow Python", which is useless
    /// once it is already allowed (macOS pins the grant to an unsigned interpreter
    /// unreliably) and simply wrong when the network is down.
    private var discoverabilityBanner: some View {
        let networkDown = store.meshState?.beaconBlockReason == "network-down"
        // Build the detail string OUTSIDE the view builder: a ternary over long string
        // concatenations inside `Text(...)` makes SwiftUI's type-checker blow past its
        // time budget ("unable to type-check in reasonable time"). Plain typed lets are
        // trivial for it; the builder only ever sees `Text(detail)`.
        let downText: String = "No usable network — even a loopback send fails, so the "
            + "network stack looks down. Check this machine's connection; it isn't a "
            + "permissions problem."
        let gateText: String = "macOS is blocking this node's Local Network access. If "
            + "“Python” already appears enabled in Privacy & Security → Local Network, "
            + "the grant hasn't taken effect — toggle it off and back on."
        let detail: String = networkDown ? downText : gateText
        return HStack(spacing: 8) {
            Image(systemName: "wifi.exclamationmark")
                .font(.system(size: 15, weight: .bold)).foregroundStyle(.red)
            VStack(alignment: .leading, spacing: 1) {
                Text("DEVICE IS NOT DISCOVERABLE")
                    .font(.system(size: 11, weight: .heavy)).foregroundStyle(.red)
                Text(detail)
                    .font(.system(size: 9)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 6)
            if !networkDown {
                Button("Open") { openLocalNetworkSettings() }
                    .buttonStyle(.borderedProminent).tint(.red).controlSize(.small)
                    .help("Open System Settings → Privacy & Security → Local Network")
            }
        }
        .padding(.horizontal, 8).padding(.vertical, 6)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.red.opacity(0.12)))
        .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.red.opacity(0.45), lineWidth: 1))
    }

    private func openLocalNetworkSettings() {
        let anchor = "x-apple.systempreferences:com.apple.preference.security?Privacy_LocalNetwork"
        if let url = URL(string: anchor) { NSWorkspace.shared.open(url) }
    }

    private func scanBanner(_ text: String) -> some View {
        HStack(spacing: 6) {
            ProgressView().controlSize(.small).scaleEffect(0.7)
            Text(text).font(.system(size: 10, weight: .semibold))
                .foregroundStyle(Color(hex: "#30B0C7") ?? .teal)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 8).padding(.vertical, 4)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.teal.opacity(0.10)))
    }

    // MARK: nodes column

    private func nodesColumn(selfNode: MeshNode?, peers: [MeshPeer]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            sectionLabel("NODES")
            defaultTrustRow()
            if let s = selfNode { nodeCard(node: s, peer: nil) }
            ForEach(peers.sorted { $0.name.lowercased() < $1.name.lowercased() }, id: \.id) { p in
                nodeCard(node: nil, peer: p)
            }
        }
        .padding(7)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.gray.opacity(0.07)))
    }

    /// One node row. Exactly one of `node` (self) / `peer` is non-nil; the common
    /// attributes are read from whichever is present. Split into small typed subviews
    /// so the SwiftUI type-checker doesn't choke on one big builder.
    private func nodeCard(node: MeshNode?, peer: MeshPeer?) -> some View {
        let id = peer?.id ?? node?.id ?? ""
        let name = peer?.name ?? node?.name ?? "?"
        let platform = peer?.platform ?? node?.platform ?? "unknown"
        let tier = peer?.tier ?? node?.tier ?? 3
        let tokens = peer?.tokens ?? node?.tokens ?? "ok"
        let strengthAuto = peer?.strengthAuto ?? node?.strengthAuto ?? true
        let tokensAuto = peer?.tokensAuto ?? node?.tokensAuto ?? true
        let tokensPct = peer?.tokensPct ?? node?.tokensPct ?? 1.0
        let sessionPct = peer != nil ? peer?.tokensSessionPct : node?.tokensSessionPct
        let weekPct = peer != nil ? peer?.tokensWeekPct : node?.tokensWeekPct
        return VStack(alignment: .leading, spacing: 4) {
            nodeHeaderRow(name: name, platform: platform, peer: peer)
            if let addr = peer?.addr, !addr.isEmpty {
                Text(addr).font(.system(size: 9, design: .monospaced)).foregroundStyle(.secondary)
            }
            HStack(spacing: 10) {
                strengthEditor(nodeID: id, tier: tier, auto: strengthAuto)
                tokenEditor(nodeID: id, tokens: tokens, auto: tokensAuto)
                Spacer(minLength: 0)
            }
            quotaRow(tokens: tokens, sessionPct: sessionPct, weekPct: weekPct,
                     legacyPct: tokensPct, auto: tokensAuto)
            if let peer {
                // A newly-seen, still-untrusted device gets the one-time decision prompt;
                // once decided it collapses to the compact Personal/Foreign toggle.
                if isNewDevice(peer) { newDeviceCallout(peer) } else { trustToggle(peer) }
            }
        }
        .padding(6)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.06)))
    }

    private func nodeHeaderRow(name: String, platform: String, peer: MeshPeer?) -> some View {
        let meta = platformMeta(platform)
        return HStack(spacing: 6) {
            Text(meta.glyph).font(.system(size: 13))
                .frame(width: 20, height: 20)
                .background(RoundedRectangle(cornerRadius: 5).fill(meta.color.opacity(0.2)))
            Text(name).font(.caption).lineLimit(1)
            Spacer(minLength: 4)
            statusBadge(peer)
        }
    }

    /// 'self' for the local node; for a peer, the connection UPTIME while linked
    /// ('up 3m', counting up) or 'down · seen 2m ago' — a meaningful clock, not 'up 0s'.
    private func statusBadge(_ peer: MeshPeer?) -> some View {
        guard let peer else { return AnyView(badge("self", .green)) }
        let text: String
        if peer.link == "up" || peer.link == "stale" {
            text = peer.uptimeSecs.map { "\(peer.link) \(fmtDur($0))" } ?? peer.link
        } else {
            text = peer.lastSeenSecsAgo.map { "down · seen \(fmtDur($0)) ago" } ?? "down"
        }
        return AnyView(badge(text, linkColor(peer.link)))
    }

    /// Machine-strength picker in plain words. 'Auto' (default) tracks the hardware-
    /// detected tier; picking a word pins it (1 = strongest).
    private func strengthEditor(nodeID: String, tier: Int, auto: Bool) -> some View {
        let bounds = catalog?.tierBounds ?? (min: 1, max: 5, default: 3)
        let cat = catalog
        let autoLabel = "Auto · " + (cat?.tierLabel(tier) ?? "tier \(tier)")
        return Picker("", selection: Binding(
            get: { auto ? "auto" : String(tier) },
            set: { sel in
                if sel == "auto" {
                    store.meshSetAttr(nodeID: nodeID, attrs: ["strengthAuto": true])
                } else if let t = Int(sel) {
                    store.meshSetAttr(nodeID: nodeID, attrs: ["tier": t])
                }
            }
        )) {
            Text(autoLabel).tag("auto")
            ForEach(bounds.min...bounds.max, id: \.self) { t in
                Text(cat?.tierLabel(t) ?? "tier \(t)").tag(String(t))
            }
        }
        .labelsHidden().pickerStyle(.menu).frame(maxWidth: 150)
        .help("Machine strength — auto-detected from RAM/CPU/GPU (1 = strongest). "
              + "'weakest-first' routing keeps strong machines free; pick a word to pin it.")
    }

    /// Token-budget *setting* only — the measurement lives in `quotaRow`, so the picker
    /// never doubles as an indicator. 'Auto' (default) derives ok/low/out from the node's
    /// real quota; picking ok/low/out pins it (a pause escape).
    private func tokenEditor(nodeID: String, tokens: String, auto: Bool) -> some View {
        let ids = catalog?.tokens.map { $0.id } ?? ["ok", "low", "out"]
        return Picker("", selection: Binding(
            get: { auto ? "auto" : tokens },
            set: { sel in store.meshSetAttr(nodeID: nodeID, attrs: ["tokens": sel]) }
        )) {
            Text("Auto").tag("auto")
            ForEach(ids, id: \.self) { tid in
                Text("\(tokenMeta(tid).glyph) \(tid)").tag(tid)
            }
        }
        .labelsHidden().pickerStyle(.menu).frame(width: 92)
        .help("Token-budget setting. Auto derives ok/low/out from the node's real remaining "
              + "quota (see the quota row); picking a value pins the state until set back to "
              + "Auto. The mesh skips 'out' nodes.")
    }

    /// Read-only quota indicator, deliberately separate from the token-budget input:
    /// the effective state's color, and the real remaining percentages per rate-limit
    /// window (5-hour session · 7-day week) when the node's probe has them — else the
    /// local '≈NN%' estimate. 'pinned' flags a manual override.
    private func quotaRow(tokens: String, sessionPct: Double?, weekPct: Double?,
                          legacyPct: Double, auto: Bool) -> some View {
        let meta = tokenMeta(tokens)
        var left: String
        if let s = sessionPct {
            left = "5h \(clampedInt((s * 100).rounded()))%"
            if let w = weekPct { left += " · wk \(clampedInt((w * 100).rounded()))%" }
            left += " left"
        } else {
            left = "≈\(clampedInt((legacyPct * 100).rounded()))% left"
        }
        if !auto { left += " · pinned" }
        return HStack(spacing: 4) {
            Text("quota").font(.system(size: 9)).foregroundStyle(.secondary)
            Circle().fill(meta.color).frame(width: 6, height: 6)
            Text(left).font(.system(size: 9, weight: .bold)).foregroundStyle(meta.color)
            Spacer(minLength: 0)
        }
        .help("Remaining Claude quota — the account's real rate-limit windows (5-hour "
              + "session · 7-day week) via the OAuth usage probe; '≈' marks a local "
              + "estimate (probe unavailable). 'pinned' = a manual override is in effect "
              + "and the mesh routes on it.")
    }

    /// Whether a peer is a *newly-seen* device awaiting a trust decision: it proved a
    /// key (so it can be promoted), is currently Foreign (the zero-trust default), and
    /// the operator hasn't decided on it yet. Drives the one-time new-device prompt.
    private func isNewDevice(_ peer: MeshPeer) -> Bool {
        peer.verified && !peer.fingerprint.isEmpty && peer.trust == "foreign"
            && !store.meshAckedDevices.contains(peer.fingerprint)
    }

    /// Mark a peer Personal — add its proven fingerprint to the local allowlist so its
    /// requests run directly here. `remind` raises the "trust the other side too" note
    /// on a genuine promotion (trust is one-directional).
    private func setPersonal(_ peer: MeshPeer, remind: Bool) {
        store.meshSetTrust(fingerprint: peer.fingerprint, label: peer.name, trusted: true)
        store.meshAckDevice(fingerprint: peer.fingerprint)
        if remind && !store.meshTrustReminderSuppressed { trustReminderPeer = peer.name }
    }

    /// Mark a peer Foreign. A real demotion (it was Personal) removes it from the
    /// allowlist; a device that's already Foreign under the zero-trust default just has
    /// its new-device prompt dismissed — no needless control round-trip.
    private func setForeign(_ peer: MeshPeer) {
        if peer.trust == "personal" {
            store.meshSetTrust(fingerprint: peer.fingerprint, label: peer.name, trusted: false)
        }
        store.meshAckDevice(fingerprint: peer.fingerprint)
    }

    /// The one-time "New device" prompt shown on a peer's card until the operator
    /// decides: Make Personal (trust it) or Keep Foreign (dismiss). A new device is
    /// Foreign — zero-trust — by default, so "Keep Foreign" is the safe no-op.
    private func newDeviceCallout(_ peer: MeshPeer) -> some View {
        HStack(alignment: .top, spacing: 6) {
            Image(systemName: "sparkles").font(.system(size: 10, weight: .bold))
                .foregroundStyle(.orange)
            VStack(alignment: .leading, spacing: 1) {
                Text("New device").font(.system(size: 9, weight: .heavy)).foregroundStyle(.orange)
                Text("Foreign until you decide. Make it Personal to let it run your "
                     + "requests directly on this machine.")
                    .font(.system(size: 8)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 5) {
                    Button("Make Personal") { setPersonal(peer, remind: true) }
                        .buttonStyle(.borderedProminent).controlSize(.mini)
                        .tint(trustColor("personal"))
                    Button("Keep Foreign") { setForeign(peer) }
                        .buttonStyle(.bordered).controlSize(.mini)
                }
                .padding(.top, 1)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 7).padding(.vertical, 5)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.orange.opacity(0.12)))
        .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.orange.opacity(0.40), lineWidth: 1))
    }

    /// Personal | Foreign trust toggle for a peer's device (shown once the new-device
    /// prompt is dismissed). 'Personal' adds its proven fingerprint to the local
    /// allowlist; 'Foreign' removes it. Disabled until the peer proves a device key
    /// (trust must key on a verified fingerprint). A BANNED device (it accepted a
    /// SzpontRequest of ours and failed to deliver — docs/szpontnet/13) gets a mark +
    /// an Unban escape hatch instead: it stays declined until the operator explicitly
    /// lifts the ban.
    private func trustToggle(_ peer: MeshPeer) -> some View {
        let hasKey = !peer.fingerprint.isEmpty
        let personal = peer.trust == "personal"
        return HStack(spacing: 4) {
            Text("trust").font(.system(size: 9)).foregroundStyle(.secondary)
            if peer.trust == "banned" {
                Text("⊘ banned").font(.system(size: 9, weight: .bold))
                    .foregroundStyle(trustColor("banned"))
                    .help(banReason(peer))
                Button("unban") {
                    store.meshUnban(fingerprint: peer.verified ? peer.fingerprint : "",
                                    node: peer.id)
                }
                .buttonStyle(.borderless).font(.system(size: 9))
                Spacer(minLength: 0)
            } else {
                segButton("personal", active: personal, tint: trustColor("personal"), enabled: hasKey) {
                    if !personal { setPersonal(peer, remind: true) }
                }
                segButton("foreign", active: !personal, tint: trustColor("foreign"), enabled: hasKey) {
                    if personal { setForeign(peer) }
                }
                Spacer(minLength: 0)
                if !hasKey {
                    Text("(no key yet)").font(.system(size: 8)).foregroundStyle(.secondary)
                } else if !peer.verified {
                    Text("(unverified)").font(.system(size: 8)).foregroundStyle(.secondary)
                }
            }
        }
    }

    /// The recorded reason a peer's device was banned (hover text on the mark).
    private func banReason(_ peer: MeshPeer) -> String {
        let entries = store.meshState?.banned ?? []
        let hit = entries.first { e in
            e.fingerprint.isEmpty ? e.node == peer.id : e.fingerprint == peer.fingerprint
        }
        return hit?.reason.isEmpty == false ? hit!.reason : "banned"
    }

    /// A compact, reliably-hittable segmented pill button. `.plain` style plus an
    /// explicit `contentShape` so the whole padded capsule taps (not just the glyph or a
    /// clear background), with an always-visible outline so it reads as pressable — the
    /// old `.borderless` + `Color.clear`-backed version had a hairline hit target and no
    /// affordance, which read as "not clickable".
    private func segButton(_ label: String, active: Bool, tint: Color, enabled: Bool = true,
                           _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(label).font(.system(size: 9, weight: .bold))
                .foregroundStyle(enabled ? (active ? tint : Color.secondary)
                                         : Color.secondary.opacity(0.5))
                .padding(.horizontal, 7).padding(.vertical, 3)
                .frame(minHeight: 18)
                .background(Capsule().fill(active ? tint.opacity(0.20) : Color.clear))
                .overlay(Capsule().stroke(active ? tint.opacity(0.6)
                                                 : Color.secondary.opacity(0.30), lineWidth: 1))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain).disabled(!enabled)
    }

    /// The mesh-wide default-trust toggle: how a device is treated the moment it joins.
    /// 'Foreign' (default) is zero-trust — a new device can't run your requests until you
    /// promote it; 'Personal' is full-trust — every new device is trusted automatically.
    private func defaultTrustRow() -> some View {
        let current = store.meshState?.defaultTrust ?? "foreign"
        return HStack(spacing: 5) {
            Image(systemName: "shield.lefthalf.filled").font(.system(size: 9))
                .foregroundStyle(.secondary)
            Text("New devices:").font(.system(size: 9)).foregroundStyle(.secondary)
            segButton("Personal", active: current == "personal", tint: trustColor("personal")) {
                if current != "personal" { store.meshSetDefaultTrust(level: "personal") }
            }
            segButton("Foreign", active: current == "foreign", tint: trustColor("foreign")) {
                if current != "foreign" { store.meshSetDefaultTrust(level: "foreign") }
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 6).padding(.vertical, 4)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.05)))
        .help("Default trust level for a device the moment it joins the mesh. "
              + "Foreign (default) = zero-trust: a new device can't run your requests, "
              + "mutate this node, or own work until you mark it Personal. "
              + "Personal = full-trust: every new device is trusted automatically.")
    }

    private func trustColor(_ level: String) -> Color {
        if let hex = catalog?.trustLevel(level)?.colorHex, let c = Color(hex: hex) { return c }
        if level == "banned" { return .red }
        return level == "personal" ? .green : .gray
    }

    private func fmtDur(_ secs: Double) -> String {
        let s = clampedInt(secs)
        if s < 60 { return "\(s)s" }
        if s < 3600 { return "\(s / 60)m" }
        if s < 86400 { return "\(s / 3600)h" }
        return "\(s / 86400)d"
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
            let center = CGPoint(x: size.width / 2, y: size.height / 2)
            let points = Self.ringPoints(count: peers.count, in: size)
            var idToPoint: [String: CGPoint] = [selfNode.id: center]
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
                path.move(to: center)
                path.addLine(to: pt)
                let color = linkColor(peer.link)
                let style = StrokeStyle(lineWidth: peer.link == "up" ? 2 : 1.6,
                                        dash: peer.link == "down" ? [4, 3] : [])
                ctx.stroke(path, with: .color(color), style: style)
            }
            // nodes on top.
            drawNode(ctx, at: center, node: selfNode.platform, name: selfNode.name,
                     isSelf: true, height: size.height)
            for (peer, pt) in zip(peers, points) {
                drawNode(ctx, at: pt, node: peer.platform, name: peer.name, isSelf: false,
                         height: size.height)
            }
        }
    }

    /// Peer positions on a ring around the centre. A plain function with explicit
    /// `CGFloat` types — kept OUT of the `Canvas` ViewBuilder closure so the mixed
    /// CGFloat/Double trig doesn't blow up the SwiftUI type-checker.
    private static func ringPoints(count: Int, in size: CGSize) -> [CGPoint] {
        guard count > 0 else { return [] }
        let cx: CGFloat = size.width / 2
        let cy: CGFloat = size.height / 2
        let radius: CGFloat = min(size.width * 0.33, size.height / 2 - 30)
        var points: [CGPoint] = []
        for i in 0..<count {
            let ang: Double = -Double.pi / 2 + (2 * Double.pi * Double(i) / Double(count))
            let x: CGFloat = cx + radius * CGFloat(cos(ang))
            let y: CGFloat = cy + radius * CGFloat(sin(ang))
            points.append(CGPoint(x: x, y: y))
        }
        return points
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
