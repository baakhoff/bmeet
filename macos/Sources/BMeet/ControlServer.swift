// BMeet.app's control socket: $XDG_RUNTIME_DIR/bmeet/agent.sock (only your user can open it).
// bmeet-ctl, the BMeet window and npu-transcribe send one JSON line, BMeet.app answers one:
//   {"op": "ping"}                                        {"ok": true, "pid": ...}
//   {"op": "dictate", "clipboard": false}                 start or stop a dictation
//   {"op": "cancel"}
//   {"op": "meeting", "action": "toggle", "in_person": false}
//   {"op": "language", "language": "ru"}                  (bmeet-ctl lang wrote it already)
//   {"op": "open", "args": ["meetings"]}                  the BMeet window
//   {"op": "notify", "title": ..., "body": ..., "ok": true}
//   {"op": "status"}                                      {"dictation": "idle", "accessibility": true, ...}

import Darwin
import Foundation

final class ControlServer {
    let path: URL
    private let handler: ([String: Any]) -> [String: Any]
    private var fd: Int32 = -1

    init(path: URL, handler: @escaping ([String: Any]) -> [String: Any]) {
        self.path = path
        self.handler = handler
    }

    func start() -> Bool {
        try? FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true,
                                                 attributes: [.posixPermissions: 0o700])
        unlink(path.path)
        fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let p = path.path
        guard p.utf8.count < MemoryLayout.size(ofValue: addr.sun_path) else { return false }
        _ = withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
            p.withCString { strncpy(UnsafeMutableRawPointer(ptr).assumingMemoryBound(to: CChar.self), $0, 103) }
        }
        let old = umask(0o077)
        let ok = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        } == 0 && listen(fd, 16) == 0
        umask(old)
        guard ok else { return false }
        Thread.detachNewThread { [self] in
            while true {
                let client = accept(fd, nil, nil)
                if client < 0 { continue }
                Thread.detachNewThread { self.serve(client) }
            }
        }
        return true
    }

    private func serve(_ client: Int32) {
        defer { close(client) }
        var tv = timeval(tv_sec: 5, tv_usec: 0)
        setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        // (a client that has gone away before the answer: an error, not SIGPIPE, which would end BMeet)
        var one: Int32 = 1
        setsockopt(client, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size))
        var bytes: [UInt8] = []
        var c: UInt8 = 0
        while bytes.count < 1 << 16, read(client, &c, 1) == 1, c != 10 { bytes.append(c) }
        guard let req = (try? JSONSerialization.jsonObject(with: Data(bytes))) as? [String: Any] else {
            send(client, ["ok": false, "error": "expected one JSON line"])
            return
        }
        var answer: [String: Any] = [:]
        DispatchQueue.main.sync { answer = handler(req) }   // (the app's state lives on the main thread)
        send(client, answer)
    }

    private func send(_ client: Int32, _ obj: [String: Any]) {
        var data = (try? JSONSerialization.data(withJSONObject: obj)) ?? Data("{}".utf8)
        data.append(10)
        data.withUnsafeBytes { _ = write(client, $0.baseAddress, $0.count) }
    }

    func stop() {
        if fd >= 0 { close(fd) }
        unlink(path.path)
    }
}
