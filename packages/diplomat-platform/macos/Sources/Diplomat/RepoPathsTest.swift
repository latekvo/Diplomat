import DiplomatCore
import Foundation

/// Self-test for where the app finds its own checkout - `DIPLOMAT_REPOPATHS_TEST=1`.
///
/// `szpont` builds and opens `<checkout>/packages/diplomat-platform/macos/Diplomat.app`
/// with the checkout under `~/.diplomat`, and launchd starts that same bundle: the
/// Update button, the 06:00 self-update and the mesh spawn all find the checkout by
/// the bundle's own location. A copy kept anywhere else must not claim one by its
/// path, and a layout without `.git` is not one.
///
/// The order of the five readings is what this pins: `DIPLOMAT_SELF_REPO`, the tree
/// an unbundled run's assets sit in, the checkout around the bundle, the checkout
/// `build-app.sh` recorded in Info.plist, then `~/dev/diplomat`. Also how long a
/// resolution of the repo roots is kept (`RepoPaths.rootsTTL`), and what an Add in
/// Settings stores (`RepoPaths.rootPaths(adding:to:)`).
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
        let recorded = scratch.appendingPathComponent("recorded").path
        func root(env: String? = nil, assets: URL? = bundled, bundle: URL = bundle,
                  recorded: String? = nil) -> String {
            RepoPaths.locate(env: env, assets: assets, bundle: bundle, recorded: recorded,
                             home: home).path
        }
        check("a launchd start finds the checkout around its bundle, not ~/dev/diplomat",
              root() == checkout.path, "got \(root())")
        check("…ahead of the one it recorded, which a moved checkout leaves stale",
              root(recorded: recorded) == checkout.path, "got \(root(recorded: recorded))")
        check("a copy kept anywhere else finds the checkout it recorded",
              root(bundle: copy, recorded: recorded) == recorded,
              "got \(root(bundle: copy, recorded: recorded))")
        check("…and one with no record falls back to ~/dev/diplomat",
              root(bundle: copy, recorded: "") == fallback.path,
              "got \(root(bundle: copy, recorded: ""))")
        let devAssets = unbundled.appendingPathComponent("packages/diplomat-core/assets")
        check("swift run's assets name the tree they were read from, ahead of the bundle",
              root(assets: devAssets) == unbundled.path, "got \(root(assets: devAssets))")
        check("DIPLOMAT_SELF_REPO wins over both",
              root(env: elsewhere.path, assets: devAssets) == elsewhere.path,
              "got \(root(env: elsewhere.path, assets: devAssets))")
        check("…and an empty one is unset",
              root(env: "") == checkout.path, "got \(root(env: ""))")

        // A root that fails to resolve once is read again after `rootsTTL`, not dropped
        // until Settings opens. Off the main thread, where a read waits for git.
        print("repopaths: a repo root that resolves late")
        func git(_ args: String...) {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/git")
            p.arguments = args
            p.standardOutput = Pipe()
            p.standardError = Pipe()
            try? p.run()
            p.waitUntilExit()
        }
        let early = scratch.appendingPathComponent("early").path
        let late = scratch.appendingPathComponent("late").path
        git("init", "-q", early)
        git("-C", early, "remote", "add", "origin", "git@github.com:o/early.git")
        let t0 = Date()
        final class Box { var value: (roots: [String], complete: Bool, primary: Bool, guessed: Bool)
            = ([], false, false, false) }
        func read(_ at: TimeInterval, _ paths: [String]? = nil)
            -> (roots: [String], complete: Bool, primary: Bool, guessed: Bool) {
            let out = Box()
            let done = DispatchSemaphore(value: 0)
            Thread {
                let r = RepoPaths.resolved(for: paths ?? [early, late], now: t0.addingTimeInterval(at))
                out.value = (r.roots.map(\.slug), r.complete, r.primaryResolved, r.guessed)
                done.signal()
            }.start()
            done.wait()
            return out.value
        }
        let first = read(0)
        check("a missing checkout is left out, and the roots say not every path resolved",
              first.roots == ["o/early"] && !first.complete && first.primary && !first.guessed,
              "got \(first)")
        git("init", "-q", late)
        git("-C", late, "remote", "add", "origin", "git@github.com:o/late.git")
        let kept = read(RepoPaths.rootsTTL - 1)
        check("…a resolution is kept for rootsTTL", kept.roots == ["o/early"], "got \(kept)")
        let again = read(RepoPaths.rootsTTL)
        check("…and then read again, so the checkout that appeared is a root",
              again.roots == ["o/early", "o/late"] && again.complete, "got \(again)")
        let gone = scratch.appendingPathComponent("gone").path
        let goneFirst = read(0, [gone, early])
        check("a first path that does not resolve leaves the next one's repo first, no primary",
              goneFirst.roots == ["o/early"] && !goneFirst.primary && !goneFirst.guessed,
              "got \(goneFirst)")
        let none = read(0, [gone, scratch.appendingPathComponent("gone2").path])
        check("…and none of several resolving leaves the one root a guess",
              !none.primary && none.guessed, "got \(none)")
        RepoPaths.invalidateRoots()

        print("repopaths: a root's line in Settings")
        let bare = scratch.appendingPathComponent("bare").path
        git("init", "-q", bare)
        let standIn = [PRRef.LocalRepo(owner: "o", repo: "configured", path: bare)]
        let resolvedLine = SettingsView.rootStatus(0, bare, roots: standIn, guessed: false)
        check("a resolved first root reads as the primary",
              resolvedLine == ("o/configured - primary", false), "got \(resolvedLine)")
        let guessedLine = SettingsView.rootStatus(0, bare, roots: standIn, guessed: true)
        check("…and a guessed one as unresolved, not a checkout of the configured repo",
              guessedLine.warn && guessedLine.text.hasPrefix("No GitHub remote"), "got \(guessedLine)")

        print("repopaths: adding roots in Settings")
        let defaultRoot = RepoPaths.defaultAgentRepo
        let seeded = RepoPaths.rootPaths(adding: ["/src/x"], to: [])
        check("an Add over none listed keeps the default root, first", seeded == [defaultRoot, "/src/x"],
              "got \(seeded)")
        let more = RepoPaths.rootPaths(adding: ["/src/y", "/src/x"], to: ["/src/x"])
        check("…over a list appends what it does not hold", more == ["/src/x", "/src/y"], "got \(more)")
        let same = RepoPaths.rootPaths(adding: [defaultRoot], to: [])
        check("…and picking the default over none lists it once", same == [defaultRoot], "got \(same)")

        if failures.isEmpty { print("repopaths: all passed") }
        else { print("repopaths: FAILED \(failures.count): \(failures.joined(separator: "; "))") }
        return failures.isEmpty
    }
}
