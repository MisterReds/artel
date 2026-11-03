/* Artel chat: a side panel on every dashboard page. Talks to POST /api/chat and GET /api/chat/history.
   All text from the server is put in with textContent; nothing from a model or a project becomes HTML. */
(function () {
  'use strict';
  var root = document.getElementById('artel-chat');
  if (!root) return;
  var SUGGEST = ['Что ждёт меня?', 'Как идут дела?', 'Сколько потрачено?'];
  var fab, panel, list, input, chips, closeBtn, sendBtn, opener = null, busy = false, loaded = false;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); }
  function refHref(r) {
    if (r.type === 'file') return '/api/projects/' + encodeURIComponent(r.slug) + '/files?path=' + encodeURIComponent(r.id || '');
    return '#/p/' + encodeURIComponent(r.slug);
  }

  function proposalCard(p) {
    var card = el('div', 'ac-prop');
    var label = el('span', 'ac-act', p.label);
    var ok = el('button', 'primary', 'Выполнить'); ok.type = 'button';
    var no = el('button', null, 'Отмена'); no.type = 'button';
    function done(text, bad) {
      card.textContent = '';
      card.appendChild(el('span', 'ac-act' + (bad ? ' no' : ''), text));
    }
    ok.addEventListener('click', function () {
      ok.disabled = no.disabled = true;
      fetch('/api/chat/confirm', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id: p.id})})
        .then(function (r) { return r.json().catch(function () { return {}; }); })
        .then(function (d) { done(d.label || d.error || 'Не выполнено', !d.ok); })
        .catch(function () { done('Не получилось. Попробуйте ещё раз.', true); });
    });
    no.addEventListener('click', function () { done('Отменено: ' + p.label, true); });
    card.appendChild(label); card.appendChild(ok); card.appendChild(no);
    return card;
  }

  function addMsg(role, text, extra) {
    var m = el('div', 'ac-msg ' + role, text);
    if (extra) {
      var box = el('div', 'ac-extra');
      (extra.proposed || []).forEach(function (p) { box.appendChild(proposalCard(p)); });
      var seen = {};
      (extra.refs || []).forEach(function (r) {
        var k = r.type + '|' + r.slug + '|' + r.id; if (seen[k]) return; seen[k] = 1;
        var a = el('a', 'ac-ref', (r.type === 'task' ? 'Задача: ' : '') + (r.label || r.slug));
        a.href = refHref(r);
        a.addEventListener('click', function () { if (window.innerWidth <= 640) close(); });
        box.appendChild(a);
      });
      if (box.childNodes.length) m.appendChild(box);
    }
    list.appendChild(m);
    list.scrollTop = list.scrollHeight;
    return m;
  }

  function build() {
    fab = el('button', 'ac-fab', 'Спросить');
    fab.type = 'button';
    fab.setAttribute('aria-haspopup', 'dialog');
    fab.addEventListener('click', open);
    panel = el('section', 'ac-panel');
    panel.hidden = true;
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-modal', 'true');
    panel.setAttribute('aria-labelledby', 'ac-title');
    var head = el('div', 'ac-head');
    var h = el('h2', null, 'Спросить Artel'); h.id = 'ac-title';
    closeBtn = el('button', null, '✕'); closeBtn.type = 'button'; closeBtn.setAttribute('aria-label', 'Закрыть чат');
    closeBtn.addEventListener('click', close);
    head.appendChild(h); head.appendChild(closeBtn);
    list = el('div', 'ac-list'); list.setAttribute('role', 'log'); list.setAttribute('aria-live', 'polite');
    chips = el('div', 'ac-chips');
    SUGGEST.forEach(function (s) {
      var c = el('button', 'ac-chip', s); c.type = 'button';
      c.addEventListener('click', function () { send(s); });
      chips.appendChild(c);
    });
    var form = el('form');
    input = el('textarea'); input.rows = 1; input.maxLength = 2000; input.placeholder = 'Спросите о проектах…';
    input.setAttribute('aria-label', 'Сообщение');
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
    });
    sendBtn = el('button', 'primary', 'Отправить'); sendBtn.type = 'submit';
    form.appendChild(input); form.appendChild(sendBtn);
    form.addEventListener('submit', function (e) { e.preventDefault(); send(input.value); });
    panel.appendChild(head); panel.appendChild(list); panel.appendChild(chips); panel.appendChild(form);
    panel.addEventListener('keydown', onKey);
    root.appendChild(fab); root.appendChild(panel);
  }

  function focusables() {
    return Array.prototype.filter.call(panel.querySelectorAll('button, a[href], textarea'), function (n) {
      return !n.disabled && n.offsetParent !== null; });
  }
  function onKey(e) {
    if (e.key === 'Escape') { e.stopPropagation(); close(); return; }
    if (e.key !== 'Tab') return;
    var f = focusables(); if (!f.length) return;
    var first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  function open() {
    opener = document.activeElement;
    panel.hidden = false; fab.hidden = true;
    if (!loaded) history();
    input.focus();
  }
  function close() {
    panel.hidden = true; fab.hidden = false;
    (opener && opener.focus ? opener : fab).focus();
  }

  function fail(status, data) {
    if (status === 401) { location.href = '/login'; return; }
    addMsg('assistant err', (data && data.error) || 'Не получилось. Попробуйте ещё раз.');
  }
  function history() {
    loaded = true;
    fetch('/api/chat/history').then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
      if (!d) return;
      (d.turns || []).forEach(function (t) { addMsg(t.role === 'user' ? 'user' : 'assistant', t.text); });
      if (!(d.turns || []).length) addMsg('assistant', 'Я знаю обо всех проектах и могу рассказать, не открывая их. Что вас интересует?');
      else chips.hidden = true;
    }).catch(function () {});
  }
  function send(text) {
    text = String(text || '').trim();
    if (!text || busy) return;
    busy = true; sendBtn.disabled = true; chips.hidden = true; input.value = '';
    addMsg('user', text);
    var wait = addMsg('assistant', 'Смотрю проекты…'); wait.classList.add('ac-wait');
    fetch('/api/chat', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: text})})
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { return [r.status, d]; }); })
      .then(function (res) {
        wait.remove();
        if (res[0] !== 200) return fail(res[0], res[1]);
        addMsg('assistant', res[1].reply, res[1]);
      })
      .catch(function () { wait.remove(); fail(0, null); })
      .then(function () { busy = false; sendBtn.disabled = false; input.focus(); });
  }

  build();
  window.ArtelChat = {open: open, close: close, esc: esc};
})();
