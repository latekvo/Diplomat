import Foundation

/// Self-test for locating the checkout a bundle runs from — `DIPLOMAT_REPOPATHS_TEST=1`.
///
/// `szpont` builds and opens `<checkout>/packages/diplomat-platform/macos/Diplomat.app`
/// with the checkout under `~/.diplomat`, and launchd starts that same bundle: the
/// Update button, the 06:00 self-update and the mesh spawn all find the checkout by
/// the bundle's own location, or not at all. A copy kept anywhere else must not claim
/// one, and a layout without `.git` is not one.
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

        if failures.isEmpty { print("repopaths: all passed") }
        else { print("repopaths: FAILED \(failures.count): \(failures.joined(separator: "; "))") }
        return failures.isEmpty
    }
}
