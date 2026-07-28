// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 MaxBotev
//
// Runs ffmpeg out of process and turns its -progress stream into real progress.
//
// Jobs run one at a time on purpose: ffmpeg already saturates the cores on a
// frame this size, so running several in parallel finishes no sooner and just
// makes every bar crawl at once.

import Foundation
import Combine

enum Preset: String, CaseIterable, Identifiable {
    case proresHQ = "ProRes 422 HQ"
    case prores4444 = "ProRes 4444"
    case ffv1 = "FFV1 (lossless)"
    var id: String { rawValue }

    var ext: String { self == .ffv1 ? "mkv" : "mov" }

    var args: [String] {
        switch self {
        case .proresHQ:   return ["-c:v", "prores_ks", "-profile:v", "3", "-vendor", "apl0"]
        case .prores4444: return ["-c:v", "prores_ks", "-profile:v", "4", "-vendor", "apl0"]
        case .ffv1:       return ["-c:v", "ffv1", "-level", "3", "-g", "1"]
        }
    }

    var blurb: String {
        switch self {
        case .proresHQ:   return "Edit-friendly, ~5× smaller. Lossy."
        case .prores4444: return "Higher fidelity, larger files. Lossy."
        case .ffv1:       return "Mathematically lossless — keep for stacking."
        }
    }
}


/// Playback speed. Implemented purely by declaring a lower INPUT rate to
/// ffmpeg -- every frame is still written exactly once, nothing is duplicated,
/// interpolated or dropped. The file simply carries a slower frame rate, which
/// is what you want for studying a transit that is over in a fraction of a
/// second.
enum Speed: String, CaseIterable, Identifiable {
    case realtime = "Real-time"
    case half     = "1/2 speed"
    case quarter  = "1/4 speed"
    case eighth   = "1/8 speed"
    var id: String { rawValue }

    var divisor: Double {
        switch self {
        case .realtime: return 1
        case .half:     return 2
        case .quarter:  return 4
        case .eighth:   return 8
        }
    }

    var blurb: String {
        self == .realtime ? "" : "\(Int(divisor))× slower — every frame kept"
    }
}

@MainActor
final class Job: ObservableObject, Identifiable {
    let id = UUID()
    let url: URL
    @Published var info: SERInfo?
    @Published var state: State = .queued
    @Published var frame = 0
    @Published var speed = ""
    @Published var message = ""
    @Published var outURL: URL?
    @Published var outSize: Int64 = 0
    @Published var elapsed: Double = 0
    // Settings this job was actually converted with. Changing the codec or the
    // speed means a DIFFERENT output file, so a finished job becomes
    // convertible again rather than being stuck at "done".
    @Published var donePreset: Preset?
    @Published var doneSpeed: Speed?

    enum State: Equatable { case queued, reading, running, done, failed, cancelled }

    init(url: URL) { self.url = url }

    /// Adaptive byte units — "0.00 GB" for a short capture reads like a failure.
    static func human(_ bytes: Int64) -> String {
        let b = Double(bytes)
        if b >= 1e9 { return String(format: "%.2f GB", b / 1e9) }
        if b >= 1e6 { return String(format: "%.0f MB", b / 1e6) }
        return String(format: "%.0f KB", b / 1e3)
    }

    var name: String { url.lastPathComponent }
    var total: Int { info?.frameCount ?? 0 }
    var fraction: Double {
        guard total > 0 else { return 0 }
        return min(1.0, Double(frame) / Double(total))
    }

    var statusLine: String {
        switch state {
        case .queued:  return info.map { $0.summary } ?? "waiting…"
        case .reading: return "reading header…"
        case .running:
            let pct = Int(fraction * 100)
            return "\(pct)% · frame \(frame)/\(total)\(speed.isEmpty ? "" : " · \(speed)")"
        case .done:
            let src = Double(info?.fileSize ?? 0)
            let ratio = outSize > 0 && src > 0
                ? String(format: " · %.1f× smaller", src / Double(outSize)) : ""
            let t = elapsed < 1 ? String(format: "%.1fs", elapsed)
                  : elapsed < 90 ? String(format: "%.0fs", elapsed)
                  : String(format: "%dm %02ds", Int(elapsed) / 60, Int(elapsed) % 60)
            return "done — \(Job.human(outSize)) in \(t)\(ratio)"
        case .failed:    return "failed — \(message)"
        case .cancelled: return "cancelled"
        }
    }
}

/// Thread-safe tail of ffmpeg's stderr, for reporting why a job failed.
final class StderrTail: @unchecked Sendable {
    private let lock = NSLock()
    private var buf = ""
    func append(_ s: String) {
        lock.lock(); defer { lock.unlock() }
        buf = String((buf + s).suffix(600))
    }
    func lastLine() -> String {
        lock.lock(); defer { lock.unlock() }
        return buf.split(separator: "\n")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .last { !$0.isEmpty } ?? ""
    }
}

@MainActor
final class Converter: ObservableObject {
    @Published var jobs: [Job] = []
    @Published var preset: Preset = .proresHQ
    @Published var speed: Speed = .realtime
    @Published var isRunning = false
    @Published var overwrite = false

    private var proc: Process?
    private var cancelled = false
    // Job is its own ObservableObject, so a change to job.state does NOT
    // invalidate views watching the Converter. Forward each job's changes so
    // the footer count and the Convert button stay truthful.
    private var bag = Set<AnyCancellable>()

    var ffmpegPath: String? {
        for p in ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"]
        where FileManager.default.isExecutableFile(atPath: p) { return p }
        return nil
    }

    var overallFraction: Double {
        let active = jobs.filter { $0.state != .cancelled }
        guard !active.isEmpty else { return 0 }
        return active.reduce(0.0) { $0 + ($1.state == .done ? 1.0 : $1.fraction) }
             / Double(active.count)
    }

    func add(_ urls: [URL]) {
        for u in urls where !jobs.contains(where: { $0.url == u }) {
            let j = Job(url: u)
            j.objectWillChange
                .sink { [weak self] _ in self?.objectWillChange.send() }
                .store(in: &bag)
            jobs.append(j)
            j.state = .reading
            Task.detached {
                do {
                    let i = try SERReader.read(u)
                    await MainActor.run { j.info = i; j.state = .queued }
                } catch {
                    await MainActor.run {
                        j.state = .failed
                        j.message = error.localizedDescription
                    }
                }
            }
        }
    }

    /// A finished job whose settings no longer match the current selection --
    /// converting again would genuinely produce a different file.
    func isStale(_ j: Job) -> Bool {
        j.state == .done && (j.donePreset != preset || j.doneSpeed != speed)
    }

    /// Anything Convert could act on: queued, retryable, or now-stale results.
    var pendingCount: Int { jobs.filter { $0.state == .queued }.count }
    var retryableCount: Int {
        jobs.filter { $0.state == .failed || $0.state == .cancelled }.count
    }
    var staleCount: Int { jobs.filter { isStale($0) }.count }
    var canStart: Bool {
        ffmpegPath != nil && !isRunning
            && (pendingCount + retryableCount + staleCount) > 0
    }

    func retryAll() {
        for j in jobs where j.state == .failed || j.state == .cancelled || isStale(j) {
            j.message = ""
            j.frame = 0
            j.state = j.info == nil ? .failed : .queued
        }
    }

    func retry(_ job: Job) {
        guard job.info != nil else { return }
        job.message = ""; job.frame = 0; job.state = .queued
    }


    func removeCompleted() { jobs.removeAll { $0.state == .done } }
    func clear() { guard !isRunning else { return }; jobs.removeAll() }

    func cancel() {
        cancelled = true
        proc?.terminate()
        for j in jobs where j.state == .queued { j.state = .cancelled }
    }

    func start() {
        guard !isRunning, ffmpegPath != nil else { return }
        retryAll()                      // a second Convert press retries failures
        guard jobs.contains(where: { $0.state == .queued }) else { return }
        cancelled = false
        isRunning = true
        Task { await runQueue(); isRunning = false }
    }

    private func runQueue() async {
        for job in jobs where job.state == .queued {
            if cancelled { job.state = .cancelled; continue }
            await run(job)
        }
    }

    private func run(_ job: Job) async {
        guard let ff = ffmpegPath, let info = job.info else {
            job.state = .failed; job.message = "no ffmpeg or unreadable header"; return
        }
        // Slow-motion output gets its own name, so it can never silently
        // overwrite the real-time version of the same capture.
        let suffix = speed == .realtime ? "" : "_\(Int(speed.divisor))x-slow"
        let base = job.url.deletingPathExtension().lastPathComponent + suffix
        let outURL = job.url.deletingLastPathComponent()
            .appendingPathComponent(base)
            .appendingPathExtension(preset.ext)
        if FileManager.default.fileExists(atPath: outURL.path) && !overwrite {
            job.state = .failed
            job.message = "\(outURL.lastPathComponent) already exists (enable Overwrite)"
            return
        }

        job.state = .running
        job.frame = 0
        let t0 = Date()

        // -r before -i sets the INPUT rate: SER carries no fps field, so without
        // this every output plays at ffmpeg's 25 fps default.
        let outFPS = max(1.0, info.effectiveFPS / speed.divisor)
        var args = ["-hide_banner", "-nostdin", "-y",
                    "-r", String(format: "%.6f", outFPS),
                    "-i", job.url.path]
        args += preset.args
        args += ["-progress", "pipe:1", "-nostats", outURL.path]

        let p = Process()
        p.executableURL = URL(fileURLWithPath: ff)
        p.arguments = args
        let pipe = Pipe(), errPipe = Pipe()
        p.standardOutput = pipe
        p.standardError = errPipe
        proc = p

        // ffmpeg's stderr arrives on a reader thread, so the tail buffer has to
        // be shared safely rather than captured as a plain var.
        let tail = StderrTail()
        errPipe.fileHandleForReading.readabilityHandler = { fh in
            if let s = String(data: fh.availableData, encoding: .utf8), !s.isEmpty {
                tail.append(s)
            }
        }
        pipe.fileHandleForReading.readabilityHandler = { [weak job] fh in
            guard let job, let s = String(data: fh.availableData, encoding: .utf8) else { return }
            for line in s.split(separator: "\n") {
                let kv = line.split(separator: "=", maxSplits: 1)
                guard kv.count == 2 else { continue }
                let k = kv[0].trimmingCharacters(in: .whitespaces)
                let v = kv[1].trimmingCharacters(in: .whitespaces)
                Task { @MainActor in
                    switch k {
                    case "frame": job.frame = Int(v) ?? job.frame
                    case "speed": job.speed = v == "N/A" ? "" : v
                    default: break
                    }
                }
            }
        }

        do { try p.run() } catch {
            job.state = .failed; job.message = error.localizedDescription; return
        }
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            DispatchQueue.global().async { p.waitUntilExit(); c.resume() }
        }
        pipe.fileHandleForReading.readabilityHandler = nil
        errPipe.fileHandleForReading.readabilityHandler = nil
        proc = nil

        job.elapsed = Date().timeIntervalSince(t0)
        if cancelled {
            job.state = .cancelled
            try? FileManager.default.removeItem(at: outURL)  // no half-written files
            return
        }
        if p.terminationStatus == 0 {
            job.outURL = outURL
            job.outSize = Int64((try? FileManager.default
                .attributesOfItem(atPath: outURL.path)[.size] as? Int) ?? 0)
            job.frame = job.total
            job.donePreset = preset
            job.doneSpeed = speed
            job.state = .done
        } else {
            job.state = .failed
            let lastLine = tail.lastLine()
            job.message = lastLine.isEmpty ? "ffmpeg exit \(p.terminationStatus)" : lastLine
            try? FileManager.default.removeItem(at: outURL)
        }
    }
}
