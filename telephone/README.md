# Telephone

One-way live audio between two machines on the **same network**, with **zero
config**. The sender finds the receiver by LAN broadcast; there's no IP to type.

- `receiver.py` runs on the machine with the **speakers**.
- `sender.py` runs on the machine with the **microphone**.
- Start them in either order. The sender keeps searching until the receiver
  appears.

## How it works

```
sender:   broadcasts "who is the telephone receiver?"
receiver: replies with its IP + audio port
sender:   mic -> 512-sample int16 chunks -> UDP packets (magic + sequence #)
receiver: packets -> small jitter buffer -> speakers
```

Audio is raw 44.1 kHz mono 16-bit PCM, about 700 kbit/s. Each packet is one
Ethernet frame (1024 bytes of audio + 8-byte header), so nothing fragments.
The sequence number lets the receiver count dropped packets. The jitter buffer
holds ~46 ms before playing so Wi-Fi bursts don't cause crackle, and caps at
~230 ms so lag can never creep upward.

## Run the receiver (speakers)

```bash
pip install sounddevice numpy
python receiver.py
```

Every 5 seconds it prints packets received, drops, underruns (speaker starved),
overflows (too far behind, old audio discarded) and current buffer depth.

## Run the sender (microphone)

```bash
pip install sounddevice numpy
python sender.py
```

Ctrl+C on either side to hang up.

## Options

| Flag | Script | What it does |
|------|--------|--------------|
| `--test-tone` | sender | send a 440 Hz beep instead of the mic (test the link with no mic) |
| `--ip A.B.C.D` | sender | skip discovery, send straight to that IP |
| `--device N` | both | pick a specific mic / speaker (`python -m sounddevice` lists them) |
| `--record out.wav` | receiver | also save everything received to a WAV file |
| `--no-play` | receiver | don't open the speakers (use with `--record`) |
| `--seconds N` | both | stop automatically after N seconds |
| `--port N` | both | audio UDP port (default 5005) |

## Self-test on one machine

```bash
python receiver.py --no-play --record test.wav --seconds 8
```
then in a second terminal:
```bash
python sender.py --test-tone --seconds 4
```
`test.wav` should be 4 seconds of a clean 440 Hz tone and the receiver should
report `dropped=0`.

## Security

- LAN-only. Nothing is exposed to the internet.
- There is no encryption or password: anyone on the same network can send
  audio to the receiver or capture the stream. Fine for a home network, not
  for anything sensitive.

## Files

| File | Role |
|------|------|
| `receiver.py` | Speakers side: answers discovery, buffers and plays audio. |
| `sender.py` | Mic side: finds the receiver, streams the microphone. |
