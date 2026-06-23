import Foundation

/// Errors surfaced by the `gh` shell-out layer. Flaky-by-design: we just bubble
/// the real failure up to the UI instead of trying to be clever.
public enum GHError: LocalizedError {
    case ghNotFound
    case process(code: Int32, stderr: String)
    case graphql(messages: [String])

    public var errorDescription: String? {
        switch self {
        case .ghNotFound:
            return "`gh` CLI not found. Install GitHub CLI and run `gh auth login`."
        case .process(let code, let stderr):
            let s = stderr.trimmingCharacters(in: .whitespacesAndNewlines)
            return "gh exited \(code): \(s.isEmpty ? "(no stderr)" : s)"
        case .graphql(let messages):
            return "GraphQL: \(messages.joined(separator: "; "))"
        }
    }
}

/// Thin wrapper around the `gh` CLI. We run the binary directly (args passed
/// literally, so no shell-quoting headaches) and rely on `gh`'s own auth/config.
/// The GraphQL queries are loaded from the shared `core/graphql` assets and the
/// repo coordinates supplied as `$owner`/`$name` variables, so the query text
/// itself stays repo-agnostic.
public enum GH {
    private static var cachedPath: String?

    private static func ghPath() throws -> String {
        if let p = cachedPath { return p }
        let candidates = ["/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh"]
        for c in candidates where FileManager.default.isExecutableFile(atPath: c) {
            cachedPath = c
            return c
        }
        if let found = loginShellWhichGH() {
            cachedPath = found
            return found
        }
        throw GHError.ghNotFound
    }

    /// Last resort: ask a login shell where gh lives (covers exotic installs).
    private static func loginShellWhichGH() -> String? {
        let shell = ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/sh"
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: shell)
        proc.arguments = ["-lc", "command -v gh"]
        let out = Pipe()
        proc.standardOutput = out
        proc.standardError = Pipe()
        do { try proc.run() } catch { return nil }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        let path = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return FileManager.default.isExecutableFile(atPath: path) ? path : nil
    }

    /// Run `gh` with the given argv. stdout/stderr are redirected to temp files so
    /// large payloads can't deadlock a pipe buffer (and no cross-thread captures).
    public static func run(_ args: [String]) async throws -> Data {
        let path = try ghPath()
        let tmp = FileManager.default.temporaryDirectory
        let outURL = tmp.appendingPathComponent("argent-utils-\(UUID().uuidString).out")
        let errURL = tmp.appendingPathComponent("argent-utils-\(UUID().uuidString).err")
        FileManager.default.createFile(atPath: outURL.path, contents: nil)
        FileManager.default.createFile(atPath: errURL.path, contents: nil)
        let outHandle = try FileHandle(forWritingTo: outURL)
        let errHandle = try FileHandle(forWritingTo: errURL)
        defer {
            try? outHandle.close()
            try? errHandle.close()
            try? FileManager.default.removeItem(at: outURL)
            try? FileManager.default.removeItem(at: errURL)
        }

        return try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Data, Error>) in
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: path)
            proc.arguments = args

            var env = ProcessInfo.processInfo.environment
            let extra = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
            env["PATH"] = env["PATH"].map { "\($0):\(extra)" } ?? extra
            proc.environment = env

            proc.standardOutput = outHandle
            proc.standardError = errHandle

            proc.terminationHandler = { p in
                let outData = (try? Data(contentsOf: outURL)) ?? Data()
                let errData = (try? Data(contentsOf: errURL)) ?? Data()
                if p.terminationStatus != 0 {
                    cont.resume(throwing: GHError.process(
                        code: p.terminationStatus,
                        stderr: String(data: errData, encoding: .utf8) ?? ""))
                } else {
                    cont.resume(returning: outData)
                }
            }
            do { try proc.run() } catch { cont.resume(throwing: error) }
        }
    }

    /// Run a shared `core/graphql` query. When `withRepo` is true the repo
    /// coordinates from `core/config.json` are passed as `$owner`/`$name`.
    public static func graphql(_ queryName: String, withRepo: Bool) async throws -> Data {
        let query = try CoreAssets.graphql(queryName)
        var args = ["api", "graphql", "-f", "query=\(query)"]
        if withRepo {
            let cfg = try CoreAssets.config()
            args += ["-f", "owner=\(cfg.owner)", "-f", "name=\(cfg.repo)"]
        }
        return try await run(args)
    }
}
