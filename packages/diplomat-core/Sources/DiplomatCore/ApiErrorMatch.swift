import Foundation

// Detects a Claude CLI API-error line in a terminal's recent output, so the watcher
// can auto-send a "continue" nudge to an agent that stalled on a transient server
// error (e.g. overnight overload). The CLI prints, e.g.:
//   ⏺ API Error: 529 Overloaded. This is a server-side issue, usually temporary —
//     try again in a moment. If it persists, check https://status.claude.com.
// Kept pure + in the shared core so it's unit-testable. Two things keep it off a session
// that merely mentions the phrase: the caller passes only the last few visible lines, and
// the banner has to OPEN one of them — an agent quoting a banner mid-sentence is prose.
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

    /// A banner OPENS its own line: only decoration may precede it — the "⏺" bullet, the
    /// "⎿" tool-result elbow, box rules, indentation, a log timestamp. `[\W\d_]` is every
    /// character that is NOT a letter, in any script, because prose reaches a quoted banner
    /// through words and decoration never does. That is what separates the CLI's banner
    /// from an agent QUOTING one: a session merely discussing API errors goes static the
    /// moment its turn ends, which is indistinguishable from a stall downstream (see
    /// `isConfirmedStall`). All three carry the anchor — any one alone is enough to nudge,
    /// so an arm without it is a hole in the whole predicate. The codeless rule needs the
    /// colon too, since the other two are pinned by their digits and "API Error" on its own
    /// is two words a doc line can start with.
    private static let bannerOpensLinePattern = #"(?im)^[\W\d_]*API Error:"#
    private static let bannerCodePattern = #"(?im)^[\W\d_]*API Error:?\s*[0-9]{3}"#
    private static let bare429Pattern = #"(?m)^[\W\d_]*\b429\b"#

    /// `text` with terminal wrapping undone, for the phrase evidence a banner carries. A
    /// cut-short banner runs 70-90 columns, so a narrow pane splits it mid-phrase ("…may
    /// be\n  incomplete.") and a contiguous substring search finds nothing — the widest
    /// banner family the watcher exists for, invisible to it in exactly the panes most
    /// likely to wrap. The banner's own line-opening position is read off the ORIGINAL,
    /// where the line structure still exists.
    ///
    /// Blank rows are already gone by here (`ApiErrorWatcher.lastLines`), so this fuses
    /// every adjacent pair, not just the halves of a wrapped line — which is why only the
    /// banner's own evidence is read off it. Quota suppression is not: fusing two lines of
    /// prose into "budget … exceeded" would strand the stalled session it silences.
    private static func rejoined(_ text: String) -> String {
        text.replacingOccurrences(of: #"\n\s*"#, with: " ", options: .regularExpression)
    }

    /// True when `text` shows a transient Claude API error the watcher should nudge
    /// past — a server 5xx / rate-limit ("API Error: <3-digit code>"), a status-page
    /// error, or a codeless failure (network out, DNS, timeout, a stream cut off).
    ///
    /// The banner must OPEN a line; one quoted mid-sentence is prose, not a stall. The
    /// phrases it is read for are matched with the pane's wrapping rejoined, so a banner
    /// the pane split mid-phrase still reads.
    ///
    /// Out-of-quota and org budget-cap banners return false: nudging a capped session
    /// does nothing until the window resets, so the watcher intentionally leaves them
    /// alone. Either banner also SUPPRESSES any API-error text in the same tail, since
    /// the session is idling on the limit rather than the error.
    public static func looksLikeApiError(_ text: String) -> Bool {
        let lower = text.lowercased()
        // Quota banner present ⇒ ignore this session entirely (and suppress any stray
        // API-error text sharing the tail). Read off the ORIGINAL rather than the rejoined
        // copy: these phrases are short enough to survive a wrap, and suppression is the
        // one answer that cannot be retried, so it must not be assembled out of two lines.
        if quotaPhrases.contains(where: lower.contains) { return false }
        if lower.range(of: hitYourLimitPattern, options: .regularExpression) != nil
            || lower.range(of: budgetLimitPattern, options: .regularExpression) != nil {
            return false
        }
        let unwrapped = rejoined(lower)
        // "API Error: <3-digit code>" — the exact CLI format (529/500/503/429/…).
        if text.range(of: bannerCodePattern, options: .regularExpression) != nil {
            return true
        }
        // A bare "429 Rate limited" banner. Newer CLI builds print a rate-limit error
        // WITHOUT the "API Error:" prefix, so the 3-digit rule above misses it. A 429 is a
        // transient RPM/TPM rate limit (the window resets in seconds, unlike a weekly/usage
        // quota cap), so nudge past it like any other server error. It opens its line like
        // the prefixed banners do, which is what keeps ordinary prose about rate limits off
        // it — a retry branch, a status-code table, a note about a 429. The code on its own
        // cannot: three digits are three digits.
        if text.range(of: bare429Pattern, options: .regularExpression) != nil
            && (unwrapped.contains("rate limit") || unwrapped.contains("too many requests")) {
            return true
        }
        guard text.range(of: bannerOpensLinePattern, options: .regularExpression) != nil else {
            return false
        }
        // A codeless API failure the banner names: the status page, connectivity, or a
        // stream cut off part-way.
        return unwrapped.contains("status.claude.com")
            || codelessPhrases.contains(where: unwrapped.contains)
    }

    /// Idle-confirmation gate for the terminal watcher. A session is treated as genuinely
    /// STALLED on an API error — and so eligible for a "continue" nudge — only when its
    /// erroring tail is UNCHANGED since the previous scan. It separates a session still
    /// REDRAWING from one at rest: a CLI mid auto-retry with a live countdown, or one
    /// still printing past the error, changes between scans and must not be nudged.
    /// `previousTail` is nil the first scan a tty is seen erroring, which is never a
    /// confirmed stall — a second matching, identical scan is required. Returns false
    /// unless the current tail still looks like an API error, so a session that stopped
    /// erroring can't be nudged on stale state.
    ///
    /// What it cannot separate is one static screen from another — a session stalled on
    /// the banner and a finished session whose last screen merely CONTAINS one are both
    /// frozen, and the second reads as a confirmed stall as soon as two scans see it
    /// unchanged. Telling those apart is `looksLikeApiError`'s job, not this gate's.
    public static func isConfirmedStall(previousTail: String?, currentTail: String) -> Bool {
        looksLikeApiError(currentTail) && previousTail == currentTail
    }

    /// A short human duration for the audit line: "2m", "45m", "1h 30m", "3h".
    /// Twin of `apiwatch.human_interval`; the resolver puts its output in a reason
    /// string the parity diff compares verbatim.
    public static func humanInterval(_ seconds: TimeInterval) -> String {
        let total = Int(seconds.rounded())
        let hours = total / 3600
        let minutes = (total % 3600) / 60
        if hours > 0 && minutes > 0 { return "\(hours)h \(minutes)m" }
        if hours > 0 { return "\(hours)h" }
        if minutes > 0 { return "\(minutes)m" }
        return "\(total)s"
    }

}
