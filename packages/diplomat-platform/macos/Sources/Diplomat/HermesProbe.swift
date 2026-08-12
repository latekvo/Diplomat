import Foundation
import SQLite3
import DiplomatCore

/// Reading a Hermes run's own session store: the impure half of `HermesStore`.
///
/// The decisions — which session is this run's, what its last message says, what it spent
/// — live in `DiplomatCore.HermesStore`, shared with the Linux front-end and pinned by the
/// core smoke. What is here is only what a pure library cannot do: open the agent's
/// SQLite file and read four rows out of it. They are split because DiplomatCore is built
/// for Linux too, where SQLite is a system library this package does not take.
///
/// Every connection is read-only and carries a short busy timeout. The agent owns this
/// database and writes it mid-turn; a probe that blocked its writer — or worse, wrote to
/// it — would be a tracking mechanism that damaged the thing it tracks. Nothing here
/// throws: a store that cannot be read is a run whose screen is read instead.
enum HermesProbe {
    /// Hermes' session store. `DIPLOMAT_HERMES_DB` overrides it, the same escape hatch the
    /// Linux reader takes and how a self-test points at a fixture instead of the
    /// developer's real sessions.
    static var dbPath: String {
        if let env = ProcessInfo.processInfo.environment["DIPLOMAT_HERMES_DB"], !env.isEmpty {
            return (env as NSString).expandingTildeInPath
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".hermes/state.db").path
    }

    /// Which session in the store is this run's, by its opening prompt.
    ///
    /// Hermes keeps one store for the whole machine, so the directory and dispatch time
    /// only narrow the field — the prompt is what makes the match exact when two runs are
    /// working in one checkout, which the applet's own task cap makes ordinary.
    static func bind(_ p: TrackedProcess, directory: String, taken: Set<String>) -> String {
        guard !p.promptFile.isEmpty,
              let prompt = try? String(contentsOfFile: p.promptFile, encoding: .utf8)
        else { return "" }
        let found = candidates(directory: directory, since: p.createdAt.timeIntervalSince1970,
                               taken: taken)
        for sessionID in found.prefix(OpenCodeAPI.maxCandidates) {
            let opening = row("SELECT role, content FROM messages WHERE session_id = ? "
                              + "ORDER BY id LIMIT 1", columns: 2, text: [sessionID])
            if HermesStore.isOurs(role: opening?[0] as? String,
                                  content: opening?[1] as? String, prompt: prompt) {
                return sessionID
            }
        }
        return ""
    }

    /// What that session's last message says: mid-turn, or back at the prompt.
    static func state(sessionID: String) -> AgentState.SessionState? {
        guard let last = row("SELECT role, finish_reason FROM messages WHERE session_id = ? "
                             + "ORDER BY id DESC LIMIT 1", columns: 2, text: [sessionID])
        else { return nil }
        return HermesStore.stateOf(role: last[0] as? String,
                                   finishReason: last[1] as? String)
    }

    /// What one finished run spent, or nil if the store cannot say.
    static func sessionTokens(sessionID: String) -> Double? {
        guard !sessionID.isEmpty,
              let row = row("SELECT input_tokens, output_tokens, cache_write_tokens "
                            + "FROM sessions WHERE id = ?", columns: 3, text: [sessionID])
        else { return nil }
        return HermesStore.sessionTokens(input: row[0] as? Int, output: row[1] as? Int,
                                         cacheWrite: row[2] as? Int)
    }

    /// Sessions that could be a run's, oldest first.
    ///
    /// `source` is left alone deliberately: Hermes tags a session by how it was started
    /// and gates which toolsets load on that tag, so narrowing by it here would mean
    /// passing one at spawn time and quietly changing what the agent can do.
    private static func candidates(directory: String, since: Double,
                                   taken: Set<String>) -> [String] {
        let rows = query("SELECT id FROM sessions WHERE cwd = ? AND started_at >= ? "
                         + "ORDER BY started_at, id", columns: 1,
                         text: [directory], double: [since])
        return rows.compactMap { $0[0] as? String }.filter { !taken.contains($0) }
    }

    private static func row(_ sql: String, columns: Int, text: [String]) -> [Any?]? {
        query(sql, columns: columns, text: text, double: []).first
    }

    /// One read-only query. Every failure is an empty result: an absent store, a schema
    /// this build does not know, an agent holding the write lock longer than the tick can
    /// wait — all of them mean the same thing to the caller.
    private static func query(_ sql: String, columns: Int, text: [String],
                              double: [Double]) -> [[Any?]] {
        guard FileManager.default.fileExists(atPath: dbPath) else { return [] }
        var db: OpaquePointer?
        guard sqlite3_open_v2("file:\(dbPath)?mode=ro", &db,
                              SQLITE_OPEN_READONLY | SQLITE_OPEN_URI, nil) == SQLITE_OK else {
            sqlite3_close(db)
            return []
        }
        defer { sqlite3_close(db) }
        sqlite3_busy_timeout(db, Int32(HermesStore.busyTimeout * 1000))
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
            sqlite3_finalize(stmt)
            return []
        }
        defer { sqlite3_finalize(stmt) }
        // SQLITE_TRANSIENT: the parameters are Swift strings whose buffers this call does
        // not own past the bind, so SQLite has to take its own copy.
        let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
        for (i, value) in text.enumerated() {
            sqlite3_bind_text(stmt, Int32(i + 1), value, -1, transient)
        }
        for (i, value) in double.enumerated() {
            sqlite3_bind_double(stmt, Int32(text.count + i + 1), value)
        }
        var out: [[Any?]] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            out.append((0..<columns).map { column -> Any? in
                switch sqlite3_column_type(stmt, Int32(column)) {
                case SQLITE_INTEGER: return Int(sqlite3_column_int64(stmt, Int32(column)))
                case SQLITE_FLOAT: return sqlite3_column_double(stmt, Int32(column))
                case SQLITE_TEXT:
                    return String(cString: sqlite3_column_text(stmt, Int32(column)))
                default: return nil
                }
            })
        }
        return out
    }
}
