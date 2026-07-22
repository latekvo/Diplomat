import Foundation

/// The cross-process settings file: `~/.diplomat/config.json`.
///
/// Almost every setting belongs in this app's UserDefaults, and stays there. The repo
/// root can't: the agent that consumes it is launched by whichever process picks the
/// work up, and one of those is a **mesh node** — a separate, stdlib-only Python
/// process with neither UserDefaults nor Qt (the README documents joining a mesh with
/// "no Qt needed"), which outlives this app and can't be handed a value at spawn time.
///
/// So this one knob lives in the shared `~/.diplomat` tree, alongside the ban list and
/// the mesh snapshot both front-ends already exchange there. Every reader re-reads on
/// use, so a change lands on the next spawn instead of the next process start.
enum AppConfig {
    /// The agents' repo root (Settings → REPO ROOT). Same key on the Linux side.
    static let repoRootKey = "repoRoot"

    /// Overridable so a self-test can point at a scratch file instead of the real one —
    /// same escape hatch as the mesh's `DIPLOMAT_MESH_DIR`.
    static var url: URL {
        if let env = ProcessInfo.processInfo.environment["DIPLOMAT_CONFIG"], !env.isEmpty {
            return URL(fileURLWithPath: (env as NSString).expandingTildeInPath)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".diplomat/config.json")
    }

    /// The whole file, or `[:]` when it's absent, unreadable or corrupt — a truncated
    /// or hand-edited file must degrade to defaults, never break a spawn.
    static func read() -> [String: Any] {
        guard let data = try? Data(contentsOf: url),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else { return [:] }
        return obj
    }

    static func string(_ key: String) -> String { read()[key] as? String ?? "" }

    /// Read-modify-write, atomically (Foundation writes to a temp file and renames), so a
    /// node reading concurrently never sees a torn file. Keys the file already holds
    /// survive a normal write; a file that failed to parse (see `read`) is rewritten from
    /// defaults, so a *corrupt* file loses any other keys — fine while repo root is the
    /// only key. Best-effort: an unwritable HOME must never throw into the UI.
    static func set(_ key: String, _ value: String) {
        var obj = read()
        if value.isEmpty { obj.removeValue(forKey: key) } else { obj[key] = value }
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                 withIntermediateDirectories: true)
        guard let data = try? JSONSerialization.data(withJSONObject: obj,
                                                    options: [.prettyPrinted, .sortedKeys])
        else { return }
        try? data.write(to: url, options: .atomic)
    }
}
