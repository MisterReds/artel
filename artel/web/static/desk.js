/* Arteliq desktop helpers: what to tell the owner through system notifications and the app badge.
   Pure functions (no DOM, no network), so they are tested under node; index.html wires them to the Notification
   API. API: ArtelDesk.fresh(portfolio, shownIds) -> new notifications, .remember(ids, newIds, cap), .loadShown(raw),
   .badgeCount(portfolio). */
(function () {
  'use strict';
  const MAX_BODY = 100;
  const clip = (s, n) => (s.length <= n ? s : s.slice(0, n - 1).trimEnd() + '…');

  function fresh(portfolio, shown) {
    const seen = new Set(shown || []);
    const out = [];
    for (const p of portfolio || []) {
      for (const a of p.attention || []) {
        if (a.reason !== 'нужен человек') continue;  // only what the owner must answer
        // the ask id (or, without one, the text) is part of the key: a question asked again notifies again
        const id = 'ask:' + p.slug + ':' + a.task_id + ':' + (a.asked != null ? a.asked : a.question || '');
        if (seen.has(id)) continue;
        const name = p.name || p.slug;
        out.push({id, kind: 'ask', slug: p.slug, task_id: a.task_id, title: 'Arteliq: нужен ваш ответ',
                  body: clip(a.question ? name + ': ' + a.question : name, MAX_BODY)});
      }
      if (p.status === 'done' || p.stage === 'done') {
        const id = 'done:' + p.slug;
        if (!seen.has(id)) out.push({id, kind: 'done', slug: p.slug, title: 'Arteliq: готово', body: clip(p.name || p.slug, MAX_BODY)});
      }
    }
    return out;
  }

  // the newest `cap` ids, oldest first; an id seen again keeps one place
  function remember(ids, add, cap) {
    const all = (ids || []).filter(x => !(add || []).includes(x)).concat(add || []);
    return all.slice(Math.max(0, all.length - (cap || 500)));
  }

  // the ids stored by an earlier call or another window: always read fresh, never trust a copy in memory
  function loadShown(raw) {
    try { const v = JSON.parse(raw || '[]'); return Array.isArray(v) ? v : []; } catch (e) { return []; }
  }

  const badgeCount = portfolio => (portfolio || []).reduce((n, p) => n + (p.attention || []).length, 0);

  // the auditor writes «… критериям: (1) a; (2) b. Остальное.» as one paragraph: lead, the numbered criteria, a tail.
  // Markers must run 1, 2, 3… and there must be at least two, otherwise the text stays whole. Text is not altered.
  function verdict(text) {
    const t = String(text == null ? '' : text);
    const marks = [];
    const re = /\((\d{1,2})\)\s+/g;
    let m;
    while ((m = re.exec(t))) {
      if (Number(m[1]) !== marks.length + 1) break;
      marks.push({at: m.index, end: re.lastIndex});
    }
    if (marks.length < 2) return {lead: t, items: [], tail: ''};
    const clean = x => x.trim().replace(/[;,.]+$/, '').trim();
    const items = marks.map((k, i) => t.slice(k.end, i + 1 < marks.length ? marks[i + 1].at : t.length));
    let tail = '';
    const last = items[items.length - 1];
    const cut = /\.\s+(?=[A-ZА-ЯЁ])/.exec(last);  // a sentence after the last criterion is not part of it
    if (cut) { items[items.length - 1] = last.slice(0, cut.index); tail = last.slice(cut.index + 1).trim(); }
    return {lead: t.slice(0, marks[0].at).trim(), items: items.map(clean), tail};
  }

  window.ArtelDesk = {fresh, remember, loadShown, badgeCount, verdict};
})();
