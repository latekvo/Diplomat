import Foundation

/// Everything the "Resolve conflicts" wizard collects, plus the logic that turns
/// it into the prompt handed to a fresh `claude` session. Pure value type — the
/// prompt text comes from `core/conflicts.json`; only the assembly order/conditions
/// live here, shared verbatim with the Linux front-end.
public struct ConflictConfig {
    /// Whose PRs we sweep for merge conflicts: my own, another user's, or one
    /// specific PR. The same axis the Review wizard uses — see `PRTarget`.
    public typealias Target = PRTarget

    public var target: Target
    public var username: String
    /// The authenticated viewer login (from the Store), used as the @handle for "mine".
    public var me: String
    public var specificPR: String

    public init(target: Target = .mine, username: String = "", me: String = "",
                specificPR: String = "") {
        self.target = target
        self.username = username
        self.me = me
        self.specificPR = specificPR
    }

    /// The @handle whose PRs we sweep (empty in single-PR mode).
    public var authorHandle: String {
        switch target {
        case .mine:
            return me.isEmpty ? "me" : me
        case .someone:
            let u = username.trimmingCharacters(in: .whitespaces)
            return u.isEmpty ? "" : u
        case .specific:
            return ""
        }
    }

    public var isSinglePR: Bool { target == .specific }

    /// The configured target repo (owner, repo), from the shared core config.
    public var targetRepo: (owner: String, repo: String) {
        CoreAssets.repoCoordinates()
    }

    /// The single-PR field parsed as a number / URL / `owner/repo#n` shorthand,
    /// checked against the target repo.
    public var prRef: PRRef {
        let (owner, repo) = targetRepo
        return PRRef.parse(specificPR, owner: owner, repo: repo)
    }

    /// SPAWN is only meaningful once we know what to sweep: a usable PR reference in
    /// single-PR mode, or a non-empty author handle otherwise.
    public var isValid: Bool {
        isSinglePR ? prRef.isValid : !authorHandle.isEmpty
    }

    public func buildPrompt() -> String {
        let conflicts = try? CoreAssets.conflicts()
        let scope = conflicts?.scope ?? [:]
        let blocks = conflicts?.blocks ?? [:]
        let (owner, repo) = CoreAssets.repoCoordinates()

        var out: [String] = []

        if isSinglePR {
            out.append((scope["single"] ?? "")
                .replacingOccurrences(of: "{pr}", with: prRef.numberString)
                .replacingOccurrences(of: "{owner}", with: owner)
                .replacingOccurrences(of: "{repo}", with: repo))
        } else {
            let tmpl = target == .mine ? (scope["scopeMine"] ?? "") : (scope["scopeOther"] ?? "")
            let scopeText = tmpl.replacingOccurrences(of: "{handle}", with: authorHandle)
            out.append((scope["multi"] ?? "")
                .replacingOccurrences(of: "{scope}", with: scopeText)
                .replacingOccurrences(of: "{owner}", with: owner)
                .replacingOccurrences(of: "{repo}", with: repo))
        }

        // The merge block reads "Merge …" for one PR, "For each, merge …" for many.
        let lead = isSinglePR ? "Merge" : "For each, merge"
        if let m = blocks["merge"] { out.append(m.replacingOccurrences(of: "{lead}", with: lead)) }
        if let b = blocks["bar"] { out.append(b) }
        if let s = blocks["summary"] { out.append(s) }
        if let t = blocks["trailer"] { out.append(t) }

        return out.joined(separator: "\n\n")
    }
}
