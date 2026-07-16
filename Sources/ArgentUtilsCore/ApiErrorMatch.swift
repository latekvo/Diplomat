import Foundation

// Detects a Claude CLI API-error line in a terminal's recent output, so the watcher
// can auto-send a "continue" nudge to an agent that stalled on a transient server
// error (e.g. overnight overload). The CLI prints, e.g.:
//   ⏺ API Error: 529 Overloaded. This is a server-side issue, usually temporary —
//     try again in a moment. If it persists, check https://status.claude.com.
// Kept pure + in the shared core so it's unit-testable; the caller restricts the text
// it passes to the last few visible lines, which is what keeps this from firing on a
// session that merely mentions the phrase higher up.
public enum ApiErrorMatch {
    /// Connectivity failures that the CLI prints with NO status code — e.g.
    ///   "API Error: Unable to connect to API"
    ///   "API Error: Connection error."
    /// so a dropped/returning network resumes the agent just like a 5xx would.
    private static let connectivityPhrases = [
        "unable to connect", "connection error", "connection refused",
        "connection reset", "connection timed out", "network error",
        "fetch failed", "econnrefused", "enotfound", "etimedout", "getaddrinfo",
    ]

    /// Out-of-token-quota banners. The CLI prints these WITHOUT any "API Error"
    /// prefix — e.g.
    ///   "You've hit your weekly limit."  (the exact current phrasing)
    ///   "Claude usage limit reached. Your limit will reset at 4pm (Europe/Warsaw)."
    ///   "5-hour limit reached ∙ resets 6pm"
    /// These are detected only to be IGNORED: an out-of-quota agent can't make
    /// progress until its limit window resets, so auto-nudging it does nothing but
    /// churn (and spammed the audit log). A quota banner also SUPPRESSES a
    /// co-occurring API-error match in the same tail — the session idles on the
    /// limit, not the error.
    private static let quotaPhrases = [
        "usage limit reached",
        "hour limit reached",     // "5-hour limit reached ∙ resets …"
        "weekly limit reached",
        "session limit reached",
        "limit will reset at",    // "Your limit will reset at 4pm (…)"
        "out of tokens",
    ]
    /// "You've hit your weekly/usage/session/5-hour limit" — the "hit your … limit"
    /// family, matched with a small gap so new limit names keep matching.
    private static let hitYourLimitPattern = #"hit your [a-z0-9\- ]{0,16}limit"#

    /// True when `text` shows a transient Claude API error the watcher should nudge
    /// past — a server 5xx / rate-limit ("API Error: <3-digit code>"), a status-page
    /// error, or a codeless connectivity failure (network out, DNS, timeout).
    ///
    /// Out-of-quota banners return false: nudging a quota-limited session does nothing
    /// until the window resets, so the watcher intentionally leaves them alone. A quota
    /// banner also SUPPRESSES any API-error text in the same tail, since the session is
    /// idling on the limit rather than the error.
    public static func looksLikeApiError(_ text: String) -> Bool {
        let lower = text.lowercased()
        // Quota banner present ⇒ ignore this session entirely (and suppress any stray
        // API-error text sharing the tail).
        if quotaPhrases.contains(where: lower.contains) { return false }
        if lower.range(of: hitYourLimitPattern, options: .regularExpression) != nil {
            return false
        }
        // "API Error: <3-digit code>" — the exact CLI format (529/500/503/429/…).
        if text.range(of: #"API Error:?\s*[0-9]{3}"#, options: .regularExpression) != nil {
            return true
        }
        // A bare "429 Rate limited" banner. Newer CLI builds print a rate-limit error
        // WITHOUT the "API Error:" prefix, so the 3-digit rule above misses it. A 429 is a
        // transient RPM/TPM rate limit (the window resets in seconds, unlike a weekly/usage
        // quota cap), so nudge past it like any other server error. Requiring the 429 code
        // keeps ordinary prose about rate limits ("bump the rate limit in config.yaml")
        // from tripping it, and the quota check above already excluded the usage caps.
        if lower.range(of: #"\b429\b"#, options: .regularExpression) != nil
            && (lower.contains("rate limit") || lower.contains("too many requests")) {
            return true
        }
        // Or any API error that points at the status page (user's broader ask).
        if lower.contains("api error") && lower.contains("status.claude.com") {
            return true
        }
        // Or a codeless API connectivity error (network out, DNS, timeout, …).
        if lower.contains("api error") && connectivityPhrases.contains(where: lower.contains) {
            return true
        }
        return false
    }

    /// Idle-confirmation gate for the terminal watcher. A session is treated as genuinely
    /// STALLED on an API error — and so eligible for a "continue" nudge — only when its
    /// erroring tail is UNCHANGED since the previous scan. An actively-working session
    /// changes between scans and must not be nudged: e.g. one that merely prints or
    /// discusses an API-error string (like the session developing this very feature), one
    /// that already recovered and moved on while the error line is still on screen, or a
    /// CLI mid auto-retry with a live countdown. `previousTail` is nil the first scan a
    /// tty is seen erroring, which is never a confirmed stall — a second matching,
    /// identical scan is required. Returns false unless the current tail still looks like
    /// an API error, so a session that stopped erroring can't be nudged on stale state.
    public static func isConfirmedStall(previousTail: String?, currentTail: String) -> Bool {
        looksLikeApiError(currentTail) && previousTail == currentTail
    }
}
