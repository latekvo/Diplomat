import Foundation

/// Errors surfaced by the `gh` shell-out layer. Flaky-by-design: we just bubble
/// the real failure up to the UI instead of trying to be clever.
public enum GHError: LocalizedError {
    case ghNotFound
    case process(code: Int32, stderr: String)
    case timeout(seconds: TimeInterval)
    case graphql(messages: [String])

    public var errorDescription: String? {
        switch self {
        case .ghNotFound:
            return "`gh` CLI not found. Install GitHub CLI and run `gh auth login`."
        case .process(let code, let stderr):
            let s = stderr.trimmingCharacters(in: .whitespacesAndNewlines)
            return "gh exited \(code): \(s.isEmpty ? "(no stderr)" : s)"
        case .timeout(let seconds):
            return "gh timed out after \(Int(seconds))s"
        case .graphql(let messages):
            return "GraphQL: \(messages.joined(separator: "; "))"
        }
    }
}

/// Thin wrapper around the `gh` CLI. We run the binary directly (args passed
/// literally, so no shell-quoting headaches) and rely on `gh`'s own auth/config.
/// The GraphQL queries are loaded from the shared `assets/graphql` assets and the
/// repo coordinates supplied as `$owner`/`$name` variables, so the query text
/// itself stays repo-agnostic.
public enum GH {
    // Lock-protected cache (the old bare `var` was read/written from concurrent
    // async `run()` calls — a data race). Caches only on SUCCESS, so gh installed
    // after launch is picked up by the next call instead of requiring a restart;
    // one that no longer launches is forgotten the same way.
    private static let pathLock = NSLock()
    private static var cachedPath: String?

    private static func forget(_ path: String) {
        pathLock.lock()
        defer { pathLock.unlock() }
        if cachedPath == path { cachedPath = nil }
    }

    /// Where gh is looked for before the login shell is asked.
    public static let candidatePaths = ["/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh"]

    private static func ghPath() throws -> String {
        // Read per call, never cached: an override taken back is gone.
        if let env = ProcessInfo.processInfo.environment["DIPLOMAT_GH"], !env.isEmpty { return env }
        pathLock.lock()
        defer { pathLock.unlock() }
        if let p = cachedPath { return p }
        for c in candidatePaths where FileManager.default.isExecutableFile(atPath: c) {
            cachedPath = c
            return c
        }
        if let found = loginShellWhichGH() {
            cachedPath = found
            return found
        }
        throw GHError.ghNotFound
    }

    /// How long the login shell may take to say where gh lives. It sources the
    /// user's profile, which is free to hang, under `pathLock`, so every `run`
    /// would wait behind it; `timeout` bounds the gh call, not this.
    private static let lookupTimeout: TimeInterval = 5

    /// Last resort: ask a login shell where gh lives (covers exotic installs).
    /// stdout is a file rather than a pipe: a job the profile leaves in the
    /// background keeps a pipe open after the shell exits, and reading it to the
    /// end would wait for that job. The wait is on the deadline rather than
    /// `waitUntilExit`: corelibs sees a process exit only once every descendant
    /// holding its end of the launch socketpair has, so that job would hold the
    /// wait past the kill.
    private static func loginShellWhichGH() -> String? {
        let shell = ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/sh"
        let outURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("diplomat-\(UUID().uuidString).which")
        guard FileManager.default.createFile(atPath: outURL.path, contents: nil),
              let outHandle = try? FileHandle(forWritingTo: outURL) else { return nil }
        defer {
            try? outHandle.close()
            try? FileManager.default.removeItem(at: outURL)
        }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: shell)
        proc.arguments = ["-lc", "command -v gh"]
        proc.standardOutput = outHandle
        proc.standardError = FileHandle.nullDevice
        let exited = DispatchSemaphore(value: 0)
        proc.terminationHandler = { _ in exited.signal() }
        do { try proc.run() } catch { proc.terminationHandler = nil; return nil }
        if exited.wait(timeout: .now() + lookupTimeout) == .timedOut {
            stop(proc)
            return nil
        }
        let path = (try? Data(contentsOf: outURL))
            .flatMap { String(data: $0, encoding: .utf8) }?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return FileManager.default.isExecutableFile(atPath: path) ? path : nil
    }

    private static func stop(_ proc: Process) {
        proc.terminate()
        DispatchQueue.global().asyncAfter(deadline: .now() + 5) {
            if proc.isRunning { kill(proc.processIdentifier, SIGKILL) }
        }
    }

    /// How long one `gh` call may take - the Linux twin's `gh.run` budget. Every
    /// monitor waits on this call, so a response that never comes would otherwise
    /// hold both of them until the app restarts.
    public static let timeout: TimeInterval = 60

    /// Run `gh` with the given argv. stdout/stderr are redirected to temp files so
    /// large payloads can't deadlock a pipe buffer (and no cross-thread captures).
    public static func run(_ args: [String], timeout: TimeInterval = timeout) async throws -> Data {
        let path = try ghPath()
        let tmp = FileManager.default.temporaryDirectory
        let outURL = tmp.appendingPathComponent("diplomat-\(UUID().uuidString).out")
        let errURL = tmp.appendingPathComponent("diplomat-\(UUID().uuidString).err")
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

            // The deadline only asks the process to stop; the termination handler is
            // the one place the continuation is resumed, so a request that ends on its
            // own right then is still resumed exactly once. The handler must not hold
            // the deadline: the deadline holds the process, the process its handler,
            // and a launch that fails leaves nothing to break that ring.
            let timedOut = Flag()
            proc.terminationHandler = { p in
                if timedOut.isSet {
                    cont.resume(throwing: GHError.timeout(seconds: timeout))
                    return
                }
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
            // Darwin keeps the handler of a process that never ran until it is cleared.
            do { try proc.run() } catch {
                proc.terminationHandler = nil
                forget(path)
                cont.resume(throwing: error)
                return
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout) {
                guard proc.isRunning else { return }
                timedOut.set()
                stop(proc)
            }
        }
    }

    private final class Flag: @unchecked Sendable {
        private let lock = NSLock()
        private var value = false
        func set() { lock.lock(); value = true; lock.unlock() }
        var isSet: Bool { lock.lock(); defer { lock.unlock() }; return value }
    }

    /// Run a shared `assets/graphql` query. When `withRepo` is true the repo
    /// coordinates from `assets/config.json` are passed as `$owner`/`$name`.
    /// `variables` go through gh's `-f`, which sends the value as a string;
    /// `typedVariables` through `-F`, which lets gh parse it into the JSON type the
    /// query declares — the spelling a `Boolean!` needs.
    ///
    /// Retries once on failure. GitHub intermittently times the heavier queries out,
    /// so a single retry turns a transient blip into a non-event. It lives here
    /// rather than in `run` because that one also carries mutations (`pr merge`),
    /// which must never be replayed.
    public static func graphql(_ queryName: String, withRepo: Bool,
                               variables: [(String, String)] = [],
                               typedVariables: [(String, String)] = []) async throws -> Data {
        let query = try CoreAssets.graphql(queryName)
        var args = ["api", "graphql", "-f", "query=\(query)"]
        if withRepo {
            let cfg = try CoreAssets.config()
            args += ["-f", "owner=\(cfg.owner)", "-f", "name=\(cfg.repo)"]
        }
        for (k, v) in variables { args += ["-f", "\(k)=\(v)"] }
        for (k, v) in typedVariables { args += ["-F", "\(k)=\(v)"] }

        var lastError: Error?
        for attempt in 0..<2 {
            do {
                return try await run(args)
            } catch {
                lastError = error
                if attempt == 0 { try? await Task.sleep(nanoseconds: 800_000_000) }
            }
        }
        throw lastError!
    }
}
