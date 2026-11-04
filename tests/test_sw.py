"""The service worker's fetch strategy, run under node with a fake network and cache."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

SW = Path(__file__).resolve().parents[1] / "artel/web/static/sw.js"
node = pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")

HARNESS = r"""
const fs = require('fs');
const [path, online] = [process.argv[1], process.argv[2] === '1'];
const store = new Map([['http://x/static/desk.js', 'OLD'], ['http://x/static/favicon.svg', 'OLD']]);
const handlers = {};
const cache = {match: async req => store.has(req.url) ? {text: store.get(req.url), ok: true} : undefined,
               put: async (req, r) => { store.set(typeof req === 'string' ? req : req.url, r.text); }};
const ctx = {
  self: {addEventListener: (t, f) => { handlers[t] = f; }, location: {origin: 'http://x'}, skipWaiting() {}, clients: {claim() {}}},
  caches: {open: async () => cache, match: async () => undefined, keys: async () => [], delete: async () => true},
  fetch: async req => { if (!online) throw new Error('offline'); return {ok: true, redirected: false, text: 'NEW', clone() { return this; }}; },
  URL, Promise, Set, console,
};
new Function(...Object.keys(ctx), fs.readFileSync(path, 'utf8'))(...Object.values(ctx));
(async () => {
  const out = {};
  for (const p of ['/static/desk.js', '/static/favicon.svg']) {
    let answer; const req = {url: 'http://x' + p, method: 'GET', mode: 'no-cors'};
    handlers.fetch({request: req, respondWith: pr => { answer = pr; }});
    try { out[p] = answer ? (await answer).text : null; } catch (e) { out[p] = 'ERR'; }
  }
  await new Promise(r => setTimeout(r, 20));
  out.stored = store.get('http://x/static/desk.js');
  console.log(JSON.stringify(out));
})();
"""


def run(online: bool) -> dict:
    out = subprocess.run(["node", "-e", HARNESS, str(SW), "1" if online else "0"], capture_output=True, text=True,
                         timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@node
def test_helper_script_is_network_first_so_an_update_never_runs_stale():
    got = run(online=True)
    assert got["/static/desk.js"] == "NEW"  # not the cached OLD
    assert got["/static/favicon.svg"] == "OLD"  # other shell assets stay cache-first
    assert got["stored"] == "NEW"  # and the cache is refreshed for offline use


@node
def test_helper_script_comes_from_cache_only_when_offline():
    got = run(online=False)
    assert got["/static/desk.js"] == "OLD" and got["/static/favicon.svg"] == "OLD"
