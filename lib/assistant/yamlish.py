"""A small YAML subset reader, standard library only.

In the subset (enough for config.yaml, source files and routines):

- block maps (`key: value`) and block lists (`- item`), including lists of maps and a list written at its
  parent key's indent;
- flow lists `[a, b]` and flow maps `{a: 1, b: x}`, nested, on one line;
- plain, 'single-quoted' and "double-quoted" strings; ints, floats, true/false, null/~/empty;
- `#` comments, full-line and trailing (a `#` starts a comment at the start of a line or after a space or tab,
  never inside quotes).

Everything else raises `YamlError`, whose message starts with `line N:` and says why: tabs in indentation,
a tab after a `-` or `?` indicator, anchors, aliases, tags, `|`/`>` multi-line scalars, `---`/`...` document markers, bad indentation, unclosed
quotes or brackets, duplicate keys, complex `?` keys, a `- ` list item inside a flow collection, a plain value
holding `: `, and a plain value starting with `@`, `` ` ``, `%`, `]`, `}`, `,` or `? ` (quote it).
"""
from __future__ import annotations

import re

__all__ = ["YamlError", "load", "parse"]

INT = re.compile(r"[-+]?[0-9]+\Z")
FLOAT = re.compile(r"[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?\Z")
TRUE, FALSE, NULL = ("true", "True", "TRUE"), ("false", "False", "FALSE"), ("null", "Null", "NULL", "~")
RESERVED = {"&": "anchors ('&') are not supported", "*": "aliases ('*') are not supported",
            "!": "tags ('!') are not supported", "|": "multi-line scalars ('|') are not supported",
            ">": "multi-line scalars ('>') are not supported", "%": "directives ('%') are not supported",
            "@": "'@' cannot start a plain value; quote it", "`": "'`' cannot start a plain value; quote it",
            "]": "']' cannot start a plain value; quote it", "}": "'}' cannot start a plain value; quote it",
            ",": "',' cannot start a plain value; quote it"}
ESCAPES = {"\\": "\\", '"': '"', "/": "/", "n": "\n", "t": "\t", "r": "\r", "0": "\0", " ": " ",
           "b": "\b", "f": "\f", "e": "\x1b", "a": "\a", "v": "\v"}
HEX = {"x": 2, "u": 4, "U": 8}


class YamlError(ValueError):
    """The text is outside the subset. `line` is 1-based; `reason` says what is wrong."""

    def __init__(self, line: int, reason: str):
        super().__init__(f"line {line}: {reason}")
        self.line = line
        self.reason = reason


def load(text: str) -> dict:
    """Parse `text` into a dict (an empty document is `{}`)."""
    return parse(text)[0]


def parse(text: str) -> tuple:
    """Parse `text`; return `(data, key_lines)`, where key_lines maps each map key's path (a tuple) to its line."""
    lines = _lines(text)
    if not lines:
        return {}, {}
    p = _Parser(lines)
    first = lines[0]
    if _is_item(first.text):
        raise YamlError(first.no, "the top level must be a map of 'key: value' lines, not a list")
    if _split_key(first.text, first.no) is None:
        raise YamlError(first.no, "the top level must be a map of 'key: value' lines")
    data = p.block(first.indent, ())
    if p.i < len(lines):
        ln = lines[p.i]
        raise YamlError(ln.no, f"bad indentation: expected {first.indent} spaces, found {ln.indent}")
    return data, p.key_lines


class _Line:
    __slots__ = ("no", "indent", "text")

    def __init__(self, no: int, indent: int, text: str):
        self.no, self.indent, self.text = no, indent, text


def _lines(text: str) -> list:
    out = []
    # Only \r\n, \r and \n end a line (str.splitlines() would also split on \f, \x85, \u2028 and others).
    for no, raw in enumerate(text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        body = raw.lstrip(" ")
        indent = len(raw) - len(body)
        if not body.strip():
            continue
        if body.startswith("\t"):
            raise YamlError(no, "tabs are not allowed in indentation; use spaces")
        body = _strip_comment(body, no).rstrip()
        if not body:
            continue
        if indent == 0 and (body == "---" or body == "..." or body.startswith(("--- ", "... "))):
            raise YamlError(no, "document markers ('---', '...') are not supported; use one document per file")
        out.append(_Line(no, indent, body))
    return out


def _strip_comment(s: str, no: int) -> str:
    """Cut a trailing comment. A quote opens a string only where a value or key starts (see `_value_start`), so the
    quote in `don't`, `rock 'n roll`, `well-'known` or `Bob, 'the builder` stays plain text."""
    i, n, depth, quote_end = 0, len(s), 0, -1
    while i < n:
        c = s[i]
        if c == "#" and (i == 0 or s[i - 1] in " \t"):
            return s[:i]
        if c in "\"'" and _value_start(s[:i], depth, quote_end):
            i = quote_end = _quote_end(s, i, no)
        elif c in "[{" and (depth or _value_start(s[:i], 0, quote_end)):
            depth += 1
        elif c in "]}" and depth:
            depth -= 1
        i += 1
    return s


def _value_start(before: str, depth: int, quote_end: int) -> bool:
    """True when a value or key may start after the text `before`. Outside brackets (`depth` 0): at the line's
    start, after only the leading block indicators `- ` / `? `, or after `: `. Inside a flow collection: after
    `[`, `{`, `,`, or a `:` followed by a space or tab, or a `:` that follows the closing quote at `quote_end`
    with only spaces or tabs between (JSON style `"a":"b"`, `"a" :"b"`)."""
    if not depth:
        return re.fullmatch(r"(?:[-?] +)*", before) is not None or re.search(r":[ \t]+\Z", before) is not None
    stripped = before.rstrip(" \t")
    last = stripped[-1:]
    if last in ("[", "{", ","):
        return True
    if last != ":":
        return False
    return stripped != before or len(stripped[:-1].rstrip(" \t")) - 1 == quote_end


def _quote_end(s: str, i: int, no: int) -> int:
    """Index of the quote that closes the string opened at s[i]."""
    q, j = s[i], i + 1
    while j < len(s):
        if q == '"' and s[j] == "\\":
            j += 2
            continue
        if s[j] == q:
            if q == "'" and j + 1 < len(s) and s[j + 1] == "'":
                j += 2
                continue
            return j
        j += 1
    raise YamlError(no, f"unclosed quote ({q}); quoted strings must end on the same line")


def _is_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")


def _check_indicator_tab(text: str, no: int) -> None:
    """YAML reads `-` or `?` followed by a tab as an indicator; refuse it rather than read it as plain text."""
    if text.startswith(("-\t", "?\t")):
        raise YamlError(no, "a tab after '-' or '?' is not supported; use a space")


def _split_key(text: str, no: int):
    """`key: rest` -> (key, rest); None when the text is not a map entry. Keys are always strings."""
    _check_indicator_tab(text, no)
    if text == "?" or text.startswith("? "):
        raise YamlError(no, "complex keys ('?') are not supported")
    if text[0] in "\"'":
        end = _quote_end(text, 0, no)
        after = text[end + 1:]
        if re.match(r":(\s|\Z)", after):
            return _quoted(text[:end + 1], no), after[1:].strip()
        if after.lstrip().startswith(":"):
            raise YamlError(no, "a quoted key must be followed by ': '")
        return None
    if text[0] in "[{":
        if re.match(r"[\[{].*[\]}]\s*:(\s|\Z)", text):
            raise YamlError(no, "flow collections cannot be keys")
        return None
    m = re.search(r":(\s|\Z)", text)
    if not m:
        return None
    key = text[:m.start()].rstrip()
    _check_plain(key, no)
    return key, text[m.end():].strip()


def _check_plain(s: str, no: int) -> None:
    if s and s[0] in RESERVED:
        raise YamlError(no, RESERVED[s[0]])


class _Parser:
    def __init__(self, lines: list):
        self.lines = lines
        self.i = 0
        self.key_lines = {}

    def peek(self):
        return self.lines[self.i] if self.i < len(self.lines) else None

    def block(self, indent: int, path: tuple):
        ln = self.peek()
        return self.seq(indent, path) if _is_item(ln.text) else self.map(indent, path)

    def map(self, indent: int, path: tuple) -> dict:
        out, first_seen = {}, {}
        while True:
            ln = self.peek()
            if ln is None or ln.indent < indent:
                return out
            if ln.indent > indent:
                raise YamlError(ln.no, f"bad indentation: expected {indent} spaces, found {ln.indent}"
                                       " (multi-line values are not supported)")
            kv = None if _is_item(ln.text) else _split_key(ln.text, ln.no)
            if kv is None:
                raise YamlError(ln.no, "expected 'key: value' at this indent")
            key, rest = kv
            if key in first_seen:
                raise YamlError(ln.no, f"duplicate key '{key}' (first on line {first_seen[key]})")
            first_seen[key] = ln.no
            self.key_lines[path + (key,)] = ln.no
            self.i += 1
            if rest:
                out[key] = self.inline(rest, ln.no, path + (key,))
                continue
            nxt = self.peek()
            if nxt is not None and nxt.indent > indent:
                out[key] = self.block(nxt.indent, path + (key,))
            elif nxt is not None and nxt.indent == indent and _is_item(nxt.text):
                out[key] = self.seq(indent, path + (key,), at_parent=True)
            else:
                out[key] = None

    def seq(self, indent: int, path: tuple, at_parent: bool = False) -> list:
        """A block list at `indent`. `at_parent`: the list sits at its key's indent and ends at the next key."""
        out = []
        while True:
            ln = self.peek()
            if ln is None or ln.indent < indent:
                return out
            if ln.indent > indent:
                raise YamlError(ln.no, f"bad indentation: expected {indent} spaces, found {ln.indent}")
            if not _is_item(ln.text):
                if at_parent:
                    return out
                raise YamlError(ln.no, "expected a list item ('- ') at this indent")
            rest = ln.text[1:]
            content = rest.lstrip(" ")
            item_path = path + (len(out),)
            if not content:
                self.i += 1
                nxt = self.peek()
                out.append(self.block(nxt.indent, item_path) if nxt is not None and nxt.indent > indent else None)
                continue
            col = indent + 1 + (len(rest) - len(content))
            if _is_item(content) or _split_key(content, ln.no) is not None:
                # `- key: v` or `- - x`: the item is a block that starts at the content's column.
                self.lines[self.i] = _Line(ln.no, col, content)
                out.append(self.block(col, item_path))
            else:
                self.i += 1
                out.append(self.inline(content, ln.no, item_path))

    def inline(self, text: str, no: int, path: tuple):
        if text[0] in "[{":
            f = _Flow(text, no, self, path)
            value = f.value()
            f.ws()
            if f.i < len(text):
                raise YamlError(no, f"unexpected text after the closing bracket: {text[f.i:]!r}")
            return value
        if text[0] in "\"'":
            end = _quote_end(text, 0, no)
            if text[end + 1:].strip():
                raise YamlError(no, f"unexpected text after a quoted string: {text[end + 1:].strip()!r}")
            return _quoted(text, no)
        if _is_item(text):
            raise YamlError(no, "a list cannot start after 'key:' on the same line; put '- ' items on the next lines")
        _check_indicator_tab(text, no)
        if text == "?" or text.startswith("? "):
            raise YamlError(no, "complex keys ('?') are not supported")
        _check_plain(text, no)
        if re.search(r":(\s|\Z)", text):
            raise YamlError(no, "a plain value cannot contain ': '; quote the value")
        return _plain(text)


def _plain(s: str):
    if s in TRUE:
        return True
    if s in FALSE:
        return False
    if s in NULL:
        return None
    if INT.match(s):
        return int(s)
    if FLOAT.match(s):
        return float(s)
    return s


def _quoted(s: str, no: int) -> str:
    """Decode a whole quoted token, quotes included."""
    body = s[1:-1]
    if s[0] == "'":
        return body.replace("''", "'")
    out, i = [], 0
    while i < len(body):
        c = body[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        e = body[i + 1] if i + 1 < len(body) else ""
        if e in ESCAPES:
            out.append(ESCAPES[e])
            i += 2
        elif e in HEX:
            digits = body[i + 2:i + 2 + HEX[e]]
            if len(digits) != HEX[e] or not re.fullmatch(r"[0-9a-fA-F]+", digits) or int(digits, 16) > 0x10FFFF:
                raise YamlError(no, f"bad escape '\\{e}{digits}' in a double-quoted string")
            out.append(chr(int(digits, 16)))
            i += 2 + HEX[e]
        else:
            raise YamlError(no, f"unknown escape '\\{e}' in a double-quoted string")
    return "".join(out)


class _Flow:
    """Recursive-descent reader for one line's `[...]` / `{...}`."""

    def __init__(self, s: str, no: int, parser: _Parser, path: tuple):
        self.s, self.i, self.no, self.p, self.path = s, 0, no, parser, path

    def ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in " \t":
            self.i += 1

    def value(self, path=None, key=False):
        """One entry. With `key`, a plain token stays the string it reads as (keys are always strings)."""
        path = self.path if path is None else path
        self.ws()
        if self.i >= len(self.s):
            raise YamlError(self.no, "unclosed bracket: the line ended inside a flow collection")
        c = self.s[self.i]
        if c == "[":
            return self.seq(path)
        if c == "{":
            return self.map(path)
        if c in "\"'":
            end = _quote_end(self.s, self.i, self.no)
            tok = self.s[self.i:end + 1]
            self.i = end + 1
            return _quoted(tok, self.no)
        start = self.i
        while self.i < len(self.s):
            ch = self.s[self.i]
            if ch in ",[]{}" or (ch == ":" and (self.i + 1 == len(self.s) or self.s[self.i + 1] in " \t,]}")):
                break
            self.i += 1
        tok = self.s[start:self.i].strip()
        if not tok:
            raise YamlError(self.no, "empty entry in a flow collection")
        _check_indicator_tab(tok, self.no)
        if tok == "?" or tok.startswith("? "):
            raise YamlError(self.no, "complex keys ('?') are not supported")
        if tok == "-" or tok.startswith("- "):
            raise YamlError(self.no, "a block list item ('- ') cannot appear inside a flow collection")
        _check_plain(tok, self.no)
        return tok if key else _plain(tok)

    def expect_more(self, close: str) -> bool:
        """After an entry: True when a ',' leads to another entry, False when `close` ends the collection."""
        self.ws()
        if self.i >= len(self.s):
            raise YamlError(self.no, f"unclosed bracket: expected '{close}' before the end of the line")
        c = self.s[self.i]
        self.i += 1
        if c == close:
            return False
        if c != ",":
            raise YamlError(self.no, f"expected ',' or '{close}' in a flow collection, found {c!r}")
        self.ws()
        if self.i < len(self.s) and self.s[self.i] == close:  # a trailing comma
            self.i += 1
            return False
        if self.i < len(self.s) and self.s[self.i] == ",":
            raise YamlError(self.no, "empty entry in a flow collection")
        return True

    def closes_now(self, close: str) -> bool:
        self.i += 1
        self.ws()
        if self.i < len(self.s) and self.s[self.i] == close:
            self.i += 1
            return True
        return False

    def seq(self, path: tuple) -> list:
        out = []
        if self.closes_now("]"):
            return out
        while True:
            out.append(self.value(path + (len(out),)))
            if not self.expect_more("]"):
                return out

    def map(self, path: tuple) -> dict:
        out = {}
        if self.closes_now("}"):
            return out
        while True:
            self.ws()
            if self.i < len(self.s) and self.s[self.i] in "[{":
                raise YamlError(self.no, "flow collections cannot be keys")
            key = self.value(path, key=True)
            self.ws()
            if self.i >= len(self.s):
                raise YamlError(self.no, "unclosed bracket: expected '}' before the end of the line")
            if self.s[self.i] != ":":
                raise YamlError(self.no, f"expected ':' after the key '{key}' in a flow map")
            self.i += 1
            if key in out:
                raise YamlError(self.no, f"duplicate key '{key}'")
            self.p.key_lines[path + (key,)] = self.no
            self.ws()
            if self.i < len(self.s) and self.s[self.i] in ",}":
                out[key] = None
            else:
                out[key] = self.value(path + (key,))
            if not self.expect_more("}"):
                return out
