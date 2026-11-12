"""Access profiles: how the dispatcher and workers reach Claude, on the subscription or through a gateway.

A profile is one JSON file in <data>/profiles/. It stores commands that print a key, never a key itself."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from autopilot import cli, config, events, paths

SUBSCRIPTION = "subscription"
FORBIDDEN_SETTINGS = ("permissions", "hooks", "apiKeyHelper")
_CREDENTIAL_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
_CREDENTIAL_NAMES = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")


def _credential_env(env) -> list:
    if not isinstance(env, dict):
        return []
    return [k for k in env if isinstance(k, str)
            and (k.upper() in _CREDENTIAL_NAMES or any(w in k.upper() for w in _CREDENTIAL_WORDS))]
GATEWAY_FIELDS = ("base_url", "key_command", "refresh_command", "rotate_command", "models_command")

# The settings that moved into the subscription profile: (config key, profile field).
_MOVED = [("models.dispatcher", "dispatcher_model"), ("models.worker", "model"),
          ("worker_effort", "effort"), ("reply_check.model", "small_model")]


def _blank(name: str, mode: str = "subscription") -> dict:
    return {"name": name, "mode": mode, "label": name, "model": None, "dispatcher_model": None,
            "small_model": None, "effort": None, "daily_cap_usd": None, "gateway": None, "env": {}, "settings": {}}


def path(name: str) -> Path:
    return paths.data_root() / "profiles" / f"{name}.json"


def _command_output(cmd: str):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)
    if r.returncode != 0:
        return None, f"exit {r.returncode}"
    return r.stdout, ""


def validate(p: dict) -> list:
    errs = []
    name = p.get("name")
    if not paths.valid_name(name if isinstance(name, str) else ""):
        errs.append("name: lowercase letters, digits and dashes, starting with a letter")
    mode = p.get("mode")
    if mode not in ("subscription", "gateway"):
        errs.append("mode: must be subscription or gateway")
    gw = p.get("gateway")
    if mode == "gateway":
        if not isinstance(gw, dict):
            gw = {}
        for f in ("base_url", "key_command"):
            if not gw.get(f):
                errs.append(f"gateway.{f}: required for a gateway profile")
        if gw.get("base_url") and not str(gw["base_url"]).startswith(("https://", "http://")):
            errs.append("gateway.base_url: must be an http(s) URL")
        if not p.get("small_model"):
            errs.append("small_model: required for a gateway profile")
        cap = p.get("daily_cap_usd")
        if isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap <= 0:
            errs.append("daily_cap_usd: required for a gateway profile, a number above 0")
    elif mode == "subscription" and p.get("gateway"):
        errs.append("gateway: a subscription profile has none")
    settings = p.get("settings") or {}
    if not isinstance(settings, dict):
        errs.append("settings: must be an object")
    else:
        for k in FORBIDDEN_SETTINGS:
            if k in settings:
                errs.append(f"settings.{k}: not allowed in a profile"
                            + (" (put the key command in gateway.key_command)" if k == "apiKeyHelper" else ""))
        for k in _credential_env(settings.get("env")):
            errs.append(f"settings.env.{k}: looks like a credential; a profile stores commands, never keys")
    env = p.get("env") or {}
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        errs.append("env: must map names to strings")
    for k in _credential_env(env):
        errs.append(f"env.{k}: looks like a credential; a profile stores commands, never keys")
    return errs


def _model_errors(p: dict) -> list:
    cmd = (p.get("gateway") or {}).get("models_command")
    if not cmd:
        return []
    out, why = _command_output(cmd)
    if out is None:
        return [f"gateway.models_command: failed ({why})"]
    try:
        known = json.loads(out)
        if not isinstance(known, list):
            raise ValueError
    except ValueError:
        return ["gateway.models_command: must print a JSON list of model names"]
    return [f"{f}: {p[f]} is not offered by the gateway" for f in ("model", "dispatcher_model", "small_model")
            if p.get(f) and p[f] not in known]


def save(p: dict) -> None:
    errs = validate(p)
    if not errs:
        errs = _model_errors(p)
    if errs:
        raise ValueError("; ".join(errs))
    target = path(p["name"])
    target.parent.mkdir(parents=True, exist_ok=True)
    config._write(target, p)


def load(name: str) -> dict:
    f = path(name) if paths.valid_name(name or "") else None
    if f is not None and f.exists():
        return {**_blank(name), **json.loads(f.read_text())}
    if name == SUBSCRIPTION:
        return _blank(SUBSCRIPTION)
    raise KeyError(f"no profile named {name!r}")


def names() -> list:
    stored = [f.stem for f in (paths.data_root() / "profiles").glob("*.json")]
    return sorted(set(stored) | {SUBSCRIPTION})


def active() -> dict:
    return load(config.load_global()["active_profile"])


def for_env(env) -> dict:
    """The profile a session runs on: the one its environment names (a worker keeps its own profile after the
    active one changes), else the active profile."""
    name = (env or {}).get("AUTOPILOT_PROFILE")
    if name:
        try:
            return load(name)
        except (KeyError, ValueError, OSError):
            pass
    return active()


def use(name: str) -> None:
    load(name)   # KeyError for an unknown name
    config.set_setting("active_profile", name)
    events.append_global("profile_used", {"profile": name})
    apply()


# ---- settings overlay ----

_BLANKED_ENV = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_API_KEY")


def overlay(p: dict) -> dict:
    """The settings keys a profile owns, never `permissions` or `hooks`. A gateway profile points the session at
    the gateway and names the command that prints its key. A subscription profile blanks every gateway variable,
    so a session bills the subscription even when the shared background daemon carries gateway variables."""
    extra = dict(p.get("settings") or {})
    extra_env = extra.pop("env", None) or {}
    if p.get("mode") == "gateway":
        gw = p["gateway"]
        env = {"ANTHROPIC_BASE_URL": gw["base_url"], "ANTHROPIC_MODEL": p.get("model"),
               "ANTHROPIC_SMALL_FAST_MODEL": p["small_model"], "ANTHROPIC_DEFAULT_HAIKU_MODEL": p["small_model"],
               "ENABLE_TOOL_SEARCH": "true", "AUTOPILOT_PROFILE": p["name"]}
        env = {k: v for k, v in env.items() if v is not None}
        out = {"apiKeyHelper": gw["key_command"]}
    else:
        env = {"AUTOPILOT_PROFILE": p["name"], **{k: "" for k in _BLANKED_ENV}}
        out = {}
    out["env"] = {**env, **(p.get("env") or {}), **extra_env}
    if p.get("mode") == "gateway":
        # A token or key in the shared daemon's env outranks apiKeyHelper, so blank both: the helper's key wins.
        out["env"].update({"ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_API_KEY": ""})
    else:
        out["env"].update({k: "" for k in _BLANKED_ENV})   # the blanks win: no silent fallback to a gateway
    out.update(extra)
    return out


def merge(floor: dict, ov: dict) -> dict:
    """Keep the floor's permissions, hooks and its AUTOPILOT_* variables (except AUTOPILOT_PROFILE); all else
    comes from the overlay. Merging never changes a deny or allow list."""
    out = {k: v for k, v in ov.items() if k not in ("permissions", "hooks", "env")}
    kept = {k: v for k, v in (floor.get("env") or {}).items()
            if k.startswith("AUTOPILOT_") and k != "AUTOPILOT_PROFILE"}
    out["env"] = {**kept, **(ov.get("env") or {})}
    out["permissions"] = floor["permissions"]
    if floor.get("hooks"):
        out["hooks"] = floor["hooks"]
    return out


def page_profile() -> dict:
    """Sessions that use claude.ai tools (a dispatcher pass, a reply check, a page refresh) cannot authenticate
    through a gateway, so they always run on the subscription."""
    return load(SUBSCRIPTION)


def light_settings() -> Path:
    return paths.data_root() / "settings-light.json"


def ensure_light() -> Path:
    """The light settings file, written from the page profile when it is not there yet."""
    f = light_settings()
    if not f.exists():
        from autopilot import perms
        perms._write(f, merge(perms.static_settings("light"), overlay(page_profile())))
    return f


def apply() -> list:
    """Re-merge the overlays into the settings files already written, without rediscovering tools."""
    from autopilot import perms
    root, done = paths.data_root(), []

    def remerge(f: Path, floor: dict, p: dict) -> None:
        perms._write(f, merge(floor, overlay(p)))
        done.append(f)

    act, page = active(), page_profile()
    files = sorted((root / "projects").glob("*/settings.json")) + sorted((root / "projects").glob("*/oneoff-*.json"))
    for f in files:
        remerge(f, json.loads(f.read_text()), act)
    disp = root / "settings-dispatcher.json"
    if disp.exists():
        remerge(disp, json.loads(disp.read_text()), page)
    remerge(light_settings(), perms.static_settings("light"), page)
    return done


def migrate_from_config() -> bool:
    """Copy the model settings that moved into the subscription profile, once. True when it did."""
    if path(SUBSCRIPTION).exists():
        return False
    cfg = config.load_global()
    p = _blank(SUBSCRIPTION)
    for key, field in _MOVED:
        p[field] = config.lookup(cfg, key)
    save(p)
    for key, _ in _MOVED:
        config.reset_setting(key)
    return True


# ---- key health, refresh and rotation ----
# A gateway key reaches a session only through the profile's key command (Claude Code runs it as apiKeyHelper).
# autopilot runs that command to learn whether it works, and throws the output away: no key is ever read,
# logged or stored.

KEY_TTL = timedelta(minutes=10)       # a working key is checked again after this
KEY_FAIL_TTL = timedelta(seconds=60)  # a failing key is checked again sooner: the user may have just fixed it
KEY_FIX = "Run the key command in a terminal (sign in again if it asks), then the next gate tick starts work."
_AUTH_TEXT = re.compile(r"\b40[13]\b|authentication[_ ]error|authentication[_ ]failed|invalid[ _-]*(x-)?api[ _-]*key",
                        re.IGNORECASE)
_AUTH_FIELDS = ("authentication_failed", "authentication_error", "invalid_api_key")


def _key_file() -> Path:
    return paths.data_root() / "profile-key.json"


def _refresh_file() -> Path:
    return paths.data_root() / "profile-refresh.json"


def _read(f: Path) -> dict:
    try:
        d = json.loads(f.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _parse_time(text):
    try:
        at = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo else at.astimezone()


def _fresh(rec: dict, now: datetime, ttl: timedelta = KEY_TTL):
    at = _parse_time(rec.get("at"))
    if at is None:
        return False
    return abs(now.astimezone() - at) < ttl


def _run_silent(cmd: str, timeout: int) -> str:
    """Run a command with its output thrown away. Returns "" on success, else a short reason (never output)."""
    try:
        r = subprocess.run(cmd, shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return type(e).__name__
    return "" if r.returncode == 0 else f"exit {r.returncode}"


def check_key(p: dict, now: datetime):
    """The key command's error text, or None when it works (or the profile is the subscription). The answer is
    cached 10 minutes in <data>/profile-key.json as {at, ok, error}."""
    if p.get("mode") != "gateway":
        return None
    rec = _read(_key_file())
    # A failure found by the key command is re-checked after 60 s; one set by repeated gateway rejections
    # (marked "auth") holds 10 minutes, since the key command itself works.
    ttl = KEY_TTL if rec.get("ok") or rec.get("auth") else KEY_FAIL_TTL
    if rec.get("profile") == p["name"] and _fresh(rec, now, ttl):
        return None if rec.get("ok") else rec.get("error") or "key command failed"
    why = _run_silent(p["gateway"]["key_command"], 60)
    err = f"the key command failed ({why})" if why else None
    config._write(_key_file(), {"at": now.isoformat(), "ok": not err, "error": err, "profile": p["name"]})
    return err


def rotate(name: str) -> None:
    """Run the profile's rotate command (output thrown away), forget the cached key check, re-apply settings."""
    cmd = (load(name).get("gateway") or {}).get("rotate_command")
    if not cmd:
        raise ValueError(f"{name} has no rotate_command")
    why = _run_silent(cmd, 120)
    if why:
        raise RuntimeError(f"rotate_command failed ({why})")   # its output may hold a key, so it is never shown
    _key_file().unlink(missing_ok=True)
    if _read(_refresh_file()).get("profile") == name:
        _refresh_file().unlink(missing_ok=True)
    apply()


def session_auth_failed(session_id: str, after=None) -> bool:
    """True when the end of a session's transcript holds an API authentication failure (HTTP 401/403). Claude
    Code writes an API error as an assistant line flagged isApiErrorMessage (model <synthetic>); the check is
    tolerant: that flag, an auth `error` field, or a synthetic message, and the text must name the failure.
    A failure counts only if its timestamp is newer than `after` (the last refresh) and no normal assistant
    line follows it (the session recovered)."""
    from autopilot import usage
    f = usage.find_transcript(session_id)
    if not f:
        return False
    try:
        with open(f, "rb") as h:
            h.seek(0, 2)
            h.seek(max(0, h.tell() - 65536))
            lines = h.read().decode("utf-8", "replace").splitlines()[-12:]
    except OSError:
        return False
    found = False
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("type") not in ("assistant", "system"):
            continue
        msg = e.get("message") if isinstance(e.get("message"), dict) else {}
        err = e.get("error")
        flagged = (e.get("isApiErrorMessage") is True or msg.get("model") == "<synthetic>"
                   or (isinstance(err, str) and err in _AUTH_FIELDS))
        if not flagged:
            if e.get("type") == "assistant":
                found = False   # a normal reply after the error: the session recovered
            continue
        at = _parse_time(e.get("timestamp")) if e.get("timestamp") else None
        if after is not None and at is not None and at <= after:
            continue   # an old error, from before the last refresh
        content = msg.get("content")
        parts = [c.get("text", "") for c in content if isinstance(c, dict)] if isinstance(content, list) else [str(content or "")]
        text = " ".join(parts + [str(e.get("content") or ""), err if isinstance(err, str) else ""])
        if _AUTH_TEXT.search(text):
            found = True
    return found


def note_session_end(session_id: str, profile_name: str, now: datetime) -> bool:
    """A gateway session ended: when its transcript shows an authentication failure, run the refresh command
    (else the key command) once and re-apply the settings. A second failure within 10 minutes sets the key
    error, which blocks new workers until the key command works again. True when a failure was found."""
    try:
        p = load(profile_name)
    except (KeyError, ValueError, OSError):
        return False
    if p.get("mode") != "gateway":
        return False
    last = _read(_refresh_file())
    if last.get("profile") != p["name"]:
        last = {}
    if not session_auth_failed(session_id, _parse_time(last.get("at"))):
        return False
    if last and _fresh(last, now):
        config._write(_key_file(), {"at": now.isoformat(), "ok": False, "profile": p["name"], "auth": True,
                                    "error": "the gateway rejected the key again right after a refresh"})
        events.append_global("key_error", {"profile": p["name"]})
        return True
    gw = p["gateway"]
    _run_silent(gw.get("refresh_command") or gw["key_command"], 60)
    config._write(_refresh_file(), {"at": now.isoformat(), "profile": p["name"]})
    _key_file().unlink(missing_ok=True)
    events.append_global("key_refreshed", {"profile": p["name"]})
    apply()
    return True


# ---- ap profile ----

def _fail(msg: str) -> int:
    print(f"ap profile: {msg}", file=sys.stderr)
    return 1


def _from_args(a) -> dict:
    p = _blank(a.name, "gateway" if a.gateway else "subscription")
    p.update(label=a.label or a.name, model=a.model, dispatcher_model=a.dispatcher_model,
             small_model=a.small_model, effort=a.effort)
    if a.gateway:
        p["gateway"] = {"base_url": a.base_url, "key_command": a.key_command, "refresh_command": a.refresh_command,
                        "rotate_command": a.rotate_command, "models_command": a.models_command}
        p["daily_cap_usd"] = float(a.daily_cap) if a.daily_cap is not None else None
    for item in a.env:
        k, sep, v = item.partition("=")
        if not sep or not k:
            raise ValueError(f"--env {item!r}: use KEY=VALUE")
        p["env"][k] = v
    if a.settings_file:
        p["settings"] = json.loads(Path(a.settings_file).read_text())
    return p


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ap profile")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for c in ("show", "use", "rotate", "remove"):
        sub.add_parser(c).add_argument("name")
    add = sub.add_parser("add")
    add.add_argument("name")
    mode = add.add_mutually_exclusive_group(required=True)
    mode.add_argument("--subscription", action="store_true")
    mode.add_argument("--gateway", action="store_true")
    for f in ("base-url", "key-command", "refresh-command", "rotate-command", "models-command", "daily-cap",
              "label", "model", "dispatcher-model", "small-model", "effort", "settings-file"):
        add.add_argument("--" + f)
    add.add_argument("--env", action="append", default=[])
    return ap


@cli.command("profile")
def main(argv: list) -> int:
    try:
        a = _parser().parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    if a.cmd == "list":
        cur = config.load_global()["active_profile"]
        for n in names():
            p = load(n)
            print(f"{'*' if n == cur else ' '} {n}\t{p['mode']}\t{p['label']}")
        return 0
    if a.cmd == "add":
        if path(a.name).exists() or a.name == SUBSCRIPTION:
            return _fail(f"{a.name} already exists")
        try:
            save(_from_args(a))
        except (ValueError, OSError) as e:
            return _fail(str(e))
        return 0
    try:
        p = load(a.name)
    except KeyError as e:
        return _fail(e.args[0])
    if a.cmd == "show":
        print(json.dumps(p, indent=2, ensure_ascii=False))
    elif a.cmd == "use":
        use(a.name)
    elif a.cmd == "rotate":
        try:
            rotate(a.name)
        except (ValueError, RuntimeError) as e:
            return _fail(str(e))
        print("rotate_command succeeded; settings re-applied")
    elif a.cmd == "remove":
        if a.name == SUBSCRIPTION:
            return _fail("the subscription profile cannot be removed")
        if a.name == config.load_global()["active_profile"]:
            return _fail(f"{a.name} is the active profile; use another first")
        path(a.name).unlink(missing_ok=True)
    return 0
