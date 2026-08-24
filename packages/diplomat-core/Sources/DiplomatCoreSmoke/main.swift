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

// Every prompt built below names the model this machine's agents run on in its
// attribution tag (`AgentModel`), which would otherwise bake whatever the developer
// happens to be running into the golden files they regenerate. Point every one of the
// documented overrides at a path inside a scratch directory that is never created, so
// the prompts assembled here are the model-free ones the goldens hold — on a CI runner
// and on a working machine alike. The Linux suite's conftest fences the same names.
let fence = FileManager.default.temporaryDirectory
    .appendingPathComponent("diplomat-smoke-no-agent-state", isDirectory: true)
setenv("DIPLOMAT_CONFIG", fence.appendingPathComponent("config.json").path, 1)
setenv("DIPLOMAT_CLAUDE_DIR", fence.appendingPathComponent("claude").path, 1)
setenv("DIPLOMAT_HERMES_CONFIG", fence.appendingPathComponent("hermes.yaml").path, 1)
setenv("DIPLOMAT_OPENCODE_CONFIG_DIR", fence.appendingPathComponent("opencode-config").path, 1)
setenv("DIPLOMAT_OPENCODE_STATE_DIR", fence.appendingPathComponent("opencode-state").path, 1)

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
// What a Review-PRs sweep expands into — one queued review per PR, so this decides
// how many agents a single press eventually starts. #102 is bob's only draft; #101
// and #103..105 are other people's or not drafts.
check(Filters.sweptPRs(prs, author: "bob", includeDrafts: true,
                       includeReady: false).map { $0.number } == [102],
      "a drafts-only sweep covers that author's drafts")
check(Filters.sweptPRs(prs, author: "bob", includeDrafts: false,
                       includeReady: true).isEmpty,
      "…and a ready-only sweep of the same author covers nothing")
check(Filters.sweptPRs(prs, author: "BOB", includeDrafts: true,
                       includeReady: true).map { $0.number } == [102],
      "the handle is typed by hand, and GitHub logins are case-insensitive")
check(Filters.sweptPRs(prs, author: "", includeDrafts: true, includeReady: true).isEmpty,
      "no handle yet sweeps nobody, rather than everybody")
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
check(mesh.duties.map { $0.id } == ["review", "issues", "conflicts", "audit"], "mesh duty ids")
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
check(mesh.placement(for: "issues", overrides: nil).spread.isEmpty, "issues has no spread")
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
// A gossiped override can carry a MALFORMED spread entry (one that names no platform,
// or a non-object) — the wire layer tolerates garbage (config.Placement._parse_spread).
// One bad spread entry must skip only THAT entry, never collapse the whole duties map
// (which used to hide EVERY operator placement override in the topology view). Mirrors
// the Python port: both duties survive, the bad entry is dropped, strategies are kept.
// 'audit' carries a non-OBJECT duty value (junk) — it must drop only that duty, not
// collapse the map; 'review'/'conflicts' are valid objects with malformed spreads.
let malformedJSON = """
{"rev":4,"updatedBy":"nodeB","duties":{
  "review":{"strategy":"surplus-first","spread":[{"count":2},"garbage",{"platform":""}]},
  "conflicts":{"strategy":"round-robin","spread":[{"platform":"android","count":"x"},{"platform":"linux"}]},
  "audit":"not-an-object"
}}
""".data(using: .utf8)!
let malformed = try JSONDecoder().decode(MeshOverrides.self, from: malformedJSON)
check(malformed.duties.keys.sorted() == ["conflicts", "review"],
      "a malformed spread entry (or non-object duty value) must not drop the whole map")
// The dropped 'audit' duty falls back to its catalog default (linux+macos spread).
check(mesh.placement(for: "audit", overrides: malformed).spread.map { $0.platform } == ["linux", "macos"],
      "a duty with a junk override value resolves to the catalog default")
let mReview = mesh.placement(for: "review", overrides: malformed)
check(mReview.strategy == "surplus-first" && mReview.spread.isEmpty,
      "review keeps its strategy; its all-malformed spread resolves to empty")
let mConflicts = mesh.placement(for: "conflicts", overrides: malformed)
check(mConflicts.strategy == "round-robin", "conflicts keeps its overridden strategy")
// android: bad count "x" falls back to the schema default 1; linux: missing count → 1.
check(mConflicts.spread.map { $0.platform } == ["android", "linux"]
      && mConflicts.spread.allSatisfy { $0.count == 1 },
      "malformed/absent spread counts fall back to 1, matching _parse_spread")
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
// The ban-list mirror (foreign accountability, szpontnet-spec/docs/13): who this node
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
// The discoverability banner keys on the node's OWN diagnosis: `beaconBlockReason`
// travels beside `beaconBlocked` so the UI shows a Local-Network gate vs a genuinely
// downed network, not a fixed "allow Python" guess. An older node predating the field
// decodes to "" — the Local-Network case — so the banner stays backward-compatible.
let blockedSnap = MeshSnapshot.decode("""
{"pid":1,"tcpPort":40878,"linking":0,"peers":[],"assignments":{},
 "beaconBlocked":true,"beaconBlockReason":"network-down"}
""".data(using: .utf8)!)!
check(blockedSnap.beaconBlocked && blockedSnap.beaconBlockReason == "network-down",
      "beaconBlockReason decodes beside beaconBlocked")
let legacyBlocked = MeshSnapshot.decode("""
{"pid":1,"tcpPort":40878,"linking":0,"peers":[],"assignments":{},"beaconBlocked":true}
""".data(using: .utf8)!)!
check(legacyBlocked.beaconBlocked && legacyBlocked.beaconBlockReason == "",
      "a node without beaconBlockReason defaults to \"\" (the Local-Network case)")
// Trust + accounting fields (device-key fingerprints, personal/foreign verdicts,
// advertised stats, the published allowlist) — shaped exactly like the node writes
// them since the trust/load-balancing layer landed.
let trustJSONText = """
{"pid":4242,"tcpPort":40878,"v":1,
 "self":{"id":"aaa","name":"here","platform":"macos","tier":2,"tokens":"ok",
         "fingerprint":"f00d","stats":{"plan":"max-5x","usageAvg":0.8,"quotaLeft":4.2,"surplus":1.75}},
 "peers":[{"id":"bbb","name":"lin","platform":"linux","tier":4,"tokens":"low",
           "link":"up","addr":"192.168.1.9:40878","lastSeenSecsAgo":1.4,"sees":["aaa"],
           "verified":true,"fingerprint":"beef","trust":"foreign","surplus":1.25,
           "stats":{"plan":"pro","usageAvg":0.25,"quotaLeft":1.5,"surplus":1.25}}],
 "trusted":[{"fingerprint":"beef","label":"linux box"}],
 "assignments":{}}
"""
let trustSnap = MeshSnapshot.decode(trustJSONText.data(using: .utf8)!)!
check(trustSnap.selfNode?.fingerprint == "f00d", "self fingerprint decode")
check(trustSnap.selfNode?.stats?.plan == "max-5x", "self stats decode")
// surplus is the advertised burn-down ratio (from stats.surplus), NOT the absolute
// quotaLeft − usageAvg — a small plan ahead of its reset can out-rank a big idle one.
check(abs((trustSnap.selfNode?.surplus ?? 0) - 1.75) < 0.0001, "self surplus = advertised pace")
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
      && legacySnap.peers[0].surplus == MeshStats.neutralSurplus, "pre-trust peer defaults")
check(legacySnap.selfNode?.surplus == MeshStats.neutralSurplus && legacySnap.selfNode?.stats == nil,
      "no stats ⇒ neutral surplus (on the burn-down line, 1.0)")
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

// A sweep is queued as one review per PR, and `forPR` is the config behind each: the
// operator's depth and toggles, narrowed to that PR, with the disposition the sweep
// already knew rather than a fresh `gh` poll per PR.
let sweptOne = other.forPR(337)
check(sweptOne.target == .specific && sweptOne.specificPR == "337"
      && sweptOne.specificAuthor == .theirs && sweptOne.depth == other.depth,
      "a swept PR keeps the sweep's depth and inherits its disposition")
check(mine.forPR(337).specificAuthor == .mine, "…and my own sweep hands down .mine")
check(sweptOne.buildPrompt().hasPrefix("Review PR #337 in \(cfg.owner)/\(cfg.repo)."),
      "a queued review is the single-PR prompt, not the sweep's")
check(mine.sweepAuthor == me && other.sweepAuthor == "someuser"
      && single("337").sweepAuthor.isEmpty,
      "the sweep's author is the real login, never the prompt's \"me\" fallback")
check(ReviewConfig(depth: "max", me: "").sweepAuthor.isEmpty
      && ReviewConfig(depth: "max", me: "").authorHandle == "me",
      "…which is the whole difference: an unresolved login sweeps nobody")

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

section("agent runner")
// The runner is chosen in ONE config file that both front-ends read, so a machine
// can hand a mesh job to the other platform and must get the same agent out of it.
// These are the exact strings `tests/test_runner.py` pins on the Python side; the
// two are compared literally, because a difference here is two applets spawning
// different CLIs from one setting.
check(AgentRunner.from("") == .claude, "an unset runner must be Claude Code")
check(AgentRunner.from("gpt-cli") == .claude, "an unknown runner must degrade, not fail")
check(AgentRunner.from("opencode") == .opencode)
check(AgentRunner.from("hermes") == .hermes)
check(AgentRunner.claude.agentCommand(promptFile: "/tmp/p.txt", model: "ignored")
        == "claude \"$(cat '/tmp/p.txt')\"",
      "the Claude command is what every existing install is mid-flight on")
check(AgentRunner.opencode.agentCommand(promptFile: "/tmp/p.txt")
        == "\(AgentRunner.permissionEnv)='\(AgentRunner.permissionValue)' "
         + "opencode --prompt \"$(cat '/tmp/p.txt')\"")
check(AgentRunner.opencode.agentCommand(promptFile: "/tmp/p.txt", model: "openrouter/moonshotai/kimi-k2")
        .hasSuffix("opencode -m 'openrouter/moonshotai/kimi-k2' --prompt \"$(cat '/tmp/p.txt')\""),
      "a configured model must reach the agent")
check(!AgentRunner.opencode.agentCommand(promptFile: "/tmp/p.txt", model: "  ").contains(" -m "),
      "a blank model must leave OpenCode's own choice alone")
// Hermes is windowed like the other two: `--tui` is what the operator watches and types
// into, and `-q` is what makes the prompt the session's opening message — the key
// `HermesStore.isOurs` matches a run to its session by.
check(AgentRunner.hermes.agentCommand(promptFile: "/tmp/p.txt")
        == "hermes chat --tui --yolo -q \"$(cat '/tmp/p.txt')\"")
check(AgentRunner.hermes.agentCommand(promptFile: "/tmp/p.txt", model: "openai/gpt-5.2")
        == "hermes chat --tui --yolo -m 'openai/gpt-5.2' -q \"$(cat '/tmp/p.txt')\"")
check(AgentRunner.hermes.setupCommand == "hermes setup; hermes status",
      "each runner's provider wizard is its own; Diplomat holds no key for either")
// Every scan that counts, adopts or reaps an agent goes through this: a runner it
// cannot see is an agent that burns quota while holding no bay of the task cap.
check(AgentRunner.isAgentLine("501 ttys000 30 opencode --prompt Review PR #7 in o/r"))
check(AgentRunner.isAgentLine("501 ttys000 30 claude Review PR #7 in o/r"))
check(!AgentRunner.isAgentLine("501 ttys000 30 vim notes.txt"))
// All three interrupt hints, captured from the real CLIs. No string contains another,
// so matching one spelling reads every agent of the other runners as idle.
check(AgentActivity.looksBusy("  Build  GLM-5.2\n  ⬝⬝⬝  esc interrupt      ctrl+p commands"),
      "an OpenCode pane mid-turn must read as busy")
check(AgentActivity.looksBusy("⏵⏵ bypass permissions on · esc to interrupt · ←"),
      "a Claude Code pane mid-turn must read as busy")
check(AgentActivity.looksBusy(" ─ (°ロ°) contemplating… · 33s │ glm 5.2 xhigh │ 1 session\n ❯ Ctrl+C to interrupt…"),
      "a Hermes pane mid-turn must read as busy")
check(!AgentActivity.looksBusy("  Build  GLM-5.2\n     27.4K (3%)  ctrl+p commands"),
      "a finished OpenCode pane must give its bay back")
check(!AgentActivity.looksBusy(" ─ ready │ glm 5.2 xhigh │ 26k/1m │ ✓ 0s │ 1 session\n ❯"),
      "a finished Hermes pane must give its bay back")
// The port is what lets the applet ASK an OpenCode agent what it is doing instead of
// reading its status bar. Pinned literally for the same reason the rest of the command
// is: the two front-ends must spawn one server, not two different ones.
check(AgentRunner.opencode.agentCommand(promptFile: "/tmp/p.txt", port: 47_910)
        == "\(AgentRunner.permissionEnv)='\(AgentRunner.permissionValue)' "
         + "opencode --port 47910 --prompt \"$(cat '/tmp/p.txt')\"")
check(AgentRunner.claude.agentCommand(promptFile: "/tmp/p.txt", port: 47_910)
        == "claude \"$(cat '/tmp/p.txt')\"",
      "Claude Code serves no session, so a port must not reach its command")
check(!AgentRunner.hermes.agentCommand(promptFile: "/tmp/p.txt", port: 47_910).contains("47910"),
      "Hermes serves no port either; it answers from its own store")
check(!AgentRunner.opencode.agentCommand(promptFile: "/tmp/p.txt", port: 0).contains("--port"),
      "a run with no port must spawn exactly as it did before, not with --port 0")
print("agent runner assertions passed")

section("agent model")
// The model named in the attribution tag every posted comment opens with. Ids are one
// per provider and none of them is a display name, so these pin the rules that turn one
// into the other — a wrong answer here is a wrong model attributed on a public comment.
check(AgentModel.displayName("claude-opus-5") == "Opus 5",
      "Anthropic's id for the model everyone calls Opus 5")
check(AgentModel.displayName("claude-haiku-4-5-20251001") == "Haiku 4.5",
      "a release stamp dates a model, it does not name it; 4-5 is one version")
check(AgentModel.displayName("openrouter/moonshotai/kimi-k3") == "Kimi K3",
      "the provider path is routing, not a name")
check(AgentModel.displayName("qwen/qwen-3.8-max") == "Qwen 3.8 Max",
      "a vendor whose name IS the model family must survive the path strip")
check(AgentModel.displayName("openai/gpt-5.2") == "GPT 5.2", "initialisms are not title-cased")
check(AgentModel.displayName("moonshotai/kimi-k2:free") == "Kimi K2",
      "an OpenRouter variant suffix qualifies a model, it does not name another")
check(AgentModel.displayName("opus[1m]") == "Opus",
      "Claude Code's context-window suffix is not part of the name")
// Without a stated name the rules would title-case this into "X Preview F Free".
check(AgentModel.displayName("opencode/x-preview-f-free") == "Ox Alpha",
      "an id whose name is not in it is named outright")
check(AgentModel.displayName("X-Preview-F-Free") == "Ox Alpha",
      "stated names match case-insensitively, as every other list in models.json does")
// Everything that names no single model leaves the tag exactly as it has always read.
check(AgentModel.displayName("") == "")
check(AgentModel.displayName("<synthetic>") == "",
      "Claude Code's sentinel for a turn no model produced — rejected by shape")
check(AgentModel.displayName("default") == "" && AgentModel.displayName("opusplan") == "",
      "aliases that stand for a policy rather than a model")
// The two forms the tag takes. An undetected model must leave it byte-identical to
// what every golden file holds, which is what makes the model an addition, not a rewrite.
check(AgentModel.fillTag("\\[[Diplomat](u){model}\\]: ", model: "Opus 5")
        == "\\[[Diplomat](u), Opus 5\\]: ")
check(AgentModel.fillTag("\\[[Diplomat](u){model}\\]: ", model: "") == "\\[[Diplomat](u)\\]: ")

// The lookup end to end, over a fixture tree rather than the machine running this.
let modelFixture = FileManager.default.temporaryDirectory
    .appendingPathComponent("diplomat-agent-model-\(ProcessInfo.processInfo.processIdentifier)",
                            isDirectory: true)
try? FileManager.default.removeItem(at: modelFixture)
let claudeHome = modelFixture.appendingPathComponent("claude", isDirectory: true)
let sessions = claudeHome.appendingPathComponent("projects/-repo", isDirectory: true)
try FileManager.default.createDirectory(at: sessions, withIntermediateDirectories: true)
let configFile = modelFixture.appendingPathComponent("config.json")
let hermesConfig = modelFixture.appendingPathComponent("hermes.yaml")
let openCodeConfig = modelFixture.appendingPathComponent("opencode-config", isDirectory: true)
let openCodeState = modelFixture.appendingPathComponent("opencode-state", isDirectory: true)
try FileManager.default.createDirectory(at: openCodeConfig, withIntermediateDirectories: true)
try FileManager.default.createDirectory(at: openCodeState, withIntermediateDirectories: true)
func writeConfig(_ json: String) throws { try json.write(to: configFile, atomically: true, encoding: .utf8) }
func writeHermes(_ yaml: String) throws { try yaml.write(to: hermesConfig, atomically: true, encoding: .utf8) }
func writeOpenCode(_ name: String, _ json: String) throws {
    try json.write(to: openCodeConfig.appendingPathComponent(name), atomically: true, encoding: .utf8)
}
func writeOpenCodeState(_ json: String) throws {
    try json.write(to: openCodeState.appendingPathComponent("model.json"), atomically: true, encoding: .utf8)
}
func detectModel() -> String {
    AgentModel.detect(configFile: configFile, claudeHome: claudeHome, hermesConfig: hermesConfig,
                      openCodeConfig: openCodeConfig, openCodeState: openCodeState)
}
func writeTranscript(_ name: String, _ body: String, ageSecs: Double) throws {
    let url = sessions.appendingPathComponent(name)
    try body.write(to: url, atomically: true, encoding: .utf8)
    try FileManager.default.setAttributes(
        [.modificationDate: Date().addingTimeInterval(-ageSecs)], ofItemAtPath: url.path)
}

check(detectModel() == "",
      "a machine that has said nothing must add nothing to the tag")

// Claude Code is told no model by Diplomat, so what it last actually ran is the answer.
try writeTranscript("old.jsonl", "{\"message\":{\"model\":\"claude-sonnet-5\"}}\n", ageSecs: 600)
try writeTranscript("new.jsonl", "{\"message\":{\"model\":\"claude-opus-5\"}}\n", ageSecs: 10)
check(detectModel() == "Opus 5",
      "the newest transcript is the one that says what `claude` starts on now")
// A synthetic last turn must fall through to the real one before it, not blank the tag.
try writeTranscript("new.jsonl",
                    "{\"message\":{\"model\":\"claude-opus-5\"}}\n{\"message\":{\"model\":\"<synthetic>\"}}\n",
                    ageSecs: 10)
check(detectModel() == "Opus 5")
// A session touched before its first turn was written holds no model at all, and the
// newest file is regularly that one — stopping at it would drop the model whenever a
// run had just been started.
try writeTranscript("newest.jsonl", "{\"type\":\"user\",\"message\":{\"role\":\"user\"}}\n", ageSecs: 1)
check(detectModel() == "Opus 5",
      "the newest transcript with a model in it, not merely the newest transcript")
// A model pinned in Settings belongs to the OTHER runners: `AgentRunner.claude`'s
// command carries no model flag, so claiming it here would attribute a model that
// never ran.
try writeConfig("{\"agentModel\": \"openrouter/moonshotai/kimi-k3\"}")
check(detectModel() == "Opus 5",
      "a pin left over from OpenCode must not be attributed to a Claude Code run")
try writeConfig("{\"agentRunner\": \"opencode\", \"agentModel\": \"openrouter/moonshotai/kimi-k3\"}")
check(detectModel() == "Kimi K3",
      "for the runners Diplomat does pin, the pin is what the spawn passes them")
try writeConfig("{\"agentRunner\": \"hermes\"}")
check(detectModel() == "",
      "an unpinned runner whose own choice cannot be read still names nothing")
// No pin means Hermes starts on the default its own picker wrote down, and a spawn
// carries no `-m` to override it — so that file is what the run is on.
try writeHermes("""
model:
  default: moonshotai/kimi-k3
  provider: openrouter
providers:
  ollama-launch:
    default_model: silver:e4b

""")
check(detectModel() == "Kimi K3",
      "an unpinned Hermes run is named by the model Hermes' own config starts it on")
try writeConfig("{\"agentRunner\": \"hermes\", \"agentModel\": \"qwen/qwen-3.8-max\"}")
check(detectModel() == "Qwen 3.8 Max",
      "a pin is passed as `-m` and beats the picker's default, so it is what the tag says")
// An unpinned OpenCode is named out of OpenCode's own settings, in the order OpenCode
// itself resolves them. Hermes' config is still on disk throughout: it is Hermes' alone,
// and naming an OpenCode run out of it would attribute a model that never ran.
try writeConfig("{\"agentRunner\": \"opencode\"}")
check(detectModel() == "", "a machine where OpenCode has written nothing down names nothing")
try writeOpenCodeState("""
{"recent": [{"providerID": "anthropic", "modelID": "claude-opus-5"},
            {"providerID": "ollama-cloud", "modelID": "glm-5.2"}],
 "favorite": [], "variant": {}}
""")
check(detectModel() == "Opus 5",
      "the head of the recent list is the model OpenCode's picker restores")
// A head written without its model must fall through to the entry behind it rather than
// blanking a tag that entry can still fill.
try writeOpenCodeState("""
{"recent": [{"providerID": "anthropic"}, {"providerID": "ollama-cloud", "modelID": "glm-5.2"}]}
""")
check(detectModel() == "GLM 5.2", "an entry that names no model is walked past, not read as one")
// A `model` in the config beats the picker's history — OpenCode reads it first, so a run
// started with no `-m` is on that one whatever was used last.
try writeOpenCode("config.json", "{\"model\": \"openai/gpt-5.2\"}")
check(detectModel() == "GPT 5.2", "the config's model is what OpenCode resolves before its recent list")
// …and the three global files merge in OpenCode's own order, later winning.
try writeOpenCode("opencode.json", "{\"model\": \"qwen/qwen-3.8-max\"}")
check(detectModel() == "Qwen 3.8 Max", "opencode.json is merged over config.json")
try writeOpenCode("opencode.jsonc", """
{
  // The picker's own note, which the parser must drop rather than choke on.
  "model": "openrouter/moonshotai/kimi-k3",
  "small_model": "openai/gpt-5.4-mini",
}
""")
check(detectModel() == "Kimi K3",
      "opencode.jsonc is merged last, comments and a trailing comma and all")
// A pin in Settings is passed as `-m`, which OpenCode reads before any of its own
// settings — so it is what the run is on, and what the tag says.
try writeConfig("{\"agentRunner\": \"opencode\", \"agentModel\": \"google/gemini-3.1-pro-preview\"}")
check(detectModel() == "Gemini 3.1 Pro Preview", "`-m` beats everything OpenCode would have picked")
try FileManager.default.removeItem(at: openCodeConfig)
try FileManager.default.createDirectory(at: openCodeConfig, withIntermediateDirectories: true)
try writeOpenCodeState("{}")
// Every OpenCode config file is JSONC whatever its extension, and `model` is read off the
// top level alone — an agent's own model is that agent's, not the one a fresh spawn starts on.
check(AgentModel.configuredModel(inOpenCodeConfig: "{\"agent\": {\"build\": {\"model\": \"a/b\"}}}") == nil,
      "a `model` nested under another key is not the model the config names")
check(AgentModel.configuredModel(inOpenCodeConfig: "{\"model\": \"a/b\" /* pinned */, \"username\": \"me\"}")
        == "a/b",
      "a block comment is punctuation the parser drops")
check(AgentModel.configuredModel(inOpenCodeConfig: "{\"model\": \"a/b\", \"username\": \"http://x//y\"}")
        == "a/b",
      "a `//` inside a string is part of the string, not the start of a comment")
check(AgentModel.configuredModel(inOpenCodeConfig: "{\"model\": \"{env:OPENCODE_MODEL}\"}")
        .map(AgentModel.displayName) == "",
      "an unresolved config reference must cost the tag its model, not be named as one")
// `providers:` entries carry a `default_model:` each, and a nested mapping can carry a
// `default:` of its own; neither is the model Hermes starts on.
check(AgentModel.defaultModel(inHermesConfig: "model:\n  fallbacks:\n    default: nested\n  default: real\n")
        == "real",
      "only a direct child of `model:` is the default a session starts on")
check(AgentModel.defaultModel(inHermesConfig: "models:\n  default: other\n") == nil,
      "a key that merely starts with `model` is a different key")
check(AgentModel.defaultModel(inHermesConfig: "model:\n  default: 'a/b' # picked\n") == "a/b",
      "a quoted scalar and a trailing comment are punctuation, not part of the id")
check(AgentModel.defaultModel(inHermesConfig: "model:\n  default:\n  provider: openrouter\n") == nil,
      "a key written with no value names no model")
check(AgentModel.defaultModel(inHermesConfig: "model:\r\n  default: a/b\r\n") == "a/b",
      "a CR left by a CRLF file is line ending, not part of the id `displayName` is given")
// With no transcripts at all, what Claude Code's settings ask for.
try FileManager.default.removeItem(at: sessions)
try writeConfig("{}")
try "{\"model\": \"opus[1m]\"}".write(to: claudeHome.appendingPathComponent("settings.json"),
                                     atomically: true, encoding: .utf8)
check(detectModel() == "Opus",
      "a machine that has not run `claude` yet still knows what it is set to")
// Claude Code writes compact JSON; a reader that only accepts that spelling is one
// pretty-printer away from silently finding nothing and dropping the model.
check(AgentModel.lastModelField(in: "{\"model\" : \"claude-opus-5\"}") == "claude-opus-5")

// The tag as it actually reaches an agent, on a machine that HAS a model to name —
// the goldens are regenerated from the same assets they assert, so the prefix is
// pinned here or nowhere. The fence is lifted onto the fixture for these builds and
// put straight back, so everything after it keeps the model-free goldens.
let plainTag = "`\\[[Diplomat](https://github.com/latekvo/Diplomat)\\]: `"
let opusTag = "`\\[[Diplomat](https://github.com/latekvo/Diplomat), Opus 5\\]: `"
check(ReviewConfig(depth: "max", me: "testuser").buildPrompt().contains(plainTag),
      "with nothing to read, the tag must stay the prefix every install already posts")
try FileManager.default.createDirectory(at: sessions, withIntermediateDirectories: true)
try writeTranscript("session.jsonl", "{\"message\":{\"model\":\"claude-opus-5\"}}\n", ageSecs: 1)
setenv("DIPLOMAT_CLAUDE_DIR", claudeHome.path, 1)
setenv("DIPLOMAT_CONFIG", configFile.path, 1)
let taggedReview = ReviewConfig(depth: "max", me: "testuser").buildPrompt()
check(taggedReview.contains(opusTag), "the review tag must name the model the run is on")
check(taggedReview.contains("\"[Diplomat, Opus 5]: <your text>\""),
      "and the example it renders as must agree with the prefix it just gave")
// The audit posts under the same attribution from a second copy of the block in a
// second asset, so the model has to reach both.
check(AuditConfig(openPRs: true).buildPrompt().contains(opusTag),
      "the audit's comments carry the same tag as a review's")
check(!AuditConfig().buildPrompt().contains("[Diplomat"),
      "a read-only audit posts nothing, so it is handed no attribution rule at all")
setenv("DIPLOMAT_CONFIG", fence.appendingPathComponent("config.json").path, 1)
setenv("DIPLOMAT_CLAUDE_DIR", fence.appendingPathComponent("claude").path, 1)
try? FileManager.default.removeItem(at: modelFixture)
print("agent model assertions passed")

section("opencode sessions")
// OpenCode keeps ONE session store for the whole machine, so a run's own server lists
// every session on the box. These are the filters that get from that list to the one
// session a run owns — and the same cases `tests/test_opencode_api.py` pins.
let ours = "Review PR #7 in o/r"
let listing: [[String: Any]] = [
    ["id": "ses_a", "directory": "/repo", "time": ["created": 2_000.0]],
    ["id": "ses_b", "directory": "/repo", "time": ["created": 3_000.0]],
    ["id": "ses_old", "directory": "/repo", "time": ["created": 500.0]],
    ["id": "ses_elsewhere", "directory": "/other", "time": ["created": 3_000.0]],
]
check(OpenCodeAPI.candidates(listing, directory: "/repo", sinceMs: 1_000, taken: [])
        == ["ses_a", "ses_b"],
      "another checkout's sessions, and ones older than the run, are not candidates")
check(OpenCodeAPI.candidates(listing, directory: "/repo", sinceMs: 1_000, taken: ["ses_a"])
        == ["ses_b"],
      "a session another run already owns must never be taken twice")
// The prompt is submitted verbatim, so the match is equality — which is what tells two
// runs apart when both are working in the same checkout, the ordinary case under the
// task cap.
let opening: [[String: Any]] = [["info": ["role": "user"], "parts": [["type": "text", "text": ours]]]]
check(OpenCodeAPI.isOurs(opening, prompt: ours))
check(!OpenCodeAPI.isOurs(opening, prompt: "Review PR #8 in o/r"))
check(!OpenCodeAPI.isOurs([], prompt: ours), "a session with no messages is nobody's")
// Busy/idle, and the price. It takes the server's status AND a stamped last message to
// call a turn over; each of the two covers the other's blind spot, and the same cases
// `tests/test_opencode_api.py` pins.
let idle: [String: Any] = [:]
let busy: [String: Any] = ["ses_a": ["type": "busy"]]
check(!OpenCodeAPI.isRunning(idle, sessionID: "ses_a"),
      "a session the server is running no turn in is absent from the map")
check(OpenCodeAPI.isRunning(busy, sessionID: "ses_a"))
check(!OpenCodeAPI.isRunning(busy, sessionID: "ses_b"),
      "another session's turn says nothing about this one")
check(OpenCodeAPI.isRunning(["ses_a": ["type": "retry"]], sessionID: "ses_a"),
      "an agent waiting out a provider's backoff is not back at its prompt")
check(!OpenCodeAPI.isRunning(["ses_a": ["type": "idle"]], sessionID: "ses_a"),
      "a status that names itself idle is idle, present or not")
check(OpenCodeAPI.stateOf([], running: false) == nil,
      "a session not yet written to is not idle; it has not started")
let working: [[String: Any]] = [["info": ["role": "assistant", "time": ["created": 1.0]]]]
check(OpenCodeAPI.stateOf(working, running: false)?.busy == true)
let finished: [[String: Any]] = [["info": [
    "role": "assistant", "time": ["created": 1.0, "completed": 2.0]]]]
check(OpenCodeAPI.stateOf(finished, running: false)?.busy == false)
check(OpenCodeAPI.stateOf(finished, running: true)?.busy == true,
      "a stamped step is not a finished turn while the server is still running one")
check(OpenCodeAPI.stateOf(finished, running: nil) == nil,
      "a status the server would not report is not a turn that ended")
// A stamp that is not a time is not a stamp. The one direction that must not be
// reachable by accident is a gap reading as a finished turn.
let halfStamped: [[String: Any]] = [["info": [
    "role": "assistant", "time": ["created": 1.0, "completed": "soon"]]]]
check(OpenCodeAPI.stateOf(halfStamped, running: false)?.busy == true)
// What a run SPENT is a sum over every message, because OpenCode prices a turn per
// message — and it counts input, output and cache WRITES only. This session reports
// 60505 tokens of `total`, nearly all of it cache reads; counting those would make the
// per-task figure on the telemetry screen mean one thing for one runner and another
// for the other. Same numbers as `tests/test_telemetry.py`.
let exported: [[String: Any]] = [
    ["info": ["role": "user"]],
    ["info": ["role": "assistant", "tokens": ["total": 30_000.0, "input": 3.0,
                                              "output": 84.0, "reasoning": 9.0,
                                              "cache": ["read": 29_000.0, "write": 40.0]]]],
    ["info": ["role": "assistant", "tokens": ["total": 30_505.0, "input": 7.0,
                                              "output": 8.0, "reasoning": 0.0,
                                              "cache": ["read": 30_384.0, "write": 106.0]]]],
]
check(OpenCodeAPI.sessionTokens(exported) == 248)
check(OpenCodeAPI.sessionTokens([]) == 0)
print("opencode session assertions passed")

section("hermes sessions")
// The same two questions, answered from Hermes' own SQLite store instead of a port —
// and the same cases `tests/test_hermes_store.py` pins. A turn is over only when the
// agent itself says so.
check(HermesStore.stateOf(role: nil, finishReason: nil, delegating: false) == nil,
      "a session not yet written to is not idle; it has not started")
check(HermesStore.stateOf(role: "assistant", finishReason: "stop",
                          delegating: false)?.busy == false)
check(HermesStore.stateOf(role: "assistant", finishReason: "tool_calls",
                          delegating: false)?.busy == true,
      "asking for a tool is the middle of a turn, not the end of one")
check(HermesStore.stateOf(role: "tool", finishReason: nil, delegating: false)?.busy == true,
      "a tool result nobody has answered yet is a turn still in flight")
check(HermesStore.stateOf(role: "user", finishReason: "stop", delegating: false)?.busy == true,
      "a query not picked up yet must not read as a finished turn")
// A turn the agent ended with a background subagent still to report is not a run that
// ended: `delegate_task(background=true)` hands the turn back and answers later as a
// fresh user turn.
check(HermesStore.stateOf(role: "assistant", finishReason: "stop",
                          delegating: true)?.busy == true,
      "a fan-out that has not reported yet holds its run open")
check(HermesStore.stateOf(role: "assistant", finishReason: "stop", delegating: nil) == nil,
      "a store that could not say what is outstanding must not end a run")
check(HermesStore.stateOf(role: "assistant", finishReason: "tool_calls",
                          delegating: nil)?.busy == true,
      "a turn in flight is answered without asking about delegations at all")
// `-q` stores the query verbatim, so the match is equality — the same exactness
// `--prompt` buys under OpenCode, and the only thing separating two agents in one
// checkout.
check(HermesStore.isOurs(role: "user", content: ours, prompt: ours))
check(!HermesStore.isOurs(role: "user", content: ours, prompt: "Review PR #8 in o/r"))
check(!HermesStore.isOurs(role: "assistant", content: ours, prompt: ours),
      "an assistant message is never the message a run was submitted as")
// Input + output + cache WRITES, never the cache reads beside them — the same three
// `OpenCodeAPI.sessionTokens` and the Claude Code transcript scan sum, so one ledger
// holds every runner in one unit.
check(HermesStore.sessionTokens(input: 100, output: 20, cacheWrite: 5) == 125)
check(HermesStore.sessionTokens(input: nil, output: nil, cacheWrite: nil) == 0)
// The same session in the other unit — what the provider charged, which is what the
// budget gate holds an account billed in money to.
check(HermesStore.sessionPrice(actual: nil, estimated: 0.0675) == 0.0675,
      "the estimate answers until the provider settles it")
check(HermesStore.sessionPrice(actual: 0.071, estimated: 0.0675) == 0.071,
      "…and the settled charge is preferred once it exists")
check(HermesStore.sessionPrice(actual: nil, estimated: nil) == nil,
      "a session not yet priced is unpriced, not free — a zero would enter the "
      + "distribution the next task is gated on")
check(HermesStore.sessionPrice(actual: 0, estimated: 0.0675) == 0.0675,
      "a zero in the settled column is the column being empty, not a free task")
print("hermes session assertions passed")

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
check(aPRs.buildPrompt().contains("20 lines"))   // LOW findings earn a PR only when fix < 20 LOC
check(aPRs.buildPrompt().contains("No AI attribution"))
check(!aPRs.buildPrompt().contains("READ-ONLY audit"))
// The four-point gate every generated PR clears before it opens, named point by point for
// the same reason as the review moves below — the goldens re-bless whatever it says, and
// this is the gate deciding whether those PRs carry a discriminating test at all.
let gate = aPRs.buildPrompt()
check(gate.contains("SELF-REVIEW GATE"), "self-review gate present")
check(gate.contains("REGRESSION TEST THAT DISTINGUISHES FIXED FROM BROKEN"),
      "gate (1): a test the unfixed code would fail")
check(gate.contains("FIX THE CLASS, NOT THE INSTANCE"),
      "gate (2): every sibling call site of the same shape")
check(gate.contains("DOC / COMMENT / RATIONALE RIPPLE"),
      "gate (3): prose brought into agreement with the new behaviour")
check(gate.contains("NO DEAD CODE THE FIX INTRODUCED"),
      "gate (4): unreachable branches the fix created")
// The gate rides on openPRs, so the read-only default must not carry it.
check(!aBase.buildPrompt().contains("SELF-REVIEW GATE"),
      "no PRs to open ⇒ no self-review gate")
// Both on: issue-handling + PRs together.
let aBoth = AuditConfig(fixIssues: true, openPRs: true)
check(aBoth.buildPrompt().contains("OPEN ISSUES") && aBoth.buildPrompt().contains("focused pull request"))
print("audit prompt assertions passed")

// ---- Fix-issues prompts ----
section("issue prompts")
// The goldens are regenerated from issues.json, so they re-bless whatever it says.
// These are what actually pin the meaning: the six scopes, both enumeration paths,
// and every block a toggle turns on — each with the negative that proves the block
// is gated rather than always present.
let goldenMeSeed = "testuser"
let iAll = IssueConfig(me: goldenMeSeed)
print("issues valid=\(iAll.isValid) depth=\(IssueCatalog.depth(id: iAll.depth).title)")
// Every scope but the two that name a person is spawnable with no further input.
check(iAll.isValid && IssueConfig(target: .contributors).isValid
      && IssueConfig(target: .members).isValid, "association scopes need no handle")
check(!IssueConfig(target: .someone).isValid, "someone else's issues needs the handle")
// "Mine" before the viewer login resolves addresses the agent as GitHub's own @me
// shorthand rather than refusing to spawn — the same fallback ReviewConfig makes.
check(IssueConfig(target: .mine).isValid && IssueConfig(target: .mine).authorHandle == "me",
      "my issues falls back to @me, like the review sweep")
check(!IssueConfig(target: .specific).isValid, "a specific issue needs a number")
check(IssueConfig(target: .specific, specificIssue: "421").isValid)
// An issue URL parses; the same number as a PR URL does not (PRRef.Kind).
check(IssueConfig(target: .specific,
                  specificIssue: "https://github.com/\(cfg.owner)/\(cfg.repo)/issues/421")
        .issueRef.number == 421, "an issue URL is a usable issue reference")
check(IssueConfig(target: .specific,
                  specificIssue: "https://github.com/\(cfg.owner)/\(cfg.repo)/pull/421")
        .issueRef.number == nil, "a PR URL is not an issue reference")

// The scope sentence, one per target — a target falling through to another would
// still print a prompt, just the wrong set of issues.
let iAllP = iAll.buildPrompt()
check(iAllP.contains("go through every currently-open issue,"), "all-issues scope")
check(IssueConfig(target: .mine, me: goldenMeSeed).buildPrompt()
        .contains("issue I opened (@\(goldenMeSeed))"), "my-issues scope")
check(IssueConfig(target: .someone, username: "someuser").buildPrompt()
        .contains("opened by @someuser"), "one user's issues scope")
let iContrib = IssueConfig(target: .contributors).buildPrompt()
let iMembers = IssueConfig(target: .members).buildPrompt()
check(iContrib.contains("OUTSIDE the organisation"), "contributors scope")
check(iMembers.contains("INSIDE the organisation"), "org-members scope")
// Both association scopes name the associations from filters.json, not a hardcoded pair.
check(iContrib.contains("MEMBER or OWNER") && iMembers.contains("MEMBER or OWNER"),
      "the org/outside split is filters.json's orgAssociations")

// Enumeration: `gh issue list --json` has no authorAssociation field, so the two
// association scopes must be sent to the REST endpoint — and warned about the pull
// requests it returns alongside issues. Every other scope takes `gh issue list`.
check(iAllP.contains("gh issue list"), "a plain scope enumerates with gh issue list")
check(!iAllP.contains("author_association"), "a plain scope needs no association read")
check(iContrib.contains("author_association") && iContrib.contains("pull_request"),
      "the association path reads REST and skips the PRs it returns")
check(!iContrib.contains("gh issue list --repo"),
      "the association path never enumerates with gh issue list")

// The unassigned filter: on by default for a sweep, and it reaches BOTH the search
// qualifier and the hard rule. Off, neither survives.
check(iAllP.contains("no:assignee") && iAllP.contains("NARROW THAT SET TO THE UNASSIGNED"),
      "unassigned-only narrows the search and states the rule")
let iAssignedToo = IssueConfig(me: goldenMeSeed, unassignedOnly: false).buildPrompt()
check(!iAssignedToo.contains("no:assignee") && !iAssignedToo.contains("UNASSIGNED"),
      "unassigned-only off leaves no trace of the filter")
// A named single issue is never filtered back out by it.
let iSingle = IssueConfig(target: .specific, specificIssue: "421").buildPrompt()
check(iSingle.hasPrefix("Take issue #421 in"), "single-issue scope")
check(!iSingle.contains("UNASSIGNED"), "a hand-named issue is not filtered by assignee")
// …and the wizards do not offer the tick there either — the prompt above is the same
// whichever way it is left, so a tick that changed nothing would be the only lie.
check(IssueConfig().canFilterUnassigned && !IssueConfig(target: .specific).canFilterUnassigned,
      "the unassigned tick is offered for a sweep, not for one named issue")

// The claim: assigning the issue to me is what stops a second agent taking it, so it
// has to name the command and happen BEFORE the work.
check(iAllP.contains("--add-assignee @me") && iAllP.contains("--remove-assignee @me"),
      "the claim is taken and handed back")
check(!IssueConfig(me: goldenMeSeed, assignToMe: false).buildPrompt().contains("add-assignee"),
      "no claim when the toggle is off")

// Bugs only unless the escalation is ticked — and the escalation replaces it rather
// than stacking, so the run is never told both to skip and to take feature requests.
check(iAllP.contains("SKIP every feature request"), "bugs only by default")
let iFeatures = IssueConfig(me: goldenMeSeed, includeFeatures: true).buildPrompt()
check(iFeatures.contains("FEATURE REQUESTS ARE IN SCOPE TOO"), "the escalation opens them up")
check(!iFeatures.contains("SKIP every feature request"), "and withdraws the skip rule")

// The bar every depth is held to, and the depth ladder itself. `quick` is the one
// level that runs nothing, so it must NOT claim a reproduction.
check(iAllP.contains("DONE MEANS OBSERVABLE PROOF"), "the bar is always present")
check(iAllP.contains("CANNOT REPRODUCE"), "an unreproducible issue is a reported result")
check(iAllP.contains("CONCRETE REPRODUCTION"), "the default depth reproduces")
check(IssueConfig(depth: "quick", me: goldenMeSeed).buildPrompt()
        .contains("QUICK PASS"), "the quick level is a read-only pass")
check(!IssueConfig(depth: "quick", me: goldenMeSeed).buildPrompt()
        .contains("CONCRETE REPRODUCTION"), "the quick level promises no reproduction")
check(IssueConfig(depth: "max", me: goldenMeSeed).buildPrompt()
        .contains("DRIVE THE BUG THROUGH THE REAL ENTRY POINT"), "the max level runs the app")

// Delivery: a draft PR that closes the issue, or nothing on the remote at all.
check(iAllP.contains("DRAFT") && iAllP.contains("Fixes #<n>"), "a draft PR closes the issue")
check(iAllP.contains("REGRESSION TEST WITH EVERY FIX"), "each fix ships a test that pins it")
check(iAllP.contains("DUPLICATE") && iAllP.contains("gh pr diff"), "no duplicate PRs")
let iHandsOff = IssueConfig(me: goldenMeSeed, openPRs: false, commentOnIssue: false).buildPrompt()
check(iHandsOff.contains("opens NO pull requests"), "PRs off ⇒ nothing reaches the remote")
check(!iHandsOff.contains("gh pr create"), "and no PR is opened")
check(!iHandsOff.contains("No AI attribution"),
      "commit-authoring guidance only where we might commit")
// The attribution tag rides on posting something — a run that posts nothing wears none.
check(iAllP.contains("Made by Diplomat"), "a posting run carries the attribution tag")
check(!iHandsOff.contains("Made by Diplomat"), "a silent run carries none")
check(IssueConfig(me: goldenMeSeed, openPRs: false).buildPrompt().contains("Made by Diplomat"),
      "the issue comment alone still earns the tag")
check(iAllP.contains("gh issue comment"), "the outcome is reported on the issue")
check(!IssueConfig(me: goldenMeSeed, commentOnIssue: false).buildPrompt()
        .contains("gh issue comment"), "no comment when the toggle is off")
// Every scope ends with the report that accounts for each issue it was handed.
check(iAllP.contains("exactly one bucket"), "the summary accounts for every issue")
print("issue prompt assertions passed")

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
                                   meshStandsDown: true, atCapacity: true) == .banned,
          "ban outranks everything for \(src.rawValue)")
    check(AgentDispatchGate.decide(source: src, banned: false, agentOnPR: true,
                                   meshStandsDown: true, atCapacity: true) == .inFlight,
          "a live agent on the PR blocks \(src.rawValue) — never double-spawn")
    check(AgentDispatchGate.decide(source: src, banned: false, agentOnPR: false,
                                   meshStandsDown: false, atCapacity: false) == .proceed,
          "clear board proceeds for \(src.rawValue)")
}
// The documented trigger asymmetries — and ONLY these:
check(AgentDispatchGate.decide(source: .auto, banned: false, agentOnPR: false,
                               meshStandsDown: true, atCapacity: false) == .standDown,
      "mesh gates auto origination")
check(AgentDispatchGate.decide(source: .panel, banned: false, agentOnPR: false,
                               meshStandsDown: true, atCapacity: false) == .proceed,
      "a human's click already decided placement — panel is never mesh-gated")
check(AgentDispatchGate.decide(source: .auto, banned: false, agentOnPR: false,
                               meshStandsDown: false, atCapacity: true) == .atCapacity,
      "the device's automatic-task cap gates auto dispatch")
check(AgentDispatchGate.decide(source: .panel, banned: false, agentOnPR: false,
                               meshStandsDown: false, atCapacity: true) == .proceed,
      "a click is one deliberate agent, not a queue being emptied — never capped")
check(AgentDispatchGate.decide(source: .auto, banned: false, agentOnPR: false,
                               meshStandsDown: true, atCapacity: true) == .atCapacity,
      "capacity outranks mesh — a saturated device takes no claim it would refuse")
check(AgentDispatchGate.decide(source: .auto, banned: false, agentOnPR: false,
                               meshStandsDown: false, atCapacity: false,
                               unaffordable: true) == .unaffordable,
      "the device's rate-limit budget gates auto dispatch")
check(AgentDispatchGate.decide(source: .panel, banned: false, agentOnPR: false,
                               meshStandsDown: false, atCapacity: false,
                               unaffordable: true) == .proceed,
      "spending your own last of the limit is the operator's call — panel is never gated")
check(AgentDispatchGate.decide(source: .auto, banned: false, agentOnPR: false,
                               meshStandsDown: false, atCapacity: true,
                               unaffordable: true) == .atCapacity,
      "capacity outranks the budget — the probe is not worth taking with no free bay")
check(AgentDispatchGate.decide(source: .auto, banned: false, agentOnPR: false,
                               meshStandsDown: true, atCapacity: false,
                               unaffordable: true) == .unaffordable,
      "the budget outranks mesh, for the reason capacity does")
check(AgentDispatchGate.stealsFocus(.panel) && !AgentDispatchGate.stealsFocus(.auto),
      "panel comes forward, auto never steals focus")
check(AgentDispatchGate.label(source: .auto, core: "Review · #7", attemptNumber: 2)
      == "Auto · Review · #7 · retry 2", "auto label prefix + retry suffix")
check(AgentDispatchGate.label(source: .panel, core: "Review · #7") == "Review · #7",
      "panel label is the bare core")
check(AgentDispatchGate.label(source: .auto, core: "Review · #7", requested: true)
      == "Review · #7"
      && AgentDispatchGate.label(source: .auto, core: "Review · #7", attemptNumber: 2,
                                 requested: true) == "Review · #7 · retry 2",
      "a review the operator asked for keeps the retry suffix and loses the prefix — "
      + "it waits for the cap like auto work, but no monitor found it")
check(AgentDispatchGate.bumpsCounter(source: .auto, attemptNumber: 1)
      && !AgentDispatchGate.bumpsCounter(source: .auto, attemptNumber: 2)
      && !AgentDispatchGate.bumpsCounter(source: .panel, attemptNumber: 1),
      "only a monitor's first dispatch counts as auto-handled")

section("the device's automatic-task cap")
// PARITY: diplomat-platform/linux/tests/test_autofix.py asserts these exact numbers.
check(AgentDispatchGate.defaultAutoTaskLimit == 2, "two automatic agents by default")
check(AgentDispatchGate.clampAutoTaskLimit(0) == 1
      && AgentDispatchGate.clampAutoTaskLimit(-4) == 1,
      "a stored 0 would stop all auto work while the toggles still read on — floor is 1")
check(AgentDispatchGate.clampAutoTaskLimit(3) == 3
      && AgentDispatchGate.clampAutoTaskLimit(999) == 16,
      "the cap is held inside the range the stepper offers")
check(AgentDispatchGate.runningAutoTasks(livePRs: [], autoPRs: [], manualPRs: []) == 0,
      "an idle machine runs nothing")
check(AgentDispatchGate.runningAutoTasks(livePRs: [1, 2], autoPRs: [], manualPRs: []) == 2,
      "a live agent nobody tracked counts as automatic (an applet restart loses the book)")
check(AgentDispatchGate.runningAutoTasks(livePRs: [1, 2], autoPRs: [], manualPRs: [1]) == 1,
      "…unless it is a known manual one — a click never spends the automatic budget")
check(AgentDispatchGate.runningAutoTasks(livePRs: [], autoPRs: [3], manualPRs: []) == 1,
      "a just-spawned agent counts before ps has caught up with it")
check(AgentDispatchGate.runningAutoTasks(livePRs: [3], autoPRs: [3], manualPRs: []) == 1,
      "…and is not counted twice once it has")
check(AgentDispatchGate.runningAutoTasks(livePRs: [1, 2, 3], autoPRs: [4],
                                         manualPRs: [2]) == 3,
      "1, 3 and 4")
// An agent is spawned into an INTERACTIVE session, so finishing its work is not
// exiting: it waits at its prompt, `ps` keeps showing it, and the bay it took is never
// given back. Seen 2026-08-05 on a Linux box — two agents idle since the previous
// evening, both bays of a cap of 2 held, automatic work still being deferred 12h later.
// PARITY: the same six cases run against the Python twin in
// diplomat-platform/linux/tests/test_autofix.py.
check(AgentDispatchGate.runningAutoTasks(livePRs: [1, 2], autoPRs: [], manualPRs: [],
                                         idlePRs: [1]) == 1,
      "an untracked agent back at its prompt gives its bay back")
check(AgentDispatchGate.runningAutoTasks(livePRs: [1, 2], autoPRs: [], manualPRs: [],
                                         idlePRs: [1, 2]) == 0,
      "…and a machine of them is a machine with an empty cap")
check(AgentDispatchGate.runningAutoTasks(livePRs: [1], autoPRs: [1], manualPRs: [],
                                         idlePRs: [1]) == 0,
      "a TRACKED agent that went quiet leaves too — idle is subtracted from the union, "
      + "or `autoPRs` would re-add the very agent being let go")
check(AgentDispatchGate.runningAutoTasks(livePRs: [1, 2], autoPRs: [], manualPRs: [],
                                         idlePRs: []) == 2,
      "nothing idle changes nothing")
check(AgentDispatchGate.runningAutoTasks(livePRs: [1, 2], autoPRs: [], manualPRs: [],
                                         idlePRs: [9]) == 2,
      "an idle PR nobody is running here is not this machine's business")
check(AgentDispatchGate.runningAutoTasks(livePRs: [1, 2], autoPRs: [], manualPRs: []) == 2,
      "no evidence at all (the default) frees nothing — a terminal that cannot be read "
      + "must cost a deferral, never a burst")

section("the device's spending budget")
// PARITY: diplomat-platform/linux/tests/test_autofix.py asserts these exact numbers
// and bounds — the two front-ends decide whether to spend the same account's limit,
// so a disagreement here is one machine gating what the other starts.
check(AgentDispatchGate.defaultBudgetConfidence == 95
      && AgentDispatchGate.defaultBudgetFloorPct == 20
      && AgentDispatchGate.defaultBudgetReserveUsd == 1.0,
      "95% sure, and 20% of a window — or a dollar of an account — kept in hand "
      + "until the ledger can price a task")
check(AgentDispatchGate.budgetZ(95) == 1.6449,
      "95% one-sided — NOT the screen's two-sided 1.96 on the mean")
check(AgentDispatchGate.clampBudgetConfidence(93) == 95
      && AgentDispatchGate.clampBudgetConfidence(96) == 99
      && AgentDispatchGate.clampBudgetConfidence(1) == 50,
      "an unsupported level rounds UP — a table that cannot honour a value holds "
      + "work back rather than waving it through on a looser bound")
check(AgentDispatchGate.clampBudgetConfidence(100) == 99
      && AgentDispatchGate.clampBudgetConfidence(999) == 99,
      "…and above the table it is the strictest level, not a fallback to the default")
check(AgentDispatchGate.clampBudgetFloorPct(-1) == 0
      && AgentDispatchGate.clampBudgetFloorPct(140) == 100
      && AgentDispatchGate.clampBudgetFloorPct(.nan) == 20,
      "the floor is a real share of a window")
check(AgentDispatchGate.clampBudgetReserveUsd(-1) == 0
      && AgentDispatchGate.clampBudgetReserveUsd(250) == 100
      && AgentDispatchGate.clampBudgetReserveUsd(.nan) == 1.0,
      "the reserve is held to what its knob can express")

// A prediction bound, not the screen's interval on the mean: it carries the spread
// of the tasks and does NOT converge on the mean as n grows.
check(AgentDispatchGate.taskCostBound(mean: 2, sd: 1, count: 1,
                                      z: 1.6449, minSample: 1) == nil,
      "one observation has no spread — the ledger cannot price a task from it")
check(AgentDispatchGate.taskCostBound(mean: 2, sd: 1, count: 4,
                                      z: 1.6449, minSample: 5) == nil,
      "…nor can it below the caller's own minimum")
if let bound = AgentDispatchGate.taskCostBound(mean: 2, sd: 1, count: 4,
                                               z: 1.6449, minSample: 2) {
    check(abs(bound - (2 + 1.6449 * (1.25 as Double).squareRoot())) < 1e-12,
          "mean + z·sd·√(1 + 1/n)")
    check(bound > 2 + 1.6449, "the √(1 + 1/n) inflation is above the plain z·sd")
} else {
    check(false, "a priced ledger must yield a bound")
}

// Both windows gate, the tighter one answers, and no reading at all is no opinion.
// The ceilings are listed in the order `AutoBudget.decide` lists them, which is what
// fixes the tie-break.
func windows(_ sessionLeft: Double?, _ weekLeft: Double?,
             _ sessionCost: Double?, _ weekCost: Double?) -> [(String, Double?, Double?)] {
    [(AgentDispatchGate.windowSession, sessionLeft, sessionCost),
     (AgentDispatchGate.windowWeek, weekLeft, weekCost)]
}
check(AgentDispatchGate.budgetDecide(windows(50, 50, 10, 2), floor: 20).affordable,
      "room in both windows proceeds")
let broke = AgentDispatchGate.budgetDecide(windows(5, 50, 10, 2), floor: 20)
check(!broke.affordable && broke.window == AgentDispatchGate.windowSession
      && broke.measured && broke.left == 5 && broke.needed == 10,
      "the 5-hour window refuses, and says what it had against what was needed")
let weekBroke = AgentDispatchGate.budgetDecide(windows(90, 1, 10, 2), floor: 20)
check(!weekBroke.affordable && weekBroke.window == AgentDispatchGate.windowWeek,
      "a full 5-hour window does not buy a spent weekly one")
let floored = AgentDispatchGate.budgetDecide(windows(15, nil, nil, nil), floor: 20)
check(!floored.affordable && !floored.measured && floored.needed == 20,
      "an unpriced ledger holds the floor instead")
check(AgentDispatchGate.budgetDecide(windows(25, nil, nil, nil), floor: 20).affordable,
      "…and above the floor it proceeds")
let noReading = AgentDispatchGate.budgetDecide(windows(nil, nil, nil, nil), floor: 20)
check(noReading.affordable && noReading.window.isEmpty,
      "no reading is no opinion — a probe that is off or offline must not take the "
      + "machine's automatic work down with it")
check(AgentDispatchGate.budgetDecide([], floor: 20).affordable,
      "…and no ceilings at all is the same silence, not an error")
let tie = AgentDispatchGate.budgetDecide(windows(30, 30, 10, 10), floor: 20)
check(tie.window == AgentDispatchGate.windowSession, "session wins an exact tie")
check(tie.unit == AgentDispatchGate.unitPct, "a rate limit decides in percentages")

// The same arithmetic in the other currency. Nothing in the gate may assume a
// percentage: a $255 balance is not 255% of anything.
let dollars = [(AgentDispatchGate.windowKey, 0.10 as Double?, 0.21 as Double?),
               (AgentDispatchGate.windowCredits, 17.03 as Double?, 0.21 as Double?)]
let spent = AgentDispatchGate.budgetDecide(dollars, floor: 1.0,
                                           unit: AgentDispatchGate.unitUsd)
check(!spent.affordable && spent.window == AgentDispatchGate.windowKey
      && spent.left == 0.10 && spent.needed == 0.21
      && spent.unit == AgentDispatchGate.unitUsd,
      "a key with less than one task's worth left holds the work, in dollars")
let uncapped = AgentDispatchGate.budgetDecide(
    [(AgentDispatchGate.windowKey, nil, 0.21),
     (AgentDispatchGate.windowCredits, 0.05, 0.21)],
    floor: 1.0, unit: AgentDispatchGate.unitUsd)
check(!uncapped.affordable && uncapped.window == AgentDispatchGate.windowCredits,
      "an uncapped key has no ceiling of its own; the balance still gates")

section("the agent-task list and the queue behind the cap")
// PARITY: every `AgentTaskQueue` case below is asserted again, on the same inputs, by
// diplomat-platform/linux/tests/test_autofix.py — the Linux applet queues, arranges
// and drains the same work from its own twin of this type, so the two front-ends can
// only disagree about what runs next by failing one of the two suites.
// (`AgentTaskStatus` has no twin: a Linux spawn has no session row to sort.)
//
// The list's reading order, which is also the Agent-tasks row's status precedence.
check(AgentTaskStatus.allCases
      == [.merged, .done, .awaitingInput, .running, .starting, .unknown, .free, .queued],
      "finished first, then what wants a human, then what doesn't, then what is "
      + "spawning, then what nothing is known about, then this device's empty slots, "
      + "then what hasn't started")
check(AgentTaskStatus.merged < AgentTaskStatus.queued
      && AgentTaskStatus.awaitingInput < AgentTaskStatus.running,
      "the case order IS the sort order")
check(AgentTaskStatus.running < AgentTaskStatus.free
      && AgentTaskStatus.free < AgentTaskStatus.queued,
      "an empty slot stands where a running agent would — under them, above the queue")
check(AgentTaskStatus.running < AgentTaskStatus.starting
      && AgentTaskStatus.starting < AgentTaskStatus.free,
      "a task whose spawn is in flight is drawn where its session will be, not down "
      + "in the queue it just left")
// Every resolved state has a row to be drawn as, and the two orders agree. A state
// with no case here would draw as whatever `of` fell through to, and one ordered
// differently would sort the panel against the order `AgentState.rows` sorted it in.
check(AgentState.RunState.allCases.map(AgentTaskStatus.of)
      == [.merged, .done, .awaitingInput, .running, .starting, .unknown],
      "every state a run resolves to has a row status")
check(AgentState.stateOrder.map(AgentTaskStatus.of)
      == AgentTaskStatus.allCases.filter { $0 != .free && $0 != .queued },
      "the resolver's reading order is the row order, minus the two statuses no run "
      + "resolves to")
check(AgentTaskStatus.queued.title == "queued" && AgentTaskStatus.awaitingInput.title == "awaiting input"
      && AgentTaskStatus.free.title == "free slot" && AgentTaskStatus.starting.title == "starting"
      && AgentTaskStatus.unknown.title == "unknown",
      "the words the rows show")

// The empty bays the panel draws for the rest of the device's cap.
check(AgentTaskQueue.freeSlots(limit: 2, running: 0) == 2,
      "an idle device is all free slots")
check(AgentTaskQueue.freeSlots(limit: 2, running: 1) == 1,
      "each running automatic agent takes one")
check(AgentTaskQueue.freeSlots(limit: 2, running: 2) == 0,
      "a device at its cap has none")
check(AgentTaskQueue.freeSlots(limit: 1, running: 4) == 0,
      "more agents up than the cap allows draws no slots, not negative ones")

// Queue identity: two monitors owing the same PR are two tasks, and a push must
// not lose the operator's place for either (so: not the sha-scoped mesh key).
check(AgentTaskQueue.key(auditAction: "conflicts", prNumber: 7) == "conflicts:7",
      "queue key is the monitor's verb plus the PR")
check(AgentTaskQueue.key(auditAction: "review-req", prNumber: 7)
      != AgentTaskQueue.key(auditAction: "review-reply", prNumber: 7),
      "one PR can owe two monitors — two tasks, two keys")

check(AgentTaskQueue.order(offered: ["a", "b", "c"], saved: []) == ["a", "b", "c"],
      "never arranged ⇒ the order the monitors found it in")
check(AgentTaskQueue.order(offered: ["a", "b", "c"], saved: ["c", "a"]) == ["c", "a", "b"],
      "arranged tasks keep their place; a new one lands behind them")
check(AgentTaskQueue.order(offered: ["b"], saved: ["c", "a", "b"]) == ["b"],
      "work GitHub no longer owes drops out — the queue never outlives its evidence")
check(AgentTaskQueue.order(offered: [], saved: ["a"]) == [],
      "nothing offered ⇒ nothing queued")
check(AgentTaskQueue.order(offered: ["a", "a", "b"], saved: ["b", "b"]) == ["b", "a"],
      "a key offered or saved twice is still one task")

// A drag has to be able to reach every position, including the end.
check(AgentTaskQueue.reorder(["a", "b", "c", "d"], moving: "a", onto: "c")
      == ["b", "c", "a", "d"], "dragged down ⇒ lands after the row it was dropped on")
check(AgentTaskQueue.reorder(["a", "b", "c", "d"], moving: "d", onto: "b")
      == ["a", "d", "b", "c"], "dragged up ⇒ lands before the row it was dropped on")
check(AgentTaskQueue.reorder(["a", "b", "c"], moving: "a", onto: "c") == ["b", "c", "a"],
      "dropping on the last row is how a task is sent to the back")
check(AgentTaskQueue.reorder(["a", "b", "c"], moving: "b", onto: "b") == ["a", "b", "c"],
      "a drop onto itself rearranges nothing")
check(AgentTaskQueue.reorder(["a", "b"], moving: "z", onto: "a") == ["a", "b"]
      && AgentTaskQueue.reorder(["a", "b"], moving: "a", onto: "z") == ["a", "b"],
      "a drag naming a task that left the queue mid-drag changes nothing")

// PARITY: diplomat-platform/linux/tests/test_autofix.py asserts the same arrangements —
// the band is the one rule that outranks the operator's, so the two front-ends must
// not disagree about where a conflict fix waits.
check(AgentTaskQueue.band("conflicts:1") == 2 && AgentTaskQueue.band("review:2") == 1
      && AgentTaskQueue.band("review-req:3") == 0 && AgentTaskQueue.band("a") == 0,
      "a conflict fix bands last, a requested review behind the monitors' own finds, "
      + "and everything else — including a verbless key — first")
check(AgentTaskQueue.order(offered: ["conflicts:1", "review:2", "review-req:3",
                                     "review-reply:4"], saved: [])
      == ["review-req:3", "review-reply:4", "review:2", "conflicts:1"],
      "what GitHub is owed runs before the sweep the operator asked for, which runs "
      + "before the conflict fix another agent may make unnecessary")
check(AgentTaskQueue.order(offered: ["review:1", "review:2"], saved: ["review:2"])
      == ["review:2", "review:1"],
      "within the requested band the arrangement still decides")
check(AgentTaskQueue.reorder(["review-req:1", "review:2", "conflicts:3"],
                             moving: "review:2", onto: "review-req:1")
      == ["review-req:1", "review:2", "conflicts:3"]
      && AgentTaskQueue.reorder(["review-req:1", "review:2", "conflicts:3"],
                                moving: "review:2", onto: "conflicts:3")
      == ["review-req:1", "review:2", "conflicts:3"],
      "the requested band is a band like the others — a drag out of it is refused "
      + "whichever side it heads for")
check(AgentTaskQueue.order(offered: ["conflicts:1", "review-req:2"], saved: [])
      == ["review-req:2", "conflicts:1"],
      "a conflict fix waits behind a review however the monitors found them")
check(AgentTaskQueue.order(offered: ["conflicts:1", "review-req:2"],
                           saved: ["conflicts:1", "review-req:2"])
      == ["review-req:2", "conflicts:1"],
      "the band outranks the arrangement — 'last' is not a default a drag can undo")
check(AgentTaskQueue.order(offered: ["conflicts:1", "conflicts:2", "review-req:3", "review-req:4"],
                           saved: ["review-req:4", "conflicts:2"])
      == ["review-req:4", "review-req:3", "conflicts:2", "conflicts:1"],
      "within a band the arrangement still decides")
check(AgentTaskQueue.reorder(["review-req:1", "review-req:2", "conflicts:3"],
                             moving: "conflicts:3", onto: "review-req:1")
      == ["review-req:1", "review-req:2", "conflicts:3"]
      && AgentTaskQueue.reorder(["review-req:1", "review-req:2", "conflicts:3"],
                                moving: "review-req:1", onto: "conflicts:3")
      == ["review-req:1", "review-req:2", "conflicts:3"],
      "a drag across the band boundary is refused, not landed and sprung back")
check(AgentTaskQueue.reorder(["conflicts:1", "conflicts:2"],
                             moving: "conflicts:1", onto: "conflicts:2")
      == ["conflicts:2", "conflicts:1"],
      "within the conflict band a drag works like any other")

check(AgentTaskQueue.stillOwed(auditAction: "conflicts", prNumber: 8,
                               conflicting: [7], owingReply: [8], closed: []) == false,
      "a conflict fix on a PR this fetch no longer calls conflicting is work already done")
check(AgentTaskQueue.stillOwed(auditAction: "conflicts", prNumber: 7,
                               conflicting: [7], owingReply: [], closed: []),
      "…and one the fetch still calls conflicting is dispatched")
check(AgentTaskQueue.stillOwed(auditAction: "review-reply", prNumber: 8,
                               conflicting: [8], owingReply: [], closed: []) == false,
      "a reply on a PR whose threads are answered is work already done")
check(AgentTaskQueue.stillOwed(auditAction: "review-req", prNumber: 3,
                               conflicting: [], owingReply: [], closed: []),
      "a review requested of me is not in this fetch to check — unanswerable is not stale")
// A closed PR is the one answer that reaches every verb, including the two the fetches
// above cannot speak for: a review of a merged diff is nobody's work.
check(AgentTaskQueue.stillOwed(auditAction: "review-req", prNumber: 3,
                               conflicting: [], owingReply: [], closed: [3]) == false,
      "…until its PR closes, which no agent of mine can put back")
check(AgentTaskQueue.stillOwed(auditAction: "review", prNumber: 31,
                               conflicting: [], owingReply: [], closed: [31]) == false,
      "a review the operator asked for goes the same way — merged is merged")
check(AgentTaskQueue.stillOwed(auditAction: "conflicts", prNumber: 7,
                               conflicting: [7], owingReply: [], closed: [7]) == false,
      "closed outranks a fetch that still calls the PR conflicting")

section("agent state: what every dispatched run resolves to")
// PARITY: the whole scenario table lives in
// diplomat-platform/linux/tests/test_agent_state.py and is driven through BOTH
// implementations by test_agent_state_parity.py (via `diplomat-core agent-state`),
// reason strings included. What is asserted here is the handful of rungs that must
// never regress even if that binary is not built — above all the two directions in
// which this resolver replaced a front-end that guessed.
do {
    let now: TimeInterval = 1_000_000
    func rec(_ id: String, pid: Int? = 4242, tty: String = "pts/3",
             age: TimeInterval = 60,
             placement: AgentState.Placement = .local,
             source: String = AgentDispatchGate.Source.auto.rawValue,
             workKey: String = "", seen: TimeInterval? = nil) -> AgentState.RunRecord {
        AgentState.RunRecord(runID: id, dispatchedAt: now - age, prNumber: 337,
                             source: source, placement: placement,
                             workKey: workKey, pid: pid, tty: tty, claimSeenAt: seen)
    }
    let alive = [4242: AgentState.ProcInfo(tty: "pts/3", elapsed: 60, isAgent: true)]
    let working = "● Reading…\n⏵⏵ bypass permissions on · esc to interrupt · ← for agents"
    let atPrompt = "● Posted the review.\n❯\n⏵⏵ bypass permissions on (shift+tab to cycle)"

    func state(_ r: AgentState.RunRecord, _ e: AgentState.Evidence) -> AgentState.RunState {
        AgentState.resolveOne(r, evidence: e, now: now).state
    }
    // The rule the whole ladder exists for: a probe that could not answer never
    // yields `.finished`. Both front-ends used to read an unreadable process table
    // and an unreadable tmux as evidence that the agent was gone.
    check(state(rec("a"), AgentState.Evidence(
        processes: .unavailable("ps would not decode"), sentinels: .present([]),
        tails: .present([:]), claims: .present([]), mergedPRs: .present([]))) == .unknown,
      "an unreadable process table leaves a local run UNKNOWN, never finished")
    check(state(rec("b", placement: .meshPeer, workKey: "w", seen: now - 10),
                AgentState.Evidence(
        processes: .present([:]), sentinels: .present([]), tails: .present([:]),
        claims: .unavailable("mesh node down"), mergedPRs: .present([]))) == .unknown,
      "an unreadable claim book leaves a peer's run UNKNOWN, never finished")
    // A peer's agent is a process on somebody else's box, so a local `ps` is not
    // evidence about it at all. The Linux front-end used to retire every peer run
    // 120s after dispatch on exactly this reasoning.
    check(state(rec("c", pid: nil, tty: "", age: 3600, placement: .meshPeer,
                    workKey: "w", seen: now - 10),
                AgentState.Evidence(
        processes: .present([:]), sentinels: .present([]), tails: .present([:]),
        claims: .present(["w"]), mergedPRs: .present([]))) == .running,
      "an empty local process table never retires a run held on a peer's claim")
    // The pid IS the identity. A table we did read, without it, is positive evidence.
    check(state(rec("d"), AgentState.Evidence(
        processes: .present([:]), sentinels: .present([]), tails: .present([:]),
        claims: .present([]), mergedPRs: .present([]))) == .finished,
      "a pid missing from a table we DID read has finished")
    let live = AgentState.Evidence(processes: .present(alive), sentinels: .present([]),
                                   tails: .present(["pts/3": atPrompt]),
                                   claims: .present([]), mergedPRs: .present([]))
    check(state(rec("e"), live) == .awaitingInput,
      "a live agent whose screen shows the prompt is awaiting input")
    check(state(rec("f"), AgentState.Evidence(
        processes: .present(alive), sentinels: .present([]),
        tails: .unavailable("no tmux server"), claims: .present([]),
        mergedPRs: .present([]))) == .running,
      "…but an unreadable screen leaves it RUNNING — a bay costed, never a bay freed")
    check(state(rec("g"), AgentState.Evidence(
        processes: .present(alive), sentinels: .present([]),
        tails: .present(["pts/3": working]), claims: .present([]),
        mergedPRs: .present([]))) == .running,
      "the interrupt hint on the live status bar means mid-turn")
    // A placement the mesh routed back here: the NODE opened the terminal, so there is
    // no pid file this applet can read, ever. Judged on the prompt scan instead, or it
    // reads "unknown" for ever and its bay is never given back.
    check(state(rec("h", pid: nil, tty: "", age: 600, placement: .meshHere),
                AgentState.Evidence(
        processes: .present([:]), sentinels: .present([]),
        tails: .present(["pts/5": working]), claims: .present([]),
        mergedPRs: .present([]), liveAgents: .present([337: "pts/5"]))) == .running,
      "a pid-less run whose PR has a live agent is running, not unknown")
    check(state(rec("i", pid: nil, tty: "", age: 600, placement: .meshHere),
                AgentState.Evidence(
        processes: .present([:]), sentinels: .present([]), tails: .present([:]),
        claims: .present([]), mergedPRs: .present([]),
        liveAgents: .present([:]))) == .finished,
      "...and one whose PR has no agent in a scan that WORKED has finished")
    check(state(rec("j", pid: nil, tty: "", age: 600, placement: .meshHere),
                AgentState.Evidence(
        processes: .present([:]), sentinels: .present([]), tails: .present([:]),
        claims: .present([]), mergedPRs: .present([]),
        liveAgents: .unavailable("ps could not be read"))) == .unknown,
      "...but a scan that failed ends nothing")
    // The two sets the cap and the dedup read, which are deliberately different.
    check(AgentState.occupying.contains(.awaitingInput) == false,
      "a session at its prompt gives its bay back — the cap bounds LOAD")
    check(AgentState.blocking.contains(.awaitingInput),
      "…but still blocks a second agent: it holds the PR's context, waiting to be typed at")
    check(AgentState.occupying.contains(.unknown) && AgentState.blocking.contains(.unknown),
      "a run nothing is known about keeps both its bay and its dedup")
    // The projections, on one mixed tick.
    let mixed = [rec("auto"), rec("clicked", pid: 7, tty: "pts/9",
                                  source: AgentDispatchGate.Source.panel.rawValue),
                 rec("peer", pid: nil, tty: "", placement: .meshPeer, workKey: "w",
                     seen: now - 10)]
    let t = AgentState.tick(records: mixed, evidence: AgentState.Evidence(
        processes: .present(alive.merging([7: AgentState.ProcInfo(tty: "pts/9",
                                                                  elapsed: 60,
                                                                  isAgent: true)]) { a, _ in a }),
        sentinels: .present([]), tails: .present(["pts/3": working, "pts/9": working]),
        claims: .present(["w"]), mergedPRs: .present([]), liveAgents: .present([:])),
        now: now, limit: 2)
    check(t.capLoad == ["auto"],
      "the cap counts automatic agents that run HERE — not a click, not a peer's")
    check(t.freeSlots == 1, "one of two bays filled")
    check(t.inFlight(prNumber: 337), "and the PR they are all on reads in-flight")
    check(t.retirable.isEmpty, "nothing retires while every run is live")
    print("agent state assertions passed")
}

section("the run book's sidecars")
// PARITY: the same bodies `tests/test_agent_registry.py` parametrizes over. The guard is
// on the READ, so what it defends against is a torn write or a stray file — neither of
// which any caller can be made to produce, which is why the fixture writes them directly.
// Both front-ends read runs booked by the other, so a guard only one side applies is a
// run that binds nothing on one machine and queries a garbage session id on the other.
do {
    let book = FileManager.default.temporaryDirectory
        .appendingPathComponent("diplomat-smoke-agents-\(UUID().uuidString)", isDirectory: true)
    setenv("DIPLOMAT_AGENTS_DIR", book.path, 1)
    defer { try? FileManager.default.removeItem(at: book) }
    let now = Date().timeIntervalSince1970
    let run = AgentRegistry.createRun(
        AgentState.RunRecord(runID: AgentRegistry.newRunID(now: now), dispatchedAt: now),
        prompt: "p").runID

    check(AgentRegistry.boundSession(run) == "", "a run binds no session before one is found")
    AgentRegistry.bindSession(run, "ses_00d61ec0")
    check(AgentRegistry.boundSession(run) == "ses_00d61ec0", "and reads back the one it bound")
    for body in ["", "   ", "\n", "ses_a ses_b", String(repeating: "x", count: 400)] {
        try? body.write(to: AgentRegistry.sessionPath(run), atomically: true, encoding: .utf8)
        check(AgentRegistry.boundSession(run) == "",
              "a session file that is not one id binds nothing, not \(body.count) chars of one")
    }

    check(AgentRegistry.port(run) == nil, "a run with no port file has no port")
    check(AgentRegistry.stagePort(run, 47_910) && AgentRegistry.port(run) == 47_910,
          "a staged port reads back")
    for body in ["0", "65536", "-1", "", "not-a-port", "47910 47911"] {
        try? body.write(to: AgentRegistry.portPath(run), atomically: true, encoding: .utf8)
        check(AgentRegistry.port(run) == nil, "a port file of “\(body)” is no port")
    }

    check(AgentRegistry.runRunner(run) == "", "an unstaged runner is unknown, not Claude Code")
    AgentRegistry.stageRunner(run, AgentRunner.opencode.rawValue)
    check(AgentRegistry.runRunner(run) == "opencode", "and reads back the one it staged")
    AgentRegistry.forget([run])
    check(AgentRegistry.load().isEmpty && AgentRegistry.boundSession(run) == "",
          "forgetting a run takes its sidecars with it")
    print("run book sidecar assertions passed")
}

section("autofix mesh coordination")
// PARITY fixtures: diplomat-platform/linux/tests/test_autofix.py asserts these exact strings — two
// nodes only dedupe origination when their derivations agree byte-for-byte
// (szpontnet-spec/docs/12-work-claims.md).
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

// The ledger key is the same string whenever the claim key exists, so the two
// records of one job — the mesh's claim and the telemetry ledger's task — name it
// identically. Where the claim degrades to "" for an unknown sha, the ledger key
// does NOT: skipping a claim is safe, and skipping the ledger entry would drop
// dispatched work off every figure on the Telemetry screen.
check(AutofixMesh.ledgerKey(kind: "review", prURL: "https://github.com/acme/app/pull/123",
                            headSha: "abc123")
      == AutofixMesh.workKey(kind: "review", prURL: "https://github.com/acme/app/pull/123",
                             headSha: "abc123"),
      "ledger key must equal the claim key when the sha is known")
check(AutofixMesh.ledgerKey(kind: "review", prURL: "https://github.com/acme/app/pull/123",
                            headSha: "") == "review:github.com/acme/app#123",
      "a missing sha must not cost the ledger its entry")
check(AutofixMesh.ledgerKey(kind: "review", prURL: "https://github.com/acme/app/issues/5",
                            headSha: "x") == "", "not a PR URL → nothing to name")
check(AutofixMesh.ledgerKey(kind: "review", prURL: "not a url", headSha: "x") == "")

print("autofix mesh coordination assertions passed")

// ---- telemetry ----
section("telemetry")
// The cross-platform diff lives in tests/test_telemetry_parity.py; these are the
// properties that hold whatever the other implementation does.
let tNow = 1_785_000_000.0
let tLines = [
    // Two intervals price the window: 25% of it bought 2.5M tokens, so it is worth
    // 10M. The third spans a RESET (more left than before), which prices nothing.
    #"{"at": 1784900000, "ev": "sample", "sessionLeft": 1.0, "weekLeft": 1.0, "repoTokens": 0, "otherTokens": 0}"#,
    #"{"at": 1784903600, "ev": "sample", "sessionLeft": 0.85, "weekLeft": 0.99, "repoTokens": 1000000, "otherTokens": 500000}"#,
    #"{"at": 1784907200, "ev": "sample", "sessionLeft": 0.75, "weekLeft": 0.98, "repoTokens": 1600000, "otherTokens": 900000}"#,
    #"{"at": 1784910800, "ev": "sample", "sessionLeft": 1.0, "weekLeft": 0.97, "repoTokens": 1800000, "otherTokens": 1000000}"#,
    #"{"at": 1784920000, "ev": "queued", "key": "review:h/o/r#1@aa", "duty": "review", "pr": 1}"#,
    #"{"at": 1784920600, "ev": "started", "key": "review:h/o/r#1@aa", "remote": false, "attempt": 1}"#,
    // A retry appends a second `started`; first-wins keeps the measured wait honest.
    #"{"at": 1784929000, "ev": "started", "key": "review:h/o/r#1@aa", "remote": false, "attempt": 2}"#,
    #"{"at": 1784921800, "ev": "done", "key": "review:h/o/r#1@aa", "tokens": 500000}"#,
    // Placed on a peer: started here, spent there.
    #"{"at": 1784930000, "ev": "queued", "key": "conflicts:h/o/r#2@bb", "duty": "conflicts", "pr": 2}"#,
    #"{"at": 1784930100, "ev": "started", "key": "conflicts:h/o/r#2@bb", "remote": true, "attempt": 1}"#,
    // Still owed at `now`.
    #"{"at": 1784996400, "ev": "queued", "key": "review:h/o/r#3@cc", "duty": "review", "pr": 3}"#,
    // Junk a partially-written or newer-platform tail can hold.
    "{not json",
    #"{"at": 1784999000, "ev": "teleported", "key": "review:h/o/r#4@dd"}"#,
]
let ledger = Telemetry.fold(lines: tLines)
check(ledger.samples.count == 4, "sample count")
check(ledger.tasks.count == 3, "an unparseable or unknown line created a task")
check(ledger.tasks[0].waitSecs == 600, "a retry moved the measured wait")
check(Telemetry.calibrate(ledger.samples, session: true) == 10_000_000,
      "the window must be priced from what was actually spent, reset intervals skipped")
let summary = Telemetry.summarize(ledger, now: tNow, days: 14, steps: 56,
                                  binCount: 12, z: 1.96)
check(summary.startedCount == 2 && summary.remoteCount == 1)
check(summary.perTask.count == 1, "a peer's agent was priced against our own window")
check(summary.perTask.mean == 5, "500k of a 10M window is 5%")
// The same task against the other window: 2.8M tokens bought 3% of the week, so the
// week is worth ~93.3M and a 500k task is ~0.54% of it — a different measurement of
// the same work, not the 5-hour figure relabelled.
check(abs(summary.perTaskWeek.mean - 100 * 500_000 / (2_800_000 / 0.03)) < 1e-9,
      "the week was priced off the 5-hour calibration instead of its own")
// One axis for both, or the same task would land in a different bin on each.
check(summary.perTask.bins.last?.upper == summary.perTaskWeek.bins.last?.upper,
      "the two histograms did not share their bin edges")
check(summary.pendingReviewsNow == 1 && summary.pendingConflictsNow == 0,
      "started work is not still owed")
check(summary.repoTokens == 1_800_000 && summary.otherTokens == 1_000_000)
// The quota readings are drawn as they were taken, not resampled onto a grid: the
// 5-hour window's sawtooth is the shape worth seeing, and interpolating it would
// smooth away the resets that give it its meaning.
check(summary.quota.count == 4, "a quota reading was dropped or invented")
check(summary.quota.map(\.sessionPct) == [100, 85, 75, 100],
      "the readings moved off the instants they were measured at")
check(summary.sessionLeftPct == 100 && summary.weekLeftPct == 97,
      "the headline is the newest reading of each window")
check(Telemetry.duration(0, samples: 0) == "—", "no samples must not read as instant")
check(Telemetry.duration(90) == "1m 30s")
check(Telemetry.duration(5400) == "1h 30m")
check(Telemetry.percent(5) == "5.0%")
check(Telemetry.tokens(1_800_000) == "1.8M")
// A ledger with no quota readings can count tokens but cannot honestly turn them
// into a share of a window — the screen shows tokens and says so.
let unpriced = Telemetry.fold(lines: tLines.filter { !$0.contains("\"sample\"") })
check(Telemetry.calibrate(unpriced.samples, session: true) == nil)
let unpricedSummary = Telemetry.summarize(unpriced, now: tNow, days: 14, steps: 56,
                                          binCount: 12, z: 1.96)
check(unpricedSummary.sessionLimitTokens == nil)
check(unpricedSummary.perTask.count == 0, "a percentage was invented without a price")
check(unpricedSummary.perTaskWeek.count == 0, "a weekly percentage was invented too")
check(unpricedSummary.perTaskTokensMean == 500_000, "the raw cost is still reported")
// And the two fail apart: the 5-hour window resets on its own cycle, so a ledger
// whose samples straddle a reset reads as a window that went UP and prices nothing,
// while the week only ever falls. The screen and the dispatch gate both act on the
// week alone here, so it must not go empty with the window that failed.
let sessionBlind = Telemetry.fold(lines: tLines.map {
    $0.replacingOccurrences(of: #""sessionLeft": 0.85"#, with: #""sessionLeft": 1.0"#)
      .replacingOccurrences(of: #""sessionLeft": 0.75"#, with: #""sessionLeft": 1.0"#)
})
let sessionBlindSummary = Telemetry.summarize(sessionBlind, now: tNow, days: 14,
                                              steps: 56, binCount: 12, z: 1.96)
check(sessionBlindSummary.sessionLimitTokens == nil)
check(sessionBlindSummary.perTask.count == 0)
check(sessionBlindSummary.perTaskWeek.count == 1,
      "the week went unpriced with the 5-hour window rather than on its own readings")
// A probe that has been down for an hour must not blank a figure it measured
// perfectly well an hour ago, and a missing reading is not a window at zero.
let blind = Telemetry.fold(lines: tLines + [
    #"{"at": 1784914400, "ev": "sample", "sessionLeft": null, "weekLeft": null, "repoTokens": 1800000, "otherTokens": 1000000}"#,
])
let blindSummary = Telemetry.summarize(blind, now: tNow, days: 14, steps: 56,
                                       binCount: 12, z: 1.96)
check(blindSummary.quota.count == 5, "a blind sample is still a sample taken")
check(blindSummary.quota.last?.sessionPct == nil, "a gap was filled in")
check(blindSummary.sessionLeftPct == 100 && blindSummary.weekLeftPct == 97,
      "a silent probe blanked the last figure it did measure")
print("telemetry assertions passed")

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
// Default now: soft-approve ON, no hard verdict → a perfectly-clean PR gets a friendly
// thank-you comment, but still NO APPROVE action.
section("known-theirs review prompt (default → soft-approve)")
let kt = ReviewConfig(depth: "max", target: .specific, me: "latekvo",
                      markReady: false, leaveReviews: true, replyToReviews: false,
                      specificPR: "500", specificAuthor: .theirs).buildPrompt()
check(!kt.contains("WHO AUTHORED IT"), "known-theirs skips the author poll")
check(!kt.contains("CASE A") && !kt.contains("CASE B"), "no case branching")
check(kt.contains("SOMEONE ELSE'S"), "review-only framing")
check(kt.contains("ABSOLUTELY DO NOT touch their branch"), "reviewOnly block present")
check(kt.contains("POST a pull-request review"), "leaveReviews block present")
check(kt.contains("Do NOT mark this PR ready"), "otherNoMarkReady present")
check(kt.contains("that single clean pass ends the loop"), "max-depth fragment present")
// Soft-approve: no hard verdict, but a friendly clean comment — never an APPROVE action.
check(kt.contains("Do NOT submit an APPROVE"), "soft-approve still withholds the APPROVE verdict")
check(kt.contains("Thank you for contributing"), "soft-approve clean thank-you comment present")
check(!kt.contains("PR #500 looks clean"), "soft-approve replaces the silent no-verdict close")
check(!kt.contains("still APPROVE"), "the finalPass approve-verdict block is gone")
check(!kt.contains("fix it directly on the PR's branch"), "never fixes someone else's branch")
check(!kt.contains("No AI attribution"), "no commits ⇒ no attribution block")
print("known-theirs (soft-approve) review prompt assertions passed")

// ---- known-theirs, soft-approve OFF → fully silent no-verdict ----
section("known-theirs review prompt (soft-approve off → silent)")
let kts = ReviewConfig(depth: "max", target: .specific, me: "latekvo",
                       markReady: false, leaveReviews: true, replyToReviews: false,
                       specificPR: "500", softApprove: false, specificAuthor: .theirs).buildPrompt()
check(kts.contains("Do NOT submit an APPROVE"), "no-verdict instruction present")
check(kts.contains("PR #500 looks clean"), "no-verdict {pr} substituted")
check(!kts.contains("Thank you for contributing"), "soft-approve off ⇒ no thank-you comment")
check(!kts.contains("still APPROVE"), "no finalPass block")
print("known-theirs (silent) review prompt assertions passed")

// ---- known-theirs WITH verdict (trusted author: member/maintainer/contributor) ----
// The review-request monitor sets finalPass=true when the PR author is trusted, so the
// auto-review closes with an APPROVE/changes-requested verdict instead of comments only.
// A real verdict outranks the (default-on) soft-approve, so no thank-you comment either.
section("known-theirs review prompt (trusted author → verdict)")
let ktv = ReviewConfig(depth: "max", target: .specific, me: "latekvo",
                       markReady: false, leaveReviews: true, replyToReviews: false,
                       specificPR: "500", finalPass: true, specificAuthor: .theirs).buildPrompt()
check(ktv.contains("SOMEONE ELSE'S"), "review-only framing still present with verdict")
check(ktv.contains("still APPROVE"), "trusted author ⇒ finalPass APPROVE-verdict block present")
check(!ktv.contains("Do NOT submit an APPROVE"), "trusted author ⇒ no no-verdict block")
check(!ktv.contains("Thank you for contributing"), "hard verdict outranks soft-approve")
print("known-theirs (trusted author) review prompt assertions passed")

// ---- The method every swarm depth dispatches across ----
// The goldens are regenerated from these fragments, so they pin Swift/Python parity and no
// meaning at all: a depth that stopped naming a single move would regenerate green. These
// name the method itself.
//
// On the `.mine` prompt: softApprove quotes the move names in its example comment, so on a
// `.theirs` prompt a move deleted from the fragment is still found there.
section("review moves + absence pass")
let moves = ["claims vs code", "nearest twin", "non-happy paths",
             "inputs, reachability both ways", "what outlives the call"]
let absencePass = ["a sibling that has it", "prose that promises it", "symmetry",
                   "and the mutation"]
for swarmDepth in ["standard", "deep", "max"] {
    let p = ReviewConfig(depth: swarmDepth, me: "latekvo").buildPrompt()
    for move in moves {
        check(p.contains(move), "\(swarmDepth) depth names the '\(move)' move")
    }
    for part in absencePass {
        check(p.contains(part), "\(swarmDepth) depth carries the absence pass — '\(part)'")
    }
}
// `quick` dispatches no swarm — and without this negative the loop above would pass just
// as well on a move list appended to every depth.
let quickPrompt = ReviewConfig(depth: "quick", me: "latekvo").buildPrompt()
check(!quickPrompt.contains("nearest twin"), "quick depth runs no swarm and names no moves")
check(!quickPrompt.contains("a sibling that has it"), "quick depth carries no absence pass")
print("review moves + absence pass assertions passed")

// ---- Where a swarm's spilled summaries may be read from ----
// A runner that spills a subagent's full output writes it to one machine-wide cache under a
// name carrying only a task index and a timestamp, and the applet dispatches several of
// these prompts at once against one agent home — so a glob there spans every concurrent
// run's summaries, not this one's. `quick` carries the rule too: the bar is not depth-gated.
section("delegation summary provenance")
for p in [ReviewConfig(depth: "standard", me: "latekvo").buildPrompt(),
          ReviewConfig(depth: "deep", target: .specific, me: "latekvo",
                       specificPR: "768", specificAuthor: .theirs).buildPrompt(),
          ReviewConfig(depth: "max", me: "latekvo").buildPrompt(),
          AuditConfig().buildPrompt(),
          AuditConfig(fixIssues: true, openPRs: true).buildPrompt()] {
    check(p.contains("EXACT path that delegation handed back"),
          "prompt pins the summary to the path delegation returned")
    check(p.contains("never by globbing or listing the summary cache"),
          "prompt forbids resolving a summary by pattern")
}
print("delegation summary provenance assertions passed")

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
// A turn cut short: every wording the CLI builds this family from — a cause plus one of
// two endings. The endings are what the matcher reads, so all seven must nudge.
for banner in [
    "Server error mid-response. The response above may be incomplete.",
    "Connection lost mid-response. The response above may be incomplete.",
    "Your computer went to sleep mid-response. The response above may be incomplete.",
    "The response stopped arriving. The response above may be incomplete.",
    "The response stalled before a response was produced. Try again.",
    "Connection lost before a response was produced. Try again.",
    "Your computer went to sleep before a response was produced. Try again.",
] {
    check(ApiErrorMatch.looksLikeApiError("⏺ API Error: \(banner)"), banner)
}
// An ending without the prefix is ordinary prose, not a banner.
check(!ApiErrorMatch.looksLikeApiError("note: the response above may be incomplete"))
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
// An org's spend cap is a quota too, however transient its 403 looks: it holds until
// the window rolls over or an admin raises it. It arrives WITH the "API Error: <code>"
// prefix, so it stays un-nudged only while the budget wording is read ahead of the code.
check(!ApiErrorMatch.looksLikeApiError(
    "API Error: 403 Org member budget limit exceeded (daily limit). Contact your org admin."))
check(!ApiErrorMatch.looksLikeApiError("Organization budget exceeded"))
check(!ApiErrorMatch.looksLikeApiError("workspace monthly budget limit reached"))
// Prose about a budget that isn't a cap being hit leaves a real error nudgeable.
check(ApiErrorMatch.looksLikeApiError("API Error: 529 Overloaded\nthe budget for this run was 500k tokens"))
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
// committed golden file in assets/golden-prompts/. The Linux tests assert the SAME
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
    ("issues-all", IssueConfig(me: goldenMe).buildPrompt()),
    ("issues-mine", IssueConfig(target: .mine, me: goldenMe).buildPrompt()),
    ("issues-user", IssueConfig(target: .someone, username: "someuser",
                                me: goldenMe).buildPrompt()),
    ("issues-contributors", IssueConfig(target: .contributors, me: goldenMe).buildPrompt()),
    ("issues-members", IssueConfig(target: .members, me: goldenMe).buildPrompt()),
    ("issues-single", IssueConfig(target: .specific, me: goldenMe,
                                  specificIssue: "421").buildPrompt()),
    // Every action toggle off — the one golden that holds the complementary blocks
    // (no-PRs instead of open-PRs, no unassigned filter, no claim, no attribution tag).
    ("issues-hands-off", IssueConfig(me: goldenMe, unassignedOnly: false,
                                     assignToMe: false, openPRs: false,
                                     commentOnIssue: false).buildPrompt()),
    ("issues-features-max", IssueConfig(depth: "max", me: goldenMe,
                                        includeFeatures: true).buildPrompt()),
]
let goldenDir = try CoreAssets.assetsDir().appendingPathComponent("golden-prompts")
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
