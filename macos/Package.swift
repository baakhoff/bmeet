// swift-tools-version: 5.10
// BMeet's native macOS parts: the menu bar app (hotkeys, recorder, paste, process
// supervisor), the Whisper server on the Neural Engine (WhisperKit) and the recorder CLI.
// Built by macos/install.sh with the Command Line Tools: swift build -c release.
import PackageDescription

let package = Package(
    name: "bmeet-macos",
    platforms: [.macOS(.v14)],
    dependencies: [
        // WhisperKit (Argmax, MIT): Whisper as Core ML models on the Neural Engine.
        .package(url: "https://github.com/argmaxinc/argmax-oss-swift.git", exact: "1.1.1"),
    ],
    targets: [
        .executableTarget(
            name: "bmeet-whisper",
            dependencies: [.product(name: "WhisperKit", package: "argmax-oss-swift")]
        ),
        .executableTarget(name: "bmeet-record"),
        // BMeet.app's executable: the menu bar app (macos/install.sh puts it in the bundle)
        .executableTarget(name: "BMeet"),
    ]
)
