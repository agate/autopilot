"""A project's own environment: what the user's login shell holds after `cd` into the project folder.

The gate runs under launchd with a small fixed environment. A background session started from there would get
launchd's PATH but none of the variables the user's own terminal sets around it (a flox or nix shell, a direnv
`.envrc`, a CA bundle such as NIX_SSL_CERT_FILE), so a tool from that PATH can half-work: `git fetch` failing
with "unable to get local issuer certificate" while `gh` works. Each session therefore starts with the
environment of the folder it runs in, read the way the user's terminal would build it: the login shell, `cd`,
then the folder's direnv when it has one. Nothing is configured globally. When the shell cannot be read (not an
sh-family shell, a broken profile, a timeout), the caller keeps the environment it has.

AUTOPILOT_PROJECT_ENV=off turns this off.
"""
from __future__ import annotations

import os
import pwd
import signal
import subprocess

TIMEOUT_S = 30  # an interactive login shell may take several seconds; a launch takes longer anyway
MARK = "__AUTOPILOT_PROJECT_ENV__"
SH_FAMILY = ("zsh", "bash", "sh", "ksh")
# $1 is the folder. direnv exports in sh syntax for every sh-family shell, and only for an allowed .envrc.
SCRIPT = ('cd -- "$1" || exit 97\n'
          'if command -v direnv >/dev/null 2>&1; then eval "$(direnv export bash 2>/dev/null)"; fi\n'
          f"printf '%s\\0' {MARK}\n"
          "exec env -0\n")


def _login_shell() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_shell or "/bin/zsh"
    except KeyError:
        return os.environ.get("SHELL") or "/bin/zsh"


def _parse(out: bytes):
    head, sep, body = out.rpartition(MARK.encode() + b"\0")
    if not sep:
        return None
    env = {}
    for item in body.split(b"\0"):
        k, eq, v = item.partition(b"=")
        if eq and k:
            env[k.decode(errors="replace")] = v.decode(errors="replace")
    return env if env.get("PATH") else None


def enabled() -> bool:
    return os.environ.get("AUTOPILOT_PROJECT_ENV") != "off"


def load(cwd: str, shell: str = None):
    """The environment the user's terminal has in `cwd`, or None when it cannot be read."""
    if not enabled():
        return None
    if shell is None:
        shell = _login_shell()
        if os.path.basename(shell) not in SH_FAMILY:  # fish and others do not run this sh script
            return None
    try:
        proc = subprocess.Popen([shell, "-lic", SCRIPT, "autopilot-env", str(cwd)], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return None
    try:
        out, _ = proc.communicate(timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # The whole group: a profile's own background child must not keep the pipe (and this call) open.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        return None
    if proc.returncode != 0:
        return None
    return _parse(out)
