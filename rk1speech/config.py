"""Settings: /etc/rk1-speech/rk1-speech.conf (KEY=VALUE, shell-like) over the defaults,
and the pinned versions in versions.env. Standard library only.

Every setting is documented in docs/SETTINGS.md; the defaults are the configuration
measured on the Turing RK1 (see the README's Performance section).
"""
from __future__ import annotations

import shlex
import socket
from pathlib import Path

CONF = Path("/etc/rk1-speech/rk1-speech.conf")
ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "RK1S_USER": "",                 # service user; empty = the user running install.py
    "RK1S_PREFIX": "/opt/rk1-speech",
    "RK1S_BOARD_NAME": "",           # empty = the host name
    "RK1S_STT_PORT": "8772",
    "RK1S_TTS_PORT": "8771",
    "RK1S_HEALTH_PORT": "8770",
    "RK1S_STT_NPU_CORE": "auto",
    "RK1S_STT_THREADS": "4",
    "RK1S_STT_NICE": "-5",
    "RK1S_TTS_CORES": "1,2,0",
    "RK1S_TTS_WORKERS": "6",
    "RK1S_TTS_THREADS": "4",
    "RK1S_TTS_VOICE": "af_heart",
    "RK1S_CPU_AFFINITY": "4-7",
    "RK1S_STT_CAPACITY": "1",
    "RK1S_TTS_CAPACITY": "3",
    "RK1S_ANNOUNCE": "yes",
    "RK1S_HEALTH": "yes",
    "RK1S_PYTHON": "auto",
}


def parse_env(text: str) -> dict:
    """KEY=VALUE lines; '#' comments; values may be quoted as in a shell."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        try:
            words = shlex.split(value, comments=True)
        except ValueError:
            words = [value.strip()]
        out[key] = " ".join(words)
    return out


def load(path: Path | str | None = CONF) -> dict:
    """The defaults, overridden by the config file when it exists."""
    cfg = dict(DEFAULTS)
    if path is not None and Path(path).is_file():
        cfg.update({k: v for k, v in parse_env(Path(path).read_text()).items() if k in DEFAULTS})
    if not cfg["RK1S_BOARD_NAME"]:
        cfg["RK1S_BOARD_NAME"] = socket.gethostname().split(".")[0]
    return cfg


def versions(root: Path = ROOT) -> dict:
    return parse_env((root / "versions.env").read_text())


def truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "yes", "true", "on")


def render_conf(cfg: dict) -> str:
    """The config file the installer writes when there is none: every key, with its value."""
    lines = ["# rk1-speech settings (docs/SETTINGS.md). Re-run install.py after editing.", ""]
    for key in DEFAULTS:
        lines.append(f"{key}={shlex.quote(str(cfg.get(key, DEFAULTS[key])))}")
    return "\n".join(lines) + "\n"
