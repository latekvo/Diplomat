import SwiftUI
import AppKit

// MARK: - Review depth (the complexity slider)

/// The review-complexity continuum, from a fast static read all the way to a
/// maximal E2E run with a double-pass hard-repro verification. Each level
/// subsumes the ones below it; the slider picks one and its `promptFragment`
/// is dropped straight into the agent prompt.
enum ReviewDepth: Int, CaseIterable, Identifiable {
    case quick, standard, deep, max
    var id: Int { rawValue }

    var title: String {
        switch self {
        case .quick:    return "Quick static pass"
        case .standard: return "Standard swarm"
        case .deep:     return "Deep · hard repro"
        case .max:      return "Full E2E ×2"
        }
    }
    /// One-liner shown under the slider so the level reads at a glance.
    var blurb: String {
        switch self {
        case .quick:    return "Read each diff, flag obvious issues. Nothing run."
        case .standard: return "Swarm per PR across the review lenses; verify findings."
        case .deep:     return "Swarm + concrete repro before & after every on-branch fix."
        case .max:      return "Run each PR E2E; second independent pass; repro-verified."
        }
    }
    /// The chunk of agent instructions describing how hard to review.
    var promptFragment: String {
        switch self {
        case .quick:
            return "Do a QUICK STATIC review of each PR: read the full diff and the surrounding code it touches, and flag obvious bugs, regressions, scope creep and bad practices. You don't need to run or reproduce anything — this is a fast read-through pass."
        case .standard:
            return "For each PR, dispatch a swarm of review agents (3–8) covering the standard lenses — correctness, scope/simplification, edge cases (nulls, boundaries, errors, concurrency) and ripple effects on callers/docs/tests. Treat every finding as a lead, not a verdict: verify it against the actual code before acting, then fix the real ones directly on the PR's branch."
        case .deep:
            return "For each PR, dispatch swarms of agents across the review lenses. For EVERY suspected issue, build a concrete, hard reproduction that proves it real BEFORE touching anything; then fix it directly on the PR's branch and re-run the same reproduction to confirm the fix lands. Keep dispatching fresh swarms at each PR until a full pass turns up nothing left to fix."
        case .max:
            return "Maximum rigor. For each PR: run it END-TO-END through its real entry point and confirm observable behaviour — not just that it compiles. Dispatch swarms across every review lens; every finding gets a concrete hard reproduction before the fix and the SAME repro re-run after to prove the fix lands. Then do a SECOND, independent verification pass over the PR to catch anything the first missed. Fix directly on the branches, and keep dispatching swarms until TWO consecutive passes come back completely clean."
        }
    }
}

// MARK: - Review config + prompt builder

/// Everything the wizard collects, plus the logic that turns it into the prompt
/// handed to a fresh `claude` session. Pure value type so it's trivially testable
/// from the headless self-test.
struct ReviewConfig {
    var depth: ReviewDepth = .deep
    /// True ⇒ review my own PRs; false ⇒ review `username`'s PRs.
    var targetIsMine: Bool = true
    var username: String = ""
    /// The authenticated viewer login (from the Store), used as the @handle for "mine".
    var me: String = ""

    var markReady: Bool = true       // mark perfectly-clean PRs ready for review
    var leaveReviews: Bool = true    // effective only when reviewing OTHER people's PRs
    var replyToReviews: Bool = true  // effective only when reviewing MY PRs

    // Which PR states are in scope. With neither on, we fall back to reviewing a
    // single PR by number (`specificPR`).
    var includeDrafts: Bool = true
    var includeReady: Bool = true
    var specificPR: String = ""

    /// The "final pass" escalation: a culminating full-E2E verdict pass. Off by default.
    var finalPass: Bool = false

    /// The @handle whose PRs we go through.
    var authorHandle: String {
        if targetIsMine { return me.isEmpty ? "me" : me }
        let u = username.trimmingCharacters(in: .whitespaces)
        return u.isEmpty ? "" : u
    }

    // Marking ready and replying to your own threads only make sense on your own
    // PRs; leaving a formal review only makes sense on someone else's. Each toggle
    // is greyed out where it doesn't apply.
    var canMarkReady: Bool { targetIsMine }
    var canLeaveReviews: Bool { !targetIsMine }
    var canReplyToReviews: Bool { targetIsMine }
    var effMarkReady: Bool { markReady && canMarkReady }
    var effLeaveReviews: Bool { leaveReviews && canLeaveReviews }
    var effReplyToReviews: Bool { replyToReviews && canReplyToReviews }

    /// With neither PR-state box ticked, we review one PR by number instead.
    var isSinglePR: Bool { !includeDrafts && !includeReady }
    var trimmedPR: String { specificPR.trimmingCharacters(in: .whitespaces) }

    /// SPAWN is only meaningful once we know what to review: either a valid PR
    /// number (single-PR mode) or whose PRs + at least one PR state.
    var isValid: Bool {
        if isSinglePR { return Int(trimmedPR) != nil }
        return !authorHandle.isEmpty
    }

    /// Human description of which PR states are in scope (multi-PR mode).
    private var prKind: String {
        switch (includeDrafts, includeReady) {
        case (true, true):  return "currently-open PR (draft or ready-for-review)"
        case (true, false): return "currently-open DRAFT PR"
        default:            return "currently-open ready-for-review (non-draft) PR"
        }
    }

    func buildPrompt() -> String {
        var blocks: [String] = []

        if isSinglePR {
            blocks.append("Review PR #\(trimmedPR) in \(GH.owner)/\(GH.repo). Use the `gh` CLI to fetch it.")
        } else {
            let scope = targetIsMine
                ? "each \(prKind) of mine (authored by @\(authorHandle))"
                : "each \(prKind) authored by @\(authorHandle)"
            blocks.append("Go through \(scope) in \(GH.owner)/\(GH.repo). Use the `gh` CLI to enumerate them.")
        }
        blocks.append(depth.promptFragment)
        blocks.append("Hold to the bar in my CLAUDE.md throughout: prove every issue beyond reasonable doubt with a concrete reproduction before you act on it, scale the swarm to the work, and never report something fixed without re-running the repro to confirm it landed.")

        if effMarkReady {
            blocks.append("If a PR turns out perfectly clean — no issues, regressions or bad practices left — mark it ready for review and report its number to me. List every PR you cleared at the end.")
        }
        if effLeaveReviews {
            blocks.append("Leave a formal GitHub review on each PR (POST a pull-request review, not a top-level comment), following the review-comment rules in my CLAUDE.md: one inline per-line comment per finding anchored to the exact line(s), describe the problem and its concrete impact only (never propose the fix), strip every internal severity/category marking from the text, and never leave an LGTM / \"no issues\" comment.")
        }
        if effReplyToReviews {
            blocks.append("For review threads that OTHERS have left on these PRs: address each one, and never mark a thread resolved without first replying \"Fixed in <commit_hash>\" with the real commit hash.")
        }

        blocks.append("Keep dispatching swarms until every PR you go through comes back clean. No AI attribution anywhere in git/GitHub — commits authored as me, no Co-Authored-By, no \"Generated with\" taglines.")

        if finalPass {
            blocks.append("Then, one last FULL E2E pass on the real built binaries with massive swarms of code-analysis agents. Provide super-concrete reproductions for any finding you manage to surface. Deliver a verdict on each PR:\n• If it turns out perfect — confirm every previously-raised issue is resolved, and if so, APPROVE it.\n• If there are only a few nitpicks — point them out and ask for them to be resolved, but still APPROVE.\n• If there are major blockers — leave the review as \"changes requested\".")
        }

        return blocks.joined(separator: "\n\n")
    }
}

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

    /// Stage the prompt, open the terminal, run claude. Returns the prompt file URL.
    @discardableResult
    static func spawn(_ prompt: String, terminal preferred: SpawnTerminal) throws -> URL {
        let term = resolved(preferred)
        let file = try writePrompt(prompt)
        try runOsascript(appleScript(for: term, shellCommand: shellCommand(promptFile: file)))
        return file
    }

    static func writePrompt(_ prompt: String) throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("argent-utils-review-\(UUID().uuidString).txt")
        do { try prompt.write(to: url, atomically: true, encoding: .utf8) }
        catch { throw SpawnError.write(error.localizedDescription) }
        return url
    }

    /// `cd '<repo>' 2>/dev/null; claude "$(cat '<promptfile>')"`
    static func shellCommand(promptFile: URL) -> String {
        "cd \(shq(repoPath)) 2>/dev/null; claude \"$(cat \(shq(promptFile.path)))\""
    }

    /// Wrap the shell command in an "open a new window and run this" script for
    /// the given terminal.
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
                tell current session of w
                    write text "\(esc)"
                end tell
            end tell
            """
        case .terminal:
            return """
            tell application "Terminal"
                activate
                do script "\(esc)"
            end tell
            """
        }
    }

    /// POSIX single-quote a path for safe embedding in the shell command.
    private static func shq(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    private static func runOsascript(_ script: String) throws {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        proc.arguments = ["-e", script]
        let errPipe = Pipe()
        proc.standardError = errPipe
        do { try proc.run() }
        catch { throw SpawnError.osascript(code: -1, stderr: "\(error)") }
        proc.waitUntilExit()
        if proc.terminationStatus != 0 {
            let err = String(data: errPipe.fileHandleForReading.readDataToEndOfFile(),
                             encoding: .utf8) ?? ""
            throw SpawnError.osascript(code: proc.terminationStatus, stderr: err)
        }
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
    init(scrolls: Bool = true) { self.scrolls = scrolls }

    @State private var depthValue: Double = Double(ReviewDepth.deep.rawValue)
    @State private var targetIsMine = true
    @State private var username = ""
    @State private var markReady = true
    @State private var leaveReviews = true
    @State private var replyToReviews = true
    @State private var includeDrafts = true
    @State private var includeReady = true
    @State private var specificPR = ""
    @State private var finalPass = false
    @State private var status: String?

    private var config: ReviewConfig {
        ReviewConfig(
            depth: ReviewDepth(rawValue: Int(depthValue)) ?? .deep,
            targetIsMine: targetIsMine,
            username: username,
            me: store.effectiveMe,
            markReady: markReady,
            leaveReviews: leaveReviews,
            replyToReviews: replyToReviews,
            includeDrafts: includeDrafts,
            includeReady: includeReady,
            specificPR: specificPR,
            finalPass: finalPass)
    }
    private var depth: ReviewDepth { config.depth }

    var body: some View {
        Group {
            if scrolls { ScrollView { content } } else { content }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 10) {
            titleRow
            targetRow
            scopeRow
            depthRow
            checkboxes
            finalPassRow
            spawnButton
            if let status { statusLine(status) }
        }
        .padding(.trailing, 2)
        // Animate contextual rows reflowing as the target/scope change.
        .animation(.easeInOut(duration: 0.22), value: targetIsMine)
        .animation(.easeInOut(duration: 0.22), value: includeDrafts)
        .animation(.easeInOut(duration: 0.22), value: includeReady)
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
            Picker("", selection: $targetIsMine) {
                Text(store.effectiveMe.isEmpty ? "My PRs" : "My PRs (@\(store.effectiveMe))").tag(true)
                Text("Someone else's").tag(false)
            }
            .labelsHidden()
            .pickerStyle(.segmented)

            if !targetIsMine {
                HStack(spacing: 6) {
                    Image(systemName: "at").font(.caption2).foregroundStyle(.secondary)
                    TextField("github username", text: $username)
                        .textFieldStyle(.plain)
                        .font(.callout)
                }
                .padding(6)
                .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.1)))
                .transition(rowTransition)
            }
        }
    }

    private var scopeRow: some View {
        VStack(alignment: .leading, spacing: 6) {
            Group {
                Toggle(isOn: $includeDrafts) { Text("Review draft PRs").font(.caption) }
                Toggle(isOn: $includeReady) { Text("Review ready-for-review PRs").font(.caption) }
            }
            .toggleStyle(.checkbox)

            // With neither box ticked, review exactly one PR by number.
            if config.isSinglePR {
                HStack(spacing: 6) {
                    Image(systemName: "number").font(.caption2).foregroundStyle(.secondary)
                    TextField("PR # to review", text: $specificPR)
                        .textFieldStyle(.plain)
                        .font(.callout)
                }
                .padding(6)
                .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.1)))
                .help("Review just this one PR.")
                .transition(rowTransition)
            }
        }
    }

    private var depthRow: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text("Review depth").font(.caption.bold()).foregroundStyle(.secondary)
                Spacer()
                Text(depth.title).font(.caption.bold()).foregroundStyle(.primary)
            }
            Slider(value: $depthValue,
                   in: 0...Double(ReviewDepth.allCases.count - 1),
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
        Button { spawn() } label: {
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
            .background(RoundedRectangle(cornerRadius: 7).fill(config.isValid ? tint : Color.gray))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!config.isValid)
        .help("Open a new \(AgentSpawner.resolved(store.terminal).title) window running claude with this review prompt.")
    }

    private func statusLine(_ msg: String) -> some View {
        Text(msg)
            .font(.system(size: 10, design: .monospaced))
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func spawn() {
        let cfg = config
        let preferred = store.terminal
        let term = AgentSpawner.resolved(preferred)
        status = "Launching \(term.title)…"
        Task.detached {
            do {
                _ = try AgentSpawner.spawn(cfg.buildPrompt(), terminal: preferred)
                await MainActor.run { status = "Launched \(term.title) · \(Fmt.clock(Date()))" }
            } catch {
                let msg = (error as? LocalizedError)?.errorDescription ?? "\(error)"
                await MainActor.run { status = "Failed: \(msg)" }
            }
        }
    }
}
