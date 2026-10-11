"""Plugin packages: install Artel plugins from a git URL, a pip spec (PyPI) or a local path.

Layout (ARTEL_HOME, default ~/.artel)::

    plugins.json                registry: source, version, enabled flag, entry points, static manifests, last error
    plugins/<name>/             one ``pip install --target`` directory per package (its own deps included)

Safety rules
    * Installing never imports package code. pip (and the package's build backend) is the only thing that runs.
      The manifest shown before enabling is read statically: ``PLUGIN = {...}`` is parsed with ``ast`` and
      ``literal_eval``, never executed.
    * A package is installed disabled. Its plugins' ``setup`` runs only once it is enabled.
    * The package directory joins ``sys.path`` only while the package is loaded, appended (it never shadows Artel
      or the standard library), and leaves it on detach together with the modules imported from it.
    * A plugin that fails to import or to set up is rolled back, the package is switched off and the error is
      stored in the registry; Artel keeps running.

Entry points
    A pip-installable package declares its plugin modules in ``pyproject.toml``::

        [project.entry-points."artel.plugins"]
        hello = "artel_plugin_hello"            # module with setup(kernel) and PLUGIN = {...}

    A plain directory (no build step) can carry ``artel-plugin.toml`` instead::

        name = "my-plugins"
        version = "0.1.0"
        [plugins]
        approval = "my_plugins.approval"

Manifest (``PLUGIN`` in the plugin module): ``name``, ``version``, ``provides``, ``requires`` — as for built-ins —
plus ``permissions`` (what the plugin asks for, shown before enabling; plugins run in-process, so this is a
declaration for the person deciding, not a sandbox) and ``description``.
"""

from __future__ import annotations

import ast
import configparser
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

ENTRY_POINT_GROUP = "artel.plugins"
TOML_MANIFEST = "artel-plugin.toml"
PERMISSIONS = {
    "system_prompt": "дописывает системный промпт агентов",
    "tools": "регистрирует инструменты для агентов",
    "events": "читает события проекта и пишет в журнал",
    "intercept": "перехватывает вызовы моделей и инструментов (waterfall)",
    "shell": "запускает команды",
    "network": "ходит в сеть",
    "files": "читает и пишет файлы вне рабочей папки проекта",
}
_IGNORE = shutil.ignore_patterns(".git", "build", "dist", "*.egg-info", "__pycache__", ".venv", "*.pyc")
_lock = threading.RLock()


class PackageError(Exception):
    pass


# --- registry --------------------------------------------------------------------------------------

def home() -> Path:
    from .project import artel_home

    return artel_home()


def plugins_dir() -> Path:
    return home() / "plugins"


def registry_path() -> Path:
    return home() / "plugins.json"


def load_registry() -> dict:
    p = registry_path()
    try:
        reg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        reg = {}
    reg.setdefault("packages", {})
    reg.setdefault("builtin_disabled", [])
    return reg


def save_registry(reg: dict) -> None:
    p = registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def get(name: str) -> dict:
    entry = load_registry()["packages"].get(norm(name))
    if entry is None:
        raise PackageError(f"плагин {name} не установлен")
    return entry


def disabled_builtins() -> set:
    try:
        return set(load_registry().get("builtin_disabled", []))
    except Exception:  # noqa: BLE001 - a broken registry must not stop Artel
        return set()


# --- static inspection (never imports package code) ----------------------------------------------

def module_file(root: Path, module: str) -> Optional[Path]:
    base = Path(root, *module.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def static_manifest(path: Optional[Path], fallback_name: str) -> dict:
    """``PLUGIN = {...}`` and the docstring of a plugin module, read with ``ast`` (no code runs)."""
    out: dict = {"name": fallback_name}
    if path is None:
        out["note"] = "модуль не найден в пакете"
        return out
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, ValueError) as exc:
        out["note"] = f"модуль не читается: {exc}"
        return out
    doc = ast.get_docstring(tree)
    for node in tree.body:
        target = node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else \
            node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(target, ast.Name) and target.id == "PLUGIN" and node.value is not None:
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                out["note"] = "PLUGIN не литерал: манифест виден только после включения"
                break
            if isinstance(value, dict):
                out.update(value)
            break
    else:
        out["note"] = "в модуле нет PLUGIN"
    if doc and "description" not in out:
        out["description"] = doc.strip().splitlines()[0]
    for key in ("provides", "requires", "permissions"):
        out[key] = [str(x) for x in (out.get(key) or [])]
    return out


def _read_toml(path: Path) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError as exc:
            raise PackageError(f"{path.name}: нужен Python 3.11+ или пакет tomli") from exc
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PackageError(f"{path}: {exc}") from exc


def _dists(site: Path) -> list[dict]:
    """Distributions pip put into ``site``: name, version and their ``artel.plugins`` entry points."""
    out = []
    for info in sorted(site.glob("*.dist-info")):
        meta: dict = {}
        mp = info / "METADATA"
        if mp.exists():
            for line in mp.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    break
                k, _, v = line.partition(":")
                meta.setdefault(k.strip().lower(), v.strip())
        eps: dict = {}
        ep = info / "entry_points.txt"
        if ep.exists():
            cp = configparser.ConfigParser(delimiters=("=",), interpolation=None)
            cp.optionxform = str  # type: ignore[assignment]
            try:
                cp.read_string(ep.read_text(encoding="utf-8"))
            except configparser.Error:
                pass
            if cp.has_section(ENTRY_POINT_GROUP):
                eps = {k.strip(): v.strip().split()[0] for k, v in cp.items(ENTRY_POINT_GROUP)}
        out.append({"name": meta.get("name") or info.name.split("-")[0], "version": meta.get("version", ""),
                    "summary": meta.get("summary", ""), "entry_points": eps})
    return out


def describe_site(site: Path, entry_points: dict) -> dict:
    manifests, perms = {}, []
    for ep, target in entry_points.items():
        mod = target.partition(":")[0]
        m = static_manifest(module_file(site, mod), ep)
        manifests[ep] = m
        perms += [p for p in m["permissions"] if p not in perms]
    return {"manifests": manifests, "permissions": perms}


# --- sources -----------------------------------------------------------------------------------------

GIT_HOSTS = ("github.com/", "gitlab.com/", "bitbucket.org/", "codeberg.org/", "git.sr.ht/")


def classify(source: str) -> tuple[str, str, Optional[str]]:
    """(kind, location, ref): kind is ``path``, ``git`` or ``pip``."""
    s = source.strip()
    if not s:
        raise PackageError("пустой источник")
    if s.startswith("git+") or s.startswith("git@") or s.endswith(".git") or s.startswith("git://") \
            or (s.startswith(("https://", "http://")) and any(h in s for h in GIT_HOSTS)):
        url = s[4:] if s.startswith("git+") else s
        ref = None
        if "://" in url:
            left, sep, right = url.rpartition("@")
            if sep and left.count("/") >= 3 and ":" not in right and right:
                url, ref = left, right
        return "git", url, ref
    p = Path(os.path.expanduser(s))
    if s.startswith((".", "/", "~")) or p.exists():
        if not p.exists():
            raise PackageError(f"нет такого пути: {s}")
        return "path", str(p.resolve()), None
    return "pip", s, None


def _run(cmd: list, cwd: Optional[Path] = None, timeout: int = 900) -> subprocess.CompletedProcess:
    env = {**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)


Runner = Callable[..., subprocess.CompletedProcess]


def _pip(spec: str, target: Path, runner: Runner, local: bool = False) -> None:
    base = [sys.executable, "-m", "pip", "install", "--target", str(target), "--no-input",
            "--no-warn-script-location", "--disable-pip-version-check", "-q"]
    attempts = []
    if local:
        import importlib.util as iu

        if iu.find_spec("setuptools") and iu.find_spec("wheel"):  # offline-friendly first try
            attempts.append(base + ["--no-build-isolation", spec])
    # a PyPI package comes as a wheel only: an sdist would run its setup.py on install, before the person has read
    # the manifest and agreed to enable it. Source trees (git, a folder) build here, by the person's explicit choice.
    attempts.append(base + ([spec] if local else ["--only-binary=:all:", spec]))
    err = ""
    for cmd in attempts:
        p = runner(cmd)
        if p.returncode == 0:
            return
        err = (p.stderr or p.stdout or "").strip()
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
    raise PackageError(f"pip install {spec} не удался:\n{err[-2000:]}")


def _git_clone(url: str, ref: Optional[str], dest: Path, runner: Runner) -> str:
    cmd = ["git", "clone", "--depth", "1", "-q"] + (["--branch", ref] if ref else []) + [url, str(dest)]
    p = runner(cmd)
    if p.returncode != 0 and ref:  # a commit sha cannot be cloned with --branch
        shutil.rmtree(dest, ignore_errors=True)
        p = runner(["git", "clone", "-q", url, str(dest)])
        if p.returncode == 0:
            p = runner(["git", "-C", str(dest), "checkout", "-q", ref])
    if p.returncode != 0:
        raise PackageError(f"git clone {url} не удался:\n{(p.stderr or p.stdout or '').strip()[-2000:]}")
    rev = runner(["git", "-C", str(dest), "rev-parse", "HEAD"])
    return (rev.stdout or "").strip()[:12]


def _build(kind: str, location: str, ref: Optional[str], staging: Path, runner: Runner) -> dict:
    """Install the source into ``staging``; returns {name, version, entry_points, revision?}."""
    site = staging / "site"
    site.mkdir(parents=True)
    info: dict = {}
    src: Optional[Path] = None
    if kind == "git":
        src = staging / "src"
        info["revision"] = _git_clone(location, ref, src, runner)
    elif kind == "path":
        src = staging / "src"   # build from a copy: pip/setuptools never write into the person's directory
        shutil.copytree(location, src, ignore=_IGNORE)
    if src is not None and not ((src / "pyproject.toml").exists() or (src / "setup.py").exists()):
        if not (src / TOML_MANIFEST).exists():
            raise PackageError(f"{location}: не пакет плагина — нет pyproject.toml, setup.py или {TOML_MANIFEST}")
        data = _read_toml(src / TOML_MANIFEST)
        eps = {str(k): str(v) for k, v in (data.get("plugins") or {}).items()}
        if not data.get("name") or not eps:
            raise PackageError(f"{TOML_MANIFEST}: нужны name и [plugins]")
        shutil.copytree(src, site, dirs_exist_ok=True, ignore=_IGNORE)
        (site / TOML_MANIFEST).unlink(missing_ok=True)
        return {**info, "name": str(data["name"]), "version": str(data.get("version", "")), "entry_points": eps,
                "summary": str(data.get("description", ""))}
    _pip(str(src) if src is not None else location, site, runner, local=src is not None)
    dists = [d for d in _dists(site) if d["entry_points"]]
    if not dists:
        raise PackageError(f"{location}: пакет не объявляет точек входа [project.entry-points.\"{ENTRY_POINT_GROUP}\"]")
    want = None
    if src is not None and (src / "pyproject.toml").exists():
        want = (_read_toml(src / "pyproject.toml").get("project") or {}).get("name")
    elif kind == "pip":
        want = re.match(r"[A-Za-z0-9._-]+", location).group(0) if re.match(r"[A-Za-z0-9._-]+", location) else None
    pick = [d for d in dists if want and norm(d["name"]) == norm(want)] or dists
    if len(pick) > 1:
        raise PackageError("в установке несколько пакетов с плагинами: " + ", ".join(d["name"] for d in pick)
                           + " — ставьте их по одному")
    return {**info, **pick[0]}


# --- install / update / remove -----------------------------------------------------------------------

def install(source: str, *, runner: Runner = _run, replace: bool = False) -> dict:
    """Install a package (disabled). Returns its registry entry, including the static manifests."""
    kind, location, ref = classify(source)
    plugins_dir().mkdir(parents=True, exist_ok=True)
    staging = plugins_dir() / f".staging-{uuid.uuid4().hex[:8]}"
    try:
        built = _build(kind, location, ref, staging, runner)
        name = norm(built["name"])
        with _lock:
            reg = load_registry()
            old = reg["packages"].get(name)
            if old and not replace:
                raise PackageError(f"{name} уже установлен; обновление: artel plugins update {name}")
            dest = plugins_dir() / name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(staging / "site"), str(dest))
            now = time.time()
            entry = {"name": name, "dist": built["name"], "version": built.get("version", ""),
                     "summary": built.get("summary", ""), "kind": kind,
                     "source": location if kind == "path" else source,
                     "revision": built.get("revision"), "path": str(dest),
                     "entry_points": built["entry_points"], **describe_site(dest, built["entry_points"]),
                     "enabled": False, "error": None,
                     "installed_at": (old or {}).get("installed_at", now), "updated_at": now}
            if old:
                grew = set(entry["permissions"]) - set(old.get("permissions", []))
                entry["enabled"] = bool(old.get("enabled")) and not grew
                if old.get("enabled") and grew:
                    entry["error"] = "новая версия просит новые разрешения: " + ", ".join(sorted(grew)) + \
                                     " — проверьте манифест и включите снова"
            reg["packages"][name] = entry
            save_registry(reg)
            return entry
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def update(name: str, *, kernel=None, runner: Runner = _run) -> dict:
    old = get(name)
    if kernel is not None:
        detach(kernel, old)
    entry = install(old["source"], runner=runner, replace=True)
    if kernel is not None and entry["enabled"]:
        attach(kernel, entry)
    return get(entry["name"])


def remove(name: str, *, kernel=None) -> dict:
    entry = get(name)
    if kernel is not None:
        detach(kernel, entry)
    _forget(Path(entry["path"]))
    shutil.rmtree(entry["path"], ignore_errors=True)
    with _lock:
        reg = load_registry()
        reg["packages"].pop(entry["name"], None)
        save_registry(reg)
    return entry


def _set(name: str, **fields: Any) -> dict:
    with _lock:
        reg = load_registry()
        entry = reg["packages"].get(norm(name))
        if entry is None:
            raise PackageError(f"плагин {name} не установлен")
        entry.update(fields)
        save_registry(reg)
        return entry


# --- enable / disable / load -------------------------------------------------------------------------

def _forget(site: Path) -> None:
    """Take a package directory off ``sys.path`` and drop the modules imported from it."""
    s = str(site)
    while s in sys.path:
        sys.path.remove(s)
    for mod_name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None) or ""
        if f.startswith(s + os.sep):
            sys.modules.pop(mod_name, None)
    importlib.invalidate_caches()


def plugin_ids(entry: dict) -> list[str]:
    """Kernel record names of a package's plugins (the entry point targets)."""
    return list(entry.get("entry_points", {}).values())


def attach(kernel, entry: dict, persist: bool = True) -> Optional[str]:
    """Load an enabled package into ``kernel``. All or nothing: on failure its plugins are rolled back, the package
    is switched off with the error (``persist``) and the error is returned."""
    site = Path(entry["path"])
    loaded: list[str] = []
    error = None
    if not site.is_dir():
        error = f"папка пакета пропала: {site}"
    else:
        if str(site) not in sys.path:
            sys.path.append(str(site))
            importlib.invalidate_caches()
        for target in plugin_ids(entry):
            mod_name, _, attr = target.partition(":")
            if target in kernel.records:
                continue
            try:
                obj = getattr(importlib.import_module(mod_name), attr) if attr else None
            except (Exception, SystemExit) as exc:  # noqa: BLE001 - untrusted import
                kernel.failed[target] = f"{type(exc).__name__}: {exc}"
                obj = False
            record = kernel.load_plugin_safe(target, obj) if obj is not False else None
            if record is None:
                error = f"{target}: {kernel.failed.get(target, 'не загрузился')}"
                break
            loaded.append(target)
    if error is None:
        return None
    for target in reversed(loaded):
        try:
            kernel.unload_plugin(target)
        except Exception:  # noqa: BLE001
            pass
    _forget(site)
    kernel.failed[f"package:{entry['name']}"] = error
    if persist:
        try:
            _set(entry["name"], enabled=False, error=error, failed_at=time.time())
        except PackageError:
            pass
    return error


def detach(kernel, entry: dict) -> None:
    for target in reversed(plugin_ids(entry)):
        if target in kernel.records:
            try:
                kernel.unload_plugin(target)
            except Exception:  # noqa: BLE001 - keep unloading
                pass
    _forget(Path(entry["path"]))


def load_enabled(kernel) -> list[str]:
    """Attach every enabled package; returns the errors (each failing package is switched off)."""
    errors = []
    try:
        packages = list(load_registry()["packages"].values())
    except Exception as exc:  # noqa: BLE001 - never fatal
        kernel.failed["plugins.json"] = repr(exc)
        return [repr(exc)]
    for entry in packages:
        if entry.get("enabled"):
            err = attach(kernel, entry)
            if err:
                errors.append(f"{entry['name']}: {err}")
    return errors


def enable(name: str, *, kernel=None, config: Optional[dict] = None) -> dict:
    """Switch a package on. With a live ``kernel`` it is loaded there; otherwise it is test-loaded in a fresh
    kernel (setup and teardown run once) so a broken plugin is reported now, not at the next run."""
    entry = _set(name, enabled=True, error=None)
    if kernel is not None:
        attach(kernel, entry)
        return get(name)
    from .kernel import build_kernel
    from .project import deep_merge, load_config

    trial = build_kernel(deep_merge(config or load_config(), {"plugin_packages": False}))
    if attach(trial, entry) is None:
        trial.shutdown(reason="trial")
        _forget(Path(entry["path"]))
    return get(name)


def disable(name: str, *, kernel=None) -> dict:
    entry = _set(name, enabled=False)
    if kernel is not None:
        detach(kernel, entry)
    return entry


def set_builtin(dotted: str, enabled: bool, *, kernel=None) -> dict:
    from .kernel import CORE_PLUGINS, DEFAULT_PLUGINS

    if dotted not in DEFAULT_PLUGINS:
        raise PackageError(f"{dotted} — не встроенный плагин")
    if dotted in CORE_PLUGINS and not enabled:
        raise PackageError(f"{dotted} — ядро Artel, его нельзя выключить")
    with _lock:
        reg = load_registry()
        off = [d for d in reg["builtin_disabled"] if d != dotted] + ([] if enabled else [dotted])
        reg["builtin_disabled"] = off
        save_registry(reg)
    if kernel is not None:
        if enabled and dotted not in kernel.records:
            kernel.load_plugin_safe(dotted)
        elif not enabled and dotted in kernel.records:
            kernel.unload_plugin(dotted)
    return {"name": dotted, "enabled": enabled}


# --- overview ------------------------------------------------------------------------------------------

def overview(config: Optional[dict] = None, kernel=None) -> list[dict]:
    """Built-in, configured and installed plugins with their state, for ``artel plugins list`` and the dashboard."""
    import importlib.util as iu

    from .kernel import CORE_PLUGINS, DEFAULT_PLUGINS

    config = config or (kernel.config if kernel is not None else {})
    off = disabled_builtins() | set(config.get("disable_plugins", []))
    failed = getattr(kernel, "failed", {}) if kernel is not None else {}
    rows = []
    for kind, names in (("builtin", DEFAULT_PLUGINS), ("config", list(config.get("plugins", [])))):
        for dotted in names:
            try:
                spec = iu.find_spec(dotted)
            except (ImportError, ValueError):
                spec = None
            origin = Path(spec.origin) if spec and spec.origin and spec.origin.endswith(".py") else None
            m = static_manifest(origin, dotted)
            rows.append({"id": dotted, "name": m.get("name", dotted), "kind": kind, "version": m.get("version", ""),
                         "enabled": dotted not in off, "core": dotted in CORE_PLUGINS,
                         "loaded": kernel is not None and dotted in kernel.records,
                         "error": failed.get(dotted), "manifests": {dotted: m}, "permissions": m["permissions"],
                         "source": "artel" if kind == "builtin" else "artel.yaml: plugins"})
    for entry in load_registry()["packages"].values():
        rows.append({**entry, "id": entry["name"], "kind": "package", "core": False,
                     "loaded": kernel is not None and all(t in kernel.records for t in plugin_ids(entry))
                     and bool(plugin_ids(entry))})
    return rows


__all__ = ["install", "update", "remove", "enable", "disable", "set_builtin", "overview", "load_enabled", "attach",
           "detach", "classify", "static_manifest", "PackageError", "PERMISSIONS", "ENTRY_POINT_GROUP"]
