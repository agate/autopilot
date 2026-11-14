"""`ap2 install` and `ap2 uninstall`: v2's own launchd job (design section 12, "Install").

The job runs `ap2 gate` every 60 seconds under v2's own label, next to v1's and secretary's jobs, and never touches
theirs. Only the user runs these commands. Every path is worked out at call time from `Path.home()` (or the folder
passed in), so a test's temp home moves all of them, and every launchctl call goes through a runner a test replaces.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import time
from pathlib import Path

from assistant import config, datadir
from assistant.config import ConfigError

LABEL = "com.assistant.gate"
INTERVAL = 60  # seconds between gate runs (design 7.1)
TOOLS = ("claude", "gh", "git", "python3")
ERR_LOG = "gate.err.log"  # launchd's own stderr for the job: Python errors and the `ap2: ...` line
LAUNCHCTL_TIMEOUT = 30  # seconds
UNLOAD_TRIES = 10  # `launchctl print` checks after bootout: a running gate can take a moment to exit
UNLOAD_WAIT = 0.5  # seconds between those checks, so install and uninstall wait about 4.5 s for the job to go
NO_COMPILER = ("no C compiler, so the gate runs bin/ap2 directly: macOS refuses that when the package is in a "
               "cloud-synced folder (install Xcode command line tools, then run `ap2 install` again)")
PRIVACY = ("macOS will ask to let assistant-gate into {folder}, and the gate cannot run until it is allowed "
           "(System Settings > Privacy & Security > Files and Folders)")
# macOS asks before a program reads these (System Settings > Privacy & Security > Files and Folders)
PROTECTED = (("Documents",), ("Desktop",), ("Downloads",), ("Library", "Mobile Documents"))


class InstallError(ConfigError):
    """The job could not be written or loaded. A ConfigError, so `ap2` prints one line and exits 1."""


def plist_path(agents_dir=None) -> Path:
    """`<agents_dir>/com.assistant.gate.plist`; the folder defaults to ~/Library/LaunchAgents."""
    folder = Path(agents_dir) if agents_dir is not None else Path.home() / "Library" / "LaunchAgents"
    return folder / f"{LABEL}.plist"


def launcher_path(home=None) -> Path:
    """The compiled launcher, outside any synced folder: macOS refuses a login item whose program sits in a
    cloud-synced folder, and the package often does (v1 found this; its launcher lives in the same place)."""
    base = Path(home) if home is not None else Path.home()
    return base / "Library" / "Application Support" / "assistant" / "assistant-gate"


def err_log_path(home=None) -> Path:
    """launchd's stderr for the job, outside the data folder: launchd opens it before the program runs, and the
    default data folder is one macOS guards (Documents), so a log there could stop the job from starting."""
    base = Path(home) if home is not None else Path.home()
    return base / "Library" / "Logs" / "assistant" / ERR_LOG


def _protected(folder: Path) -> bool:
    """Is the folder inside one macOS asks about before a program may read it (Documents, Desktop, Downloads, iCloud
    Drive)? Letter case is ignored, as the default disk does (~/documents is ~/Documents there)."""
    home = Path.home().resolve()
    folder = Path(folder).resolve()
    return any(datadir._inside(folder, home.joinpath(*parts)) for parts in PROTECTED)


def ap2_program() -> Path:
    from assistant import workers
    return workers.package_root() / "bin" / "ap2"


def domain() -> str:
    return f"gui/{os.getuid()}"


def target() -> str:
    return f"{domain()}/{LABEL}"


def path_value(path_dirs) -> str:
    """The tool folders, then the system folders (v1's SYSTEM_PATH), each once, joined for PATH."""
    from autopilot.install import SYSTEM_PATH
    out = []
    for d in list(path_dirs) + list(SYSTEM_PATH):
        if d and d not in out:
            out.append(d)
    return ":".join(out)


def render_plist(argv, data, path_dirs, err_log) -> bytes:
    data = Path(data)
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [str(a) for a in argv],
        "StartInterval": INTERVAL,
        "RunAtLoad": True,
        # Each run is already in gate.json and gate.log; the run's JSON on stdout would add lines without a time.
        "StandardOutPath": os.devnull,
        "StandardErrorPath": str(err_log),
        "EnvironmentVariables": {"PATH": path_value(path_dirs), datadir.ENV: str(data)},
    })


def _launchctl(*args) -> tuple:
    """Run `launchctl ARGS`; (exit code, output). A missing launchctl (Linux) or a timeout is (127, why)."""
    try:
        out = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=LAUNCHCTL_TIMEOUT,
                             stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 127, f"launchctl timed out after {LAUNCHCTL_TIMEOUT} s"
    except (OSError, subprocess.SubprocessError) as e:
        return 127, f"cannot run launchctl: {e}"
    return out.returncode, ((out.stdout or "") + (out.stderr or "")).strip()


def _build(ap, dest):
    from autopilot.install import build_launcher
    return build_launcher(ap, dest)


def _wait_gone(run, sleep) -> tuple:
    """After bootout, ask `print` up to UNLOAD_TRIES times, UNLOAD_WAIT apart, until the job is gone: bootout can
    return while a running gate is still exiting. Returns the last `print` result: 0 means still loaded; non-zero
    means gone, except 127, which means launchctl could not be asked."""
    rc, msg = 0, ""
    for n in range(UNLOAD_TRIES):
        rc, msg = run("print", target())
        if rc != 0:
            break
        if n + 1 < UNLOAD_TRIES:
            sleep(UNLOAD_WAIT)
    return rc, msg


def _one_line(text) -> str:
    return " ".join(str(text).split()) or "no output"


def _write_atomic(path: Path, payload: bytes) -> None:
    tmp = path.with_name(path.name + ".new")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(payload)
        os.replace(tmp, path)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise InstallError(f"cannot write {path}: {e}") from e


def install(data=None, agents_dir=None, home=None, launchctl=None, which=None, build=None, sleep=None) -> dict:
    """Write v2's plist and load it. `data` is the --data flag (None: AP2_DATA, else the default folder). The data
    folder is set up with `datadir.ensure`, so a new config.yaml says `mode: shadow`; an existing one is kept as it
    is. An old job is booted out and waited for (as in uninstall) before the new one is bootstrapped. Raises
    ConfigError (InstallError when launchctl refuses the job)."""
    run = launchctl or _launchctl
    sleep = sleep or time.sleep
    which = which or shutil.which
    build = build or _build
    folder = datadir.ensure(datadir.resolve(None if data is None else str(data)))
    mode = config.load(folder).mode  # a config.yaml the gate cannot read is refused before the job is written
    tools = {t: which(t) for t in TOOLS}
    dirs = [str(Path(p).parent) for p in tools.values() if p]
    ap2 = ap2_program()
    try:
        launcher = build(str(ap2), launcher_path(home))
    except OSError as e:  # e.g. a file where the launcher's folder should be
        raise InstallError(f"cannot build the launcher at {launcher_path(home)}: {_one_line(e)}") from e
    argv = [str(launcher)] if launcher else [str(ap2), "gate"]
    plist = plist_path(agents_dir)
    err_log = err_log_path(home)
    try:
        err_log.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise InstallError(f"cannot create {err_log.parent}: {_one_line(e)}") from e
    _write_atomic(plist, render_plist(argv, folder, dirs, err_log))
    run("bootout", target())  # not loaded yet is fine; `print` below says whether it is gone
    rc, _ = _wait_gone(run, sleep)
    if rc == 0:  # bootstrapping now would fail with "Input/output error" and leave no job loaded
        raise InstallError(f"launchctl bootout did not finish: {LABEL} is still loaded after {UNLOAD_TRIES} checks; "
                           "run `ap2 install` again")
    rc, msg = run("bootstrap", domain(), str(plist))
    if rc != 0:
        raise InstallError(f"launchctl bootstrap failed: {_one_line(msg)}; run `ap2 install` again")
    notes = [] if launcher else [NO_COMPILER]
    if _protected(folder):
        notes.append(PRIVACY.format(folder=folder))
    return {"label": LABEL, "plist": str(plist), "program": argv, "interval": INTERVAL, "data_dir": str(folder),
            "mode": mode, "tools": tools, "note": "; ".join(notes)}


def uninstall(agents_dir=None, home=None, launchctl=None, sleep=None) -> dict:
    """Unload v2's job and remove its plist and launcher. The data folder and the err log are not touched. bootout
    can return while the gate is still exiting, so `print` is asked up to UNLOAD_TRIES times, UNLOAD_WAIT apart. No
    file is removed unless `print` says the job is gone; one that cannot be asked (127) is an error, not "gone"."""
    run = launchctl or _launchctl
    sleep = sleep or time.sleep
    _, out = run("bootout", target())  # not loaded is fine; `print` below says whether it is gone
    rc, msg = _wait_gone(run, sleep)
    if rc == 127:
        raise InstallError(f"cannot confirm {LABEL} is gone: {_one_line(msg)}")
    if rc == 0:
        raise InstallError(f"launchctl bootout failed: {LABEL} is still loaded after {UNLOAD_TRIES} checks "
                           f"({_one_line(out)}); run `ap2 uninstall` again")
    removed = []
    for f in (plist_path(agents_dir), launcher_path(home)):
        try:
            f.unlink()
        except FileNotFoundError:
            continue
        except OSError as e:
            raise InstallError(f"cannot remove {f}: {e}") from e
        removed.append(str(f))
    return {"label": LABEL, "removed": removed}
