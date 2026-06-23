import Foundation

/// Loader for the shared, language-neutral `core/` assets — the single source of
/// truth shared verbatim with the Linux (Qt6/PySide6) front-end. Nothing here is
/// UI- or platform-specific; it just resolves the `core/` directory and decodes
/// the JSON / GraphQL files.
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
    }

    public struct Review: Decodable {
        public struct Depth: Decodable {
            public let id: String
            public let title: String
            public let blurb: String
            public let fragment: String
        }
        public let defaultDepth: String
        public let depths: [Depth]
        public let scope: [String: String]
        public let blocks: [String: String]
    }

    // MARK: - Directory resolution

    /// Candidate locations for `core/`, in priority order: an explicit override,
    /// the app bundle's Resources (the packaged `.app`), the current directory
    /// (`swift run`), and the repo layout relative to this source file.
    private static func candidateDirs() -> [URL] {
        var dirs: [URL] = []
        if let env = ProcessInfo.processInfo.environment["ARGENT_UTILS_CORE"] {
            dirs.append(URL(fileURLWithPath: env))
        }
        if let res = Bundle.main.resourceURL {
            dirs.append(res.appendingPathComponent("core"))
        }
        dirs.append(URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
            .appendingPathComponent("core"))
        // Sources/ArgentUtilsCore/CoreAssets.swift -> repo root is three levels up.
        let here = URL(fileURLWithPath: #filePath)
        dirs.append(here.deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("core"))
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

    public static func coreDir() throws -> URL {
        guard let dir = resolvedDir else {
            let tried = candidateDirs().map { $0.path }.joined(separator: ", ")
            throw CoreError(message: "could not locate shared core/ assets (tried: \(tried))")
        }
        return dir
    }

    // MARK: - Loaders (decoded once, cached)

    private static func loadJSON<T: Decodable>(_ name: String, as type: T.Type) throws -> T {
        let url = try coreDir().appendingPathComponent(name)
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
    private static let _catalog = try? loadJSON("catalog.json", as: CatalogFile.self)
    private static let _filters = try? loadJSON("filters.json", as: Filters.self)
    private static let _review = try? loadJSON("review.json", as: Review.self)

    public static func config() throws -> Config {
        guard let c = _config else { return try loadJSON("config.json", as: Config.self) }
        return c
    }

    public static func catalog() throws -> [CatalogEntry] {
        guard let c = _catalog else { return try loadJSON("catalog.json", as: CatalogFile.self).tools }
        return c.tools
    }

    public static func filters() throws -> Filters {
        guard let f = _filters else { return try loadJSON("filters.json", as: Filters.self) }
        return f
    }

    public static func review() throws -> Review {
        guard let r = _review else { return try loadJSON("review.json", as: Review.self) }
        return r
    }

    public static func graphql(_ name: String) throws -> String {
        let url = try coreDir().appendingPathComponent("graphql").appendingPathComponent("\(name).graphql")
        do {
            return try String(contentsOf: url, encoding: .utf8)
        } catch {
            throw CoreError(message: "failed to read \(url.path): \(error)")
        }
    }
}
