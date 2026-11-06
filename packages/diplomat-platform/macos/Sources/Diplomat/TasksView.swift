import SwiftUI
import AppKit
import DiplomatCore

/// The Tasks screen: every tracked task, its status and the reason tags behind it, its tags
/// and links, and its history.
///
/// Draws `store.taskViews` (`TaskStore.views`, the rows `diplomat-core tasks-list` prints for
/// the Linux twin); every change is a `TaskEdit` through `Store.editTask`.
struct TasksView: View {
    @EnvironmentObject var store: Store
    @Binding var isPresented: Bool

    /// Tasks drawn with their actions and history, by id.
    @State private var expanded: Set<String>
    /// Tasks whose whole history is listed rather than its newest `historyShown`.
    @State private var wholeHistory: Set<String> = []
    @State private var showArchived: Bool
    @State private var composing: Bool
    @State private var newTitle: String
    @State private var newTags: String
    @State private var newLinks: String
    /// The one inline editor open, and what is typed into it.
    @State private var editor: Editor?
    @State private var editorText: String
    @State private var editorTags: String

    struct Editor: Equatable {
        enum Kind { case link, note, tags }
        let task: String
        let kind: Kind
    }

    private static let historyShown = 12

    /// Seeded only by the headless renders.
    init(isPresented: Binding<Bool>, seedExpanded: Set<String> = [], seedArchived: Bool = false,
         seedComposing: (title: String, tags: String, links: String)? = nil,
         seedEditor: Editor? = nil, seedEditorText: String = "", seedEditorTags: String = "") {
        _isPresented = isPresented
        _expanded = State(initialValue: seedExpanded)
        _showArchived = State(initialValue: seedArchived)
        _composing = State(initialValue: seedComposing != nil)
        _newTitle = State(initialValue: seedComposing?.title ?? "")
        _newTags = State(initialValue: seedComposing?.tags ?? "")
        _newLinks = State(initialValue: seedComposing?.links ?? "")
        _editor = State(initialValue: seedEditor)
        _editorText = State(initialValue: seedEditorText)
        _editorTags = State(initialValue: seedEditorTags)
    }

    var body: some View {
        let views = store.taskViews.filter { showArchived || $0.task.archivedAt == nil }
        return VStack(alignment: .leading, spacing: 8) {
            header
            statusLine
            if let error = store.tasksSyncError { banner(error, tint: .orange, dismiss: nil) }
            if let error = store.tasksReadError { banner(error, tint: .red, dismiss: nil) }
            if let error = store.tasksEditError {
                banner(error, tint: .red) { store.tasksEditError = nil }
            }
            if composing { newTaskForm }
            if views.isEmpty {
                Text(showArchived || store.taskViews.isEmpty
                     ? "No tasks yet. A task is something that outlives one PR: give it a title, "
                        + "tags and its links, and Diplomat follows them."
                     : "Every task is archived.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .cardChrome(padding: 6)
            }
            VStack(alignment: .leading, spacing: 4) {
                ForEach(views, id: \.task.id) { view in
                    row(view)
                }
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .onAppear {
            let store = store
            Task { await store.reloadTasks() }
        }
    }

    // MARK: - Header and status

    private var header: some View {
        let active = store.activeTaskCount
        let archived = store.taskViews.count - active
        return HStack(spacing: 6) {
            Image(systemName: "checklist").foregroundStyle(.secondary)
            Text("Tasks").font(.subheadline.bold())
            CountPill(count: active)
            Spacer()
            ActionChip(symbol: showArchived ? "checkmark.square.fill" : "square",
                       text: archived > 0 ? "Show archived (\(archived))" : "Show archived",
                       tint: showArchived ? .accentColor : .secondary) {
                showArchived.toggle()
            }
            ActionChip(symbol: "arrow.triangle.2.circlepath",
                       text: store.tasksSyncing ? "Syncing…" : "Sync now",
                       tint: .accentColor, enabled: active > 0 && !store.tasksSyncing) {
                let store = store
                Task { await store.syncTasks() }
            }
            ActionChip(symbol: "plus", text: "New task", tint: .accentColor, enabled: !composing) {
                composing = true
            }
            Button { withAnimation(.easeInOut(duration: 0.15)) { isPresented = false } } label: {
                Text("Done").bold()
            }
            .buttonStyle(.borderless)
            .keyboardShortcut(.cancelAction)
        }
    }

    private var statusLine: some View {
        let failing = store.tasksSyncError != nil
        let tint: Color = store.activeTaskCount == 0 ? .secondary : (failing ? .orange : .green)
        return HStack(spacing: 6) {
            Circle().fill(tint).frame(width: 7, height: 7)
            Text(statusText).font(.system(size: 11)).foregroundStyle(.secondary)
            Spacer()
        }
    }

    private var statusText: String {
        guard store.activeTaskCount > 0 else {
            return "Nothing to sync: syncing starts with the first task that is not archived."
        }
        let last = store.tasksLastSync.map { "last sync \(Fmt.clock($0))" } ?? "not synced yet"
        let every = Fmt.duration(Store.tasksSyncInterval)
        return "GitHub state and Comms pings of every linked item · \(last) · every \(every)"
    }

    private func banner(_ message: String, tint: Color, dismiss: (() -> Void)?) -> some View {
        HStack(alignment: .top, spacing: 6) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 10)).foregroundStyle(tint)
            Text(message).font(.system(size: 11))
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            Spacer(minLength: 0)
            if let dismiss {
                Button(action: dismiss) { Image(systemName: "xmark").font(.system(size: 9, weight: .bold)) }
                    .buttonStyle(.plain).foregroundStyle(.secondary)
            }
        }
        .cardChrome(fill: tint.opacity(0.14))
    }

    // MARK: - New task

    private var newTaskForm: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text("NEW TASK").font(.system(size: 9, weight: .bold))
                .foregroundStyle(.secondary).kerning(0.5)
            TextField("Title, e.g. Physical iPhone support", text: $newTitle)
                .textFieldStyle(.roundedBorder).font(.system(size: 11))
            TextField("Tags, e.g. area:devices kind:feature blocked-on:apple", text: $newTags)
                .textFieldStyle(.roundedBorder).font(.system(size: 11))
            ZStack(alignment: .topLeading) {
                TextEditor(text: $newLinks)
                    .font(.system(size: 11, design: .monospaced))
                    .frame(height: 52)
                    .overlay(RoundedRectangle(cornerRadius: 4).stroke(Color.gray.opacity(0.3)))
                if newLinks.isEmpty {
                    Text("Links, one per line: a PR or issue URL, o/r#12, #12, a Slack permalink, any URL")
                        .font(.system(size: 11)).foregroundStyle(.tertiary)
                        .padding(.horizontal, 5).padding(.vertical, 1)
                        .allowsHitTesting(false)
                }
            }
            HStack(spacing: 6) {
                Spacer()
                ActionChip(symbol: nil, text: "Cancel", tint: .secondary) { closeComposer() }
                ActionChip(symbol: "plus", text: "Create", tint: .accentColor,
                           enabled: !newTitle.trimmingCharacters(in: .whitespaces).isEmpty) {
                    create()
                }
            }
        }
        .cardChrome(fill: Color.accentColor.opacity(0.07), padding: 7)
    }

    private func create() {
        let edit = TaskEdit.create(title: newTitle, tags: TasksView.words(newTags),
                                   links: newLinks.split(whereSeparator: \.isNewline)
                                       .map { $0.trimmingCharacters(in: .whitespaces) }
                                       .filter { !$0.isEmpty })
        let store = store
        let composer = $composing
        Task {
            if await store.editTask(edit, sync: true) { composer.wrappedValue = false }
        }
    }

    private func closeComposer() {
        composing = false
        newTitle = ""
        newTags = ""
        newLinks = ""
    }

    /// Tags as typed: separated by spaces, commas or new lines.
    static func words(_ raw: String) -> [String] {
        raw.split(whereSeparator: { $0.isWhitespace || $0 == "," }).map(String.init)
    }

    // MARK: - Rows

    private func row(_ view: TaskView) -> some View {
        let id = view.task.id
        let open = expanded.contains(id)
        return TaskRow(view: view, expanded: open,
                       toggle: {
                           if open { expanded.remove(id) } else { expanded.insert(id) }
                       }) {
            if open {
                VStack(alignment: .leading, spacing: 5) {
                    actions(view.task)
                    if let editor, editor.task == id { editorForm(editor, task: view.task) }
                    history(view)
                }
                .padding(.leading, 14)
            }
        }
    }

    private func actions(_ task: TrackedTask) -> some View {
        let id = task.id
        return HStack(spacing: 5) {
            ActionChip(symbol: "link.badge.plus", text: "Add link", tint: .accentColor) {
                openEditor(Editor(task: id, kind: .link), text: "", tags: "")
            }
            ActionChip(symbol: "note.text.badge.plus", text: "Add note", tint: .accentColor) {
                openEditor(Editor(task: id, kind: .note), text: "", tags: "")
            }
            ActionChip(symbol: "tag", text: "Edit tags", tint: .accentColor) {
                openEditor(Editor(task: id, kind: .tags), text: task.tags.joined(separator: " "), tags: "")
            }
            if let pinned = task.pinnedStatus {
                ActionChip(symbol: "pin.slash", text: "Unpin \(pinned.rawValue)", tint: .orange) {
                    edit(.update(id: id, title: nil, tags: nil, pinnedStatus: ""))
                }
            } else {
                ActionChip(symbol: "checkmark.circle", text: "Pin done", tint: .green) {
                    edit(.update(id: id, title: nil, tags: nil, pinnedStatus: TaskStatus.done.rawValue))
                }
                ActionChip(symbol: "pause.circle", text: "Pin paused", tint: .orange) {
                    edit(.update(id: id, title: nil, tags: nil, pinnedStatus: TaskStatus.paused.rawValue))
                }
            }
            if task.archivedAt == nil {
                ActionChip(symbol: "archivebox", text: "Archive", tint: .secondary) {
                    edit(.archive(id: id))
                }
            } else {
                ActionChip(symbol: "arrow.uturn.backward", text: "Unarchive", tint: .accentColor) {
                    edit(.unarchive(id: id), sync: true)
                }
            }
            Spacer(minLength: 0)
            Text(id).font(.system(size: 9, design: .monospaced)).foregroundStyle(.tertiary)
                .textSelection(.enabled)
        }
    }

    private func openEditor(_ e: Editor, text: String, tags: String) {
        editor = e
        editorText = text
        editorTags = tags
    }

    private func edit(_ edit: TaskEdit, sync: Bool = false) {
        let store = store
        Task { await store.editTask(edit, sync: sync) }
    }

    private func editorForm(_ e: Editor, task: TrackedTask) -> some View {
        let prompt: String
        switch e.kind {
        case .link: prompt = "A PR or issue URL, o/r#12, #12, a Slack permalink or any URL"
        case .note: prompt = "Note: what happened, what was decided"
        case .tags: prompt = "Tags, separated by spaces"
        }
        return HStack(spacing: 5) {
            TextField(prompt, text: $editorText)
                .textFieldStyle(.roundedBorder).font(.system(size: 11))
                .onSubmit { saveEditor(e) }
            if e.kind == .note {
                TextField("Tags, e.g. decision blocked-on:apple", text: $editorTags)
                    .textFieldStyle(.roundedBorder).font(.system(size: 11))
                    .frame(width: 230)
                    .onSubmit { saveEditor(e) }
            }
            ActionChip(symbol: nil, text: "Cancel", tint: .secondary) { editor = nil }
            ActionChip(symbol: "checkmark", text: "Save", tint: .accentColor) { saveEditor(e) }
        }
    }

    private func saveEditor(_ e: Editor) {
        let edit: TaskEdit
        let sync: Bool
        switch e.kind {
        case .link:
            edit = .link(id: e.task, url: editorText)
            sync = true
        case .note:
            edit = .note(id: e.task, text: editorText, tags: TasksView.words(editorTags), url: nil)
            sync = false
        case .tags:
            edit = .update(id: e.task, title: nil, tags: TasksView.words(editorText), pinnedStatus: nil)
            sync = false
        }
        let store = store
        let open = $editor
        Task {
            if await store.editTask(edit, sync: sync), open.wrappedValue == e { open.wrappedValue = nil }
        }
    }

    private func history(_ view: TaskView) -> some View {
        let id = view.task.id
        let all = wholeHistory.contains(id)
        let shown = all ? view.history : Array(view.history.prefix(TasksView.historyShown))
        let hidden = view.history.count - shown.count
        return VStack(alignment: .leading, spacing: 1) {
            if view.history.isEmpty {
                Text("No history yet.").font(.system(size: 10)).foregroundStyle(.secondary)
            }
            ForEach(Array(shown.enumerated()), id: \.offset) { _, event in
                HistoryLine(event: event)
            }
            if hidden > 0 {
                Text("\(hidden) older…").font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(Color.accentColor)
                    .padding(.vertical, 2)
                    .contentShape(Rectangle())
                    .onTapGesture { wholeHistory.insert(id) }
                    .accessibilityAddTraits(.isButton)
            }
        }
    }
}

// MARK: - A task's row

/// One task: its status, title and reasons, then its tags and links; tapped, it unfolds into
/// `details` (its actions and history).
private struct TaskRow<Details: View>: View {
    let view: TaskView
    let expanded: Bool
    let toggle: () -> Void
    @ViewBuilder let details: () -> Details

    var body: some View {
        let task = view.task
        let dimmed = view.status == .done || task.archivedAt != nil
        return VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .center, spacing: 5) {
                Image(systemName: expanded ? "chevron.down" : "chevron.right")
                    .font(.system(size: 8, weight: .bold)).foregroundStyle(.secondary)
                    .frame(width: 9)
                StatusPill(text: view.status.rawValue, tint: TaskTint.status(view.status)).fixedSize()
                Text(task.title).font(.system(size: 11.5, weight: .semibold))
                    .lineLimit(1).truncationMode(.tail)
                    .layoutPriority(1)
                ForEach(view.reasons, id: \.self) { reason in
                    Chip(text: reason, tint: TaskTint.reason(reason))
                }
                Spacer(minLength: 4)
                if task.archivedAt != nil {
                    StatusPill(text: "archived", tint: .secondary, symbol: "archivebox").fixedSize()
                }
                Text(view.lastEventAt.map { TaskTint.ago($0) } ?? "")
                    .font(.system(size: 9).monospacedDigit()).foregroundStyle(.secondary)
                    .fixedSize()
                    .help(view.lastEventAt.map { "Last event \($0.formatted(date: .abbreviated, time: .shortened))" } ?? "")
            }
            .contentShape(Rectangle())
            .onTapGesture(perform: toggle)
            .accessibilityAddTraits(.isButton)
            if !task.tags.isEmpty || !task.links.isEmpty {
                FlowLayout(spacing: 4) {
                    ForEach(task.tags, id: \.self) { tag in
                        Chip(text: tag, tint: .secondary, symbol: nil)
                    }
                    ForEach(task.links, id: \.url) { link in
                        LinkChip(link: link, state: TaskTint.linkState(link, view: view))
                    }
                }
                .padding(.leading, 14)
            }
            details()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardChrome(fill: Color.gray.opacity(dimmed ? 0.045 : 0.08), padding: 5)
        .opacity(task.archivedAt != nil ? 0.75 : 1)
    }
}

/// One history entry: when, from where, its tags, what it is about and its excerpt; with a
/// URL, the whole line opens it.
private struct HistoryLine: View {
    let event: TaskEvent

    var body: some View {
        // A Slack line's own permalink adds nothing to its `slack` tag.
        let subject = event.subject.map(TaskTint.linkLabel).flatMap {
            $0 == "slack" && event.source == "slack" ? nil : $0
        }
        let url = event.url.flatMap(URL.init(string:))
        let line = HStack(alignment: .center, spacing: 5) {
            Text(TaskTint.ago(event.at)).font(.system(size: 9).monospacedDigit())
                .foregroundStyle(.secondary)
                .frame(width: 24, alignment: .trailing)
            Image(systemName: TaskTint.sourceSymbol(event.source))
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(TaskTint.source(event.source))
                .frame(width: 13)
                .help(event.source)
            if let subject {
                Text(subject).font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                    .foregroundStyle(.secondary).fixedSize()
            }
            ForEach(event.tags, id: \.self) { tag in
                Chip(text: tag, tint: TaskTint.eventTag(tag), small: true)
            }
            Text(event.text ?? "").font(.system(size: 10.5))
                .lineLimit(1).truncationMode(.tail)
                .frame(maxWidth: .infinity, alignment: .leading)
                .layoutPriority(-1)
            if url != nil {
                Image(systemName: "arrow.up.right").font(.system(size: 8, weight: .bold))
                    .foregroundStyle(Color.accentColor)
            }
        }
        .padding(.vertical, 1.5)
        .help(help)
        if let url {
            line.contentShape(Rectangle())
                .onTapGesture { NSWorkspace.shared.open(url) }
                .accessibilityAddTraits(.isLink)
        } else {
            line
        }
    }

    private var help: String {
        let when = event.at.formatted(date: .abbreviated, time: .shortened)
        return [when, event.text, event.url].compactMap { $0 }.joined(separator: "\n")
    }
}

/// A link of a task: `#123` / `o/r#12`, `slack`, or the host; with a GitHub item's state as a
/// dot when its history tells it. Opens in the browser.
private struct LinkChip: View {
    let link: TaskLink
    let state: TaskTint.LinkState?

    var body: some View {
        let url = URL(string: link.url)
        return Button { if let url { NSWorkspace.shared.open(url) } } label: {
            HStack(spacing: 3) {
                if let state {
                    Circle().fill(state.tint).frame(width: 6, height: 6)
                }
                Image(systemName: symbol).font(.system(size: 8, weight: .semibold))
                Text(label).font(.system(size: 10, weight: .medium))
            }
            .foregroundStyle(Color.accentColor)
            .padding(.horizontal, 6).padding(.vertical, 1.5)
            .background(Capsule().fill(Color.accentColor.opacity(0.11)))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .help(([link.title, state?.word, link.url, link.via == "manual" ? nil : "linked via \(link.via)"]
               as [String?]).compactMap { $0 }.joined(separator: "\n"))
    }

    private var label: String { TaskTint.linkLabel(link.url) }

    private var symbol: String {
        switch link.kind {
        case "pr":    return "arrow.triangle.pull"
        case "issue": return "smallcircle.filled.circle"
        case "slack": return "number"
        default:      return "link"
        }
    }
}

/// A tag-shaped token: smaller and squarer than a `StatusPill`, so a row's one status still
/// reads first.
private struct Chip: View {
    let text: String
    let tint: Color
    var symbol: String? = nil
    var small = false

    var body: some View {
        Text(text).font(.system(size: small ? 9 : 9.5, weight: .medium))
            .foregroundStyle(tint == .secondary ? Color.primary.opacity(0.7) : tint)
            .lineLimit(1)
            .fixedSize()
            .padding(.horizontal, small ? 4 : 5).padding(.vertical, small ? 0.5 : 1.5)
            .background(RoundedRectangle(cornerRadius: 4)
                .fill((tint == .secondary ? Color.gray : tint).opacity(0.14)))
    }
}

/// A small button drawn by SwiftUI alone, so a headless `ImageRenderer` snapshot shows it.
private struct ActionChip: View {
    let symbol: String?
    let text: String
    let tint: Color
    var enabled = true
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 3) {
                if let symbol { Image(systemName: symbol).font(.system(size: 9, weight: .semibold)) }
                Text(text).font(.system(size: 10, weight: .semibold))
            }
            .foregroundStyle(tint)
            .padding(.horizontal, 6).padding(.vertical, 2.5)
            .background(RoundedRectangle(cornerRadius: 5).fill(
                (tint == .secondary ? Color.gray : tint).opacity(0.12)))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.45)
        .fixedSize()
    }
}

private struct CountPill: View {
    let count: Int

    var body: some View {
        Text("\(count)").font(.system(size: 9).monospacedDigit())
            .foregroundStyle(.secondary)
            .padding(.horizontal, 5).padding(.vertical, 1)
            .background(Capsule().fill(Color.gray.opacity(0.15)))
    }
}

// MARK: - Colours and words

enum TaskTint {
    static func status(_ s: TaskStatus) -> Color {
        switch s {
        case .open:       return .gray
        case .inProgress: return .blue
        case .inReview:   return .purple
        case .blocked:    return .red
        case .done:       return .green
        case .paused:     return .orange
        }
    }

    /// `#12:ci-failing`, `stale`, `pinned`…
    static func reason(_ r: String) -> Color {
        let cause = r.split(separator: ":").last.map(String.init) ?? r
        switch cause {
        case "ci-failing", "changes-requested", "conflicts", "missing": return .red
        case "approved":                                              return .green
        case "awaiting-review":                                       return .purple
        case "stale":                                                 return .orange
        default:                                                      return .secondary
        }
    }

    static func eventTag(_ tag: String) -> Color {
        if tag.hasPrefix("@") { return .indigo }
        if tag.hasPrefix("triage:") { return .teal }
        switch tag {
        case "ci:failing", "review:changes-requested", "conflicts", "link:missing", "pr:closed":
            return .red
        case "ci:passing", "review:approved", "pr:ready", "pr:open", "issue:open",
             "pr:reopened", "issue:reopened":
            return .green
        case "pr:merged", "issue:closed":
            return .purple
        case "linked", "created":
            return .blue
        default:
            return .secondary
        }
    }

    static func source(_ s: String) -> Color {
        switch s {
        case "github":   return .primary
        case "slack":    return .purple
        case "note":     return .orange
        default:         return .secondary
        }
    }

    static func sourceSymbol(_ s: String) -> String {
        switch s {
        case "github": return "chevron.left.forwardslash.chevron.right"
        case "slack":  return "bubble.left.fill"
        case "note":   return "note.text"
        default:       return "gearshape.fill"
        }
    }

    /// `#123` / `o/r#12` for a GitHub item, `slack` for a Slack permalink, else the host.
    static func linkLabel(_ url: String) -> String {
        if let ref = GitHubRef(url: url) { return ref.label(watched: CoreAssets.repoCoordinates()) }
        if TaskLinks.parse(url, watched: CoreAssets.repoCoordinates())?.kind == "slack" { return "slack" }
        return URL(string: url)?.host ?? url
    }

    /// `now`, `12m`, `5h`, `3d`.
    static func ago(_ date: Date, now: Date = Date()) -> String {
        now.timeIntervalSince(date) < 60 ? "now" : Fmt.age(date, now: now)
    }

    struct LinkState: Equatable {
        let word: String
        let tint: Color
    }

    /// A GitHub item's state as its newest history entry that names one says: the first
    /// sight (`linked`, `pr:open`) or a later move (`pr:merged`, `pr:draft`, `link:missing`).
    /// A first sight does not say draft, so an `<item>:draft` reason does. Nil for anything
    /// else, or one whose events have aged out of the history.
    static func linkState(_ link: TaskLink, view: TaskView) -> LinkState? {
        guard link.isGitHub else { return nil }
        if view.reasons.contains(linkLabel(link.url) + ":draft") {
            return LinkState(word: "draft", tint: .gray)
        }
        let identity = TaskLinks.identity(link.url)
        for event in view.history where event.source == "github" {
            guard let subject = event.subject, TaskLinks.identity(subject) == identity else { continue }
            for tag in event.tags {
                switch tag {
                case "pr:merged":                                return LinkState(word: "merged", tint: .purple)
                case "pr:closed":                                return LinkState(word: "closed", tint: .red)
                case "issue:closed":                             return LinkState(word: "closed", tint: .purple)
                case "pr:draft":                                 return LinkState(word: "draft", tint: .gray)
                case "link:missing":                             return LinkState(word: "missing", tint: .orange)
                case "pr:open", "issue:open", "pr:reopened", "issue:reopened", "pr:ready":
                    return LinkState(word: "open", tint: .green)
                default: continue
                }
            }
        }
        return nil
    }
}
