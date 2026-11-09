import Foundation

/// A task's GitHub links as flat chains: each row is what must land before what, left to
/// right, e.g. `#12 -> #30 -> #25` (issue #12 is closed by PR #30, which #25 is stacked on).
public enum TaskGraph {
    public struct Node: Codable, Equatable, Sendable {
        /// `pr`, `issue`, or `add`: the PR an open issue has none of yet.
        public var kind: String
        /// The item's link URL; an `add` node's is the issue it would fix.
        public var url: String
        /// `#12` / `o/r#12`; `+` for `add`.
        public var label: String
        /// `open`, `draft`, `merged`, `closed` or `missing`; nil before its first sync and
        /// for `add`.
        public var state: String?
        public var author: String?
        /// An `add` node's: when its fix was last asked for (`TrackedTask.dispatched`).
        public var dispatchedAt: Date?
        /// An `add` node's `GoalWork.key`: what a `dispatched` edit records its fix by.
        public var work: String?

        public init(kind: String, url: String, label: String, state: String? = nil,
                    author: String? = nil, dispatchedAt: Date? = nil, work: String? = nil) {
            self.kind = kind
            self.url = url
            self.label = label
            self.state = state
            self.author = author
            self.dispatchedAt = dispatchedAt
            self.work = work
        }
    }

    static let maxRows = 40

    /// One row per path from an item nothing leads into to one that leads nowhere, along:
    /// - issue -> the PR that closes it (either side's closing reference)
    /// - PR -> the PR whose base branch is its head branch (same repository)
    /// - `task.edges`
    /// An open issue of `watched` (where fixes can be started) that no open, draft or
    /// merged PR of the task closes ends in an `add` node: appended to each row it ends,
    /// or on a row of its own when it leads elsewhere. Every item is on some row. Rows
    /// whose items are all merged or closed come last; at most `maxRows`.
    public static func rows(_ task: TrackedTask, snapshots: [String: GitHubSnapshot],
                            watched: (owner: String, repo: String)) -> [[Node]] {
        let g = Graph(task, snapshots)
        guard !g.links.isEmpty else { return [] }
        var paths: [[Int]] = []
        var covered = Set<Int>()
        func walk(_ path: [Int]) {
            guard paths.count < maxRows else { return }
            let next = g.out[path.last!, default: []].filter { !path.contains($0) }
            if next.isEmpty {
                paths.append(path)
                covered.formUnion(path)
            }
            for n in next { walk(path + [n]) }
        }
        let sources = g.links.indices.filter { g.into[$0, default: 0] == 0 }
        for s in sources { walk([s]) }
        // A cycle has no source: start it from its first item.
        for i in g.links.indices where !covered.contains(i) { walk([i]) }

        let needing = Set(g.links.indices.filter { g.needsPR($0) && isLocal(g.links[$0].url, watched) })
        func node(_ i: Int) -> Node {
            let link = g.links[i], snap = snapshots[link.url]
            return Node(kind: g.kind(i), url: link.url,
                        label: GitHubRef(url: link.url)?.label(watched: watched) ?? link.url,
                        state: snap.map(word), author: snap?.author)
        }
        func add(_ i: Int) -> Node {
            let url = g.links[i].url
            let key = GitHubRef(url: url).map(GoalWork.fixKey)
            return Node(kind: "add", url: url, label: "+", author: snapshots[url]?.author,
                        dispatchedAt: key.flatMap { task.dispatched[$0] }, work: key)
        }
        var rows = paths.map { path -> [Node] in
            path.map(node) + (needing.contains(path.last!) ? [add(path.last!)] : [])
        }
        let ending = Set(paths.map { $0.last! })
        for i in g.links.indices where needing.contains(i) && !ending.contains(i) {
            rows.append([node(i), add(i)])
        }
        let settled: ([Node]) -> Bool = { $0.allSatisfy { ["merged", "closed"].contains($0.state ?? "") } }
        return Array((rows.filter { !settled($0) } + rows.filter(settled)).prefix(maxRows))
    }

    /// The open issues of `watched` no open, draft or merged PR of `task` closes.
    static func issuesNeedingPR(_ task: TrackedTask, _ snapshots: [String: GitHubSnapshot],
                                watched: (owner: String, repo: String)) -> [TaskLink] {
        let g = Graph(task, snapshots)
        return g.links.indices.filter { g.needsPR($0) && isLocal(g.links[$0].url, watched) }.map { g.links[$0] }
    }

    static func isLocal(_ url: String, _ watched: (owner: String, repo: String)) -> Bool {
        guard let ref = GitHubRef(url: url) else { return false }
        return ref.owner.lowercased() == watched.owner.lowercased() && ref.repo.lowercased() == watched.repo.lowercased()
    }

    private static func word(_ s: GitHubSnapshot) -> String {
        s.state == "OPEN" && s.isDraft ? "draft" : s.state.lowercased()
    }

    private struct Graph {
        let links: [TaskLink]
        let snaps: [GitHubSnapshot?]
        var out: [Int: [Int]] = [:]
        var into: [Int: Int] = [:]
        /// Per issue, the PRs a closing reference says close it.
        var closers: [Int: Set<Int>] = [:]

        init(_ task: TrackedTask, _ snapshots: [String: GitHubSnapshot]) {
            links = task.links.filter(\.isGitHub)
            snaps = links.map { snapshots[$0.url] }
            var index: [String: Int] = [:]
            for (i, l) in links.enumerated() { index[TaskLinks.identity(l.url)] = index[TaskLinks.identity(l.url)] ?? i }
            func at(_ url: String) -> Int? { index[TaskLinks.identity(url)] }
            for i in links.indices {
                for c in snaps[i]?.closes ?? [] {
                    guard let j = at(c) else { continue }
                    let (issue, pr) = kind(i) == "pr" ? (j, i) : (i, j)
                    edge(issue, pr)
                    closers[issue, default: []].insert(pr)
                }
            }
            for i in links.indices where kind(i) == "pr" {
                guard let head = snaps[i]?.headRef, let a = GitHubRef(url: links[i].url) else { continue }
                for j in links.indices where j != i && kind(j) == "pr" && snaps[j]?.baseRef == head {
                    guard let b = GitHubRef(url: links[j].url),
                          a.owner.lowercased() == b.owner.lowercased(),
                          a.repo.lowercased() == b.repo.lowercased() else { continue }
                    edge(i, j)
                }
            }
            for e in task.edges {
                if let a = at(e.from), let b = at(e.to) { edge(a, b) }
            }
        }

        func kind(_ i: Int) -> String { snaps[i]?.kind ?? links[i].kind }

        private mutating func edge(_ a: Int, _ b: Int) {
            guard a != b, !(out[a] ?? []).contains(b) else { return }
            out[a, default: []].append(b)
            into[b, default: 0] += 1
        }

        func needsPR(_ i: Int) -> Bool {
            guard kind(i) == "issue", snaps[i]?.state == "OPEN" else { return false }
            return !(closers[i] ?? []).contains { j in
                kind(j) == "pr" && ["OPEN", "MERGED"].contains(snaps[j]?.state ?? "")
            }
        }
    }
}

/// One piece of automatic work an `auto` task wants started.
public struct GoalWork: Codable, Equatable, Sendable {
    public enum Kind: String, Codable, Sendable {
        /// A fix agent for an open issue no PR of the task closes yet.
        case fixIssue = "fix-issue"
        /// A review of an open PR's current head commit (`reviewOnPush`).
        case review
        /// The operator's own open PR: its unresolved review threads get fixed.
        case findings
    }

    public var kind: Kind
    public var url: String
    public var number: Int
    public var author: String?
    /// What `TrackedTask.dispatched` records it by once started (`dispatched` edit):
    /// `fix:<o/r#n>`, `review:<o/r#n>@<head sha>`. `findings` is never recorded: the
    /// front-end's own review-thread reconcile decides when it is owed again.
    public var key: String

    static func fixKey(_ ref: GitHubRef) -> String { "fix:\(ref.key)" }
    static func reviewKey(_ ref: GitHubRef, head: String) -> String { "review:\(ref.key)@\(head)" }

    /// A recordable key's kind (`fix-issue` / `review`), item and, for a review, head.
    public static func parse(_ key: String) -> (kind: String, ref: GitHubRef, head: String?)? {
        func ref(_ s: Substring) -> GitHubRef? {
            guard let hash = s.lastIndex(of: "#"), let slash = s[..<hash].firstIndex(of: "/"),
                  let n = TaskLinks.number(String(s[s.index(after: hash)...])) else { return nil }
            return GitHubRef(owner: String(s[..<slash]), repo: String(s[s.index(after: slash)..<hash]), number: n)
        }
        if key.hasPrefix("fix:"), let r = ref(key.dropFirst(4)) { return (Kind.fixIssue.rawValue, r, nil) }
        if key.hasPrefix("review:"), let at = key.lastIndex(of: "@"), key.index(after: at) < key.endIndex,
           let r = ref(key[key.index(key.startIndex, offsetBy: 7)..<at]) {
            return (Kind.review.rawValue, r, String(key[key.index(after: at)...]))
        }
        return nil
    }

    /// What `task` wants started now; nothing unless its mode is `auto` and it is neither
    /// archived nor pinned. Only items in `watched` (the repo the agents work in):
    /// - `fix-issue` per open issue no open, draft or merged PR of it closes, once: a fix
    ///   that ends without a PR is not asked for again (the graph's `+` asks by hand)
    /// - with `reviewOnPush`, `review` per open PR whose head commit no review was asked
    ///   for yet
    /// - `findings` per open PR `me` wrote
    public static func plan(_ task: TrackedTask, snapshots: [String: GitHubSnapshot], me: String,
                            watched: (owner: String, repo: String)) -> [GoalWork] {
        guard task.effectiveMode == .auto, task.archivedAt == nil, task.pinnedStatus == nil else { return [] }
        let home = GitHubRef(owner: watched.owner, repo: watched.repo, number: 1)
        func local(_ url: String) -> GitHubRef? {
            guard let ref = GitHubRef(url: url),
                  ref.owner.lowercased() == home.owner.lowercased(),
                  ref.repo.lowercased() == home.repo.lowercased() else { return nil }
            return ref
        }
        var out: [GoalWork] = []
        for link in TaskGraph.issuesNeedingPR(task, snapshots, watched: watched) {
            guard let ref = local(link.url) else { continue }
            let key = fixKey(ref)
            if task.dispatched[key] != nil { continue }
            out.append(GoalWork(kind: .fixIssue, url: link.url, number: ref.number,
                                author: snapshots[link.url]?.author, key: key))
        }
        for link in task.links {
            guard let s = snapshots[link.url], s.kind == "pr", s.isOpen, let ref = local(link.url) else { continue }
            if task.reviewOnPush, let head = s.headOid, task.dispatched[reviewKey(ref, head: head)] == nil {
                out.append(GoalWork(kind: .review, url: link.url, number: ref.number, author: s.author,
                                    key: reviewKey(ref, head: head)))
            }
            if !me.isEmpty, s.author?.lowercased() == me.lowercased() {
                out.append(GoalWork(kind: .findings, url: link.url, number: ref.number, author: s.author,
                                    key: "findings:\(ref.key)"))
            }
        }
        return out
    }
}

/// Proposes goal tasks: one sandboxed `claude -p` over the watched repo's open issues and
/// PRs, asked which of them serve one goal ("iPhone Duo support"). Each answer becomes a
/// `proposed` task, or adds items to one the operator has not answered yet.
///
/// Issue and PR text is untrusted: it reaches the model only on stdin, every line quoted,
/// and the call runs with no tools, MCP servers, settings or session, like
/// `CommsClassifier`. What the answer can do is bounded here: it only names numbers of
/// the candidates it was shown, and it only makes or grows `proposed` goals, which
/// nothing acts on until the operator approves one.
public enum TaskGrouper {
    public struct Result: Codable, Equatable, Sendable {
        public var ok: Bool
        public var error: String?
        /// Tasks proposed, and items added to existing goals.
        public var created: Int
        public var added: Int
    }

    /// `grouper.json` in the tasks dir: when the grouper last ran, and its error.
    public struct Mark: Codable, Equatable, Sendable {
        public var ranAt: Date
        public var error: String?
    }

    /// One open issue or PR shown to the model.
    public struct Candidate: Equatable, Sendable {
        public var number: Int
        /// `pr` or `issue`.
        public var kind: String
        public var title: String
        public var body: String
        public var author: String
        public var labels: [String]
        /// The issue numbers a PR closes.
        public var closes: [Int]
        public var url: String

        public init(number: Int, kind: String, title: String, body: String = "", author: String = "",
                    labels: [String] = [], closes: [Int] = [], url: String) {
            self.number = number
            self.kind = kind
            self.title = title
            self.body = body
            self.author = author
            self.labels = labels
            self.closes = closes
            self.url = url
        }
    }

    /// One goal of the model's answer, numbers as it gave them.
    public struct Answer: Equatable, Sendable {
        /// An existing grouper goal's id to add `items` to; nil for a new one.
        public var task: String?
        public var title: String
        public var tags: [String]
        public var items: [Int]
        public var blocks: [(from: Int, to: Int)]

        public init(task: String? = nil, title: String, tags: [String] = [], items: [Int],
                    blocks: [(from: Int, to: Int)] = []) {
            self.task = task
            self.title = title
            self.tags = tags
            self.items = items
            self.blocks = blocks
        }

        public static func == (a: Answer, b: Answer) -> Bool {
            a.task == b.task && a.title == b.title && a.tags == b.tags && a.items == b.items
                && a.blocks.map { [$0.from, $0.to] } == b.blocks.map { [$0.from, $0.to] }
        }
    }

    static func markURL(in dir: URL) -> URL { dir.appendingPathComponent("grouper.json") }

    public static func mark(in dir: URL = TaskStore.dir) -> Mark? {
        (try? Data(contentsOf: markURL(in: dir))).flatMap { try? CommsLog.decoder.decode(Mark.self, from: $0) }
    }

    /// True when it has not run for `groupSecs` (`DIPLOMAT_GOALS_SECS` overrides).
    public static func due(now: Date = Date(), in dir: URL = TaskStore.dir) -> Bool {
        guard let mark = mark(in: dir) else { return true }
        return now.timeIntervalSince(mark.ranAt) >= Double(interval())
    }

    /// `DIPLOMAT_GOALS_SECS`, else `grouper.groupSecs`; at least `grouper.minGroupSecs`.
    public static func interval() -> Int {
        guard let g = try? CoreAssets.tasks().grouper else { return 21_600 }
        let env = ProcessInfo.processInfo.environment["DIPLOMAT_GOALS_SECS"].flatMap { Int($0) }
        return max(g.minGroupSecs, env ?? g.groupSecs)
    }

    /// Fetches the candidates, asks the model, and applies its answer under the lock;
    /// writes `grouper.json` whatever came of it.
    public static func run(now: Date = Date(), dir: URL = TaskStore.dir, binary: String? = nil) async -> Result {
        func finish(_ r: Result) -> Result {
            PrivateFiles.prepare(dir)
            if let data = try? CommsLog.encoder.encode(Mark(ranAt: now, error: r.error)) {
                _ = PrivateFiles.writeWhole(data, to: markURL(in: dir))
            }
            return r
        }
        func failed(_ why: String) -> Result { finish(Result(ok: false, error: why, created: 0, added: 0)) }
        let model: CoreAssets.TasksModel.Grouper
        do { model = try CoreAssets.tasks().grouper } catch { return failed(error.localizedDescription) }
        let candidates: [Candidate]
        do { candidates = try await fetch(bodyChars: model.bodyChars) } catch {
            return failed(error.localizedDescription)
        }
        let existing: [TrackedTask]
        do { existing = try TaskStore.tasks(in: dir) } catch { return failed(error.localizedDescription) }
        guard let exe = binary ?? CommsClassifier.binary() else {
            return failed("the claude CLI was not found (set DIPLOMAT_CLAUDE to its path)")
        }
        let answers: [Answer]
        switch CommsClassifier.execute(exe, arguments(model), input: Data(input(candidates, existing).utf8),
                                       timeout: TimeInterval(model.timeoutSecs)) {
        case .failedToStart(let why): return failed("claude could not be started: \(why)")
        case .timedOut: return failed("claude did not answer within \(model.timeoutSecs)s")
        case .exited(let code, let out, let err):
            guard let parsed = parse(out) else {
                let tail = String(String(decoding: err, as: UTF8.self)
                    .trimmingCharacters(in: .whitespacesAndNewlines).suffix(200))
                return failed("claude exited \(code) with no answer" + (tail.isEmpty ? "" : ": \(tail)"))
            }
            switch parsed {
            case .failure(let why): return failed(why.message)
            case .success(let a): answers = a
            }
        }
        let watched = CoreAssets.repoCoordinates()
        PrivateFiles.prepare(dir)
        let lock = TaskStore.lockURL(in: dir)
        let applied = PrivateFiles.locked(lock) { () -> Result in
            let before: [TrackedTask]
            do { before = try TaskStore.tasks(in: dir) } catch {
                return Result(ok: false, error: error.localizedDescription, created: 0, added: 0)
            }
            let (tasks, events, created, added) = apply(answers, to: before, candidates: candidates, now: now,
                                                        minItems: model.minItems, maxNew: model.maxProposals,
                                                        watched: watched)
            guard TaskStore.append(events, in: dir, maxBytes: (try? CoreAssets.tasks().maxLogBytes) ?? 2_097_152) else {
                return Result(ok: false, error: "could not append to \(TaskStore.eventsURL(in: dir).path)",
                              created: 0, added: 0)
            }
            let after = TaskStore.noting(events, in: tasks)
            guard after == before || TaskStore.save(after, in: dir) else {
                return Result(ok: false, error: "could not write \(TaskStore.tasksURL(in: dir).path)",
                              created: 0, added: 0)
            }
            return Result(ok: true, error: nil, created: created, added: added)
        }
        return finish(applied ?? Result(ok: false, error: "could not lock \(lock.path)", created: 0, added: 0))
    }

    public static func arguments(_ model: CoreAssets.TasksModel.Grouper) -> [String] {
        ["-p", "--model", model.model,
         "--tools", "",
         "--strict-mcp-config",
         "--disable-slash-commands",
         "--setting-sources", "",
         "--no-session-persistence",
         "--output-format", "json",
         "--json-schema", model.schema,
         prompt()]
    }

    /// The instructions, the CLI's last argument.
    public static func prompt() -> String {
        guard let g = try? CoreAssets.tasks().grouper else { return "" }
        let (owner, repo) = CoreAssets.repoCoordinates()
        return CommsText.fill(g.instructions.joined(separator: "\n"), [
            "owner": owner, "repo": repo, "minItems": String(g.minItems),
        ])
    }

    /// The stdin: every goal already tracked, then every candidate, each one's text with
    /// every line quoted by `| `, so nothing in it can pass for a line of the input's own.
    public static func input(_ candidates: [Candidate], _ tasks: [TrackedTask]) -> String {
        var out = ["EXISTING GOALS"]
        let goals = tasks.filter { $0.origin == TrackedTask.grouperOrigin || $0.archivedAt == nil }
        if goals.isEmpty { out.append("(none)") }
        for t in goals {
            let numbers = t.links.compactMap { GitHubRef(url: $0.url)?.number }.map { "#\($0)" }
            let status: String
            if t.effectiveMode == .dismissed {
                status = "dismissed: never propose it again"
            } else if acceptsAdditions(t) {
                status = "proposed goal: you may add items to it"
            } else {
                status = "tracked: do not add to it or repeat it"
            }
            out.append("goal \(t.id) (\(status)), items \(numbers.joined(separator: " ")):")
            out.append(quote(t.title))
        }
        out.append("")
        out.append("CANDIDATES")
        for c in candidates {
            var head = "\(c.kind == "pr" ? "PR" : "issue") #\(c.number)"
            if !c.closes.isEmpty { head += ", closes " + c.closes.map { "#\($0)" }.joined(separator: " ") }
            out.append(head + ":")
            var text = "Title: \(c.title)"
            if !c.labels.isEmpty { text += "\nLabels: \(c.labels.joined(separator: ", "))" }
            if !c.body.isEmpty { text += "\n\(c.body)" }
            out.append(quote(text))
        }
        return out.joined(separator: "\n")
    }

    private static func quote(_ text: String) -> String {
        text.split(omittingEmptySubsequences: false, whereSeparator: \.isNewline)
            .map { $0.isEmpty ? "|" : "| " + $0 }.joined(separator: "\n")
    }

    /// The answer in `claude`'s output (its `structured_output`, or a JSON `result`); a
    /// bare `{"goals": …}` counts too. Nil when none is there.
    public static func parse(_ output: Data) -> Swift.Result<[Answer], ParseError>? {
        guard let object = CommsDecision.answerObject(in: output, key: "goals") else { return nil }
        let root: [String: Any]
        switch object {
        case .failed(let d): return .failure(ParseError(message: String(d.reason.dropFirst(CommsClassifier.failurePrefix.count))))
        case .object(let o):
            guard let o else { return .failure(ParseError(message: "the grouper's answer was not a JSON object")) }
            root = o
        }
        let goals = root["goals"] as? [[String: Any]] ?? []
        return .success(goals.compactMap { g in
            guard let title = g["title"] as? String else { return nil }
            let numbers: (Any?) -> [Int] = { ($0 as? [Any] ?? []).compactMap { ($0 as? NSNumber)?.intValue } }
            let blocks = (g["blocks"] as? [[String: Any]] ?? []).compactMap { b -> (from: Int, to: Int)? in
                guard let f = (b["from"] as? NSNumber)?.intValue, let t = (b["to"] as? NSNumber)?.intValue else { return nil }
                return (f, t)
            }
            return Answer(task: g["task"] as? String, title: title,
                          tags: (g["tags"] as? [Any] ?? []).compactMap { $0 as? String },
                          items: numbers(g["items"]), blocks: blocks)
        })
    }

    public struct ParseError: Error, Equatable {
        public let message: String
    }

    static let titleChars = 80

    /// A goal the grouper proposed that the operator has not answered yet: once observed
    /// or automated, what it holds is the operator's call.
    static func acceptsAdditions(_ t: TrackedTask) -> Bool {
        t.origin == TrackedTask.grouperOrigin && t.effectiveMode == .proposed && t.archivedAt == nil
    }

    /// `answers` applied to `tasks`, trusting only numbers among `candidates`:
    /// - with `task`, a goal still `proposed` (`acceptsAdditions`): its new items join it
    ///   (`via: grouper`), but not those it `dismissed`; a `linked`, `via:grouper` event each
    /// - else a `proposed` task (origin `grouper`, tags as given, plus `goal`) of its items
    ///   no other unarchived task links, kept when at least `minItems` remain and at most
    ///   half of them belong to one dismissed goal; at most `maxNew` of them
    /// `blocks` become edges between the goal's own items. Returns the tasks, the events,
    /// and how many were created and added.
    public static func apply(_ answers: [Answer], to tasks: [TrackedTask], candidates: [Candidate], now: Date,
                             minItems: Int, maxNew: Int, watched: (owner: String, repo: String))
        -> (tasks: [TrackedTask], events: [TaskEvent], created: Int, added: Int) {
        var tasks = tasks
        var events: [TaskEvent] = []
        var created = 0, added = 0
        let byNumber = Dictionary(candidates.map { ($0.number, $0) }, uniquingKeysWith: { a, _ in a })
        func link(_ c: Candidate) -> TaskLink {
            TaskLink(url: c.url, kind: c.kind, addedAt: now, via: TrackedTask.grouperOrigin, title: c.title)
        }
        func edges(_ a: Answer, among links: [TaskLink]) -> [TaskEdge] {
            let held = Set(links.map { TaskLinks.identity($0.url) })
            return a.blocks.compactMap { b in
                guard b.from != b.to, let f = byNumber[b.from], let t = byNumber[b.to],
                      held.contains(TaskLinks.identity(f.url)), held.contains(TaskLinks.identity(t.url)) else { return nil }
                return TaskEdge(from: f.url, to: t.url)
            }
        }
        func linkedElsewhere(_ c: Candidate, except id: String? = nil) -> Bool {
            tasks.contains { t in
                t.id != id && t.archivedAt == nil && t.links.contains { TaskLinks.identity($0.url) == TaskLinks.identity(c.url) }
            }
        }
        for a in answers {
            var seen = Set<Int>()
            let items = a.items.filter { seen.insert($0).inserted }.compactMap { byNumber[$0] }
            if let id = a.task {
                guard let i = tasks.firstIndex(where: { $0.id == id && acceptsAdditions($0) }) else { continue }
                let refused = Set(tasks[i].dismissed.map(TaskLinks.identity))
                for c in items {
                    let identity = TaskLinks.identity(c.url)
                    guard !refused.contains(identity),
                          !tasks[i].links.contains(where: { TaskLinks.identity($0.url) == identity }),
                          !linkedElsewhere(c, except: id) else { continue }
                    tasks[i].links.append(link(c))
                    events.append(TaskEvent(at: now, task: id, source: "diplomat", tags: ["linked", "via:grouper"],
                                            url: c.url, subject: c.url))
                    added += 1
                }
                for e in edges(a, among: tasks[i].links) where !tasks[i].edges.contains(e) { tasks[i].edges.append(e) }
                continue
            }
            guard created < maxNew else { continue }
            let title = String(a.title.trimmingCharacters(in: .whitespacesAndNewlines).prefix(titleChars))
            let fresh = items.filter { !linkedElsewhere($0) }
            guard !title.isEmpty, fresh.count >= max(1, minItems) else { continue }
            let identities = Set(fresh.map { TaskLinks.identity($0.url) })
            let repeatsDismissed = tasks.contains { t in
                t.effectiveMode == .dismissed
                    && 2 * t.links.filter { identities.contains(TaskLinks.identity($0.url)) }.count > fresh.count
            }
            guard !repeatsDismissed else { continue }
            var id: String
            repeat {
                id = "t-" + UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(8).lowercased()
            } while tasks.contains { $0.id == id }
            let links = fresh.map(link)
            let task = TrackedTask(id: id, title: title, tags: TaskTags.normalize(["goal"] + a.tags),
                                   links: links, createdAt: now, origin: TrackedTask.grouperOrigin,
                                   mode: .proposed, edges: edges(a, among: links))
            tasks.append(task)
            events.append(TaskEvent(at: now, task: id, source: "diplomat",
                                    tags: ["created", "via:grouper"] + task.tags.map { "tag:+\($0)" }))
            events += links.map { TaskEvent(at: now, task: id, source: "diplomat", tags: ["link:+"], url: $0.url,
                                            subject: $0.url) }
            created += 1
        }
        return (tasks, events, created, added)
    }

    /// The watched repo's open issues and PRs (`graphql/goal-candidates.graphql`), each body
    /// cut to `bodyChars`.
    static func fetch(bodyChars: Int) async throws -> [Candidate] {
        try candidates(fromResponse: try await GH.graphql("goal-candidates", withRepo: true), bodyChars: bodyChars)
    }

    static func candidates(fromResponse data: Data, bodyChars: Int) throws -> [Candidate] {
        guard let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let repo = (root["data"] as? [String: Any])?["repository"] as? [String: Any] else {
            throw GHError.graphql(messages: ["the answer holds no repository"])
        }
        func nodes(_ v: Any?) -> [[String: Any]] { (v as? [String: Any])?["nodes"] as? [[String: Any]] ?? [] }
        func candidate(_ n: [String: Any], kind: String) -> Candidate? {
            guard let number = (n["number"] as? NSNumber)?.intValue, let title = n["title"] as? String,
                  let url = n["url"] as? String else { return nil }
            let body = TaskSync.excerpt(n["bodyText"] as? String ?? "", bodyChars)
            return Candidate(number: number, kind: kind, title: title, body: body,
                             author: (n["author"] as? [String: Any])?["login"] as? String ?? "ghost",
                             labels: nodes(n["labels"]).compactMap { $0["name"] as? String },
                             closes: nodes(n["closingIssuesReferences"]).compactMap { ($0["number"] as? NSNumber)?.intValue },
                             url: url)
        }
        return nodes(repo["issues"]).compactMap { candidate($0, kind: "issue") }
            + nodes(repo["pullRequests"]).compactMap { candidate($0, kind: "pr") }
    }
}
