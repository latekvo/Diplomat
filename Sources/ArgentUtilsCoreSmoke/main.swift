import Foundation
import ArgentUtilsCore

// A Linux-verifiable smoke test for the shared core: it loads the core/ assets,
// runs the filters on a synthetic fixture, assembles the three review prompts,
// and (with ARGENT_UTILS_DUMP=1) runs the real gh pipeline so the Swift core can
// be cross-checked against the Linux Python front-end.

func section(_ s: String) { print("\n== \(s) ==") }

section("core assets")
let cfg = try CoreAssets.config()
print("config: \(cfg.owner)/\(cfg.repo)")
print("catalog: \(ToolKind.allCases.map { $0.rawValue })")
print("titles : \(ToolKind.allCases.map { $0.title })")
let f = try CoreAssets.filters()
print("filters: skillSuffix=\(f.skillSuffix) staleDays=\(f.staleReadyDays) approved=\(f.approvedDecision)")
print("depths : \(ReviewCatalog.depths().map { $0.id }) default=\(ReviewCatalog.defaultDepthID())")

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
let look = ToolData.lookup(101, prs: prs, issues: issues, me: me, visible: ToolKind.allCases)
print("lookup #101 on: \(look.onLists.map { $0.rawValue }) — \(look.presence)")

section("PR-reference parsing")
func single(_ pr: String) -> ReviewConfig {
    ReviewConfig(depth: "max", target: .specific, me: me, specificPR: pr)
}
let urlRef = PRRef.parse("https://github.com/\(cfg.owner)/\(cfg.repo)/pull/337/files",
                         owner: cfg.owner, repo: cfg.repo)
assert(urlRef.number == 337 && urlRef.isValid && !urlRef.repoMismatch)
assert(PRRef.parse("#42", owner: cfg.owner, repo: cfg.repo).number == 42)
assert(PRRef.parse("\(cfg.owner)/\(cfg.repo)#9", owner: cfg.owner, repo: cfg.repo).number == 9)
let wrongRepo = PRRef.parse("https://github.com/other/proj/pull/5", owner: cfg.owner, repo: cfg.repo)
assert(wrongRepo.number == 5 && wrongRepo.repoMismatch && !wrongRepo.isValid)
assert(PRRef.parse("not-a-pr", owner: cfg.owner, repo: cfg.repo).number == nil)
print("PR-reference assertions passed")

section("review prompts")
let mine = ReviewConfig(depth: "max", me: me)
let other = ReviewConfig(depth: "max", target: .someone, username: "someuser")
print("mine valid=\(mine.valid()) | other valid=\(other.valid()) | single valid=\(single("337").valid())")
assert(mine.buildPrompt().contains("mark it ready for review"))
assert(!mine.buildPrompt().contains("POST a pull-request review"))
assert(other.buildPrompt().contains("POST a pull-request review"))
// Someone else's PRs are review-only: a hard no-commit guard, and the
// commit-authoring guidance is dropped (we never touch their branch).
assert(other.buildPrompt().contains("ABSOLUTELY DO NOT touch their branch"))
assert(!other.buildPrompt().contains("No AI attribution"))
// My PRs do commit, so no review-only guard and the attribution rule stays.
assert(!mine.buildPrompt().contains("ABSOLUTELY DO NOT touch their branch"))
assert(single("337").buildPrompt().hasPrefix("Review PR #337 in \(cfg.owner)/\(cfg.repo)."))
// A pasted URL for the target repo resolves to the same single-PR prompt.
assert(single("https://github.com/\(cfg.owner)/\(cfg.repo)/pull/337").valid())
assert(single("https://github.com/\(cfg.owner)/\(cfg.repo)/pull/337").buildPrompt()
    .hasPrefix("Review PR #337 in \(cfg.owner)/\(cfg.repo)."))
// A URL for a different repo is rejected.
assert(!single("https://github.com/other/proj/pull/9").valid())
assert(mine.buildPrompt().contains("No AI attribution"))

// A specific PR may be mine OR someone else's, so its prompt is author-gated: it
// polls the author, then splits into CASE A (mine → fix on branch, mark ready) and
// CASE B (theirs → review-only, never touch the branch, and DO NOT mark ready).
let singlePrompt = single("337").buildPrompt()
assert(singlePrompt.contains("WHO AUTHORED IT"))
assert(singlePrompt.contains("CASE A") && singlePrompt.contains("CASE B"))
// CASE A keeps the fix-on-branch + mark-ready + attribution behaviour…
assert(singlePrompt.contains("on the PR's branch"))   // depth onBranch fix step
assert(singlePrompt.contains("mark it ready for review"))
assert(singlePrompt.contains("No AI attribution"))
// …CASE B is the hard look-don't-touch guard, with an explicit do-not-advance line.
assert(singlePrompt.contains("ABSOLUTELY DO NOT touch their branch"))
assert(singlePrompt.contains("isn't yours to advance"))
// With mark-ready off, neither the mark-ready block nor (since target≠someone) the
// generic sweep markReady survives — proving the toggle gates only CASE A.
let singleNoReady = ReviewConfig(depth: "max", target: .specific, me: me,
                                 markReady: false, specificPR: "337").buildPrompt()
assert(!singleNoReady.contains("mark it ready for review"))
assert(singleNoReady.contains("isn't yours to advance"))
print("prompt assembly assertions passed")

section("conflict prompts")
let cMine = ConflictConfig(me: me)
let cOther = ConflictConfig(target: .someone, username: "someuser")
let cSingle = ConflictConfig(target: .specific, specificPR: "337")
print("mine valid=\(cMine.isValid) | other valid=\(cOther.isValid) | single valid=\(cSingle.isValid)")
assert(cMine.isValid && cOther.isValid && cSingle.isValid)
assert(!ConflictConfig(target: .specific, specificPR: "nope").isValid)
// The single-PR field accepts a URL for the target repo, rejects other repos.
assert(ConflictConfig(target: .specific,
                      specificPR: "https://github.com/\(cfg.owner)/\(cfg.repo)/pull/337").isValid)
assert(!ConflictConfig(target: .specific, specificPR: "https://github.com/x/y/pull/1").isValid)
assert(cMine.buildPrompt().contains("authored by @\(me)"))
assert(cMine.buildPrompt().contains("For each, merge the latest `origin/main`"))
assert(cSingle.buildPrompt().hasPrefix("Take PR #337 in \(cfg.owner)/\(cfg.repo)."))
assert(cSingle.buildPrompt().contains("Merge the latest `origin/main`"))
assert(cMine.buildPrompt().contains("No AI attribution"))
print("conflict prompt assertions passed")

section("audit prompts")
// A whole-repo E2E audit needs no input (always valid), and the hard-repro bar is
// present in every variant. The two toggles independently gate the optional blocks.
let aBase = AuditConfig(me: me)
print("audit valid=\(aBase.isValid)")
assert(aBase.isValid && AuditConfig().isValid)
assert(aBase.buildPrompt().contains("100% CERTAINTY"))
assert(aBase.buildPrompt().hasPrefix("Run a FULL end-to-end test of the ENTIRE \(cfg.owner)/\(cfg.repo)"))
// Reproduction must be driven on a real simulator/emulator (always present, in bar).
assert(aBase.buildPrompt().contains("SIMULATOR / EMULATOR"))
// Severity classification (H/M/L) is always present, even in the read-only default.
assert(aBase.buildPrompt().contains("HIGH") && aBase.buildPrompt().contains("LOW"))
// Default (find-only): read-only, no issue-handling, no PRs (so no 20-LOC PR gate).
assert(aBase.buildPrompt().contains("READ-ONLY audit"))
assert(!aBase.buildPrompt().contains("OPEN ISSUES"))
assert(!aBase.buildPrompt().contains("focused pull request"))
assert(!aBase.buildPrompt().contains("20 lines"))
// fixIssues adds the bug-issue block, explicit about skipping feature requests.
let aIssues = AuditConfig(me: me, fixIssues: true)
assert(aIssues.buildPrompt().contains("OPEN ISSUES"))
assert(aIssues.buildPrompt().contains("SKIP every feature request"))
assert(aIssues.buildPrompt().contains("READ-ONLY audit"))   // still read-only
// openPRs swaps the read-only guard for the open-a-PR block + no-attribution.
let aPRs = AuditConfig(me: me, openPRs: true)
assert(aPRs.buildPrompt().contains("focused pull request"))
assert(aPRs.buildPrompt().contains("DRAFT"))   // every opened PR must be a draft
assert(aPRs.buildPrompt().contains("DUPLICATE") && aPRs.buildPrompt().contains("gh pr diff"))
assert(aPRs.buildPrompt().contains("20 lines"))   // Low/nitpick PRs only when fix < 20 LOC
assert(aPRs.buildPrompt().contains("No AI attribution"))
assert(!aPRs.buildPrompt().contains("READ-ONLY audit"))
// Both on: issue-handling + PRs together.
let aBoth = AuditConfig(me: me, fixIssues: true, openPRs: true)
assert(aBoth.buildPrompt().contains("OPEN ISSUES") && aBoth.buildPrompt().contains("focused pull request"))
print("audit prompt assertions passed")

// ---- Auto-fix monitor diff (edge-triggering) ----
section("autofix diff")
func snap(_ n: Int, mergeable: String = "MERGEABLE", decision: String = "", threads: Int = 0) -> PRSnapshot {
    PRSnapshot(number: n, title: "PR \(n)", url: "u\(n)", headRef: "b\(n)", isDraft: false,
               mergeable: mergeable, reviewDecision: decision, threadsUnresolved: threads)
}
// First run: everything seeds, nothing fires.
let base = AutofixDiff.compute(prior: [:], now: [snap(1), snap(2, mergeable: "CONFLICTING", threads: 3)])
assert(base.events.isEmpty, "baseline must not dispatch")
assert(base.fingerprints.count == 2)
// Clean -> conflicting fires exactly one conflict event.
let c = AutofixDiff.compute(prior: base.fingerprints, now: [snap(1, mergeable: "CONFLICTING"), snap(2, mergeable: "CONFLICTING", threads: 3)])
assert(c.events == [.conflict(snap(1, mergeable: "CONFLICTING"))], "clean->conflicting fires once")
// More unresolved threads OR a new CHANGES_REQUESTED fires a review event.
let rPrior = [1: PRFingerprint(mergeable: "MERGEABLE", reviewDecision: "", threadsUnresolved: 1)]
assert(AutofixDiff.compute(prior: rPrior, now: [snap(1, threads: 4)]).events == [.review(snap(1, threads: 4))])
assert(AutofixDiff.compute(prior: rPrior, now: [snap(1, decision: "CHANGES_REQUESTED", threads: 1)]).events
       == [.review(snap(1, decision: "CHANGES_REQUESTED", threads: 1))])
// Our own "Fixed in <hash>" replies (threads resolved, verdict unchanged) must NOT fire.
let selfReply = [1: PRFingerprint(mergeable: "MERGEABLE", reviewDecision: "CHANGES_REQUESTED", threadsUnresolved: 5)]
assert(AutofixDiff.compute(prior: selfReply, now: [snap(1, decision: "CHANGES_REQUESTED", threads: 0)]).events.isEmpty,
       "resolving threads must not retrigger")
// UNKNOWN mergeable carries the prior value forward — no phantom conflict.
let unk = [1: PRFingerprint(mergeable: "MERGEABLE", reviewDecision: "", threadsUnresolved: 0)]
let u = AutofixDiff.compute(prior: unk, now: [snap(1, mergeable: "UNKNOWN")])
assert(u.events.isEmpty && u.fingerprints[1]?.mergeable == "MERGEABLE", "UNKNOWN carries prior forward")
print("autofix diff assertions passed")

// ---- known-mine single-PR review prompt (auto-fix monitor) ----
section("known-mine review prompt")
let km = ReviewConfig(depth: "deep", target: .specific, me: "latekvo",
                      markReady: false, leaveReviews: false, replyToReviews: true,
                      specificPR: "440", specificAuthor: .mine).buildPrompt()
// No author poll, no CASE A/B branching — we already know it's ours.
assert(!km.contains("WHO AUTHORED IT"), "known-mine skips the author poll")
assert(!km.contains("CASE A") && !km.contains("CASE B"), "known-mine has no case branching")
assert(!km.contains("SOMEONE ELSE'S"), "known-mine has no review-only block")
// But it IS the fix-on-branch, no-attribution disposition on the right PR, and it puts
// resolving existing reviewer findings FIRST (screen/verify/fix-or-dismiss/respond).
assert(km.contains("Review PR #440"))
assert(km.contains("MINE") && km.contains("full authority"))
assert(km.contains("FIRST AND FOREMOST, resolve every reviewer finding"))
assert(km.contains(#""Fixed in <commit_hash>""#))
// The reviewer-findings step comes before the agent's own deep-review approach.
assert(km.range(of: "FIRST AND FOREMOST")!.lowerBound < km.range(of: "dispatch swarms of agents")!.lowerBound)
assert(km.contains("fix it directly on the PR's branch"))
assert(km.contains("No AI attribution"))
assert(!km.contains("mark it ready for review"), "markReady=false omits the block")
// The gated (author-unknown) path still branches, for the manual Specific-PR wizard.
let gated = ReviewConfig(depth: "deep", target: .specific, me: "latekvo", specificPR: "440").buildPrompt()
assert(gated.contains("WHO AUTHORED IT") && gated.contains("CASE A") && gated.contains("CASE B"))
print("known-mine review prompt assertions passed")

// ---- known-theirs comprehensive review (review-request monitor) ----
section("known-theirs review prompt")
let kt = ReviewConfig(depth: "max", target: .specific, me: "latekvo",
                      markReady: false, leaveReviews: true, replyToReviews: false,
                      specificPR: "500", specificAuthor: .theirs).buildPrompt()
assert(!kt.contains("WHO AUTHORED IT"), "known-theirs skips the author poll")
assert(!kt.contains("CASE A") && !kt.contains("CASE B"), "no case branching")
assert(kt.contains("SOMEONE ELSE'S"), "review-only framing")
assert(kt.contains("ABSOLUTELY DO NOT touch their branch"), "reviewOnly block present")
assert(kt.contains("POST a pull-request review"), "leaveReviews block present")
assert(kt.contains("Do NOT mark this PR ready"), "otherNoMarkReady present")
assert(kt.contains("SECOND, independent verification"), "max-depth fragment present")
// No auto-verdict — the final approve is the user's, not the agent's.
assert(kt.contains("Do NOT submit an APPROVE"), "no-verdict instruction present")
assert(kt.contains("PR #500 looks clean"), "no-verdict {pr} substituted")
assert(!kt.contains("still APPROVE"), "the finalPass approve-verdict block is gone")
assert(!kt.contains("fix it directly on the PR's branch"), "never fixes someone else's branch")
assert(!kt.contains("No AI attribution"), "no commits ⇒ no attribution block")
print("known-theirs review prompt assertions passed")

// ---- known-theirs WITH verdict (trusted author: member/maintainer/contributor) ----
// The review-request monitor sets finalPass=true when the PR author is trusted, so the
// auto-review closes with an APPROVE/changes-requested verdict instead of comments only.
section("known-theirs review prompt (trusted author → verdict)")
let ktv = ReviewConfig(depth: "max", target: .specific, me: "latekvo",
                       markReady: false, leaveReviews: true, replyToReviews: false,
                       specificPR: "500", finalPass: true, specificAuthor: .theirs).buildPrompt()
assert(ktv.contains("SOMEONE ELSE'S"), "review-only framing still present with verdict")
assert(ktv.contains("still APPROVE"), "trusted author ⇒ finalPass APPROVE-verdict block present")
assert(!ktv.contains("Do NOT submit an APPROVE"), "trusted author ⇒ no no-verdict block")
print("known-theirs (trusted author) review prompt assertions passed")

// ---- Auto-review verdict policy (skill / installer / community suppressors) ----
section("verdict policy")
let cleanFiles = ["packages/argent-core/src/foo.ts", "README.md"]
let skillFiles = ["src/skills/argent-x/SKILL.md"]
let installerFiles = ["packages/argent-installer/index.ts"]
let allOn = VerdictPolicy()   // every suppressor on — the default policy
// Trusted author, nothing sensitive touched → verdict allowed.
assert(allOn.allowsVerdict(files: cleanFiles, authorAssociation: "MEMBER"))
assert(allOn.allowsVerdict(files: cleanFiles, authorAssociation: "CONTRIBUTOR"))
// Each suppressor independently withholds the verdict.
assert(!allOn.allowsVerdict(files: skillFiles, authorAssociation: "MEMBER"), "skill ⇒ no verdict")
assert(!allOn.allowsVerdict(files: installerFiles, authorAssociation: "OWNER"), "installer ⇒ no verdict")
assert(!allOn.allowsVerdict(files: cleanFiles, authorAssociation: "NONE"), "community ⇒ no verdict")
// The exact association GitHub returns for an outside contributor.
assert(!allOn.allowsVerdict(files: cleanFiles, authorAssociation: "FIRST_TIME_CONTRIBUTOR"), "outside author ⇒ no verdict")
// Reasons are reported, in order, and can stack.
assert(allOn.withholdReasons(files: skillFiles, authorAssociation: "NONE")
        == ["touches a SKILL", "community PR"], "stacked reasons in order")
assert(allOn.withholdReasons(files: cleanFiles, authorAssociation: "MEMBER").isEmpty, "no reasons ⇒ verdict")
// Turning one suppressor off re-enables the verdict for only that class.
let skillOff = VerdictPolicy(withholdOnSkill: false)
assert(skillOff.allowsVerdict(files: skillFiles, authorAssociation: "MEMBER"), "skill off ⇒ skill PR gets verdict")
assert(!skillOff.allowsVerdict(files: installerFiles, authorAssociation: "MEMBER"), "installer still withheld")
// Everything off ⇒ always a verdict, regardless of files/author.
let allOff = VerdictPolicy(withholdOnSkill: false, withholdOnInstaller: false, withholdOnCommunity: false)
assert(allOff.allowsVerdict(files: skillFiles + installerFiles, authorAssociation: "NONE"), "all off ⇒ always verdict")
print("verdict policy assertions passed")

// ---- Claude API-error detection (terminal auto-continue) ----
section("api-error match")
assert(ApiErrorMatch.looksLikeApiError("⏺ API Error: 529 Overloaded. If it persists, check https://status.claude.com."))
assert(ApiErrorMatch.looksLikeApiError("API Error: 500 Internal Server Error"))
assert(ApiErrorMatch.looksLikeApiError("something API error, see status.claude.com for details"))
// Codeless connectivity failures (network out / DNS / timeout) must also match.
assert(ApiErrorMatch.looksLikeApiError("⏺ API Error: Unable to connect to API"))
assert(ApiErrorMatch.looksLikeApiError("API Error: Connection error."))
assert(ApiErrorMatch.looksLikeApiError("API Error: getaddrinfo ENOTFOUND api.anthropic.com"))
assert(!ApiErrorMatch.looksLikeApiError("● Running tests… 47 passed"))
assert(!ApiErrorMatch.looksLikeApiError("git push origin main"))
// "unable to connect" alone (no "api error") must NOT trip it — e.g. app logs.
assert(!ApiErrorMatch.looksLikeApiError("curl: unable to connect to localhost:8080"))
assert(!ApiErrorMatch.looksLikeApiError(""))
print("api-error match assertions passed")

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

private extension ReviewConfig {
    func valid() -> Bool { isValid }
}
