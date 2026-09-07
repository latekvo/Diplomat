import Foundation

/// Self-test for where the app finds its own checkout - `DIPLOMAT_REPOPATHS_TEST=1`.
///
/// `szpont` builds and opens `<checkout>/packages/diplomat-platform/macos/Diplomat.app`
/// with the checkout under `~/.diplomat`, and launchd starts that same bundle: the
/// Update button, the 06:00 self-update and the mesh spawn all find the checkout by
/// the bundle's own location, or not at all. A copy kept anywhere else must not claim
/// one, and a layout without `.git` is not one.
///
/// The bundle is the third reading of four, and the order is what this pins:
/// `DIPLOMAT_SELF_REPO`, the tree an unbundled run's assets sit in, the checkout around
/// the bundle, then `~/dev/diplomat`.
///
///     DIPLOMAT_REPOPATHS_TEST=1 swift run Diplomat
///
/// Lays the shapes out in a scratch directory it removes; reads nothing else.
enum RepoPathsTest {
    static func run() -> Bool {
        var failures: [String] = []
        func check(_ name: String, _ cond: Bool, _ detail: String = "") {
            if cond { print("  ok    \(name)") }
            else { print("  FAIL  \(name) \(detail)"); failures.append(name) }
        }

        let fm = FileManager.default
        let scratch = fm.temporaryDirectory.appendingPathComponent("diplomat-repopaths-\(getpid())")
        defer { try? fm.removeItem(at: scratch) }
        let checkout = scratch.appendingPathComponent("checkout")
        let bundle = checkout.appendingPathComponent("packages/diplomat-platform/macos/Diplomat.app")
        let copy = scratch.appendingPathComponent("Applications/Diplomat.app")
        for dir in [bundle, copy] {
            try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        }

        print("repopaths: the checkout a bundle runs from")
        check("a layout with no .git is not a checkout",
              RepoPaths.checkoutHolding(bundle: bundle) == nil)
        fm.createFile(atPath: checkout.appendingPathComponent(".git").path, contents: Data())
        let found = RepoPaths.checkoutHolding(bundle: bundle)
        check("the bundle build-app.sh writes names the checkout around it",
              found?.path == checkout.path, "got \(found?.path ?? "nil")")
        check("a copy kept anywhere else names none",
              RepoPaths.checkoutHolding(bundle: copy) == nil)

        // Every reading points somewhere different, so the answer says which one won.
        print("repopaths: which reading names the checkout")
        let home = scratch.appendingPathComponent("home")
        let fallback = home.appendingPathComponent("dev/diplomat")
        let bundled = bundle.appendingPathComponent("Contents/Resources/assets")
        let unbundled = scratch.appendingPathComponent("unbundled")
        let elsewhere = scratch.appendingPathComponent("elsewhere")
        func root(env: String? = nil, assets: URL? = bundled, bundle: URL = bundle) -> String {
            RepoPaths.locate(env: env, assets: assets, bundle: bundle, home: home).path
        }
        check("a launchd start finds the checkout around its bundle, not ~/dev/diplomat",
              root() == checkout.path, "got \(root())")
        check("a copy kept anywhere else falls back to ~/dev/diplomat",
              root(bundle: copy) == fallback.path, "got \(root(bundle: copy))")
        let devAssets = unbundled.appendingPathComponent("packages/diplomat-core/assets")
        check("swift run's assets name the tree they were read from, ahead of the bundle",
              root(assets: devAssets) == unbundled.path, "got \(root(assets: devAssets))")
        check("DIPLOMAT_SELF_REPO wins over both",
              root(env: elsewhere.path, assets: devAssets) == elsewhere.path,
              "got \(root(env: elsewhere.path, assets: devAssets))")
        check("…and an empty one is unset",
              root(env: "") == checkout.path, "got \(root(env: ""))")

        if failures.isEmpty { print("repopaths: all passed") }
        else { print("repopaths: FAILED \(failures.count): \(failures.joined(separator: "; "))") }
        return failures.isEmpty
    }
}
