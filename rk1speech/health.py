"""One JSON answer for the whole board: both speech servers, the NPU, temperatures, versions.

    rk1-speech-health                    print it (exit status 0 when both servers answer)
    rk1-speech-health --serve            serve it: GET /health on RK1S_HEALTH_PORT (8770)
    rk1-speech-health --stt-unit rk1-stt --tts-unit rk1-tts-npu3     units installed by hand

NPU load comes from /sys/kernel/debug/rknpu/load, which only root (or a process with
CAP_DAC_READ_SEARCH, as rk1-speech-health.service has) can read; without it the field is
null and "npu.load_error" says why. Everything else is readable by any user.

Standard library only: it runs with the system python3.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, config, probe

NPU_ADDRESS_SPACE_MB = 4096      # one IOVA space for every process on the board (see README)
_RUNTIME_CACHE = {}


def read(path, default=None):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def npu_card() -> str:
    """The NPU's DRM node (/dev/dri/card1 on the RK1), "" when there is none."""
    for link in glob.glob("/dev/dri/by-path/*npu-card"):
        return os.path.realpath(link)
    for card in sorted(glob.glob("/sys/class/drm/card*")):
        if read(f"{card}/device/uevent", "").find("DRIVER=RKNPU") >= 0:
            return "/dev/dri/" + os.path.basename(card)
    return ""


def npu_load(path="/sys/kernel/debug/rknpu/load"):
    """([percent per core], None) or (None, why)."""
    try:
        text = Path(path).read_text()
    except PermissionError:
        return None, "needs root or CAP_DAC_READ_SEARCH (debugfs)"
    except OSError as e:
        return None, str(e)
    loads = [int(x) for x in re.findall(r"Core\d+:\s*(\d+)%", text)]
    return (loads or None), (None if loads else f"unreadable: {text.strip()[:60]!r}")


def npu_freq_hz():
    for f in glob.glob("/sys/class/devfreq/*.npu/cur_freq"):
        v = read(f)
        if v and v.isdigit():
            return int(v)
    return None


def driver_version():
    v = read("/sys/module/rknpu/version")
    if v:
        return v
    m = re.search(r"v?(\d+\.\d+\.\d+)", read("/sys/kernel/debug/rknpu/version", "") or "")
    return m.group(1) if m else None


def librknnrt_version(path="/usr/lib/librknnrt.so"):
    """'2.5.0 (74ea630ce8@...)' from the library's own version string; None when absent."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime, st.st_size)
    if key not in _RUNTIME_CACHE:
        with open(path, "rb") as f:
            m = re.search(rb"librknnrt version: ([ -~]+)", f.read())
        _RUNTIME_CACHE[key] = m.group(1).decode().strip() if m else "unknown"
    return _RUNTIME_CACHE[key]


def temperatures() -> dict:
    """{zone type: degrees C} for every thermal zone ("soc", "npu", "bigcore0", ...)."""
    out = {}
    for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        kind, temp = read(f"{zone}/type"), read(f"{zone}/temp")
        if kind and temp and temp.lstrip("-").isdigit():
            out[kind.replace("-thermal", "")] = round(int(temp) / 1000, 1)
    return out


def unit_state(unit: str) -> dict:
    props = "ActiveState,SubState,UnitFileState,MainPID,NRestarts,ActiveEnterTimestamp,LoadState"
    try:
        out = subprocess.run(["systemctl", "show", unit, "-p", props], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"unit": unit, "error": str(e)}
    kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    pid = int(kv.get("MainPID") or 0)
    return {"unit": unit, "loaded": kv.get("LoadState") == "loaded",
            "active": kv.get("ActiveState"), "sub": kv.get("SubState"),
            "enabled": kv.get("UnitFileState"), "pid": pid or None,
            "restarts": int(kv["NRestarts"]) if kv.get("NRestarts", "").isdigit() else None,
            "since": kv.get("ActiveEnterTimestamp") or None}


def mapped_mb(pid: int, card: str):
    """MB of NPU buffers process `pid` has mapped (its /proc/pid/maps lines for the NPU's
    DRM node); None when that is not readable. A stand-in for the NPU address space it
    uses: the driver keeps no per-process account."""
    if not pid or not card:
        return None
    total = 0
    try:
        with open(f"/proc/{pid}/maps") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 6 and parts[5] == card:
                    lo, hi = parts[0].split("-")
                    total += int(hi, 16) - int(lo, 16)
    except OSError:
        return None
    return round(total / 2 ** 20)


def npu_processes(card: str | None = None) -> list:
    """[{pid, comm, mb}] for every process with NPU buffers mapped (as far as readable)."""
    card = npu_card() if card is None else card
    out = []
    if not card:
        return out
    for d in glob.glob("/proc/[0-9]*"):
        pid = int(os.path.basename(d))
        mb = mapped_mb(pid, card)
        if mb:
            out.append({"pid": pid, "comm": read(f"{d}/comm", "?"), "mb": mb,
                        "cmd": (read(f"{d}/cmdline", "") or "").replace("\0", " ").strip()[:160]})
    return sorted(out, key=lambda p: -p["mb"])


def deploy_manifest(prefix: str) -> dict | None:
    try:
        return json.loads((Path(prefix) / "current" / "manifest.json").read_text())
    except (OSError, ValueError):
        return None


def collect(cfg: dict, units: dict, ports: dict, host: str = "127.0.0.1", timeout: float = 2.0) -> dict:
    card = npu_card()
    loads, load_error = npu_load()
    services = {}
    for kind in ("stt", "tts"):
        st = unit_state(units[kind])
        res = probe.PROBES[kind](host, int(ports[kind]), timeout)
        services[kind] = dict(port=int(ports[kind]), ok=res["ok"], ms=res["ms"], error=res["error"],
                              health=res["reply"], npu_mapped_mb=mapped_mb(st.get("pid") or 0, card),
                              **{"systemd": st})
    procs = npu_processes(card)
    manifest = deploy_manifest(cfg["RK1S_PREFIX"])
    try:
        load_avg = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        load_avg = None
    mem = dict(line.split(":", 1) for line in (read("/proc/meminfo", "") or "").splitlines() if ":" in line)

    def kb(key):
        v = mem.get(key, "").split()
        return round(int(v[0]) / 1024) if v and v[0].isdigit() else None
    return {
        "ok": all(s["ok"] for s in services.values()),
        "board": cfg["RK1S_BOARD_NAME"],
        "hostname": socket.gethostname(),
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "services": services,
        "npu": {"load_percent": loads, "load_error": load_error, "freq_hz": npu_freq_hz(),
                "device": card or None,
                "mapped_mb_total": sum(p["mb"] for p in procs),
                "address_space_mb": NPU_ADDRESS_SPACE_MB,
                "processes": [{k: p[k] for k in ("pid", "comm", "mb")} for p in procs]},
        "temperatures_c": temperatures(),
        "host": {"model": (read("/proc/device-tree/model", "") or "").rstrip("\0") or None,
                 "kernel": platform.release(), "arch": platform.machine(),
                 "uptime_s": int(float((read("/proc/uptime", "0") or "0").split()[0])),
                 "load_avg": load_avg, "mem_available_mb": kb("MemAvailable"),
                 "mem_total_mb": kb("MemTotal")},
        "versions": {"rk1_speech": __version__, "rknpu_driver": driver_version(),
                     "librknnrt": librknnrt_version(),
                     "deploy": manifest.get("id") if manifest else None,
                     "moonshine_rknn": (manifest or {}).get("engines", {}).get("moonshine-rknn"),
                     "kokoro_rknn": (manifest or {}).get("engines", {}).get("kokoro-rknn")},
    }


def serve(cfg, units, ports, port: int, bind: str = "0.0.0.0"):
    cache = {"at": 0.0, "body": b"", "ok": False}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] not in ("/", "/health"):
                self.send_error(404)
                return
            with lock:      # at most one collection a second, however many ask
                if time.monotonic() - cache["at"] > 1.0:
                    report = collect(cfg, units, ports)
                    cache.update(at=time.monotonic(), body=json.dumps(report).encode(), ok=report["ok"])
                body, ok = cache["body"], cache["ok"]
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    ThreadingHTTPServer((bind, port), Handler).serve_forever()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rk1-speech-health", description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(config.CONF))
    ap.add_argument("--stt-unit", default="rk1-speech-stt.service")
    ap.add_argument("--tts-unit", default="rk1-speech-tts.service")
    ap.add_argument("--stt-port", type=int, help="default RK1S_STT_PORT")
    ap.add_argument("--tts-port", type=int, help="default RK1S_TTS_PORT")
    ap.add_argument("--serve", action="store_true", help="answer GET /health over HTTP instead")
    ap.add_argument("--port", type=int, help="for --serve; default RK1S_HEALTH_PORT")
    ap.add_argument("--npu-processes", action="store_true",
                    help="list the processes with NPU memory mapped, and exit")
    ap.add_argument("--compact", action="store_true", help="one line of JSON")
    a = ap.parse_args(argv)
    cfg = config.load(a.config)
    if a.npu_processes:
        print(json.dumps(npu_processes(), indent=None if a.compact else 2))
        return 0
    units = {"stt": a.stt_unit, "tts": a.tts_unit}
    ports = {"stt": a.stt_port or cfg["RK1S_STT_PORT"], "tts": a.tts_port or cfg["RK1S_TTS_PORT"]}
    if a.serve:
        serve(cfg, units, ports, a.port or int(cfg["RK1S_HEALTH_PORT"]))
        return 0
    report = collect(cfg, units, ports)
    print(json.dumps(report, indent=None if a.compact else 2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
