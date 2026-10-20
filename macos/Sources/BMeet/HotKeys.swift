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
// (modifiers ctrl, option/alt, cmd, shift; a letter, digit, space, or f1..f12; "none" turns
// one off). BMeet reads it when it starts.

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
    private let onPress: (String) -> Void
    private(set) var labels: [String: String] = [:]     // action -> "⌃⌥H"
    private(set) var menuKeys: [String: (key: String, mods: NSEvent.ModifierFlags)] = [:]   // for menu items

    init(onPress: @escaping (String) -> Void) { self.onPress = onPress }

    static func configured() -> [(String, String)] {
        var map = Dictionary(uniqueKeysWithValues: defaults)
        let file = Paths.config.appendingPathComponent("hotkeys")
        if let text = try? String(contentsOf: file, encoding: .utf8) {
            for line in text.split(separator: "\n") {
                let parts = line.split(separator: "=", maxSplits: 1).map { $0.trimmingCharacters(in: .whitespaces) }
                if parts.count == 2, !parts[0].hasPrefix("#"), map[parts[0]] != nil { map[parts[0]] = parts[1].lowercased() }
            }
        }
        return defaults.map { ($0.0, map[$0.0]!) }
    }

    func register() {
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
        var n: UInt32 = 0
        for (action, combo) in HotKeys.configured() {
            guard combo != "none", let (key, mods, label) = HotKeys.parse(combo) else {
                if combo != "none" { NSLogLine("hotkeys: cannot read \(action) = \(combo)") }
                continue
            }
            n += 1
            var ref: EventHotKeyRef?
            let status = RegisterEventHotKey(key, mods, EventHotKeyID(signature: OSType(0x424D_4554), id: n),
                                             GetApplicationEventTarget(), 0, &ref)
            if status == noErr, let ref {
                refs.append(ref)
                actions[n] = action
                labels[action] = label
                if let m = HotKeys.menuKey(combo) { menuKeys[action] = m }
            } else {
                NSLogLine("hotkeys: \(label) for \(action) is taken by another app (\(status))")
            }
        }
    }

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
                else if part.hasPrefix("f"), let n = Int(part.dropFirst()), (1...12).contains(n) {
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

    static let keyCodes: [String: UInt32] = {
        var m: [String: UInt32] = [
            "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05, "z": 0x06, "x": 0x07, "c": 0x08,
            "v": 0x09, "b": 0x0B, "q": 0x0C, "w": 0x0D, "e": 0x0E, "r": 0x0F, "y": 0x10, "t": 0x11, "1": 0x12,
            "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17, "9": 0x19, "7": 0x1A, "8": 0x1C, "0": 0x1D,
            "o": 0x1F, "u": 0x20, "i": 0x22, "p": 0x23, "l": 0x25, "j": 0x26, "k": 0x28, "n": 0x2D, "m": 0x2E,
            "space": 0x31, "escape": 0x35,
        ]
        let f: [UInt32] = [0x7A, 0x78, 0x63, 0x76, 0x60, 0x61, 0x62, 0x64, 0x65, 0x6D, 0x67, 0x6F]
        for (i, code) in f.enumerated() { m["f\(i + 1)"] = code }
        return m
    }()
}

/// BMeet.app's own messages: stderr, and ~/Library/Logs/BMeet/BMeet.log (an app started from the
/// Finder or at login has no terminal).
func NSLogLine(_ msg: String) {
    let line = "BMeet: \(msg)\n"
    FileHandle.standardError.write(line.data(using: .utf8)!)
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
