# Settings, and why each has the value it has

`/etc/rk1-speech/rk1-speech.conf` holds the settings, as `KEY=value` lines. `install.py` writes
it on the first run with every key, and never overwrites it. To change a setting, edit the file
and run `install.py` again: it re-renders the units and announcements, makes a new deploy, and
restarts the servers if anything changed. `--set KEY=value` overrides a setting for one run.

The defaults are the configuration measured on Turing RK1 boards (RK3588, 32 GB) running
recognition and synthesis together. The measurements are in the
[kokoro-rknn](https://github.com/fursman/kokoro-rknn/blob/main/eval/RESULTS.md) and
[moonshine-rknn](https://github.com/fursman/moonshine-rknn/blob/main/bench/RESULTS.md) results.

## The settings

| setting | default | what it is | why this value |
|---|---|---|---|
| `RK1S_USER` | the user running `install.py` | the user both servers run as | Never root (the installer refuses). It owns the install directory, so updates need sudo only for system files. |
| `RK1S_PREFIX` | `/opt/rk1-speech` | where engines, models and deploys live | Needs ~4 GB with the graphs copied in, ~8 GB while building them. |
| `RK1S_BOARD_NAME` | the host name | the `board=` TXT record, and the name in health and the fleet tool | |
| `RK1S_STT_PORT` | `8772` | recognition port | moonshine-rknn's own default, and what existing clients use. |
| `RK1S_TTS_PORT` | `8771` | synthesis port | kokoro-rknn's default, and the port of the CPU Kokoro server it replaces, so clients need no change. |
| `RK1S_HEALTH_PORT` | `8770` | board health JSON | Next to the other two. |
| `RK1S_STT_NPU_CORE` | `auto` | NPU core for the recogniser's encoder (`auto`, `0`, `1`, `2`) | With `auto` the runtime puts the encoder on an idle core. Pinning it to core 0 was never better: equal when synthesis keeps off core 0 (whole-clip RTF 0.46 against 0.44), worse when synthesis uses all three cores (0.76 against 0.64), because a pinned encoder cannot step around a busy core. |
| `RK1S_STT_THREADS` | `4` | onnxruntime threads for the recogniser's CPU models | One per A76 core; the decoder is what uses them. |
| `RK1S_STT_NICE` | `-5` | the recogniser's scheduling priority | As on the boards where everything was measured: when both servers want the A76s, the transcript the user is waiting for goes first. Synthesis used at most 1.0 of the 4 A76 cores even at saturation, so there is room for both. |
| `RK1S_TTS_CORES` | `1,2,0` | one synthesis lane per NPU core, in order of preference | Three lanes give three sentences at once (1.69 → 3.25 → 4.58 audio-s/s). In this order, core 0, where recognition usually lands, takes a sentence only when cores 1 and 2 are both busy, so recognition is undisturbed up to 2 sentences in flight. `1,2` keeps synthesis off core 0 entirely (whole-clip recognition RTF stays at 0.44 instead of 0.64 under full load) at ~3.4 audio-s/s per board. Streamed recognition, which is what a voice assistant uses, was unaffected either way. |
| `RK1S_TTS_WORKERS` | `6` | CPU session sets (front, harmonic source, iSTFT) | More than the lanes, so the next sentences' front runs while the NPU decodes: NPU busy goes from ~80% to 87–90%, +8% throughput at saturation. Each costs memory (RSS ~4 GB in all); fewer use less. |
| `RK1S_TTS_THREADS` | `4` | onnxruntime threads per synthesis session | 4 is what was measured (and bit-identical to the earlier NPU server's output); 1 or 2 change the output slightly, within the CPU reference's own floor, and save a little CPU. |
| `RK1S_TTS_VOICE` | `af_heart` | default voice | The voice the quality was measured with. |
| `RK1S_CPU_AFFINITY` | `4-7` | CPU cores both servers may use | The four Cortex-A76s of the RK3588 (the installer checks they are A76s). On the A55s synthesis loses 40% of its throughput and 2–3 s of first audio, and recognition's decoder is several times slower. Both on the A76s was measured best for one board doing both. |
| `RK1S_STT_CAPACITY` | `1` | the `capacity=` TXT record for recognition | One stream at a time is what was measured next to synthesis. A hint for clients, not a limit. |
| `RK1S_TTS_CAPACITY` | `3` | the `capacity=` TXT record for synthesis | 3 sentences in flight keep first audio unchanged (p90 3.95 s); a 4th waits for a lane. |
| `RK1S_ANNOUNCE` | `yes` | publish the mDNS announcements | `no` removes them; clients then need the servers listed by hand. |
| `RK1S_HEALTH` | `yes` | run `rk1-speech-health.service` on `RK1S_HEALTH_PORT` | |
| `RK1S_PYTHON` | `auto` | the Python for the engines | `auto`: uv's Python 3.12 if uv is installed, else `python3.12`, else `python3` if it is 3.10–3.12. Both engines were measured with 3.12. |

## Fixed in the units

| setting | where | why |
|---|---|---|
| `Restart=on-failure`, `RestartSec=3` | both servers | Back within seconds after a crash; not after a deliberate stop. |
| `Conflicts=rk1-stt.service moonshine-rknn.service` | recognition | The same port and NPU. |
| `Conflicts=rk1-tts-npu3.service rk1-tts-npu2.service rk1-tts-cpu.service rk1-tts.service kokoro-rknn.service` | synthesis | Servers installed by hand before rk1-speech, and kokoro-rknn's example unit. Two NPU synthesisers do not fit the NPU's address space next to recognition (one older server alone mapped ~2.5 GB). |
| `After=network-online.target` | both | The announcements and clients need the network. |
| `AmbientCapabilities=CAP_DAC_READ_SEARCH`, `NoNewPrivileges=yes` | health | Only to read the NPU load from debugfs, which is root's; nothing else is privileged. |
| `CPUAffinity=0-3`, `Nice=10` | health | Out of the speech servers' way. |

## Pinned versions (versions.env)

| pin | value | why |
|---|---|---|
| `MOONSHINE_RKNN_COMMIT` | `d1822a5…` | The commit whose quick start was verified on a fresh board. |
| `KOKORO_RKNN_COMMIT` | `e32630f…` | The published version, checked bit-identical to the lanes server measured in production. |
| `KOKORO_BUCKETS` | `64 96 128 160 200 256 320 400` | The decoder lengths kokoro-rknn builds (1.6–10 s of speech per sentence). |
| `RKNN_TOOLKIT2` | `2.3.2` | What kokoro-rknn's graphs were built and measured with. 2.5.0 compiles them ~3× slower and its output was not evaluated. |
| `LIBRKNNRT_*` | 2.5.0, from the `v2.5.0` tag, sha256-checked | The runtime both engines were measured on. kokoro-rknn's C API use was written against it (its quirks are documented there); moonshine-rknn's encoder is compiled with toolkit 2.5.0. |
| `RKNPU_DRIVER_MIN` | `0.9.2` | Both engines were measured on 0.9.2; with an older driver moonshine-rknn runs its encoder on the CPU (its troubleshooting table). |
| `PYTHON_VERSION` | `3.12` | Measured with 3.12; 3.10–3.12 work. |

`constraints/stt.txt` and `constraints/tts.txt` hold the Python package versions installed on the
measured boards. They are applied as pip constraints to each engine's own requirements, so the
engines decide *what* is installed and the constraints decide *which version*.
