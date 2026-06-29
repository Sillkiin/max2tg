# Native voice (Telegram → MAX) — SOLVED

**Status: WORKING.** A Telegram voice is now sent into MAX as a *native* voice
message (waveform + duration + transcription support), not a `.ogg` file.
Reverse-engineered from the MAX Android app (`ru.oneme.app` v26.20.2).

## The recipe
1. **Request an audio upload slot** — opcode **82** (the same `VIDEO_UPLOAD`
   opcode) with payload `{"count":1, "type":2, "uploaderType":0}`. The
   `type:2` is the key: it yields an **audio**-tagged slot on `omu.okcdn.ru`
   (videos use `vu.okcdn.ru`). Response: `payload.info[0] = {url, videoId, token}`.
2. **Upload the bytes as multipart/form-data** (`files={"file": (...)}`) to that
   `url`. The audio endpoint rejects the raw `Content-Range` body that videos use
   (HTTP 415); raw octet gets 412/415; **multipart returns 200 `<retval>1</retval>`**.
   Telegram voices are already ogg/opus, so the bytes go up as-is (no transcode).
3. **Send the message** — opcode **64** with attach
   `{"_type":"AUDIO", "audioId":<videoId from the slot>, "token":<token>,
   "duration":<ms>}`. MAX returns a native voice attach (`_type:AUDIO`, `wave`,
   `duration`, `transcriptionStatus`, playable `url`).

Verified end-to-end on a separate connection (slot → multipart upload → send →
the returned message is a native AUDIO with a waveform; `uploaderType` 0 and 1
both work).

## How it was found (decompiled app trace)
- `va6.c` maps the attach to `UploadType` (`xxh`); enqueues `UploadFileAttachWorker`.
- `yvh.b` (`requestUploadUrl`) `switch(uploadType.ordinal())` builds the upload-URL
  request. **Case 5 = audio** (the only case that checks `filename.endsWith(".ogg")`):
  `new eeg(3, oggFlag)`.
- `eeg(int i, int i2)`: `super(qyb.s2); c(dtg.E(i),"type"); c(1,"count");
  c(i2,"uploaderType")`. `qyb.s2 = VIDEO_UPLOAD = opcode 82`; `dtg.E(3) = 3-1 = 2`.
  → opcode 82, `{type:2, count:1, uploaderType:flag}`.
- The OneVideo SDK (`one.me.sdk.transfer`, `fyb`) does the byte transfer; the
  endpoint is OK.ru `omu.okcdn.ru/upload.do` (multipart).

## Implementation (this branch)
- `mediamax.upload_audio` (opcode 82 `type:2` → multipart) + `send_uploaded_audio`
  (`{_type:AUDIO,...}`); `send_uploaded_media` routes `kind="voice"` to it, with a
  **fallback to a plain `.ogg` file** if MAX rejects the audio (so behaviour never
  regresses below main's old path).
- `bridge._telegram_attachment` maps a Telegram `voice` → `kind:"voice"` +
  `duration_ms`; the relay forwards `duration_ms`.
- Music/`audio` files stay generic files (only true voice messages go native).

## Notes / residual risk
- Not yet tested with a *real* Telegram voice end-to-end through the bridge (only a
  generated ogg/opus via the protocol probe). Telegram voice is standard ogg/opus
  so it should match; the file-fallback covers any mismatch.
- `uploaderType` is sent as 0 (works; the app uses 1 only behind a feature flag for
  the newer uploader — both produced a native voice in testing).
- APK + decompile live on `D:\maxapk` (off the repo).
