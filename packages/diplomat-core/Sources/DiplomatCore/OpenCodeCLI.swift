import Foundation

/// The installed `opencode` executable: where it is, which major it is, and what a 2.x
/// spawn stages for it — the impure half of `OpenCodeAPI` that both front-ends share.
///
/// Here rather than in a front-end because the macOS applet and the `diplomat-core` CLI
/// the Linux front-end reaches `AgentModel` through both have to ask which major is
/// installed, and two resolvers would be two answers to the one question every OpenCode
/// seam forks on.
public enum OpenCodeCLI {
    /// How long `opencode --version` may take: 0.03 s on 2.0.18 and under a second on
    /// 1.x, so this only ever bounds a wedged binary.
    public static let versionTimeout: TimeInterval = 5

    /// How long the user's shell may take to say where the CLI is. It sources their
    /// rc, which can be slow — a version manager, a prompt framework — so it gets its
    /// own budget rather than sharing the command's.
    public static let resolveTimeout: TimeInterval = 10

    // Guards the resolved path against concurrent callers — the retirements `Store`
    // prices from a detached task, a spawn resolving its major beside them. Cached on
    // SUCCESS only, so an `opencode` installed after launch is picked up by the next
    // caller rather than needing a restart — the same rule, and the same shape, as
    // `GH.ghPath`.
    private static let binaryLock = NSLock()
    private static var cachedPath: String?

    /// The `opencode` executable, found the way the spawn finds it.
    ///
    /// A spawn types its command into a terminal window, and that window's shell is the
    /// user's own — which is what puts a per-user install on `PATH`, and what Settings
    /// promises when it says an rc-only install still runs. An app launched from the
    /// Dock inherits none of that, so asking this process's environment alone would
    /// find nothing for exactly the installs the spawn supports.
    ///
    /// So this `PATH` first, and only on a miss the shell. What comes back is a path,
    /// run directly rather than through the shell, because the rc that put it on `PATH`
    /// is equally free to print a banner and the CLI's stdout has to stay parseable.
    public static func binary() -> String? {
        binaryLock.lock()
        defer { binaryLock.unlock() }
        if let cached = cachedPath { return cached }
        cachedPath = onPath("opencode") ?? shellPath(to: "opencode")
        return cachedPath
    }

    /// Whether the installed OpenCode is 2.x, per `OpenCodeAPI.isServiceVersion`.
    ///
    /// Asked afresh every time rather than cached: an upgrade between two spawns must move
    /// the second one to the command the new binary takes, and every caller is rare — a
    /// spawn, a retirement, a prompt build. Anything that stops the question being
    /// answered — no binary, a timeout, a non-zero exit — is 1.x, the shape every spawn
    /// had before 2.0.
    public static func installedIsService() -> Bool {
        guard let binary = binary() else { return false }
        return isService(binary: binary)
    }

    /// `installedIsService` for a binary already resolved.
    public static func isService(binary: String) -> Bool {
        guard let out = run(binary, ["--version"], within: versionTimeout) else { return false }
        return OpenCodeAPI.isServiceVersion(String(decoding: out, as: UTF8.self))
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

    /// Where the user's shell says `name` is, if it names a real file.
    ///
    /// Interactive as well as login, because a terminal window's shell is both and on
    /// zsh it is the interactive pass that reads `.zshrc`. The last qualifying line,
    /// because an rc is free to print above the answer; an alias or a shell function
    /// fails the test — `command -v` describes those rather than locating them — and
    /// reads the same as not installed.
    private static func shellPath(to name: String) -> String? {
        let shell = ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh"
        guard let out = run(shell, ["-ilc", "command -v \(name)"], within: resolveTimeout),
              let text = String(data: out, encoding: .utf8) else { return nil }
        for line in text.split(separator: "\n").reversed() {
            let path = line.trimmingCharacters(in: .whitespaces)
            if FileManager.default.isExecutableFile(atPath: path) { return path }
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
        while proc.isRunning, Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
        guard !proc.isRunning else {
            proc.terminate()
            return nil
        }
        return proc.terminationStatus == 0 ? try? Data(contentsOf: url) : nil
    }
}
