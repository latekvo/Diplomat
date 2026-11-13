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

section("free-disk floor")
// Python twin: test_diskfloor.py asserts the same numbers and wording.
check(DiskFloor.floor(env: [:]) == 5 << 30,
      "the floor is config.json's 5 GiB, got \(String(describing: DiskFloor.floor(env: [:])))")
check(DiskFloor.floor(env: [DiskFloor.envOverride: "64"]) == 64 << 20,
      "the override is in MiB")
check(DiskFloor.floor(env: [DiskFloor.envOverride: "0"]) == 0, "an override of 0 turns it off")
for junk in ["", "-1", "+64", " 64", "5GiB", "1.5"] {
    check(DiskFloor.floor(env: [DiskFloor.envOverride: junk]) == 5 << 30,
          "an override that is not a whole MiB count falls back to the asset: \(junk)")
}
for huge in ["9223372036854775807", "99999999999999999999999"] {
    check(DiskFloor.floor(env: [DiskFloor.envOverride: huge]).map { $0 > 1 << 60 } == true,
          "an override past what Int64 holds is a floor no disk passes: \(huge)")
}
let diskTmp = FileManager.default.temporaryDirectory.path
check((DiskFloor.free(at: diskTmp) ?? 0) > 0, "the temp dir's volume reads")
check(DiskFloor.free(at: diskTmp + "/diplomat-no-such/deeper") != nil,
      "a path not made yet reads its nearest existing ancestor's volume")
let tmpReading = DiskFloor.lowest(of: [diskTmp], env: [DiskFloor.envOverride: "1"])
check(tmpReading?.path == diskTmp && tmpReading?.floor == 1 << 20 && tmpReading?.low == false,
      "a reading names its path and floor, got \(String(describing: tmpReading))")
check(DiskFloor.size(5 << 30) == "5.0 GiB" && DiskFloor.size(1_288_490_189) == "1.2 GiB",
      "GiB to one decimal")
check(DiskFloor.size(0) == "0 MiB" && DiskFloor.size((1 << 30) - 1) == "1023 MiB",
      "whole MiB under a GiB")
check(DiskFloor.describe(.init(path: "/v", free: 3 << 20, floor: 5 << 30))
      == "3 MiB free on /v, below the 5.0 GiB floor", "the reason line")
check(DiskFloor.Reading(path: "/v", free: 5 << 30, floor: 5 << 30).low == false
      && DiskFloor.Reading(path: "/v", free: (5 << 30) - 1, floor: 5 << 30).low,
      "exactly the floor is enough, a byte under is not")
check(DiskFloor.floor(env: [:], asset: 2.5) == 5 << 29, "the asset is in GiB")
check(DiskFloor.floor(env: [:], asset: 1e10) == .max,
      "an asset floor past what Int64 holds is one no disk passes, not a trap")
for junk in [-1, .nan, .infinity] as [Double] {
    check(DiskFloor.floor(env: [:], asset: junk) == nil, "an asset floor of \(junk) is none")
}
// The floor rides in config.json beside the repo coordinates, and a bad one must not
// take them down with it: `diskfloor.floor` ignores it, and `config()` still answers.
for value in ["\"5\"", "true", "null", "[5]"] {
    let raw = #"{"owner": "o", "repo": "r", "minFreeDiskGiB": "# + value + "}"
    let cfg = try? JSONDecoder().decode(CoreAssets.Config.self, from: Data(raw.utf8))
    check(cfg?.owner == "o" && cfg?.repo == "r" && cfg?.minFreeDiskGiB == nil,
          "a minFreeDiskGiB of \(value) drops only the floor, got \(String(describing: cfg))")
}
check((try? JSONDecoder().decode(CoreAssets.Config.self,
                                 from: Data(#"{"owner": "o", "repo": "r"}"#.utf8)))
          .map { $0.minFreeDiskGiB == nil } == true, "no minFreeDiskGiB is no floor")

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
check(!ThreadTriage.owed(isResolved: false, viewerCanResolve: true, lastCommentAuthor: "LateKVO", me: "latekvo",
                         unansweredRunFinding: false))
check(ThreadTriage.owed(isResolved: false, viewerCanResolve: true, lastCommentAuthor: "reviewer", me: "latekvo",
                        unansweredRunFinding: false))
check(!ThreadTriage.owed(isResolved: true, viewerCanResolve: true, lastCommentAuthor: "reviewer", me: "latekvo",
                         unansweredRunFinding: false))
check(!ThreadTriage.owed(isResolved: false, viewerCanResolve: false, lastCommentAuthor: "reviewer", me: "latekvo",
                         unansweredRunFinding: false))
// Missing viewerCanResolve (older payloads) defaults to owed; nil author (deleted user) is owed.
check(ThreadTriage.owed(isResolved: false, viewerCanResolve: nil, lastCommentAuthor: "reviewer", me: "latekvo",
                        unansweredRunFinding: false))
check(ThreadTriage.owed(isResolved: false, viewerCanResolve: true, lastCommentAuthor: nil, me: "latekvo",
                        unansweredRunFinding: false))
// A run posts its findings under my login, so its unanswered finding is owed although the
// last comment is mine; once anyone replies, or the thread is resolved, it is not.
let runFinding = #"\[[Diplomat](https://github.com/latekvo/Diplomat), Opus 5.5\]: a finding"#
check(ThreadTriage.isUnansweredRunFinding(commentCount: 1, lastCommentBody: "\n " + runFinding))
check(!ThreadTriage.isUnansweredRunFinding(commentCount: 2, lastCommentBody: runFinding),
      "a run's finding someone replied to is answered")
check(!ThreadTriage.isUnansweredRunFinding(commentCount: 1, lastCommentBody: "a note of my own"),
      "an untagged comment of mine is not a run's finding")
check(!ThreadTriage.isUnansweredRunFinding(commentCount: nil, lastCommentBody: runFinding))
check(ThreadTriage.owed(isResolved: false, viewerCanResolve: true, lastCommentAuthor: "latekvo",
                        me: "latekvo", unansweredRunFinding: true), "a run's unanswered finding is owed")
check(!ThreadTriage.owed(isResolved: true, viewerCanResolve: true, lastCommentAuthor: "latekvo",
                         me: "latekvo", unansweredRunFinding: true), "a resolved run finding is not owed")
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
// A link to a listed checkout is accepted and resolves to it; the configured repo
// outranks a listed one with the same coordinates, and an unlisted repo still mismatches.
let radon = PRRef.LocalRepo(owner: "software-mansion", repo: "radon", path: "/tmp/radon")
let twin = PRRef.LocalRepo(owner: cfg.owner, repo: cfg.repo, path: "/tmp/twin")
let listed = PRRef.parse("https://github.com/Software-Mansion/Radon/pull/291",
                         owner: cfg.owner, repo: cfg.repo, also: [twin, radon])
check(listed.number == 291 && listed.isValid && listed.repo == radon)
check(PRRef.parse("software-mansion/radon#7", owner: cfg.owner, repo: cfg.repo,
                  also: [radon]).repo == radon)
check(PRRef.parse("\(cfg.owner)/\(cfg.repo)#9", owner: cfg.owner, repo: cfg.repo,
                  also: [twin]).repo == nil)
check(PRRef.parse("https://github.com/other/proj/pull/5", owner: cfg.owner, repo: cfg.repo,
                  also: [radon]).repoMismatch)
check(PRRef.parse("291", owner: cfg.owner, repo: cfg.repo, also: [radon]).repo == nil)
// The configs follow the resolved repo in single-item mode only: a sweep with a listed
// link left in its field still targets, and works in, the configured repo.
var radonReview = single("https://github.com/software-mansion/radon/pull/291")
radonReview.localRepos = [radon]
check(radonReview.targetRepo == ("software-mansion", "radon") && radonReview.checkout == "/tmp/radon")
check(radonReview.buildPrompt().contains("software-mansion/radon"))
check(!radonReview.buildPrompt().contains("\(cfg.owner)/\(cfg.repo)"))
radonReview.target = .mine
check(radonReview.targetRepo == (cfg.owner, cfg.repo) && radonReview.checkout == nil)
var radonIssue = IssueConfig(target: .specific,
                             specificIssue: "https://github.com/software-mansion/radon/issues/4")
check(!radonIssue.isValid)
radonIssue.localRepos = [radon]
check(radonIssue.isValid && radonIssue.checkout == "/tmp/radon"
      && radonIssue.buildPrompt().contains("software-mansion/radon"))
var radonConflict = ConflictConfig(target: .specific,
                                   specificPR: "https://github.com/software-mansion/radon/pull/291")
radonConflict.localRepos = [radon]
check(radonConflict.isValid && radonConflict.checkout == "/tmp/radon"
      && radonConflict.buildPrompt().contains("software-mansion/radon"))
// A checkout's GitHub remotes, in every URL form git writes, upstream first.
for url in ["git@github.com:software-mansion/radon.git", "https://github.com/software-mansion/radon",
            "https://github.com/software-mansion/radon.git/", "ssh://git@github.com/software-mansion/radon.git",
            "https://user@github.com/software-mansion/radon.git",
            "ssh://git@github.com:22/software-mansion/radon.git"] {
    check(PRRef.LocalRepo.coordinates(remoteURL: url) ?? ("", "", "")
          == ("github.com", "software-mansion", "radon"), url)
}
check(PRRef.LocalRepo.coordinates(remoteURL: "git@bitbucket.org:software-mansion/radon.git") == nil)
check(PRRef.LocalRepo.coordinates(remoteURL: "https://github.com.evil.io/a/b") == nil)
check(PRRef.LocalRepo.coordinates(remoteURL: "/srv/git/radon.git") == nil)
let remotes = """
remote.mine.url git@github.com:me/thing.git
remote.origin.url git@github.com:me/radon.git
remote.gitlab.url git@gitlab.com:x/y.git
remote.upstream.url https://github.com/software-mansion/radon.git
remote.dup.url https://github.com/Software-Mansion/Radon
"""
check(PRRef.LocalRepo.entries(path: "/r", remotes: remotes).map(\.slug)
      == ["software-mansion/radon", "me/radon", "me/thing", "x/y"]
      && PRRef.LocalRepo.entries(path: "/r", remotes: remotes).map(\.host)
      == ["github.com", "github.com", "github.com", "gitlab.com"])
check(PRRef.LocalRepo.entries(path: "/r", remotes: "").isEmpty)
// A queued config stored before the field existed still decodes.
var stored = try JSONSerialization.jsonObject(with: JSONEncoder().encode(single("337"))) as! [String: Any]
stored.removeValue(forKey: "localRepos")
check((try? JSONDecoder().decode(ReviewConfig.self,
                                 from: JSONSerialization.data(withJSONObject: stored))) != nil)
print("PR-reference assertions passed")

section("items across repos")
check(PRRef.itemKey(owner: "Software-Mansion", repo: "Argent", number: 12) == "software-mansion/argent#12",
      "an item key is lowercased")
for (url, want) in [("https://github.com/Software-Mansion/Argent/pull/12", "software-mansion/argent#12"),
                    ("https://github.com/o/r/issues/7#issuecomment-1", "o/r#7"),
                    ("https://www.github.com/o/r.js/pull/3/files?w=1", "o/r.js#3"),
                    ("http://github.com/o/r/pull/9/", "o/r#9"),
                    ("github.com/o/r/pull/9", "o/r#9")] {
    check(PRRef.itemKey(url: url) == want, "\(url) -> \(String(describing: PRRef.itemKey(url: url)))")
}
for url in ["https://gitlab.com/o/r/pull/1", "https://github.com.evil.io/o/r/pull/1",
            "https://github.com/o/r/pull/12x", "https://github.com/o/r/compare/1",
            "https://github.com/o/r/pull/0", "u1", ""] {
    check(PRRef.itemKey(url: url) == nil, "\(url) names no item")
}
check(PRRef.splitItemKey("o/r.js#12").map { [$0.root, String($0.number)] } == ["o/r.js", "12"],
      "an item key splits into its root key and number")
for bad in ["o/r#x", "or#1", "o//x#1", "o/r/#1", "/r#1", "o/#1", "#1", "o/r#", "o/r#+1", "12"] {
    check(PRRef.splitItemKey(bad) == nil, "\(bad) is not an item key")
}
check(PRRef.migrateItemKey("12", primary: "O/R") == "o/r#12"
      && PRRef.migrateItemKey("o/x#12", primary: "o/r") == "o/x#12"
      && PRRef.migrateItemKey("abc", primary: "o/r") == "abc"
      && PRRef.migrateItemKey("", primary: "o/r") == "",
      "a single-repo build's bare number is the primary root's; anything else is left as is")
check(PRRef.migrateQueueKey("conflicts:12", primary: "o/r") == "conflicts:o/r#12"
      && PRRef.migrateQueueKey("review-req:o/x#12", primary: "o/r") == "review-req:o/x#12"
      && PRRef.migrateQueueKey("investigate:1508284197000015", primary: "o/r")
         == "investigate:1508284197000015"
      && PRRef.migrateQueueKey("nocolon", primary: "o/r") == "nocolon"
      && PRRef.migrateQueueKey("conflicts:", primary: "o/r") == "conflicts:",
      "a legacy queue key moves into the primary root, save an investigation's Slack ts")
check(PRRef.itemLabel(item: "software-mansion/argent#12", roots: 1) == "#12"
      && PRRef.itemLabel(item: "software-mansion/argent#12", roots: 0) == "#12"
      && PRRef.itemLabel(item: "software-mansion/argent#12", roots: 2) == "argent#12",
      "a label names the repo only once there are several roots")

section("repo roots")
do {
    let cfgSlug = (owner: "software-mansion", repo: "argent")
    let fork = """
    remote.origin.url git@github.com:me/argent.git
    remote.upstream.url https://github.com/someone/else.git
    remote.canon.url https://github.com/Software-Mansion/Argent.git
    """
    let radonRemotes = "remote.origin.url git@github.com:software-mansion/radon.git"
    let home = ProcessInfo.processInfo.environment["HOME"] ?? NSHomeDirectory()
    let remotes = ["/w/fork": fork, "/w/radon": radonRemotes, "/w/plain": "",
                   "/w/gitlab": "remote.origin.url git@gitlab.com:x/y.git",
                   "/w/bitbucket": "remote.origin.url git@bitbucket.org:x/y.git",
                   "\(home)/dev/radon2": radonRemotes]
    let resolved = RepoRoots.resolve(paths: ["/w/fork", "/w/missing", "/w/radon", "/w/bitbucket",
                                             "/w/gitlab"],
                                     remotes: remotes, configured: cfgSlug)
    check(resolved == [PRRef.LocalRepo(owner: "software-mansion", repo: "argent", path: "/w/fork"),
                       PRRef.LocalRepo(owner: "software-mansion", repo: "radon", path: "/w/radon"),
                       PRRef.LocalRepo(owner: "x", repo: "y", path: "/w/gitlab", host: "gitlab.com")],
          "a fork clone (origin me/argent) is the configured repo, named as configured, not by "
          + "its sibling remote; a gitlab.com remote resolves to itself; a path that is no "
          + "checkout or has neither a GitHub nor a GitLab remote is skipped: \(resolved)")
    let siblings = ["/radon": "remote.origin.url https://github.com/o/radon\n"
                        + "remote.argent.url https://github.com/\(cfgSlug.owner)/\(cfgSlug.repo)\n"
                        + "remote.fork.url https://github.com/me/argent",
                    "/argent": "remote.origin.url https://github.com/\(cfgSlug.owner)/\(cfgSlug.repo)",
                    "/odd": "remote.gh.url https://github.com/me/\(cfgSlug.repo)"]
    check(RepoRoots.resolve(paths: ["/radon", "/argent"], remotes: siblings, configured: cfgSlug)
          == [PRRef.LocalRepo(owner: "o", repo: "radon", path: "/radon"),
              PRRef.LocalRepo(owner: cfgSlug.owner, repo: cfgSlug.repo, path: "/argent")],
          "a sibling remote naming the configured repo does not take a checkout: its origin does")
    check(RepoRoots.resolve(paths: ["/odd"], remotes: siblings, configured: cfgSlug)
          == [PRRef.LocalRepo(owner: cfgSlug.owner, repo: cfgSlug.repo, path: "/odd")],
          "with no upstream or origin, any remote may name it")
    check(RepoRoots.resolve(paths: ["/argent"], remotes: siblings,
                            configured: (cfgSlug.owner.uppercased(), cfgSlug.repo.uppercased()))
          == [PRRef.LocalRepo(owner: cfgSlug.owner, repo: cfgSlug.repo, path: "/argent")],
          "however the config cases it, the remote naming it is the root")
    check(RepoRoots.resolve(paths: ["/w/radon"], remotes: ["/w/radon": fork + "\n" + radonRemotes],
                            configured: ("x", "y")).map(\.slug) == ["someone/else"],
          "with no configured remote, the first entry (upstream first) names the checkout")
    check(RepoRoots.resolve(paths: ["/w/radon", "~/dev/radon2"], remotes: remotes,
                            configured: cfgSlug).map(\.path) == ["/w/radon"],
          "two paths naming one repo keep the first")
    check(RepoRoots.resolve(paths: ["~/dev/radon2"], remotes: remotes, configured: cfgSlug)
          == [PRRef.LocalRepo(owner: "software-mansion", repo: "radon", path: "\(home)/dev/radon2")],
          "a leading ~ is expanded before the lookup, and the path kept expanded")
    check(RepoRoots.resolve(paths: ["~/nowhere", "/w/plain"], remotes: remotes, configured: cfgSlug)
          == [PRRef.LocalRepo(owner: "software-mansion", repo: "argent", path: "\(home)/nowhere")],
          "nothing resolving falls back to the configured repo at the first path")
    check(RepoRoots.resolve(paths: [], remotes: remotes, configured: cfgSlug).isEmpty,
          "no paths, no roots")
    let forkOnly = ["/w/forkonly": "remote.origin.url git@github.com:someone/ARGENT.git",
                    "/w/moved": "remote.origin.url https://github.com/old-owner/argent\n"
                        + "remote.upstream.url https://github.com/o/radon"]
    check(RepoRoots.resolve(paths: ["/w/forkonly", "/w/moved"], remotes: forkOnly,
                            configured: cfgSlug)
          == [PRRef.LocalRepo(owner: "software-mansion", repo: "argent", path: "/w/forkonly")],
          "a fork-only clone (origin someone/argent) answers for the configured repo, so "
          + "another path naming it is skipped")
    check(RepoRoots.resolve(paths: ["/w/moved"], remotes: forkOnly, configured: cfgSlug)
          == [PRRef.LocalRepo(owner: "software-mansion", repo: "argent", path: "/w/moved")]
          && RepoRoots.resolve(paths: ["/w/forkonly"], remotes: forkOnly,
                               configured: ("x", "y")).map(\.slug) == ["someone/ARGENT"],
          "a pre-transfer URL does too, ahead of an upstream; a remote of another name does not")
    check(RepoRoots.allResolve(paths: ["/w/radon", "~/dev/radon2"], remotes: remotes)
          && !RepoRoots.allResolve(paths: ["/w/radon", "/w/missing"], remotes: remotes)
          && !RepoRoots.allResolve(paths: ["/w/radon", "/w/plain"], remotes: remotes)
          && !RepoRoots.allResolve(paths: ["/w/bitbucket"], remotes: remotes)
          && RepoRoots.allResolve(paths: ["/w/gitlab"], remotes: remotes),
          "every path resolves only while each has a GitHub or GitLab remote")
    let pair = ["/src/r", "/src/other"]
    let both = ["/src/r": "remote.origin.url https://github.com/o/r",
                "/src/other": "remote.origin.url https://github.com/o/other"]
    let otherFirst = ["/src/r": "remote.origin.url https://bitbucket.org/o/r",
                      "/src/other": "remote.origin.url https://github.com/o/other"]
    let gitlabFirst = ["/src/r": "remote.origin.url https://gitlab.com/o/r",
                       "/src/other": "remote.origin.url https://github.com/o/other"]
    check(RepoRoots.primaryResolves(paths: pair, remotes: both)
          && !RepoRoots.guessed(paths: pair, remotes: both),
          "both paths resolving: the first root is the first path's, and not a guess")
    check(!RepoRoots.primaryResolves(paths: pair, remotes: otherFirst)
          && !RepoRoots.guessed(paths: pair, remotes: otherFirst),
          "a first path with only a Bitbucket remote is no primary, the second still resolving")
    check(RepoRoots.primaryResolves(paths: pair, remotes: gitlabFirst)
          && !RepoRoots.guessed(paths: pair, remotes: [
              "/src/r": gitlabFirst["/src/r"]!,
              "/src/other": "remote.origin.url https://gitlab.example.com/g/sub/other"]),
          "a first path with a GitLab remote is the primary, and two GitLab paths no guess")
    check(!RepoRoots.primaryResolves(paths: pair, remotes: [:])
          && RepoRoots.guessed(paths: pair, remotes: [:]),
          "none of several resolving: no primary, and the one root a guess")
    check(RepoRoots.primaryResolves(paths: ["/src/r"], remotes: [:])
          && !RepoRoots.guessed(paths: ["/src/r"], remotes: [:]),
          "a lone unresolved path is the primary it always was, not a guess")
    check(RepoRoots.expand("~") == home && RepoRoots.expand("~user/x") == "~user/x"
          && RepoRoots.expand("/a/b/") == "/a/b/",
          "only a bare ~ or ~/ expands, and nothing else is touched")
}

section("home repo")
do {
    let radonHome = PRRef.LocalRepo(owner: "software-mansion", repo: "radon", path: "/r/radon")
    var sweep = ReviewConfig(depth: "max", me: me)
    sweep.home = radonHome
    check(sweep.targetRepo == ("software-mansion", "radon") && sweep.checkout == "/r/radon",
          "a sweep with a home targets and works in it")
    check(sweep.buildPrompt().contains("software-mansion/radon")
          && !sweep.buildPrompt().contains("\(cfg.owner)/\(cfg.repo)"),
          "…and its prompt names it alone")
    let one = sweep.forPR(12)
    check(one.home == radonHome && one.prRef.number == 12 && one.prRef.repo == nil
          && one.targetRepo == ("software-mansion", "radon") && one.checkout == "/r/radon"
          && one.buildPrompt().contains("PR #12 in software-mansion/radon"),
          "the per-PR config keeps the home, so a bare number is the home's")
    var linked = ReviewConfig(depth: "max", target: .specific, me: me,
                              specificPR: "https://github.com/\(cfg.owner)/\(cfg.repo)/pull/3")
    linked.home = radonHome
    check(linked.prRef.repoMismatch, "a link is checked against the home repo first")
    linked.localRepos = [PRRef.LocalRepo(owner: cfg.owner, repo: cfg.repo, path: "/r/main")]
    check(linked.isValid && linked.checkout == "/r/main"
          && linked.targetRepo == (cfg.owner, cfg.repo),
          "…then the listed ones, whose checkout wins over the home's")
    var issue = IssueConfig(me: me).forIssue(7)
    issue.home = radonHome
    check(issue.issueRef.number == 7 && issue.checkout == "/r/radon"
          && issue.buildPrompt().contains("software-mansion/radon"),
          "an issue fix targets its home")
    var conflict = ConflictConfig(me: me)
    conflict.home = radonHome
    check(conflict.targetRepo == ("software-mansion", "radon") && conflict.checkout == "/r/radon"
          && conflict.buildPrompt().contains("software-mansion/radon"),
          "a conflict sweep targets its home")
    var audit = AuditConfig()
    audit.home = radonHome
    check(audit.buildPrompt().contains("software-mansion/radon")
          && !audit.buildPrompt().contains("\(cfg.owner)/\(cfg.repo)")
          && audit.checkout == "/r/radon",
          "an audit audits its home, in its checkout")
    check(ReviewConfig(depth: "max", me: me).checkout == nil
          && AuditConfig().checkout == nil
          && AuditConfig().targetRepo == (cfg.owner, cfg.repo),
          "no home is the configured repo in the default root")
    let roundTrip = try JSONDecoder().decode(ReviewConfig.self, from: JSONEncoder().encode(one))
    check(roundTrip.home == radonHome, "a queued config keeps its home across a restart")
}

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
// A pid the OS handed on after the node died names a process started after its last write.
func written(_ stamp: String?) -> MeshSnapshot {
    MeshSnapshot.decode(Data((stamp.map { #"{"pid": 1, "updatedAt": "\#($0)"}"# } ?? #"{"pid": 1}"#).utf8))!
}
let wrote = written("2026-11-04T16:06:48.462459+00:00")
check(abs((wrote.writtenAt?.timeIntervalSince1970 ?? 0) - 1_793_808_408.462459) < 0.001,
      "Python's isoformat, microseconds and all")
check(written("2026-11-04T16:06:48+00:00").writtenAt == Date(timeIntervalSince1970: 1_793_808_408),
      "…and without them, as it writes a whole second")
check(!wrote.names(processStarted: wrote.writtenAt!.addingTimeInterval(1)),
      "a process started after the snapshot was written is not the node that wrote it")
check(wrote.names(processStarted: wrote.writtenAt!.addingTimeInterval(-600)))
check(written(nil).names(processStarted: Date()) && written("now").names(processStarted: Date())
        && wrote.names(processStarted: nil),
      "with either time unknown, the pid is all there is")
check(written("2026-11-04T16:06:48+00:00") == written("2026-11-05T00:00:00+00:00"),
      "the write time alone is no change to the snapshot")
check(snap.selfNode?.platform == "macos" && snap.selfNode?.tier == 2, "self node")
// The console fields (strength auto, auto token %, real uptime, trust) decode.
check(snap.selfNode?.strengthAuto == true && snap.selfNode?.tokensPct == 0.81, "self console fields")
// The real weekly quota percentage (OAuth usage probe) decodes on both shapes, and
// an older peer's 5-hour field beside it is ignored.
check(snap.selfNode?.tokensWeekPct == 0.55, "self week quota decode")
check(snap.peers[0].tokensWeekPct == 0.4, "peer week quota decode")
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
// A week-window percentage move IS a meaningful change — the quota indicator
// must repaint when the probe reports a new integer percent.
let snap3 = MeshSnapshot.decode("""
{"pid":4242,"tcpPort":40878,"linking":0,
 "self":{"id":"aaa","name":"here","platform":"macos","tier":2,"tokens":"ok",
         "strengthAuto":true,"tokensAuto":true,"tokensPct":0.81,"uptimeSecs":930.0,
         "tokensSessionPct":0.81,"tokensWeekPct":0.48},
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
check(snap != snap3, "a week-quota percent move is a meaningful change")
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
check(legacySnap.selfNode?.tokensWeekPct == nil && legacySnap.peers[0].tokensWeekPct == nil,
      "pre-probe snapshots ⇒ nil week quota (UI falls back to ≈estimate)")
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
check(!mine.buildPrompt().contains("Anything you post on someone else's PR goes in ONE formal GitHub review"))
check(other.buildPrompt().contains("Anything you post on someone else's PR goes in ONE formal GitHub review"))
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
check(cMine.buildPrompt().contains("For each, merge the latest version of the PR's BASE branch"))
check(cSingle.buildPrompt().hasPrefix("Take PR #337 in \(cfg.owner)/\(cfg.repo)."))
check(cSingle.buildPrompt().contains("Merge the latest version of the PR's BASE branch"))
// A stacked PR conflicts with the branch it targets, not with main: merging main clears
// nothing, so the conflicts duty would re-dispatch it forever and pollute its diff.
for p in [cSingle.buildPrompt(), cMine.buildPrompt()] {
    check(p.contains("--json baseRefName") && p.contains("--no-ff origin/<baseRefName>"),
          "the merge targets the PR's own base")
    check(!p.contains("origin/main"), "no conflict prompt merges main regardless of the base")
}
check(cMine.buildPrompt().contains("No AI attribution"))
// A clean local merge of a PR GitHub still calls CONFLICTING must be pushed: left
// untouched, the PR stays flagged and the conflicts reconciler re-dispatches it forever.
for p in [cSingle.buildPrompt(), cMine.buildPrompt()] {
    guard let flagged = p.range(of: "If GitHub says `CONFLICTING`"),
          let push = p.range(of: "commit that clean merge and push it"),
          let untouched = p.range(of: "Otherwise there's nothing to resolve here — abort/undo the merge and leave that PR untouched") else {
        check(false, "clean-merge-but-GitHub-conflicting branch missing"); continue
    }
    check(flagged.upperBound <= push.lowerBound && push.upperBound <= untouched.lowerBound,
          "only a PR GitHub does not flag is left untouched")
}
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
// OpenCode 2.x: create the staged session, prompt it, attach a TUI. Pinned byte for byte:
// the pid file is only the agent's own for exactly this shape (`;` before the TUI,
// `|| exit` after the prompt). Linux differs only in quoting: it leaves a plain path bare.
let serviceCommand = AgentRunner.opencode.agentCommand(
    promptFile: "/tmp/p.txt", model: "openai/gpt-5-mini", port: 47_910,
    serviceSession: "ses_diplomat_0123456789abcdef0123456789abcdef")
check(serviceCommand
        == "opencode api session.create -d \"$(cat '/tmp/p.txt.session.json')\" "
         + "&& opencode api session.prompt --param sessionID=ses_diplomat_0123456789abcdef0123456789abcdef "
         + "-d \"$(cat '/tmp/p.txt.prompt.json')\" "
         + "|| exit; opencode --session ses_diplomat_0123456789abcdef0123456789abcdef",
      "the 2.x command: \(serviceCommand)")
for flag in ["--port", " -m ", AgentRunner.permissionEnv, "--prompt"] {
    check(!serviceCommand.contains(flag), "2.x takes no \(flag); it belongs to the service")
}
check(AgentRunner.opencode.agentCommand(promptFile: "/tmp/it's.txt", serviceSession: "ses_x")
        .contains("\"$(cat '/tmp/it'\\''s.txt.session.json')\""),
      "the staged files are quoted the way the prompt file is")
check(AgentRunner.opencode.agentCommand(promptFile: "/tmp/p.txt", serviceSession: "")
        == AgentRunner.opencode.agentCommand(promptFile: "/tmp/p.txt"),
      "no service session is a 1.x spawn, unchanged")
check(AgentRunner.claude.agentCommand(promptFile: "/tmp/p.txt", serviceSession: "ses_x")
        == "claude \"$(cat '/tmp/p.txt')\"",
      "only OpenCode has a service to create a session on")
check(AgentRunner.hermes.agentCommand(promptFile: "/tmp/p.txt", serviceSession: "ses_x")
        == "hermes chat --tui --yolo -q \"$(cat '/tmp/p.txt')\"")
// `auth` is 2.x's name for the provider wizard and an alias of 1.x's `providers`.
check(AgentRunner.opencode.setupCommand == "opencode auth login; opencode auth list",
      "one provider-wizard spelling serves both OpenCode majors")
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
func sessionModel() -> String {
    AgentModel.openCodeSessionModel(configDir: openCodeConfig, stateDir: openCodeState)
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
// `detected()` remembers what Claude Code last ran, since every queued task's prompt is
// rebuilt on every poll and the walk costs seconds on a busy machine; the poll re-reads
// it off the main thread with `detectedAfresh()`.
do {
    let memoEnv = ["DIPLOMAT_CONFIG": configFile.path, "DIPLOMAT_CLAUDE_DIR": claudeHome.path]
    let priorMemoEnv = memoEnv.keys.map { ($0, ProcessInfo.processInfo.environment[$0]) }
    for (key, value) in memoEnv { setenv(key, value, 1) }
    defer {
        for (key, value) in priorMemoEnv {
            if let value { setenv(key, value, 1) } else { unsetenv(key) }
        }
        try? FileManager.default.removeItem(at: sessions.appendingPathComponent("memo.jsonl"))
    }
    check(AgentModel.detectedAfresh() == "Opus 5", "detectedAfresh reads the transcripts")
    try writeTranscript("memo.jsonl", "{\"message\":{\"model\":\"claude-sonnet-5\"}}\n", ageSecs: 0)
    check(AgentModel.detected() == "Opus 5",
          "detected() answers from what was last read, without walking the transcripts again")
    check(AgentModel.detectedAfresh() == "Sonnet 5", "detectedAfresh reads them again")
    check(AgentModel.detected() == "Sonnet 5", "and detected() answers with what that read")
}
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
// The 2.x service never reads them, so an unpinned 2.x spawn names that head itself, and
// the tag names the model the run is on whichever major runs it.
check(sessionModel() == "anthropic/claude-opus-5",
      "an unpinned 2.x session starts on the picker's last model, not the service's first")
try writeOpenCodeState("""
{"recent": [{"providerID": "anthropic", "modelID": "claude-opus-5"}],
 "variant": {"anthropic/claude-opus-5": "high", "openai/gpt-5.2": "low"}}
""")
check(sessionModel() == "anthropic/claude-opus-5#high",
      "with the variant the picker last chose for that model")
check(detectModel() == "Opus 5", "a variant qualifies the model the tag names")
try writeOpenCodeState("""
{"recent": [{"providerID": "anthropic", "modelID": "claude-opus-5"}],
 "variant": {"anthropic/claude-opus-5": "default"}}
""")
check(sessionModel() == "anthropic/claude-opus-5", "the picker's `default` variant is no variant")
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
check(sessionModel() == "",
      "a config model is the service's own to apply, so a 2.x session names none")
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
// A 2.x pin may carry a `#variant`.
try writeConfig("{\"agentRunner\": \"opencode\", \"agentModel\": \"openai/gpt-5.2#high\"}")
check(detectModel() == "GPT 5.2", "a pinned variant is still the model it qualifies")
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
// A run's own server answers out of a session store it shares with every other agent in
// this checkout and its worktrees. These are what get from that store to the one session
// a run owns — and the same cases `tests/test_opencode_api.py` pins.
// Half the narrowing is the server's: the directory keeps a busier checkout next door
// out of the answer, the limit lifts the hundred rows the route answers with by default.
check(OpenCodeAPI.sessionPath(directory: "/repo") == "/session?directory=/repo&limit=1000")
check(OpenCodeAPI.sessionPath(directory: "/tmp/a b+c")
        == "/session?directory=/tmp/a%20b%2Bc&limit=1000",
      "a checkout path is whatever the disk says, and a query cannot carry all of it")
check(OpenCodeAPI.sessionPath(directory: "") == nil,
      "an empty ?directory= is not a filter matching nothing, it is no filter at all")
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
// 2.x's `session export` carries each message's tokens at its top level, not under `info`.
check(OpenCodeAPI.sessionTokens([
    ["type": "user", "text": "hi"],
    ["type": "assistant", "tokens": ["input": 3.0, "output": 84.0, "reasoning": 9.0,
                                     "cache": ["read": 29_000.0, "write": 40.0]]],
    ["type": "idle", "outcome": "succeeded"],
]) == 127, "a 2.x export is summed by the same rule")
print("opencode session assertions passed")

section("opencode 2.x service")
// Anything unreadable is 1.x. Real `--version` outputs, 2.0.18 then 1.x.
check(OpenCodeAPI.isServiceVersion("opencode v2.0.18\n"))
check(OpenCodeAPI.isServiceVersion("opencode v10.1.0"), "a major is a number, not a digit")
check(!OpenCodeAPI.isServiceVersion("1.18.33\n"))
check(!OpenCodeAPI.isServiceVersion("1.4.3"))
for garbage in ["", "opencode", "v2.0", "command not found: opencode", "é2"] {
    check(!OpenCodeAPI.isServiceVersion(garbage), "“\(garbage)” names no version, so it is 1.x")
}
check(OpenCodeAPI.exportArguments(sessionID: "ses_a", service: true) == ["session", "export", "ses_a"])
check(OpenCodeAPI.exportArguments(sessionID: "ses_a", service: false) == ["export", "ses_a"],
      "1.x has no `session export`, 2.x no top-level `export`")
// A reused id makes `session.create` answer with somebody else's existing session.
let minted = OpenCodeAPI.newSessionID()
check(minted.hasPrefix("ses_diplomat_") && minted.count == 45
        && minted.dropFirst(13).allSatisfy { "0123456789abcdef".contains($0) },
      "ses_diplomat_ and 32 lowercase hex digits, not \(minted)")
check(minted != OpenCodeAPI.newSessionID())
// A pin splits at the FIRST `/`, and a `#variant` off the LAST `#` of the rest.
check(OpenCodeAPI.modelRef("") == nil && OpenCodeAPI.modelRef("  ") == nil,
      "no pin is no model object: the service picks its configured one")
check(OpenCodeAPI.modelRef("openai/gpt-5-mini") == ["providerID": "openai", "id": "gpt-5-mini"])
check(OpenCodeAPI.modelRef("openrouter/moonshotai/kimi-k3")
        == ["providerID": "openrouter", "id": "moonshotai/kimi-k3"],
      "an OpenRouter id keeps its own path")
check(OpenCodeAPI.modelRef("openai/gpt-5#high")
        == ["providerID": "openai", "id": "gpt-5", "variant": "high"])
check(OpenCodeAPI.modelRef("openrouter/a/b#x#y")
        == ["providerID": "openrouter", "id": "a/b#x", "variant": "y"])
check(OpenCodeAPI.modelRef("gpt-5") == ["providerID": "gpt-5", "id": ""],
      "a pin with no provider is all provider, which the service then fails in plain view")
let unpinned = OpenCodeAPI.sessionBody(id: "ses_x", directory: "/repo", model: "")
check(unpinned["model"] == nil, "an unpinned session sends no model")
check(unpinned["id"] as? String == "ses_x"
        && (unpinned["location"] as? [String: String]) == ["directory": "/repo"]
        && (unpinned["permissions"] as? [[String: String]])
            == [["action": "*", "resource": "*", "effect": "allow"]],
      "every spawned session is created with the allow-all ruleset")
check((OpenCodeAPI.sessionBody(id: "ses_x", directory: "/repo", model: "openai/gpt-5#high")["model"]
        as? [String: String]) == ["providerID": "openai", "id": "gpt-5", "variant": "high"])
// What the spawn stages, read back as `opencode api` will read it.
do {
    let dir = FileManager.default.temporaryDirectory
        .appendingPathComponent("diplomat-smoke-stage-\(UUID().uuidString)", isDirectory: true)
    let repo = dir.appendingPathComponent("repo", isDirectory: true)
    let link = dir.appendingPathComponent("link")
    try FileManager.default.createDirectory(at: repo, withIntermediateDirectories: true)
    try FileManager.default.createSymbolicLink(at: link, withDestinationURL: repo)
    defer { try? FileManager.default.removeItem(at: dir) }
    let promptFile = dir.appendingPathComponent("prompt.txt")
    let text = "Review PR #7 in o/r\n  it's \"quoted\" — and $(not run)\n"
    try text.write(to: promptFile, atomically: true, encoding: .utf8)
    guard let sid = OpenCodeCLI.stageSession(promptFile: promptFile, directory: link.path,
                                             model: "openrouter/a/b#max") else {
        check(false, "a writable run directory stages a session")
        exit(1)
    }
    check(sid.hasPrefix("ses_diplomat_"))
    func staged(_ suffix: String) -> [String: Any] {
        let data = (try? Data(contentsOf: URL(fileURLWithPath: promptFile.path + suffix))) ?? Data()
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
    }
    let session = staged(".session.json")
    let physical = URL(fileURLWithPath: repo.path).resolvingSymlinksInPath().path
    check(session["id"] as? String == sid, "the staged session is the one the id names")
    check(((session["location"] as? [String: Any])?["directory"] as? String)
            .map { $0.hasSuffix(physical) && !$0.hasSuffix("/link") } == true,
          "the directory is the checkout's physical path, not the link the agent was cd'd through")
    check((session["model"] as? [String: String])
            == ["providerID": "openrouter", "id": "a/b", "variant": "max"])
    check(staged(".prompt.json")["text"] as? String == text, "the prompt is staged verbatim")
    for suffix in [".session.json", ".prompt.json"] {
        let mode = (try? FileManager.default.attributesOfItem(atPath: promptFile.path + suffix))?[
            .posixPermissions] as? NSNumber
        check(mode?.intValue == 0o600, "\(suffix) carries the prompt, so it is owner-only")
    }
    check(OpenCodeCLI.stageSession(promptFile: dir.appendingPathComponent("absent.txt"),
                                   directory: repo.path, model: "") == nil,
          "a prompt that cannot be read stages nothing to spawn on")
}
// Finding the service: the file every 2.x client writes, under XDG_STATE_HOME when set.
let home = URL(fileURLWithPath: "/Users/me")
check(OpenCodeAPI.serviceFile(environment: ["XDG_STATE_HOME": "/x/state"], home: home).path
        == "/x/state/opencode/service.json")
check(OpenCodeAPI.serviceFile(environment: ["XDG_STATE_HOME": ""], home: home).path
        == "/Users/me/.local/state/opencode/service.json",
      "an empty XDG variable is unset, as the spec has it")
let endpoint = OpenCodeAPI.serviceEndpoint(Data(
    #"{"id":"u","version":"2.0.18","url":"http://127.0.0.1:49374/","pid":1,"password":"pw"}"#.utf8))
check(endpoint?.base == "http://127.0.0.1:49374"
        && endpoint?.authorization == "Basic b3BlbmNvZGU6cHc=",
      "HTTP Basic as opencode:<password>, against the URL with no trailing slash")
check(OpenCodeAPI.serviceEndpoint(Data(#"{"url":"http://127.0.0.1:1"}"#.utf8))?.authorization == nil,
      "a service that wrote no password is asked with none")
for bad in ["", "{}", #"{"url":""}"#, #"{"url":7}"#, "[]", "<html>"] {
    check(OpenCodeAPI.serviceEndpoint(Data(bad.utf8)) == nil, "“\(bad)” names no service")
}
check(OpenCodeAPI.servicePath(sessionID: "ses_a") == "/api/session/ses_a")
check(OpenCodeAPI.servicePath(sessionID: "ses_a", suffix: "/interrupt") == "/api/session/ses_a/interrupt")
check(OpenCodeAPI.servicePath(sessionID: "a/b?c") == "/api/session/a%2Fb%3Fc",
      "an id is one path segment, whatever a hand-edited session file says")
check(OpenCodeAPI.servicePath(sessionID: "") == nil)
// Busy/idle for a 2.x session: in the active map is busy, out of it with `time.idle`
// stamped is idle, and out of it unstamped is a first turn not yet ended — busy.
func info(_ time: [String: Any]) -> [String: Any] { ["data": ["id": "ses_a", "time": time]] }
let nobody: [String: Any] = ["data": [String: Any]()]
let runningA: [String: Any] = ["data": ["ses_a": ["type": "running"]]]
check(OpenCodeAPI.serviceState(session: info(["created": 1.0]), active: runningA,
                               sessionID: "ses_a")?.busy == true)
check(OpenCodeAPI.serviceState(session: info(["created": 1.0, "idle": 2.0]), active: nobody,
                               sessionID: "ses_a")?.busy == false)
check(OpenCodeAPI.serviceState(session: info(["created": 1.0]), active: nobody,
                               sessionID: "ses_a")?.busy == true,
      "a session whose first turn has not ended is not idle")
check(OpenCodeAPI.serviceState(session: info(["created": 1.0, "idle": 2.0]), active: runningA,
                               sessionID: "ses_a")?.busy == true,
      "an earlier turn's idle stamp does not end the one now running")
check(OpenCodeAPI.serviceState(session: info(["created": 1.0, "idle": 2.0]),
                               active: ["data": ["ses_b": ["type": "running"]]],
                               sessionID: "ses_a")?.busy == false,
      "another session's turn says nothing about this one")
check(OpenCodeAPI.serviceState(session: info(["idle": "soon"]), active: nobody,
                               sessionID: "ses_a")?.busy == true,
      "a stamp that is not a time is not a stamp")
// How the stamped turn ended: shapes as 2.2.5 answers them, against a refusing provider.
func ended(_ outcome: String?) -> [String: Any] {
    var data: [String: Any] = ["id": "ses_a", "time": ["created": 1.0, "busy": 1.5, "idle": 2.0]]
    data["outcome"] = outcome
    return ["data": data]
}
check(OpenCodeAPI.serviceState(session: ended("failed"), active: nobody, sessionID: "ses_a")
        == AgentState.SessionState(busy: false, failure: ""),
      "a turn its runner says failed is a failure")
for other in ["succeeded", "interrupted", nil] {
    check(OpenCodeAPI.serviceState(session: ended(other), active: nobody, sessionID: "ses_a")
            == AgentState.SessionState(busy: false),
          "an outcome of \(other ?? "(none)") is no failure")
}
check(OpenCodeAPI.serviceState(session: ended("failed"), active: runningA, sessionID: "ses_a")
        == AgentState.SessionState(busy: true),
      "a turn running now is not the earlier one that failed")
check(OpenCodeAPI.serviceLastMessagePath(sessionID: "ses_a")
        == "/api/session/ses_a/message?limit=1&order=desc")
check(OpenCodeAPI.failureMessage(["data": [["type": "idle", "outcome": "failed",
                                             "error": ["type": "provider.quota",
                                                       "message": "Insufficient account funds",
                                                       "status": 402]]]])
        == "Insufficient account funds")
for said in [nil, ["data": []], ["data": [["type": "assistant", "error": ["message": "x"]]]],
             ["data": [["type": "idle", "outcome": "failed"]]]] as [Any?] {
    check(OpenCodeAPI.failureMessage(said) == "", "\(String(describing: said)) says nothing went wrong")
}
let serviceGarbage: [Any?] = [nil, [String: Any](), ["data": "x"], "<html>", [["data": [:]]],
                         ["_tag": "SessionNotFoundError"]]
for bad in serviceGarbage {
    check(OpenCodeAPI.serviceState(session: bad, active: nobody, sessionID: "ses_a") == nil,
          "a session answer of \(String(describing: bad)) is unavailable, not idle")
    check(OpenCodeAPI.serviceState(session: info(["idle": 2.0]), active: bad,
                                   sessionID: "ses_a") == nil,
          "an active answer of \(String(describing: bad)) is unavailable, not idle")
}
// A 2.x agent with no pid file: its command line names only its session, so the scan
// matches on that session's opening message.
check(OpenCodeAPI.serviceOpeningPath(sessionID: "ses_a")
        == "/api/session/ses_a/message?limit=1&order=asc",
      "the opening message alone, oldest first")
check(OpenCodeAPI.serviceOpeningPath(sessionID: "") == nil)
check(OpenCodeAPI.openingText(["data": [["type": "user", "text": "Review PR #7 in o/r"]]])
        == "Review PR #7 in o/r")
check(OpenCodeAPI.openingText(["data": [[String: Any]]()]) == nil,
      "a session created and not yet prompted has no opening text")
for bad in [nil, "<html>", ["data": [["type": "assistant", "text": "x"]]],
            ["data": [["type": "user"]]], ["data": "x"]] as [Any?] {
    check(OpenCodeAPI.openingText(bad) == nil, "\(String(describing: bad)) opens with no prompt")
}
check(OpenCodeAPI.attachedSession("opencode --session ses_diplomat_0123abcd")
        == "ses_diplomat_0123abcd")
check(OpenCodeAPI.attachedSession("/home/u/.npm/bin/opencode --session ses_x") == "ses_x")
check(OpenCodeAPI.attachedSession("/opt/oc/opencode.exe --session\tses_Ab-9_z") == "ses_Ab-9_z")
// Every wrapper of a 2.x TUI carries its words too; the tmux lines are what a real spawn
// put in `ps` (2.0.18), and none is the TUI.
let wrapped = "ses_diplomat_49b172f752bd4c2f86a4ec0c5f050dc1"
for line in [
    "script -q /dev/null tmux -L x new-session -s y zsh -i -c 'cd /tmp; zsh -i -c \"sleep 9; opencode --session \(wrapped)\"; exec sh'",
    "tmux -L x new-session -s y zsh -i -c 'cd /tmp; zsh -i -c \"sleep 9; opencode --session \(wrapped)\"; exec sh'",
    "zsh -i -c cd /tmp; zsh -i -c \"sleep 9; opencode --session \(wrapped)\"; exec sh",
    "zsh -i -c sleep 9; opencode --session \(wrapped)",
    "xterm -e bash -c 'cd /tmp; opencode --session \(wrapped)'",
    "opencode --session \(wrapped) --prompt x", "notopencode --session \(wrapped)",
    "opencode --prompt \"$(cat '/p')\"", "opencode --port 1", "opencode --session x_1",
    "opencode --session ses", "opencode --session ses_a'", "claude --resume ses_a",
] {
    check(OpenCodeAPI.attachedSession(line) == nil, "“\(line)” attaches to no 2.x session")
}

extension OpenCodeCLI.Resolution { var fields: [String?] { [path, stateHome] } }
// The resolver: one login shell, an interactive one inside it, and a probe that prints
// the path and then the shell's state root on a marked line.
check(OpenCodeCLI.resolverArguments(shell: "/bin/zsh")
        == ["-l", "-c", "'/bin/zsh' -i -c 'command -v opencode; printf '\\''\\n@@XDG_STATE_HOME=%s\\n'\\'' \"$XDG_STATE_HOME\"'"],
      "the resolver's shape: \(OpenCodeCLI.resolverArguments(shell: "/bin/zsh"))")
let executables: Set<String> = ["/opt/a/opencode", "/opt/b/opencode"]
let found = OpenCodeCLI.parseResolution(
    "welcome back!\n/opt/a/opencode\n\n@@XDG_STATE_HOME=/x/state\n/opt/b/opencode\n") {
    executables.contains($0)
}
check(found.fields == ["/opt/a/opencode", "/x/state"],
      "the path is the last executable line above the marker, whatever an rc prints: \(found)")
check(OpenCodeCLI.parseResolution("/opt/b/opencode\n/opt/a/opencode  \n@@XDG_STATE_HOME=\n") {
        executables.contains($0)
      }.fields == ["/opt/a/opencode", ""],
      "an unset XDG_STATE_HOME is \"\", which is the default root")
check(OpenCodeCLI.parseResolution("opencode: aliased to oc\n@@XDG_STATE_HOME=/s\n") {
        executables.contains($0)
      }.fields == [nil, "/s"],
      "an alias names no executable")
check(OpenCodeCLI.parseResolution("/opt/a/opencode\n") { executables.contains($0) }.fields
        == ["/opt/a/opencode", nil],
      "a shell that never printed the marker said nothing about its state root")
check(OpenCodeCLI.parseResolution("@@XDG_STATE_HOME=/rc\n/opt/a/opencode\n@@XDG_STATE_HOME=/s\n") {
        executables.contains($0)
      }.fields == ["/opt/a/opencode", "/s"],
      "the probe's own marker is the last one")
check(OpenCodeCLI.parseResolution("\u{1B}]1337;RemoteHost=u@h\u{07}\u{1B}]1337;CurrentDir=/tmp\u{1B}\\"
        + "\u{1B}[?2004h/opt/a/opencode\n\u{1B}]133;D\u{07}@@XDG_STATE_HOME=/s\n") {
        executables.contains($0)
      }.fields == ["/opt/a/opencode", "/s"],
      "a shell integration's escapes, on the path's own line, are not part of the path")

// …against real shells, over a throwaway HOME.
do {
    let fx = FileManager.default.temporaryDirectory
        .appendingPathComponent("diplomat-smoke-resolve-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: fx) }
    func executable(_ path: String, _ body: String) {
        let url = fx.appendingPathComponent(path)
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                 withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: url.path, contents: Data(body.utf8),
                                       attributes: [.posixPermissions: 0o755])
    }
    let stub = "#!/bin/sh\necho 'opencode v2.0.18'\n"
    executable("nvm/opencode", stub)
    // nvm's shape: `~/.bashrc` alone, which a login bash skips and an interactive one reads.
    executable(".bashrc", "echo 'welcome back!'\nexport PATH=\(fx.path)/nvm:$PATH\n"
                          + "export XDG_STATE_HOME=\(fx.path)/state\n")
    let bash = OpenCodeCLI.resolve(shell: "/bin/bash",
                                   environment: ["HOME": fx.path, "PATH": "/usr/bin:/bin"])
    check(bash.fields == ["\(fx.path)/nvm/opencode", "\(fx.path)/state"],
          "an install only ~/.bashrc puts on PATH resolves, with that rc's state root: \(bash)")
    // Homebrew's shape on Apple silicon: `~/.zprofile` alone, which only a login zsh reads.
    if FileManager.default.isExecutableFile(atPath: "/bin/zsh") {
        executable("brew/opencode", stub)
        executable("zdot/.zprofile", "export PATH=\(fx.path)/brew:$PATH\n")
        let zsh = OpenCodeCLI.resolve(shell: "/bin/zsh",
                                      environment: ["HOME": fx.path, "ZDOTDIR": "\(fx.path)/zdot",
                                                    "PATH": "/usr/bin:/bin"])
        check(zsh.fields == ["\(fx.path)/brew/opencode", ""],
              "an install only ~/.zprofile puts on PATH resolves: \(zsh)")
    }

    // The process-wide resolution, through a shell whose PATH head a check can move.
    let pick = fx.appendingPathComponent("pick")
    func install(_ dir: String) {
        executable("\(dir)/opencode", stub)
        try? "\(fx.path)/\(dir)".write(to: pick, atomically: true, encoding: .utf8)
    }
    executable("rcshell", "#!/bin/sh\nexport PATH=\"$(cat '\(pick.path)'):$PATH\"\n"
                          + "export XDG_STATE_HOME=\(fx.path)/shellstate\nexec /bin/sh \"$@\"\n")
    let priorShell = ProcessInfo.processInfo.environment["SHELL"]
    setenv("SHELL", fx.appendingPathComponent("rcshell").path, 1)
    defer { if let priorShell { setenv("SHELL", priorShell, 1) } else { unsetenv("SHELL") } }
    let t0 = Date().addingTimeInterval(100_000)
    install("a")
    check(OpenCodeCLI.binary(now: t0) == "\(fx.path)/a/opencode", "the shell's install is found")
    check(OpenCodeCLI.serviceFile(now: t0).path == "\(fx.path)/shellstate/opencode/service.json",
          "the service file is under the shell's state root, not this process's")
    install("b")
    check(OpenCodeCLI.binary(now: t0.addingTimeInterval(59)) == "\(fx.path)/a/opencode",
          "a resolution is trusted for a minute")
    check(OpenCodeCLI.binary(now: t0.addingTimeInterval(61)) == "\(fx.path)/b/opencode",
          "…and re-asked after it, so an install the rc now puts first is found")
    try? FileManager.default.removeItem(at: fx.appendingPathComponent("b"))
    install("c")
    check(OpenCodeCLI.binary(now: t0.addingTimeInterval(62)) == "\(fx.path)/c/opencode",
          "a binary that moved is re-resolved inside the minute")

    let counter = fx.appendingPathComponent("asked")
    func versioned(_ version: String) {
        executable("v/opencode", "#!/bin/sh\necho x >> '\(counter.path)'\necho '\(version)'\n")
    }
    func asked() -> Int {
        ((try? String(contentsOf: counter, encoding: .utf8)) ?? "").split(separator: "\n").count
    }
    let versionedPath = fx.appendingPathComponent("v/opencode").path
    versioned("opencode v2.0.18")
    check(OpenCodeCLI.isService(binary: versionedPath) && OpenCodeCLI.isService(binary: versionedPath)
            && asked() == 1,
          "a binary that has not changed is not run again")
    versioned("1.4.3")
    check(!OpenCodeCLI.isService(binary: versionedPath) && asked() == 2,
          "a binary rewritten in place is asked again")
}
print("opencode 2.x service assertions passed")

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
// A Fix-issues sweep is expanded into one queued run per issue, so EVERY prompt this
// builds works one issue: the scope decides which issues get queued and never reaches
// the agent. The goldens are regenerated from issues.json, so they re-bless whatever
// it says. These are what actually pin the meaning: the six scopes as an enumeration,
// the swept-vs-named split, and every block a toggle turns on — each with the negative
// that proves the block is gated rather than always present.
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

// The scope is enumerated HERE, against the open issues the panel already holds —
// which is what lets one SPAWN become one agent per issue. `authorAssociation` comes
// off the same fetch, so the community/org cut is a filter like the rest rather than
// an enumeration the agent is talked through.
let sweepIssues: [OpenIssue] = [
    OpenIssue(number: 301, title: "outsider", url: "i/301", author: "ext",
              authorAssociation: "NONE", createdAt: now, updatedAt: now, commentCount: 0,
              assignees: [], labels: [], memberResponded: false),
    OpenIssue(number: 302, title: "mine", url: "i/302", author: goldenMeSeed,
              authorAssociation: "OWNER", createdAt: now, updatedAt: now, commentCount: 0,
              assignees: [], labels: [], memberResponded: false),
    OpenIssue(number: 303, title: "claimed", url: "i/303", author: "someuser",
              authorAssociation: "MEMBER", createdAt: now, updatedAt: now, commentCount: 0,
              assignees: ["dev"], labels: [], memberResponded: false),
]
func swept(_ target: IssueTarget, author: String = "", unassignedOnly: Bool = true) -> [Int] {
    Filters.sweptIssues(sweepIssues, target: target, author: author,
                        unassignedOnly: unassignedOnly).map(\.number)
}
check(swept(.all) == [301, 302], "the whole open list, minus what somebody holds")
check(swept(.all, unassignedOnly: false) == [301, 302, 303],
      "…and with the filter off, the claimed one is swept too")
check(swept(.mine, author: goldenMeSeed) == [302] && swept(.someone, author: "SOMEUSER",
                                                           unassignedOnly: false) == [303],
      "a scope that names a person matches its author, case-insensitively")
check(swept(.mine) == [], "…and sweeps nobody at all before that login resolves")
check(swept(.contributors) == [301] && swept(.members, unassignedOnly: false) == [302, 303],
      "the association scopes cut on filters.json's orgAssociations")
check(swept(.specific) == [], "one named issue is dispatched, never swept")

// The new-community-issue monitor offers each issue filed after its mark once, and
// only one nobody inside the org has answered or claimed.
func filed(_ n: Int, _ assoc: String, at minutes: Double, assignees: [String] = [],
           memberResponded: Bool = false) -> OpenIssue {
    let at = now.addingTimeInterval(minutes * 60)
    return OpenIssue(number: n, title: "#\(n)", url: "i/\(n)", author: "u\(n)",
                     authorAssociation: assoc, createdAt: at, updatedAt: at, commentCount: 0,
                     assignees: assignees, labels: [], memberResponded: memberResponded)
}
let filedIssues = [filed(405, "NONE", at: 9), filed(401, "NONE", at: -5),
                   filed(402, "CONTRIBUTOR", at: 2), filed(403, "MEMBER", at: 4),
                   filed(404, "NONE", at: 6, assignees: ["dev"]),
                   filed(406, "COLLABORATOR", at: 7, memberResponded: true),
                   filed(407, "COLLABORATOR", at: 8)]
let firstScan = Filters.newCommunityIssues(filedIssues, since: nil, now: now)
check(firstScan.fresh.isEmpty && firstScan.since == now.addingTimeInterval(9 * 60),
      "the first scan queues none of the backlog, and marks the newest issue it saw")
check(Filters.newCommunityIssues([], since: nil, now: now).since == now,
      "…falling back to the local clock when there is no issue to read one from")
let nextScan = Filters.newCommunityIssues(filedIssues, since: now, now: now)
check(nextScan.fresh.map(\.number) == [402, 407, 405],
      "after the mark: outsiders' issues nobody answered or claimed, oldest first")
check(nextScan.since == now.addingTimeInterval(9 * 60)
      && Filters.newCommunityIssues(filedIssues, since: nextScan.since, now: now).fresh.isEmpty,
      "…and the mark moves past them, so the next scan offers none of them again")
let lateMark = now.addingTimeInterval(60 * 60)
check(Filters.newCommunityIssues(filedIssues, since: lateMark, now: now).since == lateMark,
      "the mark never moves backwards")
check(IssueConfig(target: .mine, me: goldenMeSeed).sweepAuthor == goldenMeSeed
      && IssueConfig(target: .someone, username: "someuser").sweepAuthor == "someuser"
      && IssueConfig(target: .all, me: goldenMeSeed).sweepAuthor.isEmpty,
      "the sweep's author is the real login, never the prompt's \"me\" fallback")
check(IssueConfig(target: .mine, me: "").sweepAuthor.isEmpty
      && IssueConfig(target: .mine, me: "").authorHandle == "me",
      "…which is the whole difference: an unresolved login sweeps nobody")

// `forIssue` is the config behind one queued fix: the operator's depth and toggles,
// narrowed to that issue, and the SCOPE KEPT — it is what still says this issue was
// swept rather than named, and so has to be re-checked before it is worked.
let sweptIssue = IssueConfig(depth: "quick", me: goldenMeSeed).forIssue(302)
check(sweptIssue.specificIssue == "302" && sweptIssue.target == .all
      && sweptIssue.depth == "quick",
      "a swept issue keeps the sweep's depth and its scope")
let sweptPrompt = sweptIssue.buildPrompt()
check(sweptPrompt.hasPrefix("Take issue #302 in \(cfg.owner)/\(cfg.repo),"),
      "a queued fix is the one-issue prompt, not the sweep's")

// What a swept issue carries that a hand-named one does not: its turn can come hours
// later, so the state the sweep selected on is re-read before anything is touched.
check(sweptPrompt.contains("CAME OUT OF A SWEEP") && sweptPrompt.contains("closed since"),
      "a swept issue re-checks that nobody has closed it in the meantime")
check(sweptPrompt.contains("UNASSIGNED WHEN THE SWEEP PICKED IT UP"),
      "…and that nobody has claimed it")
let iAssignedToo = IssueConfig(me: goldenMeSeed, unassignedOnly: false).forIssue(302).buildPrompt()
check(!iAssignedToo.contains("UNASSIGNED"),
      "unassigned-only off leaves no trace of the filter")
let iSingle = IssueConfig(target: .specific, specificIssue: "421").buildPrompt()
check(iSingle.hasPrefix("Take issue #421 in"), "a hand-named issue names itself")
check(!iSingle.contains("CAME OUT OF A SWEEP") && !iSingle.contains("UNASSIGNED"),
      "…and re-checks nothing: it was named just now, by hand")
// …and the wizard does not offer the tick there either — the prompt above is the same
// whichever way it is left, so a tick that changed nothing would be the only lie.
check(IssueConfig().canFilterUnassigned && !IssueConfig(target: .specific).canFilterUnassigned,
      "the unassigned tick is offered for a sweep, not for one named issue")

// The claim: assigning the issue to me is what stops a second agent taking it, so it
// has to name the command and happen BEFORE the work.
check(sweptPrompt.contains("--add-assignee @me") && sweptPrompt.contains("--remove-assignee @me"),
      "the claim is taken and handed back")
check(!IssueConfig(me: goldenMeSeed, assignToMe: false).forIssue(302).buildPrompt()
        .contains("add-assignee"), "no claim when the toggle is off")

// Bugs only unless the escalation is ticked — and the escalation replaces it rather
// than stacking, so the run is never told both to skip and to take feature requests.
check(sweptPrompt.contains("ONLY A REAL BUG REPORT"), "bugs only by default")
let iFeatures = IssueConfig(me: goldenMeSeed, includeFeatures: true).forIssue(302).buildPrompt()
check(iFeatures.contains("FEATURE REQUEST IS IN SCOPE TOO"), "the escalation opens them up")
check(!iFeatures.contains("ONLY A REAL BUG REPORT"), "and withdraws the skip rule")

// The bar every depth is held to, and the depth ladder itself. `quick` is the one
// level that runs nothing, so it must NOT claim a reproduction.
check(sweptPrompt.contains("DONE MEANS OBSERVABLE PROOF"), "the bar is always present")
check(sweptPrompt.contains("CANNOT REPRODUCE"), "an unreproducible issue is a reported result")
check(sweptPrompt.contains("QUICK PASS"), "the quick level is a read-only pass")
check(!sweptPrompt.contains("CONCRETE REPRODUCTION"), "the quick level promises no reproduction")
let iDefault = IssueConfig(me: goldenMeSeed).forIssue(302).buildPrompt()
check(iDefault.contains("CONCRETE REPRODUCTION"), "the default depth reproduces")
check(IssueConfig(depth: "max", me: goldenMeSeed).forIssue(302).buildPrompt()
        .contains("DRIVE THE BUG THROUGH THE REAL ENTRY POINT"), "the max level runs the app")

// Delivery: a draft PR that closes the issue, or nothing on the remote at all. The
// closing reference names THIS issue — a run that works one issue can spell it out.
check(iDefault.contains("DRAFT") && iDefault.contains("Fixes #302"),
      "a draft PR closes the issue it was queued for")
check(iDefault.contains("REGRESSION TEST WITH THE FIX"), "the fix ships a test that pins it")
check(iDefault.contains("DUPLICATE") && iDefault.contains("gh pr diff"), "no duplicate PRs")
let iHandsOff = IssueConfig(me: goldenMeSeed, openPRs: false,
                            commentOnIssue: false).forIssue(302).buildPrompt()
check(iHandsOff.contains("opens NO pull requests"), "PRs off ⇒ nothing reaches the remote")
check(!iHandsOff.contains("gh pr create"), "and no PR is opened")
check(!iHandsOff.contains("No AI attribution"),
      "commit-authoring guidance only where we might commit")
// The attribution tag rides on posting something — a run that posts nothing wears none.
check(iDefault.contains("Made by Diplomat"), "a posting run carries the attribution tag")
check(!iHandsOff.contains("Made by Diplomat"), "a silent run carries none")
check(IssueConfig(me: goldenMeSeed, openPRs: false).forIssue(302).buildPrompt()
        .contains("Made by Diplomat"), "the issue comment alone still earns the tag")
check(iDefault.contains("gh issue comment 302"), "the outcome is reported on the issue")
check(!IssueConfig(me: goldenMeSeed, commentOnIssue: false).forIssue(302).buildPrompt()
        .contains("gh issue comment"), "no comment when the toggle is off")
// The thank-you rides on the issue comment of a run that opens a PR, and on nothing else.
check(iDefault.contains("ALSO THANKS THE REPORTER"), "a PR-opening run thanks the reporter")
check(!IssueConfig(me: goldenMeSeed, openPRs: false).forIssue(302).buildPrompt()
        .contains("THANKS THE REPORTER"), "no thanks when no PR is opened")
check(!IssueConfig(me: goldenMeSeed, commentOnIssue: false).forIssue(302).buildPrompt()
        .contains("THANKS THE REPORTER"), "no thanks when nothing is posted on the issue")
// The run ends by accounting for the one issue it was handed, in one bucket.
check(iDefault.contains("one report on issue #302"), "the report is about that issue")
print("issue prompt assertions passed")

// ---- Auto-fix monitor diff (edge-triggering) ----
section("autofix diff")
func snap(_ n: Int, mergeable: String = "MERGEABLE", decision: String = "", threads: Int = 0,
          repo: String = "o/r") -> PRSnapshot {
    PRSnapshot(number: n, title: "PR \(n)", url: "https://github.com/\(repo)/pull/\(n)", isDraft: false,
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
let rPrior = ["o/r#1": PRFingerprint(mergeable: "MERGEABLE", reviewDecision: "", threadsUnresolved: 1)]
check(AutofixDiff.compute(prior: rPrior, now: [snap(1, threads: 4)]).events == [.review(snap(1, threads: 4))])
check(AutofixDiff.compute(prior: rPrior, now: [snap(1, decision: "CHANGES_REQUESTED", threads: 1)]).events
       == [.review(snap(1, decision: "CHANGES_REQUESTED", threads: 1))])
// Our own "Fixed in <hash>" replies (threads resolved, verdict unchanged) must NOT fire.
let selfReply = ["o/r#1": PRFingerprint(mergeable: "MERGEABLE", reviewDecision: "CHANGES_REQUESTED", threadsUnresolved: 5)]
check(AutofixDiff.compute(prior: selfReply, now: [snap(1, decision: "CHANGES_REQUESTED", threads: 0)]).events.isEmpty,
       "resolving threads must not retrigger")
// UNKNOWN mergeable carries the prior value forward — no phantom conflict.
let unk = ["o/r#1": PRFingerprint(mergeable: "MERGEABLE", reviewDecision: "", threadsUnresolved: 0)]
let u = AutofixDiff.compute(prior: unk, now: [snap(1, mergeable: "UNKNOWN")])
check(u.events.isEmpty && u.fingerprints["o/r#1"]?.mergeable == "MERGEABLE", "UNKNOWN carries prior forward")
// Fingerprints are per item: #1 in another repo is a PR seen for the first time, so it
// seeds silently rather than inheriting o/r#1's state and firing a conflict.
let twoRepos = AutofixDiff.compute(prior: unk, now: [snap(1, mergeable: "CONFLICTING", repo: "o/other"),
                                                     snap(1, repo: "O/R")])
check(twoRepos.events.isEmpty && twoRepos.fingerprints.keys.sorted() == ["o/other#1", "o/r#1"],
      "the same number in two repos is two fingerprints: \(twoRepos)")
check(AutofixDiff.compute(prior: twoRepos.fingerprints,
                          now: [snap(1, mergeable: "CONFLICTING", repo: "o/other"),
                                snap(1, mergeable: "CONFLICTING")]).events
      == [.conflict(snap(1, mergeable: "CONFLICTING"))],
      "and only the repo whose PR turned conflicting fires")
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
check(AgentDispatchGate.decide(source: .auto, banned: false, agentOnPR: false,
                               meshStandsDown: false, atCapacity: false,
                               lowDisk: true) == .lowDisk,
      "the free-disk floor gates auto dispatch")
check(AgentDispatchGate.decide(source: .panel, banned: false, agentOnPR: false,
                               meshStandsDown: false, atCapacity: false,
                               lowDisk: true) == .proceed,
      "one agent on a tight disk is the operator's call — panel is never disk-gated")
check(AgentDispatchGate.decide(source: .auto, banned: false, agentOnPR: false,
                               meshStandsDown: true, atCapacity: true, unaffordable: true,
                               lowDisk: true) == .lowDisk,
      "the disk outranks capacity, the budget and mesh — a freed bay frees no space")
check(AgentDispatchGate.decide(source: .auto, banned: false, agentOnPR: true,
                               meshStandsDown: false, atCapacity: false,
                               lowDisk: true) == .inFlight,
      "an agent already on the PR outranks the disk — nothing would start either way")
// The auto-review allowlist: an author verdict like the ban, so it holds whoever
// asks, and it ranks below the ban because a ban's remedy is the only one that would
// work on an author carrying both.
for src in [AgentDispatchGate.Source.panel, .auto] {
    check(AgentDispatchGate.decide(source: src, banned: false, agentOnPR: true,
                                   meshStandsDown: true, atCapacity: true,
                                   outsideAllowlist: true) == .notAllowed,
          "an unlisted author outranks everything but the ban for \(src.rawValue)")
    check(AgentDispatchGate.decide(source: src, banned: true, agentOnPR: false,
                                   meshStandsDown: false, atCapacity: false,
                                   outsideAllowlist: true) == .banned,
          "the ban outranks the allowlist for \(src.rawValue)")
}
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
check(AgentDispatchGate.runningAutoTasks(livePRs: ["o/r#1", "o/r#2"], autoPRs: [], manualPRs: []) == 2,
      "a live agent nobody tracked counts as automatic (an applet restart loses the book)")
check(AgentDispatchGate.runningAutoTasks(livePRs: ["o/r#1", "o/r#2"], autoPRs: [], manualPRs: ["o/r#1"]) == 1,
      "…unless it is a known manual one — a click never spends the automatic budget")
check(AgentDispatchGate.runningAutoTasks(livePRs: [], autoPRs: ["o/r#3"], manualPRs: []) == 1,
      "a just-spawned agent counts before ps has caught up with it")
check(AgentDispatchGate.runningAutoTasks(livePRs: ["o/r#3"], autoPRs: ["o/r#3"], manualPRs: []) == 1,
      "…and is not counted twice once it has")
check(AgentDispatchGate.runningAutoTasks(livePRs: ["o/r#1", "o/r#2", "o/r#3"], autoPRs: ["o/r#4"],
                                         manualPRs: ["o/r#2"]) == 3,
      "1, 3 and 4")
// An agent is spawned into an INTERACTIVE session, so finishing its work is not
// exiting: it waits at its prompt, `ps` keeps showing it, and the bay it took is never
// given back. Seen 2026-08-05 on a Linux box — two agents idle since the previous
// evening, both bays of a cap of 2 held, automatic work still being deferred 12h later.
// PARITY: the same six cases run against the Python twin in
// diplomat-platform/linux/tests/test_autofix.py.
check(AgentDispatchGate.runningAutoTasks(livePRs: ["o/r#1", "o/r#2"], autoPRs: [], manualPRs: [],
                                         idlePRs: ["o/r#1"]) == 1,
      "an untracked agent back at its prompt gives its bay back")
check(AgentDispatchGate.runningAutoTasks(livePRs: ["o/r#1", "o/r#2"], autoPRs: [], manualPRs: [],
                                         idlePRs: ["o/r#1", "o/r#2"]) == 0,
      "…and a machine of them is a machine with an empty cap")
check(AgentDispatchGate.runningAutoTasks(livePRs: ["o/r#1"], autoPRs: ["o/r#1"], manualPRs: [],
                                         idlePRs: ["o/r#1"]) == 0,
      "a TRACKED agent that went quiet leaves too — idle is subtracted from the union, "
      + "or `autoPRs` would re-add the very agent being let go")
check(AgentDispatchGate.runningAutoTasks(livePRs: ["o/r#1", "o/r#2"], autoPRs: [], manualPRs: [],
                                         idlePRs: []) == 2,
      "nothing idle changes nothing")
check(AgentDispatchGate.runningAutoTasks(livePRs: ["o/r#1", "o/r#2"], autoPRs: [], manualPRs: [],
                                         idlePRs: ["o/r#9"]) == 2,
      "an idle PR nobody is running here is not this machine's business")
check(AgentDispatchGate.runningAutoTasks(livePRs: ["o/r#1", "o/r#2"], autoPRs: [], manualPRs: []) == 2,
      "no evidence at all (the default) frees nothing — a terminal that cannot be read "
      + "must cost a deferral, never a burst")
check(AgentDispatchGate.runningAutoTasks(livePRs: ["o/a#1", "o/b#1"], autoPRs: [],
                                         manualPRs: ["o/a#1"], idlePRs: []) == 1,
      "#1 in two repos is two agents: the panel spawn on one leaves the other counted")

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

// Every ceiling gates, the tighter one answers, and no reading at all is no opinion.
// The ceilings' order fixes the tie-break.
let windowFirst = "first"
func windows(_ firstLeft: Double?, _ weekLeft: Double?,
             _ firstCost: Double?, _ weekCost: Double?) -> [(String, Double?, Double?)] {
    [(windowFirst, firstLeft, firstCost),
     (AgentDispatchGate.windowWeek, weekLeft, weekCost)]
}
check(AgentDispatchGate.budgetDecide(windows(50, 50, 10, 2), floor: 20).affordable,
      "room in both windows proceeds")
let broke = AgentDispatchGate.budgetDecide(windows(5, 50, 10, 2), floor: 20)
check(!broke.affordable && broke.window == windowFirst
      && broke.measured && broke.left == 5 && broke.needed == 10,
      "the first window refuses, and says what it had against what was needed")
let weekBroke = AgentDispatchGate.budgetDecide(windows(90, 1, 10, 2), floor: 20)
check(!weekBroke.affordable && weekBroke.window == AgentDispatchGate.windowWeek,
      "a full first window does not buy a spent weekly one")
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
check(tie.window == windowFirst, "the first-listed ceiling wins an exact tie")
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
// The status precedence, which is the list's reading order from `.awaitingInput` down.
check(AgentTaskStatus.allCases
      == [.merged, .done, .failed, .awaitingInput, .running, .starting, .unknown,
          .free, .queued],
      "an outcome, then a local exit, then a spawn that never ran, then what wants a "
      + "human, then what doesn't, then what is spawning, then what nothing is known "
      + "about, then this device's empty slots, then what hasn't started")
// Which of those statuses a row can actually wear is decided one enum over, on the
// states: both front-ends drop the runs that have ENDED (`Store.publish`,
// `Store.running_tasks`), so `.merged` and `.done` head the order and no drawn row
// reaches them. A state in neither set would be one the list draws and the dedup
// ignores — a second agent dispatched beside a run that is still up.
check(AgentState.ended.union(AgentState.blocking) == Set(AgentState.RunState.allCases)
      && AgentState.ended.isDisjoint(with: AgentState.blocking),
      "every state either ends a run or keeps it on the list")
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
      == [.merged, .done, .failed, .awaitingInput, .running, .starting, .unknown],
      "every state a run resolves to has a row status")
check(AgentState.stateOrder.map(AgentTaskStatus.of)
      == AgentTaskStatus.allCases.filter { $0 != .free && $0 != .queued },
      "the resolver's reading order is the row order, minus the two statuses no run "
      + "resolves to")
check(AgentTaskStatus.queued.title == "queued" && AgentTaskStatus.awaitingInput.title == "awaiting input"
      && AgentTaskStatus.free.title == "free slot" && AgentTaskStatus.starting.title == "starting"
      && AgentTaskStatus.unknown.title == "unknown"
      // Not "done", which is the whole point of the state: the work is still owed.
      && AgentTaskStatus.failed.title == "never started",
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
check(AgentTaskQueue.key(auditAction: "conflicts", item: "o/r#7") == "conflicts:o/r#7",
      "queue key is the monitor's verb plus the item")
check(AgentTaskQueue.key(auditAction: "review-req", item: "o/r#7")
      != AgentTaskQueue.key(auditAction: "review-reply", item: "o/r#7"),
      "one PR can owe two monitors — two tasks, two keys")
check(AgentTaskQueue.key(auditAction: "conflicts", item: PRRef.itemKey(owner: "o", repo: "a", number: 7))
      != AgentTaskQueue.key(auditAction: "conflicts", item: PRRef.itemKey(owner: "o", repo: "b", number: 7)),
      "…and #7 in two repos is two tasks")
check(AgentTaskQueue.item(ofKey: "conflicts:o/r#7") == "o/r#7"
      && AgentTaskQueue.item(ofKey: "review-req:o/r#7") == "o/r#7",
      "a queue key gives back its item")
check(AgentTaskQueue.item(ofKey: "investigate:1508284197000015") == nil
      && AgentTaskQueue.item(ofKey: "conflicts:7") == nil
      && AgentTaskQueue.item(ofKey: "nocolon") == nil,
      "a Slack ts, a legacy bare number and a malformed key name no item")

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
check(AgentTaskQueue.band("conflicts:1") == 4 && AgentTaskQueue.band("investigate:9") == 3
      && AgentTaskQueue.band("review:2") == 1 && AgentTaskQueue.band("issues:3") == 2
      && AgentTaskQueue.band("review-req:4") == 0 && AgentTaskQueue.band("a") == 0,
      "a conflict fix bands last, behind an investigation, which waits behind a requested "
      + "issue fix, which waits behind a requested review, all behind the monitors' own "
      + "finds; everything else — including a verbless key — first")
check(AgentTaskQueue.order(offered: ["conflicts:1", "investigate:6", "review:2", "review-req:3",
                                     "review-reply:4", "issues:5"], saved: [])
      == ["review-req:3", "review-reply:4", "review:2", "issues:5", "investigate:6", "conflicts:1"],
      "what GitHub is owed runs before the sweeps the operator asked for, which run "
      + "before an investigation, which runs before the conflict fix another agent may "
      + "make unnecessary")
check(AgentTaskQueue.order(offered: ["investigate:6", "issues:5", "conflicts:1"],
                           saved: ["conflicts:1", "investigate:6", "issues:5"])
      == ["issues:5", "investigate:6", "conflicts:1"],
      "an investigation's band holds against the operator's arrangement either way")
check(AgentTaskQueue.reorder(["issues:5", "investigate:6"], moving: "investigate:6",
                             onto: "issues:5") == ["issues:5", "investigate:6"],
      "an investigation cannot be dragged out of its band")
check(AgentTaskQueue.requestedActions == ["review", "issues", "investigate"],
      "an investigation is asked-for work, cancellable like the sweeps'")
check(AgentTaskQueue.order(offered: ["review:1", "issues:2"], saved: ["issues:2"])
      == ["review:1", "issues:2"],
      "a requested review is its own band ahead of issues, so it drains first however "
      + "the two were arranged")
check(AgentTaskQueue.reorder(["review-req:1", "review:2", "conflicts:3"],
                             moving: "review:2", onto: "review-req:1")
      == ["review-req:1", "review:2", "conflicts:3"]
      && AgentTaskQueue.reorder(["review-req:1", "review:2", "conflicts:3"],
                                moving: "review:2", onto: "conflicts:3")
      == ["review-req:1", "review:2", "conflicts:3"],
      "the requested-review band is a band like the others — a drag out of it is "
      + "refused whichever side it heads for")
check(AgentTaskQueue.reorder(["review:1", "issues:2"], moving: "issues:2", onto: "review:1")
      == ["review:1", "issues:2"],
      "…and a review and an issue fix are separate bands, so a drag between them is "
      + "refused")
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

check(AgentTaskQueue.stillOwed(auditAction: "conflicts", item: "o/r#8",
                               conflicting: ["o/r#7"], owingReply: ["o/r#8"], closed: []) == false,
      "a conflict fix on a PR this fetch no longer calls conflicting is work already done")
check(AgentTaskQueue.stillOwed(auditAction: "conflicts", item: "o/r#7",
                               conflicting: ["o/r#7"], owingReply: [], closed: []),
      "…and one the fetch still calls conflicting is dispatched")
check(AgentTaskQueue.stillOwed(auditAction: "review-reply", item: "o/r#8",
                               conflicting: ["o/r#8"], owingReply: [], closed: []) == false,
      "a reply on a PR whose threads are answered is work already done")
check(AgentTaskQueue.stillOwed(auditAction: "review-req", item: "o/r#3",
                               conflicting: [], owingReply: [], closed: []),
      "a review requested of me is not in this fetch to check — unanswerable is not stale")
// A closed PR is the one answer that reaches every verb, including the two the fetches
// above cannot speak for: a review of a merged diff is nobody's work.
check(AgentTaskQueue.stillOwed(auditAction: "review-req", item: "o/r#3",
                               conflicting: [], owingReply: [], closed: ["o/r#3"]) == false,
      "…until its PR closes, which no agent of mine can put back")
check(AgentTaskQueue.stillOwed(auditAction: "review", item: "o/r#31",
                               conflicting: [], owingReply: [], closed: ["o/r#31"]) == false,
      "a review the operator asked for goes the same way — merged is merged")
check(AgentTaskQueue.stillOwed(auditAction: "conflicts", item: "o/r#7",
                               conflicting: ["o/r#7"], owingReply: [], closed: ["o/r#7"]) == false,
      "closed outranks a fetch that still calls the PR conflicting")
check(AgentTaskQueue.stillOwed(auditAction: "investigate", item: "1508284197000015",
                               conflicting: [], owingReply: [], closed: ["1508284197000015"]),
      "an investigation is about no PR, so no PR closing retires it, even one of its number")

check(AgentTaskQueue.stillOwed(auditAction: "conflicts", item: "o/a#7",
                               conflicting: ["o/a#7"], owingReply: [], closed: ["o/b#7"]),
      "#7 closing in another repo retires nothing here")
check(AgentTaskQueue.stillOwed(auditAction: "conflicts", item: "o/a#7",
                               conflicting: ["o/b#7"], owingReply: [], closed: []) == false,
      "…and #7 conflicting in another repo owes nothing here")

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
                             prURL: "https://github.com/o/r/pull/337",
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
        mergedPRs: .present([]), liveAgents: .present(["o/r#337": "pts/5"]))) == .running,
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
    check(state(rec("h2", pid: nil, tty: "", age: 600, placement: .meshHere),
                AgentState.Evidence(
        processes: .present([:]), sentinels: .present([]),
        tails: .present(["pts/5": working]), claims: .present([]),
        mergedPRs: .present([]), liveAgents: .present(["o/other#337": "pts/5"]))) == .finished,
      "an agent on #337 of ANOTHER repo is not this run's agent")
    check(state(rec("m"), AgentState.Evidence(
        processes: .present(alive), sentinels: .present([]),
        tails: .present(["pts/3": working]), claims: .present([]),
        mergedPRs: .present(["o/other#337"]))) == .running
          && state(rec("m"), AgentState.Evidence(
        processes: .present(alive), sentinels: .present([]),
        tails: .present(["pts/3": working]), claims: .present([]),
        mergedPRs: .present(["o/r#337"]))) == .merged,
      "#337 merging in another repo leaves this run alone; its own merging ends it")
    // The outermost backstop, and the only rung that ends a run on the clock. Every
    // check above runs with it OFF (`resolveOne`'s default), which is what keeps them
    // about the rung each was written for.
    func deadlined(_ r: AgentState.RunRecord, _ e: AgentState.Evidence,
                   _ cutoff: TimeInterval?) -> AgentState.RunState {
        AgentState.resolveOne(r, evidence: e, now: now, deadline: cutoff).state
    }
    let oldAndWorking = AgentState.Evidence(
        processes: .present([4242: AgentState.ProcInfo(
            tty: "pts/3", elapsed: AgentState.runDeadline + 3600, isAgent: true)]),
        sentinels: .present([]), tails: .present(["pts/3": working]),
        claims: .present([]), mergedPRs: .present([]), tokensLeft: .present(true))
    let ancient = rec("k", age: AgentState.runDeadline + 3600)
    check(deadlined(ancient, oldAndWorking, AgentState.runDeadline) == .finished,
      "a run past the deadline is over whatever its screen still shows")
    check(deadlined(ancient, oldAndWorking, nil) == .running,
      "…and is left exactly as it was with the deadline switched off")
    var broke = oldAndWorking
    broke.tokensLeft = .present(false)
    check(deadlined(ancient, broke, AgentState.runDeadline) == .running,
      "…and with nothing left to spend: a parked agent ages like a wedged one")
    broke.tokensLeft = .unsupported("no limit this machine can read")
    check(deadlined(ancient, broke, AgentState.runDeadline) == .running,
      "…and with no limit readable at all, which is not the positive answer it needs")
    check(deadlined(rec("l", pid: nil, tty: "", age: AgentState.runDeadline + 3600,
                        placement: .meshPeer, workKey: "w", seen: now - 10),
                    AgentState.Evidence(processes: .present([:]), sentinels: .present([]),
                                        tails: .present([:]), claims: .present(["w"]),
                                        mergedPRs: .present([]),
                                        tokensLeft: .present(true)),
                    AgentState.runDeadline) == .running,
      "a peer's run is not ended by THIS device's deadline — its claim still holds")
    // Closing a window is the one destructive thing a tick does, and the gate is the
    // verdict rather than the clock: `pastDeadline` stays true for the rest of a long
    // run's life, so re-asking it reaps a five-hour run that stopped on its own.
    func reaps(_ e: AgentState.Evidence) -> Bool {
        let states = AgentState.resolve(records: [ancient], evidence: e, now: now,
                                        deadline: AgentState.runDeadline)
        return !AgentState.reapable(records: [ancient], states: states).isEmpty
    }
    var reported = oldAndWorking
    reported.tails = .present(["pts/3": atPrompt])
    reported.activity = .present(["k": AgentState.TurnReport(verb: .idle, at: now)])
    var sentinelled = oldAndWorking
    sentinelled.sentinels = .present(["k"])
    check(reaps(oldAndWorking),
      "the window of a run nothing could say ever stopped is nobody's")
    check(!reaps(reported),
      "…but one whose agent reported the turn over five hours in is not the reaper's")
    check(!reaps(sentinelled),
      "…and so does one whose exit code landed")
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
        roots: [], now: now, limit: 2)
    check(t.capLoad == ["auto"],
      "the cap counts automatic agents that run HERE — not a click, not a peer's")
    check(t.freeSlots == 1, "one of two bays filled")
    check(t.inFlight(item: "o/r#337"), "and the PR they are all on reads in-flight")
    check(!t.inFlight(item: "o/other#337"), "…while #337 in another repo does not")
    check(t.retirable.isEmpty, "nothing retires while every run is live")
    print("agent state assertions passed")
}

section("agent state: one number in two repos")
// Two roots both have a #5. Every place the resolver matches a run to evidence goes by
// item, so neither repo's agent, merge or scan sighting is read as the other's.
do {
    let now: TimeInterval = 2_000_000
    func run(_ id: String, _ slug: String, pid: Int? = nil, tty: String = "",
             placement: AgentState.Placement = .meshHere) -> AgentState.RunRecord {
        AgentState.RunRecord(runID: id, dispatchedAt: now - 600, prNumber: 5,
                             prURL: "https://github.com/\(slug)/pull/5",
                             placement: placement, pid: pid, tty: tty)
    }
    check(run("x", "O/A").item == "o/a#5", "a run's item is its URL's, lowercased")
    check(AgentState.RunRecord(runID: "y", dispatchedAt: now,
                               prURL: "https://github.com/o/a/pull/5").item == nil,
          "a run about no PR number has no item, whatever its URL")
    check(AgentState.RunRecord(runID: "z", dispatchedAt: now, prNumber: 5).item == nil,
          "…and a PR number with no URL to place it names none either")

    let scan: Observation<[String: String]> = .present(["o/a#5": "pts/1", "o/b#5": "pts/2"])
    let synth = AgentState.synthesizeUntracked([], liveAgents: scan, roots: [], now: now)
    check(synth.map(\.runID) == ["untracked:o/a#5", "untracked:o/b#5"]
          && synth.map(\.prURL) == ["https://github.com/o/a/pull/5",
                                    "https://github.com/o/b/pull/5"]
          && synth.allSatisfy { $0.prNumber == 5 && $0.untracked },
          "two live agents on #5 in two repos are two untracked runs: \(synth.map(\.runID))")
    let booked = AgentState.synthesizeUntracked([run("a", "o/a", tty: "pts/1")],
                                                liveAgents: scan, roots: [], now: now)
    check(booked.map(\.runID) == ["a", "untracked:o/b#5"],
          "a record on o/a#5 covers only o/a's agent: \(booked.map(\.runID))")
    check(AgentState.synthesizeUntracked(synth, liveAgents: scan, roots: [], now: now).count == 2,
          "and the next tick makes no third")

    let lab = PRRef.LocalRepo(owner: "Group/Sub", repo: "Proj", path: "/w/proj",
                              host: "gitlab.example.com")
    let labScan: Observation<[String: String]> = .present(["gitlab.example.com/group/sub/proj#337": "pts/4"])
    let hub = PRRef.LocalRepo(owner: "O", repo: "A", path: "/w/a")
    let hubRun = AgentState.synthesizeUntracked([], liveAgents: .present(["o/a#5": "pts/5"]),
                                                roots: [hub], now: now)
    check(hubRun.map(\.prURL) == ["https://github.com/O/A/pull/5"] && hubRun.map(\.item) == ["o/a#5"],
          "a GitHub root's untracked run links to its root's PR on github.com, its item the scan's")
    let labRun = AgentState.synthesizeUntracked([], liveAgents: labScan, roots: [lab], now: now)
    let labAgain = AgentState.synthesizeUntracked(labRun, liveAgents: labScan, roots: [lab],
                                                  now: now + 8)
    check(labRun.map(\.prURL) == ["https://gitlab.example.com/Group/Sub/Proj/-/merge_requests/337"]
          && labRun.map(\.item) == ["gitlab.example.com/group/sub/proj#337"]
          && labAgain.map(\.runID) == ["untracked:gitlab.example.com/group/sub/proj#337"],
          "an agent on a nested GitLab merge request is one untracked run, linked to the merge "
          + "request, on the scan's item, tick after tick: \(labAgain.map(\.prURL))")

    let adopted = AgentState.adoptTTYs([run("a", "o/a")], processes: .present([:]),
                                       liveAgents: .present(["o/b#5": "pts/2"]))
    check(adopted[0].tty.isEmpty, "a pid-less run never adopts another repo's agent's tty")

    // Each run ended by its merge with its agent still up and no pid to tell agents
    // apart: released only when alone on its item, and o/b's run is not o/a's neighbour.
    let ended = [run("a", "o/a", tty: "pts/1", placement: .local),
                 run("b", "o/b", tty: "pts/2", placement: .local)]
    let mergedBoth = AgentState.Evidence(
        processes: .present([:]), sentinels: .present([]),
        tails: .present(["pts/1": "\u{276F}", "pts/2": "\u{276F}"]), claims: .present([]),
        mergedPRs: .present(["o/a#5", "o/b#5"]), liveAgents: scan)
    let verdicts = AgentState.resolve(records: ended, evidence: mergedBoth, now: now)
    let heirs = AgentState.releaseEnded(ended, states: verdicts, evidence: mergedBoth, now: now)
    check(verdicts.values.allSatisfy { $0.state == .merged }
          && heirs.map(\.0.runID).sorted() == ["untracked:o/a#5", "untracked:o/b#5"],
          "two pid-less runs on #5 in two repos are each alone on their item: "
          + "\(heirs.map(\.0.runID))")

    let t = AgentState.tick(records: [run("a", "o/a", pid: 11, tty: "pts/1", placement: .local)],
                            evidence: AgentState.Evidence(
                                processes: .present([11: AgentState.ProcInfo(
                                    tty: "pts/1", elapsed: 600, isAgent: true)]),
                                sentinels: .present([]),
                                tails: .present(["pts/1": "esc to interrupt"]),
                                claims: .present([]), mergedPRs: .present([]),
                                liveAgents: .present([:])),
                            roots: [], now: now, limit: 2)
    check(t.inFlight(item: "o/a#5") && !t.inFlight(item: "o/b#5"),
          "an agent on o/a#5 blocks a second there and nothing on o/b#5")

    let roots: Set<String> = ["software-mansion/argent", "o/r.js"]
    check(AgentState.promptItems("Review PR #12 in software-mansion/argent. Use the `gh` CLI",
                                 keys: roots) == ["software-mansion/argent#12"],
          "the prompt's closing period is not part of the repo")
    check(AgentState.promptItems("PR #3 in o/r.js. and PR #4 in O/R.JS now", keys: roots)
          == ["o/r.js#3", "o/r.js#4"],
          "a repo whose name has a dot keeps it, and the slug is matched case-insensitively")
    check(AgentState.promptItems("PR #9 in other/repo. PR #10 in o/r. PR #11 in o/r.jsx",
                                 keys: roots).isEmpty,
          "a repo outside the roots, or one that only starts like a root, is no sighting")
    let nested: Set<String> = ["group/sub/proj", "group/sub", "o/r.js"]
    check(AgentState.promptItems("Review PR #337 in group/sub/proj. Then PR #8 in Group/Sub/Proj",
                                 keys: nested) == ["group/sub/proj#337", "group/sub/proj#8"]
          && AgentState.promptItems("PR #3 in o/r.js.", keys: nested) == ["o/r.js#3"]
          && AgentState.promptItems("PR #4 in group/sub./proj", keys: nested).isEmpty,
          "a nested GitLab namespace is read too, the longest slug counting, its period the sentence's")
    print("cross-repo agent state assertions passed")
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
        prompt: "p")!.runID

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

    check(AgentRegistry.serviceSession(run) == nil, "an unbound OpenCode run is on no session")
    AgentRegistry.bindSession(run, "ses_diplomat_ab")
    check(AgentRegistry.serviceSession(run) == "ses_diplomat_ab", "bound with no port is 2.x")
    AgentRegistry.stagePort(run, 47_910)
    check(AgentRegistry.serviceSession(run) == nil, "a port makes it a 1.x run")
    try? FileManager.default.removeItem(at: AgentRegistry.portPath(run))
    AgentRegistry.stageRunner(run, AgentRunner.hermes.rawValue)
    check(AgentRegistry.serviceSession(run) == nil, "a Hermes session is not OpenCode's")
    AgentRegistry.forget([run])
    check(AgentRegistry.load().isEmpty && AgentRegistry.boundSession(run) == "",
          "forgetting a run takes its sidecars with it")
    // Every edit of the book is one locked read-modify-write. The spawn below is
    // fired from inside the transform, as test_agent_registry.py's is from inside
    // forget's read: with the read outside the lock it lands between that read and
    // the write, and the write covers it.
    AgentRegistry.add(AgentState.RunRecord(runID: "upd-a", dispatchedAt: now))
    AgentRegistry.add(AgentState.RunRecord(runID: "upd-b", dispatchedAt: now))
    func bookAfterUpdate() -> [String] {
        let spawn = DispatchGroup()
        AgentRegistry.update { book in
            DispatchQueue.global().async(group: spawn) {
                AgentRegistry.add(AgentState.RunRecord(runID: "upd-c", dispatchedAt: now))
            }
            _ = spawn.wait(timeout: .now() + 0.5)
            return book.filter { $0.runID != "upd-a" }
        }
        spawn.wait()
        return AgentRegistry.load().map(\.runID)
    }
    check(bookAfterUpdate() == ["upd-b", "upd-c"],
          "a run registered during an update is in the book it leaves")
    AgentRegistry.forget(["upd-b", "upd-c"])
    print("run book sidecar assertions passed")
}

section("a run the book refuses")
// A book that takes no write while the run's own directory does: the order a disk
// fills in, since the book is rewritten whole and the prompt is one small file.
do {
    let book = FileManager.default.temporaryDirectory
        .appendingPathComponent("diplomat-smoke-refused-\(UUID().uuidString)", isDirectory: true)
    setenv("DIPLOMAT_AGENTS_DIR", book.path, 1)
    defer { try? FileManager.default.removeItem(at: book) }
    try? FileManager.default.createDirectory(at: AgentRegistry.runsPath(),
                                             withIntermediateDirectories: true)
    let now = Date().timeIntervalSince1970
    let id = AgentRegistry.newRunID(now: now)
    check(AgentRegistry.createRun(AgentState.RunRecord(runID: id, dispatchedAt: now),
                                  prompt: "p") == nil,
          "a run the book cannot record is refused")
    check(!FileManager.default.fileExists(atPath: AgentRegistry.runDir(id).path),
          "…and leaves no run directory behind")
}

section("a hand-edited run book and ledger")
// PARITY: `test_agent_registry_parity.py` and `test_telemetry_parity.py` feed the same
// spoils to both readers. Here they pin this build's own reading, which is the half that
// differs between Darwin and corelibs if anything does.
do {
    let book = FileManager.default.temporaryDirectory
        .appendingPathComponent("diplomat-smoke-book-\(UUID().uuidString)", isDirectory: true)
    setenv("DIPLOMAT_AGENTS_DIR", book.path, 1)
    defer { try? FileManager.default.removeItem(at: book) }
    try? FileManager.default.createDirectory(at: book, withIntermediateDirectories: true)
    func read(_ runs: String, version: String = "1") -> [AgentState.RunRecord] {
        try? "{\"version\": \(version), \"runs\": [\(runs)]}"
            .write(to: AgentRegistry.runsPath(), atomically: true, encoding: .utf8)
        return AgentRegistry.load()
    }
    for spoilt in ["1", "1.0", "\"true\""] {
        check(read("{\"runId\": \"r\", \"untracked\": \(spoilt)}").map(\.untracked) == [false],
              "`\"untracked\": \(spoilt)` is not a flag")
    }
    check(read("{\"runId\": \"r\", \"untracked\": true}").map(\.untracked) == [true],
          "a real flag still reads as one")
    let numbers = read("{\"runId\": \"r\", \"pid\": true, \"prNumber\": true, \"dispatchedAt\": true}")
    check(numbers.count == 1 && numbers[0].pid == nil && numbers[0].prNumber == nil
            && numbers[0].dispatchedAt == 0, "`true` is not the number 1")
    check(read("{\"runId\": \"r\"}", version: "true").isEmpty, "`\"version\": true` is no schema")
    check(read("{\"runId\": \"r\"}, 5").map(\.runID) == ["r"],
          "one entry that is not a record costs that entry, not the book")
    let text = "{\"version\": 1, \"runs\": [{\"runId\": \"r\", \"untracked\": true}]}"
    for (encoding, bytes) in [("UTF-16", text.data(using: .utf16)!),
                              ("UTF-8 with a byte-order mark", Data([0xEF, 0xBB, 0xBF]) + Data(text.utf8))] {
        try? bytes.write(to: AgentRegistry.runsPath(), options: .atomic)
        check(AgentRegistry.load().isEmpty, "a book in \(encoding) is unreadable, as to Python")
    }
    // Each read one way by `json` and another by Darwin's `JSONSerialization`, corelibs' or both.
    func nested(_ levels: Int) -> String {
        String(repeating: "[", count: levels) + String(repeating: "]", count: levels)
    }
    for (spoil, fields) in [("-1e400", ", \"x\": -1e400"), ("a trailing comma", ", \"x\": [1,]"),
                            ("a trailing comma in the record", ","),
                            ("an empty array past the depth limit", ", \"x\": \(nested(510))"),
                            ("a string opening with an escaped U+FEFF", ", \"label\": \"\\uFEFFx\""),
                            ("a string opening with U+FEFF", ", \"tty\": \"\u{FEFF}pts/3\"")] {
        check(read("{\"runId\": \"r\"\(fields)}").isEmpty, "a book holding \(spoil) is unreadable")
    }
    check(read("{\"runId\": \"r\", \"label\": \"\\\",]x\u{FEFF}\", \"x\": \(nested(509))}")
            .map(\.label) == ["\",]x\u{FEFF}"],
          "`,]` and a U+FEFF inside a string, and nesting at the limit, are read")
    check(read("{\"runId\": \"r\"}", version: "18446744073709551617").isEmpty,
          "a version that wraps to 1 in Int64 is no schema")
    check(JSONInput.parse(Data("{\"title\": \"\u{FEFF}Fix\", \"body\": \"\\uFEFF\"}".utf8)) != nil,
          "a CLI payload holding a PR title that opens with U+FEFF is read")
    // corelibs keeps a repeated key's last value, and both platforms' parsers misround
    // some numbers (corelibs the first literal, both the other two); `json` does neither.
    let repeated = read("{\"runId\": \"r\", \"prNumber\": 5, \"pid\": 1, \"prNumber\": 7, "
                        + "\"pid\": 2, \"tty\": \"t\"}")
    check(repeated.map(\.prNumber) == [5] && repeated.map(\.pid) == [1]
            && repeated.map(\.tty) == ["t"], "a key given twice keeps its first value")
    let nested = JSONInput.parse(Data("{\"a\": 1, \"a\": {\"b\": 2, \"b\": 3}, \"c\": [\"t\", \"t\", \"t\"]}".utf8))
    check(nested?["a"] as? Int == 1 && nested?["c"] as? [String] == ["t", "t", "t"],
          "a repeat holding a repeat goes whole, and an array's equal strings all stay")
    for literal in ["1791274860.7710276", "0.026461970522229095", "680352917189831768722810"] {
        check(read("{\"runId\": \"r\", \"dispatchedAt\": \(literal)}").map(\.dispatchedAt)
                == [Double(literal)!], "`\(literal)` reads as its correctly rounded double")
    }

    let ledger = Telemetry.fold(lines: [
        "{\"at\": 100, \"ev\": \"started\", \"key\": \"k1\", \"remote\": 1}",
        "{\"at\": 100, \"ev\": \"started\", \"key\": \"k2\", \"remote\": true}",
        "{\"at\": true, \"ev\": \"queued\", \"key\": \"k3\"}",
        "{\"at\": \"100\\u0000\", \"ev\": \"queued\", \"key\": \"k4\"}",
        "\u{200B}{\"at\": 100, \"ev\": \"queued\", \"key\": \"k5\"}",
        " {\"at\": 100, \"ev\": \"queued\", \"key\": \"k6\"}\r",
        "\u{FEFF}{\"at\": 100, \"ev\": \"queued\", \"key\": \"k7\"}",
    ])
    check(ledger.tasks.map(\.key) == ["k1", "k2", "k6"] && ledger.tasks.map(\.remote) == [false, true, false],
          "the ledger reads `\"remote\": 1` as local, `\"at\": true` or `\"100\\0\"` as no "
            + "timestamp, and JSON's whitespace alone around a line")
    let line = Telemetry.fold(lines: ["{\"at\": 100, \"ev\": \"done\", \"key\": \"k\", \"pr\": 5, "
                                      + "\"pr\": 7, \"tokens\": 1791274860.7710276}"])
    check(line.tasks.map(\.pr) == [5] && line.tasks.map(\.tokens) == [1791274860.7710276],
          "a ledger line keeps a repeated key's first value and reads a double exactly")
    print("hand-edited run book and ledger assertions passed")
}

section("the run book under contention")
// Every edit is a read-modify-write under one lock, and the lock covers the read: a
// read outside it is a copy that misses what a concurrent writer booked, and the
// write that follows drops it. Eight threads each book a run and forget their previous
// one, forty times, racing; what survives is exactly each thread's last run.
do {
    let book = FileManager.default.temporaryDirectory
        .appendingPathComponent("diplomat-smoke-agents-\(UUID().uuidString)", isDirectory: true)
    setenv("DIPLOMAT_AGENTS_DIR", book.path, 1)
    defer { try? FileManager.default.removeItem(at: book) }
    let now = Date().timeIntervalSince1970
    let threads = 8, rounds = 40
    let started = Date()
    func race() {
        let done = DispatchGroup()
        for t in 0..<threads {
            DispatchQueue.global().async(group: done) {
                var previous: String?
                for k in 0..<rounds {
                    let id = "t\(t)-\(k)"
                    AgentRegistry.update { $0 + [AgentState.RunRecord(runID: id, dispatchedAt: now)] }
                    if let previous { AgentRegistry.forget([previous]) }
                    previous = id
                }
            }
        }
        done.wait()
    }
    race()
    let survivors = Set(AgentRegistry.load().map(\.runID))
    let expected = Set((0..<threads).map { "t\($0)-\(rounds - 1)" })
    check(survivors == expected,
          "\(threads * rounds) racing updates and forgets lose nothing: "
          + "\(expected.subtracting(survivors).count) missing, \(survivors.subtracting(expected).count) resurrected")
    print("run book contention assertions passed (\(threads * rounds) updates in "
          + "\(String(format: "%.2f", Date().timeIntervalSince(started)))s)")
}

section("numbers in a hand-edited run book")
// PARITY: `tests/test_agent_registry_parity.py` reads these books on both sides. The
// two parsers already disagree on these literals before either decoder runs, so both
// default them by one rule (`AgentRegistry.number`): finite, and inside Int64 for an
// integer field.
do {
    let book = FileManager.default.temporaryDirectory
        .appendingPathComponent("diplomat-smoke-agents-\(UUID().uuidString)", isDirectory: true)
    setenv("DIPLOMAT_AGENTS_DIR", book.path, 1)
    defer { try? FileManager.default.removeItem(at: book) }
    try? FileManager.default.createDirectory(at: book, withIntermediateDirectories: true)
    let wide = """
    {"version": \(AgentRegistry.schemaVersion), "runs": [
      {"runId": "r1", "dispatchedAt": 1, "claimSeenAt": 1e300, "pid": 1e300,
       "prNumber": 99999999999999999999},
      {"runId": "r2", "dispatchedAt": 1, "pid": 99999999999999999999, "prNumber": 1e300}]}
    """
    try? wide.write(to: AgentRegistry.runsPath(), atomically: true, encoding: .utf8)
    let runs = AgentRegistry.load()
    check(runs.map(\.runID) == ["r1", "r2"], "the records survive their unusable numbers")
    check(runs[0].claimSeenAt == 1e300, "a finite double stays what it is")
    check(runs.allSatisfy { $0.pid == nil && $0.prNumber == nil },
          "past Int64 is no pid and no PR number, whichever way it is spelled")

    let infinite = """
    {"version": \(AgentRegistry.schemaVersion), "runs": [
      {"runId": "r1", "dispatchedAt": -1e999, "quietSince": -1e999}]}
    """
    try? infinite.write(to: AgentRegistry.runsPath(), atomically: true, encoding: .utf8)
    // Darwin's parser makes `-inf` of it, which the next write of the book dies on.
    check(AgentRegistry.load().isEmpty, "a document holding -1e999 is no book")
    print("hand-edited number assertions passed")
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
    // 10M. The third spans a RESET (more left than before), which prices nothing, and
    // ends after every local task below has started, so the 5-hour readings cover
    // them all.
    #"{"at": 1784900000, "ev": "sample", "sessionLeft": 1.0, "weekLeft": 1.0, "repoTokens": 0, "otherTokens": 0}"#,
    #"{"at": 1784903600, "ev": "sample", "sessionLeft": 0.85, "weekLeft": 0.99, "repoTokens": 1000000, "otherTokens": 500000}"#,
    #"{"at": 1784907200, "ev": "sample", "sessionLeft": 0.75, "weekLeft": 0.98, "repoTokens": 1600000, "otherTokens": 900000}"#,
    #"{"at": 1784932000, "ev": "sample", "sessionLeft": 1.0, "weekLeft": 0.97, "repoTokens": 1800000, "otherTokens": 1000000}"#,
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
                                  binCount: 12, z: 1.96, bucketHours: 12)
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
// The finished-work bars. One task finished, 21.7 hours before `now`, so it belongs
// to the 12-hour bucket that opened a day ago and to no other.
check(summary.finished.count == 28, "a fortnight of 12-hour buckets is 28 bars")
check(summary.finished.filter { $0.count > 0 }.map(\.at) == [tNow - 86_400],
      "the finished task was bucketed somewhere other than where its exit falls")
check(summary.finishedCount == 1 && summary.peakFinished == 1)
// The bars are laid backwards from `now`, so the newest covers a whole bucket like
// every other one rather than whatever fraction of one the range ends on.
check(summary.finished.last?.at == tNow - 43_200,
      "the newest bar is not a whole bucket ending at `now`")
check(summary.bucketHours == 12, "the bar width must come back out with the bars")
check(summary.repoTokens == 1_800_000 && summary.otherTokens == 1_000_000)
// The quota readings are drawn as they were taken, not resampled onto a grid: the
// 5-hour window's sawtooth is the shape worth seeing, and interpolating it would
// smooth away the resets that give it its meaning.
check(summary.quota.count == 4, "a quota reading was dropped or invented")
check(summary.quota.map(\.sessionPct) == [100, 85, 75, 100],
      "the readings moved off the instants they were measured at")
// The fixture's newest reading is most of a day old, and the headline carries no
// age: a figure that stale is a claim about a window that has since moved, so the
// card declines to make it. The chart still draws the reading where it was taken.
check(summary.weekLeftPct == nil,
      "a day-old reading was printed as what is left right now")
check(Telemetry.duration(0, samples: 0) == "—", "no samples must not read as instant")
check(Telemetry.duration(90) == "1m 30s")
check(Telemetry.duration(5400) == "1h 30m")
check(Telemetry.percent(5) == "5.0%")
check(Telemetry.tokens(1_800_000) == "1.8M")
// What a bar counts, on a ledger written for the question: a run the mesh placed on
// a peer finished all the same and is drawn here, though its cost is the peer's and
// stays out of every per-task figure; a run still going at `now` is not drawn; and a
// run that started before the range opened is placed by its EXIT, which is inside it.
let fLedger = Telemetry.fold(lines: [
    #"{"at": \#(tNow - 7200), "ev": "started", "key": "review:h/o/r#10@a", "remote": true, "attempt": 1}"#,
    #"{"at": \#(tNow - 3600), "ev": "done", "key": "review:h/o/r#10@a", "tokens": 1000}"#,
    #"{"at": \#(tNow - 7200), "ev": "started", "key": "review:h/o/r#11@b", "remote": false, "attempt": 1}"#,
    #"{"at": \#(tNow - 10 * 86_400), "ev": "started", "key": "review:h/o/r#12@c", "remote": false, "attempt": 1}"#,
    #"{"at": \#(tNow - 5400), "ev": "done", "key": "review:h/o/r#12@c", "tokens": 1000}"#,
])
let fSummary = Telemetry.summarize(fLedger, now: tNow, days: 1, steps: 8, binCount: 4,
                                   z: 1.96, bucketHours: 4)
check(fSummary.finished.count == 6, "a day of 4-hour buckets is 6 bars")
check(fSummary.finishedCount == 2,
      "a peer's finished run, or one that outlived the start of the range, was dropped")
check(fSummary.finished.last?.count == 2, "both exits belong in the newest bucket")
check(fSummary.peakFinished == 2)
// A lookback the bucket does not divide, which no shipped range is: the bars still
// run backwards from `now`, so the NEWEST is a whole bucket and it is the oldest that
// reaches back past the range. Laid forwards from the start instead, the newest would
// hold whatever fraction of a bucket was left over and read as a slump.
let oddSummary = Telemetry.summarize(fLedger, now: tNow, days: 1, steps: 8, binCount: 4,
                                     z: 1.96, bucketHours: 5)
check(oddSummary.finished.count == 5, "a day of 5-hour buckets is 5 bars")
check(oddSummary.finished.last?.at == tNow - 5 * 3600
      && oddSummary.finished.first?.at == tNow - 25 * 3600,
      "the bars were laid forwards from the start of the range")
check(Telemetry.bucketLabel(4) == "4h" && Telemetry.bucketLabel(48) == "48h")
check(Telemetry.bucketLabel(0.5) == "30m 00s", "a fractional bucket read as 0h")
check(Telemetry.bucketLabel(1e19) == "\(Int.max)h", "a width past Int's range must clamp, not trap")
// The bar width per lookback, which is the shared model's to decide — the two screens
// draw the same bars only because they ask this rather than each picking a width.
let tModel = try? CoreAssets.telemetry()
check(tModel?.bucketHours(days: 7) == 4 && tModel?.bucketHours(days: 14) == 12
      && tModel?.bucketHours(days: 30) == 24 && tModel?.bucketHours(days: 60) == 48,
      "the shipped ranges do not bucket the way the model says")
// A value past Int's range is a corrupt or hand-edited line, and `Int(Double)` traps on
// it. The Telemetry screen folds the ledger on every repaint, so a trap there would
// take the app down at every launch.
let poisoned = Telemetry.fold(lines: [
    #"{"at": 1784920000, "ev": "queued", "key": "review:h/o/r#9@zz", "duty": "review", "pr": 1e300}"#])
check(poisoned.tasks.first?.pr == Int.max, "an out-of-range pr must clamp, not trap")
check(Telemetry.duration(1e300).hasSuffix("m"), "an absurd duration must format, not trap")
// A count whose share of the window overflows a Double. As `inf` it would bin as NaN,
// which `Int(Double)` traps on - the same repaint-time trap. The share is dropped;
// the count is still what the task cost.
let overflowed = Telemetry.fold(lines: tLines + [
    #"{"at": 1784930000, "ev": "started", "key": "review:h/o/r#5@ee", "remote": false, "attempt": 1}"#,
    #"{"at": 1784931000, "ev": "done", "key": "review:h/o/r#5@ee", "tokens": 1e308}"#,
])
let overflowedSummary = Telemetry.summarize(overflowed, now: tNow, days: 14, steps: 56,
                                            binCount: 12, z: 1.96, bucketHours: 12)
check(overflowedSummary.perTask.count == 1 && overflowedSummary.perTaskWeek.count == 1,
      "an overflowed share must be dropped, not binned")
check(overflowedSummary.perTask.mean == 5, "the overflowed share moved the mean")
check(overflowedSummary.perTaskTokensMean > 1e300, "the count itself was dropped as a cost")
// The reading moves in whole 1% steps, so a flat interval's tokens belong to the step
// a later one shows; a reset and spending at 0% left price nothing. Twin of
// test_telemetry.py's flat-interval and reset/exhausted tests.
func weekSamples(_ readings: [(Double, Double)]) -> [Telemetry.Sample] {
    Telemetry.fold(lines: readings.enumerated().map { i, r in
        #"{"at": \#(900 * i), "ev": "sample", "weekLeft": \#(r.0), "repoTokens": \#(r.1), "otherTokens": 0}"#
    }).samples
}
check(Telemetry.calibrate(weekSamples([(0.50, 0), (0.50, 200_000), (0.50, 400_000),
                                       (0.49, 600_000)]), session: false)
        .map { abs($0 - 60_000_000) < 1 } == true,
      "a flat interval's tokens were dropped from the window's price")
check(Telemetry.calibrate(weekSamples([(0.02, 0), (0.01, 100_000), (0.0, 200_000),
                                       (0.0, 900_000), (1.0, 5_000_000), (0.99, 5_100_000),
                                       (1.0, 6_000_000)]), session: false)
        .map { abs($0 - 10_000_000) < 1 } == true,
      "a reset or an exhausted window's tokens were priced")
// A ledger with no quota readings can count tokens but cannot honestly turn them
// into a share of a window — the screen shows tokens and says so.
let unpriced = Telemetry.fold(lines: tLines.filter { !$0.contains("\"sample\"") })
check(Telemetry.calibrate(unpriced.samples, session: true) == nil)
let unpricedSummary = Telemetry.summarize(unpriced, now: tNow, days: 14, steps: 56,
                                          binCount: 12, z: 1.96, bucketHours: 12)
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
                                              steps: 56, binCount: 12, z: 1.96,
                                              bucketHours: 12)
check(sessionBlindSummary.sessionLimitTokens == nil)
check(sessionBlindSummary.perTask.count == 0)
check(sessionBlindSummary.perTaskWeek.count == 1,
      "the week went unpriced with the 5-hour window rather than on its own readings")
// A missing reading is not a window at zero: it stays a hole in the series.
let blind = Telemetry.fold(lines: tLines + [
    #"{"at": 1784933600, "ev": "sample", "sessionLeft": null, "weekLeft": null, "repoTokens": 1800000, "otherTokens": 1000000}"#,
])
let blindSummary = Telemetry.summarize(blind, now: tNow, days: 14, steps: 56,
                                       binCount: 12, z: 1.96, bucketHours: 12)
check(blindSummary.quota.count == 5, "a blind sample is still a sample taken")
check(blindSummary.quota.last?.sessionPct == nil, "a gap was filled in")
// The 5-hour price is measured from history and outlives the readings it came from,
// so it prices only work they covered: a task started at the last sample carrying a
// 5-hour reading has a share of it, one started after has a weekly share alone.
let afterSession = Telemetry.fold(lines: tLines + [
    #"{"at": 1784940000, "ev": "sample", "weekLeft": 0.96, "repoTokens": 2100000, "otherTokens": 1200000}"#,
    #"{"at": 1784932000, "ev": "started", "key": "review:h/o/r#6@ff", "remote": false, "attempt": 1}"#,
    #"{"at": 1784933000, "ev": "done", "key": "review:h/o/r#6@ff", "tokens": 300000}"#,
    #"{"at": 1784941000, "ev": "started", "key": "review:h/o/r#7@gg", "remote": false, "attempt": 1}"#,
    #"{"at": 1784942000, "ev": "done", "key": "review:h/o/r#7@gg", "tokens": 400000}"#,
])
let afterSummary = Telemetry.summarize(afterSession, now: tNow, days: 14, steps: 56,
                                       binCount: 12, z: 1.96, bucketHours: 12)
check(afterSummary.sessionLimitTokens == 10_000_000)
check(afterSummary.perTask.count == 2 && afterSummary.perTask.mean == 4,
      "the 5-hour window priced a task started after its last reading, or dropped "
      + "one started at it")
check(afterSummary.perTaskWeek.count == 3, "the week dropped a task it still measures")

// How long a reading stays an answer to "what is left right now". Either side of
// the bound, on a ledger whose newest sample is a probe that could not answer —
// the shape every failure here takes, since the probe keeps sampling once blind.
let fresh = Telemetry.quotaFreshSecs
check(fresh > 0, "the freshness bound must come off the shared model, not nowhere")
func headline(lastGoodAgo: Double) -> Double? {
    let led = Telemetry.fold(lines: [
        #"{"at": \#(tNow - lastGoodAgo), "ev": "sample", "sessionLeft": 0.42, "weekLeft": 0.8, "repoTokens": 0, "otherTokens": 0}"#,
        #"{"at": \#(tNow), "ev": "sample", "sessionLeft": null, "weekLeft": null, "repoTokens": 0, "otherTokens": 0}"#,
    ])
    let sum = Telemetry.summarize(led, now: tNow, days: 7, steps: 8, binCount: 4,
                                  z: 1.96, bucketHours: 4)
    return sum.weekLeftPct
}
// A gap shorter than the chart draws through: the reading is minutes old and still
// answers, so blanking it would lose a figure measured perfectly well.
check(headline(lastGoodAgo: fresh - 60) == 80,
      "a probe that missed a few rounds blanked the figure it did measure")
// Past it, no answer. This is the failure that hid a dead credential for six days:
// the probe went blind, every later sample carried nulls, and the card went on
// printing the last live reading as the current one on every lookback offered.
check(headline(lastGoodAgo: fresh + 60) == nil,
      "a reading the chart will not bridge was still printed as the current one")
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
// The verdict and the soft approval are CASE B's alone. CASE B is the last section, so
// the blocks both cases share (the swarm loop, the attribution tag) precede CASE A, and
// the verdict follows CASE B's header with nothing shared after it.
for (label, cfg) in [("soft-approve", ReviewConfig(depth: "deep", target: .specific, me: "latekvo",
                                                   specificPR: "440")),
                     ("verdict", ReviewConfig(depth: "deep", target: .specific, me: "latekvo",
                                              specificPR: "440", finalPass: true))] {
    let p = cfg.buildPrompt()
    let caseA = p.range(of: "▸ CASE A")!.lowerBound, caseB = p.range(of: "▸ CASE B")!.lowerBound
    let verdict = p.range(of: label == "verdict" ? "and if so, APPROVE it" : "thanking them for contributing")!
    check(verdict.lowerBound > caseB, "\(label): the verdict sits inside CASE B")
    check(p.range(of: "Keep dispatching swarms until")!.lowerBound < caseA,
          "\(label): the shared swarm loop precedes the split, not CASE B")
    check(p.range(of: "Diplomat attribution tag")!.lowerBound < caseA,
          "\(label): the shared attribution tag precedes the split, not CASE B")
    check(!p[caseA..<caseB].contains("APPROVE it") && !p[caseA..<caseB].contains("thanking them"),
          "\(label): CASE A carries no verdict")
}
// A clean pass's LOW findings are fixed on my own PR and posted on someone else's. Each
// prompt carries its own disposition's block only, and the gated one puts each in its case.
let lowsMine = "it is FIXED on the branch", lowsTheirs = "the LOW findings of that clean pass are posted"
for (label, p) in [("known-mine", km),
                   ("mine sweep", ReviewConfig(depth: "max", target: .mine, me: "latekvo").buildPrompt())] {
    check(p.contains(lowsMine) && p.contains("ZERO pending findings") && !p.contains(lowsTheirs),
          "\(label): LOW findings must be fixed, not posted")
    check(!p.contains("post the LOW"), "\(label): the swarm loop still says to post LOW findings")
}
for (label, p) in [("known-theirs", ReviewConfig(depth: "max", target: .specific, me: "latekvo",
                                                 specificPR: "500", specificAuthor: .theirs).buildPrompt()),
                   ("theirs sweep", ReviewConfig(depth: "max", target: .someone, username: "other",
                                                 me: "latekvo").buildPrompt())] {
    check(p.contains(lowsTheirs) && !p.contains(lowsMine), "\(label): LOW findings must be posted")
}
let gatedA = gated.range(of: "▸ CASE A")!.lowerBound, gatedB = gated.range(of: "▸ CASE B")!.lowerBound
check(gated.range(of: lowsMine).map { gatedA < $0.lowerBound && $0.lowerBound < gatedB } == true,
      "gated: the fix-every-LOW block belongs to CASE A")
check(gated.range(of: lowsTheirs).map { $0.lowerBound > gatedB } == true,
      "gated: the post-the-LOWs block belongs to CASE B")
print("known-mine review prompt assertions passed")

// ---- known-theirs comprehensive review (review-request monitor) ----
// Default: soft-approve ON, no hard verdict → a perfectly-clean PR gets a thank-you
// COMMENT review, but still NO APPROVE action.
section("known-theirs review prompt (default → soft-approve)")
let kt = ReviewConfig(depth: "max", target: .specific, me: "latekvo",
                      markReady: false, leaveReviews: true, replyToReviews: false,
                      specificPR: "500", specificAuthor: .theirs).buildPrompt()
check(!kt.contains("WHO AUTHORED IT"), "known-theirs skips the author poll")
check(!kt.contains("CASE A") && !kt.contains("CASE B"), "no case branching")
check(kt.contains("SOMEONE ELSE'S"), "review-only framing")
check(kt.contains("ABSOLUTELY DO NOT touch their branch"), "reviewOnly block present")
check(kt.contains("Anything you post on someone else's PR goes in ONE formal GitHub review"), "leaveReviews block present")
// A reply on an existing thread reacts to what the author said there, never a bare repeat.
check(kt.contains("A reply that disputes the finding without explaining why"), "an unexplained dispute gets a question")
check(kt.contains("judge the explanation, not the finding. If it holds, post nothing more"), "an explanation that holds ends the thread")
check(kt.contains("re-running the same reproduction is not new evidence"), "a repeated repro is not a reply")
check(!km.contains("judge the explanation"), "a no-reviews prompt carries no thread-reply rules")
// The lifecycle, step by step: opened PENDING, filled inside, submitted once.
check(kt.contains("with NO `event` field leaves the review PENDING"), "the review is opened pending")
check(kt.contains("one pending review per person per PR"), "a leftover pending review is handled")
check(kt.contains("it is yours — opened earlier in this run, or left by an earlier one. If its `commit_id` is the PR's current head, keep using it, after deleting any of its comments this run is not to post; otherwise delete it"),
      "a tagged or empty pending review is continued on the current head, replaced on a stale one")
check(kt.contains("right before it, list the pending review's comments again: one that does not open with the attribution tag is mine, added from the browser, so leave the review unsubmitted"),
      "a comment of mine added mid-run keeps the review from being submitted")
check(kt.contains("Post your findings in that review"), "leave-reviews posts the findings in it")
check(!kt.contains("Do NOT post your findings on the PR"), "…and is not told to keep them")
check(kt.contains("If one of them does not open with the `\\[[Diplomat](` attribution tag (whatever model name follows it), that review is my own draft from the browser: stop right there"),
      "my draft is looked for before any review work, and stops the run")
check(kt.contains("was left by an earlier run: you reuse or replace it at (1), and if you end without opening a review, delete it."),
      "a run that opens no review still clears an earlier run's leftover")
check(kt.contains("(1) OPEN, only once your findings are final, right before you fill it"),
      "the review is opened late, so the browser cannot add to it meanwhile")
check(kt.contains("If it has no comments, or every comment opens with the attribution tag, it is yours"),
      "a leftover is recognised whatever model or emptiness it has")
check(kt.contains("Neither rule touches a draft of mine: after a stop, never submit, fill, or delete it."),
      "the closing submit/delete rules never reach my draft")
check(kt.contains("`addPullRequestReviewThread` mutation (`pullRequestReviewId`"),
      "findings are added inside the pending review")
check(kt.contains("`addPullRequestReviewThreadReply` carrying the same `pullRequestReviewId`"),
      "replies on existing threads land inside it too")
check(kt.contains("reviews/<id>/events -f event=COMMENT"), "the pending review is submitted as COMMENT")
check(kt.contains("Never end your turn with the review still PENDING"), "never left pending")
check(kt.contains("nor through the standalone `pulls/<number>/comments` endpoint"),
      "single-comment posting is ruled out")
check(kt.contains("Do NOT mark this PR ready"), "otherNoMarkReady present")
check(kt.contains("that single clean pass ends the loop"), "max-depth fragment present")
// Soft-approve: no hard verdict, but a thank-you COMMENT review — never an APPROVE action.
check(kt.contains("Do NOT submit an APPROVE"), "soft-approve still withholds the APPROVE verdict")
check(kt.contains("Thank you for contributing"), "soft-approve clean thank-you present")
check(kt.contains("never as a top-level issue comment"), "the soft approval is a review, not an issue comment")
check(kt.contains("a COMMENT review carrying that body ONLY"), "the soft approval is a body-only COMMENT review")
check(!kt.contains("top-level comment (not an inline finding)"), "no standalone soft-approve comment")
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
check(kts.contains("delete any pending review you opened"), "a clean silent review leaves nothing pending")
check(!kts.contains("Thank you for contributing"), "soft-approve off ⇒ no thank-you review")
check(!kts.contains("still APPROVE"), "no finalPass block")
print("known-theirs (silent) review prompt assertions passed")

// ---- known-theirs WITH verdict (trusted author: member/maintainer/contributor) ----
// The review-request monitor sets finalPass=true when the PR author is trusted, so the
// auto-review closes with an APPROVE/changes-requested verdict instead of comments only.
// A real verdict outranks the (default-on) soft-approve, so no thank-you review either.
section("known-theirs review prompt (trusted author → verdict)")
let ktv = ReviewConfig(depth: "max", target: .specific, me: "latekvo",
                       markReady: false, leaveReviews: true, replyToReviews: false,
                       specificPR: "500", finalPass: true, specificAuthor: .theirs).buildPrompt()
check(ktv.contains("SOMEONE ELSE'S"), "review-only framing still present with verdict")
check(ktv.contains("still APPROVE"), "trusted author ⇒ finalPass APPROVE-verdict block present")
check(ktv.contains("gets a reply on that thread, under the reply rules above") && !ktv.contains("(\"Still present as of <commit_hash>\"), never a fresh comment"), "finalPass defers to the thread-reply rules")
check(!ktv.contains("Do NOT submit an APPROVE"), "trusted author ⇒ no no-verdict block")
check(!ktv.contains("Thank you for contributing"), "hard verdict outranks soft-approve")
check(ktv.contains("COMMENT unless a verdict instruction below explicitly tells you to APPROVE"),
      "the lifecycle's COMMENT default yields to the verdict block")
check(ktv.contains("such a verdict is this same submission's event, never a second review"),
      "the verdict is the one review's event")
print("known-theirs (trusted author) review prompt assertions passed")

// The lifecycle belongs to a formal review of someone else's PR, and to nothing else.
let ktNoReview = ReviewConfig(depth: "max", target: .specific, me: "latekvo",
                              markReady: false, leaveReviews: false, replyToReviews: false,
                              specificPR: "500", softApprove: false,
                              specificAuthor: .theirs).buildPrompt()
check(!ktNoReview.contains("addPullRequestReviewThread"), "no formal review ⇒ no lifecycle")
check(!ktNoReview.lowercased().contains("every finding goes in as an inline comment")
      && !ktNoReview.lowercased().contains("post every finding"),
      "no formal review ⇒ the closing block asks for none")
check(ktNoReview.contains("Any review you submit on this PR uses the neutral COMMENT event"),
      "…while still ruling out a verdict")
check(ktNoReview.contains("Do NOT post your findings on the PR"), "no formal review ⇒ findings stay unposted")
check(ktNoReview.contains("This outranks every other line here that puts findings in the review"),
      "…over the blocks that elsewhere put findings in the review")
// A soft approval posts a review even with findings kept off the PR, so it gets the lifecycle.
let ktSoftOnly = ReviewConfig(depth: "max", target: .specific, me: "latekvo",
                              markReady: false, leaveReviews: false, replyToReviews: false,
                              specificPR: "500", specificAuthor: .theirs).buildPrompt()
check(ktSoftOnly.contains("with NO `event` field leaves the review PENDING"),
      "a soft approval without leave-reviews still opens a pending review")
check(ktSoftOnly.contains("one pending review per person per PR"),
      "…and meets my draft with the same rule")
check(ktSoftOnly.contains("Do NOT post your findings on the PR")
      && !ktSoftOnly.contains("Post your findings in that review"),
      "…while its findings stay off the PR")
check(other.buildPrompt().contains("with NO `event` field leaves the review PENDING"),
      "a sweep of someone else's PRs gets the lifecycle")
check(!mine.buildPrompt().contains("Do NOT post your findings on the PR"), "my own PRs ⇒ no review blocks at all")
let ktVerdictOnly = ReviewConfig(depth: "max", target: .specific, me: "latekvo",
                                 markReady: false, leaveReviews: false, replyToReviews: false,
                                 specificPR: "500", finalPass: true,
                                 specificAuthor: .theirs).buildPrompt()
check(ktVerdictOnly.contains("with NO `event` field leaves the review PENDING"),
      "a verdict without leave-reviews still goes through the lifecycle")
check(ktVerdictOnly.contains("Post your findings in that review")
      && !ktVerdictOnly.contains("Do NOT post your findings on the PR"),
      "…and posts its findings in it, as the verdict pass requires")
check(!km.contains("addPullRequestReviewThread"), "my own PR ⇒ no lifecycle")

// ---- the after-run check of a review's lifecycle (mirrors test_reviewaudit.py) ----
section("ReviewAudit")
do {
    let since: TimeInterval = 1_760_000_000  // 2025-10-09T08:53:20Z
    let before = "2025-10-09T08:00:00Z", after = "2025-10-09T09:30:00Z"
    let tagged = #"\\[[Diplomat](https://github.com/latekvo/Diplomat) Opus\\]: nit"#
    func review(_ state: String = "COMMENTED", created: String = after, submitted: String = after,
                threads: Int = 0, replies: Int = 0, body: String = tagged,
                commented: String? = nil, extra: [String] = []) -> String {
        let at = commented ?? created
        let comments = Array(repeating: #"{"body":"\#(body)","createdAt":"\#(at)","replyTo":null}"#, count: threads)
            + Array(repeating: #"{"body":"\#(body)","createdAt":"\#(at)","replyTo":{"id":"c"}}"#, count: replies)
            + extra
        let sub = state == "PENDING" ? "null" : "\"\(submitted)\""
        return #"{"state":"\#(state)","createdAt":"\#(created)","submittedAt":\#(sub),"#
            + #""comments":{"nodes":[\#(comments.joined(separator: ","))]}}"#
    }
    func findings(_ reviews: [String], author: String = "bob") -> [String] {
        let json = #"{"author":{"login":"\#(author)"},"reviews":{"nodes":[\#(reviews.joined(separator: ","))]}}"#
        let pr = try! JSONDecoder().decode(ReviewAudit.PullRequest.self, from: Data(json.utf8))
        return ReviewAudit.findings(pr, me: "alice", since: since)
    }
    let pending = "left its review PENDING — never submitted, so only I can see it"
    check(findings([review("PENDING", threads: 2)]) == [pending], "a pending review the run opened")
    check(findings([review("PENDING")]) == [pending], "an empty pending review the run opened")
    check(findings([review("PENDING", created: before)]) == [], "an older empty pending review is mine")
    check(findings([review("PENDING", threads: 2, body: "mine")]) == [],
          "a pending review of mine the run left alone is not its fault")
    check(findings([review(threads: 2), review(threads: 1, body: "mine")]) == [],
          "a review I submit from the browser during the run is no split")
    check(findings([review("PENDING", created: before, threads: 1, replies: 1, body: #"\n "# + tagged,
                           commented: after)])
          == [pending], "an older tagged pending review the run added to is the run's")
    check(findings([review("PENDING", created: before, threads: 2)]) == [],
          "an older tagged pending review the run did not touch is an earlier run's")
    check(findings([review("PENDING", created: before, threads: 2, body: tagged,
                           extra: [#"{"body":"mine","replyTo":null}"#])]) == [],
          "an older pending review with one untagged comment is mine")
    check(findings([review("APPROVED", replies: 1), review("CHANGES_REQUESTED", replies: 2)]) == [],
          "submitted verdict reviews are not pending")
    check(findings([review(threads: 1), review(threads: 1), review(threads: 1)])
          == ["posted its findings as 3 separate reviews instead of one"], "a split is reported")
    check(findings([review(threads: 4, replies: 1), review(replies: 1), review()]) == [],
          "replies and a body-only review open no thread")
    check(findings([review(submitted: before, threads: 1), review(threads: 1)]) == [],
          "reviews before the run are not its own")
    check(findings([review(threads: 1), review(threads: 1)], author: "ALICE") == [],
          "a split on my own PR is not judged")
    check(findings([review("PENDING"), review(threads: 1), review(threads: 2)])
          == [pending, "posted its findings as 2 separate reviews instead of one"], "both together")
    // One machine checks each run: the store the runs it spawned, a node the ones it ran.
    let url7 = "https://github.com/acme/widgets/pull/7"
    func retiring(kind: String = "review", url: String = url7,
                  placement: AgentState.Placement = .local) -> AgentState.RunRecord {
        AgentState.RunRecord(runID: "r", dispatchedAt: since, prNumber: 7, prURL: url, kind: kind,
                             placement: placement)
    }
    check(ReviewAudit.checkedOnRetire(retiring()), "the store checks a review it spawned")
    check(!ReviewAudit.checkedOnRetire(retiring(placement: .meshPeer))
            && !ReviewAudit.checkedOnRetire(retiring(placement: .meshHere)),
          "and leaves one the mesh placed to the node that ran it, even here")
    check(!ReviewAudit.checkedOnRetire(retiring(kind: "conflicts")) && !ReviewAudit.checkedOnRetire(retiring(url: "")),
          "a review run, on a PR")
    check(ReviewAudit.coordinates("https://github.com/acme/widgets/pull/7/").map { "\($0)" }
          == "(owner: \"acme\", repo: \"widgets\", number: 7)", "a PR URL's coordinates")
    for notPR in ["https://github.com/acme/widgets/issues/7", "https://ghe.example.com/acme/widgets/pull/7",
                  "https://github.com/acme/widgets/pull/7/files", ""] {
        check(ReviewAudit.coordinates(notPR) == nil, "no coordinates for \(notPR)")
    }
    // audit asks gh for my reviews on that PR (mirrors test_audit_asks_for_my_reviews_on_that_pr).
    let dir = FileManager.default.temporaryDirectory.appendingPathComponent("smoke-audit-\(getpid())")
    try! FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: dir) }
    let argsFile = dir.appendingPathComponent("args"), gh = dir.appendingPathComponent("gh")
    let answer = #"{"data":{"repository":{"pullRequest":{"author":{"login":"bob"},"reviews":{"nodes":["#
        + review("PENDING") + "]}}}}}"
    try! "#!/bin/sh\nfor a in \"$@\"; do printf '%s\\n' \"$a\"; done > '\(argsFile.path)'\ncat <<'EOF'\n\(answer)\nEOF\n"
        .write(to: gh, atomically: true, encoding: .utf8)
    chmod(gh.path, 0o755)
    setenv("DIPLOMAT_GH", gh.path, 1)
    let got = try? await ReviewAudit.audit(prURL: "https://github.com/acme/widgets/pull/7", me: "alice", since: since)
    unsetenv("DIPLOMAT_GH")
    let asked = ((try? String(contentsOf: argsFile, encoding: .utf8)) ?? "").split(separator: "\n").map(String.init)
    check(got == [pending], "audit judges what gh answers, got: \(String(describing: got))")
    check(asked.first == "api" && asked.dropFirst().first == "graphql"
          && asked.contains { $0.hasPrefix("query=") }
          && asked.contains { $0.contains("reviews(last: 30, author: $me)") },
          "audit runs the my-reviews query")
    for (flag, value) in [("-f", "owner=acme"), ("-f", "name=widgets"), ("-f", "me=alice"), ("-F", "number=7")] {
        check(zip(asked, asked.dropFirst()).contains { $0 == (flag, value) }, "audit passes \(flag) \(value)")
    }
    var unknownLogin = ""
    do { _ = try await ReviewAudit.audit(prURL: "https://github.com/acme/widgets/pull/7", me: "", since: since) }
    catch { unknownLogin = error.localizedDescription }
    let gitlabRun = try? await ReviewAudit.audit(
        prURL: "https://gitlab.example.com/g/sub/p/-/merge_requests/7", me: "", since: since)
    check(unknownLogin == "the gh login is not known yet" && gitlabRun == [],
          "no gh login fails a GitHub PR's check, and says nothing of a merge request's: "
          + "\(unknownLogin) \(String(describing: gitlabRun))")
}
print("ReviewAudit assertions passed")

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

// ---- The auto-review author allowlist ----
section("author allowlist")
// PARITY: the Python twin (autofix.parse_author_allowlist / author_allowed) asserts
// these exact shapes.
check(AuthorAllowlist.parse("").isEmpty, "blank ⇒ no list")
check(AuthorAllowlist.parse("   ").isEmpty, "whitespace ⇒ no list")
check(AuthorAllowlist.parse("alice, @bob  carol") == ["alice", "bob", "carol"],
      "commas, whitespace and a leading @ are all the operator's to use")
check(AuthorAllowlist.parse("@@dave") == ["dave"], "every leading @ comes off")
// One person, two spellings: GitHub logins are case-insensitive, so a list that kept
// both would misreport its own length to the settings row that counts it.
check(AuthorAllowlist.parse("Bob, bob, BOB") == ["Bob"], "first spelling wins, once")
check(AuthorAllowlist.parse("zoe alice") == ["zoe", "alice"], "the operator's order, unsorted")
// Empty ⇒ everyone: an applet never told otherwise reviews what it always did.
check(AuthorAllowlist.allows("anyone", in: []), "no list ⇒ no limit")
check(AuthorAllowlist.allows("", in: []), "no list ⇒ even an unnamed author")
check(AuthorAllowlist.allows("Bob", in: ["bob"]), "membership is case-insensitive")
check(AuthorAllowlist.allows("bob", in: ["BOB"]), "…in both directions")
check(!AuthorAllowlist.allows("carol", in: ["bob"]), "a list is a limit")
// A non-empty list is closed, so a deleted account (which reaches the monitor as "")
// is outside it rather than waved through.
check(!AuthorAllowlist.allows("", in: ["bob"]), "an author GitHub cannot name is outside a list")
print("author allowlist assertions passed")

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
// A run that finished on its own word and posted nothing (soft-approve off on a clean PR,
// or my own draft found on it) answered the request: it is never re-dispatched, however
// long the backoff — while a run that died is still retried.
let concludedRec = ReviewAttempt(requestedAt: "s", lastDispatchedAt: t0.addingTimeInterval(-6 * 60),
                                 attempts: 1, concludedAt: t0.addingTimeInterval(-60))
check(ReviewReconcile.decide(prior: concludedRec, stamp: "s", inFlight: false, banned: false, now: t0)
        == .skipConcluded, "a concluded request is not retried")
check(ReviewReconcile.decide(prior: concludedRec, stamp: "newer-stamp", inFlight: false, banned: false,
                             now: t0.addingTimeInterval(2 * 60 * 60))
        == .dispatch(attemptNumber: 1), "…but a genuine re-request of it is a fresh one")
do {
    let at = t0.timeIntervalSince1970
    func run(_ id: String, kind: String, pr: Int, repo: String = "r") -> AgentState.RunRecord {
        AgentState.RunRecord(runID: id, dispatchedAt: at - 600, prNumber: pr,
                             prURL: "https://github.com/o/\(repo)/pull/\(pr)", kind: "review",
                             ledgerKey: "\(kind):github.com/o/r#\(pr)@abc", pid: 7000 + pr, tty: "ttys0\(pr)")
    }
    let runs = [run("idle", kind: AutofixMesh.kindReviewReq, pr: 11),
                run("ended", kind: AutofixMesh.kindReviewReq, pr: 12),
                run("gone", kind: AutofixMesh.kindReviewReq, pr: 13),
                run("reply", kind: AutofixMesh.kindReviewReply, pr: 14),
                run("session", kind: AutofixMesh.kindReviewReq, pr: 15),
                run("refused", kind: AutofixMesh.kindReviewReq, pr: 16)]
    let alive = Dictionary(uniqueKeysWithValues: [15, 16].map {
        (7000 + $0, AgentState.ProcInfo(tty: "ttys0\($0)", elapsed: 600, isAgent: true))
    })
    let verdicts = AgentState.resolve(records: runs, evidence: AgentState.Evidence(
        processes: .present(alive), sentinels: .present([]), tails: .present([:]),
        claims: .present([]), mergedPRs: .present([]),
        sessions: .present(["session": AgentState.SessionState(busy: false),
                            "refused": AgentState.SessionState(busy: false, failure: "402")]),
        activity: .present(["idle": AgentState.TurnReport(verb: .idle, at: at - 5),
                            "ended": AgentState.TurnReport(verb: .ended, at: at - 5),
                            "reply": AgentState.TurnReport(verb: .idle, at: at - 5)])), now: at)
    check(ReviewReconcile.concluded(runs, verdicts: verdicts) == ["o/r#11", "o/r#15"],
          "only a review-request run its own runner reported over concludes, got "
          + "\(ReviewReconcile.concluded(runs, verdicts: verdicts).sorted())")
    // A turn its provider refused did none of the work: the run failed, and says why.
    check(verdicts["refused"]?.state == .failed
            && verdicts["refused"]?.reason.hasSuffix("its runner reported the turn failed: 402") == true,
          "a refused turn is a failure, got \(String(describing: verdicts["refused"]))")
    check(verdicts["session"]?.state == .finished, "a session that went idle well finished")
    let book = ["o/r#11": a1old, "o/r#13": a1old, "o/other#11": a1old]
    let stamped = ReviewReconcile.conclude(book, prs: ["o/r#11", "o/r#15"], at: t0)
    check(stamped["o/r#11"]?.concludedAt == t0 && stamped["o/r#13"]?.concludedAt == nil
            && stamped["o/r#15"] == nil && stamped["o/other#11"]?.concludedAt == nil,
          "a conclusion stamps the record it answers, in its own repo only, and invents none")
    let otherRepo = [run("idle", kind: AutofixMesh.kindReviewReq, pr: 11, repo: "other")]
    check(ReviewReconcile.concluded(otherRepo, verdicts: verdicts) == ["o/other#11"],
          "a run concludes the request of its own repo's #11")
    let late = t0.addingTimeInterval(ReviewReconcile.retryMaxBackoff + 60)
    check(ReviewReconcile.retained(stamped, owed: ["o/r#11": "s", "o/r#13": "s"], now: late).keys.sorted()
            == ["o/r#11"],
          "a concluded record outlives the backoff ceiling while its request is still owed")
    check(ReviewReconcile.retained(stamped, owed: ["o/r#11": "newer-stamp"], now: late).isEmpty,
          "…and is dropped once that request is no longer the one owed")
}
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
check(ApiErrorMatch.looksLikeApiError("API Error: something, see status.claude.com for details"))
// The banner has to OPEN a line. Quoted mid-sentence it is prose, not a stall.
check(!ApiErrorMatch.looksLikeApiError("the predicate returns true on ⏺ API Error: Connection error."))
check(!ApiErrorMatch.looksLikeApiError("quoting API Error: 529 Overloaded in a sentence"))
// An agent's prose is not always Latin, and it disqualifies a line just the same:
// `[^A-Za-z]` would read 这是 as decoration and nudge the session that wrote it.
check(!ApiErrorMatch.looksLikeApiError("这是 API Error: Connection error. 的例子"))
// A line that merely NAMES the banner is not one either: the CLI always prints the colon,
// and without it "API Error" is just two words a doc line can start with.
check(!ApiErrorMatch.looksLikeApiError(
    "⏺ Notes on the transport layer:\n  - API Error is surfaced verbatim to the caller\n"
    + "  - the client retries on a transient network\n    error before giving up"))
// …but decoration in front of it is not prose: the transcript bullet, the tool-result
// elbow, a pane's left border and a log timestamp all precede a real banner. Only LETTERS
// disqualify a line — prose reaches a quoted banner through words.
check(ApiErrorMatch.looksLikeApiError("  ⎿  API Error: Connection error."))
check(ApiErrorMatch.looksLikeApiError("│ ⏺ API Error: 503 Service Unavailable"))
// Casing is the CLI's to change; the watcher does not key on it — and each rule carries
// its own flag, so the coded one needs its own lowercase banner.
check(ApiErrorMatch.looksLikeApiError("⏺ api error: connection error."))
check(ApiErrorMatch.looksLikeApiError("⏺ api error: 529 overloaded."))
// Codeless on purpose: a 3-digit code would match through the code rule instead and leave
// the digits-are-decoration intent unpinned.
check(ApiErrorMatch.looksLikeApiError("21:28:22 API Error: Connection error."))
// The banner is never the first line of a real tail — it sits under the transcript and
// over the prompt box. Both anchored rules need a tail of this shape to pin that `^` is
// per LINE and not per tail; the codeless one cannot reach the code rule.
check(ApiErrorMatch.looksLikeApiError(
    "⏺ Running the suite…\n⏺ API Error: Connection error.\n  ? for shortcuts"))
check(ApiErrorMatch.looksLikeApiError(
    "⏺ Running the suite…\n⏺ API Error: 500 Internal Server Error\n  ? for shortcuts"))
// The rejoin fuses every adjacent pair of rows, so suppression reads the ORIGINAL: off
// the rejoined copy, two lines of ordinary prose assemble into a limit banner and silence
// a session that really is stalled, with no second chance at it.
check(ApiErrorMatch.looksLikeApiError(
    "⏺ The dispatcher stops as soon as the workspace token budget\n  exceeded the daily cap.\n"
    + "⏺ API Error: 529 Overloaded. If it persists, check https://status.claude.com."))
check(ApiErrorMatch.looksLikeApiError(
    "⏺ The client backs off long before you hit your\n  limit, so nothing is dropped.\n"
    + "⏺ API Error: Connection error."))
// The contiguous phrase list fuses the same way the two gap regexes above do.
check(ApiErrorMatch.looksLikeApiError(
    "⏺ The quota family is matched on the phrase the limit will reset\n"
    + "  at whatever hour the window rolls over.\n⏺ API Error: Connection error."))
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
// These banners run 70-90 columns, so a narrow pane wraps them mid-phrase. The evidence
// is read off a copy with the wrapping rejoined, so the split must not hide the banner.
check(ApiErrorMatch.looksLikeApiError(
    "⏺ API Error: Your computer went to sleep mid-response. The response above may be\n  incomplete."))
check(ApiErrorMatch.looksLikeApiError(
    "⏺ API Error: Connection lost before a response was\n  produced. Try again."))
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
// Its line, not the tail's first line — this arm needs its own mid-tail fixture, since it
// shares no pattern with the two "API Error" rules.
check(ApiErrorMatch.looksLikeApiError(
    "⏺ Running the suite…\n✗ 429 Rate limited · retrying in 34s\n  ? for shortcuts"))
// Its rate-limit context wraps like any other banner's, so it reads the rejoined copy.
check(ApiErrorMatch.looksLikeApiError("⏺ 429 Rate\n  limited · retrying in 34s"))
// But a 429 an agent WROTE is not a banner, however much rate-limit context surrounds it.
// This arm carries no "API Error:" prefix to anchor, so the code itself has to open the
// line — otherwise it is the widest hole in the predicate, matching any tail that mentions
// both a 429 and a rate limit anywhere.
check(!ApiErrorMatch.looksLikeApiError("got a 429 rate limit, retrying"))
check(!ApiErrorMatch.looksLikeApiError("    if resp.status_code == 429:  # rate limit, back off"))
check(!ApiErrorMatch.looksLikeApiError("quoting API Error: 429 Rate limit exceeded in a sentence"))
check(!ApiErrorMatch.looksLikeApiError("这是 429 rate limit 的例子"))
// But a 429 rate-limit co-occurring with a quota banner still idles on the quota.
check(!ApiErrorMatch.looksLikeApiError("429 Rate limited\nYou've hit your weekly limit."))
// And a bare 429 without a rate-limit phrase (e.g. a line count) must NOT trip it —
// including one that opens its line, which the anchor alone would wave through.
check(!ApiErrorMatch.looksLikeApiError("Deleted 429 stale entries"))
check(!ApiErrorMatch.looksLikeApiError("429 stale entries pruned from the cache"))
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
check(AuditCategory.of(action: "comms-ban") == .bans, "a Comms ban line is a Bans line")
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
check(AuditCategory.of(action: "mesh-stop") == .mesh, "stopping a node left running is a Mesh row")
for verb in ["comms-ping", "comms-review", "comms-fix", "comms-bump", "comms-investigate",
             "comms-error", "comms-recovered", "comms-notice", "comms-reply"] {
    check(AuditCategory.of(action: verb) == .comms, "\(verb) is a Comms row")
}
check(AuditCategory.displayOrder.firstIndex(of: .comms) == AuditCategory.displayOrder.firstIndex(of: .mesh)! + 1
      && AuditCategory.comms.title == "Comms"
      && AuditCategory.comms.symbol == "bubble.left.and.bubble.right.fill",
      "the Comms chip sits after Mesh")
// audit-categories.json is the Linux applet's twin of this enum.
do {
    let url = try CoreAssets.assetsDir().appendingPathComponent("audit-categories.json")
    let obj = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any]
    let ids = (obj?["categories"] as? [[String: Any]])?.compactMap { $0["id"] as? String } ?? []
    check(ids == AuditCategory.displayOrder.map(\.rawValue),
          "audit-categories.json lists the Swift categories in order: \(ids)")
    let actions = obj?["actions"] as? [String: String] ?? [:]
    for (verb, cat) in actions {
        check(AuditCategory.of(action: verb).rawValue == cat, "\(verb) maps the same on both sides")
    }
}
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

try commsSmoke()
print("comms assertions passed")

try await tasksSmoke()
try await goalsSmoke()
print("tasks assertions passed")
try await gitlabSmoke()

// ---- Golden prompts (cross-platform parity) ----
// Every prompt mode both front-ends can assemble is compared byte-for-byte against a
// committed golden file in assets/golden-prompts/. The Linux tests assert the SAME
// files, so Swift and Python can only drift from each other by failing one CI job.
// Regenerate after an intentional core/*.json change: DIPLOMAT_GOLDEN_WRITE=1 swift run
// DiplomatCoreSmoke.
section("golden prompts")
let goldenMe = "testuser"
// A GitLab home: the one input the GitLab goldens add to their GitHub twins.
let goldenGitLab = PRRef.LocalRepo(owner: "group/sub", repo: "proj", path: "/w/proj",
                                   host: "gitlab.example.com")
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
    // The two shapes a Fix-issues run comes in. A scope never reaches the agent — it
    // is enumerated into one queued fix per issue — so the six scopes are one golden
    // between them, taken through `forIssue` as the sweep queues them.
    ("issues-swept", IssueConfig(me: goldenMe).forIssue(421).buildPrompt()),
    ("issues-single", IssueConfig(target: .specific, me: goldenMe,
                                  specificIssue: "421").buildPrompt()),
    // Every action toggle off — the one golden that holds the complementary blocks
    // (no-PRs instead of open-PRs, no unassigned re-check, no claim, no attribution tag).
    ("issues-hands-off", IssueConfig(me: goldenMe, unassignedOnly: false,
                                     assignToMe: false, openPRs: false,
                                     commentOnIssue: false).forIssue(421).buildPrompt()),
    ("issues-features-max", IssueConfig(depth: "max", me: goldenMe,
                                        includeFeatures: true).forIssue(421).buildPrompt()),
    ("investigate", commsGoldenInvestigate.buildPrompt()),
    ("comms-classifier", CommsClassifier.prompt(settings: CommsSettings(
        owner: "software-mansion", repo: "argent", login: goldenMe))),
    ("comms-classifier-multi", CommsClassifier.prompt(settings: CommsSettings(
        repos: ["software-mansion/argent", "latekvo/diplomat"], login: goldenMe))),
    ("review-single-gitlab", { var c = ReviewConfig(depth: "max", target: .specific, me: goldenMe,
                                                    specificPR: "337")
                               c.home = goldenGitLab
                               return c.buildPrompt() }()),
    ("issues-single-gitlab", { var c = IssueConfig(target: .specific, me: goldenMe, specificIssue: "421")
                               c.home = goldenGitLab
                               return c.buildPrompt() }()),
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

// ---- GH: a gh that never answers is given up on ----
// The Linux twin's gh.run carries the same 60 s budget. A stub that sleeps stands in
// for the stalled response, with the deadline shortened to a second; DIPLOMAT_GH names
// it.
do {
    let stub = FileManager.default.temporaryDirectory.appendingPathComponent("smoke-gh-\(getpid()).sh")
    // exec, so the process the deadline signals is the one that sleeps.
    try "#!/bin/sh\nexec sleep 30\n".write(to: stub, atomically: true, encoding: .utf8)
    chmod(stub.path, 0o755)
    defer { try? FileManager.default.removeItem(at: stub) }
    setenv("DIPLOMAT_GH", stub.path, 1)
    let started = Date()
    var outcome = "returned"
    do { _ = try await GH.run(["api", "/"], timeout: 1) }
    catch GHError.timeout(let s) { outcome = "timeout \(Int(s))" }
    catch { outcome = "other: \(error)" }
    check(outcome == "timeout 1", "a stalled gh is reported as a timeout, got: \(outcome)")
    check(Date().timeIntervalSince(started) < 10, "the deadline is what ends the wait, not the stub")
    unsetenv("DIPLOMAT_GH")
}

// ---- API: the panel's open PRs are read a page at a time (mirrors test_open_prs.py) ----
// The stub answers the page `<owner>-<after>.json` names (`-.json` for the first) and
// logs each call's cursor; a page it has no file for fails the call.
do {
    let dir = FileManager.default.temporaryDirectory.appendingPathComponent("smoke-prs-\(getpid())")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: dir) }
    let gh = dir.appendingPathComponent("gh"), log = dir.appendingPathComponent("log")
    try """
    #!/bin/sh
    owner=; after=
    for a in "$@"; do case "$a" in owner=*) owner="${a#owner=}";; after=*) after="${a#after=}";; esac; done
    echo "$owner-$after" >> '\(log.path)'
    exec cat "\(dir.path)/$owner-$after.json"
    """.write(to: gh, atomically: true, encoding: .utf8)
    chmod(gh.path, 0o755)
    func page(_ owner: String, _ after: String, _ number: Int, next: String?) throws {
        let pr = #"{"number":\#(number),"title":"t","url":"u","isDraft":false,"createdAt":"2026-01-01T00:00:00Z","#
            + #""reviewDecision":null,"mergeable":"MERGEABLE","author":{"login":"a"},"files":{"nodes":[]},"#
            + #""timelineItems":{"nodes":[]},"reviewThreads":{"nodes":[]}}"#
        let info = next.map { #"{"hasNextPage":true,"endCursor":"\#($0)"}"# } ?? #"{"hasNextPage":false,"endCursor":"end"}"#
        try #"{"data":{"repository":{"pullRequests":{"pageInfo":\#(info),"nodes":[\#(pr)]}}}}"#
            .write(to: dir.appendingPathComponent("\(owner)-\(after).json"), atomically: true, encoding: .utf8)
    }
    for i in 1...5 { try page("many", i == 1 ? "" : "c\(i - 1)", i, next: "c\(i)") }
    try page("two", "", 1, next: "c1")
    try page("two", "c1", 2, next: nil)
    setenv("DIPLOMAT_GH", gh.path, 1)
    defer { unsetenv("DIPLOMAT_GH") }
    func calls() -> [String] {
        defer { try? FileManager.default.removeItem(at: log) }
        return ((try? String(contentsOf: log, encoding: .utf8)) ?? "").split(separator: "\n").map(String.init)
    }

    let many = try await API.fetchOpenPRs(PRRef.LocalRepo(owner: "many", repo: "r", path: "")).map(\.number)
    check(many == [1, 2, 3, 4], "four pages, in order, and no fifth, got: \(many)")
    let manyCalls = calls()
    check(manyCalls == ["many-", "many-c1", "many-c2", "many-c3"],
          "each page asks after the one before, got: \(manyCalls)")
    let two = try await API.fetchOpenPRs(PRRef.LocalRepo(owner: "two", repo: "r", path: "")).map(\.number)
    let twoCalls = calls()
    check(two == [1, 2] && twoCalls == ["two-", "two-c1"],
          "the last page ends the reading, got: \(two) after \(twoCalls)")
    print("open PRs paged ok")
}

// ---- GH: the login shell's answer is read without waiting on what it left behind ----
// A profile that starts a job in the background leaves stdout held after the shell
// exits; the lookup reads a file rather than draining a pipe, so the answer is back
// when the shell is. Darwin only: corelibs learns of the exit from a socket that job
// inherits too, so there the lookup waits for the job either way.
#if canImport(Darwin)
do {
    let dir = FileManager.default.temporaryDirectory.appendingPathComponent("smoke-ghshell-\(getpid())")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: dir) }
    let gh = dir.appendingPathComponent("gh")
    try "#!/bin/sh\necho stub\n".write(to: gh, atomically: true, encoding: .utf8)
    chmod(gh.path, 0o755)
    let stub = dir.appendingPathComponent("shell.sh")
    try "#!/bin/sh\nsleep 8 &\necho \(gh.path)\n".write(to: stub, atomically: true, encoding: .utf8)
    chmod(stub.path, 0o755)
    let shell = ProcessInfo.processInfo.environment["SHELL"]
    setenv("SHELL", stub.path, 1)
    defer { if let shell { setenv("SHELL", shell, 1) } else { unsetenv("SHELL") } }
    let started = Date()
    var found = "nil"
    do { found = try GH.loginShellWhichGH() ?? "nil" } catch { found = "\(error)" }
    let elapsed = Date().timeIntervalSince(started)
    check(found == gh.path, "the gh the login shell names is the answer, got: \(found)")
    check(elapsed < 4, "read once the shell exits, not once the job it backgrounded does (\(elapsed)s)")
    print("login shell answered in \(String(format: "%.2f", elapsed))s beside a job it left running")
}
#else
print("corelibs: a job the login shell backgrounds holds its exit socket, so the read's shape is not observable here")
#endif

// ---- GH: a login shell that never answers is given up on, then killed ----
// SHELL names a stub that ignores the terminate and sleeps on under its own pid, so
// only the lookup's five-second deadline can end the wait, and only the kill armed
// five seconds behind the terminate can remove the stub.
let stalledShell = FileManager.default.temporaryDirectory.appendingPathComponent("smoke-shell-\(getpid()).sh")
let stalledPidFile = FileManager.default.temporaryDirectory.appendingPathComponent("smoke-shell-\(getpid()).pid")
try "#!/bin/sh\ntrap '' TERM\necho $$ > \(stalledPidFile.path)\nexec sleep 30\n"
    .write(to: stalledShell, atomically: true, encoding: .utf8)
chmod(stalledShell.path, 0o755)
defer {
    try? FileManager.default.removeItem(at: stalledShell)
    try? FileManager.default.removeItem(at: stalledPidFile)
}
func alive(_ pid: Int32) -> Bool { kill(pid, 0) == 0 }
let stalledPid: Int32
let stalledAt: Date
do {
    let shell = ProcessInfo.processInfo.environment["SHELL"]
    setenv("SHELL", stalledShell.path, 1)
    defer { if let shell { setenv("SHELL", shell, 1) } else { unsetenv("SHELL") } }
    let started = Date()
    var outcome = "returned", advice = ""
    do { _ = try GH.loginShellWhichGH() }
    catch GHError.loginShellStalled(let shell, let seconds) {
        outcome = "stalled \(shell == stalledShell.path ? "by this shell" : shell) after \(Int(seconds))s"
        advice = GHError.loginShellStalled(shell: shell, seconds: seconds).errorDescription ?? ""
    }
    catch { outcome = "other: \(error)" }
    let elapsed = Date().timeIntervalSince(started)
    check(outcome == "stalled by this shell after 5s",
          "a login shell that never answers is reported as stalled, got: \(outcome)")
    check(advice.contains(stalledShell.path) && advice.contains("DIPLOMAT_GH"),
          "what the operator reads names the shell and the way out, got: \(advice)")
    check(elapsed >= 4.5 && elapsed < 9,
          "the lookup's own deadline ends the wait, not the stub's death (\(Int(elapsed))s)")
    stalledPid = Int32((try? String(contentsOf: stalledPidFile, encoding: .utf8))?
        .trimmingCharacters(in: .whitespacesAndNewlines) ?? "") ?? 0
    check(stalledPid > 0, "the stub recorded its pid")
    check(alive(stalledPid), "the stub ignores the terminate the deadline sends")
    stalledAt = started
    print("stalled login shell given up on after \(Int(elapsed))s")
}

// ---- GH: where the login shell is the only source, its stall is what a call reports ----
// Where no candidate path holds gh (the Linux CI container), a call reaches the
// lookup, and a stall must not read as gh being absent. Nothing is cached yet: the
// override is read before the cache, and the lookups above were called directly.
let ghAtCandidatePath = GH.candidatePaths.contains(where: { FileManager.default.isExecutableFile(atPath: $0) })
if ghAtCandidatePath {
    print("gh at a candidate path: a call never reaches the login shell here, so what it makes of the lookup is untested")
} else {
    let shell = ProcessInfo.processInfo.environment["SHELL"]
    setenv("SHELL", stalledShell.path, 1)
    defer { if let shell { setenv("SHELL", shell, 1) } else { unsetenv("SHELL") } }
    var outcome = "returned"
    do { _ = try await GH.run(["--version"]) }
    catch GHError.loginShellStalled(let shell, _) { outcome = shell == stalledShell.path ? "stalled" : "stalled by \(shell)" }
    catch GHError.ghNotFound { outcome = "not found" }
    catch { outcome = "other: \(error)" }
    check(outcome == "stalled", "a stalled login shell is reported as the stall, not as no gh, got: \(outcome)")
    print("a call through a stalled login shell reports the stall")
}

// ---- GH: a cached gh that no longer launches is looked for again ----
// The path the login shell answers is kept for the process, so the gh at it moving
// or being uninstalled would otherwise fail every call until the app restarts. The
// shell stub answers whichever of two stub ghs exists.
if !ghAtCandidatePath {
    let dir = FileManager.default.temporaryDirectory.appendingPathComponent("smoke-ghcache-\(getpid())")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: dir) }
    let first = dir.appendingPathComponent("first"), second = dir.appendingPathComponent("second")
    func install(_ url: URL) throws {
        try "#!/bin/sh\necho \(url.lastPathComponent)\n".write(to: url, atomically: true, encoding: .utf8)
        chmod(url.path, 0o755)
    }
    let stub = dir.appendingPathComponent("shell.sh")
    try "#!/bin/sh\nfor p in \(first.path) \(second.path); do [ -x \"$p\" ] && { echo \"$p\"; exit 0; }; done\nexit 1\n"
        .write(to: stub, atomically: true, encoding: .utf8)
    chmod(stub.path, 0o755)
    let shell = ProcessInfo.processInfo.environment["SHELL"]
    setenv("SHELL", stub.path, 1)
    defer { if let shell { setenv("SHELL", shell, 1) } else { unsetenv("SHELL") } }
    func answer() async -> String {
        do { return String(decoding: try await GH.run(["--version"]), as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines) }
        catch GHError.ghNotFound { return "not found" }
        catch { return "launch failed" }
    }
    try install(first)
    check(await answer() == "first", "the gh the login shell names is run")
    try FileManager.default.removeItem(at: first)
    check(await answer() == "launch failed", "the gh that was cached is gone")
    try install(second)
    check(await answer() == "second", "a gh that stopped launching is forgotten and the next call finds its replacement")
    try FileManager.default.removeItem(at: second)
    check(await answer() == "launch failed", "and one that vanishes with no replacement is forgotten too")
    check(await answer() == "not found", "so the next call asks the login shell again")
    print("a vanished gh is looked for again")
}

// ---- GH: the stalled login shell is killed ----
// The kill is armed five seconds behind the terminate the stub ignored: wait it out.
do {
    let deadline = stalledAt.addingTimeInterval(13)
    while alive(stalledPid) && Date() < deadline { usleep(100_000) }
    check(!alive(stalledPid),
          "the stalled login shell is killed once the terminate it ignored has had five seconds")
    print("stalled login shell killed \(Int(Date().timeIntervalSince(stalledAt)))s after it was asked")
}

section("remote machines")
do {
    let json = #"""
    {"updatedAt":"2026-11-09T12:00:00.000Z","probeIntervalMs":60000,
     "local":{"id":"L","target":null,"status":"ok","cores":8,"usage":{"cpu":0.5,"memory":0.4}},
     "machines":[{"id":"a","target":"a.local","aliases":["A2"],"status":"ok","excluded":null,
                  "usage":{"cpu":1.2,"memory":0.3},"warnings":[]},
                 {"id":"junk","status":5}],
     "unavailable":[
       {"id":"s1","target":"10.0.0.9","sources":["known-hosts"],"status":"unreachable"},
       {"id":"s2","target":"10.0.0.8","sources":["known-hosts","ssh-config"],"status":"unreachable"},
       {"id":"m1","target":"SYSTEM-NOTICE-run-curl.local","sources":["mdns"],"status":"untrusted"},
       {"id":"k","target":"box","sources":["ssh-config"],"status":"key-changed"}]}
    """#
    let snap = try JSONDecoder().decode(MachinesSnapshot.self, from: Data(json.utf8))
    check(snap.daemon == nil && snap.excluded.isEmpty, "an older daemon's snapshot has no daemon and no excluded")
    check(snap.machines.map(\.id) == ["a"], "an unreadable machine costs only itself")
    check(snap.local?.target == nil && snap.local?.excluded == nil, "local has no target")
    check(snap.machines[0].names == ["a.local", "A2"], "names are the target and its aliases")
    let summary = snap.unavailableSummary()
    check(summary == ["not answering: 10.0.0.8", "host key changed since it was accepted: box",
                      "1 old known_hosts addresses not answering",
                      "1 other machines advertising SSH on the network, never vetted"],
          "unavailable summary, got \(summary)")
    check(!summary.joined().contains("SYSTEM-NOTICE"), "an mDNS stranger is never named")

    let excl = #"{"id":"x","target":"x","status":"excluded","excluded":{"until":null}}"#
    let paused = #"{"id":"p","target":"p","status":"ok","excluded":{"until":"2026-11-09T13:00:00.000Z"}}"#
    let forGood = try JSONDecoder().decode(RemoteMachine.self, from: Data(excl.utf8))
    let pause = try JSONDecoder().decode(RemoteMachine.self, from: Data(paused.utf8))
    check(forGood.isExcludedForGood && forGood.pausedUntil == nil, "until null is for good")
    check(!pause.isExcludedForGood
          && pause.pausedUntil == MachinesSnapshot.date("2026-11-09T13:00:00Z"), "until a time is a pause")

    check(UsageTone.cpu(1.01) == .over && UsageTone.cpu(1) == .high && UsageTone.cpu(0.74) == .normal)
    check(UsageTone.memory(0.91) == .over && UsageTone.memory(0.9) == .high
          && UsageTone.memory(0.75) == .high && UsageTone.memory(0.7) == .normal)

    let now = MachinesSnapshot.date("2026-11-09T12:00:00Z")!
    let base = MachineExclusions(exclude: ["Other"], until: [
        "a2": "2026-11-09T18:00:00.000Z", "gone": "2026-11-09T11:00:00.000Z",
        "later": "2026-11-10T00:00:00.000Z"])
    let names = ["a.local", "A2"]
    let p = base.applying(.pause(3600), target: "a.local", names: names, now: now)
    check(p.until == ["a.local": "2026-11-09T13:00:00.000Z", "a2": "2026-11-09T18:00:00.000Z",
                      "later": "2026-11-10T00:00:00.000Z"] && p.exclude == ["Other"],
          "pause sets the target's time and drops expired, got \(p)")
    let e = base.applying(.exclude, target: "a.local", names: names, now: now)
    check(e.exclude == ["Other", "a.local"] && e.until == ["later": "2026-11-10T00:00:00.000Z"],
          "exclude appends and clears every name's pause, got \(e)")
    check(e.applying(.exclude, target: "A.LOCAL", names: names, now: now).exclude == ["Other", "a.local"],
          "exclude never adds a name twice")
    let r = base.applying(.resume, target: "a.local", names: names, now: now)
    check(r.until == ["later": "2026-11-10T00:00:00.000Z"] && r.exclude == ["Other"], "resume, got \(r)")
    let i = MachineExclusions(exclude: ["Other", "A.local", "a2"], until: ["A.LOCAL": "2026-11-10T00:00:00.000Z"])
        .applying(.include, target: "a.local", names: names, now: now)
    check(i.exclude == ["Other"] && i.until.isEmpty, "include clears both, without case, got \(i)")

    // `excluded.by` can name a machine by an entry that is neither its target nor an alias.
    check(snap.unavailableLine() == "Not usable (4) - not answering: 10.0.0.8; host key changed since "
          + "it was accepted: box; 1 old known_hosts addresses not answering; 1 other machines "
          + "advertising SSH on the network, never vetted.", "unavailable line, got \(snap.unavailableLine() ?? "nil")")
    let none = try JSONDecoder().decode(MachinesSnapshot.self, from: Data(#"{"machines":[]}"#.utf8))
    check(none.unavailableLine() == nil, "no unavailable line without unavailable machines")
    check(forGood.excluded?.by == [], "an exclusion without by names nothing")
    let byJSON = #"""
    {"id":"b","target":"build-linux","status":"excluded","error":"excluded in Diplomat",
     "excluded":{"until":null,"by":["build-linux","10.0.0.21"]}}
    """#
    let byIP = try JSONDecoder().decode(RemoteMachine.self, from: Data(byJSON.utf8))
    let inc = MachineExclusions(exclude: ["Other", "10.0.0.21", "BUILD-LINUX"],
                                until: ["10.0.0.21": "2026-11-10T00:00:00.000Z"])
        .applying(.include, target: "build-linux", names: byIP.names, now: now)
    check(inc.exclude == ["Other"] && inc.until.isEmpty, "include drops the by entries, got \(inc)")
    let pausedByJSON = #"""
    {"id":"q","target":"spare.local","status":"unreachable","error":"timed out",
     "excluded":{"until":"2026-11-09T13:00:00.000Z","by":["Spare"]}}
    """#
    let pausedBy = try JSONDecoder().decode(RemoteMachine.self, from: Data(pausedByJSON.utf8))
    let pausedConfig = MachineExclusions(exclude: [], until: ["spare": "2026-11-09T13:00:00.000Z"])
    let res = pausedConfig.applying(.resume, target: "spare.local", names: pausedBy.names, now: now)
    check(res.until.isEmpty, "resume drops a pause entered under another name, got \(res)")
    let ex = pausedConfig.applying(.exclude, target: "spare.local", names: pausedBy.names, now: now)
    check(ex.exclude == ["spare.local"] && ex.until.isEmpty, "exclude drops the by pause, got \(ex)")
    check(pausedBy.problem == "⚠ unreachable: timed out" && byIP.problem == nil
          && snap.machines[0].problem == nil, "a machine that could not be read says why")

    let unparsed = MachineExclusions(exclude: [], until: ["odd": "next tuesday", "gone": "2026-11-09T11:00:00.000Z"])
        .applying(.pause(60), target: "x", names: [], now: now)
    check(unparsed.until["odd"] == "next tuesday" && unparsed.until["gone"] == nil,
          "an unparseable pause time is kept, an expired one dropped, got \(unparsed)")

    // Each edit is one key, the list first; an emptied key is removed, the rest kept.
    let cfgIn: [String: Any] = ["repoRoot": "/r", MachineExclusions.excludeKey: ["a.local"],
                                MachineExclusions.untilKey: ["a2": "2026-11-10T00:00:00.000Z"]]
    let before = MachineExclusions(config: cfgIn)
    check(before == MachineExclusions(exclude: ["a.local"], until: ["a2": "2026-11-10T00:00:00.000Z"]),
          "exclusions read from config, got \(before)")
    let after = before.applying(.include, target: "a.local", names: names, now: now)
    let edits = after.edits(from: before)
    check(edits.count == 2, "two keys changed, got \(edits.count)")
    let first = edits[0](cfgIn)
    check(first[MachineExclusions.excludeKey] == nil && first[MachineExclusions.untilKey] != nil,
          "the list is written first, and an empty list removes its key")
    let written = edits.reduce(cfgIn) { $1($0) }
    check(written.keys.sorted() == ["repoRoot"], "both keys removed, others kept, got \(written)")
    let paused2 = before.applying(.pause(3600), target: "b", names: [], now: now)
    let pauseEdits = paused2.edits(from: before)
    check(pauseEdits.count == 1
          && (pauseEdits[0](cfgIn)[MachineExclusions.untilKey] as? [String: String])?["b"]
             == "2026-11-09T13:00:00.000Z", "a pause writes only the pause map")
    // The ARM bias reads as the daemon's `settings()` + `byRank` do: default 70, clamped to 0-100.
    check(MachineArmBias.key == "remoteMachineArmBias", "the ARM bias key is the daemon's")
    let biasCases: [(Double, Int)] = [
        (70, 70), (0, 0), (100, 100), (35, 35), (-20, 0), (250, 100), (1e300, 100),
        (72.4, 72), (72.5, 73), (.nan, 70), (.infinity, 70), (-.infinity, 70),
    ]
    for (v, want) in biasCases {
        check(MachineArmBias.percent(v) == want, "percent(\(v)) = \(MachineArmBias.percent(v)), want \(want)")
    }
    check(MachineArmBias.defaultPercent == 70 && MachineArmBias.range == 0...100 && MachineArmBias.step == 5,
          "the ARM bias default, range and step match the Linux slider")
    check(MachineArmBias.badge(70) == "ARM 70 : 30 x86" && MachineArmBias.badge(0) == "ARM 0 : 100 x86",
          "ARM bias badge, got \(MachineArmBias.badge(70))")
    check(MachinesScan.body == ["full": true], "a scan asks for discovery and every probe, with no ownerPid")
    check(MachinesScan.auditText(failure: nil) == "Rescanned the remote machines"
          && MachinesScan.auditText(failure: "HTTP 500")
             == "Rescanned the remote machines FAILED: HTTP 500",
          "a scan's audit line matches Linux's and carries why it failed")

    let agoCases: [(Double, String)] = [
        (-5, "0 s ago"), (119.4, "119 s ago"), (119.5, "2 min ago"), (150, "3 min ago"),
        (5370, "90 min ago"), (9000, "3 h ago"), (5.5 * 3600, "6 h ago"),
        (2.5 * 86400, "3 d ago"),
    ]
    for (s, want) in agoCases {
        check(MachinesText.ago(seconds: s) == want, "ago(\(s)) = \(MachinesText.ago(seconds: s)), want \(want)")
    }
    let pctCases: [(Double, String)] = [(0.125, "13%"), (0.994, "99%"), (-0.005, "0%"), (1.2, "120%")]
    for (v, want) in pctCases {
        check(MachinesText.percent(v) == want, "percent(\(v)) = \(MachinesText.percent(v)), want \(want)")
    }

    let hist = #"{"version":1,"intervalMs":60000,"machines":{"a":[[0,0.2,0.4,1],[60000,0.4,0.6,3],[120000,null,0.5,null],[600000,1.5,0.5,0]]}}"#
    let h = try JSONDecoder().decode(MachineHistory.self, from: Data(hist.utf8))
    let cols = MachineHistory.columns(h.machines["a"]!, from: 0, to: 600_000, width: 5, gapMs: 180_000)
    check(cols.map(\.index) == [0, 1, 4], "columns, got \(cols)")
    check(cols[0].cpu.map { abs($0 - 0.3) < 1e-9 } == true && cols[0].ssh == 3 && cols[0].breaksBefore,
          "a column is the mean of cpu and the max of ssh, got \(cols[0])")
    check(cols[1].cpu == nil && cols[1].memory == 0.5 && !cols[1].breaksBefore, "null stays null")
    check(cols[2].breaksBefore, "a gap past three intervals breaks the line")
    print("snapshot, exclusions and history columns ok")
}

// ---- GH: the override is read per call ----
// With the stub deleted and DIPLOMAT_GH unset, the next call reaches the gh the box has
// (the Linux CI container has none), never the stub's path; the live dump below goes
// through the same lookup.
do {
    var found = ""
    do { found = String(decoding: try await GH.run(["--version"]), as: UTF8.self) }
    catch GHError.ghNotFound { found = "none installed" }
    catch { found = "other: \(error)" }
    check(found.hasPrefix("gh version") || found == "none installed",
          "the lookup after the override is gone reaches the real gh, got: \(found)")
    print("gh after the stub: \(found.split(separator: "\n").first ?? "")")
}

if ProcessInfo.processInfo.environment["DIPLOMAT_DUMP"] == "1" {
    section("live gh dump (cross-check vs Python)")
    let viewer = try await API.fetchViewerLogin()
    let configured = PRRef.LocalRepo(owner: cfg.owner, repo: cfg.repo, path: "")
    let realPRs = try await API.fetchOpenPRs(configured)
    let realIssues = try await API.fetchOpenIssues(configured)
    print("viewer @\(viewer) · PRs \(realPRs.count) · issues \(realIssues.count)")
    for kind in ToolKind.allCases {
        let c = ToolData.count(for: kind, prs: realPRs, issues: realIssues, me: viewer)
        print("\(kind.rawValue): \(c)")
    }
}

print("\nSMOKE OK")
