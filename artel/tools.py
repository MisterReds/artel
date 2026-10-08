"""Optional agent CLIs (Codex, Claude Code, DeepSeek Harness) installed on demand, only when the person decides to use
them.

Both ship native single-file builds, so no Node.js or npm is involved:
  Codex       — the release binary from github.com/openai/codex (latest release, per platform)
  Claude Code — the native build from downloads.claude.ai, verified against the release manifest's SHA-256
They go into ARTEL_HOME/tools/bin — no sudo, nothing system-wide, removable with one call. A CLI that is already on
PATH is used as is and never reinstalled.

DeepSeek Harness is distributed as a Python SDK whose platform wheel carries the bundled single-file ``dsh`` runtime
(no Node.js): ``pip install --pre deepseek-harness-sdk`` into Artel's own Python environment (the SDK is imported by
the ``dsh`` runtime). A marker in ARTEL_HOME/tools records that Artel installed it, so only that install is removable.
"""

from __future__ import annotations

import functools
import hashlib
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

WIN = sys.platform == "win32"
EXE = ".exe" if WIN else ""
TOOLS = {"codex": {"bin": "codex", "label": "Codex CLI"}, "claude": {"bin": "claude", "label": "Claude Code CLI"},
         "dsh": {"bin": "dsh", "label": "DeepSeek Harness"},
         "embeddings": {"bin": "llama-server", "label": "EmbeddingGemma 2 (локальные эмбеддинги)"}}
LLAMA_REPO = "ggml-org/llama.cpp"
LLAMA_FALLBACK_TAG = "b11459"  # a release known to run EmbeddingGemma 2 (gemma-embedding2), used if the API is out
DSH_PIP = ["deepseek-harness-sdk"]  # pulls the matching deepseek-harness-runtime-bin wheel for this platform
DSH_DISTS = ["deepseek-harness-sdk", "deepseek-harness-runtime-bin"]
CODEX_RELEASE = "https://github.com/openai/codex/releases/latest/download"
CLAUDE_RELEASES = "https://downloads.claude.ai/claude-code-releases"


def is_local_url(url: str) -> bool:
    """127.0.0.1 / localhost / ::1: reached directly, never through a system HTTP proxy."""
    from urllib.parse import urlsplit

    host = urlsplit(url or "").hostname or ""
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0") or host.endswith(".localhost")


def tools_dir() -> Path:
    from .project import artel_home

    return artel_home() / "tools"


def bin_dir() -> Path:
    return tools_dir() / "bin"


def env_with_tools(env: Optional[dict] = None) -> dict:
    env = dict(os.environ if env is None else env)
    if bin_dir().exists():
        env["PATH"] = os.pathsep.join([str(bin_dir()), env.get("PATH", "")])
    return env


def resolve(binary: str) -> Optional[str]:
    """Full path of a CLI: Artel's tools first, then the system PATH."""
    if os.path.sep in binary or (WIN and "/" in binary):
        return binary if Path(binary).exists() else None
    return shutil.which(binary, path=env_with_tools().get("PATH"))


@functools.lru_cache(maxsize=None)
def posix_shell() -> Optional[str]:
    """bash for agent commands on Windows: ARTEL_BASH, else Git for Windows' bash (its bin/bash.exe sets up PATH
    for coreutils). WSL's System32\\bash.exe is never used: it runs in another file system."""
    if os.environ.get("ARTEL_BASH"):
        return os.environ["ARTEL_BASH"]
    git = shutil.which("git")
    if git:
        for root in list(Path(git).resolve().parents)[:3]:
            if (root / "bin" / "bash.exe").is_file():
                return str(root / "bin" / "bash.exe")
    found = shutil.which("bash")
    if found and not any(part.lower() in ("system32", "windowsapps") for part in Path(found).parts):
        return found
    return None


def shell_run(command: str, **kw) -> subprocess.CompletedProcess:
    """Run a command line written in POSIX shell syntax (what agents and task checks write) and capture text
    output as UTF-8. On Windows it runs in Git Bash when available (cmd.exe otherwise), and child Pythons get
    UTF-8 mode so their output decodes."""
    kw.setdefault("encoding", "utf-8")
    kw.setdefault("errors", "replace")
    if WIN:
        env = dict(kw.get("env") or os.environ)
        env.setdefault("PYTHONUTF8", "1")
        kw["env"] = env
        bash = posix_shell()
        if bash:
            return subprocess.run([bash, "-c", command], **kw)
    return subprocess.run(command, shell=True, **kw)


def terminate_tree(proc: subprocess.Popen, force: bool = False) -> bool:
    """Stop a child CLI and whatever it started; True (so callers can note that the exit was theirs).

    POSIX: SIGTERM (SIGKILL with ``force``). Windows: a CLI is often a .cmd shim (npm installs, test fakes) whose
    real process would outlive cmd.exe and keep the output pipes open, so ``taskkill /T`` ends the whole tree."""
    if WIN:
        if proc.poll() is not None:
            return True
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        return True
    try:
        proc.kill() if force else proc.terminate()
    except OSError:
        pass
    return True


def installed_by_artel(name: str) -> bool:
    if name == "embeddings":
        return (tools_dir() / "llama").exists() or (tools_dir() / "models").exists()
    if name == "dsh":
        return (tools_dir() / "dsh.installed").exists()
    return (bin_dir() / (TOOLS[name]["bin"] + EXE)).exists()


def dsh_installed(module: str = "deepseek_harness") -> bool:
    import importlib
    import importlib.util

    importlib.invalidate_caches()
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _pip(args: list, log: Callable[[str], None]) -> None:
    import subprocess

    p = subprocess.run([sys.executable, "-m", "pip", *args], capture_output=True, text=True, timeout=1800)
    out = (p.stdout + p.stderr).strip()
    for line in out.splitlines()[-5:]:
        log(line)
    if p.returncode != 0:
        raise RuntimeError(f"pip {' '.join(args)} failed: {out[-800:]}")


def install_dsh(log: Callable[[str], None]) -> Path:
    log(f"pip install --pre {' '.join(DSH_PIP)} в окружение Artel ({sys.executable})")
    _pip(["install", "--pre", "--upgrade", *DSH_PIP], log)
    tools_dir().mkdir(parents=True, exist_ok=True)
    (tools_dir() / "dsh.installed").write_text(" ".join(DSH_DISTS))
    return Path(sys.executable)


def _arch(machine: str) -> str:
    return {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine.lower(), machine.lower())


def codex_asset(system: str = sys.platform, machine: str = platform.machine()) -> str:
    cpu = {"x64": "x86_64", "arm64": "aarch64"}[_arch(machine)]
    if system == "win32":
        return f"codex-{cpu}-pc-windows-msvc.exe.zip"
    if system == "darwin":
        return f"codex-{cpu}-apple-darwin.tar.gz"
    return f"codex-{cpu}-unknown-linux-musl.tar.gz"


def claude_platform(system: str = sys.platform, machine: str = platform.machine(), musl: Optional[bool] = None) -> str:
    arch = _arch(machine)
    if system == "win32":
        return f"win32-{arch}"
    if system == "darwin":
        return f"darwin-{arch}"
    if musl is None:
        musl = not platform.libc_ver()[0]
    return f"linux-{arch}" + ("-musl" if musl else "")


def _download(url: str, dest: Path, attempts: int = 4) -> None:
    """Download with retries on transient failures (5xx, dropped connections): 2s, 4s, 8s."""
    import time as _t
    import urllib.error

    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "artel"})
            with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            return
        except urllib.error.HTTPError as e:
            if e.code < 500 or i == attempts - 1:
                raise
        except (urllib.error.URLError, OSError):
            if i == attempts - 1:
                raise
        _t.sleep(2 ** (i + 1))


def _get_json(url: str, timeout: int = 60):
    import tempfile as _tf

    with _tf.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x.json"
        _download(url, p)
        return json.loads(p.read_text(encoding="utf-8"))


def _place(src: Path, name: str) -> Path:
    bin_dir().mkdir(parents=True, exist_ok=True)
    dest = bin_dir() / (name + EXE)
    shutil.move(str(src), dest)
    dest.chmod(0o755)
    return dest


def install_codex(log: Callable[[str], None]) -> Path:
    asset = codex_asset()
    log(f"скачиваю {asset}")
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / asset
        _download(f"{CODEX_RELEASE}/{asset}", archive)
        out = Path(tmp) / "x"
        out.mkdir()
        if asset.endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                z.extractall(out)
        else:
            with tarfile.open(archive) as t:
                try:
                    t.extractall(out, filter="data")
                except TypeError:  # Python without extraction filters
                    t.extractall(out)
        binary = next(p for p in out.rglob("codex*") if p.is_file())
        return _place(binary, "codex")


def install_claude(log: Callable[[str], None]) -> Path:
    plat = claude_platform()
    with urllib.request.urlopen(f"{CLAUDE_RELEASES}/latest", timeout=60) as r:
        version = r.read().decode().strip()
    with urllib.request.urlopen(f"{CLAUDE_RELEASES}/{version}/manifest.json", timeout=60) as r:
        manifest = json.loads(r.read())
    expected = ((manifest.get("platforms") or {}).get(plat) or {}).get("checksum")
    if not expected:
        raise RuntimeError(f"Claude Code {version}: no build for {plat}")
    log(f"скачиваю Claude Code {version} ({plat})")
    with tempfile.TemporaryDirectory() as tmp:
        binary = Path(tmp) / ("claude" + EXE)
        _download(f"{CLAUDE_RELEASES}/{version}/{plat}/claude{EXE}", binary)
        if hashlib.sha256(binary.read_bytes()).hexdigest() != expected:
            raise RuntimeError("контрольная сумма Claude Code не совпала с манифестом — файл не установлен")
        log("контрольная сумма совпала")
        return _place(binary, "claude")


def llama_asset(tag: str, system: str = sys.platform, machine: str = platform.machine()) -> str:
    arch = _arch(machine)
    if system == "win32":
        return f"llama-{tag}-bin-win-cpu-{arch}.zip"
    if system == "darwin":
        return f"llama-{tag}-bin-macos-{arch}.tar.gz"
    return f"llama-{tag}-bin-ubuntu-{arch}.tar.gz"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _extract(archive: Path, out: Path) -> None:
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(out)
    else:
        with tarfile.open(archive) as t:
            try:
                t.extractall(out, filter="data")
            except TypeError:
                t.extractall(out)


def install_embeddings(log: Callable[[str], None], quant: str = "Q8_0") -> Path:
    """llama.cpp's llama-server (official release build, CPU) + EmbeddingGemma 2 GGUF, both into ARTEL_HOME/tools.
    The model file is checked against the SHA-256 Hugging Face publishes for it; the server archive against the
    digest GitHub publishes when the API is reachable."""
    from .embeddings import MODEL_NAME, MODEL_REPO, model_path

    tag, digest = LLAMA_FALLBACK_TAG, None
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{LLAMA_REPO}/releases/latest",
                                     headers={"User-Agent": "artel", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            rel = json.loads(r.read())
        want = llama_asset(rel["tag_name"])
        asset = next((a for a in rel.get("assets", []) if a["name"] == want), None)
        if asset:  # the newest tag may still be building its binaries; then the known-good one is used
            tag, digest = rel["tag_name"], (asset.get("digest") or "").removeprefix("sha256:") or None
    except (OSError, ValueError, KeyError) as e:
        log(f"GitHub API недоступен ({e}); беру проверенную сборку {tag}")
    lib = tools_dir() / "llama"
    if not server_present():
        asset = llama_asset(tag)
        log(f"скачиваю llama.cpp {tag} ({asset})")
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / asset
            _download(f"https://github.com/{LLAMA_REPO}/releases/download/{tag}/{asset}", archive)
            if digest and _sha256(archive) != digest:
                raise RuntimeError("контрольная сумма llama.cpp не совпала с опубликованной — файл не установлен")
            if lib.exists():
                shutil.rmtree(lib)
            lib.mkdir(parents=True)
            _extract(archive, lib)
        for p in lib.rglob("llama-*"):
            if p.is_file() and not WIN:
                p.chmod(0o755)
    dest = model_path(quant)
    if not dest.exists():
        fname = f"{MODEL_NAME}-{quant}.gguf"
        tree = _get_json(f"https://huggingface.co/api/models/{MODEL_REPO}/tree/main")
        meta = next((f for f in tree if f.get("path") == fname), None)
        if not meta:
            raise RuntimeError(f"{fname} нет в {MODEL_REPO}")
        expected = (meta.get("lfs") or {}).get("oid")
        log(f"скачиваю {fname} ({round((meta.get('size') or 0) / 1e6)} МБ) с Hugging Face")
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(".part")
        _download(f"https://huggingface.co/{MODEL_REPO}/resolve/main/{fname}", part)
        if not expected or _sha256(part) != expected:
            part.unlink(missing_ok=True)
            raise RuntimeError("контрольная сумма модели не совпала с Hugging Face — файл не установлен")
        log("контрольная сумма модели совпала")
        part.replace(dest)
    return dest


def server_present() -> bool:
    from .embeddings import server_binary

    return bool(server_binary())


def install(name: str, log: Callable[[str], None] = print) -> dict:
    spec = TOOLS[name]
    if name == "embeddings":
        path = install_embeddings(log)
        log(f"{spec['label']} установлен: {path}")
        return {"ok": True, "path": str(path)}
    found = (sys.executable if dsh_installed() else None) if name == "dsh" else resolve(spec["bin"])
    if found and not installed_by_artel(name):
        log(f"{spec['label']} уже установлен: {found}")
        return {"ok": True, "path": found, "already": True}
    path = {"codex": install_codex, "claude": install_claude, "dsh": install_dsh}[name](log)
    log(f"{spec['label']} установлен: {path}")
    return {"ok": True, "path": str(path)}


def uninstall(name: str) -> dict:
    """Remove a CLI that Artel installed (a system-wide install is left alone)."""
    spec = TOOLS[name]
    if not installed_by_artel(name):
        return {"ok": False, "error": f"{spec['label']} установлен не Artel — удалите его тем же способом, каким ставили"}
    if name == "embeddings":
        from .embeddings import LocalServer

        LocalServer.stop()
        for d in (tools_dir() / "llama", tools_dir() / "models"):
            shutil.rmtree(d, ignore_errors=True)
        return {"ok": True}
    if name == "dsh":
        try:
            _pip(["uninstall", "-y", *DSH_DISTS], lambda m: None)
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
        (tools_dir() / "dsh.installed").unlink()
        return {"ok": True}
    (bin_dir() / (spec["bin"] + EXE)).unlink()
    return {"ok": True}


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
                st.update(phase="done", path=res.get("path"))
                if name == "embeddings":  # an error cached before the install must not outlive it
                    from .embeddings import reset

                    reset()
            except Exception as e:  # noqa: BLE001 - shown in the dashboard
                st.update(phase="error", error=str(e)[:500] or repr(e)[:500])

        threading.Thread(target=run, daemon=True, name=f"install-{name}").start()
        return self.state[name]
