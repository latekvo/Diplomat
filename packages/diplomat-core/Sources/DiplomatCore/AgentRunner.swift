import Foundation

/// Which agent CLI a spawn actually runs — the Swift twin of `diplomat_runtime/runner.py`.
///
/// Diplomat opens a terminal window and runs an agent in it. *Which* agent is one
/// setting, because the applet's job — dispatch, track, price, reap — is the same
/// either way:
///
/// * `claude` — Claude Code, the default and what every existing run used;
/// * `opencode` — OpenCode, whose model comes from whichever provider the user
///   configured in OpenCode itself (Anthropic, OpenRouter, a local Ollama, …);
/// * `hermes` — Hermes Agent, likewise;
/// * `freebuff` — Freebuff, Codebuff's free tier, on the account its own login
///   connects.
///
/// Only the *agent word and its flags* differ. Everything the spawn is built out of —
/// the prompt staged into a file, the completion sentinel, the pid the run is
/// identified by — is identical, deliberately: those are what `AgentRegistry` and
/// `AgentState` recognise a run by, and a second spawn shape would be a second set of
/// them to keep true.
///
/// Three of the four also take that prompt on the command line, as `$(cat …)`.
/// Freebuff takes none: its CLI accepts a prompt argument only when the same binary
/// runs as `codebuff`, and under the `freebuff` name the parser both restricts its one
/// positional to `login` and hard-codes the initial prompt to nothing. So a Freebuff
/// spawn opens on an empty composer and is handed its prompt afterwards, by typing —
/// see `promptHandoff` and `takesTypedPrompt`.
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
    case freebuff

    /// What Settings shows for each.
    public var label: String {
        switch self {
        case .claude: return "Claude Code"
        case .opencode: return "OpenCode"
        case .hermes: return "Hermes"
        case .freebuff: return "Freebuff"
        }
    }

    /// Whether this runner takes `-m <model>`. False for Freebuff, whose CLI has no
    /// such flag — the free tier picks the model server-side, and Settings hides the
    /// field rather than offering one whose value nothing would read.
    public var takesModel: Bool { self == .opencode || self == .hermes }

    /// Whether a spawn of this runner opens on an EMPTY session, so its prompt has to
    /// be typed into it afterwards rather than passed on the command line.
    ///
    /// True for Freebuff alone. Every caller that dispatches has to ask, because a run
    /// that never receives its prompt is the worst shape of failure the applet has:
    /// the process is up, `ps` sees it, it holds a bay of the task cap, and it will sit
    /// there doing nothing until a human closes the window.
    public var takesTypedPrompt: Bool { self == .freebuff }

    /// The single line typed into a Freebuff composer to start its run.
    ///
    /// A pointer at the staged prompt rather than the prompt itself, and that is the
    /// whole reason this is one line: the terminal channels that can type into a live
    /// session submit a LINE (iTerm `write text` / Terminal `do script … in tab` here,
    /// tmux `send-keys` + Enter on Linux), so a multi-line review prompt sent through
    /// either would submit at its first newline and hand the agent a fragment. Both
    /// channels already exist for the API-error nudge, and this reuses them unchanged.
    ///
    /// The file is the run's own `prompt.txt` (`AgentRegistry`), which outlives the
    /// hand-off and is deleted only when the run is retired. It sits outside the repo,
    /// so the line says how to reach it as well as what to do with it — Codebuff's file
    /// tools are scoped to the project it opened, while its terminal tool is not.
    public static func promptHandoff(promptFile: String) -> String {
        "Read the instructions in \(promptFile) (cat it in the terminal — it is "
            + "outside this project) and follow them exactly."
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
    /// the agent's screen. It is ignored by the other three, which have no such server —
    /// Hermes answers the same question from its own session store, and Freebuff's is on
    /// Freebuff's own machines. Omitting it is a supported spawn, not a broken one: the
    /// run works exactly as before and is tracked by its screen.
    ///
    /// `repo` is the checkout the spawn `cd`s into, and only Freebuff is passed it on its
    /// command line as well. `promptFile` is the one thing Freebuff ignores, taking no
    /// prompt argument at all (`promptHandoff`): it is the one runner whose returned
    /// command does not carry the prompt, and the one whose spawn is not finished when
    /// the command has run.
    public func agentCommand(promptFile: String, model: String = "", port: Int = 0,
                             repo: String = "") -> String {
        let prompt = "\"$(cat \(Self.shq(promptFile)))\""
        let trimmed = model.trimmingCharacters(in: .whitespaces)
        let flag = trimmed.isEmpty ? "" : " -m \(Self.shq(trimmed))"
        switch self {
        case .claude:
            return "claude \(prompt)"
        case .freebuff:
            // `--cwd` rather than leaning on the `cd` the spawn already did, because
            // the two fail differently. That `cd` is deliberately quiet
            // (`2>/dev/null`), and where the other runners would then simply work in
            // the wrong directory, Freebuff opens a full-screen directory PICKER when
            // its working directory is not a project — an agent that can never start,
            // holding a bay of the task cap until someone closes the window.
            return "freebuff --cwd \(Self.shq(repo))"
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
    /// Diplomat deliberately does not ask for a provider and an API key itself. OpenCode
    /// and Hermes each ship a wizard that knows their whole provider catalog, which
    /// entries take an OAuth flow rather than a key, and where each one's credentials
    /// belong — and each writes them to the store its agent reads from anyway. A key
    /// field here would be a worse copy of that which also put a secret in Diplomat's
    /// config.
    ///
    /// The listing command runs after, so the window the user is left looking at states
    /// what is now connected rather than making them trust that it worked.
    ///
    /// Freebuff is the exception to the *provider* half: it has no catalog to choose
    /// from, only the one account its own site signs a user into, so its wizard is the
    /// whole of it and there is nothing to list afterwards. The window stays on the
    /// command, which is what prints the URL the user has to open.
    public var setupCommand: String {
        switch self {
        case .freebuff: return "freebuff login"
        case .hermes: return "hermes setup; hermes status"
        default: return "opencode providers login; opencode providers list"
        }
    }

    /// Single-quote for the shell, the same way `ReviewWizard.shq` does.
    static func shq(_ s: String) -> String { "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'" }
}
