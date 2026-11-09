import Foundation

/// One operator edit, as `tasks-edit` reads it: `{"op": "create" | "update" | "link" |
/// "unlink" | "note" | "archive" | "unarchive", …}`.
public enum TaskEdit: Decodable, Equatable, Sendable {
    case create(title: String, tags: [String], links: [String])
    /// Nil leaves a field as it is; a `pinnedStatus` of `""` unpins.
    case update(id: String, title: String?, tags: [String]?, pinnedStatus: String?)
    case link(id: String, url: String)
    case unlink(id: String, url: String)
    case note(id: String, text: String?, tags: [String], url: String?)
    case archive(id: String)
    case unarchive(id: String)

    private enum Key: String, CodingKey { case op, id, title, tags, links, pinnedStatus, url, text }

    private struct AnyKey: CodingKey {
        var stringValue: String
        var intValue: Int? { nil }
        init(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { nil }
    }

    /// Each op's keys besides `op`; any other key is refused, so a misspelled one is not
    /// silently left out.
    private static let keys: [String: [Key]] = [
        "create": [.title, .tags, .links], "update": [.id, .title, .tags, .pinnedStatus],
        "link": [.id, .url], "unlink": [.id, .url], "note": [.id, .text, .tags, .url],
        "archive": [.id], "unarchive": [.id],
    ]

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Key.self)
        let op = try c.decode(String.self, forKey: .op)
        func id() throws -> String { try c.decode(String.self, forKey: .id) }
        if let allowed = Self.keys[op],
           let extra = try decoder.container(keyedBy: AnyKey.self).allKeys.map(\.stringValue)
               .sorted().first(where: { $0 != Key.op.rawValue && !allowed.map(\.rawValue).contains($0) }) {
            throw DecodingError.dataCorruptedError(
                forKey: .op, in: c,
                debugDescription: "unknown key \"\(extra)\" for op \(op) (it takes "
                    + allowed.map(\.rawValue).joined(separator: ", ") + ")")
        }
        switch op {
        case "create":
            self = .create(title: try c.decode(String.self, forKey: .title),
                           tags: try c.decodeIfPresent([String].self, forKey: .tags) ?? [],
                           links: try c.decodeIfPresent([String].self, forKey: .links) ?? [])
        case "update":
            self = .update(id: try id(), title: try c.decodeIfPresent(String.self, forKey: .title),
                           tags: try c.decodeIfPresent([String].self, forKey: .tags),
                           pinnedStatus: try c.decodeIfPresent(String.self, forKey: .pinnedStatus))
        case "link": self = .link(id: try id(), url: try c.decode(String.self, forKey: .url))
        case "unlink": self = .unlink(id: try id(), url: try c.decode(String.self, forKey: .url))
        case "note":
            self = .note(id: try id(), text: try c.decodeIfPresent(String.self, forKey: .text),
                         tags: try c.decodeIfPresent([String].self, forKey: .tags) ?? [],
                         url: try c.decodeIfPresent(String.self, forKey: .url))
        case "archive": self = .archive(id: try id())
        case "unarchive": self = .unarchive(id: try id())
        default:
            throw DecodingError.dataCorruptedError(
                forKey: .op, in: c,
                debugDescription: "unknown op \"\(op)\" (expected create | update | link | unlink"
                    + " | note | archive | unarchive)")
        }
    }
}

public struct TaskError: LocalizedError, Equatable {
    public let message: String
    /// The task as the edit left it, when the edit itself was saved and only its history
    /// line was not: repeating the edit would apply it twice.
    public let saved: TrackedTask?
    public var errorDescription: String? { message }

    init(message: String, saved: TrackedTask? = nil) {
        self.message = message
        self.saved = saved
    }

    /// One line on why JSON did not decode: `not valid JSON (line 3)`,
    /// `tasks[2].links[0]: missing key url`, `tasks[0].title: Expected to decode String…`.
    public static func describe(_ error: Error) -> String {
        guard let error = error as? DecodingError else { return error.localizedDescription }
        func at(_ path: [CodingKey]) -> String {
            path.map { $0.intValue.map { "[\($0)]" } ?? ".\($0.stringValue)" }.joined()
                .trimmingCharacters(in: CharacterSet(charactersIn: "."))
        }
        func located(_ path: [CodingKey], _ what: String) -> String {
            path.isEmpty ? what : "\(at(path)): \(what)"
        }
        switch error {
        case .dataCorrupted(let c) where c.codingPath.isEmpty:
            let detail = ((c.underlyingError as NSError?)?.userInfo["NSDebugDescription"] as? String)
                ?? c.underlyingError.map { "\($0)" } ?? ""
            let line = detail.range(of: "line [0-9]+", options: .regularExpression).map { " (\(detail[$0]))" }
            return "not valid JSON" + (line ?? "")
        case .dataCorrupted(let c), .typeMismatch(_, let c), .valueNotFound(_, let c):
            return located(c.codingPath, c.debugDescription)
        case .keyNotFound(let key, let c):
            return located(c.codingPath, "missing key \(key.stringValue)")
        @unknown default:
            return "\(error)"
        }
    }
}

/// `dir` holds `tasks.json` (`{"tasks": [TrackedTask]}`), `events.jsonl` (the history),
/// `mirror.json` (`TaskMirror`) and `tasks.lock`, which every writer of the three holds:
/// directory 0700, files 0600. JSON goes through `CommsLog.encoder`/`decoder`.
public enum TaskStore {
    public static var dir: URL {
        if let env = ProcessInfo.processInfo.environment["DIPLOMAT_TASKS_DIR"], !env.isEmpty {
            return URL(fileURLWithPath: env, isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".diplomat/tasks", isDirectory: true)
    }

    static func tasksURL(in dir: URL) -> URL { dir.appendingPathComponent("tasks.json") }
    static func eventsURL(in dir: URL) -> URL { dir.appendingPathComponent("events.jsonl") }
    static func mirrorURL(in dir: URL) -> URL { dir.appendingPathComponent("mirror.json") }
    static func lockURL(in dir: URL) -> URL { dir.appendingPathComponent("tasks.lock") }

    private struct File: Codable { var tasks: [TrackedTask] }

    /// Every task, archived ones included; none before the first is made. Throws for a
    /// file that is there but does not read, so no edit writes over it.
    public static func tasks(in dir: URL = dir) throws -> [TrackedTask] {
        let url = tasksURL(in: dir)
        guard FileManager.default.fileExists(atPath: url.path) else { return [] }
        do {
            return try CommsLog.decoder.decode(File.self, from: Data(contentsOf: url)).tasks
        } catch {
            throw TaskError(message: "\(url.path): \(TaskError.describe(error))")
        }
    }

    /// Oldest first; a torn line costs only that line.
    public static func events(in dir: URL = dir) -> [TaskEvent] {
        guard let data = try? Data(contentsOf: eventsURL(in: dir)) else { return [] }
        let d = CommsLog.decoder
        return data.split(separator: UInt8(ascii: "\n")).compactMap {
            try? d.decode(TaskEvent.self, from: Data($0))
        }
    }

    /// Empty when missing or unreadable: it is only what the last sync saw, so the next one
    /// rebuilds it, each item a first sight again.
    public static func mirror(in dir: URL = dir) -> TaskMirror {
        (try? Data(contentsOf: mirrorURL(in: dir)))
            .flatMap { try? CommsLog.decoder.decode(TaskMirror.self, from: $0) } ?? TaskMirror()
    }

    /// What `tasks-list` prints: unarchived tasks (all with `archived`), each with its
    /// status and newest-first history; tasks not done first, then by last activity.
    public static func views(archived: Bool = false, now: Date = Date(),
                             in dir: URL = dir) throws -> [TaskView] {
        let assets = try CoreAssets.tasks()
        let watched = CoreAssets.repoCoordinates()
        let snapshots = mirror(in: dir).items
        let history = Dictionary(grouping: events(in: dir).enumerated(), by: { $0.element.task })
        let views = try tasks(in: dir).filter { archived || $0.archivedAt == nil }.map { task in
            let mine = (history[task.id] ?? []).sorted {
                ($0.element.at, $0.offset) > ($1.element.at, $1.offset)
            }.map(\.element)
            let last = [task.lastEventAt, mine.first?.at].compactMap { $0 }.max() ?? task.createdAt
            let (status, reasons) = TaskStatus.derive(task, snapshots: snapshots, lastEventAt: last,
                                                      now: now, staleDays: assets.staleDays,
                                                      watched: watched)
            return TaskView(task: task, status: status, reasons: reasons,
                            history: Array(mine.prefix(assets.historyLimit)), lastEventAt: last,
                            linkStates: TaskView.states(of: task, snapshots: snapshots))
        }
        return views.sorted { a, b in
            if (a.status == .done) != (b.status == .done) { return b.status == .done }
            return a.lastEventAt != b.lastEventAt ? a.lastEventAt > b.lastEventAt : a.task.id < b.task.id
        }
    }

    /// Applies `edit` under the lock and returns the task as it now is. Every change to
    /// tags, links, pin or archival also appends a `diplomat` event naming it (`tag:+x`,
    /// `link:-`, `pinned:done`, `archived`…); a note appends its `note` event. A note's
    /// text over `noteChars` characters (Unicode scalars) is refused. Throws a `TaskError`
    /// with `saved` set when the edit was saved but its event was not appended.
    @discardableResult
    public static func edit(_ edit: TaskEdit, now: Date = Date(), in dir: URL = dir) throws -> TrackedTask {
        let assets = try CoreAssets.tasks()
        let watched = CoreAssets.repoCoordinates()
        PrivateFiles.prepare(dir)
        guard let task = try PrivateFiles.locked(lockURL(in: dir), { () throws -> TrackedTask in
            let before = try tasks(in: dir)
            var tasks = before
            let (task, events) = try apply(edit, to: &tasks, now: now, watched: watched,
                                           noteChars: assets.noteChars)
            guard tasks == before || save(tasks, in: dir) else {
                throw TaskError(message: "could not write \(tasksURL(in: dir).path)")
            }
            guard append(events, in: dir, maxBytes: assets.maxLogBytes) else {
                let path = eventsURL(in: dir).path
                guard tasks != before else { throw TaskError(message: "could not append to \(path)") }
                throw TaskError(message: "the edit was saved, but its history line was not written to \(path)",
                                saved: task)
            }
            // Not fatal: until the history rotates, `views` reads the same time off the line.
            let noted = noting(events, in: tasks)
            if noted != tasks { _ = save(noted, in: dir) }
            return noted.first { $0.id == task.id } ?? task
        }) else { throw TaskError(message: "could not lock \(lockURL(in: dir).path)") }
        return task
    }

    private static func apply(_ edit: TaskEdit, to tasks: inout [TrackedTask], now: Date,
                              watched: (owner: String, repo: String),
                              noteChars: Int) throws -> (TrackedTask, [TaskEvent]) {
        func event(_ id: String, _ tags: [String], url: String? = nil) -> TaskEvent {
            TaskEvent(at: now, task: id, source: "diplomat", tags: tags, url: url, subject: url)
        }
        func parsed(_ raw: String) throws -> (url: String, kind: String) {
            guard let link = TaskLinks.parse(raw, watched: watched) else {
                throw TaskError(message: "not a link: \(raw)")
            }
            return link
        }
        func index(_ id: String) throws -> Int {
            guard let i = tasks.firstIndex(where: { $0.id == id }) else {
                throw TaskError(message: "no task \(id)")
            }
            return i
        }

        switch edit {
        case let .create(title, tags, links):
            let title = title.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !title.isEmpty else { throw TaskError(message: "a task needs a title") }
            var id: String
            repeat {
                id = "t-" + UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(8).lowercased()
            } while tasks.contains { $0.id == id }
            var task = TrackedTask(id: id, title: title, tags: TaskTags.normalize(tags), createdAt: now)
            for raw in links {
                let link = try parsed(raw)
                if !task.links.contains(where: { TaskLinks.identity($0.url) == TaskLinks.identity(link.url) }) {
                    task.links.append(TaskLink(url: link.url, kind: link.kind, addedAt: now))
                }
            }
            tasks.append(task)
            return (task, [event(id, ["created"] + task.tags.map { "tag:+\($0)" })]
                        + task.links.map { event(id, ["link:+"], url: $0.url) })

        case let .update(id, title, tags, pinnedStatus):
            let i = try index(id)
            var events: [TaskEvent] = []
            if let title {
                let title = title.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !title.isEmpty else { throw TaskError(message: "a task needs a title") }
                tasks[i].title = title
            }
            if let tags {
                let new = TaskTags.normalize(tags), old = tasks[i].tags
                let changes = new.filter { !old.contains($0) }.map { "tag:+\($0)" }
                    + old.filter { !new.contains($0) }.map { "tag:-\($0)" }
                tasks[i].tags = new
                if !changes.isEmpty { events.append(event(id, changes)) }
            }
            if let pinnedStatus {
                let pin: TaskStatus?
                switch pinnedStatus {
                case "": pin = nil
                case TaskStatus.done.rawValue, TaskStatus.paused.rawValue:
                    pin = TaskStatus(rawValue: pinnedStatus)
                default:
                    throw TaskError(message: "pinnedStatus is done, paused or \"\" (unpin), not \"\(pinnedStatus)\"")
                }
                if pin != tasks[i].pinnedStatus {
                    tasks[i].pinnedStatus = pin
                    events.append(event(id, [pin.map { "pinned:\($0.rawValue)" } ?? "unpinned"]))
                }
            }
            return (tasks[i], events)

        case let .link(id, raw):
            let i = try index(id)
            let link = try parsed(raw)
            let identity = TaskLinks.identity(link.url)
            tasks[i].dismissed.removeAll { TaskLinks.identity($0) == identity }
            guard !tasks[i].links.contains(where: { TaskLinks.identity($0.url) == identity }) else {
                return (tasks[i], [])
            }
            tasks[i].links.append(TaskLink(url: link.url, kind: link.kind, addedAt: now))
            return (tasks[i], [event(id, ["link:+"], url: link.url)])

        case let .unlink(id, raw):
            let i = try index(id)
            let identity = TaskLinks.identity(try parsed(raw).url)
            guard let j = tasks[i].links.firstIndex(where: { TaskLinks.identity($0.url) == identity }) else {
                throw TaskError(message: "task \(id) has no link \(raw)")
            }
            let gone = tasks[i].links.remove(at: j)
            tasks[i].dismissed.append(gone.url)
            return (tasks[i], [event(id, ["link:-"], url: gone.url)])

        case let .note(id, text, tags, url):
            let i = try index(id)
            let text = text?.trimmingCharacters(in: .whitespacesAndNewlines)
            let tags = TaskTags.normalize(tags)
            let link = try url.map { try parsed($0).url }
            guard !(text ?? "").isEmpty || !tags.isEmpty || link != nil else {
                throw TaskError(message: "a note needs text, tags or a url")
            }
            guard (text ?? "").unicodeScalars.count <= noteChars else {
                throw TaskError(message: "a note's text is at most \(noteChars) characters, not "
                                + "\(text?.unicodeScalars.count ?? 0)")
            }
            return (tasks[i], [TaskEvent(at: now, task: id, source: "note", tags: tags, url: link,
                                         text: (text ?? "").isEmpty ? nil : text)])

        case let .archive(id):
            let i = try index(id)
            guard tasks[i].archivedAt == nil else { return (tasks[i], []) }
            tasks[i].archivedAt = now
            return (tasks[i], [event(id, ["archived"])])

        case let .unarchive(id):
            let i = try index(id)
            guard tasks[i].archivedAt != nil else { return (tasks[i], []) }
            tasks[i].archivedAt = nil
            return (tasks[i], [event(id, ["unarchived"])])
        }
    }

    /// `tasks` with each one's `lastEventAt` moved up to the newest of `events` it has.
    static func noting(_ events: [TaskEvent], in tasks: [TrackedTask]) -> [TrackedTask] {
        let newest = Dictionary(events.map { ($0.task, $0.at) }, uniquingKeysWith: max)
        return tasks.map { task in
            var task = task
            if let at = newest[task.id], task.lastEventAt.map({ at > $0 }) ?? true { task.lastEventAt = at }
            return task
        }
    }

    /// Whole, via a temp file. The caller holds the lock.
    static func save(_ tasks: [TrackedTask], in dir: URL) -> Bool {
        guard let data = try? CommsLog.encoder.encode(File(tasks: tasks)) else { return false }
        return PrivateFiles.writeWhole(data, to: tasksURL(in: dir))
    }

    /// All of `events` in one `write(2)`. The caller holds the lock.
    static func append(_ events: [TaskEvent], in dir: URL, maxBytes: Int) -> Bool {
        guard !events.isEmpty else { return true }
        guard let data = lines(events) else { return false }
        return PrivateFiles.append(data, to: eventsURL(in: dir), maxBytes: maxBytes)
    }

    /// `events` as the whole history, via a temp file. The caller holds the lock.
    static func rewrite(_ events: [TaskEvent], in dir: URL) -> Bool {
        guard let data = lines(events) else { return false }
        return PrivateFiles.writeWhole(data, to: eventsURL(in: dir))
    }

    private static func lines(_ events: [TaskEvent]) -> Data? {
        var data = Data()
        for e in events {
            guard let line = try? CommsLog.encoder.encode(e) else { return nil }
            data.append(line)
            data.append(UInt8(ascii: "\n"))
        }
        return data
    }
}
