# Native voice (Telegram → MAX) — reverse-engineering research

**Branch:** `experiment/native-voice-upload`
**Goal:** send a Telegram voice into MAX as a *native* voice message
(`{_type:"AUDIO", audioId, token, duration}`), not a `.ogg` file.
**Status: BLOCKED** (feasible in principle; blocked on the audio upload-URL opcode).

## What works today (on `main`)
A Telegram voice is uploaded to MAX as a `.ogg` **file** (opcode 87) and plays in
MAX, shown as a file attachment, not a voice bubble. The MAX **web** API has no
audio upload at all (see memory `max2tg-bridge` v21/v22), which is why this needed
the **mobile** app's protocol.

## Confirmed from the decompiled Android app (`ru.oneme.app` v26.20.2)
MAX Android is built on the `ru.ok.tamtam` (TamTam / OK.ru) codebase. The mobile
app **does** record + upload voice, so a native path exists. Pipeline:

1. Record via `AudioRecord` / `MediaCodec` (WebRTC capture).
2. `va6.c()` maps the attach type → `UploadType` enum (`xxh`); **AUDIO = `xxh.f`**
   (`D:\maxapk\src\sources\defpackage\va6.java`). It enqueues an Android
   **WorkManager** job `UploadFileAttachWorker` with `uploadType = AUDIO`.
3. `UploadFileAttachWorker` (`ru/ok/tamtam/upload/workers/UploadFileAttachWorker.java`)
   requests an **audio-tagged upload URL** (a WS opcode — UNKNOWN), uploads, and
   sends `{_type:"AUDIO", audioId, token, duration}` (send is opcode 64).
4. The actual byte transfer uses the OK.ru **OneVideo** SDK
   (`one.me.sdk.transfer`, class `fyb` = `OneVideoUploadOperation`, supports
   `UploadType.VIDEO / VIDEO_MESSAGE / AUDIO`). The transfer mechanism itself is
   a plain HTTP upload (see "probe" below — a simple POST is accepted).

## Disproven shortcut
Reusing the simple **video upload slot (opcode 82)** + sending an `AUDIO` attach
does NOT work. Probe result (separate connection): the upload is **accepted** and
returns an id+token for mp4 / m4a / ogg input, but sending
`{_type:"AUDIO", audioId:<that id>, token}` returns **`attachment.not.ready`**
forever — the file is type-tagged as *video* at upload time, so the audio
pipeline never marks it ready. Conclusion: the upload byte-transfer is fine; the
file must be registered as **AUDIO at upload-URL request time**.

## The blocker
The **audio upload-URL opcode** (and whether its request needs a media-type
field) is the one missing piece. It lives inside `UploadFileAttachWorker`'s
methods `l` / `m` / `D` / `w` / `x` / `y` / `z`, which are Kotlin **coroutine
state machines that jadx could not decompile** ("Method dump skipped / not
decompiled"). Command/opcode names are obfuscated (R8), so grep over the
decompiled tree doesn't surface it.

## How to continue (escalation options, in rough order of effort)
1. **Live network capture** (most reliable): run the real MAX app on a device/
   emulator with an mitm proxy (cert-pinning bypass via Frida/objection), record a
   voice, and read the upload-URL WS frame (opcode + payload) + the upload HTTP
   request directly. Definitive; needs a device/emulator.
2. **smali analysis**: `baksmali` the worker + the upload-URL request class and
   trace the `const`/opcode int passed to the WS framer through the coroutine
   state machine. Laborious but offline.
3. **Frida hook** on the running app: hook the WS-send to log opcode+payload when
   sending a voice. Fast if a device is available.

## Remaining unknowns even after the opcode is found
- Whether the upload-URL request is gated to mobile `deviceType` (a `WEB`-logged-in
  session like the bridge might be rejected).
- The exact audio container/codec the audio pipeline expects on upload.
- Whether a separate "commit/finalize" step is required after the byte upload.

## Artifacts (on disk, NOT committed — outside the repo)
- APK: `D:\maxapk\base.apk` (ru.oneme.app v26.20.2, from RuStore).
- Decompiled: `D:\maxapk\src` (jadx 1.4.7, 22,925 files).
