// BMeet's check modes, run from the command line by macos/tests/test_settings.py. None shows
// anything: no menu bar icon, no window on the screen (the Settings window is built and drawn
// off screen), no dictation panel, no notification, no question from macOS. None touches the
// real settings: each needs BMEET_RUNTIME_ROOT and XDG_CONFIG_HOME (a test's own folders), and
// --control-check also BMEET_PYTHON (a stand-in for BMeet's Python, so nothing records). Only
// --hotkey-check registers shortcuts with macOS, and only ones nobody uses (⌃⌥⇧⌘ with F13–F20),
// for a moment: the running BMeet keeps its own.

import AppKit

enum Checks {
    static let usage = """
        usage: BMeet                                   the menu bar app
               BMeet --settings-check PAGE|all [--lang en|ru] [--picture DIR] [--appearance light|dark]
                     each page of the Settings window, off screen: its height, the texts cut short or
                     not translated, every text (JSON lines; exit 1 if a page does not fit)
               BMeet --menu-check [--lang en|ru] [--status FILE]
                     the menu's items and the shortcuts BMeet would register (JSON); FILE: a
                     `bmeet-ctl status` answer to show
               BMeet --settings-act ACT... [--lang en|ru]
                     works the Settings window's controls, then prints the settings files and the
                     shortcuts (JSON). ACT: page:P feature:F live speakers voices answer:yes|no
                     language:CODE dictation-language:CODE search:TEXT shortcut:ACTION=KEYS|delete|esc
                     restore-shortcuts interface:system|en|ru unload:MINUTES login update-check
               BMeet --control-check
                     the control socket in $BMEET_RUNTIME_ROOT/bmeet, headless: one JSON line per op
               BMeet --hotkey-check
                     registers the hotkeys file's shortcuts with macOS and follows the switches
                     (only ⌃⌥⇧⌘ with F13–F20 is allowed)
        """

    enum Outcome { case exit(Int32), app(checking: Bool) }

    /// A check runs: BMeet opens nothing (no app, file or System Settings), posts no
    /// notification, and never touches the clipboard or pastes, whatever path it takes.
    private(set) static var active = false

    /// What main.swift does with these arguments: run the app, run a check, or end (2: an option
    /// BMeet does not know). macOS's own arguments (-psn_…, -NSKey value, -AppleKey value) pass.
    static func start(_ arguments: [String]) -> Outcome {
        var args = arguments[...]
        var mode: String?, page: String?, lang: String?, picture: String?, appearance: String?, status: String?
        var acts: [String] = []
        func value(_ option: String) -> String? {
            guard let v = args.popFirst(), !v.hasPrefix("--") else {
                complain("\(option) needs a value")
                return nil
            }
            return v
        }
        while let arg = args.popFirst() {
            switch arg {
            case "--help", "-h":
                print(usage)
                return .exit(0)
            case "--settings-check", "--menu-check", "--settings-act", "--control-check", "--hotkey-check":
                guard mode == nil else { complain("one check at a time"); return .exit(2) }
                mode = arg
                if arg == "--settings-check" { guard let v = value(arg) else { return .exit(2) }; page = v }
                if arg == "--settings-act" { while let a = args.first, !a.hasPrefix("--") { acts.append(a); args.removeFirst() } }
            case "--lang":
                guard let v = value(arg), Interface.languages.contains(where: { $0.code == v }) else {
                    complain("--lang is en or ru")
                    return .exit(2)
                }
                lang = v
            case "--picture": guard let v = value(arg) else { return .exit(2) }; picture = v
            case "--appearance":
                guard let v = value(arg), ["light", "dark"].contains(v) else { complain("--appearance is light or dark"); return .exit(2) }
                appearance = v
            case "--status": guard let v = value(arg) else { return .exit(2) }; status = v
            default:
                if arg.hasPrefix("-psn_") { continue }
                if arg.hasPrefix("-NS") || arg.hasPrefix("-Apple") { _ = args.popFirst(); continue }
                complain("unknown option \(arg) (BMeet --help)")
                return .exit(2)
            }
        }
        guard let mode else {
            if lang != nil || picture != nil || appearance != nil || status != nil {
                complain("--lang, --picture, --appearance and --status go with a check")
                return .exit(2)
            }
            return .app(checking: false)
        }
        let env = ProcessInfo.processInfo.environment
        guard !(env["BMEET_RUNTIME_ROOT"] ?? "").isEmpty, !(env["XDG_CONFIG_HOME"] ?? "").isEmpty else {
            complain("a check needs BMEET_RUNTIME_ROOT and XDG_CONFIG_HOME: folders of its own, never the real ones")
            return .exit(2)
        }
        if mode == "--control-check", (env["BMEET_PYTHON"] ?? "").isEmpty {
            complain("--control-check needs BMEET_PYTHON: a stand-in, so that no dictation or meeting records")
            return .exit(2)
        }
        active = true
        logToFile = false
        if mode != "--control-check" { Paths.pythonForCheck = URL(fileURLWithPath: "/usr/bin/false") }
        DictationPanel.offScreen = true
        SettingsWindow.offScreen = true
        Interface.forced = lang
        Interface.reload()
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        if let appearance { app.appearance = NSAppearance(named: appearance == "dark" ? .darkAqua : .aqua) }
        switch mode {
        case "--settings-check": return .exit(settingsCheck(page ?? "all", picture: picture))
        case "--menu-check": return .exit(menuCheck(status: status))
        case "--settings-act": return .exit(settingsAct(acts))
        case "--hotkey-check": return .exit(hotkeyCheck())
        default: return .app(checking: true)
        }
    }

    private static func complain(_ text: String) {
        FileHandle.standardError.write("BMeet: \(text)\n".data(using: .utf8)!)
    }

    /// One JSON line on stdout, at once (a test reads it while BMeet runs).
    static func out(_ object: [String: Any]) {
        guard var data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) else { return }
        data.append(10)
        FileHandle.standardOutput.write(data)
    }

    // MARK: the Settings window

    private static func settingsCheck(_ which: String, picture: String?) -> Int32 {
        let pages: [SettingsWindow.Page]
        if which == "all" {
            pages = SettingsWindow.Page.allCases
        } else if let p = SettingsWindow.Page(rawValue: which) {
            pages = [p]
        } else {
            complain("no page \(which): general, features, languages, shortcuts, about or all")
            return 2
        }
        let app = AppDelegate(checking: true)
        app.hotKeys.register()
        let window = app.settings
        if let picture { try? FileManager.default.createDirectory(atPath: picture, withIntermediateDirectories: true) }
        var fits = true
        for page in pages {
            // The longest each page can get: every state of the About page, every note of a shortcut.
            var variants: [(String, () -> Void)] = [("", {})]
            if page == .about {
                let long = "cannot reach GitHub (curl: (6) Could not resolve host: api.github.com; check the network connection)"
                variants = [
                    ("checking", { window.updateForCheck = .init(checking: true) }),
                    ("available", { window.updateForCheck = .init(available: ("10.10.10", "")) }),
                    ("failed", { window.updateForCheck = .init(error: long) }),
                    ("up to date", { window.updateForCheck = .init(checked: Date()) }),
                    ("never", { window.updateForCheck = .init() }),
                ]
            } else if page == .shortcuts {
                variants = [("", { window.notesForCheck = nil }),
                            ("off", { window.notesForCheck = L("Off on the Features page") }),
                            ("taken", { window.notesForCheck = L("Taken by another app") })]
            }
            for (variant, set) in variants {
                set()
                Interface.clearMissing()
                window.rebuild()   // (every text asked for again, so a missing one is seen)
                window.select(page)
                guard let view = window.pages[page], let content = window.window.contentView else { return 1 }
                content.layoutSubtreeIfNeeded()
                let insets = SettingsWindow.insets
                let height = ceil(view.fittingSize.height) + insets.top + insets.bottom
                let wide = view.fittingSize.width > SettingsWindow.width + 0.5
                let cut = cutOffTexts(in: content)
                let untranslated = Interface.missing.sorted()
                let ok = height <= SettingsWindow.maxHeight && !wide && cut.isEmpty && untranslated.isEmpty
                fits = fits && ok
                out(["page": page.rawValue, "variant": variant, "lang": Interface.language, "height": Int(height),
                     "window_height": Int(window.window.frame.height), "too_wide": wide, "cut": cut,
                     "untranslated": untranslated, "title": window.window.title,
                     "toolbar": (window.window.toolbar?.items ?? []).map(\.label), "texts": texts(in: content), "ok": ok])
                if let picture, variant.isEmpty || variant == "up to date" {
                    draw(window.window, to: "\(picture)/settings-\(page.rawValue)-\(Interface.language).png")
                }
            }
        }
        return fits ? 0 : 1
    }

    /// The whole window, toolbar and all, into a PNG, without it being on the screen.
    private static func draw(_ window: NSWindow, to path: String) {
        guard let frame = window.contentView?.superview else { return }
        frame.layoutSubtreeIfNeeded()
        guard let rep = frame.bitmapImageRepForCachingDisplay(in: frame.bounds) else { return }
        frame.cacheDisplay(in: frame.bounds, to: rep)
        try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
    }

    /// The labels, buttons and menus under `view` whose text does not fit their frame. (Table rows
    /// are left out: a long name may be cut there, and the row says it in full when it is wider.)
    static func cutOffTexts(in view: NSView) -> [String] {
        if view.isHidden || view is NSTableView { return [] }
        if let field = view as? NSSearchField, let hint = field.placeholderString, let font = field.font {
            return (hint as NSString).size(withAttributes: [.font: font]).width > field.frame.width - 30 ? [hint] : []
        }
        var found: [String] = []
        if let label = view as? NSTextField {
            if label.isEditable, let hint = label.placeholderString, let font = label.font {
                if (hint as NSString).size(withAttributes: [.font: font]).width > label.frame.width - 30 { found.append(hint) }
            } else if !label.isEditable, !label.stringValue.isEmpty, let cell = label.cell {
                let wide = label.intrinsicContentSize.width > label.frame.width + 0.5
                let tall = cell.cellSize(forBounds: NSRect(x: 0, y: 0, width: label.frame.width, height: 10_000)).height
                    > label.frame.height + 0.5
                if cell.wraps ? tall : wide { found.append(label.stringValue) }
            }
        } else if let popup = view as? NSPopUpButton {
            if popup.intrinsicContentSize.width > popup.frame.width + 0.5 { found.append(popup.titleOfSelectedItem ?? "pop-up menu") }
        } else if let button = view as? NSButton, button.intrinsicContentSize.width > button.frame.width + 0.5 {
            found.append(button.title)
        }
        return found + view.subviews.flatMap(cutOffTexts)
    }

    /// Every text the page shows (a test looks for words of the right language in it).
    static func texts(in view: NSView) -> [String] {
        if view.isHidden { return [] }
        var found: [String] = []
        if let field = view as? NSSearchField {   // (not its own buttons inside)
            return [field.stringValue, field.placeholderString ?? ""].filter { !$0.isEmpty }
        }
        if let popup = view as? NSPopUpButton {
            found += popup.itemTitles
        } else if let label = view as? NSTextField {
            if !label.stringValue.isEmpty { found.append(label.stringValue) }
            if let hint = label.placeholderString { found.append(hint) }
        } else if let button = view as? NSButton, !button.title.isEmpty {
            found.append(button.title)
        }
        if let table = view as? NSTableView {
            for row in 0..<min(table.numberOfRows, 8) {
                if let cell = table.view(atColumn: 0, row: row, makeIfNecessary: true) { found += texts(in: cell) }
            }
            return found
        }
        return found + view.subviews.flatMap(texts)
    }

    // MARK: the menu

    private static func menuCheck(status file: String?) -> Int32 {
        let app = AppDelegate(checking: true)
        if let file {
            guard let data = FileManager.default.contents(atPath: file),
                  let status = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
                complain("--status \(file): not a JSON object")
                return 2
            }
            app.status = status
        }
        app.hotKeys.register()
        let menu = NSMenu()
        app.build(menu)
        out(["menu": dump(menu), "shortcuts": app.hotKeys.labels, "icon": app.icon().name, "lang": Interface.language,
             "untranslated": Interface.missing.sorted()])
        return 0
    }

    private static func dump(_ menu: NSMenu) -> [[String: Any]] {
        menu.items.map { item in
            if item.isSeparatorItem { return ["title": "-"] }
            var entry: [String: Any] = ["title": item.title, "key": keyLabel(item), "enabled": item.isEnabled]
            if let sub = item.submenu { entry["submenu"] = dump(sub) }
            return entry
        }
    }

    /// A menu item's shortcut the way the menu shows it: ⌃⌥H.
    private static func keyLabel(_ item: NSMenuItem) -> String {
        guard !item.keyEquivalent.isEmpty else { return "" }
        let m = item.keyEquivalentModifierMask
        var label = (m.contains(.control) ? "⌃" : "") + (m.contains(.option) ? "⌥" : "") + (m.contains(.shift) ? "⇧" : "")
            + (m.contains(.command) ? "⌘" : "")
        let scalar = item.keyEquivalent.unicodeScalars.first!.value
        if (UInt32(NSF1FunctionKey)...UInt32(NSF35FunctionKey)).contains(scalar) {
            label += "F\(Int(scalar) - NSF1FunctionKey + 1)"
        } else {
            label += item.keyEquivalent == " " ? "Space" : item.keyEquivalent.uppercased()
        }
        return label
    }

    // MARK: working the Settings window

    private static func settingsAct(_ acts: [String]) -> Int32 {
        let app = AppDelegate(checking: true)
        app.hotKeys.register()
        let window = app.settings
        func settle() { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }   // (a rebuild runs next)
        for act in acts {
            let (name, arg) = act.firstIndex(of: ":").map { (String(act[..<$0]), String(act[act.index(after: $0)...])) } ?? (act, "")
            switch name {
            case "page":
                guard let page = SettingsWindow.Page(rawValue: arg) else { complain("no page \(arg)"); return 2 }
                window.select(page)
            case "feature":
                guard let feature = Feature(rawValue: arg) else { complain("no feature \(arg)"); return 2 }
                window.select(.features)
                window.featureBoxes[feature]?.performClick(nil)
            case "live", "speakers", "voices":
                window.select(.features)
                (name == "live" ? window.liveBox : name == "speakers" ? window.speakersBox : window.voicesBox).performClick(nil)
            case "answer":
                SettingsWindow.answers.append(arg == "yes")
            case "login", "update-check":
                window.select(.general)
                (name == "login" ? window.loginBox : window.updateBox).performClick(nil)
            case "interface":
                window.select(.general)
                guard let item = window.interfacePopup.itemArray.first(where: { $0.representedObject as? String == arg }) else {
                    complain("no interface language \(arg)")
                    return 2
                }
                window.interfacePopup.select(item)
                window.interfacePopup.sendAction(window.interfacePopup.action, to: window.interfacePopup.target)
            case "unload":
                window.select(.general)
                guard let minutes = Int(arg), window.unloadPopup.indexOfItem(withTag: minutes) >= 0 else {
                    complain("unload: one of 0, 1, 5, 15, 30, 60")
                    return 2
                }
                window.unloadPopup.selectItem(withTag: minutes)
                window.unloadPopup.sendAction(window.unloadPopup.action, to: window.unloadPopup.target)
            case "search":
                window.select(.languages)
                window.search.stringValue = arg
                window.controlTextDidChange(Notification(name: NSControl.textDidChangeNotification, object: window.search))
            case "language":
                if window.page != .languages { window.select(.languages) }
                guard let row = window.rows.firstIndex(of: arg),
                      let cell = window.table.view(atColumn: 0, row: row, makeIfNecessary: true) as? LanguageRow else {
                    complain("language \(arg) is not in the list")
                    return 2
                }
                cell.box.performClick(nil)
            case "dictation-language":
                window.select(.languages)
                guard let item = window.dictationPopup.itemArray.first(where: { $0.representedObject as? String == arg }) else {
                    complain("\(arg) is not a language that is on")
                    return 2
                }
                window.dictationPopup.select(item)
                window.dictationPopup.sendAction(window.dictationPopup.action, to: window.dictationPopup.target)
            case "shortcut":
                let parts = arg.split(separator: "=", maxSplits: 1).map(String.init)
                guard parts.count == 2, let recorder = window.recorders[parts[0]] else { complain("shortcut:ACTION=KEYS"); return 2 }
                if window.page != .shortcuts { window.select(.shortcuts) }
                recorder.performClick(nil)
                guard let event = keyEvent(parts[1]) else { complain("keys \(parts[1])"); return 2 }
                recorder.take(event)
            case "restore-shortcuts":
                window.select(.shortcuts)
                window.restoreButton.performClick(nil)
            default:
                complain("unknown act \(act)")
                return 2
            }
            settle()
        }
        var files: [String: String] = [:]
        for name in (try? FileManager.default.contentsOfDirectory(atPath: Paths.config.path)) ?? [] {
            files[name] = (try? String(contentsOf: Paths.config.appendingPathComponent(name), encoding: .utf8)) ?? ""
        }
        out(["files": files, "shortcuts": app.hotKeys.labels, "page": window.page.rawValue, "title": window.window.title,
             "toolbar": (window.window.toolbar?.items ?? []).map(\.label), "message": window.shortcutMessage.stringValue,
             "rows": Array(window.rows.prefix(6)), "lang": Interface.language, "recording": window.recorders.values.contains(where: \.isRecording),
             "texts": texts(in: window.window.contentView ?? NSView())])
        return 0
    }

    /// A key press as the recorder gets it: "ctrl+option+j", "delete", "esc", "h".
    private static func keyEvent(_ keys: String) -> NSEvent? {
        var flags: NSEvent.ModifierFlags = []
        var code: UInt16?
        for part in keys.split(separator: "+").map(String.init) {
            switch part {
            case "ctrl": flags.insert(.control)
            case "option": flags.insert(.option)
            case "cmd": flags.insert(.command)
            case "shift": flags.insert(.shift)
            case "delete": code = 0x33
            case "esc": code = 0x35
            default: code = HotKeys.keyCodes[part].map { UInt16($0) }
            }
        }
        guard let code else { return nil }
        return NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: flags, timestamp: 0, windowNumber: 0, context: nil,
                                characters: "", charactersIgnoringModifiers: "", isARepeat: false, keyCode: code)
    }

    // MARK: the control socket

    static func ready() { out(["ready": true, "socket": Paths.runtime.appendingPathComponent("agent.sock").path]) }

    static func handled(_ req: [String: Any], _ answer: [String: Any], by app: AppDelegate) {
        var line: [String: Any] = ["op": req["op"] as? String ?? "", "answer": answer,
                                   "panel_shown": app.dictation.panelShown, "dictation": app.dictation.state.rawValue]
        if req["op"] as? String == "settings" {
            line["page"] = app.settings.page.rawValue
            line["shown"] = app.settings.window.isVisible
        }
        out(line)
    }

    // MARK: registering shortcuts

    private static func hotkeyCheck() -> Int32 {
        // (never one the running BMeet or another app may hold: all four modifiers and F13–F20)
        let rare = { (combo: String) -> Bool in
            let parts = Set(combo.split(separator: "+").map(String.init))
            return parts.isSuperset(of: ["ctrl", "option", "shift", "cmd"]) && parts.count == 5
                && parts.contains { ["f13", "f14", "f15", "f16", "f17", "f18", "f19", "f20"].contains($0) }
        }
        let combos = HotKeys.configured().map(\.1).filter { $0 != "none" }
        guard !combos.isEmpty, combos.allSatisfy(rare) else {
            complain("--hotkey-check registers shortcuts for real: every shortcut in its hotkeys file must be ctrl+option+shift+cmd with one of f13..f20")
            return 2
        }
        let app = AppDelegate(checking: true)
        let keys = app.hotKeys!
        keys.dryRun = false
        func step(_ name: String) {
            out(["step": name, "registered": keys.registeredCount, "shortcuts": keys.labels, "taken": keys.taken])
        }
        keys.register()
        step("start")
        Feature.dictation.set(false); app.settingsChanged(); step("dictation off")
        Feature.meetings.set(false); app.settingsChanged(); step("meetings off")
        Feature.dictation.set(true); app.settingsChanged(); step("dictation on")
        Feature.meetings.set(true); app.settingsChanged(); step("meetings on")
        var map = Dictionary(uniqueKeysWithValues: HotKeys.configured())
        map["dictate"] = "ctrl+option+shift+cmd+f20"
        HotKeys.write(map); app.settingsChanged(); step("dictate changed")
        app.pauseHotKeys(true); step("paused")
        app.pauseHotKeys(false); step("resumed")
        let taken = !keys.taken.isEmpty
        keys.unregister()
        step("end")
        return taken ? 1 : 0
    }
}
