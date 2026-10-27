import Foundation

/// Asking the user's own shell where a CLI is, the way an agent's terminal would find it.
///
/// An app launched from the Dock (or a systemd user unit) inherits a bare `PATH`; the
/// per-user installs every agent CLI ships as (Homebrew, npm, nvm, `~/.local/bin`) are
/// put on it by the shell's rc files. So the question goes to the shell.
public enum LoginShell {
    /// How long the shell may take to answer; its rc files (a version manager, a prompt
    /// framework) can be slow.
    public static let timeout: TimeInterval = 10

    /// `<shell> -l -c` running `<shell> -i -c <probe>` — a login shell, then an interactive
    /// one inside it, which is the pair the agent itself runs under: the terminal window's
    /// login shell, then the interactive inner shell `AgentSpawner.shellCommand` starts.
    /// Both halves matter. Homebrew on Apple silicon puts its `PATH` in `~/.zprofile`
    /// alone, which only a login zsh reads, and nvm lives in `~/.bashrc`, which a login
    /// bash skips and only an interactive one reads.
    public static func arguments(shell: String, probe: String) -> [String] {
        ["-l", "-c", "\(AgentRunner.shq(shell)) -i -c \(AgentRunner.shq(probe))"]
    }

    /// The probe that prints where the executable `name` is on the shell's `PATH`, past
    /// any alias or function of the same name: zsh's `whence -p`, else bash's `type -P`.
    /// Not `command -v`, which prints an alias's text instead of the file — and an
    /// operator's `alias claude='claude --dangerously-skip-permissions'` is exactly the
    /// definition a sandboxed call must not pick up.
    public static func locateProbe(_ name: String) -> String {
        let q = AgentRunner.shq(name)
        return "whence -p \(q) 2>/dev/null || type -P \(q) 2>/dev/null"
    }

    /// The executable `name` as `shell` locates it, or nil when it names none in time.
    /// `environment`, when given, replaces this process's own.
    public static func locate(_ name: String, shell: String,
                              environment: [String: String]? = nil) -> String? {
        guard let out = OpenCodeCLI.run(shell, arguments(shell: shell, probe: locateProbe(name)),
                                        within: timeout, environment: environment) else {
            return nil
        }
        return OpenCodeCLI.parseResolution(String(decoding: out, as: UTF8.self)) {
            FileManager.default.isExecutableFile(atPath: $0)
        }.path
    }

    /// `name` on this process's own `PATH`.
    public static func onPath(_ name: String) -> String? {
        let path = ProcessInfo.processInfo.environment["PATH"] ?? ""
        for dir in path.split(separator: ":") where !dir.isEmpty {
            let candidate = "\(dir)/\(name)"
            if FileManager.default.isExecutableFile(atPath: candidate) { return candidate }
        }
        return nil
    }
}
