# rk1-speech

**Put an RK3588 board on the LAN and it joins the house as a listen-and-speak appliance.**
One installer sets up streaming speech recognition
([moonshine-rknn](https://github.com/fursman/moonshine-rknn)) and speech synthesis
([kokoro-rknn](https://github.com/fursman/kokoro-rknn)) with their heavy work on the NPU,
announces them over mDNS so clients find them by themselves, and reports the board's health as
one JSON. A fleet tool looks after any number of boards from one machine, one board at a time,
so speech never goes away while they are updated.

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![RK3588](https://img.shields.io/badge/NPU-RK3588-orange.svg)
![Python 3.10–3.12](https://img.shields.io/badge/python-3.10–3.12-blue.svg)

| piece | what it does |
|---|---|
| [`install.py`](install.py) | sets up a fresh board, or updates one: system checks, both engines at pinned versions, their models, systemd units with the measured settings, the mDNS announcements. Idempotent, with `--dry-run`, `--rollback` and `--uninstall` |
| [`avahi`](rk1speech/announce.py) announcements | `_speech-stt._tcp` and `_speech-tts._tcp`, with TXT records naming the protocol, engine, version, capacity, NPU cores and board |
| [`rk1-speech-health`](rk1speech/health.py) | both servers, NPU load and memory, temperatures and versions in one JSON, from the command line or `GET /health` on port 8770 |
| [`rk1-speech-fleet`](rk1speech/fleet.py) | from any Linux machine with SSH: discover the boards, then `status`, `update`, `rollback`, `restart` one board at a time, never taking the last server of a kind off the LAN |

Everything here is the Python standard library; the engines bring their own dependencies.

---

- [Hardware](#hardware) · [Performance](#performance) · [How it fits together](#how-it-fits-together)
- [Install](#install) · [The fleet](#the-fleet) · [Discovery](#discovery) · [Protocols](#protocols) · [Health](#health)
- [Troubleshooting](#troubleshooting) · [Development](#development) · [Credits and licence](#credits-and-licence)

## Hardware

Built and measured on **Turing RK1** modules (Rockchip RK3588: four Cortex-A76 and four
Cortex-A55 cores, a three-core NPU, 32 GB) in a Turing Pi 2, with:

| | tested | checked by the installer |
|---|---|---|
| OS | Ubuntu 22.04, kernel 5.10 (rockchip) | aarch64, `rockchip,rk3588` in the device tree, systemd |
| NPU driver | RKNPU 0.9.2 | 0.9.2 or later |
| NPU runtime | librknnrt 2.5.0 | 2.5.0 (installed from Rockchip's `v2.5.0` tag, sha256-checked, if missing) |
| CPU | A76s are cores 4–7 | the cores in `RK1S_CPU_AFFINITY` are Cortex-A76 |
| memory | 32 GB (the synthesiser uses ~4 GB) | warns below 7 GB |
| disk | ~4 GB, ~8 GB while building the NPU graphs | refuses when short |

Other RK3588 boards (Rock 5, Orange Pi 5, …) have the same NPU and runtime and should work, but
have not been tried.

## Performance

What one board does with both servers running, as installed (from the engines' measurements;
details in [kokoro-rknn's results](https://github.com/fursman/kokoro-rknn/blob/main/eval/RESULTS.md)
and [moonshine-rknn's](https://github.com/fursman/moonshine-rknn/blob/main/bench/RESULTS.md)):

**Listening** (Moonshine medium streaming, encoder on the NPU):
- **The final transcript is ready ~0 ms after you stop talking**: median / p90 / max 0 / 0 / 0 ms
  with 0.5 s of trailing silence, because it is decoded during the pause.
- **1.3 CPU cores while listening**, against 6.3 for moonshine-voice on the same board.
- Word error rate 2.66% (LibriSpeech test-clean, 300 utterances), the same as the original model.
- **With synthesis running on the same board** the final transcript still came ~1 ms after the
  audio ended in 135 of 138 streamed utterances; the other three took 0.2–0.7 s, all with
  synthesis using recognition's NPU core.

**Speaking** (Kokoro-82M, decoder on the NPU):
- **The quality of CPU Kokoro.** Log-spectral distance from kokoro-onnx on the CPU 0.72–1.50 dB,
  against 0.68–1.36 dB for kokoro-onnx against itself with other onnxruntime settings; loudness
  within 0.07 dB. In an informal blind test (one listener, loudness-matched), the listener
  could not reliably tell it from CPU Kokoro over four A/B rounds.
- **~1.1 s to first audio for a short sentence** ("Sure, I can help with that.", 1.6 s of
  speech: 0.95 s measured with the sentence spread over all three NPU cores; a single lane, as
  installed, is ~10% slower). A 5-second sentence starts in 2.9 s. Each sentence is synthesised
  whole, so a client should send a reply a sentence at a time.
- **4.6 seconds of audio per second per board** with three sentences in flight (one per NPU
  core) and first audio unchanged (p90 under 4 s); 5.1 at saturation. Four boards: ~18 audio-s/s,
  extrapolated, as the boards are independent.
- **0.19 CPU core-seconds per second of audio.** CPU Kokoro on the same board makes 1.2–1.4
  audio-s/s using all four A76 cores.

## How it fits together

```
     clients (a voice assistant, ...)                              any Linux machine
       │  browse _speech-stt._tcp / _speech-tts._tcp (mDNS)          rk1-speech-fleet
       │  health-check, then send each request to an idle board        │ ssh, one board
       ▼                                                               ▼ at a time
 ┌──────────────────────────────── one RK3588 board ────────────────────────────────┐
 │  avahi-daemon ◀── /etc/avahi/services/rk1-speech-{stt,tts}.service               │
 │                                                                                   │
 │  rk1-speech-stt.service  :8772            rk1-speech-tts.service  :8771           │
 │  moonshine-rknn, framed TCP               kokoro-rknn, HTTP POST /tts (streamed)  │
 │    A76 cores 4-7: VAD, frontend,            A76 cores 4-7: G2P, front, harmonic   │
 │                   decoder (Nice -5)                        source, iSTFT; 6 workers│
 │    NPU core "auto": streaming encoder       NPU lanes on cores 1, 2, then 0:      │
 │                                             whole-sentence decoder, one per core  │
 │                                                                                   │
 │  rk1-speech-health.service :8770   GET /health: both servers, NPU, temps, versions│
 │                                                                                   │
 │  NPU: 3 cores, ONE ~4 GB address space for every process on the board            │
 │       recognition maps ~0.2 GB, synthesis ~2.1 GB: room for nothing else big      │
 │                                                                                   │
 │  /opt/rk1-speech/current ─▶ deploys/<id> ─▶ engines/, models/ (previous kept)     │
 └───────────────────────────────────────────────────────────────────────────────────┘
```

**Why these settings** (each is in [docs/SETTINGS.md](docs/SETTINGS.md) with its measurement):
- **Synthesis lanes on NPU cores 1, 2, 0**, in that order: three sentences at once, and core 0,
  where recognition's encoder usually lands, is used only when 1 and 2 are both busy.
- **Recognition on NPU core `auto`**: the runtime picks an idle core, which pinning never beat.
- **All CPU work on the A76s (cores 4–7)**: on the A55s synthesis loses 40% of its throughput and
  2–3 s of first audio, and recognition's decoder is several times slower.
- **Ports 8772 and 8771**: the engines' defaults and what existing clients use.

## Install

On the board, as the user the servers should run as (with sudo):

```sh
sudo apt install -y git
git clone https://github.com/fursman/rk1-speech && cd rk1-speech
./install.py --dry-run          # what it would do; changes nothing
./install.py                    # do it
```

The dry run prints every check and every command it would run. This is one of the boards it
was built on, which already ran hand-installed servers, with
`--dry-run --takeover --kokoro-buckets <its old graphs> --moonshine-models <its old models>`
(commands and some paths shortened):

```
rk1-speech 0.1.0 on rk1-4 (dry run), service user ubuntu, prefix /opt/rk1-speech
[ok   ] platform: Turing Machines RK1, aarch64, kernel 5.10.160-rockchip
[ok   ] CPU cores: 4-7 are Cortex-A76
[ok   ] RKNPU driver: 0.9.2
[ok   ] librknnrt: 2.5.0 (74ea630ce8@2026-09-30T09:41:17)
[ok   ] Python: ~/.local/share/uv/python/cpython-3.12-linux-aarch64-gnu/bin/python3.12 (3.12.16, uv's Python 3.12)
[ok   ] system packages: git gcc libc6-dev curl avahi-daemon
[ok   ] disk and memory: 12.8 GB free on /opt, about 3.9 GB needed; 31 GB RAM
[WOULD] ports: 8772 (stt): rk1-stt.service (installed by hand); 8771 (tts): rk1-tts-npu3.service (installed by hand); will stop and disable rk1-stt.service, rk1-tts-npu3.service just before starting the new servers
[ok   ] NPU address space: the speech servers map ~2.3 GB of the NPU's ~4 GB address space; other processes: 0 MB
[WOULD] /opt/rk1-speech: missing
          sudo install -d -o ubuntu -g ubuntu -m 0755 /opt/rk1-speech
[WOULD] /etc/rk1-speech/rk1-speech.conf: missing: written with the settings in use
[WOULD] moonshine-rknn source: clone https://github.com/fursman/moonshine-rknn at d1822a562426
          git clone --quiet --no-checkout https://github.com/fursman/moonshine-rknn /opt/rk1-speech/engines/moonshine-rknn-d1822a562426-631b8afe.tmp
          ...
[WOULD] kokoro-rknn source: clone https://github.com/fursman/kokoro-rknn at e32630f9f33b
[WOULD] moonshine-rknn environment: /opt/rk1-speech/engines/moonshine-rknn-d1822a562426-631b8afe/.venv: requirements.txt with stt.txt (versions measured on the boards)
[WOULD] kokoro-rknn environment: /opt/rk1-speech/engines/kokoro-rknn-e32630f9f33b-4f4234ed/.venv: requirements.txt with tts.txt (versions measured on the boards)
[WOULD] moonshine-rknn models: copy 8 of 8 files from <its old models>, download the rest
[WOULD] kokoro-rknn models: /opt/rk1-speech/models/kokoro-rknn-e32630f9f33b-522a3d0d: fetch, extract, copy 16 NPU graphs from <its old graphs> (all known-good by hash or REGTASK-free by their logs)
[WOULD] deploy: new deploy 20261102-214356-75b4c203: moonshine-rknn d1822a562426, kokoro-rknn e32630f9f33b
[WOULD] systemd units: write rk1-speech-stt.service, rk1-speech-tts.service, rk1-speech-health.service, /usr/local/bin/rk1-speech-health
[WOULD] mDNS announcement: _speech-stt._tcp on 8772, _speech-tts._tcp on 8771 (avahi reads /etc/avahi/services by itself)
[WOULD] start: stop and disable rk1-stt.service, rk1-tts-npu3.service, then enable and start rk1-speech-stt.service, rk1-speech-tts.service, rk1-speech-health.service, wait until both answer; if they do not, roll back (or restore the hand-installed servers)

dry run: 13 step(s) would change something, 0 check(s) failed; nothing was changed.
```

On a fresh board the models step instead downloads moonshine-rknn's files and builds the 16 NPU
graphs (~15 minutes), and about 8 GB of disk is needed while building.

**What it does, in order** (each step looks first and does nothing if it is already right):

1. **Checks:** aarch64 and RK3588; the CPU cores are A76s; RKNPU driver ≥ 0.9.2; librknnrt
   2.5.0 (installs it if missing; replaces another version only with `--replace-runtime`);
   Python 3.10–3.12; disk and memory; the ports; **the NPU address space** (any other process
   holding NPU memory is listed, and the install refuses if the speech servers would not fit).
2. **System packages:** `git gcc libc6-dev curl avahi-daemon` (and `python3-venv` if needed).
3. **Both engines** at the commits in [versions.env](versions.env), each in its own venv with the
   package versions measured on the boards ([constraints/](constraints)), and kokoro-rknn's
   40-line C helper compiled.
4. **The models**, as each engine documents: moonshine-rknn's `scripts/fetch_models.py` (~460 MB,
   every file checksummed, including its prebuilt NPU encoder); kokoro-rknn's
   `build/fetch_models.py`, `build/extract.py` and `build/build_buckets.sh`, which compiles 16
   NPU graphs with rknn-toolkit2 2.3.2 (~15 minutes on the board). No model is stored in this
   repository.
5. **A deploy:** `/opt/rk1-speech/deploys/<id>` links the engines and models and holds a copy of
   rk1-speech itself and of the units and announcements it was installed with; `current` points
   at it, and the one before stays as `previous`. Engines, models and deploys are never changed
   once made: a new version or setting makes new ones beside them.
6. **The systemd units** `rk1-speech-stt`, `rk1-speech-tts`, `rk1-speech-health`, and the
   **avahi** service files.
7. **Start** them and wait until both servers answer. If they do not, it rolls back to the
   previous deploy, and if that does not answer either (or there is none) on a board taken over
   from hand-installed servers, it starts those again. It records which deploy it last started
   healthy, so a run that stopped before restarting the servers is finished by the next one.

**Faster first installs.** `--kokoro-buckets DIR` copies compiled graphs instead of building them,
from another board or from an x86-64 build (`build/build_buckets.sh` runs there too). Every graph
must either match a hash in [known-good/](known-good/kokoro-rknn-buckets.sha256) (the graphs that
ran in production) or come with its compile log showing no REGTASK error (see
[Troubleshooting](#troubleshooting)). `--moonshine-models DIR` copies moonshine-rknn's files
before fetching, and they are still checksummed.

**A board with servers installed by hand** (the same ports, e.g. `rk1-stt`, `rk1-tts-npu3`): the
installer stops and says so. `--takeover` builds everything first, then stops and disables those
units just before starting the new ones (about 30 s without speech on that board), and starts
them again if the new servers don't come up.

**Settings** live in `/etc/rk1-speech/rk1-speech.conf`; [docs/SETTINGS.md](docs/SETTINGS.md)
lists each, its default and the measurement behind it. Edit, then run `install.py` again.

**Other commands:**

```sh
/opt/rk1-speech/current/install.py --rollback     # back to the previous deploy (swaps current and previous; goes back again if it does not come up)
/opt/rk1-speech/current/install.py --prune        # delete deploys, engines and models neither uses
/opt/rk1-speech/current/install.py --uninstall    # stop and remove units and announcements (leaves /opt)
```

A rollback swaps the code, engines, models, units and announcements together. It does not
rewrite the config file: edit it back if the change you are undoing was a setting.

## The fleet

`bin/rk1-speech-fleet` runs from any Linux machine that can SSH to the boards with a key, as a
user with passwordless sudo there. It finds the boards by their announcements, plus a static
list for boards that don't announce yet, in `~/.config/rk1-speech/boards`:

```
# name   [user@]address   [stt port] [tts port]
rk1-1    ubuntu@rk1-1.local
rk1-2    ubuntu@rk1-2.local
```

```sh
rk1-speech-fleet discover                 # the boards and what they announce
rk1-speech-fleet status                   # both servers on each, lanes busy, NPU load, temperature, versions
rk1-speech-fleet update --dry-run         # what an update would do, board by board
rk1-speech-fleet update                   # install this checkout on each board in turn
rk1-speech-fleet rollback                 # each board back to its previous deploy, in turn
rk1-speech-fleet restart tts              # restart the synthesisers, in turn
rk1-speech-fleet --only rk1-2 restart     # just one board
```

`status` prints one line per board (illustrative values; a board without rk1-speech shows only
what its servers answer):

```
board      address         stt        tts        tts lanes npu load %  soc C  deploy                   versions
rk1-1      10.0.0.11       ok 2 ms    ok 2 ms    0/3       0 0 0       30.5   20261102-214356-75b4c203 moonshine d1822a562426 kokoro e32630f9f33b rknpu 0.9.2 rt 2.5.0
rk1-2      10.0.0.12       ok 1 ms    ok 2 ms    0/3       -           -      -                        (rk1-speech not installed, or no health)
```

**Rolling, and never all boards down at once.** For each board in turn, the tool first checks
that at least `--min-up` other boards (default 1) answer for every server it is about to
interrupt; if not, it stops before touching it. It then acts on that one board and waits up to
5 minutes for both servers to answer from the network before moving on. A board that fails
stops the rollout there, so the rest keep serving; `install.py` on that board has already rolled
itself back. With `--dry-run` it does the checks and prints what it would run.

`update` sends this checkout (or `--source DIR`) to `/opt/rk1-speech/src` on the board and runs
`install.py` there, passing `--takeover`, `--kokoro-buckets` and `--moonshine-models` through
if given. A request in flight on a board when it stops is cut off; a client that sends each
request to a board answering its health checks (as the voice assistant does) sends the next
ones to the other boards until it is back.

## Discovery

Each board announces two DNS-SD services through avahi. `python3 -m rk1speech.mdns` lists them
from any machine, with no avahi needed there (`avahi-browse -rt _speech-tts._tcp` works too).
From a board announcing its hand-installed servers (address replaced):

```
$ python3 -m rk1speech.mdns
{"instance": "rk1-4 speech recognition._speech-stt._tcp.local", "type": "_speech-stt._tcp", "name": "rk1-4 speech recognition",
 "host": "rk1-4.local", "address": "<address>", "port": 8772, "txt": {"txtvers": "1", "proto": "moonshine-tcp/1",
 "engine": "moonshine-rknn", "version": "b1687c4", "model": "moonshine-medium-streaming", "rate": "16000",
 "format": "f32le", "capacity": "1", "npu_cores": "auto", "board": "rk1-4"}}
{"instance": "rk1-4 speech synthesis._speech-tts._tcp.local", "type": "_speech-tts._tcp", "name": "rk1-4 speech synthesis",
 "host": "rk1-4.local", "address": "<address>", "port": 8771, "txt": {"txtvers": "1", "proto": "kokoro-http/1",
 "engine": "kokoro-82m-npu3", "version": "npu3-2026-11-02", "model": "kokoro-82m-v1.0", "rate": "24000",
 "format": "f32le", "path": "/tts", "voice": "af_heart", "capacity": "3", "npu_cores": "1,2,0", "board": "rk1-4"}}
```

A board installed by rk1-speech says `engine=kokoro-rknn`, the engine's commit as `version`, and
adds `health=8770`. The TXT records,
and the rules for clients, are in [docs/PROTOCOL.md](docs/PROTOCOL.md#service-discovery-dns-sd-over-mdns).
The essentials: use a service only if you know its `proto`; check its health before relying on
it, because an announcement means the board is set up to serve, not that the server is up this
second.

**Clients.** The [voice assistant](https://github.com/fursman/Assistant) this was built for looks
for both types in the background (every 5 minutes, every minute while it has found none), adds
each board it finds to its pool and uses it once its health check answers. Servers listed
explicitly in its configuration win over what it finds.

## Protocols

The servers' own protocols, unchanged: [docs/PROTOCOL.md](docs/PROTOCOL.md) describes each
precisely.

- **Recognition, `moonshine-tcp/1`**: TCP frames of a type byte, a 4-byte big-endian length and a
  payload. `B {"rate": 16000}` begins an utterance, `A` carries float32 audio as it arrives, the
  server sends `P` with the transcript so far, `E` ends the utterance and gets `F` with the final
  transcript. `T` transcribes a whole clip; `H` is health.
- **Synthesis, `kokoro-http/1`**: `POST /tts {"text", "voice", "speed"}` answers with float32
  little-endian mono at `X-Sample-Rate` (24000), streamed a piece at a time; hanging up stops it.
  `GET /health` gives the lanes, voices and counters.

## Health

```sh
rk1-speech-health                 # on the board; exit status 0 when both servers answer
sudo rk1-speech-health            # with the NPU load too (debugfs is root's)
curl http://<board>:8770/health   # the same from the health service (200 or 503)
rk1-speech-health --npu-processes # who holds NPU memory
```

One JSON: each server's answer, latency, systemd state and NPU memory; NPU load per core,
frequency and the total mapped against the ~4 GB space; every thermal zone; uptime, load and
memory; and the versions of the driver, the runtime, the deploy and both engines. An example is
in [docs/PROTOCOL.md](docs/PROTOCOL.md#board-health).

## Troubleshooting

| symptom | cause and fix |
|---|---|
| A server fails to start; `rknn_init` returns **-6**; the kernel log says `rknpu_gem_get_pages: dma map ... fail` | **The NPU address space is full.** Every process's NPU memory comes from one ~4 GB space; recognition maps ~0.2 GB and synthesis ~2.1 GB. Something else holds the rest: an older synthesis server, an experiment. `rk1-speech-health --npu-processes` lists them; stop them. The installer checks this before it starts anything. The `dma map` lines are harmless themselves (nothing runs). |
| A compile log says **`REGTASK: The bit width of field value exceeds the limit`** | **Never run that model, not even once.** It hangs the NPU for minutes (job timeouts and soft resets in the kernel log). kokoro-rknn's build deletes such graphs, and the installer refuses any graph that is neither known-good by hash nor accompanied by a clean compile log. You would only see this after changing the graph surgery. |
| The installer stops at `ports: ... installed by hand` | Hand-installed servers hold the ports. `--takeover` replaces them (and puts them back if the new ones fail). |
| The installer stops at `librknnrt: ... is 2.x, not 2.5.0` | Another runtime version is installed, which other programs may use. `--replace-runtime` replaces it. |
| Synthesis takes ~30 s to answer after a start | Normal: three lanes of eight compiled graphs load and warm up. Clients and the fleet wait for its health. |
| First audio 2–3 s slower than above, or partial transcripts lag | The servers are on the A55 cores. `RK1S_CPU_AFFINITY` must be the A76s (4–7 on the RK3588); the installer checks. |
| Recognition's health says `"encoder": "CPU"` | `librknnrt.so` is not in `/usr/lib`, or the RKNPU driver is older than 0.9.2. |
| `npu.load_percent` is `null` | Read without root: use `sudo rk1-speech-health` or the health service. |
| No board is found over mDNS | Is `avahi-daemon` running on the board (`systemctl status avahi-daemon`), and are its files in `/etc/avahi/services`? Is multicast blocked between your networks (Wi-Fi client isolation, VLANs)? `python3 -m rk1speech.mdns --target <board address>` asks a board directly. Otherwise list the boards: the fleet's static list, or your client's own configuration. |
| Disk full | `install.py --prune`; `/opt/rk1-speech/build/` (the toolkit, ~4 GB) can go once the models exist. |

## Development

```sh
python3 -m pytest tests        # no board, no network: stand-ins for both
```

| path | |
|---|---|
| `install.py`, `rk1speech/installer.py` | the installer: one function per step, `STEPS` at the bottom |
| `rk1speech/units.py`, `rk1speech/announce.py` | the systemd units and avahi files, rendered from the settings |
| `rk1speech/health.py`, `rk1speech/probe.py` | the health JSON, and the two servers' health checks |
| `rk1speech/mdns.py` | a small DNS-SD browser (one-shot mDNS queries) |
| `rk1speech/fleet.py`, `bin/rk1-speech-fleet` | the fleet tool |
| `versions.env`, `constraints/`, `known-good/` | what gets installed, and which NPU graphs are trusted |

To release: change the pins in `versions.env` (or the code), test on one board with
`install.py --dry-run` and then `rk1-speech-fleet --only <board> update`, then
`rk1-speech-fleet update` for the rest.

## Credits and licence

rk1-speech is the deployment layer; the work it deploys is other people's as much as ours:

- **[Kokoro](https://github.com/hexgrad/kokoro)** and the Kokoro-82M weights, by **hexgrad**,
  Apache-2.0: the voice.
- **[kokoro-server](https://github.com/marty1885/kokoro-server)** by **marty1885**: Kokoro's
  decoder on the RK3588 NPU before anyone else, and where kokoro-rknn started (GlobalAveragePool
  normalisation, folding Snake's 1/α, the ConvTranspose rewrite). Parts of kokoro-rknn's graph
  surgery are adapted from it; kokoro-server has no licence file, so, as kokoro-rknn's
  [NOTICE](https://github.com/fursman/kokoro-rknn/blob/main/NOTICE) explains, what was adapted is
  treated as carrying Kokoro's Apache-2.0 terms. rk1-speech contains none of its code, and
  installs kokoro-rknn under that same policy.
- **[kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx)** by **thewh1teagle**, MIT: the
  ONNX export and the `Kokoro` class kokoro-rknn extends.
- **[Moonshine](https://github.com/moonshine-ai/moonshine)** by **Moonshine AI** (moonshine-ai),
  MIT: the speech recogniser and moonshine-voice's model build. **[Silero VAD](https://github.com/snakers4/silero-vad)**, MIT.
- **Rockchip**: the RKNPU driver, the [RKNN runtime (librknnrt) and rknn-toolkit2](https://github.com/airockchip/rknn-toolkit2),
  under Rockchip's terms. The installer downloads the runtime from Rockchip's repository; it is
  not redistributed here.
- **[moonshine-rknn](https://github.com/fursman/moonshine-rknn)** (MIT) and
  **[kokoro-rknn](https://github.com/fursman/kokoro-rknn)** (Apache-2.0): the two engines.

No model weights or compiled models are in this repository; `known-good/` holds only hashes.
rk1-speech is licensed under the [Apache License 2.0](LICENSE); see [NOTICE](NOTICE).
