"""Access profiles: how a worker reaches Claude, on the subscription or through a gateway (design section 12,
access profiles). Ported from v1's `lib/autopilot/profile.py`, never imported: v1's version writes v1's data folder.

A profile is one JSON file, `<data>/profiles/<name>.json`, written by code (`store.write_json`). It stores commands
that print a key, never a key. The active profile's name is in `<data>/active-profile.json` (`{"name": ...}`); with
no such file it is the built-in `subscription`, which always exists (a stored `profiles/subscription.json` replaces
it). A missing or broken active profile is refused (ProfileError), never replaced by another one.

`overlay(p)` is the settings a profile owns (never `permissions` or `hooks`); `merge(floor, overlay)` lays it under
a worker's permission floor, which keeps its deny and allow lists, hooks and `AP2_*` variables. Nothing here reads
or writes v1's data folder, and a symlink at `profiles/`, a profile file or `active-profile.json` is refused. A
folder on the way that cannot be read (mode 000) is a one-line ProfileError naming the path, never read as empty.
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
import subprocess
from datetime import datetime
from pathlib import Path

from assistant.config import ConfigError
from assistant.store import write_json

SUBSCRIPTION = "subscription"
FOLDER = "profiles"
ACTIVE = "active-profile.json"
MODES = ("subscription", "gateway")
GATEWAY_FIELDS = ("base_url", "key_command", "refresh_command", "rotate_command", "models_command")
FIELDS = ("name", "mode", "label", "model", "dispatcher_model", "small_model", "effort", "daily_cap_usd", "gateway",
          "env", "settings", "chosen_at")
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
# A profile is often named after a gateway project (`team_project`), so `_` is allowed: unlike a project name
# (`projects.NAME`, which stays as it is). v1's rule, with \Z so a trailing newline never matches.
PROFILE_NAME = re.compile(r"[a-z][a-z0-9_-]{0,39}\Z")
NAME_RULE = "lowercase letters, digits, - and _, starting with a letter (40 at most)"
LONG_CONTEXT = "[1m]"  # Claude Code's marker for the 1M context window; the gateway lists the name without it
# Choosing the model and effort: the first time a profile is set up or used, both are picked from a list fetched
# just then (never cached), and `chosen_at` records when.
EFFORTS = ("low", "medium", "high", "xhigh", "max")
RECOMMENDED_MODEL = "claude-opus-5-5[1m]"
RECOMMENDED_EFFORT = "high"
DEFAULT_MODEL = "default"  # Claude Code's own default model, stored as null
CHOICE_TIMEOUT = 20  # seconds `gateway.models_command` may take when it lists the choices
# The shape of a model name the choices may show (`gpt-x`, `claude-opus-5-5[1m]`, `us.anthropic.claude-x-v1:0`,
# `anthropic/claude-x`): the list is shown in a terminal and in logs, so an entry with a space, a control character or
# any other character a model name never has (a gateway's error text, a terminal control sequence) is dropped from the
# choices, never shown. A bare token shaped like a model name would still be shown: a gateway's model list holds no
# keys. What a save accepts is checked against the full list (`fetch_models`).
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}")
NEEDS_CHOICE = 3  # `ap2 profile`'s exit code when a choice is needed and no terminal can ask for it


class ProfileError(ConfigError):
    """A profile cannot be saved, loaded or used. A ConfigError, so `ap2` prints one line and exits 1."""


class ModelsUnavailable(ProfileError):
    """The model list could not be fetched: the models command failed, timed out or printed no JSON list of texts.
    The message names the exit code or the exception only, never the command's output or its command line."""


def _blank(name: str, mode: str = SUBSCRIPTION) -> dict:
    return {"name": name, "mode": mode, "label": name, "model": None, "dispatcher_model": None, "small_model": None,
            "effort": None, "daily_cap_usd": None, "gateway": None, "env": {}, "settings": {}, "chosen_at": None}


def builtin() -> dict:
    """The built-in subscription profile: today's worker launch (`--effort xhigh`, no `--model`)."""
    return dict(_blank(SUBSCRIPTION), label="Subscription", effort="xhigh")


# -- where things are kept -----------------------------------------------------------------------------------------

def _lstat(path: Path, fix: str = ""):
    """`os.lstat(path)`, or None when nothing is there (a missing file, or a folder on the way that is a file). Any
    other OSError, e.g. a folder on the way with mode 000, is a one-line ProfileError naming the path (and `fix`):
    never "nothing there", so an unreadable folder never reads as empty and nothing falls back."""
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        raise ProfileError(f"cannot read {path}: {e.strerror or type(e).__name__}"
                           + (f"; {fix}" if fix else "")) from e


def _no_link(path: Path, fix: str = "") -> Path:
    st = _lstat(path, fix)  # lstat: a dangling link counts too
    if st is not None and stat.S_ISLNK(st.st_mode):
        raise ProfileError(f"{path} is a symlink; ap2 never follows a link where it keeps its profiles. Remove the "
                           "link and save the profile again")
    return path


def _exists(path: Path) -> bool:
    """A path `_no_link` passed is there. Raises ProfileError when it cannot be looked at (`_lstat`)."""
    return _lstat(path) is not None


def folder(data) -> Path:
    return _no_link(Path(data) / FOLDER)


def path(data, name) -> Path:
    if not (isinstance(name, str) and PROFILE_NAME.match(name)):
        raise ProfileError(f"not a profile name: {name!r} ({NAME_RULE})")
    return _no_link(folder(data) / f"{name}.json")


def active_file(data, fix: str = "") -> Path:
    return _no_link(Path(data) / ACTIVE, fix)


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


def _finite(x) -> bool:
    """`math.isfinite`, but False for an int too big for a float (where `isfinite` raises OverflowError)."""
    try:
        return math.isfinite(x)
    except OverflowError:
        return False


def validate(p) -> list:
    """Every reason `p` is not a usable profile; [] when it is. Runs no command (see `save`)."""
    if not isinstance(p, dict):
        return ["a profile must be a JSON object"]
    errs = []
    name = p.get("name")
    if not (isinstance(name, str) and PROFILE_NAME.match(name)):
        errs.append(f"name: {NAME_RULE}")
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
        if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not _finite(cap) or cap <= 0:
            errs.append("daily_cap_usd: required for a gateway profile, a finite number above 0")
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
    if p.get("chosen_at") is not None and not _iso_time(p["chosen_at"]):
        errs.append("chosen_at: must be an ISO time or null")
    return errs


def _iso_time(v) -> bool:
    if not isinstance(v, str):
        return False
    try:
        datetime.fromisoformat(v)
    except ValueError:
        return False
    return True


def base_model(name: str) -> str:
    """`name` without one trailing `[1m]` (LONG_CONTEXT); any other name as it is."""
    return name[:-len(LONG_CONTEXT)] if name.endswith(LONG_CONTEXT) else name


def _listed(cmd: str, timeout: int) -> list:
    """The JSON list of model names `cmd` prints. Raises ModelsUnavailable naming the exit code or the exception
    only: an error never shows the command's output (it may hold more than model names) or its command line. A
    command line Python cannot hand to the shell (a NUL byte, a lone surrogate: a ValueError) fails the same way.
    The list itself is only parsed here; `model_choices` shows only its entries shaped like a model name."""
    try:
        r = subprocess.run(cmd, shell=True, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ModelsUnavailable(f"gateway.models_command: failed (no answer within {timeout} s)") from None
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        raise ModelsUnavailable(f"gateway.models_command: failed ({type(e).__name__})") from None
    if r.returncode != 0:
        raise ModelsUnavailable(f"gateway.models_command: failed (exit {r.returncode})")
    try:
        known = json.loads(r.stdout)
    except (ValueError, RecursionError):
        known = None
    if not (isinstance(known, list) and all(isinstance(m, str) for m in known)):
        raise ModelsUnavailable("gateway.models_command: must print a JSON list of model names")
    return known


def _model_errors(p: dict, known: list = None) -> list:
    """The models the profile names that `gateway.models_command` does not list. A name ending in `[1m]` counts as
    listed when its base name is; it is stored and passed with the marker. `known`: the list fetched for this same
    choice just before (the full list of `fetch_models`), checked instead, so the command does not run again."""
    cmd = (p.get("gateway") or {}).get("models_command")
    if not cmd:
        return []
    if known is None:
        try:
            known = _listed(cmd, MODELS_TIMEOUT)
        except ModelsUnavailable as e:
            return [str(e)]
    return [f"{f}: {p[f]} is not offered by the gateway" for f in ("model", "dispatcher_model", "small_model")
            if p.get(f) and p[f] not in known and base_model(p[f]) not in known]


# -- the store -------------------------------------------------------------------------------------------------------

def save(data, p: dict, known: list = None) -> dict:
    """Validate `p` (and its models against `gateway.models_command`, or against `known`, the list fetched for this
    same choice just before, with no command run), then write it. Raises ProfileError with every reason."""
    p = {**_blank(p.get("name") if isinstance(p, dict) else ""), **(p if isinstance(p, dict) else {})}
    errs = validate(p) or _model_errors(p, known)
    if errs:
        raise ProfileError("; ".join(errs))
    target = path(data, p["name"])
    try:
        write_json(target, {k: p.get(k) for k in FIELDS})
    except OSError as e:
        raise ProfileError(f"cannot write {target}: {e}") from e
    return load(data, p["name"])


def load(data, name) -> dict:
    """The profile `name`, checked. Raises ProfileError when it is unknown, unreadable or not valid."""
    f = path(data, name)
    if not _exists(f):  # an unreadable folder raises here: never the built-in subscription
        if name == SUBSCRIPTION:
            return builtin()
        raise ProfileError(f"no profile named {name!r} ({f} does not exist)")
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
    try:
        entries = os.listdir(d)  # not glob(): it reads an unreadable folder as empty
    except (FileNotFoundError, NotADirectoryError):
        entries = []
    except OSError as e:
        raise ProfileError(f"cannot read {d}: {e.strerror or type(e).__name__}") from e
    stored = [n[:-len(".json")] for n in entries if n.endswith(".json") and PROFILE_NAME.match(n[:-len(".json")])]
    return sorted(set(stored) | {SUBSCRIPTION})


def active_name(data) -> str:
    """The active profile's name (`subscription` with no active-profile.json). Raises ProfileError when the file
    cannot be read or names no valid name."""
    f = active_file(data, FIX)
    if _lstat(f, FIX) is None:  # an unreadable data folder raises here: never `subscription`
        return SUBSCRIPTION
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise ProfileError(f"{f}: cannot read: {e}; {FIX}") from e
    name = raw.get("name") if isinstance(raw, dict) else None
    if not (isinstance(name, str) and PROFILE_NAME.match(name)):
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
    try:
        write_json(f, {"name": name})
    except OSError as e:
        raise ProfileError(f"cannot write {f}: {e}") from e
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
    """Delete a stored profile. The subscription and the active profile cannot be removed, and nothing is removed
    while active-profile.json is there but holds no name at all (unreadable, bad JSON, no string `name`): the active
    one is then unknown."""
    f = path(data, name)
    if name == SUBSCRIPTION:
        raise ProfileError("the subscription profile cannot be removed")
    try:
        current = active_name(data)
    except ProfileError as e:
        af = active_file(data)
        current = _raw_active(af)
        if current is None and _exists(af):  # unreadable, bad JSON, no name: it may be `name`, so fail closed
            raise ProfileError(f"cannot remove {name!r}: the active profile is unknown, since {e}") from None
    if name == current:
        raise ProfileError(f"{name} is the active profile; `ap2 profile use` another one first")
    if not _exists(f):
        raise ProfileError(f"no profile named {name!r}")
    try:
        f.unlink()
    except OSError as e:
        raise ProfileError(f"cannot remove {f}: {e}") from e
    return {"removed": name}


def page_profile(data) -> dict:
    """Sessions that use claude.ai tools (the dispatcher, the page-answer read, the page refresh) cannot sign in
    through a gateway, so they always run on the subscription (design section 12, access profiles)."""
    return load(data, SUBSCRIPTION)


# -- choosing the model and effort -----------------------------------------------------------------------------------
# Read only by `ap2 profile`: a worker start never asks, it runs the active profile as it is stored.

def now() -> str:
    """The time a choice is made: local time with its UTC offset, to the second (ISO 8601)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def needs_choice(data, p: dict) -> bool:
    """True when `p`'s model and effort were never chosen (no `chosen_at`). The subscription never needs a choice,
    built in or stored: v2 has no migration, and a subscription-only user is never asked. `data` is unused today; it
    keeps the call the same as the other profile functions."""
    return p.get("name") != SUBSCRIPTION and not p.get("chosen_at")


def _with_long(names) -> list:
    """Every claude-* model but haiku gets its 1M context variant first, then the plain name; others as they are.
    No name twice."""
    out = []
    for m in names:
        base = base_model(m)
        pair = ([base + LONG_CONTEXT, base] if base.startswith("claude-") and not base.startswith("claude-haiku-")
                else [m])
        out += [x for x in pair if x not in out]
    return out


def fetch_models(data, p: dict) -> tuple:
    """`(shown, known)` for `p`, fetched now on every call (never cached), with the models command run once.
    `shown`: the models `p` may be chosen to run, as `model_choices` returns them. `known`: what a save checks the
    profile's models against (`save(data, p, known)`): every entry the models command printed, with the `[1m]`
    variant and the plain name of each claude-* entry but haiku added (as `_with_long` does for `shown`), none
    dropped, so a listed small model of another shape still saves. With no models command both are the same list.
    Raises ModelsUnavailable when the command fails, and SpendError (a ConfigError) when the price override is
    broken."""
    cmd = (p.get("gateway") or {}).get("models_command") if p.get("mode") == "gateway" else None
    if cmd:
        listed = _listed(cmd, CHOICE_TIMEOUT)
        return _with_long(m for m in listed if MODEL_NAME.fullmatch(m)), _with_long(listed)
    from assistant import spend  # imported here: spend reads transcripts, which a profile load never needs
    shown = _with_long(m for m in spend.prices(data) if m.startswith("claude-")) + [DEFAULT_MODEL]
    return shown, shown


def model_choices(data, p: dict) -> list:
    """The models `p` may be chosen to run, fetched now on every call (never cached). A gateway with a models
    command: the entries of the list it prints that are shaped like a model name (`MODEL_NAME`); any other entry is
    dropped, never shown. Otherwise (the subscription, or a gateway with no models command) the priced claude-*
    models of `spend.prices(data)`, then `default`. Raises as `fetch_models` does."""
    return fetch_models(data, p)[0]


def recommended_model(models: list):
    return RECOMMENDED_MODEL if RECOMMENDED_MODEL in models else (models[0] if models else None)


def choices(data, p: dict, models: list = None) -> dict:
    """What `p` can choose from, fetched now (or `models`, fetched by the caller just before). Raises
    ModelsUnavailable."""
    models = model_choices(data, p) if models is None else models
    return {"profile": p["name"], "models": models, "efforts": list(EFFORTS),
            "recommended_model": recommended_model(models), "recommended_effort": RECOMMENDED_EFFORT}


def check_effort(effort) -> None:
    if effort not in EFFORTS:
        raise ProfileError(f"effort {effort!r} is not offered; choose one of: {', '.join(EFFORTS)}")


def check_model(model, models: list) -> None:
    if model not in models:
        raise ProfileError(f"model {model!r} is not offered; choose one of: {', '.join(models)}")


def choose(data, name: str, model: str, effort: str, models: list = None, known: list = None) -> dict:
    """Save `name`'s model and effort, and mark it chosen. The model is checked against the list fetched now (or
    `models`, fetched by the caller for this same choice); the saved file against the full list of the same fetch
    (`known`, which defaults to `models`), so one choice runs the models command once. `default` is stored as a null
    model. Nothing is written on a refusal. Returns the saved profile."""
    p = load(data, name)
    if models is None:
        models, known = fetch_models(data, p)
    check_model(model, models)
    check_effort(effort)
    p.update(model=None if model == DEFAULT_MODEL else model, effort=effort, chosen_at=now())
    return save(data, p, models if known is None else known)


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
    if a.model == DEFAULT_MODEL:  # Claude Code's own default model, stored as null
        p["model"] = None
    if a.model and a.effort:  # both given: chosen at creation (`ap2 profile add` checks them against the list)
        p["chosen_at"] = now()
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
