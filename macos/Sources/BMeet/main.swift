// BMeet.app's entry point (see App.swift).
import AppKit

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)   // a menu bar app: no Dock icon
// SIGTERM (the installer updating it, `kill`): quit as from the menu, so a dictation is
// cancelled and the socket and pid file are cleaned up
signal(SIGTERM, SIG_IGN)
let term = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
term.setEventHandler { NSApp.terminate(nil) }
term.resume()
app.run()
