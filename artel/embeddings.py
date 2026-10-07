"""Embeddings: meaning-based search for skills, project files and code — local and private by default.

The default model is Google's EmbeddingGemma 2 (Apache-2.0, 740M parameters; text, code, images, video and audio in one
768-dimensional space, Matryoshka-truncatable to 512/256/128). Artel runs its text part on the person's machine with
llama.cpp's ``llama-server`` (GGUF from unsloth/embeddinggemma-2-GGUF, ~310 MB at Q8_0), installed on demand into
ARTEL_HOME/tools like the agent CLIs — no Python ML stack, no data leaving the machine.

Backends (``embeddings.backend``):
  auto  — local if installed, otherwise off (the default)
  local — the managed llama-server with the installed GGUF, started lazily on a free localhost port
  http  — any OpenAI-compatible ``/v1/embeddings`` endpoint (an external llama-server, Ollama, LM Studio, vLLM…)
  off   — no embeddings; retrieval falls back to lexical BM25 and nothing else changes

EmbeddingGemma is trained with short task prefixes (see the model card); they are applied to text when
``embeddings.prefixes`` is ``gemma`` (the default for the local model). Vectors are cached in
ARTEL_HOME/embeddings.sqlite by (model, dimension, prefixed text), so a skill or file is embedded once.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import math
import os
import socket
import sqlite3
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

DEFAULTS = {"backend": "auto", "dim": 768, "quant": "Q8_0", "url": "", "model": "", "api_key_env": "",
            "prefixes": "auto", "batch": 32, "timeout_s": 120}
MODEL_REPO = "unsloth/embeddinggemma-2-GGUF"
MODEL_NAME = "embeddinggemma-2"
DIMS = (768, 512, 256, 128)

# EmbeddingGemma 2 task instructions (model card, "Prompt instructions")
QUERY_PREFIX = {"search": "task: search result | query: {}", "code": "task: code retrieval | query: {}",
                "qa": "task: question answering | query: {}", "similarity": "task: sentence similarity | query: {}"}
DOC_PREFIX = "title: {} | text: {}"


def settings(config: dict) -> dict:
    return {**DEFAULTS, **(config.get("embeddings") or {})}


def model_dir() -> Path:
    from .tools import tools_dir

    return tools_dir() / "models"


def model_path(quant: str = "Q8_0") -> Path:
    return model_dir() / f"{MODEL_NAME}-{quant}.gguf"


def server_binary() -> Optional[str]:
    from .tools import EXE, tools_dir

    d = tools_dir() / "llama"
    if not d.exists():
        return None
    for p in sorted(d.rglob("llama-server" + EXE)):
        if p.is_file():
            return str(p)
    return None


def installed(config: dict) -> bool:
    return bool(server_binary()) and model_path(settings(config)["quant"]).exists()


def backend(config: dict) -> str:
    b = settings(config)["backend"]
    if b == "auto":
        return "local" if installed(config) else "off"
    return b


class EmbedError(RuntimeError):
    pass


# --- vector cache ------------------------------------------------------------------------------------------------------

class Cache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.db.execute("CREATE TABLE IF NOT EXISTS v (k TEXT PRIMARY KEY, vec BLOB)")
        self.lock = threading.Lock()

    @staticmethod
    def key(model: str, text: str) -> str:
        return hashlib.sha256(f"{model}\0{text}".encode("utf-8")).hexdigest()

    def get(self, keys: list[str]) -> dict:
        out = {}
        with self.lock:
            for i in range(0, len(keys), 500):
                part = keys[i:i + 500]
                q = f"SELECT k, vec FROM v WHERE k IN ({','.join('?' * len(part))})"
                for k, blob in self.db.execute(q, part):
                    out[k] = list(struct.unpack(f"{len(blob) // 4}f", blob))
        return out

    def put(self, items: dict) -> None:
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO v VALUES (?, ?)",
                                [(k, struct.pack(f"{len(v)}f", *v)) for k, v in items.items()])
            self.db.commit()


# --- the managed local server ----------------------------------------------------------------------------------------

class LocalServer:
    """One llama-server per Artel process, started on first use and stopped at exit."""

    _lock = threading.Lock()
    _proc: Optional[subprocess.Popen] = None
    _url = ""
    _model = ""

    @classmethod
    def url(cls, config: dict) -> str:
        s = settings(config)
        model = model_path(s["quant"])
        with cls._lock:
            if cls._proc and cls._proc.poll() is None and cls._model == str(model):
                return cls._url
            binary = server_binary()
            if not binary or not model.exists():
                raise EmbedError("локальная модель эмбеддингов не установлена: «Провайдеры» → «Эмбеддинги» → «Установить»")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            log = open(model_dir() / "llama-server.log", "ab")
            env = dict(os.environ)
            env["LD_LIBRARY_PATH"] = os.pathsep.join([str(Path(binary).parent), env.get("LD_LIBRARY_PATH", "")])
            cls._proc = subprocess.Popen(
                [binary, "-m", str(model), "--embedding", "--pooling", "mean", "--host", "127.0.0.1",
                 "--port", str(port), "-c", "8192", "-ub", "8192", "-b", "8192", "--no-webui"],
                stdout=log, stderr=subprocess.STDOUT, env=env)
            cls._url, cls._model = f"http://127.0.0.1:{port}", str(model)
            deadline = time.time() + 90
            while time.time() < deadline:
                if cls._proc.poll() is not None:
                    raise EmbedError(f"llama-server завершился (код {cls._proc.returncode}); журнал: "
                                     f"{model_dir() / 'llama-server.log'}")
                try:
                    with urllib.request.urlopen(cls._url + "/health", timeout=2) as r:
                        if r.status == 200:
                            return cls._url
                except (urllib.error.URLError, OSError):
                    pass
                time.sleep(0.3)
            cls.stop()
            raise EmbedError("llama-server не ответил за 90 с")

    @classmethod
    def stop(cls) -> None:
        if cls._proc and cls._proc.poll() is None:
            from .tools import terminate_tree

            terminate_tree(cls._proc)
            try:
                cls._proc.wait(10)
            except subprocess.TimeoutExpired:
                terminate_tree(cls._proc, force=True)
        cls._proc = None

    @classmethod
    def running(cls) -> bool:
        return bool(cls._proc and cls._proc.poll() is None)


atexit.register(LocalServer.stop)


# --- the embedder ------------------------------------------------------------------------------------------------------

def _normalize(v: list[float], dim: int) -> list[float]:
    v = v[:dim] if dim and len(v) > dim else v  # Matryoshka: a prefix of the vector is a smaller embedding
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class Embedder:
    def __init__(self, config: dict):
        self.cfg = settings(config)
        self.config = config
        self.kind = backend(config)
        if self.kind not in ("local", "http"):
            raise EmbedError("эмбеддинги выключены")
        if self.kind == "http" and not self.cfg["url"]:
            raise EmbedError("embeddings.url не задан")
        self.model = (MODEL_NAME + "-" + self.cfg["quant"]) if self.kind == "local" else (self.cfg["model"] or self.cfg["url"])
        pref = self.cfg["prefixes"]
        self.gemma = pref == "gemma" or (pref == "auto" and (self.kind == "local" or "embeddinggemma" in self.model.lower()))
        self.dim = int(self.cfg["dim"] or 0)
        from .project import artel_home

        self.cache = Cache(artel_home() / "embeddings.sqlite")

    def _url(self) -> str:
        return LocalServer.url(self.config) if self.kind == "local" else self.cfg["url"].rstrip("/")

    def _call(self, texts: list[str]) -> list[list[float]]:
        body = {"input": texts}
        if self.kind == "http" and self.cfg["model"]:
            body["model"] = self.cfg["model"]
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(self.cfg["api_key_env"]) if self.cfg["api_key_env"] else None
        if key:
            headers["Authorization"] = f"Bearer {key}"
        base = self._url()
        url = base if base.endswith("/embeddings") else base + ("/embeddings" if base.endswith("/v1") else "/v1/embeddings")
        req = urllib.request.Request(url, json.dumps(body).encode("utf-8"), headers)
        try:
            with urllib.request.urlopen(req, timeout=float(self.cfg["timeout_s"])) as r:
                data = json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise EmbedError(f"сервер эмбеддингов не ответил: {e}") from e
        rows = sorted(data.get("data") or [], key=lambda d: d.get("index", 0))
        if len(rows) != len(texts):
            raise EmbedError(f"сервер эмбеддингов вернул {len(rows)} векторов на {len(texts)} текстов")
        return [r["embedding"] for r in rows]

    def prepare(self, text: str, kind: str = "document", task: str = "search", title: str = "") -> str:
        text = text.strip() or " "
        if not self.gemma:
            return text
        if kind == "query":
            return QUERY_PREFIX.get(task, QUERY_PREFIX["search"]).format(text)
        return DOC_PREFIX.format(title.strip() or "none", text)

    def cached(self, texts: list[str], kind: str = "document", task: str = "search",
               titles: Optional[list[str]] = None) -> list[bool]:
        keys = [Cache.key(self.model, self.prepare(t, kind, task, (titles or [""] * len(texts))[i]))
                for i, t in enumerate(texts)]
        have = self.cache.get(list(dict.fromkeys(keys)))
        return [k in have for k in keys]

    def warm(self, texts: list[str], titles: Optional[list[str]] = None) -> None:
        """Embed documents in the background (one warming thread per process), so later searches over them are
        fully semantic without making the current caller wait."""
        with _warm_lock:
            _warm_queue.extend((self, t, (titles or [""] * len(texts))[i]) for i, t in enumerate(texts))
            global _warm_thread
            if _warm_thread is None or not _warm_thread.is_alive():
                _warm_thread = threading.Thread(target=_warm_loop, daemon=True, name="embed-warm")
                _warm_thread.start()

    def embed(self, texts: list[str], kind: str = "document", task: str = "search",
              titles: Optional[list[str]] = None) -> list[list[float]]:
        prepared = [self.prepare(t, kind, task, (titles or [""] * len(texts))[i]) for i, t in enumerate(texts)]
        keys = [Cache.key(self.model, p) for p in prepared]
        have = self.cache.get(list(dict.fromkeys(keys)))
        todo = list(dict.fromkeys(k for k in keys if k not in have))
        if todo:
            text_of = dict(zip(keys, prepared))
            step = max(1, int(self.cfg["batch"]))
            for i in range(0, len(todo), step):  # saved per batch: an interrupted run keeps what it computed
                part = todo[i:i + step]
                new = dict(zip(part, self._call([text_of[k] for k in part])))
                self.cache.put(new)
                have.update(new)
        return [_normalize(have[k], self.dim) for k in keys]


_embedders: dict = {}
_failures: dict = {}
_warm_lock = threading.Lock()
_warm_queue: list = []
_warm_thread: Optional[threading.Thread] = None


def _warm_loop() -> None:
    while True:
        with _warm_lock:
            batch, _warm_queue[:] = _warm_queue[:16], _warm_queue[16:]
        if not batch:
            return
        emb = batch[0][0]
        same = [(t, title) for e, t, title in batch if e is emb]
        try:
            emb.embed([t for t, _ in same], titles=[title for _, title in same])
        except EmbedError as e:
            failed(emb.config, str(e))
            with _warm_lock:
                _warm_queue.clear()
            return


def get(config: dict) -> Optional[Embedder]:
    """The configured embedder, or None (off, not installed, or recently failing — retried after a minute).
    Never raises: callers fall back to lexical search."""
    key = json.dumps(settings(config), sort_keys=True)
    if key in _failures and time.time() - _failures[key][1] < 60:
        return None
    try:
        if key not in _embedders:
            _embedders[key] = Embedder(config)
        return _embedders[key]
    except EmbedError as e:
        _failures[key] = (str(e), time.time())
        return None


def failed(config: dict, error: str) -> None:
    """A call failed at run time: skip embeddings for a minute instead of retrying on every search."""
    key = json.dumps(settings(config), sort_keys=True)
    _embedders.pop(key, None)
    _failures[key] = (error, time.time())


def last_error(config: dict) -> str:
    hit = _failures.get(json.dumps(settings(config), sort_keys=True))
    return hit[0] if hit else ""


def status(config: dict) -> dict:
    s = settings(config)
    b = backend(config)
    return {"backend": s["backend"], "active": b, "installed": installed(config), "model": MODEL_NAME,
            "quant": s["quant"], "dim": s["dim"], "url": s["url"], "running": LocalServer.running(),
            "server": server_binary() or "", "model_file": str(model_path(s["quant"])),
            "error": last_error(config)}


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # vectors are normalized


__all__ = ["Embedder", "EmbedError", "LocalServer", "get", "status", "settings", "backend", "installed", "cosine",
           "DIMS", "MODEL_REPO", "MODEL_NAME"]
