import Foundation

/// Running AppleScript, in one place.
///
/// The applet drives its terminals entirely through `osascript`: spawning a
/// session, reading back a window id, listing open windows, closing one,
/// dumping visible buffers for the API-error watcher, focusing a tty. Seven call
/// sites each built their own `Process`, and the copies had already diverged on
/// the part that matters — *what a failure looks like*. Some returned `""` for
/// both "the script printed nothing" and "osascript refused to run", which is
/// how a revoked Automation permission stayed invisible: the watcher read an
/// empty dump as "no sessions" forever.
///
/// So the distinction is the API here. `capture` yields `nil` for any failure and
/// a string only for a clean run; `run` hands back the full outcome for the one
/// caller that needs stderr in a typed error; `runSilently` is the boolean form
/// for scripts whose output is irrelevant.
///
/// Every entry point reads stdout *before* `waitUntilExit()`. The other order
/// deadlocks the moment a script's output exceeds the pipe buffer, with the child
/// blocked on a full pipe and the parent blocked waiting for the child.
enum OSAScript {
    private static let executable = "/usr/bin/osascript"

    /// One completed `osascript` invocation. `nil` from the calls below means the
    /// process could not be launched at all.
    struct Outcome {
        let status: Int32
        let stdout: String
        let stderr: String

        var succeeded: Bool { status == 0 }
    }

    /// Run `script` to completion. `nil` only when the process could not be
    /// launched; a script that ran and failed comes back as an `Outcome` with a
    /// non-zero `status` and osascript's own message in `stderr`.
    static func run(_ script: String) -> Outcome? {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: executable)
        proc.arguments = ["-e", script]
        let outPipe = Pipe(), errPipe = Pipe()
        proc.standardOutput = outPipe
        proc.standardError = errPipe
        do { try proc.run() } catch { return nil }
        // Drain both pipes before waiting — see the note above.
        let outData = outPipe.fileHandleForReading.readDataToEndOfFile()
        let errData = errPipe.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        return Outcome(status: proc.terminationStatus,
                       stdout: String(data: outData, encoding: .utf8) ?? "",
                       stderr: String(data: errData, encoding: .utf8) ?? "")
    }

    /// The script's stdout, or `nil` on ANY failure — launch or non-zero exit.
    /// Callers that treat "" as meaningful data need this distinction: an empty
    /// success and a refusal are different answers.
    static func capture(_ script: String) -> String? {
        guard let outcome = run(script), outcome.succeeded else { return nil }
        return outcome.stdout
    }

    /// Whether the script ran and exited 0. For scripts driven for their effect.
    @discardableResult
    static func runSilently(_ script: String) -> Bool {
        run(script)?.succeeded ?? false
    }

    /// Launch and return immediately, without waiting or reading anything.
    ///
    /// Only for provoking the macOS "control <app>" Automation prompt at startup:
    /// the prompt is modal until the user answers, so waiting here would hang the
    /// launch behind a dialog the user may not have noticed yet.
    static func fireAndForget(_ script: String) {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: executable)
        proc.arguments = ["-e", script]
        proc.standardOutput = Pipe()
        proc.standardError = Pipe()
        try? proc.run()
    }
}
