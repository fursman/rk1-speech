# Protocols

A board running rk1-speech offers three things on the LAN, and announces the first two:

| what | default port | protocol | DNS-SD type | code |
|---|---|---|---|---|
| speech recognition | 8772/tcp | `moonshine-tcp/1`: framed TCP | `_speech-stt._tcp` | [moonshine_rknn/server.py](https://github.com/fursman/moonshine-rknn/blob/main/moonshine_rknn/server.py) |
| speech synthesis | 8771/tcp | `kokoro-http/1`: HTTP, streamed | `_speech-tts._tcp` | [kokoro_rknn/server.py](https://github.com/fursman/kokoro-rknn/blob/main/kokoro_rknn/server.py) |
| board health | 8770/tcp | HTTP, JSON | (in the TXT records: `health=8770`) | [rk1speech/health.py](../rk1speech/health.py) |

rk1-speech doesn't change the two servers' protocols; it names them, so a client can tell
from an announcement whether it can talk to a server. The descriptions below come from the
servers' code at the commits pinned in [versions.env](../versions.env).

- [Service discovery (DNS-SD over mDNS)](#service-discovery-dns-sd-over-mdns)
- [moonshine-tcp/1: speech recognition](#moonshine-tcp1-speech-recognition)
- [kokoro-http/1: speech synthesis](#kokoro-http1-speech-synthesis)
- [Board health](#board-health)

## Service discovery (DNS-SD over mDNS)

avahi-daemon on the board publishes two services from `/etc/avahi/services/rk1-speech-stt.service`
and `rk1-speech-tts.service` ([rk1speech/announce.py](../rk1speech/announce.py) writes them):

| | recognition | synthesis |
|---|---|---|
| service type | `_speech-stt._tcp` | `_speech-tts._tcp` |
| instance name | `<host name> speech recognition` | `<host name> speech synthesis` |
| SRV | port 8772 on `<host name>.local` | port 8771 on `<host name>.local` |

The type names follow RFC 6335 (15 characters at most, letters, digits and hyphens). They name
the job, not the engine, so another engine speaking the same protocol can announce the same type.

**TXT records**, in this order (RFC 6763 section 6: `key=value`, keys case-insensitive):

| key | recognition | synthesis | meaning |
|---|---|---|---|
| `txtvers` | `1` | `1` | version of this set of keys |
| `proto` | `moonshine-tcp/1` | `kokoro-http/1` | the wire protocol and its major version: **check this first** |
| `engine` | `moonshine-rknn` | `kokoro-rknn` | the engine behind it (the name its health reply gives) |
| `version` | engine commit, e.g. `d1822a562426` | engine commit, e.g. `e32630f9f33b` | what is installed |
| `model` | `moonshine-medium-streaming` | `kokoro-82m-v1.0` | the model |
| `rate` | `16000` | `24000` | sample rate: of the audio expected / of the audio sent |
| `format` | `f32le` | `f32le` | samples: 32-bit float, little-endian, mono |
| `path` | | `/tts` | the synthesis endpoint |
| `voice` | | `af_heart` | the default voice |
| `capacity` | `1` | `3` | requests at once without slowing down (below) |
| `npu_cores` | `auto` | `1,2,0` | NPU cores used: `auto`, or the lanes in order of preference |
| `board` | e.g. `rk1-1` | e.g. `rk1-1` | the board's name (`RK1S_BOARD_NAME`, default its host name) |
| `health` | `8770` | `8770` | port of the board's health endpoint, when installed |

**capacity** is what was measured, not a limit the server enforces. Synthesis: 3 sentences in
flight, one per NPU core, with first audio unchanged (p90 3.95 s against 3.56 s at 1); a 4th
queues. Recognition: 1, because only one stream at a time was measured next to synthesis; the
server accepts more connections.

**Rules for clients:**
- Use a service only if you know its `proto` name and major version. A server that changes its
  protocol incompatibly announces `/2`.
- Ignore keys you don't know; new keys may be added without a `txtvers` change.
- An announcement says a board is set up to serve, not that the server is up right now (avahi
  announces the files whether or not the service runs). Check health before relying on it: the
  `H` frame or `GET /health` below.

**Looking from a shell:** `avahi-browse -rt _speech-tts._tcp`, or with no avahi installed,
`python3 -m rk1speech.mdns` (one JSON object per service). The voice assistant this was built
for looks with one-shot mDNS queries from an ephemeral port (RFC 6762 section 5.1), which avahi
answers by unicast, so the client needs neither avahi nor port 5353.

## moonshine-tcp/1: speech recognition

Plain TCP. Every message, both ways, is a **frame**:

```
+--------+----------------------------+-----------------+
| type   | length                     | payload         |
| 1 byte | 4 bytes, unsigned, big end | length bytes    |
+--------+----------------------------+-----------------+
```

`type` is one ASCII letter. A frame longer than 64 MiB gets `X "frame too large"` and the
connection is closed.

**Client to server:**

| type | payload | what it does | answer |
|---|---|---|---|
| `H` | anything (ignored; send `{}` or nothing) | health | `O` |
| `B` | JSON `{"rate": 16000}`, or empty (16000) | begin an utterance; an unfinished one is dropped | none |
| `A` | float32 LE mono samples at the `B` rate | audio for the current utterance, any chunk size; ignored with no utterance begun | `P` frames, whenever the transcript changes |
| `E` | empty | end of utterance | `F` (empty text when no utterance was begun), or `X` |
| `T` | float32 LE mono samples **at 16 kHz** (not resampled) | transcribe a whole clip | `F` or `X` |

Other types are ignored.

**Server to client:**

| type | payload |
|---|---|
| `O` | JSON: `{"ok": true, "engine": "moonshine-rknn", "model": "medium_streaming", "encoder": "NPU", "streaming": true}`; `encoder` is `"CPU"` when the NPU is not usable |
| `P` | UTF-8 text: the transcript so far. Sent from the server's own thread at any time while audio flows, roughly 3 times a second when it changes |
| `F` | UTF-8 text: the final transcript of the utterance (or clip) |
| `X` | UTF-8 text: an error; that utterance is over |

**Behaviour:**
- One connection carries any number of utterances, one after another (`B`, `A`..., `E`, then `B`
  again). Each utterance's `P` frames come before its `F`.
- Audio at another rate than 16 kHz is resampled by linear interpolation; 16 kHz avoids that.
- Feeding never blocks the socket: audio is queued and processed on a thread of its own.
- The final transcript is computed during the speaker's pause, so with ~0.5 s of trailing silence
  `F` arrives about 0 ms after `E` (measured: median / p90 / max 0 / 0 / 0 ms).
- Closing the connection drops an unfinished utterance.

**A minimal client** (Python standard library):

```python
import json, socket, struct
HDR = struct.Struct(">cI")

def send(s, kind, payload=b""):
    s.sendall(HDR.pack(kind, len(payload)) + payload)

def recv(s):
    head = s.recv(5, socket.MSG_WAITALL)
    kind, n = HDR.unpack(head)
    return kind, s.recv(n, socket.MSG_WAITALL) if n else b""

s = socket.create_connection(("board.local", 8772))
send(s, b"B", json.dumps({"rate": 16000}).encode())
for chunk in microphone_chunks():                  # numpy float32 at 16 kHz
    send(s, b"A", chunk.astype("<f4").tobytes())   # P frames arrive meanwhile; read them on another thread
send(s, b"E")
while (msg := recv(s))[0] == b"P":
    pass
print(msg[1].decode())                            # F: the final transcript
```

## kokoro-http/1: speech synthesis

HTTP/1.1.

**`GET /health`** → `200`, JSON:

```json
{"ok": true, "engine": "kokoro-rknn", "rate": 24000, "voices": ["af_alloy", "..."],
 "voice": "af_heart", "lanes": 3, "lanes_busy": 0, "requests": 0, "npu": 28, "cpu_fallback": 1}
```

`lanes_busy` is how many NPU lanes are decoding now, `requests` how many `/tts` requests are in
progress, `npu` and `cpu_fallback` count sentences decoded on the NPU and on the CPU (sentences
longer than the largest bucket, 10 s of speech) since start. A server installed before
kokoro-rknn was published reports `"engine": "kokoro-82m-npu3"`, with the same fields.

**`POST /tts`**, body JSON:

| field | | default |
|---|---|---|
| `text` | required; stripped; empty → `400 {"error": "no text"}` | |
| `voice` | a name from `voices`; unknown or empty → the default | `--voice` (`af_heart`) |
| `speed` | float; missing, `0` or `null` → 1.0 | 1.0 |

The language is English (`en-us`). The reply is `200` with

```
Content-Type: application/octet-stream
X-Sample-Rate: 24000
X-Sample-Format: f32le
Transfer-Encoding: chunked
```

and a body of float32 little-endian mono samples. Text of up to 120 characters is synthesised
whole; longer text is cut at clause breaks into balanced pieces, and each piece is written as soon
as it is made, so audio arrives in bursts. **Read it as a stream and keep any partial sample**: a
read can end mid-sample. Within one request the pieces come in order; the first piece of any
request goes before other requests' later pieces.

**Behaviour:**
- **Hanging up stops the request** before its next piece. That is how a client interrupts (barge-in).
- If synthesis fails part-way, the stream simply ends early (the status is already sent).
- A body that is not JSON gets a 500.
- Up to 3 sentences are synthesised at once (one per NPU core); more wait for a free lane.

**A minimal client:**

```python
import http.client, json, numpy as np
c = http.client.HTTPConnection("board.local", 8771)
c.request("POST", "/tts", json.dumps({"text": "The library opens at nine."}),
          {"Content-Type": "application/json"})
r = c.getresponse()
rate, spare = int(r.getheader("X-Sample-Rate")), b""
while data := r.read1(65536):
    data, cut = spare + data, len(spare + data) // 4 * 4
    play(np.frombuffer(data[:cut], "<f4"), rate)
    spare = data[cut:]
```

## Board health

`rk1-speech-health.service` answers `GET /health` (or `GET /`) on port 8770 with one JSON object
for the whole board: `200` when both servers answer, `503` otherwise. The same JSON comes from
`rk1-speech-health` on the board. Abridged:

```json
{
  "ok": true, "board": "rk1-1", "hostname": "rk1-1", "time": "2026-11-02T21:10:37+0000",
  "services": {
    "stt": {"port": 8772, "ok": true, "ms": 3.2, "error": null,
            "health": {"ok": true, "engine": "moonshine-rknn", "encoder": "NPU", "...": "..."},
            "npu_mapped_mb": 193,
            "systemd": {"unit": "rk1-speech-stt.service", "active": "active", "sub": "running",
                        "enabled": "enabled", "pid": 1234, "restarts": 0, "since": "..."}},
    "tts": {"port": 8771, "ok": true, "ms": 1.9, "health": {"lanes": 3, "lanes_busy": 0, "voices": 54, "...": "..."},
            "npu_mapped_mb": 2119, "systemd": {"...": "..."}}
  },
  "npu": {"load_percent": [0, 0, 0], "load_error": null, "freq_hz": 1000000000, "device": "/dev/dri/card1",
          "mapped_mb_total": 2312, "address_space_mb": 4096,
          "processes": [{"pid": 1235, "comm": "python", "mb": 2119}, {"pid": 1234, "comm": "python", "mb": 193}]},
  "temperatures_c": {"soc": 29.6, "bigcore0": 30.5, "bigcore1": 30.5, "littlecore": 30.5, "center": 29.6, "gpu": 29.6, "npu": 29.6},
  "host": {"model": "Turing Machines RK1", "kernel": "5.10.160-rockchip", "arch": "aarch64", "uptime_s": 329987,
           "load_avg": [0.0, 0.06, 0.1], "mem_available_mb": 26048, "mem_total_mb": 31787},
  "versions": {"rk1_speech": "0.1.0", "rknpu_driver": "0.9.2", "librknnrt": "2.5.0 (74ea630ce8@2026-09-30T09:41:17)",
               "deploy": "20261102-214356-75b4c203", "moonshine_rknn": "d1822a562426", "kokoro_rknn": "e32630f9f33b"}
}
```

- `npu.load_percent` comes from `/sys/kernel/debug/rknpu/load`. Only root can read it; the health
  service has `CAP_DAC_READ_SEARCH` for that alone. Run by hand without root, it is `null` and
  `load_error` says why.
- `npu_mapped_mb` is what each process has mapped from the NPU's DRM node (`/proc/<pid>/maps`).
  The driver keeps no per-process account of the NPU's ~4 GB address space, so this is the
  nearest measure: if the total approaches 4096, the next model load fails.
- The service collects at most once a second, however often it is asked.
