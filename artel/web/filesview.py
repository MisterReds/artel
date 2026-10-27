"""File viewer backend: what the dashboard may show of a project's shared/ folder.

One rule for every route: a path is relative to shared/, and it is served only if its real location (symlinks
followed) is inside shared/ - the same filter the archive uses. Pure helpers; the HTTP handler only sends results.
"""

from __future__ import annotations

import csv
import io
import mimetypes
import os
import re
import stat
from pathlib import Path
from typing import Optional
from urllib.parse import quote

PREVIEW_MAX = 2 * 1024 * 1024  # larger files are offered for download only
CSV_ROWS = 1000
TREE_MAX = 5000
COLLAPSED = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache", ".mypy_cache", ".ruff_cache"}

LANGS = {".py": "py", ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js", ".ts": "ts", ".tsx": "ts",
         ".json": "json", ".css": "css", ".html": "html", ".htm": "html", ".sh": "sh", ".bash": "sh", ".md": "md",
         ".markdown": "md", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".sql": "sql", ".xml": "xml",
         ".svg": "xml", ".go": "go", ".rs": "rs", ".java": "java", ".c": "c", ".h": "c", ".cpp": "cpp",
         ".rb": "rb", ".php": "php", ".ini": "ini", ".cfg": "ini"}
IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg", ".avif"}
VIDEOS = {".mp4", ".webm", ".mov", ".ogv", ".m4v"}
TEXTISH = {".txt", ".log", ".env", ".gitignore", ".csv", ".tsv", ".rst", ".lock"}
# content types for the raw/preview routes; anything else is an opaque download
TYPES = {".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
         ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
         ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
         ".ico": "image/x-icon", ".bmp": "image/bmp", ".avif": "image/avif", ".pdf": "application/pdf",
         ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
         ".ogv": "video/ogg", ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
         ".woff2": "font/woff2", ".woff": "font/woff", ".wasm": "application/wasm"}


def resolve(base: Path, rel: str) -> Optional[Path]:
    """The real path of `rel` inside `base`, or None when it points outside (.., absolute, symlink out)."""
    if "\0" in rel:
        return None
    rel = rel.strip()
    if rel.startswith(("/", "\\")) or Path(rel).is_absolute() or (len(rel) > 1 and rel[1] == ":"):
        return None
    base = base.resolve()
    try:
        target = (base / rel).resolve()
    except (OSError, RuntimeError):
        return None
    return target if target == base or target.is_relative_to(base) else None


def _fd_path(fd: int) -> Optional[str]:
    """Where an open file really is: /proc on Linux, F_GETPATH on macOS; None when the platform cannot say."""
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        pass
    try:
        import fcntl

        if hasattr(fcntl, "F_GETPATH"):
            return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024).split(b"\0", 1)[0])
    except (ImportError, OSError):
        pass
    return None


def open_inside(base: Path, rel: str) -> Optional[tuple[int, Path]]:
    """Open `rel` (a regular file inside `base`) once and return (fd, real path), or None. The check is made on the
    descriptor that is then read, so a path swapped for a symlink after resolve() cannot lead outside: the last
    component is opened with O_NOFOLLOW and the descriptor's own location is verified."""
    real = resolve(base, rel)
    if real is None:
        return None
    try:
        fd = os.open(real, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
                     | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    ok = False
    try:
        st = os.fstat(fd)
        if stat.S_ISREG(st.st_mode):
            where = _fd_path(fd)
            if where is not None:
                ok = Path(where).is_relative_to(base.resolve())
            else:  # no way to ask the OS: the file at the freshly resolved path must be the one we hold
                again = resolve(base, rel)
                if again is not None:
                    now = os.stat(again)
                    ok = (now.st_dev, now.st_ino) == (st.st_dev, st.st_ino)
    except OSError:
        ok = False
    if not ok:
        os.close(fd)
        return None
    return fd, real


def lang_of(path: Path) -> Optional[str]:
    return LANGS.get(path.suffix.lower())


def is_text(head: bytes) -> bool:
    if b"\0" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as exc:  # a multi-byte character cut by the sample is fine
        return exc.start >= len(head) - 3


KNOWN = set(LANGS) | IMAGES | VIDEOS | TEXTISH | {".pdf", ".csv", ".tsv"}


def kind_of(path: Path, head: bytes = b"") -> str:
    """markdown | html | code | json | csv | image | pdf | video | text | binary"""
    ext = path.suffix.lower()
    if ext in (".md", ".markdown"):
        return "markdown"
    if ext in (".html", ".htm"):
        return "html"
    if ext == ".json":
        return "json"
    if ext in (".csv", ".tsv"):
        return "csv"
    if ext in IMAGES:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext in VIDEOS:
        return "video"
    if ext in LANGS:
        return "code"
    if ext in TEXTISH or not ext:
        return "text" if is_text(head) else "binary"
    return "text" if is_text(head) else "binary"


def _head(path: Path, n: int = 4096) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except OSError:
        return b"\0"


def last_start(tasks: list[dict]) -> float:
    """When the most recently started task began (0 if none did)."""
    return max((t.get("started_at") or 0 for t in tasks), default=0)


def tree(base: Path, since: float = 0.0) -> dict:
    """Every file under `base` (flat list, paths relative to it). Noise folders appear collapsed with no children;
    symlinks that lead outside are left out. `changed`: modified since `since` (start of the last task)."""
    base = base.resolve()
    items: list[dict] = []
    truncated = False
    for dirpath, dirnames, files in os.walk(base):
        d = Path(dirpath)
        keep = []
        for name in sorted(dirnames):
            p = d / name
            real = resolve(base, p.relative_to(base).as_posix())
            if real is None or not real.is_dir():
                continue
            rel = p.relative_to(base).as_posix()
            if name in COLLAPSED:
                items.append({"path": rel, "dir": True, "collapsed": True, "size": 0})
                continue
            items.append({"path": rel, "dir": True, "size": 0})
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(files):
            p = d / name
            rel = p.relative_to(base).as_posix()
            real = resolve(base, rel)
            if real is None or not real.is_file():
                continue
            st = real.stat()
            items.append({"path": rel, "dir": False, "size": st.st_size, "mtime": st.st_mtime,
                          "kind": kind_of(real, _head(real, 512) if real.suffix.lower() not in KNOWN else b""),
                          "changed": bool(since) and st.st_mtime >= since})
        if len(items) >= TREE_MAX:
            truncated = True
            break
    return {"items": items[:TREE_MAX], "truncated": truncated}


def csv_rows(text: str, ext: str = ".csv") -> dict:
    delim = "\t" if ext == ".tsv" else ","
    try:
        if ext == ".csv":
            delim = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
    except csv.Error:
        pass
    rd = csv.reader(io.StringIO(text, newline=""), delimiter=delim)
    rows, total = [], 0
    header: list[str] = []
    for i, row in enumerate(rd):
        if i == 0:
            header = row
            continue
        total += 1
        if len(rows) < CSV_ROWS:
            rows.append(row)
    return {"header": header, "rows": rows, "total_rows": total}


def preview(base: Path, rel: str) -> Optional[dict]:
    """Preview payload for one file, or None when the path is refused / not a file."""
    opened = open_inside(base, rel)
    if opened is None:
        return None
    fd, real = opened
    with os.fdopen(fd, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        head = f.read(4096)
        kind = kind_of(real, head)
        out = {"path": rel, "name": real.name, "size": size, "kind": kind, "lang": lang_of(real)}
        if kind in ("image", "pdf", "video", "binary"):
            return out
        data = head + f.read(PREVIEW_MAX + 1 - len(head))  # never more than the limit (+1 to see it is exceeded)
    if len(data) > PREVIEW_MAX:
        return {**out, "kind": "toolarge"}
    text = data.decode("utf-8", errors="replace")
    if kind == "csv":
        try:
            return {**out, **csv_rows(text, real.suffix.lower())}
        except csv.Error:  # e.g. a field above the parser's size limit: show the raw text instead
            return {**out, "kind": "text", "text": text, "note": "Не удалось разобрать таблицу, показан исходный текст"}
    return {**out, "text": text}


def content_type(path: Path) -> str:
    return TYPES.get(path.suffix.lower()) or mimetypes.types_map.get(path.suffix.lower()) or "application/octet-stream"


def attachment(name: str) -> str:
    """Content-Disposition for a download: an ASCII fallback plus the RFC 5987 UTF-8 name (headers are Latin-1)."""
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", name) or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"
