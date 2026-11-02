"""The fleet tool (rk1speech/fleet.py): which boards there are, and rolling changes that
never take the last server of a kind off the LAN. The boards are stand-ins.

    python3 -m pytest tests
"""
import io
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rk1speech import fleet, mdns  # noqa: E402


def svc(kind, board, address, port, **txt):
    return mdns.Service(f"{board} x.{fleet.TYPES[kind]}.local", fleet.TYPES[kind], f"{board} x",
                        f"{board}.local", address, port, dict(board=board, **txt))


def test_announced_and_listed_boards_are_merged_by_address(tmp_path):
    f = tmp_path / "boards"
    f.write_text("# name address\nden admin@10.0.0.11\nattic 10.0.0.14 9772 9771\n\n")
    static = fleet.read_boards_file(f, "ubuntu")
    found = fleet.from_mdns({"stt": [svc("stt", "rk1-1", "10.0.0.11", 8772)],
                             "tts": [svc("tts", "rk1-1", "10.0.0.11", 8771, health="8770"),
                                     svc("tts", "rk1-2", "10.0.0.12", 8771)]}, "ubuntu")
    boards = fleet.merge(found, static)
    by = {b.address: b for b in boards}
    assert [b.name for b in boards] == ["attic", "den", "rk1-2"]
    assert by["10.0.0.11"].ssh == "admin@10.0.0.11" and by["10.0.0.11"].source == "mdns+static"
    assert by["10.0.0.11"].ports == {"stt": 8772, "tts": 8771} and by["10.0.0.11"].health_port == 8770
    assert by["10.0.0.12"].ports == {"tts": 8771} and by["10.0.0.12"].ssh == "ubuntu@10.0.0.12"
    assert by["10.0.0.14"].ports == {"stt": 9772, "tts": 9771} and by["10.0.0.14"].source == "static"


def _boards(n):
    return [fleet.Board(f"b{i}", f"10.0.0.{i}", f"u@10.0.0.{i}", {"stt": 8772, "tts": 8771}, "static", {}, None)
            for i in range(1, n + 1)]


def _args(**kw):
    a = types.SimpleNamespace(dry_run=False, min_up=1, only=None)
    a.__dict__.update(kw)
    return a


def _fake_lan(monkeypatch, down=()):
    """Every server answers, except those in `down` ({(address, kind)})."""
    def probes(board, timeout=2.0):
        return {k: {"ok": (board.address, k) not in down, "ms": 1.0, "error": None, "reply": {}}
                for k in board.ports}
    monkeypatch.setattr(fleet, "probes", probes)
    monkeypatch.setattr(fleet, "wait_ready", lambda b, kinds, **k: {
        kk: {"ok": (b.address, kk) not in down, "ms": 1.0, "error": "down"} for kk in kinds})


def test_one_board_at_a_time_in_order(monkeypatch):
    _fake_lan(monkeypatch)
    did = []
    out = io.StringIO()
    rc = fleet.rolling(_args(), _boards(3), ["stt", "tts"], lambda b: (did.append(b.name), (True, ""))[1],
                       lambda b: f"restart {b.name}", out)
    assert rc == 0 and did == ["b1", "b2", "b3"]
    assert out.getvalue().count("healthy") == 3


def test_never_the_last_board_of_a_kind(monkeypatch):
    # b2's synthesiser is down: taking b1 down would leave b3 alone, fine with --min-up 1,
    # not with 2.
    _fake_lan(monkeypatch, down={("10.0.0.2", "tts")})
    did = []
    out = io.StringIO()
    rc = fleet.rolling(_args(min_up=2), _boards(3), ["stt", "tts"], lambda b: (did.append(b.name), (True, ""))[1],
                       lambda b: "x", out)
    assert rc == 2 and did == [] and "STOP" in out.getvalue() and "no tts" in out.getvalue()
    did.clear()
    assert fleet.rolling(_args(min_up=1), _boards(2), ["stt"], lambda b: (did.append(b.name), (True, ""))[1],
                         lambda b: "x", io.StringIO()) == 0


def test_a_single_board_is_not_taken_down_without_min_up_0(monkeypatch):
    _fake_lan(monkeypatch)
    did = []
    act = lambda b: (did.append(b.name), (True, ""))[1]  # noqa: E731
    assert fleet.rolling(_args(), _boards(1), ["stt", "tts"], act, lambda b: "x", io.StringIO()) == 2
    assert fleet.rolling(_args(min_up=0), _boards(1), ["stt", "tts"], act, lambda b: "x", io.StringIO()) == 0
    assert did == ["b1"]


def test_a_board_that_does_not_come_back_stops_the_rollout(monkeypatch):
    _fake_lan(monkeypatch)
    did = []

    def wait_ready(b, kinds, **k):
        return {kk: {"ok": b.name != "b2", "ms": 1.0, "error": "refused"} for kk in kinds}
    monkeypatch.setattr(fleet, "wait_ready", wait_ready)
    out = io.StringIO()
    rc = fleet.rolling(_args(), _boards(4), ["stt", "tts"], lambda b: (did.append(b.name), (True, ""))[1],
                       lambda b: "x", out)
    assert rc == 2 and did == ["b1", "b2"] and "did not come back" in out.getvalue()


def test_a_failed_action_stops_the_rollout(monkeypatch):
    _fake_lan(monkeypatch)
    did = []
    rc = fleet.rolling(_args(), _boards(3), ["tts"], lambda b: (did.append(b.name), (b.name != "b1", "boom"))[1],
                       lambda b: "x", io.StringIO())
    assert rc == 2 and did == ["b1"]


def test_a_dry_run_only_looks(monkeypatch):
    _fake_lan(monkeypatch)
    did = []
    out = io.StringIO()
    assert fleet.rolling(_args(dry_run=True), _boards(2), ["stt", "tts"], lambda b: did.append(b) or (True, ""),
                         lambda b: f"would restart {b.name}", out) == 0
    assert did == [] and "would restart b1" in out.getvalue() and "would restart b2" in out.getvalue()


def test_only_picks_the_boards_to_change_but_all_of_them_cover(monkeypatch):
    _fake_lan(monkeypatch)
    did = []
    rc = fleet.rolling(_args(only="b3"), _boards(3), ["stt", "tts"], lambda b: (did.append(b.name), (True, ""))[1],
                       lambda b: "x", io.StringIO())
    assert rc == 0 and did == ["b3"]


def test_the_upload_replaces_the_source_whole_without_touching_the_prefix_owner():
    cmd = fleet.upload_command("/opt/rk1-speech")
    assert "sudo tar --no-same-owner -xzf - -C /opt/rk1-speech/src.new" in cmd
    assert cmd.index("src.new") < cmd.index("sudo mv /opt/rk1-speech/src.new /opt/rk1-speech/src")
    assert "chown" not in cmd and "install -d -o" not in cmd
    tarball = fleet.source_tarball(fleet.SRC)
    assert tarball[:2] == b"\x1f\x8b"


def test_one_board_with_two_addresses_is_one_board():
    a = svc("stt", "rk1-1", "10.0.0.11", 8772)
    b = svc("tts", "rk1-1", "10.0.0.99", 8771)       # the same host, answering from another address
    boards = fleet.from_mdns({"stt": [a], "tts": [b]}, "ubuntu")
    assert len(boards) == 1 and boards[0].ports == {"stt": 8772, "tts": 8771}


def test_a_listed_board_named_by_host_name_is_not_counted_twice(monkeypatch):
    monkeypatch.setattr(fleet, "resolve", lambda host: host)       # rk1-1.local does not resolve here
    static = [fleet.Board("rk1-1", "rk1-1.local", "ubuntu@rk1-1.local", dict(fleet.DEFAULT_PORTS),
                          "static", {}, None)]
    found = fleet.from_mdns({"tts": [svc("tts", "rk1-1", "10.0.0.11", 8771)]}, "ubuntu")
    boards = fleet.merge(found, static)
    assert len(boards) == 1 and boards[0].ssh == "ubuntu@rk1-1.local" and boards[0].address == "10.0.0.11"


def test_an_update_checks_cover_for_both_servers_even_if_one_was_not_heard(monkeypatch):
    # b1's tts announcement was lost; b2 has no tts at all: updating b1 would leave none.
    boards = [fleet.Board("b1", "10.0.0.1", "u@10.0.0.1", {"stt": 8772}, "mdns", {}, None),
              fleet.Board("b2", "10.0.0.2", "u@10.0.0.2", {"stt": 8772}, "mdns", {}, None)]
    _fake_lan(monkeypatch)
    did = []
    act = lambda b: (did.append(b.name), (True, ""))[1]  # noqa: E731
    assert fleet.rolling(_args(), boards, ["stt", "tts"], act, lambda b: "x", io.StringIO(), all_kinds=True) == 2
    assert did == []
