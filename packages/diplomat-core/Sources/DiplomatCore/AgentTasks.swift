import Foundation

// MARK: - The Agent-tasks list (spawned sessions + the queue behind the cap)
//
// The panel answers one question — what is this machine doing about my PRs — with
// one list, so the agents it has SPAWNED and the automatic work its task cap is
// HOLDING (`AgentDispatchGate.atCapacity`) are rows of the same list. Both the
// order those rows are shown in and the order the queue is drained in are decided
// here, pure: the sequence the operator reads off the panel and the sequence the
// monitor actually runs are then the same rules, not two implementations of an
// intention.

/// Where one Agent-tasks row sits in the list, and what its status reads.
///
/// The order is the reading order the panel wants and *also* `ProcessRow`'s
/// existing status precedence: an outcome ("merged" — the PR landed) outranks a
/// local exit ("done" — the `claude` process left, whatever it achieved), an idle
/// session that wants a human ("awaiting input") outranks one that doesn't need
/// anything ("running"), and work that has not started yet is last. Finished rows
/// sit at the top because they are the only ones asking to be read.
///
/// `free` is the one case that is not a task: it is a slot of the device's cap with
/// nothing in it. It sorts with the running agents rather than with the queue
/// because it stands where one of them would — the panel draws the cap as a row of
/// bays, so how many are open is read at the same glance as what is in the rest.
public enum AgentTaskStatus: Int, Comparable, CaseIterable {
    case merged = 0
    case done
    case awaitingInput
    case running
    case free
    case queued

    public static func < (lhs: AgentTaskStatus, rhs: AgentTaskStatus) -> Bool {
        lhs.rawValue < rhs.rawValue
    }

    /// The status of a spawned session, from the three flags the tracker recomputes
    /// on each sweep. A queued task has no session and is `.queued` by construction;
    /// an empty slot has no task at all and is `.free`.
    public static func ofSession(merged: Bool, done: Bool,
                                 awaitingInput: Bool) -> AgentTaskStatus {
        if merged { return .merged }
        if done { return .done }
        if awaitingInput { return .awaitingInput }
        return .running
    }

    /// The word the row shows.
    public var title: String {
        switch self {
        case .merged:        return "merged"
        case .done:          return "done"
        case .awaitingInput: return "awaiting input"
        case .running:       return "running"
        case .free:          return "free slot"
        case .queued:        return "queued"
        }
    }
}

/// The order automatic work waits in.
///
/// The queue is a *view* of what the monitors would re-offer, not a second copy of
/// their state: the cap defers work by writing no attempt record, so every poll
/// re-offers everything GitHub still owes and the list is rebuilt from that. Only
/// the operator's arrangement of it is remembered, because that is the one thing a
/// poll cannot reconstruct.
public enum AgentTaskQueue {
    /// A queued task's identity, stable across polls and applet restarts: the
    /// monitor's verb plus the PR. Not the mesh work key — that one is scoped to a
    /// head sha, so a push during the wait would read as a different task and lose
    /// the operator's place for it.
    ///
    /// The verb is part of the key because a PR can owe two different monitors at
    /// once (a conflict *and* an unaddressed review); they are two tasks, and the
    /// one that dispatches first makes the other read as in-flight rather than
    /// overwriting it.
    public static func key(auditAction: String, prNumber: Int) -> String {
        "\(auditAction):\(prNumber)"
    }

    /// Slots of the device's automatic-task cap with nothing running in them — the
    /// empty bays the panel draws under the sessions.
    ///
    /// Clamped at zero because `running` can legitimately exceed the cap: it counts
    /// agents this device did not necessarily start (an untracked `claude` in `ps`
    /// counts as automatic), and lowering the cap while agents run leaves them
    /// running. Both would otherwise render as a negative number of free slots.
    public static func freeSlots(limit: Int, running: Int) -> Int {
        max(0, limit - running)
    }

    /// The queue for this poll: everything still offered, in the order the operator
    /// last dragged it into, with tasks they have never arranged appended in the
    /// order the monitors found them.
    ///
    /// Keys that are no longer offered fall out — the work was taken by an agent,
    /// resolved, or its author banned — because a queue that outlived its evidence
    /// would hand "execute now" a task GitHub no longer owes. (Not a mesh claim: the
    /// cap outranks the mesh gate, so a device with anything queued is by definition
    /// one that never asked a peer. Peer-owned work leaves the queue when the drain
    /// reaches it and the mesh answers.)
    public static func order(offered: [String], saved: [String]) -> [String] {
        let live = Set(offered)
        var out: [String] = []
        var seen = Set<String>()
        for key in saved where live.contains(key) && !seen.contains(key) {
            out.append(key)
            seen.insert(key)
        }
        for key in offered where !seen.contains(key) {
            out.append(key)
            seen.insert(key)
        }
        return out
    }

    /// One drag: `moving` lands where it was dropped relative to `onto` — after it
    /// when it came from above, before it when it came from below.
    ///
    /// Both directions are needed for every position to be reachable. An
    /// "always insert before the row you dropped on" rule can never move a task to
    /// the end of the queue, which is exactly the arrangement someone reaches for
    /// first (this one is not urgent — run it last).
    ///
    /// A drag onto a key that is not in the queue, or onto itself, is not a
    /// rearrangement and leaves the order alone.
    public static func reorder(_ order: [String], moving id: String,
                               onto target: String) -> [String] {
        guard id != target,
              let from = order.firstIndex(of: id),
              let to = order.firstIndex(of: target) else { return order }
        var out = order
        out.remove(at: from)
        guard let anchor = out.firstIndex(of: target) else { return order }
        out.insert(id, at: from < to ? anchor + 1 : anchor)
        return out
    }
}
