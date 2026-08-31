import Foundation

/// The single source of truth for "are we a one-shot headless self-test?" —
/// shared by the AppDelegate (skip the singleton kill / automation prompt) and
/// the Store (skip polls, watchers, and allocator shell-outs). Previously each
/// kept its own copy of this env-var list; a mode added to only one of them
/// either killed the live menu-bar app from a self-test or started real polls
/// (and potentially agent dispatch) during a one-shot check.
enum Headless {
    /// Any one-shot self-test mode (dump, lookup, render, prompt print, track
    /// test, device dump, poll/scan dry-runs).
    static let active: Bool = {
        let env = ProcessInfo.processInfo.environment
        return env["DIPLOMAT_DUMP"] == "1"
            || env["DIPLOMAT_SELF_UPDATE"] == "1"
            || env["DIPLOMAT_LOOKUP"] != nil
            || env["DIPLOMAT_PRINT_PROMPT"] != nil
            || env["DIPLOMAT_SETTINGS_DUMP"] == "1"
            || env["DIPLOMAT_RENDER"] != nil
            || env["DIPLOMAT_TRACK_TEST"] == "1"
            || env["DIPLOMAT_QUEUE_TEST"] == "1"
            || env["DIPLOMAT_PUBLISH_TEST"] == "1"
            || env["DIPLOMAT_SWEEP_TEST"] == "1"
            || env["DIPLOMAT_DEVICE_DUMP"] == "1"
            || env["DIPLOMAT_AUTOFIX_POLL"] == "1"
            || env["DIPLOMAT_APIWATCH_SCAN"] == "1"
            || env["DIPLOMAT_SPAWN_FOCUS_TEST"] == "1"
            || env["DIPLOMAT_SPAWN_SCRIPT_TEST"] == "1"
            || env["DIPLOMAT_OSA_TEST"] == "1"
            || env["DIPLOMAT_MESH_CMD_TEST"] == "1"
            || env["DIPLOMAT_ALLOCATOR_TEST"] == "1"
    }()

    /// Specifically the DIPLOMAT_RENDER snapshot mode. Renders seed a real
    /// Store with preview values, and they share the live app's defaults domain —
    /// so NOTHING may be persisted in this mode, or a render would silently
    /// overwrite the user's real settings (including the auto-approve opt-in).
    static let isRender = ProcessInfo.processInfo.environment["DIPLOMAT_RENDER"] != nil
}
