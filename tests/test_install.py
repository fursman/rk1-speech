"""The installer's pieces that decide what is safe (rk1speech/installer.py), the settings
(config.py), the units (units.py) and the health JSON's parsers (health.py). Nothing here
touches the machine: the steps are given stand-ins for what they would look at.

    python3 -m pytest tests
"""
import io
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rk1speech import config, health, installer, units  # noqa: E402

VER = {"KOKORO_BUCKETS": "64 96", "MOONSHINE_RKNN_COMMIT": "a" * 40, "KOKORO_RKNN_COMMIT": "b" * 40}


# --- the NPU graphs: never one with REGTASK errors -----------------------------------

def _graphs(d: Path, names=("dec_T64_A.rknn", "dec_T64_B.rknn", "dec_T96_A.rknn", "dec_T96_B.rknn")):
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_bytes(n.encode())


def _log(d: Path, name, errors=0, compiled=True):
    (d / "logs").mkdir(parents=True, exist_ok=True)
    text = "E REGTASK: The bit width of field value exceeds the limit\n" * errors
    (d / "logs" / name).write_text(text + ("COMPILED x.rknn\n" if compiled else "failed\n"))


def test_graphs_with_clean_logs_are_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "known_good", lambda: {})
    _graphs(tmp_path)
    for n in ("T64_A", "T64_B", "T96_A", "T96_B"):
        _log(tmp_path, f"{n}.log")
    ok, problems = installer.check_buckets(tmp_path, VER)
    assert problems == [] and len(ok) == 4


def test_a_graph_with_regtask_errors_is_refused_even_if_its_hash_is_known(tmp_path, monkeypatch):
    _graphs(tmp_path)
    digest = installer.sha256(tmp_path / "dec_T96_B.rknn")
    monkeypatch.setattr(installer, "known_good", lambda: {digest: "dec_T96_B.rknn"})
    for n in ("T64_A", "T64_B", "T96_A"):
        _log(tmp_path, f"{n}.log")
    _log(tmp_path, "T96_B.log", errors=3)
    ok, problems = installer.check_buckets(tmp_path, VER)
    assert len(ok) == 3 and problems == ["dec_T96_B.rknn: 3 REGTASK errors in its compile log: never run it"]


def test_a_graph_without_a_log_needs_a_known_good_hash(tmp_path, monkeypatch):
    _graphs(tmp_path)
    good = {installer.sha256(tmp_path / n): n for n in ("dec_T64_A.rknn", "dec_T64_B.rknn", "dec_T96_A.rknn")}
    monkeypatch.setattr(installer, "known_good", lambda: good)
    ok, problems = installer.check_buckets(tmp_path, VER)
    assert len(ok) == 3 and "dec_T96_B.rknn: not a known-good build" in problems[0]
    (tmp_path / "dec_T96_A.rknn").unlink()
    ok, problems = installer.check_buckets(tmp_path, VER)
    assert "dec_T96_A.rknn: missing" in problems


def test_a_log_that_never_says_compiled_does_not_count(tmp_path):
    _log(tmp_path, "T64_A.log", compiled=False)
    assert installer.regtask_errors(tmp_path / "logs/T64_A.log") is None
    assert installer.regtask_errors(tmp_path / "logs/none.log") is None


def test_the_known_good_manifest_lists_every_bucket():
    names = sorted(installer.known_good().values())
    want = sorted(installer.bucket_names(config.versions()))
    assert names == want and len(names) == 16


# --- small parsers -----------------------------------------------------------------------

def test_cpu_lists_and_versions():
    assert installer.cpu_list("4-7") == [4, 5, 6, 7]
    assert installer.cpu_list("0,2, 4-5") == [0, 2, 4, 5]
    assert installer.vtuple("0.9.2") == (0, 9, 2) and installer.vtuple("v0.9.10") > installer.vtuple("0.9.2")


def test_config_file_overrides_defaults_and_ignores_unknown_keys(tmp_path):
    f = tmp_path / "rk1-speech.conf"
    f.write_text("# comment\nRK1S_TTS_PORT=9771\nRK1S_TTS_CORES='1,2'\nRK1S_BOARD_NAME=den\nNOPE=1\n")
    cfg = config.load(f)
    assert cfg["RK1S_TTS_PORT"] == "9771" and cfg["RK1S_TTS_CORES"] == "1,2"
    assert cfg["RK1S_BOARD_NAME"] == "den" and "NOPE" not in cfg
    assert cfg["RK1S_STT_PORT"] == "8772"
    again = config.parse_env(config.render_conf(cfg))
    assert {k: again[k] for k in config.DEFAULTS} == {k: cfg[k] for k in config.DEFAULTS}


def test_versions_pin_full_commits_and_the_runtime():
    v = config.versions()
    for key in ("MOONSHINE_RKNN_COMMIT", "KOKORO_RKNN_COMMIT"):
        assert len(v[key]) == 40 and int(v[key], 16) >= 0
    assert len(v["LIBRKNNRT_SHA256"]) == 64 and v["LIBRKNNRT_VERSION"] == "2.5.0"
    assert "/v2.5.0/" in v["LIBRKNNRT_URL"]


def test_units_carry_the_measured_settings():
    cfg = dict(config.DEFAULTS, RK1S_BOARD_NAME="b")
    stt, tts, hl = units.stt(cfg, "speech"), units.tts(cfg, "speech"), units.health(cfg, "speech")
    assert "--port 8772 --npu-core auto" in stt and "CPUAffinity=4-7" in stt and "Nice=-5" in stt
    assert "--port 8771 --cores 1,2,0 --workers 6 --threads 4 --voice af_heart" in tts
    assert "CPUAffinity=4-7" in tts and "User=speech" in tts
    assert "Conflicts=rk1-tts-npu3.service" in tts and "Conflicts=rk1-stt.service" in stt
    assert "AmbientCapabilities=CAP_DAC_READ_SEARCH" in hl and "CPUAffinity=0-3" in hl
    assert "/opt/rk1-speech/current/stt/.venv/bin/python -m moonshine_rknn.server" in stt


def test_health_parses_the_npu_load_and_the_runtime_version(tmp_path):
    f = tmp_path / "load"
    f.write_text("NPU load:  Core0: 12%, Core1:  0%, Core2: 87%,\n")
    assert health.npu_load(str(f)) == ([12, 0, 87], None)
    loads, why = health.npu_load(str(tmp_path / "missing"))
    assert loads is None and why
    lib = tmp_path / "librknnrt.so"
    lib.write_bytes(b"\0junk\0librknnrt version: 2.5.0 (74ea630ce8@2026-09-30T09:41:17)\0more")
    assert health.librknnrt_version(str(lib)) == "2.5.0 (74ea630ce8@2026-09-30T09:41:17)"
    assert health.librknnrt_version(str(tmp_path / "none.so")) is None


# --- the steps -------------------------------------------------------------------------------

def _ctx(tmp_path, **args):
    a = types.SimpleNamespace(dry_run=True, takeover=False, kokoro_buckets=None, moonshine_models=None,
                              replace_runtime=False, no_start=False, rollback=False, uninstall=False, prune=False)
    for k, v in args.items():
        setattr(a, k, v)
    cfg = dict(config.DEFAULTS, RK1S_BOARD_NAME="b", RK1S_PREFIX=str(tmp_path / "opt"))
    return installer.Ctx(a, cfg, config.versions(), out=io.StringIO())


def test_hand_installed_servers_are_only_replaced_with_takeover(tmp_path, monkeypatch):
    active = {"rk1-stt.service", "rk1-tts-npu3.service"}
    monkeypatch.setattr(installer, "unit_info", lambda ctx, u: {
        "ActiveState": "active" if u in active else "inactive",
        "UnitFileState": "enabled" if u in active else "disabled"})
    monkeypatch.setattr(installer, "port_listening", lambda port: True)
    st = installer.step_ports(_ctx(tmp_path))
    assert st.state == "fail" and "--takeover" in st.detail
    ctx = _ctx(tmp_path, takeover=True)
    st = installer.step_ports(ctx)
    assert st.state == "todo" and sorted(ctx.taken_over) == sorted(active)


def test_a_port_held_by_something_unknown_is_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "unit_info", lambda ctx, u: {"ActiveState": "inactive",
                                                                 "UnitFileState": "disabled"})
    monkeypatch.setattr(installer, "port_listening", lambda port: int(port) == 8771)
    st = installer.step_ports(_ctx(tmp_path, takeover=True))
    assert st.state == "fail" and "port 8771 (tts) is in use" in st.detail


def test_the_npu_address_space_check_counts_other_processes(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.health, "npu_card", lambda: "/dev/dri/card1")
    monkeypatch.setattr(installer.health, "npu_processes", lambda card: [
        {"pid": 1, "comm": "python", "mb": 2500, "cmd": "npu2_server.py"}])
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(ctx, "look", lambda argv, timeout=60: (0, "0"))
    st = installer.step_npu_space(ctx)
    assert st.state == "fail" and "npu2_server.py" in st.detail and "dma map" in st.detail
    monkeypatch.setattr(installer.health, "npu_processes", lambda card: [])
    assert installer.step_npu_space(ctx).state == "ok"


def test_a_dry_run_changes_nothing(tmp_path):
    ran = []
    ctx = _ctx(tmp_path)

    def step(ctx):
        return installer.Step("thing", "todo", "would do it", [installer.Action("touch x", lambda: ran.append(1))])
    assert installer.run_steps(ctx, [step]) == 0
    assert ran == [] and "touch x" in ctx.out.getvalue() and "nothing was changed" in ctx.out.getvalue()
    ctx.dry = False
    assert installer.run_steps(ctx, [step]) == 0 and ran == [1]


def test_applying_stops_at_the_first_failed_check(tmp_path):
    ran = []
    ctx = _ctx(tmp_path, dry_run=False)
    steps = [lambda c: installer.Step("check", "fail", "no"),
             lambda c: installer.Step("later", "todo", "", [installer.Action("x", lambda: ran.append(1))])]
    assert installer.run_steps(ctx, steps) == 2 and ran == []


def test_the_service_user_is_never_root(tmp_path):
    a = types.SimpleNamespace(dry_run=True)
    with pytest.raises(installer.Fail):
        installer.Ctx(a, dict(config.DEFAULTS, RK1S_USER="root"), config.versions())


def test_a_new_deploy_only_when_something_changed(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    st = installer.step_deploy(ctx)
    assert st.state == "todo" and "new deploy" in st.detail
    plan = installer.deploy_plan(ctx)
    cur = tmp_path / "opt" / "deploys" / "d1"
    cur.mkdir(parents=True)
    (cur / "manifest.json").write_text('{"id": "d1", "digest": "%s"}' % plan["digest"])
    (tmp_path / "opt" / "current").symlink_to(cur)
    assert installer.step_deploy(ctx).state == "ok"
    ctx.cfg["RK1S_TTS_CORES"] = "1,2"
    assert installer.step_deploy(ctx).state == "todo", "a setting changed"


def test_changing_the_buckets_makes_a_new_model_directory(tmp_path):
    ctx = _ctx(tmp_path)
    before = ctx.model_dir("kokoro-rknn")
    ctx.ver = dict(ctx.ver, KOKORO_BUCKETS="64 96 128")
    assert ctx.model_dir("kokoro-rknn") != before
    assert ctx.model_dir("moonshine-rknn").name == "moonshine-rknn-" + ctx.ver["MOONSHINE_RKNN_COMMIT"][:12]


def test_a_new_release_alone_does_not_rebuild_an_engine_environment(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    d, stamp = ctx.engine_dir("kokoro-rknn"), None
    stamp = installer.venv_stamp(ctx, d, ["requirements.txt"], installer.SRC / "constraints" / "tts.txt")
    monkeypatch.setattr(installer, "__version__", "9.9.9")
    assert ctx.engine_dir("kokoro-rknn") == d
    assert installer.venv_stamp(ctx, d, ["requirements.txt"], installer.SRC / "constraints" / "tts.txt") == stamp


def test_servers_are_restarted_until_the_current_deploy_has_been_started(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(installer, "unit_info", lambda c, u: {"ActiveState": "active", "UnitFileState": "enabled"})
    monkeypatch.setattr(installer, "wait_healthy", lambda c, timeout=0: (True, {}))
    opt = tmp_path / "opt"
    (opt / "deploys" / "d2").mkdir(parents=True)
    (opt / "current").symlink_to(opt / "deploys" / "d2")
    (opt / "started").write_text("d1")          # a run that stopped before restarting them
    assert installer.step_start(ctx).state == "todo"
    (opt / "started").write_text("d2")
    assert installer.step_start(ctx).state == "ok"


def test_a_planned_deploy_shows_the_restart_in_a_dry_run(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(installer, "unit_info", lambda c, u: {"ActiveState": "active", "UnitFileState": "enabled"})
    assert installer.step_deploy(ctx).state == "todo"
    assert installer.step_start(ctx).state == "todo"


def test_a_deploy_keeps_its_units_and_announcements(tmp_path):
    shown = [a.shown for a in installer.step_deploy(_ctx(tmp_path)).actions]
    assert any("/units/rk1-speech-tts.service" in s for s in shown)
    assert any("/avahi/rk1-speech-stt.service" in s for s in shown)


def test_new_announcements_are_read_with_a_reload(tmp_path, monkeypatch):
    """`install` creates a file before setting its mode, and avahi, watching the
    directory, once caught the synthesis file unreadable and never announced it."""
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(installer, "AVAHI_DIR", tmp_path / "avahi")
    monkeypatch.setattr(ctx, "look", lambda argv, timeout=60: (0, "active"))
    shown = [a.shown for a in installer.step_announce(ctx).actions]
    assert any("rk1-speech-tts.service" in s for s in shown)
    assert shown[-1].endswith("systemctl reload-or-restart avahi-daemon")
