import DiplomatCore
import Foundation

/// `diplomat-core agent-registry` — write records through the Swift registry and read
/// them back, so the on-disk format can be diffed against the Python twin.
///
/// The book at `~/.diplomat/agents/runs.json` is read and written by BOTH front-ends,
/// and by the mesh node asking whether this machine has room. That makes its field
/// names a cross-language contract rather than a Swift detail: a renamed property that
/// only one side follows does not fail anything loudly — the other side simply reads a
/// run with no label, no source and no ledger key, which is indistinguishable from the
/// applet having forgotten it.
///
/// So `test_agent_registry_parity.py` writes a book with Python, has this read it, then
/// has this write one and reads it with Python. Anything either side drops shows up as
/// a diff.
///
/// Input:  `{ "mode": "read" }`
///      or `{ "mode": "write", "runs": [ {…}, … ] }`
///      or `{ "mode": "hooks", "activityPath": "…", "donePath": "…" | null }`
/// Output: `{ "runs": [ … ] }` — whatever the registry holds afterwards, as it decodes
/// it — or, for `hooks`, `{ "settings": {…} }`. `$DIPLOMAT_AGENTS_DIR` says where; the
/// test points it at a temp dir.
///
/// `hooks` is here for the same reason the rest is: the settings are shell commands
/// that WRITE the activity file, so a difference of one byte between the two front-ends
/// is a run that reports its turns differently depending on which applet spawned it —
/// and that file is read back by both.
enum AgentRegistryCommand {
    static func run(_ obj: [String: Any]) {
        if (obj["mode"] as? String) == "hooks" {
            emitHooks(activityPath: obj["activityPath"] as? String ?? "",
                      donePath: obj["donePath"] as? String)
            return
        }
        if (obj["mode"] as? String) == "write" {
            let runs = (obj["runs"] as? [[String: Any]] ?? []).map(decode)
            AgentRegistry.save(runs)
        }
        let out: [String: Any] = ["runs": AgentRegistry.load().map(encode)]
        guard let data = try? JSONSerialization.data(
            withJSONObject: out, options: [.sortedKeys, .prettyPrinted]) else {
            die("could not serialise the registry", 1)
        }
        FileHandle.standardOutput.write(data)
    }

    /// The `--settings` payload this side would stage, re-parsed so the comparison is
    /// over the commands rather than over JSON key order.
    private static func emitHooks(activityPath: String, donePath: String?) {
        guard let json = AgentCompletion.settingsJSON(activityPath: activityPath,
                                                      donePath: donePath),
              let settings = try? JSONSerialization.jsonObject(
                  with: Data(json.utf8)) else {
            die("could not build the hook settings", 1)
        }
        guard let data = try? JSONSerialization.data(
            withJSONObject: ["settings": settings],
            options: [.sortedKeys, .prettyPrinted]) else {
            die("could not serialise the hook settings", 1)
        }
        FileHandle.standardOutput.write(data)
    }

    private static func encode(_ r: AgentState.RunRecord) -> [String: Any] {
        [
            "runId": r.runID, "dispatchedAt": r.dispatchedAt,
            "prNumber": r.prNumber.map { $0 as Any } ?? NSNull(),
            "prUrl": r.prURL, "kind": r.kind, "label": r.label, "source": r.source,
            "placement": r.placement.rawValue, "node": r.node, "workKey": r.workKey,
            "ledgerKey": r.ledgerKey, "pid": r.pid.map { $0 as Any } ?? NSNull(),
            "tty": r.tty, "claimSeenAt": r.claimSeenAt.map { $0 as Any } ?? NSNull(),
            "quietDigest": r.quietDigest,
            "quietSince": r.quietSince.map { $0 as Any } ?? NSNull(),
            "untracked": r.untracked,
        ]
    }

    private static func decode(_ d: [String: Any]) -> AgentState.RunRecord {
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
            untracked: JSONInput.flag(d["untracked"]))
    }
}
