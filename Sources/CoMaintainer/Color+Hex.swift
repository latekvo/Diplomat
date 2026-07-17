import SwiftUI
import AppKit

/// Hex ↔ Color bridging so per-tool tint overrides can live in UserDefaults as
/// "#RRGGBB" strings (sRGB, opacity dropped — the tints are always opaque).
extension Color {
    /// "#7F3FBF" (sRGB, 8-bit per channel).
    var hexRGB: String {
        let ns = NSColor(self).usingColorSpace(.sRGB) ?? .gray
        let r = Int((ns.redComponent * 255).rounded())
        let g = Int((ns.greenComponent * 255).rounded())
        let b = Int((ns.blueComponent * 255).rounded())
        return String(format: "#%02X%02X%02X", r, g, b)
    }

    /// Parses "#RRGGBB" / "RRGGBB"; nil on anything else.
    init?(hex: String) {
        var s = hex.trimmingCharacters(in: .whitespaces)
        if s.hasPrefix("#") { s.removeFirst() }
        guard s.count == 6, let v = Int(s, radix: 16) else { return nil }
        self = Color(.sRGB,
                     red: Double((v >> 16) & 0xFF) / 255,
                     green: Double((v >> 8) & 0xFF) / 255,
                     blue: Double(v & 0xFF) / 255)
    }
}
