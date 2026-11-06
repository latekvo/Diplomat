import Foundation

/// Owner-only files guarded by an exclusive `flock`: the primitives Comms' and Tasks'
/// logs share. Directories 0700, files 0600.
enum PrivateFiles {
    /// Made 0700 even if it already exists: what is in it may be the operator's DMs, and
    /// their home may be readable by other local users.
    static func prepare(_ dir: URL) {
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true,
                                                 attributes: [.posixPermissions: 0o700])
        chmod(dir.path, 0o700)
    }

    /// `body`'s answer, run while holding `lock` (a 0600 file, made if missing); nil when
    /// the lock cannot be taken.
    static func locked<T>(_ lock: URL, _ body: () throws -> T) rethrows -> T? {
        let fd = open(lock.path, O_RDWR | O_CREAT, 0o600)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        guard flock(fd, LOCK_EX) == 0 else { return nil }
        return try body()
    }

    /// `O_APPEND`, so another writer's line is never overwritten; `write(2)`, so a full
    /// disk costs the lines instead of aborting the process. Past `maxBytes` the file
    /// first keeps its newest half, cut at a line boundary. The caller holds the file's
    /// lock: a line appended mid-rotation would land in the replaced file. False when
    /// `data` is not all on disk.
    static func append(_ data: Data, to url: URL, maxBytes: Int) -> Bool {
        rotateIfLarge(url, maxBytes: maxBytes)
        let fd = open(url.path, O_WRONLY | O_APPEND | O_CREAT, 0o600)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        fchmod(fd, 0o600)
        return data.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) } == data.count
    }

    /// Whole or not at all, owner-only: a 0600 temp file renamed over `url`.
    @discardableResult
    static func writeWhole(_ data: Data, to url: URL) -> Bool {
        let tmp = url.path + ".tmp-\(getpid())-\(UUID().uuidString)"
        let fd = open(tmp, O_WRONLY | O_CREAT | O_EXCL, 0o600)
        guard fd >= 0 else { return false }
        let wrote = data.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
        close(fd)
        guard wrote == data.count, rename(tmp, url.path) == 0 else {
            unlink(tmp)
            return false
        }
        return true
    }

    private static func rotateIfLarge(_ url: URL, maxBytes: Int) {
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: url.path),
              let size = attrs[.size] as? Int, size > maxBytes,
              let data = try? Data(contentsOf: url) else { return }
        let mid = data.index(data.startIndex, offsetBy: data.count / 2)
        guard let nl = data[mid...].firstIndex(of: UInt8(ascii: "\n")) else { return }
        writeWhole(Data(data[data.index(after: nl)...]), to: url)
    }
}
