import Foundation

/// Self-test for the shared AppleScript runner — `DIPLOMAT_OSA_TEST=1`.
///
/// Every terminal-driving path in the applet goes through `OSAScript`, and its
/// load-bearing property is the easiest one to get wrong: a script that *ran and
/// printed nothing* must stay distinguishable from one that *could not run*.
/// Conflating them is how a revoked Automation permission reads as "no terminal
/// sessions" and the API-error watcher goes quietly blind.
///
/// The scripts here are pure AppleScript expressions — no `tell application` — so
/// they need no Automation permission and run on a CI box with no windows open.
///
///   DIPLOMAT_OSA_TEST=1 swift run Diplomat
///
/// Exit code is pass/fail, so CI can gate on it.
enum OSATest {
    static func run() -> Bool {
        var failures: [String] = []

        func check(_ name: String, _ condition: Bool, _ detail: @autoclosure () -> String = "") {
            if condition {
                print("  ok    \(name)")
            } else {
                let d = detail()
                print("  FAIL  \(name)\(d.isEmpty ? "" : " — \(d)")")
                failures.append(name)
            }
        }

        print("== OSAScript ==")

        // A clean run: stdout comes back, exit status is 0.
        let ok = OSAScript.run("return \"marker-42\"")
        check("run() reports success for a good script", ok?.succeeded == true,
              "status \(ok.map { "\($0.status)" } ?? "nil")")
        check("run() returns stdout", ok?.stdout.contains("marker-42") == true,
              "got \(ok?.stdout ?? "nil")")

        // A script that runs and fails: an Outcome, NOT nil, carrying osascript's
        // own message — that message is what the wizard surfaces to the user.
        let bad = OSAScript.run("error \"boom-marker\"")
        check("run() returns an outcome for a failing script", bad != nil)
        check("run() reports the failure status", bad?.succeeded == false,
              "status \(bad.map { "\($0.status)" } ?? "nil")")
        check("run() captures stderr", bad?.stderr.contains("boom-marker") == true,
              "got \(bad?.stderr ?? "nil")")

        // capture(): the distinction the copies lost.
        check("capture() yields stdout on success",
              OSAScript.capture("return \"marker-42\"")?.contains("marker-42") == true)
        check("capture() yields nil — not \"\" — on failure",
              OSAScript.capture("error \"boom\"") == nil)
        // The heart of it: an empty success is NOT a failure. osascript terminates
        // its output with a newline even for an empty result, so the value is "\n"
        // — what matters is that it is non-nil and carries no content.
        let empty = OSAScript.capture("return \"\"")
        check("capture() yields a non-nil value for a script that printed nothing",
              empty != nil, "an empty success must not be reported as a failure")
        check("...and that value is blank",
              empty?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == true,
              "got \(empty.map { "\($0.debugDescription)" } ?? "nil")")

        // runSilently(): the boolean form.
        check("runSilently() is true for a good script",
              OSAScript.runSilently("return \"x\""))
        check("runSilently() is false for a failing script",
              !OSAScript.runSilently("error \"boom\""))

        // Large output must not deadlock: the pipes are drained BEFORE waiting, so a
        // payload past the buffer size still comes back whole. The size matters — a
        // macOS pipe buffer is 64 KiB, so anything under that fits and would pass
        // even with the drain and the wait in the wrong order. 256 KiB does not.
        let chunk = String(repeating: "0123456789abcdef", count: 64)  // 1 KiB
        let big = OSAScript.capture(
            "set s to \"\"\nrepeat 256 times\n set s to s & \"\(chunk)\"\nend repeat\nreturn s")
        check("capture() survives output larger than the pipe buffer",
              (big?.count ?? 0) >= 256 * 1024, "got \(big?.count ?? -1) chars")

        if failures.isEmpty {
            print("\nOSA TEST OK")
            return true
        }
        print("\nOSA TEST FAILED: \(failures.joined(separator: ", "))")
        return false
    }
}
