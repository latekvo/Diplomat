import SwiftUI
import AppKit
import ArgentUtilsCore

// The review-depth model and ReviewConfig prompt builder now live in
// ArgentUtilsCore (driven by core/review.json) and are shared verbatim with the
// Linux front-end. This file keeps only the macOS-specific bits: the terminal
// chooser, the AppleScript/iTerm spawner, and the SwiftUI wizard view.

// MARK: - Terminal choice

/// Which terminal SPAWN AGENT drives. iTerm is preferred; Terminal.app is the
/// always-present fallback.
enum SpawnTerminal: String, CaseIterable, Identifiable {
    case iterm, terminal
    var id: String { rawValue }

    var title: String { self == .iterm ? "iTerm" : "Terminal" }
    var bundleID: String { self == .iterm ? "com.googlecode.iterm2" : "com.apple.Terminal" }
    /// The name AppleScript addresses the app by.
    var appName: String { self == .iterm ? "iTerm" : "Terminal" }
    var isInstalled: Bool {
        NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) != nil
    }
}

// MARK: - Spawning a detached claude session in a terminal

/// Opens a brand-new terminal window running `claude "<prompt>"`, fully detached
/// from this applet. The prompt is written to a temp file and read back with
/// `$(cat …)` so we never have to wrestle a multi-line prompt through nested
/// shell + AppleScript quoting.
enum AgentSpawner {
    /// The local checkout the agent works in. Personal-machine path; the `cd` is
    /// best-effort (`;`, not `&&`) so `claude` still starts if it ever moves.
    static let repoPath = "/Users/ignacylatka/dev/argent"

    /// Resolve the terminal to actually drive: the preferred one if installed,
    /// else the first installed alternative, else Terminal.app (always present).
    static func resolved(_ preferred: SpawnTerminal) -> SpawnTerminal {
        if preferred.isInstalled { return preferred }
        return SpawnTerminal.allCases.first(where: { $0.isInstalled }) ?? .terminal
    }

    /// Proactively provoke the macOS "control <terminal>" automation prompt so the
    /// user grants it up front instead of on first SPAWN. No-op once granted; runs
    /// fire-and-forget so a pending prompt never blocks startup.
    static func triggerAutomationPrompt(preferred: SpawnTerminal) {
        let term = resolved(preferred)
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        proc.arguments = ["-e", "tell application \"\(term.appName)\" to get version"]
        proc.standardOutput = Pipe()
        proc.standardError = Pipe()
        try? proc.run()   // don't wait — the prompt itself is the point
    }

    enum SpawnError: LocalizedError {
        case write(String)
        case osascript(code: Int32, stderr: String)

        var errorDescription: String? {
            switch self {
            case .write(let m): return "Couldn't stage prompt: \(m)"
            case .osascript(let code, let stderr):
                let s = stderr.trimmingCharacters(in: .whitespacesAndNewlines)
                return "osascript exited \(code): \(s.isEmpty ? "(no stderr)" : s)"
            }
        }
    }

    /// What a spawn produced: the staged prompt file plus the OS-level handles the
    /// applet keeps so it can later focus the window and detect completion.
    struct SpawnResult {
        let promptFile: URL
        let donePath: String
        let terminal: SpawnTerminal
        let windowID: String
        let sessionID: String
        let tty: String
    }

    /// Stage the prompt, open the terminal, run claude. The AppleScript reports the
    /// new window/session/tty back on stdout, which we capture so the spawned
    /// session can be tracked (focused + polled for completion) afterwards.
    @discardableResult
    static func spawn(_ prompt: String, terminal preferred: SpawnTerminal) throws -> SpawnResult {
        let term = resolved(preferred)
        let file = try writePrompt(prompt)
        let donePath = doneFilePath()
        let cmd = shellCommand(promptFile: file, donePath: donePath)
        let (wid, sid, tty) = try runSpawn(command: cmd, terminal: term)
        return SpawnResult(promptFile: file, donePath: donePath, terminal: term,
                           windowID: wid, sessionID: sid, tty: tty)
    }

    /// Open a new terminal window running `command`, returning the captured
    /// (windowID, sessionID, tty). The execution path shared by the real spawn and
    /// the tracking self-test (`ARGENT_UTILS_TRACK_TEST`).
    static func runSpawn(command: String, terminal term: SpawnTerminal) throws -> (String, String, String) {
        let captured = try runOsascriptCapturing(appleScript(for: term, shellCommand: command))
        return parseCapture(captured)
    }

    static func writePrompt(_ prompt: String) throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("argent-utils-review-\(UUID().uuidString).txt")
        do { try prompt.write(to: url, atomically: true, encoding: .utf8) }
        catch { throw SpawnError.write(error.localizedDescription) }
        return url
    }

    /// A fresh path for the per-spawn completion sentinel (not created until the
    /// spawned shell writes it). Lives under ~/.argent, NOT the temp dir — macOS
    /// purges temp files after ~3 days, and the sweep re-derives `done` from the
    /// file's existence, so a purged sentinel flipped long-lived completed sessions
    /// back to "running".
    static func doneFilePath() -> String {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".argent/pr-monitor/done")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("argent-utils-done-\(UUID().uuidString).txt").path
    }

    /// `cd '<repo>' 2>/dev/null; claude "$(cat '<promptfile>')"; printf %s $? > '<done>'`
    ///
    /// The trailing `printf … > done` writes a sentinel the moment `claude` returns,
    /// so the applet can mark the session complete even while its window stays open.
    static func shellCommand(promptFile: URL, donePath: String) -> String {
        "cd \(shq(repoPath)) 2>/dev/null; claude \"$(cat \(shq(promptFile.path)))\"; printf %s $? > \(shq(donePath))"
    }

    /// Wrap the shell command in an "open a new window, run this, and report the
    /// window id / session id / tty" script for the given terminal. The trailing
    /// `return …` line makes osascript print `wid|sid|tty` on stdout.
    static func appleScript(for term: SpawnTerminal, shellCommand cmd: String) -> String {
        let esc = cmd
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        switch term {
        case .iterm:
            return """
            tell application "iTerm"
                activate
                set w to (create window with default profile)
                set _sid to ""
                set _tty to ""
                tell current session of w
                    write text "\(esc)"
                    set _tty to tty
                    set _sid to id
                end tell
                set _wid to (id of w) as string
            end tell
            return _wid & "|" & _sid & "|" & _tty
            """
        case .terminal:
            return """
            tell application "Terminal"
                activate
                set _tab to do script "\(esc)"
                set _tty to tty of _tab
                set _wid to (id of front window) as string
            end tell
            return _wid & "||" & _tty
            """
        }
    }

    /// Split osascript's `wid|sid|tty` line. Empty middle field is expected for
    /// Terminal.app (no stable session id). Missing fields degrade to "".
    static func parseCapture(_ s: String) -> (String, String, String) {
        let parts = s.trimmingCharacters(in: .whitespacesAndNewlines)
            .split(separator: "|", omittingEmptySubsequences: false).map(String.init)
        return (parts.count > 0 ? parts[0] : "",
                parts.count > 1 ? parts[1] : "",
                parts.count > 2 ? parts[2] : "")
    }

    /// POSIX single-quote a path for safe embedding in the shell command.
    private static func shq(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    /// Run an AppleScript, returning its stdout. Throws on a non-zero exit.
    @discardableResult
    private static func runOsascriptCapturing(_ script: String) throws -> String {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        proc.arguments = ["-e", script]
        let outPipe = Pipe()
        let errPipe = Pipe()
        proc.standardOutput = outPipe
        proc.standardError = errPipe
        do { try proc.run() }
        catch { throw SpawnError.osascript(code: -1, stderr: "\(error)") }
        let outData = outPipe.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        if proc.terminationStatus != 0 {
            let err = String(data: errPipe.fileHandleForReading.readDataToEndOfFile(),
                             encoding: .utf8) ?? ""
            throw SpawnError.osascript(code: proc.terminationStatus, stderr: err)
        }
        return String(data: outData, encoding: .utf8) ?? ""
    }
}

// MARK: - Shared SPAWN AGENT button

/// The tinted "SPAWN AGENT" button shared by the Review and Resolve-conflicts
/// wizards: full-width, coloured when the config is valid and grey when not, with
/// a help string naming the terminal it will open.
struct SpawnAgentButton: View {
    let isValid: Bool
    let tint: Color
    let terminalTitle: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 6) {
                Image(systemName: "play.fill")
                Text("SPAWN AGENT").bold().kerning(0.5)
                Spacer()
                Image(systemName: "terminal.fill").font(.caption2).opacity(0.8)
            }
            .foregroundStyle(.white)
            .padding(.vertical, 8)
            .padding(.horizontal, 10)
            .frame(maxWidth: .infinity)
            .background(RoundedRectangle(cornerRadius: 7).fill(isValid ? tint : Color.gray))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!isValid)
        .help("Open a new \(terminalTitle) window running claude with this prompt.")
    }
}

// MARK: - Review wizard (shown in the results area)

/// The Review-PRs wizard: target, scope, depth and action toggles, then SPAWN.
/// Rendered in the results pane when the "Review PRs" grid card is selected.
struct ReviewWizardView: View {
    @EnvironmentObject var store: Store
    private let tint = Color.pink

    /// Shared appear/disappear transition for contextual rows that are shown only
    /// where they apply (fade + slide).
    private let rowTransition: AnyTransition = .opacity.combined(with: .move(edge: .top))

    /// The results area is shorter than the wizard, so it scrolls in the app.
    /// `scrolls: false` (headless render only) drops the ScrollView so the
    /// snapshot isn't blank (ImageRenderer can't render ScrollView content).
    private let scrolls: Bool

    /// Default init for the live app. The optional `seed*` params let the headless
    /// renderer snapshot specific states (e.g. single-PR mode) without driving the
    /// UI; they default to nil, leaving each `@State`'s declared value.
    init(scrolls: Bool = true,
         seedTarget: PRTarget? = nil,
         seedSpecificPR: String? = nil,
         seedUsername: String? = nil,
         seedSpecificAuthor: SpecificAuthor? = nil,
         seedSpecificAuthorLogin: String? = nil) {
        self.scrolls = scrolls
        if let v = seedTarget { _target = State(initialValue: v) }
        if let v = seedSpecificPR { _specificPR = State(initialValue: v) }
        if let v = seedUsername { _username = State(initialValue: v) }
        if let v = seedSpecificAuthor { _specificAuthor = State(initialValue: v) }
        if let v = seedSpecificAuthorLogin { _specificAuthorLogin = State(initialValue: v) }
    }

    @State private var depthValue: Double = ReviewWizardView.defaultDepthValue()
    @State private var target: PRTarget = .mine
    @State private var username = ""
    @State private var markReady = true
    @State private var leaveReviews = true
    @State private var replyToReviews = true
    @State private var includeDrafts = true
    @State private var includeReady = true
    @State private var specificPR = ""
    @State private var finalPass = false
    @State private var status: String?
    /// For a specific PR: the polled author disposition (mine / theirs / not-yet). Drives
    /// which action toggles show. `.unknown` while we determine it (offers all, gated).
    @State private var specificAuthor: SpecificAuthor = .unknown
    @State private var authorLoading = false
    /// The polled author login (for a specific PR) — used to check the ban list.
    @State private var specificAuthorLogin: String?

    /// The review-depth levels, loaded once from the shared core.
    private var depths: [ReviewDepth] { ReviewCatalog.depths() }
    private var depthIndex: Int {
        guard !depths.isEmpty else { return 0 }
        return min(max(Int(depthValue), 0), depths.count - 1)
    }
    private var depth: ReviewDepth {
        depths.isEmpty
            ? ReviewDepth(id: "", title: "", blurb: "", fragment: "")
            : depths[depthIndex]
    }

    private static func defaultDepthValue() -> Double {
        let all = ReviewCatalog.depths()
        let idx = all.firstIndex(where: { $0.id == ReviewCatalog.defaultDepthID() }) ?? 0
        return Double(idx)
    }

    private var config: ReviewConfig {
        ReviewConfig(
            depth: depth.id,
            target: target,
            username: username,
            me: store.effectiveMe,
            markReady: markReady,
            leaveReviews: leaveReviews,
            replyToReviews: replyToReviews,
            includeDrafts: includeDrafts,
            includeReady: includeReady,
            specificPR: specificPR,
            finalPass: finalPass,
            specificAuthor: specificAuthor)
    }

    var body: some View {
        Group {
            if scrolls { ScrollView { content } } else { content }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 10) {
            titleRow
            if let banned = bannedTargetLogin { bannedWarning(banned) }
            targetRow
            contextRow
            if target == .specific { authorHint }
            // Draft/ready scope applies to a whose-PRs sweep, not a single PR.
            if target != .specific { scopeRow }
            depthRow
            checkboxes
            // The Final-E2E verdict makes no sense for my own PRs (I don't approve my
            // own work); hidden for the mine disposition.
            if config.canFinalPass { finalPassRow }
            spawnButton
            if let status { statusLine(status) }
        }
        .padding(.trailing, 2)
        // Animate contextual rows reflowing as the target/scope/author change.
        .animation(.easeInOut(duration: 0.22), value: target)
        .animation(.easeInOut(duration: 0.22), value: specificAuthor)
        .animation(.easeInOut(duration: 0.22), value: includeDrafts)
        .animation(.easeInOut(duration: 0.22), value: includeReady)
        .onChange(of: specificPR) { _ in refreshAuthor() }
        .onChange(of: target) { _ in refreshAuthor() }
    }

    /// The author being reviewed IF they're banned for prompt injection — nil otherwise.
    /// For "someone else's PRs" it's the handle; for a specific PR it's the polled author.
    /// (My own PRs are never banned.)
    private var bannedTargetLogin: String? {
        let bans = store.bannedAuthors
        switch target {
        case .mine:
            return nil
        case .someone:
            let u = username.trimmingCharacters(in: .whitespaces)
            return BanList.isBanned(u, in: bans) ? u : nil
        case .specific:
            if let login = specificAuthorLogin, BanList.isBanned(login, in: bans) { return login }
            return nil
        }
    }

    /// A flashing red warning shown while the targeted author is banned for prompt
    /// injection — reviewing their PRs is discouraged. Flashes for as long as the ban
    /// stands (it clears the instant they're un-banned).
    private func bannedWarning(_ login: String) -> some View {
        TimelineView(.periodic(from: Date(), by: 0.5)) { ctx in
            let on = Int(ctx.date.timeIntervalSince1970 * 2) % 2 == 0
            HStack(spacing: 7) {
                Image(systemName: "exclamationmark.octagon.fill")
                    .font(.system(size: 13)).foregroundStyle(.white)
                VStack(alignment: .leading, spacing: 1) {
                    Text("@\(login) is BANNED for prompt injection")
                        .font(.caption.bold()).foregroundStyle(.white)
                    Text("Reviewing their PRs is strongly discouraged while the ban stands.")
                        .font(.system(size: 10)).foregroundStyle(.white.opacity(0.92))
                }
                Spacer(minLength: 0)
            }
            .padding(9)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 8).fill(Color.red.opacity(on ? 0.95 : 0.5)))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.red, lineWidth: on ? 2.5 : 0.5))
        }
    }

    /// A one-line note under the single-PR field: whose PR it is once polled, so the
    /// user knows why some toggles disappeared.
    @ViewBuilder
    private var authorHint: some View {
        let (icon, text, color): (String, String, Color) = {
            if authorLoading { return ("hourglass", "Checking who authored this PR…", .secondary) }
            switch specificAuthor {
            case .mine:    return ("person.fill", "Your PR — fix-on-branch review.", .green)
            case .theirs:  return ("person.2.fill", "Someone else's PR — review only, hands off.", .orange)
            case .unknown: return ("questionmark.circle", "Enter a PR to detect whether it's yours.", .secondary)
            }
        }()
        HStack(spacing: 5) {
            Image(systemName: icon).font(.system(size: 9)).foregroundStyle(color)
            Text(text).font(.system(size: 10)).foregroundStyle(color)
        }
        .transition(rowTransition)
    }

    /// Whether the shared contextual field acts as a github-username box (someone
    /// else's), a single-PR box (specific PR), or is hidden (mine).
    private enum ContextRole { case none, username, pr }
    private var contextRole: ContextRole {
        switch target {
        case .specific: return .pr
        case .someone:  return .username
        case .mine:     return .none
        }
    }

    private var titleRow: some View {
        HStack(spacing: 6) {
            Image(systemName: "checklist").foregroundStyle(tint)
            Text("Review PRs").font(.subheadline.bold())
            Spacer()
        }
    }

    private var targetRow: some View {
        VStack(alignment: .leading, spacing: 5) {
            Picker("", selection: $target) {
                ForEach(PRTarget.allCases) { t in
                    Text(t.title).tag(t)
                }
            }
            .labelsHidden()
            .pickerStyle(.segmented)

            if target == .mine, !store.effectiveMe.isEmpty {
                Text("PRs authored by @\(store.effectiveMe)")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
    }

    /// One field, one slot — the github-username box and the single-PR box share a
    /// place and never show together (see `contextRole`).
    @ViewBuilder
    private var contextRow: some View {
        switch contextRole {
        case .username:
            contextField(icon: "at", placeholder: "github username", text: $username)
                .transition(rowTransition)
        case .pr:
            VStack(alignment: .leading, spacing: 3) {
                contextField(icon: "number", placeholder: "PR # or URL", text: $specificPR)
                    .help("Review just this one PR — paste its number or GitHub URL.")
                if let warning = prWarning {
                    Text(warning)
                        .font(.system(size: 10))
                        .foregroundStyle(.red.opacity(0.85))
                }
            }
            .transition(rowTransition)
        case .none:
            EmptyView()
        }
    }

    private func contextField(icon: String, placeholder: String, text: Binding<String>) -> some View {
        HStack(spacing: 6) {
            Image(systemName: icon).font(.caption2).foregroundStyle(.secondary)
            TextField(placeholder, text: text)
                .textFieldStyle(.plain)
                .font(.callout)
        }
        .padding(6)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.1)))
    }

    /// A hint under the PR field when a pasted URL points at a different repo.
    private var prWarning: String? {
        guard config.prRef.repoMismatch else { return nil }
        let (owner, repo) = config.targetRepo
        return "That PR isn't in \(owner)/\(repo)."
    }

    private var scopeRow: some View {
        VStack(alignment: .leading, spacing: 6) {
            Toggle(isOn: $includeDrafts) { Text("Review draft PRs").font(.caption) }
            Toggle(isOn: $includeReady) { Text("Review ready-for-review PRs").font(.caption) }
        }
        .toggleStyle(.checkbox)
    }

    private var depthRow: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text("Review depth").font(.caption.bold()).foregroundStyle(.secondary)
                Spacer()
                Text(depth.title).font(.caption.bold()).foregroundStyle(.primary)
            }
            Slider(value: $depthValue,
                   in: 0...Double(max(depths.count - 1, 0)),
                   step: 1)
                .tint(tint)
            Text(depth.blurb).font(.system(size: 10)).foregroundStyle(.secondary)
        }
    }

    private var checkboxes: some View {
        VStack(alignment: .leading, spacing: 6) {
            if config.canMarkReady {
                Toggle(isOn: $markReady) {
                    Text("Mark clean PRs ready for review").font(.caption)
                }
                .help("Mark perfectly-clean PRs ready for review.")
                .transition(rowTransition)
            }
            if config.canLeaveReviews {
                Toggle(isOn: $leaveReviews) {
                    Text("Leave reviews (CLAUDE.md format)").font(.caption)
                }
                .help("Post per-line reviews on these PRs.")
                .transition(rowTransition)
            }
            if config.canReplyToReviews {
                Toggle(isOn: $replyToReviews) {
                    Text("Reply to others' review threads").font(.caption)
                }
                .help("Reply \"Fixed in <hash>\" on threads others left.")
                .transition(rowTransition)
            }
        }
        .toggleStyle(.checkbox)
    }

    /// The escalation toggle — off by default, visually highlighted so it reads as
    /// the special "go all the way" option. Appends a final E2E + verdict block.
    private var finalPassRow: some View {
        let highlight = Color.yellow
        return Toggle(isOn: $finalPass) {
            HStack(spacing: 6) {
                Image(systemName: "sparkles").foregroundStyle(.orange)
                Text("Final E2E pass + verdict").font(.caption.bold())
                Spacer(minLength: 0)
            }
        }
        .toggleStyle(.checkbox)
        .padding(7)
        .background(RoundedRectangle(cornerRadius: 7).fill(highlight.opacity(finalPass ? 0.30 : 0.16)))
        .overlay(RoundedRectangle(cornerRadius: 7).stroke(.orange.opacity(finalPass ? 0.9 : 0.5), lineWidth: finalPass ? 1.4 : 1))
        .help("One last full-E2E pass with big swarms: approve clean PRs, request changes on real blockers.")
    }

    private var spawnButton: some View {
        SpawnAgentButton(isValid: config.isValid,
                         tint: tint,
                         terminalTitle: AgentSpawner.resolved(store.terminal).title,
                         action: spawn)
    }

    private func statusLine(_ msg: String) -> some View {
        Text(msg)
            .font(.system(size: 10, design: .monospaced))
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// A short label for the ongoing-processes list, e.g. "Review · #337 · Deep".
    private var trackingLabel: String {
        let d = depth.title
        switch target {
        case .mine: return "Review · my PRs · \(d)"
        case .someone:
            let u = username.trimmingCharacters(in: .whitespaces)
            return "Review · @\(u.isEmpty ? "user" : u) · \(d)"
        case .specific:
            let n = config.prRef.number.map { "#\($0)" } ?? "PR"
            return "Review · \(n) · \(d)"
        }
    }

    /// The one PR this run concerns (single-PR mode only) — the open-in-browser
    /// fallback when its window can't be focused.
    private var trackingPRURL: String? {
        guard target == .specific, let n = config.prRef.number else { return nil }
        let (owner, repo) = config.targetRepo
        return "https://github.com/\(owner)/\(repo)/pull/\(n)"
    }

    /// Poll the specific PR's author (debounced) so the wizard can hide the toggles that
    /// don't apply and pick the right mine/theirs prompt — no author-guessing left to
    /// the spawned agent.
    private func refreshAuthor() {
        guard target == .specific else { specificAuthor = .unknown; specificAuthorLogin = nil; authorLoading = false; return }
        let ref = config.prRef
        guard ref.isValid, let num = ref.number else {
            specificAuthor = .unknown; specificAuthorLogin = nil; authorLoading = false; return
        }
        let (owner, repo) = config.targetRepo
        let me = store.effectiveMe
        let pending = specificPR
        specificAuthor = .unknown        // offer all toggles while we determine
        specificAuthorLogin = nil
        authorLoading = true
        Task {
            try? await Task.sleep(nanoseconds: 400_000_000)   // debounce keystrokes
            if specificPR != pending { return }               // superseded by newer input
            let login = await Self.fetchAuthor(owner: owner, repo: repo, number: num)
            guard specificPR == pending, target == .specific else { return }
            authorLoading = false
            specificAuthorLogin = login
            if let login, !me.isEmpty {
                specificAuthor = login.lowercased() == me.lowercased() ? .mine : .theirs
            } else {
                specificAuthor = .unknown
            }
        }
    }

    /// One `gh pr view … --json author` → the author login, or nil on failure.
    private static func fetchAuthor(owner: String, repo: String, number: Int) async -> String? {
        guard let data = try? await GH.run(
            ["pr", "view", String(number), "--repo", "\(owner)/\(repo)", "--json", "author"])
        else { return nil }
        struct R: Decodable { struct A: Decodable { let login: String }; let author: A }
        return (try? JSONDecoder().decode(R.self, from: data))?.author.login
    }

    private func spawn() {
        let cfg = config
        let preferred = store.terminal
        let term = AgentSpawner.resolved(preferred)
        let label = trackingLabel
        let prURL = trackingPRURL
        status = "Launching \(term.title)…"
        Task.detached {
            do {
                let result = try AgentSpawner.spawn(cfg.buildPrompt(), terminal: preferred)
                await MainActor.run {
                    store.track(kind: "review", label: label, prURL: prURL, result: result)
                    status = "Launched \(term.title) · \(Fmt.clock(Date()))"
                }
            } catch {
                let msg = (error as? LocalizedError)?.errorDescription ?? "\(error)"
                await MainActor.run { status = "Failed: \(msg)" }
            }
        }
    }
}
