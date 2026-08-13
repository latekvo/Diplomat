import Foundation

/// Which model the agent a spawn from this machine would start runs on, and how that
/// model is named in the Diplomat attribution tag.
///
/// Every comment, review and reply Diplomat posts opens with that tag, and naming the
/// model in it is what lets a reader tell an Opus 5 review from a Kimi K3 one without
/// asking. A pin in Settings is the only thing that tells Diplomat outright; without one
/// it goes and looks for what the runner will pick for itself:
///
/// * **OpenCode / Hermes** are pinned to a model in `~/.diplomat/config.json` (Settings,
///   beside the runner), and the spawn passes exactly that to the CLI. Blank means the
///   runner uses the model its own picker remembers — a choice Diplomat deliberately
///   does not second-guess, but one it can still *name* where the runner writes that
///   choice down: Hermes keeps it in `~/.hermes/config.yaml`, and a spawn passed no
///   `-m` starts on exactly that. OpenCode's equivalent is not read (see
///   `foreignRunnerModel`), so an unpinned OpenCode still names nothing.
/// * **Claude Code** takes no model from Diplomat at all: it is started through the
///   user's own `claude` alias, and picks its model from its own settings, a `--model`
///   in that alias, or an in-session `/model`. The only source that accounts for all
///   three is what it *actually ran*, which it writes into every transcript — so that
///   is what is read, newest first, falling back to what its settings ask for on a
///   machine that has no transcripts yet.
///
/// The answer is the **dispatching** machine's, which is the rule the rest of the
/// prompt already follows (repo coordinates, the @handle, every toggle): a job the
/// mesh routes to a peer is assembled here and shipped as finished text, so it names
/// the model of the machine that assembled it rather than the one that runs it.
///
/// Resolved per prompt build, never cached: a wizard's config is rebuilt on every
/// redraw, and a spawn is the one moment where the extra directory listing is both
/// affordable and worth being current for.
public enum AgentModel {

    // MARK: - The name that goes in the tag

    /// The model named in the tag for a spawn started here, or "" when nothing on this
    /// machine says which one that is.
    public static func detected() -> String {
        detect(configFile: configURL(), claudeHome: claudeHomeURL(), hermesConfig: hermesConfigURL())
    }

    /// `detected()` against explicit locations, so the smoke test can drive the whole
    /// lookup over a fixture instead of over the developer's own machine.
    public static func detect(configFile: URL, claudeHome: URL, hermesConfig: URL) -> String {
        let cfg = readJSONObject(configFile)
        // Same two keys as `AppConfig` (macOS) and the runtime's `appconfig.py` write.
        let runner = AgentRunner.from(cfg["agentRunner"] as? String ?? "")
        let pinned = (cfg["agentModel"] as? String ?? "")
        // The Claude runner ignores that field (`AgentRunner.agentCommand` passes it no
        // model flag), so a pin left over from OpenCode must not be claimed here.
        if runner == .claude { return displayName(claudeCodeModel(home: claudeHome)) }
        guard pinned.isEmpty else { return displayName(pinned) }
        return displayName(foreignRunnerModel(runner, hermesConfig: hermesConfig))
    }

    /// The tag block with `{model}` filled in: `, Opus 5` when a model is known and
    /// nothing at all when it isn't, so an undetected model leaves the tag reading
    /// exactly as it always has.
    public static func fillTag(_ block: String, model: String) -> String {
        block.replacingOccurrences(of: "{model}", with: model.isEmpty ? "" : ", \(model)")
    }

    // MARK: - Model id -> display name

    /// A raw model id as the tag spells it: `claude-opus-5` → `Opus 5`,
    /// `openrouter/moonshotai/kimi-k3` → `Kimi K3`, `qwen/qwen-3.8-max` → `Qwen 3.8 Max`.
    ///
    /// Ids are one per provider and none of them is a display name, so this is rules
    /// rather than a lookup table — a table would name the handful of models that
    /// existed when it was written and go quietly wrong for the rest. The three lists
    /// the rules consult live in `assets/models.json`.
    ///
    /// Returns "" for anything that names no single model, which the tag renders as no
    /// model at all: an empty id, one of the aliases that stands for a *policy* rather
    /// than a model (`default`, `opusplan`), or a sentinel like Claude Code's
    /// `<synthetic>`, caught by shape so its successors are caught too.
    public static func displayName(_ raw: String) -> String {
        let naming = try? CoreAssets.models()
        var s = raw.trimmingCharacters(in: .whitespaces)
        // `openrouter/moonshotai/kimi-k3` — the provider path is routing, not a name.
        if let slash = s.lastIndex(of: "/") { s = String(s[s.index(after: slash)...]) }
        // OpenRouter variant suffixes (`:free`, `:thinking`) and Claude Code's
        // context-window suffix (`opus[1m]`) qualify one model rather than naming another.
        if let colon = s.firstIndex(of: ":") { s = String(s[s.startIndex..<colon]) }
        if let bracket = s.firstIndex(of: "["), s.hasSuffix("]") { s = String(s[s.startIndex..<bracket]) }
        s = s.trimmingCharacters(in: .whitespaces)
        guard !s.isEmpty else { return "" }
        let ignore = naming?.ignore ?? []
        if ignore.contains(where: { $0.caseInsensitiveCompare(s) == .orderedSame }) { return "" }
        guard s.allSatisfy(isIDCharacter) else { return "" }

        var words = s.split(whereSeparator: { $0 == "-" || $0 == "_" || $0 == " " }).map(String.init)
        // A release stamp (`claude-haiku-4-5-20251001`) dates the model, it doesn't name it.
        if let last = words.last, last.count == 8, last.allSatisfy(\.isNumber) { words.removeLast() }
        // `claude-opus-5` is Anthropic's id for the model everyone calls Opus 5.
        let strip = naming?.stripPrefixes ?? []
        while let first = words.first,
              strip.contains(where: { $0.caseInsensitiveCompare(first) == .orderedSame }),
              words.count > 1 {
            words.removeFirst()
        }
        // `opus-4-5` is Opus 4.5 — a version split across two id segments, not two words.
        words = joinVersionRuns(words)
        let acronyms = naming?.acronyms ?? []
        return words.map { word in
            acronyms.contains(where: { $0.caseInsensitiveCompare(word) == .orderedSame })
                ? word.uppercased()
                : word.prefix(1).uppercased() + word.dropFirst()
        }.joined(separator: " ")
    }

    /// What an id may be made of. Anything else is a sentinel rather than a model —
    /// Claude Code writes `<synthetic>` into transcripts for turns no model produced.
    private static func isIDCharacter(_ c: Character) -> Bool {
        c.isLetter || c.isNumber || c == "." || c == "-" || c == "_" || c == " "
    }

    /// Collapse each run of two or more all-digit words into one dotted version.
    private static func joinVersionRuns(_ words: [String]) -> [String] {
        var out: [String] = []
        var run: [String] = []
        func flush() {
            if run.count > 1 { out.append(run.joined(separator: ".")) } else { out.append(contentsOf: run) }
            run = []
        }
        for word in words {
            if !word.isEmpty && word.allSatisfy(\.isNumber) { run.append(word) } else { flush(); out.append(word) }
        }
        flush()
        return out
    }

    // MARK: - Asking Claude Code what it runs

    /// What Claude Code would run here: what it last actually ran, else what its
    /// settings ask for.
    private static func claudeCodeModel(home: URL) -> String {
        if let ran = lastRunModel(projects: home.appendingPathComponent("projects")) { return ran }
        return readJSONObject(home.appendingPathComponent("settings.json"))["model"] as? String ?? ""
    }

    /// How many transcripts back to look before giving up. A machine mid-session has its
    /// answer in the first one; the allowance is for the newest few holding no model to
    /// read — a session touched before its first turn was written, or one whose tail is
    /// all tool results and synthetic turns.
    private static let transcriptsScanned = 5

    /// How much of a transcript's tail is read. Every turn carries the model, so the
    /// last one is always within a few KB of the end — and a transcript can be
    /// hundreds of MB, which is why the whole file is never touched.
    private static let transcriptTailBytes = 64 * 1024

    /// The model of the most recent turn Claude Code recorded on this machine.
    ///
    /// Newest transcript first, by modification time — `~/.claude/projects/<munged
    /// cwd>/<session>.jsonl`, one directory per working directory. Deliberately not
    /// restricted to the repo Diplomat spawns into: a machine whose last Claude Code
    /// run was anywhere is a machine whose `claude` starts on that model.
    private static func lastRunModel(projects: URL) -> String? {
        let fm = FileManager.default
        guard let walk = fm.enumerator(at: projects,
                                       includingPropertiesForKeys: [.contentModificationDateKey],
                                       options: [.skipsHiddenFiles]) else { return nil }
        var transcripts: [(URL, Date)] = []
        for case let url as URL in walk where url.pathExtension == "jsonl" {
            let stamp = (try? url.resourceValues(forKeys: [.contentModificationDateKey]))?
                .contentModificationDate ?? .distantPast
            transcripts.append((url, stamp))
        }
        for (url, _) in transcripts.sorted(by: { $0.1 > $1.1 }).prefix(transcriptsScanned) {
            if let model = lastModelField(inTailOf: url) { return model }
        }
        return nil
    }

    /// The last `"model"` value in a transcript's tail, ignoring the ones that name no
    /// model (`<synthetic>`, which `displayName` rejects — checked here too so a
    /// synthetic *last* turn falls through to the real one before it).
    private static func lastModelField(inTailOf url: URL) -> String? {
        guard let fh = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? fh.close() }
        let end = (try? fh.seekToEnd()) ?? 0
        let from = end > UInt64(transcriptTailBytes) ? end - UInt64(transcriptTailBytes) : 0
        guard (try? fh.seek(toOffset: from)) != nil, let data = try? fh.readToEnd() else { return nil }
        // Lossy on purpose: a fixed-size window starts mid-character about as often as not,
        // and one mangled character at the front costs nothing that is being looked for.
        return lastModelField(in: String(decoding: data, as: UTF8.self))
    }

    /// The scan itself, over text — the half worth testing without a file.
    public static func lastModelField(in text: String) -> String? {
        var found: String?
        var cursor = text.startIndex
        while let key = text.range(of: "\"model\"", range: cursor..<text.endIndex) {
            cursor = key.upperBound
            // `"model": "x"` and `"model":"x"` are the same record; Claude Code writes
            // the compact one, and a reader that only accepts that spelling is one
            // pretty-printer away from silently finding nothing.
            var i = cursor
            while i < text.endIndex, text[i] == " " || text[i] == ":" { i = text.index(after: i) }
            guard i < text.endIndex, text[i] == "\"" else { continue }
            let open = text.index(after: i)
            guard let close = text.range(of: "\"", range: open..<text.endIndex) else { break }
            let value = String(text[open..<close.lowerBound])
            if !displayName(value).isEmpty { found = value }
            cursor = close.upperBound
        }
        return found
    }

    // MARK: - Asking a foreign runner what it runs

    /// What an unpinned OpenCode / Hermes spawn starts on, read from the runner's own
    /// picker state.
    ///
    /// Only Hermes is asked: OpenCode's selection lives in its own store, whose layout
    /// this does not read — and a guess at it would put a model in the tag that never
    /// ran, which is worse than the empty tag it replaces.
    private static func foreignRunnerModel(_ runner: AgentRunner, hermesConfig: URL) -> String {
        guard runner == .hermes,
              let text = try? String(contentsOf: hermesConfig, encoding: .utf8)
        else { return "" }
        return defaultModel(inHermesConfig: text) ?? ""
    }

    /// The `default:` under the top-level `model:` mapping of a Hermes config:
    ///
    ///     model:
    ///       default: moonshotai/kimi-k3
    ///       provider: openrouter
    ///
    /// That key is what `hermes chat` starts a session on when it is passed no `-m`,
    /// which is every spawn Diplomat makes with the pin left blank.
    ///
    /// A scan rather than a YAML parse: one scalar is wanted out of a file whose every
    /// other key is Hermes' own business, and this library is linked into a static
    /// binary that takes no dependencies it can avoid. Only a *direct* child of
    /// `model:` counts, so the `default_model:` each entry under `providers:` carries
    /// is never mistaken for the model Hermes actually starts on.
    public static func defaultModel(inHermesConfig text: String) -> String? {
        var inModel = false
        var childIndent: Int?
        // By `isNewline`, not by "\n": Swift reads a CRLF pair as one Character, so a
        // split on "\n" matches nothing in a CRLF file and returns it as one line.
        for line in text.split(whereSeparator: \.isNewline) {
            let indent = line.prefix(while: { $0 == " " }).count
            let body = line.dropFirst(indent)
            if body.isEmpty || body.hasPrefix("#") { continue }
            if indent == 0 {
                inModel = body.hasPrefix("model:")
                childIndent = nil
                continue
            }
            guard inModel else { continue }
            if childIndent == nil { childIndent = indent }
            guard indent == childIndent, body.hasPrefix("default:") else { continue }
            return scalar(String(body.dropFirst("default:".count)))
        }
        return nil
    }

    /// One YAML scalar as written after its key, or nil when it is written as nothing.
    private static func scalar(_ raw: String) -> String? {
        var s = raw.trimmingCharacters(in: .whitespaces)
        if let quote = s.first, quote == "\"" || quote == "'" {
            guard let close = s.dropFirst().firstIndex(of: quote) else { return nil }
            s = String(s[s.index(after: s.startIndex)..<close])
        } else if let comment = s.range(of: " #") {
            // Unquoted, `#` opens a comment only after a space, so this cannot eat an id.
            s = String(s[s.startIndex..<comment.lowerBound])
        }
        s = s.trimmingCharacters(in: .whitespaces)
        return s.isEmpty ? nil : s
    }

    // MARK: - The three files it reads

    /// The cross-process settings file both front-ends write — `~/.diplomat/config.json`,
    /// relocatable with `DIPLOMAT_CONFIG` exactly as `AppConfig` and `appconfig.py` do.
    ///
    /// Read here rather than passed in because the Linux front-end reaches this code
    /// only through the `diplomat-core` CLI: a value it resolved itself would be a
    /// second implementation of the same lookup, which is the drift the whole
    /// single-sourced prompt builder exists to prevent.
    private static func configURL() -> URL {
        if let env = ProcessInfo.processInfo.environment["DIPLOMAT_CONFIG"], !env.isEmpty {
            return URL(fileURLWithPath: (env as NSString).expandingTildeInPath)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".diplomat/config.json")
    }

    /// Claude Code's own state directory, under the same `DIPLOMAT_CLAUDE_DIR` override
    /// the transcript-based token scans on both platforms already honour.
    private static func claudeHomeURL() -> URL {
        if let env = ProcessInfo.processInfo.environment["DIPLOMAT_CLAUDE_DIR"], !env.isEmpty {
            return URL(fileURLWithPath: (env as NSString).expandingTildeInPath)
        }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".claude")
    }

    /// Hermes' own config, under `DIPLOMAT_HERMES_CONFIG` — the twin of the
    /// `DIPLOMAT_HERMES_DB` override `hermesstore.py` reads its session store through,
    /// and named for a file for the same reason: one file is all either of them wants.
    private static func hermesConfigURL() -> URL {
        if let env = ProcessInfo.processInfo.environment["DIPLOMAT_HERMES_CONFIG"], !env.isEmpty {
            return URL(fileURLWithPath: (env as NSString).expandingTildeInPath)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".hermes/config.yaml")
    }

    /// One JSON object off disk, or `[:]` for anything that isn't one — an absent,
    /// truncated or hand-edited file must cost the tag its model, never the prompt.
    private static func readJSONObject(_ url: URL) -> [String: Any] {
        guard let data = try? Data(contentsOf: url),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else { return [:] }
        return obj
    }
}
