"""Self-update: an installed Artel pulls the improvements made since it was installed.

The installation is a git clone. `status` fetches the update channel (remote + branch, default origin/master,
where evolution merges only tested changes) and reports what is new: commits, roadmap items that became done,
whether dependencies changed. `apply` refuses to touch local edits or a diverged history, verifies the new
version in a temporary worktree (the pillar invariants by default) before switching, fast-forwards, reinstalls
dependencies when pyproject.toml changed, and records the step so `rollback` can return to the previous version.

Config (artel.yaml):  update: {remote: origin, branch: master, verify: invariants|full|none, check_interval_min: 30}
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import yaml

SMOKE = ("import importlib, pkgutil, artel, artel.cli\n"
         "for m in pkgutil.walk_packages(artel.__path__, 'artel.'):\n"
         "    if '.web.static' not in m.name: importlib.import_module(m.name)\n"
         "try:\n    artel.cli.main(['--help'])\nexcept SystemExit as e:\n    assert not e.code, e.code\n")
DEFAULTS = {"remote": "origin", "branch": "master", "verify": "invariants", "check_interval_min": 30}


class UpdateError(RuntimeError):
    pass


def install_root() -> Path:
    return Path(__file__).resolve().parent.parent


class Updater:
    def __init__(self, root: Optional[Path] = None, config: Optional[dict] = None, home: Optional[Path] = None):
        self.root = Path(root or install_root())
        self.cfg = {**DEFAULTS, **((config or {}).get("update") or {})}
        if home is None:
            from .project import artel_home

            home = artel_home()
        self.history_path = Path(home) / "update_history.json"
        self._last: Optional[dict] = None

    # --- git ------------------------------------------------------------
    def _git(self, *args: str, check: bool = True, cwd: Optional[Path] = None) -> str:
        p = subprocess.run(["git", *args], cwd=cwd or self.root, capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace")
        if check and p.returncode != 0:
            raise UpdateError(f"git {' '.join(args)}: {(p.stderr or p.stdout).strip()[-400:]}")
        return p.stdout.strip()

    @property
    def upstream(self) -> str:
        return f"{self.cfg['remote']}/{self.cfg['branch']}"

    def is_git_install(self) -> bool:
        return (self.root / ".git").exists()

    # --- status ---------------------------------------------------------
    def status(self, fetch: bool = True) -> dict:
        if not self.is_git_install():
            return {"available": False, "can_update": False,
                    "reason": "this installation is not a git clone; reinstall with `git clone` to enable updates"}
        out = {"checked_at": time.time(), "channel": self.upstream, "fetch_error": None}
        if fetch:
            try:
                self._git("fetch", "--quiet", self.cfg["remote"], self.cfg["branch"])
            except (UpdateError, subprocess.TimeoutExpired) as e:
                out["fetch_error"] = str(e)
        cur = self._git("rev-parse", "HEAD")
        remote = self._git("rev-parse", "--verify", "--quiet", self.upstream, check=False)
        out.update(current=cur, remote=remote or None, branch=self._git("rev-parse", "--abbrev-ref", "HEAD"))
        if not remote:
            out.update(available=False, can_update=False, reason=f"{self.upstream} not found")
            return self._remember(out)
        behind = int(self._git("rev-list", "--count", f"HEAD..{remote}") or 0)
        ahead = int(self._git("rev-list", "--count", f"{remote}..HEAD") or 0)
        dirty = [ln[3:] for ln in self._git("status", "--porcelain", "--untracked-files=no").splitlines()]
        log = self._git("log", "--first-parent", "--format=%H%x1f%cI%x1f%s", f"HEAD..{remote}") if behind else ""
        commits = [dict(zip(("sha", "date", "subject"), ln.split("\x1f"))) for ln in log.splitlines() if ln]
        changed = self._git("diff", "--name-only", "HEAD", remote).splitlines() if behind else []
        out.update(behind=behind, ahead=ahead, dirty=dirty, commits=commits[:100], changed_files=len(changed),
                   deps_changed="pyproject.toml" in changed, roadmap_done=self._roadmap_done(cur, remote),
                   available=behind > 0)
        reason = ""
        if not behind:
            reason = "up to date"
        elif dirty:
            reason = f"local changes in {len(dirty)} files; commit or discard them first"
        elif ahead:
            reason = f"local history has {ahead} commits not on {self.upstream}; updates only fast-forward"
        out.update(can_update=behind > 0 and not reason, reason=reason)
        return self._remember(out)

    def _remember(self, out: dict) -> dict:
        self._last = out
        return out

    def _roadmap_done(self, a: str, b: str) -> list[dict]:
        def statuses(sha: str) -> dict:
            text = self._git("show", f"{sha}:roadmap.yaml", check=False)
            try:
                data = yaml.safe_load(text) or {}
            except yaml.YAMLError:
                return {}
            return {it["id"]: it for it in data.get("items", []) if isinstance(it, dict) and "id" in it}

        old, new = statuses(a), statuses(b)
        return [{"id": k, "title": v.get("title", ""), "from": (v.get("source") or {}).get("harness", "")}
                for k, v in new.items() if v.get("status") == "done" and (old.get(k) or {}).get("status") != "done"]

    # --- apply / rollback -----------------------------------------------
    def verify(self, sha: str) -> dict:
        """Run the configured checks on ``sha`` in a temporary worktree; the installation is untouched."""
        mode = self.cfg["verify"]
        if mode == "none":
            return {"ok": True, "mode": mode, "output": ""}
        tmp = Path(tempfile.mkdtemp(prefix="artel-update-"))
        wt = tmp / "candidate"
        try:
            self._git("worktree", "add", "--detach", "--quiet", str(wt), sha)
            target = ["tests/test_invariants.py"] if mode == "invariants" and (wt / "tests" / "test_invariants.py").exists() \
                else ["tests"]
            env = {**os.environ, "PYTHONPATH": str(wt), "PYTHONDONTWRITEBYTECODE": "1",
                   "ARTEL_HOME": str(tmp / "home")}
            if not self._ensure_pytest():
                # no test runner and none could be installed (offline): at least prove the new version imports and
                # its CLI starts, instead of refusing every update
                p = subprocess.run([sys.executable, "-c", SMOKE], cwd=wt, capture_output=True, text=True,
                                   timeout=300, env=env, encoding="utf-8", errors="replace")
                return {"ok": p.returncode == 0, "mode": "smoke (pytest unavailable)",
                        "output": (p.stdout + p.stderr)[-2000:]}
            p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *target],
                               cwd=wt, capture_output=True, text=True, timeout=1800, env=env, encoding="utf-8", errors="replace")
            return {"ok": p.returncode == 0, "mode": mode, "output": (p.stdout + p.stderr)[-2000:]}
        finally:
            self._git("worktree", "remove", "--force", str(wt), check=False)
            self._git("worktree", "prune", check=False)
            shutil.rmtree(tmp, ignore_errors=True)

    def apply(self, verify: bool = True) -> dict:
        st = self.status(fetch=True)
        if not st.get("can_update"):
            raise UpdateError(st.get("reason") or "no update available")
        target, before = st["remote"], st["current"]
        if verify:
            v = self.verify(target)
            if not v["ok"]:
                self._record({"action": "refused", "from": before, "to": target, "reason": "verification failed",
                              "output": v["output"][-800:]})
                raise UpdateError(f"the new version fails its {v['mode']} checks; not installed:\n{v['output'][-1200:]}")
        self._git("merge", "--ff-only", "--quiet", target)
        deps = self._reinstall_deps() if st["deps_changed"] else None
        entry = self._record({"action": "update", "from": before, "to": target, "commits": len(st["commits"]),
                              "roadmap_done": [r["id"] for r in st["roadmap_done"]], "deps": deps})
        return {"updated": True, **entry, "restart_required": True}

    def rollback(self) -> dict:
        hist = [h for h in self.history() if h["action"] == "update"]
        if not hist:
            raise UpdateError("no update to roll back")
        last = hist[-1]
        cur = self._git("rev-parse", "HEAD")
        if cur != last["to"]:
            raise UpdateError(f"HEAD is {cur[:10]}, not the last update {last['to'][:10]}; roll back by hand")
        if self._git("status", "--porcelain", "--untracked-files=no"):
            raise UpdateError("local changes present; commit or discard them first")
        self._git("reset", "--hard", "--quiet", last["from"])
        entry = self._record({"action": "rollback", "from": cur, "to": last["from"]})
        return {"rolled_back": True, **entry, "restart_required": True}

    @staticmethod
    def _ensure_pytest() -> bool:
        """The checks need pytest; installations made by the installer do not have it. Install it into Artel's
        own environment on first use (small, no other effect); False when that is impossible (e.g. offline)."""
        import importlib.util

        if importlib.util.find_spec("pytest") is not None:
            return True
        p = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "pytest>=8"], capture_output=True,
                           text=True, timeout=600, encoding="utf-8", errors="replace")
        importlib.invalidate_caches()
        return p.returncode == 0 and importlib.util.find_spec("pytest") is not None

    def _reinstall_deps(self) -> dict:
        p = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "-e", str(self.root)],
                           capture_output=True, text=True, timeout=900, encoding="utf-8", errors="replace")
        return {"ok": p.returncode == 0, "output": (p.stdout + p.stderr)[-500:]}

    # --- history --------------------------------------------------------
    def history(self) -> list[dict]:
        try:
            return json.loads(self.history_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return []

    def _record(self, entry: dict) -> dict:
        entry = {"ts": time.time(), **entry}
        hist = self.history() + [entry]
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.history_path.write_text(json.dumps(hist[-200:], ensure_ascii=False, indent=1), encoding="utf-8")
        return entry


def restart_process() -> None:
    """Replace the current process with a fresh one running the same command (new code is loaded)."""
    argv = [sys.executable, "-m", "artel.cli", *sys.argv[1:]]
    if sys.platform == "win32":
        # Windows has no exec: os.execv starts a new process without quoting its arguments (a path with spaces
        # splits) and returns the console early. Start the successor properly and leave.
        subprocess.Popen(argv)
        os._exit(0)
    os.execv(sys.executable, argv)
