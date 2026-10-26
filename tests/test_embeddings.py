"""Embeddings and hybrid retrieval: EmbeddingGemma prefixes, cache, Matryoshka truncation, the per-call budget with
background warming, the lexical fallback when no model answers, and the places that use it (skills, `search`)."""
import json
import math
import threading
import time
import zlib
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from artel import embeddings
from artel.project import DEFAULT_CONFIG, deep_merge
from artel.retrieval import bm25, chunks, rank, search_files

TOPICS = {"limit": ("limit", "limits", "лимит", "quota", "квота"), "food": ("borscht", "борщ", "soup", "суп")}


def _vec(text: str, dim: int = 768) -> list:
    """A toy multilingual embedding: one axis per topic (any language), plus hashed words for the rest."""
    t = text.lower().split("|")[-1]
    v = [0.0] * dim
    for i, words in enumerate(TOPICS.values()):
        v[i] = 5.0 * sum(w in t for w in words)
    for w in t.split():
        v[10 + zlib.crc32(w.encode()) % (dim - 10)] += 0.2
    return v


class Fake:
    def __init__(self):
        self.calls: list = []
        handler = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                handler.calls.append(body["input"])
                out = json.dumps({"data": [{"index": i, "embedding": _vec(t)} for i, t in enumerate(body["input"])]})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(out.encode())

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    embeddings._embedders.clear()
    embeddings._failures.clear()
    f = Fake()
    yield f, deep_merge(DEFAULT_CONFIG, {"embeddings": {"backend": "http", "url": f.url, "model": "embeddinggemma-2",
                                                        "dim": 256}})
    f.srv.shutdown()


DOCS = ["When Claude hits its usage limit the work moves to Codex.", "Рецепт: борщ со сметаной.",
        "The dashboard shows tasks on a kanban board."]


def test_meaning_beats_missing_words_and_vectors_are_cached(fake):
    f, cfg = fake
    r = rank("что делать, когда кончилась квота", DOCS, cfg)  # no shared word with the right document
    assert r[0][0] == 0 and r[0][2] == "hybrid"
    assert bm25("что делать, когда кончилась квота", DOCS) == [0.0, 0.0, 0.0]
    sent = [t for call in f.calls for t in call]
    assert "task: search result | query: что делать, когда кончилась квота" in sent  # EmbeddingGemma prefixes
    assert any(t.startswith("title: none | text: When Claude") for t in sent)
    n = sum(len(c) for c in f.calls)
    rank("квота", DOCS, cfg)
    assert sum(len(c) for c in f.calls) == n + 1  # documents come from the cache, only the new query is sent
    v = embeddings.get(cfg).embed(["x limit"])[0]
    assert len(v) == 256 and abs(math.sqrt(sum(x * x for x in v)) - 1) < 1e-6  # Matryoshka cut, renormalized


def test_without_a_model_retrieval_is_lexical_and_never_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    embeddings._embedders.clear()
    embeddings._failures.clear()
    assert embeddings.get(DEFAULT_CONFIG) is None and embeddings.backend(DEFAULT_CONFIG) == "off"
    assert rank("kanban board", DOCS, DEFAULT_CONFIG)[0] == (2, pytest.approx(rank("kanban board", DOCS)[0][1]), "lexical")
    down = deep_merge(DEFAULT_CONFIG, {"embeddings": {"backend": "http", "url": "http://127.0.0.1:9"}})
    r = rank("kanban board", DOCS, down)
    assert r[0][0] == 2 and r[0][2] == "lexical" and "не ответил" in embeddings.last_error(down)
    t = time.time()
    rank("kanban board", DOCS, down)  # a failing server is skipped for a while, not retried on every search
    assert time.time() - t < 0.5


def test_budget_embeds_the_best_lexical_matches_now_and_the_rest_in_the_background(fake):
    f, cfg = fake
    docs = [f"note {i} about gardening" for i in range(30)] + ["the provider limit handler"]
    r = rank("provider limit", docs, cfg, budget=4)
    assert r[0][0] == 30 and r[0][2] == "hybrid"
    first = [t for t in f.calls[0] + f.calls[1] if t.startswith("title")]
    assert len(first) == 4 and any("the provider limit handler" in t for t in first)
    deadline = time.time() + 10
    emb = embeddings.get(cfg)
    while not all(emb.cached(docs)) and time.time() < deadline:
        time.sleep(0.05)
    assert all(emb.cached(docs))


def test_skills_index_is_cut_to_what_the_goal_needs(fake, tmp_path):
    from artel.skills import SkillStore

    _, cfg = fake
    store = SkillStore(tmp_path / "skills")
    for i in range(12):
        store.create(f"gardening-{i}", f"---\nname: gardening-{i}\ndescription: Grow plants, variant {i}.\n---\nbody\n")
    store.create("quota-failover", "---\nname: quota-failover\ndescription: Switch providers when a limit hits.\n---\nb\n")
    store.create("house-style", "---\nname: house-style\ndescription: Formatting rules.\n---\nb\n")
    store.pin("house-style")
    idx = store.index(limit=9, query="у провайдера кончилась квота", config=cfg)
    lines = idx.splitlines()
    assert lines[0].startswith("- house-style") and lines[1].startswith("- quota-failover") and len(lines) <= 9
    assert store.index(limit=120).count("\n") == 13  # a small library is listed whole, as before


def test_search_tool_finds_code_by_meaning(fake, tmp_path):
    from artel.manager import new_kernel, run_goal
    from artel.tooling import ToolContext

    _, cfg = fake
    cfg = deep_merge(cfg, {"provider": "scripted"})
    project, _ = run_goal("search", cfg)
    (project.shared / "app").mkdir(parents=True, exist_ok=True)
    (project.shared / "app" / "quota.py").write_text("def on_limit(err):\n    return switch_runtime()\n" * 3)
    (project.shared / "app" / "soup.py").write_text("def cook():\n    return 'борщ'\n")
    k = new_kernel(cfg)
    ctx = ToolContext(k, project, role="worker", workspace=project.shared, task={"id": "t-1"})
    out = k.tools["search"].fn(ctx, query="what happens when the quota runs out", path="shared/app")
    assert out.splitlines()[0].endswith("by meaning and keywords:") and "shared/app/quota.py:1-" in out.splitlines()[2]
    assert any(e["kind"] == "search/query" for e in project.log.read())
    assert "search" in [t.name for t in k.tools_for("worker")]


def test_chunks_keep_line_ranges_and_skip_junk(tmp_path):
    (tmp_path / "a.py").write_text("\n".join(f"line {i} " + "x" * 60 for i in range(100)))
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret")
    (tmp_path / "b.bin").write_bytes(b"\x00\x01")
    cs = chunks(tmp_path, size=1000, overlap=100)
    assert {c["path"] for c in cs} == {"a.py"} and cs[0]["start"] == 1 and cs[-1]["end"] == 100
    assert all(cs[i]["start"] <= cs[i - 1]["end"] for i in range(1, len(cs)))  # overlapping windows
    assert search_files(tmp_path, "line 42")[0]["start"] <= 43


@pytest.fixture
def local_server(tmp_path, monkeypatch):
    """A 'installed' local model: the fake llama-server in ARTEL_HOME/tools/llama and an empty GGUF file."""
    import shutil
    import sys

    if sys.platform == "win32":
        pytest.skip("the fake llama-server is a shebang script")
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    llama = tmp_path / "home" / "tools" / "llama"
    llama.mkdir(parents=True)
    shutil.copy(Path(__file__).parent / "fakes" / "llama-server", llama / "llama-server")
    (llama / "llama-server").chmod(0o755)
    (tmp_path / "home" / "tools" / "models").mkdir(parents=True)
    embeddings.model_path("Q8_0").write_bytes(b"gguf")
    embeddings.reset()
    embeddings.LocalServer.stop()
    yield deep_merge(DEFAULT_CONFIG, {"embeddings": {"backend": "auto"}})
    embeddings.LocalServer.stop()
    embeddings.reset()


def test_local_server_survives_concurrent_starts_and_stops(local_server):
    """Dashboard checks, background warming and an uninstall touch the one server from different threads; none of
    them may crash another (AttributeError: 'NoneType' object has no attribute 'poll')."""
    import threading

    errors, urls = [], []

    def start():
        try:
            urls.append(embeddings.LocalServer.url(local_server))
        except embeddings.EmbedError:
            pass  # stopped underneath: a clean, catchable failure is fine
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=start) for _ in range(4)] + [threading.Thread(target=embeddings.LocalServer.stop)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    assert embeddings.installed(local_server) and embeddings.backend(local_server) == "local"
    assert rank("kanban board", DOCS, local_server)[0][2] == "hybrid"


def test_a_server_that_dies_at_start_is_reported_and_search_falls_back(local_server, monkeypatch):
    monkeypatch.setenv("FAKE_LLAMA_CRASH", "1")
    r = rank("kanban board", DOCS, local_server)
    assert r[0] == (2, pytest.approx(r[0][1]), "lexical")
    err = embeddings.last_error(local_server)
    assert "завершился" in err and "gemma-embedding2" in err  # the server's own last words reach the dashboard


def test_any_embedding_failure_never_breaks_a_search(fake, monkeypatch):
    _, cfg = fake

    def boom(*a, **kw):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(embeddings.Embedder, "embed", boom)
    assert rank("kanban board", DOCS, cfg)[0][2] == "lexical"


def test_a_system_http_proxy_never_hides_the_local_server(local_server, monkeypatch):
    """Offices and schools set http_proxy; requests to 127.0.0.1 must still reach the local llama-server."""
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")  # a proxy that does not exist
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    assert rank("kanban board", DOCS, local_server)[0][2] == "hybrid"


def test_warm_restarts_the_loop_when_work_arrives_between_pop_and_exit(monkeypatch):
    """Lost-wakeup window: the loop saw an empty queue; warm() appends before the loop has finished exiting."""
    done = threading.Event()
    in_window = threading.Event()
    release = threading.Event()
    real_take = embeddings._warm_take
    seen = []

    class E:
        config: dict = {}

        def embed(self, texts, titles=None):
            seen.extend(texts)
            if "second" in texts:
                done.set()

    def slow_take():
        batch = real_take()
        if not batch and not in_window.is_set():
            in_window.set()      # queue was empty and the flag is already cleared, but the thread is still alive
            release.wait(5)
        return batch

    monkeypatch.setattr(embeddings, "_warm_take", slow_take)
    monkeypatch.setattr(embeddings, "_warm_queue", [])
    monkeypatch.setattr(embeddings, "_warm_running", False)
    emb = E()
    embeddings.Embedder.warm(emb, ["first"])
    assert in_window.wait(5)
    embeddings.Embedder.warm(emb, ["second"])   # lands in the window
    release.set()
    assert done.wait(5), "queued text was never embedded (lost wakeup)"
    assert seen == ["first", "second"]
