// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 MaxBotev
// SER → ProRes / FFV1 batch converter.

import SwiftUI
import AppKit
import UniformTypeIdentifiers

let BG      = Color(red: 0.02, green: 0.03, blue: 0.06)
let PANEL   = Color(red: 0.04, green: 0.07, blue: 0.12)
let LINE    = Color(red: 0.09, green: 0.15, blue: 0.24)
let CYAN    = Color(red: 0.14, green: 0.90, blue: 1.00)
let GREEN   = Color(red: 0.22, green: 1.00, blue: 0.69)
let AMBER   = Color(red: 1.00, green: 0.80, blue: 0.30)
let RED     = Color(red: 1.00, green: 0.30, blue: 0.43)
let DIM     = Color(red: 0.36, green: 0.48, blue: 0.61)
let MONO    = Font.system(.body, design: .monospaced)

@main
struct SERConvertApp: App {
    @StateObject private var conv = Converter()
    @StateObject private var scan = TransitScanner()
    @StateObject private var tools = Toolchain()
    var body: some Scene {
        WindowGroup("SER Convert") {
            RootView().environmentObject(conv).environmentObject(scan)
                .environmentObject(tools)
                .frame(minWidth: 820, minHeight: 520)
                .preferredColorScheme(.dark)
        }
        .windowResizability(.contentMinSize)
    }
}

enum Tab: String, CaseIterable, Identifiable {
    case convert = "Convert"
    case transit = "Transit"
    var id: String { rawValue }
}

struct RootView: View {
    @State private var tab: Tab = .convert
    @EnvironmentObject var tools: Toolchain
    var body: some View {
        // Nothing in this app works without ffmpeg, so offer to fix that first
        // rather than letting every action fail one at a time.
        if !tools.ready { SetupView() } else { main }
    }

    private var main: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Text("SER▸CONVERT")
                    .font(.system(size: 15, weight: .bold, design: .monospaced))
                    .foregroundStyle(.white).tracking(2)
                Picker("", selection: $tab) {
                    ForEach(Tab.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented).labelsHidden().frame(width: 190)
                Spacer()
            }
            .padding(.horizontal, 14).padding(.vertical, 8)
            .background(PANEL)
            Divider().overlay(LINE)
            if tab == .convert { ContentView() } else { TransitTab() }
        }
        .background(BG)
    }
}

struct ContentView: View {
    @EnvironmentObject var conv: Converter
    @State private var dragOver = false

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider().overlay(LINE)
            list
            Divider().overlay(LINE)
            footer
        }
        .background(BG)
        .onDrop(of: [.fileURL], isTargeted: $dragOver) { providers in
            load(providers); return true
        }
        .overlay(dragOver ? CYAN.opacity(0.07) : .clear)
        .overlay(alignment: .top) {
            if dragOver {
                Text("DROP .SER FILES")
                    .font(.system(.caption, design: .monospaced)).foregroundStyle(CYAN)
                    .padding(8).background(PANEL).overlay(Rectangle().stroke(CYAN))
                    .padding(.top, 60)
            }
        }
    }

    private var header: some View {
        HStack(spacing: 12) {
            Picker("", selection: $conv.preset) {
                ForEach(Preset.allCases) { Text($0.rawValue).tag($0) }
            }
            .labelsHidden().frame(width: 190).disabled(conv.isRunning)
            Picker("", selection: $conv.speed) {
                ForEach(Speed.allCases) { Text($0.rawValue).tag($0) }
            }
            .labelsHidden().frame(width: 130).disabled(conv.isRunning)
            Text(conv.speed == .realtime ? conv.preset.blurb : conv.speed.blurb)
                .font(.caption)
                .foregroundStyle(conv.speed == .realtime ? DIM : AMBER)
            Spacer()
            Toggle("Overwrite", isOn: $conv.overwrite)
                .toggleStyle(.checkbox).font(.caption).disabled(conv.isRunning)
            Button("Add Files…") { pick() }.disabled(conv.isRunning)
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(PANEL)
    }

    private var list: some View {
        Group {
            if conv.jobs.isEmpty {
                VStack(spacing: 10) {
                    Spacer()
                    Text("Drop .ser files here").font(MONO).foregroundStyle(DIM)
                    Text("output is written beside each source file")
                        .font(.caption).foregroundStyle(DIM.opacity(0.7))
                    Spacer()
                }.frame(maxWidth: .infinity)
            } else {
                ScrollView {
                    VStack(spacing: 6) {
                        ForEach(conv.jobs) { JobRow(job: $0) }
                    }.padding(10)
                }
            }
        }.frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var footer: some View {
        HStack(spacing: 12) {
            if conv.isRunning {
                ProgressView(value: conv.overallFraction).frame(width: 190).tint(CYAN)
                Text("\(Int(conv.overallFraction * 100))% overall")
                    .font(.system(.caption, design: .monospaced)).foregroundStyle(CYAN)
            } else {
                let n = conv.pendingCount, r = conv.retryableCount, st = conv.staleCount
                Text(n > 0 ? "\(n) file\(n == 1 ? "" : "s") queued"
                     : st > 0 ? "\(st) file\(st == 1 ? "" : "s") — settings changed"
                     : r > 0 ? "\(r) failed — press Retry" : "nothing queued")
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(n == 0 && (r > 0 || st > 0) ? AMBER : DIM)
            }
            Spacer()
            Button("Clear") { conv.clear() }.disabled(conv.isRunning || conv.jobs.isEmpty)
            Button("Remove Done") { conv.removeCompleted() }
                .disabled(!conv.jobs.contains { $0.state == .done })
            if conv.isRunning {
                Button("Cancel") { conv.cancel() }.tint(RED)
            } else {
                Button(conv.pendingCount == 0 && conv.staleCount == 0
                       && conv.retryableCount > 0 ? "Retry" : "Convert") { conv.start() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!conv.canStart)
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(PANEL)
    }

    private func pick() {
        let p = NSOpenPanel()
        p.allowsMultipleSelection = true
        p.canChooseDirectories = false
        if let t = UTType(filenameExtension: "ser") { p.allowedContentTypes = [t] }
        p.title = "Select SER files"
        if p.runModal() == .OK { conv.add(p.urls) }
    }

    private func load(_ providers: [NSItemProvider]) {
        for pr in providers {
            _ = pr.loadObject(ofClass: URL.self) { url, _ in
                guard let url, url.pathExtension.lowercased() == "ser" else { return }
                Task { @MainActor in conv.add([url]) }
            }
        }
    }
}

struct JobRow: View {
    @ObservedObject var job: Job
    @EnvironmentObject var conv: Converter

    /// What the output will actually be, given the chosen speed.
    private var rateNote: String? {
        guard let i = job.info, conv.speed != .realtime,
              job.state == .queued || job.state == .running else { return nil }
        let out = max(1.0, i.effectiveFPS / conv.speed.divisor)
        let dur = Double(i.frameCount) / out
        return String(format: "→ %.2f fps · %.0fs playback", out, dur)
    }

    private var tint: Color {
        switch job.state {
        case .done: return GREEN
        case .failed: return RED
        case .cancelled: return DIM
        case .running: return CYAN
        default: return DIM
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 8) {
                Text(job.name).font(.system(.body, design: .monospaced)).foregroundStyle(.white)
                if let w = job.info?.timingWarning {
                    Text("⚠ \(w)").font(.caption2).foregroundStyle(AMBER)
                        .help("Capture timing was irregular; a constant-rate movie smooths this away.")
                }
                Spacer()
                if job.state == .done, let out = job.outURL {
                    Button("Reveal") { NSWorkspace.shared.activateFileViewerSelecting([out]) }
                        .buttonStyle(.link).font(.caption)
                }
                if job.state == .failed || job.state == .cancelled, job.info != nil {
                    Button("Retry") { conv.retry(job) }
                        .buttonStyle(.link).font(.caption).disabled(conv.isRunning)
                }
                Text(conv.isStale(job) ? "RECONVERT"
                     : String(describing: job.state).uppercased())
                    .font(.system(size: 10, weight: .bold, design: .monospaced))
                    .foregroundStyle(conv.isStale(job) ? AMBER : tint)
            }
            if job.state == .running || job.state == .done {
                ProgressView(value: job.state == .done ? 1 : job.fraction)
                    .tint(tint).frame(height: 3)
            }
            HStack(spacing: 8) {
                Text(job.statusLine).font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(job.state == .failed ? RED : DIM)
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                if let r = rateNote {
                    Text(r).font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(AMBER)
                }
            }
        }
        .padding(10)
        .background(PANEL)
        .overlay(Rectangle().stroke(LINE, lineWidth: 1))
    }
}
