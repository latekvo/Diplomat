import DiplomatCore
import Foundation

/// The Comms subcommands — the Linux front-end's whole reach into the Slack watcher, so
/// the tick, the screen's projection and the plan are each one implementation.
///
///   comms-tick    stdin = CommsSettings JSON; env DIPLOMAT_SLACK_TOKEN. One tick with the
///                 real classifier; stdout = {"me": {...}|null, "items": [{"message",
///                 "decision"}], "error": "..."|null}. Exits 0 when `error` is set: a
///                 failed tick is an answer, not a crash.
///   comms-feed    [--limit N]  stdout = the folded [CommsItem], newest first (default 200).
///   comms-effect  stdin = {"decision", "message", "toggles", "settings"?}; stdout = the
///                 CommsEffect.
///
/// All three read and write `$DIPLOMAT_COMMS_DIR` (default `~/.diplomat/comms`).
enum CommsCommand {
    static func tick(_ input: Data) {
        let settings = decode(CommsSettings.self, input, "CommsSettings")
        let token = ProcessInfo.processInfo.environment["DIPLOMAT_SLACK_TOKEN"] ?? ""
        let result = CommsWatch.tick(token: token, settings: settings) {
            CommsClassifier.run($0, settings: settings)
        }
        emitJSON(result)
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
