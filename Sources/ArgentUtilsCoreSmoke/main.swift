import Foundation
import ArgentUtilsCore

// A Linux-verifiable smoke test for the shared core: it loads the core/ assets,
// runs the filters on a synthetic fixture, assembles the three review prompts,
// and (with ARGENT_UTILS_DUMP=1) runs the real gh pipeline so the Swift core can
// be cross-checked against the Linux Python front-end.

func section(_ s: String) { print("\n== \(s) ==") }

/// Assertion that survives release builds: `assert()` compiles out under `-c release`,
/// which would have turned this whole suite vacuously green if CI ever switched
/// configurations. Prints the failing line and exits non-zero.
func check(_ condition: Bool, _ message: @autoclosure () -> String = "",
           file: StaticString = #filePath, line: UInt = #line) {
    if !condition {
        let msg = message()
        print("CHECK FAILED at \(file):\(line)\(msg.isEmpty ? "" : " — \(msg)")")
        exit(1)
    }
}

section("core assets")
let cfg = try CoreAssets.config()
print("config: \(cfg.owner)/\(cfg.repo)")
print("catalog: \(ToolKind.allCases.map { $0.rawValue })")
print("titles : \(ToolKind.allCases.map { $0.title })")
let f = try CoreAssets.filters()
print("filters: skillSuffix=\(f.skillSuffix) staleDays=\(f.staleReadyDays) approved=\(f.approvedDecision)")
print("depths : \(ReviewCatalog.depths().map { $0.id }) default=\(ReviewCatalog.defaultDepthID())")
// ToolKind hardcodes its case list while catalog.json is data — assert they agree, or a
// seventh catalog entry would appear on Linux (which iterates the JSON) but silently
// not on macOS, and a renamed id would fall back to placeholder titles/icons unnoticed.
let catalogIDs = try CoreAssets.catalog().map { $0.id }
check(catalogIDs == ToolKind.allCases.map { $0.rawValue },
      "catalog.json ids \(catalogIDs) != ToolKind.allCases")
for kind in ToolKind.allCases {
    check(kind.title != kind.rawValue, "catalog title missing for \(kind.rawValue)")
    check(kind.systemImage != "questionmark.circle", "catalog sfSymbol missing for \(kind.rawValue)")
}

section("filters on synthetic fixture")
let now = Date()
let old = now.addingTimeInterval(-15 * 86400)
let prs: [OpenPR] = [
    OpenPR(number: 101, title: "skill", url: "u/101", isDraft: false, author: "alice",
           createdAt: now, readyForReviewAt: nil, files: ["skills/foo/SKILL.md"],
           reviewDecision: nil, reviewThreads: []),
    OpenPR(number: 102, title: "installer", url: "u/102", isDraft: true, author: "bob",
           createdAt: now, readyForReviewAt: nil, files: ["packages/argent-installer/x.ts"],
           reviewDecision: nil, reviewThreads: []),
    OpenPR(number: 103, title: "stale", url: "u/103", isDraft: false, author: "carol",
           createdAt: old, readyForReviewAt: old, files: ["src/x.ts"],
           reviewDecision: nil, reviewThreads: []),
    OpenPR(number: 104, title: "approved", url: "u/104", isDraft: false, author: "latekvo",
           createdAt: now, readyForReviewAt: nil, files: ["a.ts"],
           reviewDecision: "APPROVED", reviewThreads: []),
    OpenPR(number: 105, title: "unaddressed", url: "u/105", isDraft: false, author: "latekvo",
           createdAt: now, readyForReviewAt: nil, files: ["b.ts"], reviewDecision: nil,
           reviewThreads: [ReviewThread(isResolved: false, viewerCanResolve: true, lastCommentAuthor: "rev")]),
]
let issues: [OpenIssue] = [
    OpenIssue(number: 201, title: "ext", url: "i/201", author: "ext", authorAssociation: "NONE",
              createdAt: old, updatedAt: old, commentCount: 0, assignees: [], labels: ["bug"], memberResponded: false),
    OpenIssue(number: 202, title: "member", url: "i/202", author: "dev", authorAssociation: "MEMBER",
              createdAt: now, updatedAt: now, commentCount: 1, assignees: [], labels: [], memberResponded: true),
]
let me = "latekvo"
for kind in ToolKind.allCases {
    let ids = ToolData.items(for: kind, prs: prs, issues: issues, me: me).map { $0.id }
    print("\(kind.rawValue): \(ids)")
}
// Exact expected ids per tool — this section used to only print, so a filter that
// regressed to returning [] still passed. (The Python tests assert these numbers;
// the Swift core deserves the same.)
check(Filters.skillPRs(prs).map { $0.number } == [101], "skill filter")
check(Filters.installerPRs(prs).map { $0.number } == [102], "installer filter")
check(Filters.staleReadyPRs(prs, now: now).map { $0.number } == [103], "stale-ready filter")
check(Filters.unaddressedExternalIssues(issues).map { $0.number } == [201], "external-issues filter")
check(Filters.myApprovedPRs(prs, me: me).map { $0.number } == [104], "my-approved filter")
check(Filters.myUnaddressedReviewPRs(prs, me: me).map { $0.number } == [105], "my-unaddressed filter")
check(Filters.myApprovedPRs(prs, me: "").isEmpty, "empty me → no approved PRs")
// isSkillFile matches the FILENAME (skill.md / *.skill.md), never a bare suffix —
// "docs/reskill.md" must not count as a SKILL file (it feeds the verdict gate).
check(Filters.isSkillFile("skills/foo/SKILL.md"))
check(Filters.isSkillFile("any/dir/agent.skill.md"))
check(!Filters.isSkillFile("docs/reskill.md"), "reskill.md is not a SKILL file")
check(!Filters.isSkillFile("skill.md.bak"))
let look = ToolData.lookup(101, prs: prs, issues: issues, me: me, visible: ToolKind.allCases)
print("lookup #101 on: \(look.onLists.map { $0.rawValue }) — \(look.presence)")
check(look.onLists.map { $0.rawValue } == ["skillPRs"], "lookup #101 lists")

section("thread triage (shared 'I owe a reply' rule)")
// Case-insensitive me-comparison (GitHub logins are case-insensitive): a thread whose
// last comment is mine — however my login is cased — is NOT owed.
check(!ThreadTriage.owed(isResolved: false, viewerCanResolve: true, lastCommentAuthor: "LateKVO", me: "latekvo"))
check(ThreadTriage.owed(isResolved: false, viewerCanResolve: true, lastCommentAuthor: "reviewer", me: "latekvo"))
check(!ThreadTriage.owed(isResolved: true, viewerCanResolve: true, lastCommentAuthor: "reviewer", me: "latekvo"))
check(!ThreadTriage.owed(isResolved: false, viewerCanResolve: false, lastCommentAuthor: "reviewer", me: "latekvo"))
// Missing viewerCanResolve (older payloads) defaults to owed; nil author (deleted user) is owed.
check(ThreadTriage.owed(isResolved: false, viewerCanResolve: nil, lastCommentAuthor: "reviewer", me: "latekvo"))
check(ThreadTriage.owed(isResolved: false, viewerCanResolve: true, lastCommentAuthor: nil, me: "latekvo"))
// OpenPR.unaddressedThreads flows through the same rule.
check(prs[4].unaddressedThreads(me: "LATEKVO").count == 1, "unaddressedThreads is case-insensitive on me")
print("thread-triage assertions passed")

section("PR-reference parsing")
func single(_ pr: String) -> ReviewConfig {
    ReviewConfig(depth: "max", target: .specific, me: me, specificPR: pr)
}
let urlRef = PRRef.parse("https://github.com/\(cfg.owner)/\(cfg.repo)/pull/337/files",
                         owner: cfg.owner, repo: cfg.repo)
check(urlRef.number == 337 && urlRef.isValid && !urlRef.repoMismatch)
check(PRRef.parse("#42", owner: cfg.owner, repo: cfg.repo).number == 42)
check(PRRef.parse("\(cfg.owner)/\(cfg.repo)#9", owner: cfg.owner, repo: cfg.repo).number == 9)
let wrongRepo = PRRef.parse("https://github.com/other/proj/pull/5", owner: cfg.owner, repo: cfg.repo)
check(wrongRepo.number == 5 && wrongRepo.repoMismatch && !wrongRepo.isValid)
check(PRRef.parse("not-a-pr", owner: cfg.owner, repo: cfg.repo).number == nil)
// ASCII digits only, matching the Python port: a leading '+' (which Int() alone
// accepts) and non-ASCII digits are rejected on both sides.
check(PRRef.parse("+337", owner: cfg.owner, repo: cfg.repo).number == nil)
check(PRRef.parse("#+337", owner: cfg.owner, repo: cfg.repo).number == nil)
check(PRRef.parse("٣٣٧", owner: cfg.owner, repo: cfg.repo).number == nil)
print("PR-reference assertions passed")

section("review prompts")
let mine = ReviewConfig(depth: "max", me: me)
let other = ReviewConfig(depth: "max", target: .someone, username: "someuser")
print("mine valid=\(mine.isValid) | other valid=\(other.isValid) | single valid=\(single("337").isValid)")
check(mine.buildPrompt().contains("mark it ready for review"))
check(!mine.buildPrompt().contains("POST a pull-request review"))
check(other.buildPrompt().contains("POST a pull-request review"))
// Someone else's PRs are review-only: a hard no-commit guard, and the
// commit-authoring guidance is dropped (we never touch their branch).
check(other.buildPrompt().contains("ABSOLUTELY DO NOT touch their branch"))
check(!other.buildPrompt().contains("No AI attribution"))
// My PRs do commit, so no review-only guard and the attribution rule stays.
check(!mine.buildPrompt().contains("ABSOLUTELY DO NOT touch their branch"))
check(single("337").buildPrompt().hasPrefix("Review PR #337 in \(cfg.owner)/\(cfg.repo)."))
// A pasted URL for the target repo resolves to the same single-PR prompt.
check(single("https://github.com/\(cfg.owner)/\(cfg.repo)/pull/337").isValid)
check(single("https://github.com/\(cfg.owner)/\(cfg.repo)/pull/337").buildPrompt()
    .hasPrefix("Review PR #337 in \(cfg.owner)/\(cfg.repo)."))
// A URL for a different repo is rejected.
check(!single("https://github.com/other/proj/pull/9").isValid)
check(mine.buildPrompt().contains("No AI attribution"))

// A specific PR may be mine OR someone else's, so its prompt is author-gated: it
// polls the author, then splits into CASE A (mine → fix on branch, mark ready) and
// CASE B (theirs → review-only, never touch the branch, and DO NOT mark ready).
let singlePrompt = single("337").buildPrompt()
check(singlePrompt.contains("WHO AUTHORED IT"))
check(singlePrompt.contains("CASE A") && singlePrompt.contains("CASE B"))
// CASE A keeps the fix-on-branch + mark-ready + attribution behaviour…
check(singlePrompt.contains("on the PR's branch"))   // depth onBranch fix step
check(singlePrompt.contains("mark it ready for review"))
check(singlePrompt.contains("No AI attribution"))
// …CASE B is the hard look-don't-touch guard, with an explicit do-not-advance line.
check(singlePrompt.contains("ABSOLUTELY DO NOT touch their branch"))
check(singlePrompt.contains("isn't yours to advance"))
// With mark-ready off, neither the mark-ready block nor (since target≠someone) the
// generic sweep markReady survives — proving the toggle gates only CASE A.
let singleNoReady = ReviewConfig(depth: "max", target: .specific, me: me,
                                 markReady: false, specificPR: "337").buildPrompt()
check(!singleNoReady.contains("mark it ready for review"))
check(singleNoReady.contains("isn't yours to advance"))
print("prompt assembly assertions passed")

section("conflict prompts")
let cMine = ConflictConfig(me: me)
let cOther = ConflictConfig(target: .someone, username: "someuser")
let cSingle = ConflictConfig(target: .specific, specificPR: "337")
print("mine valid=\(cMine.isValid) | other valid=\(cOther.isValid) | single valid=\(cSingle.isValid)")
check(cMine.isValid && cOther.isValid && cSingle.isValid)
check(!ConflictConfig(target: .specific, specificPR: "nope").isValid)
// The single-PR field accepts a URL for the target repo, rejects other repos.
check(ConflictConfig(target: .specific,
                      specificPR: "https://github.com/\(cfg.owner)/\(cfg.repo)/pull/337").isValid)
check(!ConflictConfig(target: .specific, specificPR: "https://github.com/x/y/pull/1").isValid)
check(cMine.buildPrompt().contains("authored by @\(me)"))
check(cMine.buildPrompt().contains("For each, merge the latest `origin/main`"))
check(cSingle.buildPrompt().hasPrefix("Take PR #337 in \(cfg.owner)/\(cfg.repo)."))
check(cSingle.buildPrompt().contains("Merge the latest `origin/main`"))
check(cMine.buildPrompt().contains("No AI attribution"))
print("conflict prompt assertions passed")

section("audit prompts")
// A whole-repo E2E audit needs no input (always valid), and the hard-repro bar is
// present in every variant. The two toggles independently gate the optional blocks.
let aBase = AuditConfig()
print("audit valid=\(aBase.isValid)")
check(aBase.isValid && AuditConfig().isValid)
check(aBase.buildPrompt().contains("100% CERTAINTY"))
check(aBase.buildPrompt().hasPrefix("Run a FULL end-to-end test of the ENTIRE \(cfg.owner)/\(cfg.repo)"))
// Reproduction must be driven on a real simulator/emulator (always present, in bar).
check(aBase.buildPrompt().contains("SIMULATOR / EMULATOR"))
// Severity classification (H/M/L) is always present, even in the read-only default.
check(aBase.buildPrompt().contains("HIGH") && aBase.buildPrompt().contains("LOW"))
// Default (find-only): read-only, no issue-handling, no PRs (so no 20-LOC PR gate).
check(aBase.buildPrompt().contains("READ-ONLY audit"))
check(!aBase.buildPrompt().contains("OPEN ISSUES"))
check(!aBase.buildPrompt().contains("focused pull request"))
check(!aBase.buildPrompt().contains("20 lines"))
// fixIssues adds the bug-issue block, explicit about skipping feature requests.
let aIssues = AuditConfig(fixIssues: true)
check(aIssues.buildPrompt().contains("OPEN ISSUES"))
check(aIssues.buildPrompt().contains("SKIP every feature request"))
check(aIssues.buildPrompt().contains("READ-ONLY audit"))   // still read-only
// openPRs swaps the read-only guard for the open-a-PR block + no-attribution.
let aPRs = AuditConfig(openPRs: true)
check(aPRs.buildPrompt().contains("focused pull request"))
check(aPRs.buildPrompt().contains("DRAFT"))   // every opened PR must be a draft
check(aPRs.buildPrompt().contains("DUPLICATE") && aPRs.buildPrompt().contains("gh pr diff"))
check(aPRs.buildPrompt().contains("20 lines"))   // Low/nitpick PRs only when fix < 20 LOC
check(aPRs.buildPrompt().contains("No AI attribution"))
check(!aPRs.buildPrompt().contains("READ-ONLY audit"))
// Both on: issue-handling + PRs together.
let aBoth = AuditConfig(fixIssues: true, openPRs: true)
check(aBoth.buildPrompt().contains("OPEN ISSUES") && aBoth.buildPrompt().contains("focused pull request"))
print("audit prompt assertions passed")

// ---- Auto-fix monitor diff (edge-triggering) ----
section("autofix diff")
func snap(_ n: Int, mergeable: String = "MERGEABLE", decision: String = "", threads: Int = 0) -> PRSnapshot {
    PRSnapshot(number: n, title: "PR \(n)", url: "u\(n)", isDraft: false,
               mergeable: mergeable, reviewDecision: decision, threadsUnresolved: threads)
}
// First run: everything seeds, nothing fires.
let base = AutofixDiff.compute(prior: [:], now: [snap(1), snap(2, mergeable: "CONFLICTING", threads: 3)])
check(base.events.isEmpty, "baseline must not dispatch")
check(base.fingerprints.count == 2)
// Clean -> conflicting fires exactly one conflict event.
let c = AutofixDiff.compute(prior: base.fingerprints, now: [snap(1, mergeable: "CONFLICTING"), snap(2, mergeable: "CONFLICTING", threads: 3)])
check(c.events == [.conflict(snap(1, mergeable: "CONFLICTING"))], "clean->conflicting fires once")
// More unresolved threads OR a new CHANGES_REQUESTED fires a review event.
let rPrior = [1: PRFingerprint(mergeable: "MERGEABLE", reviewDecision: "", threadsUnresolved: 1)]
check(AutofixDiff.compute(prior: rPrior, now: [snap(1, threads: 4)]).events == [.review(snap(1, threads: 4))])
check(AutofixDiff.compute(prior: rPrior, now: [snap(1, decision: "CHANGES_REQUESTED", threads: 1)]).events
       == [.review(snap(1, decision: "CHANGES_REQUESTED", threads: 1))])
// Our own "Fixed in <hash>" replies (threads resolved, verdict unchanged) must NOT fire.
let selfReply = [1: PRFingerprint(mergeable: "MERGEABLE", reviewDecision: "CHANGES_REQUESTED", threadsUnresolved: 5)]
check(AutofixDiff.compute(prior: selfReply, now: [snap(1, decision: "CHANGES_REQUESTED", threads: 0)]).events.isEmpty,
       "resolving threads must not retrigger")
// UNKNOWN mergeable carries the prior value forward — no phantom conflict.
let unk = [1: PRFingerprint(mergeable: "MERGEABLE", reviewDecision: "", threadsUnresolved: 0)]
let u = AutofixDiff.compute(prior: unk, now: [snap(1, mergeable: "UNKNOWN")])
check(u.events.isEmpty && u.fingerprints[1]?.mergeable == "MERGEABLE", "UNKNOWN carries prior forward")
print("autofix diff assertions passed")

// ---- known-mine single-PR review prompt (auto-fix monitor) ----
section("known-mine review prompt")
let km = ReviewConfig(depth: "deep", target: .specific, me: "latekvo",
                      markReady: false, leaveReviews: false, replyToReviews: true,
                      specificPR: "440", specificAuthor: .mine).buildPrompt()
// No author poll, no CASE A/B branching — we already know it's ours.
check(!km.contains("WHO AUTHORED IT"), "known-mine skips the author poll")
check(!km.contains("CASE A") && !km.contains("CASE B"), "known-mine has no case branching")
check(!km.contains("SOMEONE ELSE'S"), "known-mine has no review-only block")
// But it IS the fix-on-branch, no-attribution disposition on the right PR, and it puts
// resolving existing reviewer findings FIRST (screen/verify/fix-or-dismiss/respond).
check(km.contains("Review PR #440"))
check(km.contains("MINE") && km.contains("full authority"))
check(km.contains("FIRST AND FOREMOST, resolve every reviewer finding"))
check(km.contains(#""Fixed in <commit_hash>""#))
// The reviewer-findings step comes before the agent's own deep-review approach.
check(km.range(of: "FIRST AND FOREMOST")!.lowerBound < km.range(of: "dispatch swarms of agents")!.lowerBound)
check(km.contains("fix it directly on the PR's branch"))
check(km.contains("No AI attribution"))
check(!km.contains("mark it ready for review"), "markReady=false omits the block")
// The gated (author-unknown) path still branches, for the manual Specific-PR wizard.
let gated = ReviewConfig(depth: "deep", target: .specific, me: "latekvo", specificPR: "440").buildPrompt()
check(gated.contains("WHO AUTHORED IT") && gated.contains("CASE A") && gated.contains("CASE B"))
print("known-mine review prompt assertions passed")

// ---- known-theirs comprehensive review (review-request monitor) ----
section("known-theirs review prompt")
let kt = ReviewConfig(depth: "max", target: .specific, me: "latekvo",
                      markReady: false, leaveReviews: true, replyToReviews: false,
                      specificPR: "500", specificAuthor: .theirs).buildPrompt()
check(!kt.contains("WHO AUTHORED IT"), "known-theirs skips the author poll")
check(!kt.contains("CASE A") && !kt.contains("CASE B"), "no case branching")
check(kt.contains("SOMEONE ELSE'S"), "review-only framing")
check(kt.contains("ABSOLUTELY DO NOT touch their branch"), "reviewOnly block present")
check(kt.contains("POST a pull-request review"), "leaveReviews block present")
check(kt.contains("Do NOT mark this PR ready"), "otherNoMarkReady present")
check(kt.contains("SECOND, independent verification"), "max-depth fragment present")
// No auto-verdict — the final approve is the user's, not the agent's.
check(kt.contains("Do NOT submit an APPROVE"), "no-verdict instruction present")
check(kt.contains("PR #500 looks clean"), "no-verdict {pr} substituted")
check(!kt.contains("still APPROVE"), "the finalPass approve-verdict block is gone")
check(!kt.contains("fix it directly on the PR's branch"), "never fixes someone else's branch")
check(!kt.contains("No AI attribution"), "no commits ⇒ no attribution block")
print("known-theirs review prompt assertions passed")

// ---- known-theirs WITH verdict (trusted author: member/maintainer/contributor) ----
// The review-request monitor sets finalPass=true when the PR author is trusted, so the
// auto-review closes with an APPROVE/changes-requested verdict instead of comments only.
section("known-theirs review prompt (trusted author → verdict)")
let ktv = ReviewConfig(depth: "max", target: .specific, me: "latekvo",
                       markReady: false, leaveReviews: true, replyToReviews: false,
                       specificPR: "500", finalPass: true, specificAuthor: .theirs).buildPrompt()
check(ktv.contains("SOMEONE ELSE'S"), "review-only framing still present with verdict")
check(ktv.contains("still APPROVE"), "trusted author ⇒ finalPass APPROVE-verdict block present")
check(!ktv.contains("Do NOT submit an APPROVE"), "trusted author ⇒ no no-verdict block")
print("known-theirs (trusted author) review prompt assertions passed")

// ---- Auto-review verdict policy (skill / installer / community suppressors) ----
section("verdict policy")
let cleanFiles = ["packages/argent-core/src/foo.ts", "README.md"]
let skillFiles = ["src/skills/argent-x/SKILL.md"]
let installerFiles = ["packages/argent-installer/index.ts"]
let allOn = VerdictPolicy()   // every suppressor on — the default policy
// Trusted author, nothing sensitive touched → verdict allowed.
check(allOn.allowsVerdict(files: cleanFiles, authorAssociation: "MEMBER"))
check(allOn.allowsVerdict(files: cleanFiles, authorAssociation: "CONTRIBUTOR"))
// Each suppressor independently withholds the verdict.
check(!allOn.allowsVerdict(files: skillFiles, authorAssociation: "MEMBER"), "skill ⇒ no verdict")
check(!allOn.allowsVerdict(files: installerFiles, authorAssociation: "OWNER"), "installer ⇒ no verdict")
check(!allOn.allowsVerdict(files: cleanFiles, authorAssociation: "NONE"), "community ⇒ no verdict")
// The exact association GitHub returns for an outside contributor.
check(!allOn.allowsVerdict(files: cleanFiles, authorAssociation: "FIRST_TIME_CONTRIBUTOR"), "outside author ⇒ no verdict")
// Reasons are reported, in order, and can stack.
check(allOn.withholdReasons(files: skillFiles, authorAssociation: "NONE")
        == ["touches a SKILL", "community PR"], "stacked reasons in order")
check(allOn.withholdReasons(files: cleanFiles, authorAssociation: "MEMBER").isEmpty, "no reasons ⇒ verdict")
// Turning one suppressor off re-enables the verdict for only that class.
let skillOff = VerdictPolicy(withholdOnSkill: false)
check(skillOff.allowsVerdict(files: skillFiles, authorAssociation: "MEMBER"), "skill off ⇒ skill PR gets verdict")
check(!skillOff.allowsVerdict(files: installerFiles, authorAssociation: "MEMBER"), "installer still withheld")
// Everything off ⇒ always a verdict, regardless of files/author.
let allOff = VerdictPolicy(withholdOnSkill: false, withholdOnInstaller: false, withholdOnCommunity: false)
check(allOff.allowsVerdict(files: skillFiles + installerFiles, authorAssociation: "NONE"), "all off ⇒ always verdict")
print("verdict policy assertions passed")

// ---- Review reconciler (retry unaddressed reviews) ----
section("review reconcile")
// Backoff: 5m → 10m → 20m → 40m → … → capped at 3h.
check(ReviewReconcile.retryDelay(afterAttempts: 1) == 5 * 60)
check(ReviewReconcile.retryDelay(afterAttempts: 2) == 10 * 60)
check(ReviewReconcile.retryDelay(afterAttempts: 3) == 20 * 60)
check(ReviewReconcile.retryDelay(afterAttempts: 20) == 3 * 60 * 60, "backoff caps at 3h")
let t0 = Date(timeIntervalSinceReferenceDate: 1_000_000)
// Never attempted → dispatch #1.
check(ReviewReconcile.decide(prior: nil, stamp: "2026-01-01T00:00:00Z",
                              inFlight: false, banned: false, now: t0) == .dispatch(attemptNumber: 1))
// Banned author → never, even if owed and idle.
check(ReviewReconcile.decide(prior: nil, stamp: "s", inFlight: false, banned: true, now: t0) == .skipBanned)
// An agent is running for it → leave it be (ban check comes first only when banned).
check(ReviewReconcile.decide(prior: nil, stamp: "s", inFlight: true, banned: false, now: t0) == .skipInFlight)
// Dispatched 1 min ago, agent no longer running, still owed → cool down (5m − 1m = 4m left).
let a1 = ReviewAttempt(requestedAt: "s", lastDispatchedAt: t0.addingTimeInterval(-60), attempts: 1)
check(ReviewReconcile.decide(prior: a1, stamp: "s", inFlight: false, banned: false, now: t0)
        == .skipCoolingDown(4 * 60), "within backoff ⇒ wait")
// Same record but the 5-min backoff has elapsed → this is the unaddressed retry (#2).
let a1old = ReviewAttempt(requestedAt: "s", lastDispatchedAt: t0.addingTimeInterval(-6 * 60), attempts: 1)
check(ReviewReconcile.decide(prior: a1old, stamp: "s", inFlight: false, banned: false, now: t0)
        == .dispatch(attemptNumber: 2), "past backoff, still owed ⇒ re-dispatch")
// A re-request (new stamp) shortly after we dispatched is a force-push re-stamp — suppress
// it rather than spawn a duplicate review agent.
check(ReviewReconcile.decide(prior: a1, stamp: "newer-stamp", inFlight: false, banned: false, now: t0)
        == .skipCoolingDown(ReviewReconcile.reRequestCooldown - 60), "re-request within cooldown ⇒ suppressed")
// A re-request long after our last dispatch (past the force-push window) is a genuine fresh
// review need → dispatch #1.
let aOld = ReviewAttempt(requestedAt: "s", lastDispatchedAt: t0.addingTimeInterval(-2 * 60 * 60), attempts: 1)
check(ReviewReconcile.decide(prior: aOld, stamp: "newer-stamp", inFlight: false, banned: false, now: t0)
        == .dispatch(attemptNumber: 1), "re-request past cooldown ⇒ fresh dispatch")
print("review reconcile assertions passed")

// ---- Agent activity (running vs awaiting input) ----
section("agent activity")
// A working session: the CLI's live status bar carries the interrupt hint (real capture).
let busyTail = """
✻ Reticulating… (2m 54s · ↓ 10.6k tokens)
                                                       55301 tokens
──────────────────────────────────────────────────────────────────
❯
──────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt · ← for agents · ↓ to manage
"""
check(AgentActivity.looksBusy(busyTail), "interrupt hint on the status bar ⇒ busy")
// A finished turn idling at the prompt: same layout, but no interrupt hint (real capture).
let idleTail = """
✻ Sautéed for 22m 22s
                                             new task? /clear to save 240.5k tokens
──────────────────────────────────────────────────────────────────
❯ Reply to hubgan summarizing the reset-semantics issues
──────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents
"""
check(!AgentActivity.looksBusy(idleTail), "no interrupt hint ⇒ awaiting input")
// Scrollback trap: an earlier turn's interrupt hint sits high in the buffer, but the
// live bottom is the idle prompt — scanning only the tail must NOT read busy.
let staleTail = """
  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt · ← for agents
⏺ Done. Ran the tests — 47 passed.
✻ Baked for 4m 39s
                                                                  99% context left
──────────────────────────────────────────────────────────────────
❯
──────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents
"""
check(!AgentActivity.looksBusy(staleTail), "stale hint in scrollback must not read busy")
check(!AgentActivity.looksBusy(""), "empty buffer ⇒ not busy")
print("agent activity assertions passed")

// ---- Claude API-error detection (terminal auto-continue) ----
section("api-error match")
check(ApiErrorMatch.looksLikeApiError("⏺ API Error: 529 Overloaded. If it persists, check https://status.claude.com."))
check(ApiErrorMatch.looksLikeApiError("API Error: 500 Internal Server Error"))
check(ApiErrorMatch.looksLikeApiError("something API error, see status.claude.com for details"))
// Codeless connectivity failures (network out / DNS / timeout) must also match.
check(ApiErrorMatch.looksLikeApiError("⏺ API Error: Unable to connect to API"))
check(ApiErrorMatch.looksLikeApiError("API Error: Connection error."))
check(ApiErrorMatch.looksLikeApiError("API Error: getaddrinfo ENOTFOUND api.anthropic.com"))
check(!ApiErrorMatch.looksLikeApiError("● Running tests… 47 passed"))
check(!ApiErrorMatch.looksLikeApiError("git push origin main"))
// "unable to connect" alone (no "api error") must NOT trip it — e.g. app logs.
check(!ApiErrorMatch.looksLikeApiError("curl: unable to connect to localhost:8080"))
check(!ApiErrorMatch.looksLikeApiError(""))
print("api-error match assertions passed")

// ---- Golden prompts (cross-platform parity) ----
// Every prompt mode both front-ends can assemble is compared byte-for-byte against a
// committed golden file in core/golden-prompts/. The Linux tests assert the SAME
// files, so Swift and Python can only drift from each other by failing one CI job.
// Regenerate after an intentional core/*.json change: ARGENT_GOLDEN_WRITE=1 swift run
// ArgentUtilsCoreSmoke.
section("golden prompts")
let goldenMe = "testuser"
let goldenModes: [(String, String)] = [
    ("review-mine-max", ReviewConfig(depth: "max", me: goldenMe).buildPrompt()),
    ("review-user-max", ReviewConfig(depth: "max", target: .someone, username: "someuser",
                                     me: goldenMe).buildPrompt()),
    ("review-single-unknown", ReviewConfig(depth: "max", target: .specific, me: goldenMe,
                                           specificPR: "337").buildPrompt()),
    ("conflicts-mine", ConflictConfig(me: goldenMe).buildPrompt()),
    ("conflicts-user", ConflictConfig(target: .someone, username: "someuser",
                                      me: goldenMe).buildPrompt()),
    ("conflicts-single", ConflictConfig(target: .specific, me: goldenMe,
                                        specificPR: "337").buildPrompt()),
    ("audit", AuditConfig().buildPrompt()),
    ("audit-issues", AuditConfig(fixIssues: true).buildPrompt()),
    ("audit-prs", AuditConfig(openPRs: true).buildPrompt()),
    ("audit-all", AuditConfig(fixIssues: true, openPRs: true).buildPrompt()),
]
let goldenDir = try CoreAssets.coreDir().appendingPathComponent("golden-prompts")
if ProcessInfo.processInfo.environment["ARGENT_GOLDEN_WRITE"] == "1" {
    try FileManager.default.createDirectory(at: goldenDir, withIntermediateDirectories: true)
    for (name, prompt) in goldenModes {
        try prompt.write(to: goldenDir.appendingPathComponent("\(name).txt"),
                         atomically: true, encoding: .utf8)
    }
    print("wrote \(goldenModes.count) golden prompts to \(goldenDir.path)")
} else {
    for (name, prompt) in goldenModes {
        let url = goldenDir.appendingPathComponent("\(name).txt")
        let golden = (try? String(contentsOf: url, encoding: .utf8)) ?? ""
        check(!golden.isEmpty, "missing golden \(name).txt — run ARGENT_GOLDEN_WRITE=1")
        check(prompt == golden, "prompt \(name) drifted from its golden file")
    }
    print("golden-prompt assertions passed (\(goldenModes.count) modes)")
}

if ProcessInfo.processInfo.environment["ARGENT_UTILS_DUMP"] == "1" {
    section("live gh dump (cross-check vs Python)")
    let viewer = try await API.fetchViewerLogin()
    let realPRs = try await API.fetchOpenPRs()
    let realIssues = try await API.fetchOpenIssues()
    print("viewer @\(viewer) · PRs \(realPRs.count) · issues \(realIssues.count)")
    for kind in ToolKind.allCases {
        let c = ToolData.count(for: kind, prs: realPRs, issues: realIssues, me: viewer)
        print("\(kind.rawValue): \(c)")
    }
}

print("\nSMOKE OK")
