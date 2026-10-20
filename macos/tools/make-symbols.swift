// make-symbols OUT_DIR - the BMeet window's icons on a Mac: SF Symbols, drawn on this Mac.
//
// The window (bmeet/app/bmeet, the Qt app of Linux) asks for freedesktop theme icons
// ("edit-delete", "media-record", ...), which a Mac does not have. This draws the SF Symbol that
// means the same for each of them, as OUT_DIR/NAME.png for light mode and NAME-dark.png for
// dark mode (64 px: 32 pt at 2x), in the grays of macOS's own toolbar icons. macos/install.sh
// runs it once; the app finds them in macos/icons. (They are drawn here, not kept in the
// repository: SF Symbols are Apple's, for use on Apple's systems.)
import AppKit

let symbols: [String: String] = [
    "audio-input-microphone": "mic", "meeting-attending": "person.2", "view-media-lyrics": "doc.text",
    "document-import": "waveform.badge.plus", "document-open": "folder.badge.plus", "document-open-folder": "folder",
    "edit-delete": "trash", "list-remove": "minus.circle", "edit-find": "magnifyingglass",
    "media-playback-start": "play.fill", "media-playback-stop": "stop.fill", "media-playback-pause": "pause.fill",
    "media-record": "record.circle", "process-stop": "xmark.circle", "view-refresh": "arrow.clockwise",
    "edit-copy": "doc.on.doc", "document-export": "square.and.arrow.up", "document-save": "square.and.arrow.down",
    "edit-clear-history": "xmark.bin", "preferences-desktop-locale": "globe", "im-user": "person.text.rectangle",
    "group": "person.3", "video-x-generic": "film", "audio-x-generic": "waveform",
    "media-view-subtitles-symbolic": "captions.bubble", "dialog-warning": "exclamationmark.triangle.fill",
    "dialog-error": "xmark.octagon.fill", "dialog-information": "info.circle.fill", "paused": "pause.circle",
]
// (the status icons keep their meaning's color, as in macOS's own alerts)
let tints: [String: NSColor] = ["dialog-warning": .systemOrange, "dialog-error": .systemRed,
                                "dialog-information": .systemBlue, "media-record": .systemRed]

let out = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icons")
try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
let px = 64
var missing: [String] = []

func draw(_ symbol: NSImage, _ color: NSColor) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let size = symbol.size
    let scale = min(CGFloat(px) * 0.84 / size.width, CGFloat(px) * 0.84 / size.height)
    let w = size.width * scale, h = size.height * scale
    let rect = NSRect(x: (CGFloat(px) - w) / 2, y: (CGFloat(px) - h) / 2, width: w, height: h)
    let tinted = NSImage(size: size, flipped: false) { r in
        symbol.draw(in: r)
        color.set()
        r.fill(using: .sourceAtop)
        return true
    }
    tinted.draw(in: rect)
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

for (name, symbolName) in symbols.sorted(by: { $0.key < $1.key }) {
    let config = NSImage.SymbolConfiguration(pointSize: 48, weight: .regular)
    guard let symbol = NSImage(systemSymbolName: symbolName, accessibilityDescription: nil)?.withSymbolConfiguration(config) else {
        missing.append("\(name) (\(symbolName))")
        continue
    }
    let light = tints[name] ?? NSColor(calibratedWhite: 0.0, alpha: 0.72)
    let dark = tints[name] ?? NSColor(calibratedWhite: 1.0, alpha: 0.80)
    try! draw(symbol, light).write(to: out.appendingPathComponent("\(name).png"))
    try! draw(symbol, dark).write(to: out.appendingPathComponent("\(name)-dark.png"))
}
print("\(symbols.count - missing.count) icons in \(out.path)" + (missing.isEmpty ? "" : "; not on this Mac: " + missing.joined(separator: ", ")))
