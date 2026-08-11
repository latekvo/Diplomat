import Foundation
import DiplomatCore

/// The cross-process settings file: `~/.diplomat/config.json`.
///
/// Almost every setting belongs in this app's UserDefaults, and stays there. A few
/// can't: the repo root every spawn `cd`s into, the cap on how many automatic agents
/// may run here at once, and the three knobs of the rate-limit budget those agents are
/// started against. Each is consumed by whichever process picks the work up, and one of
/// those is a **mesh node** — a separate, stdlib-only Python process with neither
/// UserDefaults nor Qt (the README documents joining a mesh with "no Qt needed"), which
/// outlives this app and can't be handed a value at spawn time.
///
/// So those knobs live in the shared `~/.diplomat` tree, alongside the ban list and
/// the mesh snapshot both front-ends already exchange there. Every reader re-reads on
/// use, so a change lands on the next spawn instead of the next process start.
enum AppConfig {
    /// The agents' repo root (Settings → REPO ROOT). Same key on the Linux side.
    static let repoRootKey = "repoRoot"
    /// How many automatic agents this machine runs at once. Same key on the Linux side.
    static let autoTaskLimitKey = "autoTaskLimit"
    /// Whether automatic work is held back when the rate-limit windows are too low to
    /// afford it. Same three keys on the Linux side.
    static let autoBudgetGateKey = "autoBudgetGate"
    /// How sure the gate must be that a task fits, as a percentage.
    static let autoBudgetConfidenceKey = "autoBudgetConfidence"
    /// Share of each window to keep in hand while the ledger cannot price a task.
    static let autoBudgetFloorPctKey = "autoBudgetFloorPct"

    /// Overridable so a self-test can point at a scratch file instead of the real one —
    /// same escape hatch as the mesh's `SZPONTNET_DIR`.
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

    /// One integer key, or `fallback` when it's absent or isn't one.
    ///
    /// `NSNumber` bridges JSON booleans and numbers alike, so `as? Int` alone would
    /// read a hand-edited `true` back as the number 1 and quietly become a real cap of
    /// one agent. Checking the bridged type excludes that, matching the Python twin.
    static func int(_ key: String, fallback: Int) -> Int {
        guard let n = read()[key] as? NSNumber,
              CFGetTypeID(n) != CFBooleanGetTypeID() else { return fallback }
        return n.intValue
    }

    /// Read-modify-write, atomically (Foundation writes to a temp file and renames), so a
    /// node reading concurrently never sees a torn file. Keys the file already holds
    /// survive a normal write; a file that failed to parse (see `read`) is rewritten from
    /// defaults, so a *corrupt* file loses the other keys — each then falls back to its
    /// own default, the same degradation an absent file gets. Best-effort: an unwritable
    /// HOME must never throw into the UI.
    static func set(_ key: String, _ value: String) {
        var obj = read()
        if value.isEmpty { obj.removeValue(forKey: key) } else { obj[key] = value }
        write(obj)
    }

    /// `set` for a number, which has no "empty means remove" spelling of its own.
    static func setInt(_ key: String, _ value: Int) {
        var obj = read()
        obj[key] = value
        write(obj)
    }

    private static func write(_ obj: [String: Any]) {
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                 withIntermediateDirectories: true)
        guard let data = try? JSONSerialization.data(withJSONObject: obj,
                                                    options: [.prettyPrinted, .sortedKeys])
        else { return }
        try? data.write(to: url, options: .atomic)
    }

    /// How many automatic agents this device will run at once, clamped to the range
    /// the Settings stepper offers.
    ///
    /// Resolved here rather than at each caller because there are two, in different
    /// processes: this app's dispatch gate and — for work a mesh peer routes in — the
    /// node's, through the Python host. A cap the two disagree on is not a cap.
    static var autoTaskLimit: Int {
        AgentDispatchGate.clampAutoTaskLimit(
            int(autoTaskLimitKey, fallback: AgentDispatchGate.defaultAutoTaskLimit))
    }

    /// One boolean key, or `fallback` when it's absent or isn't one. A number is NOT
    /// accepted: `1` in a hand-edited file is as likely to be a stray count as an
    /// intended "on", and every consumer here has a safe default to fall back to.
    static func bool(_ key: String, fallback: Bool) -> Bool {
        guard let n = read()[key] as? NSNumber,
              CFGetTypeID(n) == CFBooleanGetTypeID() else { return fallback }
        return n.boolValue
    }

    /// One numeric key as a Double, or `fallback` when it's absent, isn't a number, or
    /// is one no arithmetic can use. Booleans are excluded for the reason `int`
    /// excludes them; an integer is accepted, since 20 and 20.0 are the same
    /// percentage and only one of them survives a hand edit.
    static func double(_ key: String, fallback: Double) -> Double {
        guard let n = read()[key] as? NSNumber,
              CFGetTypeID(n) != CFBooleanGetTypeID(),
              n.doubleValue.isFinite else { return fallback }
        return n.doubleValue
    }

    /// `set` for a flag.
    static func setBool(_ key: String, _ value: Bool) {
        var obj = read()
        obj[key] = value
        write(obj)
    }

    /// `set` for a fractional number (the budget floor is a percentage the UI offers
    /// in half-steps).
    static func setDouble(_ key: String, _ value: Double) {
        var obj = read()
        obj[key] = value
        write(obj)
    }

    /// Whether automatic work is held back when the rate-limit windows are too low to
    /// afford it. Lives here, not in UserDefaults, for the reason the task cap does: a
    /// mesh node spends this machine's limit on work this app never sees, and the two
    /// must not disagree about whether that limit is being watched.
    static var autoBudgetGate: Bool { bool(autoBudgetGateKey, fallback: true) }

    /// How sure the gate must be that a task fits before it starts one, as a
    /// percentage, snapped to a level with a quantile behind it.
    static var autoBudgetConfidence: Int {
        AgentDispatchGate.clampBudgetConfidence(
            int(autoBudgetConfidenceKey,
                fallback: AgentDispatchGate.defaultBudgetConfidence))
    }

    /// The share of each rate-limit window to keep in hand while the ledger is too
    /// thin to price a task.
    static var autoBudgetFloorPct: Double {
        AgentDispatchGate.clampBudgetFloorPct(
            double(autoBudgetFloorPctKey,
                   fallback: AgentDispatchGate.defaultBudgetFloorPct))
    }
}
