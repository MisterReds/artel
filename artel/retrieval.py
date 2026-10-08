"""Hybrid retrieval: lexical BM25 always, plus meaning (embeddings, artel/embeddings.py) when a model is available.

The two rankings are fused with reciprocal rank fusion, so exact names and identifiers still win where they match,
and a question asked in other words (or another language) still finds the right item. Without an embedding model
the result is plain BM25 — retrieval never fails because a model is missing.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path
from typing import Optional

TOKEN = re.compile(r"[\w]+", re.UNICODE)
RRF_K = 60
TEXT_SUFFIXES = {".py", ".md", ".txt", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".toml", ".html",
                 ".css", ".sh", ".ps1", ".go", ".rs", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb",
                 ".php", ".sql", ".csv", ".ini", ".cfg", ".rst", ".swift", ".scala", ".lua", ".r", ".vue", ".svelte"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build"}


def tokens(text: str) -> list[str]:
    out = []
    for t in TOKEN.findall(text.lower()):
        out.append(t)
        if "_" in t:  # snake_case identifiers also match their parts
            out.extend(p for p in t.split("_") if p)
    return out


def bm25(query: str, docs: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    q = set(tokens(query))
    if not q or not docs:
        return [0.0] * len(docs)
    toks = [tokens(d) for d in docs]
    avg = sum(len(t) for t in toks) / len(toks) or 1.0
    df = Counter(w for t in toks for w in set(t) if w in q)
    n = len(docs)
    scores = []
    for t in toks:
        tf = Counter(w for w in t if w in q)
        s = 0.0
        for w, f in tf.items():
            idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
            s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(t) / avg))
        scores.append(s)
    return scores


def _semantic(emb, query: str, docs: list[str], titles: Optional[list[str]], task: str, order_lex: list[int],
              budget: int) -> list[Optional[float]]:
    """Cosine scores for documents whose vectors are cached plus up to ``budget`` new ones (best lexical matches
    first); the rest are queued for background embedding and scored on a later search."""
    titles = titles or [""] * len(docs)
    have = emb.cached(docs, titles=titles)
    new = [i for i in order_lex if not have[i]][:budget]
    take = sorted({i for i, h in enumerate(have) if h} | set(new))
    qv = emb.embed([query], kind="query", task=task)[0]
    vecs = emb.embed([docs[i] for i in take], titles=[titles[i] for i in take]) if take else []
    sem: list[Optional[float]] = [None] * len(docs)
    for i, v in zip(take, vecs):
        sem[i] = emb_cos(qv, v)
    rest = [i for i in range(len(docs)) if sem[i] is None]
    if rest:
        emb.warm([docs[i] for i in rest], [titles[i] for i in rest])
    return sem


def emb_cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def rank(query: str, docs: list[str], config: Optional[dict] = None, task: str = "search",
         titles: Optional[list[str]] = None, top: Optional[int] = None,
         budget: int = 48) -> list[tuple[int, float, str]]:
    """Indices of ``docs`` by relevance: [(index, score, how)], how = 'hybrid' | 'lexical'.
    ``task`` picks the embedding instruction: search | code | qa | similarity. At most ``budget`` documents are
    embedded during the call; the rest are indexed in the background (see :func:`_semantic`)."""
    if not docs:
        return []
    lex = bm25(query, docs)
    order_lex = sorted(range(len(docs)), key=lambda i: -lex[i])
    sem = None
    if config is not None:
        from . import embeddings

        emb = embeddings.get(config)
        if emb is not None:
            try:
                sem = _semantic(emb, query, docs, titles, task, order_lex, budget)
            except Exception as e:  # noqa: BLE001 - any embedding failure falls back to keywords, never fails the search
                embeddings.failed(config, str(e) if isinstance(e, embeddings.EmbedError) else repr(e))
                sem = None
    if sem is None:
        ranked = [(i, lex[i], "lexical") for i in order_lex if lex[i] > 0]
    else:
        order_sem = sorted((i for i in range(len(docs)) if sem[i] is not None), key=lambda i: -sem[i])
        fused = Counter()
        for r, i in enumerate(order_sem):
            fused[i] += 1 / (RRF_K + r + 1)
        q = set(tokens(query))
        for r, i in enumerate(order_lex):
            if lex[i] > 0:  # a document sharing a word or two (e.g. UI strings in the query's language) is weak
                cover = len(q & set(tokens(docs[i]))) / len(q)  # evidence; one matching most terms is strong
                fused[i] += min(1.0, 2 * cover) ** 2 / (RRF_K + r + 1)
        ranked = [(i, round(s, 6), "hybrid") for i, s in sorted(fused.items(), key=lambda x: -x[1])]
    return ranked[:top] if top else ranked


# --- files ------------------------------------------------------------------------------------------------------------

def chunks(root: Path, max_files: int = 2000, max_bytes: int = 400_000, size: int = 1600,
           overlap: int = 200) -> list[dict]:
    """Text files under ``root`` cut into overlapping chunks with line ranges (for code and document search)."""
    out: list[dict] = []
    files = 0
    for p in sorted(root.rglob("*")):
        if files >= max_files:
            break
        if not p.is_file() or any(part in SKIP_DIRS or part.startswith(".") for part in p.relative_to(root).parts):
            continue
        if p.suffix.lower() not in TEXT_SUFFIXES and p.suffix:
            continue
        try:
            if p.stat().st_size > max_bytes:
                continue
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        files += 1
        rel = p.relative_to(root).as_posix()
        lines = text.splitlines()
        start, buf, n = 0, [], 0
        for i, line in enumerate(lines):
            buf.append(line)
            n += len(line) + 1
            if n >= size:
                out.append({"path": rel, "start": start + 1, "end": i + 1, "text": "\n".join(buf)})
                keep, m = [], 0
                for ln in reversed(buf):  # overlap: carry the tail into the next chunk
                    m += len(ln) + 1
                    if m > overlap:
                        break
                    keep.insert(0, ln)
                start, buf, n = i + 1 - len(keep), keep, sum(len(x) + 1 for x in keep)
        if buf and (not out or out[-1]["path"] != rel or out[-1]["end"] < len(lines)):
            out.append({"path": rel, "start": start + 1, "end": len(lines), "text": "\n".join(buf)})
    return out


def search_files(root: Path, query: str, config: Optional[dict] = None, top: int = 8, task: str = "code") -> list[dict]:
    cs = chunks(root)
    hits = rank(query, [c["text"] for c in cs], config, task=task, titles=[c["path"] for c in cs], top=top)
    return [{**cs[i], "score": s, "how": how} for i, s, how in hits]


__all__ = ["bm25", "rank", "chunks", "search_files", "tokens"]
