"""OS sandbox for shell commands run by agents (W2-sandbox).

Config (opt-in; the default ``mode: off`` changes nothing for existing users)::

    sandbox:
      mode: auto        # off | auto | bwrap | seatbelt
      network: true     # false: no network inside the sandbox
      writable: []      # extra writable paths (besides the task worktree, the workspace and temp)
      temp: true        # the temp directories are writable
      require: false    # true: refuse to run a command when no sandbox is available (instead of a note)
      gates: false      # true: run the review gates sandboxed as well (writable: gate cwd + temp)

The sandboxed command sees the whole system read-only and may write only to the given paths (the task's
deliverables worktree, the agent's workspace, the temp directory and ``writable``).

* Linux: bubblewrap (``bwrap``) — ``--ro-bind / /`` then ``--bind`` of every writable path, fresh ``/dev`` and
  ``/proc``, a new PID namespace and session, ``--unshare-net`` when the network is off.
* macOS: ``sandbox-exec`` with a generated Seatbelt profile (allow by default, deny file writes except the
  writable subpaths, optionally deny the network).
* Elsewhere, or when the tool is missing or cannot create namespaces (e.g. unprivileged containers): the
  command runs unsandboxed and the result carries a clear note — or, with ``require: true``, is refused.

Ideas from OpenAI Codex codex-rs/linux-sandbox (bwrap.rs: read-only root bind, writable re-binds, fresh
/dev and /proc, --unshare-pid/--unshare-net, --new-session, --die-with-parent) and codex-rs/core seatbelt
policies — Apache-2.0, Copyright OpenAI. Reimplemented here for Artel.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

MODES = ("off", "auto", "bwrap", "seatbelt")
SEATBELT = "/usr/bin/sandbox-exec"
_probe_lock = threading.Lock()
_probe_cache: dict[tuple, tuple[bool, str]] = {}


def settings(config: dict) -> dict:
    s = {"mode": "off", "network": True, "writable": [], "temp": True, "require": False, "gates": False}
    s.update((config or {}).get("sandbox") or {})
    if s["mode"] not in MODES:
        s["mode"] = "auto"
    return s


@dataclass
class Plan:
    """How to run one command: ``argv`` for subprocess (shell=False), the backend used, and a note."""
    argv: list
    backend: str                     # bwrap | seatbelt | none
    note: str = ""                   # set when a sandbox was asked for but is not in effect
    writable: list = field(default_factory=list)

    @property
    def sandboxed(self) -> bool:
        return self.backend != "none"


def _real(paths) -> list[str]:
    out: list[str] = []
    for p in paths:
        if not p:
            continue
        try:
            rp = os.path.realpath(os.path.expanduser(str(p)))
        except (OSError, ValueError):
            continue
        if os.path.isdir(rp) and rp not in out:
            out.append(rp)
    return out


def temp_dirs() -> list[str]:
    dirs = [tempfile.gettempdir(), "/tmp", "/var/tmp"]
    if os.environ.get("TMPDIR"):
        dirs.append(os.environ["TMPDIR"])
    return _real(dirs)


# --- Linux: bubblewrap ----------------------------------------------------------------------------------------
def bwrap_args(command: str, *, cwd: str, writable: list[str], network: bool, binary: str = "bwrap") -> list:
    args = [binary, "--die-with-parent", "--new-session", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--unshare-pid", "--unshare-ipc"]
    if not network:
        args.append("--unshare-net")
    for w in writable:  # later mounts win: these re-open the writable subtrees of the read-only root
        args += ["--bind", w, w]
    args += ["--chdir", cwd, "--", "/bin/sh", "-c", command]
    return args


def _probe(argv: list) -> tuple[bool, str]:
    """Run a trivial command with the same sandbox flags once; cache whether this host supports them."""
    key = tuple(a for a in argv if a.startswith("--"))
    with _probe_lock:
        if key in _probe_cache:
            return _probe_cache[key]
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=20)
            res = (p.returncode == 0, (p.stderr or p.stdout).strip()[:300])
        except (OSError, subprocess.SubprocessError) as exc:
            res = (False, str(exc)[:300])
        _probe_cache[key] = res
        return res


def bwrap_status(network: bool = True) -> tuple[bool, str]:
    """(usable, reason) for bubblewrap on this host."""
    if not sys.platform.startswith("linux"):
        return False, "bubblewrap is Linux-only"
    binary = shutil.which("bwrap")
    if not binary:
        return False, "bubblewrap (bwrap) is not installed — e.g. `apt-get install bubblewrap`"
    ok, err = _probe(bwrap_args("true", cwd="/", writable=temp_dirs(), network=network, binary=binary))
    return (True, binary) if ok else (False, f"bwrap cannot create a sandbox here: {err or 'unknown error'}")


# --- macOS: Seatbelt ------------------------------------------------------------------------------------------
def _sb_str(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def seatbelt_profile(writable: list[str], network: bool) -> str:
    allow = " ".join(f"(subpath {_sb_str(w)})" for w in writable)
    lines = ["(version 1)", "(allow default)", "(deny file-write*)",
             f"(allow file-write* {allow} (literal \"/dev/null\") (literal \"/dev/zero\") (literal \"/dev/dtracehelper\")"
             " (regex #\"^/dev/tty\") (regex #\"^/dev/fd/\"))"]
    if not network:
        lines += ["(deny network-outbound)", "(deny network-inbound)", "(allow network-outbound (remote unix-socket))"]
    return "\n".join(lines) + "\n"


def seatbelt_args(command: str, *, writable: list[str], network: bool) -> list:
    return [SEATBELT, "-p", seatbelt_profile(writable, network), "/bin/sh", "-c", command]


def seatbelt_status() -> tuple[bool, str]:
    if sys.platform != "darwin":
        return False, "sandbox-exec (Seatbelt) is macOS-only"
    if not os.path.exists(SEATBELT):
        return False, f"{SEATBELT} not found"
    return True, SEATBELT


# --- entry point ----------------------------------------------------------------------------------------------
def plan(config: dict, command: str, *, cwd, writable, mode: Optional[str] = None) -> Plan:
    """Decide how to run ``command`` under the configured sandbox. Never raises; see ``Plan.note``."""
    s = settings(config)
    mode = mode or s["mode"]
    plain = Plan(argv=["/bin/sh", "-c", command], backend="none")
    if mode == "off":
        return plain
    allowed = _real([*writable, *s["writable"]]) + (temp_dirs() if s["temp"] else [])
    allowed = list(dict.fromkeys(allowed))
    cwd = os.path.realpath(str(cwd))
    reasons = []
    if mode in ("auto", "bwrap"):
        ok, why = bwrap_status(bool(s["network"]))
        if ok:
            return Plan(argv=bwrap_args(command, cwd=cwd, writable=allowed, network=bool(s["network"]), binary=why),
                        backend="bwrap", writable=allowed)
        reasons.append(why)
    if mode in ("auto", "seatbelt"):
        ok, why = seatbelt_status()
        if ok:
            return Plan(argv=seatbelt_args(command, writable=allowed, network=bool(s["network"])), backend="seatbelt",
                        writable=allowed)
        reasons.append(why)
    plain.note = f"sandbox `{mode}` unavailable ({'; '.join(reasons)}); the command ran WITHOUT a sandbox"
    return plain


__all__ = ["Plan", "plan", "settings", "bwrap_args", "bwrap_status", "seatbelt_profile", "seatbelt_status",
           "temp_dirs", "MODES"]
