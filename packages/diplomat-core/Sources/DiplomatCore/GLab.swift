import Foundation

/// The `glab` CLI wrapper, `GH`'s GitLab twin: the same process runner, deadline and error
/// type, so every caller's `GHError` handling covers both. Python twin:
/// `diplomat_runtime/glab.py`.
public enum GLab {
    private static let pathLock = NSLock()
    private static var cachedPath: String?

    public static let candidatePaths = ["/opt/homebrew/bin/glab", "/usr/local/bin/glab", "/usr/bin/glab"]

    /// `$DIPLOMAT_GLAB`, else `locate`'s answer. A found path is kept until it fails to launch.
    static func glabPath() throws -> String {
        let env = ProcessInfo.processInfo.environment
        if let o = env["DIPLOMAT_GLAB"], !o.isEmpty { return o }
        pathLock.lock()
        defer { pathLock.unlock() }
        if let p = cachedPath { return p }
        guard let found = locate(env: env) else { throw GHError.glabNotFound }
        cachedPath = found
        return found
    }

    /// `candidates`, then `PATH`, then a login `SHELL`'s lookup: an app started by launchd
    /// has a bare `PATH`.
    public static func locate(env: [String: String], candidates: [String] = candidatePaths) -> String? {
        let onPath = (env["PATH"] ?? "").split(separator: ":").map { "\($0)/glab" }
        if let found = (candidates + onPath).first(where: {
            FileManager.default.isExecutableFile(atPath: $0)
        }) { return found }
        return LoginShell.locate("glab", shell: env["SHELL"] ?? "/bin/sh")
    }

    private static func forget(_ path: String) {
        pathLock.lock()
        defer { pathLock.unlock() }
        if cachedPath == path { cachedPath = nil }
    }

    public static func output(_ args: [String], timeout: TimeInterval = GH.timeout) async throws
        -> (status: Int32, stdout: Data, stderr: String) {
        let path = try glabPath()
        let out = try await GH.execute(path, args, timeout: timeout,
                                       timedOut: GHError.glabTimeout(seconds: timeout),
                                       failedToLaunch: { forget(path) })
        return (out.status, out.stdout, out.stderr)
    }

    public static func run(_ args: [String], timeout: TimeInterval = GH.timeout) async throws -> Data {
        let out = try await output(args, timeout: timeout)
        guard out.status == 0 else { throw GHError.glabProcess(code: out.status, stderr: out.stderr) }
        return out.stdout
    }

    /// `-R` as a URL, which names the host too.
    public static func repoFlag(host: String, slug: String) -> [String] {
        ["-R", "https://\(host)/\(slug)"]
    }

    /// `slug` as REST `projects/:id` takes it, its `/` percent-encoded.
    public static func projectID(_ slug: String) -> String {
        slug.addingPercentEncoding(withAllowedCharacters: unreserved) ?? slug
    }

    private static let unreserved = CharacterSet(
        charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

    /// `typedVariables` go through `-F`, which parses them into the declared type (as a
    /// `Boolean!` needs). One retry, as `GH.graphql`. Throws on an `errors` array, and on a
    /// `path` the answer holds no project for: a missing or forbidden project must read as
    /// unreadable, not empty, or work queued in it is dropped as answered.
    public static func graphql(_ queryName: String, host: String,
                               variables: [(String, String)] = [],
                               typedVariables: [(String, String)] = []) async throws -> Data {
        let query = try CoreAssets.graphql(queryName)
        var args = ["api", "--hostname", host, "graphql", "-f", "query=\(query)"]
        for (k, v) in variables { args += ["-f", "\(k)=\(v)"] }
        for (k, v) in typedVariables { args += ["-F", "\(k)=\(v)"] }
        let path = variables.first { $0.0 == "path" }?.1
        return try await twice {
            let out = try await output(args)
            try check(out.stdout, path: path, host: host)
            guard out.status == 0 else {
                throw GHError.glabProcess(code: out.status, stderr: out.stderr)
            }
            return out.stdout
        }
    }

    /// Which of `ids` are members of `slug`: one REST read per `GitLab.memberPaths` chunk,
    /// each retried once; any failure throws.
    public static func members(host: String, slug: String, ids: [Int]) async throws -> Set<Int> {
        var found = Set<Int>()
        for path in GitLab.memberPaths(slug: slug, ids: ids) {
            found.formUnion(try await twice {
                try GitLab.memberIDs(try await run(["api", "--hostname", host, path]))
            })
        }
        return found
    }

    private static func twice<T>(_ call: () async throws -> T) async throws -> T {
        do { return try await call() } catch {
            try? await Task.sleep(nanoseconds: 800_000_000)
            return try await call()
        }
    }

    static func check(_ data: Data, path: String?, host: String) throws {
        guard let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }
        if let errs = obj["errors"] as? [[String: Any]], !errs.isEmpty {
            throw GHError.graphql(messages: errs.map { $0["message"] as? String ?? "?" })
        }
        if let path, ((obj["data"] as? [String: Any])?["project"] as? [String: Any]) == nil {
            throw GHError.unanswered("GitLab project \(path) not found on \(host)")
        }
    }

    /// The hosts in the first `config.yml` found where glab looks; empty when none is readable.
    public static func configuredHosts(env: [String: String] = ProcessInfo.processInfo.environment)
        -> Set<String> {
        let home = env["HOME"] ?? NSHomeDirectory()
        var files: [String] = []
        if let d = env["GLAB_CONFIG_DIR"], !d.isEmpty { files.append("\(d)/config.yml") }
        let xdg = env["XDG_CONFIG_HOME"].flatMap { $0.isEmpty ? nil : $0 } ?? "\(home)/.config"
        files.append("\(xdg)/glab-cli/config.yml")
        #if os(macOS)
        files.append("\(home)/Library/Application Support/glab-cli/config.yml")
        #endif
        guard let file = files.first(where: { FileManager.default.fileExists(atPath: $0) }),
              let text = try? String(contentsOfFile: file, encoding: .utf8) else { return [] }
        return GitLabHosts.parse(config: text)
    }
}
