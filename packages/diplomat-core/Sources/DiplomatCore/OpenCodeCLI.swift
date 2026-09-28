import Foundation

/// The installed `opencode` executable: where it is, which major it is, and what a 2.x
/// spawn stages for it — the impure half of `OpenCodeAPI`.
///
/// On Linux only the major is used from here (`AgentModel`, via the `diplomat-core` CLI);
/// the Python runtime twins the rest and passes the major it found (`majorOverride`).
public enum OpenCodeCLI {
    /// How long `opencode --version` may take: 0.03 s on 2.0.18 and under a second on
    /// 1.x, so this only ever bounds a wedged binary.
    public static let versionTimeout: TimeInterval = 5

    /// How long the user's shell may take to say where the CLI is; its rc files (a version
    /// manager, a prompt framework) can be slow.
    public static let resolveTimeout: TimeInterval = 10

    /// How long a resolution is trusted. An upgrade from 1.x to 2.x ordinarily MOVES the
    /// binary (`~/.opencode/bin` or Homebrew, to npm's `@opencode/cli`), and a path kept
    /// past that spawns every run the 1.x way against a 2.x CLI that rejects it.
    public static let resolveTTL: TimeInterval = 60

    /// What the resolver's shell prints ahead of its own `$XDG_STATE_HOME`.
    public static let stateMarker = "@@XDG_STATE_HOME="

    /// What one ask of the user's shell found: the executable, and the state root
    /// OpenCode's service writes its discovery file under for that shell.
    public struct Resolution {
        public let path: String?
        /// The shell's `$XDG_STATE_HOME`, `""` for unset; nil when the shell never got as
        /// far as saying (no marker line), which is not the same as unset.
        public let stateHome: String?
    }

    // Callers race: `Store`'s detached pricing, a spawn resolving its major, the probe's
    // sweep. Only a found binary is cached (as `GH.ghPath`), so a later install is seen.
    private static let resolveLock = NSLock()
    private static var cached: (resolution: Resolution, at: Date)?

    // The major each binary answered, keyed by what an in-place upgrade changes.
    private static let versionLock = NSLock()
    private static var versions: [VersionKey: Bool] = [:]

    /// The `opencode` executable, found the way the spawn finds it.
    ///
    /// A spawn runs in the user's own shell, which is what puts a per-user install on
    /// `PATH` (and what Settings promises for an rc-only install); an app launched from the
    /// Dock inherits none of that. So the shell first (`resolverArguments`), this `PATH`
    /// only when it names nothing. The order matters beyond reach: an rc can put another
    /// install first (a 1.x in `~/.opencode/bin` beside a 2.x on the system `PATH`), and
    /// `installedIsService` must describe the binary the agent will run. The path is run
    /// directly, not through the shell, since an rc may print a banner onto stdout.
    ///
    /// Re-asked once `resolveTTL` has passed or the file it named is gone. `now` is the
    /// smoke test's clock.
    public static func binary(now: Date = Date()) -> String? {
        current(now: now).path
    }

    /// Where OpenCode's service writes `service.json`: an agent the user's shell ran starts
    /// the service, so that shell's `$XDG_STATE_HOME` decides, and this process's only when
    /// the shell did not say.
    public static func serviceFile(now: Date = Date()) -> URL {
        var environment = ProcessInfo.processInfo.environment
        if let state = current(now: now).stateHome { environment["XDG_STATE_HOME"] = state }
        return OpenCodeAPI.serviceFile(environment: environment,
                                       home: FileManager.default.homeDirectoryForCurrentUser)
    }

    private static func current(now: Date) -> Resolution {
        resolveLock.lock()
        defer { resolveLock.unlock() }
        if let c = cached, now.timeIntervalSince(c.at) < resolveTTL,
           let path = c.resolution.path, FileManager.default.isExecutableFile(atPath: path) {
            return c.resolution
        }
        let asked = resolve(shell: ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh")
        let found = Resolution(path: asked.path ?? onPath("opencode"), stateHome: asked.stateHome)
        cached = found.path == nil ? nil : (found, now)
        return found
    }

    /// Ask `shell` where `opencode` is and what its `$XDG_STATE_HOME` is, the way the
    /// agent's own shell would answer. `environment` is the smoke test's; nil inherits.
    public static func resolve(shell: String,
                               environment: [String: String]? = nil) -> Resolution {
        guard let out = run(shell, resolverArguments(shell: shell), within: resolveTimeout,
                            environment: environment) else {
            return Resolution(path: nil, stateHome: nil)
        }
        return parseResolution(String(decoding: out, as: UTF8.self)) {
            FileManager.default.isExecutableFile(atPath: $0)
        }
    }

    /// `<shell> -l -c` running `<shell> -i -c <probe>` — a login shell, then an interactive
    /// one inside it, which is the pair the agent itself runs under: the terminal window's
    /// login shell, then the interactive inner shell `AgentSpawner.shellCommand` starts.
    /// Both halves matter. Homebrew on Apple silicon puts its `PATH` in `~/.zprofile`
    /// alone, which only a login zsh reads, and nvm lives in `~/.bashrc`, which a login
    /// bash skips and only an interactive one reads.
    ///
    /// The probe prints the path, then `$XDG_STATE_HOME` on a marked line after a newline,
    /// so it stands alone whatever the rc printed.
    public static func resolverArguments(shell: String) -> [String] {
        let probe = "command -v opencode; printf '\\n\(stateMarker)%s\\n' \"$XDG_STATE_HOME\""
        return ["-l", "-c", "\(AgentRunner.shq(shell)) -i -c \(AgentRunner.shq(probe))"]
    }

    /// The resolver's answer out of what the shell printed. The path is the last line
    /// before the marker that names an executable file — an rc is free to print above it,
    /// and an alias or a shell function fails the test, since `command -v` describes those
    /// rather than locating them — and the state root is the marker line's value.
    public static func parseResolution(_ text: String,
                                       isExecutable: (String) -> Bool) -> Resolution {
        let lines = text.split(whereSeparator: \.isNewline).map(String.init)
        let marker = lines.lastIndex { $0.hasPrefix(stateMarker) }
        let above = marker.map { Array(lines[..<$0]) } ?? lines
        let path = above.reversed()
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .first(where: isExecutable)
        return Resolution(path: path,
                          stateHome: marker.map { String(lines[$0].dropFirst(stateMarker.count)) })
    }

    /// Whether the installed OpenCode is 2.x, per `OpenCodeAPI.isServiceVersion`.
    ///
    /// Anything that stops it answering (no binary, a timeout, a non-zero exit) reads as 1.x.
    public static func installedIsService() -> Bool {
        guard let binary = binary() else { return false }
        return isService(binary: binary)
    }

    /// `installedIsService` for a binary already resolved.
    ///
    /// Remembered per path, inode, size and mtime (through any symlink), so a steady state
    /// costs a `stat` and an in-place upgrade is asked again. A failed answer is not kept.
    public static func isService(binary: String) -> Bool {
        let key = VersionKey(binary)
        if let key {
            versionLock.lock()
            let known = versions[key]
            versionLock.unlock()
            if let known { return known }
        }
        guard let out = run(binary, ["--version"], within: versionTimeout) else { return false }
        let service = OpenCodeAPI.isServiceVersion(String(decoding: out, as: UTF8.self))
        if let key {
            versionLock.lock()
            versions[key] = service
            versionLock.unlock()
        }
        return service
    }

    /// What `isService` is remembered under.
    private struct VersionKey: Hashable {
        let path: String, inode: UInt64, size: UInt64, modified: Double

        init?(_ path: String) {
            guard let attributes = try? FileManager.default.attributesOfItem(
                      atPath: OpenCodeCLI.physicalPath(path)),
                  let inode = (attributes[.systemFileNumber] as? NSNumber)?.uint64Value,
                  let size = (attributes[.size] as? NSNumber)?.uint64Value,
                  let modified = (attributes[.modificationDate] as? Date)?.timeIntervalSince1970
            else { return nil }
            self.path = path
            self.inode = inode
            self.size = size
            self.modified = modified
        }
    }

    /// The major `DIPLOMAT_OPENCODE_MAJOR` names — `true` for exactly `2`, `false` for
    /// exactly `1`, nil for anything else, which leaves the question to `installedIsService`.
    /// The Linux runtime sets it, having resolved the binary itself, so a prompt build
    /// costs no shell and no `--version`.
    public static func majorOverride(_ environment: [String: String]) -> Bool? {
        switch environment["DIPLOMAT_OPENCODE_MAJOR"] {
        case "2": return true
        case "1": return false
        default: return nil
        }
    }

    /// Stage what a 2.x spawn hands `opencode api`, and return the session id it minted —
    /// nil when either file could not be written, which is a run that cannot start.
    ///
    /// `<prompt>.session.json` and `<prompt>.prompt.json`, owner-only since they carry the
    /// prompt. Files, not arguments, for the reason the prompt itself is staged: the command
    /// is typed through AppleScript and two shells (`AgentRunner.agentCommand`).
    ///
    /// `directory` is where the agent is `cd`'d into; the session records its physical
    /// path, the one the agent's own tools will report.
    public static func stageSession(promptFile: URL, directory: String,
                                    model: String) -> String? {
        guard let prompt = try? String(contentsOf: promptFile, encoding: .utf8) else {
            return nil
        }
        let sessionID = OpenCodeAPI.newSessionID()
        let bodies: [(String, [String: Any])] = [
            (".session.json", OpenCodeAPI.sessionBody(id: sessionID,
                                                      directory: physicalPath(directory),
                                                      model: model)),
            (".prompt.json", OpenCodeAPI.promptBody(prompt)),
        ]
        for (suffix, body) in bodies {
            guard let data = try? JSONSerialization.data(
                      withJSONObject: body, options: [.sortedKeys, .withoutEscapingSlashes]),
                  FileManager.default.createFile(atPath: promptFile.path + suffix,
                                                 contents: data,
                                                 attributes: [.posixPermissions: 0o600])
            else { return nil }
        }
        return sessionID
    }

    /// `path` with every symlink resolved, or `path` itself when it cannot be.
    ///
    /// `realpath(3)` rather than `URL.resolvingSymlinksInPath`, which on macOS answers
    /// `/tmp` for what the kernel, and so the agent's own `pwd`, calls `/private/tmp`.
    static func physicalPath(_ path: String) -> String {
        guard let resolved = realpath(path, nil) else { return path }
        defer { free(resolved) }
        return String(cString: resolved)
    }

    /// `name` on this process's own `PATH`.
    private static func onPath(_ name: String) -> String? {
        let path = ProcessInfo.processInfo.environment["PATH"] ?? ""
        for dir in path.split(separator: ":") where !dir.isEmpty {
            let candidate = "\(dir)/\(name)"
            if FileManager.default.isExecutableFile(atPath: candidate) { return candidate }
        }
        return nil
    }

    /// Run a command and return its stdout — nil if it could not be started, overran
    /// `timeout`, or exited non-zero.
    ///
    /// stdout goes to a temp file, as in `GH.run`: a full 64K pipe blocks the child, and
    /// draining it is an unbounded read the deadline could not interrupt.
    public static func run(_ executable: String, _ arguments: [String],
                           within timeout: TimeInterval,
                           environment: [String: String]? = nil) -> Data? {
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
        if let environment { proc.environment = environment }
        proc.standardOutput = sink
        proc.standardError = FileHandle.nullDevice
        // An interactive shell that inherited a terminal would try to drive it.
        proc.standardInput = FileHandle.nullDevice
        guard (try? proc.run()) != nil else { return nil }
        let deadline = Date().addingTimeInterval(timeout)
        while proc.isRunning, Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
        guard !proc.isRunning else {
            proc.terminate()
            return nil
        }
        return proc.terminationStatus == 0 ? try? Data(contentsOf: url) : nil
    }
}
