import DiplomatCore
import Foundation

/// `diplomat-core agent-state` — run one tick of the agent-state resolver over a
/// fixture and print every answer it produces.
///
/// Same standing as `tool-data` and `telemetry`: `AgentState` (Swift) and
/// `diplomat_runtime/agentstate.py` (the shared runtime) are two implementations of one
/// decision, and neither can delegate to the other — this runs on an 8-second poll and
/// on every dispatch, so a subprocess per tick is not an option. A drift would be
/// invisible in the worst way: both applets keep drawing rows, they just quietly
/// disagree about whether your agent is still running. `test_agent_state_parity.py`
/// drives both over the scenario table and diffs this output.
///
/// The whole pipeline is emitted, not just the resolver, because the order of the three
/// steps is itself a decision the two sides have to share: claims are observed BEFORE
/// resolving (so a sighting taken this tick counts this tick), and untracked agents are
/// synthesized AFTER (so a live agent that already has a record is not duplicated).
///
/// Input:
/// ```
/// { "now": 1000000.0, "limit": 2, "deadline": 14400.0,
///   "records": [ {"runId": …, "dispatchedAt": …, "pid": …, …} ],
///   "evidence": { "processes": {"status": "present", "value": {"4242": {…}}},
///                 "liveAgents": {"status": "present", "value": {"404": "pts/3"}}, … } }
/// ```
/// Output: the resolved rows in display order, every projection, and the records as
/// the pipeline left them — so a claim sighting written by the wrong step is a diff.
enum AgentStateCommand {
    static func run(_ obj: [String: Any]) {
        let now = (obj["now"] as? NSNumber)?.doubleValue ?? 0
        let limit = (obj["limit"] as? NSNumber)?.intValue ?? 2
        // Absent means the operator's switch is off, which is the Python default too —
        // a payload that forgot the key must not silently get a deadline of zero.
        let deadline = (obj["deadline"] as? NSNumber)?.doubleValue
        let evidence = decodeEvidence(obj["evidence"] as? [String: Any] ?? [:])
        let records = (obj["records"] as? [[String: Any]] ?? []).map(decodeRecord)
        let t = AgentState.tick(records: records, evidence: evidence,
                                now: now, limit: limit, deadline: deadline)

        var inFlight: [String: Bool] = [:]
        for pr in Set(t.records.compactMap(\.prNumber)) {
            inFlight[String(pr)] = t.inFlight(prNumber: pr)
        }
        let out: [String: Any] = [
            "rows": t.rows.map { r, s in
                ["runId": r.runID, "state": s.state.rawValue, "reason": s.reason]
            },
            "capLoad": t.capLoad.sorted(),
            "retirable": t.retirable.map(\.runID).sorted(),
            // The one destructive projection, so the two sides have to agree on it by
            // name: a window closed on one platform and left open on the other is a
            // machine that behaves differently depending on which applet is running.
            "reapable": t.reapable.map(\.runID).sorted(),
            "freeSlots": t.freeSlots,
            "inFlight": inFlight,
            "records": t.records.map { r -> [String: Any] in
                ["runId": r.runID, "claimSeenAt": r.claimSeenAt.map { $0 as Any } ?? NSNull(),
                 "untracked": r.untracked, "placement": r.placement.rawValue,
                 "quietDigest": r.quietDigest,
                 "quietSince": r.quietSince.map { $0 as Any } ?? NSNull()]
            },
        ]
        guard let data = try? JSONSerialization.data(
            withJSONObject: out, options: [.sortedKeys, .prettyPrinted]) else {
            die("could not serialise agent state", 1)
        }
        FileHandle.standardOutput.write(data)
    }

    // MARK: - Decoding
    //
    // A field absent from the payload decodes to `.unavailable`, never to an empty
    // answer — the same rule the resolver is built on, applied to its own input.

    private static func decodeObs<T>(_ raw: Any?,
                                     _ coerce: (Any) -> T?) -> Observation<T> {
        guard let d = raw as? [String: Any] else { return .unavailable("absent from payload") }
        let status = d["status"] as? String ?? "unavailable"
        guard status == "present" else {
            let reason = d["reason"] as? String ?? ""
            return status == "unsupported" ? .unsupported(reason) : .unavailable(reason)
        }
        guard let v = d["value"], let coerced = coerce(v) else {
            return .unavailable("value did not decode")
        }
        return .present(coerced)
    }

    private static func intSet(_ v: Any) -> Set<Int>? {
        guard let a = v as? [Any] else { return nil }
        return Set(a.compactMap { ($0 as? NSNumber)?.intValue })
    }

    private static func decodeEvidence(_ d: [String: Any]) -> AgentState.Evidence {
        AgentState.Evidence(
            processes: decodeObs(d["processes"]) { v in
                guard let m = v as? [String: Any] else { return nil }
                var out: [Int: AgentState.ProcInfo] = [:]
                for (k, p) in m {
                    guard let pid = Int(k), let pd = p as? [String: Any] else { continue }
                    out[pid] = AgentState.ProcInfo(
                        tty: pd["tty"] as? String ?? "",
                        elapsed: (pd["elapsed"] as? NSNumber)?.doubleValue ?? 0,
                        isAgent: pd["isAgent"] as? Bool ?? false)
                }
                return out
            },
            sentinels: decodeObs(d["sentinels"]) { ($0 as? [String]).map(Set.init) },
            tails: decodeObs(d["tails"]) { $0 as? [String: String] },
            claims: decodeObs(d["claims"]) { ($0 as? [String]).map(Set.init) },
            mergedPRs: decodeObs(d["mergedPrs"]) { intSet($0) },
            liveAgents: decodeObs(d["liveAgents"]) { v in
                guard let m = v as? [String: Any] else { return nil }
                var out: [Int: String] = [:]
                for (k, tty) in m {
                    guard let pr = Int(k) else { continue }
                    out[pr] = tty as? String ?? ""
                }
                return out
            },
            sessions: decodeObs(d["sessions"]) { v in
                guard let m = v as? [String: Any] else { return nil }
                var out: [String: AgentState.SessionState] = [:]
                for (runID, s) in m {
                    guard let sd = s as? [String: Any] else { continue }
                    out[runID] = AgentState.SessionState(busy: sd["busy"] as? Bool ?? false)
                }
                return out
            },
            activity: decodeObs(d["activity"]) { v in
                guard let m = v as? [String: Any] else { return nil }
                var out: [String: AgentState.TurnReport] = [:]
                for (runID, pair) in m {
                    guard let t = pair as? [Any], t.count == 2,
                          let raw = t[0] as? String,
                          let verb = AgentState.TurnReport.Verb(rawValue: raw),
                          let at = (t[1] as? NSNumber)?.doubleValue else { continue }
                    out[runID] = AgentState.TurnReport(verb: verb, at: at)
                }
                return out
            },
            tokensLeft: decodeObs(d["tokensLeft"]) { v in
                // Strictly a bool, matching Python's `isinstance(v, bool)`: `as? Bool`
                // alone bridges every NSNumber, so a stray 0 or 1 would answer the
                // deadline's precondition out of whatever happened to be truthy.
                guard let n = v as? NSNumber,
                      CFGetTypeID(n) == CFBooleanGetTypeID() else { return nil }
                return n.boolValue
            })
    }

    private static func decodeRecord(_ d: [String: Any]) -> AgentState.RunRecord {
        AgentState.RunRecord(
            runID: d["runId"] as? String ?? "",
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
            quietDigest: d["quietDigest"] as? String ?? "",
            quietSince: (d["quietSince"] as? NSNumber)?.doubleValue,
            untracked: d["untracked"] as? Bool ?? false)
    }
}
