import Foundation

/// Which agent CLI a spawn actually runs — the Swift twin of `diplomat_app/runner.py`.
///
/// Diplomat opens a terminal window and runs an agent in it. *Which* agent is one
/// setting, because the applet's job — dispatch, track, price, reap — is the same
/// either way:
///
/// * `claude` — Claude Code, the default and what every existing run used;
/// * `opencode` — OpenCode, whose model comes from whichever provider the user
///   configured in OpenCode itself (Anthropic, OpenRouter, a local Ollama, …).
///
/// Only the *agent word* differs. Everything the spawn is built out of — the prompt
/// staged into a file and handed over as `$(cat …)`, the completion sentinel, the pid
/// the run is identified by — is identical, deliberately: those are what
/// `AgentRegistry` and `ProcessTracker` recognise a run by, and a second spawn shape
/// would be a second set of them to keep true.
///
/// Credentials are the one thing this type refuses to hold. OpenCode has its own
/// provider store and its own login wizard, and that is where a key belongs — not in
/// `~/.diplomat/config.json`, which is world-readable by default and copied around by
/// the mesh. Diplomat stores the *choice* of runner and model; OpenCode stores the
/// secret.
public enum AgentRunner: String, CaseIterable, Sendable {
    case claude
    case opencode

    /// What Settings shows for each.
    public var label: String {
        switch self {
        case .claude: return "Claude Code"
        case .opencode: return "OpenCode"
        }
    }

    /// The configured runner, falling back to Claude Code.
    ///
    /// An unrecognised value degrades rather than failing the spawn — a hand-edited or
    /// newer config must not leave the applet unable to dispatch at all.
    public static func from(_ raw: String) -> AgentRunner {
        AgentRunner(rawValue: raw.trimmingCharacters(in: .whitespaces)) ?? .claude
    }

    /// OpenCode's permission gate, opened for a spawned agent.
    ///
    /// Claude Code gets its autonomy from the user's own `claude` alias (that is what
    /// `--dangerously-skip-permissions` in it is for). OpenCode has no alias to carry
    /// it, so the equivalent is set here — an agent that stops to ask permission in a
    /// window nobody is watching never finishes, and holds a bay of the task cap until
    /// someone notices.
    ///
    /// Carried as an assignment *inside the command*, never in the spawner's
    /// environment: the macOS spawner has no environment channel at all, typing a line
    /// into a fresh window via AppleScript, and the Linux twin's `tmux new-session`
    /// runs its command with the tmux *server's* environment.
    public static let permissionEnv = "OPENCODE_PERMISSION"
    public static let permissionValue = #"{"edit":"allow","bash":"allow","webfetch":"allow","external_directory":"allow","doom_loop":"allow"}"#

    /// The one command that runs the agent: `<cli> <prompt-bearing args>`.
    ///
    /// This is the *whole* of what a runner changes about a spawn. It is a shell
    /// snippet rather than an argv because the prompt reaches the agent as
    /// `"$(cat <file>)"` — a staged file beats threading a multi-line prompt through
    /// nested AppleScript and shell quoting.
    ///
    /// It must stay a *simple command* with the agent word first: under Claude Code
    /// that word has to be alias-expandable, since the alias is what carries
    /// `--dangerously-skip-permissions`. A leading variable assignment keeps that
    /// property for OpenCode, which has no alias to expand.
    ///
    /// `model` is a `provider/model` id, or empty to let OpenCode use the one its own
    /// picker already remembers — passing a guess would silently move the user off it.
    public func agentCommand(promptFile: String, model: String = "") -> String {
        let prompt = "\"$(cat \(Self.shq(promptFile)))\""
        switch self {
        case .claude:
            return "claude \(prompt)"
        case .opencode:
            let trimmed = model.trimmingCharacters(in: .whitespaces)
            let flag = trimmed.isEmpty ? "" : " -m \(Self.shq(trimmed))"
            let grant = "\(Self.permissionEnv)=\(Self.shq(Self.permissionValue))"
            // `--prompt` starts the TUI with the prompt already submitted, which is
            // what makes this a windowed agent the user can watch and type into — the
            // same affordance the Claude runner has. `opencode run` would be headless.
            return "\(grant) opencode\(flag) --prompt \(prompt)"
        }
    }

    /// Whether a `ps` line is an agent of *any* runner.
    ///
    /// Every scan that counts, adopts or reaps an agent asks this, so the answer stays
    /// in one place: a runner the applet can spawn but a scan cannot see is an agent
    /// that runs forever without holding a bay, and one the panel redraws as untracked
    /// on every tick.
    ///
    /// Deliberately as loose as the test it replaces — a wrapper shell and the agent
    /// both carry the word, which is what the age half of the pid-adoption guard is
    /// there to disambiguate.
    public static func isAgentLine(_ line: String) -> Bool {
        allCases.contains { line.contains($0.rawValue) }
    }

    /// The command that lets a user connect a provider to OpenCode.
    ///
    /// Diplomat deliberately does not ask for a provider and an API key itself.
    /// OpenCode ships a wizard that knows its whole provider catalog, which entries
    /// take an OAuth flow rather than a key, and where each one's credentials belong —
    /// and it writes them to the store the agent reads from anyway. A second key field
    /// here would be a worse copy of it that also puts a secret in Diplomat's config.
    public static let setupCommand = "opencode providers login; opencode providers list"

    /// Single-quote for the shell, the same way `ReviewWizard.shq` does.
    static func shq(_ s: String) -> String { "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'" }
}
