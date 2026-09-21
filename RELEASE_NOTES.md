# v2.0.0 — Camera and desktop sharing in one app

This update builds on Claude's RoomCam v1.1.1. The viewer can switch between webcam video and any desktop monitor, listen to the microphone, desktop speakers, or both, and record the selected view with the combined audio. Browser controls provide the same source and audio selections; recording remains in the Windows viewer.

**Password setup is in the listener only.** The server starts without a password popup, waits for pairing, and provides a tray status/Stop sharing control. Old server-side password settings are ignored; the new session password is not saved by the server. Download both matching `webcam_server.exe` and `viewer.exe`.

- Independent microphone and desktop-audio controls; local playback mute.
- Fixed 48 kHz stereo mixing preserves the recording format across microphone sample-rate changes.
- Shared speaker capture handles audio reconnects without reopening the device for each viewer connection.
- Bundled CA trust, discovery retries, tunnel restart handling, and signed session advertisements after pairing.
- The existing REC button, MP4/MKV/AVI recording support, device selection, and browser view are retained.
- `CheckConnection.exe` includes local hardware, synthetic internet, and regression checks.

Validation passed: 23 packaged regression tests, two physical monitors, a physical webcam and microphone, speaker-tone detection, packaged viewer decoding, and generated-media tunnel replacement. The packaged server also passed pairing, desktop video, speaker-tone delivery, and three audio reconnects in a Windows 11 VirtualBox NAT guest. See the included validation report for scope.

The same Windows x64 build targets physical machines and VMs. Audio still requires a working playback device and camera/microphone features require available devices and Windows permissions. An unlocked desktop and outbound internet are required. This remains a PoC with public initial discovery and best-effort third-party services; see README and VALIDATION for the tested scope and limitations.
