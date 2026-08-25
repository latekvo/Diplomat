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
/// The order is the reading order the panel wants and *also* the row's status
/// precedence: an outcome ("merged" — the PR landed) outranks a
/// local exit ("done" — the `claude` process left, whatever it achieved), an idle
/// session that wants a human ("awaiting input") outranks one that doesn't need
/// anything ("running"), and work that has not started yet is last.
///
/// The list a front-end draws starts at "awaiting input": the two statuses above it
/// are what a run that has ENDED reads as, and an ended run is retired rather than
/// drawn (`AgentState.ended`).
///
/// `starting` is the span between the two halves of that list: a task taken off the
/// queue whose spawn has not answered yet. It sorts directly under the running
/// agents because that is where the session it becomes will be drawn, so the row
/// that a click turns from queued into starting barely moves again.
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
    case starting
    case unknown
    case free
    case queued

    public static func < (lhs: AgentTaskStatus, rhs: AgentTaskStatus) -> Bool {
        lhs.rawValue < rhs.rawValue
    }

    /// The status a run's state reads as. A queued task has no run behind it and is
    /// `.queued` by construction (`.starting` while its dispatch runs); an empty slot has
    /// no task at all and is `.free`.
    ///
    /// The two enums are one order in two vocabularies — `AgentState.stateOrder` ranks the
    /// runs, this ranks them among the rows that are not runs. A new `RunState` stops this
    /// compiling; a REORDERED one does not, so the smoke walks `stateOrder` through here
    /// and checks the result against `allCases`.
    public static func of(_ state: AgentState.RunState) -> AgentTaskStatus {
        switch state {
        case .merged:        return .merged
        case .finished:      return .done
        case .awaitingInput: return .awaitingInput
        case .running:       return .running
        case .starting:      return .starting
        case .unknown:       return .unknown
        }
    }

    /// The word the row shows.
    public var title: String {
        switch self {
        case .merged:        return "merged"
        case .done:          return "done"
        case .awaitingInput: return "awaiting input"
        case .running:       return "running"
        case .starting:      return "starting"
        // Said out loud rather than guessed at: a row nothing could be learned about
        // holds its bay and says so, and its reason is drawn beside it.
        case .unknown:       return "unknown"
        case .free:          return "free slot"
        case .queued:        return "queued"
        }
    }
}

/// The order work waits in.
///
/// Most of the queue is a *view* of what the monitors would re-offer, not a second
/// copy of their state: the cap defers work by writing no attempt record, so every
/// poll re-offers everything GitHub still owes and the list is rebuilt from that.
/// Only the operator's arrangement of it is remembered, because that is the one
/// thing a poll cannot reconstruct.
///
/// The exception is the work the operator asks for by sweeping a scope
/// (`requestedActions`) — a Review-PRs sweep's reviews, a Fix-issues sweep's fixes.
/// GitHub has nothing to re-offer them from — a PR does not record that someone
/// wanted it reviewed, nor an issue that someone swept it — so that ask is the
/// front-end's own list, and it is the front-end that offers one task per item on
/// each poll until each is dispatched.
public enum AgentTaskQueue {
    /// A queued task's identity, stable across polls and applet restarts: the
    /// monitor's verb plus the item it is about. Not the mesh work key — that one is
    /// scoped to a head sha, so a push during the wait would read as a different task
    /// and lose the operator's place for it.
    ///
    /// The verb is part of the key because a PR can owe two different monitors at
    /// once (a conflict *and* an unaddressed review); they are two tasks, and the
    /// one that dispatches first makes the other read as in-flight rather than
    /// overwriting it. It is also what keeps the two numbering spaces apart: issue
    /// #421 and PR #421 are unrelated pieces of work, and only the verb says which
    /// one a key names.
    public static func key(auditAction: String, number: Int) -> String {
        "\(auditAction):\(number)"
    }

    /// The two verbs a sweep's work is queued under — the same ones the Review-PRs
    /// and Fix-issues spawns write to the activity feed, because each ask is that
    /// spawn, split into one task per PR / per issue.
    public static let reviewAction = "review"
    public static let issuesAction = "issues"
    public static let requestedActions: Set<String> = [reviewAction, issuesAction]

    /// The bands whose work waits behind the rest, nearest-first — everything the
    /// operator asked for, then a conflict fix, which waits behind everything.
    /// Matched off the queue key rather than the job, because the operator's saved
    /// arrangement is a list of keys and has to be banded the same way after a
    /// restart, with no job to consult (`order`).
    public static let lastBands: [Set<String>] = [requestedActions, ["conflicts"]]

    /// Which band of the queue a task waits in: 0 for the monitors' own finds, 1 for
    /// work the operator asked for, 2 for a conflict fix. Bands outrank the
    /// operator's arrangement; within one, the arrangement decides.
    ///
    /// A monitor's find is first because it is answering something GitHub is already
    /// owed — a review requested of me, a thread on my PR waiting on a reply — and
    /// that debt is visible to other people. A requested review is a sweep the
    /// operator started when they had the time for it; it is worth the whole cap
    /// eventually, but not ahead of the work the repository is waiting on. Sweeping
    /// fifty drafts otherwise buries every review request behind them for a day.
    ///
    /// Resolving a conflict stays last: it is the one unit of work that another
    /// agent's run routinely makes unnecessary — a review-reply agent works the same
    /// branch and lands its own merge on the way, and a review of someone else's PR
    /// can leave this one behind a rebase. Run first, a conflict fix spends a bay of
    /// the cap on the state of the branch as it was BEFORE the work in front of it
    /// landed — and often on a conflict that no longer exists by the time it opens
    /// the diff. It is also the cheapest to re-derive: the reconciler re-offers it
    /// every poll for as long as GitHub still calls the PR conflicting, so a fix
    /// deferred is never a fix lost.
    public static func band(_ key: String) -> Int {
        let verb = String(key.prefix(while: { $0 != ":" }))
        return (lastBands.firstIndex { $0.contains(verb) }.map { $0 + 1 }) ?? 0
    }

    /// Does the evidence of THIS poll still owe a task the queue is holding?
    ///
    /// A queued task carries the prompt and the verdict of the poll that staged it,
    /// which can be a whole poll period old by the time a slot frees — and in that
    /// gap the agent ahead of it in the queue was working the very branch it is
    /// about to open. So the drain asks again before it spends a bay: a conflict fix
    /// on a PR GitHub no longer calls conflicting, or a reply on a PR whose threads
    /// are answered, is work somebody already did.
    ///
    /// A PR that has left the open state retires every verb, the operator's own ask
    /// included: merged or closed, there is no branch left to fix and a review lands
    /// on a diff nobody will open again. `closed` is positive evidence — the PRs this
    /// cycle SAW closed — so a PR missing from it reads as open and its row stands,
    /// which is the safe direction for the one answer that also forgets the ask
    /// behind the row.
    ///
    /// While the PR is open, only the two verbs `conflicting`/`owingReply` come from
    /// are answerable — both are jobs on MY PRs, and `snapshots` is the fetch of
    /// exactly those. A review requested of me lives in the other fetch, and nothing
    /// on this machine retires it: it is owed until I review it, which is what the
    /// agent is for. A review the operator asked for is owed for the same reason, by
    /// their word rather than GitHub's. Unanswerable is not stale, so it stands.
    ///
    /// Only ever asked about a task whose number is a PR's. An issue fix the operator
    /// asked for is numbered in the other space entirely, and the caller stands it
    /// down rather than pricing issue #421 against the PRs closed this cycle.
    public static func stillOwed(auditAction: String, prNumber: Int,
                                 conflicting: Set<Int>,
                                 owingReply: Set<Int>,
                                 closed: Set<Int>) -> Bool {
        if closed.contains(prNumber) { return false }
        switch auditAction {
        case "conflicts":    return conflicting.contains(prNumber)
        case "review-reply": return owingReply.contains(prNumber)
        default:             return true
        }
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
    ///
    /// Requested reviews and then conflict fixes fall to the back whatever order they
    /// were found in (`band`). The monitors find their work mid-cycle — the conflict
    /// reconciler runs before the review-request fetch even begins — so without the
    /// bands a poll's own sequence would decide, and a sweep of fifty drafts offered
    /// first would hold up every review GitHub is waiting on.
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
        // Banded by a stable partition rather than `sort`, which is not guaranteed
        // stable in the standard library: everything above keeps its place within
        // the band it lands in, and that order is the operator's arrangement.
        return (0...lastBands.count).flatMap { b in out.filter { band($0) == b } }
    }

    /// One drag: `moving` lands where it was dropped relative to `onto` — after it
    /// when it came from above, before it when it came from below.
    ///
    /// Both directions are needed for every position to be reachable. An
    /// "always insert before the row you dropped on" rule can never move a task to
    /// the end of the queue, which is exactly the arrangement someone reaches for
    /// first (this one is not urgent — run it last).
    ///
    /// A drag onto a key that is not in the queue, onto itself, or into another band
    /// is not a rearrangement and leaves the order alone. The last of those is the
    /// same answer as the first two rather than a partial move, because a conflict fix
    /// dragged above a review would be re-banded on the next poll and snap back: a
    /// drag that cannot survive one poll is better refused than shown landing.
    public static func reorder(_ order: [String], moving id: String,
                               onto target: String) -> [String] {
        guard band(id) == band(target) else { return order }
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
