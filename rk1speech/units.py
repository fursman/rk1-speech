"""The systemd units, rendered from the settings (docs/SETTINGS.md says why each value).

Rendered rather than templated through EnvironmentFile=, so `systemctl cat` shows the
values that actually run.
"""
from __future__ import annotations

DOC = "https://github.com/fursman/rk1-speech"

# Servers installed by hand before rk1-speech (and the engines' own example units):
# the same ports and the same NPU address space, so never two at once.
LEGACY = {
    "stt": ["rk1-stt.service", "moonshine-rknn.service"],
    "tts": ["rk1-tts-npu3.service", "rk1-tts-npu2.service", "rk1-tts-cpu.service",
            "rk1-tts.service", "kokoro-rknn.service"],
}
UNITS = {"stt": "rk1-speech-stt.service", "tts": "rk1-speech-tts.service",
         "health": "rk1-speech-health.service"}


def stt(cfg: dict, user: str) -> str:
    p = cfg["RK1S_PREFIX"]
    return f"""# Written by rk1-speech install.py; edit /etc/rk1-speech/rk1-speech.conf and re-run it instead.
[Unit]
Description=rk1-speech: speech recognition (moonshine-rknn, encoder on the RK3588 NPU), port {cfg['RK1S_STT_PORT']}
Documentation={DOC}
After=network-online.target
Wants=network-online.target
Conflicts={' '.join(LEGACY['stt'])}

[Service]
User={user}
WorkingDirectory={p}/current/stt
# NPU core "auto": the runtime puts the encoder on an idle core, so it can step around
# the synthesiser's lanes (pinning it was never better in the measurements).
ExecStart={p}/current/stt/.venv/bin/python -m moonshine_rknn.server --models {p}/current/stt-models --host 0.0.0.0 --port {cfg['RK1S_STT_PORT']} --npu-core {cfg['RK1S_STT_NPU_CORE']} --threads {cfg['RK1S_STT_THREADS']}
# The four Cortex-A76 cores: on the A55s the decoder is several times slower.
CPUAffinity={cfg['RK1S_CPU_AFFINITY']}
# As on the boards it was measured on: recognition gets the A76s first when both servers
# want them at once.
Nice={cfg['RK1S_STT_NICE']}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
"""


def tts(cfg: dict, user: str) -> str:
    p = cfg["RK1S_PREFIX"]
    return f"""# Written by rk1-speech install.py; edit /etc/rk1-speech/rk1-speech.conf and re-run it instead.
[Unit]
Description=rk1-speech: speech synthesis (kokoro-rknn, decoder on the RK3588 NPU), port {cfg['RK1S_TTS_PORT']}
Documentation={DOC}
After=network-online.target
Wants=network-online.target
# One NPU synthesiser per board: they share a ~4 GB NPU address space with recognition.
Conflicts={' '.join(LEGACY['tts'])}

[Service]
User={user}
WorkingDirectory={p}/current/tts
# One decoder lane per NPU core, preferred in this order: with 1,2,0 core 0 (where
# recognition usually lands) only takes a sentence when 1 and 2 are both busy.
# More CPU workers than lanes, so the next sentences' front runs while the NPU decodes.
ExecStart={p}/current/tts/.venv/bin/python -m kokoro_rknn.server --models {p}/current/tts-models --host 0.0.0.0 --port {cfg['RK1S_TTS_PORT']} --cores {cfg['RK1S_TTS_CORES']} --workers {cfg['RK1S_TTS_WORKERS']} --threads {cfg['RK1S_TTS_THREADS']} --voice {cfg['RK1S_TTS_VOICE']}
# The four Cortex-A76 cores: on the A55s the CPU work becomes the bottleneck
# (throughput -40%, first audio +2-3 s).
CPUAffinity={cfg['RK1S_CPU_AFFINITY']}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
"""


def health(cfg: dict, user: str) -> str:
    p = cfg["RK1S_PREFIX"]
    return f"""# Written by rk1-speech install.py; edit /etc/rk1-speech/rk1-speech.conf and re-run it instead.
[Unit]
Description=rk1-speech: board health as JSON (GET /health), port {cfg['RK1S_HEALTH_PORT']}
Documentation={DOC}
After=network-online.target

[Service]
User={user}
ExecStart=/usr/bin/python3 -I {p}/current/bin/rk1-speech-health --serve --port {cfg['RK1S_HEALTH_PORT']}
# Only to read the NPU load from debugfs, which is root's alone; nothing else.
AmbientCapabilities=CAP_DAC_READ_SEARCH
CapabilityBoundingSet=CAP_DAC_READ_SEARCH
NoNewPrivileges=yes
PrivateTmp=yes
# Out of the speech servers' way: the A55s, at low priority.
CPUAffinity=0-3
Nice=10
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


RENDER = {"stt": stt, "tts": tts, "health": health}
