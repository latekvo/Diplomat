import Foundation

/// The installed `opencode` executable: where it is, which major it is, and what a 2.x
/// spawn stages for it — the impure half of `OpenCodeAPI`.
///
/// In the core because the macOS applet runs all of it in-process, and the `diplomat-core`
/// CLI reaches one piece of it — the major, for `AgentModel` — on Linux, where spawning,
/// pricing and probing are the Python runtime's own twin of this file. There the runtime
/// passes the major it already found (`majorOverride`), so the CLI resolves nothing
/// itself unless it is run without one.
public enum OpenCodeCLI {
    /// How long `opencode --version` may take: 0.03 s on 2.0.18 and under a second on
    /// 1.x, so this only ever bounds a wedged binary.
    public static let versionTimeout: TimeInterval = 5

    /// How long the user's shell may take to say where the CLI is. It sources their
    /// rc files, which can be slow — a version manager, a prompt framework — so it gets
    /// its own budget rather than sharing the command's.
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

    // Guards the resolution against concurrent callers — the retirements `Store` prices
    // from a detached task, a spawn resolving its major beside them, the probe's sweep.
    // Cached only when a binary was found, so an `opencode` installed after launch is
    // picked up by the next caller — the same rule, and the same shape, as `GH.ghPath` —
    // and trusted for `resolveTTL` and only while the file it names is still there.
    private static let resolveLock = NSLock()
    private static var cached: (resolution: Resolution, at: Date)?

    // The major each binary answered, keyed by what an in-place upgrade changes.
    private static let versionLock = NSLock()
    private static var versions: [VersionKey: Bool] = [:]

    /// The `opencode` executable, found the way the spawn finds it.
    ///
    /// A spawn types its command into a terminal window, and that window's shell is the
    /// user's own — which is what puts a per-user install on `PATH`, and what Settings
    /// promises when it says an rc-only install still runs. An app launched from the
    /// Dock inherits none of that, so asking this process's environment alone would
    /// find nothing for exactly the installs the spawn supports.
    ///
    /// So the shell first (`resolverArguments`), and this `PATH` only when the shell names
    /// nothing. The order matters beyond reach: an rc can put a different install ahead of
    /// the one this process sees (a 1.x under `~/.opencode/bin` beside a 2.x on the system
    /// `PATH`), and `installedIsService` must describe the binary the spawned agent will
    /// run, or a run is spawned the wrong way for the CLI that executes it. What comes
    /// back is a path, run directly rather than through the shell, because the rc that
    /// put it on `PATH` is equally free to print a banner and the CLI's stdout has to
    /// stay parseable.
    ///
    /// Re-asked once `resolveTTL` has passed or the file it named is gone, so an upgrade
    /// that moves the binary reaches the next spawn without a restart. `now` is the
    /// smoke test's clock.
    public static func binary(now: Date = Date()) -> String? {
        current(now: now).path
    }

    /// Where OpenCode's service writes `service.json` for the user's shell: the service is
    /// started by an agent that shell ran, so it is THAT shell's `$XDG_STATE_HOME` which
    /// decides, not this process's. Falls back to this process's own environment when the
    /// shell did not say.
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
    /// The probe prints the path, then the shell's `$XDG_STATE_HOME` on a marked line of
    /// its own — after a newline, so it is a line of its own whatever came before.
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
    /// Anything that stops the question being answered — no binary, a timeout, a non-zero
    /// exit — is 1.x, the shape every spawn had before 2.0.
    public static func installedIsService() -> Bool {
        guard let binary = binary() else { return false }
        return isService(binary: binary)
    }

    /// `installedIsService` for a binary already resolved.
    ///
    /// Remembered per binary as the file stands — its path, inode, size and modification
    /// time, through any symlink — so a steady state costs a `stat` and an upgrade in
    /// place, which changes at least one of them, is asked again. A binary that could not
    /// answer is not remembered.
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
    /// The Linux runtime sets it on every `diplomat-core` it runs, having already resolved
    /// the binary itself, so a prompt build there costs no shell and no `--version`.
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
    /// Two files beside the prompt, named for it — `<prompt>.session.json` and
    /// `<prompt>.prompt.json` — owner-only like the prompt itself, because they carry it.
    /// Files rather than inline arguments because the command is typed into a terminal
    /// through AppleScript on macOS, and a multi-line prompt quoted through that and two
    /// shells is exactly what staging the prompt file already avoids
    /// (`AgentRunner.agentCommand`).
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

    /// `name` on this process's own `PATH` — free, and right whenever the caller was
    /// launched from a shell that already had it.
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
    /// stdout goes to a temp file rather than a pipe, the way `GH.run` does it. A pipe
    /// holds 64K and then blocks the child until someone drains it — and the drain is
    /// an unbounded read, so the deadline below could only be reached once the thing it
    /// exists to bound had already finished.
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
