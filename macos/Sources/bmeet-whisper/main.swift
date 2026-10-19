// bmeet-whisper - Whisper on the Apple Neural Engine (WhisperKit) for every BMeet process.
//
// On Linux each npu-transcribe process runs Whisper on the Intel NPU itself (OpenVINO GenAI).
// On a Mac the models are Core ML programs that WhisperKit runs on the Neural Engine, and one
// loaded copy is shared: dictation, the live meeting transcript, the speaker pass and file
// transcription all ask this server, so an 8 GB Mac holds Whisper once (~0.7 GB), not once per
// process. It listens on a Unix socket in BMeet's runtime folder (only your user can open it),
// takes one request per connection, runs them one at a time, the most urgent first (dictation
// before a file), and exits after a while without requests (the next client starts it again).
//
//   bmeet-whisper serve [--socket PATH] [--models DIR] [--idle SECONDS] [--compute ane|cpu|gpu]
//   bmeet-whisper transcribe FILE.f32 [--model turbo|small] [--language XX] [--translate] [--words]
//
// Protocol: the client sends one JSON line, then `samples` float32 little-endian samples (16 kHz
// mono) when the op takes audio. The answer is one JSON line.
//   {"op": "transcribe", "model": "turbo", "samples": N, "language": "ru"|null, "task":
//    "transcribe"|"translate", "timestamps": true, "words": false, "priority": 0}
//     -> {"ok": true, "language": "ru", "text": ..., "segments": [{"start", "end", "text",
//        "words": [{"word", "start", "end", "probability"}]}], "device": "ANE", "seconds": 0.4}
//   {"op": "detect", "model": "turbo", "samples": N}  -> {"ok": true, "language": "en"}
//   {"op": "load", "model": "turbo"}                    -> {"ok": true, "device": "ANE", "seconds": 2.1}
//   {"op": "status"}                                    -> {"ok": true, "pid", "models": {...}}
// Errors: {"ok": false, "error": "..."}. Times are seconds from the start of the audio sent.
//
// Models: DIR/openai_whisper-large-v3-v20240930_626MB ("turbo") and DIR/openai_whisper-small_216MB
// ("small", for translating), with WhisperKit's tokenizers in DIR/tokenizers/openai/whisper-*.
// The first load on the Neural Engine has macOS compile the model for it (a few minutes for
// turbo on an M1, once; macOS caches the result).

import CoreML
import Darwin
import Foundation
import WhisperKit

// MARK: - settings

let variants: [String: (folder: String, tokenizer: String)] = [
    "turbo": ("openai_whisper-large-v3-v20240930_626MB", "openai/whisper-large-v3"),
    "small": ("openai_whisper-small_216MB", "openai/whisper-small"),
]

func runtimeDir() -> URL {
    if let dir = ProcessInfo.processInfo.environment["BMEET_RUNTIME_DIR"], !dir.isEmpty {
        return URL(fileURLWithPath: dir)
    }
    var buf = [CChar](repeating: 0, count: Int(PATH_MAX))
    let n = confstr(_CS_DARWIN_USER_TEMP_DIR, &buf, buf.count)
    let tmp = n > 0 ? String(cString: buf) : NSTemporaryDirectory()
    return URL(fileURLWithPath: tmp).appendingPathComponent("bmeet")
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

struct Options {
    var socket = runtimeDir().appendingPathComponent("whisper.sock")
    var models = defaultModels()
    var idle: Double = 600          // seconds without a request before the server exits
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

final class Engine: @unchecked Sendable {
    let options: Options
    private var pipes: [String: WhisperKit] = [:]
    private var devices: [String: String] = [:]
    private var lastUse: [String: Date] = [:]
    let gate = Gate()
    private(set) var lastRequest = Date()
    private(set) var active = 0
    private let lock = NSLock()

    init(options: Options) { self.options = options }

    func touch(_ delta: Int) {
        lock.lock()
        active += delta
        lastRequest = Date()
        lock.unlock()
    }

    func idleSeconds() -> Double {
        lock.lock()
        defer { lock.unlock() }
        return active > 0 ? 0 : Date().timeIntervalSince(lastRequest)
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
    func pipe(_ model: String, compute computeName: String = "") async throws -> (WhisperKit, String) {
        let first = computeName.isEmpty ? options.compute : computeName
        let key = "\(model)/\(first)"
        if let p = pipes[key] {
            lastUse[key] = Date()
            return (p, devices[key] ?? "?")
        }
        guard let v = variants[model] else { throw WhisperError.modelsUnavailable("unknown model \(model)") }
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
                let device = c == "ane" ? "ANE" : c.uppercased()
                pipes[key] = p
                devices[key] = device
                lastUse[key] = Date()
                log(String(format: "%@ ready on %@ in %.1f s", v.folder, device, Date().timeIntervalSince(t0)))
                return (p, device)
            } catch {
                lastError = error
                log("WARNING: loading on \(c) failed: \(error)")
            }
        }
        throw lastError ?? WhisperError.modelsUnavailable("could not load \(model)")
    }

    /// The translation model is needed rarely: let it go after a few idle minutes.
    func unloadIdle(except keep: String = "turbo/ane", after seconds: Double = 180) async {
        for (model, t) in lastUse where model != keep && Date().timeIntervalSince(t) > seconds {
            if let p = pipes.removeValue(forKey: model) {
                await p.unloadModels()
                log("unloaded \(model) (unused for \(Int(seconds)) s)")
            }
            lastUse.removeValue(forKey: model)
        }
    }

    func status() -> [String: Any] {
        ["ok": true, "pid": Int(getpid()), "models": devices, "compute": options.compute]
    }
}

func languageCode(_ s: String) -> String {
    // WhisperKit reports codes ("en"); keep the code if it ever gives "<|en|>" or a name
    s.trimmingCharacters(in: CharacterSet(charactersIn: "<|>"))
}

func handle(_ req: [String: Any], audio: [Float], engine: Engine) async -> [String: Any] {
    let op = req["op"] as? String ?? ""
    if op == "status" { return engine.status() }
    let model = req["model"] as? String ?? "turbo"
    let priority = req["priority"] as? Int ?? 0
    await engine.gate.enter(priority: priority)
    let t0 = Date()
    defer { Task { await engine.gate.leave() } }
    do {
        let (pipe, device) = try await engine.pipe(model, compute: req["compute"] as? String ?? "")
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
    let pidFile = dir.appendingPathComponent("whisper.pid")
    try? "\(getpid())\n".write(to: pidFile, atomically: true, encoding: .utf8)
    log("listening on \(path) (models in \(options.models.path), \(options.compute))")

    let engine = Engine(options: options)
    signal(SIGPIPE, SIG_IGN)
    for sig in [SIGTERM, SIGINT] {
        signal(sig) { _ in exit(0) }
    }
    atexit {
        let dir = runtimeDir()
        unlink(dir.appendingPathComponent("whisper.sock").path)
        unlink(dir.appendingPathComponent("whisper.pid").path)
    }
    // idle exit (and dropping the translation model)
    Thread.detachNewThread {
        while true {
            sleep(15)
            if engine.idleSeconds() > options.idle {
                log("no requests for \(Int(options.idle)) s: exiting")
                exit(0)
            }
            let sem = DispatchSemaphore(value: 0)
            Task { await engine.unloadIdle(); sem.signal() }
            sem.wait()
        }
    }
    while true {
        let client = accept(fd, nil, nil)
        if client < 0 { continue }
        engine.touch(1)
        Thread.detachNewThread {
            defer { close(client); engine.touch(-1) }
            guard let line = readLine(client),
                  let req = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any] else {
                reply(client, ["ok": false, "error": "expected one JSON line"])
                return
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
