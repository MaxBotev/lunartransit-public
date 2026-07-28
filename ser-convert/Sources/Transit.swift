// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 MaxBotev
//
// Finds the frames where an aircraft crosses the lunar disc, and exports them.
//
// A silhouetted aircraft is far darker than even the darkest maria, so counting
// near-black pixels INSIDE the disc separates a transit from everything else by
// orders of magnitude. On a real capture the count went from a baseline of ~40
// to 28155 at mid-transit -- no threshold tuning needed to see that.
//
// Only every Nth pixel is examined, which is plenty: the aim is to find the
// frames, not to measure them. Extraction is then done by ffmpeg straight from
// the SER, so the stills are bit-exact from the original sensor data rather
// than a re-encode of an already-lossy movie.

import Foundation
import AppKit

struct TransitRun: Identifiable, Equatable {
    let id = UUID()
    var start: Int
    var end: Int
    var peakFrame: Int
    var peakScore: Int
    var count: Int { end - start + 1 }
    func seconds(_ fps: Double) -> Double { Double(count) / max(fps, 0.001) }
}

@MainActor
final class TransitScanner: ObservableObject {
    @Published var url: URL?
    @Published var info: SERInfo?
    @Published var runs: [TransitRun] = []
    @Published var selected: TransitRun?
    @Published var scanning = false
    @Published var exporting = false
    @Published var progress: Double = 0
    @Published var status = ""
    @Published var preview: NSImage?
    @Published var previewFrame: Int?
    @Published var rotation: Double = 0        // degrees, clockwise
    @Published var padFrames: Int = 2          // clean frames kept either side
    @Published var lastExport: URL?

    var ffmpeg: String? {
        for p in ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"]
        where FileManager.default.isExecutableFile(atPath: p) { return p }
        return nil
    }

    func load(_ u: URL) {
        url = u; runs = []; selected = nil; preview = nil; previewFrame = nil
        status = "reading header…"
        Task.detached {
            do {
                let i = try SERReader.read(u)
                await MainActor.run { self.info = i; self.status = i.summary }
            } catch {
                await MainActor.run {
                    self.status = "not a readable SER: \(error.localizedDescription)"
                }
            }
        }
    }

    // ---- detection -------------------------------------------------------
    func scan() {
        guard let u = url, let i = info, !scanning else { return }
        scanning = true; progress = 0; runs = []; selected = nil
        status = "scanning \(i.frameCount) frames…"
        Task.detached(priority: .userInitiated) {
            do {
                let found = try Self.detect(u, i) { p in
                    Task { @MainActor in self.progress = p }
                }
                await MainActor.run {
                    self.runs = found
                    self.scanning = false
                    self.progress = 1
                    if found.isEmpty {
                        self.status = "no transit found — no dark object crossed the disc"
                    } else {
                        self.status = "\(found.count) crossing\(found.count == 1 ? "" : "s") found"
                        self.select(found.max(by: { $0.peakScore < $1.peakScore })!)
                    }
                }
            } catch {
                await MainActor.run {
                    self.scanning = false
                    self.status = "scan failed: \(error.localizedDescription)"
                }
            }
        }
    }

    /// Returns the frame ranges where something dark crossed the disc.
    nonisolated static func detect(_ u: URL, _ info: SERInfo,
                                   _ onProgress: @escaping (Double) -> Void) throws -> [TransitRun] {
        let h = try FileHandle(forReadingFrom: u)
        defer { try? h.close() }
        let w = info.width, ht = info.height, n = info.frameCount
        let bpp = info.depth <= 8 ? 1 : 2
        let frameBytes = info.bytesPerFrame
        let step = 4                       // even: stays on one Bayer position

        func sample(_ idx: Int) throws -> [Float] {
            try h.seek(toOffset: UInt64(178 + idx * frameBytes))
            guard let d = try h.read(upToCount: frameBytes), d.count == frameBytes else {
                throw SERReader.Err.msg("short read at frame \(idx)")
            }
            var out = [Float](); out.reserveCapacity((ht / step) * (w / step))
            d.withUnsafeBytes { raw in
                var y = 0
                while y < ht {
                    let row = y * w * bpp
                    var x = 0
                    while x < w {
                        // for 16-bit take the high byte: only relative levels matter
                        let o = row + x * bpp + (bpp == 2 ? 1 : 0)
                        out.append(Float(raw[o]))
                        x += step
                    }
                    y += step
                }
            }
            return out
        }

        // Reference = median of frames spread through the file, i.e. the static
        // Moon with any transient (a passing aircraft) voted out.
        let probes = [0, n / 4, n / 2, 3 * n / 4, n - 1].map { max(0, min(n - 1, $0)) }
        var stack = [[Float]]()
        for p in probes { stack.append(try sample(p)) }
        let m = stack[0].count
        var ref = [Float](repeating: 0, count: m)
        for i in 0..<m {
            var col = stack.map { $0[i] }
            col.sort()
            ref[i] = col[col.count / 2]
        }
        let peak = ref.max() ?? 0
        guard peak > 25 else { throw SERReader.Err.msg("frames look blank (peak \(Int(peak)))") }
        let discCut = peak * 0.25, darkCut = peak * 0.16
        var disc = [Bool](repeating: false, count: m)
        var discN = 0
        for i in 0..<m where ref[i] > discCut { disc[i] = true; discN += 1 }
        guard discN > 100 else { throw SERReader.Err.msg("no lunar disc found in the frames") }

        var scores = [Int](repeating: 0, count: n)
        for f in 0..<n {
            let s = try sample(f)
            var c = 0
            for i in 0..<m where disc[i] && s[i] < darkCut { c += 1 }
            scores[f] = c
            if f % 32 == 0 { onProgress(Double(f) / Double(n)) }
        }
        onProgress(1)

        // Baseline from the median, so lunar terrain and noise do not count.
        var sorted = scores; sorted.sort()
        let med = Double(sorted[sorted.count / 2])
        let thr = max(med * 3 + 20, 40)

        var out = [TransitRun]()
        var i = 0
        while i < n {
            guard Double(scores[i]) > thr else { i += 1; continue }
            var j = i
            var gap = 0
            var k = i
            while k < n {
                if Double(scores[k]) > thr { j = k; gap = 0 }
                else { gap += 1; if gap > 3 { break } }
                k += 1
            }
            var pf = i, ps = 0
            for q in i...j where scores[q] > ps { ps = scores[q]; pf = q }
            // A real crossing lasts more than a frame or two; single-frame
            // spikes are cosmic rays, satellites glinting, or noise.
            if j - i + 1 >= 3 {
                out.append(TransitRun(start: i, end: j, peakFrame: pf, peakScore: ps))
            }
            i = j + 1
        }
        // Strongest first: a real crossing outscores noise by orders of
        // magnitude (28155 vs ~100 on a measured capture), so the one you
        // want is always at the top.
        return out.sorted { $0.peakScore > $1.peakScore }
    }

    // ---- preview ---------------------------------------------------------
    func select(_ r: TransitRun) {
        selected = r
        loadPreview(frame: r.peakFrame)
    }

    func loadPreview(frame: Int) {
        guard let u = url, let ff = ffmpeg else { return }
        previewFrame = frame
        let tmp = FileManager.default.temporaryDirectory
            .appendingPathComponent("serconvert_preview.png")
        Task.detached(priority: .userInitiated) {
            try? FileManager.default.removeItem(at: tmp)
            let p = Process()
            p.executableURL = URL(fileURLWithPath: ff)
            // scaled down: this is for judging the rotation, not for pixel-peeping
            p.arguments = ["-hide_banner", "-v", "error", "-y", "-i", u.path,
                           "-vf", "select='eq(n\\,\(frame))',scale=1100:-1",
                           "-vsync", "0", "-frames:v", "1", tmp.path]
            p.standardError = Pipe(); p.standardOutput = Pipe()
            try? p.run(); p.waitUntilExit()
            let img = NSImage(contentsOf: tmp)
            await MainActor.run { self.preview = img }
        }
    }

    // ---- export ----------------------------------------------------------
    /// ffmpeg filter for the chosen rotation. Exact quarter turns use transpose,
    /// which just re-orders pixels; arbitrary angles must resample, so they are
    /// applied once here, straight from the SER, rather than to an export.
    static func rotateFilter(_ deg: Double) -> String? {
        let d = ((deg.truncatingRemainder(dividingBy: 360)) + 360)
            .truncatingRemainder(dividingBy: 360)
        if abs(d) < 0.01 { return nil }
        if abs(d - 90) < 0.01 { return "transpose=1" }
        if abs(d - 180) < 0.01 { return "transpose=1,transpose=1" }
        if abs(d - 270) < 0.01 { return "transpose=2" }
        let rad = d * .pi / 180.0
        return String(format:
            "rotate=%.6f:ow=rotw(%.6f):oh=roth(%.6f):fillcolor=black", rad, rad, rad)
    }

    func export() {
        guard let u = url, let r = selected, let ff = ffmpeg, !exporting else { return }
        exporting = true
        let lo = max(0, r.start - padFrames)
        let hi = min((info?.frameCount ?? r.end) - 1, r.end + padFrames)
        let dir = u.deletingLastPathComponent()
            .appendingPathComponent(u.deletingPathExtension().lastPathComponent + "_transit")
        let rot = Self.rotateFilter(rotation)
        status = "exporting frames \(lo)–\(hi)…"
        Task.detached(priority: .userInitiated) {
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            var vf = "select='between(n\\,\(lo)\\,\(hi))'"
            if let rot { vf += "," + rot }
            let p = Process()
            p.executableURL = URL(fileURLWithPath: ff)
            p.arguments = ["-hide_banner", "-v", "error", "-y", "-i", u.path,
                           "-vf", vf, "-vsync", "0", "-frame_pts", "1",
                           dir.appendingPathComponent("f_%04d.png").path]
            let err = Pipe(); p.standardError = err; p.standardOutput = Pipe()
            try? p.run()
            let e = err.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            let ok = p.terminationStatus == 0
            let count = (try? FileManager.default
                .contentsOfDirectory(atPath: dir.path).filter { $0.hasSuffix(".png") }.count) ?? 0
            await MainActor.run {
                self.exporting = false
                if ok {
                    self.lastExport = dir
                    self.status = "exported \(count) frames to \(dir.lastPathComponent)"
                } else {
                    let msg = String(data: e, encoding: .utf8)?
                        .split(separator: "\n").last.map(String.init) ?? "ffmpeg failed"
                    self.status = "export failed: \(msg)"
                }
            }
        }
    }
}
