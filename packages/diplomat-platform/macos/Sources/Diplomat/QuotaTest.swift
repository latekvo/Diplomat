import Foundation
import DiplomatCore

/// Headless self-test for WHICH credential the quota probe spends, driven by
/// `DIPLOMAT_QUOTA_TEST=1`.
///
/// The arithmetic the probe feeds is pinned on both sides already (`Telemetry`, the
/// parity suite); what had no cover at all was the step before it — reading a token off
/// this machine. That step failed silently for four days: `.credentials.json` held a
/// token that expired on 2026-08-27 while Claude Code kept refreshing the live one in
/// the login Keychain, the probe stopped at the file, and every reading came back
/// missing. Nothing said so, because a ceiling with no reading is SKIPPED by
/// `AgentDispatchGate.budgetDecide` and a call where none has one is affordable — so the
/// dispatch budget did not fail closed or loudly, it quietly stopped gating, and the
/// machine spent a night dispatching agents into an exhausted weekly window.
///
/// It dials nothing: the file half of the candidate list is a fixture directory this
/// file writes, and the Keychain half is pinned, so the answer is the fixture's on a
/// developer's Mac (which has a real credentials item) and on a CI runner (which has
/// none) alike.
///
///     DIPLOMAT_QUOTA_TEST=1 swift run Diplomat
enum QuotaTest {
    /// Returns overall pass/fail so the launcher can exit non-zero — a FAIL that still
    /// exits 0 can't gate anything.
    @discardableResult
    static func run() -> Bool {
        var pass = true
        func check(_ name: String, _ ok: Bool) {
            print("\(ok ? "PASS" : "FAIL") — \(name)")
            if !ok { pass = false }
        }

        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("diplomat-quota-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        setenv("DIPLOMAT_CLAUDE_DIR", dir.path, 1)

        func writeFileToken(_ token: String?) {
            let url = dir.appendingPathComponent(".credentials.json")
            guard let token else { try? FileManager.default.removeItem(at: url); return }
            let blob = ["claudeAiOauth": ["accessToken": token]]
            try? JSONSerialization.data(withJSONObject: blob).write(to: url)
        }

        // The outage this exists for: a file that still parses, holding a token the
        // endpoint no longer accepts. Stopping there is a probe pinned to a dead
        // credential for as long as the file exists.
        writeFileToken("oat-stale")
        Quota.pinKeychain("oat-live")
        check("a stale credentials file does not hide the live Keychain token",
              Quota.oauthTokens() == ["oat-stale", "oat-live"])

        // The file stays FIRST, which is what lets DIPLOMAT_CLAUDE_DIR decide a
        // fixture's answer instead of being shadowed by the real login Keychain.
        writeFileToken("oat-file")
        Quota.pinKeychain("oat-keychain")
        check("the credentials file is still asked first",
              Quota.oauthTokens().first == "oat-file")

        // The ordinary Mac, where both sources hold the same live token. A second
        // identical request would spend the shared per-account bucket for an answer
        // that cannot differ.
        writeFileToken("oat-same")
        Quota.pinKeychain("oat-same")
        check("one credential in two places is presented once",
              Quota.oauthTokens() == ["oat-same"])

        // Linux, and a Mac that never wrote the file: each source answers alone.
        writeFileToken(nil)
        Quota.pinKeychain("oat-keychain-only")
        check("a machine with no credentials file falls back to the Keychain",
              Quota.oauthTokens() == ["oat-keychain-only"])

        writeFileToken("oat-file-only")
        Quota.pinKeychain(nil)
        check("a machine with no Keychain item still offers its file",
              Quota.oauthTokens() == ["oat-file-only"])

        // No candidate at all is the one failure retrying cannot fix, and the gate that
        // keeps a logged-out machine's sample from sleeping out its whole schedule.
        writeFileToken(nil)
        Quota.pinKeychain(nil)
        check("a logged-out machine offers nothing", Quota.oauthTokens().isEmpty)

        print("\nQUOTA TEST \(pass ? "OK" : "FAILED")")
        return pass
    }
}
