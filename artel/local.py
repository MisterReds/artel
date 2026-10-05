"""Small local models (Gemma, Qwen…) served by llama.cpp on this machine.

Uses:
- a provider for any role (``provider_by_role: {librarian: local}``) via the
  OpenAI-compatible llama.cpp server;
- a fast yes/no gatekeeper in front of expensive coordination roles
  (``local_gate: {skill_review: true, refine: true}``);
- fully offline operation (``provider: local``).

Every local call is best-effort: if the server is down, gates fall back to "yes"
(run the expensive role) so quality never silently degrades.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

from .project import artel_home

MODELS = {
    "gemma-3-1b": ("unsloth/gemma-3-1b-it-GGUF", "gemma-3-1b-it-Q4_K_M.gguf"),
    "qwen2.5-1.5b": ("Qwen/Qwen2.5-1.5B-Instruct-GGUF", "qwen2.5-1.5b-instruct-q4_k_m.gguf"),
    "qwen2.5-3b": ("Qwen/Qwen2.5-3B-Instruct-GGUF", "qwen2.5-3b-instruct-q4_k_m.gguf"),
}
DEFAULT_PORT = 8090


def models_dir() -> Path:
    d = Path(os.environ.get("ARTEL_MODELS", artel_home() / "models"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def model_path(name: str) -> Path:
    return models_dir() / MODELS[name][1]


def pull(name: str) -> Path:
    repo, fname = MODELS[name]
    dest = model_path(name)
    if dest.exists() and dest.stat().st_size > 10_000_000:
        return dest
    tmp = dest.with_suffix(".part")
    with httpx.stream("GET", f"https://huggingface.co/{repo}/resolve/main/{fname}", follow_redirects=True,
                      timeout=None) as r:
        r.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in r.iter_bytes(1 << 20):
                fh.write(chunk)
    tmp.replace(dest)
    return dest


def serve(name: str, port: int = DEFAULT_PORT, ctx: int = 8192, threads: Optional[int] = None,
          background: bool = True) -> subprocess.Popen | None:
    path = model_path(name) if name in MODELS else Path(name)
    if not path.exists():
        path = pull(name)
    cmd = [sys.executable, "-m", "llama_cpp.server", "--model", str(path), "--host", "127.0.0.1", "--port",
           str(port), "--n_ctx", str(ctx), "--n_threads", str(threads or os.cpu_count() or 4)]
    if name.startswith("qwen"):
        cmd += ["--chat_format", "chatml-function-calling"]
    log = (models_dir() / f"server-{port}.log").open("a")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    if not background:
        proc.wait()
        return None
    for _ in range(240):
        if healthy(port):
            return proc
        if proc.poll() is not None:
            raise RuntimeError(f"llama.cpp server exited; see {log.name}")
        time.sleep(0.5)
    raise RuntimeError("llama.cpp server did not become ready in 120s")


def healthy(port: int = DEFAULT_PORT) -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{port}/v1/models", timeout=1.0).status_code == 200
    except httpx.HTTPError:
        return False


def ask(prompt: str, system: str = "", max_tokens: int = 64, port: int = DEFAULT_PORT,
        timeout: float = 30.0) -> Optional[str]:
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    try:
        r = httpx.post(f"http://127.0.0.1:{port}/v1/chat/completions", timeout=timeout,
                       json={"messages": msgs, "max_tokens": max_tokens, "temperature": 0})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"] or ""
    except (httpx.HTTPError, KeyError, ValueError):
        return None


GATE_SYSTEM = ("You are a strict triage filter. Answer with exactly one word: YES or NO. Answer NO only when you "
               "are confident; when unsure answer YES.")


def gate(question: str, context: str, port: int = DEFAULT_PORT) -> tuple[bool, str]:
    """Fast local yes/no. Returns (decision, raw). Unavailable or unclear -> (True, reason)."""
    t0 = time.time()
    raw = ask(f"{question}\n\n<context>\n{context[-6000:]}\n</context>\n\nAnswer YES or NO.", GATE_SYSTEM,
              max_tokens=4, port=port)
    if raw is None:
        return True, "local model unavailable"
    word = raw.strip().upper()
    decision = not word.startswith("NO")
    return decision, f"{word[:10]} in {time.time() - t0:.2f}s"


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="artel local")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pull")
    p.add_argument("name", choices=sorted(MODELS))
    s = sub.add_parser("serve")
    s.add_argument("name")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--ctx", type=int, default=8192)
    a = sub.add_parser("ask")
    a.add_argument("prompt")
    a.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub.add_parser("list")
    args = ap.parse_args(argv)
    if args.cmd == "pull":
        print(pull(args.name))
    elif args.cmd == "serve":
        serve(args.name, args.port, args.ctx, background=False)
    elif args.cmd == "ask":
        t0 = time.time()
        print(ask(args.prompt, max_tokens=256, port=args.port), f"\n[{time.time() - t0:.2f}s]")
    else:
        for n, (repo, f) in MODELS.items():
            path = model_path(n)
            print(f"{n:<14} {'downloaded' if path.exists() else '-':<11} {repo}/{f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["pull", "serve", "ask", "gate", "healthy", "MODELS", "json"]
