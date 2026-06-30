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
