"""Access profiles: how a worker reaches Claude, on the subscription or through a gateway (design section 12,
access profiles). Ported from v1's `lib/autopilot/profile.py`, never imported: v1's version writes v1's data folder.

A profile is one JSON file, `<data>/profiles/<name>.json`, written by code (`store.write_json`). It stores commands
that print a key, never a key. The active profile's name is in `<data>/active-profile.json` (`{"name": ...}`); with
no such file it is the built-in `subscription`, which always exists (a stored `profiles/subscription.json` replaces
it). A missing or broken active profile is refused (ProfileError), never replaced by another one.

`overlay(p)` is the settings a profile owns (never `permissions` or `hooks`); `merge(floor, overlay)` lays it under
a worker's permission floor, which keeps its deny and allow lists, hooks and `AP2_*` variables. Nothing here reads
or writes v1's data folder, and a symlink at `profiles/`, a profile file or `active-profile.json` is refused.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from assistant.config import ConfigError
from assistant.projects import NAME
from assistant.store import write_json

SUBSCRIPTION = "subscription"
FOLDER = "profiles"
ACTIVE = "active-profile.json"
MODES = ("subscription", "gateway")
GATEWAY_FIELDS = ("base_url", "key_command", "refresh_command", "rotate_command", "models_command")
FIELDS = ("name", "mode", "label", "model", "dispatcher_model", "small_model", "effort", "daily_cap_usd", "gateway",
          "env", "settings")
FORBIDDEN_SETTINGS = ("permissions", "hooks", "apiKeyHelper", "disableAllHooks")
MODELS_TIMEOUT = 30  # seconds `gateway.models_command` may take
FIX = "run `ap2 profile list`, then `ap2 profile use <name>` with a profile that loads"
_CREDENTIAL_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
_CREDENTIAL_NAMES = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
# Blanked on the subscription (and these blanks win), so a session bills the subscription even when the shared
# background daemon carries gateway variables.
_BLANKED_ENV = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_API_KEY")
# A cloud-provider switch in the daemon's env would send any session to that provider instead: blanked in both modes.
_PROVIDER_ENV = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
_OWN_PREFIXES = ("AP2_", "AUTOPILOT_")  # variables v2 (and v1) set themselves


class ProfileError(ConfigError):
    """A profile cannot be saved, loaded or used. A ConfigError, so `ap2` prints one line and exits 1."""


def _blank(name: str, mode: str = SUBSCRIPTION) -> dict:
    return {"name": name, "mode": mode, "label": name, "model": None, "dispatcher_model": None, "small_model": None,
            "effort": None, "daily_cap_usd": None, "gateway": None, "env": {}, "settings": {}}


def builtin() -> dict:
    """The built-in subscription profile: today's worker launch (`--effort xhigh`, no `--model`)."""
    return dict(_blank(SUBSCRIPTION), label="Subscription", effort="xhigh")


# -- where things are kept -----------------------------------------------------------------------------------------

def _no_link(path: Path) -> Path:
    if path.is_symlink():  # lstat: a dangling link counts too
        raise ProfileError(f"{path} is a symlink; ap2 never follows a link where it keeps its profiles. Remove the "
                           "link and save the profile again")
    return path


def folder(data) -> Path:
    return _no_link(Path(data) / FOLDER)


def path(data, name) -> Path:
    if not (isinstance(name, str) and NAME.match(name)):
        raise ProfileError(f"not a profile name: {name!r} (lowercase letters, digits and -, starting with a letter)")
    return _no_link(folder(data) / f"{name}.json")


def active_file(data) -> Path:
    return _no_link(Path(data) / ACTIVE)


# -- validation ------------------------------------------------------------------------------------------------------

def _credential_env(env) -> list:
    if not isinstance(env, dict):
        return []
    return [k for k in env if isinstance(k, str)
            and (k.upper() in _CREDENTIAL_NAMES or any(w in k.upper() for w in _CREDENTIAL_WORDS))]


def _owned_env(env) -> list:
    """Variables ap2 itself sets in a worker's settings: a profile may not set them. [(name, why)]"""
    if not isinstance(env, dict):
        return []
    out = []
    for k in env:
        if not isinstance(k, str):
            continue
        if k.upper().startswith(_OWN_PREFIXES):
            out.append((k, "ap2 sets its own AP2_ variables (and AUTOPILOT_ is v1's)"))
        elif k.upper() in _BLANKED_ENV + _PROVIDER_ENV and k.upper() not in _CREDENTIAL_NAMES:
            out.append((k, "set by the profile's own fields (gateway.base_url, model, small_model), or blanked"))
    return out


def validate(p) -> list:
    """Every reason `p` is not a usable profile; [] when it is. Runs no command (see `save`)."""
    if not isinstance(p, dict):
        return ["a profile must be a JSON object"]
    errs = []
    name = p.get("name")
    if not (isinstance(name, str) and NAME.match(name)):
        errs.append("name: lowercase letters, digits and -, starting with a letter")
    mode = p.get("mode")
    if mode not in MODES:
        errs.append("mode: must be subscription or gateway")
    for f in ("label", "model", "dispatcher_model", "small_model", "effort"):
        if p.get(f) is not None and not (isinstance(p[f], str) and p[f]):
            errs.append(f"{f}: must be a non-empty text or null")
    gw = p.get("gateway")
    if mode == "gateway":
        if not isinstance(gw, dict):
            gw = {}
        for f in ("base_url", "key_command"):
            if not gw.get(f):
                errs.append(f"gateway.{f}: required for a gateway profile")
        for f in GATEWAY_FIELDS:
            if gw.get(f) is not None and not isinstance(gw[f], str):
                errs.append(f"gateway.{f}: must be a text")
        if isinstance(gw.get("base_url"), str) and gw["base_url"] and not gw["base_url"].startswith(
                ("https://", "http://")):
            errs.append("gateway.base_url: must be an http(s) URL")
        if not p.get("small_model"):
            errs.append("small_model: required for a gateway profile")
        cap = p.get("daily_cap_usd")
        if isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap <= 0:
            errs.append("daily_cap_usd: required for a gateway profile, a number above 0")
    elif mode == "subscription" and gw:
        errs.append("gateway: a subscription profile has none")
    settings = p.get("settings") if p.get("settings") is not None else {}
    if not isinstance(settings, dict):
        errs.append("settings: must be an object")
    else:
        for k in FORBIDDEN_SETTINGS:
            if k in settings:
                errs.append(f"settings.{k}: not allowed in a profile"
                            + (" (put the key command in gateway.key_command)" if k == "apiKeyHelper" else ""))
        senv = settings.get("env")
        if senv is not None and not (isinstance(senv, dict)
                                     and all(isinstance(k, str) and isinstance(v, str) for k, v in senv.items())):
            errs.append("settings.env: must map names to texts")
        for k in _credential_env(senv):
            errs.append(f"settings.env.{k}: looks like a credential; a profile stores commands, never keys")
        errs += [f"settings.env.{k}: {why}" for k, why in _owned_env(senv)]
    env = p.get("env") if p.get("env") is not None else {}
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        errs.append("env: must map names to texts")
    for k in _credential_env(env):
        errs.append(f"env.{k}: looks like a credential; a profile stores commands, never keys")
    errs += [f"env.{k}: {why}" for k, why in _owned_env(env)]
    return errs


def _model_errors(p: dict) -> list:
    """The models the profile names that `gateway.models_command` does not list. The command's output is only
    parsed, never shown: it may hold more than model names."""
    cmd = (p.get("gateway") or {}).get("models_command")
    if not cmd:
        return []
    try:
        r = subprocess.run(cmd, shell=True, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=MODELS_TIMEOUT)
    except subprocess.TimeoutExpired:
        return [f"gateway.models_command: failed (no answer within {MODELS_TIMEOUT} s)"]
    except (OSError, subprocess.SubprocessError) as e:
        return [f"gateway.models_command: failed ({type(e).__name__})"]
    if r.returncode != 0:
        return [f"gateway.models_command: failed (exit {r.returncode})"]
    try:
        known = json.loads(r.stdout)
    except ValueError:
        known = None
    if not isinstance(known, list):
        return ["gateway.models_command: must print a JSON list of model names"]
    return [f"{f}: {p[f]} is not offered by the gateway" for f in ("model", "dispatcher_model", "small_model")
            if p.get(f) and p[f] not in known]


# -- the store -------------------------------------------------------------------------------------------------------

def save(data, p: dict) -> dict:
    """Validate `p` (and its models against `gateway.models_command`), then write it. Raises ProfileError with
    every reason."""
    p = {**_blank(p.get("name") if isinstance(p, dict) else ""), **(p if isinstance(p, dict) else {})}
    errs = validate(p) or _model_errors(p)
    if errs:
        raise ProfileError("; ".join(errs))
    target = path(data, p["name"])
    write_json(target, {k: p.get(k) for k in FIELDS})
    return load(data, p["name"])


def load(data, name) -> dict:
    """The profile `name`, checked. Raises ProfileError when it is unknown, unreadable or not valid."""
    f = path(data, name)
    if not f.exists():
        if name == SUBSCRIPTION:
            return builtin()
        raise ProfileError(f"no profile named {name!r}")
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise ProfileError(f"{f}: cannot read: {e}") from e
    if not isinstance(raw, dict):
        raise ProfileError(f"{f}: must hold a JSON object")
    p = {**_blank(name), **raw}
    if p.get("name") != name:
        raise ProfileError(f"{f}: its name {p.get('name')!r} is not {name!r}")
    errs = validate(p)
    if errs:
        raise ProfileError(f"{f}: {'; '.join(errs)}")
    return p


def names(data) -> list:
    """Every profile name, the built-in subscription included, sorted."""
    d = folder(data)
    stored = [f.stem for f in d.glob("*.json") if NAME.match(f.stem)] if d.is_dir() else []
    return sorted(set(stored) | {SUBSCRIPTION})


def active_name(data) -> str:
    """The active profile's name (`subscription` with no active-profile.json). Raises ProfileError when the file
    cannot be read or names no valid name."""
    f = active_file(data)
    if not f.exists():
        return SUBSCRIPTION
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise ProfileError(f"{f}: cannot read: {e}; {FIX}") from e
    name = raw.get("name") if isinstance(raw, dict) else None
    if not (isinstance(name, str) and NAME.match(name)):
        raise ProfileError(f"{f}: must be {{\"name\": <profile name>}}; {FIX}")
    return name


def active(data) -> dict:
    """The active profile. Raises ProfileError naming the file and the fix when it cannot be loaded: no worker may
    then start, and nothing falls back to another profile."""
    name = active_name(data)
    try:
        return load(data, name)
    except ProfileError as e:
        raise ProfileError(f"the active profile {name!r} cannot be loaded ({e}); {FIX}") from None


def use(data, name: str) -> dict:
    """Make `name` the active profile. No settings file is rewritten: running workers keep their profile, and the
    next start or resume takes this one. Returns {"active": name, "was": the old name, or None when unreadable}."""
    load(data, name)  # unknown or broken: refused, nothing written
    f = active_file(data)  # a link is refused here, before anything is read
    try:
        was = active_name(data)
    except ProfileError:
        was = _raw_active(f)  # a broken file: say what it held, when anything
    write_json(f, {"name": name})
    return {"active": name, "was": was}


def _raw_active(f: Path):
    """The name a broken active-profile.json still holds, when any (so `use` can say what it replaced)."""
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    name = raw.get("name") if isinstance(raw, dict) else None
    return name if isinstance(name, str) else None


def remove(data, name: str) -> dict:
    """Delete a stored profile. The subscription and the active profile cannot be removed."""
    f = path(data, name)
    if name == SUBSCRIPTION:
        raise ProfileError("the subscription profile cannot be removed")
    try:
        current = active_name(data)
    except ProfileError:
        current = _raw_active(active_file(data))
    if name == current:
        raise ProfileError(f"{name} is the active profile; `ap2 profile use` another one first")
    if not f.exists():
        raise ProfileError(f"no profile named {name!r}")
    f.unlink()
    return {"removed": name}


def page_profile(data) -> dict:
    """Sessions that use claude.ai tools (the dispatcher, the page-answer read, the page refresh) cannot sign in
    through a gateway, so they always run on the subscription (design section 12, access profiles)."""
    return load(data, SUBSCRIPTION)


# -- the settings overlay -------------------------------------------------------------------------------------------

def overlay(p: dict) -> dict:
    """The settings keys a profile owns, never `permissions` or `hooks`. A gateway profile points the session at the
    gateway and names the command that prints its key (`apiKeyHelper`). A subscription profile blanks every gateway
    variable, and those blanks win over the profile's own env."""
    extra = dict(p.get("settings") or {})
    extra_env = extra.pop("env", None) or {}
    for k in ("permissions", "hooks"):
        extra.pop(k, None)
    if p.get("mode") == "gateway":
        gw = p["gateway"]
        # No model of its own: blank the variable, so a model name in the daemon's env cannot reach the gateway.
        env = {"ANTHROPIC_BASE_URL": gw["base_url"], "ANTHROPIC_MODEL": p.get("model") or "",
               "ANTHROPIC_SMALL_FAST_MODEL": p["small_model"], "ANTHROPIC_DEFAULT_HAIKU_MODEL": p["small_model"],
               "ENABLE_TOOL_SEARCH": "true", "AP2_PROFILE": p["name"]}
        out = {"apiKeyHelper": gw["key_command"]}
    else:
        env = {"AP2_PROFILE": p["name"], **{k: "" for k in _BLANKED_ENV}}
        out = {}
    out["env"] = {**env, **(p.get("env") or {}), **extra_env}
    if p.get("mode") == "gateway":
        # A token or key in the shared daemon's env outranks apiKeyHelper, so blank both: the helper's key wins.
        out["env"].update({"ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_API_KEY": ""})
    else:
        out["env"].update({k: "" for k in _BLANKED_ENV})  # the blanks win: no silent fallback to a gateway
    out["env"].update({k: "" for k in _PROVIDER_ENV})
    out["env"]["AP2_PROFILE"] = p["name"]
    out.update({k: v for k, v in extra.items() if k != "apiKeyHelper"})
    return out


def merge(floor: dict, ov: dict) -> dict:
    """Keep the floor's permissions, hooks and its AP2_* variables (except AP2_PROFILE), which win over the overlay;
    all else comes from the overlay. Merging never changes a deny or allow list."""
    out = {k: v for k, v in ov.items() if k not in ("permissions", "hooks", "env")}
    kept = {k: v for k, v in (floor.get("env") or {}).items() if k.startswith("AP2_") and k != "AP2_PROFILE"}
    out["env"] = {**(ov.get("env") or {}), **kept}  # the floor's role, card and data folder win
    out["permissions"] = floor["permissions"]
    if floor.get("hooks"):
        out["hooks"] = floor["hooks"]
    return out


# -- ap2 profile add ------------------------------------------------------------------------------------------------

def from_args(a) -> dict:
    """The profile `ap2 profile add` describes (argparse namespace `a`). Raises ProfileError on a bad flag value."""
    p = _blank(a.name, "gateway" if a.gateway else SUBSCRIPTION)
    p.update(label=a.label or a.name, model=a.model, dispatcher_model=a.dispatcher_model,
             small_model=a.small_model, effort=a.effort)
    if a.gateway:
        p["gateway"] = {"base_url": a.base_url, "key_command": a.key_command, "refresh_command": a.refresh_command,
                        "rotate_command": a.rotate_command, "models_command": a.models_command}
        if a.daily_cap is not None:
            try:
                p["daily_cap_usd"] = float(a.daily_cap)
            except ValueError:
                raise ProfileError(f"--daily-cap {a.daily_cap!r}: not a number") from None
    elif any(getattr(a, f) for f in ("base_url", "key_command", "refresh_command", "rotate_command",
                                     "models_command", "daily_cap")):
        raise ProfileError("--base-url, --key-command, --refresh-command, --rotate-command, --models-command and "
                           "--daily-cap are for a --gateway profile")
    for item in a.env or []:
        k, sep, v = item.partition("=")
        if not sep or not k:
            raise ProfileError(f"--env {item!r}: use KEY=VALUE")
        p["env"][k] = v
    if a.settings_file:
        try:
            p["settings"] = json.loads(Path(a.settings_file).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as e:
            raise ProfileError(f"--settings-file {a.settings_file!r}: cannot read: {e}") from e
    return p
