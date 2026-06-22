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

        return blocks.joined(separator: "\n\n")
    }
}

// MARK: - Spawning a detached claude session in iTerm2

/// Opens a brand-new iTerm2 window running `claude "<prompt>"`, fully detached
/// from this applet. The prompt is written to a temp file and read back with
/// `$(cat …)` so we never have to wrestle a multi-line prompt through nested
/// shell + AppleScript quoting.
enum AgentSpawner {
    /// The local checkout the agent works in. Personal-machine path; the `cd` is
    /// best-effort (`;`, not `&&`) so `claude` still starts if it ever moves.
    static let repoPath = "/Users/ignacylatka/dev/argent"

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

    /// Stage the prompt, open iTerm, run claude. Returns the prompt file URL.
    @discardableResult
    static func spawn(_ prompt: String) throws -> URL {
        let file = try writePrompt(prompt)
        try runOsascript(appleScript(shellCommand: shellCommand(promptFile: file)))
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

    /// Wrap the shell command in an iTerm "open a new window and run this" script.
    static func appleScript(shellCommand cmd: String) -> String {
        let esc = cmd
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        return """
        tell application "iTerm"
            activate
            set w to (create window with default profile)
            tell current session of w
                write text "\(esc)"
            end tell
        end tell
        """
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

// MARK: - Actions panel (the wizard UI)

/// The fourth section: a "Review PRs" button that expands inline into a small
/// wizard, then spawns a detached `claude` review session in iTerm2.
struct ActionsPanel: View {
    @EnvironmentObject var store: Store
    private let tint = Color.pink

    @State private var expanded = false

    init(startExpanded: Bool = false) {
        _expanded = State(initialValue: startExpanded)
    }
    @State private var depthValue: Double = Double(ReviewDepth.deep.rawValue)
    @State private var targetIsMine = true
    @State private var username = ""
    @State private var markReady = true
    @State private var leaveReviews = true
    @State private var replyToReviews = true
    @State private var includeDrafts = true
    @State private var includeReady = true
    @State private var specificPR = ""
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
            specificPR: specificPR)
    }
    private var depth: ReviewDepth { config.depth }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if expanded { wizard } else { collapsedButton }
        }
        .padding(8)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.gray.opacity(0.06)))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.gray.opacity(0.15), lineWidth: 1))
    }

    // Collapsed: just the entry button.
    private var collapsedButton: some View {
        Button { withAnimation(.easeInOut(duration: 0.15)) { expanded = true } } label: {
            HStack(spacing: 8) {
                Image(systemName: "checklist").foregroundStyle(tint)
                Text("Review PRs").bold().foregroundStyle(.primary)
                Spacer()
                Image(systemName: "chevron.down").font(.caption2).foregroundStyle(.secondary)
            }
            .padding(.vertical, 4)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    // Expanded: the wizard.
    private var wizard: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            targetRow
            scopeRow
            depthRow
            checkboxes
            spawnButton
            if let status { statusLine(status) }
        }
    }

    private var header: some View {
        Button { withAnimation(.easeInOut(duration: 0.15)) { expanded = false } } label: {
            HStack(spacing: 8) {
                Image(systemName: "checklist").foregroundStyle(tint)
                Text("Review PRs").bold().foregroundStyle(.primary)
                Spacer()
                Image(systemName: "chevron.up").font(.caption2).foregroundStyle(.secondary)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
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
            HStack(spacing: 6) {
                Image(systemName: "number").font(.caption2).foregroundStyle(.secondary)
                TextField("PR # to review (when neither above)", text: $specificPR)
                    .textFieldStyle(.plain)
                    .font(.callout)
            }
            .padding(6)
            .background(RoundedRectangle(cornerRadius: 6).fill(Color.gray.opacity(0.1)))
            .disabled(!config.isSinglePR)
            .opacity(config.isSinglePR ? 1 : 0.4)
            .help(config.isSinglePR ? "Review just this one PR." : "Untick both boxes above to review a single PR by number.")
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
            Toggle(isOn: $markReady) {
                Text("Mark clean PRs ready for review").font(.caption)
            }
            .disabled(!config.canMarkReady)
            .help(config.canMarkReady ? "Mark perfectly-clean PRs ready for review." : "You only mark your own PRs ready.")

            Toggle(isOn: $leaveReviews) {
                Text("Leave reviews (CLAUDE.md format)").font(.caption)
            }
            .disabled(!config.canLeaveReviews)
            .help(config.canLeaveReviews ? "Post per-line reviews on these PRs." : "You don't review your own PRs.")

            Toggle(isOn: $replyToReviews) {
                Text("Reply to others' review threads").font(.caption)
            }
            .disabled(!config.canReplyToReviews)
            .help(config.canReplyToReviews ? "Reply \"Fixed in <hash>\" on threads others left." : "Only applies to your own PRs.")
        }
        .toggleStyle(.checkbox)
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
        .help("Open a new iTerm2 window running claude with this review prompt.")
    }

    private func statusLine(_ msg: String) -> some View {
        Text(msg)
            .font(.system(size: 10, design: .monospaced))
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func spawn() {
        let cfg = config
        status = "Launching iTerm…"
        Task.detached {
            do {
                _ = try AgentSpawner.spawn(cfg.buildPrompt())
                await MainActor.run { status = "Launched · \(Fmt.clock(Date()))" }
            } catch {
                let msg = (error as? LocalizedError)?.errorDescription ?? "\(error)"
                await MainActor.run { status = "Failed: \(msg)" }
            }
        }
    }
}
