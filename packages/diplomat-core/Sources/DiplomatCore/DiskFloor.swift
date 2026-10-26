import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// The free space below which this machine starts none of the disk-hungry work it
/// starts on its own: no automatic agent (each makes a worktree and installs into it)
/// and no scheduled self-update (a merge that hits ENOSPC halfway leaves a
/// half-updated checkout). The floor is `minFreeDiskGiB` in `assets/config.json`,
/// shared with the Python twin `diskfloor.py`; `DIPLOMAT_MIN_FREE_DISK_MIB` overrides it.
///
/// Free space is what `statfs` reports available to an unprivileged writer
/// (`f_bavail`), the number `shutil.disk_usage` reads on the Python side. Not
/// `volumeAvailableCapacityForImportantUsageKey`: that one counts purgeable space, which
/// no write can use until macOS has purged it.
public enum DiskFloor {
    public struct Reading: Equatable {
        /// The path asked about; the reading is for the volume holding it.
        public let path: String
        public let free: Int64
        public let floor: Int64
        public var low: Bool { free < floor }

        public init(path: String, free: Int64, floor: Int64) {
            self.path = path
            self.free = free
            self.floor = floor
        }
    }

    public static let envOverride = "DIPLOMAT_MIN_FREE_DISK_MIB"

    /// The floor in bytes, or nil when neither the override nor the asset names one.
    public static func floor(env: [String: String] = ProcessInfo.processInfo.environment)
        -> Int64? {
        if let raw = env[envOverride], let mib = Int64(raw), mib >= 0 {
            return mib << 20
        }
        guard let gib = (try? CoreAssets.config())?.minFreeDiskGiB, gib.isFinite, gib >= 0
        else { return nil }
        return Int64(gib * Double(1 << 30))
    }

    /// Bytes free on the volume holding `path`, asked of its nearest existing ancestor:
    /// a directory not made yet will be made on that volume. Nil when none is readable.
    public static func free(at path: String) -> Int64? {
        var url = URL(fileURLWithPath: path).standardizedFileURL
        while true {
            if let n = statFree(url.path) { return n }
            let parent = url.deletingLastPathComponent()
            if parent.path == url.path { return nil }
            url = parent
        }
    }

    /// The reading for whichever of `paths` has the least room, the volume work that
    /// writes to all of them runs out on first. Nil when there is no floor or no path
    /// is readable, which leaves the work ungated.
    public static func lowest(of paths: [String],
                              env: [String: String] = ProcessInfo.processInfo.environment)
        -> Reading? {
        guard let floor = floor(env: env) else { return nil }
        return paths.compactMap { p in free(at: p).map { Reading(path: p, free: $0, floor: floor) } }
            .min { $0.free < $1.free }
    }

    /// Why work is held: "1.2 GiB free on /path, below the 5.0 GiB floor".
    public static func describe(_ r: Reading) -> String {
        "\(size(r.free)) free on \(r.path), below the \(size(r.floor)) floor"
    }

    /// GiB to one decimal, or whole MiB under one GiB. Python twin: `diskfloor.size`.
    public static func size(_ bytes: Int64) -> String {
        if bytes >= 1 << 30 {
            return String(format: "%.1f GiB", Double(bytes) / Double(1 << 30))
        }
        return "\(bytes >> 20) MiB"
    }

    private static func statFree(_ path: String) -> Int64? {
        #if canImport(Darwin)
        var s = statfs()
        guard statfs(path, &s) == 0 else { return nil }
        return Int64(s.f_bavail) * Int64(s.f_bsize)
        #else
        var s = statvfs()
        guard statvfs(path, &s) == 0 else { return nil }
        return Int64(s.f_bavail) * Int64(s.f_frsize)
        #endif
    }
}
