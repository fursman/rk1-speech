"""mDNS browsing (rk1speech/mdns.py) against a fake responder on localhost, and the
announcements (rk1speech/announce.py) it should find. No network needed.

    python3 -m pytest tests
"""
import socket
import struct
import sys
import threading
import xml.dom.minidom
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rk1speech import announce, config, mdns  # noqa: E402


def _name(n):
    return b"".join(bytes([len(p)]) + p.encode() for p in n.split(".")) + b"\0"


def _rr(name, rtype, rdata, ttl=120):
    return _name(name) + struct.pack(">HHIH", rtype, 0x8001, ttl, len(rdata)) + rdata


class Responder:
    """Answers queries on 127.0.0.1:<port>. `split`: the PTR alone at first."""

    def __init__(self, services, split=False):
        self.services, self.split = services, split
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.asked = []
        threading.Thread(target=self._run, daemon=True).start()

    def _answers(self, qname, qtype):
        out = []
        for s in self.services:
            inst = f"{s['label']}.{s['type']}.local"
            srv = _rr(inst, mdns.SRV, struct.pack(">HHH", 0, 0, s["port"]) + _name(s["host"]))
            txt = _rr(inst, mdns.TXT, b"".join(bytes([len(t)]) + t.encode() for t in s["txt"]))
            a = _rr(s["host"], mdns.A, socket.inet_aton(s["address"]))
            if qtype == mdns.PTR and qname == f"{s['type']}.local":
                out += [_rr(qname, mdns.PTR, _name(inst))] + ([] if self.split else [srv, txt, a])
            elif qname == inst and qtype == mdns.SRV:
                out.append(srv)
            elif qname == inst and qtype == mdns.TXT:
                out.append(txt)
            elif qname == s["host"] and qtype == mdns.A:
                out.append(a)
        return out

    def _run(self):
        while True:
            try:
                data, src = self.sock.recvfrom(9000)
            except OSError:
                return
            qid, _, qd = struct.unpack(">HHH", data[:6])
            off, out = 12, []
            for _ in range(qd):
                qname, off = mdns._read_name(data, off)
                qtype = struct.unpack(">H", data[off:off + 2])[0]
                off += 4
                self.asked.append(qtype)
                out += self._answers(qname, qtype)
            if out:
                self.sock.sendto(struct.pack(">HHHHHH", qid, 0x8400, 0, len(out), 0, 0) + b"".join(out), src)

    def close(self):
        self.sock.close()


def service(kind, board, address, port):
    cfg = dict(config.DEFAULTS, RK1S_BOARD_NAME=board)
    return {"label": f"{board} {announce.LABELS[kind]}", "type": announce.TYPES[kind], "port": port,
            "host": f"{board}.local", "address": address,
            "txt": announce.txt_records(kind, cfg, version="abc123")}


def test_browse_finds_the_announced_servers():
    r = Responder([service("stt", "rk1-1", "10.0.0.11", 8772), service("tts", "rk1-1", "10.0.0.11", 8771),
                   service("stt", "rk1-2", "10.0.0.12", 8772)])
    try:
        found = mdns.browse("_speech-stt._tcp", 0.4, ["127.0.0.1"], iface="", port=r.port)
    finally:
        r.close()
    assert [(s.name, s.address, s.port) for s in found] == [
        ("rk1-1 speech recognition", "10.0.0.11", 8772), ("rk1-2 speech recognition", "10.0.0.12", 8772)]
    assert found[0].host == "rk1-1.local"
    assert found[0].txt["proto"] == "moonshine-tcp/1" and found[0].txt["board"] == "rk1-1"
    assert found[0].txt["version"] == "abc123" and found[0].txt["npu_cores"] == "auto"


def test_browse_asks_for_what_the_first_reply_left_out():
    r = Responder([service("tts", "rk1-3", "10.0.0.13", 8771)], split=True)
    try:
        found = mdns.browse("_speech-tts._tcp", 0.4, ["127.0.0.1"], iface="", port=r.port)
    finally:
        r.close()
    assert [(s.address, s.port, s.txt["capacity"]) for s in found] == [("10.0.0.13", 8771, "3")]
    assert {mdns.PTR, mdns.SRV, mdns.TXT, mdns.A} <= set(r.asked)


def test_browse_with_no_answer_is_empty():
    r = Responder([])
    try:
        assert mdns.browse("_speech-stt._tcp", 0.3, ["127.0.0.1"], iface="", port=r.port) == []
    finally:
        r.close()


def test_query_and_parse_round_trip():
    q = mdns.build_query([("_speech-tts._tcp.local", mdns.PTR)], qid=9)
    qid, is_resp, recs = mdns.parse(q)
    assert qid == 9 and not is_resp and recs == []
    assert mdns.txt_dict([b"Engine=x", b"engine=y", b"flag", b"=bad"]) == {"engine": "x", "flag": ""}


def test_announcement_files_are_valid_avahi_xml():
    cfg = dict(config.DEFAULTS, RK1S_BOARD_NAME="rk1-9")
    for kind in ("stt", "tts"):
        doc = xml.dom.minidom.parseString(announce.render(kind, cfg, version="0123456789ab"))
        svc = doc.getElementsByTagName("service")[0]
        assert svc.getElementsByTagName("type")[0].firstChild.data == announce.TYPES[kind]
        port = svc.getElementsByTagName("port")[0].firstChild.data
        assert port == cfg[f"RK1S_{kind.upper()}_PORT"]
        txt = [t.firstChild.data for t in svc.getElementsByTagName("txt-record")]
        assert txt[:2] == ["txtvers=1", f"proto={announce.PROTOCOLS[kind]}"]
        assert "board=rk1-9" in txt and "health=8770" in txt and "version=0123456789ab" in txt
    tts = announce.txt_records("tts", cfg)
    assert "npu_cores=1,2,0" in tts and "capacity=3" in tts and "path=/tts" in tts
    no_health = announce.txt_records("stt", dict(cfg, RK1S_HEALTH="no"))
    assert not any(t.startswith("health=") for t in no_health)


def test_malformed_records_do_not_break_browsing():
    """An A record of the wrong length, and an SRV pointing at the root name."""
    bad = service("stt", "rk1-5", "10.0.0.15", 8772)
    r = Responder([bad])
    orig = r._answers

    def answers(qname, qtype):
        out = []
        for rec in orig(qname, qtype):
            if rec.startswith(_name("rk1-5.local")) and struct.unpack(">H", rec[13:15])[0] == mdns.A:
                rec = _rr("rk1-5.local", mdns.A, b"\x0a\x00\x3a")          # 3 bytes, one of them ':'
            out.append(rec)
        inst = "rk1-6 x._speech-stt._tcp.local"
        if qtype == mdns.PTR:
            out += [_rr(qname, mdns.PTR, _name(inst)),
                    _rr(inst, mdns.SRV, struct.pack(">HHH", 0, 0, 1) + b"\0")]
        return out
    r._answers = answers
    try:
        found = mdns.browse("_speech-stt._tcp", 0.4, ["127.0.0.1"], iface="", port=r.port)
    finally:
        r.close()
    assert found and all(isinstance(s.address, str) for s in found)
    assert not any(s.name.startswith("rk1-6") for s in found)
