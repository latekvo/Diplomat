import Foundation
import DiplomatCore

// A Linux-verifiable smoke test for the shared core: it loads the core/ assets,
// runs the filters on a synthetic fixture, assembles the three review prompts,
// and (with DIPLOMAT_DUMP=1) runs the real gh pipeline so the Swift core can
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

section("mesh model + snapshot decode")
let mesh = try CoreAssets.mesh()
print("mesh: platforms=\(mesh.platforms.map { $0.id }) tokens=\(mesh.tokens.map { $0.id }) "
    + "strategies=\(mesh.strategies.map { $0.id }) duties=\(mesh.duties.map { $0.id })")
// The duty catalog + placement strategies the panel edits — assert the shape the UI
// depends on, so a mesh.json edit that drops a field fails here (like catalog.json above).
check(mesh.duties.map { $0.id } == ["review", "conflicts", "audit"], "mesh duty ids")
check(mesh.tokens.map { $0.id } == ["ok", "low", "out"], "mesh token ids")
check(mesh.tierBounds == (1, 5, 3), "mesh tier bounds")
check(mesh.strategies.contains { $0.id == mesh.defaultStrategy }, "defaultStrategy is a real strategy")
// Strength words + trust vocabulary the console renders (the "tier N" → words fix).
check(mesh.tierLabel(1) == "Very strong" && mesh.tierLabel(5) == "Very light", "tier labels")
check(mesh.trustLevel("personal") != nil && mesh.trustLevel("foreign") != nil, "trust levels")
// Placement resolution: the audit duty carries a linux+macos spread; review/conflicts don't.
let auditPlacement = mesh.placement(for: "audit", overrides: nil)
check(auditPlacement.spread.map { $0.platform } == ["linux", "macos"], "audit spread platforms")
check(auditPlacement.spread.allSatisfy { $0.count == 1 }, "audit spread counts")
check(mesh.placement(for: "review", overrides: nil).spread.isEmpty, "review has no spread")
check(mesh.placement(for: "review", overrides: nil).tokenAware, "review is token-aware by default")
// A gossiped override wins over the catalog default (mirrors config.placement_for).
let overrideJSON = """
{"rev":3,"updatedBy":"nodeA","duties":{"review":{"strategy":"strongest-first","tokenAware":false}}}
""".data(using: .utf8)!
let overrides = try JSONDecoder().decode(MeshOverrides.self, from: overrideJSON)
check(overrides.rev == 3 && overrides.updatedBy == "nodeA", "overrides header")
let overridden = mesh.placement(for: "review", overrides: overrides)
check(overridden.strategy == "strongest-first" && !overridden.tokenAware, "override wins over default")
check(mesh.placement(for: "conflicts", overrides: overrides).strategy == mesh.defaultStrategy,
      "an un-overridden duty keeps its catalog default")
// The topology snapshot the UI renders (self + peers + assignments), decoded from a
// synthetic state.json shaped exactly like the node writes.
let snapJSON = """
{"pid":4242,"tcpPort":40878,"v":1,"linking":0,
 "self":{"id":"aaa","name":"here","platform":"macos","tier":2,"tokens":"ok",
         "strengthAuto":true,"tokensAuto":true,"tokensPct":0.81,"uptimeSecs":930.0,
         "tokensSessionPct":0.81,"tokensWeekPct":0.55},
 "peers":[{"id":"bbb","name":"lin","platform":"linux","tier":4,"tokens":"low",
           "link":"up","addr":"192.168.1.9:40878","lastSeenSecsAgo":1.4,"sees":["aaa"],
           "strengthAuto":false,"tokensAuto":true,"tokensPct":0.2,"uptimeSecs":187.0,
           "tokensSessionPct":0.2,"tokensWeekPct":0.4,
           "trust":"personal","fingerprint":"ff11","verified":true}],
 "banned":[{"fingerprint":"ee22","node":"ccc","label":"flaky-box",
            "reason":"accepted SzpontRequest b1c2 (review) and failed to deliver: no response to readiness reminder",
            "bannedAt":1784057240.5,"jobId":"b1c2"}],
 "assignments":{"audit":{"assigned":["aaa"],"shortfall":[{"missing":1,"platform":"linux"}]}}}
""".data(using: .utf8)!
check(MeshSnapshot.decode(snapJSON) != nil, "snapshot decodes")
let snap = MeshSnapshot.decode(snapJSON)!
check(snap.pid == 4242 && snap.tcpPort == 40878, "snapshot header")
check(snap.selfNode?.platform == "macos" && snap.selfNode?.tier == 2, "self node")
// The console fields (strength auto, auto token %, real uptime, trust) decode.
check(snap.selfNode?.strengthAuto == true && snap.selfNode?.tokensPct == 0.81, "self console fields")
// The real per-window quota percentages (OAuth usage probe) decode on both shapes.
check(snap.selfNode?.tokensSessionPct == 0.81 && snap.selfNode?.tokensWeekPct == 0.55,
      "self session/week quota decode")
check(snap.peers[0].tokensSessionPct == 0.2 && snap.peers[0].tokensWeekPct == 0.4,
      "peer session/week quota decode")
check(snap.peers[0].strengthAuto == false && snap.peers[0].uptimeSecs == 187.0, "peer strength/uptime")
check(snap.peers[0].trust == "personal" && snap.peers[0].verified == true, "peer trust decode")
// The ban-list mirror (foreign accountability, docs/szpontnet/13): who this node
// marked banned and why — the panel's mark + tooltip depend on these fields.
check(snap.banned.count == 1, "banned list decodes")
check(snap.banned[0].fingerprint == "ee22" && snap.banned[0].node == "ccc"
      && snap.banned[0].label == "flaky-box", "banned entry identity")
check(snap.banned[0].reason.contains("failed to deliver") && snap.banned[0].bannedAt > 0,
      "banned entry reason/time")
check(snap.banned[0].jobId == "b1c2", "banned entry names the undelivered job")
check(mesh.trustLevel("banned") != nil, "banned trust level ships in the catalog")
check(snap.peers.count == 1 && snap.peers[0].link == "up" && snap.peers[0].sees == ["aaa"], "peer decode")
check(snap.assignments["audit"]?.assigned == ["aaa"], "assignment decode")
check(snap.assignments["audit"]?.shortfall.first?.platform == "linux", "shortfall decode")
// `lastSeenSecsAgo` is intentionally excluded from peer equality (it ticks every write),
// so two snapshots that differ only in it compare equal — the change-detecting poll relies
// on this to not fire twice a second on an idle mesh.
// Differs from `snap` ONLY in the ticking fields (lastSeenSecsAgo + uptimeSecs); the
// stable fields (incl. strength/trust/token state) match, so they must compare equal.
let snap2 = MeshSnapshot.decode("""
{"pid":4242,"tcpPort":40878,"linking":0,
 "self":{"id":"aaa","name":"here","platform":"macos","tier":2,"tokens":"ok",
         "strengthAuto":true,"tokensAuto":true,"tokensPct":0.79,"uptimeSecs":999.0,
         "tokensSessionPct":0.81,"tokensWeekPct":0.55},
 "peers":[{"id":"bbb","name":"lin","platform":"linux","tier":4,"tokens":"low",
           "link":"up","addr":"192.168.1.9:40878","lastSeenSecsAgo":9.9,"sees":["aaa"],
           "strengthAuto":false,"tokensAuto":true,"tokensPct":0.2,"uptimeSecs":999.0,
           "tokensSessionPct":0.2,"tokensWeekPct":0.4,
           "trust":"personal","fingerprint":"ff11","verified":true}],
 "banned":[{"fingerprint":"ee22","node":"ccc","label":"flaky-box",
            "reason":"accepted SzpontRequest b1c2 (review) and failed to deliver: no response to readiness reminder",
            "bannedAt":1784057240.5,"jobId":"b1c2"}],
 "assignments":{"audit":{"assigned":["aaa"],"shortfall":[{"missing":1,"platform":"linux"}]}}}
""".data(using: .utf8)!)
check(snap == snap2, "snapshot equality ignores lastSeenSecsAgo/uptime/raw-fraction drift")
// A session-window percentage move IS a meaningful change — the quota indicator
// must repaint when the probe reports a new integer percent.
let snap3 = MeshSnapshot.decode("""
{"pid":4242,"tcpPort":40878,"linking":0,
 "self":{"id":"aaa","name":"here","platform":"macos","tier":2,"tokens":"ok",
         "strengthAuto":true,"tokensAuto":true,"tokensPct":0.81,"uptimeSecs":930.0,
         "tokensSessionPct":0.63,"tokensWeekPct":0.55},
 "peers":[{"id":"bbb","name":"lin","platform":"linux","tier":4,"tokens":"low",
           "link":"up","addr":"192.168.1.9:40878","lastSeenSecsAgo":1.4,"sees":["aaa"],
           "strengthAuto":false,"tokensAuto":true,"tokensPct":0.2,"uptimeSecs":187.0,
           "tokensSessionPct":0.2,"tokensWeekPct":0.4,
           "trust":"personal","fingerprint":"ff11","verified":true}],
 "banned":[{"fingerprint":"ee22","node":"ccc","label":"flaky-box",
            "reason":"accepted SzpontRequest b1c2 (review) and failed to deliver: no response to readiness reminder",
            "bannedAt":1784057240.5,"jobId":"b1c2"}],
 "assignments":{"audit":{"assigned":["aaa"],"shortfall":[{"missing":1,"platform":"linux"}]}}}
""".data(using: .utf8)!)
check(snap != snap3, "a session-quota percent move is a meaningful change")
// Trust + accounting fields (device-key fingerprints, personal/foreign verdicts,
// advertised stats, the published allowlist) — shaped exactly like the node writes
// them since the trust/load-balancing layer landed.
let trustJSONText = """
{"pid":4242,"tcpPort":40878,"v":1,
 "self":{"id":"aaa","name":"here","platform":"macos","tier":2,"tokens":"ok",
         "fingerprint":"f00d","stats":{"plan":"max-5x","usageAvg":0.8,"quotaLeft":4.2}},
 "peers":[{"id":"bbb","name":"lin","platform":"linux","tier":4,"tokens":"low",
           "link":"up","addr":"192.168.1.9:40878","lastSeenSecsAgo":1.4,"sees":["aaa"],
           "verified":true,"fingerprint":"beef","trust":"foreign","surplus":1.25,
           "stats":{"plan":"pro","usageAvg":0.25,"quotaLeft":1.5}}],
 "trusted":[{"fingerprint":"beef","label":"linux box"}],
 "assignments":{}}
"""
let trustSnap = MeshSnapshot.decode(trustJSONText.data(using: .utf8)!)!
check(trustSnap.selfNode?.fingerprint == "f00d", "self fingerprint decode")
check(trustSnap.selfNode?.stats?.plan == "max-5x", "self stats decode")
check(abs((trustSnap.selfNode?.surplus ?? 0) - 3.4) < 0.0001, "self surplus = quotaLeft − usageAvg")
check(trustSnap.peers[0].verified && trustSnap.peers[0].trust == "foreign", "peer trust decode")
check(trustSnap.peers[0].fingerprint == "beef" && trustSnap.peers[0].surplus == 1.25, "peer key + surplus")
check(trustSnap.trusted.first?.label == "linux box", "published allowlist decode")
// Legacy snapshots (pre-trust, pre-console) default to unverified/personal with
// neutral surplus and no stats.
let legacySnap = MeshSnapshot.decode("""
{"pid":4242,"tcpPort":40878,
 "self":{"id":"aaa","name":"here","platform":"macos","tier":2,"tokens":"ok"},
 "peers":[{"id":"bbb","name":"lin","platform":"linux","tier":4,"tokens":"low",
           "link":"up","sees":["aaa"]}],
 "assignments":{}}
""".data(using: .utf8)!)!
check(!legacySnap.peers[0].verified && legacySnap.peers[0].trust == "personal"
      && legacySnap.peers[0].surplus == 0, "pre-trust peer defaults")
check(legacySnap.selfNode?.surplus == 0 && legacySnap.selfNode?.stats == nil,
      "no stats ⇒ neutral surplus")
check(legacySnap.selfNode?.tokensSessionPct == nil && legacySnap.peers[0].tokensWeekPct == nil,
      "pre-probe snapshots ⇒ nil session/week quota (UI falls back to ≈estimate)")
check(legacySnap.trusted.isEmpty, "no published allowlist ⇒ empty")
// A trust flip IS a meaningful change (unlike lastSeenSecsAgo/uptime drift) — the
// poll must republish when a peer's verdict moves.
let trustSnap2 = MeshSnapshot.decode(
    trustJSONText.replacingOccurrences(of: "\"foreign\"", with: "\"personal\"").data(using: .utf8)!)!
check(trustSnap != trustSnap2, "trust flip is a meaningful change")
print("mesh assertions passed")

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

// ---- mesh coordination (work keys + assignment gate) ----
section("unified dispatch gate")
// The behavior matrix of the ONE pipeline both interfaces ride. PARITY: the
// Python twin (autofix.dispatch_decide etc.) asserts these exact semantics —
// and any new source asymmetry must be added HERE first, or it's a bug.
for src in [AgentDispatchGate.Source.panel, .auto] {
    check(AgentDispatchGate.decide(source: src, banned: true, agentOnPR: true,
                                   meshStandsDown: true) == .banned,
          "ban outranks everything for \(src.rawValue)")
    check(AgentDispatchGate.decide(source: src, banned: false, agentOnPR: true,
                                   meshStandsDown: true) == .inFlight,
          "a live agent on the PR blocks \(src.rawValue) — never double-spawn")
    check(AgentDispatchGate.decide(source: src, banned: false, agentOnPR: false,
                                   meshStandsDown: false) == .proceed,
          "clear board proceeds for \(src.rawValue)")
}
// The documented trigger asymmetries — and ONLY these:
check(AgentDispatchGate.decide(source: .auto, banned: false, agentOnPR: false,
                               meshStandsDown: true) == .standDown,
      "mesh gates auto origination")
check(AgentDispatchGate.decide(source: .panel, banned: false, agentOnPR: false,
                               meshStandsDown: true) == .proceed,
      "a human's click already decided placement — panel is never mesh-gated")
check(AgentDispatchGate.stealsFocus(.panel) && !AgentDispatchGate.stealsFocus(.auto),
      "panel comes forward, auto never steals focus")
check(AgentDispatchGate.label(source: .auto, core: "Review · #7", attemptNumber: 2)
      == "Auto · Review · #7 · retry 2", "auto label prefix + retry suffix")
check(AgentDispatchGate.label(source: .panel, core: "Review · #7") == "Review · #7",
      "panel label is the bare core")
check(AgentDispatchGate.bumpsCounter(source: .auto, attemptNumber: 1)
      && !AgentDispatchGate.bumpsCounter(source: .auto, attemptNumber: 2)
      && !AgentDispatchGate.bumpsCounter(source: .panel, attemptNumber: 1),
      "only a monitor's first dispatch counts as auto-handled")

section("autofix mesh coordination")
// PARITY fixtures: linux/tests/test_autofix.py asserts these exact strings — two
// nodes only dedupe origination when their derivations agree byte-for-byte
// (docs/szpontnet/12-work-claims.md).
check(AutofixMesh.workKey(kind: "review", prURL: "https://github.com/acme/app/pull/123",
                          headSha: "abc123")
      == "review:github.com/acme/app#123@abc123", "work key reference convention")
check(AutofixMesh.workKey(kind: "review-reply", prURL: "https://github.com/a/b/pull/9",
                          headSha: "F00")
      == "review-reply:github.com/a/b#9@F00")
check(AutofixMesh.workKey(kind: "conflicts", prURL: "https://github.com/a/b/pull/9",
                          headSha: "F00")
      == "conflicts:github.com/a/b#9@F00")
check(AutofixMesh.workKey(kind: "review", prURL: "https://GitHub.com/Acme/App/pull/5",
                          headSha: "AbC")
      == "review:github.com/Acme/App#5@AbC",
      "host lowercased; owner/repo/sha case preserved")
// Safe degradation: no sha / not a PR URL / garbage → "" (claim gate skipped).
check(AutofixMesh.workKey(kind: "review", prURL: "https://github.com/acme/app/pull/123",
                          headSha: "") == "")
check(AutofixMesh.workKey(kind: "review", prURL: "https://github.com/acme/app/issues/5",
                          headSha: "x") == "")
check(AutofixMesh.workKey(kind: "review", prURL: "https://github.com/acme/app",
                          headSha: "x") == "")
check(AutofixMesh.workKey(kind: "review", prURL: "not a url", headSha: "x") == "")
check(AutofixMesh.workKey(kind: "review", prURL: "", headSha: "x") == "")

print("autofix mesh coordination assertions passed")

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
let cleanFiles = ["packages/diplomat-core/src/foo.ts", "README.md"]
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
// Out-of-token-quota banners (no "API Error" prefix) are intentionally IGNORED — an
// out-of-quota agent can't progress until its window resets, so nudging just churns.
// Every format the CLI has used for the limit message must return false.
check(!ApiErrorMatch.looksLikeApiError("You've hit your weekly limit."))
check(!ApiErrorMatch.looksLikeApiError("You've hit your usage limit."))
check(!ApiErrorMatch.looksLikeApiError("Claude usage limit reached. Your limit will reset at 4pm (Europe/Warsaw)."))
check(!ApiErrorMatch.looksLikeApiError("5-hour limit reached ∙ resets 6pm"))
check(!ApiErrorMatch.looksLikeApiError("Weekly limit reached ∙ resets Oct 14"))
check(!ApiErrorMatch.looksLikeApiError("Session limit reached ∙ resets 3am"))
check(!ApiErrorMatch.looksLikeApiError("You are out of tokens for this period."))
// A quota banner SUPPRESSES a co-occurring API error in the same tail — the session
// idles on the limit, not the error, so we must not nudge it.
check(!ApiErrorMatch.looksLikeApiError("API Error: 529 Overloaded\nYou've hit your weekly limit."))
// Genuine transient errors still match.
check(ApiErrorMatch.looksLikeApiError("API Error: 429 rate_limit_error"))
check(ApiErrorMatch.looksLikeApiError("⏺ API Error: 529 Overloaded"))
// A bare "429 Rate limited" banner (no "API Error:" prefix) is a transient rate limit —
// the window resets in seconds — so it must nudge like any other server error.
check(ApiErrorMatch.looksLikeApiError("429 Rate limited"))
check(ApiErrorMatch.looksLikeApiError("✗ 429 Rate limited · retrying in 34s"))
check(ApiErrorMatch.looksLikeApiError("429 Too Many Requests"))
// But a 429 rate-limit co-occurring with a quota banner still idles on the quota.
check(!ApiErrorMatch.looksLikeApiError("429 Rate limited\nYou've hit your weekly limit."))
// And a bare 429 without a rate-limit phrase (e.g. a line count) must NOT trip it.
check(!ApiErrorMatch.looksLikeApiError("Deleted 429 stale entries"))
check(!ApiErrorMatch.looksLikeApiError("● Running tests… 47 passed"))
check(!ApiErrorMatch.looksLikeApiError("git push origin main"))
// "unable to connect" alone (no "api error") must NOT trip it — e.g. app logs.
check(!ApiErrorMatch.looksLikeApiError("curl: unable to connect to localhost:8080"))
// Ordinary prose about limits (rate limiter code, config talk) must NOT trip it.
check(!ApiErrorMatch.looksLikeApiError("bump the rate limit in config.yaml"))
check(!ApiErrorMatch.looksLikeApiError("the retry limit was reached, giving up"))
check(!ApiErrorMatch.looksLikeApiError(""))
print("api-error match assertions passed")

// ---- Idle-confirmation gate (nudge only a session stalled across two scans) ----
section("api-error idle-confirmation")
let errTail = "⏺ API Error: 529 Overloaded. check https://status.claude.com"
// First scan a tty is seen erroring (no prior tail) is NOT a confirmed stall — we wait
// for a second, identical scan before nudging.
check(!ApiErrorMatch.isConfirmedStall(previousTail: nil, currentTail: errTail))
// Two identical erroring scans ⇒ the session is static (genuinely stuck) ⇒ nudge.
check(ApiErrorMatch.isConfirmedStall(previousTail: errTail, currentTail: errTail))
// An actively-working session whose tail CHANGED between scans must NOT be nudged, even
// though both tails match — it's producing output, not stalled. Covers the CLI mid
// auto-retry (live countdown) and a session merely printing error strings while it works.
check(!ApiErrorMatch.isConfirmedStall(previousTail: "API Error: 429 rate limited · retry in 34s",
                                      currentTail: "API Error: 429 rate limited · retry in 12s"))
check(!ApiErrorMatch.isConfirmedStall(previousTail: "line one\n⏺ 429 Rate limited",
                                      currentTail: "line one\n⏺ 429 Rate limited\n⏺ Reading file.swift"))
// A stable tail that ISN'T an API error is never a stall (ordinary idle prompt sitting
// there unchanged must not be nudged just because it stopped moving).
check(!ApiErrorMatch.isConfirmedStall(previousTail: "$ git status\nnothing to commit",
                                      currentTail: "$ git status\nnothing to commit"))
print("api-error idle-confirmation assertions passed")

// ---- Activity-feed category taxonomy (panel filter chips) ----
section("audit category")
check(AuditCategory.of(action: "review") == .review)
check(AuditCategory.of(action: "review-req") == .review, "auto review-request groups under Reviews")
check(AuditCategory.of(action: "review-reply") == .reply, "my-PR review responses are their own type")
check(AuditCategory.of(action: "conflicts") == .conflicts)
check(AuditCategory.of(action: "audit") == .audit)
check(AuditCategory.of(action: "nudge") == .apiRestart, "API-error nudge is the API-restart type")
// Out-of-quota stalls are their own type (the auto-resume itself is disabled, but the
// historical `quota-stall` rows still get their own chip, not lumped into System).
check(AuditCategory.of(action: "quota-stall") == .quota, "quota stalls are the Out-of-quota type")
check(AuditCategory.of(action: "quota-resume") == .quota)
check(AuditCategory.of(action: "merge") == .merge)
check(AuditCategory.of(action: "merge-failed") == .merge)
check(AuditCategory.of(action: "ban") == .bans, "bans are their own category")
check(AuditCategory.of(action: "unban") == .bans)
// LAN-mesh coordination rows (peer churn, duty takeovers, dispatches) get their own chip.
check(AuditCategory.of(action: "mesh-up") == .mesh)
check(AuditCategory.of(action: "mesh-peer-down") == .mesh, "peer loss is a Mesh row")
check(AuditCategory.of(action: "mesh-takeover") == .mesh, "duty takeovers are Mesh rows")
check(AuditCategory.of(action: "mesh-dispatch") == .mesh)
check(AuditCategory.of(action: "mesh-dispatch-failed") == .mesh)
check(AuditCategory.of(action: "mesh-spawn") == .mesh)
// Device / health / anything unmapped falls through to System so no row is uncategorized.
check(AuditCategory.of(action: "kill-device") == .system)
check(AuditCategory.of(action: "repair-done") == .system)
check(AuditCategory.of(action: "allocator-install") == .system)
check(AuditCategory.of(action: "poll-failed") == .system)
check(AuditCategory.of(action: "spawn-failed") == .system)
check(AuditCategory.of(action: "warn") == .system)
check(AuditCategory.of(action: "totally-new-verb") == .system, "unknown verbs never vanish")
check(AuditCategory.displayOrder.count == AuditCategory.allCases.count)
print("audit category assertions passed")

// ---- Golden prompts (cross-platform parity) ----
// Every prompt mode both front-ends can assemble is compared byte-for-byte against a
// committed golden file in core/golden-prompts/. The Linux tests assert the SAME
// files, so Swift and Python can only drift from each other by failing one CI job.
// Regenerate after an intentional core/*.json change: DIPLOMAT_GOLDEN_WRITE=1 swift run
// DiplomatCoreSmoke.
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
if ProcessInfo.processInfo.environment["DIPLOMAT_GOLDEN_WRITE"] == "1" {
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
        check(!golden.isEmpty, "missing golden \(name).txt — run DIPLOMAT_GOLDEN_WRITE=1")
        check(prompt == golden, "prompt \(name) drifted from its golden file")
    }
    print("golden-prompt assertions passed (\(goldenModes.count) modes)")
}

if ProcessInfo.processInfo.environment["DIPLOMAT_DUMP"] == "1" {
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
