// BMeet.app's entry point (see App.swift). With an option, a check instead (Checks.swift).
import AppKit

let checking: Bool
switch Checks.start(Array(CommandLine.arguments.dropFirst())) {
case .exit(let code): exit(code)
case .app(let check): checking = check
}
Interface.reload()
let app = NSApplication.shared
let delegate = AppDelegate(checking: checking)
app.delegate = delegate
app.setActivationPolicy(checking ? .prohibited : .accessory)   // a menu bar app: no Dock icon
// SIGTERM (the installer updating it, `kill`): quit as from the menu, so a dictation is
// cancelled and the socket and pid file are cleaned up (without asking about a meeting)
signal(SIGTERM, SIG_IGN)
let term = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
term.setEventHandler {
    delegate.quitWithoutAsking = true
    NSApp.terminate(nil)
}
term.resume()
// A client of the control socket or the speech engine that has gone away: an error on that
// write, not the end of BMeet
signal(SIGPIPE, SIG_IGN)
app.run()
