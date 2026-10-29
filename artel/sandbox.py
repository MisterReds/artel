"""OS sandbox for shell commands run by agents (W2-sandbox).

Config (on by default wherever a backend exists; ``mode: off`` is the explicit way to run without isolation)::

    sandbox:
      mode: auto        # off | auto | bwrap | seatbelt
      network: true     # false: no network inside the sandbox
      writable: []      # extra writable paths (besides the task worktree, the workspace and temp)
      temp: true        # the temp directories are writable
      require: true     # fail closed: a backend that exists but cannot start a command never falls back to running it
                        # unsandboxed (an explicit mode: bwrap/seatbelt without its backend is refused too)
      gates: true       # the task checks (review gates) run sandboxed as well (writable: gate cwd + temp)

The sandboxed command sees the whole system read-only and may write only to the given paths (the task's
deliverables worktree, the agent's workspace, the temp directory and ``writable``).

* Linux: bubblewrap (``bwrap``) — ``--ro-bind / /`` then ``--bind`` of every writable path, fresh ``/dev`` and
  ``/proc``, a new PID namespace and session, ``--unshare-net`` when the network is off.
* macOS: ``sandbox-exec`` with a generated Seatbelt profile (allow by default, deny file writes except the
  writable subpaths, optionally deny the network).
* Elsewhere, or when the tool is missing or cannot create namespaces (e.g. unprivileged containers) and the mode is
  ``auto``: there is no backend; the command runs unsandboxed, the result carries a clear note and ``status()``
  says so once for the dashboard and ``artel providers``.

Inside the sandbox secrets are unreadable (~/.ssh, ~/.aws, ~/.gnupg, ~/.config/gh, ~/.netrc, ~/.docker/config.json,
~/.kube and ARTEL_HOME except the current project and the shims/tools directories) and the owner's configuration
(policy.yaml, artel.yaml, the skills library) is read-only even under a writable ancestor (``protected_paths``).

Ideas from OpenAI Codex codex-rs/linux-sandbox (bwrap.rs: read-only root bind, writable re-binds, fresh
/dev and /proc, --unshare-pid/--unshare-net, --new-session, --die-with-parent) and codex-rs/core seatbelt
policies — Apache-2.0, Copyright OpenAI. Reimplemented here for Artel.
"""

from __future__ import annotations

import os
import re
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


DEFAULTS = {"mode": "auto", "network": True, "writable": [], "temp": True, "require": True, "gates": True}
HOME_SECRETS = (".ssh", ".aws", ".gnupg", ".config/gh", ".netrc", ".docker/config.json", ".kube")
# what a failed sandbox start looks like on stderr (bwrap setup errors; Seatbelt refusing to apply the profile)
_START_FAILURE = re.compile(r"^(?:bwrap: (?!execvp)|sandbox-exec: sandbox_apply)", re.M)


def settings(config: dict) -> dict:
    s = {**DEFAULTS, "writable": []}
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
    error: str = ""                  # the sandbox was demanded (mode / require) but cannot be had: do NOT run

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


def _real_any(paths) -> list[str]:
    """Like _real, but files count too (a denied ~/.netrc is a file)."""
    out: list[str] = []
    for p in paths:
        rp = os.path.realpath(os.path.expanduser(str(p)))
        if os.path.exists(rp) and rp not in out:
            out.append(rp)
    return out


def temp_dirs() -> list[str]:
    dirs = [tempfile.gettempdir(), "/tmp", "/var/tmp"]
    if os.environ.get("TMPDIR"):
        dirs.append(os.environ["TMPDIR"])
    return _real(dirs)


# --- Linux: bubblewrap ----------------------------------------------------------------------------------------
def _within(path: str, bases) -> bool:
    return any(path == b or path.startswith(b.rstrip("/") + "/") for b in bases)


def read_policy(project_root=None) -> tuple[list[str], list[str]]:
    """(deny, expose): existing paths whose reads are denied inside the sandbox, and the parts of them that stay
    visible (the current project, ARTEL_HOME/shims, ARTEL_HOME/tools)."""
    from .project import artel_home

    home = os.path.expanduser("~")
    ah = os.path.realpath(str(artel_home()))
    deny = _real_any([os.path.join(home, s) for s in HOME_SECRETS] + [ah])
    expose = [p for p in _real([project_root, os.path.join(ah, "shims"), os.path.join(ah, "tools")])
              if _within(p, [ah])]
    return deny, expose


def protected_paths(project_root=None) -> list[str]:
    """Owner configuration agents may never write: policy.yaml, artel.yaml and the skills library, in ARTEL_HOME
    and in the project. One list for the file tools, the command policy and both sandboxes."""
    from .project import artel_home

    bases = [artel_home()] + ([Path(project_root)] if project_root else [])
    out = []
    for base in bases:
        for name in ("policy.yaml", "artel.yaml") + (("skills",) if base == bases[0] else ()):
            out.append(os.path.realpath(str(base / name)))
    return list(dict.fromkeys(out))


def bwrap_args(command: str, *, cwd: str, writable: list[str], network: bool, binary: str = "bwrap",
               deny=(), expose=(), protected=()) -> list:
    args = [binary, "--die-with-parent", "--new-session", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--unshare-pid", "--unshare-ipc"]
    # network on: the host network is shared on purpose, so a proxy on 127.0.0.1 (HTTP(S)_PROXY) stays reachable
    args.append("--share-net" if network else "--unshare-net")
    for w in writable:  # later mounts win: these re-open the writable subtrees of the read-only root
        args += ["--bind", w, w]
    for d in deny:  # secrets last-but-one, so a writable ancestor (the temp dir ARTEL_HOME may live in) cannot undo it
        args += ["--tmpfs", d] if os.path.isdir(d) else ["--ro-bind", "/dev/null", d]
    for e in expose:
        args += ["--ro-bind", e, e]
    for w in writable:  # the writable parts of the project were hidden with its parent: open them again
        if _within(w, expose):
            args += ["--bind", w, w]
    for p in protected:  # read-only even under a writable ancestor; only paths that exist and are visible
        if os.path.exists(p) and _within(p, writable) and (not _within(p, deny) or _within(p, expose)):
            args += ["--ro-bind", p, p]
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


def seatbelt_profile(writable: list[str], network: bool, deny=(), expose=(), protected=()) -> str:
    allow = " ".join(f"(subpath {_sb_str(w)})" for w in writable)
    lines = ["(version 1)", "(allow default)", "(deny file-write*)",
             f"(allow file-write* {allow} (literal \"/dev/null\") (literal \"/dev/zero\") (literal \"/dev/dtracehelper\")"
             " (regex #\"^/dev/tty\") (regex #\"^/dev/fd/\"))"]
    lines += [f"(deny file-read* (subpath {_sb_str(d)}))" for d in deny]  # later rules win in Seatbelt
    lines += [f"(allow file-read* (subpath {_sb_str(e)}))" for e in expose]
    lines += [f"(deny file-write* (subpath {_sb_str(p)}))" for p in protected]
    if not network:
        lines += ["(deny network-outbound)", "(deny network-inbound)", "(allow network-outbound (remote unix-socket))"]
    return "\n".join(lines) + "\n"


def seatbelt_args(command: str, *, writable: list[str], network: bool, deny=(), expose=(), protected=()) -> list:
    return [SEATBELT, "-p", seatbelt_profile(writable, network, deny, expose, protected), "/bin/sh", "-c", command]


def seatbelt_status() -> tuple[bool, str]:
    if sys.platform != "darwin":
        return False, "sandbox-exec (Seatbelt) is macOS-only"
    if not os.path.exists(SEATBELT):
        return False, f"{SEATBELT} not found"
    return True, SEATBELT


# --- entry point ----------------------------------------------------------------------------------------------
_failures: dict[str, str] = {}   # why a sandbox start failed last (shown by status(); one entry, not a log)


def plan(config: dict, command: str, *, cwd, writable, mode: Optional[str] = None, project_root=None) -> Plan:
    """Decide how to run ``command`` under the configured sandbox. Never raises; see ``Plan.note`` / ``Plan.error``."""
    s = settings(config)
    mode = mode or s["mode"]
    from .tools import shell_argv

    plain = Plan(argv=shell_argv(command), backend="none")  # Windows: Git Bash (cmd.exe without it)
    if mode == "off":
        return plain
    allowed = _real([*writable, *s["writable"]]) + (temp_dirs() if s["temp"] else [])
    allowed = list(dict.fromkeys(allowed))
    cwd = os.path.realpath(str(cwd))
    deny, expose = read_policy(project_root)
    protected = protected_paths(project_root)
    reasons = []
    if mode in ("auto", "bwrap"):
        ok, why = bwrap_status(bool(s["network"]))
        if ok:
            return Plan(argv=bwrap_args(command, cwd=cwd, writable=allowed, network=bool(s["network"]), binary=why,
                                        deny=deny, expose=expose, protected=protected),
                        backend="bwrap", writable=allowed)
        reasons.append(why)
    if mode in ("auto", "seatbelt"):
        ok, why = seatbelt_status()
        if ok:
            return Plan(argv=seatbelt_args(command, writable=allowed, network=bool(s["network"]), deny=deny,
                                           expose=expose, protected=protected), backend="seatbelt", writable=allowed)
        reasons.append(why)
    plain.note = f"sandbox `{mode}` unavailable ({'; '.join(reasons)}); the command ran WITHOUT a sandbox"
    if mode != "auto" and s["require"]:  # the owner asked for this backend by name: no silent downgrade
        plain.error = f"sandbox cannot start commands: {'; '.join(reasons)}"
    return plain


def start_failure(run: Plan, returncode: int, stderr: str) -> str:
    """A sandbox that exists but did not start for this command ('' when it did): the environment is broken, the
    command never ran, and it must not be retried unsandboxed. Remembered for ``status()``."""
    if not run.sandboxed or returncode == 0:
        return ""
    m = _START_FAILURE.search(stderr or "")
    if not m:
        return ""
    reason = (stderr or "")[m.start():].strip().splitlines()[0][:300]
    _failures[run.backend] = reason
    return f"sandbox cannot start commands ({run.backend}): {reason}; the command was NOT run unsandboxed"


def refusal(run: Plan, config: dict) -> str:
    """Why ``run`` must not execute at all ('' when it may): a demanded backend is missing."""
    return run.error if settings(config)["require"] else ""


def status(config: dict) -> dict:
    """What the dashboard and ``artel providers`` show: ``{mode, active, backend, message, failure}``."""
    s = settings(config)
    failure = next(iter(_failures.values()), "")
    if s["mode"] == "off":
        return {"mode": "off", "active": False, "backend": "none", "failure": failure,
                "message": "Сейчас команды агентов выполняются без изоляции: она выключена в настройках (sandbox.mode: off). "
                           "Включить: sandbox.mode: auto в artel.yaml."}
    run = plan(config, "true", cwd=os.getcwd(), writable=[], mode=s["mode"])
    if run.sandboxed:
        msg = (f"Команды агентов изолированы ({run.backend}): запись только в рабочие папки, чтение ключей "
               "(~/.ssh, ~/.aws, ~/.gnupg, ~/.config/gh) и чужих проектов закрыто.")
        if failure:
            msg += f" Последний сбой запуска изоляции: {failure}. Команды в этом случае не выполняются."
        return {"mode": s["mode"], "active": True, "backend": run.backend, "message": msg, "failure": failure}
    how = ("Linux: установите bubblewrap (apt-get install bubblewrap) и разрешите непривилегированные "
           "user namespaces; macOS: нужен /usr/bin/sandbox-exec; Windows: используйте WSL2")
    why = run.note.split("(", 1)[-1].rsplit(")", 1)[0] if "(" in run.note else ""
    return {"mode": s["mode"], "active": False, "backend": "none", "failure": failure,
            "message": f"Сейчас команды агентов выполняются без изоляции: на этой машине её нечем обеспечить ({why}). "
                       f"Как включить: {how}."}


__all__ = ["Plan", "plan", "settings", "bwrap_args", "bwrap_status", "seatbelt_profile", "seatbelt_status",
           "temp_dirs", "MODES", "DEFAULTS", "read_policy", "protected_paths", "start_failure", "refusal", "status"]
