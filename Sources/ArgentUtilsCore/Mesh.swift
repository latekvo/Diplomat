import Foundation

/// Shared, platform-neutral model for **Argent Mesh** — the LAN P2P duty-coordination
/// layer. This is the Swift half of the "two front-ends, one brain" split: it decodes
/// the shared `core/mesh.json` (duty catalog, placement strategies, tier/token model)
/// and the local node's public topology snapshot (`~/.argent/mesh/state.json`), exactly
/// as the Python front-end's `argent_utils.mesh.config` / `.statefile` do.
///
/// Nothing here opens a socket or spawns a node — it's pure decode + placement logic,
/// so it builds and is unit-testable on Linux alongside the rest of the core. The
/// macOS UI layers a SwiftUI screen (`MeshView`) and a control client (`MeshBridge`)
/// on top; the Linux front-end renders the same model in Qt.

// MARK: - The shared model (core/mesh.json)

/// The subset of `core/mesh.json` the UIs render: the duty catalog, placement
/// strategies, and the platform / token / tier vocabularies. Protocol constants and
/// account/accounting knobs are the node's concern and deliberately not decoded here.
public struct MeshCatalog: Decodable, Equatable {
    public struct Platform: Decodable, Equatable {
        public let id: String
        public let title: String
        public let emoji: String
        public let colorHex: String
    }
    public struct Token: Decodable, Equatable {
        public let id: String
        public let title: String
        public let emoji: String
        public let colorHex: String
    }
    public struct Tiers: Decodable, Equatable {
        public let min: Int
        public let max: Int
        /// `default` is a Swift keyword — mapped from the JSON key.
        public let defaultTier: Int
        /// Human words per strength level ("1" → "Very strong", …), optional.
        public let labels: [String: String]?
        enum CodingKeys: String, CodingKey { case min, max, defaultTier = "default", labels }
    }
    /// A trust classification (personal / foreign) — the toggle vocabulary.
    public struct TrustLevel: Decodable, Equatable {
        public let id: String
        public let title: String
        public let linuxGlyph: String?
        public let colorHex: String
    }
    public struct Trust: Decodable, Equatable {
        public let levels: [TrustLevel]
    }
    public struct Strategy: Decodable, Equatable {
        public let id: String
        public let title: String
        public let detail: String
    }
    public struct Duty: Decodable, Equatable {
        public let id: String
        public let title: String
        public let emoji: String
        public let colorHex: String
        public let placement: PlacementDTO
    }
    /// The raw placement policy as it appears in the JSON (all fields optional, so a
    /// gossiped override that only carries a strategy still decodes). Resolved into a
    /// concrete `MeshPlacement` via `MeshPlacement.from`.
    public struct PlacementDTO: Decodable, Equatable {
        public let strategy: String?
        public let tokenAware: Bool?
        public let spread: [SpreadDTO]?
    }
    public struct SpreadDTO: Decodable, Equatable {
        public let platform: String
        public let count: Int?
    }

    public let platforms: [Platform]
    public let tokens: [Token]
    public let tiers: Tiers
    public let trust: Trust?
    public let strategies: [Strategy]
    public let duties: [Duty]
    public let defaultStrategy: String

    /// (min, max, default) machine tier, mirroring `config.tier_bounds()`.
    public var tierBounds: (min: Int, max: Int, default: Int) {
        (tiers.min, tiers.max, tiers.defaultTier)
    }

    /// Human word for a strength tier ("Very strong" … "Very light"), mirroring
    /// `config.tier_label`; falls back to `tier N` if unlabelled.
    public func tierLabel(_ tier: Int) -> String {
        tiers.labels?[String(tier)] ?? "tier \(tier)"
    }

    public func platform(_ id: String) -> Platform? { platforms.first { $0.id == id } }
    public func token(_ id: String) -> Token? { tokens.first { $0.id == id } }
    public func duty(_ id: String) -> Duty? { duties.first { $0.id == id } }
    public func trustLevel(_ id: String) -> TrustLevel? { trust?.levels.first { $0.id == id } }

    /// The effective placement for a duty: the gossiped override if present, else the
    /// `core/mesh.json` default. Mirrors `config.placement_for`.
    public func placement(for dutyID: String, overrides: MeshOverrides?) -> MeshPlacement {
        if let dto = overrides?.duties[dutyID] {
            return MeshPlacement.from(dto, defaultStrategy: defaultStrategy)
        }
        if let duty = duty(dutyID) {
            return MeshPlacement.from(duty.placement, defaultStrategy: defaultStrategy)
        }
        return MeshPlacement(strategy: defaultStrategy, tokenAware: true, spread: [])
    }
}

// MARK: - Placement (per-duty policy)

public struct MeshSpread: Equatable {
    public let platform: String
    public let count: Int
    public init(platform: String, count: Int) {
        self.platform = platform
        self.count = count
    }
}

/// The resolved placement policy for one duty, mirroring `config.Placement`.
public struct MeshPlacement: Equatable {
    public var strategy: String
    public var tokenAware: Bool
    /// [(platform, count)] the duty must cover; empty = any one node.
    public var spread: [MeshSpread]

    public init(strategy: String, tokenAware: Bool, spread: [MeshSpread]) {
        self.strategy = strategy
        self.tokenAware = tokenAware
        self.spread = spread
    }

    public static func from(_ dto: MeshCatalog.PlacementDTO, defaultStrategy: String) -> MeshPlacement {
        MeshPlacement(
            strategy: dto.strategy ?? defaultStrategy,
            tokenAware: dto.tokenAware ?? true,
            spread: (dto.spread ?? []).map { MeshSpread(platform: $0.platform, count: $0.count ?? 1) }
        )
    }

    /// A JSON-object representation (for the control-socket `set-overrides` command),
    /// mirroring `config.Placement.to_dict`.
    public func jsonObject() -> [String: Any] {
        [
            "strategy": strategy,
            "tokenAware": tokenAware,
            "spread": spread.map { ["platform": $0.platform, "count": $0.count] },
        ]
    }
}

/// Mesh-wide placement edits, gossiped last-writer-wins — mirrors
/// `config.PlacementOverrides`. `duties` maps duty id → the raw placement policy.
public struct MeshOverrides: Decodable, Equatable {
    public let rev: Int
    public let updatedBy: String
    public let duties: [String: MeshCatalog.PlacementDTO]

    enum CodingKeys: String, CodingKey { case rev, updatedBy, duties }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        rev = (try? c.decode(Int.self, forKey: .rev)) ?? 0
        updatedBy = (try? c.decode(String.self, forKey: .updatedBy)) ?? ""
        duties = (try? c.decode([String: MeshCatalog.PlacementDTO].self, forKey: .duties)) ?? [:]
    }
}

// MARK: - Topology snapshot (~/.argent/mesh/state.json)

/// A node's advertised quota accounting (`NodeInfo.stats` in the Python node): the
/// plan it runs on and the derived load figures the `surplus-first` dispatch strategy
/// ranks by. Absent entirely for nodes that haven't recorded any usage.
public struct MeshStats: Decodable, Equatable {
    public let plan: String
    public let usageAvg: Double
    public let quotaLeft: Double

    enum CodingKeys: String, CodingKey { case plan, usageAvg, quotaLeft }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        plan = (try? c.decode(String.self, forKey: .plan)) ?? ""
        usageAvg = (try? c.decode(Double.self, forKey: .usageAvg)) ?? 0
        quotaLeft = (try? c.decode(Double.self, forKey: .quotaLeft)) ?? 0
    }
    public init(plan: String, usageAvg: Double, quotaLeft: Double) {
        self.plan = plan; self.usageAvg = usageAvg; self.quotaLeft = quotaLeft
    }
}

/// One node's gossiped attributes, common to `self` and each peer.
public struct MeshNode: Decodable, Equatable {
    public let id: String
    public let name: String
    public let platform: String
    public let tier: Int
    public let tokens: String
    /// Whether the tier was auto-detected from hardware (vs pinned by an edit).
    public let strengthAuto: Bool
    /// Whether the token state is auto-derived from real usage (vs a manual pin).
    public let tokensAuto: Bool
    /// Fraction of the token budget still remaining (1.0 = fresh, 0.0 = out) — the
    /// binding value: min(session, week) from the real probe, else the heuristic.
    public let tokensPct: Double
    /// Real remaining fraction of the 5-hour session window (OAuth usage probe);
    /// nil when the node is on the heuristic fallback (or an older build).
    public let tokensSessionPct: Double?
    /// Real remaining fraction of the 7-day week window; nil like `tokensSessionPct`.
    public let tokensWeekPct: Double?
    /// Seconds this node has been running (self view) — nil when absent.
    public let uptimeSecs: Double?
    /// This node's device-key fingerprint (sha256 of the raw Ed25519 pubkey, hex);
    /// `""` when the node runs keyless (`cryptography` not installed).
    public let fingerprint: String
    /// Advertised quota accounting; nil until the node has stats to gossip.
    public let stats: MeshStats?

    enum CodingKeys: String, CodingKey {
        case id, name, platform, tier, tokens, strengthAuto, tokensAuto, tokensPct,
             tokensSessionPct, tokensWeekPct, uptimeSecs, fingerprint, stats
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = (try? c.decode(String.self, forKey: .id)) ?? ""
        name = (try? c.decode(String.self, forKey: .name)) ?? "?"
        platform = (try? c.decode(String.self, forKey: .platform)) ?? "unknown"
        tier = (try? c.decode(Int.self, forKey: .tier)) ?? 3
        tokens = (try? c.decode(String.self, forKey: .tokens)) ?? "ok"
        strengthAuto = (try? c.decode(Bool.self, forKey: .strengthAuto)) ?? true
        tokensAuto = (try? c.decode(Bool.self, forKey: .tokensAuto)) ?? true
        tokensPct = (try? c.decode(Double.self, forKey: .tokensPct)) ?? 1.0
        tokensSessionPct = try? c.decode(Double.self, forKey: .tokensSessionPct)
        tokensWeekPct = try? c.decode(Double.self, forKey: .tokensWeekPct)
        uptimeSecs = try? c.decode(Double.self, forKey: .uptimeSecs)
        fingerprint = (try? c.decode(String.self, forKey: .fingerprint)) ?? ""
        stats = try? c.decode(MeshStats.self, forKey: .stats)
    }

    public init(id: String, name: String, platform: String, tier: Int, tokens: String,
                strengthAuto: Bool = true, tokensAuto: Bool = true, tokensPct: Double = 1.0,
                tokensSessionPct: Double? = nil, tokensWeekPct: Double? = nil,
                uptimeSecs: Double? = nil, fingerprint: String = "", stats: MeshStats? = nil) {
        self.id = id; self.name = name; self.platform = platform
        self.tier = tier; self.tokens = tokens
        self.strengthAuto = strengthAuto; self.tokensAuto = tokensAuto
        self.tokensPct = tokensPct
        self.tokensSessionPct = tokensSessionPct; self.tokensWeekPct = tokensWeekPct
        self.uptimeSecs = uptimeSecs; self.fingerprint = fingerprint
        self.stats = stats
    }

    /// `uptimeSecs` ticks, and `tokensPct`/`stats` drift with real usage, so all three
    /// are excluded from equality — otherwise the change-detecting poll (see `Store`)
    /// would fire twice a second on self's own uptime. The session/week percentages
    /// ARE compared: they move at most about once a minute (integer-percent probe)
    /// and the quota indicator must repaint when they do. Mirrors `MeshPeer.==`.
    public static func == (a: MeshNode, b: MeshNode) -> Bool {
        a.id == b.id && a.name == b.name && a.platform == b.platform && a.tier == b.tier
            && a.tokens == b.tokens && a.strengthAuto == b.strengthAuto
            && a.tokensAuto == b.tokensAuto && a.fingerprint == b.fingerprint
            && a.tokensSessionPct == b.tokensSessionPct && a.tokensWeekPct == b.tokensWeekPct
    }

    /// `quotaLeft − usageAvg`, the figure `surplus-first` ranks by; 0 (neutral) with
    /// no stats — mirrors `NodeInfo.surplus()`.
    public var surplus: Double { stats.map { $0.quotaLeft - $0.usageAvg } ?? 0 }
}

/// A peer as seen from the local node: its node attributes plus the link state, remote
/// address, liveness, and the trust the local node derived for it.
public struct MeshPeer: Decodable, Equatable {
    public let id: String
    public let name: String
    public let platform: String
    public let tier: Int
    public let tokens: String
    /// "up" | "stale" | "down".
    public let link: String
    public let addr: String?
    public let lastSeenSecsAgo: Double?
    /// Peer ids this peer reports it currently sees (drives the peer↔peer graph edges).
    public let sees: [String]
    public let strengthAuto: Bool
    public let tokensAuto: Bool
    public let tokensPct: Double
    /// Real remaining fractions per rate-limit window (see `MeshNode`); nil on the
    /// heuristic fallback or an older peer build.
    public let tokensSessionPct: Double?
    public let tokensWeekPct: Double?
    /// Seconds the current link has been up ("up 3m") — nil while down.
    public let uptimeSecs: Double?
    /// "personal" | "foreign" | "banned" — the local allowlist's (and ban list's)
    /// verdict on the VERIFIED key (an empty allowlist classes everyone personal,
    /// preserving pre-trust behavior; "banned" = the device accepted a
    /// SzpontRequest and failed to deliver it — see docs/szpontnet/13).
    public let trust: String
    /// The peer's device-key fingerprint (proven if `verified`, else merely claimed;
    /// `""` for keyless peers).
    public let fingerprint: String
    /// True once the peer has proven possession of its device key on this link
    /// (Ed25519 fresh-nonce signature) — advertised identity alone never sets this.
    public let verified: Bool
    /// The peer's advertised `quotaLeft − usageAvg` (3dp), used by `surplus-first`.
    public let surplus: Double
    /// Advertised quota accounting; nil until the peer gossips stats.
    public let stats: MeshStats?

    enum CodingKeys: String, CodingKey {
        case id, name, platform, tier, tokens, link, addr, lastSeenSecsAgo, sees,
             strengthAuto, tokensAuto, tokensPct, tokensSessionPct, tokensWeekPct,
             uptimeSecs, trust, fingerprint, verified, surplus, stats
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = (try? c.decode(String.self, forKey: .id)) ?? ""
        name = (try? c.decode(String.self, forKey: .name)) ?? "?"
        platform = (try? c.decode(String.self, forKey: .platform)) ?? "unknown"
        tier = (try? c.decode(Int.self, forKey: .tier)) ?? 3
        tokens = (try? c.decode(String.self, forKey: .tokens)) ?? "ok"
        link = (try? c.decode(String.self, forKey: .link)) ?? "down"
        addr = try? c.decode(String.self, forKey: .addr)
        lastSeenSecsAgo = try? c.decode(Double.self, forKey: .lastSeenSecsAgo)
        sees = (try? c.decode([String].self, forKey: .sees)) ?? []
        strengthAuto = (try? c.decode(Bool.self, forKey: .strengthAuto)) ?? true
        tokensAuto = (try? c.decode(Bool.self, forKey: .tokensAuto)) ?? true
        tokensPct = (try? c.decode(Double.self, forKey: .tokensPct)) ?? 1.0
        tokensSessionPct = try? c.decode(Double.self, forKey: .tokensSessionPct)
        tokensWeekPct = try? c.decode(Double.self, forKey: .tokensWeekPct)
        uptimeSecs = try? c.decode(Double.self, forKey: .uptimeSecs)
        trust = (try? c.decode(String.self, forKey: .trust)) ?? "personal"
        fingerprint = (try? c.decode(String.self, forKey: .fingerprint)) ?? ""
        verified = (try? c.decode(Bool.self, forKey: .verified)) ?? false
        surplus = (try? c.decode(Double.self, forKey: .surplus)) ?? 0
        stats = try? c.decode(MeshStats.self, forKey: .stats)
    }

    /// `lastSeenSecsAgo`/`uptimeSecs` tick on every snapshot write, so they're excluded
    /// from equality: a change-detecting poll (see `Store`) must not fire twice a second
    /// on an idle mesh. The genuine signals (`link`, `trust`, token state) are compared.
    /// Mirrors the Python front-end's `_mesh_meaningfully_changed` strip.
    public static func == (a: MeshPeer, b: MeshPeer) -> Bool {
        a.id == b.id && a.name == b.name && a.platform == b.platform && a.tier == b.tier
            && a.tokens == b.tokens && a.link == b.link && a.addr == b.addr && a.sees == b.sees
            && a.strengthAuto == b.strengthAuto && a.tokensAuto == b.tokensAuto
            && a.trust == b.trust && a.verified == b.verified && a.fingerprint == b.fingerprint
            && a.tokensSessionPct == b.tokensSessionPct && a.tokensWeekPct == b.tokensWeekPct
    }
}

/// One local-allowlist entry as published in the snapshot's `trusted` list — the
/// operator-managed device-key allowlist (`~/.argent/mesh/trusted.json`).
public struct MeshTrustedEntry: Decodable, Equatable {
    public let fingerprint: String
    public let label: String

    enum CodingKeys: String, CodingKey { case fingerprint, label }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        fingerprint = (try? c.decode(String.self, forKey: .fingerprint)) ?? ""
        label = (try? c.decode(String.self, forKey: .label)) ?? ""
    }
}

/// One ban-list entry as published in the snapshot's `banned` list — a device this
/// node marked as having accepted a SzpontRequest and failed to deliver it (or one
/// the operator banned manually). Machine-local (`~/.argent/mesh/banned.json`),
/// never gossiped; `fingerprint` is empty for a keyless device (then `node` is the
/// best-effort key). See docs/szpontnet/13-foreign-execution.md#the-ban.
public struct MeshBannedEntry: Decodable, Equatable {
    public let fingerprint: String
    public let node: String
    public let label: String
    public let reason: String
    public let bannedAt: Double

    enum CodingKeys: String, CodingKey { case fingerprint, node, label, reason, bannedAt }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        fingerprint = (try? c.decode(String.self, forKey: .fingerprint)) ?? ""
        node = (try? c.decode(String.self, forKey: .node)) ?? ""
        label = (try? c.decode(String.self, forKey: .label)) ?? ""
        reason = (try? c.decode(String.self, forKey: .reason)) ?? ""
        bannedAt = (try? c.decode(Double.self, forKey: .bannedAt)) ?? 0
    }
}

public struct MeshShortfall: Decodable, Equatable {
    public let missing: Int
    public let platform: String

    enum CodingKeys: String, CodingKey { case missing, platform }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        missing = (try? c.decode(Int.self, forKey: .missing)) ?? 1
        platform = (try? c.decode(String.self, forKey: .platform)) ?? "?"
    }
}

/// A duty's live routing: the node ids it's assigned to, and any platform slots it
/// can't currently fill.
public struct MeshAssignment: Decodable, Equatable {
    public let assigned: [String]
    public let shortfall: [MeshShortfall]

    enum CodingKeys: String, CodingKey { case assigned, shortfall }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        assigned = (try? c.decode([String].self, forKey: .assigned)) ?? []
        shortfall = (try? c.decode([MeshShortfall].self, forKey: .shortfall)) ?? []
    }
}

/// The public topology snapshot the node rewrites atomically every couple of seconds.
/// Mirrors the shape documented in `argent_utils.mesh.statefile`.
public struct MeshSnapshot: Decodable, Equatable {
    /// The node process pid, used for the liveness check.
    public let pid: Int?
    /// The local control endpoint (127.0.0.1:tcpPort) for edits/dispatch.
    public let tcpPort: Int?
    /// This machine's own node info (`self` in the JSON).
    public let selfNode: MeshNode?
    public let peers: [MeshPeer]
    /// The local device-key allowlist, as `[{fingerprint, label}]` — published so
    /// front-ends can render the trust boundary without touching `trusted.json`.
    public let trusted: [MeshTrustedEntry]
    /// The local ban list mirror — who this node marked banned (accepted a
    /// SzpontRequest, failed to deliver) and why. Empty when nobody is.
    public let banned: [MeshBannedEntry]
    public let assignments: [String: MeshAssignment]
    public let overrides: MeshOverrides?
    /// Peers mid-handshake right now — drives the "linking to N…" scanning banner.
    public let linking: Int
    /// True while EVERY beacon send fails: the node is undiscoverable (typically an
    /// OS privacy gate — macOS Local Network — denying LAN sends). Drives the loud
    /// "device is not discoverable" banner so the mesh doesn't just look empty.
    public let beaconBlocked: Bool

    enum CodingKeys: String, CodingKey {
        case pid, tcpPort, selfNode = "self", peers, trusted, banned, assignments, overrides,
             linking, beaconBlocked
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        pid = try? c.decode(Int.self, forKey: .pid)
        tcpPort = try? c.decode(Int.self, forKey: .tcpPort)
        selfNode = try? c.decode(MeshNode.self, forKey: .selfNode)
        peers = (try? c.decode([MeshPeer].self, forKey: .peers)) ?? []
        trusted = (try? c.decode([MeshTrustedEntry].self, forKey: .trusted)) ?? []
        banned = (try? c.decode([MeshBannedEntry].self, forKey: .banned)) ?? []
        assignments = (try? c.decode([String: MeshAssignment].self, forKey: .assignments)) ?? [:]
        overrides = try? c.decode(MeshOverrides.self, forKey: .overrides)
        linking = (try? c.decode(Int.self, forKey: .linking)) ?? 0
        beaconBlocked = (try? c.decode(Bool.self, forKey: .beaconBlocked)) ?? false
    }

    /// Decode a snapshot from raw JSON bytes; nil for garbage/absent, matching
    /// `statefile.read_state`.
    public static func decode(_ data: Data) -> MeshSnapshot? {
        try? JSONDecoder().decode(MeshSnapshot.self, from: data)
    }
}
