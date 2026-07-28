// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 MaxBotev
// Shown in place of the app when a required tool is missing.

import SwiftUI
import AppKit

struct SetupView: View {
    @EnvironmentObject var tools: Toolchain

    var body: some View {
        VStack(spacing: 16) {
            Spacer()
            Text("⚙︎").font(.system(size: 40)).foregroundStyle(AMBER)
            Text("One thing missing")
                .font(.system(size: 17, weight: .bold, design: .monospaced))
                .foregroundStyle(.white)
            Text(tools.advice)
                .font(.system(size: 12, design: .monospaced)).foregroundStyle(DIM)
                .multilineTextAlignment(.center)

            if tools.brew != nil {
                VStack(spacing: 10) {
                    Button(tools.installing ? "Installing…" : "Install ffmpeg")
                        { tools.installFFmpeg() }
                        .keyboardShortcut(.defaultAction)
                        .disabled(tools.installing)
                    Text("runs  brew install ffmpeg  — a few minutes the first time")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(DIM.opacity(0.8))
                }
            } else {
                VStack(spacing: 10) {
                    Text("Install Homebrew first, then reopen this app.")
                        .font(.system(size: 12, design: .monospaced)).foregroundStyle(DIM)
                    Button("Open brew.sh") {
                        NSWorkspace.shared.open(URL(string: "https://brew.sh")!)
                    }
                    Text("Homebrew changes system directories and asks for your\n"
                         + "password, so this app won't install it for you.")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(DIM.opacity(0.8))
                        .multilineTextAlignment(.center)
                }
            }

            if let e = tools.lastError {
                Text(e).font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(RED).multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if !tools.log.isEmpty {
                ScrollView {
                    Text(tools.log)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(DIM)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                        .padding(8)
                }
                .frame(maxWidth: 620, maxHeight: 220)
                .background(PANEL)
                .overlay(Rectangle().stroke(LINE))
            }

            Button("Check again") { tools.refresh() }
                .buttonStyle(.link).font(.caption).disabled(tools.installing)
            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(24)
        .background(BG)
    }
}
