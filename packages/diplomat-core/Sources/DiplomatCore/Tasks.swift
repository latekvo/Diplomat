import Foundation

/// Something that outlives one PR ("physical iPhone support"): described by tags rather
/// than prose, tied to GitHub items, Slack threads and any other URL by its links.
public struct TrackedTask: Codable, Equatable, Sendable {
    public var id: String
    public var title: String
    public var tags: [String]
    public var links: [TaskLink]
    public var createdAt: Date
    /// The operator's override of the derived status: `paused` or `done`.
    public var pinnedStatus: TaskStatus?
    /// Not synced, and left out of `TaskStore.views` unless asked for.
    public var archivedAt: Date?
    /// The links the operator removed, which a sync's closing reference does not add back;
    /// linking one again takes it off. Compared by `TaskLinks.identity`; written only when
    /// not empty.
    public var dismissed: [String]
    /// The `at` of the newest event appended to its history: what outlives the history's
    /// rotation. Nil when none was recorded.
    public var lastEventAt: Date?
    /// `grouper` for a goal `TaskGrouper` proposed; nil for one the operator made.
    public var origin: String?
    /// What Diplomat does about it; nil reads as `observe`.
    public var mode: TaskMode?
    /// In `auto`: review each open linked PR again on every new head commit.
    public var reviewOnPush: Bool
    /// What the grouper said must land before what, between its own links.
    public var edges: [TaskEdge]
    /// The automatic work already asked for (`GoalWork.key`), and when.
    public var dispatched: [String: Date]

    public static let grouperOrigin = "grouper"

    public init(id: String, title: String, tags: [String] = [], links: [TaskLink] = [],
                createdAt: Date, pinnedStatus: TaskStatus? = nil, archivedAt: Date? = nil,
                dismissed: [String] = [], lastEventAt: Date? = nil, origin: String? = nil,
                mode: TaskMode? = nil, reviewOnPush: Bool = false, edges: [TaskEdge] = [],
                dispatched: [String: Date] = [:]) {
        self.id = id
        self.title = title
        self.tags = tags
        self.links = links
        self.createdAt = createdAt
        self.pinnedStatus = pinnedStatus
        self.archivedAt = archivedAt
        self.dismissed = dismissed
        self.lastEventAt = lastEventAt
        self.origin = origin
        self.mode = mode
        self.reviewOnPush = reviewOnPush
        self.edges = edges
        self.dispatched = dispatched
    }

    public var effectiveMode: TaskMode { mode ?? .observe }

    private enum CodingKeys: String, CodingKey {
        case id, title, tags, links, createdAt, pinnedStatus, archivedAt, dismissed, lastEventAt
        case origin, mode, reviewOnPush, edges, dispatched
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(id: try c.decode(String.self, forKey: .id), title: try c.decode(String.self, forKey: .title),
                  tags: try c.decode([String].self, forKey: .tags),
                  links: try c.decode([TaskLink].self, forKey: .links),
                  createdAt: try c.decode(Date.self, forKey: .createdAt),
                  pinnedStatus: try c.decodeIfPresent(TaskStatus.self, forKey: .pinnedStatus),
                  archivedAt: try c.decodeIfPresent(Date.self, forKey: .archivedAt),
                  dismissed: try c.decodeIfPresent([String].self, forKey: .dismissed) ?? [],
                  lastEventAt: try c.decodeIfPresent(Date.self, forKey: .lastEventAt),
                  origin: try c.decodeIfPresent(String.self, forKey: .origin),
                  mode: try c.decodeIfPresent(TaskMode.self, forKey: .mode),
                  reviewOnPush: try c.decodeIfPresent(Bool.self, forKey: .reviewOnPush) ?? false,
                  edges: try c.decodeIfPresent([TaskEdge].self, forKey: .edges) ?? [],
                  dispatched: try c.decodeIfPresent([String: Date].self, forKey: .dispatched) ?? [:])
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(id, forKey: .id)
        try c.encode(title, forKey: .title)
        try c.encode(tags, forKey: .tags)
        try c.encode(links, forKey: .links)
        try c.encode(createdAt, forKey: .createdAt)
        try c.encodeIfPresent(pinnedStatus, forKey: .pinnedStatus)
        try c.encodeIfPresent(archivedAt, forKey: .archivedAt)
        if !dismissed.isEmpty { try c.encode(dismissed, forKey: .dismissed) }
        try c.encodeIfPresent(lastEventAt, forKey: .lastEventAt)
        try c.encodeIfPresent(origin, forKey: .origin)
        try c.encodeIfPresent(mode, forKey: .mode)
        if reviewOnPush { try c.encode(reviewOnPush, forKey: .reviewOnPush) }
        if !edges.isEmpty { try c.encode(edges, forKey: .edges) }
        if !dispatched.isEmpty { try c.encode(dispatched, forKey: .dispatched) }
    }
}

/// What Diplomat does about a task.
public enum TaskMode: String, Codable, CaseIterable, Sendable {
    /// A goal the grouper made, waiting for the operator: shown, never acted on.
    case proposed
    /// Shown with its graph; the operator acts on it by hand.
    case observe
    /// Its issues with no PR get fix agents, and its PRs their reviews and finding fixes.
    case auto
    /// A proposal the operator turned down: archived, never listed, and the grouper does
    /// not propose it again.
    case dismissed
}

/// `from` must land before `to`: both are link URLs of the same task.
public struct TaskEdge: Codable, Equatable, Hashable, Sendable {
    public var from: String
    public var to: String

    public init(from: String, to: String) {
        self.from = from
        self.to = to
    }
}

public struct TaskLink: Codable, Equatable, Sendable {
    /// GitHub items as `https://github.com/<owner>/<repo>/(pull|issues)/<n>`.
    public var url: String
    /// `pr`, `issue`, `slack` or `url`.
    public var kind: String
    public var addedAt: Date
    /// `manual`, or `closes` when a sync added it through a closing reference.
    public var via: String
    /// GitHub's title as of the last sync.
    public var title: String?

    public init(url: String, kind: String, addedAt: Date, via: String = "manual",
                title: String? = nil) {
        self.url = url
        self.kind = kind
        self.addedAt = addedAt
        self.via = via
        self.title = title
    }

    public var isGitHub: Bool { kind == "pr" || kind == "issue" }
}

/// One line of a task's history, `events.jsonl`.
public struct TaskEvent: Codable, Equatable, Sendable {
    /// When it happened (a comment's `createdAt`, a Slack `ts`), not when it was seen.
    public var at: Date
    public var task: String
    /// `github`, `slack`, `note` or `diplomat` (the operator's own edits).
    public var source: String
    public var tags: [String]
    /// The most specific link: the comment, the review, the Slack permalink.
    public var url: String?
    /// The task link it is about.
    public var subject: String?
    /// A one-line excerpt; a note's whole body.
    public var text: String?

    public init(at: Date, task: String, source: String, tags: [String], url: String? = nil,
                subject: String? = nil, text: String? = nil) {
        self.at = at
        self.task = task
        self.source = source
        self.tags = tags
        self.url = url
        self.subject = subject
        self.text = text
    }
}

/// The last-seen GitHub state of one linked item: what a sync diffs against.
public struct GitHubSnapshot: Codable, Equatable, Sendable {
    /// `pr` or `issue`.
    public var kind: String
    public var title: String
    /// `OPEN`, `CLOSED`, `MERGED`, or `MISSING` for an item GitHub says does not exist.
    public var state: String
    public var isDraft: Bool
    public var reviewDecision: String?
    /// The head commit's check rollup: `SUCCESS`, `FAILURE`, `ERROR`, `PENDING`, or nil.
    public var ci: String?
    /// The last known `MERGEABLE` or `CONFLICTING`; GitHub's transient `UNKNOWN` is not kept.
    public var mergeable: String?
    /// Comment and review ids already in the history: every id of the last fetch, then
    /// the newest others, `seenLimit` in all.
    public var seenIDs: [String]
    /// When a sync last wrote it.
    public var updatedAt: Date
    /// Its closing references' URLs: the issues a PR closes, the PRs that close an issue.
    public var closes: [String]
    public var author: String?
    /// A PR's branches and head commit.
    public var baseRef: String?
    public var headRef: String?
    public var headOid: String?

    public static let missing = "MISSING"
    static let seenLimit = 500

    public init(kind: String, title: String, state: String, isDraft: Bool = false,
                reviewDecision: String? = nil, ci: String? = nil, mergeable: String? = nil,
                seenIDs: [String] = [], updatedAt: Date, closes: [String] = [], author: String? = nil,
                baseRef: String? = nil, headRef: String? = nil, headOid: String? = nil) {
        self.kind = kind
        self.title = title
        self.state = state
        self.isDraft = isDraft
        self.reviewDecision = reviewDecision
        self.ci = ci
        self.mergeable = mergeable
        self.seenIDs = seenIDs
        self.updatedAt = updatedAt
        self.closes = closes
        self.author = author
        self.baseRef = baseRef
        self.headRef = headRef
        self.headOid = headOid
    }

    private enum CodingKeys: String, CodingKey {
        case kind, title, state, isDraft, reviewDecision, ci, mergeable, seenIDs, updatedAt
        case closes, author, baseRef, headRef, headOid
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(kind: try c.decode(String.self, forKey: .kind), title: try c.decode(String.self, forKey: .title),
                  state: try c.decode(String.self, forKey: .state),
                  isDraft: try c.decode(Bool.self, forKey: .isDraft),
                  reviewDecision: try c.decodeIfPresent(String.self, forKey: .reviewDecision),
                  ci: try c.decodeIfPresent(String.self, forKey: .ci),
                  mergeable: try c.decodeIfPresent(String.self, forKey: .mergeable),
                  seenIDs: try c.decode([String].self, forKey: .seenIDs),
                  updatedAt: try c.decode(Date.self, forKey: .updatedAt),
                  closes: try c.decodeIfPresent([String].self, forKey: .closes) ?? [],
                  author: try c.decodeIfPresent(String.self, forKey: .author),
                  baseRef: try c.decodeIfPresent(String.self, forKey: .baseRef),
                  headRef: try c.decodeIfPresent(String.self, forKey: .headRef),
                  headOid: try c.decodeIfPresent(String.self, forKey: .headOid))
    }

    public var isOpen: Bool { state == "OPEN" }
}

/// `mirror.json`: every synced item keyed by its link URL, and how far the Slack fold has
/// read Comms' log: the `at` of the newest ping it took, and the ids of the pings it took
/// logged in that same second.
public struct TaskMirror: Codable, Equatable, Sendable {
    public var items: [String: GitHubSnapshot]
    public var commsWatermark: Date?
    public var commsHandled: [String]

    public init(items: [String: GitHubSnapshot] = [:], commsWatermark: Date? = nil,
                commsHandled: [String] = []) {
        self.items = items
        self.commsWatermark = commsWatermark
        self.commsHandled = commsHandled
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(items: try c.decode([String: GitHubSnapshot].self, forKey: .items),
                  commsWatermark: try c.decodeIfPresent(Date.self, forKey: .commsWatermark),
                  commsHandled: try c.decodeIfPresent([String].self, forKey: .commsHandled) ?? [])
    }
}

/// One `tasks-list` row.
public struct TaskView: Codable, Equatable, Sendable {
    public var task: TrackedTask
    public var status: TaskStatus
    public var reasons: [String]
    /// Newest first, at most `historyLimit`.
    public var history: [TaskEvent]
    /// Its last activity: the newer of its own `lastEventAt` and its newest event still in
    /// the history, else its `createdAt`.
    public var lastEventAt: Date
    /// Each synced GitHub link's state by its url: `open`, `draft`, `merged`, `closed` or
    /// `missing`. A link not synced yet has none.
    public var linkStates: [String: String]
    /// Its GitHub links as flat chains, each one what must land before what
    /// (`TaskGraph.rows`).
    public var graph: [[TaskGraph.Node]]
    /// In `auto` mode, the work it still wants started (`GoalWork.plan`); else empty.
    public var work: [GoalWork]

    public static func states(of task: TrackedTask, snapshots: [String: GitHubSnapshot]) -> [String: String] {
        var out: [String: String] = [:]
        for link in task.links where link.isGitHub {
            guard let snap = snapshots[link.url] else { continue }
            out[link.url] = snap.state == "OPEN" && snap.isDraft ? "draft" : snap.state.lowercased()
        }
        return out
    }
}

public enum TaskTags {
    static let maxLength = 64
    private static let colon = try! NSRegularExpression(pattern: "\\s*:\\s*")

    /// Lowercased and trimmed, no whitespace around `:`, other whitespace runs as `-`, at
    /// most `maxLength` characters; empty ones dropped, repeats too, first-seen order kept.
    public static func normalize(_ tags: [String]) -> [String] {
        var out: [String] = []
        for raw in tags {
            let lower = raw.lowercased()
            let words = colon.stringByReplacingMatches(in: lower, range: NSRange(lower.startIndex..., in: lower),
                                                       withTemplate: ":")
                .split(whereSeparator: { $0.isWhitespace })
            let tag = String(words.joined(separator: "-").prefix(maxLength))
            if !tag.isEmpty, !out.contains(tag) { out.append(tag) }
        }
        return out
    }
}

/// A GitHub issue or PR, whichever of the two its number is.
public struct GitHubRef: Equatable, Hashable, Sendable {
    public let owner: String
    public let repo: String
    public let number: Int

    public init(owner: String, repo: String, number: Int) {
        self.owner = owner
        self.repo = repo
        self.number = number
    }

    /// Of a canonical link URL (`TaskLinks.parse`'s, or GitHub's own).
    public init?(url: String) {
        guard let g = TaskLinks.match(url, TaskLinks.canonical), let n = TaskLinks.number(g[3]) else { return nil }
        self.init(owner: g[1], repo: g[2], number: n)
    }

    /// GitHub compares owner and repo without case, and a PR and an issue never share a
    /// number.
    public var key: String { "\(owner)/\(repo)#\(number)".lowercased() }

    /// `#n` in the watched repo, `owner/repo#n` elsewhere.
    public func label(watched: (owner: String, repo: String)) -> String {
        GitHubRef(owner: watched.owner, repo: watched.repo, number: number).key == key
            ? "#\(number)" : "\(owner)/\(repo)#\(number)"
    }

    func url(kind: String) -> String {
        "https://github.com/\(owner)/\(repo)/\(kind == "pr" ? "pull" : "issues")/\(number)"
    }
}

public enum TaskLinks {
    /// What a link field accepts, as its canonical URL and kind; nil for anything else
    /// (including any scheme but http and https):
    /// - `https://github.com/o/r/pull/12`, `…/issues/12`, with any `.diff` / `.patch`,
    ///   `#fragment`, deeper path or query dropped -> `pr` / `issue`
    /// - `o/r#12` -> `issue` until a sync says it is a PR; `#12` -> the same in `watched`
    /// - `https://<workspace>.slack.com/archives/…` -> `slack`
    /// - any other `http(s)://` URL -> `url`
    public static func parse(_ raw: String,
                             watched: (owner: String, repo: String)) -> (url: String, kind: String)? {
        let s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if let g = match(s, itemURL), let n = number(g[4]) {
            let kind = g[3].lowercased() == "pull" ? "pr" : "issue"
            return (GitHubRef(owner: g[1], repo: g[2], number: n).url(kind: kind), kind)
        }
        if let g = match(s, fullRef), let n = number(g[3]) {
            return (GitHubRef(owner: g[1], repo: g[2], number: n).url(kind: "issue"), "issue")
        }
        if let g = match(s, bareRef), let n = number(g[1]) {
            return (GitHubRef(owner: watched.owner, repo: watched.repo, number: n).url(kind: "issue"), "issue")
        }
        guard !s.contains(where: { $0.isWhitespace }), let c = URLComponents(string: s),
              let scheme = c.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let host = c.host?.lowercased(), !host.isEmpty else { return nil }
        if host.hasSuffix(".slack.com"), c.path.hasPrefix("/archives/") { return (s, "slack") }
        return (s, "url")
    }

    /// What two links are the same link by: a GitHub item's `GitHubRef.key`, else the URL
    /// with its scheme and host lowercased.
    public static func identity(_ url: String) -> String {
        if let key = GitHubRef(url: url)?.key { return key }
        guard let scheme = url.range(of: "://") else { return url }
        let host = url[scheme.upperBound...].firstIndex(where: { "/?#".contains($0) }) ?? url.endIndex
        return url[..<host].lowercased() + url[host...]
    }

    /// Every GitHub item `text` names: an item URL on github.com, `o/r#n`, or a bare `#n`
    /// meaning `watched`. A `#n` or `o/r#n` must stand alone: `#12` is not in `#123`,
    /// `a#12` or `…/r#12`; nor is it in the label of Slack's `<url|label>`, which names
    /// what its URL does.
    public static func mentions(in text: String, watched: (owner: String, repo: String)) -> Set<String> {
        let text = slackLabel.stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text),
                                                       withTemplate: "<$1>")
        var keys = Set<String>()
        for g in matches(text, mentionURL) {
            if let n = number(g[3]) { keys.insert(GitHubRef(owner: g[1], repo: g[2], number: n).key) }
        }
        for g in matches(text, mentionRef) {
            if let n = number(g[3]) { keys.insert(GitHubRef(owner: g[1], repo: g[2], number: n).key) }
        }
        for g in matches(text, mentionNumber) {
            if let n = number(g[1]) {
                keys.insert(GitHubRef(owner: watched.owner, repo: watched.repo, number: n).key)
            }
        }
        return keys
    }

    private static let name = "([A-Za-z0-9_.-]+)"

    // Compiled once: the status of every row is derived on each redraw.
    private static func regex(_ pattern: String) -> NSRegularExpression {
        try! NSRegularExpression(pattern: pattern, options: [.caseInsensitive])
    }

    static let canonical = regex("^https://github\\.com/\(name)/\(name)/(?:pull|issues)/([0-9]+)$")
    private static let itemURL = regex(
        "^https?://(?:www\\.)?github\\.com/\(name)/\(name)/(pull|issues)/([0-9]+)(?:\\.(?:diff|patch))?(?:[/?#]\\S*)?$")
    private static let fullRef = regex("^\(name)/\(name)#([0-9]+)$")
    private static let bareRef = regex("^#([0-9]+)$")
    private static let mentionURL = regex(
        "(?<![A-Za-z0-9_./-])(?:https?://)?(?:www\\.)?github\\.com/\(name)/\(name)/(?:pull|issues)/([0-9]+)(?![0-9])")
    private static let mentionRef = regex("(?<![A-Za-z0-9_./-])\(name)/\(name)#([0-9]+)(?![A-Za-z0-9_])")
    private static let mentionNumber = regex("(?<![A-Za-z0-9_./#-])#([0-9]+)(?![A-Za-z0-9_])")
    private static let slackLabel = regex("<([^<>|]*)\\|[^<>]*>")

    /// A positive number of ASCII digits that fits an `Int`.
    static func number(_ s: String) -> Int? {
        Int(s).flatMap { $0 > 0 ? $0 : nil }
    }

    /// The capture groups of `re`'s first match (0 = the whole match).
    static func match(_ s: String, _ re: NSRegularExpression) -> [String]? {
        matches(s, re).first
    }

    private static func matches(_ s: String, _ re: NSRegularExpression) -> [[String]] {
        re.matches(in: s, range: NSRange(s.startIndex..., in: s)).map { m in
            (0..<m.numberOfRanges).map { i in Range(m.range(at: i), in: s).map { String(s[$0]) } ?? "" }
        }
    }
}

public enum TaskStatus: String, Codable, CaseIterable, Sendable {
    case open
    case inProgress = "in-progress"
    case inReview = "in-review"
    case blocked, done, paused

    /// The status and the reason tags behind it; the first rule that holds wins:
    /// 1. pinned -> it, `pinned`
    /// 2. no GitHub links -> `open`, `no-github-links`
    /// 3. every GitHub link MERGED or CLOSED -> `done`, `all-closed`
    /// 4. an open PR failing CI, with changes requested or conflicting -> `blocked`,
    ///    `<item>:ci-failing` / `:changes-requested` / `:conflicts` per cause
    /// 5. an open non-draft PR -> `in-review`, `<item>:approved` / `:awaiting-review` each
    /// 6. else `in-progress`, `<item>:draft` / `:open` per open item, `:missing` per item
    ///    GitHub does not have and `:unsynced` per item no sync has fetched yet
    /// `<item>` is `#n`, or `owner/repo#n` outside `watched`. A task that is neither done
    /// nor paused also gets `stale` when `lastEventAt` is over `staleDays` old.
    public static func derive(_ task: TrackedTask, snapshots: [String: GitHubSnapshot],
                              lastEventAt: Date, now: Date, staleDays: Int,
                              watched: (owner: String, repo: String)) -> (status: TaskStatus, reasons: [String]) {
        let (status, reasons) = rule(task, snapshots, watched)
        guard status != .done, status != .paused,
              now.timeIntervalSince(lastEventAt) > Double(staleDays) * 86_400 else { return (status, reasons) }
        return (status, reasons + ["stale"])
    }

    private static func rule(_ task: TrackedTask, _ snapshots: [String: GitHubSnapshot],
                             _ watched: (owner: String, repo: String)) -> (TaskStatus, [String]) {
        if let pinned = task.pinnedStatus { return (pinned, ["pinned"]) }
        let items = task.links.filter(\.isGitHub).map { link in
            (label: GitHubRef(url: link.url)?.label(watched: watched) ?? link.url,
             snap: snapshots[link.url])
        }
        if items.isEmpty { return (.open, ["no-github-links"]) }
        if items.allSatisfy({ ["MERGED", "CLOSED"].contains($0.snap?.state ?? "") }) {
            return (.done, ["all-closed"])
        }
        let openPRs = items.compactMap { i in
            i.snap.flatMap { $0.kind == "pr" && $0.isOpen ? (i.label, $0) : nil }
        }
        let blocked = openPRs.flatMap { label, s in
            [s.ci == "FAILURE" || s.ci == "ERROR" ? "\(label):ci-failing" : nil,
             s.reviewDecision == "CHANGES_REQUESTED" ? "\(label):changes-requested" : nil,
             s.mergeable == "CONFLICTING" ? "\(label):conflicts" : nil].compactMap { $0 }
        }
        if !blocked.isEmpty { return (.blocked, blocked) }
        let ready = openPRs.filter { !$0.1.isDraft }
        if !ready.isEmpty {
            return (.inReview, ready.map { label, s in
                "\(label):\(s.reviewDecision == "APPROVED" ? "approved" : "awaiting-review")"
            })
        }
        return (.inProgress, items.compactMap { i in
            guard let s = i.snap else { return "\(i.label):unsynced" }
            if s.state == GitHubSnapshot.missing { return "\(i.label):missing" }
            guard s.isOpen else { return nil }
            return "\(i.label):\(s.isDraft ? "draft" : "open")"
        })
    }
}
