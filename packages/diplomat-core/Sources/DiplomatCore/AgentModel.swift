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
///   runner picks for itself, out of settings each one writes down and this reads back:
///   Hermes' `~/.hermes/config.yaml` names the model a session passed no `-m` starts
///   on, and OpenCode resolves one the way `openCodeModel` mirrors — the `model` its
///   config names, else the head of its picker's recent list.
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
        detect(configFile: configURL(), claudeHome: claudeHomeURL(), hermesConfig: hermesConfigURL(),
               openCodeConfig: openCodeConfigURL(), openCodeState: openCodeStateURL())
    }

    /// `detected()` against explicit locations, so the smoke test can drive the whole
    /// lookup over a fixture instead of over the developer's own machine.
    public static func detect(configFile: URL, claudeHome: URL, hermesConfig: URL,
                              openCodeConfig: URL, openCodeState: URL) -> String {
        let cfg = readJSONObject(configFile)
        // Same two keys as `AppConfig` (macOS) and the runtime's `appconfig.py` write.
        let runner = AgentRunner.from(cfg["agentRunner"] as? String ?? "")
        let pinned = (cfg["agentModel"] as? String ?? "")
        // The Claude runner ignores that field (`AgentRunner.agentCommand` passes it no
        // model flag), so a pin left over from OpenCode must not be claimed here.
        if runner == .claude { return displayName(claudeCodeModel(home: claudeHome)) }
        guard pinned.isEmpty else { return displayName(pinned) }
        return displayName(foreignRunnerModel(runner, hermesConfig: hermesConfig,
                                              openCodeConfig: openCodeConfig,
                                              openCodeState: openCodeState))
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
    /// rather than a table of models — a table would name the handful that existed when
    /// it was written and go quietly wrong for the rest. An id the rules cannot reach a
    /// name from, because the name is not in the id at all (`x-preview-f-free` is
    /// `Ox Alpha`), is named outright in `assets/models.json`, beside the three
    /// exception lists the rules consult.
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
        // First, so a stated name beats every rule below, `ignore` included.
        if let named = naming?.displayNames?
            .first(where: { $0.key.caseInsensitiveCompare(s) == .orderedSame }) {
            return named.value
        }
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

    /// What an unpinned OpenCode / Hermes spawn starts on, read from the settings each
    /// runner picks its own model out of.
    private static func foreignRunnerModel(_ runner: AgentRunner, hermesConfig: URL,
                                           openCodeConfig: URL, openCodeState: URL) -> String {
        if runner == .opencode {
            return openCodeModel(configDir: openCodeConfig, stateDir: openCodeState)
        }
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

    // MARK: - Asking OpenCode what it runs

    /// OpenCode's global config files, in the order it merges them — later wins.
    private static let openCodeConfigFiles = ["config.json", "opencode.json", "opencode.jsonc"]

    /// What an OpenCode spawn carrying no `-m` starts on, resolved the way OpenCode's
    /// own `Provider.defaultModel` does (read out of the 1.4.3 binary): the `model` its
    /// config names, else the head of the recent list its model picker persists to
    /// `<state>/model.json` — which is both the model the next TUI restores and the one
    /// the last turn actually ran on.
    ///
    /// Narrower than OpenCode's own answer in two places, each costing the tag its model
    /// rather than handing it a wrong one. A `model` set by a config file *inside the
    /// repo* is not read: the spawn's directory is the front-ends' to resolve
    /// (`RepoPaths.agentRepo`, `review.repo_path`), and deriving it here would be a third
    /// copy of that lookup. And OpenCode walks past a recent entry whose provider it can
    /// no longer reach, which takes a provider list this does not build.
    private static func openCodeModel(configDir: URL, stateDir: URL) -> String {
        for name in openCodeConfigFiles.reversed() {
            if let text = try? String(contentsOf: configDir.appendingPathComponent(name),
                                      encoding: .utf8),
               let model = configuredModel(inOpenCodeConfig: text) { return model }
        }
        guard let text = try? String(contentsOf: stateDir.appendingPathComponent("model.json"),
                                     encoding: .utf8) else { return "" }
        return recentModel(inOpenCodeState: text) ?? ""
    }

    /// The `model` one OpenCode config file names, or nil when it names none.
    ///
    /// Every OpenCode config is JSONC whatever its extension — `opencode.json` and
    /// `config.json` go through the same parser `opencode.jsonc` does — so a comment is
    /// valid in all three and `JSONSerialization` takes none of them. Hence the strip.
    ///
    /// A value written as an OpenCode `{env:…}` / `{file:…}` reference is left
    /// unresolved, and `displayName` rejects it for the punctuation: the tag loses its
    /// model, which is the wanted outcome — the alternative is naming the reference.
    public static func configuredModel(inOpenCodeConfig text: String) -> String? {
        guard let data = strippedJSONC(text).data(using: .utf8),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let raw = obj["model"] as? String
        else { return nil }
        let trimmed = raw.trimmingCharacters(in: .whitespaces)
        return trimmed.isEmpty ? nil : trimmed
    }

    /// The head of OpenCode's recent-model list, spelled `provider/model` the way its
    /// config names one, or nil for a list that holds no model.
    ///
    /// An entry missing its `modelID` is walked past rather than read as a model with an
    /// empty name, so a half-written head does not blank a tag the entry behind it can
    /// still fill.
    public static func recentModel(inOpenCodeState text: String) -> String? {
        guard let data = text.data(using: .utf8),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let recent = obj["recent"] as? [[String: Any]]
        else { return nil }
        for entry in recent {
            let provider = (entry["providerID"] as? String ?? "").trimmingCharacters(in: .whitespaces)
            let model = (entry["modelID"] as? String ?? "").trimmingCharacters(in: .whitespaces)
            guard !model.isEmpty else { continue }
            return provider.isEmpty ? model : "\(provider)/\(model)"
        }
        return nil
    }

    /// JSONC as the JSON `JSONSerialization` will take: comments dropped, and only
    /// comments — the trailing comma OpenCode's parser is configured to allow is one
    /// `JSONSerialization` already accepts on both platforms this builds for (measured
    /// against Foundation on macOS 15.5 and on swift:6.0 Linux), so dropping it here
    /// would be code no fixture could hold to account.
    ///
    /// Inside a string literal none of that punctuation is punctuation, which is what
    /// the `inString` arm is for: one provider `baseURL` is enough to cut a config in
    /// half at its `//` and lose the model the whole file was opened for.
    private static func strippedJSONC(_ text: String) -> String {
        let c = Array(text)
        var out = ""
        var i = 0
        var inString = false
        while i < c.count {
            let ch = c[i]
            if inString {
                out.append(ch)
                if ch == "\\", i + 1 < c.count { out.append(c[i + 1]); i += 2; continue }
                if ch == "\"" { inString = false }
                i += 1
                continue
            }
            if ch == "/", i + 1 < c.count, c[i + 1] == "/" {
                while i < c.count, c[i] != "\n" { i += 1 }
                continue
            }
            if ch == "/", i + 1 < c.count, c[i + 1] == "*" {
                i += 2
                while i + 1 < c.count, !(c[i] == "*" && c[i + 1] == "/") { i += 1 }
                i = min(i + 2, c.count)
                continue
            }
            if ch == "\"" { inString = true }
            out.append(ch)
            i += 1
        }
        return out
    }

    // MARK: - The state it reads

    /// The cross-process settings file both front-ends write — `~/.diplomat/config.json`,
    /// relocatable with `DIPLOMAT_CONFIG` exactly as `AppConfig` and `appconfig.py` do.
    ///
    /// Read here rather than passed in because the Linux front-end reaches this code
    /// only through the `diplomat-core` CLI: a value it resolved itself would be a
    /// second implementation of the same lookup, which is the drift the whole
    /// single-sourced prompt builder exists to prevent.
    private static func configURL() -> URL {
        envPath("DIPLOMAT_CONFIG") ?? homePath(".diplomat/config.json")
    }

    /// Claude Code's own state directory, under the same `DIPLOMAT_CLAUDE_DIR` override
    /// the transcript-based token scans on both platforms already honour.
    private static func claudeHomeURL() -> URL {
        envPath("DIPLOMAT_CLAUDE_DIR") ?? homePath(".claude")
    }

    /// Hermes' own config, under `DIPLOMAT_HERMES_CONFIG` — the twin of the
    /// `DIPLOMAT_HERMES_DB` override `hermesstore.py` reads its session store through,
    /// and named for a file for the same reason: one file is all either of them wants.
    private static func hermesConfigURL() -> URL {
        envPath("DIPLOMAT_HERMES_CONFIG") ?? homePath(".hermes/config.yaml")
    }

    /// OpenCode's own config and state directories, under `DIPLOMAT_OPENCODE_CONFIG_DIR`
    /// and `DIPLOMAT_OPENCODE_STATE_DIR` — named for directories the way
    /// `DIPLOMAT_CLAUDE_DIR` is, and two of them because OpenCode keeps its config and
    /// its state under different XDG roots.
    ///
    /// Unset, they resolve the way OpenCode's own `Global.Path` does, `XDG_*` included:
    /// the spawn is a terminal window inheriting this environment, so an operator who
    /// moves those roots moves the files the run will actually be started from.
    private static func openCodeConfigURL() -> URL {
        envPath("DIPLOMAT_OPENCODE_CONFIG_DIR")
            ?? xdgPath("XDG_CONFIG_HOME", under: ".config").appendingPathComponent("opencode")
    }

    private static func openCodeStateURL() -> URL {
        envPath("DIPLOMAT_OPENCODE_STATE_DIR")
            ?? xdgPath("XDG_STATE_HOME", under: ".local/state").appendingPathComponent("opencode")
    }

    /// A path named by `key` in the environment, `~` expanded; nil when unset or empty.
    private static func envPath(_ key: String) -> URL? {
        guard let env = ProcessInfo.processInfo.environment[key], !env.isEmpty else { return nil }
        return URL(fileURLWithPath: (env as NSString).expandingTildeInPath)
    }

    private static func homePath(_ relative: String) -> URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(relative)
    }

    /// One XDG base directory, resolved as OpenCode resolves it: the variable's value
    /// verbatim when it is set, else the spec's default under home. Deliberately without
    /// the tilde expansion `envPath` does — OpenCode applies none, and a path this
    /// reads differently is a directory the run would not be started from.
    private static func xdgPath(_ key: String, under fallback: String) -> URL {
        if let env = ProcessInfo.processInfo.environment[key], !env.isEmpty {
            return URL(fileURLWithPath: env)
        }
        return homePath(fallback)
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
