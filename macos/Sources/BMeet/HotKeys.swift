// Global shortcuts: what KDE's shortcuts (Meta+H, Meta+Alt+M, ...) do on Linux.
//
// Carbon's RegisterEventHotKey: the shortcut is BMeet's alone while it runs, and it needs no
// permission (unlike watching every key, which needs Input Monitoring). The defaults put
// Linux's Meta on ⌃⌥, which apps rarely use:
//   dictate              ⌃⌥H     (Meta+H)
//   dictate-clipboard    ⌃⌥⌘H    (Meta+Alt+H)
//   language             ⌃⌥⇧H    (Meta+Shift+H)
//   meeting              ⌃⌥M     (Meta+Alt+M)
//   meeting-in-person    ⌃⌥⇧M    (Meta+Alt+Shift+M)
// ~/.config/bmeet/hotkeys changes them, one per line: `dictate = ctrl+option+h`
// (modifiers ctrl, option/alt, cmd, shift; a letter, digit, space, or f1..f20; "none" turns
// one off). Settings → Shortcuts writes it. BMeet registers them again whenever the file or a
// feature's switch changes (the shortcuts of a feature that is off are not registered).

import AppKit
import Carbon
import Foundation

final class HotKeys {
    static let defaults: [(String, String)] = [
        ("dictate", "ctrl+option+h"), ("dictate-clipboard", "ctrl+option+cmd+h"),
        ("language", "ctrl+option+shift+h"), ("meeting", "ctrl+option+m"),
        ("meeting-in-person", "ctrl+option+shift+m"),
    ]
    private var refs: [EventHotKeyRef] = []
    private var actions: [UInt32: String] = [:]
    private var handler: EventHandlerRef?
    private var nextID: UInt32 = 0
    private let onPress: (String) -> Void
    /// A check: the shortcuts are worked out but not registered (the running BMeet has them).
    var dryRun = false
    private(set) var labels: [String: String] = [:]     // action -> "⌃⌥H", for the ones registered
    private(set) var menuKeys: [String: (key: String, mods: NSEvent.ModifierFlags)] = [:]   // for menu items
    /// action -> its label, for the ones another app has taken.
    private(set) var taken: [String: String] = [:]

    init(onPress: @escaping (String) -> Void) { self.onPress = onPress }

    static var file: URL { Settings.file("hotkeys") }

    /// Every action with its combo ("none": off), as the file sets them.
    static func configured() -> [(String, String)] {
        var map = Dictionary(uniqueKeysWithValues: defaults)
        if let text = try? String(contentsOf: file, encoding: .utf8) {
            for line in text.split(separator: "\n") {
                let parts = line.split(separator: "=", maxSplits: 1).map { $0.trimmingCharacters(in: .whitespaces) }
                if parts.count == 2, !parts[0].hasPrefix("#"), map[parts[0]] != nil { map[parts[0]] = parts[1].lowercased() }
            }
        }
        return defaults.map { ($0.0, map[$0.0]!) }
    }

    /// The shortcuts to register now: the configured ones that are not off, of features that are on.
    static func plan() -> [(String, String)] {
        configured().filter { action, combo in combo != "none" && Feature.of(hotKey: action)?.isOn != false }
    }

    /// Writes the file the way configured() reads it: the shortcuts that are not the defaults,
    /// so a default that changes in a later version still reaches the others.
    static func write(_ combos: [String: String]) {
        var lines = ["# BMeet's shortcuts (Settings → Shortcuts writes this file): action = keys, or none.",
                     "# Modifiers ctrl, option, cmd, shift; a letter, digit, space or f1..f20."]
        for (action, standard) in defaults {
            if let combo = combos[action], combo != standard { lines.append("\(action) = \(combo)") }
        }
        Paths.write(lines.joined(separator: "\n"), to: file)
    }

    /// Registers the plan, letting go of what was registered before.
    func register() {
        unregister()
        if handler == nil && !dryRun {
            var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
            let me = Unmanaged.passUnretained(self).toOpaque()
            InstallEventHandler(GetApplicationEventTarget(), { _, event, user in
                var id = EventHotKeyID()
                GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID), nil,
                                  MemoryLayout<EventHotKeyID>.size, nil, &id)
                let me = Unmanaged<HotKeys>.fromOpaque(user!).takeUnretainedValue()
                if let action = me.actions[id.id] { DispatchQueue.main.async { me.onPress(action) } }
                return noErr
            }, 1, &spec, me, &handler)
        }
        for (action, combo) in HotKeys.plan() {
            guard let (key, mods, label) = HotKeys.parse(combo) else {
                NSLogLine("hotkeys: cannot read \(action) = \(combo)")
                continue
            }
            var ok = dryRun
            if !dryRun {
                nextID += 1
                var ref: EventHotKeyRef?
                let status = RegisterEventHotKey(key, mods, EventHotKeyID(signature: OSType(0x424D_4554), id: nextID),
                                                 GetApplicationEventTarget(), 0, &ref)
                if status == noErr, let ref {
                    refs.append(ref)
                    actions[nextID] = action
                    ok = true
                } else {
                    NSLogLine("hotkeys: \(label) for \(action) is taken by another app (\(status))")
                }
            }
            if ok {
                labels[action] = label
                if let m = HotKeys.menuKey(combo) { menuKeys[action] = m }
            } else {
                taken[action] = label
            }
        }
    }

    /// Lets go of every shortcut (while Settings → Shortcuts records one, and before registering again).
    func unregister() {
        for ref in refs { UnregisterEventHotKey(ref) }
        refs = []
        actions = [:]
        labels = [:]
        menuKeys = [:]
        taken = [:]
    }

    /// How many shortcuts are registered with macOS now (a check).
    var registeredCount: Int { refs.count }

    /// The combo as a menu item's key equivalent (shown in the menu the Mac way: ⌃⌥H).
    static func menuKey(_ combo: String) -> (key: String, mods: NSEvent.ModifierFlags)? {
        var mods: NSEvent.ModifierFlags = []
        var key = ""
        for part in combo.split(separator: "+").map(String.init) {
            switch part {
            case "ctrl", "control": mods.insert(.control)
            case "option", "alt", "opt": mods.insert(.option)
            case "cmd", "command": mods.insert(.command)
            case "shift": mods.insert(.shift)
            case "space": key = " "
            default:
                if part.count == 1 { key = part }
                else if part.hasPrefix("f"), let n = Int(part.dropFirst()), (1...20).contains(n) {
                    key = String(Character(UnicodeScalar(NSF1FunctionKey + n - 1)!))
                } else { return nil }
            }
        }
        return key.isEmpty ? nil : (key, mods)
    }

    static func parse(_ combo: String) -> (UInt32, UInt32, String)? {
        var mods: UInt32 = 0
        var label = ""
        var key: UInt32?
        var keyLabel = ""
        for part in combo.split(separator: "+").map(String.init) {
            switch part {
            case "ctrl", "control": mods |= UInt32(controlKey)
            case "option", "alt", "opt": mods |= UInt32(optionKey)
            case "cmd", "command": mods |= UInt32(cmdKey)
            case "shift": mods |= UInt32(shiftKey)
            default:
                guard let k = keyCodes[part] else { return nil }
                key = k
                keyLabel = part.count == 1 ? part.uppercased() : part.capitalized
            }
        }
        guard let key else { return nil }
        if mods & UInt32(controlKey) != 0 { label += "⌃" }
        if mods & UInt32(optionKey) != 0 { label += "⌥" }
        if mods & UInt32(shiftKey) != 0 { label += "⇧" }
        if mods & UInt32(cmdKey) != 0 { label += "⌘" }
        return (key, mods, label + keyLabel)
    }

    /// The label of a combo ("ctrl+option+h" → "⌃⌥H"), or the combo itself if it cannot be read.
    static func label(_ combo: String) -> String { parse(combo)?.2 ?? combo }

    /// A key pressed in Settings → Shortcuts as a combo for the file, or nil when it cannot be a
    /// shortcut: a key BMeet has no name for, or a key without ⌃, ⌥ or ⌘ (but an F key).
    static func combo(keyCode: UInt16, modifiers: NSEvent.ModifierFlags) -> String? {
        guard let name = keyCodes.first(where: { $0.value == UInt32(keyCode) })?.key, name != "escape" else { return nil }
        let flags = modifiers.intersection([.control, .option, .command, .shift])
        guard name.hasPrefix("f") && name.count > 1 || !flags.intersection([.control, .option, .command]).isEmpty else { return nil }
        var parts: [String] = []
        if flags.contains(.control) { parts.append("ctrl") }
        if flags.contains(.option) { parts.append("option") }
        if flags.contains(.command) { parts.append("cmd") }
        if flags.contains(.shift) { parts.append("shift") }
        return (parts + [name]).joined(separator: "+")
    }

    static let keyCodes: [String: UInt32] = {
        var m: [String: UInt32] = [
            "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05, "z": 0x06, "x": 0x07, "c": 0x08,
            "v": 0x09, "b": 0x0B, "q": 0x0C, "w": 0x0D, "e": 0x0E, "r": 0x0F, "y": 0x10, "t": 0x11, "1": 0x12,
            "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17, "9": 0x19, "7": 0x1A, "8": 0x1C, "0": 0x1D,
            "o": 0x1F, "u": 0x20, "i": 0x22, "p": 0x23, "l": 0x25, "j": 0x26, "k": 0x28, "n": 0x2D, "m": 0x2E,
            "space": 0x31, "escape": 0x35,
        ]
        let f: [UInt32] = [0x7A, 0x78, 0x63, 0x76, 0x60, 0x61, 0x62, 0x64, 0x65, 0x6D, 0x67, 0x6F,
                           0x69, 0x6B, 0x71, 0x6A, 0x40, 0x4F, 0x50, 0x5A]
        for (i, code) in f.enumerated() { m["f\(i + 1)"] = code }
        return m
    }()
}

/// Turns off writing BMeet.log (a check: its messages go to stderr only).
var logToFile = true

/// BMeet.app's own messages: stderr, and ~/Library/Logs/BMeet/BMeet.log (an app started from the
/// Finder or at login has no terminal).
func NSLogLine(_ msg: String) {
    let line = "BMeet: \(msg)\n"
    FileHandle.standardError.write(line.data(using: .utf8)!)
    guard logToFile else { return }
    let url = Paths.logs.appendingPathComponent("BMeet.log")
    try? FileManager.default.createDirectory(at: Paths.logs, withIntermediateDirectories: true)
    if !FileManager.default.fileExists(atPath: url.path) { FileManager.default.createFile(atPath: url.path, contents: nil) }
    if let h = try? FileHandle(forWritingTo: url) {
        h.seekToEndOfFile()
        let stamp = ISO8601DateFormatter().string(from: Date())
        h.write("\(stamp) \(line)".data(using: .utf8)!)
        try? h.close()
    }
}
