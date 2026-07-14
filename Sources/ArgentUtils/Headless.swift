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
        return env["ARGENT_UTILS_DUMP"] == "1"
            || env["ARGENT_UTILS_LOOKUP"] != nil
            || env["ARGENT_UTILS_PRINT_PROMPT"] != nil
            || env["ARGENT_UTILS_SETTINGS_DUMP"] == "1"
            || env["ARGENT_UTILS_RENDER"] != nil
            || env["ARGENT_UTILS_TRACK_TEST"] == "1"
            || env["ARGENT_UTILS_DEVICE_DUMP"] == "1"
            || env["ARGENT_UTILS_AUTOFIX_POLL"] == "1"
            || env["ARGENT_UTILS_APIWATCH_SCAN"] == "1"
            || env["ARGENT_UTILS_SPAWN_FOCUS_TEST"] == "1"
    }()

    /// Specifically the ARGENT_UTILS_RENDER snapshot mode. Renders seed a real
    /// Store with preview values, and they share the live app's defaults domain —
    /// so NOTHING may be persisted in this mode, or a render would silently
    /// overwrite the user's real settings (including the auto-approve opt-in).
    static let isRender = ProcessInfo.processInfo.environment["ARGENT_UTILS_RENDER"] != nil
}
