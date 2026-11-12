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
FORBIDDEN_SETTINGS = ("permissions", "hooks", "apiKeyHelper", "disableAllHooks")
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


# A profile is often named after a gateway project, so underscores are allowed (unlike autopilot names).
_PROFILE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")


def path(name: str) -> Path:
    return paths.data_root() / "profiles" / f"{name}.json"


def _command_output(cmd: str, timeout: int = 30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout} s"
    except (OSError, subprocess.SubprocessError) as e:
        return None, type(e).__name__   # never the command line: the page shows this text
    if r.returncode != 0:
        return None, f"exit {r.returncode}"
    return r.stdout, ""


def validate(p: dict) -> list:
    errs = []
    name = p.get("name")
    if not _PROFILE_NAME.match(name if isinstance(name, str) else ""):
        errs.append("name: lowercase letters, digits, dashes and underscores, starting with a letter (40 at most)")
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
        errs += [f"settings.env.{k}: {why}" for k, why in _owned_env(settings.get("env"))]
    env = p.get("env") or {}
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        errs.append("env: must map names to strings")
    for k in _credential_env(env):
        errs.append(f"env.{k}: looks like a credential; a profile stores commands, never keys")
    errs += [f"env.{k}: {why}" for k, why in _owned_env(env)]
    return errs


def _owned_env(env) -> list:
    """Variables autopilot itself sets in a profile's settings: a profile may not set them. [(name, why)]"""
    if not isinstance(env, dict):
        return []
    out = []
    for k in env:
        if not isinstance(k, str):
            continue
        if k.upper().startswith("AUTOPILOT_"):
            out.append((k, "autopilot sets its own AUTOPILOT_ variables"))
        elif k.upper() in _BLANKED_ENV + _PROVIDER_ENV and k.upper() not in _CREDENTIAL_NAMES:
            out.append((k, "set by the profile's own fields (gateway.base_url, model, small_model), or blanked"))
    return out


LONG_CONTEXT = "[1m]"   # Claude Code's marker for the 1M context window; it strips it before sending


def _base_model(name: str) -> str:
    return name[:-len(LONG_CONTEXT)] if name.endswith(LONG_CONTEXT) else name


class ModelsUnavailable(RuntimeError):
    """The model list could not be fetched: the models command failed or printed no JSON list."""


def _listed(cmd: str, timeout: int = 30) -> list:
    out, why = _command_output(cmd, timeout)
    if out is None:
        raise ModelsUnavailable(f"gateway.models_command: failed ({why})")
    try:
        known = json.loads(out)
        if not isinstance(known, list) or not all(isinstance(m, str) for m in known):
            raise ValueError
    except ValueError:
        raise ModelsUnavailable("gateway.models_command: must print a JSON list of model names") from None
    return known


def _model_errors(p: dict, known: list = None) -> list:
    """The model fields the gateway does not offer. `known`: a list fetched for this same check just before, so
    the models command is not run again."""
    cmd = (p.get("gateway") or {}).get("models_command")
    if not cmd:
        return []
    if known is None:
        try:
            known = _listed(cmd)
        except ModelsUnavailable as e:
            return [str(e)]
    return [f"{f}: {p[f]} is not offered by the gateway" for f in ("model", "dispatcher_model", "small_model")
            if p.get(f) and p[f] not in known and _base_model(p[f]) not in known]


def save(p: dict, known: list = None) -> None:
    """Validate and write a profile. `known`: the model list fetched just before (see _model_errors)."""
    errs = validate(p)
    if not errs:
        errs = _model_errors(p, known)
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


def active_or_error():
    """(the active profile, None), or (a stand-in, the reason) when it cannot be loaded: its file was removed
    or is broken. The stand-in has no mode, so it is never taken for the subscription or a gateway."""
    name = config.load_global().get("active_profile")
    try:
        return load(name), None
    except (KeyError, ValueError, OSError, TypeError) as e:
        why = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
        return {**_blank(str(name)), "mode": None}, f"the active profile {name!r} cannot be loaded: {why}"


PROFILE_FIX = "Run `ap profile list`, then `ap profile use <name>` with a profile that loads."


def use(name: str) -> None:
    """Make `name` the active profile: rewrite every settings file for it first, and only then record it. If
    the files cannot be written, the active profile stays what it was and the error is raised."""
    p = load(name)   # KeyError for an unknown name
    apply(p)
    config.set_setting("active_profile", name)
    events.append_global("profile_used", {"profile": name})


def file_profile(settings) -> str:
    """The profile a settings file was written for (its env.AUTOPILOT_PROFILE), "" when it names none."""
    try:
        env = json.loads(Path(settings).read_text()).get("env") or {}
    except (OSError, ValueError, AttributeError):
        return ""
    v = env.get("AUTOPILOT_PROFILE") if isinstance(env, dict) else None
    return v if isinstance(v, str) else ""


def launch_profile(settings, expected: str = None) -> dict:
    """The profile a session started with `settings` runs on, read from the file itself. It must be `expected`
    (default: the active profile); when it is not, the file is out of date and nothing may start on it: an
    event is logged and RuntimeError says how to fix it."""
    act, err = active_or_error()
    if expected is None and err:
        raise RuntimeError(f"{err}. {PROFILE_FIX}")
    want = expected or act["name"]
    got = file_profile(settings)
    if got != want:
        events.append_global("settings_out_of_date", {"file": str(settings), "file_profile": got or None,
                                                      "expected": want})
        raise RuntimeError(f"{settings} was written for profile {got or '(none)'!r}, not {want!r}: the settings are "
                           f"out of date; run `ap profile use {act['name'] if not err else '<name>'}` to rewrite them")
    return load(want)


# ---- settings overlay ----

_BLANKED_ENV = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_API_KEY")
# A cloud-provider switch in the daemon's env would send any session to that provider instead: blanked in both modes.
_PROVIDER_ENV = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")


def overlay(p: dict) -> dict:
    """The settings keys a profile owns, never `permissions` or `hooks`. A gateway profile points the session at
    the gateway and names the command that prints its key. A subscription profile blanks every gateway variable,
    so a session bills the subscription even when the shared background daemon carries gateway variables."""
    extra = dict(p.get("settings") or {})
    extra_env = extra.pop("env", None) or {}
    if p.get("mode") == "gateway":
        gw = p["gateway"]
        # No model of its own: blank the variable, so a model name in the daemon's env cannot reach the gateway.
        env = {"ANTHROPIC_BASE_URL": gw["base_url"], "ANTHROPIC_MODEL": p.get("model") or "",
               "ANTHROPIC_SMALL_FAST_MODEL": p["small_model"], "ANTHROPIC_DEFAULT_HAIKU_MODEL": p["small_model"],
               "ENABLE_TOOL_SEARCH": "true", "AUTOPILOT_PROFILE": p["name"]}
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
    out["env"].update({k: "" for k in _PROVIDER_ENV})
    out.update(extra)
    return out


def merge(floor: dict, ov: dict) -> dict:
    """Keep the floor's permissions, hooks and its AUTOPILOT_* variables (except AUTOPILOT_PROFILE), which win
    over the overlay; all else comes from the overlay. Merging never changes a deny or allow list."""
    out = {k: v for k, v in ov.items() if k not in ("permissions", "hooks", "env")}
    kept = {k: v for k, v in (floor.get("env") or {}).items()
            if k.startswith("AUTOPILOT_") and k != "AUTOPILOT_PROFILE"}
    out["env"] = {**(ov.get("env") or {}), **kept}   # the floor's role and data folder win
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
    """The light settings file, written from the page profile when it is not there yet or was written for another
    profile. Its floor is fixed (no discovery), so rewriting it is always safe."""
    f = light_settings()
    if file_profile(f) != SUBSCRIPTION:
        from autopilot import perms
        perms._write(f, merge(perms.static_settings("light"), overlay(page_profile())))
    return f


class SettingsUnreadable(RuntimeError):
    """apply() could not read or merge some settings files. `done` are the files it rewrote, `skipped` the
    (path, exception class) pairs it left alone."""

    def __init__(self, done: list, skipped: list):
        self.done, self.skipped = done, skipped
        names = ", ".join(str(f) for f, _ in skipped)
        super().__init__(f"could not read or merge these settings files: {names}. "
                         "Fix or delete them, then run `ap perms write <ap>` for each autopilot.")


def apply(act: dict = None) -> list:
    """Re-merge the overlays into the settings files already written, without rediscovering tools: the page files
    (dispatcher, light) for the subscription first, then worker and one-off files for `act` (default the active
    profile). A file that cannot be read or merged is skipped and logged (settings_unreadable); the rest are still
    rewritten, and SettingsUnreadable is raised at the end naming every skipped file."""
    from autopilot import perms
    root, done, skipped = paths.data_root(), [], []

    def remerge(f: Path, floor, p: dict) -> None:
        try:
            if callable(floor):
                floor = floor()
            perms._write(f, merge(floor, overlay(p)))
        except (ValueError, KeyError, AttributeError, TypeError) as e:   # JSONDecodeError is a ValueError
            skipped.append((f, type(e).__name__))
            try:
                events.append_global("settings_unreadable", {"path": str(f), "error": type(e).__name__})
            except Exception:  # noqa: BLE001 - logging must not hide the skip
                pass
            return
        done.append(f)

    act, page = act or active(), page_profile()
    disp = root / "settings-dispatcher.json"
    if disp.exists():
        remerge(disp, lambda: json.loads(disp.read_text()), page)
    remerge(light_settings(), lambda: perms.static_settings("light"), page)
    files = sorted((root / "projects").glob("*/settings.json")) + sorted((root / "projects").glob("*/oneoff-*.json"))
    for f in files:
        remerge(f, lambda f=f: json.loads(f.read_text()), act)
    if skipped:
        raise SettingsUnreadable(done, skipped)
    return done


def migrate_from_config() -> bool:
    """Copy the model settings that moved into the subscription profile, once, and rewrite the settings files
    for it, so an upgraded install gets the subscription blanks at once. True when it did. The files are
    rewritten first: if that fails nothing is saved, and the next run tries again."""
    if path(SUBSCRIPTION).exists():
        _mark_subscription_chosen()
        return False
    cfg = config.load_global()
    p = _blank(SUBSCRIPTION)
    p["chosen_at"] = _now()
    for key, field in _MOVED:
        p[field] = config.lookup(cfg, key)
    errs = validate(p)
    if errs:
        raise ValueError("; ".join(errs))
    if cfg.get("active_profile") in (None, SUBSCRIPTION):
        try:
            apply(p)
        except SettingsUnreadable as e:
            # Only worker and one-off files may be left out: the page files must carry the profile, or fail.
            page = {str(root / n) for n in ("settings-dispatcher.json", "settings-light.json")
                    for root in (paths.data_root(),)}
            if any(str(f) in page for f, _ in e.skipped):
                raise
    save(p)
    for key, _ in _MOVED:
        config.reset_setting(key)
    return True


def _mark_subscription_chosen() -> None:
    """An install migrated before profiles had a choice keeps running unprompted: its subscription profile counts
    as chosen."""
    try:
        raw = json.loads(path(SUBSCRIPTION).read_text())
    except (OSError, ValueError):
        return   # unreadable: left for `ap profile show` to report, never overwritten here
    if isinstance(raw, dict) and not raw.get("chosen_at"):
        raw["chosen_at"] = _now()
        config._write(path(SUBSCRIPTION), raw)


# ---- choosing the model and effort ----
# The first time a profile is set up or used, its model and effort are picked from a list fetched just then.

EFFORTS = ("low", "medium", "high", "xhigh", "max")
RECOMMENDED_MODEL = "claude-opus-5-5[1m]"
RECOMMENDED_EFFORT = "high"
DEFAULT_MODEL = "default"   # Claude Code's own default model, stored as null
MODELS_TIMEOUT_S = 20
NEEDS_CHOICE = 3            # the exit code when a choice is needed and no terminal can ask for it


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _with_long(names) -> list:
    """Every claude-* model but haiku gets its 1M context variant first, then the plain name; others as they are."""
    out = []
    for m in names:
        base = _base_model(m)
        pair = [base + LONG_CONTEXT, base] if base.startswith("claude-") and not base.startswith("claude-haiku-") else [m]
        out += [x for x in pair if x not in out]
    return out


def model_choices(p: dict) -> list:
    """The models `p` may run, fetched fresh on every call (never cached). A gateway with a models command: that
    list. Otherwise no list can be fetched, so the priced claude-* models and `default` (Claude Code's default).
    Raises ModelsUnavailable when the models command fails."""
    cmd = (p.get("gateway") or {}).get("models_command") if p.get("mode") == "gateway" else None
    if cmd:
        return _with_long(_listed(cmd, MODELS_TIMEOUT_S))
    from autopilot import spend
    return _with_long(m for m in spend.prices() if m.startswith("claude-")) + [DEFAULT_MODEL]


def recommended_model(models: list):
    return RECOMMENDED_MODEL if RECOMMENDED_MODEL in models else (models[0] if models else None)


def needs_choice(p: dict) -> bool:
    if p.get("name") == SUBSCRIPTION and not path(SUBSCRIPTION).exists():
        return False   # the built-in subscription before migration wrote its file: Claude Code's defaults
    return not p.get("chosen_at")


def is_chosen_as(p: dict, model: str, effort: str) -> bool:
    """True when `p` is chosen and holds exactly this model (`default` = null) and effort."""
    return (not needs_choice(p) and p.get("effort") == effort
            and p.get("model") == (None if model == DEFAULT_MODEL else model))


def choices(p: dict) -> dict:
    """What `p` can choose from, fetched now. Raises ModelsUnavailable."""
    models = model_choices(p)
    return {"profile": p["name"], "models": models, "efforts": list(EFFORTS),
            "recommended_model": recommended_model(models), "recommended_effort": RECOMMENDED_EFFORT}


def choices_or_error(p: dict) -> dict:
    try:
        return choices(p)
    except ModelsUnavailable as e:
        return {"profile": p["name"], "error": str(e)}


def choose(name: str, model: str, effort: str, models: list = None) -> dict:
    """Save `name`'s model and effort, checked against a list fetched now (or `models`, fetched by the caller just
    before), and mark it chosen. Re-applies the settings when it is the active profile. ValueError for a value
    not offered; ModelsUnavailable when the list cannot be fetched. The page's choice card is the caller's to
    close (the page path closes it only once the switch worked)."""
    p = load(name)
    models = model_choices(p) if models is None else models
    if model not in models:
        raise ValueError(f"model {model!r} is not offered; choose one of: {', '.join(models)}")
    if effort not in EFFORTS:
        raise ValueError(f"effort {effort!r}: choose one of {', '.join(EFFORTS)}")
    p.update(model=None if model == DEFAULT_MODEL else model, effort=effort, chosen_at=_now())
    save(p, models)
    if config.load_global().get("active_profile") == name:
        apply(p)
    events.append_global("profile_chosen", {"profile": name, "model": p["model"], "effort": effort})
    return p


def _ask(title: str, options: list, recommended) -> str:
    """A numbered prompt; Enter takes the recommended entry. EOFError when the input ends."""
    print(title)
    for i, o in enumerate(options, 1):
        print(f"  {i}) {o}" + (" (recommended)" if o == recommended else ""))
    default = options.index(recommended) + 1 if recommended in options else None
    while True:
        got = input(f"Number{f' [{default}]' if default else ''}: ").strip()
        if not got and default:
            return options[default - 1]
        if got.isdigit() and 1 <= int(got) <= len(options):
            return options[int(got) - 1]
        if got in options:
            return got
        print(f"Type a number from 1 to {len(options)}.")


def _tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _prompt_choose(name: str, model=None, effort=None) -> int:
    """Ask in the terminal for what is missing, then save the choice. 0, or 1 with the reason printed."""
    try:
        c = choices(load(name))
        if model is None:
            model = _ask(f"Model for {name}:", c["models"], c["recommended_model"])
        if effort is None:
            effort = _ask(f"Effort for {name}:", c["efforts"], c["recommended_effort"])
        choose(name, model, effort, c["models"])
        close_choice(name)
    except EOFError:
        return _fail("no choice made; nothing changed")
    except (ValueError, ModelsUnavailable, OSError) as e:
        return _fail(str(e))
    return 0


def _print_choices(p: dict, hint: str) -> None:
    print(json.dumps(choices_or_error(p), indent=2, ensure_ascii=False))
    print(f"ap profile: {hint}", file=sys.stderr)


# The choice card on the page: written when a switch from the page names a profile that needs a choice.

def _choice_file() -> Path:
    return paths.data_root() / "profile-choice.json"


CHOICE_TTL = timedelta(hours=6)   # an abandoned card must not keep the one-minute answer check running


def choice_card():
    """The open choice card, or None. A card older than CHOICE_TTL is closed here."""
    d = _read(_choice_file())
    if not d.get("profile"):
        return None
    at = _parse_time(d.get("at"))
    if at is None or datetime.now().astimezone() - at > CHOICE_TTL:
        _choice_file().unlink(missing_ok=True)
        return None
    return d


def open_choice(name: str, refused: str = None) -> dict:
    """Fetch the list once and open (or refresh) the page's choice card for `name`. A fetch failure is shown on
    the card instead of a list."""
    c = choices_or_error(load(name))
    c["at"] = _now()
    if refused:
        c["refused"] = refused
    config._write(_choice_file(), c)
    return c


def close_choice(name: str = None) -> None:
    """Close the choice card (only when it is for `name`, if given)."""
    if name is None or (choice_card() or {}).get("profile") == name:
        _choice_file().unlink(missing_ok=True)


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


def cached_key_error(p: dict):
    """The last key check's error for `p`, from its record only (no command runs); None when it worked or was
    never checked."""
    rec = _read(_key_file())
    if p.get("mode") != "gateway" or rec.get("profile") != p.get("name") or rec.get("ok"):
        return None
    return rec.get("error") or "key command failed"


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
    ch = sub.add_parser("choices")
    ch.add_argument("name")
    ch.add_argument("--json", action="store_true")
    cs = sub.add_parser("choose")
    cs.add_argument("name")
    cs.add_argument("--model")
    cs.add_argument("--effort")
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
            p = _from_args(a)
        except (ValueError, OSError) as e:
            return _fail(str(e))
        if a.effort is not None and a.effort not in EFFORTS:
            return _fail(f"--effort {a.effort!r}: choose one of {', '.join(EFFORTS)}")
        known = None
        if a.model and a.effort:   # both given: chosen at creation, the model checked like `choose` does
            errs = validate(p)   # a broken profile is refused before its models command runs
            if errs:
                return _fail("; ".join(errs))
            try:
                known = model_choices(p)
            except ModelsUnavailable as e:
                return _fail(str(e))
            if a.model not in known:
                return _fail(f"model {a.model!r} is not offered; choose one of: {', '.join(known)}")
            p.update(model=None if a.model == DEFAULT_MODEL else a.model, chosen_at=_now())
        try:
            save(p, known)
        except (ValueError, OSError) as e:
            return _fail(str(e))
        if p.get("chosen_at"):
            return 0
        if _tty():
            code = _prompt_choose(a.name, a.model, a.effort)
            if code:
                print(f"ap profile: {a.name} is saved without a chosen model and effort; run "
                      f"`ap profile choose {a.name}` to choose them", file=sys.stderr)
            return code
        _print_choices(p, f"{a.name} is saved, but its model and effort are not chosen yet: run "
                          f"`ap profile choose {a.name} --model <model> --effort <effort>` with values from the list above")
        return 0
    try:
        p = load(a.name)
    except KeyError as e:
        return _fail(e.args[0])
    if a.cmd == "show":
        print(json.dumps(p, indent=2, ensure_ascii=False))
    elif a.cmd == "choices":
        try:
            c = choices(p)
        except ModelsUnavailable as e:
            return _fail(str(e))
        if a.json:
            print(json.dumps(c, indent=2, ensure_ascii=False))
        else:
            mark = lambda v, rec: v + (" (recommended)" if v == rec else "")
            print("models:  " + ", ".join(mark(m, c["recommended_model"]) for m in c["models"]))
            print("efforts: " + ", ".join(mark(e, c["recommended_effort"]) for e in c["efforts"]))
    elif a.cmd == "choose":
        if a.model is not None and a.effort is not None:
            try:
                choose(a.name, a.model, a.effort)
            except (ValueError, ModelsUnavailable, OSError) as e:
                return _fail(str(e))
            close_choice(a.name)
            return 0
        if _tty():
            return _prompt_choose(a.name, a.model, a.effort)
        missing = " and ".join(f for f, v in (("--model", a.model), ("--effort", a.effort)) if v is None)
        _print_choices(p, f"no terminal to ask in: give {missing} with values from the list above")
        return NEEDS_CHOICE
    elif a.cmd == "use":
        # Re-applying the active profile never asks: it is already what workers run on.
        if needs_choice(p) and a.name != config.load_global().get("active_profile"):
            if not _tty():
                _print_choices(p, f"{a.name} has no model and effort chosen yet, so nothing switched: run "
                                  f"`ap profile choose {a.name} --model <model> --effort <effort>`, then "
                                  f"`ap profile use {a.name}`")
                return NEEDS_CHOICE
            code = _prompt_choose(a.name)
            if code:
                return code
        use(a.name)
        close_choice()   # a card left on the page for any switch is given up
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
        close_choice(a.name)
    return 0
