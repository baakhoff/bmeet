// BMeet's check modes, run from the command line by macos/tests/test_settings.py and
// test_island.py. None shows anything: no menu bar icon, no window on the screen (the Settings
// window and the island are built and drawn off screen), no dictation panel, no notification, no
// question from macOS. None touches the real settings: each needs BMEET_RUNTIME_ROOT and
// XDG_CONFIG_HOME (a test's own folders), and --control-check also BMEET_PYTHON (a stand-in for
// BMeet's Python, so nothing records). Only --hotkey-check registers shortcuts with macOS, and
// only ones nobody uses (⌃⌥⇧⌘ with F13–F20), for a moment: the running BMeet keeps its own. A
// check's island has the screen and housing --screen and --housing give (none without them):
// never this Mac's, unless --screen real asks to measure them.

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
               BMeet --island-check STATE|all|status [--open] [--act ACT...] [--status FILE...]
                     [--lang en|ru] [--picture DIR] [--appearance light|dark]
                     the island around the camera, off screen: its frame, its texts, the texts
                     cut short or not translated (JSON lines; exit 1 if one is wrong). STATE:
                     idle listening words transcribing meeting meeting-paused in-person
                     in-person-paused speakers saved failed; all: each, closed and open; status:
                     what BMeet makes of each FILE in turn (a `bmeet-ctl status` answer, plus
                     "notify": [{title, body, ok}], notifications that came after it, and
                     "written": PATH, what a stop printed).
                     ACT (after one state, or the last FILE): click outside escape hover leave
                     end island-off island-on no-housing button:ACTION
               Any check: --screen W,H --housing X,W,H|none, the screen and its camera housing
                     in points (X from the screen's left edge); --screen real: this Mac's
        """

    enum Outcome { case exit(Int32), app(checking: Bool) }

    /// A check runs: BMeet opens nothing (no app, file or System Settings), posts no
    /// notification, and never touches the clipboard or pastes, whatever path it takes.
    private(set) static var active = false

    /// What main.swift does with these arguments: run the app, run a check, or end (2: an option
    /// BMeet does not know). macOS's own arguments (-psn_…, -NSKey value, -AppleKey value) pass.
    static func start(_ arguments: [String]) -> Outcome {
        var args = arguments[...]
        var mode: String?, page: String?, lang: String?, picture: String?, appearance: String?
        var statuses: [String] = [], screen: String?, housing: String?, open = false, islandActs: [String] = []
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
            case "--settings-check", "--menu-check", "--settings-act", "--control-check", "--hotkey-check", "--island-check":
                guard mode == nil else { complain("one check at a time"); return .exit(2) }
                mode = arg
                if arg == "--settings-check" || arg == "--island-check" { guard let v = value(arg) else { return .exit(2) }; page = v }
                if arg == "--settings-act" { while let a = args.first, !a.hasPrefix("--") { acts.append(a); args.removeFirst() } }
            case "--act":
                while let a = args.first, !a.hasPrefix("--") { islandActs.append(a); args.removeFirst() }
                guard !islandActs.isEmpty else { complain("--act needs an act"); return .exit(2) }
            case "--open": open = true
            case "--screen": guard let v = value(arg) else { return .exit(2) }; screen = v
            case "--housing": guard let v = value(arg) else { return .exit(2) }; housing = v
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
            case "--status": guard let v = value(arg) else { return .exit(2) }; statuses.append(v)
            default:
                if arg.hasPrefix("-psn_") { continue }
                if arg.hasPrefix("-NS") || arg.hasPrefix("-Apple") { _ = args.popFirst(); continue }
                complain("unknown option \(arg) (BMeet --help)")
                return .exit(2)
            }
        }
        guard let mode else {
            if lang != nil || picture != nil || appearance != nil || !statuses.isEmpty || screen != nil || housing != nil
                || open || !islandActs.isEmpty {
                complain("--lang, --picture, --appearance, --status, --screen, --housing, --open and --act go with a check")
                return .exit(2)
            }
            return .app(checking: false)
        }
        if (open || !islandActs.isEmpty) && mode != "--island-check" {
            complain("--open and --act go with --island-check")
            return .exit(2)
        }
        let env = ProcessInfo.processInfo.environment
        guard !(env["BMEET_RUNTIME_ROOT"] ?? "").isEmpty, !(env["XDG_CONFIG_HOME"] ?? "").isEmpty else {
            complain("a check needs BMEET_RUNTIME_ROOT and XDG_CONFIG_HOME: folders of its own, never the real ones")
            return .exit(2)
        }
        // The island's screen and housing: the options', never this Mac's unless --screen real
        Island.offScreen = true
        if screen == "real" {
            guard housing == nil else { complain("--screen real measures this Mac's housing: no --housing with it"); return .exit(2) }
            Island.checkMeasuresScreens = true
        } else if let screen {
            guard let s = numbers(screen, 2), s[0] > 0, s[1] > 0 else { complain("--screen is W,H in points, or real"); return .exit(2) }
            if let housing, housing != "none" {
                guard let c = numbers(housing, 3), c[0] > 0, c[1] > 0, c[2] > 0, c[0] + c[1] < s[0], c[2] < s[1] else {
                    complain("--housing is X,W,H inside the screen (X from its left edge), or none")
                    return .exit(2)
                }
                Island.checkGeometry = IslandGeometry.make(screen: NSRect(x: 0, y: 0, width: s[0], height: s[1]),
                                                           left: c[0], right: s[0] - c[0] - c[1], height: c[2])
            }
        } else if housing != nil {
            complain("--housing goes with --screen W,H")
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
        case "--menu-check": return .exit(menuCheck(status: statuses.last))
        case "--settings-act": return .exit(settingsAct(acts))
        case "--hotkey-check": return .exit(hotkeyCheck())
        case "--island-check":
            return .exit(islandCheck(page ?? "all", open: open, acts: islandActs, statuses: statuses, picture: picture,
                                     dark: appearance == "dark"))
        default: return .app(checking: true)
        }
    }

    /// "1800,1182" as numbers, nil unless it is `count` of them, none negative.
    private static func numbers(_ text: String, _ count: Int) -> [CGFloat]? {
        let parts = text.split(separator: ",", omittingEmptySubsequences: false)
        let values = parts.compactMap { Double($0.trimmingCharacters(in: .whitespaces)) }
        guard parts.count == count, values.count == count, values.allSatisfy({ $0.isFinite && $0 >= 0 }) else { return nil }
        return values.map { CGFloat($0) }
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
        // (the island's words while you speak lose their oldest words at the start, as they should)
        if view.isHidden || view is NSTableView || view.identifier?.rawValue == "words" { return [] }
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
            case "live", "speakers", "voices", "island":
                window.select(.features)
                (name == "live" ? window.liveBox : name == "speakers" ? window.speakersBox : name == "voices" ? window.voicesBox
                    : window.islandBox).performClick(nil)
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
             "unload": window.unloadPopup.titleOfSelectedItem ?? "", "dictation_language": Settings.language,
             "texts": texts(in: window.window.contentView ?? NSView()),
             "disabled": disabledButtons(in: window.window.contentView ?? NSView())])
        return 0
    }

    /// The titles of the buttons and switches that cannot be clicked now.
    static func disabledButtons(in view: NSView) -> [String] {
        if view.isHidden { return [] }
        let mine = (view as? NSButton).flatMap { $0.isEnabled || $0.title.isEmpty ? nil : [$0.title] } ?? []
        return mine + view.subviews.flatMap(disabledButtons)
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
                                   "panel_shown": app.dictation.panelShown, "dictation": app.dictation.state.rawValue,
                                   "island": app.island.state.name]
        if req["op"] as? String == "settings" {
            line["page"] = app.settings.page.rawValue
            line["shown"] = app.settings.window.isVisible
        }
        out(line)
    }

    // MARK: the island

    /// What the island's controls asked BMeet to do (AppDelegate.islandAction; a check prints it).
    static var islandActions: [String] = []

    static let islandStates = ["listening", "words", "transcribing", "meeting", "meeting-paused", "in-person",
                               "in-person-paused", "speakers", "saved", "failed"]

    /// A state as BMeet would show it, with the texts it would have: a meeting 12:34 old, words
    /// being said, a transcript.
    static func sampleIsland(_ name: String) -> IslandState? {
        let ru = Interface.language == "ru"
        let file = Paths.home.appendingPathComponent("Documents/Meetings/2026-11-28_1405-meeting.md").path
        let language = ru ? "ru" : "en"   // (the words below are said in it)
        var said = IslandState.Words(language: language)
        said.final = ru ? "Итак, план на эту неделю: доделать остров" : "So the plan for this week is to finish the island"
        said.draft = ru ? "и отправить его на проверку" : "and send it out for review"
        switch name {
        case "idle": return .idle
        case "listening": return .listening(IslandState.Words(language: language))
        case "words": return .listening(said)
        case "transcribing": return .transcribing(IslandState.Words(language: language))
        case "meeting", "meeting-paused", "in-person", "in-person-paused":
            return .meeting(started: Date().timeIntervalSince1970 - 754, paused: name.hasSuffix("-paused"),
                            inPerson: name.hasPrefix("in-person"))
        case "speakers": return .speakers(file: file)
        case "saved": return .saved(file: file)
        case "failed":
            return .failed(title: L("Speakers not told apart"), detail: L("The transcript stays as it was written during the meeting."),
                           file: file)
        default: return nil
        }
    }

    private static func islandCheck(_ which: String, open: Bool, acts: [String], statuses: [String], picture: String?,
                                    dark: Bool) -> Int32 {
        guard which == "all" || which == "status" || sampleIsland(which) != nil else {
            complain("no island state \(which): \(islandStates.joined(separator: " ")), idle, all or status")
            return 2
        }
        guard which != "status" || !statuses.isEmpty else { complain("--island-check status needs --status FILE"); return 2 }
        guard acts.isEmpty || which != "all" else { complain("--act goes with one state, or after --status"); return 2 }
        let app = AppDelegate(checking: true)
        let island = app.island
        if let picture { try? FileManager.default.createDirectory(atPath: picture, withIntermediateDirectories: true) }
        var fits = true
        var sheet: [(String, NSBitmapImageRep)] = []
        let shade = dark ? "dark" : "light"
        func report(_ extra: [String: Any]) {
            island.view.layoutNow()
            let (made, ok) = islandLine(app)
            let line = made.merging(extra) { $1 }
            fits = fits && ok
            out(line)
        }

        if which == "status" {
            for file in statuses {
                guard let data = FileManager.default.contents(atPath: file),
                      let s = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
                    complain("--status \(file): not a JSON object")
                    return 2
                }
                app.statusArrived(s)
                // (a check's own keys: the notifications that came after this status, the transcript a
                // stop printed)
                for n in s["notify"] as? [[String: Any]] ?? [] {
                    island.notified(n["title"] as? String ?? "", n["body"] as? String ?? "", ok: n["ok"] as? Bool ?? true)
                }
                if let written = s["written"] as? String { island.transcriptWritten(written) }
                app.refreshIsland()
                report(["status": file])
            }
        }

        for name in which == "all" ? islandStates : which == "status" ? [] : [which] {
            for opened in which == "all" ? [false, true] : [open] {
                Interface.clearMissing()
                islandActions = []
                island.present(.idle)
                island.present(sampleIsland(name)!)
                if opened { island.open() }
                report([:])
                if let picture, let image = islandPicture(island, dark: dark) {
                    let id = name + (opened ? "-open" : "")
                    save(image, to: "\(picture)/island-\(id)-\(Interface.language)-\(shade).png")
                    sheet.append((name + (opened ? ", open" : ""), image))
                }
            }
        }
        for act in acts {
            var hit: Bool?
            let view = island.view
            switch act {
            case "click", "outside":
                // where the mouse would be: the island's left side beside the housing, or a corner of
                // its panel outside the island's shape
                view.layoutNow()
                let point = act == "click" ? NSPoint(x: view.shapeRect.minX + 12, y: view.bounds.maxY - view.housingRect.height / 2)
                    : NSPoint(x: view.bounds.minX + 1, y: view.bounds.minY + 1)
                let target = view.hitTest(view.convert(point, to: view.superview))
                hit = target != nil
                if act == "click", target === view, let event = NSEvent.mouseEvent(
                    with: .leftMouseDown, location: view.convert(point, to: nil), modifierFlags: [], timestamp: 0,
                    windowNumber: view.window?.windowNumber ?? 0, context: nil, eventNumber: 0, clickCount: 1, pressure: 1) {
                    view.mouseDown(with: event)
                } else if act == "outside", target == nil {
                    island.close()   // (what a click elsewhere does: the panel let it through)
                }
            case "escape": island.escape()   // (what the Esc hot key does)
            case "hover", "leave": island.setHover(act == "hover")
            // The ways the island goes, each as BMeet takes it: what it knows says nothing runs
            // (after a STATE alone, nothing does), the switch is turned off or on, the housing goes
            case "end": app.refreshIsland()
            case "island-off", "island-on":
                Settings.set("island", act == "island-on" ? "on" : "off")
                app.settingsChanged()
            case "no-housing":
                Island.checkGeometry = nil
                Island.checkMeasuresScreens = false
                island.measure()
                island.onScreens()
            default:
                guard act.hasPrefix("button:"), let button = view.buttons.first(where: { "button:" + $0.what.rawValue == act }) else {
                    complain("no act \(act) here: click outside escape hover leave end island-off island-on no-housing "
                             + "button:ACTION (\(view.buttons.map(\.what.rawValue)))")
                    return 2
                }
                button.performClick(nil)
            }
            report(["act": act, "hit": hit.map { $0 as Any } ?? NSNull()])
        }
        if let picture, which == "all", !sheet.isEmpty {
            save(islandSheet(sheet, dark: dark), to: "\(picture)/island-\(Interface.language)-\(shade).png")
        }
        return fits ? 0 : 1
    }

    /// The island as a check prints it: where it is, its texts, and whether it is right: centred
    /// on the housing, at least as wide and as tall, on the screen, no text cut short, outside its
    /// shape or under the camera, every text translated.
    private static func islandLine(_ app: AppDelegate) -> ([String: Any], Bool) {
        let island = app.island, view = island.view
        func rect(_ r: NSRect) -> [Double] { [r.minX, r.minY, r.width, r.height].map { (Double($0) * 100).rounded() / 100 } }
        let shown = island.state != .idle
        var line: [String: Any] = [
            "state": island.state.name, "open": island.isOpen, "lang": Interface.language, "island": shown,
            "panel": !island.shows, "hover": island.hover, "escape_held": app.hotKeys.escapeHeld, "actions": islandActions,
            "untranslated": Interface.missing.sorted(), "screen": island.geometry.map { rect($0.screen) } ?? NSNull(),
            "housing": island.geometry.map { rect($0.housing) } ?? NSNull(),
            "frame": NSNull(), "window": NSNull(), "texts": [String](), "cut": [String](), "buttons": [String](),
            "outside": [String](), "under_camera": [String](),
        ]
        var ok = Interface.missing.isEmpty
        if shown, let g = island.geometry, let frame = island.frame {
            let body = view.shapeRect.insetBy(dx: -0.5, dy: -0.5), camera = view.housingRect.insetBy(dx: 0.5, dy: 0.5)
            var outside: [String] = [], underCamera: [String] = []
            for v in view.subviews where !v.isHidden && v.alphaValue > 0 && v.frame.width > 0 {
                let name = (v as? NSButton)?.title ?? (v as? NSTextField)?.stringValue ?? "sign"
                if !body.contains(v.frame) { outside.append(name) }
                if v.frame.intersects(camera) { underCamera.append(name) }
            }
            // (and, stricter, what AppKit itself would draw cut short: a label set by its frame)
            let truncated = view.subviews.compactMap { $0 as? NSTextField }.filter { label in
                guard !label.isHidden, label.identifier?.rawValue != "words", !label.stringValue.isEmpty, let cell = label.cell,
                      !cell.wraps else { return false }
                return cell.expansionFrame(withFrame: label.bounds, in: label) != .zero
            }.map(\.stringValue)
            let cut = Array(Set(cutOffTexts(in: view) + truncated)).sorted()
            let centred = abs(frame.midX - g.housing.midX) < 0.5
            let covers = frame.width >= g.housing.width && frame.maxY == g.screen.maxY && frame.minY <= g.housing.minY
            let onScreen = frame.minX >= g.screen.minX && frame.maxX <= g.screen.maxX
            line["frame"] = rect(frame)
            line["window"] = island.panelFrame.map(rect) ?? NSNull()
            line["texts"] = texts(in: view)
            line["cut"] = cut
            line["buttons"] = view.buttons.map(\.what.rawValue)
            line["outside"] = outside
            line["under_camera"] = underCamera
            line["centred"] = centred
            ok = ok && cut.isEmpty && outside.isEmpty && underCamera.isEmpty && centred && covers && onScreen
        }
        line["ok"] = ok
        return (line, ok)
    }

    /// A bitmap of `size` points at twice as many pixels (a Retina screen's).
    private static func retina(_ size: NSSize) -> NSBitmapImageRep {
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(ceil(size.width * 2)), pixelsHigh: Int(ceil(size.height * 2)),
                                   bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                   colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        rep.size = size
        return rep
    }

    private static func save(_ rep: NSBitmapImageRep, to path: String) {
        try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
    }

    /// The top of the screen around the housing with the island on it: a desktop, a dark or a
    /// light menu bar, the housing in black, and the island drawn off screen.
    private static func islandPicture(_ island: Island, dark: Bool) -> NSBitmapImageRep? {
        guard let g = island.geometry else { return nil }
        let view = island.view
        view.layoutNow()
        let menuBar = (g.housing.height * 54 / 47.5).rounded()   // (this Mac: 54 points with a 47.5-point housing)
        let width = min(g.screen.width, 520), height = max(menuBar, island.state == .idle ? 0 : view.bounds.height) + 26
        var drawn: NSBitmapImageRep?
        if island.state != .idle {
            let rep = retina(view.bounds.size)
            view.cacheDisplay(in: view.bounds, to: rep)
            drawn = rep
        }
        let picture = retina(NSSize(width: width, height: height))
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: picture)
        // (flat colors: the pictures stay small)
        (dark ? NSColor(calibratedRed: 0.16, green: 0.13, blue: 0.27, alpha: 1)
              : NSColor(calibratedRed: 0.72, green: 0.80, blue: 0.94, alpha: 1)).setFill()
        NSRect(x: 0, y: 0, width: width, height: height).fill()
        (dark ? NSColor(white: 0.13, alpha: 1) : NSColor(white: 0.95, alpha: 1)).setFill()
        NSRect(x: 0, y: height - menuBar, width: width, height: menuBar).fill()
        NSColor.black.setFill()
        let housing = IslandView.Shape(width: g.housing.width, height: g.housing.height, radius: (g.housing.height * 0.42).rounded())
        IslandView.outline(housing, centerX: width / 2, top: height, shoulder: 0).fill()
        drawn?.draw(in: NSRect(x: width / 2 - (view.centerX ?? view.bounds.midX), y: height - view.bounds.height,
                               width: view.bounds.width, height: view.bounds.height),
                    from: .zero, operation: .sourceOver, fraction: 1, respectFlipped: false, hints: nil)
        NSGraphicsContext.restoreGraphicsState()
        return picture
    }

    /// The pictures of every state, one under the other, each named on its left.
    private static func islandSheet(_ rows: [(String, NSBitmapImageRep)], dark: Bool) -> NSBitmapImageRep {
        let labels: CGFloat = 150, gap: CGFloat = 2
        let width = labels + (rows.map { $0.1.size.width }.max() ?? 0)
        let height = rows.reduce(0) { $0 + $1.1.size.height } + gap * CGFloat(max(0, rows.count - 1))
        let sheet = retina(NSSize(width: width, height: height))
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: sheet)
        NSColor(white: dark ? 0.1 : 0.95, alpha: 1).setFill()
        NSRect(x: 0, y: 0, width: width, height: height).fill()
        var top = height
        for (name, image) in rows {
            let h = image.size.height
            image.draw(in: NSRect(x: labels, y: top - h, width: image.size.width, height: h))
            let text = NSAttributedString(string: name, attributes: [
                .font: NSFont.systemFont(ofSize: 12, weight: .medium), .foregroundColor: NSColor(white: dark ? 0.75 : 0.3, alpha: 1)])
            text.draw(at: NSPoint(x: 12, y: top - 34))
            top -= h + gap
        }
        NSGraphicsContext.restoreGraphicsState()
        return sheet
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
