import SwiftUI
import DiplomatCore

/// The Machines screen: the remote-machines MCP's status and one panel per machine in its
/// pool, in the daemon's own order.
struct MachinesScreen: View {
    @EnvironmentObject var store: Store
    @Binding var isPresented: Bool
    /// Polls while on screen; off in a render, whose seeded state a real read would
    /// overwrite.
    var live: Bool = !Headless.isRender

    @Environment(\.renderNow) private var renderNow
    @State private var onScreen = false
    /// Ticks with the snapshot poll, so the ages on screen keep moving.
    @State private var tick = Date()

    private var now: Date { renderNow ?? tick }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            HStack(alignment: .top, spacing: 12) {
                statusCard.frame(width: MainRoot.columnWidth, alignment: .topLeading)
                eventsCard.frame(width: MainRoot.columnWidth, alignment: .topLeading)
            }
            machines
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .background {
            if live {
                WindowVisibility { shown in
                    DispatchQueue.main.async { if onScreen != shown { onScreen = shown } }
                }
            }
        }
        .task(id: onScreen) {
            guard live, onScreen else { return }
            while !Task.isCancelled {
                await store.refreshMachinesState()
                tick = Date()
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
        .task(id: onScreen) {
            guard live, onScreen else { return }
            while !Task.isCancelled {
                await AppLog.timed("machinesPoll") { await store.refreshMachinesInstall() }
                try? await Task.sleep(nanoseconds: 15_000_000_000)
            }
        }
    }

    private var install: AllocatorInstall? { store.machinesInstall }
    private var snapshot: MachinesSnapshot? { store.machinesState }

    // MARK: header

    private var daemonLight: (Color, String) {
        guard let s = install else { return (.gray, "checking") }
        if s.daemonRunning { return (.green, "daemon running") }
        return s.installed ? (.gray, "daemon idle") : (.gray, "not installed")
    }

    private var header: some View {
        let (dot, label) = daemonLight
        let counts: String? = snapshot.map(MachinesUI.counts)
        return HStack(spacing: 6) {
            Image(systemName: "server.rack").foregroundStyle(.secondary)
            Text("Machines").font(.subheadline.bold())
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

    // MARK: status

    private var statusCard: some View {
        SettingsCard(symbol: "server.rack", title: "REMOTE MACHINES", tint: .teal,
                     pill: AllocatorUI.pill(install)) {
            SettingRow(title: "Daemon", summary: daemonSummary) {
                daemonPill
            }
            facts
            SettingRow(title: "Offload builds to other machines", summary: MachinesUI.summary,
                       stacked: true) {
                VStack(alignment: .leading, spacing: 7) {
                    AllocatorMarks(status: install)
                    InstallerButtons(status: install, busy: store.machinesBusy,
                                     ready: RemoteMachines.ready, offersDaemon: true,
                                     restartHelp: MachinesUI.restartHelp,
                                     install: { Task { await store.installMachines() } },
                                     uninstall: { Task { await store.uninstallMachines() } },
                                     start: { Task { await store.startMachinesDaemon() } },
                                     restart: { Task { await store.restartMachinesDaemon() } },
                                     recheck: { Task { await store.refreshMachinesInstall() } })
                }
            }
            SettingRow(title: "Refresh", summary: refreshSummary) {
                HStack(spacing: 6) {
                    if store.machinesBusy { ProgressView().controlSize(.small) }
                    Button { Task { await store.refreshMachines() } } label: {
                        Label("Refresh", systemImage: "arrow.triangle.2.circlepath")
                    }
                    .buttonStyle(.bordered).controlSize(.small)
                    .disabled(!refreshable)
                }
            }
            if store.machineExclusionFailed {
                note(AllocatorUI.saveError, tint: .red)
            }
        }
    }

    private var daemonPill: StatusPill {
        guard let s = install else { return StatusPill(text: "checking", tint: .secondary) }
        if s.daemonRunning { return StatusPill(text: "running", tint: .green, symbol: "bolt.fill") }
        if s.installed { return StatusPill(text: "idle", tint: .secondary, symbol: "moon.zzz.fill") }
        return StatusPill(text: "not installed", tint: .secondary, symbol: "circle.dashed")
    }

    private var daemonSummary: String {
        guard let s = install else { return "Asking the installer…" }
        guard s.installed || s.daemonRunning else { return "Install it to run the daemon." }
        guard s.daemonRunning else { return MachinesUI.idleNote }
        guard let d = snapshot?.daemon else { return RemoteMachines.package.socketPath }
        let pid = d.pid.map { "pid \(clampedInt($0)) · " } ?? ""
        let started = d.startedAt.flatMap(MachinesSnapshot.date)
            .map { "started \(MachinesUI.ago($0, now: now)) · " } ?? ""
        return pid + started + RemoteMachines.package.socketPath
    }

    /// A running daemon can be asked to scan whether or not this checkout installed it.
    private var refreshable: Bool {
        guard let s = install, RemoteMachines.ready, !store.machinesBusy else { return false }
        return s.installed || s.daemonRunning
    }

    private var refreshSummary: String {
        install?.daemonRunning == true
            ? "Discover machines again and re-read every one now."
            : "Start the daemon, which scans as it comes up."
    }

    @ViewBuilder
    private var facts: some View {
        if let snap = snapshot {
            let rows = MachinesUI.facts(snap, running: install?.daemonRunning == true, now: now)
            Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 3) {
                ForEach(rows, id: \.0) { row in
                    GridRow {
                        Text(row.0).font(.system(size: 10)).foregroundStyle(.secondary)
                        Text(row.1).font(.system(size: 10, weight: .medium).monospacedDigit())
                    }
                }
            }
        }
    }

    // MARK: events

    static let eventActions: Set<String> = [
        "machine-pause", "machine-resume", "machine-exclude", "machine-include",
        "machines-install", "machines-uninstall",
    ]

    private var eventsCard: some View {
        let events = store.auditEntries.filter { Self.eventActions.contains($0.action) }.prefix(6)
        return SettingsCard(symbol: "clock.arrow.circlepath", title: "RECENT", tint: .secondary) {
            if events.isEmpty {
                note("No machine actions in the activity feed.")
            } else {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(Array(events)) { AuditRow(entry: $0) }
                }
            }
        }
    }

    // MARK: machines

    @ViewBuilder
    private var machines: some View {
        if let snap = snapshot {
            VStack(alignment: .leading, spacing: 12) {
                if let stale = staleNote(snap) { note(stale) }
                let panels = MachinesUI.panels(snap)
                ForEach(Array(stride(from: 0, to: panels.count, by: 2)), id: \.self) { i in
                    HStack(alignment: .top, spacing: 12) {
                        panel(panels[i])
                        if i + 1 < panels.count { panel(panels[i + 1]) } else { Spacer(minLength: 0) }
                    }
                }
                unavailable(snap)
            }
        } else {
            SettingsCard(symbol: "server.rack", title: "MACHINES", tint: .teal) {
                note("No snapshot yet: the remote-machines daemon has not run on this machine. "
                     + "Once installed, an agent session starts it.")
            }
        }
    }

    private func staleNote(_ snap: MachinesSnapshot) -> String? {
        guard install?.daemonRunning == false,
              let at = snap.updatedAt.flatMap(MachinesSnapshot.date) else { return nil }
        return "The daemon is idle, so these are its last readings, as of \(MachinesUI.ago(at, now: now))."
    }

    private func panel(_ p: MachinesUI.Panel) -> some View {
        MachinePanel(machine: p.machine, rank: p.rank, isLocal: p.isLocal,
                     samples: store.machinesHistory?.machines[p.machine.id] ?? [],
                     intervalMs: store.machinesHistory?.intervalMs ?? 60_000, now: now,
                     onAction: { action in store.setMachineExclusion(action, for: p.machine) })
            .frame(width: MainRoot.columnWidth, alignment: .topLeading)
    }

    @ViewBuilder
    private func unavailable(_ snap: MachinesSnapshot) -> some View {
        let parts = snap.unavailableSummary()
        if !parts.isEmpty {
            let text = "Not usable (\(snap.unavailable.count)): " + parts.joined(separator: "; ") + "."
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: "network.slash").font(.system(size: 9)).foregroundStyle(.secondary)
                Text(text).font(.system(size: 10)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cardChrome()
        }
    }

    private func note(_ text: String, tint: Color = .secondary) -> some View {
        Text(text).font(.system(size: 10)).foregroundStyle(tint)
            .fixedSize(horizontal: false, vertical: true)
    }
}

// MARK: - text

enum MachinesUI {
    struct Panel {
        let machine: RemoteMachine
        let rank: Int?
        let isLocal: Bool
    }

    /// The pool best first, then the excluded ones, then this machine.
    static func panels(_ s: MachinesSnapshot) -> [Panel] {
        s.machines.enumerated().map { Panel(machine: $1, rank: $0 + 1, isLocal: false) }
            + s.excluded.map { Panel(machine: $0, rank: nil, isLocal: false) }
            + (s.local.map { [Panel(machine: $0, rank: nil, isLocal: true)] } ?? [])
    }

    static func counts(_ s: MachinesSnapshot) -> String {
        let paused = s.excluded.filter { !$0.isExcludedForGood }.count
        var parts = ["\(s.machines.count) in pool"]
        if paused > 0 { parts.append("\(paused) paused") }
        if s.excluded.count > paused { parts.append("\(s.excluded.count - paused) excluded") }
        return parts.joined(separator: " · ")
    }

    static func facts(_ s: MachinesSnapshot, running: Bool, now: Date) -> [(String, String)] {
        func age(_ iso: String?) -> String {
            iso.flatMap(MachinesSnapshot.date).map { ago($0, now: now) } ?? "never"
        }
        let sessions = running ? s.daemon?.sessions.map { String(clampedInt($0)) } ?? "unknown" : "0"
        let every = s.probeIntervalMs.map { "every \(number($0 / 1000)) s" } ?? "unknown"
        return [("Agent sessions", sessions), ("Last scan", age(s.updatedAt)),
                ("Last discovery", age(s.discoveredAt)), ("Load read", every)]
    }

    static let idleNote = "Idle, which is not an error: the daemon exits 10 minutes after "
        + "its last agent session and starts with the next one."

    static let restartHelp = "Stop the running daemon and start this checkout's. Its table "
        + "is rebuilt by the next scan."

    static var summary: String {
        let pkg = RemoteMachines.package
        if !pkg.packageAvailable {
            return "⚠ Package not found at \(pkg.packageDir). Set DIPLOMAT_REMOTE_MACHINES_DIR "
                + "to point at it."
        }
        if NodePackage.resolveNode() == nil {
            return "⚠ Node.js not found. Install Node (or set DIPLOMAT_NODE): the MCP server "
                + "and its daemon need it."
        }
        return "Agents list these machines and run builds and tests on the least loaded one."
    }

    /// `12 s ago`, `14 min ago`, `3 h ago`, `2 d ago`.
    static func ago(_ date: Date, now: Date) -> String {
        let s = max(0, clampedInt(now.timeIntervalSince(date)))
        if s < 120 { return "\(s) s ago" }
        if s < 7200 { return "\(s / 60) min ago" }
        if s < 172_800 { return "\(s / 3600) h ago" }
        return "\(s / 86400) d ago"
    }

    /// At most two decimals, none when whole.
    static func number(_ v: Double) -> String {
        guard v.isFinite else { return "?" }
        if v == v.rounded() { return String(clampedInt(v)) }
        var s = String(format: "%.2f", v)
        while s.hasSuffix("0") { s.removeLast() }
        return s
    }

    private static let gb = 1_073_741_824.0

    /// As the daemon's `format.js` writes sizes: one decimal under 10 GB.
    static func gigabytes(_ bytes: Double, unit: Bool = true) -> String {
        String(format: bytes < 10 * gb ? "%.1f" : "%.0f", bytes / gb) + (unit ? " GB" : "")
    }

    static func specs(_ m: RemoteMachine) -> String {
        let os = [m.os?.name, m.os?.arch].compactMap { $0 }.joined(separator: " ")
        let parts: [String?] = [
            os.isEmpty ? nil : os,
            m.cpu?.model,
            m.cores.map { "\(number($0)) cores" },
            m.memory?.totalBytes.map { "\(gigabytes($0)) RAM" },
            m.gpus.flatMap(gpus),
        ]
        return parts.compactMap { $0 }.joined(separator: " · ")
    }

    /// `2× NVIDIA RTX 6000 Ada, Apple M2 Ultra`, in the order the daemon lists them.
    static func gpus(_ gpus: [RemoteMachine.GPU]) -> String? {
        var counts: [(String, Int)] = []
        for name in gpus.compactMap(\.name) where !name.isEmpty {
            if let i = counts.firstIndex(where: { $0.0 == name }) { counts[i].1 += 1 }
            else { counts.append((name, 1)) }
        }
        guard !counts.isEmpty else { return nil }
        return counts.map { $0.1 > 1 ? "\($0.1)× \($0.0)" : $0.0 }.joined(separator: ", ")
    }

    static func percent(_ v: Double) -> String { "\(clampedInt((v * 100).rounded()))%" }

    static func cpuDetail(_ m: RemoteMachine) -> String {
        let load = m.load?.last1m.map(number) ?? "?"
        let cores = m.cores.map(number) ?? "?"
        let spare = m.spareCores.map { " · \(number($0)) spare" } ?? ""
        return "load \(load) on \(cores) cores\(spare)"
    }

    static func memoryDetail(_ m: RemoteMachine) -> String {
        guard let total = m.memory?.totalBytes else { return "" }
        let used = m.memory?.availableBytes.map { total - $0 }
        var s = used.map { "\(gigabytes($0, unit: false)) of \(gigabytes(total)) used" }
            ?? "\(gigabytes(total))"
        if let p = m.memory?.pressure, p == "warning" || p == "critical" { s += " · pressure \(p)" }
        return s
    }

    static func detail(_ m: RemoteMachine, now: Date) -> String {
        var parts: [String] = []
        if let n = m.sshSessions { parts.append("SSH sessions \(number(n))") }
        if let free = m.disk?.freeBytes { parts.append("\(gigabytes(free)) disk free") }
        if let at = m.probedAt.flatMap(MachinesSnapshot.date) { parts.append("read \(ago(at, now: now))") }
        return parts.joined(separator: " · ")
    }

    static func color(_ tone: UsageTone) -> Color {
        switch tone {
        case .normal: return .green
        case .high: return .orange
        case .over: return .red
        }
    }

    static let cpuColor = Color.blue
    static let memoryColor = Color.purple
    static let sshColor = Color.orange
}

// MARK: - one machine

struct MachinePanel: View {
    let machine: RemoteMachine
    /// Its place in the pool; nil out of it.
    let rank: Int?
    let isLocal: Bool
    let samples: [MachineHistory.Sample]
    let intervalMs: Double
    let now: Date
    let onAction: (MachineExclusions.Action) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            title
            let specs = MachinesUI.specs(machine)
            if !specs.isEmpty {
                Text(specs).font(.system(size: 10)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            bars
            let detail = MachinesUI.detail(machine, now: now)
            if !detail.isEmpty {
                Text(detail).font(.system(size: 9).monospacedDigit()).foregroundStyle(.secondary)
            }
            ForEach(machine.warnings ?? [], id: \.self) { w in
                Label(w, systemImage: "exclamationmark.triangle.fill")
                    .font(.system(size: 10)).foregroundStyle(.orange)
            }
            MachineHistoryChart(samples: samples, intervalMs: intervalMs, now: now)
            actions
        }
        .padding(.horizontal, 10).padding(.vertical, 9)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.gray.opacity(0.11)))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.gray.opacity(0.22), lineWidth: 1))
    }

    private var name: String { isLocal ? "This machine" : machine.target ?? machine.id }

    private var secondary: String {
        if isLocal { return machine.machineName ?? "" }
        return machine.aliases.map { $0.joined(separator: ", ") } ?? ""
    }

    private var login: String {
        guard !isLocal, let host = machine.hostname else { return "" }
        let user = machine.user.map { "\($0)@" } ?? ""
        let port = machine.port.map { $0 == 22 ? "" : ":\(clampedInt($0))" } ?? ""
        return user + host + port
    }

    private var title: some View {
        HStack(spacing: 6) {
            if let rank {
                Text("#\(rank)").font(.system(size: 10, weight: .heavy).monospacedDigit())
                    .foregroundStyle(.white)
                    .padding(.horizontal, 5).padding(.vertical, 1)
                    .background(Capsule().fill(Color.teal))
            }
            Text(name).font(.system(size: 12, weight: .semibold)).lineLimit(1)
            if !secondary.isEmpty {
                Text(secondary).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
            }
            if !login.isEmpty {
                Text(login).font(.system(size: 9, design: .monospaced)).foregroundStyle(.secondary)
                    .lineLimit(1).truncationMode(.middle)
            }
            Spacer(minLength: 4)
            chips
        }
    }

    @ViewBuilder
    private var chips: some View {
        if isLocal {
            StatusPill(text: "never offloaded to", tint: .secondary)
        } else if let until = machine.pausedUntil {
            StatusPill(text: "Paused until \(Fmt.clock(until))", tint: .orange, symbol: "pause.fill")
        } else if machine.isExcludedForGood {
            StatusPill(text: "Excluded", tint: .secondary, symbol: "nosign")
        }
        let warnings = machine.warnings?.count ?? 0
        if warnings > 0 {
            StatusPill(text: warnings == 1 ? "1 warning" : "\(warnings) warnings", tint: .orange,
                       symbol: "exclamationmark.triangle.fill")
        }
    }

    @ViewBuilder
    private var bars: some View {
        if let cpu = machine.usage?.cpu {
            UsageBar(label: "CPU", fill: cpu, tone: UsageTone.cpu(cpu),
                     detail: MachinesUI.cpuDetail(machine))
        } else {
            UsageBar(label: "CPU", fill: nil, tone: .normal, detail: unreadNote)
        }
        if let mem = machine.usage?.memory {
            UsageBar(label: "RAM", fill: mem, tone: UsageTone.memory(mem),
                     detail: MachinesUI.memoryDetail(machine))
        } else {
            UsageBar(label: "RAM", fill: nil, tone: .normal, detail: unreadNote)
        }
    }

    private var unreadNote: String {
        machine.isExcludedForGood ? "not read while excluded" : "not read"
    }

    @ViewBuilder
    private var actions: some View {
        if !isLocal {
            HStack(spacing: 6) {
                Spacer(minLength: 0)
                if machine.isExcludedForGood {
                    button("Include", "plus.circle", "Put it back in the pool.") { onAction(.include) }
                } else {
                    if machine.pausedUntil != nil {
                        button("Resume", "play.fill", "Put it back in the pool now.") { onAction(.resume) }
                    } else {
                        pauseMenu
                    }
                    button("Exclude", "nosign",
                           "Take it out of the pool until included again. It is not contacted meanwhile.") {
                        onAction(.exclude)
                    }
                }
            }
        }
    }

    private var pauseMenu: some View {
        Menu {
            Button("1 hour") { onAction(.pause(3600)) }
            Button("4 hours") { onAction(.pause(4 * 3600)) }
            Button("24 hours") { onAction(.pause(24 * 3600)) }
        } label: {
            Label("Pause", systemImage: "pause.fill")
        }
        .menuStyle(.borderedButton).controlSize(.small).fixedSize()
        .tooltip("Take it out of the pool for a while. Its load is still read.")
    }

    private func button(_ title: String, _ symbol: String, _ help: String,
                        _ action: @escaping () -> Void) -> some View {
        Button(action: action) { Label(title, systemImage: symbol) }
            .buttonStyle(.bordered).controlSize(.small)
            .tooltip(help)
    }
}

/// One labelled usage bar; `fill` nil draws it empty.
private struct UsageBar: View {
    let label: String
    let fill: Double?
    let tone: UsageTone
    let detail: String

    var body: some View {
        let share = min(1, max(0, fill ?? 0))
        let percent = fill.map(MachinesUI.percent) ?? "–"
        let tint = MachinesUI.color(tone)
        HStack(spacing: 8) {
            Text(label).font(.system(size: 9, weight: .bold)).foregroundStyle(.secondary)
                .frame(width: 26, alignment: .leading)
            SplitBar(left: share, right: 1 - share, leftColor: tint, rightColor: Color.gray.opacity(0.5))
                .frame(width: 170, height: 8)
                .opacity(fill == nil ? 0.5 : 1)
            Text(percent).font(.system(size: 10, weight: .bold).monospacedDigit())
                .foregroundStyle(fill == nil ? Color.secondary : tint)
                .frame(width: 38, alignment: .trailing)
            Text(detail).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
            Spacer(minLength: 0)
        }
    }
}

// MARK: - history chart

/// CPU and RAM on a 0-100 % axis on the left, SSH sessions as steps on the right. The
/// span runs from the first reading (at least an hour back) to now, a pixel a column.
struct MachineHistoryChart: View {
    let samples: [MachineHistory.Sample]
    let intervalMs: Double
    let now: Date

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 8) {
                Text("HISTORY").font(.system(size: 8, weight: .bold)).foregroundStyle(.secondary)
                    .kerning(0.5)
                Spacer(minLength: 0)
                legend("CPU", MachinesUI.cpuColor)
                legend("RAM", MachinesUI.memoryColor)
                legend("SSH", MachinesUI.sshColor)
            }
            if samples.isEmpty {
                Text("no readings yet").font(.system(size: 10)).foregroundStyle(.tertiary)
                    .frame(maxWidth: .infinity, minHeight: 36)
                    .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.06)))
            } else {
                chart.frame(height: 90)
            }
        }
    }

    private func legend(_ text: String, _ color: Color) -> some View {
        HStack(spacing: 3) {
            RoundedRectangle(cornerRadius: 1).fill(color).frame(width: 9, height: 3)
            Text(text).font(.system(size: 8, weight: .semibold)).foregroundStyle(.secondary)
        }
    }

    private var chart: some View {
        Canvas { ctx, size in
            let padL: CGFloat = 26, padR: CGFloat = 16, padT: CGFloat = 5, padB: CGFloat = 13
            let w = size.width - padL - padR, h = size.height - padT - padB
            guard w > 1, h > 1 else { return }
            let end = now.timeIntervalSince1970 * 1000
            let start = min(samples.first?.at ?? end, end - 3_600_000)
            let cols = MachineHistory.columns(samples, from: start, to: end, width: Int(w),
                                              gapMs: 3 * intervalMs)
            let sshTop = max(1, cols.compactMap(\.ssh).max() ?? 0)

            func x(_ c: MachineHistory.Column) -> CGFloat { padL + CGFloat(c.index) + 0.5 }
            func y(_ share: Double) -> CGFloat { padT + h * CGFloat(1 - min(1, max(0, share))) }

            for share in [0.0, 0.5, 1.0] {
                var grid = Path()
                grid.move(to: CGPoint(x: padL, y: y(share)))
                grid.addLine(to: CGPoint(x: padL + w, y: y(share)))
                ctx.stroke(grid, with: .color(.gray.opacity(0.25)),
                           style: StrokeStyle(lineWidth: 0.5, dash: share == 0 ? [] : [2, 3]))
            }

            // The SSH steps first, so the two percentages read on top of them.
            var steps = Path()
            var last: CGPoint?
            for c in cols {
                guard let v = c.ssh else { last = nil; continue }
                let p = CGPoint(x: x(c), y: y(v / sshTop))
                if let l = last, !c.breaksBefore {
                    steps.addLine(to: CGPoint(x: p.x, y: l.y))
                    steps.addLine(to: p)
                } else {
                    steps.move(to: p)
                }
                last = p
            }
            ctx.stroke(steps, with: .color(MachinesUI.sshColor.opacity(0.8)), lineWidth: 1)

            for (value, color) in [(\MachineHistory.Column.memory, MachinesUI.memoryColor),
                                   (\MachineHistory.Column.cpu, MachinesUI.cpuColor)] {
                var line = Path()
                var open = false
                var segment: [CGPoint] = []
                func close() {
                    // A reading with none beside it is a dot, not an invisible line.
                    if segment.count == 1, let p = segment.first {
                        ctx.fill(Path(ellipseIn: CGRect(x: p.x - 1.2, y: p.y - 1.2, width: 2.4, height: 2.4)),
                                 with: .color(color))
                    }
                    segment = []
                }
                for c in cols {
                    guard let v = c[keyPath: value] else { open = false; close(); continue }
                    let p = CGPoint(x: x(c), y: y(v))
                    if open && !c.breaksBefore {
                        line.addLine(to: p)
                    } else {
                        close()
                        line.move(to: p)
                    }
                    segment.append(p)
                    open = true
                }
                close()
                ctx.stroke(line, with: .color(color), lineWidth: 1.3)
            }

            ctx.draw(axisText("100%"), at: CGPoint(x: padL - 3, y: y(1)), anchor: .trailing)
            ctx.draw(axisText("0%"), at: CGPoint(x: padL - 3, y: y(0)), anchor: .trailing)
            ctx.draw(axisText(MachinesUI.number(sshTop)), at: CGPoint(x: padL + w + 3, y: y(1)),
                     anchor: .leading)
            ctx.draw(axisText("0"), at: CGPoint(x: padL + w + 3, y: y(0)), anchor: .leading)
            let span = Fmt.duration((end - start) / 1000)
            ctx.draw(axisText("\(span) ago"), at: CGPoint(x: padL, y: padT + h + 7), anchor: .leading)
            ctx.draw(axisText("now"), at: CGPoint(x: padL + w, y: padT + h + 7), anchor: .trailing)
        }
    }
}
