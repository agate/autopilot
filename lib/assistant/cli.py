"""ap2 command line. Run `ap2` with no arguments for the command list."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from assistant import config, datadir, templates
from assistant.cards import LANES, CardError
from assistant.config import ConfigError, ShadowModeError
from assistant.resources import WorkerError
from assistant.store import Store

DATA_HELP = f"data folder (default: ${datadir.ENV}, else ~/Documents/Assistant)"
BY_HELP = "who makes the change, for the audit list (default: ap2)"


class _Parser(argparse.ArgumentParser):
    """argparse's parser, with its help, usage and error text written the way `ap2 config` writes its output."""

    def _print_message(self, message, file=None):
        # argparse sends help, usage and error text here. `file` is None when that stream is closed; argparse
        # would then fall back to stderr (help meant for stdout), so say nothing instead.
        if not message or file is None:
            return
        try:
            _write(message, file)
        except BrokenPipeError:
            pass  # the reader went away; argparse goes on to its own exit code (0 for help, 2 for an error)

    def error(self, message):
        if sys.stderr is None:  # stderr is closed; print_usage(None) would print the usage to stdout
            self.exit(2)
        super().error(message)


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="ap2", description="Autopilot v2 (shadow mode by default).")
    sub = p.add_subparsers(dest="command", metavar="<command>", title="commands")
    c = sub.add_parser("config", help="print the effective config as JSON",
                       description="Print the effective config (defaults filled in) and the data folder as JSON. "
                                   "Creates the data folder's layout on first use.")
    c.add_argument("--data", metavar="PATH", help=DATA_HELP)

    card = sub.add_parser("card", help="add, show or list cards", description="Add, show or list cards.")
    card_sub = card.add_subparsers(dest="card_command", metavar="<card command>", title="card commands")
    card_sub.required = True
    a = card_sub.add_parser("add", help="add a card from a JSON file and print it",
                            description="Add the card in FILE (JSON; - reads stdin) and print it with its id. The "
                                        "store sets id, created_at, updated_at and audit.")
    a.add_argument("file", metavar="FILE")
    a.add_argument("--by", default="ap2", help=BY_HELP)
    a.add_argument("--data", metavar="PATH", help=DATA_HELP)
    s = card_sub.add_parser("show", help="print one card", description="Print one card as JSON.")
    s.add_argument("id", metavar="ID")
    s.add_argument("--data", metavar="PATH", help=DATA_HELP)
    li = card_sub.add_parser("list", help="print the cards as a JSON list",
                             description="Print every card, oldest first, as a JSON list.")
    li.add_argument("--lane", choices=LANES)
    li.add_argument("--kind")
    li.add_argument("--parent", metavar="ID")
    li.add_argument("--data", metavar="PATH", help=DATA_HELP)

    se = sub.add_parser("search", help="find cards by their words",
                        description="Print, as a JSON list, the cards whose plain words, title, kind or feed hold "
                                    "every word of TEXT (any letter case, part of a word is enough).")
    se.add_argument("text", nargs="+", metavar="TEXT")
    se.add_argument("--data", metavar="PATH", help=DATA_HELP)

    r = sub.add_parser("restore", help="put a card back as a restore point saved it",
                       description="Put back the card saved in restore point POINT and print it. The current card is "
                                   "saved as a new point first, so this can be undone too.")
    r.add_argument("point", metavar="POINT")
    r.add_argument("--by", default="ap2", help=BY_HELP)
    r.add_argument("--data", metavar="PATH", help=DATA_HELP)
    _source_parser(sub)
    _scan_parser(sub)
    _gate_parser(sub)
    _install_parsers(sub)
    _template_parser(sub)
    _review_parser(sub)
    _worker_parser(sub)
    _profile_parser(sub)
    _spend_parser(sub)
    _cap_parser(sub)
    _serve_parser(sub)
    _phone_parser(sub)
    sub.add_parser("hook-park", help="a worker's PreToolUse hook: at the usage line, deny all but its park")
    _import_v1_parser(sub)
    sub.add_parser("hook-guard", help="a worker's PreToolUse hook on Bash: deny shell writes into the data folder "
                                      "or the package")
    sub.add_parser("hook-write", help="a read-only worker's PreToolUse hook on Write: deny a write outside its run "
                                      "directory")
    return p


# -- ap2 worker (design 8.1, 8.2) ------------------------------------------------------------------------------------

TRANSCRIPTS_HELP = "folder Claude Code keeps conversations in, read only (default: ~/.claude/projects)"
USAGE_HELP = "Claude Code's usage cache (default: ~/.claude.json)"


def _worker_parser(sub) -> None:
    w = sub.add_parser("worker", help="start, check, end or list workers",
                       description="Workers are `claude --bg` sessions, one per card and role. Starting or resuming "
                                   "one needs live mode; listing, checking, ending and archiving work in shadow mode.")
    ws = w.add_subparsers(dest="worker_command", metavar="<worker command>", title="worker commands")
    ws.required = True

    def add(name, help_, description, card=True, role=True, transcripts=False):
        c = ws.add_parser(name, help=help_, description=description)
        if card:
            c.add_argument("card", metavar="CARD")
        if role:
            c.add_argument("role", metavar="ROLE", choices=_roles())
        if transcripts:
            c.add_argument("--transcripts", metavar="PATH", help=TRANSCRIPTS_HELP)
        c.add_argument("--by", default="ap2 worker", help=BY_HELP.replace("ap2)", "ap2 worker)"))
        c.add_argument("--data", metavar="PATH", help=DATA_HELP)
        return c

    add("list", "print every worker and its state", "Print every worker on record, with its state, as a JSON list.",
        card=False, role=False)
    s = add("start", "start a worker on a card",
            "Start a `claude --bg` worker for ROLE on CARD in --cwd, and print its record. Live mode only.")
    s.add_argument("--cwd", metavar="PATH", help="the folder the session runs in (a worktree)")
    s.add_argument("--prompt-file", metavar="FILE", help="the brief, as a text file")
    s.add_argument("--needs", default=None, metavar="A,B", help="single-use resources the work needs")
    s.add_argument("--resume", action="store_true", help="continue the saved (or parked) session")
    add("end", "stop a worker and keep its session on the card",
        "Stop the worker, save its session id on the card and archive its transcript.", transcripts=True)
    c = add("check", "look at every worker and act on its state",
            "Classify every worker and act: end the done, ask for questions, resume the dead once, pause the idle "
            "and the blocked. Prints what it did as a JSON list.", card=False, role=False, transcripts=True)
    c.add_argument("--usage", metavar="PATH", help="resume parked workers whose usage window has reset; " + USAGE_HELP)
    pk = add("park", "park a worker at the usage line", "A worker parks itself while usage is at the park line; it "
             "keeps its slot and ports.")
    pk.add_argument("--note", default="", help="one line: where the worker is and the next step")
    pk.add_argument("--usage", metavar="PATH", help=USAGE_HELP)
    a = add("answer", "answer a worker's question and resume it",
            "Resume the worker that asked CARD's question, with TEXT in its prompt. Live mode only.", role=False)
    a.add_argument("text", nargs="+", metavar="TEXT")
    add("finish", "archive a done card's sessions",
        "Make the last gzipped copy of each of CARD's sessions; in live mode also delete the originals "
        "(`claude rm`).", role=False, transcripts=True)


def _roles():
    from assistant.workers import ROLES
    return ROLES


def cmd_worker(args) -> int:
    from assistant.workers import Workers

    data = datadir.ensure(datadir.resolve(args.data))
    w = Workers(data, transcripts_dir=getattr(args, "transcripts", None))
    sub = args.worker_command
    if sub == "list":
        out = w.list()
    elif sub == "start":
        prompt = ""
        if args.prompt_file:
            try:
                prompt = Path(args.prompt_file).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                raise WorkerError(f"cannot read {args.prompt_file!r}: {e}") from e
        needs = None if args.needs is None else [n for n in args.needs.split(",") if n]
        out = w.start(args.card, args.role, cwd=args.cwd, prompt=prompt, needs=needs, resume=args.resume, by=args.by)
    elif sub == "end":
        out = w.end(args.card, args.role, by=args.by)
    elif sub == "check":
        out = w.check(by=args.by, usage_file=args.usage)
    elif sub == "park":
        out = w.park(args.card, args.role, args.note, usage_file=args.usage)
    elif sub == "answer":
        out = w.answer(args.card, " ".join(args.text), by=args.by)
    else:
        out = w.finish(args.card, by=args.by)
    _print_json(out)
    return 0


class _OutputError(Exception):
    """Writing the output failed for a reason other than a broken pipe (e.g. a full disk)."""


def _write(text: str, stream) -> None:
    """Write `text` to `stream` and flush it now, so a write error shows up here and not in the flush at exit.
    A closed stream (None) gets nothing. When the write fails, the stream is pointed at devnull first (when devnull
    can be opened), so the flush at exit does not fail again."""
    if stream is None:
        return
    try:
        stream.write(text)
        stream.flush()
    except OSError as e:  # only the output's errors; the commands turn their own OSErrors into ConfigError
        _to_devnull(stream)
        if isinstance(e, BrokenPipeError):
            raise
        raise _OutputError(e) from e


def _write_out(text: str) -> None:
    """Print `text` (and a newline) to stdout; see `_write`."""
    _write(text + "\n", sys.stdout)


def _to_devnull(stream) -> None:
    """Point the stream's file descriptor at devnull (the recipe from the Python signal module docs). Does
    nothing when the stream is not a real file. Never raises: when devnull cannot be opened or moved onto the fd
    (e.g. EMFILE, too many open files), the stream is left as it is and the caller still raises the write's own
    error. The devnull fd it opens is closed again, unless it landed on the stream's own fd."""
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):  # an in-memory stream (io.UnsupportedOperation)
        return
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        os.dup2(devnull, fd)
    except OSError:
        pass
    finally:
        if devnull != fd:  # cannot happen after a fresh os.open while fd is open, but never close the stream's fd
            try:
                os.close(devnull)
            except OSError:
                pass


def _print_json(out) -> None:
    text = json.dumps(out, sort_keys=True, indent=2, ensure_ascii=False)
    try:
        text.encode(getattr(sys.stdout, "encoding", None) or "utf-8")  # sys.stdout is None when stdout is closed
    except UnicodeEncodeError:  # e.g. a Latin-1 terminal: \uXXXX escapes are still the same JSON
        text = json.dumps(out, sort_keys=True, indent=2, ensure_ascii=True)
    _write_out(text)


def cmd_config(args) -> int:
    data = datadir.ensure(datadir.resolve(args.data))
    out = config.load(data).as_dict()
    out["data_dir"] = str(data)
    _print_json(out)
    return 0


def _store(args) -> Store:
    return Store(datadir.ensure(datadir.resolve(args.data)))


def _read_card_file(name: str):
    if name == "-" and sys.stdin is None:
        raise CardError("cannot read -: stdin is closed")
    # !r keeps a newline in the name from splitting the one-line error
    try:
        if name != "-":
            text = Path(name).read_text(encoding="utf-8")
        elif getattr(sys.stdin, "buffer", None) is not None:
            text = sys.stdin.buffer.read().decode("utf-8")  # UTF-8 like a file, whatever the locale says
        else:
            text = sys.stdin.read()
    except (OSError, UnicodeDecodeError) as e:
        raise CardError(f"cannot read {name!r}: {e}") from e
    try:
        return json.loads(text)
    except ValueError as e:
        raise CardError(f"{name!r}: not JSON: {e}") from e


def cmd_card(args) -> int:
    if args.card_command == "add":
        card = _read_card_file(args.file)  # read before the data folder is touched
        out = _store(args).add(card, by=args.by)
    elif args.card_command == "show":
        out = _store(args).get(args.id)
    else:
        out = _store(args).list(lane=args.lane, kind=args.kind, parent=args.parent)
    _print_json(out)
    return 0


def cmd_search(args) -> int:
    _print_json(_store(args).search(" ".join(args.text)))
    return 0


def cmd_restore(args) -> int:
    _print_json(_store(args).restore(args.point, by=args.by))
    return 0


# -- ap2 profile (access profiles, design section 12) -----------------------------------------------------------------
# A profile says how workers reach Claude: the subscription, or a gateway through a key command. `use` writes only
# inside <data> (active-profile.json, and the profile's file when it asks for a choice), so it works in shadow mode;
# `list` and `show` create nothing. `rotate` runs the profile's rotate command (its output thrown away) and forgets
# the cached key check (keys.py); it writes only inside <data>, so it works in shadow mode too.

def _profile_parser(sub) -> None:
    pr = sub.add_parser("profile", help="list, show, add, choose, use, rotate or remove access profiles",
                        description="Access profiles: how workers reach Claude (the subscription, or a gateway "
                                    "reached with a key command). Every worker start or resume runs on the active "
                                    "one; a running worker keeps its own. A profile whose model and effort "
                                    "were never chosen asks for them: in a terminal, a numbered prompt; with no "
                                    "terminal, the choices as JSON and exit 3.")
    ps = pr.add_subparsers(dest="profile_command", metavar="<profile command>", title="profile commands")
    ps.required = True
    li = ps.add_parser("list", help="print every profile and which one is active",
                       description="Print {active, active_error, profiles} as JSON; a gateway profile's row has "
                                   "key_error, the last key check's error (null when it worked or never ran). "
                                   "Reads only: no key command runs.")
    li.add_argument("--data", metavar="PATH", help=DATA_HELP)
    for name, help_ in (("show", "print one profile as JSON (its commands, never a key)"),
                        ("use", "make NAME the active profile; prints {active, was}"),
                        ("rotate", "mint a new key: run NAME's rotate command (its output thrown away) and forget "
                                   "the cached key check; prints {rotated}"),
                        ("remove", "delete a stored profile (not the subscription, not the active one)")):
        c = ps.add_parser(name, help=help_, description=help_[0].upper() + help_[1:] + ".")
        c.add_argument("name", metavar="NAME")
        c.add_argument("--data", metavar="PATH", help=DATA_HELP)
    ch = ps.add_parser("choices", help="print the models and efforts NAME can choose from, fetched now",
                       description="Print {profile, models, efforts, recommended_model, recommended_effort} as "
                                   "JSON. The models are fetched now (a gateway's --models-command runs every "
                                   "time; otherwise the priced claude-* models and `default`).")
    ch.add_argument("name", metavar="NAME")
    ch.add_argument("--data", metavar="PATH", help=DATA_HELP)
    co = ps.add_parser("choose", help="choose NAME's model and effort from the list fetched now",
                       description="Save NAME's model and effort, checked against the list fetched now, and print "
                                   "the profile. A missing flag is asked for in a terminal; with no terminal the "
                                   "choices are printed as JSON and ap2 exits 3.")
    co.add_argument("name", metavar="NAME")
    co.add_argument("--model", metavar="MODEL", help="one of the models `ap2 profile choices NAME` lists")
    co.add_argument("--effort", metavar="EFFORT", help="one of the efforts `ap2 profile choices NAME` lists")
    co.add_argument("--data", metavar="PATH", help=DATA_HELP)
    add = ps.add_parser("add", help="save a new profile and print it",
                        description="Save a new profile NAME and print it. A gateway profile needs --base-url, "
                                    "--key-command (a command that prints the key; the key is never stored), "
                                    "--small-model and --daily-cap. With --models-command, the models are checked "
                                    "against the JSON list it prints. Without both --model and --effort, the "
                                    "profile is saved unchosen, then they are asked for (see `choose`).")
    add.add_argument("name", metavar="NAME")
    mode = add.add_mutually_exclusive_group(required=True)
    mode.add_argument("--subscription", action="store_true", help="the Claude subscription")
    mode.add_argument("--gateway", action="store_true", help="a Claude-compatible gateway")
    for flag, help_ in (("base-url", "the gateway's base URL"), ("key-command", "a command that prints the key"),
                        ("refresh-command", "a command that refreshes the key"),
                        ("rotate-command", "a command that mints a new key"),
                        ("models-command", "a command that prints the gateway's models as a JSON list"),
                        ("daily-cap", "the gateway profile's daily dollar cap"), ("label", "the name shown"),
                        ("model", "the workers' model (--model on a fresh start)"),
                        ("dispatcher-model", "the dispatcher's model"), ("small-model", "the small model"),
                        ("effort", "the workers' effort (--effort on a fresh start)"),
                        ("settings-file", "extra settings, as a JSON file")):
        add.add_argument("--" + flag, metavar=flag.split("-")[0].upper(), help=help_)
    add.add_argument("--env", action="append", default=[], metavar="K=V", help="an extra variable (repeatable)")
    add.add_argument("--data", metavar="PATH", help=DATA_HELP)


def cmd_profile(args) -> int:
    from assistant import keys, profiles

    sub = args.profile_command
    data = datadir.resolve(args.data)
    if sub == "list":
        try:
            current, error = profiles.active(data)["name"], None
        except profiles.ProfileError as e:
            current, error = None, str(e)
            try:
                current = profiles.active_name(data)
            except profiles.ProfileError:
                pass
        rows = []
        for name in profiles.names(data):
            row = {"name": name, "active": name == current}
            try:
                p = profiles.load(data, name)
                row.update({k: p[k] for k in ("mode", "label", "model", "effort", "daily_cap_usd")})
                if p.get("mode") == "gateway":
                    row["key_error"] = keys.cached_error(data, p)  # the record only: no command runs
            except profiles.ProfileError as e:
                row["error"] = str(e)
            rows.append(row)
        out = {"active": current, "active_error": error, "profiles": rows}
    elif sub == "show":
        out = profiles.load(data, args.name)
    elif sub == "choices":
        out = profiles.choices(data, profiles.load(data, args.name))
    else:
        datadir.ensure(data)
        if sub == "add":
            return _profile_add(data, args)
        if sub == "choose":
            return _profile_choose(data, args)
        if sub == "use":
            return _profile_use(data, args.name)
        if sub == "rotate":
            from datetime import datetime
            _print_json(keys.rotate(data, args.name, datetime.now().astimezone()))
            return 0
        out = profiles.remove(data, args.name)
    _print_json(out)
    return 0


def _profile_add(data, args) -> int:
    """Save the profile; when its model and effort are not both given, ask for them once (lesson: `add` and `use`
    ask once). One fetch serves the check, the save and the prompt."""
    from assistant import profiles

    name = args.name
    if name == profiles.SUBSCRIPTION or profiles.path(data, name).exists():
        raise profiles.ProfileError(f"{name} already exists")
    p = profiles.from_args(args)
    if args.effort is not None:
        profiles.check_effort(args.effort)
    errs = profiles.validate(p)  # a broken profile is refused before its models command runs
    if errs:
        raise profiles.ProfileError("; ".join(errs))
    models, known = profiles.fetch_models(data, p)  # the shown list, and the full one a save checks
    if args.model is not None:  # a flag value not offered is refused before anything is saved, asked or printed
        profiles.check_model(args.model, models)
    saved = profiles.save(data, p, known)
    if not profiles.needs_choice(data, saved):
        _print_json(saved)
        return 0
    c = profiles.choices(data, saved, models)
    if not _stdin_is_terminal():
        return _print_choices(c, f"{name} is saved, but its model and effort are not chosen yet: run "
                                 f"`ap2 profile choose {name} --model M --effort E` with values from the list above")
    try:
        out = _prompt_choose(data, name, c, args.model, args.effort, known)
    except (EOFError, profiles.ProfileError) as e:
        why = "no choice made" if isinstance(e, EOFError) else str(e)
        raise profiles.ProfileError(f"{why}; {name} is saved without a chosen model and effort: "
                                    f"`ap2 profile choose {name}` finishes it") from None
    _print_json(out)
    return 0


def _profile_choose(data, args) -> int:
    from assistant import profiles

    name = args.name
    if args.model is not None and args.effort is not None:
        _print_json(profiles.choose(data, name, args.model, args.effort))
        return 0
    p = profiles.load(data, name)
    models, known = profiles.fetch_models(data, p)
    c = profiles.choices(data, p, models)
    if not _stdin_is_terminal():
        _check_given(c, args.model, args.effort)
        return _print_choices(c, f"no terminal to ask in: run `ap2 profile choose {name} --model M --effort E` "
                                 "with values from the list above")
    try:
        out = _prompt_choose(data, name, c, args.model, args.effort, known)
    except EOFError:
        raise profiles.ProfileError("no choice made; nothing changed") from None
    _print_json(out)
    return 0


def _profile_use(data, name: str) -> int:
    """Switch to `name`. A profile never chosen is chosen first (in a terminal) or nothing switches (exit 3). The
    active profile is switched to again without a question: workers already run on it."""
    from assistant import profiles

    p = profiles.load(data, name)
    try:
        current = profiles.active_name(data)
    except profiles.ProfileError:
        current = None  # broken: `use` is its fix, so it goes on
    if name != current and profiles.needs_choice(data, p):
        models, known = profiles.fetch_models(data, p)
        c = profiles.choices(data, p, models)
        if not _stdin_is_terminal():
            return _print_choices(c, f"{name} has no model and effort chosen yet, so nothing switched: run "
                                     f"`ap2 profile choose {name} --model M --effort E`, then `ap2 profile use {name}`")
        try:
            _prompt_choose(data, name, c, known=known)
        except EOFError:
            raise profiles.ProfileError("no choice made; nothing changed") from None
    _print_json(profiles.use(data, name))
    return 0


def _stdin_is_terminal() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):  # a closed or replaced stdin
        return False


def _check_given(c: dict, model, effort) -> None:
    """Refuse a flag value that is not offered, before anything is asked or printed."""
    from assistant import profiles

    if effort is not None:
        profiles.check_effort(effort)
    if model is not None:
        profiles.check_model(model, c["models"])


def _print_choices(c: dict, hint: str) -> int:
    """No terminal to ask in: the choices as JSON on stdout, one hint line on stderr, exit NEEDS_CHOICE."""
    from assistant import profiles

    _print_json(c)
    _write(f"ap2: {hint}\n", sys.stderr)
    return profiles.NEEDS_CHOICE


def _prompt_choose(data, name: str, c: dict, model=None, effort=None, known=None) -> dict:
    """Ask in the terminal for what is missing, then save the choice: the model checked against the list `c` shows,
    the saved file against `known`, the full list of the same fetch. EOFError when the input ends; ProfileError when
    a given value is not offered."""
    from assistant import profiles

    _check_given(c, model, effort)
    if not c["models"]:
        raise profiles.ProfileError("gateway.models_command: listed no models, so there is nothing to choose")
    if model is None:
        model = _ask(f"Model for {name}:", c["models"], c["recommended_model"])
    if effort is None:
        effort = _ask(f"Effort for {name}:", c["efforts"], c["recommended_effort"])
    return profiles.choose(data, name, model, effort, c["models"], known)


def _ask(title: str, options: list, recommended) -> str:
    """A numbered prompt on stderr (stdout keeps only the JSON); Enter takes the recommended entry, a number or the
    exact name is taken, anything else asks again. EOFError when the input ends or the user presses Ctrl-C, so every
    caller says "no choice made" in one line instead of a traceback."""
    lines = [title] + [f"  {i}) {o}" + (" (recommended)" if o == recommended else "")
                       for i, o in enumerate(options, 1)]
    _write("\n".join(lines) + "\n", sys.stderr)
    default = options.index(recommended) + 1 if recommended in options else None
    while True:
        _write(f"Number{f' [{default}]' if default else ''}: ", sys.stderr)
        try:
            got = input().strip()
        except (EOFError, KeyboardInterrupt):
            _write("\n", sys.stderr)  # end the prompt's line, so the error line that follows starts on its own
            raise EOFError from None
        if not got and default:
            return options[default - 1]
        # ASCII digits only and 4 at most: `int` refuses "²" (a digit to isdigit) and a number too long to read.
        if got.isascii() and got.isdigit() and len(got) <= 4 and 1 <= int(got) <= len(options):
            return options[int(got) - 1]
        if got in options:
            return got
        _write(f"Type a number from 1 to {len(options)}, or press Enter for the recommended one.\n", sys.stderr)


# -- ap2 spend (the spend ledger, design section 12) ------------------------------------------------------------------
# What the workers launched in one mode cost, priced from their transcripts. Reads only, apart from the ledger's cache.

def _spend_parser(sub) -> None:
    sp = sub.add_parser("spend", help="print what workers cost today, this week and this month",
                        description="Print the dollars spent today, this week (from Monday) and this calendar month "
                                    "by workers launched in MODE, priced from their transcripts, and what could not "
                                    "be priced. Reads only, apart from the ledger's cache.")
    sp.add_argument("--mode", choices=("gateway", "subscription"), default="gateway",
                    help="the launches to count (default: gateway)")
    sp.add_argument("--json", action="store_true", help="print the totals as JSON")
    sp.add_argument("--transcripts", metavar="PATH", help=TRANSCRIPTS_HELP)
    sp.add_argument("--data", metavar="PATH", help=DATA_HELP)


def cmd_spend(args) -> int:
    import shutil

    from assistant import spend, transcripts
    from autopilot import sessions as v1_sessions

    data = datadir.resolve(args.data)
    projects = Path(args.transcripts) if args.transcripts else transcripts.default_projects()
    claude = shutil.which("claude")
    listing = (lambda: v1_sessions.list_all(claude)) if claude else None
    try:
        t = spend.totals(data, projects, mode=args.mode, listing=listing)
    except OSError as e:  # say which side failed when the error names its file, else name both folders
        ledger = f"read or update the spend ledger in {data / spend.FOLDER}"
        where = f"read the transcripts in {projects}, or {ledger}"
        name = e.filename if isinstance(e.filename, (str, os.PathLike)) else None
        if name is not None and Path(name).is_relative_to(projects):
            where = f"read the transcripts in {projects}"
        elif name is not None and Path(name).is_relative_to(data):
            where = ledger
        raise spend.SpendError(f"cannot {where}: {e}") from e
    if args.json:
        _print_json(t)
        return 0
    lines = [f"{args.mode} spend (local time):"]
    # A total `totals` clamped (a sum too big for a number) is a marker, not a 309-digit amount; `unknown:` follows.
    lines += [f"  {k:<6} too large to show" if t[k] == sys.float_info.max else f"  {k:<6} ${t[k]:,.2f}"
              for k in ("today", "week", "month")]
    if t["unknown"]:
        lines.append("  unknown: some spend is not counted (an unpriced model, or a launch not matched to its "
                     "session)")
        if t["unknown_models"]:
            lines.append(f"  unpriced models today: {', '.join(t['unknown_models'])}")
        if t["unknown_launches"]:
            lines.append(f"  launches not matched today: {t['unknown_launches']}")
    _write_out("\n".join(lines))
    return 0


# -- ap2 cap (the daily cap, design section 12) -----------------------------------------------------------------------
# Today's cap state, the cap card's answers and the pause. Writes only inside the data folder.

def _cap_parser(sub) -> None:
    cp = sub.add_parser("cap", help="show or answer the daily cap of a gateway profile",
                        description="Print today's daily cap state: the active profile and its cap, today's raise "
                                    "and limit, today's gateway spend, the cap card and the pause. `answer` answers "
                                    "the cap card; `resume` lifts the pause. Writes only inside the data folder.")
    cp.add_argument("--json", action="store_true", help="print the state as JSON")
    cp.add_argument("--transcripts", metavar="PATH", help=TRANSCRIPTS_HELP)
    cp.add_argument("--data", metavar="PATH", help=DATA_HELP)
    cs = cp.add_subparsers(dest="cap_command", metavar="<cap command>", title="cap commands")
    an = cs.add_parser("answer", help="answer the cap card",
                       description="raise50 / raise100 raise today's limit by 50% / 100% (until local midnight); "
                                   "accept_unknown counts today's unpriced cost as $0 until midnight; pause stops "
                                   "and parks every running worker (live mode only) and refuses every start until "
                                   "`ap2 cap resume`. Prints JSON.")
    an.add_argument("option", metavar="OPTION", help="raise50, raise100, accept_unknown or pause")
    re_ = cs.add_parser("resume", help="lift the cap pause",
                        description="Remove the pause, so workers may start and resume again. Prints JSON.")
    for c in (an, re_):  # SUPPRESS: given before the subcommand, the value is kept
        c.add_argument("--transcripts", metavar="PATH", default=argparse.SUPPRESS, help=TRANSCRIPTS_HELP)
        c.add_argument("--data", metavar="PATH", default=argparse.SUPPRESS, help=DATA_HELP)


def cmd_cap(args) -> int:
    import shutil
    from datetime import datetime

    from assistant import cap, profiles, spend, transcripts

    data = datadir.ensure(datadir.resolve(args.data))
    now = datetime.now().astimezone()  # the local day, at this moment's offset
    if args.cap_command == "resume":
        _print_json(cap.resume(data))
        return 0
    if args.cap_command == "answer":
        cfg = config.load(data)
        w = None
        if cfg.mode == "live":  # the pause stops workers in live mode only
            from assistant.workers import Workers
            w = Workers(data, cfg=cfg, transcripts_dir=args.transcripts)
        _print_json(cap.answer(data, args.option, now, workers=w))
        return 0
    from autopilot import sessions as v1_sessions

    p = profiles.active(data)
    projects = Path(args.transcripts) if args.transcripts else transcripts.default_projects()
    claude = shutil.which("claude")
    listing = (lambda: v1_sessions.list_all(claude)) if claude else None
    try:
        t = spend.totals(data, projects, now, "gateway", listing)
    except OSError as e:
        raise spend.SpendError(f"cannot read or update the spend ledger in {data / spend.FOLDER}: {e}") from e
    s = cap.state(data, now)
    pz = cap.paused(data)
    out = {"day": s["day"], "profile": p["name"], "mode": p["mode"], "daily_cap_usd": p.get("daily_cap_usd"),
           "raise_pct": s["raise_pct"], "limit": cap.today_limit(data, p, now), "spent_today": t["today"],
           "unknown_today": t["unknown_today"], "unknown_models": t["unknown_models"],
           "unknown_launches": t["unknown_launches"], "card": s["card"], "card_id": s.get("card_id"),
           "accept_unknown": s.get("accept_unknown") is True, "paused": pz is not None, "pause": pz}
    if args.json:
        _print_json(out)
        return 0
    limit = "none (a subscription profile has no daily cap)" if out["limit"] is None else f"${out['limit']:,.2f}"
    lines = [f"daily cap, {out['day']} (local time):", f"  profile  {out['profile']} ({out['mode']})",
             f"  limit    {limit}" + (f" (+{out['raise_pct']}% today)" if out["raise_pct"] else ""),
             f"  spent    {cap._usd(out['spent_today'])} on gateway profiles today"  # a clamped total: a marker
             + (", some of it cannot be priced" if out["unknown_today"] else ""),
             f"  card     {out['card'] or 'none'}" + (f" ({out['card_id']})" if out["card_id"] else "")]
    if out["accept_unknown"]:
        lines.append("  today's unpriced cost is accepted as $0")
    if pz is not None:
        lines.append("  paused: no worker starts or resumes until `ap2 cap resume`")
    _write_out("\n".join(lines))
    return 0


COMMANDS = {"config": cmd_config, "card": cmd_card, "search": cmd_search, "restore": cmd_restore}
COMMANDS["worker"] = cmd_worker
COMMANDS["profile"] = cmd_profile
COMMANDS["spend"] = cmd_spend
COMMANDS["cap"] = cmd_cap


# -- ap2 source (the source engine, design 5) ---------------------------------------------------------------------

def _source_parser(sub) -> None:
    src = sub.add_parser("source", help="list, show or read sources",
                         description="List, show or read the source files in the plugin's sources/ and the data "
                                     "folder's sources/ (the user's file wins on the same name).")
    src_sub = src.add_subparsers(dest="source_command", metavar="<source command>", title="source commands")
    src_sub.required = True
    li = src_sub.add_parser("list", help="print every source as a JSON list",
                            description="Print every source (name, brings, via, fill, every, origin, file) as a "
                                        "JSON list, by name.")
    li.add_argument("--data", metavar="PATH", help=DATA_HELP)
    sh = src_sub.add_parser("show", help="print one source, merged, as JSON",
                            description="Print the source NAME as the engine reads it: extends merged, checked.")
    sh.add_argument("name", metavar="NAME")
    sh.add_argument("--data", metavar="PATH", help=DATA_HELP)
    rd = src_sub.add_parser("read", help="read a source and make or update its cards",
                            description="Read the source NAME and print what each item became. A via: command "
                                        "source runs its read.run; any other source needs --raw FILE, the output "
                                        "a scan session saved. The raw output is saved under "
                                        "sources/.raw/NAME/ first.")
    rd.add_argument("name", metavar="NAME")
    rd.add_argument("--raw", metavar="FILE", help="the saved output of the read, instead of running read.run")
    rd.add_argument("--records", metavar="FILE",
                    help="fill: ai only: the records a scan session filled, as a JSON list")
    rd.add_argument("--dry-run", action="store_true",
                    help="read and show what each item would become; save nothing")
    rd.add_argument("--data", metavar="PATH", help=DATA_HELP)


def cmd_source(args) -> int:
    from assistant import engine as engine_module
    from assistant.engine import Engine
    from assistant.sources import Loader, SourceError, read_capped

    path = datadir.resolve(args.data)
    dry_run = getattr(args, "dry_run", False)
    if args.source_command == "read" and args.raw is None and args.records is not None:
        # First, so the refused command reads nothing (a --records /dev/stdin would wait) and creates nothing.
        raise SourceError("--records needs --raw FILE, the output the records were filled from")
    if not dry_run:  # a dry run saves nothing, the data folder's layout included
        datadir.ensure(path)
    if args.source_command == "list":
        out = [s.summary() for s in Loader(path).load_all()]
    elif args.source_command == "show":
        out = Loader(path).load(args.name).data
    else:
        engine = Engine(path)
        records = None
        if args.records is not None:
            try:
                # Read with a byte limit, as a --raw file is: json.loads of a huge file takes many times its size.
                cap = engine_module.MAX_OUTPUT
                held = read_capped(args.records, cap, f"the records file {args.records!r} is larger than {cap} bytes")
                records = json.loads(held.decode("utf-8"))
            except (OSError, UnicodeDecodeError, ValueError, RecursionError) as e:
                why = "nested too deeply" if isinstance(e, RecursionError) else e
                raise SourceError(f"cannot read the records {args.records!r}: {why}") from e
        if args.records is not None and not isinstance(records, list):
            raise SourceError(f"the records file {args.records!r} must hold a JSON list")
        if args.raw is not None:
            out = engine.read_saved(args.name, args.raw, records=records, dry_run=dry_run)
        else:
            out = engine.read(args.name, dry_run=dry_run)
    _print_json(out)
    return 0


COMMANDS["source"] = cmd_source


# -- ap2 gate (slice 5) -------------------------------------------------------------------------------------------

def _gate_now(text: str):
    """--now: an ISO time with a zone (2026-11-10T09:00:00Z or ...-08:00), for trying a run at another time. It
    may not be later than the real time (plus the slot grace): a run writes last_slot, makes cards and prunes
    restore points, so a run in the future would hold back every real slot until then. A year before 1971 is
    refused too (the slot and prune arithmetic would go below year 1)."""
    from datetime import datetime, timezone
    from assistant import cards, routines
    try:
        at = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO time: {text!r} (for example 2026-11-10T09:00:00Z)") from None
    if at.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{text!r} has no time zone; add Z or an offset such as -08:00")
    try:
        in_utc = at.astimezone(timezone.utc)
    except OverflowError:
        in_utc = None
    if in_utc is None or in_utc.year < 1971:
        raise argparse.ArgumentTypeError(f"{text!r} is out of range (years 1971 to now)")
    if in_utc > cards.now_utc() + routines.GRACE:
        raise argparse.ArgumentTypeError(f"{text!r} cannot be later than the real time: a run writes last_slot, "
                                         f"makes cards and prunes restore points")
    return at


def _gate_parser(sub) -> None:
    g = sub.add_parser("gate", help="run the gate once and print what it did",
                       description="One gate run, no AI: start the routine slots that are due (card first, then "
                                   "last_slot), list the watches that are due, prune restore points older than 30 "
                                   "days, and write gate.json and gate.log. Prints the run as JSON. While another "
                                   "run holds the gate lock, prints {\"locked\": true, ...} and exits 0.")
    g.add_argument("--now", type=_gate_now, metavar="ISO", help="run as if it were this time (default: now)")
    g.add_argument("--data", metavar="PATH", help=DATA_HELP)


def cmd_gate(args) -> int:
    from assistant import cards, gate
    data = datadir.ensure(datadir.resolve(args.data))
    _print_json(gate.run(data, args.now or cards.now_utc()))
    return 0


COMMANDS["gate"] = cmd_gate


# -- ap2 install, uninstall and doctor (v2's own launchd job; design section 12, "Install") ------------------------

INSTALL_DESCRIPTION = ("Write and load v2's own launchd job, com.assistant.gate, which runs `ap2 gate` every 60 "
                       "seconds (plist in ~/Library/LaunchAgents, launcher in ~/Library/Application Support/"
                       "assistant). It never touches v1's or secretary's job. Only the user runs it, never a worker. "
                       "Sets up the data folder (a new config.yaml says mode: shadow; an existing one is kept as it "
                       "is) and prints what it wrote as JSON.")


def _install_parsers(sub) -> None:
    i = sub.add_parser("install", help="write and load v2's own launchd job (com.assistant.gate)",
                       description=INSTALL_DESCRIPTION)
    i.add_argument("--data", metavar="PATH", help=DATA_HELP)
    sub.add_parser("uninstall", help="unload and remove v2's launchd job; the data folder is kept",
                   description="Unload v2's own launchd job, com.assistant.gate, and remove its plist and launcher. "
                               "It never touches v1's or secretary's job, and the data folder is kept. Only the user "
                               "runs it. Prints what it removed as JSON.")
    d = sub.add_parser("doctor", help="check v2's launchd job, data folder, tools, profile and mode",
                       description="Check v2's launchd job, whether the gate ran in the last 3 minutes, the data "
                                   "folder, the tools on the job's PATH (claude, gh, git, python3), the active "
                                   "profile and the mode, with a fix line under each failure. Read-only: creates "
                                   "nothing. Exits 1 when a check fails.")
    d.add_argument("--data", metavar="PATH", help=DATA_HELP)


def cmd_install(args) -> int:
    from assistant import install
    _print_json(install.install(args.data))
    return 0


def cmd_uninstall(args) -> int:
    from assistant import install
    _print_json(install.uninstall())
    return 0


def cmd_doctor(args) -> int:
    from assistant import doctor
    res = doctor.checks(args.data)
    _write_out("\n".join(doctor.lines(res)))
    return 0 if all(c["ok"] for c in res) else 1


COMMANDS["install"] = cmd_install
COMMANDS["uninstall"] = cmd_uninstall
COMMANDS["doctor"] = cmd_doctor


# --- ap2 template (design 6) ---------------------------------------------------------------------------------
# Read-only: none of these creates the data folder or writes a file. A bad template is a TemplateError (a
# ConfigError), printed as one `ap2: <file>: line N: ...` line with exit 1.

def _template_parser(sub) -> None:
    t = sub.add_parser("template", help="list, show or check card templates",
                       description="List, show or check card templates: the built-in ones and the user's own in "
                                   "<data>/templates/, which win on the same kind.")
    t_sub = t.add_subparsers(dest="template_command", metavar="<template command>", title="template commands")
    t_sub.required = True
    li = t_sub.add_parser("list", help="print every template's kind and origin as a JSON list",
                          description="Print every template (built in, and the user's own) as a JSON list, by kind.")
    li.add_argument("--data", metavar="PATH", help=DATA_HELP)
    sh = t_sub.add_parser("show", help="print one template, with each role's tools",
                          description="Print the template in use for KIND as JSON: its file, what it shows, and "
                                      "each role with the tools and permission floor it keeps.")
    sh.add_argument("kind", metavar="KIND")
    sh.add_argument("--data", metavar="PATH", help=DATA_HELP)
    ch = t_sub.add_parser("check", help="validate a template file without installing it",
                          description="Validate the template in FILE (named <kind>.yaml) and say which template it "
                                      "would replace. Nothing is copied or installed.")
    ch.add_argument("file", metavar="FILE")
    ch.add_argument("--data", metavar="PATH", help=DATA_HELP)


def cmd_template(args) -> int:
    data = datadir.resolve(args.data)  # refused folders are refused here too; nothing is created
    if args.template_command == "list":
        out = [templates.summary(t) for t in templates.catalog(data).values()]
    elif args.template_command == "show":
        cat = templates.catalog(data)
        if args.kind not in cat:
            raise templates.TemplateError(f"no template for kind {args.kind!r} (kinds: {', '.join(cat)})")
        out = templates.describe(cat[args.kind])
    else:
        out = templates.check(args.file, data)
    _print_json(out)
    return 0


COMMANDS["template"] = cmd_template


# --- ap2 review (PR review cards, design 6.2) --------------------------------------------------------------------
# Every gh call reads (prreview.gh_read refuses anything else); only the card and its run directory are written, in
# shadow or live mode. A ReviewError is a ConfigError: one `ap2: ...` line, exit 1.

def _review_parser(sub) -> None:
    r = sub.add_parser("review", help="plan, check and apply a PR review card's review",
                       description="The pr-review role's steps on a PR review card: plan the review (read from "
                                   "GitHub), give the verdict the findings imply, check the report, put it on the "
                                   "card, and sync the card with the PR. Nothing is ever posted to GitHub.")
    r_sub = r.add_subparsers(dest="review_command", metavar="<review command>", title="review commands")
    r_sub.required = True
    pl = r_sub.add_parser("plan", help="read the PR and write the review plan",
                          description="Read CARD's pull request from GitHub (its head, merge base and files), pick "
                                      "the review angles, write <data>/sessions/CARD/prreview-plan.json and print "
                                      "it. A delta reviews only the commits since the last review.")
    pl.add_argument("card", metavar="CARD")
    pl.add_argument("--stage", choices=("review", "delta"), default="review")
    pl.add_argument("--checkout", metavar="PATH",
                    help="a git checkout of the repo; its .autopilot/review.yaml is read at the head commit when the "
                         "checkout has it (nothing is fetched)")
    pl.add_argument("--data", metavar="PATH", help=DATA_HELP)
    ve = r_sub.add_parser("verdict", help="print the verdict the four finding groups give",
                          description="Check the four finding groups in FILE (JSON, or a report holding them as "
                                      "findings; - reads stdin) and print the verdict they give. Exit 1 with every "
                                      "error.")
    ve.add_argument("--file", required=True, metavar="FILE")
    ve.add_argument("--data", metavar="PATH", help=DATA_HELP)
    ch = r_sub.add_parser("check", help="check the review report against the plan and the card",
                          description="Check <data>/sessions/CARD/prreview.json and print {ok, errors}. Exit 1 when "
                                      "it is not ok.")
    ch.add_argument("card", metavar="CARD")
    ch.add_argument("--data", metavar="PATH", help=DATA_HELP)
    ap = r_sub.add_parser("apply", help="put a checked report on the card",
                          description="Put the checked report on CARD (a restore point is saved) and print the card.")
    ap.add_argument("card", metavar="CARD")
    ap.add_argument("--by", default="ap2 review", help=BY_HELP.replace("ap2)", "ap2 review)"))
    ap.add_argument("--data", metavar="PATH", help=DATA_HELP)
    sy = r_sub.add_parser("sync", help="read the PR's state and close the card when it is done",
                          description="Read the PR on GitHub and print its state: merged, closed, reviewed, "
                                      "request_removed (these move the card to done), not_reviewed, new_commits or "
                                      "current. Records the head on the card.")
    sy.add_argument("card", metavar="CARD")
    sy.add_argument("--data", metavar="PATH", help=DATA_HELP)


def cmd_review(args) -> int:
    from assistant import cards, prreview

    if args.review_command == "verdict":
        data = datadir.resolve(args.data)
        out = prreview.verdict_of(_read_review_file(args.file), cards.languages(config.load(data)))
        _print_json(out)
        return 1 if out["errors"] else 0
    if args.review_command == "check":
        out = prreview.check(datadir.resolve(args.data), args.card)
        _print_json(out)
        return 0 if out["ok"] else 1
    data = datadir.ensure(datadir.resolve(args.data))
    if args.review_command == "plan":
        out = prreview.plan(data, args.card, args.stage, args.checkout)
    elif args.review_command == "apply":
        out = prreview.apply(data, args.card, by=args.by)
    else:
        out = prreview.sync(data, args.card)
    _print_json(out)
    return 0


def _read_review_file(name: str):
    from assistant import prreview, sources

    if name == "-" and sys.stdin is None:
        raise prreview.ReviewError("cannot read -: stdin is closed")
    try:
        if name == "-":
            raw = sys.stdin.buffer.read(sources.MAX_SOURCE + 1) if getattr(sys.stdin, "buffer", None) is not None \
                else sys.stdin.read(sources.MAX_SOURCE + 1).encode("utf-8")
            if len(raw) > sources.MAX_SOURCE:
                raise prreview.ReviewError(f"-: larger than {sources.MAX_SOURCE} bytes")
        else:
            raw = sources.read_capped(name, sources.MAX_SOURCE, f"{name!r}: larger than {sources.MAX_SOURCE} bytes")
        return json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as e:
        why = "nested too deeply" if isinstance(e, RecursionError) else e
        raise prreview.ReviewError(f"cannot read {name!r}: {why}") from e


COMMANDS["review"] = cmd_review


# --- ap2 scan (scan sessions, design 5.3) ------------------------------------------------------------------------
# Starting the session itself is a later slice; these are its plan, its PostToolUse hook and its take.

def _scan_parser(sub) -> None:
    sc = sub.add_parser("scan", help="plan a scan session, save its replies, take its cards",
                        description="The scan-session contract for via: mcp, connector and skill sources: plan "
                                    "what the session calls, save each tool reply (the hook), and turn the finished "
                                    "scan into cards.")
    sc_sub = sc.add_subparsers(dest="scan_command", metavar="<scan command>", title="scan commands")
    sc_sub.required = True
    pl = sc_sub.add_parser("plan", help="write a scan plan and print it with its brief",
                           description="Write sources/.scan/<stamp>/plan.json for the sources NAME (none: every "
                                       "mcp, connector and skill source) and print it: the calls, the errors and "
                                       "the brief. Starts no process.")
    pl.add_argument("names", nargs="*", metavar="NAME")
    pl.add_argument("--data", metavar="PATH", help=DATA_HELP)
    sv = sc_sub.add_parser("save-raw", help="the PostToolUse hook: log one tool reply of a scan",
                           description="Read one PostToolUse hook payload (tool_name, tool_input, tool_response) "
                                       "from stdin and append it to the scan folder's calls.jsonl when the plan "
                                       "names its tool.")
    sv.add_argument("--plan", required=True, metavar="FILE", help="the scan's plan.json")
    tk = sc_sub.add_parser("take", help="turn a finished scan into cards",
                           description="Build each planned call's raw output from the scan log and read it "
                                       "(ap2 source read --raw), printing each read and the errors.")
    tk.add_argument("plan", metavar="PLAN", help="the scan's plan.json")
    tk.add_argument("--dry-run", action="store_true", help="show what each item would become; save nothing")
    tk.add_argument("--data", metavar="PATH", help=DATA_HELP)


def cmd_scan(args) -> int:
    from assistant import engine as engine_module, scan

    if args.scan_command == "save-raw":
        if sys.stdin is None:
            raise scan.ScanError("cannot read the hook payload: stdin is closed")
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        try:
            payload = stream.read(engine_module.MAX_OUTPUT + 1)
        except OSError as e:
            raise scan.ScanError(f"cannot read the hook payload: {e}") from e
        if isinstance(payload, str):
            payload = payload.encode("utf-8", errors="surrogateescape")
        _print_json(scan.save_raw(args.plan, payload))
        return 0
    path = datadir.resolve(args.data)
    if args.scan_command == "plan":
        _print_json(scan.plan(datadir.ensure(path), args.names))
        return 0
    if not args.dry_run:
        datadir.ensure(path)
    _print_json(scan.take(path, args.plan, dry_run=args.dry_run))
    return 0


COMMANDS["scan"] = cmd_scan


# --- ap2 serve (the local page, design 9.1) --------------------------------------------------------------------
# Serves the page and its JSON on 127.0.0.1 only, until Ctrl-C or SIGTERM (exit 0). A port that cannot be used is
# a ConfigError: one `ap2: cannot listen on 127.0.0.1:<port>: ...` line, exit 1.

def _serve_port(text: str) -> int:
    import re
    if not re.fullmatch(r"[0-9]{1,5}", text, re.ASCII) or int(text) > 65535:
        raise argparse.ArgumentTypeError(f"not a port from 0 to 65535: {text!r}")
    return int(text)


def _serve_parser(sub) -> None:
    from assistant import server
    s = sub.add_parser("serve", help="serve the local page on 127.0.0.1",
                       description="Serve the local page and its JSON on 127.0.0.1 (this Mac only), until Ctrl-C or "
                                   "SIGTERM. Prints one JSON line first: url, data_dir and mode. Read-only, but for "
                                   "saving a question on a card, answering today's cap card and switching the "
                                   "access profile; in live mode a pause answer stops the running workers, and in "
                                   "both modes a switch to a gateway profile may run its models command. Sets up "
                                   "the data folder's layout on first use.")
    s.add_argument("--port", type=_serve_port, default=server.PORT, metavar="N",
                   help=f"the port (default: {server.PORT}; 0 takes a free one)")
    s.add_argument("--data", metavar="PATH", help=DATA_HELP)


def cmd_serve(args) -> int:
    import signal
    import threading
    from assistant import server
    data = datadir.ensure(datadir.resolve(args.data))
    cfg = config.load(data)  # a bad config.yaml is refused before listening
    srv = server.make_server(data, port=args.port)

    def stop(signum, frame):
        # Ask the loop to stop instead of raising here: an exception raised in a signal handler can land inside
        # the server's request handling, which logs and swallows it, and the server would keep running.
        # shutdown() waits for serve_forever() to return, so it runs in its own thread, not on this one (the
        # thread running serve_forever()). A second SIGTERM just asks again.
        threading.Thread(target=srv.shutdown, daemon=True).start()

    old = signal.signal(signal.SIGTERM, stop)
    try:
        _write_out(json.dumps({"url": srv.url, "data_dir": str(data), "mode": cfg.mode}, sort_keys=True,
                              ensure_ascii=False))
        srv.serve_forever()  # returns within its poll interval after a SIGTERM
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, old)
        srv.server_close()
    return 0


COMMANDS["serve"] = cmd_serve


# --- ap2 phone (the phone page's data, design 9.2) -----------------------------------------------------------------
# Builds the data only: publishing it is the dispatcher's step (live mode only), so there is no --publish here.

def _phone_parser(sub) -> None:
    p = sub.add_parser("phone", help="build the phone page's data", description="The phone page's data.")
    ps = p.add_subparsers(dest="phone_command", metavar="<phone command>", title="phone commands")
    ps.required = True
    b = ps.add_parser("build", help="print the phone page's data as JSON",
                      description="Print the phone page's data (open cards, recent History, full cards, under the "
                                  "phone_* settings in config.yaml) as one JSON document, or write it to --out. "
                                  "Read-only: it writes nothing else and publishes nothing; publishing is the "
                                  "dispatcher's step, in live mode only. Sets up the data folder's layout on "
                                  "first use.")
    b.add_argument("--out", metavar="PATH", help="write the JSON to this file (replaced whole) instead of stdout")
    b.add_argument("--data", metavar="PATH", help=DATA_HELP)


def cmd_phone(args) -> int:
    from assistant import phone
    data = datadir.ensure(datadir.resolve(args.data))
    text = json.dumps(phone.build(data), sort_keys=True, ensure_ascii=False) + "\n"
    if args.out is None:
        out = getattr(sys.stdout, "buffer", None)
        if out is None:  # a stream with no bytes underneath (stdout closed is None too): as _write does
            _write(text, sys.stdout)
            return 0
        try:
            out.write(text.encode("utf-8"))
            out.flush()
        except OSError as e:
            _to_devnull(out)
            if isinstance(e, BrokenPipeError):
                raise
            raise _OutputError(e) from e
        return 0
    _write_file_whole(Path(args.out), text.encode("utf-8"))
    return 0


def _write_file_whole(path: Path, body: bytes) -> None:
    """Write `body` to a temp file next to `path`, then rename it over `path`, so a reader never sees half a file.
    Raises ConfigError naming the path."""
    import tempfile
    folder = path.parent
    if not folder.is_dir():
        raise ConfigError(f"--out {path}: the folder {folder} does not exist")
    if path.is_dir():
        raise ConfigError(f"--out {path}: is a folder")
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(folder), prefix=f".{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
    except OSError as e:
        raise ConfigError(f"--out {path}: cannot write: {e}") from e
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


COMMANDS["phone"] = cmd_phone


# --- ap2 import-v1 (design 10, 12 "v1 import") --------------------------------------------------------------------
# v1's folder is only read; cards are written in the data folder only, so this runs in shadow mode.

def _import_v1_parser(sub) -> None:
    im = sub.add_parser("import-v1", help="copy v1 projects into cards, read-only",
                        description="Read each v1 project (its config.json and state.json) and make or update its "
                                    "cards: one project card, one code card per task, one question card per open "
                                    "ask or gate. v1's files are only read. Prints the report as JSON; exits 1 "
                                    "after it when a project could not be read.")
    im.add_argument("--from", dest="from_dir", metavar="DIR",
                    help="v1's data folder, read only (default: $AUTOPILOT_DATA, else ~/.claude/autopilot-data)")
    im.add_argument("--project", action="append", metavar="NAME", help="import only this project (repeatable)")
    im.add_argument("--dry-run", action="store_true", help="show what each card would become; write nothing")
    im.add_argument("--data", metavar="PATH", help=DATA_HELP)


def cmd_import_v1(args) -> int:
    from assistant import importv1

    out = importv1.run(datadir.resolve(args.data), args.from_dir, args.project, dry_run=args.dry_run)
    _print_json(out)
    if out["refused"]:
        names = ", ".join(r["name"] for r in out["refused"])
        _write(f"ap2: could not read the v1 project{'s' if len(out['refused']) > 1 else ''} {names}; "
               f"see refused in the report\n", sys.stderr)
        return 1
    return 0


COMMANDS["import-v1"] = cmd_import_v1


def main(argv: list) -> int:
    if argv[:1] == ["hook-park"]:  # runs before every worker tool call: skip the parser
        from assistant import parkhook
        return parkhook.main(argv[1:], sys.stdin)
    if argv[:1] == ["hook-guard"]:  # runs before every worker Bash call: skip the parser
        from assistant import guard
        return guard.main(argv[1:], sys.stdin)
    if argv[:1] == ["hook-write"]:  # runs before a read-only worker's Write call: skip the parser
        from assistant import workers
        return workers.write_hook(argv[1:], sys.stdin)
    # sys.stderr is None when stderr is closed; print(file=None) would then write to stdout, so say nothing.
    parser = _parser()
    try:
        args = parser.parse_args(argv)  # help and argument errors exit here (0 and 2), through _Parser
        if args.command is None:
            if sys.stderr is not None:
                parser.print_help(sys.stderr)
            return 2
        code = COMMANDS[args.command](args)
    except (ConfigError, CardError, WorkerError, ShadowModeError) as e:
        try:
            _write(f"ap2: {e}\n", sys.stderr)  # a closed stderr (None) gets nothing
        except (BrokenPipeError, _OutputError):
            pass  # stderr's reader is gone or its write failed; the line is lost, ap2 still exits 1
        return 1
    except BrokenPipeError:
        # The reader went away (`ap2 config | true`); the data folder is still set up.
        return 1
    except _OutputError as e:
        # e.g. `ap2 config > /dev/full` (disk full); the data folder is still set up. If stderr is the stream that
        # failed (`ap2 2> /dev/full`), this line cannot be shown either.
        try:
            _write(f"ap2: cannot write the output: {e}\n", sys.stderr)
        except (OSError, _OutputError):
            pass
        return 1
    return code
