"""The «без изоляции» notice: a compact strip that can be folded for the session, never lost.

The page script is a single inline block, so the notice logic is cut out of index.html and run in node with a tiny DOM.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parents[1] / "artel" / "web" / "index.html").read_text(encoding="utf-8")
node = pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")


def run(steps: str, storage_throws: bool = False):
    start = HTML.index("let isoReason=''")
    end = HTML.index("async function pollIso")
    code = HTML[start:end]
    storage = (
        "{getItem(){throw new Error('blocked')},setItem(){throw new Error('blocked')}}"
        if storage_throws
        else "{getItem:k=>store[k]??null,setItem:(k,v)=>{store[k]=v}}"
    )
    src = f"""
const els={{}};
const mk=id=>els[id]=els[id]||{{id,hidden:true,innerHTML:'',onclick:null,setAttribute(){{}}}};
const $=s=>mk(s.slice(1));['unisolated','isochip','iso-more','iso-x','iso-det'].forEach(mk);
const icon=()=>'', esc=s=>String(s);
const store={{}};
global.sessionStorage={storage};
{code}
$('#isochip').onclick=()=>isoHide(false);
const out=[];const snap=()=>out.push([!els.unisolated.hidden,!els.isochip.hidden]);
{steps}
console.log(JSON.stringify(out));
"""
    r = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@node
def test_strip_shows_then_folds_to_a_chip_and_comes_back():
    out = run("isoReason='no bwrap';isoDraw();isoSync();snap();"
              "els['iso-x'].onclick();snap();els.isochip.onclick();snap();")
    assert out == [[True, False], [False, True], [True, False]]  # strip → chip → strip, never both, never neither


@node
def test_nothing_is_shown_when_the_sandbox_works():
    assert run("isoReason='';isoDraw();isoSync();snap();") == [[False, False]]


@node
def test_blocked_session_storage_still_folds_the_strip():
    out = run("isoReason='x';isoDraw();isoSync();snap();els['iso-x'].onclick();snap();", storage_throws=True)
    assert out == [[True, False], [False, True]]


def test_the_strip_keeps_the_honest_text_and_a_disclosure():
    assert 'id="unisolated"' in HTML and "без изоляции" in HTML
    assert "не от намеренно враждебного исполнителя" in HTML and 'aria-controls="iso-det"' in HTML
