// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 MaxBotev
//
// SER file inspection: header fields plus the true frame rate.
//
// The frame rate matters more than it looks. SER has no fps field -- timing
// lives in an optional trailer of per-frame timestamps -- so ffmpeg falls back
// to 25 fps and every converted file plays at the wrong speed. Reading the
// trailer here is what makes the output correct.

import Foundation

struct SERInfo {
    var width = 0
    var height = 0
    var depth = 0
    var frameCount = 0
    var colorID = 0
    var instrument = ""
    var fileSize: Int64 = 0
    var fps: Double?          // nil when the file carries no timestamp trailer
    var startDate: Date?
    var durationSec: Double?
    var timingWarning: String?

    var colorName: String {
        switch colorID {
        case 0: return "MONO"
        case 8: return "BAYER RGGB"
        case 9: return "BAYER GRBG"
        case 10: return "BAYER GBRG"
        case 11: return "BAYER BGGR"
        case 100: return "RGB"
        case 101: return "BGR"
        default: return "ColorID \(colorID)"
        }
    }

    var bytesPerFrame: Int {
        let bpp = depth <= 8 ? 1 : 2
        let planes = colorID >= 100 ? 3 : 1
        return width * height * bpp * planes
    }

    /// Rate to hand ffmpeg: measured when available, else the SER-conventional 25.
    var effectiveFPS: Double { fps ?? 25.0 }

    var summary: String {
        let f = fps.map { String(format: "%.3f fps", $0) } ?? "no timestamps (assuming 25)"
        return "\(width)×\(height) · \(depth)-bit · \(colorName) · \(frameCount) frames · \(f)"
    }
}

enum SERReader {
    static func read(_ url: URL) throws -> SERInfo {
        let h = try FileHandle(forReadingFrom: url)
        defer { try? h.close() }

        guard let header = try h.read(upToCount: 178), header.count == 178 else {
            throw Err.msg("file is too short to be a SER (need a 178-byte header)")
        }
        let magic = String(bytes: header[0..<14], encoding: .ascii) ?? ""
        guard magic == "LUCAM-RECORDER" else {
            throw Err.msg("not a SER file (header says \"\(magic)\")")
        }

        func i32(_ off: Int) -> Int {
            let b = header[off..<(off + 4)]
            return Int(Int32(littleEndian: b.withUnsafeBytes { $0.loadUnaligned(as: Int32.self) }))
        }
        func str(_ off: Int) -> String {
            let raw = Array(header[off..<(off + 40)])
            let cut = raw.prefix { $0 != 0 }
            return String(bytes: cut, encoding: .utf8)?
                .trimmingCharacters(in: .whitespaces) ?? ""
        }

        var info = SERInfo()
        info.colorID    = i32(18)
        info.width      = i32(26)
        info.height     = i32(30)
        info.depth      = i32(34)
        info.frameCount = i32(38)
        info.instrument = str(82)
        info.fileSize   = Int64((try? FileManager.default
            .attributesOfItem(atPath: url.path)[.size] as? Int) ?? 0)

        guard info.width > 0, info.height > 0, info.frameCount > 0 else {
            throw Err.msg("header has implausible geometry")
        }

        try readTimestamps(h, &info)
        return info
    }

    /// Trailer = frameCount × Int64 ticks (100 ns since 0001-01-01), if present.
    private static func readTimestamps(_ h: FileHandle, _ info: inout SERInfo) throws {
        let payloadEnd = 178 + Int64(info.bytesPerFrame) * Int64(info.frameCount)
        guard info.fileSize >= payloadEnd + Int64(info.frameCount) * 8 else { return }
        try h.seek(toOffset: UInt64(payloadEnd))
        guard let raw = try h.read(upToCount: info.frameCount * 8),
              raw.count == info.frameCount * 8 else { return }

        var ticks = [Int64](repeating: 0, count: info.frameCount)
        raw.withUnsafeBytes { buf in
            for i in 0..<info.frameCount {
                ticks[i] = Int64(littleEndian: buf.loadUnaligned(fromByteOffset: i * 8,
                                                                 as: Int64.self))
            }
        }
        guard let first = ticks.first, let last = ticks.last, info.frameCount > 1 else { return }

        let span = Double(last - first) / 1e7
        guard span > 0 else { return }
        info.fps = Double(info.frameCount - 1) / span
        info.durationSec = span
        // .NET ticks -> Date (ticks at the Unix epoch = 621355968000000000)
        info.startDate = Date(timeIntervalSince1970: Double(first - 621_355_968_000_000_000) / 1e7)

        // Flag irregular capture timing: it does not stop the conversion, but a
        // constant-rate movie quietly smooths it away, which matters if the
        // frames are ever used for timing rather than looking at.
        var minGap = Double.greatestFiniteMagnitude, maxGap = -Double.greatestFiniteMagnitude
        for i in 0..<(info.frameCount - 1) {
            let g = Double(ticks[i + 1] - ticks[i]) / 1e7
            minGap = min(minGap, g); maxGap = max(maxGap, g)
        }
        let mean = span / Double(info.frameCount - 1)
        var notes: [String] = []
        if minGap < 0 { notes.append("out-of-order timestamps") }
        if maxGap > mean * 3 { notes.append(String(format: "stall of %.0f ms", maxGap * 1000)) }
        if !notes.isEmpty { info.timingWarning = notes.joined(separator: ", ") }
    }

    enum Err: LocalizedError {
        case msg(String)
        var errorDescription: String? { if case .msg(let m) = self { return m }; return nil }
    }
}
