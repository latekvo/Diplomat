import Foundation
import DiplomatCore

/// The two repository roots the app cares about: its OWN checkout (`root` — the source
/// tree behind self-update and the mesh node) and the TARGET repo the agents work in
/// (`agentRepo` — Settings → REPO ROOT). They are unrelated paths; keeping both here
/// keeps the two resolutions from being confused for each other.
///
/// `root` is the source tree behind both the self-update (git pull + rebuild) and the
/// mesh node (`python3 -m szpontnet`, which runs from `runtimePackage` with
/// `szpontnetPackage` on its import path).
///
/// The checkout is located by, in order: an explicit env override, the layout
/// inferred when running unbundled (`swift run`, where the shared assets resolve to
/// `<repo>/packages/diplomat-core/assets`), the layout `build-app.sh` writes the
/// bundle into (`<repo>/packages/diplomat-platform/macos/Diplomat.app` - what `szpont`
/// opens and launchd starts), then the user's conventional checkout path for a copy
/// of the bundle kept anywhere else. Mirrors the Linux front-end's
/// `selfupdate.repo_root` (env `DIPLOMAT_SELF_REPO`, else the checkout its own file
/// sits in).
enum RepoPaths {
    private static var home: URL { FileManager.default.homeDirectoryForCurrentUser }

    /// The checkout root. Env-overridable; falls back to the conventional clone path
    /// (a personal, single-checkout setup). Everything that lives beside the app in
    /// this monorepo — the SzpontNet library, the device-allocator package — hangs off
    /// here, so a moved or renamed checkout relocates all of them together.
    static var root: URL {
        if let env = ProcessInfo.processInfo.environment["DIPLOMAT_SELF_REPO"], !env.isEmpty {
            return URL(fileURLWithPath: env)
        }
        // Running unbundled (`swift run Diplomat`): CoreAssets resolves assets/ to
        // <repo>/packages/diplomat-core/assets, so the repo root is three levels up.
        // Skip this when the assets came from inside the .app bundle
        // (…/Contents/Resources/assets), which isn't a checkout.
        if let assets = try? CoreAssets.assetsDir(),
           assets.lastPathComponent == "assets",
           !assets.path.contains(".app/Contents/") {
            return assets.deletingLastPathComponent()   // packages/diplomat-core
                .deletingLastPathComponent()            // packages
                .deletingLastPathComponent()            // the checkout
        }
        if let checkout = checkoutHolding(bundle: Bundle.main.bundleURL) {
            return checkout
        }
        return home.appendingPathComponent("dev/diplomat")
    }

    /// The checkout `bundle` was built inside, while it still sits there: `build-app.sh`
    /// writes `<checkout>/packages/diplomat-platform/macos/Diplomat.app`, and `szpont`
    /// keeps that checkout under `~/.diplomat`, where no convention would find it. A
    /// bundle copied anywhere else, or a layout with no `.git`, names none.
    static func checkoutHolding(bundle: URL) -> URL? {
        let parents = bundle.pathComponents.dropLast().suffix(3)
        guard Array(parents) == ["packages", "diplomat-platform", "macos"] else { return nil }
        let checkout = bundle
            .deletingLastPathComponent()   // macos
            .deletingLastPathComponent()   // diplomat-platform
            .deletingLastPathComponent()   // packages
            .deletingLastPathComponent()   // the checkout
        let git = checkout.appendingPathComponent(".git").path   // a file, in a worktree
        return FileManager.default.fileExists(atPath: git) ? checkout : nil
    }

    /// The monorepo's package directory — every sibling this app reaches for lives
    /// under it, so the layout is spelled once rather than at each use.
    static var packages: URL { root.appendingPathComponent("packages") }

    /// The SzpontNet library's project dir (what goes on a node's `PYTHONPATH`).
    static var szpontnetPackage: URL { packages.appendingPathComponent("szpontnet-core") }

    /// Diplomat's platform-neutral Python runtime — the half below the UI that both
    /// front-ends run. macOS reaches for it to get `diplomat_runtime.szponthost`, the
    /// module that puts Diplomat behind a mesh node; the other front-end's package is
    /// no part of that, and this app never resolves it.
    static var runtimePackage: URL {
        packages.appendingPathComponent("diplomat-runtime")
    }

    /// This app's own package dir, and the scripts inside it that build the bundle
    /// and register it with launchd.
    static var macosPackage: URL {
        packages.appendingPathComponent("diplomat-platform/macos")
    }
    static var installDir: URL { macosPackage.appendingPathComponent("install") }

    /// True when `root` looks like an actual checkout, so the UI can disable the Update
    /// button / mesh spawn with a clear reason instead of failing obscurely on a missing
    /// directory. Both trees, because a node spawn needs both: the library it runs, and
    /// the runtime holding the host module that puts Diplomat behind it.
    static var checkoutPresent: Bool {
        let fm = FileManager.default
        return fm.fileExists(atPath: root.appendingPathComponent(".git").path)
            && fm.fileExists(atPath: szpontnetPackage.appendingPathComponent("szpontnet").path)
            && fm.fileExists(atPath: runtimePackage.appendingPathComponent("diplomat_runtime").path)
    }

    // MARK: - the TARGET repo (where the agents work)

    /// The checkout every spawned agent `cd`s into — the local clone of the *target*
    /// repo from `assets/config.json` (`software-mansion/argent`), NOT Diplomat's own
    /// source tree (`root`).
    ///
    /// Strongest first: the `DIPLOMAT_REPO` env override (every other `DIPLOMAT_*`
    /// knob wins over stored state, and the Linux front-end reads the same variable),
    /// the path picked in Settings, then `~/dev/<repo>`. The Settings hint calls the
    /// env override out when it's set, so a shadowed field is never a silent no-op.
    ///
    /// Re-read on every use (a spawn, a hint refresh): the pick lives in the shared
    /// `AppConfig` file precisely so a change reaches a *running* mesh node too.
    static var agentRepo: String {
        if let env = agentRepoEnvOverride { return env }
        let stored = storedAgentRepo
        return stored.isEmpty ? defaultAgentRepo : expand(stored)
    }

    /// The user's pick from Settings, trimmed; empty when unset (⇒ fall back).
    /// Newlines are trimmed too — one would otherwise break the spawn's AppleScript
    /// string literal (the Linux twin's `.strip()` does the same).
    private static var storedAgentRepo: String {
        AppConfig.string(AppConfig.repoRootKey)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// `~/dev/<repo>` — the conventional checkout path for whichever repo `assets/config.json`
    /// targets, so the fallback follows a retargeted config instead of naming one repo.
    static var defaultAgentRepo: String {
        home.appendingPathComponent("dev/\(CoreAssets.repoCoordinates().repo)").path
    }

    /// `DIPLOMAT_REPO`, when it's set — the Settings screen shows that it wins.
    static var agentRepoEnvOverride: String? {
        let env = ProcessInfo.processInfo.environment["DIPLOMAT_REPO"] ?? ""
        return env.isEmpty ? nil : expand(env)
    }

    /// Expand a leading `~` so a hand-typed "~/dev/argent" resolves like it would in
    /// the shell (the spawn command single-quotes the path, so the shell won't).
    private static func expand(_ path: String) -> String {
        (path as NSString).expandingTildeInPath
    }

    /// How the Settings hint describes the resolved path. A relative entry gets its own
    /// state: `isCheckout` would judge it against THIS app's working directory while the
    /// spawn's `cd` runs in the terminal's — the two disagree, so neither verdict is
    /// honest. Mirrors `settingsview._repo_state` on Linux.
    enum AgentRepoState { case ok, envShadowed, notAbsolute, notACheckout }

    static var agentRepoState: AgentRepoState {
        if agentRepoEnvOverride != nil { return .envShadowed }
        let path = agentRepo
        if !path.hasPrefix("/") { return .notAbsolute }
        return isCheckout(path) ? .ok : .notACheckout
    }

    /// Whether `path` is a git checkout (`.git` dir, or the file a worktree uses).
    /// The spawn's `cd` is best-effort, so a wrong path would otherwise fail silently
    /// and run the agent in the home directory — the Settings hint warns instead.
    static func isCheckout(_ path: String) -> Bool {
        FileManager.default.fileExists(
            atPath: URL(fileURLWithPath: path).appendingPathComponent(".git").path)
    }
}
