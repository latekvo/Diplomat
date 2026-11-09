import DiplomatCore
import Foundation

/// The Linux front-end's (and any agent's) whole reach into Tasks. All three read and
/// write `$DIPLOMAT_TASKS_DIR` (default `~/.diplomat/tasks`); the sync also reads
/// `$DIPLOMAT_COMMS_DIR` and runs `$DIPLOMAT_GH`.
///
///   tasks-list   [--archived]  stdout = [TaskView] ({task, status, reasons, history,
///                lastEventAt, linkStates}), archived tasks too with --archived.
///   tasks-sync   one `TaskSync.run`; stdout = {"ok", "error"?, "events": n}. Exits 0 when
///                `error` is set: a failed sync is an answer, and `ok` with an `error`
///                names the links it skipped.
///   tasks-edit   stdin = a TaskEdit ({"op": "create" | "update" | "link" | "unlink" |
///                "note" | "archive" | "unarchive", …}); stdout = the task as it now is.
///                Exits 1 with the reason on stderr for an unknown op, key or task, or an
///                edit that does not apply. An edit saved without its history line exits
///                0, printing the task, with the reason on stderr: it is not to be retried.
enum TasksCommand {
    static func list(_ arguments: [String]) {
        guard arguments.allSatisfy({ $0 == "--archived" }) else {
            die("tasks-list takes only --archived", 1)
        }
        do { emitJSON(try TaskStore.views(archived: !arguments.isEmpty)) } catch {
            die(error.localizedDescription, 1)
        }
    }

    private final class Box: @unchecked Sendable {
        var result: TaskSync.Result?
    }

    static func sync() {
        let box = Box()
        let done = DispatchSemaphore(value: 0)
        Task {
            box.result = await TaskSync.run()
            done.signal()
        }
        done.wait()
        emitJSON(box.result!)
    }

    static func edit(_ input: Data) {
        let edit: TaskEdit
        do { edit = try CommsLog.decoder.decode(TaskEdit.self, from: input) } catch {
            die("stdin is not a TaskEdit: \(TaskError.describe(error))", 1)
        }
        do { emitJSON(try TaskStore.edit(edit)) } catch {
            guard let saved = (error as? TaskError)?.saved else { die(error.localizedDescription, 1) }
            let warning = "diplomat-core: warning: \(error.localizedDescription)\n"
            try? FileHandle.standardError.write(contentsOf: Data(warning.utf8))
            emitJSON(saved)
        }
    }

    private static func emitJSON<T: Encodable>(_ value: T) {
        guard var data = try? CommsLog.encoder.encode(value) else {
            die("could not encode the answer", 1)
        }
        data.append(UInt8(ascii: "\n"))
        emit(data)
    }
}
