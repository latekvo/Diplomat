import SwiftUI
import AppKit
import DiplomatCore

/// The Tasks screen: every tracked task, its status and the reason tags behind it, its tags
/// and links, and its history.
///
/// Draws `store.taskViews` (`TaskStore.views`, the rows `diplomat-core tasks-list` prints for
/// the Linux twin); every change is a `TaskEdit` through `Store.editTask`. What an editor
/// holds is its `TaskDraft`, observed by the editor's own view, so typing redraws that editor
/// and not every row.
struct TasksView: View {
    @EnvironmentObject var store: Store
    @Binding var isPresented: Bool

    /// Tasks drawn with their actions and history, by id.
    @State private var expanded: Set<String>
    /// Tasks whose whole history is listed rather than its newest `historyShown`.
    @State private var wholeHistory: Set<String> = []
    @State private var showArchived: Bool
    /// The new-task form, while open: what it was opened with.
    @State private var composer: TaskDraft.Seed?
    /// The one inline editor open. Held here rather than by its form, so what is typed and
    /// why it was refused outlive the form while its row is folded or hidden.
    @State private var editor: Editor?
    /// Why a row's last action (a pin, an archive, a removed link), or a save whose editor
    /// closed before its answer, was refused, by task id.
    @State private var actionErrors: [String: String]

    struct Editor {
        enum Kind { case link, note, tags }
        let task: String
        let kind: Kind
        /// Also who this editor is: an answer closes only the editor whose draft saved.
        let draft: TaskDraft

        @MainActor
        init(task: String, kind: Kind, seed: TaskDraft.Seed = .init()) {
            self.task = task
            self.kind = kind
            switch kind {
            case .link: draft = TaskDraft(.link(task: task), seed: seed)
            case .note: draft = TaskDraft(.note(task: task), seed: seed)
            case .tags: draft = TaskDraft(.tags(task: task), seed: seed)
            }
        }
    }

    private static let historyShown = 12

    /// Seeded only by the headless renders.
    init(isPresented: Binding<Bool>, seedExpanded: Set<String> = [], seedArchived: Bool = false,
         seedComposer: TaskDraft.Seed? = nil, seedEditor: Editor? = nil,
         seedActionErrors: [String: String] = [:]) {
        _isPresented = isPresented
        _expanded = State(initialValue: seedExpanded)
        _showArchived = State(initialValue: seedArchived)
        _composer = State(initialValue: seedComposer)
        _editor = State(initialValue: seedEditor)
        _actionErrors = State(initialValue: seedActionErrors)
    }

    var body: some View {
        let views = store.taskViews.filter { showArchived || $0.task.archivedAt == nil }
        return VStack(alignment: .leading, spacing: 8) {
            header
            statusLine
            if let error = store.tasksSyncError { banner(error, tint: .orange) }
            if let error = store.tasksReadError { banner(error, tint: .red) }
            if let seed = composer {
                let open = $composer
                NewTaskForm(store: store, seed: seed) { open.wrappedValue = nil }
            }
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
            ActionChip(symbol: "plus", text: "New task", tint: .accentColor, enabled: composer == nil) {
                composer = TaskDraft.Seed()
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

    private func banner(_ message: String, tint: Color) -> some View {
        HStack(alignment: .top, spacing: 6) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 10)).foregroundStyle(tint)
            Text(message).font(.system(size: 11))
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            Spacer(minLength: 0)
        }
        .cardChrome(fill: tint.opacity(0.14))
    }

    /// Tags as typed: separated by spaces, commas or new lines.
    static func words(_ raw: String) -> [String] {
        raw.split(whereSeparator: { $0.isWhitespace || $0 == "," }).map(String.init)
    }

    // MARK: - Rows

    private func row(_ view: TaskView) -> some View {
        let id = view.task.id
        let open = expanded.contains(id)
        let errors = $actionErrors
        return TaskRow(view: view, labels: store.taskLabels, expanded: open,
                       error: actionErrors[id],
                       toggle: {
                           if open { expanded.remove(id) } else { expanded.insert(id) }
                       },
                       dismissError: { errors.wrappedValue[id] = nil },
                       unlink: { url in edit(.unlink(id: id, url: url), task: id) }) {
            if open {
                VStack(alignment: .leading, spacing: 5) {
                    actions(view.task)
                    if let editor, editor.task == id {
                        let current = $editor
                        let isOpen = { current.wrappedValue?.draft === editor.draft }
                        TaskEditorForm(store: store, editor: editor, close: {
                            if isOpen() { current.wrappedValue = nil }
                        }, saved: { taken in
                            if isOpen() {
                                if taken { current.wrappedValue = nil }
                            } else if !taken {
                                errors.wrappedValue[id] = editor.draft.error
                            }
                        })
                    }
                    history(view)
                }
                .padding(.leading, 14)
            }
        }
    }

    private func actions(_ task: TrackedTask) -> some View {
        let id = task.id
        return HStack(spacing: 5) {
            if task.archivedAt != nil {
                ActionChip(symbol: "arrow.uturn.backward", text: "Unarchive", tint: .accentColor) {
                    edit(.unarchive(id: id), task: id, sync: true)
                }
            } else {
                editActions(task)
            }
            Spacer(minLength: 0)
            Text(id).font(.system(size: 9, design: .monospaced)).foregroundStyle(.tertiary)
                .textSelection(.enabled)
        }
    }

    @ViewBuilder
    private func editActions(_ task: TrackedTask) -> some View {
        let id = task.id
        Group {
            ActionChip(symbol: "link.badge.plus", text: "Add link", tint: .accentColor) {
                open(.link, on: id)
            }
            ActionChip(symbol: "note.text.badge.plus", text: "Add note", tint: .accentColor) {
                open(.note, on: id)
            }
            ActionChip(symbol: "tag", text: "Edit tags", tint: .accentColor) {
                open(.tags, on: id, seed: .init(text: task.tags.joined(separator: " ")))
            }
            if let pinned = task.pinnedStatus {
                ActionChip(symbol: "pin.slash", text: "Unpin \(pinned.rawValue)", tint: .orange) {
                    edit(.update(id: id, title: nil, tags: nil, pinnedStatus: ""), task: id)
                }
            } else {
                ActionChip(symbol: "checkmark.circle", text: "Pin done", tint: .green) {
                    edit(.update(id: id, title: nil, tags: nil, pinnedStatus: TaskStatus.done.rawValue), task: id)
                }
                ActionChip(symbol: "pause.circle", text: "Pin paused", tint: .orange) {
                    edit(.update(id: id, title: nil, tags: nil, pinnedStatus: TaskStatus.paused.rawValue), task: id)
                }
            }
            ActionChip(symbol: "archivebox", text: "Archive", tint: .secondary) {
                edit(.archive(id: id), task: id)
            }
        }
    }

    /// Asked for again, the open editor stays as typed.
    private func open(_ kind: Editor.Kind, on id: String, seed: TaskDraft.Seed = .init()) {
        guard editor?.task != id || editor?.kind != kind else { return }
        editor = Editor(task: id, kind: kind, seed: seed)
    }

    private func edit(_ edit: TaskEdit, task id: String, sync: Bool = false) {
        let store = store
        let errors = $actionErrors
        Task { errors.wrappedValue[id] = await store.editTask(edit, sync: sync) }
    }

    private func history(_ view: TaskView) -> some View {
        let id = view.task.id
        let all = wholeHistory.contains(id)
        let shown = all ? view.history : Array(view.history.prefix(TasksView.historyShown))
        let hidden = view.history.count - shown.count
        let labels = store.taskLabels
        return VStack(alignment: .leading, spacing: 1) {
            if view.history.isEmpty {
                Text("No history yet.").font(.system(size: 10)).foregroundStyle(.secondary)
            }
            ForEach(Array(shown.enumerated()), id: \.offset) { _, event in
                HistoryLine(event: event, subject: event.subject.map { labels[$0] ?? $0 })
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

// MARK: - Editors

/// What one of the screen's editors holds: what is typed, and why the core refused its last
/// save, which the editor draws itself.
@MainActor
final class TaskDraft: ObservableObject {
    enum Kind: Equatable {
        case create
        case link(task: String)
        case note(task: String)
        case tags(task: String)
    }

    /// What an editor opens with.
    struct Seed: Hashable {
        var text = ""
        var tags = ""
        var links = ""
        var error: String?
    }

    let kind: Kind
    /// A new task's title; the link, the note or the tags of the others.
    @Published var text: String
    /// A new task's or a note's tags.
    @Published var tags: String
    /// A new task's links, one per line.
    @Published var links: String
    @Published var error: String?

    init(_ kind: Kind, seed: Seed = Seed()) {
        self.kind = kind
        text = seed.text
        tags = seed.tags
        links = seed.links
        error = seed.error
    }

    var edit: TaskEdit {
        switch kind {
        case .create:
            return .create(title: text, tags: TasksView.words(tags),
                           links: links.split(whereSeparator: \.isNewline)
                               .map { $0.trimmingCharacters(in: .whitespaces) }
                               .filter { !$0.isEmpty })
        case .link(let id):
            return .link(id: id, url: text)
        case .note(let id):
            return .note(id: id, text: text, tags: TasksView.words(tags), url: nil)
        case .tags(let id):
            return .update(id: id, title: nil, tags: TasksView.words(text), pinnedStatus: nil)
        }
    }

    /// Applies `edit`: true, and every field emptied, once the core takes it; false, with
    /// `error` saying why and what was typed kept, when it refuses. A new link or task syncs
    /// right after, so its GitHub state shows without waiting for the timer.
    func submit(to store: Store) async -> Bool {
        let sync: Bool
        switch kind {
        case .create, .link: sync = true
        case .note, .tags: sync = false
        }
        error = await store.editTask(edit, sync: sync)
        guard error == nil else { return false }
        text = ""
        tags = ""
        links = ""
        return true
    }
}

/// A refusal, drawn where the edit that caused it was made.
private struct InlineError: View {
    let message: String
    var dismiss: (() -> Void)? = nil

    var body: some View {
        HStack(alignment: .top, spacing: 4) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 9)).foregroundStyle(.red)
            Text(message).font(.system(size: 10.5)).foregroundStyle(.red)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            Spacer(minLength: 0)
            if let dismiss {
                Button(action: dismiss) { Image(systemName: "xmark").font(.system(size: 8, weight: .bold)) }
                    .buttonStyle(.plain).foregroundStyle(.secondary)
            }
        }
    }
}

private struct NewTaskForm: View {
    let store: Store
    @StateObject private var draft: TaskDraft
    let close: () -> Void

    init(store: Store, seed: TaskDraft.Seed, close: @escaping () -> Void) {
        self.store = store
        _draft = StateObject(wrappedValue: TaskDraft(.create, seed: seed))
        self.close = close
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text("NEW TASK").font(.system(size: 9, weight: .bold))
                .foregroundStyle(.secondary).kerning(0.5)
            TextField("Title, e.g. Physical iPhone support", text: $draft.text)
                .textFieldStyle(.roundedBorder).font(.system(size: 11))
            TextField("Tags, e.g. area:devices kind:feature blocked-on:apple", text: $draft.tags)
                .textFieldStyle(.roundedBorder).font(.system(size: 11))
            ZStack(alignment: .topLeading) {
                TextEditor(text: $draft.links)
                    .font(.system(size: 11, design: .monospaced))
                    .frame(height: 52)
                    .overlay(RoundedRectangle(cornerRadius: 4).stroke(Color.gray.opacity(0.3)))
                if draft.links.isEmpty {
                    Text("Links, one per line: a PR or issue URL, o/r#12, #12, a Slack permalink, any URL")
                        .font(.system(size: 11)).foregroundStyle(.tertiary)
                        .padding(.horizontal, 5).padding(.vertical, 1)
                        .allowsHitTesting(false)
                }
            }
            HStack(spacing: 6) {
                if let error = draft.error { InlineError(message: error) }
                Spacer()
                ActionChip(symbol: nil, text: "Cancel", tint: .secondary, action: close)
                ActionChip(symbol: "plus", text: "Create", tint: .accentColor,
                           enabled: !draft.text.trimmingCharacters(in: .whitespaces).isEmpty) {
                    let (draft, store, close) = (draft, store, close)
                    Task { if await draft.submit(to: store) { close() } }
                }
            }
        }
        .cardChrome(fill: Color.accentColor.opacity(0.07), padding: 7)
    }
}

/// A row's add-link, note or tags editor.
private struct TaskEditorForm: View {
    let store: Store
    @ObservedObject private var draft: TaskDraft
    let close: () -> Void
    /// A save's answer: whether the core took it.
    let saved: (Bool) -> Void
    private let prompt: String
    private let note: Bool

    init(store: Store, editor: TasksView.Editor, close: @escaping () -> Void,
         saved: @escaping (Bool) -> Void) {
        self.store = store
        self.close = close
        self.saved = saved
        switch editor.kind {
        case .link: prompt = "A PR or issue URL, o/r#12, #12, a Slack permalink or any URL"
        case .note: prompt = "Note: what happened, what was decided"
        case .tags: prompt = "Tags, separated by spaces"
        }
        note = editor.kind == .note
        draft = editor.draft
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 5) {
                TextField(prompt, text: $draft.text)
                    .textFieldStyle(.roundedBorder).font(.system(size: 11))
                    .onSubmit(save)
                if note {
                    TextField("Tags, e.g. decision blocked-on:apple", text: $draft.tags)
                        .textFieldStyle(.roundedBorder).font(.system(size: 11))
                        .frame(width: 230)
                        .onSubmit(save)
                }
                ActionChip(symbol: nil, text: "Cancel", tint: .secondary, action: close)
                ActionChip(symbol: "checkmark", text: "Save", tint: .accentColor, action: save)
            }
            if let error = draft.error { InlineError(message: error) }
        }
    }

    private func save() {
        let (draft, store, saved) = (draft, store, saved)
        Task { saved(await draft.submit(to: store)) }
    }
}

// MARK: - A task's row

/// One task: its status, title and reasons, then its tags and links; tapped, it unfolds into
/// `details` (its actions and history).
private struct TaskRow<Details: View>: View {
    let view: TaskView
    /// `Store.taskLabels`.
    let labels: [String: String]
    let expanded: Bool
    /// Why its last action was refused.
    let error: String?
    let toggle: () -> Void
    let dismissError: () -> Void
    let unlink: (String) -> Void
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
                Text(TaskTint.ago(view.lastEventAt))
                    .font(.system(size: 9).monospacedDigit()).foregroundStyle(.secondary)
                    .fixedSize()
                    .tooltip("Last event \(view.lastEventAt.formatted(date: .abbreviated, time: .shortened))")
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
                        LinkChip(link: link, label: labels[link.url] ?? link.url,
                                 state: TaskTint.linkState(view.linkStates[link.url], kind: link.kind),
                                 remove: task.archivedAt == nil ? { unlink(link.url) } : nil)
                    }
                }
                .padding(.leading, 14)
            }
            if let error {
                InlineError(message: error, dismiss: dismissError).padding(.leading, 14)
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
    /// The label of the link it is about.
    let subject: String?

    var body: some View {
        // A Slack line's own permalink adds nothing to its `slack` tag.
        let subject = subject == "slack" && event.source == "slack" ? nil : subject
        let url = event.url.flatMap(URL.init(string:))
        let line = HStack(alignment: .center, spacing: 5) {
            Text(TaskTint.ago(event.at)).font(.system(size: 9).monospacedDigit())
                .foregroundStyle(.secondary)
                .frame(width: 24, alignment: .trailing)
            Image(systemName: TaskTint.sourceSymbol(event.source))
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(TaskTint.source(event.source))
                .frame(width: 13)
                .tooltip(event.source)
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
        .tooltip(help)
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

/// A link of a task: `#123` / `o/r#12`, `slack`, or the host; with a synced GitHub item's
/// state as a dot. Opens in the browser; its context menu also removes it.
private struct LinkChip: View {
    let link: TaskLink
    let label: String
    let state: TaskTint.LinkState?
    /// Nil on an archived task, which offers only Unarchive.
    let remove: (() -> Void)?

    var body: some View {
        let url = URL(string: link.url)
        let open = { if let url { NSWorkspace.shared.open(url) } }
        return Button(action: open) {
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
        .contextMenu {
            Button("Open", action: open).disabled(url == nil)
            if let remove { Button("Remove link", action: remove) }
        }
        .tooltip(([link.title, state?.word, link.url, link.via == "manual" ? nil : "linked via \(link.via)"]
                  as [String?]).compactMap { $0 }.joined(separator: "\n"))
    }

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

    /// The label of every link and history subject of `views`, by URL: `#123` / `o/r#12` for
    /// a GitHub item, `slack` for a Slack permalink, else the host.
    static func labels(of views: [TaskView]) -> [String: String] {
        let watched = CoreAssets.repoCoordinates()
        var out: [String: String] = [:]
        for view in views {
            for url in view.task.links.map(\.url) + view.history.compactMap(\.subject) where out[url] == nil {
                if let ref = GitHubRef(url: url) {
                    out[url] = ref.label(watched: watched)
                } else if TaskLinks.parse(url, watched: watched)?.kind == "slack" {
                    out[url] = "slack"
                } else {
                    out[url] = URL(string: url)?.host ?? url
                }
            }
        }
        return out
    }

    /// `now`, `12m`, `5h`, `3d`.
    static func ago(_ date: Date, now: Date = Date()) -> String {
        now.timeIntervalSince(date) < 60 ? "now" : Fmt.age(date, now: now)
    }

    struct LinkState: Equatable {
        let word: String
        let tint: Color
    }

    /// A `TaskView.linkStates` word as a dot: a closed PR was given up on, a closed issue
    /// was done.
    static func linkState(_ word: String?, kind: String) -> LinkState? {
        switch word {
        case "open":    return LinkState(word: "open", tint: .green)
        case "draft":   return LinkState(word: "draft", tint: .gray)
        case "merged":  return LinkState(word: "merged", tint: .purple)
        case "closed":  return LinkState(word: "closed", tint: kind == "pr" ? .red : .purple)
        case "missing": return LinkState(word: "missing", tint: .orange)
        default:        return nil
        }
    }
}
