// Where BMeet's parts are, as macos/install.sh puts them, and its settings files.

import Foundation

enum Paths {
    static let env = ProcessInfo.processInfo.environment
    static let home = FileManager.default.homeDirectoryForCurrentUser

    static var data: URL { env["XDG_DATA_HOME"].map { URL(fileURLWithPath: $0) } ?? home.appendingPathComponent(".local/share") }
    static var config: URL {
        (env["XDG_CONFIG_HOME"].map { URL(fileURLWithPath: $0) } ?? home.appendingPathComponent(".config"))
            .appendingPathComponent("bmeet")
    }

    /// BMeet's program files: $BMEET_SRC, else the install (~/.local/share/bmeet/src).
    static var src: URL { env["BMEET_SRC"].map { URL(fileURLWithPath: $0) } ?? data.appendingPathComponent("bmeet/src") }
    /// The venv's Python (numpy, Core ML, PyQt6).
    static var python: URL { env["BMEET_PYTHON"].map { URL(fileURLWithPath: $0) } ?? data.appendingPathComponent("bmeet/venv/bin/python") }

    static var ctl: URL { src.appendingPathComponent("bmeet/bmeet-ctl") }
    static var meeting: URL { src.appendingPathComponent("macos/bmeet-meeting") }
    static var transcribe: URL { src.appendingPathComponent("transcribe/npu-transcribe") }
    static var windowApp: URL {
        Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/BMeet Window.app")
    }

    /// $XDG_RUNTIME_DIR/bmeet as BMeet's Python programs see it: your temp folder (macos/bmeetmac.py),
    /// or $BMEET_RUNTIME_ROOT/bmeet (tests). Not $XDG_RUNTIME_DIR: a shell may export one, BMeet.app has none.
    static var runtime: URL {
        var root = env["BMEET_RUNTIME_ROOT"] ?? ""
        if root.isEmpty {
            var buf = [CChar](repeating: 0, count: Int(PATH_MAX))
            let n = confstr(_CS_DARWIN_USER_TEMP_DIR, &buf, buf.count)
            root = n > 0 ? String(cString: buf) : NSTemporaryDirectory()
        }
        return URL(fileURLWithPath: root).appendingPathComponent("bmeet")
    }

    static var logs: URL { home.appendingPathComponent("Library/Logs/BMeet") }

    static func read(_ url: URL, default value: String = "") -> String {
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return value }
        let first = text.split(separator: "\n").first.map { $0.trimmingCharacters(in: .whitespaces) } ?? ""
        return first.isEmpty ? value : first
    }

    static func write(_ text: String, to url: URL) {
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? (text + "\n").write(to: url, atomically: true, encoding: .utf8)
    }
}

/// The settings BMeet keeps in ~/.config/bmeet (one word per file, as on Linux).
enum Settings {
    static var language: String { Paths.read(Paths.config.appendingPathComponent("language"), default: "en") }
    static var languages: [String] {
        let words = Paths.read(Paths.config.appendingPathComponent("languages"), default: "en")
            .replacingOccurrences(of: ",", with: " ").split(separator: " ").map { $0.lowercased() }
        var seen = Set<String>()
        return words.filter { $0.range(of: "^[a-z]{2,3}$", options: .regularExpression) != nil && seen.insert($0).inserted }
    }
    /// Text while you speak: "on" (the default on a Mac: a panel shows it) or "off".
    static var liveText: Bool { Paths.read(Paths.config.appendingPathComponent("live-dictation"), default: "on") != "off" }
    static var speakers: Bool { Paths.read(Paths.config.appendingPathComponent("speakers"), default: "on") == "on" }
    static var startAtLogin: Bool { Paths.read(Paths.config.appendingPathComponent("start-at-login"), default: "on") == "on" }

    static func set(_ name: String, _ value: String) { Paths.write(value, to: Paths.config.appendingPathComponent(name)) }
}
