// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 MaxBotev
// Transit tab: find the crossing, judge the rotation, export the stills.

import SwiftUI
import AppKit
import UniformTypeIdentifiers

struct TransitTab: View {
    @EnvironmentObject var t: TransitScanner
    @State private var dragOver = false

    var body: some View {
        HSplitView {
            leftPane.frame(minWidth: 300, idealWidth: 340)
            previewPane.frame(minWidth: 380)
        }
        .background(BG)
        .onDrop(of: [.fileURL], isTargeted: $dragOver) { providers in
            for pr in providers {
                _ = pr.loadObject(ofClass: URL.self) { url, _ in
                    guard let url, url.pathExtension.lowercased() == "ser" else { return }
                    Task { @MainActor in t.load(url) }
                }
            }
            return true
        }
        .overlay(dragOver ? CYAN.opacity(0.07) : .clear)
    }

    // ---- left: file, scan, results --------------------------------------
    private var leftPane: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Button("Open SER…") { pick() }.disabled(t.scanning || t.exporting)
                Spacer()
                Button(t.scanning ? "Scanning…" : "Find Transit") { t.scan() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(t.url == nil || t.info == nil || t.scanning || t.exporting)
            }
            if let u = t.url {
                Text(u.lastPathComponent).font(.system(.body, design: .monospaced))
                    .foregroundStyle(.white).lineLimit(1).truncationMode(.middle)
            } else {
                Text("Drop a .ser file here").font(MONO).foregroundStyle(DIM)
            }
            if let i = t.info {
                Text(i.summary).font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(DIM).fixedSize(horizontal: false, vertical: true)
            }
            if t.scanning {
                ProgressView(value: t.progress).tint(CYAN)
                Text("\(Int(t.progress * 100))% — reading every 4th pixel")
                    .font(.system(size: 11, design: .monospaced)).foregroundStyle(CYAN)
            }
            Text(t.status).font(.system(size: 11, design: .monospaced))
                .foregroundStyle(t.status.contains("fail") || t.status.contains("no transit")
                                 ? AMBER : DIM)
                .fixedSize(horizontal: false, vertical: true)

            if !t.runs.isEmpty {
                Divider().overlay(LINE)
                Text("CROSSINGS").font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(DIM).tracking(2)
                ScrollView {
                    VStack(spacing: 4) {
                        ForEach(t.runs) { r in runRow(r) }
                    }
                }.frame(maxHeight: 160)
            }
            Spacer()
            if t.selected != nil { exportControls }
        }
        .padding(12)
    }

    private func runRow(_ r: TransitRun) -> some View {
        let on = t.selected == r
        return Button { t.select(r) } label: {
            HStack(spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("frames \(r.start)–\(r.end)")
                        .font(.system(size: 12, design: .monospaced))
                        .foregroundStyle(on ? CYAN : .white)
                    Text(String(format: "%d frames · %.3f s · peak %d",
                                r.count, r.seconds(t.info?.effectiveFPS ?? 25), r.peakScore))
                        .font(.system(size: 10, design: .monospaced)).foregroundStyle(DIM)
                }
                Spacer()
            }
            .padding(8)
            .background(on ? CYAN.opacity(0.12) : PANEL)
            .overlay(Rectangle().stroke(on ? CYAN.opacity(0.6) : LINE))
        }
        .buttonStyle(.plain)
    }

    private var exportControls: some View {
        VStack(alignment: .leading, spacing: 8) {
            Divider().overlay(LINE)
            HStack {
                Text("ROTATION").font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(DIM).tracking(2)
                Spacer()
                Text(String(format: "%.1f°", t.rotation))
                    .font(.system(size: 12, design: .monospaced)).foregroundStyle(CYAN)
            }
            Slider(value: $t.rotation, in: -180...180, step: 0.5).tint(CYAN)
            HStack(spacing: 6) {
                ForEach([-90.0, 0.0, 90.0, 180.0], id: \.self) { d in
                    Button(d == 0 ? "0°" : String(format: "%.0f°", d)) { t.rotation = d }
                        .font(.system(size: 11, design: .monospaced))
                }
                Spacer()
            }
            if let f = TransitScanner.rotateFilter(t.rotation), f.hasPrefix("rotate") {
                Text("⚠ not a quarter turn — pixels get resampled once, from the SER")
                    .font(.system(size: 10)).foregroundStyle(AMBER)
                    .fixedSize(horizontal: false, vertical: true)
            } else if t.rotation != 0 {
                Text("quarter turn — pixels only re-ordered, nothing resampled")
                    .font(.system(size: 10)).foregroundStyle(GREEN)
            }
            Stepper("context frames: \(t.padFrames)", value: $t.padFrames, in: 0...20)
                .font(.system(size: 11, design: .monospaced)).foregroundStyle(DIM)
            HStack {
                Button(t.exporting ? "Exporting…" : "Export Stills") { t.export() }
                    .disabled(t.exporting || t.scanning)
                if let d = t.lastExport {
                    Button("Reveal") { NSWorkspace.shared.activateFileViewerSelecting([d]) }
                        .buttonStyle(.link).font(.caption)
                }
            }
        }
    }

    // ---- right: preview --------------------------------------------------
    private var previewPane: some View {
        VStack(spacing: 8) {
            if let img = t.preview {
                GeometryReader { geo in
                    Image(nsImage: img)
                        .resizable().scaledToFit()
                        .rotationEffect(.degrees(t.rotation))
                        .frame(width: geo.size.width, height: geo.size.height)
                }
                if let r = t.selected, let f = t.previewFrame {
                    VStack(spacing: 6) {
                        Text("frame \(f)  (peak of \(r.start)–\(r.end))")
                            .font(.system(size: 11, design: .monospaced)).foregroundStyle(DIM)
                        // scrub within the crossing to pick the best silhouette
                        Slider(
                            value: Binding(
                                get: { Double(f) },
                                set: { t.loadPreview(frame: Int($0.rounded())) }),
                            in: Double(r.start)...Double(max(r.start, r.end)),
                            step: 1
                        ).tint(CYAN).frame(maxWidth: 320)
                    }
                }
            } else {
                VStack(spacing: 8) {
                    Spacer()
                    Text(t.url == nil ? "no file loaded"
                         : t.runs.isEmpty ? "run Find Transit to locate the crossing"
                         : "select a crossing")
                        .font(MONO).foregroundStyle(DIM)
                    Spacer()
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func pick() {
        let p = NSOpenPanel()
        p.allowsMultipleSelection = false
        p.canChooseDirectories = false
        if let ty = UTType(filenameExtension: "ser") { p.allowedContentTypes = [ty] }
        if p.runModal() == .OK, let u = p.urls.first { t.load(u) }
    }
}
