import Foundation

/// First-run daemon setup. When launched from a terminal (`swift run`) and not
/// yet installed, offers to install Diplomat as a login LaunchAgent (via the
/// existing `install/install-autostart.sh`) so it boots on every login. Silent
/// for non-interactive launches (`open`, launchd) and headless self-tests.
enum Daemon {
    static let label = "com.ignacy.diplomat"

    static var plistPath: String {
        (NSHomeDirectory() as NSString).appendingPathComponent("Library/LaunchAgents/\(label).plist")
    }
    static var isInstalled: Bool {
        FileManager.default.fileExists(atPath: plistPath)
    }

    /// Show the opt-in only on an interactive TTY and only when not already a
    /// daemon. Returns true when an install was kicked off (caller should hand off
    /// / exit; the installed daemon takes over via the singleton).
    @discardableResult
    static func offerInstallIfInteractive() -> Bool {
        guard isatty(STDIN_FILENO) != 0 else { return false }   // not a terminal (open / launchd)
        guard !isInstalled else {
            out("Diplomat: already installed as a login daemon — running this instance for now.\n")
            return false
        }
        out("""

        ┌─ Diplomat setup ─────────────────────────────────────────
        │ Install as a background daemon? This will:
        │   • build + copy Diplomat.app to /Applications
        │   • add a per-user LaunchAgent so the wrench boots on login
        │   • start it now (it replaces this foreground instance)
        │   • ask macOS for permission to control your terminal (SPAWN)
        └──────────────────────────────────────────────────────────────
        Accept [y/N]
        """)
        guard let line = readLine(strippingNewline: true),
              line.trimmingCharacters(in: .whitespaces).lowercased().hasPrefix("y") else {
            out("Skipped — running in the foreground for this session.\n")
            return false
        }
        return install()
    }

    /// Kick off `install/install-autostart.sh` detached (it survives this process
    /// being replaced by the freshly-launched daemon) and return.
    @discardableResult
    static func install() -> Bool {
        guard let script = installScriptPath() else {
            err("Diplomat: install-autostart.sh not found (no checkout at \(RepoPaths.root.path)).\n")
            return false
        }
        out("Installing… (log: /tmp/diplomat-install.log)\n")
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/bash")
        // nohup + background so the orphaned installer keeps running after we exit.
        proc.arguments = ["-c", "nohup /bin/bash \(shq(script)) > /tmp/diplomat-install.log 2>&1 &"]
        do { try proc.run() } catch {
            err("Diplomat: install failed to start: \(error)\n"); return false
        }
        proc.waitUntilExit()   // the outer shell returns immediately after backgrounding
        return true
    }

    private static func installScriptPath() -> String? {
        let p = RepoPaths.installDir.appendingPathComponent("install-autostart.sh").path
        return FileManager.default.isExecutableFile(atPath: p) ? p : nil
    }

    private static func shq(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }
    private static func out(_ s: String) { FileHandle.standardOutput.write(Data(s.utf8)) }
    private static func err(_ s: String) { FileHandle.standardError.write(Data(s.utf8)) }
}
