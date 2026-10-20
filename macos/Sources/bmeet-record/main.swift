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
// (BMeet.app): without it the tap delivers silence. Both follow the devices (headphones, AirPods,
// another sample rate): the microphone starts again, the tap is built again, and what fails is
// tried again and logged. WAV headers are written at the start with open sizes (npu-transcribe
// --live reads the files while they grow) and fixed at the end.

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
    /// written is cut off, so a device clock that runs fast does not drift ahead of the other
    /// track: the microphone and the system audio follow two devices' clocks).
    func append(_ samples: [Int16], host: UInt64?) {
        var samples = samples
        if let host {
            let at = Int64(((hostSeconds(host) - hostSeconds(origin)) * rate).rounded())
            if at > nextSample + Int64(rate / 50) {          // a gap of more than 20 ms: fill it
                let gap = min(at - nextSample, Int64(rate * 5))
                samples = [Int16](repeating: 0, count: Int(gap)) + samples
            } else if at < nextSample - Int64(rate / 50) {   // more than 20 ms ahead of its time: cut
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

/// Float32 buffers -> the average of all their channels. Each buffer says how many channels it
/// holds (interleaved, or one buffer per channel) and how many bytes: the frames are counted from
/// that, not from a format read earlier.
func mixToMono(_ list: UnsafeMutableAudioBufferListPointer) -> [Float] {
    var frames = Int.max, channels = 0
    for b in list where b.mNumberChannels > 0 && b.mData != nil {
        frames = min(frames, Int(b.mDataByteSize) / (4 * Int(b.mNumberChannels)))
        channels += Int(b.mNumberChannels)
    }
    guard channels > 0, frames > 0 else { return [] }
    var out = [Float](repeating: 0, count: frames)
    for b in list where b.mNumberChannels > 0 {
        guard let p = b.mData?.assumingMemoryBound(to: Float.self) else { continue }
        let n = Int(b.mNumberChannels)
        for i in 0..<frames {
            var s: Float = 0
            for c in 0..<n { s += p[i * n + c] }
            out[i] += s
        }
    }
    let scale = 1 / Float(channels)
    for i in 0..<frames { out[i] *= scale }
    return out
}

/// A flag a callback can set (one per tap device: its first odd buffer is reported once).
final class Flag { var on = false }

// MARK: - the microphone (AVAudioEngine)

final class Microphone {
    private let engine = AVAudioEngine()
    private let converter = Converter()
    private let onAudio: ([Int16], UInt64?) -> Void
    private var observer: NSObjectProtocol?
    private var stopped = false
    private var restarts = 0      // (a newer device change cancels the retries of an older one)

    init(onAudio: @escaping ([Int16], UInt64?) -> Void) { self.onAudio = onAudio }

    deinit { if let observer { NotificationCenter.default.removeObserver(observer) } }

    /// The default input device. start(), stop() and the restarts after a device change run on
    /// the main thread.
    func start() throws {
        // A new input device (AirPods connected, ...): start over on it. Added once, not on every
        // start: one more observer on each restart would double the restarts with every change.
        if observer == nil {
            observer = NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange,
                                                              object: engine, queue: .main) { [weak self] _ in
                guard let self, !self.stopped else { return }
                log("the input device changed: following it")
                self.restarts += 1
                self.restart(self.restarts, tries: 0)
            }
        }
        try startEngine()
    }

    private func startEngine() throws {
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
        do { try engine.start() } catch { input.removeTap(onBus: 0); throw error }
    }

    /// After a device change: the engine again, on the new device. Mid-switch that device may not
    /// be ready yet (0 Hz, or the start fails): then again 0.5 s later, 6 tries in all.
    private func restart(_ id: Int, tries: Int) {
        guard !stopped, id == restarts else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        do {
            try startEngine()
            if tries > 0 { log("the microphone is back") }
        } catch {
            if tries < 5 {
                if tries == 0 { log("microphone: \(error.localizedDescription); trying again") }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in self?.restart(id, tries: tries + 1) }
            } else {
                log("microphone: \(error.localizedDescription); it stays off until the input device changes again")
            }
        }
    }

    func stop() {
        stopped = true
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
    var size = UInt32(MemoryLayout<T>.size)   // (plain values only: an AudioObjectID, a rate, a stream format)
    return withUnsafeMutableBytes(of: &value) { AudioObjectGetPropertyData(object, &address, 0, nil, &size, $0.baseAddress!) }
}

func defaultOutputDevice() -> AudioObjectID? {
    var device = AudioObjectID(kAudioObjectUnknown)
    guard audioProperty(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultOutputDevice, &device) == noErr,
          device != kAudioObjectUnknown else { return nil }
    return device
}

/// The system audio: a process tap of everything the Mac plays, alone in a private aggregate
/// device. start(), stop() and every rebuild run on the main thread (Meeting's). The IO block has
/// a queue of its own and the property listeners another, and those only hand the work to the
/// main thread: stopping the device on the IO block's queue would wait for that queue itself.
@available(macOS 14.2, *)
final class SystemAudio {
    private var tap = AudioObjectID(kAudioObjectUnknown)
    private var aggregate = AudioObjectID(kAudioObjectUnknown)
    private var proc: AudioDeviceIOProcID?
    private let converter = Converter()
    private let onAudio: ([Int16], UInt64?) -> Void
    private let queue = DispatchQueue(label: "bmeet-record.tap")           // the IO block's, nothing else
    private let events = DispatchQueue(label: "bmeet-record.tap-events")   // the property listeners'
    private var outputListener: Listener?
    private var listeners: [Listener] = []
    private var setup: Setup?         // what the running tap was built for
    private var stopped = false
    private var rebuilds = 0          // (a newer change cancels the retries of an older one)
    private var builds = 0
    private var layoutRebuilds = 0

    private struct Listener {
        let object: AudioObjectID
        let address: AudioObjectPropertyAddress
        let block: AudioObjectPropertyListenerBlock
    }

    /// What the IO block was made for: the default output device and its sample rate, the tap
    /// device's sample rate, the tap's format.
    private struct Setup: Equatable {
        var output: AudioObjectID
        var outputRate: Float64
        var rate: Float64
        var tapRate: Float64
        var channels: UInt32
    }

    init(onAudio: @escaping ([Int16], UInt64?) -> Void) { self.onAudio = onAudio }

    deinit {
        if let outputListener { remove(outputListener) }
        teardown()
    }

    /// Tap the system audio, and keep following it: another default output device (headphones,
    /// AirPods) or a new sample rate or channel count (AirPods switching to their call profile,
    /// Audio MIDI Setup) builds it all again. What fails is logged and tried again.
    func start() {
        // (until stop(): a tap that could not be built gets another chance with the next device)
        outputListener = listen(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultOutputDevice,
                                "the output device changed")
        rebuilds += 1
        rebuild(rebuilds, why: nil, tries: 0)
    }

    func stop() {
        stopped = true
        if let outputListener { remove(outputListener) }
        outputListener = nil
        teardown()
    }

    /// Tell changed() on the main thread when `selector` of `object` changes.
    private func listen(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, _ why: String) -> Listener? {
        var address = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                                 mElement: kAudioObjectPropertyElementMain)
        let block: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            guard let self else { return }
            DispatchQueue.main.async { self.changed(why) }
        }
        guard AudioObjectAddPropertyListenerBlock(object, &address, events, block) == noErr else { return nil }
        return Listener(object: object, address: address, block: block)
    }

    private func remove(_ listener: Listener) {
        var address = listener.address
        AudioObjectRemovePropertyListenerBlock(listener.object, &address, events, listener.block)
    }

    /// The setup as it is now (zeros for a tap that is not there).
    private func current() -> Setup {
        let output = defaultOutputDevice() ?? AudioObjectID(kAudioObjectUnknown)
        var outputRate: Float64 = 0, rate: Float64 = 0
        var format = AudioStreamBasicDescription()
        if output != kAudioObjectUnknown { _ = audioProperty(output, kAudioDevicePropertyNominalSampleRate, &outputRate) }
        if aggregate != kAudioObjectUnknown { _ = audioProperty(aggregate, kAudioDevicePropertyNominalSampleRate, &rate) }
        if tap != kAudioObjectUnknown { _ = audioProperty(tap, kAudioTapPropertyFormat, &format) }
        return Setup(output: output, outputRate: outputRate, rate: rate, tapRate: format.mSampleRate,
                     channels: format.mChannelsPerFrame)
    }

    /// A listener fired: build it all again if something the IO block depends on changed (not for
    /// an event that changed none of that, our own start's say).
    private func changed(_ why: String) {
        guard !stopped else { return }
        if let setup, setup == current() { return }
        later(why)
    }

    /// The IO block got another channel count than the tap's format says (that just changed, or
    /// another stream is in): build it again, which reads the format anew; after 3 times, the
    /// buffers are taken as they come.
    private func oddBuffers(_ build: Int, _ channels: Int, _ expected: Int) {
        guard !stopped, build == builds else { return }
        layoutRebuilds += 1
        let then = layoutRebuilds < 3 ? "" : " (from now on taken as they come)"
        later("\(channels) channels of system audio where the tap has \(expected)\(then)")
    }

    /// A rebuild a moment later: one change often brings several events.
    private func later(_ why: String) {
        rebuilds += 1
        let id = rebuilds
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { [weak self] in self?.rebuild(id, why: why, tries: 0) }
    }

    /// Build the tap (again: what there is of it goes first). When that fails, again a little
    /// later, 8 tries in all; after that the next output device tries again.
    private func rebuild(_ id: Int, why: String?, tries: Int) {
        guard !stopped, id == rebuilds else { return }
        if let why { log("\(why): tapping the system audio again") }
        teardown()
        do {
            try build()
            if tries > 0 { log("system audio: tapped") }
        } catch {
            teardown()
            if tries < 7 {
                log("system audio: \(error.localizedDescription); trying again")
                let delay = min(0.5 * Double(1 << tries), 8)
                DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
                    self?.rebuild(id, why: nil, tries: tries + 1)
                }
            } else {
                log("system audio: \(error.localizedDescription); giving up until the output device changes")
            }
        }
    }

    /// The tap, its aggregate device, the IO block, and the listeners for what would change them.
    private func build() throws {
        func failure(_ what: String, _ status: OSStatus) -> NSError {
            NSError(domain: "bmeet-record", code: Int(status), userInfo: [NSLocalizedDescriptionKey: "\(what) (\(status))"])
        }
        let description = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
        description.uuid = UUID()
        description.name = "BMeet system audio"
        description.isPrivate = true
        description.muteBehavior = .unmuted
        var status = AudioHardwareCreateProcessTap(description, &tap)
        guard status == noErr else {
            tap = AudioObjectID(kAudioObjectUnknown)
            throw failure("cannot tap the system audio", status)
        }
        var format = AudioStreamBasicDescription()
        _ = audioProperty(tap, kAudioTapPropertyFormat, &format)
        // The tap alone, no sub-device: with the output device in it, the IO block would also get
        // that device's microphone (a USB headset; AirPods, which would switch to their call profile).
        let config: [String: Any] = [
            kAudioAggregateDeviceNameKey: "BMeet system audio",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapUIDKey: description.uuid.uuidString,
                                               kAudioSubTapDriftCompensationKey: true]],
        ]
        status = AudioHardwareCreateAggregateDevice(config as CFDictionary, &aggregate)
        guard status == noErr else {
            aggregate = AudioObjectID(kAudioObjectUnknown)
            throw failure("cannot create the tap device", status)
        }
        var nominal: Float64 = 0      // (the rate the IO block's buffers come at)
        _ = audioProperty(aggregate, kAudioDevicePropertyNominalSampleRate, &nominal)
        let sampleRate = nominal > 0 ? nominal : format.mSampleRate > 0 ? format.mSampleRate : 48_000
        let expected = layoutRebuilds < 3 ? Int(format.mChannelsPerFrame) : 0    // (0: take what comes)
        builds += 1
        let id = builds, odd = Flag()
        status = AudioDeviceCreateIOProcIDWithBlock(&proc, aggregate, queue) { [weak self] _, input, inputTime, _, _ in
            guard let self else { return }
            let list = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
            let channels = list.reduce(0) { $0 + Int($1.mNumberChannels) }
            if expected > 0 && channels != expected {       // (skipped: the main thread builds it again)
                if !odd.on { odd.on = true; DispatchQueue.main.async { self.oddBuffers(id, channels, expected) } }
                return
            }
            let mono = mixToMono(list)
            if mono.isEmpty { return }
            let host: UInt64? = inputTime.pointee.mFlags.contains(.hostTimeValid) ? inputTime.pointee.mHostTime : nil
            self.onAudio(self.converter.convert(mono, sampleRate: sampleRate), host)
        }
        guard status == noErr, let proc else { throw failure("no tap callback", status) }
        status = AudioDeviceStart(aggregate, proc)
        guard status == noErr else { throw failure("the tap does not start", status) }
        // A new sample rate or format, on the tap device, the tap or the output device (on macOS 14
        // the tap device may not tell): build it again.
        let now = current()
        listeners = [listen(aggregate, kAudioDevicePropertyNominalSampleRate, "the sample rate changed"),
                     listen(tap, kAudioTapPropertyFormat, "the system audio's format changed")].compactMap { $0 }
        if now.output != kAudioObjectUnknown,
           let l = listen(now.output, kAudioDevicePropertyNominalSampleRate, "the output device's sample rate changed") {
            listeners.append(l)
        }
        setup = now
    }

    /// Stop and destroy the IO block, the tap device and the tap, and drop their listeners.
    private func teardown() {
        for l in listeners { remove(l) }
        listeners = []
        setup = nil
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
/// BMEET_RECORD_SYSTEM_CLOCK (tests): the system audio's file as from a device whose clock runs
/// that many times fast, so more samples come than their host times allow.
final class FileSource {
    private let url: URL
    private let onAudio: ([Int16], UInt64?) -> Void
    private var timer: DispatchSourceTimer?
    private var samples: [Int16] = []
    private var pos = 0
    private let speed = Double(ProcessInfo.processInfo.environment["BMEET_RECORD_SPEED"] ?? "") ?? 1
    private let clock: Double

    init(path: String, clock: Double = 1, onAudio: @escaping ([Int16], UInt64?) -> Void) {
        url = URL(fileURLWithPath: path)
        self.clock = clock > 0 ? clock : 1
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
            let due = min(self.samples.count, Int((hostSeconds(nowHost()) - started) * rate * self.speed * self.clock))
            if due > self.pos {
                let seconds = Double(self.pos) / rate / self.speed / self.clock
                let host = t0 + UInt64(seconds * 1e9 * Double(timebase.denom) / Double(timebase.numer))
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
                let clock = Double(env["BMEET_RECORD_SYSTEM_CLOCK"] ?? "") ?? 1
                let f = FileSource(path: path, clock: clock) { samples, host in rs.append(samples, host: host) }
                do { try f.start() } catch { log("system audio file: \(error.localizedDescription)") }
                files.append(f)
            } else if #available(macOS 14.2, *) {
                let s = SystemAudio { samples, host in rs.append(samples, host: host) }
                s.start()      // (it logs what fails, and tries again)
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
    // BMeet's folder in your temp folder, as BMeet's Python programs see it (macos/bmeetmac.py), or
    // in $BMEET_RUNTIME_ROOT (tests). Not $XDG_RUNTIME_DIR: a shell may export one, BMeet.app has none.
    var root = ProcessInfo.processInfo.environment["BMEET_RUNTIME_ROOT"] ?? ""
    if root.isEmpty {
        var buf = [CChar](repeating: 0, count: Int(PATH_MAX))
        let n = confstr(_CS_DARWIN_USER_TEMP_DIR, &buf, buf.count)
        root = n > 0 ? String(cString: buf) : NSTemporaryDirectory()
    }
    return (root.hasSuffix("/") ? root : root + "/") + "bmeet"
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
