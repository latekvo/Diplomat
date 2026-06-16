// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "ArgentUtils",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "ArgentUtils",
            path: "Sources/ArgentUtils"
        )
    ]
)
