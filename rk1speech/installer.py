"""Set up, update, roll back or remove rk1-speech on one RK3588 board.

    ./install.py --dry-run          what it would change, changing nothing
    ./install.py                    do it (as the service user, with sudo)
    ./install.py --rollback         back to the previous deploy
    ./install.py --uninstall        stop and remove the units and announcements (not /opt)

Every step looks before it acts, so running it again changes nothing that is already
right. The steps, in order, are in STEPS at the bottom; docs/INSTALL.md describes them.

Layout under RK1S_PREFIX (/opt/rk1-speech):

    engines/moonshine-rknn-<commit>/   the engine at its pinned commit, with its .venv
    engines/kokoro-rknn-<commit>/      the same, plus kokoro_rknn/libinterleave.so
    models/moonshine-rknn-<commit>/    scripts/fetch_models.py's files, checksummed
    models/kokoro-rknn-<commit>/       fetched, cut (build/extract.py) and compiled graphs
    build/kokoro-rknn-<commit>/        build environments (deletable once models exist)
    deploys/<id>/                      one release: symlinks to the above, manifest.json,
                                       this package and bin/rk1-speech-health
    current -> deploys/<id>            what the units run
    previous -> deploys/<id>           what --rollback goes back to
"""
from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import platform
import pwd
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import __version__, announce, config, health, probe, units

SRC = Path(__file__).resolve().parent.parent          # the rk1-speech checkout running this
AVAHI_DIR = Path("/etc/avahi/services")
UNIT_DIR = Path("/etc/systemd/system")
BIN_LINK = Path("/usr/local/bin/rk1-speech-health")
# What the two servers map of the NPU's ~4 GB address space (measured on an RK1:
# /proc/<pid>/maps of the NPU's DRM node), and how much of it to allow in all.
NPU_MB = {"stt": 200, "tts": 2150}
NPU_BUDGET_MB = 3600
MOONSHINE_FILES_MB = 460
KOKORO_FETCH_MB = 360
KOKORO_RUNTIME_MB = 1700      # buckets (~1.2 GB) + front/har/tail + the reference model
KOKORO_BUILD_MB = 4500        # toolkit venv (torch) + intermediate ONNX graphs
ENGINES_MB = 1500             # checkouts and venvs
HEALTH_TIMEOUT = 300.0
ENGINE_CONSTRAINTS = {"moonshine-rknn": "stt.txt", "kokoro-rknn": "tts.txt"}


class Fail(Exception):
    pass


class Step:
    def __init__(self, title, state="ok", detail="", actions=()):
        self.title, self.state, self.detail, self.actions = title, state, detail, list(actions)


class Action:
    """One change. `shown` is what --dry-run prints; `fn` does it."""

    def __init__(self, shown: str, fn):
        self.shown, self.fn = shown, fn


class Ctx:
    def __init__(self, args, cfg, ver, out=sys.stdout):
        self.args, self.cfg, self.ver, self.out = args, cfg, ver, out
        self.dry = args.dry_run
        self.euid = os.geteuid()
        self.user = cfg["RK1S_USER"] or os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name
        if self.user == "root":
            raise Fail("the services must not run as root: set RK1S_USER or run install.py as that user")
        try:
            pw = pwd.getpwnam(self.user)
        except KeyError:
            raise Fail(f"no such user {self.user!r} (RK1S_USER)")
        self.home, self.group = Path(pw.pw_dir), grp.getgrgid(pw.pw_gid).gr_name
        self.prefix = Path(cfg["RK1S_PREFIX"])
        self.python = None
        self.python_version = None
        self.changed = False          # the running deploy or a unit changed: restart
        self.taken_over = []          # hand-installed units to stop, and restore on failure
        self.new_deploy = None

    # -- running things --
    def as_root(self, argv):
        return list(argv) if self.euid == 0 else ["sudo", *argv]

    def as_user(self, argv):
        """As the service user: directly when that is who runs this, else through sudo."""
        if self.euid == 0:
            return ["runuser", "-u", self.user, "--", *argv]
        if pwd.getpwuid(os.getuid()).pw_name != self.user:
            return ["sudo", "-u", self.user, "--", *argv]
        return list(argv)

    def look(self, argv, timeout=60):
        """A read-only command, run in a dry run too: (returncode, stdout)."""
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            return r.returncode, r.stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            return 127, str(e)

    def run(self, argv, cwd=None):
        print(f"    $ {' '.join(shlex.quote(str(a)) for a in argv)}", file=self.out, flush=True)
        r = subprocess.run([str(a) for a in argv], cwd=cwd)
        if r.returncode != 0:
            raise Fail(f"command failed ({r.returncode}): {' '.join(map(str, argv))}")

    # -- actions --
    def cmd(self, argv, root=False, cwd=None, note=""):
        argv = [str(a) for a in argv]
        argv = self.as_root(argv) if root else self.as_user(argv)
        shown = " ".join(shlex.quote(a) for a in argv) + (f"   # {note}" if note else "")
        if cwd:
            shown = f"(cd {cwd}) {shown}"
        return Action(shown, lambda: self.run(argv, cwd))

    def write(self, path: Path, content: str, root=True, mode="0644", note=""):
        def fn():
            with tempfile.NamedTemporaryFile("w", delete=False, prefix="rk1-speech-") as f:
                f.write(content)
                tmp = f.name
            os.chmod(tmp, 0o644)     # install may run as another user (root writing for the service user)
            try:
                owner = ["-o", "root", "-g", "root"] if root else ["-o", self.user, "-g", self.group]
                argv = ["install", "-D", "-m", mode, *(owner if (root or self.euid == 0) else []), tmp, str(path)]
                self.run(self.as_root(argv) if root else self.as_user(argv))
            finally:
                os.unlink(tmp)
        return Action(f"write {path} ({len(content.splitlines())} lines){'   # ' + note if note else ''}", fn)

    def symlink(self, target, link: Path, note=""):
        """Atomically: a new link beside the old, renamed over it."""
        tmp = link.with_name(f".{link.name}.new")
        return Action(f"ln -sfn {target} {link}{'   # ' + note if note else ''}",
                      lambda: (self.run(self.as_user(["ln", "-sfn", str(target), str(tmp)])),
                               self.run(self.as_user(["mv", "-T", str(tmp), str(link)]))))

    def call(self, shown, fn):
        return Action(shown, fn)

    # -- paths --
    def engine_dir(self, name):
        """engines/<name>-<commit>-<environment>: never changed once a deploy uses it. A new
        Python or new constraints make a new directory, not a rebuilt venv under a running
        server, so the previous deploy stays intact for a rollback."""
        commit = self.ver[f"{name.upper().replace('-', '_')}_COMMIT"]
        cons = file_text(SRC / "constraints" / ENGINE_CONSTRAINTS[name]) or ""
        env = stamp_of(commit, cons, self.python or "", self.python_version or "")[:8]
        return self.prefix / "engines" / f"{name}-{commit[:12]}-{env}"

    def _model_key(self, name):
        """<commit>, plus for kokoro-rknn what else decides its graphs (the bucket lengths
        and the toolkit), so changing either makes a new directory."""
        commit = self.ver[f"{name.upper().replace('-', '_')}_COMMIT"]
        if name == "kokoro-rknn":
            extra = stamp_of(" ".join(self.ver["KOKORO_BUCKETS"].split()), self.ver["RKNN_TOOLKIT2"])[:8]
            return f"{commit[:12]}-{extra}"
        return commit[:12]

    def model_dir(self, name):
        return self.prefix / "models" / f"{name}-{self._model_key(name)}"

    def build_dir(self, name):
        return self.prefix / "build" / f"{name}-{self._model_key(name)}"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def vtuple(s):
    return tuple(int(x) for x in re.findall(r"\d+", s or "")[:3])


def cpu_parts() -> dict:
    """{cpu number: 'CPU part' id} from /proc/cpuinfo (0xd0b = Cortex-A76, 0xd05 = A55)."""
    parts, cur = {}, None
    for line in (health.read("/proc/cpuinfo", "") or "").splitlines():
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if k == "processor":
            cur = int(v)
        elif k == "CPU part" and cur is not None:
            parts[cur] = v.lower()
    return parts


def cpu_list(spec: str) -> list:
    out = []
    for part in spec.replace(" ", "").split(","):
        if "-" in part:
            lo, hi = part.split("-")
            out += list(range(int(lo), int(hi) + 1))
        elif part:
            out.append(int(part))
    return out


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def known_good() -> dict:
    """{sha256: file name} of graphs known to be safe (known-good/kokoro-rknn-buckets.sha256)."""
    out = {}
    for line in (SRC / "known-good" / "kokoro-rknn-buckets.sha256").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            digest, name = line.split()
            out[digest] = name
    return out


def regtask_errors(log: Path):
    """REGTASK lines in a compile log ("... exceeds the limit"); None when there is no log
    or it does not say the model compiled."""
    try:
        text = log.read_text(errors="replace")
    except OSError:
        return None
    if "COMPILED" not in text:
        return None
    return text.count("exceeds the limit")


def bucket_names(ver) -> list:
    return [f"dec_T{t}_{k}.rknn" for t in ver["KOKORO_BUCKETS"].split() for k in "AB"]


def check_buckets(src: Path, ver, logs: Path | None = None) -> tuple:
    """(ok names, problems) for the compiled graphs in `src`: each must be known-good by
    hash, or have a compile log (in `logs`, default src/logs) with no REGTASK error. A
    graph with REGTASK errors hangs the NPU for minutes; it is never accepted."""
    good = known_good()
    logs = logs or src / "logs"
    ok, problems = [], []
    for name in bucket_names(ver):
        f = src / name
        if not f.is_file():
            problems.append(f"{name}: missing")
            continue
        t, k = re.match(r"dec_T(\d+)_([AB])", name).groups()
        n = regtask_errors(logs / f"T{t}_{k}.log")
        if n:
            problems.append(f"{name}: {n} REGTASK errors in its compile log: never run it")
            continue
        if good.get(sha256(f)) == name or n == 0:
            ok.append(name)
        else:
            problems.append(f"{name}: not a known-good build and no compile log in {logs}")
    return ok, problems


def dir_mb(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total // 2 ** 20


def port_listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", int(port))) == 0


def unit_info(ctx, unit) -> dict:
    rc, out = ctx.look(["systemctl", "show", unit, "-p", "LoadState,ActiveState,UnitFileState,FragmentPath"])
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def git_head(ctx, d: Path):
    if not (d / ".git").exists():
        return None
    rc, out = ctx.look(["git", "-C", str(d), "rev-parse", "HEAD"])
    return out if rc == 0 else None


def file_text(p: Path):
    try:
        return p.read_text()
    except OSError:
        return None


def stamp_of(*parts) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p if isinstance(p, bytes) else str(p).encode())
        h.update(b"\0")
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# the steps
# ---------------------------------------------------------------------------

def step_platform(ctx):
    problems = []
    if platform.machine() != "aarch64":
        problems.append(f"needs aarch64, this is {platform.machine()}")
    compat = (health.read("/proc/device-tree/compatible", "") or "")
    if "rockchip,rk3588" not in compat:
        problems.append("not an RK3588 (no rockchip,rk3588 in /proc/device-tree/compatible)")
    if not shutil.which("systemctl"):
        problems.append("no systemd")
    model = (health.read("/proc/device-tree/model", "") or "").rstrip("\0")
    if problems:
        return Step("platform", "fail", "; ".join(problems))
    return Step("platform", "ok", f"{model or 'RK3588'}, {platform.machine()}, kernel {platform.release()}")


def step_cpus(ctx):
    want = cpu_list(ctx.cfg["RK1S_CPU_AFFINITY"])
    parts = cpu_parts()
    bad = [c for c in want if parts.get(c) != "0xd0b"]
    if bad:
        return Step("CPU cores", "fail",
                    f"RK1S_CPU_AFFINITY={ctx.cfg['RK1S_CPU_AFFINITY']}: cores {bad} are not Cortex-A76 "
                    f"(CPU part {[parts.get(c) for c in bad]}); the speech servers need the A76s")
    return Step("CPU cores", "ok", f"{ctx.cfg['RK1S_CPU_AFFINITY']} are Cortex-A76")


def step_driver(ctx):
    v = health.driver_version()
    need = ctx.ver["RKNPU_DRIVER_MIN"]
    if not v:
        return Step("RKNPU driver", "fail", "no RKNPU driver loaded (/sys/module/rknpu/version)")
    if vtuple(v) < vtuple(need):
        return Step("RKNPU driver", "fail", f"{v}, needs {need} or later (a kernel update; not done here)")
    return Step("RKNPU driver", "ok", v)


def step_runtime(ctx):
    path = Path("/usr/lib/librknnrt.so")
    want, url, digest = ctx.ver["LIBRKNNRT_VERSION"], ctx.ver["LIBRKNNRT_URL"], ctx.ver["LIBRKNNRT_SHA256"]
    have = health.librknnrt_version(str(path))
    if have and have.split()[0] == want:
        same = sha256(path) == digest
        return Step("librknnrt", "ok", f"{have}" + ("" if same else " (another build of it; kept)"))

    def fetch():
        tmpdir = Path(tempfile.mkdtemp(prefix="rk1-speech-rt-"))
        try:
            tmp = tmpdir / "librknnrt.so"
            ctx.run(["curl", "-fsSL", "-o", str(tmp), url])
            if sha256(tmp) != digest:
                raise Fail(f"librknnrt.so from {url} has another sha256 than {digest}")
            ctx.run(ctx.as_root(["install", "-m", "0644", "-o", "root", "-g", "root", str(tmp), str(path)]))
            ctx.run(ctx.as_root(["ldconfig"]))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    act = Action(f"download {url}, check sha256 {digest[:16]}..., install as {path}", fetch)
    if have and not ctx.args.replace_runtime:
        return Step("librknnrt", "fail", f"{path} is {have}, not {want}; other NPU programs on this board "
                    f"may depend on it. --replace-runtime replaces it")
    return Step("librknnrt", "todo", f"{'is ' + have if have else 'missing'}; needs {want}", [act])


def step_python(ctx):
    want = ctx.cfg["RK1S_PYTHON"]
    pv = ctx.ver["PYTHON_VERSION"]
    candidates = []
    if want != "auto":
        candidates.append(("RK1S_PYTHON", want))
    else:
        uv = shutil.which("uv") or str(ctx.home / ".local/bin/uv")
        if os.access(uv, os.X_OK):
            rc, out = ctx.look(ctx.as_user([uv, "python", "find", pv]))
            if rc == 0 and out:
                candidates.append((f"uv's Python {pv}", out.splitlines()[-1]))
        for exe in (f"python{pv}", "python3"):
            if shutil.which(exe):
                candidates.append((exe, shutil.which(exe)))
    for label, exe in candidates:
        rc, out = ctx.look([exe, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"])
        if rc != 0:
            continue
        if not (3, 10) <= vtuple(out)[:2] <= (3, 12):
            continue
        ctx.python, ctx.python_version = exe, out
        rc2, _ = ctx.look([exe, "-c", "import venv, ensurepip"])
        ctx.python_needs_venv_pkg = rc2 != 0
        note = "" if out.startswith(pv + ".") else f" (measured with {pv}; 3.10-3.12 work)"
        return Step("Python", "ok", f"{exe} ({out}, {label}){note}"
                    + ("; needs python3-venv" if rc2 else ""))
    return Step("Python", "fail", f"no Python 3.10-3.12 found; e.g. `uv python install {pv}` "
                f"(as the measured boards have) or set RK1S_PYTHON")


def step_packages(ctx):
    need = ["git", "gcc", "libc6-dev", "curl"]
    if getattr(ctx, "python_needs_venv_pkg", False):
        need.append("python3-venv")
    if config.truthy(ctx.cfg["RK1S_ANNOUNCE"]):
        need.append("avahi-daemon")
    missing = []
    for pkg in need:
        rc, out = ctx.look(["dpkg-query", "-W", "-f=${Status}", pkg])
        if rc != 0 or "install ok installed" not in out:
            missing.append(pkg)
    if not missing:
        return Step("system packages", "ok", " ".join(need))
    return Step("system packages", "todo", "missing: " + " ".join(missing),
                [ctx.cmd(["apt-get", "update"], root=True),
                 ctx.cmd(["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y",
                          "--no-install-recommends", *missing], root=True)])


def step_resources(ctx):
    st = os.statvfs(ctx.prefix if ctx.prefix.exists() else ctx.prefix.parent)
    free_mb = st.f_bavail * st.f_frsize // 2 ** 20
    need = 0
    if not ctx.engine_dir("moonshine-rknn").exists() or not ctx.engine_dir("kokoro-rknn").exists():
        need += ENGINES_MB
    if not (ctx.model_dir("moonshine-rknn") / ".rk1-speech.json").exists():
        need += MOONSHINE_FILES_MB
    if not (ctx.model_dir("kokoro-rknn") / ".rk1-speech.json").exists():
        need += KOKORO_FETCH_MB + KOKORO_RUNTIME_MB + (0 if ctx.args.kokoro_buckets else KOKORO_BUILD_MB)
    mem_mb = int((health.read("/proc/meminfo", "") or "MemTotal: 0").split()[1]) // 1024
    notes = [f"{free_mb / 1024:.1f} GB free on {ctx.prefix.parent}, about {need / 1024:.1f} GB needed",
             f"{mem_mb / 1024:.0f} GB RAM"]
    if free_mb < need:
        return Step("disk and memory", "fail", "; ".join(notes)
                    + ("" if ctx.args.kokoro_buckets else
                       " (--kokoro-buckets DIR skips the ~4.5 GB graph build)"))
    if mem_mb < 7000:
        return Step("disk and memory", "warn", "; ".join(notes) + ": the synthesiser alone uses ~4 GB")
    return Step("disk and memory", "ok", "; ".join(notes))


def step_ports(ctx):
    """The ports must be free, ours, or held by a hand-installed server we may take over."""
    problems, legacy_active, details = [], [], []
    for kind in ("stt", "tts"):
        port = int(ctx.cfg[f"RK1S_{kind.upper()}_PORT"])
        ours = unit_info(ctx, units.UNITS[kind]).get("ActiveState") == "active"
        old = [u for u in units.LEGACY[kind]
               if unit_info(ctx, u).get("ActiveState") in ("active", "activating", "reloading")
               or unit_info(ctx, u).get("UnitFileState") == "enabled"]
        legacy_active += old
        busy = port_listening(port)
        if ours:
            details.append(f"{port} ({kind}): {units.UNITS[kind]}")
        elif old:
            details.append(f"{port} ({kind}): {', '.join(old)} (installed by hand)")
        elif busy:
            problems.append(f"port {port} ({kind}) is in use by something else (RK1S_{kind.upper()}_PORT)")
        else:
            details.append(f"{port} ({kind}): free")
    if problems:
        return Step("ports", "fail", "; ".join(problems))
    if legacy_active:
        ctx.taken_over = legacy_active
        if not ctx.args.takeover:
            return Step("ports", "fail", "; ".join(details) + ". --takeover stops and disables "
                        f"{', '.join(legacy_active)} when the new servers start (and restores them if "
                        "the new servers do not come up)")
        return Step("ports", "todo", "; ".join(details) + f"; will stop and disable {', '.join(legacy_active)} "
                    "just before starting the new servers",
                    [Action(f"(later) systemctl disable --now {' '.join(legacy_active)}", lambda: None)])
    return Step("ports", "ok", "; ".join(details))


def step_npu_space(ctx):
    card = health.npu_card()
    procs = health.npu_processes(card)
    ours = set()
    for u in [units.UNITS["stt"], units.UNITS["tts"], *ctx.taken_over]:
        rc, pid = ctx.look(["systemctl", "show", u, "-p", "MainPID", "--value"])
        if pid.isdigit() and int(pid):
            ours.add(int(pid))
    others = [p for p in procs if p["pid"] not in ours]
    other_mb = sum(p["mb"] for p in others)
    need = NPU_MB["stt"] + NPU_MB["tts"]
    detail = (f"the speech servers map ~{need / 1024:.1f} GB of the NPU's ~4 GB address space; "
              f"other processes: {other_mb} MB"
              + "".join(f"\n        {p['pid']} {p['comm']} {p['mb']} MB: {p['cmd'][:80]}" for p in others))
    if not card:
        return Step("NPU address space", "fail", "no NPU device (/dev/dri/by-path/*npu-card)")
    if other_mb + need > NPU_BUDGET_MB:
        return Step("NPU address space", "fail", detail + "\n        stop those first: when the space runs "
                    "out, rknn_init fails with -6 and the kernel logs 'dma map ... fail'")
    return Step("NPU address space", "ok", detail)


def step_prefix(ctx):
    p = ctx.prefix
    if p.is_dir() and p.stat().st_uid == pwd.getpwnam(ctx.user).pw_uid:
        return Step(f"{p}", "ok", f"owned by {ctx.user}")
    return Step(f"{p}", "todo", "missing" if not p.exists() else "not owned by the service user",
                [ctx.cmd(["install", "-d", "-o", ctx.user, "-g", ctx.group, "-m", "0755", p], root=True)])


def step_config(ctx):
    if config.CONF.is_file():
        return Step(f"{config.CONF}", "ok", "kept as it is (edit it and re-run to change a setting)")
    content = config.render_conf(ctx.cfg)
    return Step(f"{config.CONF}", "todo", "missing: written with the settings in use",
                [ctx.write(config.CONF, content)])


def step_checkout(name):
    def step(ctx):
        key = name.upper().replace("-", "_")
        repo, commit = ctx.ver[f"{key}_REPO"], ctx.ver[f"{key}_COMMIT"]
        d = ctx.engine_dir(name)
        head = git_head(ctx, d)
        if head == commit:
            rc, dirty = ctx.look(["git", "-C", str(d), "status", "--porcelain", "--untracked-files=no"])
            if not dirty:
                return Step(f"{name} source", "ok", f"{d} at {commit[:12]}")
            return Step(f"{name} source", "fail", f"{d} has local changes; move it away to rebuild it")
        if d.exists():
            return Step(f"{name} source", "fail", f"{d} exists but is not {commit[:12]}; move it away")
        tmp = d.with_name(d.name + ".tmp")
        return Step(f"{name} source", "todo", f"clone {repo} at {commit[:12]}", [
            ctx.cmd(["rm", "-rf", tmp]),
            ctx.cmd(["git", "clone", "--quiet", "--no-checkout", repo, tmp]),
            ctx.cmd(["git", "-C", tmp, "-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", commit]),
            ctx.cmd(["mv", "-T", tmp, d])])
    return step


def venv_stamp(ctx, d: Path, reqs, constraints: Path):
    """What the venv was built from: the Python, the engine's requirements and the constraints.
    Not rk1-speech's own version: a new release must not rebuild a venv that a running deploy
    (or the previous one, kept for a rollback) uses."""
    parts = [ctx.python]
    for r in reqs:
        parts.append(file_text(d / r) or f"<{r} after checkout>")
    parts.append(file_text(constraints) or "")
    return stamp_of(*parts)


def venv_actions(ctx, venv: Path, req_files, constraints: Path, cwd: Path, extra=()):
    pip = venv / "bin" / "pip"
    args = [pip, "install", "--quiet", "--disable-pip-version-check"]
    for r in req_files:
        args += ["-r", r]
    args += [*extra, "-c", constraints]
    return [ctx.cmd(["rm", "-rf", venv]),
            ctx.cmd([ctx.python or "python3", "-m", "venv", venv]),
            Action(f"(cd {cwd}) {' '.join(map(str, args))}",
                   lambda: ctx.run(ctx.as_user([str(a) for a in args]), cwd=str(cwd)))]


def step_venv(name, reqs, constraints, extra_actions=None):
    def step(ctx):
        d = ctx.engine_dir(name)
        venv = d / ".venv"
        cons = SRC / "constraints" / constraints
        want = venv_stamp(ctx, d, reqs, cons)
        stamp = venv / "rk1-speech.stamp"
        extras = extra_actions(ctx, d) if extra_actions else []
        extras_ok = all(ok for ok, _ in extras)
        if file_text(stamp) == want and (venv / "bin" / "python").exists() and extras_ok:
            return Step(f"{name} environment", "ok", f"{venv}")
        acts = []
        if file_text(stamp) != want or not (venv / "bin" / "python").exists():
            acts += venv_actions(ctx, venv, reqs, cons, d)
            acts.append(Action(f"write {stamp}", lambda: ctx.run(ctx.as_user(
                ["sh", "-c", f"printf %s {shlex.quote(want)} > {shlex.quote(str(stamp))}"]))))
        for ok, act in extras:
            if not ok:
                acts.append(act)
        return Step(f"{name} environment", "todo",
                    f"{venv}: {'requirements.txt with ' + constraints + ' (versions measured on the boards)'}",
                    acts)
    return step


def interleave(ctx, d: Path):
    so, c = d / "kokoro_rknn" / "libinterleave.so", d / "kokoro_rknn" / "interleave.c"
    ok = so.exists() and c.exists() and so.stat().st_mtime >= c.stat().st_mtime
    return [(ok, ctx.cmd(["gcc", "-O3", "-march=armv8.2-a", "-shared", "-fPIC", "-o", so, c],
                         note="the 40-line C helper that interleaves graph A's output into graph B"))]


def marker_ok(d: Path) -> bool:
    m = d / ".rk1-speech.json"
    try:
        files = json.loads(m.read_text())["files"]
    except (OSError, ValueError, KeyError):
        return False
    return all((d / f).is_file() and (d / f).stat().st_size == size for f, size in files.items())


def write_marker(ctx, d: Path, names, extra=None):
    def fn():
        data = {"files": {n: (d / n).stat().st_size for n in names}, "written": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                **(extra or {})}
        ctx.run(ctx.as_user(["sh", "-c", f"cat > {shlex.quote(str(d / '.rk1-speech.json'))} <<'EOF'\n"
                                         f"{json.dumps(data, indent=1)}\nEOF"]))
    return Action(f"write {d / '.rk1-speech.json'} (sizes of the verified files)", fn)


MOONSHINE_MODELS = ["frontend.onnx", "streaming_config.json", "tokenizer.json", "adapter.ort",
                    "cross_kv.ort", "decoder_kv.ort", "silero_vad.onnx", "stream_encoder_c16.rknn"]


def step_moonshine_models(ctx):
    d = ctx.model_dir("moonshine-rknn")
    if marker_ok(d):
        return Step("moonshine-rknn models", "ok", f"{d}")
    eng = ctx.engine_dir("moonshine-rknn")
    acts = [ctx.cmd(["mkdir", "-p", d])]
    seed = Path(ctx.args.moonshine_models) if ctx.args.moonshine_models else None
    note = "download ~460 MB (Moonshine's ONNX export, moonshine-voice's decoder, Silero VAD, the NPU encoder)"
    if seed:
        have = [f for f in MOONSHINE_MODELS if (seed / f).is_file()]
        note = f"copy {len(have)} of {len(MOONSHINE_MODELS)} files from {seed}, download the rest"
        acts += [ctx.cmd(["cp", "--reflink=auto", "-p", seed / f, d / f]) for f in have]
    acts.append(ctx.cmd([eng / ".venv/bin/python", eng / "scripts/fetch_models.py", d],
                        note="verifies every file's sha256 (moonshine-rknn's own list)"))
    acts.append(write_marker(ctx, d, MOONSHINE_MODELS))
    return Step("moonshine-rknn models", "todo", note, acts)


def step_kokoro_models(ctx):
    d = ctx.model_dir("kokoro-rknn")
    buckets = d / "buckets"
    runtime = ["kokoro-v1.0.onnx", "voices-v1.0.bin", "front.onnx", "har.onnx", "tail.onnx"] + \
              [f"buckets/{n}" for n in bucket_names(ctx.ver)]
    if marker_ok(d):
        ok, problems = check_buckets(buckets, ctx.ver)
        if problems:
            return Step("kokoro-rknn models", "fail", "; ".join(problems))
        return Step("kokoro-rknn models", "ok", f"{d}: {len(ok)} NPU graphs, each known-good or REGTASK-free")
    eng, b = ctx.engine_dir("kokoro-rknn"), ctx.build_dir("kokoro-rknn")
    cons = SRC / "constraints" / "tts.txt"
    acts = [ctx.cmd(["mkdir", "-p", d, b]),
            ctx.cmd([eng / ".venv/bin/python", eng / "build/fetch_models.py", d],
                    note="kokoro-v1.0.onnx + voices-v1.0.bin from kokoro-onnx's release, checksummed")]
    acts += venv_actions(ctx, b / ".venv-build", ["requirements-build.txt"], cons, eng)
    acts.append(ctx.cmd([b / ".venv-build/bin/python", eng / "build/extract.py", d],
                        note="cut front/har/dec/tail.onnx out of the reference model"))
    src = Path(ctx.args.kokoro_buckets) if ctx.args.kokoro_buckets else None
    if src:
        ok, problems = check_buckets(src, ctx.ver)
        if problems:
            return Step("kokoro-rknn models", "fail", f"--kokoro-buckets {src}: " + "; ".join(problems))
        acts.append(ctx.cmd(["mkdir", "-p", buckets / "logs"]))
        acts += [ctx.cmd(["cp", "--reflink=auto", "-p", src / n, buckets / n]) for n in bucket_names(ctx.ver)]
        if (src / "logs").is_dir():
            acts.append(ctx.cmd(["cp", "-rp", f"{src}/logs/.", buckets / "logs"]))
        how = f"copy {len(ok)} NPU graphs from {src} (all known-good by hash or REGTASK-free by their logs)"
    else:
        tk = b / ".venv-tk"
        acts += [ctx.cmd(["rm", "-rf", tk]), ctx.cmd([ctx.python or "python3", "-m", "venv", tk]),
                 ctx.cmd([tk / "bin/pip", "install", "--quiet", "--disable-pip-version-check",
                          f"rknn-toolkit2=={ctx.ver['RKNN_TOOLKIT2']}", "setuptools<70", "opencv-python-headless"],
                         note="Rockchip's compiler; pins its own numpy/onnx/torch, so a venv of its own")]
        acts.append(ctx.cmd(["bash", eng / "build/build_buckets.sh", d, b / ".venv-build/bin/python",
                             tk / "bin/python", *ctx.ver["KOKORO_BUCKETS"].split()],
                            note="~15 min on the board; deletes any graph whose log shows REGTASK errors"))
        how = f"build {len(bucket_names(ctx.ver))} NPU graphs with rknn-toolkit2 {ctx.ver['RKNN_TOOLKIT2']} (~15 min)"

    def verify():
        ok, problems = check_buckets(buckets, ctx.ver)
        if problems:
            raise Fail("NPU graphs not safe to run: " + "; ".join(problems))
    acts.append(Action(f"check every graph in {buckets}: known-good hash or a compile log with 0 REGTASK errors",
                       verify))
    acts.append(ctx.cmd(["sh", "-c", f"rm -f {shlex.quote(str(d))}/dec.onnx {shlex.quote(str(buckets))}/*.onnx"],
                        note="intermediate graphs, ~1.6 GB, not needed to run"))
    acts.append(write_marker(ctx, d, runtime))
    return Step("kokoro-rknn models", "todo", f"{d}: fetch, extract, {how}", acts)


def deploy_plan(ctx) -> dict:
    """What the next deploy would hold, and its id."""
    eng = {n: ctx.engine_dir(n) for n in ("moonshine-rknn", "kokoro-rknn")}
    pkg = sorted((SRC / "rk1speech").glob("*.py"))
    settings = {k: ctx.cfg[k] for k in config.DEFAULTS}
    links = {"stt": eng["moonshine-rknn"], "tts": eng["kokoro-rknn"],
             "stt-models": ctx.model_dir("moonshine-rknn"), "tts-models": ctx.model_dir("kokoro-rknn")}
    digest = stamp_of(__version__, json.dumps(settings, sort_keys=True), json.dumps(ctx.ver, sort_keys=True),
                      json.dumps({k: str(v) for k, v in links.items()}, sort_keys=True),
                      *(p.read_bytes() for p in pkg))
    return {"digest": digest, "rk1_speech": __version__,
            "engines": {n: ctx.ver[f"{n.upper().replace('-', '_')}_COMMIT"][:12] for n in eng},
            "links": links, "settings": settings}


def read_manifest(path: Path):
    try:
        return json.loads((path / "manifest.json").read_text())
    except (OSError, ValueError):
        return None


# The rest of the source, so `current/install.py --rollback` runs from the deploy itself.
DEPLOY_FILES = ["install.py", "versions.env", "constraints", "known-good", "LICENSE", "NOTICE"]

LAUNCHER = """#!/usr/bin/python3 -I
# rk1-speech-health: the board's health as JSON (rk1speech/health.py in this deploy).
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from rk1speech.health import main
sys.exit(main())
"""


def step_deploy(ctx):
    plan = deploy_plan(ctx)
    cur = ctx.prefix / "current"
    man = read_manifest(cur) if cur.exists() else None
    if man and man.get("digest") == plan["digest"]:
        return Step("deploy", "ok", f"current = {man['id']}")
    did = time.strftime("%Y%m%d-%H%M%S") + "-" + plan["digest"][:8]
    d = ctx.prefix / "deploys" / did
    manifest = dict(plan, id=did, created=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    links={k: str(v) for k, v in plan["links"].items()},
                    host=socket.gethostname(), python=ctx.python)
    acts = [ctx.cmd(["mkdir", "-p", d / "bin", d / "rk1speech"])]
    acts += [ctx.cmd(["ln", "-sfn", target, d / name]) for name, target in plan["links"].items()]
    acts.append(ctx.cmd(["sh", "-c", f"cp -p {shlex.quote(str(SRC))}/rk1speech/*.py {shlex.quote(str(d))}/rk1speech/"],
                        note="this package, so a rollback also rolls back the tools"))
    acts.append(ctx.cmd(["cp", "-rp", *[SRC / f for f in DEPLOY_FILES if (SRC / f).exists()], d]))
    acts.append(ctx.write(d / "bin/rk1-speech-health", LAUNCHER, root=False, mode="0755"))
    # Its units and announcements too, so a rollback restores the settings with the code.
    for kind, text in wanted_units(ctx).items():
        acts.append(ctx.write(d / "units" / units.UNITS[kind], text, root=False))
    if config.truthy(ctx.cfg["RK1S_ANNOUNCE"]):
        for path, text in avahi_files(ctx).items():
            acts.append(ctx.write(d / "avahi" / path.name, text, root=False))
    acts.append(ctx.write(d / "manifest.json", json.dumps(manifest, indent=1) + "\n", root=False))
    if cur.exists():
        acts.append(ctx.symlink(os.readlink(cur), ctx.prefix / "previous", note="for --rollback"))
    acts.append(ctx.symlink(d, cur))
    ctx.changed, ctx.new_deploy = True, did      # the start step restarts the servers on it
    was = f" (was {man['id']})" if man else ""
    return Step("deploy", "todo", f"new deploy {did}{was}: moonshine-rknn {plan['engines']['moonshine-rknn']}, "
                f"kokoro-rknn {plan['engines']['kokoro-rknn']}", acts)


def wanted_units(ctx) -> dict:
    out = {"stt": units.stt(ctx.cfg, ctx.user), "tts": units.tts(ctx.cfg, ctx.user)}
    if config.truthy(ctx.cfg["RK1S_HEALTH"]):
        out["health"] = units.health(ctx.cfg, ctx.user)
    return out


def step_units(ctx):
    acts, notes = [], []
    for kind, text in wanted_units(ctx).items():
        path = UNIT_DIR / units.UNITS[kind]
        if file_text(path) != text:
            acts.append(ctx.write(path, text))
            notes.append(units.UNITS[kind])
    target = ctx.prefix / "current" / "bin" / "rk1-speech-health"
    if not (BIN_LINK.is_symlink() and os.readlink(BIN_LINK) == str(target)):
        acts.append(ctx.cmd(["ln", "-sfn", target, BIN_LINK], root=True))
        notes.append(str(BIN_LINK))
    if not acts:
        return Step("systemd units", "ok", ", ".join(units.UNITS[k] for k in wanted_units(ctx)))
    acts.append(ctx.cmd(["systemctl", "daemon-reload"], root=True))
    ctx.changed = True      # the start step restarts the servers with them
    return Step("systemd units", "todo", "write " + ", ".join(notes), acts)


def avahi_files(ctx) -> dict:
    out = {}
    for kind, name in (("stt", "moonshine-rknn"), ("tts", "kokoro-rknn")):
        version = ctx.ver[f"{name.upper().replace('-', '_')}_COMMIT"][:12]
        out[AVAHI_DIR / announce.FILES[kind]] = announce.render(kind, ctx.cfg, name, version)
    return out


def step_announce(ctx):
    files = avahi_files(ctx)
    if not config.truthy(ctx.cfg["RK1S_ANNOUNCE"]):
        stale = [p for p in files if p.exists()]
        if stale:
            return Step("mDNS announcement", "todo", "RK1S_ANNOUNCE=no: withdraw it",
                        [ctx.cmd(["rm", "-f", *stale], root=True)])
        return Step("mDNS announcement", "ok", "off (RK1S_ANNOUNCE=no)")
    acts = [ctx.write(p, text) for p, text in files.items() if file_text(p) != text]
    rc, active = ctx.look(["systemctl", "is-active", "avahi-daemon"])
    if active != "active":
        acts.append(ctx.cmd(["systemctl", "enable", "--now", "avahi-daemon"], root=True))
    elif acts:
        # avahi does watch the directory, but `install` creates a file before it
        # sets its mode: avahi can catch it unreadable ("Permission denied") and
        # then ignore it until the next change. Seen on a board whose speech
        # synthesis never got announced. A reload reads the finished files.
        acts.append(ctx.cmd(["systemctl", "reload-or-restart", "avahi-daemon"], root=True))
    types = ", ".join(f"{announce.TYPES[k]} on {ctx.cfg[f'RK1S_{k.upper()}_PORT']}" for k in ("stt", "tts"))
    if not acts:
        return Step("mDNS announcement", "ok", types)
    return Step("mDNS announcement", "todo", f"{types} (avahi reads {AVAHI_DIR} by itself)", acts)


def wait_healthy(ctx, timeout=HEALTH_TIMEOUT) -> tuple:
    """Both servers answering, on the ports of the deploy that is current (after a rollback,
    the previous deploy's settings)."""
    man = read_manifest(ctx.prefix / "current") or {}
    settings = man.get("settings") or ctx.cfg
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        last = {k: probe.PROBES[k]("127.0.0.1", int(settings.get(f"RK1S_{k.upper()}_PORT")
                                                    or ctx.cfg[f"RK1S_{k.upper()}_PORT"]), 2.0)
                for k in ("stt", "tts")}
        if all(r["ok"] for r in last.values()):
            return True, last
        time.sleep(2)
    return False, last


def start_and_check(ctx, names, restart=True):
    verb = "restart" if restart else "start"
    ctx.run(ctx.as_root(["systemctl", "enable", *names]))
    ctx.run(ctx.as_root(["systemctl", verb, *names]))
    print(f"    waiting for both servers to answer (up to {HEALTH_TIMEOUT:.0f} s; synthesis loads in ~30 s)",
          file=ctx.out, flush=True)
    return wait_healthy(ctx)


def current_id(ctx):
    cur = ctx.prefix / "current"
    return Path(os.readlink(cur)).name if cur.is_symlink() else None


def mark_started(ctx):
    """Remember which deploy the servers were last started on and found healthy, so a run
    that stopped before restarting them (--no-start, a failure) is finished by the next."""
    ctx.run(ctx.as_user(["sh", "-c", f"printf %s {shlex.quote(current_id(ctx) or '')} > "
                                     f"{shlex.quote(str(ctx.prefix / 'started'))}"]))


def try_start(ctx, names):
    """(ok, why): enable and restart, then wait for both servers. Never raises."""
    try:
        ok, res = start_and_check(ctx, names)
    except Fail as e:
        return False, str(e)
    return ok, "; ".join(f"{k}: {r['error'] or r['reply']}" for k, r in res.items() if not r["ok"])


def step_start(ctx):
    names = [units.UNITS[k] for k in wanted_units(ctx)]
    states = {n: unit_info(ctx, n) for n in names}
    running = all(s.get("ActiveState") == "active" for s in states.values())
    enabled = all(s.get("UnitFileState") == "enabled" for s in states.values())
    if ctx.args.no_start:
        return Step("start", "ok", "--no-start: not started (the next run starts them)")
    started = file_text(ctx.prefix / "started")
    if running and enabled and not ctx.changed and not ctx.taken_over and started == current_id(ctx):
        ok, res = wait_healthy(ctx, timeout=5)
        return Step("start", "ok" if ok else "warn", "running; " + ", ".join(
            f"{k} {'answers' if r['ok'] else 'does not answer: ' + str(r['error'])}" for k, r in res.items()))

    def go():
        try:
            if ctx.taken_over:
                ctx.run(ctx.as_root(["systemctl", "disable", "--now", *ctx.taken_over]))
            ok, why = try_start(ctx, names)
        except Fail as e:
            ok, why = False, str(e)
        if ok:
            mark_started(ctx)
            print("    both servers answer", file=ctx.out)
            return
        print(f"    the servers did not come up ({why})", file=ctx.out)
        recovered = False
        if ctx.new_deploy and (ctx.prefix / "previous").is_symlink():
            print("    rolling back to the previous deploy", file=ctx.out)
            try:
                swap(ctx)
                recovered, _ = try_start(ctx, names)
            except Fail as e:
                print(f"    rollback failed: {e}", file=ctx.out)
            if recovered:
                mark_started(ctx)
        if not recovered and ctx.taken_over:
            print(f"    restoring {', '.join(ctx.taken_over)}", file=ctx.out)
            ctx.run(ctx.as_root(["sh", "-c", f"systemctl disable --now {' '.join(names)} || true"]))
            ctx.run(ctx.as_root(["systemctl", "enable", "--now", *ctx.taken_over]))
            recovered = True
        raise Fail(f"the servers did not come up ({why}); "
                   + ("recovered: the previous servers run again" if recovered else "NO SERVERS RUNNING"))
    what = "restart" if running else "enable and start"
    extra = f"stop and disable {', '.join(ctx.taken_over)}, then " if ctx.taken_over else ""
    return Step("start", "todo", f"{extra}{what} {', '.join(names)}, wait until both answer; "
                "if they do not, roll back (or restore the hand-installed servers)",
                [Action(f"{extra}systemctl enable + {what.split()[-1]} {' '.join(names)}; health check; "
                        "roll back on failure", go)])


def swap(ctx):
    """current <-> previous, and the units and announcements of the deploy that is now
    current (deploys made before they were kept keep the files in place)."""
    cur, prev = ctx.prefix / "current", ctx.prefix / "previous"
    a, b = os.readlink(cur), os.readlink(prev)
    for target, link in ((b, cur), (a, prev)):
        ctx.symlink(target, link).fn()
    d = Path(b)
    if (d / "units").is_dir():
        for f in sorted((d / "units").glob("*.service")):
            ctx.run(ctx.as_root(["install", "-m", "0644", "-o", "root", "-g", "root", str(f), str(UNIT_DIR / f.name)]))
        mine = {AVAHI_DIR / n for n in announce.FILES.values()}
        keep = {AVAHI_DIR / f.name for f in (d / "avahi").glob("*.service")} if (d / "avahi").is_dir() else set()
        for f in sorted(keep):
            ctx.run(ctx.as_root(["install", "-m", "0644", "-o", "root", "-g", "root", str(d / "avahi" / f.name), str(f)]))
        if mine - keep:
            ctx.run(ctx.as_root(["rm", "-f", *map(str, sorted(mine - keep))]))
        ctx.run(ctx.as_root(["systemctl", "daemon-reload"]))
        ctx.run(ctx.as_root(["systemctl", "reload-or-restart", "avahi-daemon"]))  # see step_announce


def deploy_units(path: Path, fallback) -> list:
    d = path / "units"
    names = sorted(f.name for f in d.glob("*.service")) if d.is_dir() else []
    return names or fallback


STEPS = [step_platform, step_cpus, step_driver, step_runtime, step_python, step_packages,
         step_resources, step_ports, step_npu_space, step_prefix, step_config,
         step_checkout("moonshine-rknn"), step_checkout("kokoro-rknn"),
         step_venv("moonshine-rknn", ["requirements.txt"], "stt.txt"),
         step_venv("kokoro-rknn", ["requirements.txt"], "tts.txt", interleave),
         step_moonshine_models, step_kokoro_models, step_deploy, step_units, step_announce, step_start]

MARK = {"ok": "ok   ", "todo": "WOULD" , "fail": "FAIL ", "warn": "warn "}


def run_steps(ctx, steps=STEPS) -> int:
    failed = changes = 0
    for fn in steps:
        try:
            st = fn(ctx)
        except Fail as e:
            st = Step(fn.__name__, "fail", str(e))
        mark = MARK[st.state] if (ctx.dry or st.state != "todo") else "do   "
        print(f"[{mark}] {st.title}: {st.detail}", file=ctx.out, flush=True)
        if st.state == "fail":
            failed += 1
            if not ctx.dry:
                break
            continue
        if st.state == "todo":
            changes += 1
            for act in st.actions:
                if ctx.dry:
                    print(f"          {act.shown}", file=ctx.out)
                else:
                    try:
                        act.fn()
                    except Fail as e:
                        print(f"[FAIL ] {st.title}: {e}", file=ctx.out, flush=True)
                        return 2
    if ctx.dry:
        print(f"\ndry run: {changes} step(s) would change something, {failed} check(s) failed; "
              "nothing was changed.", file=ctx.out)
    else:
        print(f"\n{'failed' if failed else 'done'}: {changes} step(s) changed something.", file=ctx.out)
    return 2 if failed else 0


def rollback(ctx) -> int:
    cur, prev = ctx.prefix / "current", ctx.prefix / "previous"
    if not (cur.is_symlink() and prev.is_symlink()):
        print("nothing to roll back to (no previous deploy)", file=ctx.out)
        return 2
    a, b = read_manifest(cur), read_manifest(prev)
    print(f"current {a and a.get('id')} -> previous {b and b.get('id')}", file=ctx.out)
    names = deploy_units(prev.resolve(), [units.UNITS[k] for k in wanted_units(ctx)])
    if ctx.dry:
        print(f"          swap {cur} and {prev} (and their units and announcements); systemctl restart "
              f"{' '.join(names)}; health check; swap back if it fails", file=ctx.out)
        return 0
    swap(ctx)
    ok, why = try_start(ctx, names)
    if ok:
        mark_started(ctx)
        print("both servers answer", file=ctx.out)
        return 0
    print(f"the previous deploy did not come up ({why}); going back", file=ctx.out)
    swap(ctx)
    ok2, why2 = try_start(ctx, deploy_units(cur.resolve(), names))
    if ok2:
        mark_started(ctx)
    print("back on the deploy from before; both servers answer" if ok2 else f"servers still down: {why2}",
          file=ctx.out)
    return 2


def prune(ctx) -> int:
    """Delete deploys other than current and previous, and the engines, models and build
    directories neither of them uses."""
    keep_deploys, keep = set(), set()
    for link in ("current", "previous"):
        p = ctx.prefix / link
        if p.is_symlink():
            d = Path(os.readlink(p))
            keep_deploys.add(d.name)
            for name in ("stt", "tts", "stt-models", "tts-models"):
                if (d / name).is_symlink():
                    keep.add(Path(os.readlink(d / name)).name)
    doomed = [d for d in sorted((ctx.prefix / "deploys").glob("*")) if d.name not in keep_deploys]
    for sub in ("engines", "models"):
        doomed += [d for d in sorted((ctx.prefix / sub).glob("*")) if d.name not in keep]
    doomed += sorted((ctx.prefix / "build").glob("*"))
    if not doomed:
        print("nothing to prune", file=ctx.out)
        return 0
    act = ctx.cmd(["rm", "-rf", *doomed])
    print(f"  {act.shown}", file=ctx.out)
    if not ctx.dry:
        act.fn()
    return 0


def uninstall(ctx) -> int:
    names = [units.UNITS[k] for k in ("stt", "tts", "health")]
    acts = [ctx.cmd(["sh", "-c", f"systemctl disable --now {' '.join(names)} || true"], root=True),
            ctx.cmd(["rm", "-f", *[UNIT_DIR / n for n in names], BIN_LINK,
                     *[AVAHI_DIR / f for f in announce.FILES.values()]], root=True),
            ctx.cmd(["systemctl", "daemon-reload"], root=True)]
    for a in acts:
        print(f"  {a.shown}", file=ctx.out)
        if not ctx.dry:
            a.fn()
    print(f"left in place: {ctx.prefix} and {config.CONF} (delete them by hand to free the disk)", file=ctx.out)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="install.py", description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="Settings: /etc/rk1-speech/rk1-speech.conf (docs/SETTINGS.md).")
    ap.add_argument("--dry-run", action="store_true", help="show what would change; change nothing")
    ap.add_argument("--config", default=str(config.CONF))
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="a setting for this run (first install: also written to the config file)")
    ap.add_argument("--takeover", action="store_true",
                    help="replace hand-installed servers on the same ports (rk1-stt, rk1-tts-*, ...)")
    ap.add_argument("--kokoro-buckets", metavar="DIR",
                    help="copy compiled NPU graphs from DIR instead of building them (each must be "
                         "known-good by hash or have a REGTASK-free compile log in DIR/logs)")
    ap.add_argument("--moonshine-models", metavar="DIR",
                    help="copy moonshine-rknn's model files from DIR before fetching (still checksummed)")
    ap.add_argument("--replace-runtime", action="store_true",
                    help="replace a /usr/lib/librknnrt.so of another version")
    ap.add_argument("--no-start", action="store_true", help="install and enable, but do not (re)start")
    ap.add_argument("--rollback", action="store_true", help="switch to the previous deploy and restart")
    ap.add_argument("--uninstall", action="store_true", help="stop and remove units and announcements")
    ap.add_argument("--prune", action="store_true",
                    help="delete deploys, engines and models that neither current nor previous uses")
    a = ap.parse_args(argv)
    cfg = config.load(a.config)
    for item in a.set:
        k, _, v = item.partition("=")
        if k not in config.DEFAULTS:
            ap.error(f"unknown setting {k}")
        cfg[k] = v
    try:
        ctx = Ctx(a, cfg, config.versions())
    except Fail as e:
        print(f"install.py: {e}", file=sys.stderr)
        return 2
    print(f"rk1-speech {__version__} on {socket.gethostname()} ({'dry run' if a.dry_run else 'applying'}), "
          f"service user {ctx.user}, prefix {ctx.prefix}", flush=True)
    if a.rollback:
        return rollback(ctx)
    if a.uninstall:
        return uninstall(ctx)
    if a.prune:
        return prune(ctx)
    return run_steps(ctx)
