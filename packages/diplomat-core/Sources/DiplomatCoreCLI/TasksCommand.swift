import DiplomatCore
import Foundation

/// The Linux front-end's (and any agent's) whole reach into Tasks. All three read and
/// write `$DIPLOMAT_TASKS_DIR` (default `~/.diplomat/tasks`); the sync also reads
/// `$DIPLOMAT_COMMS_DIR` and runs `$DIPLOMAT_GH`.
///
///   tasks-list   [--archived]  stdout = [TaskView] ({task, status, reasons, history,
///                lastEventAt}), archived tasks too with --archived.
///   tasks-sync   one `TaskSync.run`; stdout = {"ok", "error"?, "events": n}. Exits 0 when
///                `error` is set: a failed sync is an answer.
///   tasks-edit   stdin = a TaskEdit ({"op": "create" | "update" | "link" | "unlink" |
///                "note" | "archive" | "unarchive", …}); stdout = the task as it now is.
///                Exits 1 with the reason on stderr for an unknown op or task, or an edit
///                that does not apply.
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
            let why: String
            switch error as? DecodingError {
            case .dataCorrupted(let c)?: why = c.debugDescription
            case .keyNotFound(let key, _)?: why = "no \"\(key.stringValue)\""
            case .typeMismatch(_, let c)?, .valueNotFound(_, let c)?:
                why = "\"\(c.codingPath.map(\.stringValue).joined(separator: "."))\": \(c.debugDescription)"
            default: why = "\(error)"
            }
            die("stdin is not a TaskEdit: \(why)", 1)
        }
        do { emitJSON(try TaskStore.edit(edit)) } catch {
            die(error.localizedDescription, 1)
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
