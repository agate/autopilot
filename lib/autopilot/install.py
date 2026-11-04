from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from autopilot import cli, config, paths

LABEL = "com.autopilot.gate"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
SYSTEM_PATH = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]


def capture_tools() -> dict:
    tools = {t: shutil.which(t) for t in ("claude", "gh", "git", "python3")}
    cfg = config.load_global()
    cfg["tools"] = {**cfg.get("tools", {}), **{k: v for k, v in tools.items() if v}}
    config.save_global(cfg)
    return tools


def render_plist(program: str, interval: int, path_dirs: list, log_dir: Path) -> bytes:
    path = []
    for d in list(path_dirs) + SYSTEM_PATH:
        if d and d not in path:
            path.append(d)
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [program, "gate"],
        "StartInterval": interval,
        "RunAtLoad": True,
        "StandardOutPath": str(Path(log_dir) / "gate.log"),
        "StandardErrorPath": str(Path(log_dir) / "gate.log"),
        "EnvironmentVariables": {"PATH": ":".join(path), "AUTOPILOT_DATA": str(paths.data_root())},
    })


def command_for_user() -> str:
    return f'"{paths.package_root() / "bin" / "ap"}" install'


def _launchctl(*args) -> tuple:
    out = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)
    return out.returncode, (out.stdout + out.stderr).strip()


def is_installed() -> bool:
    if not PLIST.exists():
        return False
    rc, _ = _launchctl("print", f"gui/{os.getuid()}/{LABEL}")
    return rc == 0


def install() -> str:
    tools = capture_tools()
    dirs = [str(Path(p).parent) for p in tools.values() if p]
    gcfg = config.load_global()
    paths.data_root().mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(render_plist(str(paths.package_root() / "bin" / "ap"), gcfg["gate_interval_s"], dirs,
                                   paths.data_root()))
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{LABEL}")
    rc, msg = _launchctl("bootstrap", domain, str(PLIST))
    if rc != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {msg}")
    return f"installed {PLIST}; tools: " + ", ".join(f"{k}={v}" for k, v in tools.items())


@cli.command("install")
def _install(args: list) -> int:
    if "--print" in args:
        print(command_for_user())
        return 0
    if "--check" in args:
        ok = is_installed()
        print("installed" if ok else "not installed")
        return 0 if ok else 1
    print(install())
    return 0


@cli.command("uninstall")
def _uninstall(args: list) -> int:
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    try:
        PLIST.unlink()
    except FileNotFoundError:
        pass
    print(f"removed {PLIST}")
    return 0


def kick() -> int:
    """Ask launchd to run the gate now. Never -k: a running gate is left alone."""
    rc, msg = _launchctl("kickstart", f"gui/{os.getuid()}/{LABEL}")
    if rc != 0:
        print(msg, file=sys.stderr)
    return rc


@cli.command("kick")
def _kick(args: list) -> int:
    return kick()
