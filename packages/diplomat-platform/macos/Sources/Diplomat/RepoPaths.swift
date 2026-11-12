import Foundation
import DiplomatCore

/// The repository roots the app cares about: its OWN checkout (`root`, the source tree
/// behind self-update: git pull + rebuild) and the TARGET repos the agents work in
/// (`roots`, Settings → REPO ROOTS, the first being `agentRepo`). Unrelated paths, kept
/// side by side so the two are never confused.
///
/// The checkout is located by, in order: an explicit env override, the layout
/// inferred when running unbundled (`swift run`, where the shared assets resolve to
/// `<repo>/packages/diplomat-core/assets`), the layout `build-app.sh` writes the
/// bundle into (`<repo>/packages/diplomat-platform/macos/Diplomat.app` - what `szpont`
/// opens and launchd starts), the checkout `build-app.sh` recorded in the bundle's
/// Info.plist (for a copy kept anywhere else), then the user's conventional checkout
/// path. Mirrors the Linux front-end's `selfupdate.repo_root` (env
/// `DIPLOMAT_SELF_REPO`, else the checkout its own file sits in).
enum RepoPaths {
    private static var home: URL { FileManager.default.homeDirectoryForCurrentUser }

    /// The checkout root. Env-overridable; falls back to the conventional clone path
    /// (a personal, single-checkout setup). Everything that lives beside the app in
    /// this monorepo — the device-allocator package, the install scripts — hangs off
    /// here, so a moved or renamed checkout relocates all of them together.
    static var root: URL {
        locate(env: ProcessInfo.processInfo.environment["DIPLOMAT_SELF_REPO"],
               assets: try? CoreAssets.assetsDir(),
               bundle: Bundle.main.bundleURL,
               recorded: Bundle.main.object(forInfoDictionaryKey: "DiplomatCheckout") as? String,
               home: home)
    }

    /// `root`, from the five readings it is made of.
    static func locate(env: String?, assets: URL?, bundle: URL, recorded: String?,
                       home: URL) -> URL {
        if let env, !env.isEmpty {
            return URL(fileURLWithPath: env)
        }
        // Running unbundled (`swift run Diplomat`): CoreAssets resolves assets/ to
        // <repo>/packages/diplomat-core/assets, so the repo root is three levels up.
        // Skip this when the assets came from inside the .app bundle
        // (…/Contents/Resources/assets), which isn't a checkout.
        if let assets,
           assets.lastPathComponent == "assets",
           !assets.path.contains(".app/Contents/") {
            return assets.deletingLastPathComponent()   // packages/diplomat-core
                .deletingLastPathComponent()            // packages
                .deletingLastPathComponent()            // the checkout
        }
        if let checkout = checkoutHolding(bundle: bundle) {
            return checkout
        }
        if let recorded, !recorded.isEmpty {
            return URL(fileURLWithPath: recorded)
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

    /// This app's own package dir, and the scripts inside it that build the bundle
    /// and register it with launchd.
    static var macosPackage: URL {
        packages.appendingPathComponent("diplomat-platform/macos")
    }
    static var installDir: URL { macosPackage.appendingPathComponent("install") }

    /// True when `root` is an actual checkout, so the UI can disable the Update button
    /// with a clear reason instead of failing obscurely on a missing directory.
    static var checkoutPresent: Bool { isCheckout(root.path) }

    // MARK: - the TARGET repos (where the agents work)

    /// The repo roots, as stored (a `~` left unexpanded), the first being the primary:
    /// local clones of the *target* repos, NOT Diplomat's own source tree (`root`).
    ///
    /// Strongest first: the `DIPLOMAT_REPO` env override, a single root (every other
    /// `DIPLOMAT_*` knob wins over stored state, and the Linux front-end reads the same
    /// variable), the roots picked in Settings (`AppConfig.repoRoots`), then
    /// `~/dev/<repo>`. The Settings hint calls the env override out when it's set, so a
    /// shadowed field is never a silent no-op.
    ///
    /// Re-read on every use (a spawn, a hint refresh), so a pick lands on the next
    /// spawn.
    static var rootPaths: [String] {
        let env = ProcessInfo.processInfo.environment["DIPLOMAT_REPO"] ?? ""
        if !env.isEmpty { return [env] }
        let stored = AppConfig.repoRoots
        return stored.isEmpty ? [defaultAgentRepo] : stored
    }

    /// `rootPaths`, each resolved to the GitHub repo it is a checkout of
    /// (`RepoRoots.resolve`); never empty.
    ///
    /// Reading the remotes costs a `git` call per root, so a resolution is kept for
    /// `rootsTTL` per list of paths, or until `invalidateRoots` drops it: a root that
    /// failed to resolve, or whose remotes changed, is read again within that. A read on
    /// the main thread never waits for git: it answers the last resolution of these
    /// paths, or with none as though no root had a remote (the primary path standing for
    /// the configured repo), and resolves in the background for the next read.
    static var roots: [PRRef.LocalRepo] { resolved(for: rootPaths).roots }

    /// Seconds a resolution of the roots is kept. Twin of `review.ROOTS_TTL`.
    static let rootsTTL: TimeInterval = 60

    struct RootsReading: Equatable {
        let roots: [PRRef.LocalRepo]
        /// Every path had a GitHub remote (`RepoRoots.allResolve`).
        let complete: Bool
        /// The first root is the first path's own repo (`RepoRoots.primaryResolves`).
        let primaryResolved: Bool
        /// None of several paths resolved, the one root a guess (`RepoRoots.guessed`).
        let guessed: Bool

        init(paths: [String], remotes: [String: String]) {
            roots = RepoRoots.resolve(paths: paths, remotes: remotes,
                                      configured: CoreAssets.repoCoordinates())
            complete = RepoRoots.allResolve(paths: paths, remotes: remotes)
            primaryResolved = RepoRoots.primaryResolves(paths: paths, remotes: remotes)
            guessed = RepoRoots.guessed(paths: paths, remotes: remotes)
        }
    }

    /// The cached reading of `paths`, else a fresh one.
    static func resolved(for paths: [String], now: Date = Date()) -> RootsReading {
        rootsLock.lock()
        let cached = rootsCache.flatMap { $0.paths == paths ? $0 : nil }
        rootsLock.unlock()
        if let cached, now.timeIntervalSince(cached.at) < rootsTTL {
            return cached.reading
        }
        guard !Thread.isMainThread else {
            rootsLock.lock()
            let start = !rootsResolving
            rootsResolving = true
            rootsLock.unlock()
            if start {
                Task.detached(priority: .utility) { _ = resolveRoots(paths, at: Date()) }
            }
            return cached?.reading ?? RootsReading(paths: paths, remotes: [:])
        }
        return resolveRoots(paths, at: now)
    }

    /// Forget the resolved roots, so a remote added or renamed since is read again.
    static func invalidateRoots() {
        rootsLock.lock()
        rootsCache = nil
        rootsLock.unlock()
    }

    private static let rootsLock = NSLock()
    private static var rootsCache: (paths: [String], at: Date, reading: RootsReading)?
    private static var rootsResolving = false

    private static func resolveRoots(_ paths: [String], at: Date) -> RootsReading {
        var remotes: [String: String] = [:]
        for path in paths.map(RepoRoots.expand) where isCheckout(path) {
            remotes[path] = LocalRepos.remotes(of: path)
        }
        let reading = RootsReading(paths: paths, remotes: remotes)
        rootsLock.lock()
        rootsCache = (paths, at, reading)
        rootsResolving = false
        rootsLock.unlock()
        return reading
    }

    /// The checkout every spawned agent `cd`s into when nothing names another: the
    /// primary root's.
    static var agentRepo: String { roots.first?.path ?? defaultAgentRepo }

    /// `~/dev/<repo>` — the conventional checkout path for whichever repo `assets/config.json`
    /// targets, so the fallback follows a retargeted config instead of naming one repo.
    static var defaultAgentRepo: String {
        home.appendingPathComponent("dev/\(CoreAssets.repoCoordinates().repo)").path
    }

    /// The stored root list once Settings adds `picked` to `listed`, a pick already listed
    /// skipped. An empty list stands for `fallback`, so it is listed first: an Add must
    /// not stop the agents working in the root they work in now.
    static func rootPaths(adding picked: [String], to listed: [String],
                          fallback: String = defaultAgentRepo) -> [String] {
        var out = listed.isEmpty ? [fallback] : listed
        for path in picked where !out.contains(path) { out.append(path) }
        return out
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

    /// How Settings describes one stored root. A relative entry gets its own state:
    /// `isCheckout` would judge it against THIS app's working directory while the spawn's
    /// `cd` runs in the terminal's; the two disagree, so neither verdict is honest.
    /// Mirrors `settingsview._root_problem` on Linux.
    enum RootState { case ok, notAbsolute, notACheckout }

    static func rootState(_ raw: String) -> RootState {
        let path = RepoRoots.expand(raw.trimmingCharacters(in: .whitespacesAndNewlines))
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
