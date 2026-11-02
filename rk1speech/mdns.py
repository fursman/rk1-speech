"""A small mDNS / DNS-SD browser: standard library only, no avahi needed on the asking side.

    from rk1speech.mdns import browse
    for s in browse("_speech-tts._tcp"):
        print(s.instance, s.address, s.port, s.txt)

It sends one-shot queries (RFC 6762 section 5.1) to 224.0.0.251:5353 from an
ephemeral port, so responders answer by unicast to that port: no multicast
group to join, no port 5353 to share with a local avahi-daemon. A PTR query
finds the instances; their SRV, TXT and A records usually come back in the same
reply (avahi adds them), and anything missing is asked for once more.

`targets` sends the same query by unicast to given hosts' port 5353 instead,
which avahi answers too: useful across a router, or to ask one board.

    python3 -m rk1speech.mdns _speech-stt._tcp _speech-tts._tcp [--target IP] [--timeout S]
"""
from __future__ import annotations

import argparse
import json
import random
import select
import socket
import struct
import sys
import time
from typing import NamedTuple

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
A, PTR, TXT, AAAA, SRV = 1, 12, 16, 28, 33
CLASS_IN = 1


class Service(NamedTuple):
    instance: str       # "rk1-4 speech recognition._speech-stt._tcp.local"
    type: str           # "_speech-stt._tcp"
    name: str           # the instance label alone: "rk1-4 speech recognition"
    host: str           # SRV target: "rk1-4.local"
    address: str        # first IPv4 address of host, "" when none came back
    port: int
    txt: dict           # TXT key=value pairs (keys lower-cased; values str)


# -- wire format --------------------------------------------------------------

def encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("utf-8")
        if not 0 < len(raw) < 64:
            raise ValueError(f"bad DNS label in {name!r}")
        out += bytes([len(raw)]) + raw
    return out + b"\0"


def build_query(questions, qid: int = 0) -> bytes:
    """questions: [(name, qtype)]. One packet asking all of them."""
    head = struct.pack(">HHHHHH", qid, 0, len(questions), 0, 0, 0)
    return head + b"".join(encode_name(n) + struct.pack(">HH", t, CLASS_IN) for n, t in questions)


def _read_name(data: bytes, off: int):
    labels, jumped, end, hops = [], False, off, 0
    while True:
        if off >= len(data):
            raise ValueError("name runs past the packet")
        n = data[off]
        if n & 0xC0 == 0xC0:
            if off + 1 >= len(data):
                raise ValueError("truncated pointer")
            if not jumped:
                end = off + 2
            off = ((n & 0x3F) << 8) | data[off + 1]
            jumped, hops = True, hops + 1
            if hops > 64:
                raise ValueError("pointer loop")
            continue
        if n == 0:
            if not jumped:
                end = off + 1
            return ".".join(labels), end
        labels.append(data[off + 1:off + 1 + n].decode("utf-8", "replace"))
        off += 1 + n


class Record(NamedTuple):
    name: str
    type: int
    ttl: int
    data: object      # PTR/SRV target name, (prio, weight, port, target), [txt bytes], address str


def parse(data: bytes):
    """(id, is_response, [Record]) for every answer, authority and additional record."""
    if len(data) < 12:
        raise ValueError("short packet")
    qid, flags, qd, an, ns, ar = struct.unpack(">HHHHHH", data[:12])
    off = 12
    for _ in range(qd):
        _, off = _read_name(data, off)
        off += 4
    records = []
    for _ in range(an + ns + ar):
        name, off = _read_name(data, off)
        if off + 10 > len(data):
            raise ValueError("truncated record")
        rtype, _rclass, ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
        off += 10
        rd = data[off:off + rdlen]
        if len(rd) != rdlen:
            raise ValueError("truncated rdata")
        if rtype == PTR:
            value = _read_name(data, off)[0]
        elif rtype == SRV:
            prio, weight, port = struct.unpack(">HHH", rd[:6])
            value = (prio, weight, port, _read_name(data, off + 6)[0])
        elif rtype == TXT:
            value, i = [], 0
            while i < len(rd):
                n = rd[i]
                value.append(rd[i + 1:i + 1 + n])
                i += 1 + n
        elif rtype == A and rdlen == 4:
            value = socket.inet_ntoa(rd)
        elif rtype == AAAA and rdlen == 16:
            value = socket.inet_ntop(socket.AF_INET6, rd)
        else:
            value = rd
        records.append(Record(name, rtype, ttl, value))
        off += rdlen
    return qid, bool(flags & 0x8000), records


def txt_dict(strings) -> dict:
    """RFC 6763 section 6: key=value, keys case-insensitive, the first one wins."""
    out = {}
    for s in strings or ():
        s = s.decode("utf-8", "replace") if isinstance(s, (bytes, bytearray)) else str(s)
        if not s or s.startswith("="):
            continue
        key, _, value = s.partition("=")
        out.setdefault(key.lower(), value)
    return out


# -- asking -------------------------------------------------------------------

def _default_interface_address() -> str:
    """The IPv4 address this machine would use to reach the LAN, "" if none."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))      # TEST-NET-1: nothing is sent
            return s.getsockname()[0]
    except OSError:
        return ""


def _exchange(questions, timeout: float, targets=(), iface: str = "", resend: float = 0.25,
              port: int = MDNS_PORT):
    """Send the questions (twice, `resend` apart, as mDNS loses packets) and gather
    every record of every reply that arrives within `timeout` seconds."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        if iface:
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface))
            except OSError:
                pass
        sock.bind(("0.0.0.0", 0))
        qid = random.randint(1, 0xFFFF)
        packet = build_query(questions, qid)
        dests = [(t, port) for t in targets] or [(MDNS_GROUP, port)]
        records = []
        start = time.monotonic()
        sends = [start, start + resend]
        while True:
            now = time.monotonic()
            while sends and sends[0] <= now:
                sends.pop(0)
                for d in dests:
                    try:
                        sock.sendto(packet, d)
                    except OSError:
                        pass
            left = start + timeout - now
            if left <= 0:
                return records
            wait = min(left, sends[0] - now) if sends else left
            ready, _, _ = select.select([sock], [], [], max(0.0, wait))
            if not ready:
                continue
            try:
                data, _src = sock.recvfrom(9000)
                rid, is_resp, recs = parse(data)
            except (OSError, ValueError, struct.error, IndexError):
                continue
            # Legacy unicast replies echo our id; a multicast responder may send 0.
            if is_resp and rid in (qid, 0):
                records.extend(recs)
    finally:
        sock.close()


def _norm(name: str) -> str:
    return name.rstrip(".").lower()


def browse(service_type: str, timeout: float = 1.0, targets=(), domain: str = "local",
           iface: str | None = None, port: int = MDNS_PORT) -> list:
    """Every instance of `service_type` ("_speech-stt._tcp") that answers within
    about `timeout` seconds (plus up to half that again for missing records),
    sorted by name. Never raises for network trouble: no answer is an empty list."""
    fqtype = f"{service_type}.{domain}"
    iface = _default_interface_address() if iface is None else iface
    try:
        recs = _exchange([(fqtype, PTR)], timeout, targets, iface, port=port)
    except (OSError, ValueError):
        return []
    ptr, srv, txt, addr = set(), {}, {}, {}

    def absorb(records):
        for r in records:
            if r.type == PTR and _norm(r.name) == _norm(fqtype) and r.ttl > 0:
                ptr.add(r.data)
            elif r.type == SRV:
                srv[_norm(r.name)] = r.data
            elif r.type == TXT:
                txt[_norm(r.name)] = r.data
            elif r.type == A and isinstance(r.data, str):     # a malformed A record stays bytes
                addr.setdefault(_norm(r.name), [])
                if r.data not in addr[_norm(r.name)]:
                    addr[_norm(r.name)].append(r.data)
    absorb(recs)
    # Whatever the first replies left out, asked for once.
    missing = [(i, SRV) for i in ptr if _norm(i) not in srv] + \
              [(i, TXT) for i in ptr if _norm(i) not in txt]
    if missing:
        try:
            absorb(_exchange(missing, timeout / 2, targets, iface, port=port))
        except (OSError, ValueError):
            pass
    hosts = {srv[_norm(i)][3] for i in ptr if _norm(i) in srv and srv[_norm(i)][3].strip(".")}
    need = [(h, A) for h in hosts if _norm(h) not in addr]
    if need:
        try:
            absorb(_exchange(need, timeout / 2, targets, iface, port=port))
        except (OSError, ValueError):
            pass
    out = []
    for inst in ptr:
        s = srv.get(_norm(inst))
        if s is None or not s[3].strip("."):
            continue
        host = s[3]
        addrs = addr.get(_norm(host), [])
        label = inst[:-len(fqtype) - 1] if _norm(inst).endswith("." + _norm(fqtype)) else inst
        out.append(Service(inst.rstrip("."), service_type, label, host.rstrip("."),
                           addrs[0] if addrs else "", s[2], txt_dict(txt.get(_norm(inst)))))
    return sorted(out, key=lambda x: (x.name.lower(), x.address, x.port))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m rk1speech.mdns",
                                 description="List DNS-SD services on the LAN, one JSON object per line.")
    ap.add_argument("types", nargs="*", default=["_speech-stt._tcp", "_speech-tts._tcp"])
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--target", action="append", default=[],
                    help="ask this host by unicast instead of the multicast group")
    a = ap.parse_args(argv)
    for t in a.types:
        for s in browse(t, a.timeout, a.target):
            print(json.dumps(s._asdict()), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
