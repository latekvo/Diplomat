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
    ///   "Claude usage limit reached. Your limit will reset at 4pm (Europe/Warsaw)."
    ///   "5-hour limit reached ∙ resets 6pm"
    ///   "Weekly limit reached ∙ resets Oct 14"
    /// A stalled-on-quota agent is handled exactly like any other API error: the
    /// nudge is sent on the same exponential backoff (2m→3h), so once the quota
    /// window resets a retry resumes the session instead of it idling forever.
    private static let quotaPhrases = [
        "usage limit reached",
        "hour limit reached",     // "5-hour limit reached ∙ resets …"
        "weekly limit reached",
        "session limit reached",
        "limit will reset at",    // "Your limit will reset at 4pm (…)"
        "out of tokens",
    ]

    public static func looksLikeApiError(_ text: String) -> Bool {
        // "API Error: <3-digit code>" — the exact CLI format (529/500/503/429/…).
        if text.range(of: #"API Error:?\s*[0-9]{3}"#, options: .regularExpression) != nil {
            return true
        }
        let lower = text.lowercased()
        // Or any API error that points at the status page (user's broader ask).
        if lower.contains("api error") && lower.contains("status.claude.com") {
            return true
        }
        // Or a token-quota banner (no "API Error" prefix in the CLI's output).
        if quotaPhrases.contains(where: lower.contains) {
            return true
        }
        // Or a codeless API connectivity error (network out, DNS, timeout, …).
        return lower.contains("api error")
            && connectivityPhrases.contains(where: lower.contains)
    }
}
