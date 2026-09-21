# RoomCam: camera, desktop, microphone, and speaker audio

A Windows x64 proof of concept combining Claude's camera/microphone viewer and recording support with the desktop-sharing features from `cmg1249-gif/chat-gpt-scripts` v0.2.0. Use it on your own equipment or with permission and the knowledge of anyone being captured.

## Run the matching v2.0.0 pair

1. Run **webcam_server.exe** on the sharing computer. There is **no server password prompt**. A tray icon provides status and **Stop sharing**.
2. Run **viewer.exe** on the viewing computer. Choose an **8–128 character password in the listener**. It discovers and pairs with the waiting server.
3. Choose camera or desktop video, then enable microphone and/or desktop audio as needed. Both audio sources start off.
4. Close the viewer with **Q**, **Escape**, or its window close button to stop capture. Stop the server itself with its tray menu.

Use the same EXEs on a physical PC or Windows VM. Python, Cloudflared, trusted CA certificates, and the viewer's recording encoder are bundled. Both computers need outbound internet access. NAT needs no inbound port forwarding. A VM still needs working display/audio drivers; Windows 11 VirtualBox guests should expose a supported speaker device, such as Intel HD Audio, for desktop audio.

Replace **both** old EXEs. The new pairing and mixed-audio protocol require a matching v2.0.0 server and viewer. Existing optional device/network settings in `roomcam_config.ini` remain readable; the server ignores its old password setting. The selected session password is kept in memory and is not written by the server. Restarting the server creates a new pairing session.

## Windows viewer controls

| Control | Action |
|---|---|
| **V** | Switch camera / desktop |
| **B** | Switch to the next monitor |
| **C** | Switch to the next camera |
| **N** | Switch to the next microphone |
| **F** | Refresh devices; camera discovery briefly probes the cameras |
| **M** | Turn the host microphone on / off |
| **O** | Turn host desktop-speaker audio on / off |
| **Space** | Mute / unmute local playback |
| **R** or **REC** | Start / stop an audio+video recording |
| **Q**, **Escape**, window close | Close viewer and stop capture |
| **L** | Close viewer and leave host capture running |

`viewer.exe --source desktop --monitor 2` starts on a particular monitor. Use `--mute` when running the viewer on the same PC as the host to prevent speaker feedback. With no camera, video falls back to the desktop.

## Browser viewer

Run `viewer.exe --browser`. Pairing and password selection still happen in the listener first. The browser then uses Basic authentication: username **admin** by default, and the password you selected in the listener.

The page provides camera/desktop selection, monitor and device dropdowns, microphone and desktop-audio toggles, **Listen / Mute playback**, and **Stop all capture**. Listen controls playback; microphone and desktop capture are separate controls. Recording is available in the Windows viewer, as in the original app. Browser video and audio use independent playback; use the Windows viewer for the timestamp-based A/V synchronization.

## Audio and recording

Microphone audio and the default Windows speaker output are converted to **48 kHz, 16-bit stereo** and sent on the same timestamped stream. With both enabled, each is mixed at half gain to reduce clipping. Changing microphone rates does not change the wire or recording format. Speaker capture is shared across reconnecting listeners to avoid repeatedly opening the audio device.

The viewer retains Claude's jitter buffer, host-clock video selection, REC button, and ffmpeg recording support. The selected video source and the combined audio are recorded together. A recording keeps the dimensions of its first frame; later source changes are resized to that canvas. Local playback mute does not mute the recording. Recording names do not overwrite earlier files when started within the same second.

Recordings are saved beside the viewer, so extract it into a writable folder. `--format mp4|mkv|avi` selects the container. `--record FILE.wav` also saves the received audio. Network delay and jitter still affect playback; this remains a PoC.

## Diagnostics and source build

Extract all files into a writable folder.

- `CheckConnection.exe`: real desktop capture on connected monitors, known speaker-tone detection, repeated audio reconnects, and packaged viewer decoding. It uses localhost only and writes `local-results.txt`.
- `CheckConnection.exe --internet`: generated camera/desktop images and tones over real Cloudflare/ntfy, listener-side pairing, and tunnel replacement. It does not transmit your desktop, webcam, or microphone. It writes `internet-results.txt`.
- `CheckConnection.exe --self-test`: protocol, audio mixing, pairing, and recording regression tests. It writes `unit-results.txt`.

See `VALIDATION.md` for the exact tested environments. A locked/secure desktop, absent devices, blocked outbound services, or service quotas may prevent operation.

To build, use **Python 3.12 on Windows x64**, install `requirements.txt`, put the official `cloudflared.exe` beside the source, then run `build.ps1 -Python <python.exe>`. The audio resampler uses Python 3.12's `audioop`; Python 3.13+ is not the supported source-build runtime. Run `python -m unittest -v test_combined` for regression checks.

## Optional settings and discovery

No configuration editing is required for the default demo. Advanced users can set matching `ROOMCAM_TOPIC` values on host/listener to separate demos, and `ROOMCAM_USERNAME` if changing the default username. The listener can read `ROOMCAM_PASSWORD` or its existing config password; the host ignores password settings and waits for pairing. Other camera, microphone, quality, and network settings from the previous version remain optional.

Initial discovery is a shared public ntfy topic with a short-lived pairing token, so use one demo host at a time and share the application only with trusted participants. Someone who can access discovery can attempt to claim an unpaired server. After pairing, advertisements are signed with a password-derived key, and reconnect discovery checks the server session before using a new address. Certificate and hostname checks remain enabled, including on fresh Windows installations with sparse root stores. Service availability and quotas still apply.

Camera/microphone features and the recorder originate in this repository's Claude version; desktop capture, shared WASAPI capture, TLS trust, and reconnect behavior draw on the user's `chat-gpt-scripts` project. Neither capture nor recording installs a startup task.
