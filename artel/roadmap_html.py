"""docs/roadmap.html — the roadmap as a page: which harnesses matter and why, what Artel took from each, what is
planned, where every item comes from. Generated from roadmap.yaml + harnesses.yaml by `artel roadmap html`."""

from __future__ import annotations

import html
from collections import Counter, defaultdict
from pathlib import Path

from .roadmap import SIGNIFICANCE, STATUS_MARK, STUDY, TIERS, Roadmap, load_harnesses, significance

E = html.escape
FEATURE = {"done": ("есть", "ok"), "partial": ("частично", "warn"), "planned": ("в плане", "run"), "gap": ("пробел", "bad"), "skip": ("не берём", "todo")}
ITEM = {"done": "ok", "partial": "warn", "in_progress": "run", "planned": "todo"}
CLASSES = {"coding-agent": "Coding-агент", "personal-agent": "Личный агент", "multi-agent": "Мультиагентный",
           "framework": "Фреймворк", "methodology": "Методика / скиллы", "memory": "Память",
           "self-improvement": "Самоулучшение", "tool": "Инструмент для агентов"}
OPEN = {"open": "открытый код", "restricted": "ограниченная лицензия", "closed": "код закрыт"}
PART_COLORS = {"adoption": "var(--c1)", "community": "var(--c2)", "curation": "var(--c3)", "activity": "var(--c4)",
               "origin": "var(--brand)"}

CSS = """
:root{--brand:#B5522B;--bg:#F7F4EE;--panel:#FFFDF9;--ink:#1E1A16;--muted:#6E665C;--line:#E4DDD1;
--ok:#3F7D4E;--warn:#B07A1F;--run:#1F7A78;--bad:#A8432E;--todo:#8A8378;
--c1:#3D6E8F;--c2:#7FA3BC;--c3:#8C6BB1;--c4:#5E9E6E;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--brand:#E07B4E;--bg:#15120F;--panel:#1E1A16;
--ink:#EFE8DD;--muted:#A39A8D;--line:#332D27;--ok:#7BBF8B;--warn:#D9A84A;--run:#5CC2BF;--bad:#E3876F;--todo:#A39A8D;
--c1:#7FB0D3;--c2:#4F7894;--c3:#B79BD8;--c4:#8CCB9B;color-scheme:dark}}
:root[data-theme="dark"]{--brand:#E07B4E;--bg:#15120F;--panel:#1E1A16;--ink:#EFE8DD;--muted:#A39A8D;--line:#332D27;
--ok:#7BBF8B;--warn:#D9A84A;--run:#5CC2BF;--bad:#E3876F;--todo:#A39A8D;--c1:#7FB0D3;--c2:#4F7894;--c3:#B79BD8;
--c4:#8CCB9B;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 Onest,system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--brand)}code{font:13px "JetBrains Mono",ui-monospace,monospace;overflow-wrap:anywhere}
header{border-bottom:3px solid var(--brand);background:var(--panel)}
.wrap{max-width:1180px;margin:0 auto;padding:0 16px}
header .wrap{display:flex;align-items:center;gap:14px;padding-top:18px;padding-bottom:16px;flex-wrap:wrap}
h1,h2,h3{font-family:Unbounded,Onest,system-ui,sans-serif;font-weight:600;letter-spacing:-.01em}
h1{font-size:22px;margin:0}h2{font-size:19px;margin:42px 0 10px}h3{font-size:15px;margin:0}
.sub{color:var(--muted);font-size:13px}
nav{display:flex;gap:6px;flex-wrap:wrap;margin-left:auto}nav a{text-decoration:none;font-size:13px;padding:4px 10px;
border:1px solid var(--line);border-radius:999px;color:var(--ink)}nav a:hover{border-color:var(--brand)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:22px 0}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.kpi b{display:block;font:600 24px Unbounded,Onest,sans-serif}.kpi span{color:var(--muted);font-size:13px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
.lead p{margin:.4em 0}.lead li{margin:.25em 0}
.tbl{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{padding:8px 10px;text-align:left;vertical-align:top;
border-bottom:1px solid var(--line)}th{font-size:12px;color:var(--muted);font-weight:600;white-space:nowrap}
tr:last-child td{border-bottom:0}td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.chip{display:inline-block;font-size:12px;padding:1px 8px;border-radius:999px;border:1px solid currentColor;
white-space:nowrap}.ok{color:var(--ok)}.warn{color:var(--warn)}.run{color:var(--run)}.bad{color:var(--bad)}
.todo{color:var(--todo)}.t-core{color:var(--brand)}.t-major{color:var(--run)}.t-niche{color:var(--todo)}
.bar{display:flex;height:10px;border-radius:5px;overflow:hidden;background:var(--line);min-width:120px}
.bar i{display:block;height:100%}
.cov{display:flex;height:8px;border-radius:4px;overflow:hidden;background:var(--line);min-width:90px}
.cov i{display:block}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin:8px 0}
.legend span::before{content:"";display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;
vertical-align:-1px;background:var(--sw)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .top{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}.card .why{color:var(--muted);font-size:13px;
margin:6px 0 10px}.feat{display:grid;grid-template-columns:1fr auto;gap:4px 10px;font-size:13.5px}
.feat .note{grid-column:1/-1;color:var(--muted);font-size:12px;margin:-2px 0 4px}
.item{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--line);border-radius:8px;
padding:12px 14px;margin:8px 0}.item.core{border-left-color:var(--brand)}
.item .top{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}.item .id{font:12px "JetBrains Mono",monospace;
color:var(--muted)}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:8px;font-size:13.5px}
.grid2 h4{margin:0 0 4px;font-size:12px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.grid2 p{margin:0}.closes{margin-top:8px;font-size:12.5px;color:var(--muted)}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}.filters button{font:inherit;font-size:13px;cursor:pointer;
background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:999px;padding:4px 12px}
.filters button[aria-pressed="true"]{border-color:var(--brand);color:var(--brand)}
details{margin:6px 0}summary{cursor:pointer;font-weight:600}
footer{color:var(--muted);font-size:12px;padding:30px 0 40px}
@media (max-width:720px){.grid2{grid-template-columns:1fr}.cards{grid-template-columns:1fr}nav{margin-left:0}}
"""

JS = """
const btns=[...document.querySelectorAll('.filters button')];
btns.forEach(b=>b.onclick=()=>{btns.forEach(x=>x.setAttribute('aria-pressed',x===b));const f=b.dataset.f;
document.querySelectorAll('.item').forEach(it=>{it.hidden=!(f==='all'||it.dataset.tags.split(' ').includes(f))});
document.querySelectorAll('.wave').forEach(w=>{w.hidden=![...w.querySelectorAll('.item')].some(i=>!i.hidden)})});
"""

MARK = ('<svg width="34" height="34" viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" rx="14" '
        'fill="var(--brand)"/><g fill="none" stroke="#FFFDF9" stroke-width="6" stroke-linecap="round"><line x1="32" '
        'y1="12" x2="14" y2="52"/><line x1="32" y1="12" x2="50" y2="52"/><line x1="21" y1="36" x2="43" y2="36"/></g></svg>')


def _repo_link(repo: str) -> str:
    return f'<a href="https://github.com/{E(repo)}">{E(repo)}</a>' if "/" in str(repo) else E(str(repo))


def _coverage(features: list) -> tuple[Counter, str]:
    c = Counter(f["artel"] for f in features)
    total = sum(c.values()) or 1
    bars = "".join(f'<i style="width:{100 * c[k] / total:.1f}%;background:var(--{cls})" title="{lbl}: {c[k]}"></i>'
                   for k, (lbl, cls) in FEATURE.items() if c[k])
    return c, f'<div class="cov">{bars}</div>'


def render_html(repo: Path, today: str | None = None) -> str:
    rm = Roadmap.of_repo(repo)
    hs = load_harnesses(repo)
    snap = hs.get("snapshot", "")
    today = today or snap or None
    rows = []
    for h in hs.get("harnesses", []):
        rows.append((significance(h, today), h))
    rows.sort(key=lambda r: -r[0]["total"])
    items = {it["id"]: it for it in rm.pillars + rm.items}
    closes: dict[str, list] = defaultdict(list)  # roadmap id -> [(harness, mechanism, status)]
    for s, h in rows:
        for f in h.get("features", []):
            if f.get("ref"):
                closes[f["ref"]].append((h["name"], f["name"], s["tier"]))

    # --- numbers ------------------------------------------------------------------------------------------------
    tier_of = {h["repo"]: s["tier"] for s, h in rows}
    core_feats = [f for s, h in rows if s["tier"] == "core" for f in h.get("features", [])]
    major_feats = [f for s, h in rows if s["tier"] == "major" for f in h.get("features", [])]
    cc, _ = _coverage(core_feats)
    mc, _ = _coverage(major_feats)
    done_items = sum(1 for it in rm.items if it.get("status") == "done")
    core_open = [it for it in rm.items if it.get("priority") == "core" and it.get("status") != "done"]
    n_core = sum(1 for s, _ in rows if s["tier"] == "core")
    n_major = sum(1 for s, _ in rows if s["tier"] == "major")

    out = ['<!doctype html><html lang="ru"><head><meta charset="utf-8">'
           '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Дорожная карта Artel</title>'
           '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="stylesheet" '
           'href="https://fonts.googleapis.com/css2?family=Onest:wght@400;600&family=Unbounded:wght@600'
           '&family=JetBrains+Mono&display=swap">', f"<style>{CSS}</style></head><body>",
           f'<header><div class="wrap">{MARK}<div><h1>Дорожная карта Artel</h1><div class="sub">харнессы, от которых '
           f'берём, и что внедряем · снимок метрик {E(snap)} · сгенерировано из roadmap.yaml и harnesses.yaml</div></div>'
           '<nav><a href="#summary">Итог</a><a href="#criteria">Критерии</a><a href="#rating">Рейтинг</a>'
           '<a href="#coverage">Что взято</a><a href="#plan">План</a></nav></div></header><main class="wrap">']

    def pct(c: Counter, *keys) -> int:
        t = sum(c.values()) or 1
        return round(100 * sum(c[k] for k in keys) / t)

    out.append('<div class="kpis">'
               f'<div class="kpi"><b>{len(rows)}</b><span>харнессов оценено ({n_core} в ядре, {n_major} значимых)</span></div>'
               f'<div class="kpi"><b>{pct(cc, "done")}%</b><span>механизмов ядра уже есть в Artel ({cc["done"]} из {sum(cc.values())})</span></div>'
               f'<div class="kpi"><b>{pct(cc, "done", "partial")}%</b><span>есть хотя бы частично</span></div>'
               f'<div class="kpi"><b>{cc["gap"] + cc["planned"]}</b><span>механизмов ядра в плане или пробелы</span></div>'
               f'<div class="kpi"><b>{done_items}/{len(rm.items)}</b><span>пунктов дорожной карты готово</span></div>'
               f'<div class="kpi"><b>{len(core_open)}</b><span>приоритетных пунктов «ядро» впереди</span></div></div>')

    # --- summary ------------------------------------------------------------------------------------------------
    findings = hs.get("findings") or []
    out.append('<h2 id="summary">Итог переработки</h2><div class="panel lead">')
    out += [f"<p>{f}</p>" for f in findings]
    out.append("</div>")

    # --- criteria -----------------------------------------------------------------------------------------------
    out.append('<h2 id="criteria">По какому критерию харнесс считается значимым</h2><div class="panel lead">'
               "<p>Индекс значимости 0–100 — сумма пяти частей. Четыре измеряются по данным (GitHub через "
               "repos.ecosyste.ms и 10 кураторских списков), одна — экспертная, с письменным обоснованием у каждого "
               "харнесса. Открытость кода в индекс не входит: она решает не <i>насколько харнесс важен</i>, а "
               "<i>насколько глубоко у него можно заимствовать</i>, и показана отдельной колонкой.</p></div>"
               '<div class="tbl" style="margin-top:10px"><table><tr><th>Часть</th><th class="n">Вес</th><th>Как считается</th></tr>')
    for key, w, label, how in SIGNIFICANCE:
        out.append(f'<tr><td><span class="legend"><span style="--sw:{PART_COLORS[key]}">{E(label)}</span></span></td>'
                   f'<td class="n">{w}</td><td>{E(how)}</td></tr>')
    out.append("</table></div>")
    out.append('<p class="sub">Уровни: ' + " · ".join(f"<b class='t-{k}'>{E(lbl)}</b> — от {t}" for t, k, lbl in TIERS)
               + ". Почему не просто звёзды: у клонов и сборников звёзд бывает больше, чем у первоисточников "
               "(claw-code — 195 тыс., ECC — 272 тыс.), а научные первоисточники самоулучшения (Darwin Gödel Machine) "
               "почти без звёзд. Поэтому в каждом классе указан его лидер.</p>")

    # --- rating -------------------------------------------------------------------------------------------------
    out.append('<h2 id="rating">Рейтинг харнессов</h2><div class="legend">'
               + "".join(f'<span style="--sw:{PART_COLORS[k]}">{E(lbl)}</span>' for k, _, lbl, _ in SIGNIFICANCE)
               + '</div><div class="tbl"><table><tr><th>#</th><th>Харнесс</th><th class="n">Индекс</th>'
                 '<th>Из чего</th><th class="n">★</th><th>Код</th><th>Взято в Artel</th></tr>')
    leaders = {}
    for s, h in rows:
        leaders.setdefault(h["class"], h["repo"])
    for i, (s, h) in enumerate(rows, 1):
        bar = "".join(f'<i style="width:{v}%;background:{PART_COLORS[k]}" title="{E(dict((a, b) for a, _, b, _ in SIGNIFICANCE)[k])}: {v}"></i>'
                      for k, v in s["parts"].items())
        c, cov = _coverage(h.get("features", []))
        covtxt = (f'{cov}<div class="sub">{c["done"]} есть · {c["partial"]} частично · {c["planned"]} в плане · '
                  f'{c["gap"]} пробел' + (f' · {c["skip"]} не берём' if c["skip"] else "") + '</div>') if h.get("features") else '<span class="sub">нечего брать</span>'
        lead = ' <span class="chip t-core" title="самый значимый в своём классе">лидер класса</span>' \
            if leaders.get(h["class"]) == h["repo"] else ""
        m = h["metrics"]
        out.append(f'<tr><td class="n">{i}</td><td><b>{E(h["name"])}</b>{lead}<br><span class="sub">{_repo_link(h["repo"])}'
                   f' · {E(CLASSES.get(h["class"], h["class"]))} · {E(STUDY.get(h.get("studied", "none"), ""))}</span></td>'
                   f'<td class="n"><b>{s["total"]:.0f}</b><br><span class="chip t-{s["tier"]}">{E(s["tier_label"].split(" — ")[0])}</span></td>'
                   f'<td title="{E(h.get("origin_note", ""))}"><div class="bar">{bar}</div><div class="sub">{s["parts"]["origin"]:.0f} за первоисточник</div></td>'
                   f'<td class="n">{(m.get("stars") or 0) / 1000:.0f}k</td><td class="sub">{E(OPEN[s["openness"]])}</td>'
                   f'<td style="min-width:170px">{covtxt}</td></tr>')
    out.append("</table></div>")

    # --- coverage cards -----------------------------------------------------------------------------------------
    out.append('<h2 id="coverage">Что взято из главных харнессов</h2><p class="sub">Ядро и значимые харнессы: их '
               'основные механизмы и есть ли они в Artel. Ссылка ведёт на пункт плана, который механизм закрывает.</p>'
               '<div class="cards">')
    for s, h in rows:
        if s["tier"] == "niche" or not h.get("features"):
            continue
        c, cov = _coverage(h["features"])
        feats = []
        for f in h["features"]:
            lbl, cls = FEATURE[f["artel"]]
            ref = f' · <a href="#{E(f["ref"])}">{E(f["ref"])}</a>' if f.get("ref") else ""
            feats.append(f'<div><b>{E(f["name"])}</b> <span class="sub">— {E(f.get("what", ""))}</span></div>'
                         f'<div><span class="chip {cls}">{lbl}</span><span class="sub">{ref}</span></div>')
            if f.get("note"):
                feats.append(f'<div class="note">{E(f["note"])}</div>')
        out.append(f'<div class="card"><div class="top"><h3>{E(h["name"])}</h3><span class="chip t-{s["tier"]}">'
                   f'{s["total"]:.0f}</span><span class="sub">{E(CLASSES.get(h["class"], h["class"]))}</span></div>'
                   f'<div class="why">{E(h.get("origin_note", ""))}</div>{cov}<div style="height:10px"></div>'
                   f'<div class="feat">{"".join(feats)}</div></div>')
    out.append("</div>")

    # --- plan ---------------------------------------------------------------------------------------------------
    out.append('<h2 id="plan">План: откуда взято и что будет внедрено</h2>'
               '<p class="sub">Порядок исполнения: сначала пункты с приоритетом «ядро» (механизмы главных харнессов, '
               'которых в Artel нет), затем по волнам. Режим <b>evolve</b> реализует сам Artel (ветка, тесты, '
               'бенчмарк, слияние), <b>design</b> — делается вручную.</p>'
               '<div class="filters"><button data-f="all" aria-pressed="true">Все</button>'
               '<button data-f="core" aria-pressed="false">Приоритет «ядро»</button>'
               '<button data-f="open" aria-pressed="false">Впереди</button>'
               '<button data-f="done" aria-pressed="false">Готово</button></div>')
    out.append('<div class="wave"><h3 style="margin:18px 0 6px">Опоры</h3>')
    for p in rm.pillars:
        src = p.get("source") or {}
        out.append(f'<div class="item" id="{E(p["id"])}" data-tags="done pillar"><div class="top"><span class="id">{E(p["id"])}</span>'
                   f'<b>{E(p["title"])}</b><span class="chip ok">{E(STATUS_MARK.get(p.get("status"), p.get("status")))}</span></div>'
                   f'<div class="closes">Откуда: {E(src.get("harness", ""))} · {_repo_link(src.get("repo", ""))}'
                   + (f' · <code>{E(", ".join(src.get("paths") or []))}</code>' if src.get("paths") else "") + "</div></div>")
    out.append("</div>")
    waves = sorted({it.get("wave", 0) for it in rm.items})
    ordered = sorted(rm.items, key=lambda it: (it.get("wave", 0), it.get("priority") != "core"))
    for w in waves:
        group = [it for it in ordered if it.get("wave", 0) == w]
        done = sum(1 for it in group if it.get("status") == "done")
        title = {0: "Сделано вручную (продукт и надёжность)", 1: "Волна 1 — надёжность цикла",
                 2: "Волна 2 — изоляция и безопасность", 3: "Волна 3 — ядро и долгие задачи",
                 4: "Волна 4 — найдено разбором проектов"}.get(w, f"Волна {w}")
        out.append(f'<div class="wave"><h3 style="margin:22px 0 6px">{E(title)} <span class="sub">— {done}/{len(group)} готово</span></h3>')
        for it in group:
            st = it.get("status", "planned")
            src = it.get("source") or {}
            tags = [st, "open" if st != "done" else "done"] + (["core"] if it.get("priority") == "core" else [])
            core = ' <span class="chip t-core">ядро</span>' if it.get("priority") == "core" else ""
            paths = f'<br><code>{E(", ".join(src.get("paths") or []))}</code>' if src.get("paths") else ""
            lic = f' · лицензия {E(str(src["license"]))}' if src.get("license") else ""
            acc = it.get("acceptance") or []
            plan = E(str(it.get("plan") or it.get("title"))).strip()
            result = f'<p style="margin-top:6px"><b>Итог:</b> {E(str(it["result"]))}</p>' if it.get("result") else ""
            target = f'<p class="sub" style="margin-top:6px">Где в Artel: <code>{E(", ".join(it.get("target") or []))}</code></p>' \
                if it.get("target") else ""
            cl = closes.get(it["id"], [])
            closes_txt = ('<div class="closes">Закрывает: ' + "; ".join(
                f'{E(hn)} — {E(fn)}' for hn, fn, _ in cl[:8]) + (" …" if len(cl) > 8 else "") + "</div>") if cl else ""
            out.append(
                f'<div class="item{" core" if it.get("priority") == "core" else ""}" id="{E(it["id"])}" data-tags="{" ".join(tags)}">'
                f'<div class="top"><span class="id">{E(it["id"])}</span><b>{E(it["title"])}</b>{core}'
                f'<span class="chip {ITEM.get(st, "todo")}">{E(STATUS_MARK.get(st, st))}</span>'
                f'<span class="sub">{E(it.get("mode", ""))}</span></div><div class="grid2">'
                f'<div><h4>Откуда</h4><p>{E(src.get("harness", ""))} · {_repo_link(src.get("repo", ""))}{lic}{paths}</p></div>'
                f'<div><h4>Что будет внедрено</h4><p>{plan}</p>'
                + (f'<p class="sub" style="margin-top:4px">Готово, когда: {E("; ".join(map(str, acc)))}</p>' if acc else "")
                + f"{target}{result}</div></div>{closes_txt}</div>")
        out.append("</div>")
    out.append(f'</main><footer class="wrap">Artel · страница генерируется командой <code>artel roadmap html</code> '
               f'из <code>roadmap.yaml</code> и <code>harnesses.yaml</code>; метрики харнессов — снимок {E(snap)}.</footer>'
               f"<script>{JS}</script></body></html>")
    return "\n".join(out)
