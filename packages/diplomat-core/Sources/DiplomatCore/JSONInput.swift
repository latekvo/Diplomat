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
/// It is used for the SHAPE only. The values still come from `JSONSerialization` (bar the
/// keys and doubles `parse` reads as `json` does), so every other cast in the decoders —
/// `as? Int`, `as? NSNumber`, `as? [String]` — reads what `JSONSerialization` produced,
/// and the one difference is that a boolean arrives as a `Flag` that no numeric cast can
/// satisfy.
public enum JSONInput {

    /// A JSON `true`/`false`, and nothing else. Deliberately not `Bool`: the whole point
    /// is that a value which is not a JSON boolean must fail the cast.
    public struct Flag {
        public let on: Bool
    }

    /// Deepest nesting read, the top-level object being 1. swift-corelibs-foundation
    /// refuses deeper, Darwin refuses deeper but for an empty container, and
    /// `jsoninput.MAX_DEPTH` is the twin.
    public static let maxDepth = 512

    /// Decode a JSON object; `nil` for anything else.
    ///
    /// Also `nil` for what `json` refuses and `JSONSerialization` takes - a trailing
    /// comma, and on Darwin `-1e400`, read as `-inf` (an uncatchable exception at the
    /// next write of the book). For a `sharedFile`, also for a string opening with
    /// U+FEFF, which Darwin drops and `json` keeps; stdin keeps that string, which may be
    /// a PR title or a Slack message.
    ///
    /// And read as `json` reads it where a `JSONSerialization` does not: a key given twice
    /// keeps its first value (corelibs keeps the last), and a number no 64-bit integer
    /// holds is its text's correctly rounded `Double` (both platforms' parsers can land an
    /// ulp or more off, corelibs on about a third of doubles).
    public static func parse(_ data: Data, sharedFile: Bool = false) -> [String: Any]? {
        // Python reads these files as UTF-8 and refuses a byte-order mark;
        // `JSONSerialization` also takes UTF-16/32, whose JSON always holds a NUL byte.
        guard !data.starts(with: [0xEF, 0xBB, 0xBF]), !data.contains(0),
              var values = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              usable(values, depth: 1), let scan = scanned(data, bomStrings: sharedFile)
        else { return nil }
        if !scan.repeats.isEmpty {
            return parse(without(scan.repeats, data), sharedFile: sharedFile)
        }
        for (path, number) in scan.doubles {
            if let fixed = corrected(values, path[...], number) as? [String: Any] { values = fixed }
        }
        // The shape pass is the costly half, and a telemetry fold pays it per ledger
        // line; only text that spells a boolean literal can hold one.
        guard data.range(of: Data("true".utf8)) != nil
                || data.range(of: Data("false".utf8)) != nil else { return values }
        guard let shape = try? JSONDecoder().decode([String: Shape].self, from: data)
        else { return nil }
        return marked(values, .object(shape)) as? [String: Any]
    }

    /// Every container within `maxDepth`.
    private static func usable(_ value: Any, depth: Int) -> Bool {
        switch value {
        case let object as [String: Any]:
            return depth <= maxDepth && object.values.allSatisfy { usable($0, depth: depth + 1) }
        case let array as [Any]:
            return depth <= maxDepth && array.allSatisfy { usable($0, depth: depth + 1) }
        default:
            return true
        }
    }

    /// What the byte pass found in a document `JSONSerialization` took.
    private struct Scan {
        /// Each member repeating a key, from the comma before it to the end of its value.
        var repeats: [Range<Int>] = []
        /// Each number no 64-bit integer holds: where it sits, and its text's `Double`.
        var doubles: [(path: [Step], value: Double)] = []
    }

    private enum Step { case key(String), index(Int) }

    /// A container the byte pass is inside.
    private struct Open {
        let object: Bool
        /// An array's current element.
        var index = 0
        /// An object's current key, unescaped, and every key it has had.
        var key: ArraySlice<UInt8> = [], keys = Set<ArraySlice<UInt8>>()
        /// The comma before the current member, and whether that member repeats a key.
        var memberFrom = 0, repeats = false

        mutating func keyed(_ name: ArraySlice<UInt8>) {
            repeats = !keys.insert(name).inserted
            if !repeats { key = name }
        }

        /// The current member ends at `end`; its span if it repeats a key.
        mutating func ended(at end: Int) -> Range<Int>? {
            defer { index += 1; memberFrom = end; repeats = false }
            return repeats ? memberFrom..<end : nil
        }
    }

    /// `nil` where a `,` outside a string is followed, past whitespace, by `]` or `}`,
    /// where a number is past `Double`'s range, or, with `bomStrings`, where a string
    /// opens with U+FEFF, raw or escaped.
    private static func scanned(_ data: Data, bomStrings: Bool) -> Scan? {
        let bytes = [UInt8](data)
        var scan = Scan(), open: [Open] = []
        var i = 0, comma = false, keyNext = false, escaped = false, string: Int?
        while i < bytes.count {
            let byte = bytes[i]
            i += 1
            if let from = string {
                if byte == UInt8(ascii: "\\") {
                    i += 1
                    escaped = true
                } else if byte == UInt8(ascii: "\"") {
                    string = nil
                    if keyNext {
                        keyNext = false
                        open[open.count - 1].keyed(escaped ? unescaped(bytes[from..<i])
                                                           : bytes[from + 1..<i - 1])
                    }
                }
                continue
            }
            switch byte {
            case UInt8(ascii: "\""):
                string = i - 1
                escaped = false
                comma = false
                let next = bytes[i..<min(i + 6, bytes.count)]
                if bomStrings, next.starts(with: [0xEF, 0xBB, 0xBF])
                    || next.first == UInt8(ascii: "\\")
                    && next.dropFirst().map({ $0 | 0x20 }).elementsEqual("ufeff".utf8) {
                    return nil
                }
            case UInt8(ascii: "{"), UInt8(ascii: "["):
                open.append(Open(object: byte == UInt8(ascii: "{")))
                keyNext = byte == UInt8(ascii: "{")
                comma = false
            case UInt8(ascii: ","):
                comma = true
                if let span = open[open.count - 1].ended(at: i - 1) { scan.repeats.append(span) }
                keyNext = open[open.count - 1].object
            case UInt8(ascii: "]"), UInt8(ascii: "}"):
                if comma { return nil }
                var closed = open.removeLast()
                if let span = closed.ended(at: i - 1) { scan.repeats.append(span) }
                keyNext = false
            case UInt8(ascii: " "), UInt8(ascii: "\t"), UInt8(ascii: "\n"), UInt8(ascii: "\r"):
                break
            case UInt8(ascii: "-"), UInt8(ascii: "0")...UInt8(ascii: "9"):
                comma = false
                let from = i - 1
                while i < bytes.count, numeric(bytes[i]) { i += 1 }
                let token = bytes[from..<i]
                let integral = !token.contains { $0 == UInt8(ascii: ".") || ($0 | 0x20) == UInt8(ascii: "e") }
                // 18 characters always fit Int64, which `JSONSerialization` reads exactly.
                if integral && token.count <= 18 { continue }
                let text = String(decoding: token, as: UTF8.self)
                if integral && (Int64(text) != nil || UInt64(text) != nil) { continue }
                guard let value = Double(text), value.isFinite else { return nil }
                scan.doubles.append((open.map {
                    $0.object ? .key(String(decoding: $0.key, as: UTF8.self)) : .index($0.index)
                }, value))
            default: comma = false
            }
        }
        return scan
    }

    private static func numeric(_ byte: UInt8) -> Bool {
        switch byte {
        case UInt8(ascii: "0")...UInt8(ascii: "9"), UInt8(ascii: "-"), UInt8(ascii: "+"),
             UInt8(ascii: "."), UInt8(ascii: "e"), UInt8(ascii: "E"):
            return true
        default:
            return false
        }
    }

    /// A quoted key holding an escape, as the bytes of the string it spells.
    private static func unescaped(_ quoted: ArraySlice<UInt8>) -> ArraySlice<UInt8> {
        let key = (try? JSONSerialization.jsonObject(with: Data(quoted), options: .fragmentsAllowed))
            as? String
        return key.map { ArraySlice($0.utf8) } ?? quoted
    }

    /// `data` less `spans`, each of which lies after or within the ones before it.
    private static func without(_ spans: [Range<Int>], _ data: Data) -> Data {
        let bytes = [UInt8](data)
        var kept: [UInt8] = [], at = 0
        for span in spans.sorted(by: { $0.lowerBound < $1.lowerBound }) where span.lowerBound >= at {
            kept += bytes[at..<span.lowerBound]
            at = span.upperBound
        }
        return Data(kept + bytes[at...])
    }

    /// `node` with the number at `path` read as `value`, or `nil` where it already is.
    private static func corrected(_ node: Any, _ path: ArraySlice<Step>, _ value: Double) -> Any? {
        switch path.first {
        case nil:
            guard let number = node as? NSNumber, number.doubleValue.bitPattern != value.bitPattern
            else { return nil }
            return NSNumber(value: value)
        case .key(let key)?:
            guard var object = node as? [String: Any], let child = object[key],
                  let fixed = corrected(child, path.dropFirst(), value) else { return nil }
            object[key] = fixed
            return object
        case .index(let index)?:
            guard var array = node as? [Any], array.indices.contains(index),
                  let fixed = corrected(array[index], path.dropFirst(), value) else { return nil }
            array[index] = fixed
            return array
        }
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
