// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "Diplomat",
    platforms: [.macOS(.v13)],
    dependencies: [
        // The shared core, from its own package in this monorepo.
        .package(path: "../../diplomat-core"),
    ],
    targets: [
        // The macOS SwiftUI menu-bar app — a thin UI renderer over the core.
        .executableTarget(
            name: "Diplomat",
            dependencies: [.product(name: "DiplomatCore", package: "diplomat-core")],
            path: "Sources/Diplomat"
        ),
    ]
)
