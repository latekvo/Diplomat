import Foundation
import DiplomatCore

/// Headless self-test of the free-disk floor against a real filesystem, driven by
/// `DIPLOMAT_LOWDISK_TEST=1`.
///
/// `scripts/full-volume.sh` mounts a small APFS image, and both holds are driven
/// through their real entry points with the work pointed at it: `dispatchAgent` with
/// the agents' repo root on the volume, and `SelfUpdate.runScheduled` with a scratch
/// checkout there that is one commit behind its origin. Free space is read by the
/// real probe; only the floor is set, through `DIPLOMAT_MIN_FREE_DISK_MIB`, relative
/// to what the volume has, so it can stand above or below it.
///
/// It never spawns: a booked agent holds the only bay, re-booked before every offer so
/// it is never past the spawn grace that would free it, and a dispatch the disk lets
/// through stops at the cap. The feed, the run book, the config file and the process
/// table are scratch or pinned, as in `QueueTest`; the updater's build and launch are
/// fixtures, its fetch and merge are real git against the scratch origin.
///
///   DIPLOMAT_LOWDISK_TEST=1 swift run Diplomat
///
/// A machine that cannot mount the volume skips (exit 0), unless
/// `DIPLOMAT_FULL_VOLUME_REQUIRED=1`, where the script fails and so does this.
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
        check("fill for the run book", volume("fill") == 0)
        let refused = record(11)
        check("on a full volume the run is refused", AgentRegistry.createRun(refused, prompt: "p") == nil)
        check("…the book holds only the run before it", AgentRegistry.load().map(\.prNumber) == [10],
              "\(AgentRegistry.load().map(\.prNumber))")
        check("…and no run directory is left",
              !fm.fileExists(atPath: AgentRegistry.runDir(refused.runID).path))
        check("a run the book cannot drop is not forgotten", !AgentRegistry.forget([booked.runID]))
        check("…it stays booked", AgentRegistry.load().map(\.runID) == [booked.runID])
        check("…with the prompt that prices it",
              fm.fileExists(atPath: AgentRegistry.promptPath(booked.runID).path))
        check("drain for the run book", volume("drain") == 0)
        check("with room it is forgotten", AgentRegistry.forget([booked.runID])
                  && AgentRegistry.load().isEmpty)
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
