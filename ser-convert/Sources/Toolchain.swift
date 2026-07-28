// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 MaxBotev
//
// Finds the external tools the app shells out to, and can install the one it
// needs via Homebrew.
//
// Deliberately limited: it will run `brew install ffmpeg` -- a well-known
// formula, through a package manager the user already trusts -- but it will not
// install Homebrew itself (a system-wide change wanting an admin password) and
// will not fetch a binary from some download site. Those stay a decision the
// user makes knowingly, so the app explains and links out instead.

import Foundation

@MainActor
final class Toolchain: ObservableObject {
    @Published var ffmpeg: String?
    @Published var brew: String?
    @Published var installing = false
    @Published var log = ""
    @Published var lastError: String?

    private static let ffmpegPaths = ["/opt/homebrew/bin/ffmpeg",
                                      "/usr/local/bin/ffmpeg",
                                      "/usr/bin/ffmpeg"]
    private static let brewPaths = ["/opt/homebrew/bin/brew",
                                    "/usr/local/bin/brew"]

    /// GUI apps launch with a bare PATH, so every lookup is by absolute path.
    static func find(_ candidates: [String]) -> String? {
        for p in candidates where FileManager.default.isExecutableFile(atPath: p) { return p }
        return nil
    }

    init() { refresh() }

    func refresh() {
        ffmpeg = Self.find(Self.ffmpegPaths)
        brew = Self.find(Self.brewPaths)
    }

    var ready: Bool { ffmpeg != nil }

    var advice: String {
        if ffmpeg != nil { return "" }
        if brew != nil { return "ffmpeg is missing. It can be installed for you." }
        return "ffmpeg is missing, and Homebrew isn't installed to fetch it."
    }

    func installFFmpeg() {
        guard let brew, !installing else { return }
        installing = true
        lastError = nil
        log = "$ brew install ffmpeg\n"
        Task.detached(priority: .userInitiated) {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: brew)
            p.arguments = ["install", "ffmpeg"]
            // Homebrew needs a usable PATH and HOME; a GUI app inherits neither.
            var env = ProcessInfo.processInfo.environment
            let brewBin = (brew as NSString).deletingLastPathComponent
            env["PATH"] = "\(brewBin):/usr/bin:/bin:/usr/sbin:/sbin"
            env["HOMEBREW_NO_AUTO_UPDATE"] = "1"      // keep it to the one job
            env["HOMEBREW_NO_ENV_HINTS"] = "1"
            env["NONINTERACTIVE"] = "1"               // never wait on a prompt
            p.environment = env

            let pipe = Pipe()
            p.standardOutput = pipe
            p.standardError = pipe
            pipe.fileHandleForReading.readabilityHandler = { fh in
                guard let s = String(data: fh.availableData, encoding: .utf8),
                      !s.isEmpty else { return }
                Task { @MainActor in
                    // keep the tail; a full brew install is thousands of lines
                    self.log = String((self.log + s).suffix(4000))
                }
            }
            do { try p.run() } catch {
                await MainActor.run {
                    self.installing = false
                    self.lastError = error.localizedDescription
                }
                return
            }
            await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
                DispatchQueue.global().async { p.waitUntilExit(); c.resume() }
            }
            pipe.fileHandleForReading.readabilityHandler = nil
            let code = p.terminationStatus
            await MainActor.run {
                self.installing = false
                self.refresh()
                if self.ffmpeg != nil {
                    self.log += "\n✅ ffmpeg installed at \(self.ffmpeg!)\n"
                } else {
                    self.lastError = code == 0
                        ? "brew finished but ffmpeg still isn't where the app looks"
                        : "brew exited with status \(code)"
                }
            }
        }
    }
}
