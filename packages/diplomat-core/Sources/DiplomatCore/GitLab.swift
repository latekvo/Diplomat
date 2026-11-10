import Foundation

/// The answers of the `assets/graphql/gitlab-*` queries, each turned into the JSON its
/// GitHub twin query answers with, so one parser reads both. Pure; Python twin:
/// `diplomat_runtime/gitlab.py`, held to the same output by the shared fixtures in
/// `Tests/fixtures/gitlab/`.
///
/// GitLab has no author association, so a project member reads as `MEMBER` and anyone
/// else as `NONE`; a merge request is numbered by its iid; a resolvable discussion is a
/// review thread; and GitLab records a review request, an approval, a requested change
/// and a ready-for-review as system notes, which stand in for GitHub's timeline events.
public enum GitLab {
    typealias JSON = [String: Any]

    /// The adapters by fixture name: `prs`, `issues`, `monitor-prs`, `closed-prs`,
    /// `review-requests` (with or without `diffStats`, so `review-requests-nofiles` too)
    /// and `mr`.
    public static func adapt(_ name: String, _ data: Data) throws -> Data {
        switch name {
        case "prs": return try prs(data)
        case "issues": return try issues(data)
        case "monitor-prs": return try monitorPRs(data)
        case "closed-prs": return try closedPRs(data)
        case "review-requests", "review-requests-nofiles": return try reviewRequests(data)
        case "mr": return try mergeRequest(data)
        default: throw GHError.unanswered("no GitLab adapter named \(name)")
        }
    }

    /// `gitlab-prs` as `prs.graphql`: `{data: {repository: {pullRequests: {nodes}}}}`.
    public static func prs(_ data: Data) throws -> Data {
        let nodes = list(try project(data)["mergeRequests"]).map { mr -> JSON in
            var n = common(mr)
            n["isDraft"] = mr["draft"] as? Bool ?? false
            n["createdAt"] = mr["createdAt"] ?? NSNull()
            n["mergeable"] = mergeable(mr)
            n["reviewDecision"] = reviewDecision(mr)
            n["files"] = ["nodes": paths(mr)]
            n["timelineItems"] = ["nodes": readyEvent(mr)]
            n["reviewThreads"] = ["nodes": threads(mr)]
            return n
        }
        return try encode(["data": ["repository": ["pullRequests": ["nodes": nodes]]]])
    }

    /// `gitlab-issues` as `issues.graphql`: `{data: {repository: {issues: {nodes}}}}`.
    public static func issues(_ data: Data) throws -> Data {
        let project = try self.project(data)
        let members = self.members(project)
        let nodes = list(project["issues"]).map { i -> JSON in
            var n = common(i)
            n["createdAt"] = i["createdAt"] ?? NSNull()
            n["updatedAt"] = i["updatedAt"] ?? NSNull()
            n["authorAssociation"] = association(username(i["author"]), members)
            n["assignees"] = ["nodes": list(i["assignees"]).map { ["login": $0["username"] ?? NSNull()] }]
            n["labels"] = ["nodes": list(i["labels"]).map { ["name": $0["title"] ?? NSNull()] }]
            n["comments"] = ["totalCount": i["userNotesCount"] as? Int ?? 0,
                             "nodes": list(i["notes"]).map {
                                 ["authorAssociation": association(username($0["author"]), members)]
                             }] as JSON
            return n
        }
        return try encode(["data": ["repository": ["issues": ["nodes": nodes]]]])
    }

    /// `gitlab-monitor-prs` as `monitor-prs.graphql`: `{data: {search: {nodes}}}`.
    public static func monitorPRs(_ data: Data) throws -> Data {
        let nodes = list(try project(data)["mergeRequests"]).map { mr -> JSON in
            var n = common(mr)
            n.removeValue(forKey: "author")
            n["isDraft"] = mr["draft"] as? Bool ?? false
            n["mergeable"] = mergeable(mr)
            n["reviewDecision"] = reviewDecision(mr)
            n["headRefOid"] = mr["diffHeadSha"] ?? NSNull()
            n["reviewThreads"] = ["nodes": threads(mr)]
            return n
        }
        return try encode(["data": ["search": ["nodes": nodes]]])
    }

    /// `gitlab-closed-prs` as `closed-prs.graphql`: the merged then the closed, each
    /// `{number}`, in `{data: {search: {nodes}}}`.
    public static func closedPRs(_ data: Data) throws -> Data {
        let project = try self.project(data)
        let nodes = (list(project["merged"]) + list(project["closed"])).map { ["number": number($0)] }
        return try encode(["data": ["search": ["nodes": nodes]]])
    }

    /// `gitlab-review-requests` as `review-requests.graphql`: `{data: {search: {nodes}}}`,
    /// with `files` only when the answer carries `diffStats` (`$withFiles`).
    public static func reviewRequests(_ data: Data) throws -> Data {
        let project = try self.project(data)
        let members = self.members(project)
        let nodes = list(project["mergeRequests"]).map { mr -> JSON in
            var n = common(mr)
            n["authorAssociation"] = association(username(mr["author"]), members)
            n["headRefOid"] = mr["diffHeadSha"] ?? NSNull()
            if mr["diffStats"] != nil { n["files"] = ["nodes": paths(mr)] }
            let activity = list(mr["activity"])
            n["timelineItems"] = ["nodes": activity.flatMap(reviewRequested)]
            n["reviews"] = ["nodes": activity.filter {
                let body = $0["body"] as? String ?? ""
                return body.hasPrefix("approved this merge request") || body.hasPrefix("requested changes")
            }.map { ["author": login($0["author"]), "submittedAt": $0["createdAt"] ?? NSNull()] }]
            n["comments"] = ["nodes": list(mr["comments"]).map {
                ["author": login($0["author"]), "createdAt": $0["createdAt"] ?? NSNull()]
            }]
            return n
        }
        return try encode(["data": ["search": ["nodes": nodes]]])
    }

    /// `gitlab-mr` as `gh pr view --json state,author`: `{state, author: {login}}`.
    public static func mergeRequest(_ data: Data) throws -> Data {
        guard let mr = try project(data)["mergeRequest"] as? JSON else {
            throw GHError.unanswered("GitLab merge request not found")
        }
        return try encode(["state": state(mr["state"]), "author": login(mr["author"])])
    }

    /// A REST `projects/:id/merge_requests/:iid` or `.../issues/:iid` answer as
    /// `gh pr view --json number,url,author,state` (`gh issue view`) prints one.
    public static func itemView(_ data: Data) throws -> Data {
        guard let obj = (try? JSONSerialization.jsonObject(with: data)) as? JSON else {
            throw GHError.unanswered("glab printed no merge request or issue")
        }
        return try encode(["number": obj["iid"] ?? NSNull(), "url": obj["web_url"] ?? NSNull(),
                           "author": login(obj["author"]), "state": state(obj["state"])])
    }

    /// GitHub's spelling of a GitLab state: `OPEN` for opened or locked, else uppercased.
    private static func state(_ raw: Any?) -> String {
        let s = raw as? String ?? ""
        return ["opened": "OPEN", "locked": "OPEN"][s] ?? s.uppercased()
    }

    /// `gitlab-viewer`'s signed-in username on `host`.
    public static func viewer(_ data: Data, host: String) throws -> String {
        let obj = (try? JSONSerialization.jsonObject(with: data)) as? JSON
        guard let user = (obj?["data"] as? JSON)?["currentUser"] as? JSON,
              let name = user["username"] as? String, !name.isEmpty else {
            throw GHError.unanswered("not signed in to \(host)")
        }
        return name
    }

    // MARK: - rules

    private static func project(_ data: Data) throws -> JSON {
        guard let obj = (try? JSONSerialization.jsonObject(with: data)) as? JSON,
              let project = (obj["data"] as? JSON)?["project"] as? JSON else {
            throw GHError.unanswered("the GitLab answer holds no project")
        }
        return project
    }

    private static func encode(_ obj: JSON) throws -> Data {
        try JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
    }

    /// A connection's `nodes`, or a bare list (`diffStats`), as objects.
    private static func list(_ raw: Any?) -> [JSON] {
        if let a = raw as? [JSON] { return a }
        return ((raw as? JSON)?["nodes"] as? [Any] ?? []).compactMap { $0 as? JSON }
    }

    private static func number(_ node: JSON) -> Any {
        (node["iid"] as? String).flatMap { Int($0) } ?? (node["iid"] as? Int).map { $0 as Any } ?? NSNull()
    }

    /// What every item node carries: number, title, url, author.
    private static func common(_ node: JSON) -> JSON {
        ["number": number(node), "title": node["title"] ?? NSNull(),
         "url": node["webUrl"] ?? NSNull(), "author": login(node["author"])]
    }

    private static func username(_ author: Any?) -> String? {
        (author as? JSON)?["username"] as? String
    }

    /// `{login}` of a GitLab author, null for none.
    private static func login(_ author: Any?) -> Any {
        username(author).map { ["login": $0] as Any } ?? NSNull()
    }

    private static func members(_ project: JSON) -> Set<String> {
        Set(list(project["projectMembers"]).compactMap { username($0["user"])?.lowercased() })
    }

    private static func association(_ user: String?, _ members: Set<String>) -> String {
        user.map { members.contains($0.lowercased()) } == true ? "MEMBER" : "NONE"
    }

    private static func mergeable(_ mr: JSON) -> String {
        if mr["conflicts"] as? Bool == true { return "CONFLICTING" }
        switch mr["detailedMergeStatus"] as? String {
        case nil, "UNCHECKED", "CHECKING", "PREPARING": return "UNKNOWN"
        default: return "MERGEABLE"
        }
    }

    private static func reviewDecision(_ mr: JSON) -> Any {
        let reviewers = list(mr["reviewers"])
        if reviewers.contains(where: {
            ($0["mergeRequestInteraction"] as? JSON)?["reviewState"] as? String == "REQUESTED_CHANGES"
        }) { return "CHANGES_REQUESTED" }
        if mr["approved"] as? Bool == true { return "APPROVED" }
        return reviewers.isEmpty ? NSNull() : "REVIEW_REQUIRED"
    }

    private static func paths(_ mr: JSON) -> [JSON] {
        list(mr["diffStats"]).map { ["path": $0["path"] ?? NSNull()] }
    }

    /// Each resolvable discussion as a review thread, its last note standing for GitHub's
    /// `comments(last: 1)`.
    private static func threads(_ mr: JSON) -> [JSON] {
        list(mr["discussions"]).filter { $0["resolvable"] as? Bool == true }.map { d in
            let notes = list(d["notes"])
            let last = notes.last
            let resolve = (last?["userPermissions"] as? JSON)?["resolveNote"] as? Bool
            let comments: [JSON] = last.map {
                [["author": login($0["author"]), "body": $0["body"] ?? NSNull()]]
            } ?? []
            return ["isResolved": d["resolved"] as? Bool ?? false,
                    "viewerCanResolve": resolve.map { $0 as Any } ?? NSNull(),
                    "comments": ["totalCount": notes.count, "nodes": comments] as JSON]
        }
    }

    /// The latest "marked ready" activity note as GitHub's ready-for-review event.
    private static func readyEvent(_ mr: JSON) -> [JSON] {
        let ready = list(mr["notes"]).filter {
            ($0["body"] as? String ?? "").hasPrefix("marked this merge request as **ready**")
        }
        guard let latest = ready.max(by: {
            ($0["createdAt"] as? String ?? "") < ($1["createdAt"] as? String ?? "")
        }) else { return [] }
        return [["__typename": "ReadyForReviewEvent", "createdAt": latest["createdAt"] ?? NSNull()]]
    }

    /// One review-requested event per user a "requested review from" note names, the
    /// removals the same note carries left out.
    private static func reviewRequested(_ note: JSON) -> [JSON] {
        let body = note["body"] as? String ?? ""
        let prefix = "requested review from "
        guard body.hasPrefix(prefix) else { return [] }
        var text = body
        if let cut = text.range(of: " and removed review request") { text = String(text[..<cut.lowerBound]) }
        guard let re = try? NSRegularExpression(pattern: #"@([\w.-]+)"#) else { return [] }
        return re.matches(in: text, range: NSRange(text.startIndex..., in: text)).compactMap { m in
            Range(m.range(at: 1), in: text).map {
                ["createdAt": note["createdAt"] ?? NSNull(),
                 "requestedReviewer": ["__typename": "User", "login": String(text[$0])]] as JSON
            }
        }
    }
}
