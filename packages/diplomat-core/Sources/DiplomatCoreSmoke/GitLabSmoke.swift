import Foundation
import DiplomatCore

/// GitLab repo roots: host reading, remotes, roots, item keys and links, the glab runner,
/// the adapters against the shared parity fixtures, and the prompt note.
func gitlabSmoke() async throws {
    section("gitlab: hosts")
    check(GitLabHosts.contains("gitlab.com", configured: [])
          && GitLabHosts.contains("GitLab.Example.com", configured: [])
          && GitLabHosts.contains("gl.example.org", configured: ["GL.example.org"]),
          "gitlab.com, any gitlab.* host and every configured host are GitLab")
    check(!GitLabHosts.contains("gl.example.org", configured: [])
          && !GitLabHosts.contains("github.com", configured: [])
          && !GitLabHosts.contains("GitHub.com", configured: ["github.com"])
          && !GitLabHosts.contains("mygitlab.com", configured: [])
          && !GitLabHosts.contains("gitlab", configured: [])
          && !GitLabHosts.contains("gitlab..com", configured: []),
          "an unconfigured host is GitLab only by a first DNS label of `gitlab`, and github.com never")

    let config = """
        # glab config
        git_protocol: ssh
        hosts:
            # a comment at child depth

            gitlab.com:
                api_protocol: https
                token: secret
            GL.Example.org:
                api_host: gl.example.org
            gitlab.example.net:8443:
                git_protocol: https
        aliases:
            fake.host:
        """
    check(GitLabHosts.parse(config: config) == ["gitlab.com", "gl.example.org", "gitlab.example.net"],
          "hosts are the keys alone at the first child's depth, port dropped, until the next "
          + "column-0 key: \(GitLabHosts.parse(config: config))")
    check(GitLabHosts.parse(config: "aliases:\n    gl.example.org:\n").isEmpty
          && GitLabHosts.parse(config: "").isEmpty,
          "no hosts section, no hosts")

    let fm = FileManager.default
    let scratch = fm.temporaryDirectory.appendingPathComponent("smoke-glab-\(getpid())")
    try fm.createDirectory(at: scratch, withIntermediateDirectories: true)
    defer { try? fm.removeItem(at: scratch) }
    func writeConfig(_ dir: URL, host: String) throws {
        try fm.createDirectory(at: dir, withIntermediateDirectories: true)
        try "hosts:\n  \(host):\n    token: x\n".write(to: dir.appendingPathComponent("config.yml"),
                                                       atomically: true, encoding: .utf8)
    }
    let emptyHome = scratch.appendingPathComponent("home-empty").path
    let glabDir = scratch.appendingPathComponent("glabdir")
    let xdg = scratch.appendingPathComponent("xdg")
    try writeConfig(glabDir, host: "gl.dir.example")
    try writeConfig(xdg.appendingPathComponent("glab-cli"), host: "gl.xdg.example")
    check(GLab.configuredHosts(env: ["HOME": emptyHome, "GLAB_CONFIG_DIR": glabDir.path,
                                     "XDG_CONFIG_HOME": xdg.path]) == ["gl.dir.example"],
          "GLAB_CONFIG_DIR's config.yml comes first")
    check(GLab.configuredHosts(env: ["HOME": emptyHome, "XDG_CONFIG_HOME": xdg.path])
          == ["gl.xdg.example"], "then XDG_CONFIG_HOME's glab-cli/config.yml")
    check(GLab.configuredHosts(env: ["HOME": emptyHome]).isEmpty, "none readable is none configured")
    let dotHome = scratch.appendingPathComponent("home-dot")
    try writeConfig(dotHome.appendingPathComponent(".config/glab-cli"), host: "gl.dot.example")
    check(GLab.configuredHosts(env: ["HOME": dotHome.path]) == ["gl.dot.example"],
          "XDG_CONFIG_HOME defaults to ~/.config")
    #if os(macOS)
    let macHome = scratch.appendingPathComponent("home-mac")
    try writeConfig(macHome.appendingPathComponent("Library/Application Support/glab-cli"),
                    host: "gl.mac.example")
    check(GLab.configuredHosts(env: ["HOME": macHome.path]) == ["gl.mac.example"],
          "on macOS, ~/Library/Application Support/glab-cli/config.yml")
    #endif

    section("gitlab: remotes and roots")
    func coords(_ url: String, _ hosts: Set<String> = []) -> [String]? {
        PRRef.LocalRepo.coordinates(remoteURL: url, gitlabHosts: hosts).map { [$0.host, $0.owner, $0.repo] }
    }
    check(coords("https://gitlab.example.com/group/sub/proj.git") == ["gitlab.example.com", "group/sub", "proj"]
          && coords("git@GitLab.com:group/proj/") == ["gitlab.com", "group", "proj"]
          && coords("ssh://git@gl.example.org:2222/a/b/c.git", ["gl.example.org"]) == ["gl.example.org", "a/b", "c"]
          && coords("git://gl.example.org/a/b", ["gl.example.org"]) == ["gl.example.org", "a", "b"],
          "GitLab remotes in every form, the owner the whole namespace, the host lowercased")
    check(coords("git@gl.example.org:a/b.git") == nil && coords("https://gitlab.com/proj") == nil
          && coords("https://github.com/o/r") == ["github.com", "o", "r"]
          && coords("https://github.com/o/r/x") == nil,
          "an unconfigured host, a lone project and a nested GitHub path are no repo")

    let mixed = """
        remote.origin.url https://github.com/o/r
        remote.gl.url https://gitlab.com/o/r
        remote.mirror.url git@gitlab.com:O/R.git
        """
    check(PRRef.LocalRepo.entries(path: "/p", remotes: mixed).map(\.host) == ["github.com", "gitlab.com"],
          "one repo per host and slug, compared case-insensitively")

    let configured = (owner: "o", repo: "r")
    let roots = RepoRoots.resolve(
        paths: ["/w/gl-same", "/w/gl-renamed", "/w/gl-up", "/w/gl-twin", "/w/gh-same"],
        remotes: ["/w/gl-same": "remote.origin.url https://gitlab.com/o/r",
                  "/w/gl-renamed": "remote.origin.url https://gitlab.com/other/r",
                  "/w/gl-up": "remote.mirror.url https://gitlab.example.com/g/m\n"
                      + "remote.origin.url https://gitlab.example.com/g/fork\n"
                      + "remote.upstream.url https://gitlab.example.com/g/sub/p",
                  "/w/gl-twin": "remote.origin.url git@gitlab.com:O/R.git",
                  "/w/gh-same": "remote.origin.url https://github.com/o/r"],
        configured: configured)
    check(roots == [PRRef.LocalRepo(owner: "o", repo: "r", path: "/w/gl-same", host: "gitlab.com"),
                    PRRef.LocalRepo(owner: "other", repo: "r", path: "/w/gl-renamed", host: "gitlab.com"),
                    PRRef.LocalRepo(owner: "g/sub", repo: "p", path: "/w/gl-up",
                                    host: "gitlab.example.com"),
                    PRRef.LocalRepo(owner: "o", repo: "r", path: "/w/gh-same")],
          "a GitLab remote is only ever itself, never the configured or a renamed GitHub repo; "
          + "a GitLab upstream outranks its origin; roots dedupe by host and slug: \(roots)")
    let besideGitHub: [String: String] = [
        "/w/mirror": "remote.origin.url git@gitlab.com:acme/widget.git\n"
            + "remote.github.url git@github.com:o/r.git",
        "/w/sibling": "remote.origin.url https://gitlab.example.com/g/sub/p\n"
            + "remote.gh.url https://github.com/x/y",
        "/w/fork": "remote.upstream.url https://gitlab.com/g/r\n"
            + "remote.gh.url https://github.com/someone/r"]
    func rootOf(_ path: String) -> [PRRef.LocalRepo] {
        RepoRoots.resolve(paths: [path], remotes: besideGitHub, configured: configured)
    }
    check(rootOf("/w/mirror") == [PRRef.LocalRepo(owner: "o", repo: "r", path: "/w/mirror")]
          && rootOf("/w/sibling") == [PRRef.LocalRepo(owner: "x", repo: "y", path: "/w/sibling")]
          && rootOf("/w/fork") == [PRRef.LocalRepo(owner: "o", repo: "r", path: "/w/fork")],
          "a checkout with a GitHub remote resolves among its GitHub remotes alone, as with no "
          + "GitLab one: the configured repo, a GitHub sibling, a renamed fork: "
          + "\(["/w/mirror", "/w/sibling", "/w/fork"].map(rootOf))")
    let sibling = PRRef.LocalRepo.entries(path: "/w/sibling", remotes: besideGitHub["/w/sibling"]!)
    let labOnly = PRRef.LocalRepo.entries(path: "/w/lab", remotes: "remote.origin.url https://gitlab.com/g/p")
    check(sibling.map(\.slug) == ["x/y", "g/sub/p"] && labOnly.map(\.slug) == ["g/p"],
          "a checkout's GitHub remotes rank ahead of a GitLab origin, so Settings names it by "
          + "the first as it resolves: \(sibling.map(\.slug))")
    let mirrored = PRRef.LocalRepo.entries(path: "/w/widget", remotes: """
        remote.lab.url https://gitlab.com/acme/lab
        remote.upstream.url https://gitlab.com/acme/widget
        remote.origin.url git@github.com:acme/widget.git
        """)
    let ghWidget = PRRef.LocalRepo(owner: "acme", repo: "widget", path: "/w/widget")
    check(mirrored == [ghWidget,
                       PRRef.LocalRepo(owner: "acme", repo: "widget", path: "/w/widget", host: "gitlab.com"),
                       PRRef.LocalRepo(owner: "acme", repo: "lab", path: "/w/widget", host: "gitlab.com")],
          "GitHub first, then GitLab, each upstream, origin, the rest: \(mirrored.map { "\($0.host)/\($0.slug)" })")
    check(PRRef.parse("acme/widget#5", owner: "o", repo: "r", also: mirrored).repo == ghWidget
          && PRRef.parse("https://github.com/acme/widget/pull/5", owner: "o", repo: "r",
                         also: mirrored).repo == ghWidget
          && PRRef.parse("https://gitlab.com/acme/widget/-/merge_requests/5", owner: "o", repo: "r",
                         also: mirrored).repo?.host == "gitlab.com",
          "with a GitLab upstream of the same slug, a host-less acme/widget#5 and a github.com link "
          + "name the GitHub repo, a GitLab link the GitLab one")

    section("gitlab: references, keys and links")
    let home = PRRef.LocalRepo(owner: "group/sub", repo: "proj", path: "/w/proj", host: "gitlab.example.com")
    let mr = "https://gitlab.example.com/group/sub/proj/-/merge_requests/12"
    let p1 = PRRef.parse(mr, owner: "group/sub", repo: "proj", host: "gitlab.example.com")
    check(p1.number == 12 && !p1.repoMismatch, "a merge request URL in the home repo")
    let p2 = PRRef.parse("see \(mr)#note_1", owner: "o", repo: "r", also: [home])
    check(p2.number == 12 && !p2.repoMismatch && p2.repo == home,
          "a merge request URL names the listed GitLab repo it is in")
    check(PRRef.parse(mr, owner: "group/sub", repo: "proj").repoMismatch
          && PRRef.parse("https://github.com/group/proj/pull/3", owner: "group", repo: "proj",
                         host: "gitlab.example.com").repoMismatch
          && PRRef.parse(mr, owner: "o", repo: "r",
                         also: [PRRef.LocalRepo(owner: "group/sub", repo: "proj", path: "")]).repoMismatch,
          "a URL matches a repo on its own host only")
    check(PRRef.parse("Group/Sub/Proj!7", owner: "group/sub", repo: "proj", host: "gitlab.example.com").number == 7
          && PRRef.parse("group/sub/proj#7", owner: "o", repo: "r", also: [home]).repo == home
          && PRRef.parse("group/sub/proj!7", owner: "group/sub", repo: "proj", kind: .issues).number == nil,
          "the nested shorthand, `!` for a merge request only")
    check(PRRef.parse("!7", owner: "group/sub", repo: "proj", host: "gitlab.example.com").number == 7
          && PRRef.parse("!7", owner: "group/sub", repo: "proj", host: "gitlab.example.com",
                         kind: .issues).number == nil
          && PRRef.parse("!7", owner: "o", repo: "r").number == nil,
          "a bare !7 is a merge request of a GitLab home, never an issue or a GitHub PR")
    let issue = PRRef.parse("https://gitlab.example.com/group/sub/proj/-/work_items/4", owner: "group/sub",
                            repo: "proj", host: "gitlab.example.com", kind: .issues)
    check(issue.number == 4 && !issue.repoMismatch
          && PRRef.parse("https://gitlab.example.com/group/sub/proj/-/issues/4", owner: "group/sub",
                         repo: "proj", host: "gitlab.example.com", kind: .issues).number == 4
          && PRRef.parse(mr, owner: "group/sub", repo: "proj", host: "gitlab.example.com",
                         kind: .issues).number == nil,
          "an issue field takes /-/issues and /-/work_items links, not a merge request's")

    check(PRRef.itemKey(url: mr) == "group/sub/proj#12"
          && PRRef.itemKey(url: "https://gitlab.example.com/Group/Sub/Proj/-/issues/7?x=1") == "group/sub/proj#7"
          && PRRef.itemKey(url: "gitlab.example.com:8443/g/p/-/work_items/3/") == "g/p#3",
          "GitLab merge request, issue and work item URLs are keyed by slug and iid")
    for bad in ["https://gitlab.example.com/group/proj/merge_requests/12",
                "https://gitlab.example.com/group/proj/-/merge_requests/0",
                "https://gitlab.example.com/group/proj/-/pipelines/4",
                "https://gitlab.example.com/proj/-/issues/4",
                "https://gitlab.example.com/group/proj/-/issues/4x"] {
        check(PRRef.itemKey(url: bad) == nil, "\(bad) is no item")
    }
    check(PRRef.splitItemKey("group/sub/proj#12").map { "\($0.slug)|\($0.number)" } == "group/sub/proj|12"
          && PRRef.splitSlug("group/sub/proj").map { "\($0.owner)|\($0.repo)" } == "group/sub|proj"
          && PRRef.splitSlug("proj") == nil
          && PRRef.itemLabel(item: "group/sub/proj#12", roots: 2) == "proj#12",
          "a nested owner splits at the last slash")

    let gh = PRRef.LocalRepo(owner: "o", repo: "r", path: "/p")
    let ghJSON = String(decoding: try JSONEncoder().encode(gh), as: UTF8.self)
    check(!ghJSON.contains("host"), "a GitHub root persists without a host: \(ghJSON)")
    let old = try JSONDecoder().decode(PRRef.LocalRepo.self,
                                       from: Data(#"{"owner":"o","repo":"r","path":"/p"}"#.utf8))
    check(old == gh && old.host == "github.com" && !old.isGitLab, "a payload without a host is GitHub")
    let back = try JSONDecoder().decode(PRRef.LocalRepo.self, from: try JSONEncoder().encode(home))
    check(back == home && back.isGitLab, "a GitLab root keeps its host")
    check(gh != PRRef.LocalRepo(owner: "o", repo: "r", path: "/p", host: "gitlab.com")
          && PRRef.LocalRepo(owner: "o", repo: "r", path: "", host: "GitLab.Example.COM").host
              == "gitlab.example.com",
          "the host is part of a root's identity, lowercased")
    check(gh.webURL(.pull, number: 5) == "https://github.com/o/r/pull/5"
          && gh.webURL(.issues, number: 5) == "https://github.com/o/r/issues/5"
          && home.webURL(.pull, number: 5) == "https://gitlab.example.com/group/sub/proj/-/merge_requests/5"
          && home.webURL(.issues, number: 5) == "https://gitlab.example.com/group/sub/proj/-/issues/5",
          "links follow the host")

    let comms = CommsSettings(repos: ["Group/Sub/Proj", "a//b", "x", "o/r"])
    check(comms.repos == ["group/sub/proj", "o/r"]
          && comms.coordinates(of: "group/sub/proj") == ("group/sub", "proj")
          && comms.owner == "group/sub"
          && CommsSettings.ref(3, repo: "group/sub/proj") == "proj#3",
          "Comms watches a nested slug, its owner everything before the last slash: \(comms.repos)")

    check(AutofixMesh.workKey(kind: "review", prURL: mr, headSha: "abc")
              == "review:gitlab.example.com/group/sub/proj#12@abc"
          && AutofixMesh.ledgerKey(kind: "review", prURL: mr, headSha: "")
              == "review:gitlab.example.com/group/sub/proj#12"
          && AutofixMesh.workKey(kind: "review", prURL: "https://github.com/o/r/pull/5", headSha: "abc")
              == "review:github.com/o/r#5@abc"
          && AutofixMesh.ledgerKey(kind: "review",
                                   prURL: "https://gitlab.example.com/group/sub/proj/-/issues/12",
                                   headSha: "abc") == "",
          "a merge request's work and ledger keys carry its host and namespace")

    section("gitlab: adapters (shared parity fixtures)")
    let fixtures = try CoreAssets.assetsDir().deletingLastPathComponent()
        .appendingPathComponent("Tests/fixtures/gitlab")
    func canonical(_ data: Data) throws -> String {
        let obj = try JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])
        return String(decoding: try JSONSerialization.data(withJSONObject: obj,
                                                           options: [.sortedKeys, .fragmentsAllowed]),
                      as: UTF8.self)
    }
    let inputs = ((try? fm.contentsOfDirectory(atPath: fixtures.path)) ?? [])
        .filter { $0.hasSuffix(".input.json") }.sorted()
    let names = inputs.map { String($0.dropLast(".input.json".count)) }
    check(Set(["prs", "issues", "monitor-prs", "closed-prs", "review-requests",
               "review-requests-nofiles", "review-requests-window", "mr", "member-paths",
               "issues-paged", "review-requests-paged"]).isSubset(of: Set(names)),
          "a fixture for every adapter in \(fixtures.path): \(names)")
    for name in names {
        let input = try Data(contentsOf: fixtures.appendingPathComponent("\(name).input.json"))
        let expected = try Data(contentsOf: fixtures.appendingPathComponent("\(name).expected.json"))
        let inputObject = try JSONSerialization.jsonObject(with: input) as? [String: Any]
        let got: String
        if name == "member-paths" {
            // {slug, ids} -> the members/all reads.
            got = try canonical(try JSONSerialization.data(withJSONObject: GitLab.memberPaths(
                slug: inputObject?["slug"] as? String ?? "", ids: inputObject?["ids"] as? [Int] ?? [])))
        } else if let answer = inputObject?["answer"], let listed = inputObject?["members"] as? [[String: Any]] {
            // {answer, members}: every lookup answers the ids of `members`, asked for or
            // not; what the adapter asked for, then its answer.
            var asked: [[Int]] = []
            let adapted = try await GitLab.adapt(name, try JSONSerialization.data(withJSONObject: answer),
                                                 reviewer: "carol", lookup: { ids in
                asked.append(ids)
                return Set(listed.compactMap { $0["id"] as? Int })
            })
            got = try canonical(try JSONSerialization.data(withJSONObject: [
                "asked": asked, "result": try JSONSerialization.jsonObject(with: adapted)]))
        } else {
            // The review-requests answers are the query's asked for reviewer `carol`.
            var lookedUp = false
            got = try canonical(try await GitLab.adapt(name, input, reviewer: "carol", lookup: { _ in
                lookedUp = true
                return []
            }))
            check(!lookedUp, "the \(name) answer's member page is whole, so no member is looked up")
        }
        let want = try canonical(expected)
        check(got == want, "the \(name) adapter matches its fixture\n got: \(got)\nwant: \(want)")
    }
    print("adapter fixtures matched: \(names.count)")

    var refused = ""
    do { _ = try GitLab.mergeRequest(Data(#"{"data":{"project":{"mergeRequest":null}}}"#.utf8)) }
    catch { refused = (error as? LocalizedError)?.errorDescription ?? "\(error)" }
    check(refused == "GitLab merge request not found", "a missing merge request is an error: \(refused)")
    func view(_ json: String) -> String {
        (try? GitLab.itemView(Data(json.utf8))).map { (try? canonical($0)) ?? "" } ?? "threw"
    }
    check(view(#"{"iid":5,"web_url":"https://gitlab.example.com/g/p/-/issues/5","state":"locked","#
               + #""author":{"username":"bob"}}"#)
              == #"{"author":{"login":"bob"},"number":5,"state":"OPEN","url":"https:\/\/gitlab.example.com\/g\/p\/-\/issues\/5"}"#
          && view(#"{"iid":6,"web_url":"u","state":"merged","author":null}"#)
              == #"{"author":null,"number":6,"state":"MERGED","url":"u"}"#
          && view(#"{"iid":7,"web_url":"u","state":"closed"}"#).contains(#""state":"CLOSED""#)
          && view("[]") == "threw",
          "a REST merge request or issue reads as gh's view of one")
    check((try? GitLab.viewer(Data(#"{"data":{"currentUser":{"username":"alice"}}}"#.utf8),
                              host: "gitlab.example.com")) == "alice",
          "the viewer is the signed-in username")
    var signedOut = ""
    do { _ = try GitLab.viewer(Data(#"{"data":{"currentUser":null}}"#.utf8), host: "gitlab.example.com") }
    catch { signedOut = (error as? LocalizedError)?.errorDescription ?? "\(error)" }
    check(signedOut == "not signed in to gitlab.example.com", "no viewer is an error: \(signedOut)")

    // A thread's system notes ("changed this line in version 2 of the diff", authored by
    // whoever pushed) are no reply: the reviewer's comment stays its last.
    let systemNotes = #"""
        {"data":{"project":{"mergeRequests":{"nodes":[{"iid":"3","discussions":{"nodes":[
          {"resolvable":true,"resolved":false,"notes":{"nodes":[
            {"author":{"username":"bob"},"body":"Fix this.","system":false,"userPermissions":{"resolveNote":false}},
            {"author":{"username":"alice"},"body":"changed this line in version 2 of the diff","system":true,
             "userPermissions":{"resolveNote":true}}]}},
          {"resolvable":true,"resolved":false,"notes":{"nodes":[
            {"author":{"username":"alice"},"body":"changed this line","system":true,"userPermissions":{"resolveNote":true}}]}}
        ]}}]}}}}
        """#
    let adaptedThreads = try canonical(try GitLab.monitorPRs(Data(systemNotes.utf8)))
    check(adaptedThreads.contains(#""reviewThreads":{"nodes":[{"comments":{"nodes":[{"author":{"login":"bob"},"body":"Fix this."}],"totalCount":1},"isResolved":false,"viewerCanResolve":true},{"comments":{"nodes":[],"totalCount":0},"isResolved":false,"viewerCanResolve":true}]}"#),
          "a thread's last comment and count leave its system notes out, its resolvability "
          + "read off its last note of any kind: \(adaptedThreads)")

    func rest(_ json: String) -> String { GitLab.mergeable(rest: Data(json.utf8)) }
    check(rest(#"{"has_conflicts":true,"detailed_merge_status":"conflict"}"#) == "CONFLICTING"
          && rest(#"{"has_conflicts":true,"detailed_merge_status":"checking"}"#) == "CONFLICTING"
          && rest(#"{"has_conflicts":false,"detailed_merge_status":"mergeable"}"#) == "MERGEABLE"
          && rest(#"{"has_conflicts":false,"detailed_merge_status":"need_rebase"}"#) == "MERGEABLE"
          && rest(#"{"has_conflicts":false,"detailed_merge_status":"unchecked"}"#) == "UNKNOWN"
          && rest(#"{"has_conflicts":false,"detailed_merge_status":"checking"}"#) == "UNKNOWN"
          && rest(#"{"has_conflicts":false,"detailed_merge_status":"preparing"}"#) == "UNKNOWN"
          && rest(#"{"has_conflicts":false}"#) == "UNKNOWN" && rest("[]") == "UNKNOWN" && rest("") == "UNKNOWN",
          "a REST merge request's status reads by the adapters' rule")

    let glRoot = PRRef.LocalRepo(owner: "g/sub", repo: "p", path: "", host: "gitlab.example.com")
    let ghRoot = PRRef.LocalRepo(owner: "o", repo: "r", path: "")
    func snap(_ url: String, _ mergeable: String) -> PRSnapshot {
        PRSnapshot(number: 0, title: "", url: url, isDraft: false, mergeable: mergeable,
                   reviewDecision: "", threadsUnresolved: 0)
    }
    let owed = AgentTaskQueue.conflicting([
        snap("https://gitlab.example.com/g/sub/p/-/merge_requests/1", "UNKNOWN"),
        snap("https://gitlab.example.com/g/sub/p/-/merge_requests/2", "CONFLICTING"),
        snap("https://gitlab.example.com/g/sub/p/-/merge_requests/3", "MERGEABLE"),
        snap("https://github.com/o/r/pull/4", "UNKNOWN"),
        snap("https://github.com/o/r/pull/5", "CONFLICTING"),
        snap("https://gitlab.example.com/x/y/-/merge_requests/6", "UNKNOWN"),
    ], roots: [glRoot, ghRoot])
    check(owed == ["g/sub/p#1", "g/sub/p#2", "o/r#5"],
          "a conflict fix stays owed on a CONFLICTING PR, and on a GitLab root's merge request "
          + "GitLab has not checked: \(owed.sorted())")

    section("gitlab: glab runner")
    let stubDir = scratch.appendingPathComponent("stub")
    try fm.createDirectory(at: stubDir, withIntermediateDirectories: true)
    let stub = stubDir.appendingPathComponent("glab")
    try """
        #!/bin/sh
        echo call >> '\(stubDir.path)/calls'
        case "$4" in *members/all*)
            echo "$4" >> '\(stubDir.path)/memberpaths'
            cat '\(stubDir.path)/members'
            cat '\(stubDir.path)/err' >&2
            exit "$(cat '\(stubDir.path)/memberscode')"
        esac
        printf '%s\\n' "$@" > '\(stubDir.path)/argv'
        cat '\(stubDir.path)/out'
        cat '\(stubDir.path)/err' >&2
        exit "$(cat '\(stubDir.path)/code')"
        """.write(to: stub, atomically: true, encoding: .utf8)
    chmod(stub.path, 0o755)
    func answer(_ out: String, code: Int = 0, err: String = "") throws {
        try out.write(to: stubDir.appendingPathComponent("out"), atomically: true, encoding: .utf8)
        try err.write(to: stubDir.appendingPathComponent("err"), atomically: true, encoding: .utf8)
        try "\(code)".write(to: stubDir.appendingPathComponent("code"), atomically: true, encoding: .utf8)
    }
    func argv() -> [String] {
        ((try? String(contentsOf: stubDir.appendingPathComponent("argv"), encoding: .utf8)) ?? "")
            .split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
    }
    func failure(_ body: () async throws -> Void) async -> String {
        do { try await body(); return "returned" }
        catch { return (error as? LocalizedError)?.errorDescription ?? "\(error)" }
    }
    let loginShell = stubDir.appendingPathComponent("loginshell")
    try "#!/bin/sh\necho 'welcome!'\necho '\(stub.path)'\n".write(to: loginShell, atomically: true, encoding: .utf8)
    chmod(loginShell.path, 0o755)
    let bareEnv = ["PATH": "/nonexistent", "SHELL": loginShell.path]
    check(GLab.locate(env: bareEnv, candidates: []) == stub.path,
          "a glab outside the candidates and a bare PATH is the one the login shell names")

    setenv("DIPLOMAT_GLAB", stub.path, 1)
    defer { unsetenv("DIPLOMAT_GLAB") }

    try answer(String(decoding: try Data(contentsOf: fixtures.appendingPathComponent("prs.input.json")),
                      as: UTF8.self))
    let prs = try await API.fetchOpenPRs(home)
    let args = argv()
    let expectedPRs = try JSONSerialization.jsonObject(
        with: try Data(contentsOf: fixtures.appendingPathComponent("prs.expected.json"))) as? [String: Any]
    let repository = (expectedPRs?["data"] as? [String: Any])?["repository"] as? [String: Any]
    let expectedCount = (repository?["pullRequests"] as? [String: Any])?["nodes"] as? [Any]
    check(!prs.isEmpty && prs.count == expectedCount?.count,
          "a GitLab root's open merge requests are read through glab and the PR parser: \(prs.count)")
    check(prs.flatMap(\.reviewThreads).contains { $0.viewerCanResolve == nil },
          "a discussion without notes is a thread whose resolvability the answer does not say")
    check(Array(args.prefix(4)) == ["api", "--hostname", "gitlab.example.com", "graphql"]
          && args.contains("path=group/sub/proj") && args.contains { $0.hasPrefix("query=") },
          "glab is asked on the root's host about its project: \(args.prefix(4))")

    try answer(#"{"data":{"project":null}}"#)
    let missing = await failure { _ = try await API.fetchOpenIssues(home) }
    check(missing == "GitLab project group/sub/proj not found on gitlab.example.com",
          "a project the answer does not hold is an unreadable root, not an empty one: \(missing)")

    // A members/all read is answered from `members`, every other call from `out`.
    func members(_ out: String, code: Int = 0) throws {
        try out.write(to: stubDir.appendingPathComponent("members"), atomically: true, encoding: .utf8)
        try "\(code)".write(to: stubDir.appendingPathComponent("memberscode"), atomically: true, encoding: .utf8)
    }
    func calls() -> Int {
        let n = ((try? String(contentsOf: stubDir.appendingPathComponent("calls"), encoding: .utf8)) ?? "")
            .split(separator: "\n").count
        try? fm.removeItem(at: stubDir.appendingPathComponent("calls"))
        return n
    }
    func memberPaths() -> [String] {
        let paths = ((try? String(contentsOf: stubDir.appendingPathComponent("memberpaths"), encoding: .utf8)) ?? "")
            .split(separator: "\n").map(String.init)
        try? fm.removeItem(at: stubDir.appendingPathComponent("memberpaths"))
        return paths
    }
    func pagedIssues(full: Bool, id: String = "gid://gitlab/User/14") -> String {
        #"{"data":{"project":{"projectMembers":{"pageInfo":{"hasNextPage":\#(full)},"#
            + #""nodes":[{"user":{"username":"alice"}}]},"issues":{"nodes":["#
            + #"{"iid":"9","title":"t","webUrl":"https://gitlab.example.com/group/sub/proj/-/issues/9","#
            + #""createdAt":"2026-11-07T09:00:00+01:00","updatedAt":"2026-11-08T09:00:00+01:00","#
            + #""author":{"id":"\#(id)","username":"Dave"},"userNotesCount":0,"notes":{"nodes":[]}}]}}}}"#
    }
    let daveRead = "projects/group%2Fsub%2Fproj/members/all?per_page=100&user_ids[]=14"
    _ = calls()
    _ = memberPaths()
    try members(#"[{"id":14,"username":"dave"}]"#)
    try answer(pagedIssues(full: false))
    let whole = try await API.fetchOpenIssues(home)
    let wholeCalls = calls()
    let wholePaths = memberPaths()
    try answer(pagedIssues(full: true))
    let lookedUp = try await API.fetchOpenIssues(home)
    let lookedUpCalls = calls()
    let lookedUpPaths = memberPaths()
    check(whole.map(\.authorAssociation) == ["NONE"] && wholeCalls == 1 && wholePaths.isEmpty
          && lookedUp.map(\.authorAssociation) == ["MEMBER"] && lookedUpCalls == 2
          && lookedUpPaths == [daveRead],
          "an author past a full member page is looked up by id in one more glab call, none for a "
          + "whole page: \(whole.map(\.authorAssociation)) \(wholeCalls) \(lookedUp.map(\.authorAssociation)) "
          + "\(lookedUpCalls) \(lookedUpPaths)")
    try members(#"[{"id":15,"username":"erin"}]"#)
    let outsider = try await API.fetchOpenIssues(home)
    check(outsider.map(\.authorAssociation) == ["NONE"] && memberPaths() == [daveRead],
          "an author members/all does not answer is no member: \(outsider.map(\.authorAssociation))")
    try members("", code: 1)
    try answer(pagedIssues(full: true), err: "403 Forbidden")
    _ = calls()
    let unread = await failure { _ = try await API.fetchOpenIssues(home) }
    check(unread == "glab exited 1: 403 Forbidden" && memberPaths() == [daveRead, daveRead] && calls() == 3,
          "a members/all read failing twice fails the read with glab's error: \(unread)")
    try members(#"[{"id":14,"username":"dave"}]"#)
    try answer(pagedIssues(full: true, id: "gid://gitlab/Group/14"))
    let unreadable = await failure { _ = try await API.fetchOpenIssues(home) }
    check(unreadable == "GitLab user Dave has no readable id" && memberPaths().isEmpty,
          "an author past a full page without a user id fails the read: \(unreadable)")
    check((try? GitLab.memberIDs(Data(#"[{"id":14},{"id":12}]"#.utf8))) == [12, 14]
          && (try? GitLab.memberIDs(Data(#"{"message":"404 Not Found"}"#.utf8))) == nil
          && (try? GitLab.memberIDs(Data(#"[{"username":"dave"}]"#.utf8))) == nil,
          "a members/all answer is its members' ids, and anything but a list of them is an error")
    try answer(#"{"errors":[{"message":"boom"}],"data":null}"#, code: 1)
    let errs = await failure { _ = try await API.fetchOpenPRs(home) }
    check(errs == "GraphQL: boom", "an errors array is the error: \(errs)")
    try answer("", code: 1, err: "401 Unauthorized")
    let denied = await failure { _ = try await API.fetchGitLabViewer(host: "gitlab.example.com") }
    check(denied == "glab exited 1: 401 Unauthorized", "a failed glab says so: \(denied)")
    check(!argv().contains { $0.hasPrefix("path=") }, "the viewer names no project")

    try answer(String(decoding: try Data(contentsOf: fixtures.appendingPathComponent("mr.input.json")),
                      as: UTF8.self))
    let state = try await API.fetchPRState(number: 12, root: home)
    let author = try await API.fetchPRAuthor(number: 12, root: home)
    check(state == "OPEN" && author == "alice" && argv().contains("iid=12"),
          "a merge request's state and author: \(state) \(author ?? "nil")")
    try answer(#"{"data":{"project":{"mergeRequest":null}}}"#)
    let gone = await failure { _ = try await API.fetchPRState(number: 99, root: home) }
    check(gone == "GitLab merge request not found", "a merge request that is not there: \(gone)")
    check(GLab.repoFlag(host: "gitlab.example.com", slug: "group/sub/proj")
          == ["-R", "https://gitlab.example.com/group/sub/proj"], "-R names the host too")
    try answer(#"{"iid":12,"has_conflicts":true,"detailed_merge_status":"conflict"}"#)
    let rechecked = try await API.recheckedMergeable(number: 12, root: home)
    check(rechecked == "CONFLICTING"
          && argv() == ["api", "--hostname", "gitlab.example.com",
                        "projects/group%2Fsub%2Fproj/merge_requests/12?with_merge_status_recheck=true", ""],
          "a merge request's status is read over REST, asking GitLab to recheck it: \(rechecked) \(argv())")
    try answer("", code: 1, err: "403 Forbidden")
    let recheckRefused = await failure { _ = try await API.recheckedMergeable(number: 12, root: home) }
    check(recheckRefused == "glab exited 1: 403 Forbidden", "a recheck glab refuses throws: \(recheckRefused)")

    section("gitlab: prompt note")
    let note = try CoreAssets.gitlab().promptNote
    check(note.contains("{host}") && note.contains("{owner}/{repo}"), "gitlab.json's note is a template")
    func noted(_ prompt: String, _ kind: String) {
        let paragraphs = prompt.components(separatedBy: "\n\n")
        let at = paragraphs.firstIndex { $0.hasPrefix("This repository is hosted on GitLab at ") }
        check(at == 1, "\(kind): the note follows the opening line, at \(at.map(String.init) ?? "nil")")
        let p = at.map { paragraphs[$0] } ?? ""
        check(p.contains("GitLab at gitlab.example.com (project group/sub/proj)")
              && p.contains("`--repo https://gitlab.example.com/group/sub/proj`") && !p.contains("{"),
              "\(kind): the note names the host and nested project: \(p.prefix(120))")
        check(paragraphs.filter { $0.contains("hosted on GitLab") }.count == 1, "\(kind): one note")
    }
    var review = ReviewConfig(depth: "max", target: .specific, me: "testuser", specificPR: "337")
    review.home = home
    noted(review.buildPrompt(), "review single")
    for author in [SpecificAuthor.mine, .theirs] {
        var known = ReviewConfig(depth: "max", target: .specific, me: "testuser", specificPR: "337",
                                 specificAuthor: author)
        known.home = home
        noted(known.buildPrompt(), "review single, \(author)")
    }
    var sweep = ReviewConfig(depth: "max", me: "testuser")
    sweep.home = home
    noted(sweep.buildPrompt(), "review sweep")
    var linked = ReviewConfig(depth: "max", target: .specific, me: "testuser", specificPR: mr)
    linked.localRepos = [home]
    noted(linked.buildPrompt(), "review of a listed GitLab repo's link")
    var conflict = ConflictConfig(target: .specific, me: "testuser", specificPR: "337")
    conflict.home = home
    noted(conflict.buildPrompt(), "conflicts")
    var fix = IssueConfig(target: .specific, me: "testuser", specificIssue: "421")
    fix.home = home
    noted(fix.buildPrompt(), "issues")
    var audit = AuditConfig()
    audit.home = home
    noted(audit.buildPrompt(), "audit")
    var githubHome = ReviewConfig(depth: "max", target: .specific, me: "testuser", specificPR: "337")
    githubHome.home = gh
    check(!githubHome.buildPrompt().contains("glab"), "a GitHub home's prompt says nothing of glab")
    let goldenDir = try CoreAssets.assetsDir().appendingPathComponent("golden-prompts")
    for file in ((try? fm.contentsOfDirectory(atPath: goldenDir.path)) ?? []) where !file.contains("gitlab") {
        let text = (try? String(contentsOf: goldenDir.appendingPathComponent(file), encoding: .utf8)) ?? ""
        check(!text.contains("glab") && !text.contains("hosted on GitLab"), "\(file) is a GitHub prompt")
    }
    print("gitlab assertions passed")
}
