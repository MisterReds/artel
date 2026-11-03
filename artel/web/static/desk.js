/* Artel desktop helpers: what to tell the owner through system notifications and the app badge.
   Pure functions (no DOM, no network), so they are tested under node; index.html wires them to the Notification
   API. API: ArtelDesk.fresh(portfolio, shownIds) -> new notifications, .remember(ids, newIds, cap),
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
        const id = 'ask:' + p.slug + ':' + a.task_id;
        if (seen.has(id)) continue;
        const name = p.name || p.slug;
        out.push({id, kind: 'ask', slug: p.slug, task_id: a.task_id, title: 'Artel: нужен ваш ответ',
                  body: clip(a.question ? name + ': ' + a.question : name, MAX_BODY)});
      }
      if (p.status === 'done' || p.stage === 'done') {
        const id = 'done:' + p.slug;
        if (!seen.has(id)) out.push({id, kind: 'done', slug: p.slug, title: 'Artel: готово', body: clip(p.name || p.slug, MAX_BODY)});
      }
    }
    return out;
  }

  // the newest `cap` ids, oldest first; an id seen again keeps one place
  function remember(ids, add, cap) {
    const all = (ids || []).filter(x => !(add || []).includes(x)).concat(add || []);
    return all.slice(Math.max(0, all.length - (cap || 500)));
  }

  const badgeCount = portfolio => (portfolio || []).reduce((n, p) => n + (p.attention || []).length, 0);

  window.ArtelDesk = {fresh, remember, badgeCount};
})();
