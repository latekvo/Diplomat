import Foundation

/// The durable book of dispatched agent runs — one record per run, on disk.
///
/// Both front-ends read and write the same file in the same format, so a run means the
/// same thing on either. Python twin: `diplomat_app/agentregistry.py`.
///
/// Layout, under `~/.diplomat/agents` (`$DIPLOMAT_AGENTS_DIR` overrides, which is also
/// how the tests get an isolated one):
///
///     runs.json            the records — the book itself
///     <run-id>/prompt.txt  what the agent was asked (also its transcript's first message)
///     <run-id>/pid         the agent's real pid, written by the agent's own shell
///     <run-id>/done        its exit code, written when it returns
///     <run-id>/port        the loopback port its OpenCode server answers on
///     <run-id>/session     which OpenCode session turned out to be this run's
///
/// The last two are the Linux front-end's; macOS carries the same two facts on the
/// `TrackedProcess` row it already keeps per run.
///
/// The per-run directory is what makes identity exact. The shell that runs the agent
/// writes its own `$$` into `pid` and then `exec`s the agent, so the pid in that file IS
/// the agent's — not a wrapper's, not a tmux client's. Before this, a run was identified
/// by matching `PR #<n> in <owner>/<repo>` against prompts in `ps` output, which could
/// not tell two runs on one PR apart and matched any unrelated session that mentioned
/// the number.
public enum AgentRegistry {
    /// Bumped only if the on-disk shape changes incompatibly. A file from the future is
    /// ignored rather than misread — an older applet must not act on records whose
    /// fields it does not understand, and the process scan still covers what is running.
    public static let schemaVersion = 1

    /// Serialises the read-modify-write in `add`: a spawn registering against a list a
    /// concurrent sweep already copied would be dropped, leaving an agent nothing
    /// counts — a bay of the cap the machine can then spend twice.
    private static let lock = NSLock()

    // MARK: - Paths

    public static func agentsDir() -> URL {
        if let override = ProcessInfo.processInfo.environment["DIPLOMAT_AGENTS_DIR"],
           !override.isEmpty {
            return URL(fileURLWithPath: override, isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".diplomat", isDirectory: true)
            .appendingPathComponent("agents", isDirectory: true)
    }

    public static func runsPath() -> URL {
        agentsDir().appendingPathComponent("runs.json")
    }

    public static func runDir(_ runID: String) -> URL {
        agentsDir().appendingPathComponent(runID, isDirectory: true)
    }

    public static func promptPath(_ runID: String) -> URL {
        runDir(runID).appendingPathComponent("prompt.txt")
    }

    public static func pidPath(_ runID: String) -> URL {
        runDir(runID).appendingPathComponent("pid")
    }

    public static func donePath(_ runID: String) -> URL {
        runDir(runID).appendingPathComponent("done")
    }

    /// A run's identity: the dispatch second, then random.
    ///
    /// The timestamp leads so a directory listing sorts into dispatch order while
    /// debugging, and the random tail is what actually makes it unique — two jobs of one
    /// poll are dispatched inside the same second.
    public static func newRunID(now: TimeInterval) -> String {
        "\(Int(now))-\(UUID().uuidString.prefix(8).lowercased())"
    }

    // MARK: - The book

    /// Every persisted record. Empty on anything unreadable — a corrupt book must
    /// degrade to "this applet has forgotten", which the process scan still covers,
    /// rather than taking the applet down on startup.
    public static func load() -> [AgentState.RunRecord] {
        guard let data = try? Data(contentsOf: runsPath()),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              (obj["version"] as? NSNumber)?.intValue == schemaVersion,
              let raw = obj["runs"] as? [[String: Any]]
        else { return [] }
        return raw.compactMap(decode).filter { !$0.runID.isEmpty }
    }

    /// Replace the book with `records`.
    @discardableResult
    public static func save(_ records: [AgentState.RunRecord]) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return write(records)
    }

    /// Append one run, read-modify-write under the lock.
    public static func add(_ record: AgentState.RunRecord) {
        lock.lock()
        defer { lock.unlock() }
        write(loadUnlocked() + [record])
    }

    /// Stage a run's directory and register it.
    ///
    /// The prompt is written here rather than to a temp file because it is what ties the
    /// run back to its Claude transcript when it finishes — the transcript's opening
    /// user message IS this text — and a run directory that outlives `/tmp` cleanup
    /// keeps that link. 0600, because the prompt can quote a private repo and `$HOME` is
    /// readable by other local users under a default umask.
    @discardableResult
    public static func createRun(_ record: AgentState.RunRecord,
                                 prompt: String) -> AgentState.RunRecord {
        let dir = runDir(record.runID)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try? prompt.write(to: promptPath(record.runID), atomically: true, encoding: .utf8)
        try? FileManager.default.setAttributes([.posixPermissions: 0o600],
                                               ofItemAtPath: promptPath(record.runID).path)
        add(record)
        return record
    }

    /// Fill in the pid of every run whose shell has written one since we last looked.
    ///
    /// A run is `.starting` until this succeeds, so the read happens every tick until it
    /// does. A malformed or absent file leaves the pid unset, which keeps the run in the
    /// spawn grace rather than declaring anything about it.
    public static func adoptPids(_ records: [AgentState.RunRecord]) -> [AgentState.RunRecord] {
        records.map { r in
            guard r.pid == nil, !r.untracked, let pid = readPid(r.runID) else { return r }
            var out = r
            out.pid = pid
            return out
        }
    }

    private static func readPid(_ runID: String) -> Int? {
        guard let raw = try? String(contentsOf: pidPath(runID), encoding: .utf8),
              let pid = Int(raw.trimmingCharacters(in: .whitespacesAndNewlines)),
              pid > 0
        else { return nil }
        return pid
    }

    /// The run ids whose agent has written its exit code.
    ///
    /// Always `.present`: this reads our own directory, and a run whose sentinel is
    /// absent is positively "has not exited yet" rather than unknown.
    public static func sentinels(_ records: [AgentState.RunRecord]) -> Observation<Set<String>> {
        var found = Set<String>()
        for r in records where FileManager.default.fileExists(atPath: donePath(r.runID).path) {
            found.insert(r.runID)
        }
        return .present(found)
    }

    /// When the agent actually exited, from the sentinel's mtime.
    ///
    /// Not "when a poll got round to noticing", which is up to a poll period later and
    /// would inflate every recorded run time by a random few minutes.
    public static func finishedAt(_ runID: String) -> TimeInterval? {
        guard let attrs = try? FileManager.default
                .attributesOfItem(atPath: donePath(runID).path),
              let date = attrs[.modificationDate] as? Date
        else { return nil }
        return date.timeIntervalSince1970
    }

    /// The prompt a run was dispatched with, for pricing it against its transcript.
    public static func prompt(_ runID: String) -> String {
        (try? String(contentsOf: promptPath(runID), encoding: .utf8)) ?? ""
    }

    /// Drop these runs from the book and delete their directories.
    ///
    /// Called with what the resolver found retirable — positive evidence the agent
    /// ended — never on a timer.
    public static func forget(_ runIDs: Set<String>) {
        guard !runIDs.isEmpty else { return }
        lock.lock()
        write(loadUnlocked().filter { !runIDs.contains($0.runID) })
        lock.unlock()
        for id in runIDs { try? FileManager.default.removeItem(at: runDir(id)) }
    }

    // MARK: - Encoding
    //
    // Hand-rolled rather than Codable, because the field names are a cross-language
    // contract with the Python twin rather than a Swift detail, and a renamed property
    // must not silently change the file both applets read.

    private static func loadUnlocked() -> [AgentState.RunRecord] { load() }

    @discardableResult
    private static func write(_ records: [AgentState.RunRecord]) -> Bool {
        let payload: [String: Any] = ["version": schemaVersion,
                                      "runs": records.map(encode)]
        guard let data = try? JSONSerialization.data(withJSONObject: payload,
                                                     options: [.prettyPrinted, .sortedKeys])
        else { return false }
        try? FileManager.default.createDirectory(at: agentsDir(),
                                                 withIntermediateDirectories: true)
        // Atomically, so a concurrent reader — the other front-end, or the mesh node
        // asking whether this machine has room — never sees a torn file.
        return (try? data.write(to: runsPath(), options: .atomic)) != nil
    }

    private static func encode(_ r: AgentState.RunRecord) -> [String: Any] {
        [
            "runId": r.runID, "dispatchedAt": r.dispatchedAt,
            "prNumber": r.prNumber.map { $0 as Any } ?? NSNull(),
            "prUrl": r.prURL, "kind": r.kind, "label": r.label, "source": r.source,
            "placement": r.placement.rawValue, "node": r.node, "workKey": r.workKey,
            "ledgerKey": r.ledgerKey,
            "pid": r.pid.map { $0 as Any } ?? NSNull(),
            "tty": r.tty,
            "claimSeenAt": r.claimSeenAt.map { $0 as Any } ?? NSNull(),
            "untracked": r.untracked,
        ]
    }

    private static func decode(_ d: [String: Any]) -> AgentState.RunRecord? {
        guard let runID = d["runId"] as? String else { return nil }
        return AgentState.RunRecord(
            runID: runID,
            dispatchedAt: (d["dispatchedAt"] as? NSNumber)?.doubleValue ?? 0,
            prNumber: (d["prNumber"] as? NSNumber)?.intValue,
            prURL: d["prUrl"] as? String ?? "",
            kind: d["kind"] as? String ?? "",
            label: d["label"] as? String ?? "",
            source: d["source"] as? String ?? AgentDispatchGate.Source.auto.rawValue,
            placement: AgentState.Placement(rawValue: d["placement"] as? String ?? "local")
                ?? .local,
            node: d["node"] as? String ?? "",
            workKey: d["workKey"] as? String ?? "",
            ledgerKey: d["ledgerKey"] as? String ?? "",
            pid: (d["pid"] as? NSNumber)?.intValue,
            tty: d["tty"] as? String ?? "",
            claimSeenAt: (d["claimSeenAt"] as? NSNumber)?.doubleValue,
            untracked: d["untracked"] as? Bool ?? false)
    }
}
