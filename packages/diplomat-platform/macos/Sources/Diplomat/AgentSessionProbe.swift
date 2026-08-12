import Foundation
import DiplomatCore

/// Asking each run's own agent what it is doing — the Swift twin of
/// `probes.agent_sessions`.
///
/// Positive evidence where the screen gives an inference: a turn the runner itself marks
/// finished, rather than whether someone else's status bar happened to have its interrupt
/// hint drawn when we looked.
///
/// Two runners answer, from different places — OpenCode over the loopback port its spawn
/// reserved (`OpenCodeProbe`), Hermes out of the SQLite store it keeps every session in
/// (`HermesProbe`) — and both come back as the same typed answer, so nothing downstream
/// learns which runner it is looking at.
enum AgentSessionProbe {
    /// Does this runner serve a session of its own to ask?
    ///
    /// A runner that does not is read off its screen — that is Claude Code, and a run
    /// whose runner was never recorded.
    static func serves(_ runner: String) -> Bool { backends[runner] != nil }

    /// What every run that serves a session says it is doing, keyed by run id.
    ///
    /// A run missing from the answer is a run this cannot reach: an OpenCode run spawned
    /// without a port, one whose server has not come up yet, one whose session has not
    /// been written to yet. Its screen is read instead, so absence here costs the older
    /// evidence and never a verdict.
    ///
    /// Which session is a run's is found once and written into the run's directory: the
    /// search reads a session's opening message, while asking a bound one what it is doing
    /// reads a single message.
    ///
    /// The directory is resolved because both stores record the agent's own working
    /// directory, which is physical, while the configured repo root is whatever the
    /// operator typed — and the match is exact equality, so one symlink between them and
    /// no run ever binds.
    static func states(for records: [AgentState.RunRecord],
                       directory: String = AgentSpawner.repoPath)
        -> [String: AgentState.SessionState] {
        let asking = records.compactMap { r -> (AgentState.RunRecord, Backend)? in
            guard let backend = backends[AgentRegistry.runRunner(r.runID)] else { return nil }
            return (r, backend)
        }
        guard !asking.isEmpty else { return [:] }
        let directory = URL(fileURLWithPath: directory).resolvingSymlinksInPath().path
        var taken = Set(asking.map { AgentRegistry.boundSession($0.0.runID) }.filter { !$0.isEmpty })
        var out: [String: AgentState.SessionState] = [:]
        // In dispatch order, so the runs that have already matched a session are out of
        // the way before a newer one goes looking — `taken` is only a useful filter if it
        // is filled in the order the sessions were created.
        for (r, backend) in asking.sorted(by: { $0.0.dispatchedAt < $1.0.dispatchedAt }) {
            var sessionID = AgentRegistry.boundSession(r.runID)
            if sessionID.isEmpty {
                sessionID = backend.bind(r, directory, taken)
                guard !sessionID.isEmpty else { continue }
                AgentRegistry.bindSession(r.runID, sessionID)
                taken.insert(sessionID)
            }
            guard let state = backend.state(r, sessionID) else { continue }
            out[r.runID] = state
        }
        return out
    }

    /// Where one runner's answers come from: which of its sessions is a run's, and what
    /// that session currently says.
    private struct Backend {
        let bind: (AgentState.RunRecord, String, Set<String>) -> String
        let state: (AgentState.RunRecord, String) -> AgentState.SessionState?
    }

    /// Which store answers for which runner. Same table as the Linux side's
    /// `probes._BACKENDS`.
    private static let backends: [String: Backend] = [
        AgentRunner.opencode.rawValue: Backend(
            bind: { r, directory, taken in
                OpenCodeProbe.bind(r, directory: directory, taken: taken)
            },
            state: { r, sessionID in OpenCodeProbe.state(r, sessionID: sessionID) }),
        AgentRunner.hermes.rawValue: Backend(
            bind: { r, directory, taken in
                HermesProbe.bind(r, directory: directory, taken: taken)
            },
            state: { _, sessionID in HermesProbe.state(sessionID: sessionID) }),
    ]
}
