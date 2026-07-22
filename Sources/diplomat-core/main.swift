import DiplomatCore
import Foundation

// diplomat-core: a thin CLI over DiplomatCore so the Linux (Qt6/PySide6) front-end
// can shell out for prompt assembly instead of re-implementing it in Python. This
// keeps the Review/Conflicts/Audit prompts single-sourced in Swift (the canonical
// implementation), eliminating the drift where the Linux side emitted stale prompts.
//
// Usage:
//   diplomat-core build-prompt      < config.json    # prints the assembled prompt
//
// The JSON config's "kind" field ("review" | "conflicts" | "audit") selects the
// builder; remaining fields mirror the Swift *Config structs (defaults applied when
// a field is absent). Core assets are resolved via $DIPLOMAT_CORE (or the usual
// relative fallbacks) — the caller should point it at the repo's core/ directory.

func die(_ msg: String, _ code: Int32) -> Never {
    FileHandle.standardError.write(Data(("diplomat-core: " + msg + "\n").utf8))
    exit(code)
}

func prTarget(_ s: String?) -> PRTarget {
    switch (s ?? "mine").lowercased() {
    case "someone": return .someone
    case "specific": return .specific
    default: return .mine
    }
}

func specificAuthor(_ s: String?) -> SpecificAuthor {
    switch (s ?? "unknown").lowercased() {
    case "mine": return .mine
    case "theirs": return .theirs
    default: return .unknown
    }
}

let args = CommandLine.arguments
guard args.count >= 2, args[1] == "build-prompt" else {
    die("usage: diplomat-core build-prompt  (JSON config on stdin)", 1)
}

let input = FileHandle.standardInput.readDataToEndOfFile()
guard let obj = (try? JSONSerialization.jsonObject(with: input)) as? [String: Any] else {
    die("invalid JSON on stdin", 1)
}

func str(_ key: String, _ def: String = "") -> String { obj[key] as? String ?? def }
func flag(_ key: String, _ def: Bool) -> Bool { obj[key] as? Bool ?? def }

let kind = str("kind")
let prompt: String
switch kind {
case "review":
    let cfg = ReviewConfig(
        depth: str("depth"),
        target: prTarget(obj["target"] as? String),
        username: str("username"),
        me: str("me"),
        markReady: flag("markReady", true),
        leaveReviews: flag("leaveReviews", true),
        replyToReviews: flag("replyToReviews", true),
        includeDrafts: flag("includeDrafts", true),
        includeReady: flag("includeReady", true),
        specificPR: str("specificPR"),
        finalPass: flag("finalPass", false),
        softApprove: flag("softApprove", true),
        specificAuthor: specificAuthor(obj["specificAuthor"] as? String)
    )
    prompt = cfg.buildPrompt()
case "conflicts":
    let cfg = ConflictConfig(
        target: prTarget(obj["target"] as? String),
        username: str("username"),
        me: str("me"),
        specificPR: str("specificPR")
    )
    prompt = cfg.buildPrompt()
case "audit":
    let cfg = AuditConfig(fixIssues: flag("fixIssues", false), openPRs: flag("openPRs", false))
    prompt = cfg.buildPrompt()
default:
    die("unknown kind \"\(kind)\" (expected review | conflicts | audit)", 1)
}

FileHandle.standardOutput.write(Data(prompt.utf8))
