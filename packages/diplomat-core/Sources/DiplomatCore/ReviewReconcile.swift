import Foundation

/// One PR's record of our attempts to auto-respond to a review someone requested from us.
/// Persisted by the front-end; the *decision* logic lives in `ReviewReconcile` (pure, so
/// it's unit-tested and shared verbatim).
public struct ReviewAttempt: Codable, Equatable {
    /// The "review requested from me" timestamp (ISO8601) this attempt series is for. A
    /// newer request supersedes it and restarts the series from attempt 1.
    public var requestedAt: String
    /// When we most recently dispatched an agent for this request. Drives the retry
    /// cooldown — we don't re-dispatch until enough time has passed to see if it landed.
    public var lastDispatchedAt: Date
    /// How many agents we've dispatched for this request so far (drives the backoff).
    public var attempts: Int

    public init(requestedAt: String, lastDispatchedAt: Date, attempts: Int) {
        self.requestedAt = requestedAt
        self.lastDispatchedAt = lastDispatchedAt
        self.attempts = attempts
    }
}

/// Decides whether a review someone requested from us still needs an agent dispatched.
///
/// The ground truth is GitHub's own `oweReview` (the request is newer than my last review
/// of that PR); the front-end only calls in here for requests it already owes. Our local
/// "we dispatched an agent" bookkeeping is just an optimization to avoid piling agents on
/// the same request every poll — but a dispatched agent can die, hit an API error, have
/// its window closed, or otherwise finish WITHOUT ever leaving a review. When that happens
/// the review is still owed yet no agent is running: it's *unaddressed*, and we must
/// re-dispatch. This type draws that line, with an exponential retry backoff so a review
/// that keeps failing isn't hammered forever.
///
/// It also guards the opposite failure — a *duplicate* dispatch. When the PR author
/// force-pushes, GitHub stamps a fresh review-requested timestamp (dismissing my pending /
/// prior review and re-requesting), which the markers alone read as a brand-new request.
/// Because we track our own dispatch locally, a new stamp within `reRequestCooldown` of our
/// last dispatch is treated as churn and suppressed, not spawned as a second agent.
public enum ReviewReconcile {
    /// Wait at least this long after a dispatch before retrying an owed-but-unaddressed
    /// review — long enough for a genuine agent to start and leave its review.
    public static let retryBase: TimeInterval = 5 * 60          // 5 min
    /// Backoff ceiling: never wait longer than this between retries to one PR.
    public static let retryMaxBackoff: TimeInterval = 3 * 60 * 60   // 3h
    /// After we dispatch a review for a PR, ignore a *new* review-requested timestamp on
    /// that PR for this long. GitHub stamps a fresh REVIEW_REQUESTED_EVENT when the author
    /// force-pushes (it dismisses my pending/prior review and re-requests), which would
    /// otherwise read as a brand-new request and spawn a duplicate agent. A genuinely fresh
    /// re-request (author addressed feedback) only comes much later, past this window.
    public static let reRequestCooldown: TimeInterval = 60 * 60   // 1h

    /// The cooldown before the next retry, given how many attempts we've already made:
    /// `retryBase * 2^(attempts-1)`, capped at `retryMaxBackoff`. 5m → 10m → 20m → … → 3h.
    public static func retryDelay(afterAttempts n: Int) -> TimeInterval {
        guard n >= 1 else { return 0 }
        let scaled = retryBase * pow(2.0, Double(n - 1))
        return min(scaled, retryMaxBackoff)
    }

    /// What to do about one owed review request this poll.
    public enum Decision: Equatable {
        /// The author is banned (prompt injection) — never auto-review them.
        case skipBanned
        /// An agent is already running for this PR — let it finish.
        case skipInFlight
        /// We dispatched recently; wait `remaining` seconds before retrying.
        case skipCoolingDown(TimeInterval)
        /// (Re)dispatch now; `attemptNumber` is 1 for a first dispatch, ≥2 for a retry.
        case dispatch(attemptNumber: Int)
    }

    /// Decide for a single owed review request.
    /// - prior: our recorded attempt for this PR, if any.
    /// - stamp: the current "requested from me" timestamp (`"-"` when unknown).
    /// - inFlight: is one of our agents currently running for this PR.
    /// - banned: is the PR's author on the prompt-injection ban list.
    /// - now: the current time.
    public static func decide(prior: ReviewAttempt?, stamp: String, inFlight: Bool,
                              banned: Bool, now: Date) -> Decision {
        if banned { return .skipBanned }
        if inFlight { return .skipInFlight }
        // Never dispatched for this PR (or its record aged out) → dispatch straight away.
        guard let rec = prior else { return .dispatch(attemptNumber: 1) }
        let elapsed = now.timeIntervalSince(rec.lastDispatchedAt)
        if rec.requestedAt == stamp {
            // Same request we already dispatched for, still owed with no agent on it: the
            // earlier attempt didn't land. Retry once the escalating backoff has elapsed.
            let delay = retryDelay(afterAttempts: rec.attempts)
            if elapsed < delay { return .skipCoolingDown(delay - elapsed) }
            return .dispatch(attemptNumber: rec.attempts + 1)
        }
        // A DIFFERENT request timestamp than the one we dispatched for. This is usually a
        // force-push: GitHub re-stamps the review request, which must NOT immediately spawn
        // a second agent. Hold off until our last dispatch is old enough that this reads as
        // a genuine fresh review need rather than churn.
        if elapsed < reRequestCooldown { return .skipCoolingDown(reRequestCooldown - elapsed) }
        return .dispatch(attemptNumber: 1)
    }
}
