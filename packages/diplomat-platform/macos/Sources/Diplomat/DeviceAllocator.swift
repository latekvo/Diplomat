import Foundation
import DiplomatCore

/// Bridge to the local device-allocator daemon + installer (the Node package under
/// `device-allocator/`). The applet is a *viewer* of the daemon's public state file
/// and a *driver* of its installer — it never allocates devices itself.
///
/// Three surfaces:
///   - `readState()` decodes the daemon's `~/.diplomat/device-allocator/state.json`
///     (the live pool + who holds what), polled by the Store.
///   - `call()` and its wrappers ask the daemon, over its unix socket, to act on a
///     device or on the remote device pool.
///   - `check()`, `install()` and their siblings shell the package's `install.js` for
///     the Devices and Settings screens' install controls.

// MARK: - state model (mirrors the daemon's public snapshot)

struct DeviceOwner: Decodable, Equatable {
    let agentName: String?
    let ownerPid: Int?
}

struct DeviceAllocation: Decodable, Identifiable, Equatable {
    let key: String
    let platform: String
    let name: String?
    let version: String?
    let apiVersion: String?
    let handle: String?
    let status: String
    let owner: DeviceOwner?
    let allocatedAt: Double?
    let idleMs: Double?
    let brokenReason: String?
    let repairLog: String?
    let format: String?
    /// `local` or `remote`. A daemon without a remote pool writes none: its rows are all
    /// local.
    var source: String? = nil

    var id: String { key }
    var isAllocated: Bool { owner?.ownerPid != nil || status == "repairing" }
    var isRemote: Bool { source == "remote" }
}

/// A JSON value of any shape, for the parts of the snapshot and of the daemon's
/// replies whose keys a remote-pool provider defines rather than this app.
enum JSONValue: Decodable, Equatable {
    case string(String), number(Double), bool(Bool), null
    case array([JSONValue]), object([String: JSONValue])

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
        else { self = .object(try c.decode([String: JSONValue].self)) }
    }

    subscript(key: String) -> JSONValue? {
        if case .object(let o) = self { return o[key] }
        return nil
    }

    var string: String? {
        if case .string(let s) = self, !s.isEmpty { return s }
        return nil
    }

    /// Any value as one line: how a key this app does not know is shown.
    var text: String {
        switch self {
        case .string(let s): return s
        case .number(let n):
            return n == n.rounded() && abs(n) < 1e15 ? String(Int64(n)) : String(n)
        case .bool(let b): return b ? "true" : "false"
        case .null: return "—"
        case .array(let a): return a.map(\.text).joined(separator: ", ")
        case .object(let o):
            return o.keys.sorted().map { "\($0): \(o[$0]?.text ?? "")" }.joined(separator: ", ")
        }
    }
}

/// The snapshot's `remote`. Every key it does not name is kept, sorted, to be shown as
/// it stands.
struct RemoteStatus: Equatable {
    struct Extra: Equatable, Hashable { let key: String; let value: String }

    let provider: String?
    /// `off`, `loading`, `error`, `on`, `signed-out`, `signing-in`,
    /// `sign-in-unanswered`, or `unknown` when the snapshot names none.
    let state: String
    let reason: String?
    let url: String?
    let loginUrl: String?
    /// With `sign-in-unanswered`: when sign-in prompts resume.
    let retryAt: Date?
    let extras: [Extra]

    init(provider: String? = nil, state: String, reason: String? = nil, url: String? = nil,
         loginUrl: String? = nil, retryAt: Date? = nil, extras: [Extra] = []) {
        self.provider = provider
        self.state = state
        self.reason = reason
        self.url = url
        self.loginUrl = loginUrl
        self.retryAt = retryAt
        self.extras = extras
    }

    /// nil for `null`; any other non-object is an unreadable status, never an absent pool.
    init?(json: JSONValue) {
        guard case .object(let o) = json else {
            if json == .null { return nil }
            self.init(state: "unknown", extras: [Extra(key: "remote", value: json.text)])
            return
        }
        var known: Set<String> = ["provider", "state", "reason", "url", "loginUrl"]
        let retryAt = o["retryAt"].flatMap(RemoteStatus.date)
        // An unparseable retryAt stays an extra, shown as it stands.
        if retryAt != nil { known.insert("retryAt") }
        self.init(provider: o["provider"]?.string,
                  state: o["state"]?.string ?? "unknown",
                  reason: o["reason"].flatMap { $0 == .null ? nil : $0.text },
                  url: o["url"]?.string,
                  loginUrl: o["loginUrl"]?.string,
                  retryAt: retryAt,
                  extras: o.keys.sorted().filter { !known.contains($0) }
                      .map { Extra(key: $0, value: o[$0]?.text ?? "") })
    }

    /// An ISO-8601 instant or epoch milliseconds.
    static func date(_ v: JSONValue) -> Date? {
        switch v {
        case .string(let s):
            let f = ISO8601DateFormatter()
            f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            if let d = f.date(from: s) { return d }
            f.formatOptions = [.withInternetDateTime]
            return f.date(from: s)
        case .number(let n) where n.isFinite && n > 0:
            return Date(timeIntervalSince1970: n / 1000)
        default:
            return nil
        }
    }
}

struct DeviceState: Decodable, Equatable {
    let devices: [DeviceAllocation]
    /// `updatedAt` is left undecoded: it changes on every publish and would defeat the
    /// Store's "publish only on change" guard.
    var daemonPid: Int? = nil
    var remote: RemoteStatus? = nil

    init(devices: [DeviceAllocation], daemonPid: Int? = nil, remote: RemoteStatus? = nil) {
        self.devices = devices
        self.daemonPid = daemonPid
        self.remote = remote
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        devices = try c.decode([Lossy<DeviceAllocation>].self, forKey: .devices)
            .compactMap(\.value)
        daemonPid = ((try? c.decodeIfPresent(Double.self, forKey: .daemonPid)) ?? nil)
            .map(clampedInt)
        remote = ((try? c.decodeIfPresent(JSONValue.self, forKey: .remote)) ?? nil)
            .flatMap(RemoteStatus.init(json:))
    }

    private enum CodingKeys: String, CodingKey { case devices, daemonPid, remote }

    private struct Lossy<T: Decodable>: Decodable {
        let value: T?
        init(from decoder: Decoder) throws { value = try? T(from: decoder) }
    }

    var allocatedCount: Int { devices.filter { $0.isAllocated }.count }
    var freeCount: Int { devices.count - allocatedCount }
}

/// One device the remote pool lists (`POST /remote/devices`).
struct RemotePoolDevice: Equatable, Identifiable {
    let id: String
    let platform: String?
    let name: String?
    let version: String?
    let apiVersion: String?
    let format: String?
    let state: String?
}

/// What the daemon answered a remote-pool call with.
enum RemoteReply: Equatable {
    case ok
    case devices([RemotePoolDevice])
    case reason(String)
}

struct DaemonReply: Equatable {
    let status: Int
    let json: JSONValue?
}

// MARK: - installer status

struct AllocatorInstall: Decodable, Equatable {
    var mcpRegistered = false
    var skillInstalled = false
    var ruleInstalled = false
    var claudeMdInjected = false
    var daemonRunning = false
    var installed = false
    /// The installed package's version, for display. `nil` until a check answers.
    var version: String?
    /// An install whose deployed copies no longer match this checkout (the installer
    /// compares them by content). Never true for a machine that isn't installed —
    /// so a deliberate uninstall is never mistaken for damage to repair.
    var outdated = false
    /// Which artifacts drifted (`skill`, `rule`, `claudeMd`, `mcp`) — shown beside
    /// the status so "out of date" says what, not just that.
    var drift: [String] = []
    /// Whether the reply said `installed` at all. `--start-daemon`'s does not, and its
    /// silence says nothing about the install (see `installStatus(over:)`).
    var reportsInstalled = false
    /// What went wrong, when the installer says (a restart whose old daemon never exited,
    /// a start beside a daemon that does not answer).
    var error: String?

    init() {}

    /// Tolerant decode: any missing key (e.g. an older `--uninstall` output that
    /// omitted `installed`, or an error payload) defaults to false rather than
    /// failing the whole decode and discarding the result.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        mcpRegistered = (try? c.decode(Bool.self, forKey: .mcpRegistered)) ?? false
        skillInstalled = (try? c.decode(Bool.self, forKey: .skillInstalled)) ?? false
        ruleInstalled = (try? c.decode(Bool.self, forKey: .ruleInstalled)) ?? false
        claudeMdInjected = (try? c.decode(Bool.self, forKey: .claudeMdInjected)) ?? false
        daemonRunning = (try? c.decode(Bool.self, forKey: .daemonRunning)) ?? false
        let installedField = try? c.decode(Bool.self, forKey: .installed)
        installed = installedField ?? false
        reportsInstalled = installedField != nil
        error = try? c.decode(String.self, forKey: .error)
        version = try? c.decode(String.self, forKey: .version)
        outdated = (try? c.decode(Bool.self, forKey: .outdated)) ?? false
        drift = (try? c.decode([String].self, forKey: .drift)) ?? []
    }

    private enum CodingKeys: String, CodingKey {
        case mcpRegistered, skillInstalled, ruleInstalled, claudeMdInjected, daemonRunning
        case installed, version, outdated, drift, error
    }

    /// This reply, keeping `previous`'s install status when the reply does not report
    /// one, so a reply without `installed` never reads as "not installed".
    func installStatus(over previous: AllocatorInstall?) -> AllocatorInstall {
        guard !reportsInstalled, let previous else { return self }
        var merged = self
        merged.installed = previous.installed
        merged.reportsInstalled = previous.reportsInstalled
        return merged
    }

    /// Unknown until the first check completes (so the UI can say "checking…").
    static let unknown = AllocatorInstall()
}

enum DeviceAllocator {
    static let package = NodePackage.deviceAllocator

    static var packageDir: String { package.packageDir }
    static var stateURL: URL { package.baseDir.appendingPathComponent("state.json") }
    static var socketPath: String { package.socketPath }

    /// Any HTTP status is an answer; nil is no answer at all.
    static func call(_ endpoint: String, body: [String: Any] = [:],
                     timeoutSecs: Int) -> DaemonReply? {
        if case .reply(let r) = send(endpoint, body: body, timeoutSecs: timeoutSecs) { return r }
        return nil
    }

    /// `call`, telling a daemon that never got the request from one that did not reply.
    static func send(_ endpoint: String, body: [String: Any] = [:],
                     timeoutSecs: Int) -> NodePackage.Sent {
        package.send(endpoint, body: body, timeoutSecs: timeoutSecs)
    }

    /// A 4xx is never a success: a device gone since the snapshot answers 404, and the
    /// audit feed must not log an action that never happened.
    static func post(_ endpoint: String, body: [String: Any], timeoutSecs: Int) -> Bool {
        call(endpoint, body: body, timeoutSecs: timeoutSecs).map { (200..<300).contains($0.status) }
            ?? false
    }

    /// Free a device and shut it down, or return a remote lease.
    @discardableResult
    static func killDevice(key: String) -> Bool {
        // Generous timeout: the daemon shuts a real simulator/emulator down inline.
        post("kill", body: ["key": key], timeoutSecs: 25)
    }

    /// Take a device back from the agent holding it, releasing on its behalf.
    static func releaseDevice(key: String, ownerPid: Int) -> Bool {
        guard let r = call("release", body: ["ownerPid": ownerPid, "deviceId": key],
                           timeoutSecs: 25),
              (200..<300).contains(r.status) else { return false }
        // 200 with nothing released: the agent had already let it go.
        if case .number(let n)? = r.json?["released"] { return n >= 1 }
        return false
    }

    /// End a device's quarantine.
    static func markRepaired(key: String) -> Bool {
        post("repaired", body: ["key": key], timeoutSecs: 10)
    }

    /// The daemon answers once the browser sign-in ends: up to ten minutes.
    static func remoteSignIn() -> RemoteReply {
        signInReply(call("remote/sign-in", timeoutSecs: 11 * 60 + 30))
    }

    /// A network call on the daemon's side; never prompts a sign-in.
    static func remoteDevices() -> RemoteReply {
        devicesReply(call("remote/devices", timeoutSecs: 90))
    }

    /// The reason a reply carries when it is not the answer asked for, or nil.
    private static func refusal(_ r: DaemonReply?) -> String? {
        guard let r else { return "the allocator daemon did not answer" }
        if r.status == 404, r.json?["error"]?.string?.hasPrefix("unknown route") == true {
            return "this allocator has no remote device pool — update it, then restart its daemon"
        }
        if !(200..<300).contains(r.status) {
            return r.json?["error"]?.string ?? r.json?["reason"]?.string
                ?? "the allocator answered HTTP \(r.status)"
        }
        return nil
    }

    static func signInReply(_ r: DaemonReply?) -> RemoteReply {
        if let why = refusal(r) { return .reason(why) }
        if r?.json?["ok"] == .bool(true) { return .ok }
        return .reason(r?.json?["reason"]?.string ?? "the sign-in did not complete")
    }

    static func devicesReply(_ r: DaemonReply?) -> RemoteReply {
        if let why = refusal(r) { return .reason(why) }
        guard case .array(let rows)? = r?.json?["devices"] else {
            return .reason(r?.json?["reason"]?.string ?? "the pool sent no device list")
        }
        return .devices(rows.enumerated().compactMap { i, row in
            guard case .object = row else { return nil }
            func field(_ k: String) -> String? {
                row[k].flatMap { $0 == .null ? nil : $0.text }.flatMap { $0.isEmpty ? nil : $0 }
            }
            return RemotePoolDevice(id: field("key") ?? "#\(i)", platform: field("platform"),
                                    name: field("name"), version: field("version"),
                                    apiVersion: field("apiVersion"), format: field("format"),
                                    state: field("state"))
        })
    }

    static var packageAvailable: Bool { package.packageAvailable }
    static var nodeAvailable: Bool { NodePackage.resolveNode() != nil }
    static var depsInstalled: Bool { package.depsInstalled }
    @discardableResult
    static func ensureDeps() -> Bool { package.ensureDeps() }

    // MARK: state

    static func readState() -> DeviceState? {
        guard let data = try? Data(contentsOf: stateURL) else { return nil }
        return try? JSONDecoder().decode(DeviceState.self, from: data)
    }

    // MARK: installer (blocking — call off the main thread)

    /// Whether a launch should run `--install`, given what `--check` reported and
    /// whether this machine's setup has already been settled.
    ///
    /// One routine because it is one decision made in two places (this app and the
    /// Linux applet, whose `deviceallocator.needs_install` is its twin) covering three
    /// situations that look alike and must not be confused:
    ///
    /// - **First run** — nothing installed, nothing settled. Install. A failure leaves
    ///   `setupDone` false, so the next launch retries; that is how a machine with no
    ///   node yet eventually gets set up.
    /// - **Stale** — installed, but the deployed skill/rule/CLAUDE.md/registration no
    ///   longer match this checkout. Re-install: `--install` rewrites every artifact,
    ///   so it is also the repair.
    /// - **Settled uninstall** — the user removed it in Settings. Leave it alone. This
    ///   is the one an "is everything in place?" check gets wrong, and getting it
    ///   wrong means silently reinstalling something the user deliberately took off.
    ///
    /// Derives current from `installed && !outdated` rather than a positive flag: an
    /// installer predating drift detection reports neither, and keying off a missing
    /// flag would reinstall on every launch forever.
    static func needsInstall(status: AllocatorInstall, setupDone: Bool) -> Bool {
        if setupDone && !status.installed { return false }
        return !(status.installed && !status.outdated)
    }

    static func check() -> AllocatorInstall { runInstaller("--check") }
    static func install() -> AllocatorInstall { runInstaller("--install") }
    static func uninstall() -> AllocatorInstall { runInstaller("--uninstall") }
    /// Start the daemon if none answers. The reply has no `installed`: see
    /// `AllocatorInstall.installStatus(over:)`.
    static func startDaemon() -> AllocatorInstall { runInstaller("--start-daemon") }
    /// The one way to replace a daemon from older code: `--install` and `--start-daemon`
    /// keep any that answers.
    static func restartDaemon() -> AllocatorInstall { runInstaller("--restart-daemon") }

    private static func runInstaller(_ arg: String) -> AllocatorInstall {
        package.runInstaller(arg)
    }
}
