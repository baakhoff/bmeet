// BMeet.app's Settings window: General, Features, Languages, Shortcuts and About, behind a
// toolbar, the Mac way. It opens from the menu (Settings… ⌘,, About BMeet) and from the control
// socket's "settings" op (bmeet-ctl settings PAGE). Each control saves at once, into its file in
// ~/.config/bmeet (Paths.swift, Settings), and BMeet.app follows it at once
// (AppDelegate.settingsChanged); the BMeet window and bmeet-ctl read the same files. A check
// builds and draws it without ever showing it (Checks.swift).

import AppKit

final class SettingsWindow: NSObject, NSToolbarDelegate, NSWindowDelegate, NSTableViewDataSource, NSTableViewDelegate,
                            NSSearchFieldDelegate {
    enum Page: String, CaseIterable {
        case general, features, languages, shortcuts, about

        var title: String {
            switch self {
            case .general: return L("General")
            case .features: return L("Features")
            case .languages: return L("Languages")
            case .shortcuts: return L("Shortcuts")
            case .about: return L("About")
            }
        }

        var symbol: String {
            switch self {
            case .general: return "gearshape"
            case .features: return "switch.2"
            case .languages: return "globe"
            case .shortcuts: return "keyboard"
            case .about: return "info.circle"
            }
        }
    }

    /// The width of every page; and the height none may pass, so the window fits a 13-inch
    /// screen with the menu bar, the Dock and its own toolbar.
    static let width: CGFloat = 520
    static let maxHeight: CGFloat = 600
    static let insets = NSEdgeInsets(top: 20, left: 24, bottom: 22, right: 24)
    /// A check: nothing is ever shown; a question is answered from this list (or "no").
    static var offScreen = false
    static var answers: [Bool] = []

    let window: NSWindow
    private unowned let app: AppDelegate
    private(set) var page: Page = .general
    private(set) var pages: [Page: NSView] = [:]
    private let container = NSView()

    // General
    private(set) var interfacePopup = NSPopUpButton()
    private(set) var loginBox = NSButton()
    private(set) var updateBox = NSButton()
    private(set) var unloadPopup = NSPopUpButton()
    private var pasteStatus = NSTextField()
    private var pasteButton = NSButton()
    private var pasteRows: [NSGridRow] = []
    // Features
    private(set) var featureBoxes: [Feature: NSButton] = [:]
    private(set) var liveBox = NSButton()
    private var liveRow = NSView()
    private(set) var speakersBox = NSButton()
    private(set) var voicesBox = NSButton()
    // Languages
    private(set) var dictationPopup = NSPopUpButton()
    private(set) var search = NSSearchField()
    private(set) var table = NSTableView()
    private(set) var rows: [String] = []
    private var languageNote = NSTextField()
    // Shortcuts
    private(set) var recorders: [String: ShortcutRecorder] = [:]
    private var notes: [String: NSTextField] = [:]
    private(set) var shortcutMessage = NSTextField()
    private(set) var restoreButton = NSButton()
    /// A check: every shortcut's note shows this text (the longest a row can get).
    var notesForCheck: String?
    // About
    private var updateText = NSTextField()
    private var updateButton = NSButton()
    private var updateSpinner = NSProgressIndicator()
    private var modelsGrid = NSGridView()
    /// A check: the About page shows this instead of what BMeet knows about updates.
    var updateForCheck: UpdateState?

    struct UpdateState {
        var checking = false
        var available: (version: String, url: String)?
        var checked: Date?
        var error: String?
    }

    init(app: AppDelegate) {
        self.app = app
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: SettingsWindow.width + 48, height: 400),
                          styleMask: [.titled, .closable], backing: .buffered, defer: true)
        super.init()
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.contentView = container
        window.toolbarStyle = .preference
        let toolbar = NSToolbar(identifier: "BMeetSettings")
        toolbar.delegate = self
        toolbar.displayMode = .iconAndLabel
        toolbar.allowsUserCustomization = false
        window.toolbar = toolbar
        build()
        select(.general)
    }

    /// Shows the window on `page` (in a check: only selects it).
    func show(_ page: Page) {
        select(page)
        guard !SettingsWindow.offScreen else { return }
        if !window.isVisible { window.center() }
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    func select(_ page: Page) {
        for recorder in recorders.values { recorder.stop() }
        self.page = page
        window.toolbar?.selectedItemIdentifier = NSToolbarItem.Identifier(page.rawValue)
        window.title = page.title
        fill(page)
        guard let view = pages[page] else { return }
        container.subviews.forEach { $0.removeFromSuperview() }
        view.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(view)
        let i = SettingsWindow.insets
        NSLayoutConstraint.activate([
            view.topAnchor.constraint(equalTo: container.topAnchor, constant: i.top),
            view.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: i.left),
        ])
        let size = NSSize(width: SettingsWindow.width + i.left + i.right, height: ceil(view.fittingSize.height) + i.top + i.bottom)
        let top = window.frame.maxY
        window.setContentSize(size)
        if window.isVisible { window.setFrameTopLeftPoint(NSPoint(x: window.frame.minX, y: top)) }
        container.layoutSubtreeIfNeeded()
    }

    /// The interface language changed: every page is built again, in it.
    func rebuild() {
        for recorder in recorders.values { recorder.stop() }
        build()
        for item in window.toolbar?.items ?? [] {
            if let p = Page(rawValue: item.itemIdentifier.rawValue) { item.label = p.title }
        }
        select(page)
    }

    /// The files changed: the controls show them (not while a shortcut is being recorded).
    func fill() {
        guard !recorders.values.contains(where: \.isRecording) else { return }
        fill(page)
    }

    func fillAbout() { if page == .about { fill(.about) } }

    private func fill(_ page: Page) {
        switch page {
        case .general: fillGeneral()
        case .features: fillFeatures()
        case .languages: fillLanguages()
        case .shortcuts: fillShortcuts()
        case .about: fillAboutPage()
        }
    }

    func windowWillClose(_ notification: Notification) {
        for recorder in recorders.values { recorder.stop() }
    }

    // MARK: the toolbar

    func toolbarAllowedItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        Page.allCases.map { NSToolbarItem.Identifier($0.rawValue) }
    }

    func toolbarDefaultItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        toolbarAllowedItemIdentifiers(toolbar)
    }

    func toolbarSelectableItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        toolbarAllowedItemIdentifiers(toolbar)
    }

    func toolbar(_ toolbar: NSToolbar, itemForItemIdentifier id: NSToolbarItem.Identifier,
                 willBeInsertedIntoToolbar flag: Bool) -> NSToolbarItem? {
        guard let page = Page(rawValue: id.rawValue) else { return nil }
        let item = NSToolbarItem(itemIdentifier: id)
        item.label = page.title
        item.image = NSImage(systemSymbolName: page.symbol, accessibilityDescription: nil)
        item.target = self
        item.action = #selector(toolbarClicked(_:))
        return item
    }

    @objc private func toolbarClicked(_ sender: NSToolbarItem) {
        if let page = Page(rawValue: sender.itemIdentifier.rawValue) { select(page) }
    }

    // MARK: building

    private func build() {
        pages = [.general: buildGeneral(), .features: buildFeatures(), .languages: buildLanguages(),
                 .shortcuts: buildShortcuts(), .about: buildAbout()]
    }

    private func stack(_ views: [NSView], spacing: CGFloat = 8) -> NSStackView {
        let s = NSStackView(views: views)
        s.orientation = .vertical
        s.alignment = .leading
        s.spacing = spacing
        s.translatesAutoresizingMaskIntoConstraints = false
        s.widthAnchor.constraint(equalToConstant: SettingsWindow.width).isActive = true
        return s
    }

    private func caption(_ text: String, width: CGFloat) -> NSTextField {
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        label.textColor = .secondaryLabelColor
        label.preferredMaxLayoutWidth = width
        label.translatesAutoresizingMaskIntoConstraints = false
        label.widthAnchor.constraint(equalToConstant: width).isActive = true
        return label
    }

    private func rowLabel(_ text: String) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.alignment = .right
        return label
    }

    private func checkbox(_ title: String, _ action: Selector) -> NSButton {
        NSButton(checkboxWithTitle: title, target: self, action: action)
    }

    private func button(_ title: String, _ action: Selector) -> NSButton {
        let b = NSButton(title: title, target: self, action: action)
        b.bezelStyle = .rounded
        return b
    }

    private func separator() -> NSBox {
        let line = NSBox()
        line.boxType = .separator
        line.translatesAutoresizingMaskIntoConstraints = false
        line.widthAnchor.constraint(equalToConstant: SettingsWindow.width).isActive = true
        return line
    }

    /// `view`, moved right by `by` points (a caption under a checkbox starts where its title does).
    private func indent(_ view: NSView, by: CGFloat = 20) -> NSView {
        let box = NSView()
        box.translatesAutoresizingMaskIntoConstraints = false
        view.translatesAutoresizingMaskIntoConstraints = false
        box.addSubview(view)
        NSLayoutConstraint.activate([
            view.leadingAnchor.constraint(equalTo: box.leadingAnchor, constant: by),
            view.trailingAnchor.constraint(lessThanOrEqualTo: box.trailingAnchor),
            view.topAnchor.constraint(equalTo: box.topAnchor),
            view.bottomAnchor.constraint(equalTo: box.bottomAnchor),
        ])
        return box
    }

    /// A grid of right-aligned labels and their controls, with captions under the controls.
    /// Rows are [label or nil, control]; a nil control makes a separator across both columns.
    private func form(_ rows: [(String?, NSView?)]) -> (grid: NSGridView, rows: [NSGridRow]) {
        let labels = rows.compactMap { $0.0 }.map(rowLabel)
        let first = ceil(labels.map(\.intrinsicContentSize.width).max() ?? 0)
        let grid = NSGridView(numberOfColumns: 2, rows: 0)
        grid.columnSpacing = 10
        grid.rowSpacing = 4
        grid.column(at: 0).xPlacement = .trailing
        grid.column(at: 0).width = first
        grid.rowAlignment = .firstBaseline
        var made: [NSGridRow] = []
        var labelIndex = 0
        for (title, view) in rows {
            let label: NSView
            if title != nil {
                label = labels[labelIndex]
                labelIndex += 1
            } else {
                label = NSGridCell.emptyContentView
            }
            if let view {
                made.append(grid.addRow(with: [label, view]))
            } else {
                let row = grid.addRow(with: [separator(), NSGridCell.emptyContentView])
                grid.mergeCells(inHorizontalRange: NSRange(location: 0, length: 2), verticalRange: NSRange(location: grid.numberOfRows - 1, length: 1))
                row.topPadding = 6
                row.bottomPadding = 6
                made.append(row)
            }
        }
        return (grid, made)
    }

    // MARK: General

    private func buildGeneral() -> NSView {
        let labels = [L("Language:"), L("Free memory after:"), L("Pasting:"), L("Settings files:")]
        let first = ceil(labels.map { rowLabel($0).intrinsicContentSize.width }.max() ?? 0)
        let second = SettingsWindow.width - first - 10

        interfacePopup = NSPopUpButton(frame: .zero, pullsDown: false)
        let system = Interface.languages.first { $0.code == Interface.resolve("system") }?.name ?? "English"
        interfacePopup.addItem(withTitle: L("System (%@)", system))
        interfacePopup.lastItem?.representedObject = "system"
        for language in Interface.languages {
            interfacePopup.addItem(withTitle: language.name)
            interfacePopup.lastItem?.representedObject = language.code
        }
        interfacePopup.target = self
        interfacePopup.action = #selector(interfaceChanged)
        let interfaceNote = caption(L("The language of BMeet's menu, its Settings and its notifications. The BMeet window follows it too."), width: second)

        loginBox = checkbox(L("Start BMeet at login"), #selector(loginChanged))
        updateBox = checkbox(L("Check for updates automatically"), #selector(updateCheckChanged))
        let updateNote = caption(L("Once a day BMeet asks GitHub for its list of releases. Nothing about you or your use of BMeet is sent."), width: second)

        unloadPopup = NSPopUpButton(frame: .zero, pullsDown: false)
        for (minutes, title) in [(0, L("Right after use")), (1, L("1 minute")), (5, L("5 minutes")), (15, L("15 minutes")),
                                 (30, L("30 minutes")), (60, L("60 minutes"))] {
            unloadPopup.addItem(withTitle: title)
            unloadPopup.lastItem?.tag = minutes
        }
        unloadPopup.target = self
        unloadPopup.action = #selector(unloadChanged)
        let unloadNote = caption(L("How long a speech model stays in memory after it was last used. Loading Whisper turbo again takes a few seconds, and about a minute the first time after an update."), width: second)

        pasteStatus = NSTextField(labelWithString: "")
        pasteButton = button(L("Allow Pasting…"), #selector(allowPasting))
        let pasteRow = NSStackView(views: [pasteStatus, pasteButton])
        pasteRow.spacing = 8
        let pasteNote = caption(L("BMeet pastes what you dictate with ⌘V, which macOS allows in Privacy & Security → Accessibility. Without it the text stays on the clipboard."), width: second)

        let folder = button(L("Open in Finder"), #selector(openSettingsFolder))
        let folderNote = caption(L("~/.config/bmeet, one file for each setting. BMeet follows changes made there too."), width: second)

        let (grid, rows) = form([
            (labels[0], interfacePopup), (nil, interfaceNote),
            (nil, nil),
            (nil, loginBox), (nil, updateBox), (nil, updateNote),
            (nil, nil),
            (labels[1], unloadPopup), (nil, unloadNote),
            (nil, nil),
            (labels[2], pasteRow), (nil, pasteNote),
            (labels[3], folder), (nil, folderNote),
        ])
        rows[4].topPadding = 2      // (the two switches together)
        rows[12].topPadding = 8
        pasteRows = [rows[10], rows[11]]
        return stack([grid])
    }

    private func fillGeneral() {
        let choice = Settings.interfaceLanguage
        if let item = interfacePopup.itemArray.first(where: { $0.representedObject as? String == choice }) {
            interfacePopup.select(item)
        }
        loginBox.state = Settings.startAtLogin ? .on : .off
        updateBox.state = Settings.updateCheck ? .on : .off
        let minutes = Settings.unloadAfter
        if unloadPopup.indexOfItem(withTag: minutes) < 0 {   // (a value written by hand)
            unloadPopup.addItem(withTitle: L("%d min", minutes))
            unloadPopup.lastItem?.tag = minutes
        }
        unloadPopup.selectItem(withTag: minutes)
        let dictationOn = Feature.dictation.isOn
        for row in pasteRows { row.isHidden = !dictationOn }
        let allowed = AXIsProcessTrusted()
        pasteStatus.stringValue = allowed ? "✓ " + L("Allowed") : L("Not allowed yet")
        pasteButton.isHidden = allowed
    }

    @objc private func interfaceChanged() {
        Settings.set("interface-language", interfacePopup.selectedItem?.representedObject as? String ?? "system")
        app.settingsChanged()
    }

    @objc private func loginChanged() {
        Settings.set("start-at-login", loginBox.state == .on ? "on" : "off")
        app.settingsChanged()
    }

    @objc private func updateCheckChanged() {
        Settings.set("update-check", updateBox.state == .on ? "on" : "off")
        app.settingsChanged()
    }

    @objc private func unloadChanged() {
        Settings.set("unload-after", "\(unloadPopup.selectedItem?.tag ?? 5)")
        app.settingsChanged()
    }

    @objc private func allowPasting() {
        guard !SettingsWindow.offScreen else { return }
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(options)
    }

    @objc private func openSettingsFolder() {
        guard !SettingsWindow.offScreen else { return }
        try? FileManager.default.createDirectory(at: Paths.config, withIntermediateDirectories: true)
        NSWorkspace.shared.open(Paths.config)
    }

    // MARK: Features

    private func buildFeatures() -> NSView {
        let width = SettingsWindow.width - 20
        var views: [NSView] = []
        var gaps: [(NSView, CGFloat)] = []
        func add(_ box: NSButton, _ text: String, last: Bool = false) {
            let note = indent(caption(text, width: width))
            views += [box, note]
            gaps += [(box, 2), (note, last ? 8 : 14)]
        }
        featureBoxes = [:]
        for feature in Feature.allCases {
            let box = checkbox(featureTitle(feature), #selector(featureChanged(_:)))
            box.identifier = NSUserInterfaceItemIdentifier(feature.rawValue)
            featureBoxes[feature] = box
        }
        liveBox = checkbox(L("Show the words while you speak"), #selector(liveChanged))
        liveRow = indent(liveBox)
        speakersBox = checkbox(L("Tell speakers apart"), #selector(speakersChanged))
        voicesBox = checkbox(L("Remember voices"), #selector(voicesChanged))

        add(featureBoxes[.dictation]!, L("Speak, and the text is pasted where you were typing or put on the clipboard. A small panel at the bottom of the screen shows that BMeet listens."))
        views.append(liveRow)
        gaps.append((liveRow, 14))
        add(featureBoxes[.meetings]!, L("Records your microphone and what your Mac plays, and saves a transcript of the call with who said what."))
        add(featureBoxes[.inPersonMeetings]!, L("Records only your microphone, for a meeting in the room, and tells the voices on it apart."))
        add(speakersBox, L("Meeting transcripts get Person 1, Person 2, … instead of only You and Remote. The meeting audio stays in memory until that is done."))
        add(voicesBox, L("Later meetings call the people you named by their names. BMeet keeps a voice fingerprint of each: biometric data about other people, so turn it on only where that is fine. Turning it off forgets them all."))
        add(featureBoxes[.fileTranscription]!, L("Turns audio and video files into text: Transcribe a File… in the menu, the BMeet window and Finder's Quick Actions."), last: true)
        let footnote = caption(L("A feature that is off leaves BMeet's menu and shortcuts. The transcripts you have stay in the BMeet window."), width: SettingsWindow.width)
        let line = separator()
        views += [line, footnote]
        gaps.append((line, 10))
        let s = stack(views)
        for (view, gap) in gaps { s.setCustomSpacing(gap, after: view) }
        return s
    }

    func featureTitle(_ feature: Feature) -> String {
        switch feature {
        case .dictation: return L("Dictation")
        case .meetings: return L("Online meetings")
        case .inPersonMeetings: return L("In-person meetings")
        case .fileTranscription: return L("File transcription")
        }
    }

    private func fillFeatures() {
        for (feature, box) in featureBoxes { box.state = feature.isOn ? .on : .off }
        liveBox.state = Settings.liveText ? .on : .off
        liveRow.isHidden = !Feature.dictation.isOn
        speakersBox.state = Settings.speakers ? .on : .off
        voicesBox.state = Settings.rememberVoices ? .on : .off
    }

    @objc private func featureChanged(_ sender: NSButton) {
        guard let feature = Feature(rawValue: sender.identifier?.rawValue ?? "") else { return }
        feature.set(sender.state == .on)
        app.settingsChanged()
        fillFeatures()
    }

    @objc private func liveChanged() {
        Settings.set("live-dictation", liveBox.state == .on ? "on" : "off")
        app.settingsChanged()
    }

    @objc private func speakersChanged() {
        Settings.set("speakers", speakersBox.state == .on ? "on" : "off")
        app.settingsChanged()
    }

    /// Remembered voices, with the BMeet window's caution: turning them on says what is kept
    /// and asks; turning them off forgets every fingerprint (asked first when there are some).
    @objc private func voicesChanged() {
        let on = voicesBox.state == .on
        voicesBox.state = on ? .off : .on   // (as it was, until the answer)
        let kept = (try? FileManager.default.contentsOfDirectory(atPath: Paths.voices.path))?.isEmpty == false
        let apply = { [weak self] in
            Settings.set("remember-voices", on ? "on" : "off")
            if !on { try? FileManager.default.removeItem(at: Paths.voices) }
            self?.app.settingsChanged()
            self?.fillFeatures()
        }
        if !on && !kept {
            apply()
        } else if on {
            ask(L("Remember voices?"),
                L("BMeet will keep a voice fingerprint of each person in your meetings, 192 numbers from which no audio or words can be recovered, and later meetings call the people you named by their names. Fingerprints are biometric data about other people: turn this on only where that is fine."),
                L("Remember Voices")) { if $0 { apply() } }
        } else {
            ask(L("Forget all remembered voices?"), L("BMeet deletes every voice fingerprint it keeps, your own too. Later meetings number the people again."),
                L("Forget Voices")) { if $0 { apply() } }
        }
    }

    /// A question as a sheet on this window; done(true) for `yes`.
    private func ask(_ question: String, _ text: String, _ yes: String, done: @escaping (Bool) -> Void) {
        if SettingsWindow.offScreen {
            done(SettingsWindow.answers.isEmpty ? false : SettingsWindow.answers.removeFirst())
            return
        }
        let alert = NSAlert()
        alert.messageText = question
        alert.informativeText = text
        alert.addButton(withTitle: yes)
        alert.addButton(withTitle: L("Cancel"))
        alert.beginSheetModal(for: window) { done($0 == .alertFirstButtonReturn) }
    }

    // MARK: Languages

    private func buildLanguages() -> NSView {
        let label = NSTextField(labelWithString: L("Dictation language:"))
        dictationPopup = NSPopUpButton(frame: .zero, pullsDown: false)
        dictationPopup.target = self
        dictationPopup.action = #selector(dictationLanguageChanged)
        let row = NSStackView(views: [label, dictationPopup])
        row.spacing = 8
        let rowNote = caption(L("The language you dictate in now. The language shortcut goes to the next language that is on."), width: SettingsWindow.width)
        search = NSSearchField()
        search.placeholderString = L("Search languages")
        search.delegate = self
        search.translatesAutoresizingMaskIntoConstraints = false
        search.widthAnchor.constraint(equalToConstant: SettingsWindow.width).isActive = true

        table = NSTableView()
        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("language"))
        column.width = SettingsWindow.width - 24
        table.addTableColumn(column)
        table.headerView = nil
        table.rowHeight = 24
        table.selectionHighlightStyle = .none
        table.dataSource = self
        table.delegate = self
        table.usesAlternatingRowBackgroundColors = true
        let scroll = NSScrollView()
        scroll.documentView = table
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            scroll.widthAnchor.constraint(equalToConstant: SettingsWindow.width),
            scroll.heightAnchor.constraint(equalToConstant: 264),
        ])
        let giga = FileManager.default.fileExists(atPath: Paths.models.appendingPathComponent("gigaam-v3-ctc-ru/model.mlmodelc").path)
        languageNote = caption(giga
            ? L("Russian is transcribed with GigaAM v3 (Sber), which makes about 20 % fewer errors than Whisper on Russian speech. Whisper takes the other languages.")
            : L("Russian is transcribed with Whisper here: GigaAM v3 is not installed (the installer's --no-gigaam)."),
            width: SettingsWindow.width)
        let onNote = caption(L("At least one language stays on."), width: SettingsWindow.width)
        let s = stack([row, rowNote, search, scroll, languageNote, onNote])
        s.setCustomSpacing(2, after: row)
        s.setCustomSpacing(12, after: rowNote)
        s.setCustomSpacing(4, after: languageNote)
        return s
    }

    private func fillLanguages() {
        let on = Settings.languages
        dictationPopup.removeAllItems()
        for code in on {
            dictationPopup.addItem(withTitle: Interface.languageName(code))
            dictationPopup.lastItem?.representedObject = code
        }
        if let item = dictationPopup.itemArray.first(where: { $0.representedObject as? String == Settings.language }) {
            dictationPopup.select(item)
        }
        sortRows()
    }

    /// The languages that are on first, in their order (⌃⌥⇧H's), then the others by name; only
    /// the ones the search finds.
    private func sortRows() {
        let on = Settings.languages
        let others = whisperLanguages.filter { !on.contains($0) }.sorted {
            Interface.languageName($0).compare(Interface.languageName($1), locale: Interface.locale) == .orderedAscending
        }
        let words = search.stringValue.trimmingCharacters(in: .whitespaces)
        let english = Locale(identifier: "en")
        rows = (on + others).filter { code in
            words.isEmpty || code.hasPrefix(words.lowercased())
                || Interface.languageName(code).range(of: words, options: [.caseInsensitive, .diacriticInsensitive]) != nil
                || (english.localizedString(forLanguageCode: code) ?? "").range(of: words, options: [.caseInsensitive, .diacriticInsensitive]) != nil
        }
        table.reloadData()
    }

    func controlTextDidChange(_ note: Notification) {
        if note.object as? NSSearchField === search { sortRows() }
    }

    func numberOfRows(in tableView: NSTableView) -> Int { rows.count }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let id = NSUserInterfaceItemIdentifier("languageRow")
        let cell = (tableView.makeView(withIdentifier: id, owner: self) as? LanguageRow) ?? LanguageRow(target: self, action: #selector(languageToggled(_:)))
        cell.identifier = id
        let code = rows[row]
        let on = Settings.languages
        cell.show(code: code, name: Interface.languageName(code), on: on.contains(code), current: code == Settings.language && on.contains(code),
                  last: on == [code])
        return cell
    }

    @objc private func languageToggled(_ sender: NSButton) {
        guard let code = sender.identifier?.rawValue else { return }
        var on = Settings.languages
        if sender.state == .on {
            if !on.contains(code) { on.append(code) }
        } else {
            on.removeAll { $0 == code }
        }
        guard !on.isEmpty else {   // (the last one stays on)
            sender.state = .on
            return
        }
        Settings.setLanguages(on)
        app.settingsChanged()
        let keep = rows
        fillLanguages()
        // (the rows keep their places until the page is shown again: a row does not jump away under the mouse)
        if Set(keep) == Set(rows) { rows = keep; table.reloadData() }
    }

    @objc private func dictationLanguageChanged() {
        guard let code = dictationPopup.selectedItem?.representedObject as? String else { return }
        Settings.set("language", code)
        app.settingsChanged()
        table.reloadData()
    }

    // MARK: Shortcuts

    private func shortcutName(_ action: String) -> String {
        switch action {
        case "dictate": return L("Dictate")
        case "dictate-clipboard": return L("Dictate to the clipboard")
        case "language": return L("Next dictation language")
        case "meeting": return L("Start or stop a meeting")
        case "meeting-in-person": return L("Start or stop an in-person meeting")
        default: return action
        }
    }

    private func buildShortcuts() -> NSView {
        let intro = caption(L("Click a shortcut and press the new keys. ⌫ turns it off, Esc leaves it as it was. The shortcuts work in every app while BMeet runs."), width: SettingsWindow.width)
        recorders = [:]
        notes = [:]
        let grid = NSGridView(numberOfColumns: 3, rows: 0)
        grid.columnSpacing = 10
        grid.rowSpacing = 10
        grid.rowAlignment = .firstBaseline
        grid.column(at: 0).xPlacement = .trailing
        let width = [L("Press the keys…"), L("Off"), "⌃⌥⇧⌘Space"].map { title -> CGFloat in
            let b = NSButton(title: title, target: nil, action: nil)
            b.bezelStyle = .rounded
            return ceil(b.intrinsicContentSize.width)
        }.max() ?? 120
        let names = HotKeys.defaults.map { shortcutName($0.0) }
        let first = ceil(names.map { rowLabel($0).intrinsicContentSize.width }.max() ?? 0)
        let third = SettingsWindow.width - first - width - 2 * grid.columnSpacing
        for (action, _) in HotKeys.defaults {
            let recorder = ShortcutRecorder(hotKey: action)
            recorder.translatesAutoresizingMaskIntoConstraints = false
            recorder.widthAnchor.constraint(equalToConstant: width).isActive = true
            recorder.onStart = { [weak self] in
                self?.shortcutMessage.stringValue = ""
                self?.app.pauseHotKeys(true)
            }
            recorder.onStop = { [weak self] in self?.app.pauseHotKeys(false) }
            recorder.onKeys = { [weak self] combo in self?.shortcutChosen(action, combo) }
            recorder.onRefused = { [weak self] in
                self?.shortcutMessage.stringValue = L("Use ⌃, ⌥ or ⌘ with a letter, a digit or Space, or an F key.")
            }
            let note = NSTextField(wrappingLabelWithString: "")
            note.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
            note.preferredMaxLayoutWidth = third
            note.translatesAutoresizingMaskIntoConstraints = false
            note.widthAnchor.constraint(equalToConstant: third).isActive = true
            recorders[action] = recorder
            notes[action] = note
            grid.addRow(with: [rowLabel(shortcutName(action)), recorder, note])
        }
        grid.column(at: 0).width = first
        shortcutMessage = NSTextField(wrappingLabelWithString: "")
        shortcutMessage.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        shortcutMessage.textColor = .systemRed
        shortcutMessage.preferredMaxLayoutWidth = SettingsWindow.width
        shortcutMessage.translatesAutoresizingMaskIntoConstraints = false
        shortcutMessage.widthAnchor.constraint(equalToConstant: SettingsWindow.width).isActive = true
        restoreButton = button(L("Restore Defaults"), #selector(restoreShortcuts))
        let footnote = caption(L("A feature's shortcuts are off while the feature is off on the Features page. ⌃⌥ with a letter is rarely taken by other apps."), width: SettingsWindow.width)
        let s = stack([intro, grid, shortcutMessage, restoreButton, footnote])
        s.setCustomSpacing(16, after: intro)
        s.setCustomSpacing(10, after: grid)
        s.setCustomSpacing(14, after: restoreButton)
        return s
    }

    private func fillShortcuts() {
        let combos = Dictionary(uniqueKeysWithValues: HotKeys.configured())
        for (action, recorder) in recorders {
            recorder.combo = combos[action] ?? "none"
            let featureOn = Feature.of(hotKey: action)?.isOn != false
            recorder.isEnabled = featureOn
            let note = notes[action]
            if let text = notesForCheck {
                note?.stringValue = text
                note?.textColor = .secondaryLabelColor
            } else if !featureOn {
                note?.stringValue = L("Off on the Features page")
                note?.textColor = .secondaryLabelColor
            } else if app.hotKeys.taken[action] != nil {
                note?.stringValue = L("Taken by another app")
                note?.textColor = .systemRed
            } else {
                note?.stringValue = ""
            }
        }
    }

    /// A shortcut recorded (or "none"): saved, unless another action has the same keys.
    private func shortcutChosen(_ action: String, _ combo: String) {
        var combos = Dictionary(uniqueKeysWithValues: HotKeys.configured())
        if combo != "none", let mine = HotKeys.parse(combo) {
            for (other, _) in HotKeys.defaults where other != action {
                if let theirs = HotKeys.parse(combos[other] ?? "none"), theirs.0 == mine.0, theirs.1 == mine.1 {
                    shortcutMessage.stringValue = L("%@ is already the shortcut for “%@”.", mine.2, shortcutName(other))
                    fillShortcuts()
                    return
                }
            }
        }
        combos[action] = combo
        HotKeys.write(combos)
        shortcutMessage.stringValue = ""
        app.settingsChanged()
        fillShortcuts()
    }

    @objc private func restoreShortcuts() {
        HotKeys.write([:])
        shortcutMessage.stringValue = ""
        app.settingsChanged()
        fillShortcuts()
    }

    // MARK: About

    private func buildAbout() -> NSView {
        // (BMeet.app's icon; a build run from the command line has none of its own)
        let own = Bundle.main.object(forInfoDictionaryKey: "CFBundleIconFile") != nil
        let icon = NSImageView(image: (own ? NSApp.applicationIconImage : NSImage(systemSymbolName: "mic.circle.fill", accessibilityDescription: nil))
                                      ?? NSImage())
        icon.imageScaling = .scaleProportionallyUpOrDown
        icon.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([icon.widthAnchor.constraint(equalToConstant: 64), icon.heightAnchor.constraint(equalToConstant: 64)])
        let name = NSTextField(labelWithString: "BMeet for macOS")
        name.font = .systemFont(ofSize: 17, weight: .semibold)
        let version = NSTextField(labelWithString: L("Version %@", app.version))
        version.textColor = .secondaryLabelColor
        version.isSelectable = true
        let textWidth = SettingsWindow.width - 64 - 14
        let what = caption(L("Dictation, meeting transcripts and file transcription on this Mac's Neural Engine. What you say stays on this Mac."), width: textWidth)
        what.textColor = .labelColor
        what.font = .systemFont(ofSize: NSFont.systemFontSize)
        let texts = NSStackView(views: [name, version, what])
        texts.orientation = .vertical
        texts.alignment = .leading
        texts.spacing = 2
        texts.setCustomSpacing(8, after: version)
        let head = NSStackView(views: [icon, texts])
        head.alignment = .top
        head.spacing = 14

        updateSpinner = NSProgressIndicator()
        updateSpinner.style = .spinning
        updateSpinner.controlSize = .small
        updateSpinner.isDisplayedWhenStopped = false
        updateButton = button(L("Check Now"), #selector(updateClicked))
        let buttonWidth = [L("Check Now"), L("Update…")].map { title -> CGFloat in
            updateButton.title = title
            return ceil(updateButton.intrinsicContentSize.width)
        }.max() ?? 100
        updateButton.translatesAutoresizingMaskIntoConstraints = false
        updateButton.widthAnchor.constraint(equalToConstant: buttonWidth).isActive = true
        let news = button(L("What's New"), #selector(whatsNew))
        let newsWidth = ceil(news.intrinsicContentSize.width)
        updateText = caption("", width: SettingsWindow.width - buttonWidth - newsWidth - 16 - 3 * 8)
        updateText.textColor = .labelColor
        updateText.font = .systemFont(ofSize: NSFont.systemFontSize)
        updateText.isSelectable = true
        let updateRow = NSStackView(views: [updateText, updateSpinner, updateButton, news])
        updateRow.alignment = .centerY
        updateRow.spacing = 8

        let modelsTitle = NSTextField(labelWithString: L("Models and licences"))
        modelsTitle.font = .systemFont(ofSize: NSFont.systemFontSize, weight: .semibold)
        modelsGrid = NSGridView(numberOfColumns: 2, rows: 0)
        modelsGrid.columnSpacing = 16
        modelsGrid.rowSpacing = 3
        let m = Paths.models
        let models: [(String, String, URL?)] = [
            (L("Whisper large-v3-turbo (speech recognition)"), "MIT", m.appendingPathComponent("whisperkit/openai_whisper-large-v3-v20240930_626MB")),
            (L("Whisper small (translation into English)"), "MIT", m.appendingPathComponent("whisperkit/openai_whisper-small_216MB")),
            (L("GigaAM v3 by Sber (Russian)"), "MIT", m.appendingPathComponent("gigaam-v3-ctc-ru/model.mlmodelc")),
            (L("Nemotron 3 Diarization by NVIDIA (who speaks when)"), "OpenMDW-1.1",
             m.appendingPathComponent("speaker-diarization/nemotron-3-diarization/encoder.mlmodelc")),
            (L("TitaNet-small by NVIDIA (voice fingerprints)"), "CC-BY-4.0", m.appendingPathComponent("speaker-diarization/titanet.mlmodelc")),
            (L("WhisperKit by Argmax (Whisper on the Neural Engine)"), "MIT", nil),
            ("BMeet", "MIT", nil),
        ]
        for (title, licence, path) in models where path.map({ FileManager.default.fileExists(atPath: $0.path) }) ?? true {
            let t = NSTextField(labelWithString: title)
            t.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
            let l = NSTextField(labelWithString: licence)
            l.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
            l.textColor = .secondaryLabelColor
            modelsGrid.addRow(with: [t, l])
        }

        let home = NSButton(title: "", target: self, action: #selector(openHome))
        home.isBordered = false
        home.attributedTitle = NSAttributedString(string: "github.com/baakhoff/bmeet",
                                                  attributes: [.foregroundColor: NSColor.linkColor, .font: NSFont.systemFont(ofSize: NSFont.systemFontSize)])
        let logs = button(L("Open Log Folder"), #selector(openLogs))
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let bottom = NSStackView(views: [home, spacer, logs])
        bottom.translatesAutoresizingMaskIntoConstraints = false
        bottom.widthAnchor.constraint(equalToConstant: SettingsWindow.width).isActive = true
        let s = stack([head, separator(), updateRow, separator(), modelsTitle, modelsGrid, separator(), bottom])
        s.spacing = 12
        s.setCustomSpacing(6, after: modelsTitle)
        return s
    }

    /// What BMeet knows about updates now: a look that runs, bmeet-ctl's last answer
    /// (update.json), and the release the status names.
    private var updateState: UpdateState {
        if let updateForCheck { return updateForCheck }
        var state = UpdateState()
        state.checking = app.updateChecking
        if let u = app.status["update"] as? [String: Any], let v = u["version"] as? String {
            state.available = (v, u["url"] as? String ?? "")
        }
        if let data = try? Data(contentsOf: Paths.cache.appendingPathComponent("update.json")),
           let last = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
            state.checked = (last["checked"] as? Double).map { Date(timeIntervalSince1970: $0) }
            if last["ok"] as? Bool == false { state.error = last["error"] as? String }
        }
        if let error = app.updateError { state.error = error }
        return state
    }

    private func fillAboutPage() {
        let state = updateState
        updateButton.isEnabled = !state.checking
        if state.checking {
            updateSpinner.startAnimation(nil)
        } else {
            updateSpinner.stopAnimation(nil)
        }
        if state.checking {
            updateText.stringValue = L("Looking for a newer release…")
            updateButton.title = L("Check Now")
        } else if let available = state.available {
            updateText.stringValue = L("BMeet %@ is available.", available.version)
            updateButton.title = L("Update…")
        } else if let error = state.error {
            updateText.stringValue = L("Could not look for updates: %@", error)
            updateButton.title = L("Check Now")
        } else if let checked = state.checked {
            let format = DateFormatter()
            format.locale = Interface.locale
            format.dateStyle = .medium
            format.timeStyle = .short
            format.doesRelativeDateFormatting = true
            updateText.stringValue = L("BMeet is up to date. Last checked: %@.", format.string(from: checked))
            updateButton.title = L("Check Now")
        } else {
            updateText.stringValue = L("BMeet has not looked for a newer release yet.")
            updateButton.title = L("Check Now")
        }
    }

    @objc private func updateClicked() {
        if updateState.available != nil {
            app.startUpdate()
        } else {
            app.checkUpdate(auto: false)
        }
    }

    /// The new release's notes, else what changed in each release so far.
    @objc private func whatsNew() {
        guard !SettingsWindow.offScreen else { return }
        let url = updateState.available.map(\.url).flatMap { $0.isEmpty ? nil : URL(string: $0) }
            ?? URL(string: "https://github.com/baakhoff/bmeet/blob/main/macos/CHANGELOG.md")!
        NSWorkspace.shared.open(url)
    }

    @objc private func openHome() {
        guard !SettingsWindow.offScreen else { return }
        NSWorkspace.shared.open(URL(string: "https://github.com/baakhoff/bmeet")!)
    }

    @objc private func openLogs() {
        guard !SettingsWindow.offScreen else { return }
        try? FileManager.default.createDirectory(at: Paths.logs, withIntermediateDirectories: true)
        NSWorkspace.shared.open(Paths.logs)
    }
}

/// A row of Settings → Languages: the language's switch with its name, its code, and a mark on
/// the dictation language.
final class LanguageRow: NSTableCellView {
    let box: NSButton
    private let code = NSTextField(labelWithString: "")
    private let mark = NSTextField(labelWithString: "")

    init(target: AnyObject, action: Selector) {
        box = NSButton(checkboxWithTitle: "", target: target, action: action)
        super.init(frame: .zero)
        code.textColor = .secondaryLabelColor
        code.font = .monospacedSystemFont(ofSize: NSFont.smallSystemFontSize, weight: .regular)
        mark.font = .systemFont(ofSize: NSFont.smallSystemFontSize, weight: .medium)
        mark.textColor = .controlAccentColor
        for v in [box, code, mark] {
            v.translatesAutoresizingMaskIntoConstraints = false
            addSubview(v)
        }
        NSLayoutConstraint.activate([
            box.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 6),
            box.centerYAnchor.constraint(equalTo: centerYAnchor),
            code.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 290),
            code.centerYAnchor.constraint(equalTo: centerYAnchor),
            mark.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 336),
            mark.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])
    }

    required init?(coder: NSCoder) { fatalError() }

    func show(code language: String, name: String, on: Bool, current: Bool, last: Bool) {
        box.title = name
        box.identifier = NSUserInterfaceItemIdentifier(language)
        box.state = on ? .on : .off
        box.isEnabled = !last   // (at least one stays on)
        code.stringValue = language
        mark.stringValue = current ? L("dictation language") : ""
    }
}

/// A shortcut in Settings → Shortcuts: click it, then press the keys. ⌫ turns it off, Esc
/// leaves it as it was. BMeet's own shortcuts are let go meanwhile (onStart, onStop).
final class ShortcutRecorder: NSButton {
    let hotKey: String
    var combo = "none" { didSet { refresh() } }
    var onStart: () -> Void = {}
    var onStop: () -> Void = {}
    var onKeys: (String) -> Void = { _ in }
    var onRefused: () -> Void = {}
    private(set) var isRecording = false
    private var monitor: Any?

    init(hotKey: String) {
        self.hotKey = hotKey
        super.init(frame: .zero)
        bezelStyle = .rounded
        setButtonType(.momentaryPushIn)
        target = self
        action = #selector(clicked)
        refresh()
    }

    required init?(coder: NSCoder) { fatalError() }

    private func refresh() {
        title = isRecording ? L("Press the keys…") : combo == "none" ? L("Off") : HotKeys.label(combo)
    }

    @objc private func clicked() { isRecording ? stop() : start() }

    private func start() {
        isRecording = true
        onStart()
        monitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown]) { [weak self] event in
            guard let self, self.isRecording else { return event }
            self.take(event)
            return nil
        }
        refresh()
    }

    func stop() {
        guard isRecording else { return }
        isRecording = false
        if let monitor { NSEvent.removeMonitor(monitor) }
        monitor = nil
        onStop()
        refresh()
    }

    /// A key pressed while recording (also a check's way in).
    func take(_ event: NSEvent) {
        guard isRecording else { return }
        let plain = event.modifierFlags.intersection([.control, .option, .command, .shift]).isEmpty
        if plain && event.keyCode == 0x35 { stop(); return }                            // Esc
        if plain && (event.keyCode == 0x33 || event.keyCode == 0x75) {                   // ⌫, ⌦
            stop()
            onKeys("none")
            return
        }
        guard let pressed = HotKeys.combo(keyCode: event.keyCode, modifiers: event.modifierFlags) else {
            onRefused()   // (a letter alone, or a key BMeet has no name for: still waiting)
            return
        }
        stop()
        onKeys(pressed)
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        if window == nil { stop() }
    }
}
