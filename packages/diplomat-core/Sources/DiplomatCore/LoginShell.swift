import Foundation

/// Asks the user's own shell where a CLI is, as an agent's terminal would find it: an app
/// launched from the Dock (or a systemd user unit) inherits a bare `PATH`, and the
/// per-user installs agent CLIs ship as (Homebrew, npm, nvm, `~/.local/bin`) are put on
/// it by the shell's rc files.
public enum LoginShell {
    /// rc files (a version manager, a prompt framework) can be slow.
    public static let timeout: TimeInterval = 10

    /// A login shell running an interactive one, the pair the agent runs under (the
    /// terminal's login shell, then `AgentSpawner.shellCommand`'s inner shell). Both
    /// matter: Homebrew on Apple silicon sets `PATH` in `~/.zprofile`, read only by a login
    /// zsh, and nvm lives in `~/.bashrc`, read only by an interactive bash.
    public static func arguments(shell: String, probe: String) -> [String] {
        ["-l", "-c", "\(AgentRunner.shq(shell)) -i -c \(AgentRunner.shq(probe))"]
    }

    /// Skips aliases and functions. Not `command -v`, which prints an alias's text: an
    /// operator's `alias claude='claude --dangerously-skip-permissions'` is exactly what a
    /// sandboxed call must not pick up.
    public static func locateProbe(_ name: String) -> String {
        let q = AgentRunner.shq(name)
        return "whence -p \(q) 2>/dev/null || type -P \(q) 2>/dev/null"
    }

    /// Nil when `shell` names no executable in time. `environment`, when given, replaces
    /// this process's own.
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
