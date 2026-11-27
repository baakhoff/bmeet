// bmeet-whisper - Whisper on the Apple Neural Engine (WhisperKit) for every BMeet process.
//
// On Linux each npu-transcribe process runs Whisper on the Intel NPU itself (OpenVINO GenAI).
// On a Mac the models are Core ML programs that WhisperKit runs on the Neural Engine, and one
// loaded copy is shared: dictation, the live meeting transcript, the speaker pass and file
// transcription all ask this server, so an 8 GB Mac holds Whisper once (~0.7 GB), not once per
// process. It listens on a Unix socket in BMeet's runtime folder (only your user can open it),
// takes one request per connection, runs them one at a time, the most urgent first (dictation
// before a file), and exits after a while without work (the next client starts it again).
//
//   bmeet-whisper serve [--socket PATH] [--models DIR] [--idle SECONDS] [--compute ane|cpu|gpu]
//   bmeet-whisper transcribe FILE.f32 [--model turbo|small] [--language XX] [--translate] [--words]
//
// Memory: a model is released `unload-after` minutes after its last use (~/.config/bmeet/unload-after,
// 0-60, default 5; 0: as soon as no request waits for it), read whenever it is needed, so a change
// applies without a restart. Only requests that use a model count as a use: `status`, `unload`
// and a client that only connects to see whether the server is up do not keep anything loaded.
// The install's own model (turbo, or small without turbo) gets the whole time; the other one, and
// a copy on another device, at most 3 minutes. When macOS says memory is short (its memory-pressure
// warning or critical event), every model is released as soon as the request that runs (if one
// does) is done. The server exits once it has held no model and had no request using one for
// --idle seconds (default 600).
//
// Protocol: the client sends one JSON line, then `samples` float32 little-endian samples (16 kHz
// mono) when the op takes audio. The answer is one JSON line.
//   {"op": "transcribe", "model": "turbo", "samples": N, "language": "ru"|null, "task":
//    "transcribe"|"translate", "timestamps": true, "words": false, "priority": 0}
//     -> {"ok": true, "language": "ru", "text": ..., "segments": [{"start", "end", "text",
//        "words": [{"word", "start", "end", "probability"}]}], "device": "ANE", "seconds": 0.4}
//   {"op": "detect", "model": "turbo", "samples": N}  -> {"ok": true, "language": "en"}
//   {"op": "load", "model": "turbo"}                    -> {"ok": true, "device": "ANE", "seconds": 2.1}
//   {"op": "unload", "model": "turbo"|null, "compute": "ane"|null}
//                                                       -> {"ok": true, "unloaded": ["turbo/ane"]}
//   {"op": "status"} -> {"ok": true, "pid", "compute", "models": {"turbo/ane": "ANE"}, "loaded":
//       {"turbo/ane": {"device", "since", "last_use", "unload_in"}}, "unload_after", "busy"}
//       (since, last_use: Unix times; unload_in, unload_after: seconds; busy: requests running or waiting)
// Errors: {"ok": false, "error": "..."}. Times are seconds from the start of the audio sent.
// `unload` without a model releases every model, with one releases it on every device (or on
// `compute` only).
//
// Models: DIR/openai_whisper-large-v3-v20240930_626MB ("turbo") and DIR/openai_whisper-small_216MB
// ("small", for translating), with WhisperKit's tokenizers in DIR/tokenizers/openai/whisper-*.
// The first load on the Neural Engine has macOS compile the model for it (a few minutes for
// turbo on an M1, once; macOS caches the result).
//
// Tests only: BMEET_UNLOAD_AFTER_SECONDS gives `unload-after` in seconds instead of the file, and
// BMEET_WHISPER_TEST=1 makes the models stand-ins (they "load" at once without files, and every
// transcript is empty) and adds the op {"op": "pressure", "level": "warning"|"critical"}, which
// does what macOS's memory-pressure event does.

import CoreML
import Darwin
import Foundation
import WhisperKit

// MARK: - settings

let variants: [String: (folder: String, tokenizer: String)] = [
    "turbo": ("openai_whisper-large-v3-v20240930_626MB", "openai/whisper-large-v3"),
    "small": ("openai_whisper-small_216MB", "openai/whisper-small"),
]

/// $XDG_RUNTIME_DIR/bmeet, as BMeet's Python programs see it (macos/bmeetmac.py: your temp folder),
/// or $BMEET_RUNTIME_ROOT/bmeet (tests). Not $XDG_RUNTIME_DIR: a shell may export one, BMeet.app has none.
func runtimeDir() -> URL {
    var root = ProcessInfo.processInfo.environment["BMEET_RUNTIME_ROOT"] ?? ""
    if root.isEmpty {
        var buf = [CChar](repeating: 0, count: Int(PATH_MAX))
        let n = confstr(_CS_DARWIN_USER_TEMP_DIR, &buf, buf.count)
        root = n > 0 ? String(cString: buf) : NSTemporaryDirectory()
    }
    return URL(fileURLWithPath: root).appendingPathComponent("bmeet")
}

func defaultModels() -> URL {
    if let dir = ProcessInfo.processInfo.environment["NPU_MODELS_DIR"], !dir.isEmpty {
        return URL(fileURLWithPath: dir).appendingPathComponent("whisperkit")
    }
    let data = ProcessInfo.processInfo.environment["XDG_DATA_HOME"].map { URL(fileURLWithPath: $0) }
        ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/share")
    return data.appendingPathComponent("npu-models/whisperkit")
}

func log(_ msg: String) {
    let line = "bmeet-whisper: \(msg)\n"
    FileHandle.standardError.write(line.data(using: .utf8)!)
}

/// Seconds a model stays loaded after its last use: ~/.config/bmeet/unload-after (minutes, 0-60;
/// a number outside counts as the nearest end, a missing or unreadable file as 5), read each time,
/// so a change from the Settings window applies at once. BMEET_UNLOAD_AFTER_SECONDS: tests only.
func unloadAfter() -> Double {
    let env = ProcessInfo.processInfo.environment
    if let s = env["BMEET_UNLOAD_AFTER_SECONDS"], let seconds = Double(s), seconds >= 0 { return seconds }
    let config = env["XDG_CONFIG_HOME"].flatMap { $0.isEmpty ? nil : URL(fileURLWithPath: $0) }
        ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".config")
    guard let text = try? String(contentsOf: config.appendingPathComponent("bmeet/unload-after"), encoding: .utf8),
          let word = text.split(whereSeparator: \.isWhitespace).first, let minutes = Int(word) else { return 5 * 60 }
    return Double(min(max(minutes, 0), 60)) * 60
}

/// BMEET_WHISPER_TEST=1 (tests only): stand-in models and the "pressure" op (see the top).
let testMode = ProcessInfo.processInfo.environment["BMEET_WHISPER_TEST"] == "1"
let SECONDARY_AT_MOST: Double = 180   // seconds: the translation model and copies on another device

struct Options {
    var socket = runtimeDir().appendingPathComponent("whisper.sock")
    var models = defaultModels()
    var idle: Double = 600          // seconds with no model loaded and none used before the server exits
    var compute = "ane"
}

// MARK: - one-at-a-time gate, most urgent first

actor Gate {
    private var busy = false
    private var waiting: [(priority: Int, order: Int, cont: CheckedContinuation<Void, Never>)] = []
    private var counter = 0

    func enter(priority: Int) async {
        if !busy {
            busy = true
            return
        }
        counter += 1
        let order = counter
        await withCheckedContinuation { cont in
            waiting.append((priority, order, cont))
        }
    }

    func leave() {
        guard !waiting.isEmpty else {
            busy = false
            return
        }
        // highest priority first, then first come first served
        let i = waiting.indices.max { a, b in
            (waiting[a].priority, -waiting[a].order) < (waiting[b].priority, -waiting[b].order)
        }!
        waiting.remove(at: i).cont.resume()
    }
}

// MARK: - the models

/// A loaded model: WhisperKit's pipeline (nil for a stand-in, BMEET_WHISPER_TEST), the device it
/// runs on, when it was loaded and when a request last used it.
final class Loaded {
    let kit: WhisperKit?
    let device: String
    let since = Date()
    var lastUse = Date()

    init(kit: WhisperKit?, device: String) {
        self.kit = kit
        self.device = device
    }
}

// (models changes only inside the gate, and under the lock, for status, which does not wait for
// the gate; current only inside the gate)
final class Engine: @unchecked Sendable {
    let options: Options
    private var models: [String: Loaded] = [:]
    /// The model that gets the whole of `unload-after`: the install's own (turbo, or small when
    /// turbo is not installed) on the server's device. It stays the same whatever the clients ask
    /// for: a one-off translation with small does not make turbo the one that goes first.
    let primary: String
    private var current = ""     // the model of the request in the gate
    let gate = Gate()
    private var connections = 0  // open connections, whatever they ask
    private var using = 0        // requests that use a model, from their first line to their answer
    private var lastWork = Date()   // when a request that uses a model last began or ended
    private var releasing = false   // releaseDue's task waits for the gate or runs
    let loadedOne = DispatchSemaphore(value: 0)   // signalled when a model is loaded (for the idle thread)
    private let lock = NSLock()

    init(options: Options) {
        self.options = options
        let turbo = options.models.appendingPathComponent(variants["turbo"]!.folder)
            .appendingPathComponent("AudioEncoder.mlmodelc")
        primary = (testMode || FileManager.default.fileExists(atPath: turbo.path) ? "turbo/" : "small/") + options.compute
    }

    private func locked<T>(_ body: () -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body()
    }

    func connected(_ delta: Int) {
        locked { connections += delta }
    }

    /// A request that uses a model begins (+1) or has been answered (-1).
    func work(_ delta: Int) {
        locked {
            using += delta
            lastWork = Date()
        }
    }

    /// Nothing loaded, nothing running and no model used for `idle` seconds: the server may exit.
    /// (Connections that only ask for the status or see whether it is up do not count as work.)
    func mayExit(idle: Double) -> Bool {
        locked { models.isEmpty && using == 0 && connections == 0 && Date().timeIntervalSince(lastWork) > idle }
    }

    func holdsModels() -> Bool {
        locked { !models.isEmpty }
    }

    /// How long `key` may stay loaded after its last use, with `unload-after` = `policy` seconds.
    func limit(_ key: String, policy: Double) -> Double {
        key == primary ? policy : min(policy, SECONDARY_AT_MOST)
    }

    /// Is a model unused for longer than it may stay?
    func due(policy: Double) -> Bool {
        locked { models.contains { Date().timeIntervalSince($0.value.lastUse) >= limit($0.key, policy: policy) } }
    }

    func compute(_ name: String) -> ModelComputeOptions {
        switch name {
        case "cpu":
            return ModelComputeOptions(melCompute: .cpuOnly, audioEncoderCompute: .cpuOnly, textDecoderCompute: .cpuOnly)
        case "gpu":
            return ModelComputeOptions(melCompute: .cpuAndGPU, audioEncoderCompute: .cpuAndGPU, textDecoderCompute: .cpuAndGPU)
        default:   // the Neural Engine; the small mel spectrogram on the CPU, so the GPU stays free
            return ModelComputeOptions(melCompute: .cpuOnly, audioEncoderCompute: .cpuAndNeuralEngine,
                                       textDecoderCompute: .cpuAndNeuralEngine)
        }
    }

    /// The loaded model (loading it first): `model` on `computeName` ("ane", "cpu", "gpu"; the
    /// server's --compute when empty). Each pair is loaded once; "ane" falls back to the CPU.
    /// (Run it inside the gate.)
    func pipe(_ model: String, compute computeName: String = "") async throws -> Loaded {
        let first = computeName.isEmpty ? options.compute : computeName
        let key = "\(model)/\(first)"
        current = key
        if let loaded = locked({ models[key] }) {
            locked { loaded.lastUse = Date() }
            return loaded
        }
        guard let v = variants[model] else { throw WhisperError.modelsUnavailable("unknown model \(model)") }
        if testMode {
            log("loading a stand-in for \(key) (BMEET_WHISPER_TEST)")
            let loaded = Loaded(kit: nil, device: "TEST")
            locked { models[key] = loaded }
            loadedOne.signal()
            return loaded
        }
        let folder = options.models.appendingPathComponent(v.folder)
        let tokenizer = options.models.appendingPathComponent("tokenizers").appendingPathComponent(v.tokenizer)
        guard FileManager.default.fileExists(atPath: folder.appendingPathComponent("AudioEncoder.mlmodelc").path) else {
            throw WhisperError.modelsUnavailable("\(folder.path) is missing - run the installer again")
        }
        var tries = [first]
        if first != "cpu" { tries.append("cpu") }
        var lastError: Error?
        for c in tries {
            let t0 = Date()
            log("loading \(v.folder) on \(c == "ane" ? "the Neural Engine" : c.uppercased())"
                + (c == "ane" ? " (the first time macOS compiles it for the Neural Engine: a few minutes, once)" : ""))
            do {
                let config = WhisperKitConfig(modelFolder: folder.path, tokenizerFolder: tokenizer,
                                              computeOptions: compute(c), verbose: false, logLevel: .error,
                                              prewarm: false, load: true, download: false)
                let p = try await WhisperKit(config)
                let loaded = Loaded(kit: p, device: c == "ane" ? "ANE" : c.uppercased())
                locked { models[key] = loaded }
                loadedOne.signal()
                log(String(format: "%@ ready on %@ in %.1f s", v.folder, loaded.device, Date().timeIntervalSince(t0)))
                return loaded
            } catch {
                lastError = error
                log("WARNING: loading on \(c) failed: \(error)")
            }
        }
        throw lastError ?? WhisperError.modelsUnavailable("could not load \(model)")
    }

    /// The request in the gate is over: its model counts as used now (a long one is not idle).
    func used() {
        locked { models[current]?.lastUse = Date() }
    }

    /// Release the models for which `why` gives a reason (logged with each); returns their keys.
    /// (Run it inside the gate: not while a request uses the models.)
    func unload(_ why: (String, Loaded) -> String?) async -> [String] {
        let gone = locked {
            var picked: [(key: String, loaded: Loaded, why: String)] = []
            for (key, loaded) in models.sorted(by: { $0.key < $1.key }) {
                if let reason = why(key, loaded) { picked.append((key, loaded, reason)) }
            }
            for p in picked { models.removeValue(forKey: p.key) }
            return picked
        }
        for p in gone {
            await p.loaded.kit?.unloadModels()
            log("unloaded \(p.key) (\(p.why))")
        }
        return gone.map(\.key)
    }

    /// Release what has been unused for longer than it may stay (inside the gate).
    func unloadIdle() async {
        let policy = unloadAfter()
        _ = await unload { key, loaded in
            let idle = Date().timeIntervalSince(loaded.lastUse)
            guard idle >= self.limit(key, policy: policy) else { return nil }
            return policy == 0 ? "unload-after is 0: released once no request waits for it"
                               : String(format: "unused for %.0f s", idle)
        }
    }

    /// Release the models that are past their time, once the gate is free: the lowest priority, so
    /// this runs only when no request waits (a request that is due next keeps its model).
    func releaseDue() {
        let start = due(policy: unloadAfter()) && locked {
            defer { releasing = true }
            return !releasing      // (one at a time: the next look finds what is still due)
        }
        guard start else { return }
        Task {
            await gate.enter(priority: Int.min)
            await unloadIdle()
            await gate.leave()
            locked { releasing = false }
        }
    }

    /// macOS says memory is short (or a test says so): every model goes, as soon as the request
    /// that runs now (if one does) is done, before the requests that wait.
    func memoryPressure(_ level: String) {
        let busy = locked { using > 0 }
        log("memory pressure (\(level)): releasing the models" + (busy ? " once the request that runs now is done" : ""))
        Task {
            await gate.enter(priority: Int.max)
            let gone = await unload { _, _ in "memory pressure: \(level)" }
            if gone.isEmpty { log("memory pressure (\(level)): no model was loaded") }
            await gate.leave()
        }
    }

    func status() -> [String: Any] {
        let policy = unloadAfter()
        return locked {
            var loaded: [String: Any] = [:]
            for (key, m) in models {
                loaded[key] = ["device": m.device, "since": m.since.timeIntervalSince1970,
                               "last_use": m.lastUse.timeIntervalSince1970,
                               "unload_in": max(0, limit(key, policy: policy) - Date().timeIntervalSince(m.lastUse))]
            }
            return ["ok": true, "pid": Int(getpid()), "compute": options.compute,
                    "models": models.mapValues { $0.device }, "loaded": loaded, "unload_after": policy,
                    "busy": using]
        }
    }
}

func languageCode(_ s: String) -> String {
    // WhisperKit reports codes ("en"); keep the code if it ever gives "<|en|>" or a name
    s.trimmingCharacters(in: CharacterSet(charactersIn: "<|>"))
}

/// The ops that use a model (they load it, and count as a use of it).
let modelOps: Set<String> = ["load", "detect", "transcribe"]

func handle(_ req: [String: Any], audio: [Float], engine: Engine) async -> [String: Any] {
    let op = req["op"] as? String ?? ""
    if op == "status" { return engine.status() }
    if op == "pressure" && testMode {
        engine.memoryPressure(req["level"] as? String ?? "warning")
        return ["ok": true]
    }
    if op == "unload" {   // (before the requests that wait, after the one that runs)
        let model = req["model"] as? String, compute = req["compute"] as? String
        await engine.gate.enter(priority: Int.max)
        let gone = await engine.unload { key, _ in
            let parts = key.split(separator: "/").map(String.init)
            return (model == nil || parts.first == model) && (compute == nil || parts.last == compute) ? "asked" : nil
        }
        await engine.gate.leave()
        return ["ok": true, "unloaded": gone]
    }
    guard modelOps.contains(op) else { return ["ok": false, "error": "unknown op \(op)"] }
    let model = req["model"] as? String ?? "turbo"
    let priority = req["priority"] as? Int ?? 0
    await engine.gate.enter(priority: priority)
    let t0 = Date()
    defer {
        engine.used()
        Task { await engine.gate.leave() }
    }
    do {
        let loaded = try await engine.pipe(model, compute: req["compute"] as? String ?? "")
        let device = loaded.device
        guard let pipe = loaded.kit else {   // (a stand-in: BMEET_WHISPER_TEST)
            let language = (req["language"] as? String).map(languageCode) ?? "en"
            return ["ok": true, "language": language, "text": "", "segments": [] as [Any], "device": device,
                    "seconds": Date().timeIntervalSince(t0)]
        }
        switch op {
        case "load":
            return ["ok": true, "device": device, "seconds": Date().timeIntervalSince(t0)]
        case "detect":
            let (language, _) = try await pipe.detectLangauge(audioArray: audio)
            return ["ok": true, "language": languageCode(language), "device": device,
                    "seconds": Date().timeIntervalSince(t0)]
        case "transcribe":
            var opts = DecodingOptions()
            opts.task = (req["task"] as? String) == "translate" ? .translate : .transcribe
            // The language: given, or detected once on this audio (WhisperKit's own detection
            // per window let a trailing hallucination - "Gracias." after Russian - name the
            // language of the whole request).
            var language = (req["language"] as? String).map(languageCode) ?? ""
            if language.isEmpty {
                language = languageCode(try await pipe.detectLangauge(audioArray: Array(audio.prefix(30 * 16_000))).language)
            }
            opts.language = language
            opts.usePrefillPrompt = true
            opts.detectLanguage = false
            opts.temperature = 0
            opts.skipSpecialTokens = true
            opts.withoutTimestamps = !((req["timestamps"] as? Bool) ?? true)
            opts.wordTimestamps = (req["words"] as? Bool) ?? false
            // WhisperKit does not start another window in the last windowClipTime seconds: 1 s
            // keeps it from making up words over a block's last silence, but a dictation shorter
            // than that would not be transcribed at all.
            let seconds = Float(audio.count) / 16_000
            opts.windowClipTime = seconds < 3 ? 0 : 1.0
            opts.chunkingStrategy = nil
            if let n = req["max_tokens"] as? Int { opts.sampleLength = min(n, 224) }
            let results = try await pipe.transcribe(audioArray: audio, decodeOptions: opts)
            var segments: [[String: Any]] = []
            for r in results {
                for s in r.segments {
                    var seg: [String: Any] = ["start": Double(s.start), "end": Double(s.end), "text": s.text]
                    if let words = s.words {
                        seg["words"] = words.map { ["word": $0.word, "start": Double($0.start), "end": Double($0.end),
                                                    "probability": Double($0.probability)] }
                    }
                    segments.append(seg)
                }
            }
            let t = results.first?.timings
            let timings: [String: Any] = t.map { ["encoding": $0.encoding, "decoding": $0.decodingLoop,
                                                  "windows": $0.totalEncodingRuns, "tokens": $0.totalDecodingLoops,
                                                  "fallbacks": $0.totalDecodingFallbacks] } ?? [:]
            return ["ok": true, "language": language, "text": results.map(\.text).joined(separator: " "),
                    "segments": segments, "device": device, "seconds": Date().timeIntervalSince(t0),
                    "timings": timings]
        default:
            return ["ok": false, "error": "unknown op \(op)"]
        }
    } catch {
        return ["ok": false, "error": "\(error)"]
    }
}

// MARK: - socket

/// The socket and pid file this server made, for atexit (a C function: it cannot capture them).
/// They are removed only while they are still these files: not another server's.
enum Owned {
    static var files: [(path: String, dev: Int64, ino: UInt64)] = []

    static func add(_ path: String) {
        var st = stat()
        if stat(path, &st) == 0 { files.append((path: path, dev: Int64(st.st_dev), ino: UInt64(st.st_ino))) }
    }

    static func remove() {
        for f in files {
            var st = stat()
            if stat(f.path, &st) == 0, Int64(st.st_dev) == f.dev, UInt64(st.st_ino) == f.ino { unlink(f.path) }
        }
    }
}

func readExactly(_ fd: Int32, _ count: Int) -> Data? {
    var data = Data(count: count)
    var got = 0
    while got < count {
        let n = data.withUnsafeMutableBytes { buf in
            read(fd, buf.baseAddress!.advanced(by: got), count - got)
        }
        if n <= 0 { return nil }
        got += n
    }
    return data
}

func readLine(_ fd: Int32, limit: Int = 1 << 20) -> String? {
    var bytes: [UInt8] = []
    var c: UInt8 = 0
    while bytes.count < limit {
        let n = read(fd, &c, 1)
        if n <= 0 { return nil }
        if c == 10 { return String(decoding: bytes, as: UTF8.self) }
        bytes.append(c)
    }
    return nil
}

func writeAll(_ fd: Int32, _ data: Data) {
    data.withUnsafeBytes { buf in
        var off = 0
        while off < buf.count {
            let n = write(fd, buf.baseAddress!.advanced(by: off), buf.count - off)
            if n <= 0 { return }
            off += n
        }
    }
}

func reply(_ fd: Int32, _ obj: [String: Any]) {
    var data = (try? JSONSerialization.data(withJSONObject: obj)) ?? Data("{\"ok\":false}".utf8)
    data.append(10)
    writeAll(fd, data)
}

func serve(_ options: Options) -> Never {
    let dir = options.socket.deletingLastPathComponent()
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true,
                                             attributes: [.posixPermissions: 0o700])
    let path = options.socket.path
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    guard fd >= 0 else { log("socket() failed"); exit(1) }
    var addr = sockaddr_un()
    addr.sun_family = sa_family_t(AF_UNIX)
    guard path.utf8.count < MemoryLayout.size(ofValue: addr.sun_path) else { log("socket path too long: \(path)"); exit(1) }
    // Another server already listening? Then this one is not needed.
    if FileManager.default.fileExists(atPath: path) {
        let probe = socket(AF_UNIX, SOCK_STREAM, 0)
        var a = sockaddr_un()
        a.sun_family = sa_family_t(AF_UNIX)
        _ = withUnsafeMutablePointer(to: &a.sun_path) { p in
            path.withCString { strncpy(UnsafeMutableRawPointer(p).assumingMemoryBound(to: CChar.self), $0, 103) }
        }
        let ok = withUnsafePointer(to: &a) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(probe, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        close(probe)
        if ok == 0 { log("already running at \(path)"); exit(0) }
        unlink(path)
    }
    _ = withUnsafeMutablePointer(to: &addr.sun_path) { p in
        path.withCString { strncpy(UnsafeMutableRawPointer(p).assumingMemoryBound(to: CChar.self), $0, 103) }
    }
    let old = umask(0o077)
    let bound = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
    }
    umask(old)
    guard bound == 0, listen(fd, 16) == 0 else { log("cannot listen on \(path): \(String(cString: strerror(errno)))"); exit(1) }
    Owned.add(path)
    let pidFile = dir.appendingPathComponent("whisper.pid")
    try? "\(getpid())\n".write(to: pidFile, atomically: true, encoding: .utf8)
    Owned.add(pidFile.path)
    log("listening on \(path) (models in \(options.models.path), \(options.compute))")

    let engine = Engine(options: options)
    signal(SIGPIPE, SIG_IGN)
    for sig in [SIGTERM, SIGINT] {
        signal(sig) { _ in exit(0) }
    }
    atexit { Owned.remove() }
    // macOS's memory-pressure events (warning, critical): the models go (Engine.memoryPressure)
    let pressure = DispatchSource.makeMemoryPressureSource(eventMask: [.warning, .critical], queue: .global())
    pressure.setEventHandler {
        engine.memoryPressure(pressure.data.contains(.critical) ? "critical" : "warning")
    }
    pressure.activate()
    // Models past their time go (when no request runs or waits), and the server exits once it has
    // held no model and done no work for --idle seconds. It looks every 15 s, more often when
    // unload-after (while a model is loaded) or --idle is short (tests give them in seconds).
    Thread.detachNewThread {
        while true {
            let wait = engine.holdsModels() ? unloadAfter() / 4 : options.idle / 2
            _ = engine.loadedOne.wait(timeout: .now() + min(15, max(0.25, wait)))   // (a load: look again sooner)
            if engine.mayExit(idle: options.idle) {
                log("no model loaded or used for \(Int(options.idle)) s: exiting")
                exit(0)
            }
            engine.releaseDue()
        }
    }
    while true {
        let client = accept(fd, nil, nil)
        if client < 0 { continue }
        engine.connected(1)
        Thread.detachNewThread {
            defer { close(client); engine.connected(-1) }
            guard let line = readLine(client),
                  let req = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any] else {
                reply(client, ["ok": false, "error": "expected one JSON line"])
                return
            }
            // (only a request that uses a model is work: a status or a look whether the server is
            // up keeps nothing loaded and the server no longer)
            let working = modelOps.contains(req["op"] as? String ?? "")
            if working { engine.work(1) }
            defer {
                if working {
                    engine.work(-1)
                    engine.releaseDue()   // (unload-after 0: right away, once no request waits)
                }
            }
            let n = req["samples"] as? Int ?? 0
            var audio: [Float] = []
            if n > 0 {
                guard n < 16_000 * 60 * 10, let data = readExactly(client, n * 4) else {
                    reply(client, ["ok": false, "error": "audio missing or too long (at most 10 min per request)"])
                    return
                }
                audio = data.withUnsafeBytes { Array($0.bindMemory(to: Float.self)) }
            }
            let sem = DispatchSemaphore(value: 0)
            var answer: [String: Any] = [:]
            Task {
                answer = await handle(req, audio: audio, engine: engine)
                sem.signal()
            }
            sem.wait()
            reply(client, answer)
        }
    }
}

// MARK: - command line

func usage() -> Never {
    FileHandle.standardError.write("""
    usage: bmeet-whisper serve [--socket PATH] [--models DIR] [--idle SECONDS] [--compute ane|cpu|gpu]
           bmeet-whisper transcribe FILE.f32 [--model turbo|small] [--language XX] [--translate] [--words]
                         [--compute ane|cpu|gpu] [--models DIR]   (FILE: raw float32 16 kHz mono; JSON on stdout)

    """.data(using: .utf8)!)
    exit(2)
}

var args = Array(CommandLine.arguments.dropFirst())
guard let command = args.first else { usage() }
args.removeFirst()
var options = Options()
var file: String?
var request: [String: Any] = ["op": "transcribe", "model": "turbo", "timestamps": true]
var i = 0
while i < args.count {
    let a = args[i]
    func value() -> String {
        i += 1
        guard i < args.count else { usage() }
        return args[i]
    }
    switch a {
    case "--socket": options.socket = URL(fileURLWithPath: value())
    case "--models": options.models = URL(fileURLWithPath: value())
    case "--idle": options.idle = Double(value()) ?? options.idle
    case "--compute": options.compute = value()
    case "--model": request["model"] = value()
    case "--language": request["language"] = value()
    case "--translate": request["task"] = "translate"
    case "--words": request["words"] = true
    default:
        if a.hasPrefix("-") || file != nil { usage() }
        file = a
    }
    i += 1
}

switch command {
case "serve":
    serve(options)
case "transcribe":
    guard let file, let data = FileManager.default.contents(atPath: file) else { usage() }
    let audio = data.withUnsafeBytes { Array($0.bindMemory(to: Float.self)) }
    let engine = Engine(options: options)
    let sem = DispatchSemaphore(value: 0)
    var answer: [String: Any] = [:]
    Task {
        answer = await handle(request, audio: audio, engine: engine)
        sem.signal()
    }
    sem.wait()
    let out = (try? JSONSerialization.data(withJSONObject: answer, options: [.prettyPrinted])) ?? Data()
    FileHandle.standardOutput.write(out)
    print()
    exit((answer["ok"] as? Bool) == true ? 0 : 1)
default:
    usage()
}
