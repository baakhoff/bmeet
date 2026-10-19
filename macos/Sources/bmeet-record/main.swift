// bmeet-record - BMeet's recorder on macOS: what pw-record does for it on Linux.
//
//   bmeet-record raw
//       The default microphone as 16 kHz mono 16-bit little-endian PCM on stdout, until
//       SIGTERM/SIGINT or stdout is closed (npu-transcribe's MicSource: dictation).
//   bmeet-record meeting DIR [--mic-only] [--state FILE] [--pointer FILE]
//       A meeting (bmeet-meeting-audio's job on Linux): the microphone into DIR/part-N-mic.wav and
//       the system audio - everything the Mac plays, the other side of a call - into
//       DIR/part-N-remote.wav, 16 kHz mono 16-bit, with DIR/part-N.start (epoch seconds). It
//       follows the meeting state file (first line recording|paused|idle): "paused" ends the
//       part, "recording" starts the next one, "idle" (or SIGINT/SIGTERM) ends the recording.
//       While it records, the pointer file names DIR (bmeet-chrome-host and npu-transcribe --live
//       look there). --mic-only: in person, no system audio.
//   bmeet-record check
//       {"microphone": "authorized"|"denied"|"not determined"|"restricted"} (asks if not determined).
//
// Both tracks of a part start at the same instant: each stream's first sample is placed by its
// hardware timestamp, so the echo of the call in your microphone lines up with the call.
// The system audio comes from a Core Audio process tap (macOS 14.2+) of all processes. The
// first use asks for permission ("System Audio Recording"), for the app that started this
// (BMeet.app): without it the tap delivers silence. WAV headers are written at the start with
// open sizes (npu-transcribe --live reads the files while they grow) and fixed at the end.

import AVFoundation
import CoreAudio
import Darwin
import Foundation

let rate = 16_000.0

func log(_ msg: String) {
    FileHandle.standardError.write("bmeet-record: \(msg)\n".data(using: .utf8)!)
}

var timebase: mach_timebase_info_data_t = {
    var t = mach_timebase_info_data_t()
    mach_timebase_info(&t)
    return t
}()

func hostSeconds(_ host: UInt64) -> Double {
    Double(host) * Double(timebase.numer) / Double(timebase.denom) / 1e9
}

func nowHost() -> UInt64 { mach_absolute_time() }

// MARK: - output: a growing WAV file or stdout

final class Sink: @unchecked Sendable {
    private let fd: Int32
    private let wav: Bool
    private var written: UInt64 = 0
    private var queue: [Data] = []
    private let lock = NSCondition()
    private var closed = false
    private var thread: Thread?
    let origin: UInt64          // host time of sample 0 of this sink
    private var nextSample: Int64 = 0

    init(fd: Int32, wav: Bool, origin: UInt64) {
        self.fd = fd
        self.wav = wav
        self.origin = origin
        if wav { writeNow(Sink.header(dataBytes: 0xFFFF_FFFF - 36)) }
        let t = Thread { [unowned self] in self.drain() }
        t.start()
        thread = t
    }

    static func open(_ path: String, origin: UInt64) -> Sink? {
        let fd = Darwin.open(path, O_WRONLY | O_CREAT | O_TRUNC, 0o600)
        return fd < 0 ? nil : Sink(fd: fd, wav: true, origin: origin)
    }

    static func header(dataBytes: UInt32) -> Data {
        var d = Data()
        func u32(_ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
        func u16(_ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
        d.append(contentsOf: Array("RIFF".utf8)); u32(dataBytes &+ 36)
        d.append(contentsOf: Array("WAVE".utf8))
        d.append(contentsOf: Array("fmt ".utf8)); u32(16); u16(1); u16(1); u32(UInt32(rate)); u32(UInt32(rate) * 2); u16(2); u16(16)
        d.append(contentsOf: Array("data".utf8)); u32(dataBytes)
        return d
    }

    private func writeNow(_ data: Data) {
        data.withUnsafeBytes { buf in
            var off = 0
            while off < buf.count {
                let n = write(fd, buf.baseAddress!.advanced(by: off), buf.count - off)
                if n <= 0 {
                    lock.lock(); closed = true; lock.unlock()   // (stdout closed: the reader went away)
                    return
                }
                off += n
            }
        }
    }

    /// Samples whose first one was heard at host time `host`: placed at their time (silence
    /// before them if the stream started late or dropped out; overlap with what is already
    /// written is cut off).
    func append(_ samples: [Int16], host: UInt64?) {
        var samples = samples
        if let host {
            let at = Int64(((hostSeconds(host) - hostSeconds(origin)) * rate).rounded())
            if at > nextSample + Int64(rate / 50) {          // a gap of more than 20 ms: fill it
                let gap = min(at - nextSample, Int64(rate * 5))
                samples = [Int16](repeating: 0, count: Int(gap)) + samples
            } else if at < nextSample - Int64(rate / 50), nextSample == 0 {
                let skip = Int(min(Int64(samples.count), nextSample - at))
                samples.removeFirst(skip)
            }
        }
        if samples.isEmpty { return }
        nextSample += Int64(samples.count)
        let data = samples.withUnsafeBufferPointer { Data(buffer: $0) }
        lock.lock()
        if !closed { queue.append(data) }
        lock.signal()
        lock.unlock()
    }

    var isClosed: Bool { lock.lock(); defer { lock.unlock() }; return closed && queue.isEmpty }

    private func drain() {
        while true {
            lock.lock()
            while queue.isEmpty && !closed { lock.wait() }
            if queue.isEmpty && closed { lock.unlock(); return }
            let batch = queue
            queue.removeAll()
            lock.unlock()
            for d in batch {
                writeNow(d)
                written += UInt64(d.count)
            }
        }
    }

    /// Write out what is queued, fix the WAV sizes, close.
    func finish() {
        lock.lock(); closed = true; lock.signal(); lock.unlock()
        while thread?.isFinished == false { usleep(5_000) }
        if wav {
            let size = UInt32(min(written, UInt64(UInt32.max - 36)))
            lseek(fd, 0, SEEK_SET)
            writeNow(Sink.header(dataBytes: size))
        }
        if fd > 2 { close(fd) }
    }
}

// MARK: - conversion to 16 kHz mono 16-bit

final class Converter {
    private var converter: AVAudioConverter?
    private var from: AVAudioFormat?
    private let to = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: rate, channels: 1, interleaved: true)!

    /// Mono float samples at `sampleRate` -> 16 kHz Int16.
    func convert(_ mono: [Float], sampleRate: Double) -> [Int16] {
        if mono.isEmpty { return [] }
        if from?.sampleRate != sampleRate {
            from = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: sampleRate, channels: 1, interleaved: false)
            converter = AVAudioConverter(from: from!, to: to)
        }
        guard let converter, let from,
              let input = AVAudioPCMBuffer(pcmFormat: from, frameCapacity: AVAudioFrameCount(mono.count)) else { return [] }
        input.frameLength = AVAudioFrameCount(mono.count)
        mono.withUnsafeBufferPointer { input.floatChannelData![0].update(from: $0.baseAddress!, count: mono.count) }
        let cap = AVAudioFrameCount(Double(mono.count) * rate / sampleRate) + 64
        guard let output = AVAudioPCMBuffer(pcmFormat: to, frameCapacity: cap) else { return [] }
        var fed = false
        var error: NSError?
        converter.convert(to: output, error: &error) { _, status in
            if fed { status.pointee = .noDataNow; return nil }
            fed = true
            status.pointee = .haveData
            return input
        }
        if let error { log("conversion failed: \(error)"); return [] }
        let n = Int(output.frameLength)
        return Array(UnsafeBufferPointer(start: output.int16ChannelData![0], count: n))
    }
}

func mixToMono(_ list: UnsafeMutableAudioBufferListPointer, frames: Int, interleavedChannels: Int) -> [Float] {
    var out = [Float](repeating: 0, count: frames)
    if list.count == 1 && interleavedChannels > 1 {             // interleaved
        guard let p = list[0].mData?.assumingMemoryBound(to: Float.self) else { return out }
        for i in 0..<frames {
            var s: Float = 0
            for c in 0..<interleavedChannels { s += p[i * interleavedChannels + c] }
            out[i] = s / Float(interleavedChannels)
        }
    } else {                                                    // one buffer per channel
        let n = Float(max(1, list.count))
        for b in list {
            guard let p = b.mData?.assumingMemoryBound(to: Float.self) else { continue }
            let count = min(frames, Int(b.mDataByteSize) / 4)
            for i in 0..<count { out[i] += p[i] / n }
        }
    }
    return out
}

// MARK: - the microphone (AVAudioEngine)

final class Microphone {
    private let engine = AVAudioEngine()
    private let converter = Converter()
    private let onAudio: ([Int16], UInt64?) -> Void
    private var observer: NSObjectProtocol?

    init(onAudio: @escaping ([Int16], UInt64?) -> Void) { self.onAudio = onAudio }

    func start() throws {
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw NSError(domain: "bmeet-record", code: 1, userInfo: [NSLocalizedDescriptionKey: "no microphone"])
        }
        input.installTap(onBus: 0, bufferSize: AVAudioFrameCount(format.sampleRate / 20), format: format) { [weak self] buf, time in
            guard let self, let data = buf.floatChannelData else { return }
            let frames = Int(buf.frameLength), channels = Int(buf.format.channelCount)
            var mono = [Float](repeating: 0, count: frames)
            for c in 0..<channels {
                let p = data[c]
                for i in 0..<frames { mono[i] += p[i] / Float(channels) }
            }
            let host: UInt64? = time.isHostTimeValid ? time.hostTime : nil
            self.onAudio(self.converter.convert(mono, sampleRate: buf.format.sampleRate), host)
        }
        engine.prepare()
        try engine.start()
        // A new input device (AirPods connected, ...): start over on it.
        observer = NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange,
                                                          object: engine, queue: nil) { [weak self] _ in
            guard let self else { return }
            log("the input device changed: following it")
            self.engine.inputNode.removeTap(onBus: 0)
            self.engine.stop()
            try? self.start()
        }
    }

    func stop() {
        if let observer { NotificationCenter.default.removeObserver(observer) }
        observer = nil
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
    }
}

// MARK: - the system audio (Core Audio process tap)

func audioProperty<T>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, _ value: inout T) -> OSStatus {
    var address = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                             mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(MemoryLayout<T>.size)
    return AudioObjectGetPropertyData(object, &address, 0, nil, &size, &value)
}

func defaultOutputUID() -> String? {
    var device = AudioObjectID(kAudioObjectUnknown)
    guard audioProperty(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultOutputDevice, &device) == noErr
    else { return nil }
    var uid: CFString = "" as CFString
    guard audioProperty(device, kAudioDevicePropertyDeviceUID, &uid) == noErr else { return nil }
    return uid as String
}

@available(macOS 14.2, *)
final class SystemAudio {
    private var tap = AudioObjectID(kAudioObjectUnknown)
    private var aggregate = AudioObjectID(kAudioObjectUnknown)
    private var proc: AudioDeviceIOProcID?
    private let converter = Converter()
    private let onAudio: ([Int16], UInt64?) -> Void
    private let queue = DispatchQueue(label: "bmeet-record.tap")
    private var listener: AudioObjectPropertyListenerBlock?

    init(onAudio: @escaping ([Int16], UInt64?) -> Void) { self.onAudio = onAudio }

    func start() throws {
        let description = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
        description.uuid = UUID()
        description.name = "BMeet system audio"
        description.isPrivate = true
        description.muteBehavior = .unmuted
        var status = AudioHardwareCreateProcessTap(description, &tap)
        guard status == noErr else { throw NSError(domain: "bmeet-record", code: Int(status),
                                                   userInfo: [NSLocalizedDescriptionKey: "cannot tap the system audio (\(status))"]) }
        var format = AudioStreamBasicDescription()
        _ = audioProperty(tap, kAudioTapPropertyFormat, &format)
        var config: [String: Any] = [
            kAudioAggregateDeviceNameKey: "BMeet system audio",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapUIDKey: description.uuid.uuidString,
                                               kAudioSubTapDriftCompensationKey: true]],
        ]
        if let output = defaultOutputUID() {       // the output device as the clock
            config[kAudioAggregateDeviceMainSubDeviceKey] = output
            config[kAudioAggregateDeviceSubDeviceListKey] = [[kAudioSubDeviceUIDKey: output]]
        }
        status = AudioHardwareCreateAggregateDevice(config as CFDictionary, &aggregate)
        guard status == noErr else { stop(); throw NSError(domain: "bmeet-record", code: Int(status),
                                                           userInfo: [NSLocalizedDescriptionKey: "cannot create the tap device (\(status))"]) }
        let sampleRate = format.mSampleRate > 0 ? format.mSampleRate : 48_000
        let interleaved = (format.mFormatFlags & kAudioFormatFlagIsNonInterleaved) == 0
        let channels = Int(max(1, format.mChannelsPerFrame))
        status = AudioDeviceCreateIOProcIDWithBlock(&proc, aggregate, queue) { [weak self] _, input, inputTime, _, _ in
            guard let self else { return }
            let list = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
            guard list.count > 0 else { return }
            let bytesPerFrame = interleaved ? 4 * channels : 4
            let frames = Int(list[0].mDataByteSize) / bytesPerFrame
            let mono = mixToMono(list, frames: frames, interleavedChannels: interleaved ? channels : 1)
            let host: UInt64? = (inputTime.pointee.mFlags.contains(.hostTimeValid)) ? inputTime.pointee.mHostTime : nil
            self.onAudio(self.converter.convert(mono, sampleRate: sampleRate), host)
        }
        guard status == noErr, let proc else { stop(); throw NSError(domain: "bmeet-record", code: Int(status),
                                                                     userInfo: [NSLocalizedDescriptionKey: "no tap callback (\(status))"]) }
        status = AudioDeviceStart(aggregate, proc)
        guard status == noErr else { stop(); throw NSError(domain: "bmeet-record", code: Int(status),
                                                           userInfo: [NSLocalizedDescriptionKey: "the tap does not start (\(status))"]) }
        // Another output device (headphones): the aggregate's clock is gone; build it again.
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                                                 mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        let block: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            guard let self else { return }
            log("the output device changed: tapping again")
            self.stop()
            try? self.start()
        }
        listener = block
        AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &address, queue, block)
    }

    func stop() {
        if let listener {
            var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                                                     mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
            AudioObjectRemovePropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &address, queue, listener)
        }
        listener = nil
        if let proc, aggregate != kAudioObjectUnknown {
            AudioDeviceStop(aggregate, proc)
            AudioDeviceDestroyIOProcID(aggregate, proc)
        }
        proc = nil
        if aggregate != kAudioObjectUnknown { AudioHardwareDestroyAggregateDevice(aggregate) }
        aggregate = AudioObjectID(kAudioObjectUnknown)
        if tap != kAudioObjectUnknown { AudioHardwareDestroyProcessTap(tap) }
        tap = AudioObjectID(kAudioObjectUnknown)
    }
}

// MARK: - tests: an audio file instead of a device

/// BMEET_RECORD_MIC_FILE / BMEET_RECORD_SYSTEM_FILE (tests): that audio file, in real time (or
/// BMEET_RECORD_SPEED times faster), instead of the microphone / the system audio. Nothing asks
/// for a permission then, so the recorder's own logic runs anywhere (CI, a Mac without them).
final class FileSource {
    private let url: URL
    private let onAudio: ([Int16], UInt64?) -> Void
    private var timer: DispatchSourceTimer?
    private var samples: [Int16] = []
    private var pos = 0
    private let speed = Double(ProcessInfo.processInfo.environment["BMEET_RECORD_SPEED"] ?? "") ?? 1

    init(path: String, onAudio: @escaping ([Int16], UInt64?) -> Void) {
        url = URL(fileURLWithPath: path)
        self.onAudio = onAudio
    }

    func start() throws {
        let file = try AVAudioFile(forReading: url)
        let format = file.processingFormat
        guard let buf = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(file.length)) else { return }
        try file.read(into: buf)
        let frames = Int(buf.frameLength), channels = Int(format.channelCount)
        var mono = [Float](repeating: 0, count: frames)
        for c in 0..<channels { for i in 0..<frames { mono[i] += buf.floatChannelData![c][i] / Float(channels) } }
        samples = Converter().convert(mono, sampleRate: format.sampleRate)
        let t0 = nowHost()
        let started = hostSeconds(t0)
        let timer = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "bmeet-record.file"))
        timer.schedule(deadline: .now(), repeating: .milliseconds(50))
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            let due = min(self.samples.count, Int((hostSeconds(nowHost()) - started) * rate * self.speed))
            if due > self.pos {
                let host = t0 + UInt64(Double(self.pos) / rate / self.speed * 1e9 * Double(timebase.denom) / Double(timebase.numer))
                self.onAudio(Array(self.samples[self.pos..<due]), self.speed == 1 ? host : nil)
                self.pos = due
            }
        }
        timer.resume()
        self.timer = timer
    }

    func stop() {
        timer?.cancel()
        timer = nil
    }
}

// MARK: - commands

var stopping = false

func onStop(_ handler: @escaping () -> Void) {
    for sig in [SIGINT, SIGTERM] {
        signal(sig, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        source.setEventHandler { stopping = true; handler() }
        source.resume()
        sources.append(source)
    }
}
var sources: [DispatchSourceSignal] = []

func raw() -> Never {
    signal(SIGPIPE, SIG_IGN)
    let sink = Sink(fd: 1, wav: false, origin: nowHost())
    let feed: ([Int16], UInt64?) -> Void = { samples, _ in sink.append(samples, host: nil) }
    var stopMic: () -> Void
    if let path = ProcessInfo.processInfo.environment["BMEET_RECORD_MIC_FILE"] {
        let f = FileSource(path: path, onAudio: feed)
        do { try f.start() } catch { log("\(error.localizedDescription)"); exit(1) }
        stopMic = f.stop
    } else {
        let mic = Microphone(onAudio: feed)
        do { try mic.start() } catch { log("\(error.localizedDescription)"); exit(1) }
        stopMic = mic.stop
    }
    onStop { stopMic(); sink.finish(); exit(0) }
    Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { _ in
        if sink.isClosed { stopMic(); exit(0) }      // (the reader closed the pipe)
    }
    RunLoop.main.run()
    exit(0)
}

func firstLine(_ path: String) -> String {
    (try? String(contentsOfFile: path, encoding: .utf8))?.split(separator: "\n").first.map(String.init)?
        .trimmingCharacters(in: .whitespaces) ?? "idle"
}

final class Meeting {
    let dir: String
    let micOnly: Bool
    let stateFile: String
    let pointer: String
    var part = 0
    var mic: Microphone?
    var system: AnyObject?
    var files: [FileSource] = []
    var micSink: Sink?
    var remoteSink: Sink?

    init(dir: String, micOnly: Bool, stateFile: String, pointer: String) {
        self.dir = dir
        self.micOnly = micOnly
        self.stateFile = stateFile
        self.pointer = pointer
    }

    func startPart() {
        part += 1
        let origin = nowHost()
        let epoch = Date().timeIntervalSince1970
        try? String(format: "%.6f\n", epoch).write(toFile: "\(dir)/part-\(part).start", atomically: true, encoding: .utf8)
        guard let ms = Sink.open("\(dir)/part-\(part)-mic.wav", origin: origin) else { log("cannot write in \(dir)"); exit(1) }
        micSink = ms
        let env = ProcessInfo.processInfo.environment
        if let path = env["BMEET_RECORD_MIC_FILE"] {
            let f = FileSource(path: path) { samples, host in ms.append(samples, host: host) }
            do { try f.start() } catch { log("microphone file: \(error.localizedDescription)") }
            files.append(f)
        } else {
            let m = Microphone { samples, host in ms.append(samples, host: host) }
            do { try m.start() } catch { log("microphone: \(error.localizedDescription)") }
            mic = m
        }
        if !micOnly {
            guard let rs = Sink.open("\(dir)/part-\(part)-remote.wav", origin: origin) else { log("cannot write in \(dir)"); exit(1) }
            remoteSink = rs
            if let path = env["BMEET_RECORD_SYSTEM_FILE"] {
                let f = FileSource(path: path) { samples, host in rs.append(samples, host: host) }
                do { try f.start() } catch { log("system audio file: \(error.localizedDescription)") }
                files.append(f)
            } else if #available(macOS 14.2, *) {
                let s = SystemAudio { samples, host in rs.append(samples, host: host) }
                do { try s.start() } catch { log("system audio: \(error.localizedDescription)") }
                system = s
            } else {
                log("system audio needs macOS 14.2 or later: the remote track stays silent")
            }
        }
        log("part \(part) started")
    }

    func stopPart() {
        guard micSink != nil else { return }
        mic?.stop()
        if #available(macOS 14.2, *) { (system as? SystemAudio)?.stop() }
        files.forEach { $0.stop() }
        files = []
        mic = nil
        system = nil
        micSink?.finish()
        remoteSink?.finish()
        micSink = nil
        remoteSink = nil
        log("part \(part) ended")
    }

    func finish() {
        stopPart()
        if (try? String(contentsOfFile: pointer, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) == dir {
            unlink(pointer)
        }
    }

    func run() -> Never {
        let fm = FileManager.default
        try? fm.createDirectory(atPath: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        // The meeting runner writes "recording" right before it starts us; wait for it a moment.
        for _ in 0..<40 where firstLine(stateFile) != "recording" { usleep(250_000) }
        guard firstLine(stateFile) == "recording" else { log("the meeting did not start; not recording"); exit(1) }
        try? fm.createDirectory(atPath: (pointer as NSString).deletingLastPathComponent, withIntermediateDirectories: true,
                                attributes: [.posixPermissions: 0o700])
        try? "\(dir)\n".write(toFile: pointer, atomically: true, encoding: .utf8)
        onStop { [self] in finish(); exit(0) }
        Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [self] _ in
            switch firstLine(stateFile) {
            case "recording": if micSink == nil { startPart() }
            case "paused": stopPart()
            default: finish(); exit(0)
            }
        }
        RunLoop.main.run()
        exit(0)
    }
}

func runtimeDir() -> String {
    // $XDG_RUNTIME_DIR/bmeet, as BMeet's Python programs see it (macos/bmeetmac.py: your temp folder)
    if let x = ProcessInfo.processInfo.environment["XDG_RUNTIME_DIR"], !x.isEmpty { return x + "/bmeet" }
    var buf = [CChar](repeating: 0, count: Int(PATH_MAX))
    let n = confstr(_CS_DARWIN_USER_TEMP_DIR, &buf, buf.count)
    let tmp = n > 0 ? String(cString: buf) : NSTemporaryDirectory()
    return (tmp.hasSuffix("/") ? tmp : tmp + "/") + "bmeet"
}

func usage() -> Never {
    FileHandle.standardError.write("""
    usage: bmeet-record raw
           bmeet-record meeting DIR [--mic-only] [--state FILE] [--pointer FILE]
           bmeet-record check

    """.data(using: .utf8)!)
    exit(2)
}

var args = Array(CommandLine.arguments.dropFirst())
guard let command = args.first else { usage() }
args.removeFirst()
switch command {
case "raw":
    raw()
case "meeting":
    var dir: String?
    var micOnly = false
    var state = ProcessInfo.processInfo.environment["BMEET_STATE_FILE"] ?? runtimeDir() + "/meeting_state"
    var pointer = ProcessInfo.processInfo.environment["BMEET_RECORDING_FILE"] ?? runtimeDir() + "/recording"
    var i = 0
    while i < args.count {
        switch args[i] {
        case "--mic-only": micOnly = true
        case "--state": i += 1; guard i < args.count else { usage() }; state = args[i]
        case "--pointer": i += 1; guard i < args.count else { usage() }; pointer = args[i]
        default: if dir != nil || args[i].hasPrefix("-") { usage() }; dir = args[i]
        }
        i += 1
    }
    guard let dir else { usage() }
    Meeting(dir: (dir as NSString).standardizingPath, micOnly: micOnly, stateFile: state, pointer: pointer).run()
case "check":
    let status = AVCaptureDevice.authorizationStatus(for: .audio)
    func name(_ s: AVAuthorizationStatus) -> String {
        switch s {
        case .authorized: return "authorized"
        case .denied: return "denied"
        case .restricted: return "restricted"
        default: return "not determined"
        }
    }
    if status == .notDetermined {
        let sem = DispatchSemaphore(value: 0)
        AVCaptureDevice.requestAccess(for: .audio) { _ in sem.signal() }
        sem.wait()
    }
    print("{\"microphone\": \"\(name(AVCaptureDevice.authorizationStatus(for: .audio)))\"}")
    exit(0)
default:
    usage()
}
