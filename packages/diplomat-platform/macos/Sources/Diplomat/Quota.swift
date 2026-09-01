import Foundation

/// How much of the Claude rate-limit windows is left — the other half of the
/// telemetry sample, and the macOS twin of the shared runtime's `quota.py`.
///
/// A GET against the OAuth usage endpoint (the same data Claude Code's `/usage`
/// screen shows) using the OAuth access token Claude Code already holds, converted
/// to the fraction of each window still unspent. The endpoint's budget is per account
/// and every Claude Code session on the machine spends it, so a caller that can afford
/// to wait retries the refusals (`insist`). That, paired with the token
/// counters from `UsageScan`, is what lets the Telemetry screen say a task cost a
/// *share of the limit* rather than an unanchored token count: Anthropic publishes a
/// utilization percentage and never a token budget, and the budget is dynamic, so
/// the window has to be priced from what actually happened
/// (`Telemetry.calibrate`).
///
/// This is deliberately Diplomat's own probe and not a call into the mesh library's
/// `szpontnet.usage`. The mesh is an optional add-on — the applet ships and runs with
/// the SzpontNet packages deleted outright, and CI proves it — so a screen that
/// reached through the mesh for its numbers would be a screen that blanks on exactly
/// the machines least likely to have it.
///
/// `DIPLOMAT_QUOTA_PROBE=0` disables it (the self-tests run offline and
/// deterministic); `DIPLOMAT_CLAUDE_DIR` moves where the credentials are read from.
enum Quota {

    private static let usageURL = URL(string: "https://api.anthropic.com/api/oauth/usage")!
    private static let beta = "oauth-2025-04-20"
    private static let timeoutSecs: TimeInterval = 4

    /// How old a reading may be before a caller probes again rather than taking it.
    /// The sample cadence is already 15 minutes, so this only guards a panel that
    /// opens repeatedly. An insisting caller's retries are inside the probe this
    /// gates, and pace themselves.
    private static let ttlSecs: TimeInterval = 55
    /// Faster retry while there is no good reading yet, so a transient failure at
    /// startup doesn't leave the screen blank for a full TTL.
    private static let retrySecs: TimeInterval = 10
    /// The extra attempts an *insisting* caller makes once the first is refused, and
    /// the wait between them: six attempts over two and a half minutes.
    ///
    /// On a machine running several Claude Code sessions a single attempt is refused
    /// (HTTP 429) more often than it succeeds, which is what leaves most of the
    /// telemetry ledger's readings missing and the quota chart in fragments. The bucket
    /// was seen refilling on a roughly two-minute cycle, so the waits are even rather
    /// than doubling: what decides whether an attempt lands is how soon after a refill
    /// it arrives.
    private static let insistAttempts = 5
    private static let insistWaitSecs: TimeInterval = 30
    /// How long a last-good reading keeps answering through failures before the probe
    /// admits it doesn't know. A sample carrying a stale fraction would price the
    /// window against tokens that were spent after it, so this is deliberately short
    /// relative to how long a window runs.
    private static let keepSecs: TimeInterval = 1800

    private struct Cache {
        var attempt: TimeInterval = 0
        var good: TimeInterval = 0
        var session: Double?
        var week: Double?
    }
    private static var cache = Cache()
    private static let lock = NSLock()

    /// How many probe rounds have been made, and how many came back with a reading.
    /// `AgentDispatchGate.budgetDecide` SKIPS a ceiling with no reading and calls one
    /// with none affordable, so a probe that stops answering does not gate less — it
    /// stops gating, and nothing else on the machine looks wrong. Hence the ratio.
    private static var probeRounds = 0
    private static var probeReadings = 0

    /// `(probe rounds made, rounds that came back with a reading)`.
    static func probeStats() -> (rounds: Int, readings: Int) {
        lock.lock(); defer { lock.unlock() }
        return (probeRounds, probeReadings)
    }

    static var probeEnabled: Bool {
        ProcessInfo.processInfo.environment["DIPLOMAT_QUOTA_PROBE"] != "0"
    }

    // MARK: - Credentials

    /// Claude Code's OAuth access tokens to try, in order: the credentials file
    /// first, then the login Keychain (where Claude Code puts it on macOS). Re-read
    /// per probe because Claude Code refreshes them as it runs.
    ///
    /// A LIST rather than the first one found, because the two sources drift apart
    /// and the file is the one that goes stale: Claude Code refreshes the Keychain
    /// item and never rewrites a `.credentials.json` an older login left behind.
    /// Asking only the file pins the probe to a dead credential for as long as that
    /// file exists, which is not a loud failure — `AgentDispatchGate.budgetDecide`
    /// skips a ceiling it cannot read, so the dispatch budget just stops gating. Four
    /// days of it here, ending in a night of agents dispatched into an exhausted
    /// weekly window.
    ///
    /// The file stays FIRST — same order as the Linux twin — so that pointing
    /// `DIPLOMAT_CLAUDE_DIR` at a fixture directory decides the probe's answer rather
    /// than being shadowed by whatever the real login Keychain happens to hold.
    static func oauthTokens() -> [String] {
        var out: [String] = []
        let url = UsageScan.claudeDir.appendingPathComponent(".credentials.json")
        if let data = try? Data(contentsOf: url), let token = accessToken(data) {
            out.append(token)
        }
        if let token = keychainToken(), !out.contains(token) { out.append(token) }
        return out
    }

    /// The `claudeAiOauth.accessToken` inside a credentials blob, whether it came
    /// from the file or the Keychain — both hold the same JSON.
    private static func accessToken(_ data: Data) -> String? {
        guard let raw = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let oauth = raw["claudeAiOauth"] as? [String: Any],
              let token = oauth["accessToken"] as? String, !token.isEmpty else { return nil }
        return token
    }

    /// Stand in for what this machine's login Keychain holds — for a self-test whose
    /// fixture controls the credentials directory but not the box it is running on.
    ///
    /// `DIPLOMAT_CLAUDE_DIR` decides the FILE half of the candidate list and nothing
    /// else, so an assertion about the order and the dedup would be decided by the box:
    /// a developer's Mac has a real `Claude Code-credentials` item and a CI runner has
    /// none, and neither agrees with the fixture.
    ///
    /// Headless-gated like every other pin: left set in a live applet the probe would
    /// spend its requests on a fixture token forever.
    ///
    /// Read WITHOUT `lock`, and so written without it: the reader is `oauthTokens`,
    /// which runs inside `attempt` with `lock` already held, and `NSLock` does not
    /// recurse. A self-test sets this once before it probes at all, which is the only
    /// ordering either side needs.
    private static var pinnedKeychain: String??
    static func pinKeychain(_ token: String?) {
        guard Headless.active else { return }
        pinnedKeychain = .some(token)
    }

    /// Read the item through `security` rather than the Keychain API: the API call
    /// from an unsigned or re-signed binary trips an authorization prompt for an item
    /// another app owns, and a modal appearing behind a background poll is worse than
    /// no reading at all.
    private static func keychainToken() -> String? {
        if let pinned = pinnedKeychain { return pinned }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/security")
        p.arguments = ["find-generic-password", "-s", "Claude Code-credentials", "-w"]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = Pipe()
        do { try p.run() } catch { return nil }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        guard p.terminationStatus == 0 else { return nil }
        return accessToken(data)
    }

    // MARK: - The probe

    /// One GET with one token. nil on any failure — offline, a 401 after the token
    /// expired mid-window, or a body that isn't an object. Blocking: it runs on the
    /// same background task as the sample it feeds.
    private static func fetch(_ token: String) -> [String: Any]? {
        var req = URLRequest(url: usageURL, timeoutInterval: timeoutSecs)
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        req.setValue(beta, forHTTPHeaderField: "anthropic-beta")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var payload: [String: Any]?
        let done = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { data, _, _ in
            defer { done.signal() }
            guard let data else { return }
            payload = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        }.resume()
        // The request already carries its own timeout; the wait is bounded a little
        // wider so a poll can never park here forever if the task never calls back.
        _ = done.wait(timeout: .now() + timeoutSecs + 2)
        return payload
    }

    /// The unspent fraction of one window from its `utilization` percent. Clamped to
    /// [0, 1]: utilization can exceed 100 during a burst, and a negative fraction
    /// would show up as a negative task cost.
    private static func fractionLeft(_ window: Any?) -> Double? {
        guard let window = window as? [String: Any],
              let util = window["utilization"] as? NSNumber,
              // A JSON `true` decodes to an NSNumber worth 1, which would read as a
              // 99%-unspent window; reject it as the malformed body it is.
              CFGetTypeID(util as CFTypeRef) != CFBooleanGetTypeID() else { return nil }
        let left = max(0, min(1, 1 - util.doubleValue / 100))
        return (left * 10_000).rounded() / 10_000
    }

    /// One round of the probe, folded into the cache. True when it came back with a
    /// reading. Called with `lock` held.
    ///
    /// Every credential is tried until one answers: "the token was refused" and "the
    /// account has no reading" are the same silence to every caller above, and only
    /// this loop can tell them apart. It stops at the first that yields a window, so
    /// the extra request is spent only where the probe was already failing.
    private static func attempt(_ now: TimeInterval) -> Bool {
        cache.attempt = now
        let tokens = oauthTokens()
        // Nothing to ask with: not a round, and not a refusal. The warning this feeds
        // names what it measured — asked and not answered — and a logged-out machine
        // did not ask.
        if tokens.isEmpty { return false }
        probeRounds += 1
        for token in tokens {
            let payload = fetch(token)
            guard let session = fractionLeft(payload?["five_hour"]) else { continue }
            cache.good = now
            cache.session = session
            cache.week = fractionLeft(payload?["seven_day"])
            probeReadings += 1
            return true
        }
        return false
    }

    /// `(session, week)` — the unspent fraction of the 5-hour and 7-day windows, or
    /// `(nil, nil)` when unavailable (probe disabled, no credentials, or offline past
    /// the keep window). Never throws.
    ///
    /// `insist` keeps trying (`insistAttempts`) rather than settling for one refused
    /// attempt, so the call blocks for up to two and a half minutes. It is for a caller
    /// with its own long cadence and nobody waiting on it — the telemetry sample, which
    /// gets one turn every 15 minutes and leaves a hole in the ledger if it comes back
    /// empty. A caller gating a dispatch takes the single attempt: a stale reading now
    /// beats a fresh one after the agent should have started.
    static func fractionsLeft(insist: Bool = false) -> (session: Double?, week: Double?) {
        guard probeEnabled else { return (nil, nil) }
        lock.lock()
        defer { lock.unlock() }
        var now = Date().timeIntervalSinceReferenceDate
        let interval = cache.session != nil ? ttlSecs : retrySecs
        if cache.attempt == 0 || now - cache.attempt >= interval {
            // No token is the one failure retrying cannot fix.
            if !attempt(now), insist, !oauthTokens().isEmpty {
                for _ in 0..<insistAttempts {
                    // The lock is dropped across the wait: `cache.attempt` is already
                    // stamped, so a dispatch asking meanwhile reads the cache instead
                    // of queueing behind the retries.
                    lock.unlock()
                    Thread.sleep(forTimeInterval: insistWaitSecs)
                    lock.lock()
                    if attempt(Date().timeIntervalSinceReferenceDate) { break }
                }
            }
            now = Date().timeIntervalSinceReferenceDate
        }
        if cache.session != nil, now - cache.good > keepSecs {
            cache.session = nil   // stale beyond trust
            cache.week = nil
        }
        return (cache.session, cache.week)
    }

    /// Self-test hook: forget any cached reading, and any memory of having probed.
    static func resetCache() {
        lock.lock()
        defer { lock.unlock() }
        cache = Cache()
        probeRounds = 0
        probeReadings = 0
    }
}
