"""Health checks of the two servers, over their own protocols (docs/PROTOCOL.md).

    stt  TCP frame  'H' {}   ->  'O' {"ok": true, ...}
    tts  HTTP       GET /health -> 200 {"ok": true, ...}
"""
from __future__ import annotations

import http.client
import json
import socket
import struct
import time

HDR = struct.Struct(">cI")


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        part = sock.recv(n - len(buf))
        if not part:
            raise ConnectionError("closed")
        buf += part
    return bytes(buf)


def stt(host: str, port: int, timeout: float = 2.0) -> dict:
    """{"ok": bool, "ms": latency, "reply": {...} | None, "error": str | None}"""
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(HDR.pack(b"H", 2) + b"{}")
            kind, size = HDR.unpack(_recv_exact(s, HDR.size))
            payload = _recv_exact(s, size) if size else b""
        if kind != b"O":
            return {"ok": False, "ms": None, "reply": None, "error": f"answered {kind!r}"}
        reply = json.loads(payload or b"{}")
        return {"ok": bool(reply.get("ok")), "ms": round((time.perf_counter() - t0) * 1000, 1),
                "reply": reply, "error": None}
    except (OSError, ValueError, struct.error) as e:
        return {"ok": False, "ms": None, "reply": None, "error": str(e) or type(e).__name__}


def tts(host: str, port: int, timeout: float = 2.0) -> dict:
    t0 = time.perf_counter()
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/health")
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            return {"ok": False, "ms": None, "reply": None, "error": f"HTTP {resp.status}"}
        reply = json.loads(body or b"{}")
        # The voice list is long and the same everywhere: keep its length.
        if isinstance(reply.get("voices"), list):
            reply["voices"] = len(reply["voices"])
        return {"ok": bool(reply.get("ok")), "ms": round((time.perf_counter() - t0) * 1000, 1),
                "reply": reply, "error": None}
    except (OSError, ValueError, http.client.HTTPException) as e:
        return {"ok": False, "ms": None, "reply": None, "error": str(e) or type(e).__name__}
    finally:
        conn.close()


PROBES = {"stt": stt, "tts": tts}
