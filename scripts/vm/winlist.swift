// Runs inside the test VM: one line per on-screen, normal-layer window, as
//   <owner pid> <owner name> <width>x<height>
// CGWindowList reports owners and bounds without the Screen Recording permission.
import CoreGraphics
import Foundation

let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
let windows = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] ?? []
for w in windows where (w[kCGWindowLayer as String] as? Int) == 0 {
    let pid = w[kCGWindowOwnerPID as String] as? Int ?? 0
    let owner = w[kCGWindowOwnerName as String] as? String ?? "?"
    let b = w[kCGWindowBounds as String] as? [String: Double] ?? [:]
    print("\(pid) \(owner) \(Int(b["Width"] ?? 0))x\(Int(b["Height"] ?? 0))")
}
