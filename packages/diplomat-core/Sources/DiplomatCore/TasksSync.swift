import Foundation

/// One linked item as GitHub answered it (`graphql/task-item.graphql`).
public struct TaskItem: Equatable, Sendable {
    /// A comment, or a review (which has a `state` and no `text`).
    public struct Activity: Equatable, Sendable {
        public var id: String
        public var url: String
        public var login: String
        public var at: Date?
        public var text: String?
        public var state: String?

        public init(id: String, url: String, login: String, at: Date?, text: String? = nil,
                    state: String? = nil) {
            self.id = id
            self.url = url
            self.login = login
            self.at = at
            self.text = text
            self.state = state
        }
    }

    /// `pr` or `issue`.
    public var kind: String
    public var title: String
    public var url: String
    public var state: String
    public var isDraft: Bool
    public var reviewDecision: String?
    public var mergeable: String?
    public var ci: String?
    /// Closing references: the issues a PR closes, or the PRs that close an issue.
    public var closes: [String]
    public var comments: [Activity]
    public var reviews: [Activity]
    public var mergedAt: Date?
    public var closedAt: Date?
    public var author: String?
    /// A PR's branches and head commit.
    public var baseRef: String?
    public var headRef: String?
    public var headOid: String?

    public init(kind: String, title: String, url: String, state: String, isDraft: Bool = false,
                reviewDecision: String? = nil, mergeable: String? = nil, ci: String? = nil,
                closes: [String] = [], comments: [Activity] = [], reviews: [Activity] = [],
                mergedAt: Date? = nil, closedAt: Date? = nil, author: String? = nil,
                baseRef: String? = nil, headRef: String? = nil, headOid: String? = nil) {
        self.kind = kind
        self.title = title
        self.url = url
        self.state = state
        self.isDraft = isDraft
        self.reviewDecision = reviewDecision
        self.mergeable = mergeable
        self.ci = ci
        self.closes = closes
        self.comments = comments
        self.reviews = reviews
        self.mergedAt = mergedAt
        self.closedAt = closedAt
        self.author = author
        self.baseRef = baseRef
        self.headRef = headRef
        self.headOid = headOid
    }

    /// Of one `issueOrPullRequest` node.
    init?(node: [String: Any]) {
        guard let type = node["__typename"] as? String, let title = node["title"] as? String,
              let url = node["url"] as? String, let state = node["state"] as? String else { return nil }
        func nodes(_ key: String) -> [[String: Any]] {
            (node[key] as? [String: Any])?["nodes"] as? [[String: Any]] ?? []
        }
        func activity(_ n: [String: Any], at key: String) -> Activity? {
            guard let id = n["id"] as? String, let url = n["url"] as? String else { return nil }
            return Activity(id: id, url: url,
                            login: (n["author"] as? [String: Any])?["login"] as? String ?? "ghost",
                            at: (n[key] as? String).flatMap(CommsLog.parseISO),
                            text: n["bodyText"] as? String, state: n["state"] as? String)
        }
        let rollup = (nodes("commits").first?["commit"] as? [String: Any])?["statusCheckRollup"]
        let ci = (rollup as? [String: Any])?["state"] as? String
        self.init(kind: type == "PullRequest" ? "pr" : "issue", title: title, url: url, state: state,
                  isDraft: node["isDraft"] as? Bool ?? false,
                  reviewDecision: node["reviewDecision"] as? String,
                  mergeable: node["mergeable"] as? String,
                  ci: ci == "EXPECTED" ? "PENDING" : ci,
                  closes: (nodes("closingIssuesReferences") + nodes("closedByPullRequestsReferences"))
                    .compactMap { $0["url"] as? String },
                  comments: nodes("comments").compactMap { activity($0, at: "createdAt") },
                  reviews: nodes("reviews").compactMap { activity($0, at: "submittedAt") },
                  mergedAt: (node["mergedAt"] as? String).flatMap(CommsLog.parseISO),
                  closedAt: (node["closedAt"] as? String).flatMap(CommsLog.parseISO),
                  author: (node["author"] as? [String: Any])?["login"] as? String,
                  baseRef: node["baseRefName"] as? String, headRef: node["headRefName"] as? String,
                  headOid: node["headRefOid"] as? String)
    }
}

/// What GitHub answered for one link.
public enum TaskFetch: Equatable, Sendable {
    case found(TaskItem)
    /// GitHub says it does not exist (or this login cannot see it).
    case missing
}

/// Brings every unarchived task up to date: GitHub's state of its items, and the Comms
/// pings that name it.
public enum TaskSync {
    public struct Result: Codable, Equatable, Sendable {
        public var ok: Bool
        /// Why it failed; with `ok`, the items GitHub refused, which were left as they were.
        public var error: String?
        /// History lines appended.
        public var events: Int
    }

    /// Fetches every GitHub link of every unarchived task (one `gh api graphql` call per
    /// `chunk` of them), then, under the lock, diffs the answers against the mirror
    /// (`apply`, its `base` the mirror read before fetching), folds Comms' log (`slack`),
    /// drops from the history the Slack events of senders on the ban list now
    /// (`withoutBanned`), appends the events and writes the tasks, each one's
    /// `lastEventAt` moved up to its events, and the mirror. A failed fetch writes nothing.
    /// An item GitHub answered with an error of its own is left as it was, and the answer
    /// is `ok` with `error` naming each such item.
    public static func run(now: Date = Date(), dir: URL = TaskStore.dir,
                           commsDir: URL = CommsLog.dir) async -> Result {
        func failed(_ message: String) -> Result { Result(ok: false, error: message, events: 0) }
        let assets: CoreAssets.TasksModel
        let tasks: [TrackedTask]
        do {
            assets = try CoreAssets.tasks()
            tasks = try TaskStore.tasks(in: dir)
        } catch {
            return failed(error.localizedDescription)
        }
        let base = TaskStore.mirror(in: dir).items
        var urls: [String] = []
        for link in tasks.filter({ $0.archivedAt == nil }).flatMap(\.links)
        where link.isGitHub && !urls.contains(link.url) {
            urls.append(link.url)
        }
        var fetched: [String: TaskFetch] = [:]
        var skipped: [String] = []
        do {
            for start in stride(from: 0, to: urls.count, by: max(1, assets.chunk)) {
                let chunk = Array(urls[start..<min(start + max(1, assets.chunk), urls.count)])
                let answer = try await fetch(chunk, perItem: assets.commentsPerItem)
                fetched.merge(answer.fetched) { $1 }
                skipped += answer.skipped
            }
        } catch {
            return failed(error.localizedDescription)
        }
        let comms = CommsLog.fold(CommsLog.read(in: commsDir))
        let watched = CoreAssets.repoCoordinates()
        let banned = CommsBan.bannedNow()

        PrivateFiles.prepare(dir)
        let lock = TaskStore.lockURL(in: dir)
        let result = PrivateFiles.locked(lock) { () -> Result in
            let before: [TrackedTask]
            do { before = try TaskStore.tasks(in: dir) } catch { return failed(error.localizedDescription) }
            let gh = apply(tasks: before, mirror: TaskStore.mirror(in: dir), base: base, fetched: fetched,
                           now: now, excerptChars: assets.excerptChars)
            var mirror = gh.mirror
            let slack = self.slack(tasks: gh.tasks, comms: comms, watermark: mirror.commsWatermark,
                                   handled: mirror.commsHandled, now: now, watched: watched, banned: banned,
                                   holdSecs: assets.commsHoldSecs, excerptChars: assets.excerptChars)
            mirror.commsWatermark = slack.watermark
            mirror.commsHandled = slack.handled
            let events = gh.events + slack.events
            if let kept = withoutBanned(TaskStore.events(in: dir), comms: comms, banned: banned) {
                guard TaskStore.rewrite(kept, in: dir) else {
                    return failed("could not rewrite \(TaskStore.eventsURL(in: dir).path)")
                }
            }
            // Events first: a write failing past them repeats them on the next sync rather
            // than losing them.
            guard TaskStore.append(events, in: dir, maxBytes: assets.maxLogBytes) else {
                return failed("could not append to \(TaskStore.eventsURL(in: dir).path)")
            }
            let after = TaskStore.noting(events, in: gh.tasks)
            guard after == before || TaskStore.save(after, in: dir) else {
                return failed("could not write \(TaskStore.tasksURL(in: dir).path)")
            }
            guard let data = try? CommsLog.encoder.encode(mirror),
                  PrivateFiles.writeWhole(data, to: TaskStore.mirrorURL(in: dir)) else {
                return failed("could not write \(TaskStore.mirrorURL(in: dir).path)")
            }
            return Result(ok: true, error: skipped.isEmpty ? nil : "skipped " + skipped.joined(separator: "; "),
                          events: events.count)
        }
        return result ?? failed("could not lock \(lock.path)")
    }

    /// The batched query for `count` items, assembled around `task-item.graphql`'s
    /// fragments: alias `iK` asks for `$oK/$rK` number `$nK`; `$c` is the per-item comment
    /// and review count.
    public static func query(count: Int) throws -> String {
        let fragments = try CoreAssets.graphql("task-item")
        let vars = ["$c: Int!"] + (0..<count).map { "$o\($0): String!, $r\($0): String!, $n\($0): Int!" }
        let items = (0..<count).map {
            "  i\($0): repository(owner: $o\($0), name: $r\($0)) {"
                + " issueOrPullRequest(number: $n\($0)) { __typename ...TaskPR ...TaskIssue } }"
        }
        return "query(\(vars.joined(separator: ", "))) {\n\(items.joined(separator: "\n"))\n}\n\n\(fragments)"
    }

    /// `links` are GitHub link URLs, whose parse cannot fail.
    private static func fetch(_ links: [String], perItem: Int) async throws
        -> (fetched: [String: TaskFetch], skipped: [String]) {
        var args = ["api", "graphql", "-f", "query=\(try query(count: links.count))", "-F", "c=\(perItem)"]
        for (i, ref) in links.compactMap(GitHubRef.init(url:)).enumerated() {
            args += ["-f", "o\(i)=\(ref.owner)", "-f", "r\(i)=\(ref.repo)", "-F", "n\(i)=\(ref.number)"]
        }
        let out = try await GH.output(args)
        do {
            return try items(fromResponse: out.stdout, links: links)
        } catch where out.status != 0 {
            throw GHError.process(code: out.status, stderr: out.stderr)
        }
    }

    /// `query`'s answer per link of `links` (the K-th answered by alias `iK`). An item or
    /// repository GitHub reports `NOT_FOUND` is `missing`. An item under any other error
    /// (FORBIDDEN, SAML…) is left out of `fetched` and named in `skipped` as `o/r#n: <message>`.
    /// An error naming no item, or an alias left out, throws.
    static func items(fromResponse data: Data, links: [String]) throws
        -> (fetched: [String: TaskFetch], skipped: [String]) {
        guard let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let answers = root["data"] as? [String: Any] else {
            throw GHError.graphql(messages: ["the answer holds no data"])
        }
        var failed: [Int: String] = [:]
        var fatal: [String] = []
        for e in root["errors"] as? [[String: Any]] ?? [] where (e["type"] as? String) != "NOT_FOUND" {
            let message = e["message"] as? String ?? "\(e)"
            if let alias = (e["path"] as? [Any])?.first as? String, alias.hasPrefix("i"),
               let i = Int(alias.dropFirst()), links.indices.contains(i) {
                failed[i] = failed[i] ?? message
            } else {
                fatal.append(message)
            }
        }
        guard fatal.isEmpty else { throw GHError.graphql(messages: fatal) }
        var out: [String: TaskFetch] = [:]
        var skipped: [String] = []
        for (i, link) in links.enumerated() {
            if let message = failed[i] {
                let ref = GitHubRef(url: link)
                skipped.append("\(ref.map { "\($0.owner)/\($0.repo)#\($0.number)" } ?? link): \(message)")
                continue
            }
            guard let answer = answers["i\(i)"] else {
                throw GHError.graphql(messages: ["no answer for \(link)"])
            }
            let node = (answer as? [String: Any])?["issueOrPullRequest"] as? [String: Any]
            out[link] = node.flatMap(TaskItem.init(node:)).map(TaskFetch.found) ?? .missing
        }
        return (out, skipped)
    }

    // MARK: - Pure steps

    /// The GitHub half of a sync. Per fetched item, once however many unarchived tasks
    /// hold it, every holder gets the same events (source `github`, subject the item):
    /// - first sight: `linked`, `<kind>:<state>`; its comments and reviews so far are
    ///   marked seen, not told
    /// - `pr:merged` / `<kind>:closed` / `<kind>:reopened`; `pr:ready` / `pr:draft`
    /// - `ci:failing` (to FAILURE or ERROR) / `ci:passing` (to SUCCESS)
    /// - `review:approved` / `review:changes-requested` / `review:required`
    /// - `conflicts` on turning CONFLICTING
    /// - per new comment `comment`, `@login`; per new submitted review `review:<state>`, `@login`
    /// - `link:missing` once when GitHub stops having it
    /// A holder's links to it take GitHub's URL, kind and title, and each closing reference
    /// a holder neither holds nor `dismissed` becomes its link (via `closes`) with a
    /// `linked`, `via:closes` event; that item is first fetched by the next sync. The
    /// mirror keeps the items some task links, archived ones included, so an archived task
    /// keeps its status and its unarchiving tells only what changed meanwhile.
    ///
    /// `base` is `mirror.items` as they were when `fetched` was asked for (nil: `mirror`'s
    /// own). An item whose entry another sync has rewritten since is left as that sync
    /// wrote it, links and all: its answer may be the newer one, and applying an older one
    /// over it would tell its changes backwards. The next sync brings it up to date.
    public static func apply(tasks: [TrackedTask], mirror: TaskMirror, base: [String: GitHubSnapshot]? = nil,
                             fetched: [String: TaskFetch], now: Date, excerptChars: Int)
        -> (tasks: [TrackedTask], mirror: TaskMirror, events: [TaskEvent]) {
        var tasks = tasks
        var items = mirror.items
        var events: [TaskEvent] = []
        var done = Set<String>()
        for t in tasks.indices where tasks[t].archivedAt == nil {
            for link in tasks[t].links {
                guard let answer = fetched[link.url], let ref = GitHubRef(url: link.url),
                      done.insert(ref.key).inserted else { continue }
                var keys = [link.url]
                if case let .found(item) = answer { keys.append(item.url) }
                if let base, keys.contains(where: { base[$0] != mirror.items[$0] }) { continue }
                let holders = tasks.indices.filter { h in
                    tasks[h].archivedAt == nil
                        && tasks[h].links.contains { GitHubRef(url: $0.url)?.key == ref.key }
                }
                func tell(_ tags: [String], at: Date = now, url: String? = nil,
                          subject: String, text: String? = nil) {
                    for h in holders {
                        events.append(TaskEvent(at: at, task: tasks[h].id, source: "github",
                                                tags: TaskTags.normalize(tags), url: url,
                                                subject: subject, text: text))
                    }
                }
                guard case let .found(item) = answer else {
                    let old = items[link.url]
                    if old?.state != GitHubSnapshot.missing { tell(["link:missing"], subject: link.url) }
                    items[link.url] = GitHubSnapshot(kind: old?.kind ?? link.kind,
                                                     title: old?.title ?? link.title ?? "",
                                                     state: GitHubSnapshot.missing,
                                                     seenIDs: old?.seenIDs ?? [], updatedAt: now)
                    continue
                }
                let (snapshot, happened) = diff(old: items[item.url] ?? items[link.url], item,
                                                now: now, excerptChars: excerptChars)
                for h in happened { tell(h.tags, at: h.at, url: h.url, subject: item.url, text: h.text) }
                items[item.url] = snapshot
                for h in holders {
                    var links: [TaskLink] = []
                    for var l in tasks[h].links {
                        if GitHubRef(url: l.url)?.key == ref.key {
                            l.url = item.url
                            l.kind = item.kind
                            l.title = item.title
                        }
                        if !links.contains(where: { TaskLinks.identity($0.url) == TaskLinks.identity(l.url) }) {
                            links.append(l)
                        }
                    }
                    let held = Set((links.map(\.url) + tasks[h].dismissed).map(TaskLinks.identity))
                    for closing in item.closes where !held.contains(TaskLinks.identity(closing)) {
                        links.append(TaskLink(url: closing, kind: item.kind == "pr" ? "issue" : "pr",
                                              addedAt: now, via: "closes"))
                        events.append(TaskEvent(at: now, task: tasks[h].id, source: "github",
                                                tags: ["linked", "via:closes"], url: item.url,
                                                subject: closing))
                    }
                    tasks[h].links = links
                }
            }
        }
        let linked = Set(tasks.flatMap(\.links).filter(\.isGitHub).map(\.url))
        var next = mirror
        next.items = items.filter { linked.contains($0.key) }
        return (tasks, next, events)
    }

    private struct Happening {
        var at: Date
        var tags: [String]
        var url: String?
        var text: String?
    }

    private static func diff(old: GitHubSnapshot?, _ item: TaskItem, now: Date,
                             excerptChars: Int) -> (GitHubSnapshot, [Happening]) {
        let old = old?.state == GitHubSnapshot.missing ? nil : old
        let reviews = item.reviews.filter { $0.state != "PENDING" }
        let seen = Set(old?.seenIDs ?? [])
        let mergeable = item.mergeable == "UNKNOWN" ? old?.mergeable : item.mergeable
        // What the fetch window still holds is never evicted: it would be told again.
        let window = (item.comments + reviews).map(\.id)
        let inWindow = Set(window)
        let older = (old?.seenIDs ?? []).filter { !inWindow.contains($0) }
        let snapshot = GitHubSnapshot(kind: item.kind, title: item.title, state: item.state,
                                      isDraft: item.isDraft, reviewDecision: item.reviewDecision,
                                      ci: item.ci, mergeable: mergeable,
                                      seenIDs: older.suffix(max(0, GitHubSnapshot.seenLimit - window.count)) + window,
                                      updatedAt: now, closes: item.closes, author: item.author,
                                      baseRef: item.baseRef, headRef: item.headRef, headOid: item.headOid)
        guard let old else {
            return (snapshot, [Happening(at: now, tags: ["linked", "\(item.kind):\(item.state.lowercased())"],
                                         url: item.url)])
        }
        var out: [Happening] = []
        if old.state != item.state {
            switch item.state {
            case "MERGED": out.append(Happening(at: item.mergedAt ?? now, tags: ["pr:merged"], url: item.url))
            case "CLOSED": out.append(Happening(at: item.closedAt ?? now, tags: ["\(item.kind):closed"], url: item.url))
            default: out.append(Happening(at: now, tags: ["\(item.kind):reopened"], url: item.url))
            }
        }
        if item.kind == "pr", old.isDraft != item.isDraft {
            out.append(Happening(at: now, tags: [item.isDraft ? "pr:draft" : "pr:ready"], url: item.url))
        }
        let failing: Set<String?> = ["FAILURE", "ERROR"]
        if failing.contains(item.ci), !failing.contains(old.ci) {
            out.append(Happening(at: now, tags: ["ci:failing"], url: item.url))
        } else if item.ci == "SUCCESS", old.ci != "SUCCESS" {
            out.append(Happening(at: now, tags: ["ci:passing"], url: item.url))
        }
        if item.reviewDecision != old.reviewDecision, let decision = item.reviewDecision {
            let tag = ["APPROVED": "review:approved", "CHANGES_REQUESTED": "review:changes-requested",
                       "REVIEW_REQUIRED": "review:required"][decision]
            if let tag { out.append(Happening(at: now, tags: [tag], url: item.url)) }
        }
        if mergeable == "CONFLICTING", old.mergeable != "CONFLICTING" {
            out.append(Happening(at: now, tags: ["conflicts"], url: item.url))
        }
        for c in item.comments where !seen.contains(c.id) {
            out.append(Happening(at: c.at ?? now, tags: ["comment", "@\(c.login)"], url: c.url,
                                 text: c.text.map { excerpt($0, excerptChars) }))
        }
        for r in reviews where !seen.contains(r.id) {
            let state = (r.state ?? "").lowercased().replacingOccurrences(of: "_", with: "-")
            out.append(Happening(at: r.at ?? now, tags: ["review:\(state)", "@\(r.login)"], url: r.url))
        }
        return (snapshot, out)
    }

    /// The Slack half of a sync: the Comms pings logged from `watermark` on, but for those
    /// in `handled` (taken by an earlier sync in `watermark`'s second), that name one of a
    /// task's links (a GitHub item by URL, `o/r#n`, or `#n` of `watched`; any other link by
    /// its URL in the text, or a Slack link by being the ping's permalink), one event per
    /// such task: source `slack`, tags `slack`, `@<sender>`, `#<channel>` or `dm`, and
    /// `triage:<action>`. A ping by one of `banned` (Slack user ids, upper case), or one
    /// Comms skipped as banned or triaged as an injection, is skipped. Pings are taken a
    /// logged second at a time, oldest first; a second holding a matching ping Comms has
    /// not triaged yet waits, up to `holdSecs`, and so does every later one. The answer's
    /// `watermark` and `handled` are where the next sync starts. With no `watermark` (the
    /// first sync), nothing is told and the watermark is `now`.
    public static func slack(tasks: [TrackedTask], comms: [CommsItem], watermark: Date?, handled: [String] = [],
                             now: Date, watched: (owner: String, repo: String), banned: Set<String> = [],
                             holdSecs: Int, excerptChars: Int)
        -> (events: [TaskEvent], watermark: Date, handled: [String]) {
        guard let watermark else { return ([], now, []) }
        let live = tasks.filter { $0.archivedAt == nil }
        let pings = comms.filter { ping in
            !ping.message.isExtraTarget
                && (ping.at > watermark || ping.at == watermark && !handled.contains(ping.message.id))
        }
        var mark = watermark, taken = handled
        var events: [TaskEvent] = []
        for at in Set(pings.map(\.at)).sorted() {
            let second = pings.filter { $0.at == at }.map { ($0, subjects(of: $0.message, in: live, watched)) }
            if second.contains(where: { !$0.1.isEmpty && $0.0.decision == nil })
                && now.timeIntervalSince(at) < Double(holdSecs) { break }
            if at != mark { taken = [] }
            mark = at
            taken += second.map(\.0.message.id)
            for (ping, subjects) in second {
                if banned.contains(ping.message.userID.uppercased()) { continue }
                if let d = ping.decision, d.action == .injection || d.reason.hasPrefix(CommsWatch.bannedPrefix) {
                    continue
                }
                let m = ping.message
                let tags = ["slack", "@\(m.sender)", m.isDM ? "dm" : "#\(m.channelName)"]
                    + (ping.decision.map { ["triage:\($0.action.rawValue)"] } ?? [])
                for (task, subject) in subjects {
                    events.append(TaskEvent(at: Double(m.ts).map { Date(timeIntervalSince1970: $0) } ?? at,
                                            task: task, source: "slack", tags: TaskTags.normalize(tags),
                                            url: m.permalink, subject: subject,
                                            text: excerpt(slackText(m.text), excerptChars)))
                }
            }
        }
        return (events, mark, taken)
    }

    /// `events` but for the Slack ones whose ping, by its permalink in `comms`, a sender in
    /// `banned` sent; nil when there are none.
    public static func withoutBanned(_ events: [TaskEvent], comms: [CommsItem], banned: Set<String>) -> [TaskEvent]? {
        let permalinks = Set(comms.filter { banned.contains($0.message.userID.uppercased()) }
                                  .map(\.message.permalink).filter { !$0.isEmpty })
        guard !permalinks.isEmpty else { return nil }
        let kept = events.filter { $0.source != "slack" || !permalinks.contains($0.url ?? "") }
        return kept.count == events.count ? nil : kept
    }

    /// Per task `message` names, the first of its links it names.
    private static func subjects(of message: CommsMessage, in tasks: [TrackedTask],
                                 _ watched: (owner: String, repo: String)) -> [(task: String, link: String)] {
        let mentioned = TaskLinks.mentions(in: message.text, watched: watched)
        return tasks.compactMap { task in
            task.links.first { link in
                if let ref = GitHubRef(url: link.url) { return mentioned.contains(ref.key) }
                return TaskLinks.identity(message.permalink) == TaskLinks.identity(link.url)
                    || message.text.contains(link.url)
            }.map { (task.id, $0.url) }
        }
    }

    /// Slack's markup as it reads: `<url|label>` -> `label`, `<url>` -> `url`, `<@U1>` ->
    /// `@U1`, `<#C1|name>` -> `#name`.
    static func slackText(_ text: String) -> String {
        var out = "", rest = text.startIndex
        for m in markup.matches(in: text, range: NSRange(text.startIndex..., in: text)) {
            guard let whole = Range(m.range, in: text), let target = Range(m.range(at: 1), in: text) else { continue }
            let label = Range(m.range(at: 2), in: text).map { String(text[$0]) }.flatMap { $0.isEmpty ? nil : $0 }
            let id = String(text[target])
            out += text[rest..<whole.lowerBound]
            if let sigil = id.first, sigil == "@" || sigil == "#" {
                out += String(sigil) + (label ?? String(id.dropFirst()))
            } else {
                out += label ?? id
            }
            rest = whole.upperBound
        }
        return out + text[rest...]
    }

    private static let markup = try! NSRegularExpression(pattern: "<([^<>|]*)(?:\\|([^<>]*))?>")

    /// One line, at most `limit` characters.
    static func excerpt(_ text: String, _ limit: Int) -> String {
        let line = text.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        guard line.count > limit else { return line }
        return line.prefix(max(0, limit - 1)).trimmingCharacters(in: .whitespaces) + "…"
    }
}
