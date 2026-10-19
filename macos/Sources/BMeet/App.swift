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
    private var timer: Timer?

    func applicationDidFinishLaunching(_ note: Notification) {
        let runtime = Paths.runtime
        try? FileManager.default.createDirectory(at: runtime, withIntermediateDirectories: true,
                                                 attributes: [.posixPermissions: 0o700])
        // One BMeet.app at a time: a second one hands over to the first and quits.
        if let answer = sendToRunning(["op": "open", "args": []]), answer["ok"] as? Bool == true {
            NSApp.terminate(nil)
            return
        }
        Paths.write("\(getpid())", to: runtime.appendingPathComponent("pid"))
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
        run(Paths.python, ["-I", Paths.ctl.path, "status"], capture: true) { [weak self] out in
            guard let self, let data = out?.data(using: .utf8),
                  let s = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }
            self.status = s
            self.refresh()
            self.schedulePoll()
        }
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
        let language = status["language"] as? String ?? Settings.language
        let line: String
        switch (dictation.state, meeting) {
        case (.recording, _): line = "Listening…"
        case (.transcribing, _): line = "Transcribing…"
        case (_, "recording"): line = "Recording: \(status["meeting_title"] as? String ?? "meeting")"
        case (_, "paused"): line = "Paused: \(status["meeting_title"] as? String ?? "meeting")"
        default: line = "Ready · \(language) · Neural Engine"
        }
        menu.addItem(withTitle: line, action: nil, keyEquivalent: "").isEnabled = false
        if let pending = status["speakers_pending"] as? [Any], !pending.isEmpty {
            menu.addItem(withTitle: "Telling speakers apart (\(pending.count))…", action: nil, keyEquivalent: "").isEnabled = false
        }
        menu.addItem(.separator())
        add(dictation.state == .recording ? "Stop dictation" : "Dictate", "dictate", #selector(menuDictate))
        add("Dictate to the clipboard", "dictate-clipboard", #selector(menuDictateClipboard))
        if dictation.state != .idle { add("Cancel dictation", nil, #selector(menuCancel)) }
        menu.addItem(.separator())
        if meeting == "idle" {
            add("Start meeting", "meeting", #selector(menuMeeting))
            add("Start in-person meeting", "meeting-in-person", #selector(menuInPerson))
        } else {
            add("Stop meeting", "meeting", #selector(menuMeeting))
            add(meeting == "paused" ? "Resume meeting" : "Pause meeting", nil, #selector(menuPause))
            if let live = status["meeting_live"] as? String, !live.isEmpty {
                add("Show live transcript", nil, #selector(menuOpenMeetings))
            }
        }
        menu.addItem(.separator())
        let langs = (status["languages"] as? [String]) ?? Settings.languages
        let langItem = NSMenuItem(title: "Dictation language: \(language)", action: nil, keyEquivalent: "")
        let sub = NSMenu()
        for l in langs {
            let i = NSMenuItem(title: l, action: #selector(menuLanguage(_:)), keyEquivalent: "")
            i.target = self
            i.state = l == language ? .on : .off
            i.representedObject = l
            sub.addItem(i)
        }
        sub.addItem(.separator())
        let next = NSMenuItem(title: "Next language", action: #selector(menuNextLanguage), keyEquivalent: "")
        next.target = self
        if let label = hotKeys.labels["language"] { next.title += "  \(label)" }
        sub.addItem(next)
        langItem.submenu = sub
        menu.addItem(langItem)
        menu.addItem(.separator())
        let recentItem = NSMenuItem(title: "Recent", action: nil, keyEquivalent: "")
        let rsub = NSMenu()
        for r in recent.prefix(8) {
            guard let path = r["path"] as? String else { continue }
            let i = NSMenuItem(title: r["title"] as? String ?? URL(fileURLWithPath: path).lastPathComponent,
                               action: #selector(menuOpenFile(_:)), keyEquivalent: "")
            i.target = self
            i.representedObject = path
            rsub.addItem(i)
        }
        if rsub.items.isEmpty { rsub.addItem(withTitle: "Nothing yet", action: nil, keyEquivalent: "").isEnabled = false }
        recentItem.submenu = rsub
        menu.addItem(recentItem)
        add("Open BMeet…", nil, #selector(menuOpenMeetings))
        add("Transcribe a file…", nil, #selector(menuTranscribeFile))
        menu.addItem(.separator())
        let settings = NSMenuItem(title: "Settings", action: nil, keyEquivalent: "")
        let ssub = NSMenu()
        toggleItem(ssub, "Show the text while you speak", Settings.liveText) { Settings.set("live-dictation", $0 ? "on" : "off") }
        toggleItem(ssub, "Tell meeting speakers apart", Settings.speakers) { Settings.set("speakers", $0 ? "on" : "off") }
        toggleItem(ssub, "Start BMeet at login", Settings.startAtLogin) { [weak self] on in
            Settings.set("start-at-login", on ? "on" : "off")
            self?.loginItem()
        }
        ssub.addItem(.separator())
        let ax = NSMenuItem(title: AXIsProcessTrusted() ? "Pasting: allowed" : "Allow pasting (Accessibility)…",
                            action: AXIsProcessTrusted() ? nil : #selector(menuAccessibility), keyEquivalent: "")
        ax.target = self
        ssub.addItem(ax)
        let folder = NSMenuItem(title: "Open the settings folder", action: #selector(menuSettingsFolder), keyEquivalent: "")
        folder.target = self
        ssub.addItem(folder)
        settings.submenu = ssub
        menu.addItem(settings)
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit BMeet", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        loadRecent()
    }

    private func add(_ title: String, _ hotkey: String?, _ action: Selector) {
        let i = NSMenuItem(title: title, action: action, keyEquivalent: "")
        i.target = self
        if let hotkey, let label = hotKeys.labels[hotkey] {
            i.title = title
            i.toolTip = label
            let attributed = NSMutableAttributedString(string: title)
            attributed.append(NSAttributedString(string: "    \(label)", attributes: [.foregroundColor: NSColor.secondaryLabelColor]))
            i.attributedTitle = attributed
        }
        menu.addItem(i)
    }

    private var toggles: [NSMenuItem: (Bool) -> Void] = [:]

    private func toggleItem(_ m: NSMenu, _ title: String, _ value: Bool, _ set: @escaping (Bool) -> Void) {
        let i = NSMenuItem(title: title, action: #selector(menuToggle(_:)), keyEquivalent: "")
        i.target = self
        i.state = value ? .on : .off
        toggles[i] = set
        m.addItem(i)
    }

    private func loadRecent() {
        run(Paths.python, ["-I", Paths.ctl.path, "recent", "--limit", "8"], capture: true) { [weak self] out in
            guard let data = out?.data(using: .utf8),
                  let list = (try? JSONSerialization.jsonObject(with: data)) as? [[String: Any]] else { return }
            self?.recent = list
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
        guard inBundle else {    // (notifications need the app's bundle)
            NSLogLine("notification: \(title) \(body)")
            return
        }
        let center = UNUserNotificationCenter.current()
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            guard granted else { NSLogLine("notification (not allowed): \(title) \(body)"); return }
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
