"""Tools without the owner: ``ensure(tool)`` makes a missing program available by itself, in this order:

  (a) it is already there (PATH, ARTEL_HOME/tools/bin, the conda environment, version-manager shims);
  (b) a pinned static binary from artel/data/tools.yaml, downloaded into ARTEL_HOME/tools/bin and run only after its
      SHA-256 matches (a mismatch leaves nothing on disk);
  (c) micromamba + conda-forge into ARTEL_HOME/tools/conda/envs/artel;
  (d) a system package: apt through the root-owned wrapper /usr/local/sbin/artel-root (only when the installer set it
      up with the owner's consent; never raw sudo), brew on macOS, winget (user scope) on Windows.

Every install is a decision event (``provision/install``: tool, method, version, path, why) and is recorded in
ARTEL_HOME/tools/installed.json, so ``remove`` can undo what Artel put there. Failures are ``provision/failed``.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from . import tools

ROOT_WRAPPER = "/usr/local/sbin/artel-root"
SUDO = ["sudo", "-n"]          # tests replace it; the wrapper is the only thing sudoers lets through
DATA = Path(__file__).parent / "data" / "tools.yaml"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+._-]{0,63}$")
GIVE_UP_S = 3600               # prepare_checks does not retry a tool that could not be provisioned for this long

Log = Optional[Callable[..., object]]
_reasons: dict = {}
_gave_up: dict = {}
_state = {"apparmor_tried": False}


def reset() -> None:
    _reasons.clear()
    _gave_up.clear()
    _state["apparmor_tried"] = False


# --- data ---------------------------------------------------------------------------------------------------------
def _data() -> dict:
    import yaml

    return yaml.safe_load(DATA.read_text(encoding="utf-8")) or {}


def catalogue() -> dict:
    return _data().get("tools") or {}


def packages() -> dict:
    return _data().get("packages") or {}


def platform_key() -> str:
    os_ = {"linux": "linux", "darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)
    cpu = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "arm64" if os_ == "macos" else "aarch64"}
    m = platform.machine().lower()
    return f"{os_}-{cpu.get(m, m)}"


def _entry(tool: str) -> Optional[tuple]:
    for name, e in catalogue().items():
        if tool in (name, e.get("bin")):
            return name, e
    return None


def valid_name(tool: str) -> bool:
    return bool(NAME_RE.match(tool or ""))


# --- records ------------------------------------------------------------------------------------------------------
def _installed_file() -> Path:
    return tools.tools_dir() / "installed.json"


def installed() -> dict:
    try:
        return json.loads(_installed_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    tools.tools_dir().mkdir(parents=True, exist_ok=True)
    tmp = _installed_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(_installed_file())


def _emit(log: Log, kind: str, **payload) -> None:
    """The durable journal (always) and the project's event log (when the caller has one)."""
    try:
        tools.tools_dir().mkdir(parents=True, exist_ok=True)
        with (tools.tools_dir() / "provision.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "kind": kind, **payload}, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass
    if log:
        log(kind, **payload)


def last_reason(tool: str) -> str:
    return _reasons.get(tool, "")


# --- (a) find -----------------------------------------------------------------------------------------------------
def _shim_dirs() -> list:
    h = Path.home()
    return [str(p) for p in (h / ".local" / "bin", h / ".cargo" / "bin", h / ".pyenv" / "shims", h / ".asdf" / "shims",
                             h / ".local" / "share" / "mise" / "shims") if p.is_dir()]


def _find(tool: str) -> Optional[str]:
    found = tools.resolve(tool)
    if found:
        return found
    return shutil.which(tool, path=os.pathsep.join(_shim_dirs())) if _shim_dirs() else None


# --- (b) pinned binary --------------------------------------------------------------------------------------------
def _download_tool(tool: str) -> tuple:
    """(path, reason). Nothing reaches ARTEL_HOME/tools/bin unless the archive's SHA-256 matches the catalogue."""
    hit = _entry(tool)
    if not hit:
        return None, "not in the pinned catalogue"
    name, e = hit
    plat = (e.get("platforms") or {}).get(platform_key())
    if not plat:
        return None, f"no pinned build of {name} for {platform_key()}"
    url, want = plat["url"], plat["sha256"].lower()
    with tempfile.TemporaryDirectory(prefix="artel-dl-") as tmp:
        archive = Path(tmp) / url.rsplit("/", 1)[-1]
        try:
            tools._download(url, archive)
        except Exception as exc:  # noqa: BLE001 - network, proxy, 404: a reason, not a crash
            return None, f"download of {name} failed: {str(exc)[:200]}"
        if tools._sha256(archive) != want:
            return None, f"checksum of {name} {e['version']} does not match the pinned sha256; not installed"
        src = archive
        if archive.name.endswith((".tar.gz", ".tgz", ".zip")):
            out = Path(tmp) / "x"
            out.mkdir()
            try:
                tools._extract(archive, out)
            except Exception as exc:  # noqa: BLE001
                return None, f"cannot unpack {name}: {str(exc)[:200]}"
            want_name = plat.get("member") or e["bin"] + tools.EXE
            src = next((p for p in sorted(out.rglob("*")) if p.name == want_name and p.is_file() and not p.is_symlink()), None)
            if src is None:
                return None, f"{want_name} is not in the {name} archive"
        return str(tools._place(src, e["bin"])), ""


# --- (c) conda ----------------------------------------------------------------------------------------------------
def conda_env() -> Path:
    return tools.tools_dir() / "conda" / "envs" / "artel"


def _conda_bins() -> list:
    e = conda_env()
    return [str(e / "bin")] if not tools.WIN else [str(e / "Library" / "bin"), str(e / "Scripts"), str(e)]


def _pkg(tool: str, manager: str) -> str:
    return (packages().get(tool) or {}).get(manager) or tool


def _conda(tool: str, cfg: dict, log: Log = None) -> tuple:
    if (cfg.get("provision") or {}).get("conda") is False:
        return None, "conda is disabled (provision.conda: false)"
    mm = _find("micromamba")
    if not mm and tool != "micromamba":
        mm, why = _download_tool("micromamba")
        if not mm:
            return None, f"micromamba unavailable: {why}"
    if not mm:
        return None, "micromamba cannot be installed through itself"
    pkg = _pkg(tool, "conda")
    if not valid_name(pkg):
        return None, f"invalid package name {pkg!r}"
    env = conda_env()
    argv = [mm, "install" if env.exists() else "create", "-y", "-p", str(env), "-c", "conda-forge", "--override-channels", pkg]
    root = tools.tools_dir() / "conda"
    root.mkdir(parents=True, exist_ok=True)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=1800, env={**os.environ, "MAMBA_ROOT_PREFIX": str(root)})
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"micromamba failed: {str(exc)[:200]}"
    if p.returncode != 0:
        return None, f"conda-forge install of {pkg} failed: {(p.stderr or p.stdout).strip()[-200:]}"
    path = shutil.which(tool, path=os.pathsep.join(_conda_bins()))
    return (path, "") if path else (None, f"conda package {pkg} installed but provides no `{tool}` program")


# --- (d) system ---------------------------------------------------------------------------------------------------
def _alias(tool: str) -> Optional[str]:
    """A system package may install the program under another name (fd-find: fdfind): link it under the asked name."""
    alias = (packages().get(tool) or {}).get("alias")
    found = shutil.which(alias) if alias else None
    if found and not tools.WIN:
        tools.bin_dir().mkdir(parents=True, exist_ok=True)
        link = tools.bin_dir() / tool
        link.unlink(missing_ok=True)
        link.symlink_to(found)
        return str(link)
    return found


def _system(tool: str, cfg: dict, log: Log = None) -> tuple:
    """(path, reason, method)."""
    if (cfg.get("provision") or {}).get("system") is False:
        return None, "system packages are disabled (provision.system: false)", ""
    if sys.platform.startswith("linux"):
        pkg, method = _pkg(tool, "apt"), "apt"
        if not os.path.exists(ROOT_WRAPPER):
            return None, f"no root wrapper at {ROOT_WRAPPER} (the installer sets it up with the owner's consent)", ""
        argv = [*SUDO, ROOT_WRAPPER, "apt-install", pkg]
    elif sys.platform == "darwin":
        pkg, method = _pkg(tool, "brew"), "brew"
        brew = shutil.which("brew")
        if not brew:
            return None, "Homebrew is not installed", ""
        argv = [brew, "install", pkg]
    elif tools.WIN:
        pkg, method = (packages().get(tool) or {}).get("winget"), "winget"
        if not pkg or not shutil.which("winget"):
            return None, "no winget package known for this tool, or winget is missing", ""
        argv = ["winget", "install", "--scope", "user", "-e", "--id", pkg, "--silent",
                "--accept-package-agreements", "--accept-source-agreements"]
    else:
        return None, f"no system package route on {sys.platform}", ""
    if method != "winget" and not valid_name(pkg.lower()):
        return None, f"invalid package name {pkg!r}", ""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{method} failed: {str(exc)[:200]}", ""
    if p.returncode != 0:
        return None, f"{method} install of {pkg} failed: {(p.stderr or p.stdout).strip()[-200:]}", ""
    path = _find(tool) or _alias(tool)
    return (path, "", method) if path else (None, f"{method} package {pkg} installed but provides no `{tool}`", "")


# --- entry points -------------------------------------------------------------------------------------------------
def _record(tool: str, method: str, version: str, path: str, why: str, packages_: Optional[list] = None) -> dict:
    rec = {"method": method, "version": version, "path": path, "why": why, "ts": time.time(),
           "packages": packages_ or []}
    data = installed()
    data[tool] = rec
    _save(data)
    return rec


def ensure(tool: str, why: str = "", log: Log = None, cfg: Optional[dict] = None) -> Optional[str]:
    """Path of ``tool``, installing it if needed; None when every route failed (``last_reason`` says why)."""
    cfg = cfg or {}
    if not valid_name(tool):
        _reasons[tool] = f"invalid tool name {tool!r}"
        return None
    hit = _entry(tool)
    prog = hit[1]["bin"] if hit else tool
    found = _find(prog)
    if found:
        return found
    reasons = []
    path, why_not = _download_tool(tool)
    if path:
        e = _entry(tool)[1]
        _record(prog, "download", e["version"], path, why)
        _emit(log, "provision/install", tool=prog, method="download", version=e["version"], path=path, why=why)
        return path
    reasons.append(why_not)
    path, why_not = _conda(prog, cfg, log)
    if path:
        pkg = _pkg(prog, "conda")
        _record(prog, "conda", "conda-forge", path, why, [pkg])
        _emit(log, "provision/install", tool=prog, method="conda", version="conda-forge", path=path, why=why, packages=[pkg])
        return path
    reasons.append(why_not)
    path, why_not, method = _system(prog, cfg, log)
    if path:
        pkg = _pkg(prog, {"apt": "apt", "brew": "brew", "winget": "winget"}[method])
        _record(prog, method, "", path, why, [pkg])
        _emit(log, "provision/install", tool=prog, method=method, version="", path=path, why=why, packages=[pkg])
        return path
    reasons.append(why_not)
    _reasons[tool] = "; ".join(r for r in reasons if r)
    _emit(log, "provision/failed", tool=prog, reason=_reasons[tool], why=why)
    return None


def remove(tool: str, log: Log = None) -> dict:
    """Undo an install Artel made. System packages are left to the owner (the wrapper has no removals)."""
    data = installed()
    rec = data.get(tool)
    if not rec:
        return {"ok": False, "error": f"{tool} is not installed by Artel; remove it the way it was installed"}
    method, path = rec.get("method"), rec.get("path") or ""
    if method == "download":
        if Path(path).parent == tools.bin_dir():
            Path(path).unlink(missing_ok=True)
    elif method == "conda":
        mm = _find("micromamba")
        if mm:
            subprocess.run([mm, "remove", "-y", "-p", str(conda_env()), *rec.get("packages", [])], capture_output=True,
                           text=True, timeout=900, env={**os.environ, "MAMBA_ROOT_PREFIX": str(tools.tools_dir() / "conda")})
        if Path(path).exists() and conda_env() in Path(path).parents:
            Path(path).unlink(missing_ok=True)
    else:
        return {"ok": False, "error": f"{tool} was installed system-wide by {method}; Artel has no way to remove system "
                                      f"packages — run `{method} remove` yourself"}
    data.pop(tool, None)
    _save(data)
    _emit(log, "provision/remove", tool=tool, method=method, path=path)
    return {"ok": True}


def fix_bwrap(err: str, log: Log = None) -> bool:
    """bwrap failed with the uid-map error (AppArmor restricts user namespaces): once per process, through the wrapper,
    install the profile that allows them for /usr/bin/bwrap. True if the wrapper ran successfully."""
    if "uid map" not in (err or "") or _state["apparmor_tried"] or not os.path.exists(ROOT_WRAPPER):
        return False
    _state["apparmor_tried"] = True
    try:
        p = subprocess.run([*SUDO, ROOT_WRAPPER, "apparmor-bwrap"], capture_output=True, text=True, timeout=120)
        ok, detail = p.returncode == 0, (p.stderr or p.stdout).strip()[-200:]
    except (OSError, subprocess.SubprocessError) as exc:
        ok, detail = False, str(exc)[:200]
    _emit(log, "provision/apparmor", action="apparmor-bwrap", ok=ok, why="bwrap: user namespaces are restricted", detail=detail)
    return ok


# --- gates --------------------------------------------------------------------------------------------------------
_KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "for", "time", "exec", "!", "{", "}",
             "case", "esac", "in"}
_BUILTINS = {"cd", "echo", "test", "export", "set", "unset", "true", "false", "exit", "source", "eval", "read", "printf",
             "pwd", "type", "command", "alias", "umask", "ulimit", "wait", "trap", "shift", "return", "local", "env", "sh",
             "bash", ":"}


def first_programs(checks) -> set:
    """The program each shell segment of the checks starts with (words that are not obviously a program are skipped)."""
    out = set()
    for cmd in checks or []:
        cmd = re.sub(r"\d*[<>]&\d*|&>>?", " ", str(cmd))  # 2>&1 is a redirect, not a command separator
        for seg in re.split(r"&&|\|\||[;|\n&]", cmd):
            try:
                words = shlex.split(seg.strip().lstrip("({!"), posix=True)
            except ValueError:
                continue
            while words and (words[0] in _KEYWORDS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0])):
                words.pop(0)
            if words and NAME_RE.match(words[0]) and not words[0].isdigit() and words[0] not in _BUILTINS:
                out.add(words[0])
    return out


def prepare_checks(checks, cfg: Optional[dict] = None, log: Log = None) -> list:
    """Before the gates run: make the missing first programs available. Only names Artel knows how to install (the
    catalogue and the package map) are installed; anything else is returned as missing, never guessed at."""
    known = set(packages())
    for name, e in catalogue().items():
        known |= {name, e.get("bin")}
    missing = []
    for prog in sorted(first_programs(checks)):
        if _find(prog):
            continue
        key = (str(tools.tools_dir()), prog)
        if prog not in known or time.time() - _gave_up.get(key, 0) < GIVE_UP_S:
            missing.append(prog)
            continue
        if not ensure(prog, why="a task check starts with it", log=log, cfg=cfg):
            _gave_up[key] = time.time()
            missing.append(prog)
    return missing
