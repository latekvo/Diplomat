import Foundation
import DiplomatCore

/// Writing and reading `~/.diplomat/pr-monitor/telemetry.jsonl` — the macOS twin of
/// the IO half of the shared runtime's `telemetry.py`. The arithmetic over what this
/// records is shared (`DiplomatCore.Telemetry`); this file only decides when a line
/// is appended and how the file is kept from growing forever.
///
/// One append-only file, one JSON object per line, opened `O_APPEND` like the
/// activity feed so this applet, its Linux counterpart and a mesh node can all
/// append without clobbering each other.
enum TelemetryLog {

    // MARK: - Location + shared knobs

    /// The same directory the activity feed lives in — one place for everything the
    /// monitors write, and one redirect to fence off in a test.
    private static var dir: URL { AuditLog.dir }

    private static var model: CoreAssets.TelemetryModel? { try? CoreAssets.telemetry() }

    static var url: URL {
        dir.appendingPathComponent(model?.ledgerFile ?? "telemetry.jsonl")
    }

    /// How often a sample is written. From the shared model so the two platforms
    /// can't drift on ledger growth.
    static var sampleInterval: TimeInterval { model?.sampleIntervalSecs ?? 900 }
    private static var retainDays: Double { model?.retainDays ?? 60 }
    private static var maxBytes: Int { model?.maxLedgerBytes ?? 4 * 1024 * 1024 }

    // MARK: - Append

    /// Append one event. Best-effort and never throwing: a monitor poll must not
    /// fail over bookkeeping.
    static func append(_ event: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: event,
                                                     options: [.sortedKeys]),
              var line = String(data: data, encoding: .utf8) else { return }
        line += "\n"
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        rotateIfLarge()
        // O_APPEND: the kernel appends atomically, so a concurrent append from a
        // mesh node (or the Linux applet over a shared home) can't be overwritten;
        // O_CREAT closes the create/exists race too. Same idiom as AuditLog.
        let fd = open(url.path, O_WRONLY | O_APPEND | O_CREAT, 0o644)
        guard fd >= 0 else { return }
        defer { close(fd) }
        _ = line.data(using: .utf8)?.withUnsafeBytes { buf in
            write(fd, buf.baseAddress, buf.count)
        }
    }

    /// Rewrite the ledger to the retention horizon once it outgrows the cap.
    ///
    /// Not a truncate: the file is the only record of what was owed and when, so
    /// the rewrite keeps every event inside `retainDays` — the longest lookback the
    /// screen offers — and drops only what no range can reach.
    private static func rotateIfLarge() {
        let fm = FileManager.default
        guard let attrs = try? fm.attributesOfItem(atPath: url.path),
              let size = attrs[.size] as? Int, size > maxBytes,
              let text = try? String(contentsOf: url, encoding: .utf8) else { return }
        let cutoff = Date().timeIntervalSince1970 - retainDays * 86_400
        let kept = text.split(separator: "\n", omittingEmptySubsequences: true).filter { line in
            guard let data = line.data(using: .utf8),
                  let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  let at = (obj["at"] as? NSNumber)?.doubleValue else { return false }
            return at >= cutoff
        }
        let tmp = url.appendingPathExtension("tmp")
        guard (try? (kept.joined(separator: "\n") + "\n").write(to: tmp, atomically: true,
                                                                encoding: .utf8)) != nil
        else { return }
        _ = try? fm.replaceItemAt(url, withItemAt: tmp)
    }

    // MARK: - Recording (what the monitors call)

    static func queued(key: String, duty: String, pr: Int) {
        append(["at": Date().timeIntervalSince1970, "ev": "queued",
                "key": key, "duty": duty, "pr": pr])
    }

    static func started(key: String, remote: Bool, attempt: Int) {
        append(["at": Date().timeIntervalSince1970, "ev": "started",
                "key": key, "remote": remote, "attempt": attempt])
    }

    static func cleared(key: String) {
        append(["at": Date().timeIntervalSince1970, "ev": "cleared", "key": key])
    }

    /// Record a completion at `at` — the sentinel file's mtime, i.e. when the agent
    /// actually exited, not when the sweep noticed (which is up to a poll period
    /// later and would inflate every run time).
    ///
    /// `runner` is which CLI spent the tokens, and it is what keeps the rate-limit
    /// percentages honest: an OpenCode or Hermes task is billed by whichever provider
    /// that runner is logged into, so its tokens are worth reporting per task but not
    /// against a window they never drew on (`Telemetry.Task.anthropic`).
    ///
    /// `usd`/`model` are the other unit that same task can be priced in, for a runner
    /// billed in money rather than out of a rate-limit window: what the provider
    /// charged, and the model whose rates it was charged at. The pair travels together
    /// because neither is worth anything alone — the same task costs cents on one model
    /// and dollars on another, so a mean taken across models would price nothing that
    /// ever ran (`Telemetry.Summary.perTaskUsd`).
    static func done(key: String, at: TimeInterval, tokens: Double?, runner: String,
                     usd: Double? = nil, model: String = "") {
        var event: [String: Any] = ["at": at, "ev": "done", "key": key]
        if let tokens { event["tokens"] = tokens }
        if !runner.isEmpty { event["runner"] = runner }
        if let usd { event["usd"] = usd }
        if !model.isEmpty { event["model"] = model }
        append(event)
    }

    static func sample(sessionLeft: Double?, weekLeft: Double?,
                       repoTokens: Double, otherTokens: Double) {
        append(["at": Date().timeIntervalSince1970, "ev": "sample",
                "sessionLeft": sessionLeft as Any? ?? NSNull(),
                "weekLeft": weekLeft as Any? ?? NSNull(),
                "repoTokens": repoTokens, "otherTokens": otherTokens])
    }

    // MARK: - Reading

    /// How much of the ledger's tail is read. Two months of 15-minute samples plus
    /// the work events run well under a megabyte; the cap is the backstop for a
    /// file that grew before a rotation could run.
    private static let readTailBytes = 4 * 1024 * 1024

    /// The ledger's tail as raw lines. A mid-file start lands mid-line, so the
    /// partial first line is dropped before decoding — same reason `AuditLog.read`
    /// does it.
    static func readLines() -> [String] {
        guard let handle = try? FileHandle(forReadingFrom: url) else { return [] }
        defer { try? handle.close() }
        guard let end = try? handle.seekToEnd() else { return [] }
        let start = end > UInt64(readTailBytes) ? end - UInt64(readTailBytes) : 0
        try? handle.seek(toOffset: start)
        guard var data = try? handle.readToEnd() else { return [] }
        if start > 0, let nl = data.firstIndex(of: 0x0A) {
            data = data[data.index(after: nl)...]
        }
        guard let text = String(data: data, encoding: .utf8) else { return [] }
        return text.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
    }

    /// The last fold, keyed by the ledger's (mtime, size). Both the 3-minute poll
    /// and the screen fold the same file, and a repaint must not re-parse a
    /// megabyte — while any append changes both parts of the key, so a stale fold
    /// is impossible.
    private static var foldCache: (key: String, ledger: Telemetry.Ledger)?

    static func load() -> Telemetry.Ledger {
        let attrs = try? FileManager.default.attributesOfItem(atPath: url.path)
        let mtime = (attrs?[.modificationDate] as? Date)?.timeIntervalSince1970 ?? 0
        let size = (attrs?[.size] as? Int) ?? 0
        let key = "\(mtime)/\(size)"
        if let cached = foldCache, cached.key == key { return cached.ledger }
        let ledger = Telemetry.fold(lines: readLines())
        foldCache = (key, ledger)
        return ledger
    }

    /// Whether it is time for another quota/token sample. Driven off the ledger's
    /// own last sample rather than a timer, so an applet that restarts every few
    /// minutes doesn't sample every launch.
    static func sampleDue(now: Date = Date()) -> Bool {
        guard let last = load().samples.last else { return true }
        return now.timeIntervalSince1970 - last.at >= sampleInterval
    }

    /// Reconcile one poll's owed set against the ledger: record work newly seen as
    /// owed, and clear work that stopped being owed before anyone started it.
    ///
    /// `kind` scopes the sweep to the keys THIS poll is authoritative about, and
    /// `duty` is the coarser bucket the screen charts. The two differ for reviews,
    /// which arrive from two independent polls: replies owed on my own PRs
    /// (`review-reply`) and reviews requested of me (`review`). Both chart as
    /// "reviews", but scoping the sweep by duty alone would make each poll clear
    /// the other's pending work.
    ///
    /// Clearing is deliberately limited to work no agent ever took: an item that
    /// was started has an outcome of its own, and marking it cleared as well would
    /// make it look like the monitor dropped work it actually did.
    static func observeOwed(kind: String, duty: String, owed: [String: Int]) {
        let prefix = kind + ":"
        let ledger = load()
        let known = Set(ledger.tasks.filter { $0.key.hasPrefix(prefix) }.map(\.key))
        for (key, pr) in owed.sorted(by: { $0.key < $1.key }) where !key.isEmpty && !known.contains(key) {
            queued(key: key, duty: duty, pr: pr)
        }
        let stale = ledger.tasks.filter {
            $0.key.hasPrefix(prefix) && $0.queuedAt != nil
                && $0.startedAt == nil && $0.clearedAt == nil
        }.map(\.key)
        for key in stale.sorted() where owed[key] == nil {
            cleared(key: key)
        }
    }
}
