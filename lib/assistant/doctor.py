"""`ap2 doctor`: is v2's launchd job installed and healthy? (design section 12, "Install").

Read-only: the data folder is found with `datadir.resolve`, never `ensure`, so a doctor run creates nothing. Each
check is `{name, ok, detail, fix}` (v1's shape) with a fix line on every failure, and `checks` never raises.
"""
from __future__ import annotations

import errno
import json
import os
import plistlib
import shlex
import shutil
from datetime import timedelta
from pathlib import Path

from assistant import cards, config, datadir, install, profiles

FRESH = timedelta(minutes=3)  # the heartbeat turns red after 3 minutes without a run (design 7.4)
AHEAD = timedelta(minutes=5)  # a last run this far ahead of now means the clock went back
PRIVACY_FIX = ("allow access to {folder} in System Settings > Privacy & Security > Files and Folders (or Full Disk "
               "Access) for assistant-gate and for the terminal running `ap2 doctor`")


def _c(name, ok, detail="", fix="") -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail, "fix": "" if ok else (fix or "see the detail")}


def _install_cmd(data_flag) -> str:
    return "`ap2 install" + (f" --data {shlex.quote(str(data_flag))}" if data_flag else "") + "`"


def _blocked(e) -> bool:
    """Did macOS's privacy control refuse to let us read config.yaml? That refusal is EPERM; a file mode that refuses
    it is EACCES, which System Settings cannot fix. `config.load` raises its ConfigError from the OSError."""
    return getattr(e.__cause__, "errno", None) == errno.EPERM


def _config_fix(e, data, other) -> str:
    if _blocked(e):
        return PRIVACY_FIX.format(folder=data)
    if isinstance(e.__cause__, PermissionError):
        return f"make {data / config.FILE} readable for this user (chmod u+r, or chown it back)"
    return other


def _same_folder(job_data, data) -> bool:
    """The same text, or two spellings of one folder (another case on a case-blind disk, a symlink)."""
    if job_data == str(data):
        return True
    try:
        return bool(job_data) and os.path.samefile(job_data, data)
    except (OSError, TypeError, ValueError):
        return False


def _read_plist(path: Path):
    """(the plist as a dict, None) or (None, why it cannot be used)."""
    try:
        with open(path, "rb") as f:
            pl = plistlib.load(f)
    except FileNotFoundError:
        return None, f"not installed ({path} does not exist)"
    except (OSError, ValueError, plistlib.InvalidFileException) as e:
        return None, f"cannot read {path}: {e}"
    if not isinstance(pl, dict):
        return None, f"{path} does not hold a dict"
    return pl, None


def _env(pl) -> dict:
    env = pl.get("EnvironmentVariables") if isinstance(pl, dict) else None
    return env if isinstance(env, dict) else {}


def _job(pl, why, data, data_flag, run, plist) -> dict:
    fix = f"run {_install_cmd(data_flag)}"
    if pl is None:
        return _c("launchd job", False, why, fix)
    if pl.get("Label") != install.LABEL:
        return _c("launchd job", False, f"{plist} has Label {pl.get('Label')!r}, not {install.LABEL}", fix)
    rc, msg = run("print", install.target())
    if rc != 0:
        first = (msg or "").strip().splitlines()[:1]
        return _c("launchd job", False, f"{plist} is not loaded (launchctl print exit {rc}"
                  + (f": {first[0]}" if first else "") + ")", fix)
    job_data = _env(pl).get(datadir.ENV)
    if data is None or not _same_folder(job_data, data):
        return _c("launchd job", False, f"the job uses the data folder {job_data or 'none'}, not "
                  f"{data or 'this one'}", fix)
    return _c("launchd job", True, f"{install.LABEL} loaded, {plist}")


def _gate(data, now, pl=None) -> dict:
    name = "gate ran recently"
    if data is None:
        return _c(name, False, "no usable data folder", "fix the data folder first (see its line)")
    err_log = pl.get("StandardErrorPath") if isinstance(pl, dict) else None
    if not isinstance(err_log, str) or not err_log:
        err_log = install.err_log_path()
    fix = f"look at {data / 'gate.log'} and {err_log}"
    try:
        last = json.loads((data / "gate.json").read_text(encoding="utf-8")).get("last_run_at")
    except FileNotFoundError:
        return _c(name, False, "never (no gate.json)", fix)
    except (OSError, UnicodeDecodeError, ValueError, AttributeError) as e:
        return _c(name, False, f"cannot read gate.json: {e}", fix)
    try:
        at = cards.parse_stamp(last)
    except (TypeError, ValueError):
        return _c(name, False, f"gate.json has last_run_at {last!r}", fix)
    age = now - at
    if age < -AHEAD:
        return _c(name, False, f"last run {last} is later than now; the clock may have gone back", fix)
    seconds = max(0, int(age.total_seconds()))
    return _c(name, age < FRESH, f"last run {last}, {seconds} s ago", fix)


def _folder(data_flag):
    """(the data folder or None, its check, the loaded config or None)."""
    name = "data folder"
    try:
        data = datadir.resolve(None if data_flag is None else str(data_flag))
    except config.ConfigError as e:
        return None, _c(name, False, str(e), f"pass --data with a folder outside ~/.claude, then run "
                                             f"`ap2 install --data <that folder>`"), None
    install_fix = f"run {_install_cmd(data_flag)} (it creates the folder)"
    if not data.exists():
        return data, _c(name, False, f"{data} does not exist", install_fix), None
    if not data.is_dir():
        return data, _c(name, False, f"{data} is not a folder", f"move it away, then {install_fix}"), None
    if not os.access(data, os.W_OK | os.X_OK):
        return data, _c(name, False, f"{data} is not writable", f"make {data} writable for this user"), None
    try:
        cfg = config.load(data)
    except config.ConfigError as e:
        return data, _c(name, False, str(e), _config_fix(e, data, f"fix {data / config.FILE} (or move it away and run "
                                                                  f"{_install_cmd(data_flag)} for a new one)")), None
    return data, _c(name, True, str(data)), cfg


def _tools(pl, data_flag=None) -> list:
    env = _env(pl)
    if pl is not None and isinstance(env.get("PATH"), str):
        path, where = env["PATH"], "the job's PATH"
    else:
        path, where = os.environ.get("PATH", ""), "the current PATH (no launchd job)"
    out = []
    for t in install.TOOLS:
        found = shutil.which(t, path=path)
        out.append(_c(f"tool {t}", found, f"{found} on {where}" if found else f"not found on {where}",
                      f"install {t} or put its folder on PATH, then run {_install_cmd(data_flag)} again"))
    return out


def _profile(data) -> dict:
    if data is None:
        return _c("active profile", False, "no usable data folder", "fix the data folder first (see its line)")
    try:
        p = profiles.active(data)
    except config.ConfigError as e:
        return _c("active profile", False, str(e), profiles.FIX)
    return _c("active profile", True, f"{p.get('name')} ({p.get('mode')})")


def _mode(data, cfg) -> dict:
    if cfg is not None:
        return _c("mode", True, cfg.mode)
    if data is None:
        return _c("mode", False, "no usable data folder", "fix the data folder first (see its line)")
    try:
        return _c("mode", True, config.load(data).mode)  # a missing folder or file reads as the default
    except config.ConfigError as e:
        return _c("mode", False, str(e), _config_fix(e, data, f"fix {data / config.FILE}"))


def _guard(name, fn, *args):
    try:
        out = fn(*args)
    except Exception as e:  # noqa: BLE001  (doctor reports; it never raises)
        return [_c(name, False, f"the check failed: {type(e).__name__}: {e}", "run `ap2 doctor` again; if it "
                                                                            "repeats, report it")]
    return out if isinstance(out, list) else [out]


def checks(data_flag=None, agents_dir=None, launchctl=None, now=None) -> list:
    run = launchctl or install._launchctl
    now = now or cards.now_utc()
    plist = install.plist_path(agents_dir)
    data, folder, cfg = None, None, None
    try:
        data, folder, cfg = _folder(data_flag)
    except Exception as e:  # noqa: BLE001
        folder = _c("data folder", False, f"the check failed: {type(e).__name__}: {e}", "run `ap2 doctor` again")
    pl, why = None, "not read"
    try:
        pl, why = _read_plist(plist)
    except Exception as e:  # noqa: BLE001
        why = f"cannot read {plist}: {e}"
    out = []
    out += _guard("launchd job", _job, pl, why, data, data_flag, run, plist)
    out += _guard("gate ran recently", _gate, data, now, pl)
    out.append(folder)
    out += _guard("tools", _tools, pl, data_flag)
    out += _guard("active profile", _profile, data)
    out += _guard("mode", _mode, data, cfg)
    return out


def lines(results) -> list:
    """v1's printout: `ok   name: detail` or `FAIL name: detail`, then `     fix: ...` under a failure."""
    out = []
    for c in results:
        out.append(f"{'ok  ' if c['ok'] else 'FAIL'} {c['name']}: {c['detail']}")
        if not c["ok"] and c["fix"]:
            out.append(f"     fix: {c['fix']}")
    return out
