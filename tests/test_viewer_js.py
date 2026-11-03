"""The file viewer's pure JS (highlighter, Markdown) run under node, and the real-browser isolation check."""
import glob
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from artel.httpd import LocalHTTPServer
from artel.project import DEFAULT_CONFIG, Project, deep_merge
from artel.web.server import Runner, make_handler

JS = Path(__file__).resolve().parents[1] / "artel/web/static/files.js"
node = pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")


def run_js(expr: str):
    src = f"global.window={{}};global.document={{}};require({json.dumps(str(JS))});const F=window.ArtelFiles;console.log(JSON.stringify({expr}))"
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@node
def test_highlighter_marks_tokens_and_keeps_lines_balanced():
    lines = run_js('F.hl("def f(x):\\n    \\"\\"\\"doc\\nmore\\"\\"\\"  # c\\n    return 1", "py")')
    assert len(lines) == 4
    assert '<span class="t-k">def</span>' in lines[0] and '<span class="t-f">f</span>' in lines[0]
    assert all(l.count("<span") == l.count("</span>") for l in lines)  # a docstring over two lines is closed on each
    assert '<span class="t-s">more&quot;&quot;&quot;</span>' in lines[2] and 't-c' in lines[2]


@node
def test_highlighter_escapes_markup_in_every_language():
    for lang in ("py", "js", "ts", "json", "css", "html", "sh", "md", "unknown"):
        html = "".join(run_js(f'F.hl("<script>alert(1)</script> & \\"x\\"", {json.dumps(lang)})'))
        assert "<script>" not in html and "&lt;" in html


@node
def test_markdown_renders_blocks_and_refuses_script_links_and_raw_html():
    out = run_js(r'''F.md("# T\n\n<img src=x onerror=alert(1)>\n\n[bad](javascript:alert(1)) [ok](https://a.b/c)\n\n- a\n- b\n\n```py\nx = 1\n```", {dir:"", raw:p=>p})''')
    assert "<h1>T</h1>" in out and "<ul><li>a</li><li>b</li></ul>" in out and 'class="md-code"' in out
    assert "<img src=x" not in out and "&lt;img" in out
    assert "javascript:" not in out.replace("[bad]", "") and 'href="https://a.b/c"' in out and 'rel="noopener noreferrer"' in out


FAKE_DOM = r"""
class N{constructor(t){this.tag=t;this.kids=[];this.on={};this.attrs={};this.open=false;this._h='';this.className=''}
 append(...c){c.forEach(x=>this.kids.push(x))} setAttribute(k,v){this.attrs[k]=v}
 addEventListener(e,f){this.on[e]=f} set innerHTML(v){this._h=v} get innerHTML(){return this._h}}
global.document={createElement:t=>new N(t)};
const count=n=>typeof n==='string'?0:1+n.kids.reduce((a,k)=>a+count(k),0);
const find=(n,f)=>typeof n==='string'?[]:[...(f(n)?[n]:[]),...n.kids.flatMap(k=>find(k,f))];
"""


def run_dom(body: str):
    src = f"global.window={{}};{FAKE_DOM}require({json.dumps(str(JS))});const F=window.ArtelFiles;{body}"
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@node
def test_language_lookup_ignores_object_prototype_names():
    assert run_js('F.hl("a", "constructor")') == ["a"] and run_js('F.hl("a", "__proto__")') == ["a"]
    assert run_js('F.hl("a", "toString")') == ["a"]


@node
def test_markdown_survives_a_malformed_percent_escape_in_an_image():
    out = run_js(r'''F.md("![x](a%zz.png) ok", {dir:"", raw:p=>p})''')
    assert "ok" in out and "<img" not in out


@node
def test_markdown_links_are_only_http_mailto_and_anchor_and_external_ones_open_safely():
    out = run_js(r'''F.md("[a](https://x.y) [m](mailto:a@b.c) [h](#top) [f](file:///etc/passwd) [r](other.md) [j](JaVaScript:alert(1)) [d](data:text/html,x)", {dir:"", raw:p=>p})''')
    assert out.count("<a ") == 3 and 'href="#top"' in out and 'href="mailto:a@b.c"' in out
    assert 'href="https://x.y" target="_blank" rel="noopener noreferrer"' in out
    for bad in ("file:", "other.md", "alert", "data:"):
        assert f'href="{bad}' not in out and "href=\"" + bad not in out
    assert ">f<" not in out and "f " in out  # the label stays as plain text


@node
def test_json_tree_builds_children_only_when_a_node_is_opened():
    r = run_dom(r'''const deep={a:{b:{c:{d:[1,2,3]}}}};const t=F.jsonNode(deep);
      const before=count(t);const lazy=find(t,n=>n.tag==="details"&&!n.open);
      const hasKids=lazy.map(n=>n.kids[1].kids.length);
      lazy[0].open=true;lazy[0].on.toggle();
      console.log(JSON.stringify({before,hasKids,after:count(t)}))''')
    assert r["hasKids"][0] == 0 and r["after"] > r["before"]


@node
def test_json_tree_caps_nodes_and_depth_and_offers_text():
    r = run_dom(r'''const big=Array.from({length:300},()=>Array.from({length:300},(_,i)=>i));
      let called=0;const t=F.jsonNode({x:{y:big}},()=>called++);
      const open=n=>{if(typeof n==="string")return;if(n.tag==="details"){n.open=true;n.on.toggle&&n.on.toggle()}n.kids.forEach(open)};
      open(t);open(t);open(t);
      const btn=find(t,n=>n.tag==="button");btn[0].onclick();
      let deep=1;for(let i=0;i<60;i++)deep={k:deep};
      const t2=F.jsonNode(deep);for(let i=0;i<40;i++)open(t2);
      console.log(JSON.stringify({nodes:count(t),buttons:btn.length,called,deepNodes:count(t2),
        deepNote:find(t2,n=>n.tag==="div"&&n.className==="fv-note").length}))''')
    assert 1000 < r["nodes"] < 20000 and r["buttons"] == 1 and r["called"] == 1
    assert r["deepNodes"] < 200 and r["deepNote"] == 1


@node
def test_size_labels_are_in_kilobytes():
    assert run_js("[F.fmtSize(12), F.fmtSize(2048), F.fmtSize(5*1024*1024)]") == ["12 Б", "2,0 КБ", "5,0 МБ"]


# --- a script in a previewed page cannot reach the dashboard ---------------------------------------------
def chromium():
    pw = pytest.importorskip("playwright.sync_api")
    ctx = pw.sync_playwright().start()
    for exe in [None, *glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")]:
        try:
            return ctx, ctx.chromium.launch(**({"executable_path": exe} if exe else {}))
        except Exception:  # noqa: BLE001 - try the next browser
            continue
    ctx.stop()
    pytest.skip("no chromium available")


def test_previewed_html_cannot_change_the_dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted"})
    project = Project.create("goal", cfg, home=tmp_path / "home")
    (project.shared / "evil.html").write_text(
        "<p id=r>pending</p><script>var r;try{parent.document.title='x';r='REACHED'}catch(e){r='blocked:'+e.name}"
        "document.getElementById('r').textContent=r;try{r=String(localStorage.length)}catch(e){}</script>")
    httpd = LocalHTTPServer(("127.0.0.1", 0), make_handler(Runner(cfg)))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    pw, br = chromium()
    try:
        page = br.new_page()
        page.goto(f"http://127.0.0.1:{httpd.server_address[1]}/#/p/{project.slug}")
        page.wait_for_selector("#tab-files")
        before = page.title()
        page.click("#tab-files")
        page.click('.fv-row[data-p="evil.html"]')
        frame = page.wait_for_selector("iframe.fv-frame").content_frame()
        frame.wait_for_function("document.getElementById('r').textContent.startsWith('blocked')", timeout=10000)
        assert frame.evaluate("document.getElementById('r').textContent").startswith("blocked:")
        assert page.title() == before != "x"
        assert page.get_attribute("iframe.fv-frame", "sandbox") == "allow-scripts"
    finally:
        br.close()
        pw.stop()
        httpd.shutdown()
        httpd.server_close()


def test_remote_mode_preview_loads_relative_css_js_and_images(tmp_path, monkeypatch):
    """Through a real login session: the sandboxed (opaque-origin) preview frame must still fetch its sibling files."""
    from artel.web.auth import Auth

    monkeypatch.setenv("ARTEL_HOME", str(tmp_path / "home"))
    cfg = deep_merge(DEFAULT_CONFIG, {"provider": "scripted"})
    project = Project.create("goal", cfg, home=tmp_path / "home")
    site = project.shared / "site"
    site.mkdir()
    (site / "index.html").write_text('<link rel=stylesheet href="s.css"><script src="a.js"></script><img id=i src="p.png">'
                                     '<p id=r>pending</p>')
    (site / "s.css").write_text("#r{color:rgb(1, 2, 3)}")
    (site / "a.js").write_text("window.onload=()=>{document.getElementById('r').textContent='js-ran'}")
    import base64
    (site / "p.png").write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="))
    auth = Auth(home=tmp_path / "home")
    token = auth.rotate()
    httpd = LocalHTTPServer(("127.0.0.1", 0), make_handler(Runner(cfg), auth))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    pw, br = chromium()
    try:
        page = br.new_page()
        assert page.request.post(base + "/api/auth/login", data={"token": token}).ok
        page.goto(f"{base}/#/p/{project.slug}")
        page.wait_for_selector("#tab-files")
        page.click("#tab-files")
        page.click('.fv-row[data-p="site/index.html"]')
        frame = page.wait_for_selector("iframe.fv-frame").content_frame()
        frame.wait_for_selector("#r", state="attached", timeout=10000)  # the page itself loads
        frame.wait_for_function("document.getElementById('r').textContent==='js-ran'", timeout=10000)
        assert frame.evaluate("getComputedStyle(document.getElementById('r')).color") == "rgb(1, 2, 3)"
        assert frame.evaluate("document.getElementById('i').naturalWidth") == 1
    finally:
        br.close()
        pw.stop()
        httpd.shutdown()
        httpd.server_close()
