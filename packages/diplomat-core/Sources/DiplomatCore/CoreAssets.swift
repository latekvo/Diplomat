import Foundation

/// Loader for the shared, language-neutral files in this package's `assets/` — the
/// single source of truth shared verbatim with the Linux (Qt6/PySide6) front-end.
/// Nothing here is UI- or platform-specific; it just resolves the `assets/`
/// directory and decodes the JSON / GraphQL files.
public enum CoreAssets {

    public struct CoreError: LocalizedError {
        public let message: String
        public var errorDescription: String? { message }
    }

    // MARK: - Decoded shapes

    public struct Config: Decodable {
        public let owner: String
        public let repo: String
    }

    public struct CatalogFile: Decodable {
        public let tools: [CatalogEntry]
    }

    public struct CatalogEntry: Decodable {
        public let id: String
        public let title: String
        public let subtitle: String
        public let sfSymbol: String
        public let emoji: String
        public let color: String
        public let colorHex: String
    }

    public struct Filters: Decodable {
        public let skillSuffix: String
        public let installerPrefixes: [String]
        public let team: [String]
        public let orgAssociations: [String]
        public let staleReadyDays: Int
        public let approvedDecision: String
        /// Author associations trusted enough for an auto-review verdict; absent in
        /// older files (optional so they still decode) — `VerdictPolicy` falls back.
        public let trustedAssociations: [String]?
    }

    public struct Review: Decodable {
        public struct Depth: Decodable {
            public let id: String
            public let title: String
            public let blurb: String
            public let fragment: String
            // The "fix it on the branch" disposition for this depth. Only emitted
            // when we may actually commit (our own PRs / a specific PR); absent
            // for flag-only depths and never used for someone else's PRs.
            public let onBranch: String?
        }
        public let defaultDepth: String
        public let depths: [Depth]
        public let scope: [String: String]
        public let blocks: [String: String]
        /// The author-conditional sub-blocks used only by the single-PR (Specific
        /// PR) path, where the PR may be mine or someone else's. See `_specificComment`.
        public let specific: [String: String]
    }

    public struct Conflicts: Decodable {
        public let scope: [String: String]
        public let blocks: [String: String]
    }

    public struct Audit: Decodable {
        public let blocks: [String: String]
    }

    /// The exception lists `AgentModel.displayName` consults when it turns a model id
    /// into the name the attribution tag wears. The rules themselves are in
    /// `AgentModel`; these are the parts no rule can derive.
    public struct Models: Decodable {
        /// Ids that name no single model, and so leave the tag with no model at all.
        public let ignore: [String]
        /// Leading id segments that name the vendor rather than the model.
        public let stripPrefixes: [String]
        /// Whole segments that are initialisms, which title-casing would mangle.
        public let acronyms: [String]
    }

    /// The Telemetry screen's model — the lookback ranges, the chart resolutions,
    /// the confidence level, and one entry per figure (title, blurb, glyph, tint).
    /// The arithmetic is `Telemetry`; this is only how it is presented, kept here so
    /// the two screens describe the same number the same way.
    public struct TelemetryModel: Decodable {
        public struct Range: Decodable {
            public let days: Int
            public let title: String
        }
        public struct Series: Decodable {
            public let steps: Int
            public let bins: Int
        }
        public struct Confidence: Decodable {
            public let level: Double
            public let z: Double
            public let title: String
        }
        public struct Metric: Decodable {
            public let id: String
            public let title: String
            public let blurb: String
            public let unit: String
            public let emoji: String
            public let linuxGlyph: String
            public let sfSymbol: String
            public let colorHex: String
        }
        public let ledgerFile: String
        public let cursorFile: String
        public let sampleIntervalSecs: Double
        public let retainDays: Double
        public let maxLedgerBytes: Int
        public let ranges: [Range]
        public let defaultRangeDays: Int
        public let series: Series
        public let confidence: Confidence
        public let minSample: Int
        public let metrics: [Metric]

        public func metric(_ id: String) -> Metric? { metrics.first { $0.id == id } }
    }

    // MARK: - Directory resolution

    /// Candidate locations for `assets/`, in priority order: an explicit override,
    /// the app bundle's Resources (the packaged `.app`), the working directory —
    /// both the package root and a checkout root — and last the package layout
    /// relative to this source file.
    ///
    /// The two working-directory candidates are what a *relocated* binary has:
    /// `diplomat-core` is built to be copied off the machine that compiled it (CI
    /// builds it once, statically, and hands it to another job), and for such a
    /// copy `#filePath` names a build tree that no longer exists. Without a cwd
    /// candidate matching how it is actually invoked, it finds nothing and every
    /// loader quietly falls back to its defaults — which is a wrong answer, not an
    /// error. Twin of `core._candidate_dirs` on the Python side.
    private static func candidateDirs() -> [URL] {
        var dirs: [URL] = []
        if let env = ProcessInfo.processInfo.environment["DIPLOMAT_CORE"] {
            dirs.append(URL(fileURLWithPath: env))
        }
        if let res = Bundle.main.resourceURL {
            dirs.append(res.appendingPathComponent("assets"))
        }
        let cwd = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        dirs.append(cwd.appendingPathComponent("assets"))
        dirs.append(cwd.appendingPathComponent("packages/diplomat-core/assets"))
        // diplomat-core/Sources/DiplomatCore/CoreAssets.swift -> the package root is three levels up.
        let here = URL(fileURLWithPath: #filePath)
        dirs.append(here.deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("assets"))
        return dirs
    }

    private static let resolvedDir: URL? = {
        let fm = FileManager.default
        for dir in candidateDirs() {
            if fm.fileExists(atPath: dir.appendingPathComponent("catalog.json").path) {
                return dir
            }
        }
        return nil
    }()

    public static func assetsDir() throws -> URL {
        guard let dir = resolvedDir else {
            let tried = candidateDirs().map { $0.path }.joined(separator: ", ")
            throw CoreError(message: "could not locate the shared assets/ directory (tried: \(tried))")
        }
        return dir
    }

    // MARK: - Loaders (decoded once, cached)

    private static func loadJSON<T: Decodable>(_ name: String, as type: T.Type) throws -> T {
        let url = try assetsDir().appendingPathComponent(name)
        do {
            let data = try Data(contentsOf: url)
            return try JSONDecoder().decode(T.self, from: data)
        } catch let e as CoreError {
            throw e
        } catch {
            throw CoreError(message: "failed to read \(url.path): \(error)")
        }
    }

    private static let _config = try? loadJSON("config.json", as: Config.self)
    private static let _mesh = try? loadJSON("mesh.json", as: MeshCatalog.self)
    private static let _catalog = try? loadJSON("catalog.json", as: CatalogFile.self)
    private static let _filters = try? loadJSON("filters.json", as: Filters.self)
    private static let _review = try? loadJSON("review.json", as: Review.self)
    private static let _conflicts = try? loadJSON("conflicts.json", as: Conflicts.self)
    private static let _audit = try? loadJSON("audit.json", as: Audit.self)
    private static let _models = try? loadJSON("models.json", as: Models.self)
    private static let _telemetry = try? loadJSON("telemetry.json", as: TelemetryModel.self)

    public static func config() throws -> Config {
        guard let c = _config else { return try loadJSON("config.json", as: Config.self) }
        return c
    }

    /// Repo coordinates from `config.json`, with the ONE hardcoded fallback the whole
    /// codebase shares. Every consumer must come through here — six call sites used to
    /// each carry their own copy of the fallback pair, so a retarget could half-apply.
    public static func repoCoordinates() -> (owner: String, repo: String) {
        let cfg = try? config()
        return (cfg?.owner ?? "software-mansion", cfg?.repo ?? "argent")
    }

    public static func catalog() throws -> [CatalogEntry] {
        guard let c = _catalog else { return try loadJSON("catalog.json", as: CatalogFile.self).tools }
        return c.tools
    }

    /// The Diplomat Mesh model (duty catalog, placement strategies, tier/token vocabulary)
    /// from `mesh.json`, shared verbatim with the Python mesh node + Linux front-end.
    public static func mesh() throws -> MeshCatalog {
        guard let m = _mesh else { return try loadJSON("mesh.json", as: MeshCatalog.self) }
        return m
    }

    public static func filters() throws -> Filters {
        guard let f = _filters else { return try loadJSON("filters.json", as: Filters.self) }
        return f
    }

    public static func review() throws -> Review {
        guard let r = _review else { return try loadJSON("review.json", as: Review.self) }
        return r
    }

    public static func conflicts() throws -> Conflicts {
        guard let c = _conflicts else { return try loadJSON("conflicts.json", as: Conflicts.self) }
        return c
    }

    public static func audit() throws -> Audit {
        guard let a = _audit else { return try loadJSON("audit.json", as: Audit.self) }
        return a
    }

    /// The model-naming exception lists from `models.json`, used to spell the model in
    /// the attribution tag every posted comment carries.
    public static func models() throws -> Models {
        guard let m = _models else { return try loadJSON("models.json", as: Models.self) }
        return m
    }

    /// The Telemetry screen's model from `telemetry.json`, shared verbatim with the
    /// Linux front-end.
    public static func telemetry() throws -> TelemetryModel {
        guard let t = _telemetry else { return try loadJSON("telemetry.json", as: TelemetryModel.self) }
        return t
    }

    public static func graphql(_ name: String) throws -> String {
        let url = try assetsDir().appendingPathComponent("graphql").appendingPathComponent("\(name).graphql")
        do {
            return try String(contentsOf: url, encoding: .utf8)
        } catch {
            throw CoreError(message: "failed to read \(url.path): \(error)")
        }
    }
}
