"""Access profiles: how the dispatcher and workers reach Claude, on the subscription or through a gateway.

A profile is one JSON file in <data>/profiles/. It stores commands that print a key, never a key itself."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from autopilot import cli, config, events, paths

SUBSCRIPTION = "subscription"
FORBIDDEN_SETTINGS = ("permissions", "hooks")
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
                errs.append(f"settings.{k}: not allowed in a profile")
    env = p.get("env") or {}
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        errs.append("env: must map names to strings")
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


def use(name: str) -> None:
    load(name)   # KeyError for an unknown name
    config.set_setting("active_profile", name)
    events.append_global("profile_used", {"profile": name})


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
        cmd = (p.get("gateway") or {}).get("rotate_command")
        if not cmd:
            return _fail(f"{a.name} has no rotate_command")
        r = subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        print(f"rotate_command exited {r.returncode}")   # its output may hold a key, so it is never shown
        return 0 if r.returncode == 0 else 1
    elif a.cmd == "remove":
        if a.name == SUBSCRIPTION:
            return _fail("the subscription profile cannot be removed")
        if a.name == config.load_global()["active_profile"]:
            return _fail(f"{a.name} is the active profile; use another first")
        path(a.name).unlink(missing_ok=True)
    return 0
