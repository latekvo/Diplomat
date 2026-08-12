import Foundation
import DiplomatCore

/// Asking each run's own agent what it is doing — the Swift twin of
/// `probes.agent_sessions`.
///
/// Positive evidence where the pane gives an inference: a turn the runner itself marks
/// finished, rather than whether someone else's status bar happened to have its interrupt
/// hint drawn when we looked.
///
/// Two runners answer, from different places — OpenCode over the loopback port its spawn
/// reserved (`OpenCodeProbe`), Hermes out of the SQLite store it keeps every session in
/// (`HermesProbe`) — and both come back as the same typed answer, so nothing downstream
/// learns which runner it is looking at.
enum AgentSessionProbe {
    /// Which session a run turned out to own, and what it currently says.
    ///
    /// The two travel together because the sweep learns both at once and persists the
    /// first: matching costs a fetch, and having paid it once the row should never pay
    /// again.
    struct AgentSession {
        let sessionID: String
        let state: AgentState.SessionState
    }

    /// What every run that serves a session says it is doing, keyed by row.
    ///
    /// A row missing from the answer is a row this cannot reach: every Claude Code run,
    /// an OpenCode run spawned without a port, one whose server has not come up yet, one
    /// whose session has not been written to yet. Its screen is read instead, so absence
    /// here costs the older evidence and never a verdict.
    ///
    /// The directory is resolved because both stores record the agent's own working
    /// directory, which is physical, while the configured repo root is whatever the
    /// operator typed — and the match is exact equality, so one symlink between them and
    /// no run ever binds.
    static func states(for procs: [TrackedProcess],
                       directory: String = AgentSpawner.repoPath) -> [UUID: AgentSession] {
        let asking = procs.compactMap { p -> (TrackedProcess, Backend)? in
            guard let backend = backends[p.runner] else { return nil }
            return (p, backend)
        }
        guard !asking.isEmpty else { return [:] }
        let directory = URL(fileURLWithPath: directory).resolvingSymlinksInPath().path
        var taken = Set(asking.map(\.0.agentSessionID).filter { !$0.isEmpty })
        var out: [UUID: AgentSession] = [:]
        // In dispatch order, so the runs that have already matched a session are out of
        // the way before a newer one goes looking — `taken` is only a useful filter if it
        // is filled in the order the sessions were created.
        for (p, backend) in asking.sorted(by: { $0.0.createdAt < $1.0.createdAt }) {
            var sessionID = p.agentSessionID
            if sessionID.isEmpty {
                sessionID = backend.bind(p, directory, taken)
                guard !sessionID.isEmpty else { continue }
                taken.insert(sessionID)
            }
            guard let state = backend.state(p, sessionID) else { continue }
            out[p.id] = AgentSession(sessionID: sessionID, state: state)
        }
        return out
    }

    /// Where one runner's answers come from: which of its sessions is a run's, and what
    /// that session currently says.
    private struct Backend {
        let bind: (TrackedProcess, String, Set<String>) -> String
        let state: (TrackedProcess, String) -> AgentState.SessionState?
    }

    /// Which store answers for which runner. A runner absent from here serves nothing and
    /// is read off its screen — that is Claude Code, and a run whose runner was never
    /// recorded. Same table as the Linux side's `probes._BACKENDS`.
    private static let backends: [String: Backend] = [
        AgentRunner.opencode.rawValue: Backend(
            bind: { p, directory, taken in
                OpenCodeProbe.bind(p, directory: directory, taken: taken)
            },
            state: { p, sessionID in OpenCodeProbe.state(p, sessionID: sessionID) }),
        AgentRunner.hermes.rawValue: Backend(
            bind: { p, directory, taken in
                HermesProbe.bind(p, directory: directory, taken: taken)
            },
            state: { _, sessionID in HermesProbe.state(sessionID: sessionID) }),
    ]
}
