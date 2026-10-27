import Foundation
import DiplomatCore

/// Headless self-test of the free-disk floor and the run book against a real volume
/// (`scripts/full-volume.sh`), through their real entry points: `dispatchAgent` with
/// the repo root on the volume, `AgentRegistry` with the book there, and
/// `SelfUpdate.runScheduled` with a scratch checkout one commit behind its origin.
/// Free space is the real probe's; only the floor is set, relative to the volume.
///
/// It never spawns: a booked agent holds the only bay, so a dispatch the disk lets
/// through stops at the cap, and what goes past the cap does so with the book on the
/// full volume, which refuses the run before any terminal opens. Feed, book, config,
/// transcripts and process table are scratch or pinned as in `QueueTest`; the
/// updater's build and launch are fixtures, its fetch and merge real git.
///
///   DIPLOMAT_LOWDISK_TEST=1 swift run Diplomat
///
/// Skips (exit 0) where no volume can mount, unless `DIPLOMAT_FULL_VOLUME_REQUIRED=1`.
enum LowDiskTest {
    @discardableResult
    @MainActor
    static func run() async -> Bool {
        var pass = true
        func check(_ name: String, _ ok: Bool, _ detail: @autoclosure () -> String = "") {
            print("\(ok ? "PASS" : "FAIL") — \(name)\(ok ? "" : " \(detail())")")
            if !ok { pass = false }
        }

        let fm = FileManager.default
        let script = RepoPaths.root.appendingPathComponent("scripts/full-volume.sh").path
        let scratch = fm.temporaryDirectory.appendingPathComponent("diplomat-lowdisk-\(getpid())")
        try? fm.createDirectory(at: scratch, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: scratch) }
        let vol = scratch.appendingPathComponent("vol")
        func volume(_ cmd: String, _ extra: String...) -> Int32 {
            run(["/bin/bash", script, cmd, vol.path] + extra).code
        }

        let mounted = volume("mount", "32m")
        if mounted == 77 {
            print("LOWDISK TEST SKIPPED — this machine cannot mount a volume")
            return true
        }
        guard mounted == 0 else {
            print("LOWDISK TEST FAILED — full-volume.sh mount exited \(mounted)")
            return false
        }
        defer { _ = volume("unmount") }

        setenv("DIPLOMAT_AUDIT_DIR", scratch.appendingPathComponent("feed").path, 1)
        setenv("DIPLOMAT_AGENTS_DIR", scratch.appendingPathComponent("agents").path, 1)
        setenv("DIPLOMAT_CONFIG", scratch.appendingPathComponent("config.json").path, 1)
        setenv("DIPLOMAT_QUOTA_PROBE", "0", 1)
        setenv("DIPLOMAT_CLAUDE_DIR", scratch.appendingPathComponent("claude").path, 1)
        AgentProbes.pinDump(.present(""))
        defer { AgentProbes.pinDump(nil) }

        let drained = DiskFloor.free(at: vol.path) ?? 0
        let floorMiB = drained >> 21    // half the empty volume
        check("the empty volume has room to stand a floor in", floorMiB >= 4,
              "free \(drained) bytes")
        func setFloor(_ mib: Int64) { setenv(DiskFloor.envOverride, "\(mib)", 1) }
        func lowDiskLines() -> [AuditEntry] { AuditLog.read().filter { $0.action == "low-disk" } }

        // MARK: the dispatcher

        let repo = vol.appendingPathComponent("repo")
        try? fm.createDirectory(at: repo, withIntermediateDirectories: true)
        setenv("DIPLOMAT_REPO", repo.path, 1)
        setFloor(floorMiB)

        let store = Store()
        store.prAutofixEnabled = true
        store.reviewRequestsEnabled = true
        store.reviewAllowlistRaw = ""
        store.autoTaskLimit = 1
        store.queueAutoRun = true
        func holdTheBay() {
            AgentRegistry.forget(Set(AgentRegistry.load().map(\.runID)))
            let now = Date().timeIntervalSince1970
            AgentRegistry.createRun(
                AgentState.RunRecord(
                    runID: AgentRegistry.newRunID(now: now), dispatchedAt: now, prNumber: 1,
                    prURL: "https://github.com/software-mansion/argent/pull/1", kind: "review",
                    label: "Auto · Review · #1", source: AgentDispatchGate.Source.auto.rawValue),
                prompt: "")
        }

        func job(_ number: Int) -> Store.AgentJob {
            Store.AgentJob(kind: "review", auditAction: "review-req",
                           label: "Review-req · #\(number)", prompt: "",
                           prURL: "https://github.com/software-mansion/argent/pull/\(number)",
                           prNumber: number, authorLogin: nil, duty: "review", workKey: "",
                           counter: .reviewRequests,
                           attemptStamp: Store.AttemptStamp.unresolvedReview)
        }
        func offer(_ n: Int) async -> Store.DispatchOutcome {
            holdTheBay()
            return await store.dispatchAgent(job(n), source: .auto)
        }

        check("the empty volume is above the floor, so the cap is what holds a job",
              await offer(2) == .atCapacity)
        check("…and no low-disk line is written", lowDiskLines().isEmpty)
        store.commitQueue()

        check("fill: full-volume.sh fills the volume", volume("fill") == 0)
        let full = DiskFloor.free(at: repo.path) ?? -1
        check("the filled volume reads below the floor", full >= 0 && full < floorMiB << 20,
              "free \(full) bytes, floor \(floorMiB) MiB")
        var outcomes: [Store.DispatchOutcome] = []
        for n in [3, 4, 5] { outcomes.append(await offer(n)) }
        check("an auto job is held for the disk, ahead of the full bay",
              outcomes == [.lowDisk, .lowDisk, .lowDisk], "\(outcomes)")
        check("nothing was booked beside the agent already up",
              AgentRegistry.load().map(\.prNumber) == [1],
              "\(AgentRegistry.load().map(\.prNumber))")
        check("nothing was logged as started",
              !AuditLog.read().contains { $0.action == "review-req" })
        let lines = lowDiskLines()
        check("one low-disk line for the whole episode", lines.count == 1, "\(lines)")
        check("…naming the volume, its free space and the floor",
              lines.first.map {
                  $0.detail.hasPrefix("Deferring auto work — ")
                      && $0.detail.contains("free on \(repo.path), below the \(floorMiB) MiB floor")
              } == true, lines.first?.detail ?? "none")
        store.commitQueue()
        check("held, not dropped: the jobs are queued",
              store.queuedTasks.map(\.id) == ["review-req:3", "review-req:4", "review-req:5"],
              "\(store.queuedTasks.map(\.id))")
        // With the bay free, only the disk stands between the drain and a spawn: guarded,
        // so a volume that somehow reads above its floor fails here instead of opening one.
        AgentRegistry.forget(Set(AgentRegistry.load().map(\.runID)))
        let drainHeld = Store.agentDisk()?.low == true
        check("the drain meets the full volume with a bay free",
              drainHeld && AgentRegistry.load().isEmpty)
        if drainHeld { await store.drainQueuedTasks(snaps: [], closed: []) }
        check("the drain stops at the first task the disk holds, leaving the rest unasked",
              store.queuedTasks.map(\.id) == ["review-req:4", "review-req:5"],
              "\(store.queuedTasks.map(\.id))")

        check("drain: full-volume.sh frees the volume", volume("drain") == 0)
        check("with room again the disk lets the job through to the cap",
              await offer(6) == .atCapacity)
        check("fill again", volume("fill") == 0)
        check("a second episode is held again", await offer(7) == .lowDisk)
        check("…and gets a line of its own", lowDiskLines().count == 2, "\(lowDiskLines())")
        check("drain again", volume("drain") == 0)

        // MARK: the run book

        // What the floor lets through - a manual start, a mesh landing - is still booked
        // before it runs, and a book the disk refuses must refuse the run with it.
        setenv("DIPLOMAT_AGENTS_DIR", vol.appendingPathComponent("agents").path, 1)
        func record(_ number: Int) -> AgentState.RunRecord {
            let now = Date().timeIntervalSince1970
            return AgentState.RunRecord(runID: AgentRegistry.newRunID(now: now), dispatchedAt: now,
                                        prNumber: number, kind: "review", label: "Review · #\(number)")
        }
        let booked = record(10)
        check("with room a run is booked",
              AgentRegistry.createRun(booked, prompt: "p") != nil
                  && AgentRegistry.load().map(\.prNumber) == [10],
              "\(AgentRegistry.load().map(\.prNumber))")
        // One whose agent has gone, for a tick to retire: a pid the pinned process table
        // does not hold, past the spawn grace, with a ledger entry its retirement prices.
        let ended = AgentState.RunRecord(
            runID: AgentRegistry.newRunID(now: Date().timeIntervalSince1970),
            dispatchedAt: Date().timeIntervalSince1970 - 600, prNumber: 12, kind: "review",
            label: "Auto · Review · #12", ledgerKey: "lowdisk-12", pid: 4242)
        check("…and one whose agent has ended", AgentRegistry.createRun(ended, prompt: "p") != nil)
        check("fill for the run book", volume("fill") == 0)
        let refused = record(11)
        let bookRefuses = AgentRegistry.createRun(refused, prompt: "p") == nil
        check("on a full volume the run is refused", bookRefuses)
        check("…the book holds only the runs before it",
              AgentRegistry.load().map(\.prNumber) == [10, 12],
              "\(AgentRegistry.load().map(\.prNumber))")
        check("…and no run directory is left",
              !fm.fileExists(atPath: AgentRegistry.runDir(refused.runID).path))
        check("a run the book cannot drop is not forgotten",
              AgentRegistry.forget([booked.runID]) == .failed)
        check("…it stays booked", AgentRegistry.load().map(\.runID).contains(booked.runID))
        check("…with the prompt that prices it",
              fm.fileExists(atPath: AgentRegistry.promptPath(booked.runID).path))

        func said(_ action: String) -> Int {
            AuditLog.read().filter { $0.action == action && $0.detail.hasPrefix(ended.label) }.count
        }
        func priced() -> Int {
            ((try? String(contentsOf: TelemetryLog.url, encoding: .utf8)) ?? "")
                .split(separator: "\n")
                .filter { $0.contains(#""ev":"done""#) && $0.contains(ended.ledgerKey) }.count
        }
        func whole() -> Bool {
            AgentRegistry.load().contains { $0.runID == ended.runID }
                && fm.fileExists(atPath: AgentRegistry.promptPath(ended.runID).path)
        }
        func bookLines(_ action: String) -> Int {
            AuditLog.read().filter { $0.action == action && $0.detail.contains("run book") }.count
        }
        _ = await store.settleAgents()
        _ = await store.settleAgents()
        check("a tick that cannot retire an ended run leaves it whole", whole())
        check("…logging, auditing and pricing nothing for it, however many ticks",
              said("retire") + said("spawn-failed") == 0 && priced() == 0,
              "retire \(said("retire")), spawn-failed \(said("spawn-failed")), priced \(priced())")
        check("…and the feed says once that the book is not saving", bookLines("warn") == 1,
              "\(bookLines("warn"))")
        store.forgetRun(ended.runID)
        // A ✕ that went through would price the run from a task of its own.
        for _ in 0..<20 where priced() == 0 { try? await Task.sleep(nanoseconds: 100_000_000) }
        _ = await store.settleAgents()
        check("a ✕ the book cannot take leaves the run whole and prices nothing",
              whole() && priced() == 0, "priced \(priced())")

        // "Execute now" goes past the floor to the spawn, which the full book refuses
        // before a terminal opens.
        _ = await store.dispatchAgent(job(13), source: .auto)
        store.commitQueue()
        check("a job the disk holds is queued", store.queuedTasks.contains { $0.id == "review-req:13" })
        store.error = nil
        if bookRefuses { await store.executeQueuedTask("review-req:13") }
        check("execute now goes past the disk floor",
              store.error?.contains("failed to spawn") == true, store.error ?? "no error")

        check("drain for the run book", volume("drain") == 0)
        check("with room it is forgotten", AgentRegistry.forget([booked.runID]) == .written
                  && AgentRegistry.load().map(\.runID) == [ended.runID])
        _ = await store.settleAgents()
        check("with room the ended run is retired once, and priced once",
              said("retire") == 1 && priced() == 1 && AgentRegistry.load().isEmpty,
              "retire \(said("retire")), priced \(priced()), book \(AgentRegistry.load().count)")
        check("…and the feed says once that the book saves again",
              bookLines("poll-recovered") == 1, "\(bookLines("poll-recovered"))")
        setenv("DIPLOMAT_AGENTS_DIR", scratch.appendingPathComponent("agents").path, 1)

        // MARK: the 06:00 updater

        let origin = scratch.appendingPathComponent("origin.git")
        let seed = scratch.appendingPathComponent("seed")
        let checkout = vol.appendingPathComponent("checkout")
        let ident = ["-c", "user.name=lowdisk", "-c", "user.email=lowdisk@localhost"]
        func git(_ args: [String], in dir: URL) -> String {
            run(["/usr/bin/git", "-C", dir.path] + args).out
        }
        func commit(_ readme: String) {
            try? readme.write(to: seed.appendingPathComponent("README"), atomically: true,
                              encoding: .utf8)
            _ = git(["add", "-A"], in: seed)
            _ = git(ident + ["commit", "-qm", readme], in: seed)
            _ = git(["push", "-q", "origin", "HEAD:main"], in: seed)
        }
        _ = run(["/usr/bin/git", "init", "-q", "--bare", "-b", "main", origin.path])
        _ = run(["/usr/bin/git", "init", "-q", "-b", "main", seed.path])
        _ = git(["remote", "add", "origin", origin.path], in: seed)
        // What `RepoPaths.checkoutPresent` asks of a checkout.
        for pkg in ["packages/szpontnet-core/szpontnet", "packages/diplomat-runtime/diplomat_runtime"] {
            let dir = seed.appendingPathComponent(pkg)
            try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
            fm.createFile(atPath: dir.appendingPathComponent("__init__.py").path, contents: Data())
        }
        commit("v1")
        _ = run(["/usr/bin/git", "clone", "-q", origin.path, checkout.path])
        commit("v2")
        let upstreamHead = git(["rev-parse", "HEAD"], in: seed)
        setenv("DIPLOMAT_SELF_REPO", checkout.path, 1)

        final class Job {
            var rebuilds = 0, launches = 0
            var lines: [String] = []
            var host: SelfUpdate.Host {
                var h = SelfUpdate.Host()
                h.rebuild = { self.rebuilds += 1 }
                h.appRunning = { false }
                h.operatorQuit = { false }
                h.launch = { self.launches += 1 }
                h.log = { self.lines.append($0) }
                return h
            }
        }
        let before = git(["rev-parse", "HEAD"], in: checkout)
        func readme() -> String {
            (try? String(contentsOf: checkout.appendingPathComponent("README"), encoding: .utf8)) ?? ""
        }
        func untouched(_ name: String, _ j: Job, code: Int32) {
            check("\(name): HEAD did not move", git(["rev-parse", "HEAD"], in: checkout) == before)
            check("\(name): the files did not change", readme() == "v1", readme())
            check("\(name): nothing was rebuilt", j.rebuilds == 0)
            let skip = j.lines.first { $0.hasPrefix("skip: ") } ?? ""
            check("\(name): the log says it is the disk",
                  skip.hasPrefix("skip: ") && skip.contains("free on \(checkout.path), below the"),
                  "\(j.lines)")
            check("\(name): the dead app is still brought back", j.launches == 1 && code == 0,
                  "launches \(j.launches), exit \(code)")
        }

        setFloor((drained >> 20) + 64)
        do {
            let j = Job()
            let code = SelfUpdate.runScheduled(j.host)
            untouched("floor above the volume", j, code: code)
        }
        setFloor(floorMiB)
        check("fill for the updater", volume("fill") == 0)
        do {
            let j = Job()
            let code = SelfUpdate.runScheduled(j.host)
            untouched("a full volume", j, code: code)
        }
        check("drain for the updater", volume("drain") == 0)
        do {
            let j = Job()
            let code = SelfUpdate.runScheduled(j.host)
            check("with room, the same checkout merges",
                  git(["rev-parse", "HEAD"], in: checkout) == upstreamHead && readme() == "v2",
                  "\(j.lines)")
            check("…and rebuilds", j.rebuilds == 1 && code == 0, "\(j.lines)")
        }

        print(pass ? "\nLOWDISK TEST OK" : "\nLOWDISK TEST FAILED")
        return pass
    }

    /// Run `argv` to completion: its exit code and trimmed stdout.
    private static func run(_ argv: [String]) -> (code: Int32, out: String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: argv[0])
        p.arguments = Array(argv.dropFirst())
        let out = Pipe()
        p.standardOutput = out
        do { try p.run() } catch { return (-1, "") }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return (p.terminationStatus,
                String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines))
    }
}
