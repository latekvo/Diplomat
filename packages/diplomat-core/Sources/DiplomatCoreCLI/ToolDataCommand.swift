import DiplomatCore
import Foundation

/// `diplomat-core tool-data` — run the six tool lists over a fixture and print the
/// rows as JSON.
///
/// This exists for one reason: `ToolData.items` (here) and `Store.items_for` (the
/// Linux applet) are two implementations of the same six lists, down to the exact
/// text of every row. Unlike prompt assembly, neither can delegate to the other —
/// the lists are rebuilt on every render, so a shell-out per render is not an
/// option — so the only thing standing between them and silent drift is a test that
/// runs both and diffs the output. That test needs a way to ask this side for its
/// answer; this is it.
///
/// Input (all dates ISO-8601 with a timezone):
/// ```
/// { "me": "octocat", "prs": [ … ], "issues": [ … ] }
/// ```
/// Output: `{ "<toolKind>": [ {id, badge, title, url, line2, line3}, … ], … }`,
/// serialised with sorted keys so the two sides can be compared byte-for-byte.
enum ToolDataCommand {
    static func run(_ obj: [String: Any]) {
        let me = obj["me"] as? String ?? ""
        let prs = (obj["prs"] as? [[String: Any]] ?? []).map(decodePR)
        let issues = (obj["issues"] as? [[String: Any]] ?? []).map(decodeIssue)

        var out: [String: Any] = [:]
        for kind in ToolKind.allCases {
            out[kind.rawValue] = ToolData.items(for: kind, prs: prs, issues: issues, me: me)
                .map { item -> [String: Any] in
                    [
                        "id": item.id,
                        "badge": item.badge,
                        "title": item.title,
                        "url": item.url,
                        "line2": item.line2,
                        "line3": item.line3 ?? NSNull(),
                    ]
                }
        }
        guard let data = try? JSONSerialization.data(
            withJSONObject: out, options: [.sortedKeys, .prettyPrinted]) else {
            die("could not serialise tool data", 1)
        }
        FileHandle.standardOutput.write(data)
    }

    /// ISO-8601 with fractional seconds optional — what both front-ends get from the
    /// GitHub API and what the fixture writes.
    private static func date(_ raw: Any?) -> Date {
        guard let s = raw as? String else { return Date(timeIntervalSince1970: 0) }
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = withFraction.date(from: s) { return d }
        return ISO8601DateFormatter().date(from: s) ?? Date(timeIntervalSince1970: 0)
    }

    private static func optionalDate(_ raw: Any?) -> Date? {
        guard raw != nil, !(raw is NSNull) else { return nil }
        return date(raw)
    }

    private static func decodePR(_ d: [String: Any]) -> OpenPR {
        OpenPR(
            number: d["number"] as? Int ?? 0,
            title: d["title"] as? String ?? "",
            url: d["url"] as? String ?? "",
            isDraft: JSONInput.flag(d["isDraft"]),
            author: d["author"] as? String ?? "",
            createdAt: date(d["createdAt"]),
            readyForReviewAt: optionalDate(d["readyForReviewAt"]),
            files: d["files"] as? [String] ?? [],
            reviewDecision: d["reviewDecision"] as? String,
            mergeable: d["mergeable"] as? String ?? "UNKNOWN",
            reviewThreads: (d["reviewThreads"] as? [[String: Any]] ?? []).map {
                ReviewThread(isResolved: JSONInput.flag($0["isResolved"]),
                             viewerCanResolve: JSONInput.flag($0["viewerCanResolve"], true),
                             lastCommentAuthor: $0["lastCommentAuthor"] as? String)
            })
    }

    private static func decodeIssue(_ d: [String: Any]) -> OpenIssue {
        OpenIssue(
            number: d["number"] as? Int ?? 0,
            title: d["title"] as? String ?? "",
            url: d["url"] as? String ?? "",
            author: d["author"] as? String ?? "",
            authorAssociation: d["authorAssociation"] as? String ?? "",
            createdAt: date(d["createdAt"]),
            updatedAt: date(d["updatedAt"]),
            commentCount: d["commentCount"] as? Int ?? 0,
            assignees: d["assignees"] as? [String] ?? [],
            labels: d["labels"] as? [String] ?? [],
            memberResponded: JSONInput.flag(d["memberResponded"]))
    }
}
