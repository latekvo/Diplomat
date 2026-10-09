import Foundation

/// A JSON object, with JSON booleans kept apart from JSON numbers.
///
/// For anything a Python twin also reads: the CLI's stdin payload, which every parity
/// pair diffs against the Python answer, and the files both front-ends share (the run
/// book, the telemetry ledger). Those only mean one thing if the two sides read them the
/// same way, and on `JSONSerialization` alone they do not. A JSON number bridges to
/// `NSNumber`, `NSNumber(1) as? Bool` is `true`, and so is `NSNumber(1.0) as? Bool` —
/// measured on macOS 15.5 and on swift-corelibs-foundation 6.0 alike. So `"tokensLeft": 1`
/// reached the resolver as a positive reading, armed the run deadline, and made this side
/// call a run finished and reapable that the Python side called running. A field that
/// arrived as a number is a field that did not survive its trip, and answering out of
/// whatever happened to be truthy is how a hole in the net looks from inside it.
///
/// `JSONDecoder` is the one parser in the standard library that tells them apart, and it
/// does so on both platforms — which `CFGetTypeID`/`CFBooleanGetTypeID` does not:
/// swift-corelibs-foundation ships CoreFoundation and exports none of those symbols, so
/// that spelling built here and failed the Linux core job (backed out in 59b6f47).
///
/// It is used for the SHAPE only. The values still come from `JSONSerialization`, so
/// every other cast in the decoders — `as? Int`, `as? NSNumber`, `as? [String]` — reads
/// what `JSONSerialization` produced, and the one difference is that a boolean arrives
/// as a `Flag` that no numeric cast can satisfy.
public enum JSONInput {

    /// A JSON `true`/`false`, and nothing else. Deliberately not `Bool`: the whole point
    /// is that a value which is not a JSON boolean must fail the cast.
    public struct Flag {
        public let on: Bool
    }

    /// Decode a JSON object; `nil` for anything else.
    public static func parse(_ data: Data) -> [String: Any]? {
        // Python reads these files as UTF-8 and refuses a byte-order mark;
        // `JSONSerialization` also takes UTF-16/32, whose JSON always holds a NUL byte.
        guard !data.starts(with: [0xEF, 0xBB, 0xBF]), !data.contains(0),
              let values = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else { return nil }
        // The shape pass is the costly half, and a telemetry fold pays it per ledger
        // line; only text that spells a boolean literal can hold one.
        guard data.range(of: Data("true".utf8)) != nil
                || data.range(of: Data("false".utf8)) != nil else { return values }
        guard let shape = try? JSONDecoder().decode([String: Shape].self, from: data)
        else { return nil }
        return marked(values, .object(shape)) as? [String: Any]
    }

    /// A flag out of a decoded payload, or `fallback` when the field is absent or is not
    /// a JSON boolean. `agentstate._flag` is the Python twin, strict for the same reason.
    public static func flag(_ raw: Any?, _ fallback: Bool = false) -> Bool {
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
