import Foundation
import DiplomatCore

/// Where the token half of the telemetry comes from: the agents' own transcripts.
/// The macOS twin of the Linux applet's `usagescan.py`.
///
/// Claude Code's are read off disk and are most of this file; an OpenCode run keeps
/// its own elsewhere and is priced at the end, through that CLI.
///
/// Claude Code appends every turn to `~/.claude/projects/<munged-cwd>/<session>.jsonl`
/// with a `usage` block, and stamps each record with the `cwd` it ran in. That is
/// enough to answer both token questions the Telemetry screen asks, and neither
/// needs anything Anthropic doesn't already write to disk:
///
/// * **how much of this machine's spend went on the monitored repo** — sum every
///   turn, split by whether its `cwd` is inside the repo the agents work in;
/// * **what one auto-task cost** — find the transcript whose opening user message
///   *is* the prompt staged for that agent, and sum that file alone.
///
/// Two rules keep this cheap enough to run on a poll:
///
/// * **cursors, not rescans.** Transcripts are append-only, so a byte offset per
///   file means only new bytes are read. The totals are cumulative counters; the
///   ledger stores them per sample and the screen takes differences.
/// * **the first scan reads nothing.** A machine can hold gigabytes of transcripts,
///   and reading them all would stall the poll that triggered it — for history that
///   predates the ledger and can never be attributed to a task anyway. So an unseen
///   file older than our first scan is seeded at EOF; only what happens from now on
///   counts.
///
/// `DIPLOMAT_CLAUDE_DIR` moves where transcripts are read from, which is how a
/// headless self-test points this at a fixture instead of the user's real logs.
enum UsageScan {

    /// Token fields that count toward a rate-limit window. Cache *reads* are
    /// excluded deliberately — huge and cheap, and counting them would swamp the
    /// signal (the same three the mesh add-on's own probe sums, so a machine
    /// running SzpontNet prices its quota the same way).
    private static let costFields = ["input_tokens", "output_tokens",
                                     "cache_creation_input_tokens"]

    static var claudeDir: URL {
        if let override = ProcessInfo.processInfo.environment["DIPLOMAT_CLAUDE_DIR"],
           !override.isEmpty {
            return URL(fileURLWithPath: override)
        }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".claude")
    }
    static var projectsDir: URL { claudeDir.appendingPathComponent("projects") }

    private static var cursorURL: URL {
        let name = (try? CoreAssets.telemetry())?.cursorFile ?? "usage-cursor.json"
        return AuditLog.dir.appendingPathComponent(name)
    }

    // MARK: - What counts as this repo

    /// The directories whose Claude sessions count as work on this repo: the
    /// checkout the agents `cd` into, plus its worktree siblings at
    /// `<root>-worktrees/*`. A branch worked on in a worktree is the same project by
    /// any honest reading, and every agent dispatched through one would otherwise
    /// land in "everything else" and make the split lie.
    static func repoRoots() -> [String] {
        let root = URL(fileURLWithPath: RepoPaths.agentRepo).standardizedFileURL.path
        guard !root.isEmpty, root != "/" else { return [] }
        let parent = (root as NSString).deletingLastPathComponent
        let name = (root as NSString).lastPathComponent
        return [root, (parent as NSString).appendingPathComponent("\(name)-worktrees")]
    }

    /// Whether a record's `cwd` sits under one of the repo roots. Compared as path
    /// components, not string prefixes: `/x/Diplomat-old` starts with `/x/Diplomat`
    /// and is a different project.
    static func isRepoCwd(_ cwd: String, roots: [String]) -> Bool {
        guard !cwd.isEmpty else { return false }
        for root in roots where cwd == root || cwd.hasPrefix(root + "/") {
            return true
        }
        return false
    }

    // MARK: - Reading one transcript

    private static func tokenCost(_ usage: [String: Any]) -> Double {
        costFields.reduce(0) { $0 + ((usage[$1] as? NSNumber)?.doubleValue ?? 0) }
    }

    /// The usage block of one record, wherever this Claude Code version puts it
    /// (nested under `message` for assistant turns, top level for some synthetic
    /// records).
    private static func usageBlock(_ rec: [String: Any]) -> [String: Any]? {
        if let message = rec["message"] as? [String: Any],
           let usage = message["usage"] as? [String: Any] { return usage }
        return rec["usage"] as? [String: Any]
    }

    /// Sum the tokens in a chunk of transcript, split repo vs other. `consumed` is
    /// the number of bytes forming COMPLETE lines: a poll can land mid-write, so the
    /// trailing partial line is left for the next scan rather than parsed and lost.
    static func scanChunk(_ data: Data, roots: [String]) -> (repo: Double, other: Double,
                                                             consumed: Int) {
        var repo = 0.0, other = 0.0, consumed = 0
        var start = data.startIndex
        while let nl = data[start...].firstIndex(of: UInt8(ascii: "\n")) {
            let line = data[start..<nl]
            consumed += data.distance(from: start, to: nl) + 1
            start = data.index(after: nl)
            guard !line.isEmpty,
                  let rec = (try? JSONSerialization.jsonObject(with: Data(line))) as? [String: Any],
                  let usage = usageBlock(rec) else { continue }
            let cost = tokenCost(usage)
            guard cost > 0 else { continue }
            if isRepoCwd(rec["cwd"] as? String ?? "", roots: roots) { repo += cost }
            else { other += cost }
        }
        return (repo, other, consumed)
    }

    // MARK: - Cumulative totals

    /// Cumulative tokens since the scanner's first run, split by project. Monotonic
    /// within a run of the cursor file; if that file is lost the counters restart at
    /// zero, which every consumer detects as a drop and treats as a segment
    /// boundary rather than a negative delta.
    struct Totals { var repo: Double; var other: Double }

    /// Advance every transcript's cursor and return the cumulative counters. Safe on
    /// a poll: it stats each transcript and reads only appended bytes.
    static func totals() -> Totals {
        let fm = FileManager.default
        var state = (try? Data(contentsOf: cursorURL))
            .flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: Any] ?? [:]
        var files = state["files"] as? [String: [String: Any]] ?? [:]
        let stored = state["totals"] as? [String: Any] ?? [:]
        var repo = (stored["repo"] as? NSNumber)?.doubleValue ?? 0
        var other = (stored["other"] as? NSNumber)?.doubleValue ?? 0
        // A first run has no horizon to compare against, so nothing is "new" and
        // every existing transcript is seeded at EOF.
        let firstRun = state["scannedAt"] == nil
        let scannedAt = (state["scannedAt"] as? NSNumber)?.doubleValue ?? 0
        let roots = repoRoots()

        var seen = Set<String>()
        if let walker = fm.enumerator(at: projectsDir,
                                      includingPropertiesForKeys: [.contentModificationDateKey,
                                                                   .fileSizeKey]) {
            for case let url as URL in walker where url.pathExtension == "jsonl" {
                let path = url.path
                seen.insert(path)
                guard let attrs = try? fm.attributesOfItem(atPath: path),
                      let size = (attrs[.size] as? NSNumber)?.intValue,
                      let mtime = (attrs[.modificationDate] as? Date)?.timeIntervalSince1970
                else { continue }
                var offset = (files[path]?["offset"] as? NSNumber)?.intValue ?? -1
                if offset < 0 {
                    // Unknown file. One that predates our first sighting is history
                    // we can never attribute, so start at its end; one written since
                    // is a session that began under our watch, so read it whole.
                    if firstRun || mtime < scannedAt {
                        files[path] = ["offset": size, "mtime": mtime]
                        continue
                    }
                    offset = 0
                }
                // Truncated or replaced: our offset points past the end, so every
                // byte in it is unread. Start over rather than skip it.
                if size < offset { offset = 0 }
                if size == offset {
                    files[path] = ["offset": offset, "mtime": mtime]
                    continue
                }
                guard let handle = try? FileHandle(forReadingFrom: url) else { continue }
                try? handle.seek(toOffset: UInt64(offset))
                let data = (try? handle.readToEnd()) ?? Data()
                try? handle.close()
                let chunk = scanChunk(data, roots: roots)
                repo += chunk.repo
                other += chunk.other
                files[path] = ["offset": offset + chunk.consumed, "mtime": mtime]
            }
        }

        // Drop cursors for transcripts that are gone, so the state tracks what is on
        // disk rather than growing forever. Deliberately NOT pruned by age: Claude
        // Code appends to an old transcript when a session is resumed, and a
        // forgotten cursor would re-read that file from byte zero and double-count.
        files = files.filter { seen.contains($0.key) }

        state = ["startedAt": state["startedAt"] ?? Date().timeIntervalSince1970,
                 "scannedAt": Date().timeIntervalSince1970,
                 "totals": ["repo": repo, "other": other],
                 "files": files]
        if let data = try? JSONSerialization.data(withJSONObject: state) {
            try? fm.createDirectory(at: AuditLog.dir, withIntermediateDirectories: true)
            try? data.write(to: cursorURL, options: .atomic)
        }
        return Totals(repo: repo, other: other)
    }

    // MARK: - Per-task attribution

    /// How long after an agent's completion its transcript may still be written. The
    /// sentinel fires when `claude` exits and the final turn is already on disk by
    /// then; the slack covers a slow flush.
    private static let mtimeSlack: TimeInterval = 600

    /// Tokens spent by the agent that ran `prompt`, or nil if it can't be found.
    ///
    /// The link is the prompt itself. A Claude Code agent is launched as
    /// `claude "$(cat <staged prompt>)"`, so the transcript's opening user message is
    /// that prompt verbatim — an exact identity, needing no new CLI flag on the spawn
    /// path (where a wrong guess would break the applet's actual job, not just its
    /// bookkeeping) and no guessing at how Claude Code mangles a cwd into a directory
    /// name.
    ///
    /// Returning nil is normal and expected — the applet restarting mid-agent loses
    /// the prompt the match needs. An OpenCode run writes no such transcript at all,
    /// keeping its sessions in a store of its own, and is priced by
    /// `opencodeTaskTokens`; the screen reports whatever neither can attribute as
    /// unattributed rather than pretending it was free.
    static func taskTokens(prompt: String, startedAt: TimeInterval,
                           endedAt: TimeInterval) -> Double? {
        let wanted = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !wanted.isEmpty else { return nil }
        let roots = repoRoots()
        for url in candidates(startedAt: startedAt, endedAt: endedAt) {
            guard openingPrompt(url) == wanted else { continue }
            guard let data = try? Data(contentsOf: url) else { return 0 }
            let chunk = scanChunk(data, roots: roots)
            return chunk.repo + chunk.other
        }
        return nil
    }

    /// How long `opencode export` may take. It reads one session out of a local store,
    /// so this is generous — but it runs on the poll that retires a run, and a wedged
    /// CLI must cost that run its price rather than the poll.
    private static let exportTimeout: TimeInterval = 20

    /// How long the user's shell may take to say where the CLI is. It sources their
    /// rc, which can be slow — a version manager, a prompt framework — so it gets its
    /// own budget rather than the export's, and the two together bound the pricing
    /// path.
    private static let resolveTimeout: TimeInterval = 10

    // Guards the resolved path against the concurrent retirements `Store` prices from
    // a detached task. Cached on SUCCESS only, so an `opencode` installed after launch
    // is picked up by the next run that retires rather than needing a restart — the
    // same rule, and the same shape, as `GH.ghPath`.
    private static let binaryLock = NSLock()
    private static var cachedOpenCodePath: String?

    /// The `opencode` executable, found the way the spawn finds it.
    ///
    /// A spawn types its command into a terminal window, and that window's shell is the
    /// user's own — which is what puts a per-user install on `PATH`, and what Settings
    /// promises when it says an rc-only install still runs. An app launched from the
    /// Dock inherits none of that, so pricing a finished run off this process's
    /// environment alone would price nil for exactly the installs the spawn supports.
    ///
    /// So this `PATH` first, and only on a miss the shell. What comes back is a path,
    /// run directly rather than through the shell, because the rc that put it on `PATH`
    /// is equally free to print a banner and the export's stdout has to stay parseable
    /// JSON.
    private static func opencodeBinary() -> String? {
        binaryLock.lock()
        defer { binaryLock.unlock() }
        if let cached = cachedOpenCodePath { return cached }
        cachedOpenCodePath = onPath("opencode") ?? shellPath(to: "opencode")
        return cachedOpenCodePath
    }

    /// `name` on this process's own `PATH` — free, and right whenever the applet was
    /// launched from a shell that already had it.
    private static func onPath(_ name: String) -> String? {
        let path = ProcessInfo.processInfo.environment["PATH"] ?? ""
        for dir in path.split(separator: ":") where !dir.isEmpty {
            let candidate = "\(dir)/\(name)"
            if FileManager.default.isExecutableFile(atPath: candidate) { return candidate }
        }
        return nil
    }

    /// Where the user's shell says `name` is, if it names a real file.
    ///
    /// Interactive as well as login, because a terminal window's shell is both and on
    /// zsh it is the interactive pass that reads `.zshrc`. The last qualifying line,
    /// because an rc is free to print above the answer; an alias or a shell function
    /// fails the test — `command -v` describes those rather than locating them — and
    /// reads the same as not installed.
    private static func shellPath(to name: String) -> String? {
        let shell = ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh"
        guard let out = capture(shell, ["-ilc", "command -v \(name)"], within: resolveTimeout),
              let text = String(data: out, encoding: .utf8) else { return nil }
        for line in text.split(separator: "\n").reversed() {
            let path = line.trimmingCharacters(in: .whitespaces)
            if FileManager.default.isExecutableFile(atPath: path) { return path }
        }
        return nil
    }

    /// Tokens spent by one OpenCode session, or nil if it cannot be read.
    ///
    /// An OpenCode run leaves nothing in `~/.claude`, so `taskTokens` cannot see it and
    /// every such run used to land in the ledger unpriced. Its own transcript is
    /// reachable through `opencode export`, which is asked for rather than read off
    /// disk: the store behind it is an internal SQLite schema, while the command is
    /// part of the CLI's published surface and already knows where the store lives.
    ///
    /// Read at retirement, not on the poll — a turn's price is per-message, so a run's
    /// is a sum over every message it produced, and the live probe (`OpenCodeAPI`)
    /// deliberately fetches one. By then the run's own server is gone, which is why
    /// this goes through the CLI rather than the port.
    ///
    /// Summed the same way Claude Code's transcripts are: input, output and cache
    /// *writes*, never cache reads. Two runners priced against different yardsticks
    /// would make the per-task figure on the telemetry screen mean two things at once.
    static func opencodeTaskTokens(sessionID: String) -> Double? {
        guard !sessionID.isEmpty,
              let binary = opencodeBinary(),
              let out = capture(binary, ["export", sessionID], within: exportTimeout),
              let root = (try? JSONSerialization.jsonObject(with: out)) as? [String: Any],
              let messages = root["messages"] as? [[String: Any]] else { return nil }
        return OpenCodeAPI.sessionTokens(messages)
    }

    /// Run a command and return its stdout — nil if it could not be started, overran
    /// `timeout`, or exited non-zero.
    ///
    /// stdout goes to a temp file rather than a pipe, the way `GH.run` does it. A pipe
    /// holds 64K and then blocks the child until someone drains it — and the drain is
    /// an unbounded read, so the deadline below could only be reached once the thing it
    /// exists to bound had already finished.
    private static func capture(_ executable: String, _ arguments: [String],
                                within timeout: TimeInterval) -> Data? {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("diplomat-capture-\(UUID().uuidString)")
        guard FileManager.default.createFile(atPath: url.path, contents: nil),
              let sink = try? FileHandle(forWritingTo: url) else { return nil }
        defer {
            try? sink.close()
            try? FileManager.default.removeItem(at: url)
        }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: executable)
        proc.arguments = arguments
        proc.standardOutput = sink
        proc.standardError = FileHandle.nullDevice
        // An interactive shell that inherited a terminal would try to drive it.
        proc.standardInput = FileHandle.nullDevice
        guard (try? proc.run()) != nil else { return nil }
        let deadline = Date().addingTimeInterval(timeout)
        while proc.isRunning, Date() < deadline { usleep(50_000) }
        guard !proc.isRunning else {
            proc.terminate()
            return nil
        }
        return proc.terminationStatus == 0 ? try? Data(contentsOf: url) : nil
    }

    /// Transcripts that could belong to a run spanning `[startedAt, endedAt]`, newest
    /// first. A transcript is appended to while its agent works, so its mtime lands
    /// at or after the agent's last turn — never before it started.
    private static func candidates(startedAt: TimeInterval,
                                   endedAt: TimeInterval) -> [URL] {
        let fm = FileManager.default
        guard let walker = fm.enumerator(at: projectsDir,
                                         includingPropertiesForKeys: [.contentModificationDateKey])
        else { return [] }
        var out: [(TimeInterval, URL)] = []
        for case let url as URL in walker where url.pathExtension == "jsonl" {
            guard let attrs = try? fm.attributesOfItem(atPath: url.path),
                  let mtime = (attrs[.modificationDate] as? Date)?.timeIntervalSince1970
            else { continue }
            if mtime >= startedAt, mtime <= endedAt + mtimeSlack { out.append((mtime, url)) }
        }
        return out.sorted { $0.0 > $1.0 }.map(\.1)
    }

    /// Lines read while looking for a transcript's first user message. The session
    /// header (mode, permission mode, a file-history snapshot, attachments) sits
    /// above it; a couple of dozen lines is generous and bounds a non-match's cost.
    private static let headerLines = 40

    /// The text of a transcript's first user message, or nil.
    private static func openingPrompt(_ url: URL) -> String? {
        guard let handle = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? handle.close() }
        // The opening prompt is one of the first records, so a bounded head read
        // beats loading a transcript that can run to megabytes.
        guard let head = try? handle.read(upToCount: 512 * 1024),
              let text = String(data: head, encoding: .utf8) else { return nil }
        for line in text.split(separator: "\n", omittingEmptySubsequences: true)
            .prefix(headerLines) {
            guard let rec = (try? JSONSerialization.jsonObject(with: Data(line.utf8)))
                    as? [String: Any], rec["type"] as? String == "user" else { continue }
            guard let message = rec["message"] as? [String: Any] else { return nil }
            return messageText(message["content"])
        }
        return nil
    }

    /// A user message's text, whether Claude Code wrote it as a bare string or as a
    /// list of content blocks.
    private static func messageText(_ content: Any?) -> String? {
        if let s = content as? String {
            return s.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        if let blocks = content as? [[String: Any]] {
            let parts = blocks.filter { $0["type"] as? String == "text" }
                .compactMap { $0["text"] as? String }
            guard !parts.isEmpty else { return nil }
            return parts.joined().trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return nil
    }
}
