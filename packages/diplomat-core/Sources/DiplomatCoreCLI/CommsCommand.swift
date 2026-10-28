import DiplomatCore
import Foundation

/// The Linux front-end's whole reach into the Slack watcher, so each piece has one
/// implementation.
///
///   comms-tick    stdin = CommsSettings JSON. One tick with the
///                 real classifier; stdout = {"me": {...}|null, "items": [{"message",
///                 "decision"}], "error": "..."|null, "notices": ["..."]}. Exits 0 when
///                 `error` is set (a failed tick is an answer), 1 when stdout cannot be
///                 written, leaving the items for the next tick.
///   comms-auth    no stdin. Who Claude Code's Slack login is, through the tick's own
///                 transport (refreshing a dead login once); stdout = {"ok": true, "user",
///                 "userId", "team", "teamId"} or {"ok": false, "error": "<the tick's
///                 wording>"}. Exits 0 either way.
///   comms-feed    [--limit N]  stdout = the folded [CommsItem], newest first (default 200).
///   comms-effect  stdin = {"decision", "message", "toggles", "settings"?}; stdout = the
///                 CommsEffect.
///
/// The tick and the auth reach Slack under Claude Code's own Slack login (`SlackToken`),
/// which `DIPLOMAT_SLACK_TOKEN` overrides. The tick, the feed and the effect read and write
/// `$DIPLOMAT_COMMS_DIR` (default `~/.diplomat/comms`).
enum CommsCommand {
    static func tick(_ input: Data) {
        let settings = decode(CommsSettings.self, input, "CommsSettings")
        var failure: Error?
        _ = CommsWatch.tick(settings: settings, deliver: { result in
            do {
                guard var data = try? CommsLog.encoder.encode(result) else {
                    die("could not encode the answer", 1)
                }
                data.append(UInt8(ascii: "\n"))
                // Only past the tick's last child, which would inherit it: a gone reader
                // is then an error, not a signal killing the tick before it saves.
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
        do {
            let me = try SlackAPI.signIn(refresh: { SlackToken.refresh() }).me
            answer = ["ok": true, "user": me.username, "userId": me.userID,
                      "team": me.organization, "teamId": me.workspace]
        } catch {
            answer = ["ok": false, "error": error.localizedDescription]
        }
        guard var data = try? JSONSerialization.data(withJSONObject: answer,
                                                     options: [.sortedKeys]) else {
            die("could not encode the answer", 1)
        }
        data.append(UInt8(ascii: "\n"))
        emit(data)
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
