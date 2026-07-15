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
        enum CodingKeys: String, CodingKey { case min, max, defaultTier = "default" }
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
    public let strategies: [Strategy]
    public let duties: [Duty]
    public let defaultStrategy: String

    /// (min, max, default) machine tier, mirroring `config.tier_bounds()`.
    public var tierBounds: (min: Int, max: Int, default: Int) {
        (tiers.min, tiers.max, tiers.defaultTier)
    }

    public func platform(_ id: String) -> Platform? { platforms.first { $0.id == id } }
    public func token(_ id: String) -> Token? { tokens.first { $0.id == id } }
    public func duty(_ id: String) -> Duty? { duties.first { $0.id == id } }

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

/// One node's gossiped attributes, common to `self` and each peer.
public struct MeshNode: Decodable, Equatable {
    public let id: String
    public let name: String
    public let platform: String
    public let tier: Int
    public let tokens: String

    enum CodingKeys: String, CodingKey { case id, name, platform, tier, tokens }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = (try? c.decode(String.self, forKey: .id)) ?? ""
        name = (try? c.decode(String.self, forKey: .name)) ?? "?"
        platform = (try? c.decode(String.self, forKey: .platform)) ?? "unknown"
        tier = (try? c.decode(Int.self, forKey: .tier)) ?? 3
        tokens = (try? c.decode(String.self, forKey: .tokens)) ?? "ok"
    }

    public init(id: String, name: String, platform: String, tier: Int, tokens: String) {
        self.id = id; self.name = name; self.platform = platform
        self.tier = tier; self.tokens = tokens
    }
}

/// A peer as seen from the local node: its node attributes plus the link state, remote
/// address, and liveness the local node observes.
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

    enum CodingKeys: String, CodingKey {
        case id, name, platform, tier, tokens, link, addr, lastSeenSecsAgo, sees
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
    }

    /// `lastSeenSecsAgo` ticks on every snapshot write, so it's excluded from equality:
    /// a change-detecting poll (see `Store`) must not fire twice a second on an idle
    /// mesh. The genuine liveness signal (`link`) is compared. Mirrors the Python
    /// front-end's `_mesh_meaningfully_changed` strip.
    public static func == (a: MeshPeer, b: MeshPeer) -> Bool {
        a.id == b.id && a.name == b.name && a.platform == b.platform && a.tier == b.tier
            && a.tokens == b.tokens && a.link == b.link && a.addr == b.addr && a.sees == b.sees
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
    public let assignments: [String: MeshAssignment]
    public let overrides: MeshOverrides?

    enum CodingKeys: String, CodingKey {
        case pid, tcpPort, selfNode = "self", peers, assignments, overrides
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        pid = try? c.decode(Int.self, forKey: .pid)
        tcpPort = try? c.decode(Int.self, forKey: .tcpPort)
        selfNode = try? c.decode(MeshNode.self, forKey: .selfNode)
        peers = (try? c.decode([MeshPeer].self, forKey: .peers)) ?? []
        assignments = (try? c.decode([String: MeshAssignment].self, forKey: .assignments)) ?? [:]
        overrides = try? c.decode(MeshOverrides.self, forKey: .overrides)
    }

    /// Decode a snapshot from raw JSON bytes; nil for garbage/absent, matching
    /// `statefile.read_state`.
    public static func decode(_ data: Data) -> MeshSnapshot? {
        try? JSONDecoder().decode(MeshSnapshot.self, from: data)
    }
}
