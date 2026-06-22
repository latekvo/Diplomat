// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "ArgentUtils",
    platforms: [.macOS(.v13)],
    targets: [
        // Platform-agnostic, Foundation-only shared core. Loads the language-neutral
        // assets in core/ (GraphQL queries, tool catalog, filter constants, review
        // prompt fragments) — the single source of truth shared with the Linux
        // (Qt6/PySide6) front-end. Compiles on macOS *and* Linux.
        .target(
            name: "ArgentUtilsCore",
            path: "Sources/ArgentUtilsCore"
        ),
        // The macOS SwiftUI menu-bar app — a thin UI renderer over the core.
        .executableTarget(
            name: "ArgentUtils",
            dependencies: ["ArgentUtilsCore"],
            path: "Sources/ArgentUtils"
        ),
        // Linux-verifiable smoke test for the core (filters + prompt + asset load).
        .executableTarget(
            name: "ArgentUtilsCoreSmoke",
            dependencies: ["ArgentUtilsCore"],
            path: "Sources/ArgentUtilsCoreSmoke"
        ),
    ]
)
