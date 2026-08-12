import Foundation

/// Which agent CLI a spawn actually runs — the Swift twin of `diplomat_app/runner.py`.
///
/// Diplomat opens a terminal window and runs an agent in it. *Which* agent is one
/// setting, because the applet's job — dispatch, track, price, reap — is the same
/// either way:
///
/// * `claude` — Claude Code, the default and what every existing run used;
/// * `opencode` — OpenCode, whose model comes from whichever provider the user
///   configured in OpenCode itself (Anthropic, OpenRouter, a local Ollama, …);
/// * `hermes` — Hermes Agent, likewise.
///
/// Only the *agent word and its flags* differ. Everything the spawn is built out of —
/// the prompt staged into a file and handed over as `$(cat …)`, the completion
/// sentinel, the pid the run is identified by — is identical, deliberately: those are
/// what `AgentRegistry` and `ProcessTracker` recognise a run by, and a second spawn
/// shape would be a second set of them to keep true.
///
/// Credentials are the one thing this type refuses to hold. Each foreign runner has its
/// own provider store and its own login wizard, and that is where a key belongs — not
/// in `~/.diplomat/config.json`, which is world-readable by default and copied around
/// by the mesh. Diplomat stores the *choice* of runner and model; the runner stores the
/// secret.
public enum AgentRunner: String, CaseIterable, Sendable {
    case claude
    case opencode
    case hermes

    /// What Settings shows for each.
    public var label: String {
        switch self {
        case .claude: return "Claude Code"
        case .opencode: return "OpenCode"
        case .hermes: return "Hermes"
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
    /// `model` is a model id, or empty to let the runner use the one its own picker
    /// already remembers — passing a guess would silently move the user off it. The
    /// Claude runner takes no such flag here and ignores it.
    ///
    /// `port` puts an OpenCode run's own server on a port the applet already knows, which
    /// is what lets `OpenCodeAPI` ask the agent what it is doing instead of reading it off
    /// the agent's screen. It is ignored by the other two, which have no such server —
    /// Hermes answers the same question from its own session store. Omitting it is a
    /// supported spawn, not a broken one: the run works exactly as before and is tracked
    /// by its screen.
    public func agentCommand(promptFile: String, model: String = "", port: Int = 0) -> String {
        let prompt = "\"$(cat \(Self.shq(promptFile)))\""
        let trimmed = model.trimmingCharacters(in: .whitespaces)
        let flag = trimmed.isEmpty ? "" : " -m \(Self.shq(trimmed))"
        switch self {
        case .claude:
            return "claude \(prompt)"
        case .hermes:
            // `--yolo` bypasses the approval prompts, the same autonomy the Claude
            // alias carries and `OPENCODE_PERMISSION` grants below. `-q` submits the
            // prompt into the TUI, so this is a windowed agent the user can watch and
            // type into, and the query is stored verbatim as the session's opening
            // message — which is how `HermesStore` tells this run's session from a
            // sibling's in the same checkout.
            return "hermes chat --tui --yolo\(flag) -q \(prompt)"
        case .opencode:
            // OpenCode's default hostname is loopback, so this exposes the run to other
            // users of this machine and to nothing else. It cannot also be
            // password-protected: the server takes one, but OpenCode's own TUI sends
            // none, so a run started with `OPENCODE_SERVER_PASSWORD` set exits on
            // `Unauthorized` before doing any work.
            let listen = port > 0 ? " --port \(port)" : ""
            let grant = "\(Self.permissionEnv)=\(Self.shq(Self.permissionValue))"
            // `--prompt` starts the TUI with the prompt already submitted, which is
            // what makes this a windowed agent the user can watch and type into — the
            // same affordance the Claude runner has. `opencode run` would be headless.
            // It also lands verbatim as the session's opening message, which is how
            // `OpenCodeAPI.isOurs` tells this run's session from a sibling's in the
            // same checkout.
            return "\(grant) opencode\(listen)\(flag) --prompt \(prompt)"
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

    /// The command that lets a user connect a provider to this runner.
    ///
    /// Diplomat deliberately does not ask for a provider and an API key itself. Both
    /// foreign runners ship a wizard that knows their whole provider catalog, which
    /// entries take an OAuth flow rather than a key, and where each one's credentials
    /// belong — and each writes them to the store its agent reads from anyway. A key
    /// field here would be a worse copy of that which also put a secret in Diplomat's
    /// config.
    ///
    /// The listing command runs after, so the window the user is left looking at states
    /// what is now connected rather than making them trust that it worked.
    public var setupCommand: String {
        self == .hermes ? "hermes setup; hermes status"
                        : "opencode providers login; opencode providers list"
    }

    /// Single-quote for the shell, the same way `ReviewWizard.shq` does.
    static func shq(_ s: String) -> String { "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'" }
}
