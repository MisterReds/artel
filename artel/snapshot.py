"""Shadow-git snapshots of a work tree (so a rejected attempt can be reverted).

Ported idea from OpenCode (packages/opencode/src/snapshot/index.ts and session/revert.ts),
MIT License, Copyright (c) opencode contributors: keep a separate git dir, `git add -A` the
work tree into it, record the tree hash, and restore by reading that tree back.

The work tree's own .git is never touched. Every method degrades to None/False instead of raising.
"""

from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path
from typing import Optional

EXCLUDES = [".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"]  # tool caches are not work


def extract_tar(data: bytes, dest) -> bool:
    """Unpack a ``git archive`` tar into ``dest`` (regular files and directories only, nothing outside ``dest``)."""
    dest = Path(dest).resolve()
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            for m in tf.getmembers():
                target = (dest / m.name).resolve()
                if (m.isfile() or m.isdir()) and (target == dest or dest in target.parents):
                    tf.extract(m, dest)
        return True
    except (OSError, tarfile.TarError):
        return False


class Snapshot:
    def __init__(self, git_dir, work_tree, ignores: bool = True):
        """``ignores=False``: the work tree's own .gitignore files and the user's global excludes are not trusted
        (a guard must also see changes to ignored files such as node_modules/ or .env); only EXCLUDES are skipped."""
        self.git_dir = Path(git_dir)
        self.work_tree = Path(work_tree)
        self.ignores = ignores

    def _git(self, *args: str, check: bool = True) -> Optional[str]:
        cmd = ["git", f"--git-dir={self.git_dir}", f"--work-tree={self.work_tree}",
               "-c", "user.name=artel", "-c", "user.email=artel@localhost",
               "-c", "core.autocrlf=false", "-c", "core.quotepath=false", "-c", "gc.auto=0",
               "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
               *([] if self.ignores else ["-c", "core.excludesFile=/dev/null"]), *args]
        try:
            p = subprocess.run(cmd, cwd=self.work_tree, capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        if check and p.returncode != 0:
            return None
        return p.stdout

    def _init(self) -> bool:
        try:
            if not self.work_tree.is_dir():
                return False
            if not (self.git_dir / "HEAD").exists():
                self.git_dir.mkdir(parents=True, exist_ok=True)
                if self._git("init", "--quiet") is None:
                    return False
            info = self.git_dir / "info"
            info.mkdir(parents=True, exist_ok=True)
            (info / "exclude").write_text("\n".join(EXCLUDES) + "\n", encoding="utf-8")
            return True
        except OSError:
            return False

    def track(self) -> Optional[str]:
        """Snapshot the work tree; return the tree hash, or None on failure."""
        add = ["add", "-A"] if self.ignores else ["add", "-A", "--force", "--", ".",
                                                               *(f":(exclude,glob)**/{x}/**" for x in EXCLUDES)]
        if not self._init() or self._git(*add) is None:
            return None
        out = self._git("write-tree")
        return out.strip() or None if out is not None else None

    def export(self, tree: str, dest) -> bool:
        """Write the files of ``tree`` into the empty directory ``dest`` (the work tree is not touched)."""
        if not tree or not self._init():
            return False
        try:
            p = subprocess.run(["git", f"--git-dir={self.git_dir}", "archive", "--format=tar", tree],
                               capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            return False
        return p.returncode == 0 and extract_tar(p.stdout, dest)

    def _tree_files(self, tree: str) -> Optional[set[str]]:
        out = self._git("ls-tree", "-r", "-z", "--name-only", tree)
        return None if out is None else {f for f in out.split("\0") if f}

    def restore(self, tree: str) -> bool:
        """Reset the work tree to exactly `tree`, deleting files created after it."""
        if not tree or not self._init():
            return False
        keep = self._tree_files(tree)
        if keep is None or self._git("read-tree", tree) is None:
            return False
        if self._git("checkout-index", "-a", "-f") is None:
            return False
        skip = "--exclude-standard" if self.ignores else f"--exclude-from={self.git_dir / 'info' / 'exclude'}"
        extra = self._git("ls-files", "-o", skip, "-z")
        if extra is None:
            return False
        ok = True
        for rel in (f for f in extra.split("\0") if f):
            if rel in keep:
                continue
            try:
                (self.work_tree / rel).unlink()
            except OSError:
                ok = False
        self._prune_empty_dirs()
        return ok

    def _prune_empty_dirs(self) -> None:
        for d in sorted((p for p in self.work_tree.rglob("*") if p.is_dir() and ".git" not in p.parts),
                        key=lambda p: len(p.parts), reverse=True):
            try:
                d.rmdir()  # only succeeds when empty
            except OSError:
                pass

    def diff_files(self, tree: str) -> Optional[list[str]]:
        """Files that differ between `tree` and the current work tree (None on failure)."""
        now = self.track()
        if not tree or now is None:
            return None
        out = self._git("diff-tree", "-r", "--name-only", "-z", tree, now)
        return None if out is None else sorted(f for f in out.split("\0") if f)
