#!/usr/bin/env python3
"""Generates the four Artel logo proposals (pure SVG paths, no fonts, no raster).

    python3 docs/brand/proposals/build.py

Each direction gets: mark(.svg/-dark.svg), lockup(-dark), tile(-dark) and favicon(-dark).
Mark grid is 64x64; the wordmark is hand-drawn from stroked centre lines (see GLYPHS), cap height 32 units.
"""
import math
from pathlib import Path

OUT = Path(__file__).resolve().parent
LIGHT = dict(ink="#1E1A16", acc="#B5522B", bg="#F7F4EE")
DARK = dict(ink="#EFE8DD", acc="#E07B4E", bg="#15120F")
TILE_L = dict(ink="#F7F4EE", acc="#1E1A16", bg="#B5522B")
TILE_D = dict(ink="#EFE8DD", acc="#E07B4E", bg="#2A241E")


def f(n):
    return f"{n:.2f}".rstrip("0").rstrip(".")


def line(x1, y1, x2, y2, w, c, cap="round"):
    return (f'<path d="M{f(x1)} {f(y1)}L{f(x2)} {f(y2)}" fill="none" stroke="{c}" stroke-width="{f(w)}" '
            f'stroke-linecap="{cap}"/>')


def poly(pts, w, c, join="round", cap="round", close=False):
    d = "M" + "L".join(f"{f(x)} {f(y)}" for x, y in pts) + ("Z" if close else "")
    return (f'<path d="{d}" fill="none" stroke="{c}" stroke-width="{f(w)}" stroke-linejoin="{join}" '
            f'stroke-linecap="{cap}"/>')


def fill(pts, c, stroke=None, sw=0):
    d = "M" + "L".join(f"{f(x)} {f(y)}" for x, y in pts) + "Z"
    s = f' stroke="{stroke}" stroke-width="{f(sw)}" stroke-linejoin="round"' if stroke else ""
    return f'<path d="{d}" fill="{c}"{s}/>'


# ---------------------------------------------------------------- marks (64 grid)
def mark_a(p, small=False):
    """Two figures leaning on each other: the A is made by two legs that rest on one another."""
    w, bar = (11, 7) if small else (9, 5.4)
    y = 42 if small else 40
    out = [line(30.5, 10, 10, 56, w, p["ink"])]
    # the second leg overlaps the first with a paper-coloured seam: they lean, they are not welded
    out.append(line(33.5, 10, 54, 56, w + 4, p["bg"]))
    out.append(line(33.5, 10, 54, 56, w, p["acc"]))
    # the left figure holds out an arm; the right one rests on it (seam), so they carry each other
    out.append(line(17.5, y, 47, y, bar, p["ink"], "butt"))
    out.append(line(33.5, 10, 54, 56, w + 4, p["bg"]) if False else "")
    out.append(_leg_over(p, w))
    return "".join(out)


def _leg_over(p, w):
    """The copper leg drawn again on top of the crossbar, with a paper seam around it."""
    return line(33.5, 10, 54, 56, w + 4, p["bg"]) + line(33.5, 10, 54, 56, w, p["acc"])


def mark_b(p, small=False):
    """A timber frame: mitred legs and a dovetail key as the crossbar."""
    w = 12 if small else 10
    legs = poly([(9, 60), (32, 18), (55, 60)], w, p["ink"], join="miter", cap="butt")
    legs = f'<clipPath id="cb"><rect x="0" y="0" width="64" height="58"/></clipPath><g clip-path="url(#cb)">{legs}</g>'
    if small:
        return legs + line(12, 42, 52, 42, 7, p["acc"], "butt")
    key = [(13.5, 35.5), (26.5, 38.4), (37.5, 38.4), (50.5, 35.5), (50.5, 46.5), (37.5, 43.6), (26.5, 43.6), (13.5, 46.5)]
    return legs + fill(key, p["acc"], p["bg"], 1.8)


def mark_c(p, small=False):
    """The A whose crossbar is a check mark that breaks out of the frame: «доводит до результата»."""
    w, cw = (10, 9.5) if small else (8, 7)
    legs = poly([(10, 57), (32, 12), (54, 57)], w, p["ink"], join="round")
    chk = [(17.5, 38), (27.5, 48), (51, 25)]
    return (legs + poly(chk, cw + 4, p["bg"], cap="round") + poly(chk, cw, p["acc"]))


def mark_d(p, small=False, tile=False):
    """A guild seal: perforated stamp edge, copper disc, the A cut out of it."""
    c = p["acc"] if not tile else p["ink"]
    out = []
    if not small:
        out.append(f'<circle cx="32" cy="32" r="30.2" fill="none" stroke="{c}" stroke-width="2.6" '
                   f'stroke-linecap="round" stroke-dasharray="0 6.38"/>')
    r = 26.5 if not small else 29
    if not tile:
        out.append(f'<circle cx="32" cy="32" r="{r}" fill="{c}"/>')
        a = p["bg"]
    else:
        out.append(f'<circle cx="32" cy="32" r="{r}" fill="none" stroke="{c}" stroke-width="2.2"/>')
        a = c
    if not small:
        out.append(f'<circle cx="32" cy="32" r="22.4" fill="none" stroke="{a}" stroke-width="1.3" opacity=".9"/>')
    wl = 6.6 if not small else 8
    ya = 47 if not small else 48
    out.append(poly([(19.5, ya), (32, 16), (44.5, ya)], wl, a, join="miter", cap="butt")
               if False else poly([(19.5, ya), (32, 17), (44.5, ya)], wl, a, join="round", cap="round"))
    out.append(line(24.5, 38 if not small else 40, 39.5, 38 if not small else 40, wl * 0.72, a, "butt"))
    return "".join(out)


MARKS = {"a": mark_a, "b": mark_b, "c": mark_c, "d": mark_d}


# ---------------------------------------------------------------- hand-drawn wordmark
# Centre lines on a baseline at y=0, going up (negative); cap height 32, x-height 22.
def glyph(ch, kind):
    G = {
        # lower-case set (width, [path d], accent-path index or None)
        "A": (30, ["M1 0L15 -32L29 0"], "M7 -10H23"),
        "r": (17, ["M4 0V-22", "M4 -13Q4 -22 15 -22"], None),
        "t": (15, ["M6 -30V-7Q6 0 14 0", "M0 -22H14"], None),
        "e": (25, ["M3 -11H21A9 11 0 1 0 18.5 -3.5"], None),
        "l": (9, ["M4 -32V0"], None),
        # capitals for the stamp-like set
        "R": (25, ["M4 0V-32H14A8.5 8.5 0 0 1 14 -15H4", "M13 -15L24 0"], None),
        "T": (24, ["M0 -32H24", "M12 -32V0"], None),
        "E": (22, ["M20 -32H4V0H20", "M4 -16H17"], None),
        "L": (21, ["M4 -32V0H19"], None),
    }
    return G[ch]


def wordmark(text, x, base, scale, w, color, acc, cap="round", join="round", tracking=5, special=None):
    """Return (svg, width). `special` customises the first letter A per direction."""
    out = []
    cur = x
    for ch in text:
        width, paths, extra = glyph(ch, None)
        g = []
        if ch == "A" and special:
            g = special(w, color, acc, cap, join)
        else:
            for d in paths:
                g.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{f(w)}" stroke-linecap="{cap}" '
                         f'stroke-linejoin="{join}"/>')
            if extra:
                g.append(f'<path d="{extra}" fill="none" stroke="{acc}" stroke-width="{f(w)}" stroke-linecap="butt"/>')
        out.append(f'<g transform="translate({f(cur)} {f(base)}) scale({f(scale)})">{"".join(g)}</g>')
        cur += (width + tracking) * scale
    return "".join(out), cur - x - tracking * scale


def a_special_two(w, color, acc, cap, join):
    """The wordmark A repeats the mark: ink left leg, copper right leg that rests on it."""
    return [f'<path d="M1 0L15.5 -32" stroke="{color}" stroke-width="{f(w)}" stroke-linecap="{cap}" fill="none"/>',
            f'<path d="M7 -10H22" stroke="{color}" stroke-width="{f(w*0.8)}" stroke-linecap="butt" fill="none"/>',
            f'<path d="M14.5 -32L29 0" stroke="{acc}" stroke-width="{f(w)}" stroke-linecap="{cap}" fill="none"/>']


def a_special_check(w, color, acc, cap, join):
    return [f'<path d="M1 0L15 -32L29 0" stroke="{color}" stroke-width="{f(w)}" stroke-linecap="{cap}" '
            f'stroke-linejoin="{join}" fill="none"/>',
            f'<path d="M7 -12L12.5 -6.5L25 -19" stroke="{acc}" stroke-width="{f(w*0.95)}" stroke-linecap="round" '
            f'stroke-linejoin="round" fill="none"/>']


def a_special_timber(w, color, acc, cap, join):
    return [f'<path d="M1 0L15 -32L29 0" stroke="{color}" stroke-width="{f(w)}" stroke-linecap="butt" '
            f'stroke-linejoin="miter" fill="none"/>',
            f'<path d="M6 -10H24" stroke="{acc}" stroke-width="{f(w*0.95)}" stroke-linecap="butt" fill="none"/>']


WORD = {
    "a": dict(text="Artel", w=6.3, cap="round", join="round", special=a_special_two),
    "b": dict(text="Artel", w=7.2, cap="butt", join="miter", special=a_special_timber, tracking=4),
    "c": dict(text="Artel", w=6.3, cap="round", join="round", special=a_special_check),
    "d": dict(text="ARTEL", w=5.6, cap="butt", join="miter", special=None, tracking=9),
}


def svg(w, h, body, extra=""):
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w*2}" height="{h*2}"{extra}>{body}</svg>\n'


def lockup(key, p):
    cfg = dict(WORD[key])
    text = cfg.pop("text")
    scale = 1.28
    x0 = 80
    base = 56
    word, wd = wordmark(text, x0, base, scale, color=p["ink"], acc=p["acc"], **cfg)
    return svg(round(x0 + wd + 6), 64, f'<g>{MARKS[key](p)}</g>{word}')


def tile(key, p, size=64):
    fn = MARKS[key]
    inner = fn(p, tile=True) if key == "d" else fn(p)
    return svg(64, 64, f'<rect width="64" height="64" rx="15" fill="{p["bg"]}"/><g transform="translate(6.4 6.4) scale(.8)">{inner}</g>')


def favicon(key, p):
    fn = MARKS[key]
    inner = fn(p, small=True, tile=True) if key == "d" else fn(p, small=True)
    return svg(64, 64, f'<rect width="64" height="64" rx="14" fill="{p["bg"]}"/><g transform="translate(2 2) scale(.94)">{inner}</g>')


DIRS = {"a": "a-leaning", "b": "b-dovetail", "c": "c-check", "d": "d-seal"}


def main():
    for k, name in DIRS.items():
        d = OUT / name
        d.mkdir(exist_ok=True)
        for suffix, p, tp in (("", LIGHT, TILE_L), ("-dark", DARK, TILE_D)):
            (d / f"mark{suffix}.svg").write_text(svg(64, 64, MARKS[k](p) if k != "d" else MARKS[k](p)), encoding="utf-8")
            (d / f"lockup{suffix}.svg").write_text(lockup(k, p), encoding="utf-8")
            (d / f"tile{suffix}.svg").write_text(tile(k, tp), encoding="utf-8")
            (d / f"favicon{suffix}.svg").write_text(favicon(k, tp), encoding="utf-8")


if __name__ == "__main__":
    main()
