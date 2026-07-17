import Foundation

// The status the top-of-panel pill renders. Populated in-process by AutofixMonitor
// after each poll (updatedAt = last successful poll), so "active" reflects a monitor
// that's genuinely running — it goes stale (→ "offline") if polling stops.

struct AutofixStatus: Equatable {
    var updatedAt: Date?
    var watching: Int
    var conflictsHandled: Int
    var reviewsHandled: Int

    /// A recent poll ⇒ the monitor is alive. The poll cadence is a few minutes; allow
    /// generous slack before we call it offline.
    var isLive: Bool {
        guard let updatedAt else { return false }
        return Date().timeIntervalSince(updatedAt) < 15 * 60
    }

    var totalHandled: Int { conflictsHandled + reviewsHandled }
}
