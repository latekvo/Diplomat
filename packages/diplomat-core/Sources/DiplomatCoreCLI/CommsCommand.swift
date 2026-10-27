import DiplomatCore
import Foundation

/// The Comms subcommands — the Linux front-end's whole reach into the Slack watcher, so
/// the tick, the screen's projection and the plan are each one implementation.
///
///   comms-tick    stdin = CommsSettings JSON; env DIPLOMAT_SLACK_TOKEN. One tick with the
///                 real classifier; stdout = {"me": {...}|null, "items": [{"message",
///                 "decision"}], "error": "..."|null, "notices": ["..."]}, `notices` being
///                 what went unread that is not an error (a catch-up past catchUpHours, a
///                 query cut short by maxSearchPages). Exits 0 when `error` is set: a
///                 failed tick is an answer, not a crash. Exits 1 when stdout cannot be
///                 written (its reader gone), leaving the items for the next tick.
///   comms-auth    no stdin; env DIPLOMAT_SLACK_TOKEN. auth.test through the tick's own
///                 transport; stdout = {"ok": true, "user", "userId", "team", "teamId"} or
///                 {"ok": false, "error": "<the tick's wording>"}. Exits 0 either way.
///   comms-feed    [--limit N]  stdout = the folded [CommsItem], newest first (default 200).
///   comms-effect  stdin = {"decision", "message", "toggles", "settings"?}; stdout = the
///                 CommsEffect.
///
/// The tick, the feed and the effect read and write `$DIPLOMAT_COMMS_DIR` (default
/// `~/.diplomat/comms`).
enum CommsCommand {
    static func tick(_ input: Data) {
        let settings = decode(CommsSettings.self, input, "CommsSettings")
        var failure: Error?
        _ = CommsWatch.tick(token: token, settings: settings, deliver: { result in
            do {
                guard var data = try? CommsLog.encoder.encode(result) else {
                    die("could not encode the answer", 1)
                }
                data.append(UInt8(ascii: "\n"))
                // Only now, past the tick's last child (which would inherit it): a gone
                // reader is then an error, not a signal killing the tick before it saves.
                signal(SIGPIPE, SIG_IGN)
                try FileHandle.standardOutput.write(contentsOf: data)
                return true
            } catch {
                failure = error
                return false
            }
        }) {
            CommsClassifier.run($0, settings: settings)
        }
        if let failure { die("could not write the output: \(failure.localizedDescription)", 1) }
    }

    static func auth() {
        let answer: [String: Any]
        if token.isEmpty {
            answer = ["ok": false, "error": CommsWatch.noToken]
        } else {
            do {
                let (me, team) = try SlackAPI(token: token).auth()
                answer = ["ok": true, "user": me.handle, "userId": me.userID,
                          "team": team, "teamId": me.teamID]
            } catch {
                answer = ["ok": false, "error": error.localizedDescription]
            }
        }
        guard var data = try? JSONSerialization.data(withJSONObject: answer,
                                                     options: [.sortedKeys]) else {
            die("could not encode the answer", 1)
        }
        data.append(UInt8(ascii: "\n"))
        emit(data)
    }

    private static var token: String {
        ProcessInfo.processInfo.environment["DIPLOMAT_SLACK_TOKEN"] ?? ""
    }

    static func feed(_ arguments: [String]) {
        var limit = 200
        if let i = arguments.firstIndex(of: "--limit") {
            guard i + 1 < arguments.count, let n = Int(arguments[i + 1]), n >= 0 else {
                die("--limit takes a whole number", 1)
            }
            limit = n
        }
        emitJSON(Array(CommsLog.fold(CommsLog.read()).prefix(limit)))
    }

    struct EffectInput: Decodable {
        let decision: CommsDecision
        let message: CommsMessage
        let toggles: CommsToggles?
        let settings: CommsSettings?
    }

    static func effect(_ input: Data) {
        let req = decode(EffectInput.self, input, "{decision, message, toggles}")
        emitJSON(CommsPlan.effect(for: req.decision, message: req.message,
                                  toggles: req.toggles ?? CommsToggles(),
                                  settings: req.settings ?? CommsSettings(),
                                  reportDir: CommsLog.reportsDir()))
    }

    static func investigatePrompt(_ input: Data) -> String {
        decode(InvestigateConfig.self, input, "InvestigateConfig").buildPrompt()
    }

    /// The classifier's instructions (the argument `claude -p` is given) for a
    /// CommsSettings payload.
    static func classifierPrompt(_ input: Data) -> String {
        CommsClassifier.prompt(settings: decode(CommsSettings.self, input, "CommsSettings"))
    }

    private static func decode<T: Decodable>(_ type: T.Type, _ input: Data, _ what: String) -> T {
        do { return try CommsLog.decoder.decode(T.self, from: input) } catch {
            die("stdin is not a \(what): \(error)", 1)
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
