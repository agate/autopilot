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
import tempfile

TIMEOUT_S = 30  # an interactive login shell may take several seconds; a launch takes longer anyway
SH_FAMILY = ("zsh", "bash", "sh", "ksh")
UNSUPPORTED = "unsupported shell"
# $1 is the folder, $2 the file the environment goes to. A file, not stdout: a profile's background job may hold
# stdout open long after the shell is done. direnv exports in sh syntax, and only for an allowed .envrc.
SCRIPT = ('cd -- "$1" || exit 97\n'
          'if command -v direnv >/dev/null 2>&1; then eval "$(direnv export bash 2>/dev/null)"; fi\n'
          'exec env -0 > "$2"\n')


def enabled() -> bool:
    return os.environ.get("AUTOPILOT_PROJECT_ENV") != "off"


def _login_shell() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_shell or "/bin/zsh"
    except KeyError:
        return os.environ.get("SHELL") or "/bin/zsh"


def _parse(raw: bytes):
    env = {}
    for item in raw.split(b"\0"):
        k, eq, v = item.partition(b"=")
        if eq and k:
            env[os.fsdecode(k)] = os.fsdecode(v)
    return env if env.get("PATH") else None


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:  # the group is already gone
        pass


def read(cwd: str, shell: str = None) -> tuple:
    """(environment, None) as the user's terminal has it in `cwd`, or (None, why it could not be read)."""
    if not enabled():
        return None, "off"
    if shell is None:
        shell = _login_shell()
        if os.path.basename(shell) not in SH_FAMILY:  # fish and others do not run this sh script
            return None, UNSUPPORTED
    fd, out = tempfile.mkstemp(prefix="autopilot-env-")
    os.close(fd)
    try:
        try:
            proc = subprocess.Popen([shell, "-lic", SCRIPT, "autopilot-env", str(cwd), out],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            return None, str(e)
        try:
            rc = proc.wait(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            rc = None
        _kill_group(proc.pid)  # also ends a profile's leftover background jobs
        if rc is None:
            proc.wait()
            return None, f"timed out after {TIMEOUT_S}s"
        if rc != 0:
            return None, f"exit {rc}"
        with open(out, "rb") as f:
            env = _parse(f.read())
        return (env, None) if env else (None, "no environment printed")
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def load(cwd: str, shell: str = None):
    """The environment the user's terminal has in `cwd`, or None when it cannot be read."""
    return read(cwd, shell)[0]
