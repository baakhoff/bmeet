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
    /// bmeet-ctl's last update check (update.json) is in here.
    static var cache: URL {
        (env["XDG_CACHE_HOME"].map { URL(fileURLWithPath: $0) } ?? home.appendingPathComponent(".cache"))
            .appendingPathComponent("bmeet")
    }
    /// The models (macos/install.sh, npu-transcribe's MODELS_DIR).
    static var models: URL { env["NPU_MODELS_DIR"].map { URL(fileURLWithPath: $0) } ?? data.appendingPathComponent("npu-models") }
    /// The remembered voices (the window's VOICES_DIR).
    static var voices: URL { env["BMEET_VOICES_DIR"].map { URL(fileURLWithPath: $0) } ?? data.appendingPathComponent("bmeet/voices") }

    /// BMeet's program files: $BMEET_SRC, else the install (~/.local/share/bmeet/src).
    static var src: URL { env["BMEET_SRC"].map { URL(fileURLWithPath: $0) } ?? data.appendingPathComponent("bmeet/src") }
    /// The venv's Python (numpy, Core ML, PyQt6).
    static var python: URL {
        pythonForCheck ?? env["BMEET_PYTHON"].map { URL(fileURLWithPath: $0) } ?? data.appendingPathComponent("bmeet/venv/bin/python")
    }
    /// A check that is given no stand-in for Python runs none of BMeet's programs: /usr/bin/false.
    static var pythonForCheck: URL?

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

    /// ~/Library/Logs/BMeet ($BMEET_LOGS: a test's own folder).
    static var logs: URL { env["BMEET_LOGS"].map { URL(fileURLWithPath: $0) } ?? home.appendingPathComponent("Library/Logs/BMeet") }

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

/// The settings BMeet keeps in ~/.config/bmeet: one value per file, as on Linux, a missing file
/// meaning the default. Each is read when it is needed (the Settings window, the BMeet window and
/// bmeet-ctl write them; docs/macos.md lists them).
enum Settings {
    static func file(_ name: String) -> URL { Paths.config.appendingPathComponent(name) }
    static func word(_ name: String, default value: String) -> String { Paths.read(file(name), default: value) }

    static var language: String { word("language", default: "en") }
    static var languages: [String] {
        let words = word("languages", default: "en")
            .replacingOccurrences(of: ",", with: " ").split(separator: " ").map { $0.lowercased() }
        var seen = Set<String>()
        let codes = words.filter { $0.range(of: "^[a-z]{2,3}$", options: .regularExpression) != nil && seen.insert($0).inserted }
        return codes.isEmpty ? ["en"] : codes
    }
    /// Text while you speak: "on" (the default on a Mac: a panel shows it) or "off".
    static var liveText: Bool { word("live-dictation", default: "on") != "off" }
    static var speakers: Bool { word("speakers", default: "on") == "on" }
    static var startAtLogin: Bool { word("start-at-login", default: "on") == "on" }
    /// Look for a newer release once a day (bmeet-ctl update --check --auto): "on" (the default) or "off".
    static var updateCheck: Bool { word("update-check", default: "on") != "off" }
    /// Voice fingerprints of the people you named (off unless you turn them on; README, Privacy).
    static var rememberVoices: Bool { word("remember-voices", default: "off") == "on" }
    /// "system" (the default), "en" or "ru"; anything else counts as "system".
    static var interfaceLanguage: String {
        let value = word("interface-language", default: "system").lowercased()
        return Interface.languages.contains { $0.code == value } ? value : "system"
    }
    /// Minutes a process that waits for work keeps a model after its last use (0: none), 5 by default.
    static var unloadAfter: Int {
        guard let minutes = Int(word("unload-after", default: "5")), (0...60).contains(minutes) else { return 5 }
        return minutes
    }

    static func set(_ name: String, _ value: String) { Paths.write(value, to: file(name)) }

    /// The dictation languages that are on (never none), and the current one among them: it
    /// stays if it is still on, else the first one takes its place.
    static func setLanguages(_ codes: [String]) {
        var seen = Set<String>()
        let on = codes.filter { seen.insert($0).inserted }
        guard !on.isEmpty else { return }
        set("languages", on.joined(separator: " "))
        if !on.contains(language) { set("language", on[0]) }
    }

    /// Everything BMeet.app follows, read at once: compared with the last reading, it says what
    /// changed (AppDelegate.settingsChanged).
    struct Snapshot: Equatable {
        var features: [Feature: Bool] = [:]
        var hotkeys = ""
        var interfaceLanguage = ""
        var languages: [String] = []
        var language = ""
        var liveText = true, speakers = true, rememberVoices = false, startAtLogin = true, updateCheck = true
        var unloadAfter = 5

        static func read() -> Snapshot {
            var s = Snapshot()
            for f in Feature.allCases { s.features[f] = f.isOn }
            s.hotkeys = (try? String(contentsOf: file("hotkeys"), encoding: .utf8)) ?? ""
            s.interfaceLanguage = Settings.interfaceLanguage
            s.languages = Settings.languages
            s.language = Settings.language
            s.liveText = Settings.liveText
            s.speakers = Settings.speakers
            s.rememberVoices = Settings.rememberVoices
            s.startAtLogin = Settings.startAtLogin
            s.updateCheck = Settings.updateCheck
            s.unloadAfter = Settings.unloadAfter
            return s
        }
    }
}

/// What can be turned off in Settings → Features. A feature that is off is gone from BMeet.app:
/// its shortcuts, its menu items, its panel and notifications; the control socket refuses it.
enum Feature: String, CaseIterable {
    case dictation
    case meetings
    case inPersonMeetings = "in-person-meetings"
    case fileTranscription = "file-transcription"

    var isOn: Bool { Settings.word(rawValue, default: "on") != "off" }

    func set(_ on: Bool) { Settings.set(rawValue, on ? "on" : "off") }

    /// The shortcuts that belong to it (HotKeys.defaults).
    var hotKeys: [String] {
        switch self {
        case .dictation: return ["dictate", "dictate-clipboard", "language"]
        case .meetings: return ["meeting"]
        case .inPersonMeetings: return ["meeting-in-person"]
        case .fileTranscription: return []
        }
    }

    static func of(hotKey action: String) -> Feature? { allCases.first { $0.hotKeys.contains(action) } }
}
