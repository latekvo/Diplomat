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

    public init(id: String, title: String, tags: [String] = [], links: [TaskLink] = [],
                createdAt: Date, pinnedStatus: TaskStatus? = nil, archivedAt: Date? = nil) {
        self.id = id
        self.title = title
        self.tags = tags
        self.links = links
        self.createdAt = createdAt
        self.pinnedStatus = pinnedStatus
        self.archivedAt = archivedAt
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
    /// Comment and review ids already in the history, newest `seenLimit`.
    public var seenIDs: [String]
    /// When a sync last wrote it.
    public var updatedAt: Date

    public static let missing = "MISSING"
    static let seenLimit = 500

    public init(kind: String, title: String, state: String, isDraft: Bool = false,
                reviewDecision: String? = nil, ci: String? = nil, mergeable: String? = nil,
                seenIDs: [String] = [], updatedAt: Date) {
        self.kind = kind
        self.title = title
        self.state = state
        self.isDraft = isDraft
        self.reviewDecision = reviewDecision
        self.ci = ci
        self.mergeable = mergeable
        self.seenIDs = seenIDs
        self.updatedAt = updatedAt
    }

    public var isOpen: Bool { state == "OPEN" }
}

/// `mirror.json`: every synced item keyed by its link URL, and how far the Slack fold has
/// read Comms' log (a ping's `at`).
public struct TaskMirror: Codable, Equatable, Sendable {
    public var items: [String: GitHubSnapshot]
    public var commsWatermark: Date?

    public init(items: [String: GitHubSnapshot] = [:], commsWatermark: Date? = nil) {
        self.items = items
        self.commsWatermark = commsWatermark
    }
}

/// One `tasks-list` row.
public struct TaskView: Codable, Equatable, Sendable {
    public var task: TrackedTask
    public var status: TaskStatus
    public var reasons: [String]
    /// Newest first, at most `historyLimit`.
    public var history: [TaskEvent]
    public var lastEventAt: Date?
    /// Each synced GitHub link's state by its url: `open`, `draft`, `merged`, `closed` or
    /// `missing`. A link not synced yet has none.
    public var linkStates: [String: String]

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

    /// Lowercased and trimmed, no whitespace around `:`, other whitespace runs as `-`, at
    /// most `maxLength` characters; empty ones dropped, repeats too, first-seen order kept.
    public static func normalize(_ tags: [String]) -> [String] {
        var out: [String] = []
        for raw in tags {
            let words = raw.lowercased().replacingOccurrences(of: "\\s*:\\s*", with: ":", options: .regularExpression)
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
        guard let g = TaskLinks.match(url, "^https://github\\.com/\(TaskLinks.name)/\(TaskLinks.name)/(?:pull|issues)/([0-9]+)$"),
              let n = TaskLinks.number(g[3]) else { return nil }
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
    /// - `https://github.com/o/r/pull/12`, `…/issues/12`, with any `#fragment`, deeper path
    ///   or query dropped -> `pr` / `issue`
    /// - `o/r#12` -> `issue` until a sync says it is a PR; `#12` -> the same in `watched`
    /// - `https://<workspace>.slack.com/archives/…` -> `slack`
    /// - any other `http(s)://` URL -> `url`
    public static func parse(_ raw: String,
                             watched: (owner: String, repo: String)) -> (url: String, kind: String)? {
        let s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if let g = match(s, "^https?://(?:www\\.)?github\\.com/\(name)/\(name)/(pull|issues)/([0-9]+)(?:[/?#]\\S*)?$"),
           let n = number(g[4]) {
            let kind = g[3].lowercased() == "pull" ? "pr" : "issue"
            return (GitHubRef(owner: g[1], repo: g[2], number: n).url(kind: kind), kind)
        }
        if let g = match(s, "^\(name)/\(name)#([0-9]+)$"), let n = number(g[3]) {
            return (GitHubRef(owner: g[1], repo: g[2], number: n).url(kind: "issue"), "issue")
        }
        if let g = match(s, "^#([0-9]+)$"), let n = number(g[1]) {
            return (GitHubRef(owner: watched.owner, repo: watched.repo, number: n).url(kind: "issue"), "issue")
        }
        guard !s.contains(where: { $0.isWhitespace }), let c = URLComponents(string: s),
              let scheme = c.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let host = c.host?.lowercased(), !host.isEmpty else { return nil }
        if host.hasSuffix(".slack.com"), c.path.hasPrefix("/archives/") { return (s, "slack") }
        return (s, "url")
    }

    /// What two links are the same link by: a GitHub item's `GitHubRef.key`, else the URL.
    public static func identity(_ url: String) -> String {
        GitHubRef(url: url)?.key ?? url
    }

    /// Every GitHub item `text` names: an item URL, `o/r#n`, or a bare `#n` meaning
    /// `watched`. A `#n` or `o/r#n` must stand alone: `#12` is not in `#123`, `a#12` or
    /// `…/r#12`.
    public static func mentions(in text: String, watched: (owner: String, repo: String)) -> Set<String> {
        var keys = Set<String>()
        for g in matches(text, "github\\.com/\(name)/\(name)/(?:pull|issues)/([0-9]+)(?![0-9])") {
            if let n = number(g[3]) { keys.insert(GitHubRef(owner: g[1], repo: g[2], number: n).key) }
        }
        for g in matches(text, "(?<![A-Za-z0-9_./-])\(name)/\(name)#([0-9]+)(?![A-Za-z0-9_])") {
            if let n = number(g[3]) { keys.insert(GitHubRef(owner: g[1], repo: g[2], number: n).key) }
        }
        for g in matches(text, "(?<![A-Za-z0-9_./#-])#([0-9]+)(?![A-Za-z0-9_])") {
            if let n = number(g[1]) {
                keys.insert(GitHubRef(owner: watched.owner, repo: watched.repo, number: n).key)
            }
        }
        return keys
    }

    static let name = "([A-Za-z0-9_.-]+)"

    /// A positive number of ASCII digits that fits an `Int`.
    static func number(_ s: String) -> Int? {
        Int(s).flatMap { $0 > 0 ? $0 : nil }
    }

    /// The capture groups of `pattern`'s first match (0 = the whole match).
    static func match(_ s: String, _ pattern: String) -> [String]? {
        matches(s, pattern).first
    }

    private static func matches(_ s: String, _ pattern: String) -> [[String]] {
        guard let re = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]) else { return [] }
        return re.matches(in: s, range: NSRange(s.startIndex..., in: s)).map { m in
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
    /// nor paused also gets `stale` when its newest event is over `staleDays` old.
    public static func derive(_ task: TrackedTask, snapshots: [String: GitHubSnapshot],
                              lastEventAt: Date?, now: Date, staleDays: Int,
                              watched: (owner: String, repo: String)) -> (status: TaskStatus, reasons: [String]) {
        let (status, reasons) = rule(task, snapshots, watched)
        guard status != .done, status != .paused, let last = lastEventAt,
              now.timeIntervalSince(last) > Double(staleDays) * 86_400 else { return (status, reasons) }
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
