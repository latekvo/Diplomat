import Foundation

/// The single source of truth for "are we a one-shot headless self-test?" —
/// shared by `Launch` (refuse a mode this build does not run), the AppDelegate
/// (skip the singleton kill / automation prompt) and the Store (skip polls,
/// watchers, and allocator shell-outs).
enum Headless {
    /// Every one-shot mode `AppDelegate.applicationDidFinishLaunching` dispatches:
    /// `true` for one that takes a value, `false` for a flag set to 1.
    static let modes: [String: Bool] = [
        "DIPLOMAT_DUMP": false,
        "DIPLOMAT_SELF_UPDATE": false,
        "DIPLOMAT_LOOKUP": true,
        "DIPLOMAT_PRINT_PROMPT": true,
        "DIPLOMAT_SETTINGS_DUMP": false,
        "DIPLOMAT_RENDER": true,
        "DIPLOMAT_TRACK_TEST": false,
        "DIPLOMAT_QUEUE_TEST": false,
        "DIPLOMAT_PUBLISH_TEST": false,
        "DIPLOMAT_SWEEP_TEST": false,
        "DIPLOMAT_QUOTA_TEST": false,
        "DIPLOMAT_DEVICE_DUMP": false,
        "DIPLOMAT_AUTOFIX_POLL": false,
        "DIPLOMAT_APIWATCH_SCAN": false,
        "DIPLOMAT_APIWATCH_TEST": false,
        "DIPLOMAT_SPAWN_FOCUS_TEST": false,
        "DIPLOMAT_SPAWN_SCRIPT_TEST": false,
        "DIPLOMAT_OSA_TEST": false,
        "DIPLOMAT_MESH_CMD_TEST": false,
        "DIPLOMAT_ALLOCATOR_TEST": false,
    ]

    private static func turnsOn(_ name: String, _ value: String) -> Bool {
        modes[name].map { $0 || value == "1" } ?? false
    }

    static let active: Bool = ProcessInfo.processInfo.environment
        .contains { name, value in turnsOn(name, value) }

    /// Specifically the DIPLOMAT_RENDER snapshot mode. Renders seed a real
    /// Store with preview values, and they share the live app's defaults domain —
    /// so NOTHING may be persisted in this mode, or a render would silently
    /// overwrite the user's real settings (including the auto-approve opt-in).
    static let isRender = ProcessInfo.processInfo.environment["DIPLOMAT_RENDER"] != nil

    /// How modes are named on either platform. No setting either app reads is.
    private static let modeSuffixes = ["_TEST", "_DUMP", "_SCAN", "_POLL"]

    /// The Linux applet's modes that have no macOS twin and none of those suffixes.
    private static let linuxOnlyModes: Set<String> = ["DIPLOMAT_AGENTS"]

    /// Whether a variable is one a caller sets to ask for a one-shot run.
    private static func looksLikeMode(_ name: String) -> Bool {
        guard name.hasPrefix("DIPLOMAT_") else { return false }
        return modes[name] != nil || linuxOnlyModes.contains(name)
            || modeSuffixes.contains { name.hasSuffix($0) }
    }

    /// The variables in `env` that ask for a one-shot this build will not run, as
    /// sorted `NAME=value`s. Started with only these, the binary would run as the
    /// live app: replace the running Diplomat (`SingleInstance`) and act on the
    /// defaults domain and `~/.diplomat` it shares.
    static func unrunnable(in env: [String: String]) -> [String] {
        env.filter { name, value in looksLikeMode(name) && !turnsOn(name, value) }
            .map { "\($0.key)=\($0.value)" }
            .sorted()
    }

    /// What `Launch` prints before exiting, when `unrunnable` names anything.
    static func refusal(_ unrunnable: [String]) -> String {
        let known = modes.sorted { $0.key < $1.key }
            .map { $0.key + ($0.value ? "=<value>" : "=1") }
        return "Diplomat: refusing to start: \(unrunnable.joined(separator: ", ")) asks for "
            + "a one-shot mode this build does not run, and without one it would replace "
            + "the running Diplomat and act on its real state.\n"
            + "Modes this build runs: \(known.joined(separator: ", "))\n"
    }
}
