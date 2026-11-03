from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from autopilot import cli, paths

SKIP_DIRS = {".git", "__pycache__", ".superpowers"}
IGNORE_MARK = "checkpkg: ignore"
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
EMAIL_OK = ("@example.com", "noreply@anthropic.com")
TICKET = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")  # checkpkg: ignore
TICKET_OK = re.compile(r"^(PROJ-\d+|UTF-8|SHA-1|SHA-256|ISO-8601)$")
URL = re.compile(r"https?://([A-Za-z0-9.-]+)")
HOSTS_OK = ("example.com", "claude.ai", "claude.com", "anthropic.com", "apple.com", "github.com",
            "fonts.googleapis.com", "fonts.gstatic.com", "cdnjs.cloudflare.com", "cdn.jsdelivr.net", "unpkg.com",
            "localhost")


def _git(key: str) -> str:
    try:
        return subprocess.run(["git", "config", "--global", key], capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def identities() -> list:
    out = [_git("user.name"), _git("user.email")]
    if "@" in out[1]:
        out.append(out[1].split("@")[0])
    out += [str(Path.home()), os.environ.get("USER", "")]
    try:
        login = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True, timeout=15)
        if login.returncode == 0:
            out.append(login.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return sorted({i for i in out if len(i) >= 3})


def _host_ok(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in HOSTS_OK)


def scan(root: Path, idents: list) -> list:
    found = []
    lowered = [(i, i.lower()) for i in idents if i]
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            rel = str(p.relative_to(root))
            for n, line in enumerate(text.splitlines(), 1):
                if IGNORE_MARK in line:
                    continue
                low = line.lower()
                for orig, i in lowered:
                    if i in low:
                        found.append({"path": rel, "line": n, "kind": "identity", "match": orig})
                for m in EMAIL.findall(line):
                    if not m.endswith(EMAIL_OK):
                        found.append({"path": rel, "line": n, "kind": "email", "match": m})
                for m in TICKET.findall(line):
                    if not TICKET_OK.match(m):
                        found.append({"path": rel, "line": n, "kind": "ticket", "match": m})
                for host in URL.findall(line):
                    if not _host_ok(host):
                        found.append({"path": rel, "line": n, "kind": "url", "match": host})
    return found


@cli.command("check-package")
def _cmd(args: list) -> int:
    root = Path(args[0]) if args else paths.package_root()
    found = scan(root, identities())
    for f in found:
        print(f"{f['path']}:{f['line']}: {f['kind']}: {f['match']}")
    print(f"{len(found)} finding(s) in {root}", file=sys.stderr)
    return 1 if found else 0
