# MIRU PC Frame Broker v0.1.0

A bounded Windows feasibility prototype for testing this loop:

`PC screen -> RAM ring buffer -> temporary HTTPS URL -> MIRU observes -> allowlisted key input -> MIRU re-observes`

## Current status

- Source and Windows package CI: pending until the pull request workflow passes.
- Real PC frame observation by ChatGPT: UNVERIFIED.
- ChatGPT-issued key input reflected on the PC: UNVERIFIED.
- Fighting-game latency: UNVERIFIED.

## Safety and scope

- Screen frames are retained only in process RAM by the broker.
- The HTTPS address is a temporary Cloudflare quick tunnel and dies when the process stops.
- A fresh 256-bit session token is embedded in the unguessable path.
- Request paths are not logged by the local broker.
- Input is locked to the exact foreground window handle selected at startup.
- The broker supports only a small key allowlist and a fixed `MIRUTEST1` typing canary.
- It does not accept arbitrary shell commands, mouse coordinates, file access, clipboard access, arbitrary text, downloads, uploads, or persistence.
- Pressing Ctrl+C stops capture, closes the tunnel, and invalidates the session.

The temporary tunnel provider can technically observe connection metadata and the token-bearing URL path. Use this prototype only for a short test, stop it immediately afterward, and do not reuse the URL.

## Windows test procedure

1. Download and extract the `miru-frame-broker-windows` workflow artifact.
2. Open a harmless target such as Notepad.
3. In PowerShell, run:

   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   .\Start-MiruFrameBroker.ps1
   ```

4. Press Enter when prompted, then switch to the exact target window during the five-second countdown.
5. Wait for a line shaped like:

   ```text
   MIRU_BROKER_URL=https://example.trycloudflare.com/s/<random-token>
   ```

6. Paste that full line into the 감독님 project chat.
7. MIRU will read `status.json`, inspect `latest.jpg` and `burst.jpg`, issue one bounded command with a unique nonce, and inspect the next frames.
8. Press Ctrl+C when the test is finished.

Optional parameters:

```powershell
.\Start-MiruFrameBroker.ps1 -Fps 10 -Monitor 1
```

For a known foreground title substring:

```powershell
.\Start-MiruFrameBroker.ps1 -TargetTitle "Notepad"
```

## Session endpoints

Given `MIRU_BROKER_URL=<base>`:

- `<base>/status.json`
- `<base>/latest.jpg`
- `<base>/burst.jpg?n=6`
- `<base>/cmd/<unique-nonce>/press/LEFT?ms=70`
- `<base>/cmd/<unique-nonce>/type-test`

Every command nonce is idempotent. Reusing a nonce returns the saved result without pressing the key again.
