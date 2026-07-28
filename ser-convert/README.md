# SER Convert

A small native macOS app that batch-converts astronomy **SER** captures to
ProRes or FFV1, using ffmpeg under the hood.

![The Transit tab: detected crossings listed with frame ranges and scores,
and the aircraft silhouette rotated so it flies horizontally.](../docs/img/serconvert-transit-tab.png)

## Why not just run ffmpeg

You can — `ffmpeg -i in.ser -c:v prores_ks -profile:v 3 out.mov` works, because
modern ffmpeg has a SER demuxer. But SER stores **no frame-rate field**: timing
lives in an optional trailer of per-frame timestamps. ffmpeg ignores it and
falls back to 25 fps, so every output plays at the wrong speed.

This app reads the trailer, computes the true rate, and passes it to ffmpeg with
`-r` before `-i`. It also flags captures whose timestamps are irregular
(out-of-order frames, stalls), which a constant-rate movie silently smooths away.

## Use

1. Drop `.ser` files on the window, or **Add Files…**
2. Pick a codec preset and a playback speed
3. **Convert**

A finished file is re-convertible the moment you change the codec or speed —
the row switches to **RECONVERT** and the button re-enables, because those
settings produce a genuinely different output. Change a setting back and it
correctly goes quiet again. Failed jobs get a **Retry** link, and the main
button relabels to **Retry** when that is all that is left.

Output is written beside each source file. Conversions run one at a time —
ffmpeg already saturates all cores on a 8 MP frame, so running several in
parallel finishes no sooner.

| Preset | Codec | Use for |
|---|---|---|
| ProRes 422 HQ | `prores_ks -profile:v 3` | editing, sharing (~5× smaller, lossy) |
| ProRes 4444 | `prores_ks -profile:v 4` | higher fidelity, larger (lossy) |
| FFV1 | `ffv1 -level 3` | archival / stacking — mathematically lossless |

### Slow motion

A transit is over in a fraction of a second, so real-time playback barely shows
it. The speed picker offers **1/2, 1/4 and 1/8**, implemented by declaring a
lower input rate to ffmpeg — **every frame is written exactly once**, nothing is
duplicated, interpolated or dropped. Only the file's frame rate changes.

A 45.669 fps capture at 1/4 speed becomes an 11.42 fps file: same frames, four
times the running time. Slow-motion output is named `<capture>_4x-slow.mov` so
it can never overwrite the real-time version.

Bayer captures are demosaiced automatically: ffmpeg reads `ColorID` from the SER
header and picks the right pattern, so an SV705C's `BAYER_GRBG` comes out with
correct colour without any manual `-pix_fmt`.

## Transit tab

Finds the frames where an aircraft crosses the lunar disc, and exports them as
full-resolution stills.

Drop a `.ser` in and press **Find Transit**. A silhouetted aircraft is far
darker than even the darkest maria, so counting near-black pixels *inside* the
disc separates a crossing from everything else by orders of magnitude — on a
real capture the count went from a baseline of ~40 to **28155** at mid-transit.
Scanning a 15 GB / 1905-frame file takes about 8 seconds.

Crossings are listed strongest first. Selecting one previews its peak frame,
and a slider scrubs through the crossing so you can pick the best silhouette.

### Rotation

A transit often runs vertically down the frame, which looks unnatural. The
rotation slider previews any angle live, and the export applies it **once,
straight from the SER** — not to an already-exported still.

Quarter turns use `transpose`, which only re-orders pixels: nothing is
resampled. Any other angle must resample, and the UI says so, so the choice is
deliberate.

Stills are written to `<capture>_transit/f_0942.png` beside the source, with the
original frame numbers preserved. They are lossless PNG straight from the sensor
data — never a re-encode of the ProRes.

## Requirements

- macOS 13+
- ffmpeg — **the app installs it for you** if it's missing

On launch it checks for ffmpeg in `/opt/homebrew/bin`, `/usr/local/bin` and
`/usr/bin` (by absolute path: a GUI app inherits no useful `PATH`, so a `which`
lookup would fail even when ffmpeg is installed). If it's absent you get a setup
screen with an **Install ffmpeg** button that runs `brew install ffmpeg` and
streams the output, then re-checks and continues.

If Homebrew itself is missing the app says so and links to brew.sh, but **will
not install it** — that writes to system directories and wants an admin
password, so it stays a decision you make knowingly. For the same reason the app
never downloads a prebuilt binary from a download site; it only asks a package
manager you already trust for a well-known formula.

## Build

```bash
./build.sh
```

Needs only the Xcode Command Line Tools — no Xcode project. Produces
`SER Convert.app`, ad-hoc signed so it launches without a developer certificate.

## Layout

```
Sources/SER.swift        SER header + timestamp-trailer parsing, true fps
Sources/Converter.swift  job queue, ffmpeg process, -progress parsing
Sources/App.swift        SwiftUI interface
build.sh                 compile + assemble the .app bundle
```
