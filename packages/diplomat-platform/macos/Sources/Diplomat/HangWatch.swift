import Darwin
import Foundation

/// Watches the main thread from a background queue: a ping is queued onto main every
/// `interval`, and one answered `threshold` or more late is logged as a hang, with how
/// much CPU the main thread burned meanwhile (busy computing, or waiting on a lock, an
/// Apple event or a subprocess) and what `AppLog.timed` work was in flight. A hang still
/// going after `sampleAfter` is logged then too, and `/usr/bin/sample` captures the
/// stacks into `diplomat-hangs/` beside the log - at most one per `sampleEvery`, newest
/// `keepSamples` kept. Every `heartbeat` a line sums up the period, so a quiet log
/// still shows the app was alive and watched.
///
/// A hang reads up to `interval` short: its ping may be queued after it began. Times are
/// uptime, which stops while the Mac sleeps, so a ping that spans a sleep is not a hang.
final class HangWatch: @unchecked Sendable {
    static let shared = HangWatch()

    var interval = 0.25
    var threshold = 0.5
    var sampleAfter = 2.0
    var sampleSeconds = 3
    var sampleEvery = 600.0
    var keepSamples = 20
    var heartbeat: Double? = 900

    private let queue = DispatchQueue(label: "com.ignacy.diplomat.hangwatch", qos: .userInteractive)
    private var timer: DispatchSourceTimer?
    private var mainThread: thread_act_t = 0
    private var started = 0.0

    // Confined to `queue`.
    private var pingSent: Double?
    private var cpuAtPing: Double?
    private var reported = false
    private var sampleFile: String?
    private var lastSample = -Double.infinity
    private var lastBeat = 0.0
    private var hangs = 0
    private var worst = 0.0

    /// Called on the main thread, which is the one it watches.
    func start() {
        precondition(Thread.isMainThread)
        guard timer == nil else { return }
        mainThread = pthread_mach_thread_np(pthread_self())
        started = AppLog.uptime
        lastBeat = started
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now(), repeating: interval, leeway: .milliseconds(50))
        t.setEventHandler { [weak self] in self?.tick() }
        timer = t
        t.resume()
    }

    private func tick() {
        let now = AppLog.uptime
        if let beat = heartbeat, now - lastBeat >= beat { logHeartbeat(now) }
        if let sent = pingSent {
            if !reported, now - sent >= sampleAfter {
                reported = true
                sampleFile = takeSample(now)
                AppLog.log("hang", "main thread unresponsive for \(AppLog.duration(now - sent)) so far; "
                    + "in flight: \(AppLog.inFlight())"
                    + (sampleFile.map { "; sampling to \($0)" } ?? ""))
            }
            return
        }
        pingSent = now
        cpuAtPing = mainCPU()
        DispatchQueue.main.async { [weak self] in
            let answered = AppLog.uptime
            self?.queue.async { self?.answered(at: answered) }
        }
    }

    private func answered(at: Double) {
        guard let sent = pingSent else { return }
        defer { pingSent = nil; reported = false; sampleFile = nil }
        let blocked = at - sent
        guard blocked >= threshold else { return }
        hangs += 1
        worst = max(worst, blocked)
        var line = "main thread blocked \(AppLog.duration(blocked))"
        if let before = cpuAtPing, let after = mainCPU() {
            let cpu = after - before
            let kind = cpu >= blocked * 0.7 ? "busy on the main thread"
                : cpu <= blocked * 0.2 ? "waiting, not computing" : "partly busy"
            line += ", main cpu \(AppLog.duration(cpu)) (\(kind))"
        }
        if !reported { line += "; in flight: \(AppLog.inFlight())" }
        if let sampleFile { line += "; sample \(sampleFile)" }
        AppLog.log("hang", line)
    }

    /// CPU seconds the main thread has used, user and system.
    private func mainCPU() -> Double? {
        var info = thread_basic_info()
        var count = mach_msg_type_number_t(MemoryLayout<thread_basic_info_data_t>.size / MemoryLayout<integer_t>.size)
        let kr = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                thread_info(mainThread, thread_flavor_t(THREAD_BASIC_INFO), $0, &count)
            }
        }
        guard kr == KERN_SUCCESS else { return nil }
        func secs(_ t: time_value_t) -> Double { Double(t.seconds) + Double(t.microseconds) / 1e6 }
        return secs(info.user_time) + secs(info.system_time)
    }

    var sampleDir: URL { AppLog.dir.appendingPathComponent("diplomat-hangs") }

    /// Starts `sample` on this process and returns the file it writes, or nil when one
    /// ran within `sampleEvery` or it could not start.
    private func takeSample(_ now: Double) -> String? {
        guard now - lastSample >= sampleEvery else { return nil }
        let fm = FileManager.default
        try? fm.createDirectory(at: sampleDir, withIntermediateDirectories: true)
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd'T'HH-mm-ss"
        let file = sampleDir.appendingPathComponent("\(f.string(from: Date()))-\(getpid()).txt").path
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/sample")
        p.arguments = ["\(getpid())", "\(sampleSeconds)", "-file", file]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch {
            AppLog.log("hang", "sample failed to start: \(error.localizedDescription)")
            return nil
        }
        lastSample = now
        let old = ((try? fm.contentsOfDirectory(atPath: sampleDir.path)) ?? []).sorted().dropLast(keepSamples)
        for name in old { try? fm.removeItem(at: sampleDir.appendingPathComponent(name)) }
        return file
    }

    private func logHeartbeat(_ now: Double) {
        lastBeat = now
        let timing = AppLog.drainTallies().sorted { $0.key < $1.key }.map { name, t in
            "\(name) \(t.count)x avg \(AppLog.duration(t.total / Double(t.count))) max \(AppLog.duration(t.worst))"
        }.joined(separator: ", ")
        AppLog.log("alive", "up \(AppLog.duration(now - started)), memory \(footprintMB())MB, "
            + "hangs \(hangs)" + (hangs > 0 ? " (worst \(AppLog.duration(worst)))" : "")
            + "; timing: \(timing.isEmpty ? "none" : timing); in flight: \(AppLog.inFlight())")
        hangs = 0
        worst = 0
    }

    /// Physical footprint, the figure Activity Monitor shows as Memory.
    private func footprintMB() -> Int {
        var info = task_vm_info_data_t()
        var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<integer_t>.size)
        let kr = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &count)
            }
        }
        return kr == KERN_SUCCESS ? Int(info.phys_footprint >> 20) : -1
    }
}
