import Foundation

/// How many dollars are left to spend on OpenRouter — the other currency a task can
/// be priced in, and the macOS twin of the shared runtime's `spend.py`.
///
/// `Quota` asks Anthropic what *share of a rate-limit window* is unspent, which is the
/// only figure that account publishes. An OpenRouter account has no window: it has
/// money, and two ceilings that money runs out against.
///
/// * the **key limit** — the cap set on the API key itself (`limit`), which resets on
///   a period the account chose, and which is what a key provisioned for automation is
///   usually held to;
/// * the **credit balance** — what was bought minus what has been spent, account-wide,
///   which does not reset at all.
///
/// Either can be the one that stops work, so both are read and the gate takes the
/// tighter (`AutoBudget.decide`). A machine whose key is uncapped has only the second;
/// both are optional and a missing one is skipped rather than guessed.
///
/// The key is read from the runner's own store, never from Diplomat's config: Hermes
/// keeps its providers' credentials in `~/.hermes/.env`, which is exactly where
/// `AgentRunner` says a secret belongs. Read per probe, because the operator can
/// rotate it under a running applet.
///
/// `DIPLOMAT_SPEND_PROBE=0` disables it (the self-tests run offline and
/// deterministic); `DIPLOMAT_HERMES_ENV` moves where the key is read from.
enum Spend {

    private static let keyURL = URL(string: "https://openrouter.ai/api/v1/key")!
    private static let creditsURL = URL(string: "https://openrouter.ai/api/v1/credits")!
    private static let timeoutSecs: TimeInterval = 4

    /// Minimum gap between endpoint attempts, and the faster retry used while there is
    /// no good reading yet — both for the reasons `Quota` gives.
    private static let ttlSecs: TimeInterval = 55
    private static let retrySecs: TimeInterval = 10
    /// How long a last-good reading keeps answering through failures. Shorter than the
    /// dollars it reports can plausibly be spent, so a stale balance can't wave through
    /// work the account can no longer pay for.
    private static let keepSecs: TimeInterval = 1800

    /// Dollars left on each ceiling, or nil for one this account doesn't have (an
    /// uncapped key) or that could not be read.
    struct Balance: Equatable {
        var keyLeft: Double?
        var creditLeft: Double?

        var known: Bool { keyLeft != nil || creditLeft != nil }
    }

    private struct Cache {
        var attempt: TimeInterval = 0
        var good: TimeInterval = 0
        var reading: Balance?
    }
    private static var cache = Cache()
    private static let lock = NSLock()

    static var probeEnabled: Bool {
        ProcessInfo.processInfo.environment["DIPLOMAT_SPEND_PROBE"] != "0"
    }

    // MARK: - Credentials

    /// Hermes' provider environment file, where its OpenRouter key is written.
    static var envURL: URL {
        if let env = ProcessInfo.processInfo.environment["DIPLOMAT_HERMES_ENV"], !env.isEmpty {
            return URL(fileURLWithPath: (env as NSString).expandingTildeInPath)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".hermes/.env")
    }

    /// The OpenRouter API key: Hermes' own env file, else this process's environment.
    ///
    /// The file wins because it is the one the *agent* will be billed through — a stale
    /// key exported into the applet's shell would otherwise price a task against an
    /// account the run never touches. Values may be quoted, as any env file's may.
    static func apiKey() -> String? {
        if let text = try? String(contentsOf: envURL, encoding: .utf8) {
            for line in text.split(whereSeparator: \.isNewline) {
                let trimmed = line.trimmingCharacters(in: .whitespaces)
                guard !trimmed.hasPrefix("#"),
                      let eq = trimmed.firstIndex(of: "=") else { continue }
                guard trimmed[trimmed.startIndex..<eq]
                    .trimmingCharacters(in: .whitespaces) == "OPENROUTER_API_KEY" else { continue }
                var value = trimmed[trimmed.index(after: eq)...]
                    .trimmingCharacters(in: .whitespaces)
                if value.count >= 2, let quote = value.first, quote == "\"" || quote == "'",
                   value.last == quote {
                    value = String(value.dropFirst().dropLast())
                }
                if !value.isEmpty { return value }
            }
        }
        let env = ProcessInfo.processInfo.environment["OPENROUTER_API_KEY"]
        return (env?.isEmpty ?? true) ? nil : env
    }

    // MARK: - The probe

    /// One GET, unwrapped from OpenRouter's `{"data": …}` envelope. nil on any failure —
    /// no network, a 401 after the key was rotated, a body that isn't an object.
    /// Blocking, like `Quota.fetch`: it runs on the background task that asked.
    private static func get(_ url: URL, key: String) -> [String: Any]? {
        var req = URLRequest(url: url, timeoutInterval: timeoutSecs)
        req.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var payload: [String: Any]?
        let done = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { data, _, _ in
            defer { done.signal() }
            guard let data else { return }
            payload = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        }.resume()
        _ = done.wait(timeout: .now() + timeoutSecs + 2)
        return payload?["data"] as? [String: Any]
    }

    /// A dollar figure from the payload, or nil for anything that isn't one. Negatives
    /// are dropped rather than clamped: an account that reports owing money has told us
    /// something this gate has no reading for, and pricing it as "zero left" would be a
    /// guess wearing a measurement's clothes.
    private static func money(_ raw: Any?) -> Double? {
        guard let n = raw as? NSNumber,
              CFGetTypeID(n as CFTypeRef) != CFBooleanGetTypeID() else { return nil }
        return n.doubleValue >= 0 ? n.doubleValue : nil
    }

    /// Both ceilings in one probe. nil when neither endpoint answered, which is what
    /// keeps a previous good reading in service through a blip.
    private static func fetch() -> Balance? {
        guard let key = apiKey() else { return nil }
        // `limit_remaining` is null for an uncapped key — a real answer meaning "this
        // ceiling does not exist", not a failed read, so it does not fail the probe.
        let keyData = get(keyURL, key: key)
        let credits = get(creditsURL, key: key)
        if keyData == nil, credits == nil { return nil }
        var creditLeft: Double?
        if let credits, let total = money(credits["total_credits"]),
           let used = money(credits["total_usage"]) {
            creditLeft = max(0, total - used)
        }
        return Balance(keyLeft: money(keyData?["limit_remaining"]), creditLeft: creditLeft)
    }

    /// Dollars left on each ceiling, or an empty `Balance` when unavailable (probe
    /// disabled, no key, or offline past the keep window). Never throws.
    static func balance() -> Balance {
        guard probeEnabled else { return Balance() }
        lock.lock()
        defer { lock.unlock() }
        let now = Date().timeIntervalSinceReferenceDate
        let interval = cache.reading != nil ? ttlSecs : retrySecs
        if cache.attempt == 0 || now - cache.attempt >= interval {
            cache.attempt = now
            if let reading = fetch(), reading.known {
                cache.good = now
                cache.reading = reading
            }
        }
        if cache.reading != nil, now - cache.good > keepSecs {
            cache.reading = nil   // stale beyond trust
        }
        return cache.reading ?? Balance()
    }

    /// Self-test hook: forget any cached reading.
    static func resetCache() {
        lock.lock()
        defer { lock.unlock() }
        cache = Cache()
    }
}
