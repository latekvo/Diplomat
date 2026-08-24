// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "diplomat-core",
    platforms: [.macOS(.v13)],
    products: [
        // The shared core, as consumed by the macOS front-end next door.
        .library(name: "DiplomatCore", targets: ["DiplomatCore"]),
        // The prompt CLI, under the name both front-ends invoke it by. The product
        // name (not the target name) is what SPM calls the binary, so
        // `$DIPLOMAT_CORE_BIN` keeps pointing at a file called `diplomat-core`.
        .executable(name: "diplomat-core", targets: ["DiplomatCoreCLI"]),
    ],
    targets: [
        // Platform-agnostic, Foundation-only shared core. Loads the language-neutral
        // files in assets/ (GraphQL queries, tool catalog, filter constants, review
        // prompt fragments) — the single source of truth shared with the Linux
        // (Qt6/PySide6) front-end. Compiles on macOS *and* Linux.
        .target(
            name: "DiplomatCore",
            path: "Sources/DiplomatCore"
        ),
        // Linux-verifiable smoke test for the core (filters + prompt + asset load).
        .executableTarget(
            name: "DiplomatCoreSmoke",
            dependencies: ["DiplomatCore"],
            path: "Sources/DiplomatCoreSmoke"
        ),
        // Thin CLI over the core so the Linux (Qt6) front-end can shell out for
        // prompt assembly instead of re-implementing it — a single source of truth
        // for the Review/Issues/Conflicts/Audit prompts. Foundation-only; builds on Linux.
        .executableTarget(
            name: "DiplomatCoreCLI",
            dependencies: ["DiplomatCore"],
            path: "Sources/DiplomatCoreCLI"
        ),
    ]
)
