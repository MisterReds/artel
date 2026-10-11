"""Per-task git worktrees over the shared deliverables (W2-worktree-isolation).

Every task's worker writes into its own worktree; the shared deliverables change only when a reviewer accepts
the work. Rejected work is dropped by resetting the worktree to the base; parallel tasks that touched the same
lines never overwrite each other: the conflict is left in the task's worktree (with git conflict markers) and the
task goes back to its worker to resolve on the updated base.

Layout (under the project root, never inside the deliverables, never touching a .git of the deliverables):

    isolation.git/            bare repo; branch ``main`` mirrors shared/ (work tree passed with --work-tree)
    worktrees/<task id>/      ``git worktree`` of branch ``task/<task id>``

Flow:
    ensure(task)     commit outside changes of shared/ to main, create/reuse the task worktree from main
    integrate(task)  commit the worktree, merge the current main into the task branch (conflicts -> list)
    accept(task)     integrate, then fast-forward main to the task branch and update shared/ (read-tree -m -u)
    discard(task)    reset the worktree to main (a rejected attempt is gone)
    drop(task)       remove the worktree and its branch (after acceptance or cancellation)

Ideas from Qwen Code packages/core/src/services/gitWorktreeService.ts (Apache-2.0, Copyright Qwen Team): one
worktree + branch per agent session under a dedicated directory, a baseline commit of the source tree before
the agent starts, and a merge back into the source when the work is applied. Reimplemented here for Artel.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

from .snapshot import EXCLUDES

MAIN = "main"
CONFLICT_MARKERS = ("<<<<<<< ", ">>>>>>> ")


class IsolationError(RuntimeError):
    """A git operation of the isolation layer failed (the caller treats it like a failed run)."""


def isolation_enabled(config: dict) -> bool:
    return bool((config.get("isolation") or {}).get("worktrees"))


class Isolation:
    def __init__(self, project, git_dir: Optional[Path] = None, base_dir: Optional[Path] = None):
        self.project = project
        self.shared = Path(project.shared)
        self.git_dir = Path(git_dir or project.root / "isolation.git")
        self.base_dir = Path(base_dir or project.root / "worktrees")
        self.lock = threading.RLock()

    @staticmethod
    def available() -> bool:
        return shutil.which("git") is not None

    # --- git plumbing ---------------------------------------------------------------------------------------
    def _run(self, args: list, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["git", "-c", "user.name=artel", "-c", "user.email=artel@localhost", "-c", "core.autocrlf=false",
               "-c", "core.quotepath=false", "-c", "gc.auto=0", "-c", "commit.gpgsign=false",
               "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "advice.detachedHead=false",
               *args]
        try:
            p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=300,
                               env={**_clean_git_env(), "GIT_TERMINAL_PROMPT": "0"})
        except (OSError, subprocess.SubprocessError) as exc:
            raise IsolationError(f"git {' '.join(args[:2])}: {exc}") from exc
        if check and p.returncode != 0:
            raise IsolationError(f"git {' '.join(map(str, args[:3]))} failed: {(p.stderr or p.stdout).strip()[:800]}")
        return p

    def _main(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """git on the shared deliverables (work tree) with the isolation repo as git dir."""
        return self._run([f"--git-dir={self.git_dir}", f"--work-tree={self.shared}", *args], self.shared, check)

    def _repo(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return self._run([f"--git-dir={self.git_dir}", *args], self.git_dir, check)

    def _wt(self, task_id: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """git in a task's worktree with its git dir named explicitly. The worktree's own ``.git`` file is writable
        by the (sandboxed) worker; git must never discover the repository through it, or a worker could point it at
        a repository of its own whose config runs commands outside the sandbox."""
        wt = self.path(task_id)
        admin = self._admin(task_id)
        if admin is None:
            raise IsolationError(f"task {task_id}: its worktree is not registered in isolation.git")
        return self._run([f"--git-dir={admin}", f"--work-tree={wt}", *args], wt, check)

    def _admin(self, task_id: str) -> Optional[Path]:
        """isolation.git/worktrees/<name> of this task's worktree (found by its recorded path, not the .git file)."""
        want = self.path(task_id).resolve()  # the directory, not .git: the worker may replace .git with a link
        for d in sorted((self.git_dir / "worktrees").glob("*")):
            try:
                if Path((d / "gitdir").read_text(encoding="utf-8").strip()).parent.resolve() == want:
                    return d
            except OSError:
                continue
        return None

    def path(self, task_id: str) -> Path:
        return self.base_dir / task_id

    @staticmethod
    def branch(task_id: str) -> str:
        return f"task/{task_id}"

    def _rev(self, ref: str) -> Optional[str]:
        p = self._repo("rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False)
        return p.stdout.strip() or None if p.returncode == 0 else None

    # --- lifecycle ------------------------------------------------------------------------------------------
    def _init(self) -> None:
        if not (self.git_dir / "HEAD").exists():
            self.git_dir.mkdir(parents=True, exist_ok=True)
            self._run(["init", "-q", "--bare", str(self.git_dir)], self.git_dir)
            self._repo("symbolic-ref", "HEAD", f"refs/heads/{MAIN}")
        info = self.git_dir / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "exclude").write_text("\n".join(EXCLUDES) + "\n")
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def sync_main(self) -> str:
        """Commit whatever changed in shared/ outside the isolation flow (people, other roles) onto main."""
        with self.lock:
            self._init()
            self._main("add", "-A")
            head = self._rev(MAIN)
            if head is None:
                self._main("commit", "-q", "--allow-empty", "--no-verify", "-m", "artel: base of shared deliverables")
            elif self._main("diff", "--cached", "--quiet", head, check=False).returncode != 0:
                self._main("commit", "-q", "--no-verify", "-m", "artel: changes made in shared/ outside a task")
            return self._rev(MAIN) or ""

    def exists(self, task_id: str) -> bool:
        return (self.path(task_id) / ".git").exists() and self._admin(task_id) is not None

    def ensure(self, task_id: str) -> Path:
        """The task's worktree, created from the current main on first use and reused by later attempts."""
        with self.lock:
            self.sync_main()
            wt = self.path(task_id)
            if self.exists(task_id):
                return wt
            self._repo("worktree", "prune")
            if wt.exists():
                shutil.rmtree(wt, ignore_errors=True)
            if self._rev(self.branch(task_id)):
                self._repo("worktree", "add", "-q", str(wt), self.branch(task_id))
            else:
                self._repo("worktree", "add", "-q", "-b", self.branch(task_id), str(wt), MAIN)
            return wt

    def _merging(self, task_id: str) -> bool:
        return self._wt(task_id, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0

    def unresolved(self, task_id: str) -> list[str]:
        """Files still carrying conflict markers from an earlier merge of main into the task branch."""
        if not self.exists(task_id) or not self._merging(task_id):
            return []
        # unmerged index entries stay until our own commit() stages the worktree, whatever the worker edits
        files = self._wt(task_id, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
        out = []
        for rel in sorted(set(files)):
            f = self.path(task_id) / rel
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            if any(line.startswith(CONFLICT_MARKERS) for line in text.splitlines()):
                out.append(rel)
        return out

    def commit(self, task_id: str, message: str = "") -> str:
        """Commit everything in the worktree (also finishes a merge whose conflicts the worker resolved)."""
        with self.lock:
            self._wt(task_id, "add", "-A")
            dirty = self._wt(task_id, "diff", "--cached", "--quiet", "HEAD", check=False).returncode != 0
            if dirty or self._merging(task_id):
                self._wt(task_id, "commit", "-q", "--no-verify", "-m", message or f"artel: work on task {task_id}")
            return self._wt(task_id, "rev-parse", "HEAD").stdout.strip()

    def integrate(self, task_id: str) -> list[str]:
        """Bring the current main into the task branch. Returns conflicting files ([] = clean).

        On conflict the worktree stays in the merge state with git's conflict markers, so the worker resolves
        them on the updated base; nothing in shared/ is touched.
        """
        with self.lock:
            self.ensure(task_id)
            left = self.unresolved(task_id)
            if left:
                return left
            self.commit(task_id)
            self.sync_main()
            p = self._wt(task_id, "merge", "--no-edit", "-q", MAIN, check=False)
            if p.returncode == 0:
                return []
            conflicts = self._wt(task_id, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
            if not conflicts:  # failed for another reason (e.g. an untracked file in the way): retry is clean
                self._wt(task_id, "merge", "--abort", check=False)
                raise IsolationError(f"merging the updated base failed: {(p.stderr or p.stdout).strip()[:800]}")
            return sorted(conflicts)

    def changed_files(self, task_id: str) -> list[str]:
        """Files the task changed relative to main (for the reviewer)."""
        if not self.exists(task_id) or not self._rev(MAIN):
            return []
        p = self._wt(task_id, "diff", "--name-only", MAIN, check=False)
        return sorted(p.stdout.split())

    def accept(self, task_id: str) -> tuple[bool, list[str]]:
        """Merge accepted work into shared/. Returns (merged, conflicting files)."""
        with self.lock:
            conflicts = self.integrate(task_id)
            if conflicts:
                return False, conflicts
            old = self.sync_main()
            new = self._rev(self.branch(task_id))
            if not new:
                raise IsolationError(f"task branch {self.branch(task_id)} is missing")
            if old != new:
                if self._repo("merge-base", "--is-ancestor", old, new, check=False).returncode != 0:
                    return False, self.integrate(task_id) or ["(base moved; re-run)"]
                # two-tree merge: updates the index and shared/ from old to new, removing deleted files
                self._main("read-tree", "-m", "-u", old, new)
                self._repo("update-ref", f"refs/heads/{MAIN}", new, old)
            self.drop(task_id)
            return True, []

    def discard(self, task_id: str) -> bool:
        """Throw the attempt away: the worktree returns to the current main."""
        with self.lock:
            if not self.exists(task_id):
                return False
            self.sync_main()
            if self._merging(task_id):
                self._wt(task_id, "merge", "--abort", check=False)
            self._wt(task_id, "reset", "-q", "--hard", MAIN)
            self._wt(task_id, "clean", "-q", "-fd")
            return True

    def drop(self, task_id: str) -> None:
        """Remove the worktree and its branch (the work is merged, or the task is cancelled)."""
        with self.lock:
            if not (self.git_dir / "HEAD").exists():
                return
            if self.path(task_id).exists():
                self._repo("worktree", "remove", "--force", str(self.path(task_id)), check=False)
                shutil.rmtree(self.path(task_id), ignore_errors=True)
            self._repo("worktree", "prune", check=False)
            self._repo("branch", "-q", "-D", self.branch(task_id), check=False)


def _clean_git_env() -> dict:
    """The environment without GIT_DIR & co. inherited from a surrounding git process (hooks, worktrees)."""
    import os

    return {k: v for k, v in os.environ.items()
            if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_PREFIX",
                         "GIT_COMMON_DIR")}


__all__ = ["Isolation", "IsolationError", "isolation_enabled"]
