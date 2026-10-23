// BMeet.app: BMeet's menu bar app on macOS. It does what KDE does for BMeet on Linux: the
// shortcuts, the panel widget (here the menu bar icon and its menu), starting meetings and
// dictations, notifications, and Finder's right-click "Transcribe". Everything that records or
// pastes runs as its child, so macOS asks for the microphone, the system audio and Accessibility
// once, for BMeet. The BMeet window (meetings, transcribed files) is the Qt app of Linux, in
// Contents/Helpers/BMeet Window.app.

import AppKit
import ServiceManagement
import UserNotifications

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate, UNUserNotificationCenterDelegate {
    private var item: NSStatusItem!
    private let menu = NSMenu()
    private var hotKeys: HotKeys!
    private let dictation = Dictation()
    private var server: ControlServer!
    private var status: [String: Any] = [:]
    private var recent: [[String: Any]] = []
    private weak var recentMenu: NSMenu?    // the Recent Transcripts submenu of the menu last shown
    private var timer: Timer?
    private var pidWritten = Date.distantPast
    /// Quit without asking about a meeting: SIGTERM (main.swift), logging out, a second BMeet.app.
    var quitWithoutAsking = false

    func applicationDidFinishLaunching(_ note: Notification) {
        let runtime = Paths.runtime
        try? FileManager.default.createDirectory(at: runtime, withIntermediateDirectories: true,
                                                 attributes: [.posixPermissions: 0o700])
        // One BMeet.app at a time: a second one hands over to the first and quits.
        if let answer = sendToRunning(["op": "open", "args": []]), answer["ok"] as? Bool == true {
            quitWithoutAsking = true
            NSApp.terminate(nil)
            return
        }
        writePid()
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.willPowerOffNotification, object: nil,
                                                          queue: .main) { [weak self] _ in
            self?.quitWithoutAsking = true
            // (a logout that goes ahead quits BMeet long before; one that was cancelled asks again)
            DispatchQueue.main.asyncAfter(deadline: .now() + 60) { [weak self] in self?.quitWithoutAsking = false }
        }
        server = ControlServer(path: runtime.appendingPathComponent("agent.sock"), handler: { [weak self] req in
            self?.control(req) ?? ["ok": false]
        })
        if !server.start() { NSLogLine("cannot listen on \(runtime.appendingPathComponent("agent.sock").path)") }

        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.image = symbol("mic")
        item.button?.imagePosition = .imageLeading
        menu.delegate = self
        item.menu = menu
        dictation.onChange = { [weak self] _ in self?.refresh(); self?.schedulePoll() }
        dictation.notify = { [weak self] title, body in self?.post(title, body) }

        hotKeys = HotKeys { [weak self] action in self?.hotKey(action) }
        hotKeys.register()
        NSLogLine("started (\(Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?")); "
                  + "shortcuts: " + hotKeys.labels.sorted { $0.key < $1.key }.map { "\($0.key) \($0.value)" }.joined(separator: ", "))
        NSApp.servicesProvider = self
        NSUpdateDynamicServices()
        if inBundle {
            UNUserNotificationCenter.current().delegate = self
            loginItem()
        }
        schedulePoll()
        poll()
        loadRecent()
    }

    /// Quitting BMeet does not stop a meeting: its recorder would go on, without the menu bar
    /// icon that shows it. So BMeet asks first, and stops the meeting (its transcript is saved;
    /// the speaker pass goes on by itself) before it quits.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let meeting = Paths.read(Paths.runtime.appendingPathComponent("meeting_state"), default: "idle")
        guard !quitWithoutAsking, meeting == "recording" || meeting == "paused" else { return .terminateNow }
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Stop the meeting and quit?"
        alert.informativeText = "BMeet is recording a meeting. Quitting BMeet would not stop the recording."
        alert.addButton(withTitle: "Stop Meeting and Quit")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return .terminateCancel }
        run(Paths.python, ["-I", Paths.meeting.path, "stop"], log: "meetings.log") { _ in
            DispatchQueue.main.async { NSApp.reply(toApplicationShouldTerminate: true) }
        }
        // (a stop that hangs does not keep BMeet from quitting)
        DispatchQueue.main.asyncAfter(deadline: .now() + 120) { NSApp.reply(toApplicationShouldTerminate: true) }
        return .terminateLater
    }

    func applicationWillTerminate(_ note: Notification) {
        dictation.cancel()
        dictation.stopEngine()
        server?.stop()
        let pid = Paths.runtime.appendingPathComponent("pid")
        if Paths.read(pid) == "\(getpid())" { try? FileManager.default.removeItem(at: pid) }
    }

    /// Opening BMeet.app again (Finder, Spotlight, the Dock): the window.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        openWindow([])
        return false
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        openWindow(["transcribe"] + urls.flatMap { ["--file", $0.path] })
    }

    // MARK: actions

    private func hotKey(_ action: String) {
        switch action {
        case "dictate": dictation.toggle(clipboard: false)
        case "dictate-clipboard": dictation.toggle(clipboard: true)
        case "language": switchLanguage(nil)
        case "meeting": meeting("toggle", inPerson: false)
        case "meeting-in-person": meeting("toggle", inPerson: true)
        default: break
        }
    }

    private func meeting(_ action: String, inPerson: Bool) {
        // Without the microphone macOS records silence: say so when a meeting starts. In person
        // there would be nothing at all to record.
        let idle = Paths.read(Paths.runtime.appendingPathComponent("meeting_state"), default: "idle") == "idle"
        if idle, action == "start" || action == "toggle", Microphone.denied {
            post(inPerson ? "Meeting not started: BMeet cannot use the microphone" : "BMeet cannot hear your microphone",
                 (inPerson ? "" : "Only the other side of the call is recorded. ")
                 + "Allow BMeet in System Settings → Privacy & Security → Microphone.")
            if inPerson { return }
        }
        // (bmeet-meeting takes one start or stop at a time itself)
        run(Paths.python, ["-I", Paths.meeting.path, action] + (inPerson ? ["--in-person"] : []), log: "meetings.log") {
            [weak self] _ in
            self?.poll()
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in self?.poll() }
    }

    private func switchLanguage(_ language: String?) {
        run(Paths.python, ["-I", Paths.ctl.path, "lang", language ?? "toggle"]) { [weak self] _ in
            guard let self else { return }
            let now = Settings.language
            self.dictation.languageChanged(now)
            self.post("Dictation language: \(now)", "")
            self.poll()
        }
    }

    func openWindow(_ args: [String]) {
        let app = Paths.windowApp
        guard FileManager.default.fileExists(atPath: app.path) else {
            post("The BMeet window is missing", "Run the installer again")
            return
        }
        let config = NSWorkspace.OpenConfiguration()
        config.arguments = args
        config.activates = true
        // (a running window takes the arguments of a second one over its own socket)
        config.createsNewApplicationInstance = !args.isEmpty && isWindowRunning()
        NSWorkspace.shared.openApplication(at: app, configuration: config) { _, error in
            if let error { NSLogLine("cannot open the window: \(error.localizedDescription)") }
        }
    }

    private func isWindowRunning() -> Bool {
        !NSRunningApplication.runningApplications(withBundleIdentifier: "io.github.baakhoff.bmeet.window").isEmpty
    }

    private func transcribe(_ files: [String], _ extra: [String]) {
        guard !files.isEmpty else { return }
        run(Paths.python, ["-I", Paths.transcribe.path, "--notify"] + extra + files, log: "transcribe.log") { _ in }
        post("Transcribing \(files.count == 1 ? URL(fileURLWithPath: files[0]).lastPathComponent : "\(files.count) files")",
             "You get a notification when it is done; the transcript goes next to the file.")
    }

    // MARK: the control socket

    private func control(_ req: [String: Any]) -> [String: Any] {
        switch req["op"] as? String ?? "" {
        case "ping":
            return ["ok": true, "pid": Int(getpid())]
        case "dictate":
            dictation.toggle(clipboard: req["clipboard"] as? Bool ?? false)
        case "cancel":
            dictation.cancel()
        case "meeting":
            meeting(req["action"] as? String ?? "toggle", inPerson: req["in_person"] as? Bool ?? false)
        case "language":
            dictation.languageChanged(req["language"] as? String ?? Settings.language)
            poll()
        case "open":
            openWindow(req["args"] as? [String] ?? [])
        case "notify":
            post(req["title"] as? String ?? "BMeet", req["body"] as? String ?? "")
        case "status":
            return ["ok": true, "dictation": dictation.state.rawValue, "accessibility": AXIsProcessTrusted(),
                    "pid": Int(getpid())]
        default:
            return ["ok": false, "error": "unknown op"]
        }
        return ["ok": true]
    }

    private func sendToRunning(_ req: [String: Any]) -> [String: Any]? {
        let path = Paths.runtime.appendingPathComponent("agent.sock").path
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        _ = withUnsafeMutablePointer(to: &addr.sun_path) { p in
            path.withCString { strncpy(UnsafeMutableRawPointer(p).assumingMemoryBound(to: CChar.self), $0, 103) }
        }
        let ok = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        guard ok == 0, var data = try? JSONSerialization.data(withJSONObject: req) else { return nil }
        data.append(10)
        data.withUnsafeBytes { _ = write(fd, $0.baseAddress, $0.count) }
        var bytes: [UInt8] = []
        var c: UInt8 = 0
        while read(fd, &c, 1) == 1, c != 10 { bytes.append(c) }
        return (try? JSONSerialization.jsonObject(with: Data(bytes))) as? [String: Any]
    }

    // MARK: state -> menu bar

    /// Every 2 s while something happens (a dictation, a meeting, a speaker pass: the menu bar
    /// shows it), else every 15 s: each look runs bmeet-ctl, a Python process.
    private func schedulePoll() {
        let meeting = status["meeting"] as? String ?? "idle"
        let pending = (status["speakers_pending"] as? [Any])?.isEmpty == false
        let busy = dictation.state != .idle || meeting != "idle" || pending
        let interval: TimeInterval = busy ? 2 : 15
        if timer?.timeInterval == interval { return }
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in self?.poll() }
    }

    private func poll() {
        writePid()
        run(Paths.python, ["-I", Paths.ctl.path, "status"], capture: true) { [weak self] out in
            guard let self, let data = out?.data(using: .utf8),
                  let s = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }
            // (a meeting or a speaker pass ended: Recent Transcripts has a new one)
            let meetingChanged = (s["meeting"] as? String) != (self.status["meeting"] as? String)
            let passes: Int? = (s["speakers_pending"] as? [Any])?.count
            let passesChanged = passes != (self.status["speakers_pending"] as? [Any])?.count
            let ended = !self.status.isEmpty && (meetingChanged || passesChanged)
            self.status = s
            self.refresh()
            self.schedulePoll()
            if ended { self.loadRecent() }
        }
    }

    /// RUN/pid, which bmeet-ctl and the BMeet window look for. macOS deletes what nothing has
    /// touched for 3 days from the temp folder, and BMeet runs for weeks: so it writes the file
    /// anew (a new file) every hour, and at once when it is gone.
    private func writePid() {
        let pid = Paths.runtime.appendingPathComponent("pid")
        guard Date().timeIntervalSince(pidWritten) > 3600 || Paths.read(pid) != "\(getpid())" else { return }
        Paths.write("\(getpid())", to: pid)
        pidWritten = Date()
    }

    private func refresh() {
        guard let button = item?.button else { return }
        let meeting = status["meeting"] as? String ?? "idle"
        let pending = (status["speakers_pending"] as? [Any])?.isEmpty == false
        var name = "mic", tint: NSColor? = nil, title = ""
        switch dictation.state {
        case .recording: name = "mic.fill"; tint = .systemRed
        case .transcribing: name = "waveform"; tint = .systemBlue
        case .idle:
            if meeting == "recording" {
                name = "record.circle.fill"; tint = .systemRed
            } else if meeting == "paused" {
                name = "pause.circle"
            } else if pending {
                name = "person.2.wave.2"
            }
        }
        if meeting == "recording" || meeting == "paused" {
            if let started = status["meeting_started"] as? Double ?? (status["meeting_started"] as? Int).map(Double.init) {
                let s = Int(Date().timeIntervalSince1970 - started)
                title = String(format: " %d:%02d", s / 60, s % 60)
            }
        }
        button.image = symbol(name)
        button.contentTintColor = tint
        button.title = title
    }

    private func symbol(_ name: String) -> NSImage? {
        let img = NSImage(systemSymbolName: name, accessibilityDescription: "BMeet")
        img?.isTemplate = true
        return img
    }

    // MARK: the menu

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let meeting = status["meeting"] as? String ?? "idle"
        let language = (status["language"] as? String ?? Settings.language).uppercased()
        let line: String, dot: NSColor
        switch (dictation.state, meeting) {
        case (.recording, _): (line, dot) = ("Listening…", .systemRed)
        case (.transcribing, _): (line, dot) = ("Transcribing…", .systemOrange)
        case (_, "recording"): (line, dot) = (status["meeting_title"] as? String ?? "Recording a meeting", .systemRed)
        case (_, "paused"): (line, dot) = ("Paused: " + (status["meeting_title"] as? String ?? "meeting"), .systemOrange)
        default: (line, dot) = ("Ready", .systemGreen)
        }
        let header = NSMenuItem(title: line, action: nil, keyEquivalent: "")
        header.image = AppDelegate.dot(dot)
        header.attributedTitle = NSAttributedString(string: line, attributes: [.font: NSFont.menuFont(ofSize: 13)])
        header.isEnabled = false
        menu.addItem(header)
        let detail = NSMenuItem(title: "\(language) · on the Neural Engine", action: nil, keyEquivalent: "")
        detail.attributedTitle = NSAttributedString(string: "\(language) · on the Neural Engine", attributes: [
            .font: NSFont.menuFont(ofSize: 11), .foregroundColor: NSColor.secondaryLabelColor])
        detail.indentationLevel = 1
        detail.isEnabled = false
        menu.addItem(detail)
        if let pending = status["speakers_pending"] as? [Any], !pending.isEmpty {
            let i = add("Telling speakers apart…", "person.2.wave.2", nil, nil)
            i.isEnabled = false
        }
        menu.addItem(.separator())
        if dictation.state == .recording {
            add("Stop Dictation", "stop.circle", "dictate", #selector(menuDictate))
        } else {
            add("Dictate", "mic", "dictate", #selector(menuDictate))
        }
        add("Dictate to Clipboard", "doc.on.clipboard", "dictate-clipboard", #selector(menuDictateClipboard))
        if dictation.state != .idle { add("Cancel Dictation", "xmark.circle", nil, #selector(menuCancel)) }
        menu.addItem(.separator())
        if meeting == "idle" {
            add("Start Meeting", "record.circle", "meeting", #selector(menuMeeting))
            add("Start In-Person Meeting", "person.3", "meeting-in-person", #selector(menuInPerson))
        } else {
            add("Stop Meeting", "stop.circle", "meeting", #selector(menuMeeting))
            add(meeting == "paused" ? "Resume Meeting" : "Pause Meeting", meeting == "paused" ? "play.circle" : "pause.circle",
                nil, #selector(menuPause))
            if let live = status["meeting_live"] as? String, !live.isEmpty {
                add("Show Live Transcript", "text.bubble", nil, #selector(menuOpenMeetings))
            }
        }
        menu.addItem(.separator())
        let langs = (status["languages"] as? [String]) ?? Settings.languages
        let langItem = add("Dictation Language", "globe", nil, nil)
        let sub = NSMenu()
        for l in langs {
            let i = NSMenuItem(title: Locale.current.localizedString(forLanguageCode: l).map { "\($0) (\(l))" } ?? l,
                               action: #selector(menuLanguage(_:)), keyEquivalent: "")
            i.target = self
            i.state = l.uppercased() == language ? .on : .off
            i.representedObject = l
            sub.addItem(i)
        }
        sub.addItem(.separator())
        let next = NSMenuItem(title: "Next Language", action: #selector(menuNextLanguage), keyEquivalent: "")
        next.target = self
        if let k = hotKeys.menuKeys["language"] { next.keyEquivalent = k.key; next.keyEquivalentModifierMask = k.mods }
        sub.addItem(next)
        langItem.submenu = sub
        let recentItem = add("Recent Transcripts", "clock", nil, nil)
        let rsub = NSMenu()
        fillRecent(rsub)
        recentItem.submenu = rsub
        recentMenu = rsub
        menu.addItem(.separator())
        add("Open BMeet…", "macwindow", nil, #selector(menuOpenMeetings))
        add("Transcribe a File…", "waveform.badge.plus", nil, #selector(menuTranscribeFile))
        let settings = add("Settings", "gearshape", nil, nil)
        let ssub = NSMenu()
        toggleItem(ssub, "Show the Words While You Speak", Settings.liveText) { Settings.set("live-dictation", $0 ? "on" : "off") }
        toggleItem(ssub, "Tell Meeting Speakers Apart", Settings.speakers) { Settings.set("speakers", $0 ? "on" : "off") }
        toggleItem(ssub, "Start BMeet at Login", Settings.startAtLogin) { [weak self] on in
            Settings.set("start-at-login", on ? "on" : "off")
            self?.loginItem()
        }
        ssub.addItem(.separator())
        let ax = NSMenuItem(title: AXIsProcessTrusted() ? "Pasting Allowed" : "Allow Pasting (Accessibility)…",
                            action: AXIsProcessTrusted() ? nil : #selector(menuAccessibility), keyEquivalent: "")
        ax.target = self
        ax.image = NSImage(systemSymbolName: AXIsProcessTrusted() ? "checkmark.circle" : "hand.raised",
                           accessibilityDescription: nil)
        ssub.addItem(ax)
        let folder = NSMenuItem(title: "Open Settings Folder", action: #selector(menuSettingsFolder), keyEquivalent: "")
        folder.target = self
        folder.image = NSImage(systemSymbolName: "folder", accessibilityDescription: nil)
        ssub.addItem(folder)
        settings.submenu = ssub
        menu.addItem(.separator())
        add("About BMeet", nil, nil, #selector(menuAbout))
        let quit = NSMenuItem(title: "Quit BMeet", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        menu.addItem(quit)
        loadRecent()
    }

    /// A menu item with an SF Symbol, and the global shortcut of `hotkey` shown the Mac way.
    @discardableResult
    private func add(_ title: String, _ symbol: String?, _ hotkey: String?, _ action: Selector?) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: action, keyEquivalent: "")
        i.target = self
        if let symbol { i.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil) }
        if let hotkey, let k = hotKeys.menuKeys[hotkey] {
            i.keyEquivalent = k.key
            i.keyEquivalentModifierMask = k.mods
        }
        menu.addItem(i)
        return i
    }

    static func dot(_ color: NSColor) -> NSImage {
        NSImage(size: NSSize(width: 10, height: 10), flipped: false) { r in
            color.setFill()
            NSBezierPath(ovalIn: r.insetBy(dx: 1, dy: 1)).fill()
            return true
        }
    }

    @objc private func menuAbout() {
        NSApp.activate(ignoringOtherApps: true)
        NSApp.orderFrontStandardAboutPanel(options: [
            .credits: NSAttributedString(string: "Dictation, meeting transcripts and file transcription on this Mac's Neural Engine: "
                                         + "what you say is not uploaded. Meeting transcripts are saved in ~/Documents/Meetings, "
                                         + "which iCloud Drive uploads when Desktop & Documents sync is on.\ngithub.com/baakhoff/bmeet",
                                         attributes: [.font: NSFont.systemFont(ofSize: 11), .foregroundColor: NSColor.secondaryLabelColor]),
        ])
    }

    private var toggles: [NSMenuItem: (Bool) -> Void] = [:]

    private func toggleItem(_ m: NSMenu, _ title: String, _ value: Bool, _ set: @escaping (Bool) -> Void) {
        let i = NSMenuItem(title: title, action: #selector(menuToggle(_:)), keyEquivalent: "")
        i.target = self
        i.state = value ? .on : .off
        toggles[i] = set
        m.addItem(i)
    }

    private func fillRecent(_ rsub: NSMenu) {
        rsub.removeAllItems()
        for r in recent.prefix(8) {
            guard let path = r["path"] as? String else { continue }
            let i = NSMenuItem(title: r["title"] as? String ?? URL(fileURLWithPath: path).lastPathComponent,
                               action: #selector(menuOpenFile(_:)), keyEquivalent: "")
            i.target = self
            i.representedObject = path
            i.image = NSImage(systemSymbolName: (r["kind"] as? String) == "meeting" ? "person.2" : "doc.text",
                              accessibilityDescription: nil)
            rsub.addItem(i)
        }
        if rsub.items.isEmpty { rsub.addItem(withTitle: "Nothing yet", action: nil, keyEquivalent: "").isEnabled = false }
    }

    private func loadRecent() {
        run(Paths.python, ["-I", Paths.ctl.path, "recent", "--limit", "8"], capture: true) { [weak self] out in
            guard let self, let data = out?.data(using: .utf8),
                  let list = (try? JSONSerialization.jsonObject(with: data)) as? [[String: Any]] else { return }
            self.recent = list
            if let m = self.recentMenu { self.fillRecent(m) }   // (the menu may be open: it shows them now)
        }
    }

    @objc private func menuDictate() { dictation.toggle(clipboard: false) }
    @objc private func menuDictateClipboard() { dictation.toggle(clipboard: true) }
    @objc private func menuCancel() { dictation.cancel() }
    @objc private func menuMeeting() { meeting("toggle", inPerson: false) }
    @objc private func menuInPerson() { meeting("start", inPerson: true) }
    @objc private func menuPause() { meeting(status["meeting"] as? String == "paused" ? "resume" : "pause", inPerson: false) }
    @objc private func menuLanguage(_ sender: NSMenuItem) { switchLanguage(sender.representedObject as? String) }
    @objc private func menuNextLanguage() { switchLanguage(nil) }
    @objc private func menuOpenMeetings() { openWindow(["meetings"]) }
    @objc private func menuOpenFile(_ sender: NSMenuItem) {
        if let p = sender.representedObject as? String { NSWorkspace.shared.open(URL(fileURLWithPath: p)) }
    }
    @objc private func menuTranscribeFile() { openWindow(["transcribe"]) }
    @objc private func menuToggle(_ sender: NSMenuItem) { toggles[sender]?(sender.state != .on) }
    @objc private func menuSettingsFolder() {
        try? FileManager.default.createDirectory(at: Paths.config, withIntermediateDirectories: true)
        NSWorkspace.shared.open(Paths.config)
    }
    @objc private func menuAccessibility() {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(options)
    }

    // MARK: Finder: right-click > Quick Actions / Services

    @objc func transcribeFiles(_ board: NSPasteboard, userData: String, error: AutoreleasingUnsafeMutablePointer<NSString>) {
        transcribe(files(board), [])
    }

    @objc func transcribeWithSpeakers(_ board: NSPasteboard, userData: String, error: AutoreleasingUnsafeMutablePointer<NSString>) {
        transcribe(files(board), ["--speakers"])
    }

    @objc func translateFiles(_ board: NSPasteboard, userData: String, error: AutoreleasingUnsafeMutablePointer<NSString>) {
        transcribe(files(board), ["--translate"])
    }

    @objc func openInBMeet(_ board: NSPasteboard, userData: String, error: AutoreleasingUnsafeMutablePointer<NSString>) {
        openWindow(["transcribe"] + files(board).flatMap { ["--file", $0] })
    }

    private func files(_ board: NSPasteboard) -> [String] {
        (board.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL] ?? []).map(\.path)
    }

    // MARK: notifications, login

    /// Run from BMeet.app (not the bare executable of a build or a test).
    private var inBundle: Bool {
        Bundle.main.bundleIdentifier != nil && ProcessInfo.processInfo.environment["BMEET_NO_NOTIFY"] == nil
    }

    func post(_ title: String, _ body: String) {
        // (the log gets the title only: a body can hold what you dictated)
        guard inBundle else {    // (notifications need the app's bundle)
            NSLogLine("notification: \(title)")
            return
        }
        let center = UNUserNotificationCenter.current()
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            guard granted else { NSLogLine("notification (not allowed): \(title)"); return }
            let content = UNMutableNotificationContent()
            content.title = title
            content.body = body
            center.add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent n: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .list])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        openWindow([])
        done()
    }

    private func loginItem() {
        let service = SMAppService.mainApp
        do {
            if Settings.startAtLogin, service.status != .enabled {
                try service.register()
            } else if !Settings.startAtLogin, service.status == .enabled {
                try service.unregister()
            }
        } catch {
            NSLogLine("start at login: \(error.localizedDescription)")
        }
    }

    // MARK: running BMeet's programs

    /// `exe args` as BMeet's child (so it records as part of BMeet); done(stdout if captured).
    private func run(_ exe: URL, _ args: [String], capture: Bool = false, log: String? = nil,
                     done: @escaping (String?) -> Void) {
        let p = Process()
        p.executableURL = exe
        p.arguments = args
        p.standardInput = FileHandle.nullDevice
        let out = Pipe()
        if capture { p.standardOutput = out } else { p.standardOutput = FileHandle.nullDevice }
        if let log {
            try? FileManager.default.createDirectory(at: Paths.logs, withIntermediateDirectories: true)
            let url = Paths.logs.appendingPathComponent(log)
            if !FileManager.default.fileExists(atPath: url.path) { FileManager.default.createFile(atPath: url.path, contents: nil) }
            if let h = try? FileHandle(forWritingTo: url) { h.seekToEndOfFile(); p.standardError = h }
        } else {
            p.standardError = FileHandle.nullDevice
        }
        p.terminationHandler = { _ in
            let text = capture ? String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) : nil
            DispatchQueue.main.async { done(text) }
        }
        do { try p.run() } catch {
            NSLogLine("cannot run \(exe.path): \(error.localizedDescription)")
            done(nil)
        }
    }
}
