// BMeet.app: BMeet's menu bar app on macOS. It does what KDE does for BMeet on Linux: the
// shortcuts, the panel widget (here the menu bar icon and its menu), starting meetings and
// dictations, notifications, and Finder's right-click "Transcribe". Everything that records or
// pastes runs as its child, so macOS asks for the microphone, the system audio and Accessibility
// once, for BMeet. The BMeet window (meetings, transcribed files) is the Qt app of Linux, in
// Contents/Helpers/BMeet Window.app. Its own Settings window is SettingsWindow.swift, and the
// island around the camera housing, on a Mac that has one, is Island.swift.

import AppKit
import ServiceManagement
import UserNotifications

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate, UNUserNotificationCenterDelegate {
    private var item: NSStatusItem?
    private let menu = NSMenu()
    private(set) var hotKeys: HotKeys!
    let dictation = Dictation()
    let island = Island()
    private var server: ControlServer!
    var status: [String: Any] = [:]
    private var recent: [[String: Any]] = []
    private weak var recentMenu: NSMenu?    // the Recent Transcripts submenu of the menu last shown
    private var timer: Timer?
    private var updateTimer: Timer?
    private var settingsTimer: Timer?
    private var pidWritten = Date.distantPast
    private var settingsWindow: SettingsWindow?
    /// The settings as BMeet last followed them (settingsChanged).
    private var seen = Settings.Snapshot()
    private var hotKeysPaused = false
    /// A look for updates that runs now, and why the last one asked for failed (the About page).
    private(set) var updateChecking = false
    private(set) var updateError: String?
    /// Quit without asking about a meeting: SIGTERM (main.swift), logging out, a second BMeet.app.
    var quitWithoutAsking = false
    /// A check (Checks.swift): no menu bar icon, no shortcuts taken from the running BMeet, no
    /// notifications, nothing on the screen; the control socket is a test's own.
    let checking: Bool

    init(checking: Bool = false) {
        self.checking = checking
        super.init()
        hotKeys = HotKeys { [weak self] action in self?.hotKey(action) }
        hotKeys.dryRun = checking
        seen = Settings.Snapshot.read()
        island.holdEscape = { [weak self] hold in self?.hotKeys.holdEscape(hold) }
        island.perform = { [weak self] action, state in self?.islandAction(action, state) }
        island.onScreens = { [weak self] in
            self?.refreshIsland()
            self?.dictation.redisplay()
            self?.settingsWindow?.fill()
        }
        dictation.useIsland = { [weak self] in self?.island.shows ?? false }
        dictation.onWords = { [weak self] in self?.refreshIsland() }
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        let runtime = Paths.runtime
        try? FileManager.default.createDirectory(at: runtime, withIntermediateDirectories: true,
                                                 attributes: [.posixPermissions: 0o700])
        // One BMeet.app at a time: a second one hands over to the first and quits.
        if !checking, let answer = sendToRunning(["op": "open", "args": []]), answer["ok"] as? Bool == true {
            quitWithoutAsking = true
            NSApp.terminate(nil)
            return
        }
        if !checking { writePid() }
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
        dictation.onChange = { [weak self] _ in self?.refresh(); self?.schedulePoll() }
        dictation.notify = { [weak self] title, body in self?.post(title, body) }
        seen = Settings.Snapshot.read()
        hotKeys.register()
        // Settings → the files in ~/.config/bmeet; BMeet follows them within 2 s, whoever wrote them
        settingsTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.settingsChanged() }
        if checking {
            Checks.ready()
            return
        }

        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.image = symbol("mic")
        item.button?.imagePosition = .imageLeading
        menu.delegate = self
        item.menu = menu
        self.item = item
        NSLogLine("started (\(version)); "
                  + "shortcuts: " + hotKeys.labels.sorted { $0.key < $1.key }.map { "\($0.key) \($0.value)" }.joined(separator: ", "))
        NSApp.servicesProvider = self
        NSUpdateDynamicServices()
        if inBundle {
            UNUserNotificationCenter.current().delegate = self
            loginItem()
        }
        refresh()
        schedulePoll()
        poll()
        loadRecent()
        // A newer release? bmeet-ctl asks GitHub at most once a day (not with ~/.config/bmeet/update-check
        // "off"); looked at a while after login, then every 6 hours
        DispatchQueue.main.asyncAfter(deadline: .now() + 30) { [weak self] in self?.checkUpdate(auto: true) }
        updateTimer = Timer.scheduledTimer(withTimeInterval: 6 * 3600, repeats: true) { [weak self] _ in
            self?.checkUpdate(auto: true)
        }
    }

    /// BMeet for macOS's version: the bundle's, or macos/VERSION of the program files.
    var version: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
            ?? Paths.read(Paths.src.appendingPathComponent("macos/VERSION"), default: "?")
    }

    /// Quitting BMeet does not stop a meeting: its recorder would go on, without the menu bar
    /// icon that shows it. So BMeet asks first, and stops the meeting (its transcript is saved;
    /// the speaker pass goes on by itself) before it quits.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let meeting = Paths.read(Paths.runtime.appendingPathComponent("meeting_state"), default: "idle")
        guard !quitWithoutAsking, !checking, meeting == "recording" || meeting == "paused" else { return .terminateNow }
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = L("Stop the meeting and quit?")
        alert.informativeText = L("BMeet is recording a meeting. Quitting BMeet would not stop the recording.")
        alert.addButton(withTitle: L("Stop Meeting and Quit"))
        alert.addButton(withTitle: L("Cancel"))
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
        if !checking, Paths.read(pid) == "\(getpid())" { try? FileManager.default.removeItem(at: pid) }
    }

    /// Opening BMeet.app again (Finder, Spotlight, the Dock): the window.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        openWindow([])
        return false
    }

    /// Finder's Open With: the window's Transcribe page with the files.
    func application(_ application: NSApplication, open urls: [URL]) {
        guard filesAllowed() else { return }
        openWindow(["transcribe"] + urls.flatMap { ["--file", $0.path] })
    }

    // MARK: settings

    /// Follows the files in ~/.config/bmeet: called every 2 s, and by the Settings window after
    /// each change. A feature turned off loses its shortcuts at once (and a dictation that runs
    /// is cancelled); another interface language rebuilds an open Settings window.
    func settingsChanged() {
        let now = Settings.Snapshot.read()
        guard now != seen else { return }
        let was = seen
        seen = now
        let newLanguage = now.interfaceLanguage != was.interfaceLanguage && Interface.reload()
        if (now.features != was.features || now.hotkeys != was.hotkeys) && !hotKeysPaused {
            hotKeys.register()
        }
        if was.features[.dictation] == true && now.features[.dictation] == false {
            dictation.cancel()
            dictation.stopEngine()
        }
        if now.unloadAfter != was.unloadAfter { dictation.scheduleIdle() }
        if now.island != was.island { dictation.redisplay() }   // (the words go to the island or the panel)
        if now.startAtLogin != was.startAtLogin && inBundle { loginItem() }
        if now.language != was.language { dictation.languageChanged(now.language) }
        refresh()
        if let window = settingsWindow {
            if newLanguage {
                DispatchQueue.main.async { window.rebuild() }   // (not from inside the control that changed it)
            } else {
                window.fill()
            }
        }
    }

    /// The Settings window on `page` (the menu, About BMeet, the control socket's "settings").
    func showSettings(_ page: SettingsWindow.Page) {
        let window = settingsWindow ?? SettingsWindow(app: self)
        settingsWindow = window
        window.show(page)
    }

    /// The Settings window as it is now, built if it is not (a check).
    var settings: SettingsWindow {
        let window = settingsWindow ?? SettingsWindow(app: self)
        settingsWindow = window
        return window
    }

    /// While Settings → Shortcuts records keys, BMeet's own shortcuts must not go off.
    func pauseHotKeys(_ paused: Bool) {
        hotKeysPaused = paused
        if paused { hotKeys.unregister() } else { hotKeys.register() }
    }

    // MARK: actions

    private func hotKey(_ action: String) {
        guard Feature.of(hotKey: action)?.isOn != false else { return }   // (turned off in the last 2 s)
        guard !checking else { return }   // (--hotkey-check's shortcuts only test the registering)
        switch action {
        case "escape": island.escape()
        case "dictate": dictation.toggle(clipboard: false)
        case "dictate-clipboard": dictation.toggle(clipboard: true)
        case "language": switchLanguage(nil)
        case "meeting": meeting("toggle", inPerson: false)
        case "meeting-in-person": meeting("toggle", inPerson: true)
        default: break
        }
    }

    /// Why this meeting action may not run now, or nil: a kind of meeting that is off is not
    /// started; one that runs can always be stopped, paused and resumed.
    private func meetingRefusal(_ action: String, inPerson: Bool) -> String? {
        let idle = Paths.read(Paths.runtime.appendingPathComponent("meeting_state"), default: "idle") == "idle"
        guard idle, action == "start" || action == "toggle" else { return nil }
        let feature: Feature = inPerson ? .inPersonMeetings : .meetings
        return feature.isOn ? nil : (inPerson ? "in-person meetings are off" : "meetings are off") + " (BMeet's Settings → Features)"
    }

    private func meeting(_ action: String, inPerson: Bool) {
        if let why = meetingRefusal(action, inPerson: inPerson) {
            NSLogLine("meeting \(action): \(why)")
            return
        }
        // Without the microphone macOS records silence: say so when a meeting starts. In person
        // there would be nothing at all to record.
        let idle = Paths.read(Paths.runtime.appendingPathComponent("meeting_state"), default: "idle") == "idle"
        if idle, action == "start" || action == "toggle", Microphone.denied {
            post(inPerson ? L("Meeting not started: BMeet cannot use the microphone") : L("BMeet cannot hear your microphone"),
                 inPerson ? L("Allow BMeet in System Settings → Privacy & Security → Microphone.")
                     : L("Only the other side of the call is recorded. Allow BMeet in System Settings → Privacy & Security → Microphone."))
            if inPerson { return }
        }
        // (bmeet-meeting takes one start or stop at a time itself). A stop prints the transcript's
        // path: the island says it was saved, or shows its speaker pass.
        island.meetingRuns += 1
        run(Paths.python, ["-I", Paths.meeting.path, action] + (inPerson ? ["--in-person"] : []), capture: true,
            log: "meetings.log") { [weak self] out in
            guard let self else { return }
            self.island.meetingRuns -= 1
            if let file = out?.split(separator: "\n").last.map(String.init), file.hasPrefix("/"), file.hasSuffix(".md"),
               FileManager.default.fileExists(atPath: file) {
                self.island.transcriptWritten(file)
                self.refreshIsland()
            }
            self.poll()
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in self?.poll() }
    }

    /// A control of the island was clicked, for the state it showed.
    private func islandAction(_ action: IslandAction, _ state: IslandState) {
        if checking { Checks.islandActions.append(action.rawValue + (action == .open ? " " + (state.file ?? "") : "")) }
        switch action {
        case .stop: dictation.stop()
        case .cancel: dictation.cancel()
        case .pause: meeting("pause", inPerson: false)
        case .resume: meeting("resume", inPerson: false)
        case .stopMeeting: meeting("stop", inPerson: false)
        case .open:
            if let file = state.file, !Checks.active { NSWorkspace.shared.open(URL(fileURLWithPath: file)) }
        case .dismiss: break
        }
    }

    private func switchLanguage(_ language: String?) {
        run(Paths.python, ["-I", Paths.ctl.path, "lang", language ?? "toggle"]) { [weak self] _ in
            guard let self else { return }
            let now = Settings.language
            self.dictation.languageChanged(now)
            self.post(L("Dictation language: %@", Interface.languageName(now)), "")
            self.poll()
        }
    }

    func openWindow(_ args: [String]) {
        guard !Checks.active else { return NSLogLine("would open the window: \(args)") }
        let app = Paths.windowApp
        guard FileManager.default.fileExists(atPath: app.path) else {
            post(L("The BMeet window is missing"), L("Run the installer again."))
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

    /// File transcription is on; else a notification says it is off (Finder's services and Open
    /// With stay in Finder: macOS takes them from BMeet.app's Info.plist, see docs/macos.md).
    private func filesAllowed() -> Bool {
        guard !Feature.fileTranscription.isOn else { return true }
        post(L("File transcription is off"), L("Turn it on in BMeet's Settings → Features."))
        return false
    }

    private func transcribe(_ files: [String], _ extra: [String]) {
        guard !files.isEmpty, filesAllowed() else { return }
        island.fileJobs += 1   // (its notifications are not the island's)
        run(Paths.python, ["-I", Paths.transcribe.path, "--notify"] + extra + files, log: "transcribe.log") { [weak self] _ in
            self?.island.fileJobs -= 1
        }
        post(files.count == 1 ? L("Transcribing %@", URL(fileURLWithPath: files[0]).lastPathComponent)
                              : L("Transcribing files: %d", files.count),
             L("You get a notification when it is done; the transcript goes next to the file."))
    }

    // MARK: the control socket

    func control(_ req: [String: Any]) -> [String: Any] {
        let answer = handle(req)
        if checking { Checks.handled(req, answer, by: self) }
        return answer
    }

    private func handle(_ req: [String: Any]) -> [String: Any] {
        switch req["op"] as? String ?? "" {
        case "ping":
            return ["ok": true, "pid": Int(getpid())]
        case "dictate":
            guard Feature.dictation.isOn else { return ["ok": false, "error": "dictation is off (BMeet's Settings → Features)"] }
            dictation.toggle(clipboard: req["clipboard"] as? Bool ?? false)
        case "cancel":
            dictation.cancel()
        case "meeting":
            let action = req["action"] as? String ?? "toggle", inPerson = req["in_person"] as? Bool ?? false
            if let why = meetingRefusal(action, inPerson: inPerson) { return ["ok": false, "error": why] }
            meeting(action, inPerson: inPerson)
        case "language":
            dictation.languageChanged(req["language"] as? String ?? Settings.language)
            poll()
        case "open":
            openWindow(req["args"] as? [String] ?? [])
        case "settings":
            showSettings(SettingsWindow.Page(rawValue: req["page"] as? String ?? "") ?? .general)
        case "notify":
            let title = req["title"] as? String ?? "BMeet", body = req["body"] as? String ?? ""
            post(title, body)
            island.notified(title, body, ok: req["ok"] as? Bool ?? true)   // (a failure of a meeting's: the island too)
            refreshIsland()
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
        guard !checking else { return }
        let meeting = status["meeting"] as? String ?? "idle"
        let pending = (status["speakers_pending"] as? [Any])?.isEmpty == false
        let busy = dictation.state != .idle || meeting != "idle" || pending
        let interval: TimeInterval = busy ? 2 : 15
        if timer?.timeInterval == interval { return }
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in self?.poll() }
    }

    private func poll() {
        guard !checking else { return }
        writePid()
        run(Paths.python, ["-I", Paths.ctl.path, "status"], capture: true) { [weak self] out in
            guard let self, let data = out?.data(using: .utf8),
                  let s = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }
            // (a meeting or a speaker pass ended: Recent Transcripts has a new one)
            let meetingChanged = (s["meeting"] as? String) != (self.status["meeting"] as? String)
            let passes: Int? = (s["speakers_pending"] as? [Any])?.count
            let passesChanged = passes != (self.status["speakers_pending"] as? [Any])?.count
            let ended = !self.status.isEmpty && (meetingChanged || passesChanged)
            let updateChanged = (s["update"] as? [String: Any])?["version"] as? String
                != (self.status["update"] as? [String: Any])?["version"] as? String
            self.statusArrived(s)
            self.schedulePoll()
            if ended { self.loadRecent() }
            if updateChanged { self.settingsWindow?.fillAbout() }
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

    /// The menu bar icon's symbol, color and text (the meeting's time).
    func icon() -> (name: String, tint: NSColor?, title: String) {
        let meeting = status["meeting"] as? String ?? "idle"
        let pending = (status["speakers_pending"] as? [Any])?.isEmpty == false
        // (the microphone while anything that records is on; with file transcription alone, a waveform)
        let records = Feature.dictation.isOn || Feature.meetings.isOn || Feature.inPersonMeetings.isOn
        var name = records ? "mic" : "waveform", tint: NSColor? = nil, title = ""
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
        return (name, tint, title)
    }

    /// A `bmeet-ctl status` answer (the poll; a check's file): the menu bar icon and the island follow it.
    func statusArrived(_ s: [String: Any]) {
        status = s
        island.follow(status: s)
        refresh()
    }

    private func refresh() {
        refreshIsland()
        guard let button = item?.button else { return }
        let (name, tint, title) = icon()
        button.image = symbol(name)
        button.contentTintColor = tint
        button.title = title
    }

    /// The island shows what BMeet does now: a dictation, with its words, else what it made of the
    /// status, the notifications and the transcripts.
    func refreshIsland() {
        let words = IslandState.Words(language: Settings.language, final: dictation.final, draft: dictation.draft,
                                      live: Settings.liveText, clipboard: dictation.clipboardOnly)
        switch dictation.state {
        case .recording: island.dictation = .listening(words)
        case .transcribing: island.dictation = .transcribing(words)
        case .idle: island.dictation = nil
        }
        island.refresh()
    }

    private func symbol(_ name: String) -> NSImage? {
        let img = NSImage(systemSymbolName: name, accessibilityDescription: "BMeet")
        img?.isTemplate = true
        return img
    }

    // MARK: the menu

    func menuNeedsUpdate(_ menu: NSMenu) {
        build(menu)
        loadRecent()
    }

    /// The menu as the settings and the state have it now: a feature that is off has no items.
    func build(_ menu: NSMenu) {
        menu.removeAllItems()
        let meeting = status["meeting"] as? String ?? "idle"
        let language = (status["language"] as? String ?? Settings.language).uppercased()
        let dictationOn = Feature.dictation.isOn, meetingsOn = Feature.meetings.isOn
        let inPersonOn = Feature.inPersonMeetings.isOn, filesOn = Feature.fileTranscription.isOn
        let line: String, dot: NSColor
        switch (dictation.state, meeting) {
        case (.recording, _): (line, dot) = (L("Listening…"), .systemRed)
        case (.transcribing, _): (line, dot) = (L("Transcribing…"), .systemOrange)
        case (_, "recording"): (line, dot) = (status["meeting_title"] as? String ?? L("Recording a meeting"), .systemRed)
        case (_, "paused"): (line, dot) = (L("Paused: %@", status["meeting_title"] as? String ?? L("meeting")), .systemOrange)
        default:
            (line, dot) = dictationOn || meetingsOn || inPersonOn || filesOn ? (L("Ready"), .systemGreen)
                : (L("Every feature is off"), .systemGray)
        }
        let header = NSMenuItem(title: line, action: nil, keyEquivalent: "")
        header.image = AppDelegate.dot(dot)
        header.attributedTitle = NSAttributedString(string: line, attributes: [.font: NSFont.menuFont(ofSize: 13)])
        header.isEnabled = false
        menu.addItem(header)
        let engine = dictationOn ? L("%@ · on the Neural Engine", language) : L("On the Neural Engine")
        let detail = NSMenuItem(title: engine, action: nil, keyEquivalent: "")
        detail.attributedTitle = NSAttributedString(string: engine, attributes: [
            .font: NSFont.menuFont(ofSize: 11), .foregroundColor: NSColor.secondaryLabelColor])
        detail.indentationLevel = 1
        detail.isEnabled = false
        menu.addItem(detail)
        if let pending = status["speakers_pending"] as? [Any], !pending.isEmpty {
            let i = add(menu, L("Telling speakers apart…"), "person.2.wave.2", nil, nil)
            i.isEnabled = false
        }
        if let update = status["update"] as? [String: Any], let version = update["version"] as? String {
            add(menu, L("Update to BMeet %@…", version), "arrow.down.circle", nil, #selector(menuUpdate))
        }
        menu.addItem(.separator())
        if dictationOn {
            if dictation.state == .recording {
                add(menu, L("Stop Dictation"), "stop.circle", "dictate", #selector(menuDictate))
            } else {
                add(menu, L("Dictate"), "mic", "dictate", #selector(menuDictate))
            }
            add(menu, L("Dictate to Clipboard"), "doc.on.clipboard", "dictate-clipboard", #selector(menuDictateClipboard))
            if dictation.state != .idle { add(menu, L("Cancel Dictation"), "xmark.circle", nil, #selector(menuCancel)) }
            menu.addItem(.separator())
        }
        // (a meeting that runs can be stopped whatever the switches say; only starting one follows them)
        if meeting != "idle" {
            add(menu, L("Stop Meeting"), "stop.circle", "meeting", #selector(menuMeeting))
            add(menu, meeting == "paused" ? L("Resume Meeting") : L("Pause Meeting"),
                meeting == "paused" ? "play.circle" : "pause.circle", nil, #selector(menuPause))
            if let live = status["meeting_live"] as? String, !live.isEmpty {
                add(menu, L("Show Live Transcript"), "text.bubble", nil, #selector(menuOpenMeetings))
            }
            menu.addItem(.separator())
        } else if meetingsOn || inPersonOn {
            if meetingsOn { add(menu, L("Start Meeting"), "record.circle", "meeting", #selector(menuMeeting)) }
            if inPersonOn { add(menu, L("Start In-Person Meeting"), "person.3", "meeting-in-person", #selector(menuInPerson)) }
            menu.addItem(.separator())
        }
        if dictationOn {
            let langs = (status["languages"] as? [String]) ?? Settings.languages
            let langItem = add(menu, L("Dictation Language"), "globe", nil, nil)
            let sub = NSMenu()
            for l in langs {
                let i = NSMenuItem(title: "\(Interface.languageName(l)) (\(l))", action: #selector(menuLanguage(_:)), keyEquivalent: "")
                i.target = self
                i.state = l.uppercased() == language ? .on : .off
                i.representedObject = l
                sub.addItem(i)
            }
            sub.addItem(.separator())
            let next = NSMenuItem(title: L("Next Language"), action: #selector(menuNextLanguage), keyEquivalent: "")
            next.target = self
            if let k = hotKeys.menuKeys["language"] { next.keyEquivalent = k.key; next.keyEquivalentModifierMask = k.mods }
            sub.addItem(next)
            langItem.submenu = sub
        }
        let recentItem = add(menu, L("Recent Transcripts"), "clock", nil, nil)
        let rsub = NSMenu()
        fillRecent(rsub)
        recentItem.submenu = rsub
        recentMenu = rsub
        menu.addItem(.separator())
        add(menu, L("Open BMeet…"), "macwindow", nil, #selector(menuOpenMeetings))
        if filesOn { add(menu, L("Transcribe a File…"), "waveform.badge.plus", nil, #selector(menuTranscribeFile)) }
        let settings = add(menu, L("Settings…"), "gearshape", nil, #selector(menuSettings))
        settings.keyEquivalent = ","
        settings.keyEquivalentModifierMask = .command
        menu.addItem(.separator())
        add(menu, L("Check for Updates…"), nil, nil, #selector(menuCheckUpdate))
        add(menu, L("About BMeet"), nil, nil, #selector(menuAbout))
        let quit = NSMenuItem(title: L("Quit BMeet"), action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        menu.addItem(quit)
    }

    /// A menu item with an SF Symbol, and the global shortcut of `hotkey` shown the Mac way.
    @discardableResult
    private func add(_ menu: NSMenu, _ title: String, _ symbol: String?, _ hotkey: String?, _ action: Selector?) -> NSMenuItem {
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
        if rsub.items.isEmpty { rsub.addItem(withTitle: L("Nothing yet"), action: nil, keyEquivalent: "").isEnabled = false }
    }

    private func loadRecent() {
        guard !checking else { return }
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
        if let p = sender.representedObject as? String, !Checks.active { NSWorkspace.shared.open(URL(fileURLWithPath: p)) }
    }
    @objc private func menuTranscribeFile() { openWindow(["transcribe"]) }
    @objc private func menuSettings() { showSettings(.general) }
    @objc private func menuAbout() { showSettings(.about) }
    @objc private func menuCheckUpdate() { checkUpdate(auto: false) }
    @objc private func menuUpdate() { startUpdate() }

    /// The update runs in Terminal (`bmeet-ctl update --terminal`): the installer quits BMeet and
    /// opens the new one at the end, and it may ask there (Homebrew, the Command Line Tools).
    func startUpdate() {
        guard !checking else { return }
        run(Paths.python, ["-I", Paths.ctl.path, "update", "--terminal"], log: "update.log") { _ in }
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
        guard filesAllowed() else { return }
        openWindow(["transcribe"] + files(board).flatMap { ["--file", $0] })
    }

    private func files(_ board: NSPasteboard) -> [String] {
        (board.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL] ?? []).map(\.path)
    }

    // MARK: notifications, login

    /// Run from BMeet.app (not the bare executable of a build or a test).
    private var inBundle: Bool {
        !checking && !Checks.active && Bundle.main.bundleIdentifier != nil && ProcessInfo.processInfo.environment["BMEET_NO_NOTIFY"] == nil
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

    // MARK: updates

    /// bmeet-ctl looks for a newer release of BMeet for macOS on GitHub (`auto`: when it is due).
    /// The menu shows one from the status; a notification says so once per release, and every
    /// time you asked (Check for Updates…, Check Now), with what it found.
    func checkUpdate(auto: Bool) {
        guard !checking else { return }
        if !auto {
            updateChecking = true
            settingsWindow?.fillAbout()
        }
        run(Paths.python, ["-I", Paths.ctl.path, "update", "--check", "--json"] + (auto ? ["--auto"] : []),
            capture: true) { [weak self] out in
            guard let self else { return }
            var answer: [String: Any]?
            if let data = out?.data(using: .utf8) {
                answer = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            }
            let version = (answer?["update"] as? [String: Any])?["version"] as? String
            if auto {
                if let version, answer?["new"] as? Bool == true {
                    self.post(L("BMeet %@ is available", version), L("Update from BMeet's menu (the microphone in the menu bar)."))
                }
            } else {
                self.updateChecking = false
                self.updateError = answer == nil ? L("bmeet-ctl did not answer") : answer?["error"] as? String
                if let answer, answer["error"] as? String == nil {
                    if let version {
                        self.post(L("BMeet %@ is available", version), L("Choose Update to BMeet %@… in BMeet's menu.", version))
                    } else {
                        self.post(L("BMeet is up to date"), L("BMeet %@ is the newest release.", answer["installed"] as? String ?? ""))
                    }
                } else {
                    self.post(L("Could not look for updates"), self.updateError ?? "")
                }
            }
            self.poll()
            self.settingsWindow?.fillAbout()
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
