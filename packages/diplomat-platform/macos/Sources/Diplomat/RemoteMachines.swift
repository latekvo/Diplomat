import Foundation
import DiplomatCore

/// Bridge to the remote-machines MCP (`packages/remote-machines`): a viewer of its daemon's
/// snapshot and history, a driver of its installer, and the writer of the operator's
/// exclusions, which the daemon reads from config.json.
enum RemoteMachines {
    static let package = NodePackage.remoteMachines

    static var stateURL: URL { package.baseDir.appendingPathComponent("state.json") }
    static var historyURL: URL { package.baseDir.appendingPathComponent("history.json") }

    static func readState() -> MachinesSnapshot? {
        guard let data = try? Data(contentsOf: stateURL) else { return nil }
        return try? JSONDecoder().decode(MachinesSnapshot.self, from: data)
    }

    static func readHistory() -> MachineHistory? {
        guard let data = try? Data(contentsOf: historyURL) else { return nil }
        return try? JSONDecoder().decode(MachineHistory.self, from: data)
    }

    /// Whether the daemon answered the scan with success; nil when none is listening.
    static func scan() -> Bool? {
        switch package.send(MachinesScan.endpoint, body: MachinesScan.body,
                            timeoutSecs: MachinesScan.timeoutSecs) {
        case .unreachable: return nil
        case .unanswered: return false
        case .reply(let r): return (200..<300).contains(r.status)
        }
    }

    // MARK: installer (blocking — call off the main thread)

    static var ready: Bool { package.packageAvailable && NodePackage.resolveNode() != nil }

    static func check() -> AllocatorInstall { package.runInstaller("--check") }
    static func install() -> AllocatorInstall { package.runInstaller("--install") }
    static func uninstall() -> AllocatorInstall { package.runInstaller("--uninstall") }
    static func startDaemon() -> AllocatorInstall { package.runInstaller("--start-daemon") }
    static func restartDaemon() -> AllocatorInstall { package.runInstaller("--restart-daemon") }

    // MARK: exclusions

    /// Applies `action` to config.json, writing only the keys it changes, and says whether
    /// they all landed.
    static func applyExclusion(_ action: MachineExclusions.Action, target: String,
                               names: [String], now: Date) -> Bool {
        let current = MachineExclusions(config: AppConfig.read())
        let next = current.applying(action, target: target, names: names, now: now)
        return next.edits(from: current).allSatisfy(AppConfig.update)
    }
}
