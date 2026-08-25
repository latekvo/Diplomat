import Foundation

/// Self-test for the focus contract of the spawn script — `DIPLOMAT_SPAWN_SCRIPT_TEST=1`.
///
/// `DIPLOMAT_SPAWN_FOCUS_TEST` proves the same contract against real windows, but it
/// needs a GUI session and Automation consent, so CI cannot host it. This reads the
/// generated AppleScript instead, which needs neither and still pins the one property
/// that distinguishes the three restore targets: whether a window is named.
///
///     DIPLOMAT_SPAWN_SCRIPT_TEST=1 swift run Diplomat
///
/// Opens no window and runs no AppleScript. Exit code is pass/fail, so CI can gate on it.
enum SpawnScriptTest {
    static func run() -> Bool {
        var failures: [String] = []
        func check(_ name: String, _ cond: Bool, _ detail: String = "") {
            if cond { print("  ok    \(name)") }
            else { print("  FAIL  \(name) \(detail)"); failures.append(name) }
        }

        func script(_ term: SpawnTerminal, restore: String?) -> String {
            AgentSpawner.appleScript(for: term, shellCommand: "echo hi", restoreFocusTo: restore)
        }

        print("spawn script: who the restore names")

        for term in SpawnTerminal.allCases {
            let capture = term == .iterm
                ? "set _prev to id of current window"
                : "set _prev to id of front window"
            let reselect = term == .iterm
                ? "select (first window whose id is _prev)"
                : "set frontmost of _pw to true"
            let creates = term == .iterm ? "create window with default profile" : "do script \"\""

            // Foreground: the operator asked to land in the new window, so nothing is
            // restored and no window is named.
            let fg = script(term, restore: nil)
            check("\(term.title) foreground activates the terminal", fg.contains("activate"))
            check("\(term.title) foreground restores nothing", !fg.contains("_prev"))

            // Another app: activating it is the whole restore — it owns no window here.
            let cross = script(term, restore: "com.apple.finder")
            check("\(term.title) cross-app activates the target app",
                  cross.contains("tell application id \"com.apple.finder\" to activate"))
            check("\(term.title) cross-app names no window", !cross.contains("_prev"))

            // The terminal itself: activating it lands on the window the spawn just
            // made, so the operator's window has to be named to come back.
            let same = script(term, restore: term.bundleID)
            check("\(term.title) same-app captures the front window", same.contains(capture))
            check("\(term.title) same-app re-selects it", same.contains(reselect))
            // Captured BEFORE the spawn's own window exists — after it, the id is the
            // agent's window and the restore is a no-op that still reads like one.
            let capturedAt = same.range(of: capture)?.lowerBound
            let createdAt = same.range(of: creates)?.lowerBound
            check("\(term.title) same-app captures before it creates",
                  capturedAt != nil && createdAt != nil && capturedAt! < createdAt!)
        }

        print(failures.isEmpty
              ? "\nSPAWN_SCRIPT_TEST OK"
              : "\nSPAWN_SCRIPT_TEST FAILED  [\(failures.joined(separator: ", "))]")
        return failures.isEmpty
    }
}
