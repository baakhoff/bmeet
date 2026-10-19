// Dictation: what voxtype's daemon and bmeet-dictate do on Linux.
//
// The engine is the same as on Linux: `npu-transcribe --dictate` (GigaAM for Russian, Whisper
// for other languages, both on the Neural Engine), which takes commands on stdin and tells the
// text as it changes on stdout. It runs as BMeet.app's child, so its recorder (bmeet-record,
// the microphone) is part of BMeet for macOS's permission. It stays loaded between dictations
// and exits after ENGINE_IDLE without one (memory on an 8 GB Mac).
//
// While you speak, a small panel at the bottom of the screen shows the words (underlined while
// they may still change); it never takes the keyboard focus, so the text goes where you were
// typing. When you stop, the text is pasted there (the clipboard, then ⌘V; your clipboard is
// put back afterwards), or only copied with the clipboard shortcut. Pasting needs the
// Accessibility permission for BMeet: without it the text stays on the clipboard.

import AppKit
import ApplicationServices
import Foundation

let ENGINE_IDLE: TimeInterval = 15 * 60
let MAX_DICTATION: TimeInterval = 5 * 60     // as voxtype: a forgotten dictation ends by itself

final class Dictation {
    enum State: String { case idle, recording, transcribing }

    private(set) var state: State = .idle { didSet { onChange(state); writeState() } }
    var onChange: (State) -> Void = { _ in }
    var notify: (String, String) -> Void = { _, _ in }

    private var engine: Process?
    private var input: FileHandle?
    private var buffer = Data()
    private var clipboardOnly = false
    private var idleTimer: Timer?
    private var maxTimer: Timer?
    private let panel = DictationPanel()
    private var final = ""
    private var draft = ""

    init() { writeState() }

    private func writeState() {
        Paths.write(state.rawValue, to: Paths.runtime.appendingPathComponent("state"))
    }

    // MARK: commands

    func toggle(clipboard: Bool) {
        switch state {
        case .idle: start(clipboard: clipboard)
        case .recording: stop()
        case .transcribing: break
        }
    }

    func start(clipboard: Bool) {
        guard state == .idle else { return }
        clipboardOnly = clipboard
        final = ""
        draft = ""
        let language = Settings.language
        if engine?.isRunning != true { startEngine() }
        send(["start": ["language": language]])   // (the engine switches models when the language changes)
        state = .recording
        panel.show(final: "", draft: "", status: "Listening…", live: Settings.liveText)
        idleTimer?.invalidate()
        maxTimer?.invalidate()
        maxTimer = Timer.scheduledTimer(withTimeInterval: MAX_DICTATION, repeats: false) { [weak self] _ in self?.stop() }
    }

    func stop() {
        guard state == .recording else { return }
        maxTimer?.invalidate()
        send(["stop": [:]])
        state = .transcribing
        panel.show(final: final, draft: draft, status: "Transcribing…", live: Settings.liveText)
    }

    func cancel() {
        guard state != .idle else { return }
        send(["cancel": [:]])
        finish()
    }

    private func finish() {
        maxTimer?.invalidate()
        panel.hide()
        state = .idle
        idleTimer?.invalidate()
        idleTimer = Timer.scheduledTimer(withTimeInterval: ENGINE_IDLE, repeats: false) { [weak self] _ in
            self?.stopEngine()
        }
    }

    /// The language changed (bmeet-ctl lang): the next dictation loads its model (the engine keeps one).
    func languageChanged(_ language: String) {}

    // MARK: the engine

    private func startEngine() {
        stopEngine()
        let p = Process()
        p.executableURL = Paths.python
        // (no --language: the engine then loads the model at the first "start", after it has started
        // recording, so what you say while it loads is kept)
        p.arguments = ["-I", Paths.transcribe.path, "--dictate"]
        let stdin = Pipe(), stdout = Pipe()
        p.standardInput = stdin
        p.standardOutput = stdout
        try? FileManager.default.createDirectory(at: Paths.logs, withIntermediateDirectories: true)
        let logURL = Paths.logs.appendingPathComponent("dictation.log")
        if !FileManager.default.fileExists(atPath: logURL.path) { FileManager.default.createFile(atPath: logURL.path, contents: nil) }
        if let log = try? FileHandle(forWritingTo: logURL) {
            log.seekToEndOfFile()
            p.standardError = log
        }
        stdout.fileHandleForReading.readabilityHandler = { [weak self] h in
            let data = h.availableData
            DispatchQueue.main.async { self?.received(data) }
        }
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                guard let self, self.engine === proc else { return }
                self.engine = nil
                if self.state != .idle {
                    self.notify("Dictation stopped", "The speech engine ended (see ~/Library/Logs/BMeet/dictation.log)")
                    self.finish()
                }
            }
        }
        do {
            try p.run()
            engine = p
            input = stdin.fileHandleForWriting
        } catch {
            notify("Dictation failed", "Could not start npu-transcribe: \(error.localizedDescription)")
        }
    }

    func stopEngine() {
        guard let p = engine else { return }
        engine = nil
        try? input?.close()     // (the engine ends when its stdin closes)
        input = nil
        DispatchQueue.global().asyncAfter(deadline: .now() + 3) { if p.isRunning { p.terminate() } }
    }

    private func send(_ cmd: [String: Any]) {
        guard var data = try? JSONSerialization.data(withJSONObject: cmd) else { return }
        data.append(10)
        input?.write(data)
    }

    private func received(_ data: Data) {
        if data.isEmpty { return }
        buffer.append(data)
        while let nl = buffer.firstIndex(of: 10) {
            let line = buffer[buffer.startIndex..<nl]
            buffer.removeSubrange(buffer.startIndex...nl)
            guard let ev = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] else { continue }
            handle(ev)
        }
    }

    private func handle(_ ev: [String: Any]) {
        if let error = ev["error"] as? String {
            notify("Dictation failed", error)
            finish()
        } else if ev["done"] != nil {
            let text = (ev["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            finish()
            if !text.isEmpty { deliver(text) }
        } else if ev["cancelled"] != nil {
            finish()
        } else if let f = ev["final"] as? String {
            final = f
            draft = ev["draft"] as? String ?? ""
            if state != .idle {
                panel.show(final: final, draft: draft, status: state == .recording ? "Listening…" : "Transcribing…",
                           live: Settings.liveText)
            }
        }
    }

    // MARK: the text into the app you were in

    private func deliver(_ text: String) {
        let board = NSPasteboard.general
        if clipboardOnly {
            board.clearContents()
            board.setString(text, forType: .string)
            notify("Dictation copied", String(text.prefix(120)))
            return
        }
        guard AXIsProcessTrusted() else {
            board.clearContents()
            board.setString(text, forType: .string)
            let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
            _ = AXIsProcessTrustedWithOptions(options)
            notify("Dictation copied: press ⌘V", "To paste by itself, allow BMeet in System Settings → Privacy & Security → Accessibility.")
            return
        }
        Paste.paste(text)
    }
}

enum Paste {
    /// Put `text` on the clipboard, press ⌘V in the front app, then give the clipboard back.
    static func paste(_ text: String) {
        let board = NSPasteboard.general
        let saved: [NSPasteboardItem] = (board.pasteboardItems ?? []).map { item in
            let copy = NSPasteboardItem()
            for type in item.types { if let d = item.data(forType: type) { copy.setData(d, forType: type) } }
            return copy
        }
        board.clearContents()
        board.setString(text, forType: .string)
        let ours = board.changeCount
        let source = CGEventSource(stateID: .combinedSessionState)
        let v: CGKeyCode = 0x09
        let down = CGEvent(keyboardEventSource: source, virtualKey: v, keyDown: true)
        let up = CGEvent(keyboardEventSource: source, virtualKey: v, keyDown: false)
        down?.flags = .maskCommand
        up?.flags = .maskCommand
        down?.post(tap: .cghidEventTap)
        up?.post(tap: .cghidEventTap)
        // (give the app time to read it; then the clipboard is yours again, unless something
        // else changed it meanwhile)
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
            guard board.changeCount == ours, !saved.isEmpty else { return }
            board.clearContents()
            board.writeObjects(saved)
        }
    }
}

/// The panel at the bottom of the screen: the words as you say them. It floats over every app
/// and desktop (full-screen ones too), ignores the mouse and never becomes the key window.
final class DictationPanel {
    private var window: NSPanel?
    private let label = NSTextField(labelWithString: "")
    private let status = NSTextField(labelWithString: "")
    private let dot = NSView()

    private func make() -> NSPanel {
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 640, height: 64),
                            styleMask: [.nonactivatingPanel, .borderless], backing: .buffered, defer: true)
        panel.level = .statusBar
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.ignoresMouseEvents = true
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        let effect = NSVisualEffectView()
        effect.material = .hudWindow
        effect.state = .active
        effect.blendingMode = .behindWindow
        effect.wantsLayer = true
        effect.layer?.cornerRadius = 14
        effect.layer?.masksToBounds = true
        panel.contentView = effect
        dot.wantsLayer = true
        dot.layer?.backgroundColor = NSColor.systemRed.cgColor
        dot.layer?.cornerRadius = 5
        status.font = .systemFont(ofSize: 11, weight: .medium)
        status.textColor = .secondaryLabelColor
        label.font = .systemFont(ofSize: 15)
        label.lineBreakMode = .byTruncatingHead
        label.maximumNumberOfLines = 2
        label.cell?.truncatesLastVisibleLine = true
        for v in [dot, status, label] {
            v.translatesAutoresizingMaskIntoConstraints = false
            effect.addSubview(v)
        }
        NSLayoutConstraint.activate([
            dot.widthAnchor.constraint(equalToConstant: 10), dot.heightAnchor.constraint(equalToConstant: 10),
            dot.leadingAnchor.constraint(equalTo: effect.leadingAnchor, constant: 16),
            dot.topAnchor.constraint(equalTo: effect.topAnchor, constant: 13),
            status.leadingAnchor.constraint(equalTo: dot.trailingAnchor, constant: 8),
            status.centerYAnchor.constraint(equalTo: dot.centerYAnchor),
            label.leadingAnchor.constraint(equalTo: effect.leadingAnchor, constant: 16),
            label.trailingAnchor.constraint(equalTo: effect.trailingAnchor, constant: -16),
            label.topAnchor.constraint(equalTo: status.bottomAnchor, constant: 4),
            label.bottomAnchor.constraint(lessThanOrEqualTo: effect.bottomAnchor, constant: -10),
        ])
        return panel
    }

    func show(final: String, draft: String, status text: String, live: Bool) {
        let panel = window ?? make()
        window = panel
        status.stringValue = text
        dot.layer?.backgroundColor = (text == "Listening…" ? NSColor.systemRed : NSColor.systemBlue).cgColor
        let shown = NSMutableAttributedString()
        if live {
            shown.append(NSAttributedString(string: final, attributes: [.foregroundColor: NSColor.labelColor]))
            if !draft.isEmpty {
                shown.append(NSAttributedString(string: (final.isEmpty ? "" : " ") + draft, attributes: [
                    .foregroundColor: NSColor.secondaryLabelColor,
                    .underlineStyle: NSUnderlineStyle.single.rawValue,
                ]))
            }
        }
        label.attributedStringValue = shown
        label.isHidden = shown.length == 0
        let height: CGFloat = shown.length == 0 ? 36 : 74
        if let screen = NSScreen.main {
            let width = min(680, screen.visibleFrame.width - 80)
            let frame = NSRect(x: screen.visibleFrame.midX - width / 2, y: screen.visibleFrame.minY + 48,
                               width: width, height: height)
            panel.setFrame(frame, display: true)
        }
        panel.orderFrontRegardless()
    }

    func hide() { window?.orderOut(nil) }
}
