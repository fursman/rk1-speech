"""DNS-SD announcements: avahi service files for the board's speech servers.

    _speech-stt._tcp   speech recognition, moonshine-rknn's framed TCP protocol
    _speech-tts._tcp   speech synthesis, kokoro-rknn's HTTP protocol

The TXT records (docs/PROTOCOL.md) say which protocol, engine and version answer,
how many requests the board takes at once without slowing down, which NPU cores it
uses and the board's name. avahi-daemon picks the files up from /etc/avahi/services
by itself; removing them withdraws the announcement.

    python3 -m rk1speech.announce stt|tts [--config FILE] [--engine-version V] ...   # prints the XML
"""
from __future__ import annotations

import argparse
import sys
from xml.sax.saxutils import escape

from . import config

TYPES = {"stt": "_speech-stt._tcp", "tts": "_speech-tts._tcp"}
PROTOCOLS = {"stt": "moonshine-tcp/1", "tts": "kokoro-http/1"}
LABELS = {"stt": "speech recognition", "tts": "speech synthesis"}
FILES = {"stt": "rk1-speech-stt.service", "tts": "rk1-speech-tts.service"}


def txt_records(kind: str, cfg: dict, engine: str = "", version: str = "") -> list:
    """key=value strings, in a fixed order."""
    board = cfg["RK1S_BOARD_NAME"]
    rec = [("txtvers", "1"), ("proto", PROTOCOLS[kind])]
    if kind == "stt":
        rec += [("engine", engine or "moonshine-rknn"), ("version", version or "unknown"),
                ("model", "moonshine-medium-streaming"), ("rate", "16000"), ("format", "f32le"),
                ("capacity", cfg["RK1S_STT_CAPACITY"]), ("npu_cores", cfg["RK1S_STT_NPU_CORE"])]
    else:
        rec += [("engine", engine or "kokoro-rknn"), ("version", version or "unknown"),
                ("model", "kokoro-82m-v1.0"), ("rate", "24000"), ("format", "f32le"),
                ("path", "/tts"), ("voice", cfg["RK1S_TTS_VOICE"]),
                ("capacity", cfg["RK1S_TTS_CAPACITY"]), ("npu_cores", cfg["RK1S_TTS_CORES"])]
    rec.append(("board", board))
    if config.truthy(cfg.get("RK1S_HEALTH", "no")):
        rec.append(("health", cfg["RK1S_HEALTH_PORT"]))
    out = []
    for k, v in rec:
        item = f"{k}={v}"
        if len(item.encode()) > 255:
            raise ValueError(f"TXT record too long: {item[:40]}...")
        out.append(item)
    return out


def render(kind: str, cfg: dict, engine: str = "", version: str = "") -> str:
    port = cfg["RK1S_STT_PORT"] if kind == "stt" else cfg["RK1S_TTS_PORT"]
    txt = "\n".join(f"    <txt-record>{escape(t)}</txt-record>" for t in txt_records(kind, cfg, engine, version))
    # %h is avahi's host name; the board name is also in the TXT records.
    return f"""<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<!-- Written by rk1-speech (https://github.com/fursman/rk1-speech): announces this
     board's {LABELS[kind]} server. Delete this file to withdraw the announcement. -->
<service-group>
  <name replace-wildcards="yes">%h {LABELS[kind]}</name>
  <service>
    <type>{TYPES[kind]}</type>
    <port>{int(port)}</port>
{txt}
  </service>
</service-group>
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m rk1speech.announce", description=__doc__.split("\n")[0])
    ap.add_argument("kind", choices=sorted(TYPES))
    ap.add_argument("--config", default=str(config.CONF))
    ap.add_argument("--engine", default="", help="engine name for the TXT record (default: the engine's own)")
    ap.add_argument("--engine-version", default="", help="engine version for the TXT record")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="override a setting, e.g. --set RK1S_HEALTH=no")
    a = ap.parse_args(argv)
    cfg = config.load(a.config)
    for item in a.set:
        k, _, v = item.partition("=")
        if k not in config.DEFAULTS:
            ap.error(f"unknown setting {k}")
        cfg[k] = v
    sys.stdout.write(render(a.kind, cfg, a.engine, a.engine_version))
    return 0


if __name__ == "__main__":
    sys.exit(main())
