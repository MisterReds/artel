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
