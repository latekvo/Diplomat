import Foundation

/// The settings that make a run report its own turn boundaries — the Swift twin of
/// `diplomat_runtime.completion`.
///
/// That module carries the full argument for why this exists; the short of it is that
/// an agent is spawned INTERACTIVELY, so finishing its work is not exiting. The exit
/// sentinel never fires, the pid probe sees the same live process either side of the
/// turn, and the screen scrape reads a string off someone else's status bar. A hook is
/// the CLI reporting the transition itself, at the instant it performs it.
///
/// Both front-ends write the same file in the same format because both read it: a run
/// this applet spawned can be resolved by the Linux one after a hand-over, and the
/// parity suite diffs the resolver over it either way.
public enum AgentCompletion {
    /// Hook event → the verb it records.
    public static let events: [(event: String, verb: AgentState.TurnReport.Verb)] = [
        ("UserPromptSubmit", .busy),
        ("Stop", .idle),
        ("SessionEnd", .ended),
    ]

    /// The `--settings` payload, as the bytes that go in the file.
    ///
    /// `donePath` is the mesh's reader of this same report: a szpontnet executor holds
    /// its claim on a work key until the agent's exit-code sentinel appears, and that
    /// fires on EXIT — so a peer's claim outlived the finished work for as long as its
    /// window stayed open. Writing it on the terminal verbs releases the key when the
    /// turn ends.
    public static func settingsJSON(activityPath: String,
                                    donePath: String? = nil) -> String? {
        var hooks: [String: Any] = [:]
        for (event, verb) in events {
            hooks[event] = [["hooks": [["type": "command",
                                        "command": append(verb, activityPath, donePath)]]]]
        }
        guard let data = try? JSONSerialization.data(withJSONObject: ["hooks": hooks])
        else { return nil }
        return String(decoding: data, as: UTF8.self)
    }

    private static func append(_ verb: AgentState.TurnReport.Verb,
                               _ activityPath: String, _ donePath: String?) -> String {
        // Append is the whole concurrency story: each line is a single small O_APPEND
        // write, so two hooks firing together interleave as whole lines rather than
        // tearing one, and a reader never sees a half-written state.
        var cmd = "printf '%s %s\\n' \(verb.rawValue) \"$(date +%s)\" >> \(shq(activityPath))"
        if let done = donePath, verb != .busy {
            // `>` not `>>`: the sentinel is read by existence and dated by mtime, and a
            // second turn's line would only move that date later.
            cmd += "; printf 0 > \(shq(done))"
        }
        return cmd
    }

    /// The run's current state and when it was reached, from the activity file's LAST
    /// recognised line — or `nil` when it says nothing yet.
    ///
    /// Last wins because the file is a log of transitions, not a set of flags: a run
    /// that finished, was nudged and finished again is idle. Unparseable lines are
    /// skipped rather than ending the scan, so a torn final line from a hook killed
    /// mid-write cannot hide the good state under it.
    public static func parse(_ text: String?) -> AgentState.TurnReport? {
        guard let text, !text.isEmpty else { return nil }
        for line in text.split(separator: "\n", omittingEmptySubsequences: true).reversed() {
            let parts = line.split(separator: " ").map(String.init)
            guard parts.count == 2,
                  let verb = AgentState.TurnReport.Verb(rawValue: parts[0]),
                  let at = Double(parts[1]) else { continue }
            return AgentState.TurnReport(verb: verb, at: at)
        }
        return nil
    }

    private static func shq(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }
}
