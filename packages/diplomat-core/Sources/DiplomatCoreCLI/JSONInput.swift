import Foundation

/// The CLI's stdin payload, with JSON booleans kept apart from JSON numbers.
///
/// Every subcommand here is one half of a parity pair: a fixture goes to this binary and
/// to the Python twin, and the two answers are diffed. That only means anything if the
/// two read the fixture the same way, and on `JSONSerialization` alone they do not.
/// A JSON number bridges to `NSNumber`, `NSNumber(1) as? Bool` is `true`, and so is
/// `NSNumber(1.0) as? Bool` — measured on macOS 15.5. So `"tokensLeft": 1` reached the
/// resolver as a positive reading, armed the run deadline, and made this side call a run
/// finished and reapable that the Python side called running. A field that arrived as a
/// number is a field that did not survive its trip, and answering out of whatever
/// happened to be truthy is how a hole in the net looks from inside it.
///
/// `JSONDecoder` is the one parser in the standard library that tells them apart, and it
/// does so on both platforms — which `CFGetTypeID`/`CFBooleanGetTypeID` does not:
/// swift-corelibs-foundation ships CoreFoundation and exports none of those symbols, so
/// that spelling built here and failed the Linux core job (backed out in 59b6f47).
///
/// It is used for the SHAPE only. The values still come from `JSONSerialization`, so
/// every other cast in the decoders — `as? Int`, `as? NSNumber`, `as? [String]` — reads
/// exactly what it always did, and the one thing that changes is that a boolean arrives
/// as a `Flag` that no numeric cast can satisfy.
enum JSONInput {

    /// A JSON `true`/`false`, and nothing else. Deliberately not `Bool`: the whole point
    /// is that a value which is not a JSON boolean must fail the cast.
    struct Flag {
        let on: Bool
    }

    /// Decode stdin. `nil` when it is not a JSON object, the same refusal as before.
    static func parse(_ data: Data) -> [String: Any]? {
        guard let values = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let shape = try? JSONDecoder().decode([String: Shape].self, from: data)
        else { return nil }
        return marked(values, .object(shape)) as? [String: Any]
    }

    /// A flag out of a decoded payload, or `fallback` when the field is absent or is not
    /// a JSON boolean. `agentstate._flag` is the Python twin, strict for the same reason.
    static func flag(_ raw: Any?, _ fallback: Bool = false) -> Bool {
        (raw as? Flag)?.on ?? fallback
    }

    /// Whether each position in the payload held a JSON boolean. Only booleans are named:
    /// numbers, strings and nulls are all `.other`, because nothing below distinguishes
    /// them and `JSONSerialization`'s own value is what gets used for them.
    private enum Shape: Decodable {
        case flag(Bool)
        case array([Shape])
        case object([String: Shape])
        case other

        init(from decoder: Decoder) throws {
            let c = try decoder.singleValueContainer()
            // Bool FIRST: it is the only case that must not be reachable by a number, and
            // `decode(Double.self)` would happily take a `true` on some platforms.
            if let b = try? c.decode(Bool.self) { self = .flag(b) }
            else if let o = try? c.decode([String: Shape].self) { self = .object(o) }
            else if let a = try? c.decode([Shape].self) { self = .array(a) }
            else { self = .other }
        }
    }

    private static func marked(_ value: Any, _ shape: Shape) -> Any {
        switch shape {
        case .flag(let on):
            return Flag(on: on)
        case .object(let fields):
            guard let d = value as? [String: Any] else { return value }
            var out: [String: Any] = [:]
            for (k, v) in d { out[k] = fields[k].map { marked(v, $0) } ?? v }
            return out
        case .array(let elements):
            guard let a = value as? [Any], a.count == elements.count else { return value }
            return zip(a, elements).map(marked)
        case .other:
            return value
        }
    }
}
