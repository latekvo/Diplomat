import SwiftUI
import ArgentUtilsCore

/// The settings screen — swapped in for the main panel body when the header gear
/// is tapped. Two knobs: the GitHub handle to treat as "me", and which tool cards
/// show in the grid. Both persist via the Store (UserDefaults-backed).
struct SettingsView: View {
    @EnvironmentObject var store: Store
    @Binding var isPresented: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            headerRow
            // Two columns, same layout as the main panel: identity + automation
            // behaviour on the left, appearance + environment on the right.
            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading, spacing: 16) {
                    identitySection
                    autofixSection
                    apiWatchSection
                }
                .frame(width: PopoverRoot.columnWidth, alignment: .topLeading)

                VStack(alignment: .leading, spacing: 16) {
                    toolsSection
                    terminalSection
                    allocatorSection
                    meshSection
                    updateSection
                }
                .frame(width: PopoverRoot.columnWidth, alignment: .topLeading)
            }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .task {
            // Freshen the allocator status only. This used to also fire a full GitHub
            // poll on EVERY Settings open — two GraphQL searches against the shared
            // 5000 pt/hr budget (and potential agent dispatch) from a view-appear
            // hook; the monitor's own cadence + wake trigger keep the rows fresh.
            await store.refreshAllocatorInstall()
            // Cheap local git fetch/compare — off the UI thread inside the Store.
            store.refreshUpdateStatus()
            if store.meshEnabled { await store.meshTick() }
        }
    }

    // MARK: mesh (LAN P2P duty coordination)

    /// Precomputed as a `String` (not concatenated inside the ViewBuilder) so the Settings
    /// column stays within the SwiftUI type-checker's reach — same pattern as `apiWatchBlurb`.
    private var meshBlurb: String {
        "Runs a small peer-to-peer node that discovers the other Argent Utils machines on "
            + "your LAN (UDP beacons) and routes duty work — reviews, conflict fixes, the full "
            + "E2E audit — to whichever node fits the placement policy (weakest-first by default, "
            + "token- and platform-aware). Configure the whole mesh from the ⬡ Mesh screen (the "
            + "⬡ button in the panel header). Off by default; no node opens on the network until "
            + "you enable it here."
    }

    private var meshSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            sectionLabel("MESH (LAN P2P)")
            Toggle(isOn: $store.meshEnabled) {
                Text("Coordinate duties with other machines on this LAN").font(.caption)
            }
            .toggleStyle(.switch).controlSize(.small)
            meshStatusRow
            Text(meshBlurb)
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder
    private var meshStatusRow: some View {
        if store.meshEnabled {
            let running = MeshBridge.nodeRunning(store.meshState)
            let peers = store.meshState?.peers.count ?? 0
            HStack(spacing: 5) {
                Image(systemName: running ? "bolt.fill" : "bolt.slash.fill")
                    .font(.system(size: 9)).foregroundStyle(running ? .green : .orange)
                Text(running
                     ? "Node running · \(peers) peer\(peers == 1 ? "" : "s")"
                     : (store.meshState == nil ? "Starting node…" : "Node not running"))
                    .font(.caption2).foregroundStyle(running ? .green : .orange)
            }
        }
    }

    // MARK: applet update

    private var updateSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            sectionLabel("UPDATE")
            updateStatusRow
            HStack(spacing: 8) {
                Button { store.updateApp() } label: { Text("Update").bold() }
                    .buttonStyle(.borderedProminent).controlSize(.small)
                    .disabled(!(store.updateState.map { !$0.isBusy } ?? false))
                Button { store.refreshUpdateStatus() } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.borderless).controlSize(.small).help("Re-check for updates")
            }
            Text(updateBlurb)
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var updateBlurb: String {
        "Pulls the latest applet from GitHub, rebuilds the argent-core prompt engine and the "
            + "app bundle, and relaunches it in place."
    }

    @ViewBuilder
    private var updateStatusRow: some View {
        // nil (before the first check) reads as "checking", matching the Linux front-end.
        switch store.updateState ?? .checking {
        case .checking:
            updateStatus("Checking…", .secondary, detail: "comparing with origin…")
        case .updating(let step):
            updateStatus("Updating…", .orange, detail: step)
        case .restarting(let commit):
            updateStatus("Restarting…", .green,
                         detail: "relaunched at \(commit) — this instance is handing over")
        case .failed(let err):
            updateStatus("Update failed", .red, detail: err)
        case .idle(let r):
            if let e = r.error {
                updateStatus("Check failed", .orange, detail: e)
            } else if let behind = r.behind, behind > 0 {
                // A diverged checkout still updates — via a merge, not a discard.
                let aheadNote = (r.ahead ?? 0) > 0 ? " · \(r.ahead!) local ahead (will merge)" : ""
                updateStatus("Update available · \(behind) commit\(behind == 1 ? "" : "s") behind", .blue,
                             detail: "\(r.commit ?? "?") on \(r.branch ?? "?") · upstream \(r.upstream ?? "?")\(aheadNote)")
            } else {
                let aheadNote = (r.ahead ?? 0) > 0 ? " · \(r.ahead!) local ahead" : ""
                updateStatus("Up to date", .primary,
                             detail: "\(r.commit ?? "?") on \(r.branch ?? "?") · upstream \(r.upstream ?? "?")\(aheadNote)")
            }
        }
    }

    private func updateStatus(_ text: String, _ color: Color, detail: String) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(text).font(.caption.bold()).foregroundStyle(color)
            Text(detail).font(.system(size: 9, design: .monospaced)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: PR auto-fix monitor

    private var autofixSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            sectionLabel("PR AUTO-FIX")
            Toggle(isOn: $store.prAutofixEnabled) {
                Text("Auto-fix my PRs (conflicts + reviews)").font(.caption)
            }
            .toggleStyle(.switch)
            .controlSize(.small)
            autofixDetail
            pollErrorRow

            Toggle(isOn: $store.reviewRequestsEnabled) {
                Text("Full-E2E review PRs that request my review").font(.caption)
            }
            .toggleStyle(.switch)
            .controlSize(.small)
            Text(reviewRequestsBlurb)
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            if store.reviewRequestsEnabled { unaddressedReviewsRow }

            if store.reviewRequestsEnabled { autoApproveBlock }
        }
    }

    /// Master switch for auto-approvals + (when on) the per-class verdict suppressors.
    /// Off by default: an auto-review never submits a verdict on my behalf until I opt in.
    private var autoApproveBlock: some View {
        VStack(alignment: .leading, spacing: 4) {
            Toggle(isOn: $store.autoApproveEnabled) {
                Text("Let auto-reviews approve / request changes").font(.caption)
            }
            .toggleStyle(.switch).controlSize(.small)
            .padding(.top, 2)
            Text("Off ⇒ every auto-review leaves inline comments only; the approve / "
                 + "request-changes call stays with you. On ⇒ a clean review may submit a "
                 + "verdict, except where withheld below.")
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if store.autoApproveEnabled { verdictPolicyBlock }
        }
    }

    private var reviewRequestsBlurb: String {
        let base = "When someone requests my review on a PR, spawns the most thorough review "
            + "(Full E2E ×2, leaving inline comments) — read-only, never touches their branch. "
            + "A review left unaddressed (agent died, lost connection, window closed) is retried "
            + "automatically until it lands."
        let handled = store.reviewRequestsHandled > 0 ? "  Reviewed \(store.reviewRequestsHandled) so far." : ""
        return base + handled
    }

    /// Shown while the monitor's polls are failing (gh auth expired, network, GraphQL
    /// errors) — previously this failure mode was completely silent: the toggles said
    /// "on", the counts froze stale, and nothing dispatched.
    @ViewBuilder
    private var pollErrorRow: some View {
        if let err = store.autofixPollError {
            HStack(alignment: .firstTextBaseline, spacing: 5) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.system(size: 9)).foregroundStyle(Color.red)
                Text("Polls failing since \(Fmt.clock(store.autofixPollErrorAt)) — \(err)")
                    .font(.caption2).foregroundStyle(Color.red)
                    .lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// Shown while any review I owe has no agent on it — the reconciler is retrying them.
    @ViewBuilder
    private var unaddressedReviewsRow: some View {
        if store.unaddressedReviews > 0 {
            let n = store.unaddressedReviews
            HStack(spacing: 5) {
                Image(systemName: "arrow.triangle.2.circlepath")
                    .font(.system(size: 9)).foregroundStyle(Color.orange)
                Text("\(n) unaddressed review\(n == 1 ? "" : "s") — retrying")
                    .font(.caption2).foregroundStyle(Color.orange)
            }
        }
    }

    /// The three configurable suppressors for the auto-review's "final pass + verdict".
    /// A PR matching any enabled row gets comments only; otherwise it gets a verdict.
    private var verdictPolicyBlock: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("WITHHOLD THE FINAL VERDICT WHEN THE PR…")
                .font(.system(size: 9, weight: .bold)).foregroundStyle(.secondary).kerning(0.5)
                .padding(.top, 4)
            verdictToggle("…touches a SKILL", isOn: $store.verdictWithholdSkill)
            verdictToggle("…touches the installer", isOn: $store.verdictWithholdInstaller)
            verdictToggle("…is a community PR (author outside the org)", isOn: $store.verdictWithholdCommunity)
            Text("Off for all three ⇒ every auto-review may approve or request changes. "
                 + "On ⇒ that class gets inline comments only; the final call stays with you.")
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.leading, 10)
    }

    private func verdictToggle(_ label: String, isOn: Binding<Bool>) -> some View {
        Toggle(isOn: isOn) { Text(label).font(.caption) }
            .toggleStyle(.switch).controlSize(.mini)
    }

    @ViewBuilder
    private var autofixDetail: some View {
        if store.prAutofixEnabled {
            let live = store.autofixStatus?.isLive == true
            let n = store.autofixStatus?.watching ?? 0
            HStack(spacing: 5) {
                Image(systemName: live ? "bolt.fill" : "bolt.slash.fill")
                    .font(.system(size: 9)).foregroundStyle(live ? Color.green : Color.orange)
                Text(live
                     ? "Active — a monitor is watching \(n) open PR\(n == 1 ? "" : "s")."
                     : "Enabled, but no monitor is running right now.")
                    .font(.caption2).foregroundStyle(live ? Color.green : Color.orange)
            }
        }
        Text("When on, an agent watches your open PRs and automatically resolves merge conflicts and addresses new review threads. Turning it off pauses agent dispatch.")
            .font(.caption2).foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
    }

    // MARK: Claude API-error watcher

    private var apiWatchBlurb: String {
        let base = "Watches every iTerm/Terminal session; when a Claude API error shows up "
            + "(e.g. \u{201C}529 Overloaded\u{201D}), it sends \u{201C}\(ApiErrorWatcher.continueMessage)\u{201D} "
            + "so a stalled agent resumes on its own. Out-of-quota stalls "
            + "(\u{201C}You've hit your weekly limit\u{201D}) are left alone — nudging can't help "
            + "until the limit resets."
        let count = store.apiWatchContinues > 0 ? "  Continued \(store.apiWatchContinues)× so far." : ""
        return base + count
    }

    private var apiWatchSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            sectionLabel("CLAUDE API ERRORS")
            Toggle(isOn: $store.apiWatchEnabled) {
                Text("Auto-continue agents on API errors").font(.caption)
            }
            .toggleStyle(.switch)
            .controlSize(.small)
            Text(apiWatchBlurb)
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var headerRow: some View {
        HStack(spacing: 6) {
            Image(systemName: "gearshape.fill").foregroundStyle(.secondary)
            Text("Settings").font(.subheadline.bold())
            Spacer()
            Button { withAnimation(.easeInOut(duration: 0.15)) { isPresented = false } } label: {
                Text("Done").bold()
            }
            .buttonStyle(.borderless)
            .keyboardShortcut(.cancelAction)
        }
    }

    // MARK: GitHub identity

    private var trimmedOverride: String {
        store.usernameOverride.trimmingCharacters(in: .whitespaces)
    }

    private var identitySection: some View {
        VStack(alignment: .leading, spacing: 6) {
            sectionLabel("GITHUB USERNAME")
            HStack(spacing: 6) {
                Image(systemName: "at").font(.caption).foregroundStyle(.secondary)
                TextField(store.me.isEmpty ? "your github handle" : store.me,
                          text: $store.usernameOverride)
                    .textFieldStyle(.plain)
                    .font(.callout)
                if !trimmedOverride.isEmpty {
                    Button { store.usernameOverride = "" } label: {
                        Image(systemName: "xmark.circle.fill")
                    }
                    .buttonStyle(.borderless).foregroundStyle(.secondary)
                    .help("Clear — fall back to the gh-authenticated user")
                }
            }
            .padding(8)
            .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.1)))

            Text(trimmedOverride.isEmpty
                 ? "Using the gh-authenticated user\(store.me.isEmpty ? "" : " (@\(store.me))"). Scopes the “My …” tools and the Review wizard."
                 : "Overriding to @\(trimmedOverride) for the “My …” tools and the Review wizard.")
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: Tool visibility

    private var toolsSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionLabel("TOOLS — COLOR & VISIBILITY")
            ForEach(ToolKind.allCases) { kind in
                HStack(spacing: 8) {
                    IconBadge(symbol: kind.systemImage, tint: store.tint(for: kind))
                    VStack(alignment: .leading, spacing: 1) {
                        Text(kind.title).font(.caption.bold())
                        Text(kind.subtitle).font(.system(size: 9)).foregroundStyle(.secondary).lineLimit(1)
                    }
                    Spacer(minLength: 6)
                    ColorPicker("", selection: Binding(
                        get: { store.tint(for: kind) },
                        set: { store.setTint($0, for: kind) }
                    ), supportsOpacity: false)
                        .labelsHidden()
                        .help("Tint for \(kind.title)")
                    Toggle("", isOn: Binding(
                        get: { !store.hiddenTools.contains(kind.rawValue) },
                        set: { store.setTool(kind, visible: $0) }
                    ))
                        .labelsHidden()
                        .toggleStyle(.switch)
                        .tint(store.tint(for: kind))
                        .help("Show \(kind.title) in the grid")
                }
            }
        }
    }

    // MARK: Terminal

    private var terminalSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            sectionLabel("SPAWN TERMINAL")
            Picker("", selection: $store.terminalChoice) {
                ForEach(SpawnTerminal.allCases) { term in
                    Text(term.title + (term.isInstalled ? "" : " (not installed)")).tag(term.rawValue)
                }
            }
            .labelsHidden()
            .pickerStyle(.segmented)
            Text("SPAWN AGENT opens a new \(AgentSpawner.resolved(store.terminal).title) window. iTerm is used when installed; otherwise Terminal.")
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: Device allocator (MCP server + skill + rule)

    @ViewBuilder
    private var allocatorSection: some View {
        let s = store.allocatorInstall
        VStack(alignment: .leading, spacing: 6) {
            sectionLabel("DEVICE ALLOCATOR (MCP)")
            HStack(spacing: 8) {
                Image(systemName: (s?.installed ?? false) ? "checkmark.seal.fill" : "exclamationmark.triangle.fill")
                    .foregroundStyle((s?.installed ?? false) ? .green : .orange)
                VStack(alignment: .leading, spacing: 1) {
                    Text(s == nil ? "Checking…" : ((s?.installed ?? false) ? "Installed" : "Not installed"))
                        .font(.caption.bold())
                    Text(statusDetail(s)).font(.system(size: 9, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                if s?.daemonRunning ?? false {
                    HStack(spacing: 3) {
                        Image(systemName: "bolt.fill").font(.system(size: 8))
                        Text("daemon").font(.system(size: 9))
                    }.foregroundStyle(.green)
                }
            }
            HStack(spacing: 8) {
                Button { Task { await store.installAllocator() } } label: {
                    Text((s?.installed ?? false) ? "Reinstall" : "Install").bold()
                }
                .buttonStyle(.borderedProminent).controlSize(.small)
                .disabled(!DeviceAllocator.packageAvailable || !DeviceAllocator.nodeAvailable)
                if s?.installed ?? false {
                    Button { Task { await store.uninstallAllocator() } } label: { Text("Uninstall") }
                        .buttonStyle(.bordered).controlSize(.small)
                }
                Button { Task { await store.refreshAllocatorInstall() } } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.borderless).controlSize(.small).help("Re-check status")
            }
            Text(allocatorHint)
                .font(.caption2)
                .foregroundStyle(allocatorReady ? Color.secondary : Color.orange)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var allocatorReady: Bool {
        DeviceAllocator.packageAvailable && DeviceAllocator.nodeAvailable
    }

    private var allocatorHint: String {
        if !DeviceAllocator.packageAvailable {
            return "Package not found at \(DeviceAllocator.packageDir). Set ARGENT_DEVICE_ALLOCATOR_DIR to point at it."
        }
        if !DeviceAllocator.nodeAvailable {
            return "Node.js not found. Install Node (or set ARGENT_NODE) — the allocator's MCP server and daemon need it to run."
        }
        return "Forces every local agent to reserve an emulator/simulator before using it (MCP server + skill + always-on rule), so agents never collide on a shared device. Reclaims a device when its agent dies or it sits idle for 15 minutes."
    }

    private func statusDetail(_ s: AllocatorInstall?) -> String {
        guard let s else { return "querying the installer…" }
        func mark(_ b: Bool) -> String { b ? "✓" : "✗" }
        return "MCP \(mark(s.mcpRegistered)) · skill \(mark(s.skillInstalled)) · "
            + "rule \(mark(s.ruleInstalled)) · CLAUDE.md \(mark(s.claudeMdInjected))"
    }

    private func sectionLabel(_ text: String) -> some View {
        Text(text).font(.caption2.bold()).foregroundStyle(.secondary).kerning(0.5)
    }
}
