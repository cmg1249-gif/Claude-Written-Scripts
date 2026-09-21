# v2.0.0 validation — 2026-09-20

## Completed source checks

- 23 regression tests: authentication, listener-owned password pairing, idempotent pairing retries, expired-token renewal, no server password prompt/config write, signed advertisements, bundled trust without OS roots, monitor metadata/selection, missing-camera fallback, audio-device failures, stereo resampling/mixing, bounded packet streams, shared capture reconnects, fixed-rate recording, and collision-safe recording names.
- Real local hardware: 12 frames from each physical monitor, 12 EMEET SmartCam S600 webcam frames at 640×480, default microphone capture/transport, and 997 Hz default-speaker loopback detection. Audio transported as 48000 Hz stereo. No captured media was saved or sent publicly.
- Browser UI checked using generated media on localhost: camera/desktop selection, monitor 2 selection, video image decoded at 640×360, desktop-audio toggle, Listen/Mute, Stop all capture. No browser console errors were observed. Tests used an isolated loopback-only fixture; production endpoint authentication is covered separately.
- Recording test produced a playable MP4 across a source-size change, retaining the recording's initial canvas dimensions.

## Final package and VM checks

- Final packaged local check passed listener-side pairing, capture from both physical monitors (12 frames each, scaled to 1600×900), 997 Hz speaker-tone detection (RMS 715.9), three audio stream reopens, and the packaged viewer's headless desktop decoding with muted playback.
- Packaged regression/recording runner passed all 23 tests, including creating and decoding an MP4 with bundled ffmpeg.
- Final packaged internet diagnostic passed generated camera and desktop frames, 997 Hz stereo tone delivery, listener-side pairing, and signed discovery before and after tunnel replacement. The diagnostic waits for the replacement address to appear in the service cache.
- Final packaged server on Windows 11 in VirtualBox NAT (10.0.2.0/24): listener-owned pairing and 12 desktop frames at 1021×748 passed over the public tunnel. After confirming playback in the guest, the 997 Hz speaker tone arrived as 48000 Hz stereo (RMS 648.5); three audio reconnects passed with nonzero audio. The initial audio attempt was silent before playback was confirmed.
- The VM has one monitor and Intel HD Audio. Multiple monitors, webcam, microphone, and packaged viewer decoding were checked on the physical PC; the VM transport check used the diagnostic reader. No captured VM media was saved.
- After testing, the user stopped playback and sharing. The original VM DVD was restored; the working Intel HD Audio setting was retained.

## Limits

Tests cannot establish operation on every Windows build, camera/driver, locked or secure desktop, disconnected remote session, or during service outages. Browser A/V playback is independent; Windows viewer uses host timestamps and buffering. Initial public discovery can be claimed by another participant who has access to the pairing token. Use one demo server at a time, keep its tray stop control accessible, and use only authorized devices.
