"""Look after every speech board on the LAN from one machine, over SSH, one board at a time.

    rk1-speech-fleet discover                 the boards: mDNS, plus the static list
    rk1-speech-fleet status [--json]          both servers on every board, NPU, temperatures, versions
    rk1-speech-fleet update [--dry-run]       install this checkout on each board in turn
    rk1-speech-fleet rollback [--dry-run]     each board back to its previous deploy, in turn
    rk1-speech-fleet restart [stt|tts|all]    restart the servers, in turn

Rolling commands never take a board down unless at least --min-up other boards (default 1)
answer for each kind of server it would interrupt, and stop at the first board that does
not come back healthy: the rest of the fleet keeps serving, untouched.

Boards come from mDNS (_speech-stt._tcp, _speech-tts._tcp; see docs/PROTOCOL.md) and from
the static list in ~/.config/rk1-speech/boards (or --boards FILE), one board per line:

    # name   [user@]address   [stt port]  [tts port]
    rk1-1    ubuntu@10.0.0.11
    rk1-2    10.0.0.12        8772        8771

The static list covers boards that do not announce themselves yet. SSH must work without
a password (keys), and sudo on the boards without one, as for the ad-hoc rollouts this replaces.
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple, Optional

from . import mdns, probe
from .units import UNITS

SRC = Path(__file__).resolve().parent.parent
BOARDS_FILE = Path(os.path.expanduser("~/.config/rk1-speech/boards"))
PREFIX = "/opt/rk1-speech"
TYPES = {"stt": "_speech-stt._tcp", "tts": "_speech-tts._tcp"}
DEFAULT_PORTS = {"stt": 8772, "tts": 8771}
READY_TIMEOUT = 300.0


class Board(NamedTuple):
    name: str
    address: str
    ssh: str                    # user@address
    ports: dict                 # {"stt": port, "tts": port} for the servers it has
    source: str                 # "mdns", "static" or "mdns+static"
    txt: dict                   # {"stt": {...}, "tts": {...}} from the announcements
    health_port: Optional[int]  # rk1-speech-health's HTTP port, when announced


def resolve(host: str) -> str:
    """An IPv4 address for `host`: itself when it is one, else DNS, else (for .local names)
    an mDNS query; the name itself when nothing answers."""
    try:
        socket.inet_aton(host)
        if host.count(".") == 3:
            return host
    except OSError:
        pass
    try:
        return socket.gethostbyname(host)
    except OSError:
        pass
    if host.endswith(".local"):
        try:
            for r in mdns._exchange([(host, mdns.A)], 0.5, iface=mdns._default_interface_address()):
                if r.type == mdns.A and isinstance(r.data, str) and r.name.lower() == host.lower():
                    return r.data
        except (OSError, ValueError):
            pass
    return host


def read_boards_file(path: Path, ssh_user: str) -> list:
    boards = []
    try:
        text = path.read_text()
    except OSError:
        return boards
    for line in text.splitlines():
        words = line.split("#", 1)[0].split()
        if len(words) < 2:
            continue
        name, target = words[0], words[1]
        user, _, address = target.rpartition("@")
        ports = dict(DEFAULT_PORTS)
        if len(words) > 2:
            ports["stt"] = int(words[2])
        if len(words) > 3:
            ports["tts"] = int(words[3])
        boards.append(Board(name, resolve(address), f"{user or ssh_user}@{address}", ports, "static", {}, None))
    return boards


def from_mdns(services: dict, ssh_user: str) -> list:
    """Boards from browse results {kind: [mdns.Service]}: one per announcing host (the SRV
    target), so a board with two addresses is still one board."""
    by_host = {}
    for kind, found in services.items():
        for s in found:
            if not s.address:
                continue
            b = by_host.setdefault(s.host.lower(), {"name": "", "address": s.address, "ports": {},
                                                    "txt": {}, "health": None})
            b["ports"][kind] = s.port
            b["txt"][kind] = s.txt
            b["name"] = b["name"] or s.txt.get("board") or s.host.split(".")[0]
            if s.txt.get("health", "").isdigit():
                b["health"] = int(s.txt["health"])
    return [Board(b["name"], b["address"], f"{ssh_user}@{b['address']}", b["ports"], "mdns", b["txt"], b["health"])
            for b in by_host.values()]


def merge(found: list, static: list) -> list:
    """mDNS boards, plus static ones they do not cover, matched by address or by name; a
    static entry names the board and its SSH login, mDNS says what it serves. Sorted by name."""
    out = {b.address: b for b in found}
    names = {b.name: b.address for b in found}
    for s in static:
        key = s.address if s.address in out else names.get(s.name)
        if key is None:
            out[s.address] = s
        else:
            out[key] = out[key]._replace(name=s.name, ssh=s.ssh, source="mdns+static")
    return sorted(out.values(), key=lambda b: (b.name, b.address))


def discover(args) -> list:
    static = read_boards_file(Path(args.boards), args.ssh_user) if args.boards else []
    for spec in args.board or []:
        name, _, target = spec.partition("=")
        if not target:
            name, target = spec, spec
        user, _, address = target.rpartition("@")
        static.append(Board(name, address, f"{user or args.ssh_user}@{address}", dict(DEFAULT_PORTS),
                            "static", {}, None))
    found = []
    if not args.no_mdns:
        with ThreadPoolExecutor(len(TYPES)) as ex:
            browsed = dict(zip(TYPES, ex.map(lambda t: mdns.browse(t, args.mdns_timeout), TYPES.values())))
        found = from_mdns(browsed, args.ssh_user)
    return merge(found, static)


def selected(args, boards) -> list:
    """The boards to act on (--only); every board still counts as cover."""
    if not getattr(args, "only", None):
        return boards
    keep = set(args.only.split(","))
    return [b for b in boards if b.name in keep or b.address in keep]


# -- talking to boards ----------------------------------------------------------

def ssh(board: Board, command: str, timeout: float = 900, stdin=None, capture=True):
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", board.ssh, command]
    return subprocess.run(argv, input=stdin, capture_output=capture, timeout=timeout,
                          text=not isinstance(stdin, bytes))


def probes(board: Board, timeout: float = 2.0) -> dict:
    return {k: probe.PROBES[k](board.address, port, timeout) for k, port in board.ports.items()}


def board_health(board: Board, use_ssh: bool) -> Optional[dict]:
    """rk1-speech-health's JSON: over HTTP when announced, else over SSH (sudo -n, for the NPU
    load), else None (rk1-speech is not installed there)."""
    if board.health_port:
        try:
            with urllib.request.urlopen(f"http://{board.address}:{board.health_port}/health", timeout=4) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read())
            except ValueError:
                pass
        except (OSError, ValueError):
            pass
    if not use_ssh:
        return None
    try:
        r = ssh(board, "sudo -n rk1-speech-health --compact 2>/dev/null || rk1-speech-health --compact",
                timeout=30)
        return json.loads(r.stdout) if r.stdout.strip() else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def wait_ready(board: Board, kinds, timeout: float = READY_TIMEOUT, every: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    res = {}
    while True:
        res = {k: probe.PROBES[k](board.address, board.ports[k], 2.0) for k in kinds if k in board.ports}
        if all(r["ok"] for r in res.values()) or time.monotonic() > deadline:
            return res
        time.sleep(every)


def others_up(boards, board: Board, kinds) -> dict:
    """{kind: number of OTHER boards answering for it}."""
    up = {k: 0 for k in kinds}
    for b in boards:
        if b.address == board.address:
            continue
        for k, r in probes(b).items():
            if k in up and r["ok"]:
                up[k] += 1
    return up


# -- commands -------------------------------------------------------------------

def cmd_discover(args, boards, out=sys.stdout) -> int:
    if args.json:
        print(json.dumps([b._asdict() for b in boards], indent=2), file=out)
        return 0
    if not boards:
        print("no boards found (none announcing, and no static list)", file=out)
        return 1
    for b in boards:
        serves = ", ".join(f"{k} :{p}" for k, p in sorted(b.ports.items()))
        extra = "  ".join(f"{k} {b.txt[k].get('engine', '?')} {b.txt[k].get('version', '?')}"
                          for k in ("stt", "tts") if k in b.txt)
        print(f"{b.name:<10} {b.address:<15} {serves:<20} {b.source:<12} {extra}".rstrip(), file=out)
    return 0


def _fmt_probe(r):
    if r is None:
        return "-"
    return f"ok {r['ms']:.0f} ms" if r["ok"] else "DOWN"


def cmd_status(args, boards, out=sys.stdout) -> int:
    rows, bad = [], 0
    for b in boards:
        p = probes(b)
        h = board_health(b, not args.no_ssh)
        rows.append({"board": b._asdict(), "probes": p, "health": h})
        bad += not all(r["ok"] for r in p.values()) or not p
    if args.json:
        print(json.dumps(rows, indent=2), file=out)
        return 1 if bad else 0
    print(f"{'board':<10} {'address':<15} {'stt':<10} {'tts':<10} {'tts lanes':<9} {'npu load %':<11} "
          f"{'soc C':<6} {'deploy':<24} versions", file=out)
    for row in rows:
        b, p, h = Board(**row["board"]), row["probes"], row["health"] or {}
        tts = (p.get("tts") or {}).get("reply") or {}
        lanes = f"{tts.get('lanes_busy', '?')}/{tts.get('lanes', '?')}" if tts else "-"
        npu = h.get("npu", {}).get("load_percent")
        temp = h.get("temperatures_c", {}).get("soc")
        v = h.get("versions", {})
        vers = (f"moonshine {v.get('moonshine_rknn') or '?'} kokoro {v.get('kokoro_rknn') or '?'} "
                f"rknpu {v.get('rknpu_driver') or '?'} rt {(v.get('librknnrt') or '?').split()[0]}"
                if h else "(rk1-speech not installed, or no health)")
        print(f"{b.name:<10} {b.address:<15} {_fmt_probe(p.get('stt')):<10} {_fmt_probe(p.get('tts')):<10} "
              f"{lanes:<9} {' '.join(map(str, npu)) if npu else '-':<11} "
              f"{temp if temp is not None else '-'!s:<6} {v.get('deploy') or '-':<24} {vers}", file=out)
    return 1 if bad else 0


def rolling(args, boards, kinds, action, describe, out=sys.stdout, all_kinds=False) -> int:
    """For each selected board in turn: check the others cover for it, act, wait until it
    answers. `action(board)` returns (ok, message); `describe(board)` what it would do.
    `all_kinds`: the action touches every kind of server whatever the board was seen to
    announce (update, rollback): a lost announcement must not skip the cover check."""
    targets = selected(args, boards)
    if not targets:
        print("no boards", file=out)
        return 1
    for i, b in enumerate(targets, 1):
        if all_kinds:
            b = b._replace(ports=dict({k: DEFAULT_PORTS[k] for k in kinds}, **b.ports))
        affected = [k for k in kinds if k in b.ports]
        up = others_up(boards, b, affected)
        short = [k for k in affected if up[k] < args.min_up]
        head = f"[{i}/{len(targets)}] {b.name} ({b.address})"
        cover = ", ".join(f"{up[k]} other {k}" for k in affected) or "nothing it serves"
        if short:
            print(f"{head}: STOP. Only {cover} up, fewer than --min-up {args.min_up}: taking this "
                  f"board down would leave no {'/'.join(short)} on the LAN. Nothing done to it or "
                  f"to the boards after it.", file=out)
            return 2
        if args.dry_run:
            print(f"{head}: {cover} up, so it may go down. Would run:\n    {describe(b)}\n"
                  f"    then wait up to {READY_TIMEOUT:.0f} s for {', '.join(affected) or 'its servers'} "
                  f"to answer before the next board", file=out)
            continue
        print(f"{head}: {cover} up; {describe(b)}", file=out, flush=True)
        ok, msg = action(b)
        if not ok:
            print(f"{head}: FAILED: {msg}\nStopped; the boards after it were not touched.", file=out)
            return 2
        res = wait_ready(b, affected)
        if not all(r["ok"] for r in res.values()):
            why = "; ".join(f"{k}: {r['error']}" for k, r in res.items() if not r["ok"])
            print(f"{head}: did not come back ({why}). Stopped; the boards after it were not touched.",
                  file=out)
            return 2
        print(f"{head}: healthy ({', '.join(f'{k} {r['ms']:.0f} ms' for k, r in res.items())})",
              file=out, flush=True)
    return 0


def _units(args) -> dict:
    u = {"stt": UNITS["stt"], "tts": UNITS["tts"]}
    for item in (args.units or "").split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            u[k.strip()] = v.strip()
    return u


def cmd_restart(args, boards, out=sys.stdout) -> int:
    kinds = ["stt", "tts"] if args.which == "all" else [args.which]
    u = _units(args)
    command = "sudo systemctl restart " + " ".join(shlex.quote(u[k]) for k in kinds)

    def act(b):
        r = ssh(b, command, timeout=120)
        return r.returncode == 0, (r.stderr or r.stdout).strip()[-400:]
    return rolling(args, boards, kinds, act, lambda b: f"ssh {b.ssh} {command!r}", out)


def _install_args(args) -> str:
    extra = []
    if args.takeover:
        extra.append("--takeover")
    if args.kokoro_buckets:
        extra += ["--kokoro-buckets", args.kokoro_buckets]
    if args.moonshine_models:
        extra += ["--moonshine-models", args.moonshine_models]
    return " ".join(shlex.quote(x) for x in extra)


def upload_command(prefix: str) -> str:
    """The remote half of the upload: unpack into src.new, then swap it for src. As root
    (read-only for everyone else), so the prefix's owner, the service user, never changes."""
    p = shlex.quote(prefix)
    return (f"set -e; sudo mkdir -p {p}; sudo rm -rf {p}/src.new; sudo mkdir -p {p}/src.new; "
            f"sudo tar --no-same-owner -xzf - -C {p}/src.new; sudo chmod -R a+rX {p}/src.new; "
            f"sudo rm -rf {p}/src.old; if [ -d {p}/src ]; then sudo mv {p}/src {p}/src.old; fi; "
            f"sudo mv {p}/src.new {p}/src")


def source_tarball(source: Path) -> bytes:
    files = ["install.py", "versions.env", "LICENSE", "NOTICE", "rk1speech", "constraints", "known-good"]
    have = [f for f in files if (source / f).exists()]
    r = subprocess.run(["tar", "-czf", "-", "--exclude=__pycache__", "-C", str(source), *have],
                       capture_output=True, check=True)
    return r.stdout


def cmd_update(args, boards, out=sys.stdout) -> int:
    source = Path(args.source).resolve() if args.source else SRC
    if not (source / "install.py").is_file():
        print(f"{source} is not an rk1-speech checkout (no install.py)", file=out)
        return 2
    prefix = args.prefix
    run_install = f"python3 {shlex.quote(prefix)}/src/install.py {_install_args(args)}".strip()
    tarball = None if args.dry_run else source_tarball(source)

    def act(b):
        r = ssh(b, upload_command(prefix), timeout=120, stdin=tarball)
        if r.returncode != 0:
            return False, f"upload failed: {(r.stderr or b'').decode(errors='replace')[-300:]}"
        r = ssh(b, run_install, timeout=3600, capture=False)
        return r.returncode == 0, f"install.py exited {r.returncode} (it rolls back by itself when it can)"
    return rolling(args, boards, ["stt", "tts"], act,
                   lambda b: f"upload {source} to {b.ssh}:{prefix}/src, then: {run_install}", out, all_kinds=True)


def cmd_rollback(args, boards, out=sys.stdout) -> int:
    command = f"python3 {shlex.quote(args.prefix)}/current/install.py --rollback"

    def act(b):
        r = ssh(b, command, timeout=900, capture=False)
        return r.returncode == 0, f"rollback exited {r.returncode}"
    return rolling(args, boards, ["stt", "tts"], act, lambda b: f"ssh {b.ssh} {command!r}", out, all_kinds=True)


def main(argv=None, out=sys.stdout) -> int:
    ap = argparse.ArgumentParser(prog="rk1-speech-fleet", description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--boards", default=str(BOARDS_FILE), help="static list of boards (default %(default)s)")
    ap.add_argument("--board", action="append", metavar="NAME=[USER@]ADDRESS", help="add a board")
    ap.add_argument("--only", metavar="NAME,...", help="just these boards (names or addresses)")
    ap.add_argument("--no-mdns", action="store_true", help="use the static list alone")
    ap.add_argument("--mdns-timeout", type=float, default=1.5)
    ap.add_argument("--ssh-user", default="ubuntu")
    ap.add_argument("--prefix", default=PREFIX, help="RK1S_PREFIX on the boards")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("discover", help="list the boards")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("status", help="health of every board")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-ssh", action="store_true", help="only what the servers say over the network")
    for name, help_ in (("update", "install the source on each board in turn"),
                        ("rollback", "each board back to its previous deploy, in turn"),
                        ("restart", "restart the servers, one board at a time")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--dry-run", action="store_true", help="check and say what would happen; do nothing")
        p.add_argument("--min-up", type=int, default=1,
                       help="other boards that must answer for each server a board stops (default 1)")
        if name == "restart":
            p.add_argument("which", nargs="?", default="all", choices=["stt", "tts", "all"])
            p.add_argument("--units", metavar="stt=UNIT,tts=UNIT",
                           help="unit names, for servers installed by hand (e.g. stt=rk1-stt,tts=rk1-tts-npu3)")
        if name == "update":
            p.add_argument("--source", metavar="DIR", help="rk1-speech checkout to install (default: this one)")
            p.add_argument("--takeover", action="store_true", help="passed to install.py")
            p.add_argument("--kokoro-buckets", metavar="DIR", help="passed to install.py (a path on the board)")
            p.add_argument("--moonshine-models", metavar="DIR", help="passed to install.py (a path on the board)")
    args = ap.parse_args(argv)
    boards = discover(args)
    if args.cmd in ("discover", "status"):
        boards = selected(args, boards)
    return {"discover": cmd_discover, "status": cmd_status, "update": cmd_update,
            "rollback": cmd_rollback, "restart": cmd_restart}[args.cmd](args, boards, out)


if __name__ == "__main__":
    sys.exit(main())
