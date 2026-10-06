"""Optional agent CLIs (Codex, Claude Code) installed on demand, only when the person decides to use them.

Everything goes into ARTEL_HOME/tools — no sudo, nothing system-wide, removable with one call:
  tools/npm/            npm prefix for the CLIs (bin/ on Linux and macOS, the prefix itself on Windows)
  tools/node/           a portable Node.js, downloaded only when the machine has no npm
A CLI that is already on PATH is used as is and never reinstalled.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

TOOLS = {
    "codex": {"npm": "@openai/codex", "bin": "codex", "label": "Codex CLI"},
    "claude": {"npm": "@anthropic-ai/claude-code", "bin": "claude", "label": "Claude Code CLI"},
}
NODE_DIST = "https://nodejs.org/dist"
WIN = sys.platform == "win32"


def tools_dir() -> Path:
    from .project import artel_home

    return artel_home() / "tools"


def _npm_prefix() -> Path:
    return tools_dir() / "npm"


def _bin_dirs() -> list[Path]:
    prefix, node = _npm_prefix(), tools_dir() / "node"
    return [prefix, node] if WIN else [prefix / "bin", node / "bin"]


def env_with_tools(env: Optional[dict] = None) -> dict:
    """PATH with Artel's own tool directories first (an npm-installed CLI is a node script and needs node)."""
    env = dict(os.environ if env is None else env)
    extra = [str(d) for d in _bin_dirs() if d.exists()]
    if extra:
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return env


def resolve(binary: str) -> Optional[str]:
    """Full path of a CLI: Artel's tools first, then the system PATH."""
    if os.path.sep in binary or (WIN and "/" in binary):
        return binary if Path(binary).exists() else None
    return shutil.which(binary, path=env_with_tools().get("PATH"))


def installed_by_artel(name: str) -> bool:
    b = TOOLS[name]["bin"]
    return any((d / (b + (".cmd" if WIN else ""))).exists() for d in _bin_dirs()[:1])


def _system_npm() -> Optional[str]:
    return shutil.which("npm.cmd" if WIN else "npm")


def node_asset(index: list[dict], system: str = sys.platform, machine: str = platform.machine()) -> tuple[str, str]:
    """(version, file name) of the newest LTS Node.js build for this platform."""
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine.lower(), machine.lower())
    osname, ext = ("win", "zip") if system == "win32" else (("darwin", "tar.gz") if system == "darwin" else ("linux", "tar.gz"))
    # keys as in nodejs.org/dist/index.json: "linux-x64", "osx-arm64-tar", "win-x64-zip"
    files_key = {"linux": f"linux-{arch}", "darwin": f"osx-{arch}-tar", "win": f"win-{arch}-zip"}[osname]
    for rel in index:
        if rel.get("lts") and files_key in rel.get("files", []):
            v = rel["version"]
            return v, f"node-{v}-{osname}-{arch}.{ext}"
    raise RuntimeError(f"no Node.js LTS build for {osname}-{arch}")


def ensure_node(log: Callable[[str], None] = print, force_portable: bool = False) -> str:
    """Path to an npm executable; downloads a portable Node.js into ARTEL_HOME/tools/node when there is none."""
    npm = None if force_portable else _system_npm()
    if npm:
        return npm
    node_dir = tools_dir() / "node"
    local = node_dir / ("npm.cmd" if WIN else "bin/npm")
    if local.exists():
        return str(local)
    log("Node.js не найден — скачиваю переносную версию в папку Artel")
    with urllib.request.urlopen(f"{NODE_DIST}/index.json", timeout=60) as r:
        version, fname = node_asset(json.loads(r.read()))
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / fname
        urllib.request.urlretrieve(f"{NODE_DIST}/{version}/{fname}", archive)
        log(f"распаковка Node.js {version}")
        if fname.endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                z.extractall(tmp)
        else:
            with tarfile.open(archive) as t:
                try:
                    t.extractall(tmp, filter="tar")
                except TypeError:  # Python without extraction filters (< 3.10.12 / 3.11.4)
                    t.extractall(tmp)
        extracted = next(p for p in Path(tmp).iterdir() if p.is_dir() and p.name.startswith("node-"))
        if node_dir.exists():
            shutil.rmtree(node_dir)
        node_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(extracted), node_dir)
    return str(local)


def install(name: str, log: Callable[[str], None] = print, force_portable_node: bool = False) -> dict:
    spec = TOOLS[name]
    found = resolve(spec["bin"])
    if found and not installed_by_artel(name):
        log(f"{spec['label']} уже установлен: {found}")
        return {"ok": True, "path": found, "already": True}
    npm = ensure_node(log, force_portable=force_portable_node)
    log(f"установка {spec['npm']} в папку Artel")
    prefix = _npm_prefix()
    prefix.mkdir(parents=True, exist_ok=True)
    p = subprocess.run([npm, "install", "-g", "--prefix", str(prefix), "--no-fund", "--no-audit", spec["npm"]],
                       capture_output=True, text=True, env=env_with_tools(), timeout=1800)
    if p.returncode != 0:
        log((p.stderr or p.stdout).strip()[-800:])
        return {"ok": False, "error": (p.stderr or p.stdout).strip()[-800:]}
    path = resolve(spec["bin"])
    log(f"{spec['label']} установлен: {path}")
    return {"ok": bool(path), "path": path}


def uninstall(name: str) -> dict:
    """Remove a CLI that Artel installed (a system-wide install is left alone)."""
    spec = TOOLS[name]
    if not installed_by_artel(name):
        return {"ok": False, "error": f"{spec['label']} установлен не Artel — удалите его тем же способом, каким ставили"}
    npm = _system_npm() or str(tools_dir() / "node" / ("npm.cmd" if WIN else "bin/npm"))
    p = subprocess.run([npm, "uninstall", "-g", "--prefix", str(_npm_prefix()), spec["npm"]], capture_output=True,
                       text=True, env=env_with_tools(), timeout=600)
    return {"ok": p.returncode == 0 and not installed_by_artel(name), "error": (p.stderr or "").strip()[-400:]}


class Installer:
    """Background install with a log the dashboard polls."""

    def __init__(self):
        self.state: dict = {}
        self.lock = threading.Lock()

    def start(self, name: str) -> dict:
        if name not in TOOLS:
            return {"phase": "error", "error": f"unknown tool {name}"}
        with self.lock:
            if self.state.get(name, {}).get("phase") == "running":
                return self.state[name]
            self.state[name] = {"phase": "running", "log": []}

        def run():
            st = self.state[name]
            try:
                res = install(name, log=lambda m: st["log"].append(m))
                st.update(phase="done" if res["ok"] else "error", **{k: v for k, v in res.items() if k != "ok"})
            except Exception as e:  # noqa: BLE001 - shown in the dashboard
                st.update(phase="error", error=repr(e)[:500])

        threading.Thread(target=run, daemon=True, name=f"install-{name}").start()
        return self.state[name]
