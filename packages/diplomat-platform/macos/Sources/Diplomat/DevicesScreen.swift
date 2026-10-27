import SwiftUI
import AppKit
import DiplomatCore

/// The Devices screen: everything about the device-allocator module in one place,
/// swapped in for the main panel body when the header's device button (or the left
/// column's Devices header) is tapped.
///
/// Lists on the left, controls on the right, as on the main panel: every device in the
/// daemon's snapshot with the actions the daemon takes for it, and the allocator events
/// from the activity feed; then the allocator itself (daemon, install, idle limits) and
/// the remote device pool (its provider module, its status, sign-in, what it lists, and
/// the devices leased from it).
///
/// Every remote field is optional: against a daemon with no remote pool the card says so
/// and the rest of the screen is unchanged.
struct DevicesScreen: View {
    @EnvironmentObject var store: Store
    @Binding var isPresented: Bool
    /// Whether the screen keeps the install status and the provider fresh while it is
    /// on screen. Off in a render, whose seeded state a real read would overwrite.
    var live: Bool = !Headless.isRender
    var pollNanoseconds: UInt64 = 15_000_000_000

    /// The provider field while it is being edited; nil shows the stored value.
    @State private var providerDraft: String?
    /// Why the last provider change did not reach config.json.
    @State private var saveError: String?
    /// MenuBarExtra hides the popover without tearing this view down, so the poll
    /// follows the window rather than the view's lifetime.
    @State private var onScreen = false
    @State private var inUseExpanded = true
    @State private var freeExpanded = true

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading, spacing: 10) {
                    devicesCard
                    eventsCard
                }
                .frame(width: PopoverRoot.columnWidth, alignment: .topLeading)
                VStack(alignment: .leading, spacing: 10) {
                    allocatorCard
                    remoteCard
                }
                .frame(width: PopoverRoot.columnWidth, alignment: .topLeading)
            }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
        // Live only: a render's ImageRenderer draws an AppKit view as a placeholder.
        .background {
            if live {
                WindowVisibility { shown in
                    DispatchQueue.main.async { if onScreen != shown { onScreen = shown } }
                }
            }
        }
        .task(id: onScreen) {
            guard live, onScreen else { return }
            // `daemonRunning` comes from `--check` alone, which no poll runs.
            while !Task.isCancelled {
                store.reloadDeviceRemoteProvider()
                await store.refreshAllocatorInstall()
                try? await Task.sleep(nanoseconds: pollNanoseconds)
            }
        }
    }

    // MARK: header

    private var daemonLight: (Color, String) {
        guard let s = store.allocatorInstall else { return (.gray, "checking") }
        if s.daemonRunning { return (.green, "daemon running") }
        return s.installed ? (.red, "daemon not running") : (.gray, "not installed")
    }

    private var header: some View {
        let (dot, label) = daemonLight
        let counts: String? = store.deviceState.map { "\($0.allocatedCount) in use · \($0.freeCount) free" }
        return HStack(spacing: 6) {
            Image(systemName: "iphone.gen3").foregroundStyle(.secondary)
            Text("Devices").font(.subheadline.bold())
            if let counts {
                Text(counts).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
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

    // MARK: devices

    private var devicesCard: some View {
        let devices = store.deviceState?.devices ?? []
        let inUse = DevicesView.sorted(devices.filter(\.isAllocated))
        let free = DevicesView.sorted(devices.filter { !$0.isAllocated })
        let pill: StatusPill? = store.deviceState == nil ? nil
            : StatusPill(text: "\(inUse.count) in use", tint: inUse.isEmpty ? .secondary : .green)
        return SettingsCard(symbol: "iphone.gen3", title: "DEVICES", tint: .cyan, pill: pill) {
            if store.deviceState == nil {
                note("No snapshot yet: the allocator daemon has not run on this machine.")
            } else if devices.isEmpty {
                note("The allocator sees no simulator or emulator on this machine.")
            }
            if store.deviceState != nil, store.allocatorInstall?.daemonRunning == false {
                note("The daemon is not running, so this is its last snapshot, not the pool now.",
                     tint: .orange)
            }
            if !inUse.isEmpty { group("In use", .green, $inUseExpanded, inUse) }
            if !free.isEmpty { group("Free", .secondary, $freeExpanded, free) }
        }
    }

    private func group(_ title: String, _ tint: Color, _ expanded: Binding<Bool>,
                       _ devices: [DeviceAllocation]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            SectionHeader(title: title.uppercased(), count: devices.count,
                          expanded: expanded, countTint: tint)
            if expanded.wrappedValue {
                ForEach(devices) { dev in row(dev) }
            }
        }
    }

    private func row(_ dev: DeviceAllocation) -> some View {
        DeviceScreenRow(dev: dev, tracked: store.agentRows,
                        provider: store.deviceState?.remote?.provider,
                        idleLimit: store.deviceIdleLeasedMinutes,
                        onFree: { Task { await store.releaseDevice(dev) } },
                        onRepaired: { Task { await store.markDeviceRepaired(dev.key) } },
                        onKill: { Task { await store.killDevice(dev.key) } })
    }

    /// The feed's allocator lines. An `auto` kill-device line is the agent reaper
    /// closing a window, not a device, so it stays out.
    static let eventActions: Set<String> = [
        "kill-device", "release-device", "repair-done", "remote-lease-ended",
        "allocator-install", "allocator-uninstall",
    ]

    private var eventsCard: some View {
        let events = store.auditEntries.filter {
            Self.eventActions.contains($0.action) && !($0.action == "kill-device" && $0.source == "auto")
        }.prefix(6)
        return SettingsCard(symbol: "clock.arrow.circlepath", title: "RECENT", tint: .secondary) {
            if events.isEmpty {
                note("No allocator events in the activity feed.")
            } else {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(Array(events)) { AuditRow(entry: $0) }
                }
            }
        }
    }

    // MARK: allocator

    private var allocatorCard: some View {
        let s = store.allocatorInstall
        return SettingsCard(symbol: "shippingbox.fill", title: "ALLOCATOR", tint: .cyan,
                            pill: AllocatorUI.pill(s)) {
            SettingRow(title: "Daemon", summary: daemonSummary(s)) {
                StatusPill(text: daemonLight.1, tint: daemonLight.0,
                           symbol: s?.daemonRunning == true ? "bolt.fill" : "bolt.slash.fill")
            }
            SettingRow(title: "Reserve a simulator before using it",
                       summary: AllocatorUI.summary, stacked: true) {
                VStack(alignment: .leading, spacing: 7) {
                    AllocatorMarks(status: s)
                    AllocatorButtons(status: s, offersDaemon: true)
                }
            }
            AllocatorIdleLimits()
        }
    }

    private func daemonSummary(_ s: AllocatorInstall?) -> String {
        guard let s else { return "Asking the installer…" }
        if s.daemonRunning {
            let pid = store.deviceState?.daemonPid.map { "pid \($0) · " } ?? ""
            return "\(pid)\(DeviceAllocator.socketPath)"
        }
        return s.installed
            ? "Not answering. An agent's first request starts it; Start runs it now."
            : "Install the allocator to run it."
    }

    // MARK: remote pool

    private var remote: RemoteStatus? { store.deviceState?.remote }

    /// Whether the daemon has a pool to call: one it reports, and not off.
    private var remoteCallable: Bool { remote.map { $0.state != "off" } ?? false }

    private var remoteCard: some View {
        SettingsCard(symbol: "network", title: "REMOTE POOL", tint: .indigo,
                     pill: RemoteUI.pill(remote, snapshot: store.deviceState != nil)) {
            SettingRow(title: "Provider module", summary: providerSummary, stacked: true) {
                providerField
            }
            if let saveError {
                note(saveError, tint: .red)
            }
            remoteStatusBlock
            SettingRow(title: "Pool", stacked: true) { remoteCalls }
            remoteLeases
        }
    }

    private var storedProvider: String {
        store.deviceRemoteProvider.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var providerSummary: String {
        let p = storedProvider
        if p.isEmpty { return "Blank = no remote pool: agents get this machine's devices only." }
        let path = (p as NSString).expandingTildeInPath
        if !FileManager.default.fileExists(atPath: path) { return "⚠ No file at \(path)." }
        return "Tried before local devices. The allocator re-reads this on every request."
    }

    private var providerField: some View {
        let draft = providerDraft ?? store.deviceRemoteProvider
        let dirty = providerDraft != nil && providerDraft != store.deviceRemoteProvider
        return HStack(spacing: 6) {
            Image(systemName: "puzzlepiece.extension").font(.caption).foregroundStyle(.secondary)
            TextField("~/path/to/provider.mjs",
                      text: Binding(get: { draft }, set: { providerDraft = $0 }))
                .textFieldStyle(.plain).font(.callout).lineLimit(1)
                .onSubmit(applyProvider)
            if !draft.isEmpty {
                Button {
                    providerDraft = nil
                    save("")
                } label: { Image(systemName: "xmark.circle.fill") }
                .buttonStyle(.borderless).foregroundStyle(.secondary)
                .help("Clear: turn the remote pool off")
            }
            Button("Apply", action: applyProvider)
                .buttonStyle(.borderedProminent).controlSize(.small).disabled(!dirty)
            Button("Choose…", action: chooseProvider)
                .buttonStyle(.bordered).controlSize(.small)
        }
        .padding(7)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.12)))
    }

    /// Committed on Apply/Return rather than per keystroke: the daemon loads whatever
    /// path the file names on its next request, half-typed or not.
    private func applyProvider() {
        guard let d = providerDraft else { return }
        if save(d) { providerDraft = nil }
    }

    /// Write one provider change and say whether it landed; a failure stays on screen
    /// until a change lands.
    @discardableResult
    private func save(_ provider: String) -> Bool {
        let ok = store.setDeviceRemoteProvider(provider)
        saveError = ok ? nil : "⚠ Couldn't save that change to \(AppConfig.url.path), so it has "
            + "no effect until it is made again."
        return ok
    }

    private func chooseProvider() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.message = "Choose the module that lends devices from the remote pool"
        panel.prompt = "Use as provider"
        NSApp.activate(ignoringOtherApps: true)
        if panel.runModal() == .OK, let url = panel.url {
            providerDraft = nil
            save(url.path)
        }
    }

    @ViewBuilder
    private var remoteStatusBlock: some View {
        if store.deviceState == nil {
            note("No snapshot from the allocator yet, so no remote status either.")
        } else if let r = remote {
            RemoteStatusView(status: r)
        } else if storedProvider.isEmpty {
            note("No remote pool configured.")
        } else {
            VStack(alignment: .leading, spacing: 6) {
                note(AllocatorUI.predatesPoolNote, tint: .orange)
                RestartDaemonButton()
            }
        }
    }

    private var remoteCalls: some View {
        let why = remoteCallable ? "" : " (no remote pool is on)"
        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Button { Task { await store.remoteSignInNow() } } label: { Text("Sign in").bold() }
                    .buttonStyle(.borderedProminent).controlSize(.small)
                    .disabled(!remoteCallable || store.remoteSignIn == .running)
                    .help("Ask the pool for a browser sign-in\(why)")
                Button { Task { await store.listRemotePool() } } label: { Text("List pool devices") }
                    .buttonStyle(.bordered).controlSize(.small)
                    .disabled(!remoteCallable || store.remoteListing == .running)
                    .help("What the pool lends now. Never prompts a sign-in\(why)")
                Spacer(minLength: 0)
            }
            RemoteCallLine(call: store.remoteSignIn, label: "Sign-in",
                           running: "Waiting for the browser sign-in (up to 10 min)…")
            RemoteCallLine(call: store.remoteListing, label: "Pool",
                           running: "Asking the pool…")
        }
    }

    @ViewBuilder
    private var remoteLeases: some View {
        let leases = (store.deviceState?.devices ?? []).filter(\.isRemote)
        if !leases.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                Text("LEASED HERE · \(leases.count)").font(.system(size: 9, weight: .bold))
                    .foregroundStyle(.secondary).kerning(0.5)
                ForEach(leases) { dev in
                    HStack(spacing: 6) {
                        Image(systemName: dev.platformIcon).font(.system(size: 9))
                            .foregroundStyle(dev.platformTint)
                        Text(dev.name ?? dev.key).font(.caption).lineLimit(1)
                        Text(dev.handle ?? "attaching").font(.system(size: 9, design: .monospaced))
                            .foregroundStyle(.secondary).lineLimit(1)
                        Spacer(minLength: 4)
                        if let who = dev.owner?.agentName {
                            Text(who).font(.system(size: 9)).foregroundStyle(.secondary).lineLimit(1)
                        }
                        DeviceStatusBadge(dev: dev)
                    }
                }
            }
        }
    }

    private func note(_ text: String, tint: Color = .secondary) -> some View {
        Text(text).font(.system(size: 10)).foregroundStyle(tint)
            .fixedSize(horizontal: false, vertical: true)
    }
}

// MARK: - remote pool pieces

enum RemoteUI {
    /// One word per state the daemon publishes; an unknown one is shown as it is.
    static func pill(_ r: RemoteStatus?, snapshot: Bool) -> StatusPill {
        guard let r else {
            return StatusPill(text: snapshot ? "not available" : "unknown", tint: .secondary,
                              symbol: "circle.dashed")
        }
        switch r.state {
        case "on":         return StatusPill(text: "on", tint: .green, symbol: "checkmark.seal.fill")
        case "off":        return StatusPill(text: "off", tint: .secondary, symbol: "circle.dashed")
        case "loading":    return StatusPill(text: "loading…", tint: .secondary, symbol: "hourglass")
        case "error":      return StatusPill(text: "error", tint: .red, symbol: "xmark.octagon.fill")
        case "signed-out": return StatusPill(text: "signed out", tint: .orange, symbol: "person.crop.circle.badge.xmark")
        case "signing-in": return StatusPill(text: "signing in…", tint: .blue, symbol: "person.crop.circle.badge.clock")
        case "sign-in-unanswered":
            return StatusPill(text: "away", tint: .orange, symbol: "moon.zzz.fill")
        default:           return StatusPill(text: r.state, tint: .gray, symbol: "questionmark.circle")
        }
    }
}

/// The provider's status, every key of it: the known ones in words, the rest as
/// `key value` lines.
private struct RemoteStatusView: View {
    let status: RemoteStatus

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(status.provider ?? "provider").font(.system(size: 11, weight: .semibold))
                Text(status.state).font(.system(size: 9, design: .monospaced)).foregroundStyle(.secondary)
                Spacer(minLength: 0)
            }
            if let reason = status.reason {
                line(reason, "exclamationmark.triangle.fill", status.state == "error" ? .red : .orange)
            }
            if let at = status.retryAt {
                line(awayText(at), "moon.zzz.fill", .orange)
            }
            if let login = status.loginUrl, let url = URL(string: login) {
                HStack(spacing: 4) {
                    Image(systemName: "person.badge.key.fill").font(.system(size: 9))
                    Link("Open the sign-in page", destination: url).font(.system(size: 10, weight: .semibold))
                    Text(login).font(.system(size: 9, design: .monospaced)).foregroundStyle(.secondary)
                        .lineLimit(1).truncationMode(.middle)
                }
            }
            if let u = status.url, let url = URL(string: u) {
                HStack(spacing: 4) {
                    Image(systemName: "link").font(.system(size: 9)).foregroundStyle(.secondary)
                    Link(u, destination: url).font(.system(size: 9, design: .monospaced))
                        .lineLimit(1).truncationMode(.middle)
                }
            }
            ForEach(status.extras, id: \.self) { extra in
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(extra.key).font(.system(size: 9, design: .monospaced)).foregroundStyle(.secondary)
                        .frame(minWidth: 70, alignment: .leading)
                    Text(extra.value).font(.system(size: 9, design: .monospaced))
                        .lineLimit(2).truncationMode(.middle)
                }
            }
        }
        .padding(7)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.indigo.opacity(0.07)))
    }

    private func awayText(_ at: Date) -> String {
        status.state == "sign-in-unanswered"
            ? "Sign-in went unanswered: treated as away, no prompt until \(Fmt.clock(at))."
            : "Retry at \(Fmt.clock(at))."
    }

    private func line(_ text: String, _ symbol: String, _ tint: Color) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 4) {
            Image(systemName: symbol).font(.system(size: 8))
            Text(text).font(.system(size: 10)).fixedSize(horizontal: false, vertical: true)
        }
        .foregroundStyle(tint)
    }
}

/// What the last sign-in or listing answered, or that it is still waiting.
private struct RemoteCallLine: View {
    let call: Store.RemoteCall
    let label: String
    let running: String

    var body: some View {
        switch call {
        case .idle:
            EmptyView()
        case .running:
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text(running).font(.system(size: 10)).foregroundStyle(.secondary)
            }
        case .answered(let reply, let at):
            answer(reply, at: Fmt.clock(at))
        }
    }

    @ViewBuilder
    private func answer(_ reply: RemoteReply, at: String) -> some View {
        switch reply {
        case .ok:
            Text("\(label): signed in · \(at)").font(.system(size: 10)).foregroundStyle(.green)
        case .reason(let why):
            Text("\(label): \(why) · \(at)").font(.system(size: 10)).foregroundStyle(.orange)
                .fixedSize(horizontal: false, vertical: true)
        case .devices(let list):
            VStack(alignment: .leading, spacing: 2) {
                Text("\(label): \(list.count) device\(list.count == 1 ? "" : "s") · \(at)")
                    .font(.system(size: 10)).foregroundStyle(.secondary)
                ForEach(list) { d in
                    HStack(spacing: 6) {
                        Text(d.name ?? d.id).font(.caption).lineLimit(1)
                        Text(Self.specs(d))
                            .font(.system(size: 9)).foregroundStyle(.secondary).lineLimit(1)
                        Spacer(minLength: 4)
                        if let s = d.state {
                            Text(s).font(.system(size: 9, weight: .bold)).foregroundStyle(.secondary)
                        }
                    }
                    .padding(.horizontal, 6).padding(.vertical, 3)
                    .background(RoundedRectangle(cornerRadius: 5).fill(Color.gray.opacity(0.06)))
                }
            }
        }
    }

    /// Outside the ViewBuilder: the macOS CI compiler times out type-checking this shape inline.
    private static func specs(_ d: RemotePoolDevice) -> String {
        let parts: [String?] = [d.platform, d.version.map { "v\($0)" },
                                d.apiVersion.map { "API \($0)" }, d.format]
        return parts.compactMap { $0 }.joined(separator: " · ")
    }
}

// MARK: - one device

/// A device on the Devices screen: what it is, where it comes from, who holds it and
/// for how long, and the daemon actions that apply to it.
struct DeviceScreenRow: View {
    let dev: DeviceAllocation
    let tracked: [Store.AgentRow]
    /// The remote pool's name, for a leased row's source chip.
    let provider: String?
    /// The leased idle limit, in minutes (0 = never), which the idle time is coloured toward.
    let idleLimit: Int
    let onFree: () -> Void
    let onRepaired: () -> Void
    let onKill: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            IconBadge(symbol: dev.platformIcon,
                      tint: dev.isAllocated ? dev.platformTint : Color.gray.opacity(0.4))
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Text(dev.name ?? dev.handle ?? dev.key).font(.caption.bold()).lineLimit(1)
                    if let v = dev.version { Text(v).font(.system(size: 9)).foregroundStyle(.secondary) }
                    if let f = dev.format { Text(f).font(.system(size: 9)).foregroundStyle(.tertiary) }
                    sourceChip
                }
                holderLine
                Text(dev.handle ?? dev.key).font(.system(size: 9, design: .monospaced))
                    .foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                if dev.status == "repairing" {
                    Text(dev.brokenReason.map { "repair: \($0)" } ?? "repair dispatched")
                        .font(.system(size: 9)).foregroundStyle(.purple).lineLimit(2)
                }
            }
            Spacer(minLength: 4)
            VStack(alignment: .trailing, spacing: 5) {
                DeviceStatusBadge(dev: dev)
                actions
            }
        }
        .padding(6)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.06)))
    }

    private var sourceChip: some View {
        let text = dev.isRemote ? (provider.map { "remote · \($0)" } ?? "remote") : "local"
        let tint: Color = dev.isRemote ? .indigo : .gray
        return Text(text).font(.system(size: 8, weight: .bold))
            .foregroundStyle(tint)
            .padding(.horizontal, 5).padding(.vertical, 1)
            .background(Capsule().fill(tint.opacity(0.14)))
    }

    @ViewBuilder
    private var holderLine: some View {
        if dev.status != "repairing", let pid = dev.owner?.ownerPid {
            HStack(spacing: 6) {
                Button {
                    // Off-main: one `ps` and up to two osascript round-trips.
                    let d = dev, t = tracked
                    Task.detached(priority: .userInitiated) { DeviceFocus.focus(d, tracked: t) }
                } label: {
                    HStack(spacing: 3) {
                        Image(systemName: "person.fill").font(.system(size: 8))
                        Text(dev.owner?.agentName ?? "pid \(pid)").font(.system(size: 9, weight: .semibold))
                            .lineLimit(1)
                        Image(systemName: "macwindow").font(.system(size: 8))
                    }
                    .foregroundStyle(dev.platformTint)
                }
                .buttonStyle(.plain)
                .help("Focus the terminal running \(dev.owner?.agentName ?? "this agent")")
                if let started = dev.allocatedAt {
                    TimelineView(.periodic(from: Date(), by: 30)) { ctx in
                        Text(heldText(since: started / 1000, now: ctx.date))
                            .font(.system(size: 9)).foregroundStyle(.secondary)
                    }
                }
                if let m = dev.idleMinutes {
                    Text("idle \(m)m").font(.system(size: 9))
                        .foregroundStyle(DeviceAllocation.idleColor(m, limit: idleLimit))
                }
            }
        }
    }

    private func heldText(since: Double, now: Date) -> String {
        let at = Date(timeIntervalSince1970: since)
        return "since \(Fmt.clock(at)) (\(Fmt.duration(now.timeIntervalSince(at))))"
    }

    /// Only what the daemon does for this row: a lease's holder can be dropped, a
    /// quarantine ended, and any running device killed (a remote one returned).
    @ViewBuilder
    private var actions: some View {
        HStack(spacing: 4) {
            if dev.isRemote {
                // A remote row exists only while leased.
                actionButton("Return", "arrow.uturn.backward.circle", .indigo, onKill,
                             "Hand the lease back to the remote pool. The agent loses the device.")
            } else {
                if dev.owner?.ownerPid != nil, dev.status != "repairing" {
                    actionButton("Free", "arrow.uturn.backward.circle", .blue, onFree,
                                 "Take it back from \(dev.owner?.agentName ?? "its agent"). "
                                 + "One the allocator booted is shut down.")
                }
                if dev.status == "repairing" {
                    actionButton("Repaired", "checkmark.seal", .purple, onRepaired,
                                 "End the quarantine and put it back in the pool.")
                }
                if dev.status != "free" {
                    actionButton("Kill", "xmark.circle", .red, onKill,
                                 "Free it and shut the simulator/emulator down.")
                }
            }
        }
    }

    private func actionButton(_ title: String, _ symbol: String, _ tint: Color,
                              _ action: @escaping () -> Void, _ help: String) -> some View {
        Button(action: action) {
            Label(title, systemImage: symbol).font(.system(size: 9, weight: .semibold))
        }
        .buttonStyle(.bordered).controlSize(.mini).tint(tint)
        .help(help)
    }
}

/// The status capsule a device row ends on.
struct DeviceStatusBadge: View {
    let dev: DeviceAllocation

    var body: some View {
        let (text, color) = dev.statusBadge
        Text(text)
            .font(.system(size: 9, weight: .bold))
            .foregroundStyle(color)
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(Capsule().fill(color.opacity(0.14)))
    }
}

extension DeviceAllocation {
    var platformIcon: String {
        switch platform {
        case "ios":        return "apple.logo"
        case "apple-tv":   return "appletv"
        case "android":    return "candybarphone"
        case "android-tv": return "tv"
        case "vega":       return "flame"
        default:           return "square.dashed"
        }
    }

    var platformTint: Color {
        switch platform {
        case "ios", "apple-tv":       return .blue
        case "android", "android-tv": return .green
        case "vega":                  return .orange
        default:                      return .gray
        }
    }

    var statusBadge: (text: String, color: Color) {
        switch status {
        case "ready":     return isAllocated ? ("in use", .green) : ("free", .secondary)
        case "booting":   return (isRemote ? "attaching" : "booting", .orange)
        case "repairing": return ("repairing", .purple)
        case "error":     return ("error", .red)
        default:          return ("free", .secondary)
        }
    }

    /// Whole minutes idle (nil under a minute). The daemon floors idleMs to minutes.
    var idleMinutes: Int? {
        guard let ms = idleMs, ms >= 60_000 else { return nil }
        return clampedInt(ms / 60_000)
    }

    /// Idle time coloured toward red as it nears `limit`, the leased idle limit in
    /// minutes, past which the daemon takes the device back (0 = never).
    static func idleColor(_ minutes: Int, limit: Int) -> Color {
        guard limit > 0 else { return .secondary }
        if minutes >= limit - 1 { return .red }
        if minutes >= limit * 2 / 3 { return .orange }
        return .secondary
    }
}

// MARK: - allocator controls (shared with Settings)

enum AllocatorUI {
    /// "Installed" alone would be a true statement about a machine still running the
    /// copies some earlier checkout laid down, so the stale case says so and the marks
    /// name what drifted. Amber for stale, not green: it is working, but not from this
    /// checkout. Mirrors `settingsview._refresh_allocator_ui`.
    static func pill(_ s: AllocatorInstall?) -> StatusPill {
        guard let s else { return StatusPill(text: "checking…", tint: .secondary) }
        let version = s.version ?? "?"
        if s.outdated {
            return StatusPill(text: "out of date · v\(version)", tint: .orange,
                              symbol: "exclamationmark.triangle.fill")
        }
        if s.installed {
            return StatusPill(text: "v\(version)", tint: .green, symbol: "checkmark.seal.fill")
        }
        return StatusPill(text: "not installed", tint: .secondary, symbol: "circle.dashed")
    }

    static var ready: Bool { DeviceAllocator.packageAvailable && DeviceAllocator.nodeAvailable }

    static let predatesPoolNote = "The running allocator daemon reports no remote pool: it "
        + "predates one. Update the checkout if it has none either, then Restart daemon."

    /// Twin of `devicesview.RESTART_HELP`.
    static let restartHelp = "Stop the running daemon and start this checkout's. Agents keep "
        + "their leases; a simulator or emulator the allocator booted is shut down as the old "
        + "daemon exits."

    static var summary: String {
        if !DeviceAllocator.packageAvailable {
            return "⚠ Package not found at \(DeviceAllocator.packageDir). Set "
                + "DIPLOMAT_DEVICE_ALLOCATOR_DIR to point at it."
        }
        if !DeviceAllocator.nodeAvailable {
            return "⚠ Node.js not found. Install Node (or set DIPLOMAT_NODE) — the "
                + "allocator's MCP server and daemon need it to run."
        }
        return "So two agents never drive the same emulator at once."
    }
}

struct AllocatorMarks: View {
    let status: AllocatorInstall?

    var body: some View {
        if let s = status {
            let drift = s.outdated && !s.drift.isEmpty ? s.drift.joined(separator: ", ") : ""
            HStack(spacing: 4) {
                MarkChip(label: "MCP", ok: s.mcpRegistered)
                MarkChip(label: "skill", ok: s.skillInstalled)
                MarkChip(label: "rule", ok: s.ruleInstalled)
                MarkChip(label: "CLAUDE.md", ok: s.claudeMdInjected)
                if s.daemonRunning {
                    StatusPill(text: "daemon", tint: .green, symbol: "bolt.fill")
                }
                Spacer(minLength: 0)
            }
            if !drift.isEmpty {
                Text("stale: \(drift)")
                    .font(.system(size: 9, design: .monospaced)).foregroundStyle(.orange)
            }
        }
    }
}

struct RestartDaemonButton: View {
    @EnvironmentObject var store: Store

    var body: some View {
        Button { Task { await store.restartAllocatorDaemon() } } label: { Text("Restart daemon") }
            .buttonStyle(.bordered).controlSize(.small)
            .disabled(!AllocatorUI.ready)
            .help(AllocatorUI.restartHelp)
    }
}

struct AllocatorButtons: View {
    @EnvironmentObject var store: Store
    let status: AllocatorInstall?
    /// Offer to start a daemon that an installed allocator has not got running, and to
    /// restart one that is.
    var offersDaemon = false

    var body: some View {
        let s = status
        HStack(spacing: 8) {
            Button { Task { await store.installAllocator() } } label: {
                Text(s?.outdated ?? false ? "Update"
                     : (s?.installed ?? false) ? "Reinstall" : "Install").bold()
            }
            .buttonStyle(.borderedProminent).controlSize(.small)
            .disabled(!AllocatorUI.ready)
            if s?.installed ?? false {
                Button { Task { await store.uninstallAllocator() } } label: { Text("Uninstall") }
                    .buttonStyle(.bordered).controlSize(.small)
            }
            if offersDaemon, let s, s.installed, !s.daemonRunning {
                Button { Task { await store.startAllocatorDaemon() } } label: { Text("Start daemon") }
                    .buttonStyle(.bordered).controlSize(.small)
                    .disabled(!AllocatorUI.ready)
            }
            if offersDaemon, let s, s.installed, s.daemonRunning {
                RestartDaemonButton()
            }
            Button { Task { await store.refreshAllocatorInstall() } } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless).controlSize(.small).help("Re-check status")
            Spacer(minLength: 0)
        }
    }
}

/// The daemon's two idle limits, in minutes, where 0 turns either off.
struct AllocatorIdleLimits: View {
    @EnvironmentObject var store: Store

    var body: some View {
        row(title: "Shut down a free device after",
            summary: "A booted device no agent holds, once its screen stops changing.",
            detail: nil,
            value: $store.deviceIdleFreeMinutes,
            range: AppConfig.deviceIdleFreeRange, step: 1)
        row(title: "Take a held device back after",
            summary: "From its agent, once its screen stops changing. It then counts as free.",
            detail: "A change to either reaches the allocator within a minute.",
            value: $store.deviceIdleLeasedMinutes,
            range: AppConfig.deviceIdleLeasedRange, step: 5)
    }

    private func row(title: String, summary: String, detail: String?,
                     value: Binding<Int>, range: ClosedRange<Int>, step: Int) -> some View {
        let n = value.wrappedValue
        let badge: String = n == 0 ? "never" : "\(n) min"
        return SettingRow(title: title, summary: summary, detail: detail, stacked: true) {
            SliderSetting(label: title,
                          value: Binding(
                              get: { Double(value.wrappedValue) },
                              set: { value.wrappedValue = Int($0.rounded()) }
                          ),
                          range: Double(range.lowerBound)...Double(range.upperBound),
                          step: Double(step),
                          badge: badge,
                          minLabel: "never", maxLabel: "\(range.upperBound) min",
                          tint: .cyan)
        }
    }
}

/// Reports whether the window around it is on screen, now and on every change.
struct WindowVisibility: NSViewRepresentable {
    let onChange: (Bool) -> Void

    func makeNSView(context: Context) -> Reader {
        let view = Reader()
        view.onChange = onChange
        return view
    }

    func updateNSView(_ view: Reader, context: Context) { view.onChange = onChange }

    final class Reader: NSView {
        var onChange: (Bool) -> Void = { _ in }
        private var token: NSObjectProtocol?

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            token.map(NotificationCenter.default.removeObserver)
            token = nil
            if let window {
                token = NotificationCenter.default.addObserver(
                    forName: NSWindow.didChangeOcclusionStateNotification, object: window,
                    queue: .main) { [weak self] _ in self?.report() }
            }
            report()
        }

        private func report() {
            onChange(window.map { $0.isVisible && $0.occlusionState.contains(.visible) } ?? false)
        }

        deinit { token.map(NotificationCenter.default.removeObserver) }
    }
}
