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
    /// Transient failures the CLI prints with NO status code, all under its "API Error:"
    /// prefix — a connectivity drop ("Unable to connect to API", "Connection error.") or
    /// a turn cut short ("Server error mid-response. The response above may be
    /// incomplete.", "Connection lost before a response was produced. Try again."). Both
    /// resume on a nudge exactly as a 5xx does. The CLI builds the cut-short line from a
    /// cause — server error, lost connection, a sleeping computer, a response that
    /// stopped arriving — plus one of two endings; the endings are what's listed here, so
    /// a new cause is covered too.
    private static let codelessPhrases = [
        "unable to connect", "connection error", "connection refused",
        "connection reset", "connection timed out", "network error",
        "fetch failed", "econnrefused", "enotfound", "etimedout", "getaddrinfo",
        "the response above may be incomplete", "before a response was produced",
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
    /// Spend caps an org sets on a member/workspace, which the API rejects with a 403 —
    ///   "API Error: 403 Org member budget limit exceeded (daily limit). Contact your
    ///    org admin."
    /// Same gap trick as above, so org/workspace/monthly wordings and the "reached"
    /// spelling all match. Filed with the quota banners rather than the errors because
    /// the code is the only thing transient-looking about it: the cap holds until its
    /// window rolls over or an admin raises it, neither of which a nudge can do.
    private static let budgetLimitPattern = #"budget[a-z0-9\- ]{0,16}(exceeded|reached)"#

    /// True when `text` shows a transient Claude API error the watcher should nudge
    /// past — a server 5xx / rate-limit ("API Error: <3-digit code>"), a status-page
    /// error, or a codeless failure (network out, DNS, timeout, a stream cut off).
    ///
    /// Out-of-quota and org budget-cap banners return false: nudging a capped session
    /// does nothing until the window resets, so the watcher intentionally leaves them
    /// alone. Either banner also SUPPRESSES any API-error text in the same tail, since
    /// the session is idling on the limit rather than the error.
    public static func looksLikeApiError(_ text: String) -> Bool {
        let lower = text.lowercased()
        // Quota banner present ⇒ ignore this session entirely (and suppress any stray
        // API-error text sharing the tail).
        if quotaPhrases.contains(where: lower.contains) { return false }
        if lower.range(of: hitYourLimitPattern, options: .regularExpression) != nil
            || lower.range(of: budgetLimitPattern, options: .regularExpression) != nil {
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
        // Or a codeless API failure: connectivity, or a stream cut off part-way.
        if lower.contains("api error") && codelessPhrases.contains(where: lower.contains) {
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
