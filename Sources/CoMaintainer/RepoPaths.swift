import Foundation
import CoMaintainerCore

/// Where the running app's own git checkout lives on disk — the source tree behind
/// both the self-update (git pull + rebuild) and the mesh node (`python3 -m
/// co_maintainer.mesh`, which runs from `<repo>/linux`).
///
/// A packaged `CoMaintainer.app` is decoupled from its source (it may sit in
/// /Applications), so the checkout is located by, in order: an explicit env override,
/// the repo layout inferred when running unbundled (`swift run`, where `core/` resolves
/// to `<repo>/core`), then the user's conventional checkout path. Mirrors the Linux
/// front-end's `selfupdate.repo_root` (env `CO_MAINTAINER_SELF_REPO`, else the checkout).
enum RepoPaths {
    private static var home: URL { FileManager.default.homeDirectoryForCurrentUser }

    /// The checkout root. Env-overridable; falls back to the conventional path used by
    /// the sibling `DeviceAllocator.packageDir` default (a personal, single-checkout setup).
    static var root: URL {
        if let env = ProcessInfo.processInfo.environment["CO_MAINTAINER_SELF_REPO"], !env.isEmpty {
            return URL(fileURLWithPath: env)
        }
        // Running unbundled (`swift run CoMaintainer`): CoreAssets resolves core/ to
        // <repo>/core, so the repo root is its parent. Skip this when core/ came from
        // inside the .app bundle (…/Contents/Resources/core), which isn't a checkout.
        if let core = try? CoreAssets.coreDir(),
           core.lastPathComponent == "core",
           !core.path.contains(".app/Contents/") {
            return core.deletingLastPathComponent()
        }
        return home.appendingPathComponent("dev/co-maintainer-applet")
    }

    /// True when `root` looks like an actual checkout (a `.git` and the `linux/` tree),
    /// so the UI can disable the Update button / mesh spawn with a clear reason instead
    /// of failing obscurely on a missing directory.
    static var checkoutPresent: Bool {
        let fm = FileManager.default
        return fm.fileExists(atPath: root.appendingPathComponent(".git").path)
            && fm.fileExists(atPath: root.appendingPathComponent("linux/co_maintainer/mesh").path)
    }
}
