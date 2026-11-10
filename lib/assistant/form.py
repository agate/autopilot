"""The form every source item fills (design 5.2), the path language of `fill: paths` (5.3), and the `{field}`
patterns of `link` and `key` (5.4), with the derived fields they may name.

Path language (a small, jq-like subset; no model decides any field):

- A path starts with `.`. `.` alone is the whole item.
- `.name` takes a key of an object; `."a key"` quotes a key holding other characters (dots, spaces). A name is
  letters, digits, `_` and `-`.
- `[]` takes every item of a list (`.incidents[]`, or `.[]` on a list at the top).
- `[text]` keeps the items of a list that hold `text`, word for word, as a text value at any depth
  (`.fields.issuelinks[is blocked by].key`). `[a.b=text]` keeps the items whose value at `.a.b` is `text`
  (`[type.inward=is blocked by]`). A filter is never a number: `[0]` is refused (no indexes).
- A step that meets the wrong type (a key on a list, `[]` on an object) or a missing key gives nothing; the
  field is then missing.

Records: the part of the `id` path up to and including its first `[]` is the item path; every other path must
start with it, and one record is made per item. With no `[]` in `id`, the reply is the items when it is a list,
else it is one item. Only `depends_on` may fan out further (it is a list); every other field must give one text
or integer (an integer is written as text).
"""
from __future__ import annotations

import re
from urllib.parse import quote

FIELDS = ("id", "thread_id", "author", "at", "text", "place", "place_id", "status", "depends_on")
REQUIRED = ("id", "at", "text")
LIST_FIELDS = ("depends_on",)
# Fields a pattern may name besides the form's: one place, so link and key patterns agree everywhere.
DERIVED = ("id_digits", "place_id", "source")

NAME = re.compile(r"[A-Za-z0-9_-]+")
FIELD_PATH = re.compile(r"[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*\Z")
PATTERN_FIELD = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
MAX_DEPTH = 64

KEY, EACH, MATCH = "key", "each", "match"


class PathError(ValueError):
    """A path or a pattern is not in the language. The message says where and why."""


def parse_path(text) -> tuple:
    """Parse a path into steps: ("key", name), ("each",), ("match", field_steps or None, value)."""
    if not (isinstance(text, str) and text.startswith(".")):
        raise PathError(f"a path starts with '.', not {text!r}")
    if text == ".":
        return ()
    steps, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "." and i + 1 < n and text[i + 1] == "[":
            i += 1  # `.[]` and `.[x]`: the dot before a bracket is optional
            continue
        if c == ".":
            i += 1
            if i < n and text[i] == '"':
                end = text.find('"', i + 1)
                if end < 0:
                    raise PathError(f"{text!r}: unclosed quote at {i}")
                if end == i + 1:
                    raise PathError(f"{text!r}: empty quoted name at {i}")
                steps.append((KEY, text[i + 1:end]))
                i = end + 1
                continue
            m = NAME.match(text, i)
            if not m:
                raise PathError(f"{text!r}: expected a name after '.' at {i}")
            steps.append((KEY, m.group()))
            i = m.end()
        elif c == "[":
            end = text.find("]", i)
            if end < 0:
                raise PathError(f"{text!r}: unclosed '[' at {i}")
            inner = text[i + 1:end]
            if inner == "":
                steps.append((EACH,))
            elif inner.strip().isdigit():
                raise PathError(f"{text!r}: [{inner}] looks like an index; indexes are not supported")
            elif inner != inner.strip():
                raise PathError(f"{text!r}: [{inner}] starts or ends with a space")
            else:
                left, eq, right = inner.partition("=")
                if eq and FIELD_PATH.match(left) and right:
                    steps.append((MATCH, tuple(left.split(".")), right))
                else:
                    steps.append((MATCH, None, inner))
            i = end + 1
        else:
            raise PathError(f"{text!r}: unexpected {c!r} at {i}")
    return tuple(steps)


def fans_out(steps: tuple) -> bool:
    return any(s[0] in (EACH, MATCH) for s in steps)


def item_prefix(id_steps: tuple) -> tuple:
    """The steps of the `id` path up to and including its first `[]`; () when it has none."""
    for n, step in enumerate(id_steps):
        if step[0] == EACH:
            return id_steps[:n + 1]
    return ()


def _holds(value, text: str, depth: int = 0) -> bool:
    if depth > MAX_DEPTH:
        return False
    if isinstance(value, str):
        return value == text
    if isinstance(value, dict):
        return any(_holds(v, text, depth + 1) for v in value.values())
    if isinstance(value, list):
        return any(_holds(v, text, depth + 1) for v in value)
    return False


def _at(value, names: tuple):
    for name in names:
        if not isinstance(value, dict) or name not in value:
            return None
        value = value[name]
    return value


def _matches(item, step) -> bool:
    _, field, text = step
    if field is None:
        return _holds(item, text)
    return _at(item, field) == text


MISSING = object()


def evaluate(steps: tuple, value):
    """The value at `steps`: one value (MISSING when absent), or, when the path fans out, a list of values."""
    current = [value]
    for step in steps:
        nxt = []
        for v in current:
            if step[0] == KEY:
                if isinstance(v, dict) and step[1] in v:
                    nxt.append(v[step[1]])
            elif isinstance(v, list):
                nxt.extend(v if step[0] == EACH else [x for x in v if _matches(x, step)])
        current = nxt
    if fans_out(steps):
        return current
    return current[0] if current else MISSING


def items(prefix: tuple, reply) -> list:
    """The items of a reply: the list at the item path, or the reply itself (a list's items, or one item)."""
    if prefix:
        return evaluate(prefix, reply)
    return list(reply) if isinstance(reply, list) else [reply]


def as_text(value, what: str) -> str:
    """A field value as text: a text or an integer. Raises ValueError with the reason."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{what} is {_kind(value)}, not a text")
    return str(value)


def _kind(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true/false"
    return {dict: "an object", list: "a list", float: "a decimal number"}.get(type(value), type(value).__name__)


def fill_item(paths: dict, prefix: tuple, item) -> dict:
    """Fill one record from one item. `paths` maps each form field to its steps (full paths, item path
    included). Raises ValueError naming the first field that is missing or has the wrong type."""
    record = {}
    for field, steps in paths.items():
        value = evaluate(steps[len(prefix):], item)
        if field in LIST_FIELDS:
            values = value if isinstance(value, list) else ([] if value is MISSING or value is None else [value])
            if values:
                record[field] = [as_text(v, f"form.{field}: an item") for v in values]
            continue
        if value is MISSING or value is None:
            if field in REQUIRED:
                raise ValueError(f"form.{field} missing")
            continue
        text = as_text(value, f"form.{field}")
        if field in REQUIRED and not text.strip():
            raise ValueError(f"form.{field} is empty")
        record[field] = text
    return record


def check_record(record) -> dict:
    """Check a record filled elsewhere (a `fill: ai` scan session): the form's fields only, the required ones
    non-empty text. Returns the record; raises ValueError with the reason."""
    if not isinstance(record, dict):
        raise ValueError(f"a record is an object, not {_kind(record)}")
    out = {}
    for field, value in record.items():
        if field == "category":
            continue
        if field not in FIELDS:
            raise ValueError(f"unknown field {field!r}")
        if field in LIST_FIELDS:
            if not isinstance(value, list):
                raise ValueError(f"form.{field} is {_kind(value)}, not a list")
            out[field] = [as_text(v, f"form.{field}: an item") for v in value]
        elif value is not None:
            out[field] = as_text(value, f"form.{field}")
    for field in REQUIRED:
        if not out.get(field, "").strip():
            raise ValueError(f"form.{field} {'is empty' if field in out else 'missing'}")
    return out


def derived(record: dict, source: str) -> dict:
    """The values a pattern may name: the record's fields, plus
    `id_digits` (the id without its dots: Slack's `1730000000.123456` gives `1730000000123456`),
    `place_id` (the record's own `place_id` when it has one, else `place`), and
    `source` (the source's name)."""
    values = {k: v for k, v in record.items() if k not in LIST_FIELDS}
    if "id" in record:
        values["id_digits"] = record["id"].replace(".", "")
    if "place_id" not in values and "place" in record:
        values["place_id"] = record["place"]
    values["source"] = source
    return values


def parse_pattern(text) -> tuple:
    """Split a `{field}` pattern into parts: ("text", s) and ("field", name). Raises PathError."""
    if not isinstance(text, str) or not text:
        raise PathError(f"a pattern is a non-empty text, not {text!r}")
    parts, i = [], 0
    for m in PATTERN_FIELD.finditer(text):
        literal = text[i:m.start()]
        if "{" in literal or "}" in literal:
            raise PathError(f"{text!r}: a brace that is not a {{field}}")
        if literal:
            parts.append(("text", literal))
        if m.group(1) in LIST_FIELDS:
            raise PathError(f"{text!r}: {{{m.group(1)}}} is a list and cannot be named in a pattern")
        parts.append(("field", m.group(1)))
        i = m.end()
    rest = text[i:]
    if "{" in rest or "}" in rest:
        raise PathError(f"{text!r}: a brace that is not a {{field}}")
    if rest:
        parts.append(("text", rest))
    return tuple(parts)


def fill_pattern(parts: tuple, values: dict, url: bool = False) -> str:
    """Fill a parsed pattern. With `url`, each value is percent-encoded (so a value cannot change the link's
    shape). Raises ValueError naming the first field the values lack or hold only as empty text."""
    out = []
    for kind, s in parts:
        if kind == "text":
            out.append(s)
        elif s not in values:
            raise ValueError(f"{{{s}}} missing")
        elif not values[s].strip():
            raise ValueError(f"{{{s}}} is empty")  # an empty part would merge different items into one key
        else:
            out.append(quote(values[s], safe="") if url else values[s])
    return "".join(out)
