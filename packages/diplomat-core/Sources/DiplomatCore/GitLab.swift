import Foundation

/// Each `assets/graphql/gitlab-*` answer as the JSON its GitHub twin query answers, so one
/// parser reads both. Pure but for the `Lookup` an adapter is handed. Python twin:
/// `diplomat_runtime/gitlab.py`, held to the same output by `Tests/fixtures/gitlab/`.
///
/// GitLab has no author association: a project member reads `MEMBER`, anyone else `NONE`.
/// A merge request's number is its iid, a resolvable discussion is a review thread, and
/// system notes stand in for GitHub's review-request, review and ready-for-review events.
/// Times are converted from the instance's offset to UTC `Z`, as GitHub gives them, so they
/// compare as strings.
public enum GitLab {
    typealias JSON = [String: Any]

    /// User ids -> the project members among them.
    public typealias Lookup = ([Int]) async throws -> Set<Int>

    /// Ids per `members/all` read: under its page of 100, and few enough for the answer's
    /// `Link` header, which repeats every id twice; a self-hosted GitLab answered 502 past
    /// about 80 ids, the header outgrowing its proxy's buffer.
    static let memberChunk = 40

    /// The adapter by fixture name; `reviewer` is who a `review-requests` answer was asked for.
    public static func adapt(_ name: String, _ data: Data, reviewer: String,
                             lookup: Lookup) async throws -> Data {
        if name.hasPrefix("review-requests") {
            return try await reviewRequests(data, reviewer: reviewer, lookup: lookup)
        }
        if name.hasPrefix("issues") { return try await issues(data, lookup: lookup) }
        switch name {
        case "prs": return try prs(data)
        case "monitor-prs": return try monitorPRs(data)
        case "closed-prs": return try closedPRs(data)
        case "mr": return try mergeRequest(data)
        default: throw GHError.unanswered("no GitLab adapter named \(name)")
        }
    }

    /// `gitlab-prs` as `prs.graphql` answers.
    public static func prs(_ data: Data) throws -> Data {
        let nodes = try list(try project(data)["mergeRequests"]).map { mr -> JSON in
            var n = common(mr)
            n["isDraft"] = mr["draft"] as? Bool ?? false
            n["createdAt"] = try utc(mr["createdAt"])
            n["mergeable"] = mergeable(mr)
            n["reviewDecision"] = reviewDecision(mr)
            n["files"] = ["nodes": paths(mr)]
            n["timelineItems"] = ["nodes": try readyEvent(mr)]
            n["reviewThreads"] = ["nodes": threads(mr)]
            return n
        }
        return try encode(["data": ["repository": ["pullRequests": ["nodes": nodes]]]])
    }

    /// `gitlab-issues` as `issues.graphql` answers.
    public static func issues(_ data: Data, lookup: Lookup) async throws -> Data {
        let project = try self.project(data)
        let members = try await self.members(project, list(project["issues"]).flatMap { i in
            [i["author"]] + list(i["notes"]).map { $0["author"] }
        }, lookup: lookup)
        let nodes = try list(project["issues"]).map { i -> JSON in
            var n = common(i)
            n["createdAt"] = try utc(i["createdAt"])
            n["updatedAt"] = try utc(i["updatedAt"])
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

    /// `gitlab-monitor-prs` as `monitor-prs.graphql` answers.
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

    /// `gitlab-closed-prs` as `closed-prs.graphql` answers: the merged, then the closed.
    public static func closedPRs(_ data: Data) throws -> Data {
        let project = try self.project(data)
        let nodes = (list(project["merged"]) + list(project["closed"])).map { ["number": number($0)] }
        return try encode(["data": ["search": ["nodes": nodes]]])
    }

    /// `gitlab-review-requests` as `review-requests.graphql` answers, less the merge requests
    /// `reviewer` answered (`request(of:)`), with `files` only when the answer carries
    /// `diffStats` (`$withFiles`).
    public static func reviewRequests(_ data: Data, reviewer: String,
                                      lookup: Lookup) async throws -> Data {
        let project = try self.project(data)
        let owed = try list(project["mergeRequests"]).compactMap { mr -> (JSON, [JSON], [JSON])? in
            let activity = list(mr["activity"])
            let events = try activity.flatMap(reviewRequested)
            return try request(of: reviewer, events, mr, activity).map { (mr, activity, $0 + events) }
        }
        let members = try await self.members(project, owed.map { $0.0["author"] }, lookup: lookup)
        let nodes = try owed.map { mr, activity, requests -> JSON in
            var n = common(mr)
            n["authorAssociation"] = association(username(mr["author"]), members)
            n["headRefOid"] = mr["diffHeadSha"] ?? NSNull()
            if mr["diffStats"] != nil { n["files"] = ["nodes": paths(mr)] }
            n["timelineItems"] = ["nodes": requests]
            n["reviews"] = ["nodes": try activity.filter {
                let body = $0["body"] as? String ?? ""
                return reviewed.contains { body.hasPrefix($0) }
            }.map { ["author": login($0["author"]), "submittedAt": try utc($0["createdAt"])] }]
            n["comments"] = ["nodes": try list(mr["comments"]).map {
                ["author": login($0["author"]), "createdAt": try utc($0["createdAt"])]
            }]
            return n
        }
        return try encode(["data": ["search": ["nodes": nodes]]])
    }

    /// Note openings of a submitted review; the last is GitHub's `COMMENTED`.
    private static let reviewed = ["approved this merge request", "requested changes", "left review comments"]
    private static let answeredStates: Set<String> = ["REVIEWED", "APPROVED", "REQUESTED_CHANGES"]

    /// `gitlab-mr` as `gh pr view --json state,author` prints it.
    public static func mergeRequest(_ data: Data) throws -> Data {
        guard let mr = try project(data)["mergeRequest"] as? JSON else {
            throw GHError.unanswered("GitLab merge request not found")
        }
        return try encode(["state": state(mr["state"]), "author": login(mr["author"])])
    }

    /// A REST merge request or issue as `gh pr|issue view --json number,url,author,state`
    /// prints it.
    public static func itemView(_ data: Data) throws -> Data {
        guard let obj = (try? JSONSerialization.jsonObject(with: data)) as? JSON else {
            throw GHError.unanswered("glab printed no merge request or issue")
        }
        return try encode(["number": obj["iid"] ?? NSNull(), "url": obj["web_url"] ?? NSNull(),
                           "author": login(obj["author"]), "state": state(obj["state"])])
    }

    /// A REST merge request's `mergeable`, by the GraphQL adapters' rule; `UNKNOWN` for an
    /// answer that is no merge request.
    public static func mergeable(rest data: Data) -> String {
        guard let obj = (try? JSONSerialization.jsonObject(with: data)) as? JSON else { return "UNKNOWN" }
        return mergeable(["conflicts": obj["has_conflicts"] ?? NSNull(),
                          "detailedMergeStatus": (obj["detailed_merge_status"] as? String)?.uppercased()
                              ?? NSNull()])
    }

    private static func state(_ raw: Any?) -> String {
        let s = raw as? String ?? ""
        return ["opened": "OPEN", "locked": "OPEN"][s] ?? s.uppercased()
    }

    /// The username a `gitlab-viewer` answer holds.
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

    private static func common(_ node: JSON) -> JSON {
        ["number": number(node), "title": node["title"] ?? NSNull(),
         "url": node["webUrl"] ?? NSNull(), "author": login(node["author"])]
    }

    private static func utc(_ at: Any?) throws -> Any {
        guard let at = at as? String else { return NSNull() }
        return try utc(at)
    }

    private static func utc(_ at: String) throws -> String {
        guard let date = CommsLog.parseISO(at) else {
            throw GHError.unanswered("GitLab time \(at) is unreadable")
        }
        return CommsLog.isoString(date)
    }

    private static func username(_ author: Any?) -> String? {
        (author as? JSON)?["username"] as? String
    }

    private static func login(_ author: Any?) -> Any {
        username(author).map { ["login": $0] as Any } ?? NSNull()
    }

    /// Lowercased member usernames: the member page, plus, when it has a next page, those
    /// of `users` it misses that `lookup` confirms.
    private static func members(_ project: JSON, _ users: [Any?], lookup: Lookup) async throws -> Set<String> {
        let page = project["projectMembers"] as? JSON ?? [:]
        var members = Set(list(page).compactMap { username($0["user"])?.lowercased() })
        if (page["pageInfo"] as? JSON)?["hasNextPage"] as? Bool == true {
            var unlisted: [Int: String] = [:]
            for case let user as JSON in users {
                guard let name = username(user), !members.contains(name.lowercased()) else { continue }
                unlisted[try userID(user, name: name)] = name.lowercased()
            }
            members.formUnion(try await lookup(unlisted.keys.sorted()).compactMap { unlisted[$0] })
        }
        return members
    }

    private static func userID(_ user: JSON, name: String) throws -> Int {
        let prefix = "gid://gitlab/User/"
        let id = user["id"] as? String ?? ""
        let digits = id.hasPrefix(prefix) ? id.dropFirst(prefix.count) : ""
        guard digits.allSatisfy({ ("0"..."9").contains($0) }), let n = Int(digits) else {
            throw GHError.unanswered("GitLab user \(name) has no readable id")
        }
        return n
    }

    /// The `members/all` reads (direct, inherited or shared-group members) for `ids`,
    /// `memberChunk` to a read.
    public static func memberPaths(slug: String, ids: [Int]) -> [String] {
        stride(from: 0, to: ids.count, by: memberChunk).map { start in
            "projects/\(GLab.projectID(slug))/members/all?per_page=100"
                + ids[start..<min(start + memberChunk, ids.count)].map { "&user_ids[]=\($0)" }.joined()
        }
    }

    public static func memberIDs(_ data: Data) throws -> Set<Int> {
        guard let page = try? JSONSerialization.jsonObject(with: data) as? [JSON] else {
            throw GHError.unanswered("GitLab answered no member list")
        }
        return Set(try page.map { member in
            guard let id = member["id"] as? Int else {
                throw GHError.unanswered("GitLab answered a member without an id")
            }
            return id
        })
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

    /// Resolvable discussions as review threads, the last non-system note standing for
    /// GitHub's `comments(last: 1)`: a system note ("changed this line in version 2 of the
    /// diff") is left by whoever pushed. `viewerCanResolve` is its last note's, of any kind.
    private static func threads(_ mr: JSON) -> [JSON] {
        list(mr["discussions"]).filter { $0["resolvable"] as? Bool == true }.map { d in
            let notes = list(d["notes"])
            let said = notes.filter { $0["system"] as? Bool != true }
            let resolve = (notes.last?["userPermissions"] as? JSON)?["resolveNote"] as? Bool
            let comments: [JSON] = said.last.map {
                [["author": login($0["author"]), "body": $0["body"] ?? NSNull()]]
            } ?? []
            return ["isResolved": d["resolved"] as? Bool ?? false,
                    "viewerCanResolve": resolve.map { $0 as Any } ?? NSNull(),
                    "comments": ["totalCount": said.count, "nodes": comments] as JSON]
        }
    }

    /// The latest "marked ready" activity note as GitHub's ready-for-review event.
    private static func readyEvent(_ mr: JSON) throws -> [JSON] {
        let ready = try list(mr["notes"]).filter {
            ($0["body"] as? String ?? "").hasPrefix("marked this merge request as **ready**")
        }.map { try utc($0["createdAt"]) }
        guard let latest = ready.max(by: { ($0 as? String ?? "") < ($1 as? String ?? "") }) else { return [] }
        return [["__typename": "ReadyForReviewEvent", "createdAt": latest]]
    }

    /// `reviewer`'s request when no note in the window names them: nil once their
    /// `reviewState` says they answered it, as GitHub's `review-requested:` drops it; else
    /// dated at the window's oldest note (the merge request, for an empty window), the
    /// query returning only merge requests requesting their review.
    private static func request(of reviewer: String, _ events: [JSON], _ mr: JSON,
                                _ activity: [JSON]) throws -> [JSON]? {
        let me = reviewer.lowercased()
        let named = events.contains {
            (($0["requestedReviewer"] as? JSON)?["login"] as? String)?.lowercased() == me
        }
        guard !named else { return [] }
        let answered = list((mr["reviewers"] as? JSON)?["nodes"]).contains {
            ($0["username"] as? String)?.lowercased() == me
                && answeredStates.contains(
                    ($0["mergeRequestInteraction"] as? JSON)?["reviewState"] as? String ?? "")
        }
        guard !answered else { return nil }
        let oldest = try activity.compactMap { $0["createdAt"] as? String }.filter { !$0.isEmpty }
            .map { try utc($0) }.min()
        return [["createdAt": try oldest ?? utc(mr["createdAt"]),
                 "requestedReviewer": ["__typename": "User", "login": reviewer]] as JSON]
    }

    /// One review-requested event per user a "requested review from" note names, minus the
    /// removals the same note carries.
    private static func reviewRequested(_ note: JSON) throws -> [JSON] {
        let body = note["body"] as? String ?? ""
        let prefix = "requested review from "
        guard body.hasPrefix(prefix) else { return [] }
        var text = body
        if let cut = text.range(of: " and removed review request") { text = String(text[..<cut.lowerBound]) }
        guard let re = try? NSRegularExpression(pattern: #"@([\w.-]+)"#) else { return [] }
        let at = try utc(note["createdAt"])
        return re.matches(in: text, range: NSRange(text.startIndex..., in: text)).compactMap { m in
            Range(m.range(at: 1), in: text).map {
                ["createdAt": at,
                 "requestedReviewer": ["__typename": "User", "login": String(text[$0])]] as JSON
            }
        }
    }
}
