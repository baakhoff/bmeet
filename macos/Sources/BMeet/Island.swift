// The island: BMeet around the camera housing at the top of the screen, the way the iPhone's
// Dynamic Island shows a call or a timer. macOS has no API for this (ActivityKit is unavailable
// on the Mac, and AppKit knows no housing), so BMeet draws its own: a black, borderless panel
// joined to the housing, above the menu bar and over full-screen apps, on every Space.
//
// - Idle: nothing shows, and the housing stays as it is.
// - Compact: it widens sideways, with a sign on the left and a short text on the right
//   (listening, transcribing, a meeting and its time, the speaker pass, saved, a failure).
// - Words while you speak: it opens downward with them. Where it shows, it takes the place of
//   the dictation panel at the bottom of the screen (Dictation.swift); on a Mac without a
//   housing, or with ~/.config/bmeet/island "off", that panel shows them as before.
// - Open (a click): the state in full, with its controls. A click outside, or Esc, closes it.
//
// It never takes the keyboard focus or activates BMeet, as dictation pastes into the app you were
// typing in: the panel cannot become the key window, and Esc is a Carbon hot key that BMeet holds
// only while the island is open (HotKeys.holdEscape). Clicks outside its shape go through to the
// menu bar and the apps below. A check lays it out off screen for a given screen and housing
// (Checks.swift, --island-check).

import AppKit

/// A screen with a camera housing, and the housing, in global coordinates.
struct IslandGeometry: Equatable {
    var screen: NSRect
    var housing: NSRect

    /// The housing of `screen`: the gap between its two auxiliary top areas, as tall as its safe
    /// area at the top. nil for a screen without one (empty areas, no top inset). Only the areas'
    /// widths are used, so the housing is where it is whatever coordinates the areas are in.
    static func measure(_ screen: NSScreen) -> IslandGeometry? {
        guard let left = screen.auxiliaryTopLeftArea, let right = screen.auxiliaryTopRightArea else { return nil }
        return make(screen: screen.frame, left: left.width, right: right.width, height: screen.safeAreaInsets.top)
    }

    /// A housing `height` tall between areas `left` and `right` points wide at the top of `screen`.
    static func make(screen: NSRect, left: CGFloat, right: CGFloat, height: CGFloat) -> IslandGeometry? {
        let width = screen.width - left - right
        guard height > 0, width > 0, left > 0, right > 0 else { return nil }
        return IslandGeometry(screen: screen, housing: NSRect(x: screen.minX + left, y: screen.maxY - height,
                                                              width: width, height: height))
    }

    /// The screen that has a housing now (the built-in display with its lid open), if one has.
    static func current() -> IslandGeometry? { NSScreen.screens.lazy.compactMap(measure).first }
}

/// What the island shows.
enum IslandState: Equatable {
    case idle
    case listening(Words)
    case transcribing(Words)
    case meeting(started: Double?, paused: Bool, inPerson: Bool)
    /// The speaker pass, and the transcript it will replace.
    case speakers(file: String?)
    case saved(file: String?)
    /// Shown until it has been looked at (opened, then closed).
    case failed(title: String, detail: String, file: String?)

    struct Words: Equatable {
        var language: String
        var final = ""
        var draft = ""
        var live = true
        var clipboard = false
    }

    var name: String {
        switch self {
        case .idle: return "idle"
        case .listening(let w): return w.live && !(w.final + w.draft).isEmpty ? "words" : "listening"
        case .transcribing: return "transcribing"
        case .meeting(_, let paused, let inPerson): return (inPerson ? "in-person" : "meeting") + (paused ? "-paused" : "")
        case .speakers: return "speakers"
        case .saved: return "saved"
        case .failed: return "failed"
        }
    }

    var file: String? {
        switch self {
        case .speakers(let f), .saved(let f), .failed(_, _, let f): return f
        default: return nil
        }
    }
}

/// The island's controls, and what each asks BMeet to do (AppDelegate.islandAction).
enum IslandAction: String, CaseIterable {
    case stop, cancel, pause, resume, stopMeeting = "stop-meeting", open, dismiss

    var title: String {
        switch self {
        case .stop, .stopMeeting: return L("Stop")
        case .cancel: return L("Cancel")
        case .pause: return L("Pause")
        case .resume: return L("Resume")
        case .open: return L("Open")
        case .dismiss: return L("OK")
        }
    }

    var symbol: String {
        switch self {
        case .stop, .stopMeeting: return "stop.fill"
        case .cancel: return "xmark"
        case .pause: return "pause.fill"
        case .resume: return "play.fill"
        case .open: return "doc.text"
        case .dismiss: return "checkmark"
        }
    }
}

/// A state as the island draws it: the sign and short text of its compact form, and what it
/// shows open.
struct IslandContent: Equatable {
    /// A symbol in a color, and the color of the mark inside it (a check mark in its circle).
    enum Sign: Equatable { case none, dot(pulse: Bool), spinner, symbol(String, NSColor, mark: NSColor? = nil) }

    var sign = Sign.none
    var short = ""
    var title = ""
    var detail = ""
    var words: Words?
    var buttons: [IslandAction] = []

    struct Words: Equatable { var final: String; var draft: String }

    init() {}

    init(_ state: IslandState, now: Date = Date()) {
        switch state {
        case .idle:
            break
        case .listening(let w), .transcribing(let w):
            let listening: Bool
            if case .listening = state { listening = true } else { listening = false }
            sign = listening ? .dot(pulse: true) : .spinner
            short = w.language.uppercased()
            title = listening ? L("Listening…") : L("Transcribing…")
            detail = Interface.languageName(w.language) + (w.clipboard ? " · " + L("to the clipboard") : "")
            if w.live, !(w.final + w.draft).isEmpty { words = Words(final: w.final, draft: w.draft) }
            buttons = listening ? [.stop, .cancel] : [.cancel]
        case .meeting(let started, let paused, let inPerson):
            sign = paused ? .symbol("pause.fill", .white) : inPerson ? .symbol("person.2.fill", .systemRed) : .dot(pulse: false)
            short = started.map { Island.clock(now.timeIntervalSince1970 - $0) } ?? ""
            title = inPerson ? L("In-person meeting") : L("Online meeting")
            detail = paused ? L("Paused. Nothing is recorded until you resume.")
                : inPerson ? L("Recording your microphone. The voices are told apart when you stop.")
                : L("Recording your microphone and what your Mac plays.")
            buttons = [paused ? .resume : .pause, .stopMeeting]
        case .speakers(let file):
            sign = .spinner
            short = L("Speakers")
            title = L("Telling speakers apart…")
            detail = L("The transcript is updated in a few minutes.")
            buttons = file != nil ? [.open] : []
        case .saved(let file):
            sign = .symbol("checkmark.circle.fill", .systemGreen, mark: .black)
            short = L("Saved")
            title = L("Transcript saved")
            detail = file.map { URL(fileURLWithPath: $0).lastPathComponent } ?? ""
            buttons = file != nil ? [.open] : []
        case .failed(let failure, let why, let file):
            sign = .symbol("exclamationmark.triangle.fill", .systemYellow, mark: .black)
            short = L("Failed")
            title = failure
            detail = why
            buttons = (file != nil ? [.open] : []) + [.dismiss]
        }
    }

    /// Two contents with the same layout: only their texts differ (a meeting's time, new words).
    func sameLayout(_ other: IslandContent) -> Bool {
        sign == other.sign && buttons == other.buttons && (words == nil) == (other.words == nil)
    }
}

/// The island: what it shows (worked out from what BMeet knows), the panel, the mouse and Esc.
final class Island {
    /// A check: the panel is never shown, nothing moves, no mouse is watched.
    static var offScreen = false
    /// A check's screen and housing (nil: a screen without one), unless it measures this Mac's.
    static var checkGeometry: IslandGeometry?
    static var checkMeasuresScreens = false
    /// How long "Saved" stays before the island closes by itself.
    static let savedFor: TimeInterval = 5

    let view = IslandView()
    private var panel: IslandPanel?
    private(set) var geometry: IslandGeometry?
    private(set) var state: IslandState = .idle
    private(set) var isOpen = false
    private(set) var hover = false
    /// Esc is BMeet's while the island is open (AppDelegate: HotKeys.holdEscape).
    var holdEscape: (Bool) -> Void = { _ in }
    /// A control was clicked: the action, for the state it was shown for.
    var perform: (IslandAction, IslandState) -> Void = { _, _ in }
    /// The screens changed (a display added or removed, another resolution, the lid).
    var onScreens: () -> Void = {}

    // What BMeet knows, which `wanted` makes a state of
    /// A dictation that runs (AppDelegate sets it from Dictation).
    var dictation: IslandState?
    private(set) var meeting: (started: Double?, paused: Bool, inPerson: Bool)?
    private(set) var pending: [String] = []
    private(set) var failure: (title: String, detail: String, file: String?)?
    private(set) var saved: (file: String?, at: Date)?
    /// BMeet's own bmeet-meeting start or stop runs; file transcriptions it started run.
    var meetingRuns = 0
    var fileJobs = 0
    private var meetingSeen = Date.distantPast

    private var monitors: [Any] = []
    private var ticker: Timer?
    private var savedTimer: Timer?

    init() {
        measure()
        view.onClick = { [weak self] in self?.click() }
        view.onAction = { [weak self] action in self?.act(action) }
        view.onSize = { [weak self] size in self?.place(size) }
        view.onClosed = { [weak self] in
            guard let self, case .idle = self.state else { return }
            self.panel?.orderOut(nil)
        }
        if !Island.offScreen {
            NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil,
                                                   queue: .main) { [weak self] _ in
                guard let self else { return }
                self.measure()
                self.onScreens()
            }
        }
    }

    /// The housing measured again: this Mac's screens, or a check's.
    func measure() {
        let was = geometry
        geometry = Island.offScreen && !Island.checkMeasuresScreens ? Island.checkGeometry : IslandGeometry.current()
        guard let g = geometry else {
            if was != nil { present(.idle) }
            return
        }
        view.housing = g.housing.size
        view.maxWidth = min(g.screen.width - 24, 640)
        if g != was, let panel, panel.isVisible { place(view.frame.size) }
    }

    /// Shown on this Mac: a housing, and ~/.config/bmeet/island not "off".
    var shows: Bool { geometry != nil && Settings.island }

    /// What to show now: a dictation, else a meeting, else a failure not yet looked at, else the
    /// speaker pass, else a transcript just saved.
    var wanted: IslandState {
        if let dictation { return dictation }
        if let m = meeting { return .meeting(started: m.started, paused: m.paused, inPerson: m.inPerson) }
        if let f = failure { return .failed(title: f.title, detail: f.detail, file: f.file) }
        if !pending.isEmpty { return .speakers(file: pending.first { FileManager.default.fileExists(atPath: $0) }) }
        if let s = saved { return .saved(file: s.file) }
        return .idle
    }

    func refresh() { present(wanted) }

    // MARK: what BMeet knows

    /// A `bmeet-ctl status`: the meeting, and the speaker passes that run. A pass that has ended
    /// has saved its transcript, or left the line that says the voices were not told apart.
    func follow(status s: [String: Any]) {
        let m = s["meeting"] as? String ?? "idle"
        let now = (s["speakers_pending"] as? [String]) ?? []
        for file in pending where !now.contains(file) { passEnded(file) }
        pending = now
        if m == "recording" || m == "paused" {
            if meeting == nil { saved = nil }   // (a new meeting: the last one's "Saved" is over)
            let started = s["meeting_started"] as? Double ?? (s["meeting_started"] as? Int).map(Double.init)
            meeting = (started, m == "paused", Island.inPerson())
            meetingSeen = Date()
        } else {
            meeting = nil
        }
    }

    /// bmeet-meeting said where it saved a meeting's transcript: its speaker pass replaces it in a
    /// few minutes, or it is the transcript (speakers off).
    func transcriptWritten(_ file: String) {
        if Island.passRuns(for: file) {
            if !pending.contains(file) { pending.append(file) }
        } else if failure == nil {
            saved = (file, Date())
        }
    }

    /// A notification BMeet posts for its programs: one that says something failed (ok false)
    /// is the island's while a meeting runs, stops or has its speakers told apart, and no file
    /// transcription of BMeet's runs.
    func notified(_ title: String, _ body: String, ok: Bool) {
        let meetingTime = meetingRuns > 0 || meeting != nil || !pending.isEmpty || Date().timeIntervalSince(meetingSeen) < 30
        guard !ok, meetingTime, fileJobs == 0 else { return }
        failure = (title, body, failure?.file)
        saved = nil
    }

    private func passEnded(_ file: String) {
        let text = (try? String(contentsOfFile: file, encoding: .utf8)) ?? ""
        if text.contains("- **Speaker pass:** the voices were not told apart") {   // (macos/bmeet-meeting writes it)
            if let f = failure {
                failure = (f.title, f.detail, f.file ?? file)
            } else {
                failure = (L("Speakers not told apart"), L("The transcript stays as it was written during the meeting."), file)
            }
        } else if failure == nil, FileManager.default.fileExists(atPath: file) {
            saved = (file, Date())
        }
    }

    /// The meeting that runs is an in-person one: its meeting.json says so (macos/bmeet-meeting).
    static func inPerson() -> Bool {
        let name = Paths.read(Paths.runtime.appendingPathComponent("meeting-toggle.started"))
        guard !name.isEmpty, !name.contains("/"),
              let data = try? Data(contentsOf: Paths.runtime.appendingPathComponent("meeting-audio/\(name)/meeting.json")),
              let info = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return false }
        return info["in_person"] as? Bool ?? false
    }

    /// A speaker pass works on `file`: a recording's speaker-pass marker names it (bmeet-ctl's
    /// speakers_pending reads the same).
    static func passRuns(for file: String) -> Bool {
        let audio = Paths.runtime.appendingPathComponent("meeting-audio")
        let names = (try? FileManager.default.contentsOfDirectory(atPath: audio.path)) ?? []
        return names.contains { Paths.read(audio.appendingPathComponent("\($0)/speaker-pass")) == file }
    }

    /// 754 seconds as 12:34; an hour and more as 1:02:03.
    static func clock(_ seconds: Double) -> String {
        let s = max(0, Int(seconds))
        return s >= 3600 ? String(format: "%d:%02d:%02d", s / 3600, s / 60 % 60, s % 60)
            : String(format: "%d:%02d", s / 60, s % 60)
    }

    // MARK: showing it

    func present(_ new: IslandState) {
        let next: IslandState = shows ? new : .idle
        let was = state
        state = next
        if case .idle = next {
            setOpen(false)
            hover = false
            stopTimers()
            unwatchMouse()
            if case .idle = was {} else { view.close() }
            return
        }
        let panel = self.panel ?? makePanel()   // (a check draws the view in it, never shown)
        view.show(IslandContent(next), open: isOpen, hover: hover && !isOpen)
        if !Island.offScreen {
            if !panel.isVisible { panel.orderFrontRegardless() }
            watchMouse()
        }
        startTimers()
    }

    /// The island's shape now, in global coordinates (its body: the shoulders that join it to the
    /// top of the screen are left out). nil when nothing shows.
    var frame: NSRect? {
        guard state != .idle, let panel else { return nil }
        let shape = view.shapeRect   // (where the view draws it, in the panel where it is)
        return NSRect(x: panel.frame.minX + shape.minX, y: panel.frame.minY + shape.minY, width: shape.width, height: shape.height)
    }

    private func makePanel() -> IslandPanel {
        let panel = IslandPanel(contentRect: NSRect(origin: .zero, size: view.frame.size),
                                styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: true)
        panel.contentView = view
        self.panel = panel
        place(view.frame.size)
        return panel
    }

    /// The panel as large as the view needs, around the housing, its top at the top of the screen.
    /// AppKit puts a window on whole points, so the panel's edges are whole and the view draws the
    /// island centred on the housing wherever that leaves it (view.centerX).
    private func place(_ size: NSSize) {
        guard let g = geometry, let panel else { return }
        let left = floor(g.housing.midX - size.width / 2), right = ceil(g.housing.midX + size.width / 2)
        panel.setFrame(NSRect(x: left, y: g.screen.maxY - size.height, width: right - left, height: size.height), display: true)
        view.centerX = g.housing.midX - panel.frame.minX
    }

    /// The panel's frame (a check prints it).
    var panelFrame: NSRect? { panel?.frame }

    // MARK: open, closed, hover

    /// A click on the island: it opens, or closes again.
    func click() {
        if isOpen { close() } else if state != .idle { open() }
    }

    func open() {
        guard state != .idle else { return }
        setOpen(true)
        view.show(IslandContent(state), open: true, hover: false)
    }

    /// Closes it when it is open (a click outside, Esc, a control that is done). A failure that was
    /// open has been looked at, and goes; so does a "Saved" whose time is over.
    func close() {
        guard isOpen else { return }
        setOpen(false)
        if case .failed = state {
            failure = nil
            refresh()
        } else if case .saved = state, let s = saved, Date().timeIntervalSince(s.at) >= Island.savedFor {
            saved = nil
            refresh()
        } else {
            view.show(IslandContent(state), open: false, hover: hover)
        }
    }

    func escape() { close() }

    func setHover(_ inside: Bool) {
        guard inside != hover else { return }
        hover = inside
        guard state != .idle, !isOpen else { return }
        view.show(IslandContent(state), open: false, hover: inside)
    }

    private func setOpen(_ open: Bool) {
        guard open != isOpen else { return }
        isOpen = open
        holdEscape(open)
    }

    private func act(_ action: IslandAction) {
        let shown = state
        if action == .dismiss {
            failure = nil
            setOpen(false)
            refresh()
            return
        }
        perform(action, shown)
        // (pause and resume stay open: the island shows the meeting change; the rest are done)
        if action != .pause && action != .resume { close() }
    }

    // MARK: the mouse

    /// Clicks reach the island only inside its shape: elsewhere the panel lets them through to
    /// the menu bar and the apps below. A click elsewhere closes it when it is open.
    private func watchMouse() {
        guard monitors.isEmpty, !Island.offScreen else { return }
        if let m = NSEvent.addGlobalMonitorForEvents(matching: [.mouseMoved, .leftMouseDragged], handler: { [weak self] _ in
            self?.mouseMoved()
        }) { monitors.append(m) }
        if let m = NSEvent.addLocalMonitorForEvents(matching: [.mouseMoved], handler: { [weak self] event in
            self?.mouseMoved()
            return event
        }) { monitors.append(m) }
        if let m = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown, .otherMouseDown], handler: { [weak self] _ in
            self?.close()
        }) { monitors.append(m) }
        mouseMoved()
    }

    private func unwatchMouse() {
        for m in monitors { NSEvent.removeMonitor(m) }
        monitors = []
        panel?.ignoresMouseEvents = true   // (while it shrinks away, clicks go through)
    }

    private func mouseMoved() {
        guard let panel else { return }
        let point = view.convert(panel.convertPoint(fromScreen: NSEvent.mouseLocation), from: nil)
        let inside = view.contains(point)
        panel.ignoresMouseEvents = !inside
        setHover(inside)
    }

    // MARK: time

    /// The spinner's turn (12 a second), a meeting's time (each second), and the end of "Saved".
    private func startTimers() {
        guard !Island.offScreen else { return }
        var meeting = false
        if case .meeting = state { meeting = true }
        let interval: TimeInterval? = view.content.sign == .spinner ? 1.0 / 12 : meeting ? 1 : nil
        if ticker?.timeInterval != interval {
            ticker?.invalidate()
            ticker = interval.map { Timer.scheduledTimer(withTimeInterval: $0, repeats: true) { [weak self] _ in self?.tick() } }
        }
        if case .saved = state, savedTimer == nil {
            savedTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.savedTick() }
        }
    }

    private func stopTimers() {
        ticker?.invalidate()
        ticker = nil
        savedTimer?.invalidate()
        savedTimer = nil
    }

    private func tick() {
        if view.content.sign == .spinner { view.sign.step += 1 }
        if case .meeting = state { view.show(IslandContent(state), open: isOpen, hover: hover && !isOpen) }
    }

    private func savedTick() {
        guard case .saved = state, let s = saved else {
            savedTimer?.invalidate()
            savedTimer = nil
            return
        }
        // (it stays while it is open or the mouse is on it)
        guard Date().timeIntervalSince(s.at) >= Island.savedFor, !isOpen, !hover else { return }
        saved = nil
        savedTimer?.invalidate()
        savedTimer = nil
        refresh()
    }
}

/// The island's window: borderless, never the key or main window, so a click on it never takes the
/// keyboard focus from the app you type in, nor activates BMeet.
final class IslandPanel: NSPanel {
    override init(contentRect: NSRect, styleMask style: NSWindow.StyleMask, backing: NSWindow.BackingStoreType, defer flag: Bool) {
        super.init(contentRect: contentRect, styleMask: style, backing: backing, defer: flag)
        level = NSWindow.Level(rawValue: NSWindow.Level.mainMenu.rawValue + 3)   // (above the menu bar)
        collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle]
        isFloatingPanel = true
        hidesOnDeactivate = false
        becomesKeyOnlyIfNeeded = true
        backgroundColor = .clear
        isOpaque = false
        hasShadow = false
        isMovable = false
        isReleasedWhenClosed = false
        animationBehavior = .none
        acceptsMouseMovedEvents = true
        appearance = NSAppearance(named: .darkAqua)
    }

    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
    /// (macOS keeps windows below the menu bar; the island belongs above it)
    override func constrainFrameRect(_ frameRect: NSRect, to screen: NSScreen?) -> NSRect { frameRect }
}

/// The island's black shape and its contents. Its top is the top of the view, centred on the
/// housing; it moves on a spring (fades only with Reduce motion on) and draws everything itself or
/// in plain labels, so a check can draw it into a picture.
final class IslandView: NSView {
    struct Shape: Equatable {
        var width: CGFloat
        var height: CGFloat
        var radius: CGFloat

        static func mix(_ a: Shape, _ b: Shape, _ p: CGFloat) -> Shape {
            Shape(width: a.width + (b.width - a.width) * p, height: a.height + (b.height - a.height) * p,
                  radius: a.radius + (b.radius - a.radius) * p)
        }
    }

    /// The housing's size: the island never gets smaller than it.
    var housing = NSSize.zero
    /// Where the housing's middle is in the view (Island.place); its middle until the view is placed.
    var centerX: CGFloat? { didSet { if centerX != oldValue { needsLayout = true; needsDisplay = true } } }
    private var cx: CGFloat { centerX ?? bounds.midX }
    /// The widest the island may get.
    var maxWidth: CGFloat = 600
    var onClick: () -> Void = {}
    var onAction: (IslandAction) -> Void = { _ in }
    /// The size the view needs (the panel follows it), and the island has shrunk into the housing.
    var onSize: (NSSize) -> Void = { _ in }
    var onClosed: () -> Void = {}

    private(set) var content = IslandContent()
    private(set) var isOpen = false
    private(set) var hover = false
    /// The shape drawn now, and the one it moves to.
    private(set) var shape = Shape(width: 0, height: 0, radius: 0)
    private(set) var target = Shape(width: 0, height: 0, radius: 0)
    private var from = Shape(width: 0, height: 0, radius: 0)
    private var closing = false
    private var started: CFTimeInterval = 0
    private var spring = (response: 0.42, damping: 0.72)
    private var timer: Timer?
    private var fade: CGFloat = 1

    let sign = IslandSign()
    let short = IslandView.label(NSFont.monospacedDigitSystemFont(ofSize: 13, weight: .semibold))
    let title = IslandView.label(.systemFont(ofSize: 14, weight: .semibold))
    let detail: NSTextField = {
        let label = NSTextField(wrappingLabelWithString: "")
        label.font = .systemFont(ofSize: 12)
        label.textColor = NSColor(white: 1, alpha: 0.62)
        label.maximumNumberOfLines = 3
        return label
    }()
    /// The words while you speak: the newest ones, the oldest cut off at the start (as the
    /// dictation panel does), underlined while they may still change.
    let words: NSTextField = {
        let label = NSTextField(labelWithString: "")
        label.font = .systemFont(ofSize: 15)
        label.lineBreakMode = .byTruncatingHead
        label.maximumNumberOfLines = 2
        label.cell?.truncatesLastVisibleLine = true
        label.identifier = NSUserInterfaceItemIdentifier("words")
        return label
    }()
    private(set) var buttons: [IslandButton] = []

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        for v in [sign, short, title, detail, words] as [NSView] { addSubview(v) }
    }

    required init?(coder: NSCoder) { fatalError() }

    private static func label(_ font: NSFont) -> NSTextField {
        let label = NSTextField(labelWithString: "")
        label.font = font
        label.textColor = .white
        label.lineBreakMode = .byTruncatingTail
        return label
    }

    // The measures follow the housing's height, so an older, lower notch gets a smaller island.
    private var h: CGFloat { housing.height }
    private var pad: CGFloat { max(10, (h * 0.28).rounded()) }
    private var signSize: CGFloat { min(16, (h * 0.36).rounded()) }
    private var shoulder: CGFloat { max(4, (h * 0.16).rounded()) }
    private var compactRadius: CGFloat { (h * 0.42).rounded() }
    private let openRadius: CGFloat = 24
    private let inset: CGFloat = 18
    private let wordsHeight: CGFloat = 40
    private let buttonHeight: CGFloat = 28

    static var reduceMotion: Bool { NSWorkspace.shared.accessibilityDisplayShouldReduceMotion }

    // MARK: what it shows

    func show(_ new: IslandContent, open: Bool, hover: Bool) {
        let rebuild = !new.sameLayout(content) || open != isOpen || closing || shape.width == 0
        closing = false   // (something to show again while it was closing: it grows from where it is)
        alphaValue = 1
        content = new
        isOpen = open
        self.hover = hover
        fill()
        if rebuild { makeButtons() }
        let next = targetShape()
        if rebuild || next != target {
            move(to: next, fadeIn: rebuild, opening: next.height > shape.height || next.width > shape.width)
        } else {
            arrange()
        }
    }

    /// Shrinks into the housing, then says so (the panel goes).
    func close() {
        guard !closing, shape.width > 0 else { return }
        closing = true
        move(to: Shape(width: housing.width, height: housing.height, radius: compactRadius), fadeIn: false, opening: false)
    }

    private func fill() {
        sign.sign = content.sign
        short.stringValue = content.short
        title.stringValue = content.title
        detail.stringValue = content.detail
        let text = NSMutableAttributedString()
        if let w = content.words {
            text.append(NSAttributedString(string: w.final + (w.final.isEmpty || w.draft.isEmpty ? "" : " "),
                                           attributes: [.foregroundColor: NSColor.white]))
            if !w.draft.isEmpty {
                text.append(NSAttributedString(string: w.draft, attributes: [
                    .foregroundColor: NSColor(white: 1, alpha: 0.6), .underlineStyle: NSUnderlineStyle.single.rawValue,
                ]))
            }
        }
        words.attributedStringValue = text
    }

    private func makeButtons() {
        buttons.forEach { $0.removeFromSuperview() }
        buttons = isOpen ? content.buttons.map { IslandButton($0, target: self, action: #selector(clicked(_:))) } : []
        buttons.forEach { addSubview($0) }
    }

    @objc private func clicked(_ sender: IslandButton) { onAction(sender.what) }

    // MARK: sizes

    private var compactWidth: CGFloat {
        let text = content.short
        // (as wide as the text with every digit a 0: a meeting's time does not make it twitch)
        let probe = IslandView.label(short.font!)
        probe.stringValue = text.replacingOccurrences(of: "[0-9]", with: "0", options: .regularExpression)
        let textWidth = ceil(max(short.intrinsicContentSize.width, probe.intrinsicContentSize.width))
        let signWidth: CGFloat = content.sign == .none ? 0 : signSize
        return housing.width + 2 * (pad + max(textWidth, signWidth) + 8)
    }

    private var buttonsWidth: CGFloat {
        buttons.reduce(0) { $0 + $1.width } + CGFloat(max(0, buttons.count - 1)) * 8
    }

    private func targetShape() -> Shape {
        let compact = min(compactWidth, maxWidth)
        if isOpen {
            let inner = max(ceil(title.intrinsicContentSize.width), buttonsWidth, min(ceil(detail.intrinsicContentSize.width), 400), 300)
            let width = min(maxWidth, max(compact, inner + 2 * inset))
            return Shape(width: width, height: openHeight(width), radius: openRadius)
        }
        if content.words != nil {
            return Shape(width: min(maxWidth, max(compact, 440)), height: h + 6 + wordsHeight + 12, radius: openRadius)
        }
        // (hovering grows it a little: it can be clicked)
        return Shape(width: compact + (hover ? 10 : 0), height: h + (hover ? 2 : 0), radius: compactRadius)
    }

    private func detailHeight(_ width: CGFloat) -> CGFloat {
        guard !content.detail.isEmpty, let cell = detail.cell else { return 0 }
        let frameWidth = detail.frame(forAlignmentRect: NSRect(x: 0, y: 0, width: width, height: 10)).width
        detail.preferredMaxLayoutWidth = width
        return ceil(cell.cellSize(forBounds: NSRect(x: 0, y: 0, width: frameWidth, height: 10_000)).height)
    }

    /// `label` where its text is `rect` (a label's frame is wider than its text, by its alignment
    /// insets: set to the text's width alone, AppKit cuts the text short).
    private func place(_ label: NSView, _ rect: NSRect) {
        label.frame = label.frame(forAlignmentRect: rect)
    }

    private func openHeight(_ width: CGFloat) -> CGFloat {
        let inner = width - 2 * inset
        var y = h + 10 + ceil(title.intrinsicContentSize.height)
        if !content.detail.isEmpty { y += 2 + detailHeight(inner) }
        if content.words != nil { y += 8 + wordsHeight }
        if !buttons.isEmpty { y += 14 + buttonHeight }
        return y + 16
    }

    // MARK: motion

    private func move(to next: Shape, fadeIn: Bool, opening: Bool) {
        appearing = shape.width == 0
        if appearing { shape = Shape(width: housing.width, height: housing.height, radius: compactRadius) }
        from = shape
        target = next
        spring = opening ? (0.42, 0.72) : (0.32, 1)
        if fadeIn { fade = 0 }
        resize()
        guard !Island.offScreen, window != nil else { return settle() }
        reduce = IslandView.reduceMotion
        if reduce && !closing { shape = target }   // (no motion: the island and its contents fade)
        started = CACurrentMediaTime()
        if timer == nil {
            let t = Timer(timeInterval: 1.0 / 60, repeats: true) { [weak self] _ in self?.step() }
            RunLoop.main.add(t, forMode: .common)
            timer = t
        }
        step()
    }

    private var appearing = false
    private var reduce = false

    private func step() {
        let t = CACurrentMediaTime() - started
        if reduce {
            let f = CGFloat(min(1, t / 0.2))
            if closing { alphaValue = 1 - f } else if appearing { alphaValue = f }
            if !closing { fade = max(fade, f) }
        } else {
            shape = Shape.mix(from, target, CGFloat(IslandView.spring(t, response: spring.response, damping: spring.damping)))
            fade = closing ? max(0, 1 - CGFloat(t / 0.12)) : min(1, max(fade, CGFloat((t - 0.08) / 0.2)))
        }
        arrange()
        needsDisplay = true
        if t >= (reduce ? 0.2 : spring.response * 1.6) { settle() }
    }

    private func settle() {
        timer?.invalidate()
        timer = nil
        shape = target
        fade = closing ? 0 : 1
        resize()
        arrange()
        needsDisplay = true
        if closing {
            closing = false
            shape = Shape(width: 0, height: 0, radius: 0)
            content = IslandContent()
            onClosed()
            alphaValue = 1
        }
    }

    /// How far a spring has gone from 0 to 1 after `t` seconds (it may pass 1 and come back):
    /// `response` is about one swing, `damping` 1 for none.
    static func spring(_ t: Double, response: Double, damping: Double) -> Double {
        let w = 2 * Double.pi / response
        if damping >= 1 { return 1 - exp(-w * t) * (1 + w * t) }
        let wd = w * (1 - damping * damping).squareRoot()
        return 1 - exp(-damping * w * t) * (cos(wd * t) + damping * w / wd * sin(wd * t))
    }

    /// The view as large as both the shape drawn and the one it goes to, with the shoulders.
    private func resize() {
        let size = NSSize(width: ceil(max(shape.width, target.width) + 2 * shoulder + 4),
                          height: ceil(max(shape.height, target.height) + 2))
        guard size != frame.size else { return }
        if window != nil { onSize(size) } else { setFrameSize(size) }   // (the panel resizes its content view)
    }

    /// The island's body in the view (a check compares the contents with it).
    var shapeRect: NSRect {
        NSRect(x: cx - shape.width / 2, y: bounds.maxY - shape.height, width: shape.width, height: shape.height)
    }

    /// The housing in the view.
    var housingRect: NSRect {
        NSRect(x: cx - housing.width / 2, y: bounds.maxY - housing.height, width: housing.width, height: housing.height)
    }

    // MARK: layout

    /// The contents where the shape is: the sign and the short text beside the housing, the rest
    /// under it, at the width the island is going to (they fade in as it gets there).
    private func arrange() {
        let top = bounds.maxY
        let minX = cx - shape.width / 2, maxX = cx + shape.width / 2
        let band = top - h / 2
        sign.frame = NSRect(x: minX + pad, y: band - signSize / 2, width: signSize, height: signSize)
        let size = short.intrinsicContentSize
        place(short, NSRect(x: maxX - pad - ceil(size.width), y: (band - size.height / 2).rounded(),
                            width: ceil(size.width), height: ceil(size.height)))
        let left = cx - target.width / 2 + inset, inner = target.width - 2 * inset
        var y = top - h - (isOpen ? 10 : 6)
        let open = isOpen && !closing
        title.isHidden = !open
        detail.isHidden = !open || content.detail.isEmpty
        words.isHidden = content.words == nil || closing
        if open {
            let th = ceil(title.intrinsicContentSize.height)
            y -= th
            place(title, NSRect(x: left, y: y, width: inner, height: th))
            if !content.detail.isEmpty {
                let dh = detailHeight(inner)
                y -= 2 + dh
                place(detail, NSRect(x: left, y: y, width: inner, height: dh))
            }
            if content.words != nil { y -= 8 }
        }
        if content.words != nil {
            y -= wordsHeight
            place(words, NSRect(x: left, y: y, width: inner, height: wordsHeight))
        }
        if open, !buttons.isEmpty {
            y -= 14 + buttonHeight
            var x = left
            for b in buttons {
                b.frame = NSRect(x: x, y: y, width: b.width, height: buttonHeight)
                x += b.width + 8
            }
        }
        for v in [title, detail, words] as [NSView] + buttons { v.alphaValue = fade }
        let bandFade = closing ? fade : 1
        sign.alphaValue = bandFade
        short.alphaValue = bandFade
    }

    override func layout() {
        super.layout()
        arrange()
    }

    // MARK: drawing, the mouse

    private func outline() -> NSBezierPath {
        IslandView.outline(shape, centerX: cx, top: bounds.maxY, shoulder: shoulder)
    }

    /// The island's outline: its top along the top of the screen, joined to it by two small inward
    /// curves (the shoulders), its bottom corners round.
    static func outline(_ s: Shape, centerX cx: CGFloat, top: CGFloat, shoulder: CGFloat) -> NSBezierPath {
        let path = NSBezierPath()
        guard s.width > 0, s.height > 0 else { return path }
        let left = cx - s.width / 2, right = cx + s.width / 2, bottom = top - s.height
        let rs = min(shoulder, s.height / 3)
        let r = max(0, min(s.radius, s.width / 2, s.height - rs))
        path.move(to: NSPoint(x: left - rs, y: top))
        path.appendArc(withCenter: NSPoint(x: left - rs, y: top - rs), radius: rs, startAngle: 90, endAngle: 0, clockwise: true)
        path.line(to: NSPoint(x: left, y: bottom + r))
        path.appendArc(withCenter: NSPoint(x: left + r, y: bottom + r), radius: r, startAngle: 180, endAngle: 270)
        path.line(to: NSPoint(x: right - r, y: bottom))
        path.appendArc(withCenter: NSPoint(x: right - r, y: bottom + r), radius: r, startAngle: 270, endAngle: 360)
        path.line(to: NSPoint(x: right, y: top - rs))
        path.appendArc(withCenter: NSPoint(x: right + rs, y: top - rs), radius: rs, startAngle: 180, endAngle: 90, clockwise: true)
        path.close()
        return path
    }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.black.setFill()
        outline().fill()
    }

    /// `point` (in the view) is on the island.
    func contains(_ point: NSPoint) -> Bool { shape.width > 0 && outline().contains(point) }

    override func hitTest(_ point: NSPoint) -> NSView? {
        guard contains(convert(point, from: superview)) else { return nil }
        if let hit = super.hitTest(point), hit is IslandButton { return hit }
        return self
    }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func mouseDown(with event: NSEvent) {
        if contains(convert(event.locationInWindow, from: nil)) { onClick() }
    }

    /// Done now (a check): no motion.
    func layoutNow() {
        if timer != nil { settle() }
        arrange()
    }
}

/// The sign at the left of the island: a red dot (pulsing while BMeet listens), a spinner, or a symbol.
final class IslandSign: NSView {
    var sign: IslandContent.Sign = .none {
        didSet {
            guard sign != oldValue else { return }
            needsDisplay = true
            pulse(sign == .dot(pulse: true))
        }
    }
    /// The spinner's turn (Island ticks it).
    var step = 0 { didSet { needsDisplay = true } }

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
    }

    required init?(coder: NSCoder) { fatalError() }

    private func pulse(_ on: Bool) {
        if on, !Island.offScreen, layer?.animation(forKey: "pulse") == nil {
            let pulse = CABasicAnimation(keyPath: "opacity")
            pulse.fromValue = 1.0
            pulse.toValue = 0.35
            pulse.duration = 0.8
            pulse.autoreverses = true
            pulse.repeatCount = .infinity
            layer?.add(pulse, forKey: "pulse")
        } else if !on {
            layer?.removeAnimation(forKey: "pulse")
        }
    }

    override func draw(_ dirtyRect: NSRect) {
        let size = min(bounds.width, bounds.height)
        let mid = NSPoint(x: bounds.midX, y: bounds.midY)
        switch sign {
        case .none:
            break
        case .dot:
            NSColor.systemRed.setFill()
            let d = (size * 0.62).rounded()
            NSBezierPath(ovalIn: NSRect(x: mid.x - d / 2, y: mid.y - d / 2, width: d, height: d)).fill()
        case .spinner:
            let spokes = 8
            for i in 0..<spokes {
                let angle = CGFloat(i) / CGFloat(spokes) * 2 * .pi
                let fadeStep = (i + step) % spokes
                NSColor(white: 1, alpha: 0.25 + 0.75 * CGFloat(fadeStep) / CGFloat(spokes - 1)).setStroke()
                let line = NSBezierPath()
                line.lineWidth = max(1.5, size * 0.12)
                line.lineCapStyle = .round
                line.move(to: NSPoint(x: mid.x + cos(angle) * size * 0.24, y: mid.y + sin(angle) * size * 0.24))
                line.line(to: NSPoint(x: mid.x + cos(angle) * size * 0.46, y: mid.y + sin(angle) * size * 0.46))
                line.stroke()
            }
        case .symbol(let name, let color, let mark):
            // (a palette's first color is the mark inside, the second its circle or triangle)
            let config = NSImage.SymbolConfiguration(pointSize: size * 0.9, weight: .semibold)
                .applying(NSImage.SymbolConfiguration(paletteColors: mark.map { [$0, color] } ?? [color]))
            guard let image = NSImage(systemSymbolName: name, accessibilityDescription: nil)?.withSymbolConfiguration(config) else { return }
            let s = image.size
            let scale = min(size / s.width, size / s.height, 1)
            let w = s.width * scale, h = s.height * scale
            image.draw(in: NSRect(x: mid.x - w / 2, y: mid.y - h / 2, width: w, height: h))
        }
    }
}

/// A control of the open island: a round-ended button with a symbol and a word. It works with the
/// first click, though the island never becomes the key window.
final class IslandButton: NSButton {
    let what: IslandAction

    init(_ what: IslandAction, target: AnyObject, action: Selector) {
        self.what = what
        super.init(frame: .zero)
        isBordered = false
        setButtonType(.momentaryChange)
        image = NSImage(systemSymbolName: what.symbol, accessibilityDescription: nil)?
            .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 11, weight: .bold))
        imagePosition = .imageLeading
        imageHugsTitle = true
        contentTintColor = .white
        attributedTitle = NSAttributedString(string: what.title, attributes: [
            .foregroundColor: NSColor.white, .font: NSFont.systemFont(ofSize: 13, weight: .medium)])
        focusRingType = .none
        refusesFirstResponder = true
        self.target = target
        self.action = action
        setAccessibilityLabel(what.title)
    }

    required init?(coder: NSCoder) { fatalError() }

    /// Its text and symbol, and room around them.
    var width: CGFloat { ceil(intrinsicContentSize.width) + 26 }

    override func draw(_ dirtyRect: NSRect) {
        let color: NSColor = what == .stopMeeting ? .systemRed : NSColor(white: 1, alpha: 0.16)
        (isHighlighted ? color.blended(withFraction: 0.25, of: .white) ?? color : color).setFill()
        NSBezierPath(roundedRect: bounds, xRadius: bounds.height / 2, yRadius: bounds.height / 2).fill()
        super.draw(dirtyRect)
    }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}
