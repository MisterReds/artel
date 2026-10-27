/* Artel file viewer: tree of a project's shared/ folder on the left, a preview by file type on the right.
   No dependencies. Everything dynamic goes through esc() or textContent; HTML files run only inside
   <iframe sandbox="allow-scripts"> served by /preview with `Content-Security-Policy: sandbox allow-scripts`.
   API: ArtelFiles.mount(host, {slug, remote, toast}) -> {open(path), reload()}; ArtelFiles.hl / .md are pure. */
(function () {
  'use strict';
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

  /* ---------- lightweight syntax highlighter ---------- */
  const words = s => '\\b(?:' + s.trim().split(/\s+/).join('|') + ')\\b';
  const JS = 'async await break case catch class const continue debugger default delete do else export extends finally for from function if import in instanceof let new of return static super switch this throw try typeof var void while with yield null true false undefined';
  const STR = '"(?:\\\\.|[^"\\\\\\n])*"|\'(?:\\\\.|[^\'\\\\\\n])*\'';
  const NUM = '\\b(?:0x[0-9a-fA-F]+|\\d+(?:\\.\\d+)?(?:e[+-]?\\d+)?)\\b';
  const RULES = {
    py: [['c', '#[^\\n]*'], ['s', '"""[\\s\\S]*?"""|\'\'\'[\\s\\S]*?\'\'\'|[rbfRBF]?(?:' + STR + ')'], ['d', '@\\w[\\w.]*'],
      ['k', words('and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield None True False self')],
      ['n', NUM], ['f', '\\b[A-Za-z_]\\w*(?=\\()']],
    js: [['c', '//[^\\n]*|/\\*[\\s\\S]*?\\*/'], ['s', '`(?:\\\\[\\s\\S]|[^`\\\\])*`|' + STR], ['k', words(JS)], ['n', NUM], ['f', '\\b[A-Za-z_$][\\w$]*(?=\\()']],
    json: [['p', '"(?:\\\\.|[^"\\\\\\n])*"(?=\\s*:)'], ['s', '"(?:\\\\.|[^"\\\\\\n])*"'], ['k', words('true false null')], ['n', '-?\\d+(?:\\.\\d+)?(?:[eE][+-]?\\d+)?']],
    css: [['c', '/\\*[\\s\\S]*?\\*/'], ['k', '@[\\w-]+'], ['s', STR], ['n', '#[0-9a-fA-F]{3,8}\\b|-?\\d*\\.?\\d+(?:px|em|rem|%|vh|vw|s|ms|deg|fr)?\\b'], ['p', '[\\w-]+(?=\\s*:)']],
    html: [['c', '<!--[\\s\\S]*?-->'], ['k', '</?[A-Za-z][\\w:-]*|/?>'], ['p', '[\\w:@.-]+(?==)'], ['s', STR]],
    sh: [['c', '(?:^|\\s)#[^\\n]*'], ['s', STR], ['d', '\\$\\{?[\\w#?@*]+\\}?'],
      ['k', words('if then else elif fi for while do done case esac function in return export local echo cd exit set unset source')], ['n', NUM]],
    md: [['k', '^#{1,6} [^\\n]*'], ['s', '```[\\s\\S]*?```|`[^`\\n]+`'], ['d', '\\*\\*[^*\\n]+\\*\\*'], ['f', '\\[[^\\]\\n]+\\]\\([^)\\n]+\\)'], ['p', '^\\s*(?:[-*+]|\\d+\\.) ']]
  };
  RULES.ts = RULES.js.map((r, i) => i === 2 ? ['k', words(JS + ' interface type enum implements public private protected readonly namespace declare abstract keyof')] : r);
  const compiled = Object.create(null);
  function rulesFor(lang) {
    if (!Object.hasOwn(RULES, lang)) return null;
    return compiled[lang] || (compiled[lang] = {cls: RULES[lang].map(r => r[0]),
      re: new RegExp(RULES[lang].map(r => '(' + r[1] + ')').join('|'), 'gm')});
  }
  /* highlighted HTML split into lines; a token that spans lines is closed and reopened on each one */
  function hl(text, lang) {
    const out = [];
    const put = (cls, t) => {
      if (!t) return;
      out.push(t.split('\n').map(seg => seg ? (cls ? '<span class="t-' + cls + '">' + esc(seg) + '</span>' : esc(seg)) : '').join('\n'));
    };
    const r = rulesFor(lang);
    if (!r) put('', text);
    else {
      let last = 0, m;
      r.re.lastIndex = 0;
      while ((m = r.re.exec(text))) {
        if (m[0] === '') { r.re.lastIndex++; continue; }
        put('', text.slice(last, m.index));
        const g = m.slice(1).findIndex(x => x !== undefined);
        put(r.cls[g], m[0]);
        last = m.index + m[0].length;
      }
      put('', text.slice(last));
    }
    return out.join('').split('\n');
  }

  /* ---------- Markdown (headings, lists, code, quotes, tables, links, images) ---------- */
  const linkOk = u => /^(https?:\/\/|mailto:|#)/i.test(u);  // anything else (javascript:, data:, relative paths) is plain text
  const safeUrl = u => /^(https?:\/\/|mailto:|#|[\w./%~-])/i.test(u) && !/^\s*(javascript|data|vbscript):/i.test(u);
  function joinPath(dir, rel) {
    const parts = dir ? dir.split('/') : [];
    for (const seg of rel.split('/')) {
      if (seg === '..') { if (!parts.length) return null; parts.pop(); } else if (seg && seg !== '.') parts.push(seg);
    }
    return parts.join('/');
  }
  function inline(src, ctx) {
    const keep = [];
    let s = esc(src).replace(/`([^`\n]+)`/g, (_, c) => { keep.push('<code>' + c + '</code>'); return '\u0000' + (keep.length - 1) + '\u0000'; });
    s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (_, alt, u) => {
      if (/^https?:\/\//i.test(u) || !safeUrl(u) || !ctx) return '<span class="md-alt">[' + alt + ']</span>';
      let dec;
      try { dec = decodeURIComponent(u.replace(/&amp;/g, '&')); } catch (e) { return '<span class="md-alt">[' + alt + ']</span>'; }
      const p = joinPath(ctx.dir, dec);
      return p == null ? alt : '<img alt="' + alt + '" src="' + esc(ctx.raw(p)) + '">';
    });
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, t, u) => {
      if (!linkOk(u)) return t;
      const ext = /^https?:\/\//i.test(u);
      return '<a href="' + u + '"' + (ext ? ' target="_blank" rel="noopener noreferrer"' : '') + '>' + t + '</a>';
    });
    s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>').replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?:;]|$)/g, '$1<em>$2</em>')
      .replace(/(^|[\s(])_([^_\n]+)_(?=[\s).,!?:;]|$)/g, '$1<em>$2</em>');
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => keep[+i]);
  }
  function md(src, ctx) {
    const lines = String(src).replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let i = 0, para = [];
    const flush = () => { if (para.length) { out.push('<p>' + inline(para.join(' '), ctx) + '</p>'); para = []; } };
    const cells = l => l.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
    while (i < lines.length) {
      const l = lines[i];
      let m;
      if ((m = /^```\s*([\w+-]*)/.exec(l))) {
        flush();
        const code = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]);
        i++;
        out.push('<pre class="md-code"><code>' + hl(code.join('\n'), m[1].toLowerCase()).join('\n') + '</code></pre>');
      } else if ((m = /^(#{1,6})\s+(.*)$/.exec(l))) {
        flush();
        out.push('<h' + m[1].length + '>' + inline(m[2].replace(/\s#+\s*$/, ''), ctx) + '</h' + m[1].length + '>');
        i++;
      } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(l)) { flush(); out.push('<hr>'); i++; }
      else if (/^\s*>/.test(l)) {
        flush();
        const q = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) q.push(lines[i++].replace(/^\s*>\s?/, ''));
        out.push('<blockquote>' + md(q.join('\n'), ctx) + '</blockquote>');
      } else if (/^\s*([-*+]|\d+\.)\s+/.test(l)) {
        flush();
        const ord = /^\s*\d+\./.test(l), items = [];
        while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
          let t = lines[i++].replace(/^\s*([-*+]|\d+\.)\s+/, '');
          const task = /^\[([ xX])\]\s+/.exec(t);
          if (task) t = t.slice(task[0].length);
          items.push('<li>' + (task ? '<input type="checkbox" disabled aria-label="пункт"' + (task[1] !== ' ' ? ' checked' : '') + '> ' : '') + inline(t, ctx) + '</li>');
        }
        out.push((ord ? '<ol>' : '<ul>') + items.join('') + (ord ? '</ol>' : '</ul>'));
      } else if (/\|/.test(l) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[i + 1])) {
        flush();
        const head = cells(l);
        i += 2;
        const body = [];
        while (i < lines.length && /\|/.test(lines[i]) && lines[i].trim()) body.push(cells(lines[i++]));
        out.push('<div class="tablewrap"><table><thead><tr>' + head.map(c => '<th>' + inline(c, ctx) + '</th>').join('') + '</tr></thead><tbody>'
          + body.map(r => '<tr>' + r.map(c => '<td>' + inline(c, ctx) + '</td>').join('') + '</tr>').join('') + '</tbody></table></div>');
      } else if (!l.trim()) { flush(); i++; }
      else { para.push(l.trim()); i++; }
    }
    flush();
    return out.join('\n');
  }

  /* ---------- helpers ---------- */
  const fmtSize = n => n == null ? '' : n < 1024 ? n + ' Б' : n < 1048576 ? (n / 1024).toFixed(n < 10240 ? 1 : 0).replace('.', ',') + ' КБ' : (n / 1048576).toFixed(1).replace('.', ',') + ' МБ';
  const ICON = {markdown: '📝', html: '🌐', code: '📜', json: '🧾', csv: '📊', image: '🖼', pdf: '📕', video: '🎬', text: '📄', binary: '📦', toolarge: '📦', file: '📄'};
  const KINDRU = {markdown: 'Markdown', html: 'Веб-страница', code: 'Код', json: 'JSON', csv: 'Таблица', image: 'Изображение', pdf: 'PDF', video: 'Видео', text: 'Текст', binary: 'Двоичный файл', toolarge: 'Большой файл'};
  const el = (tag, attrs, ...kids) => {
    const e = document.createElement(tag);
    for (const k in attrs || {}) { if (k === 'class') e.className = attrs[k]; else if (k.startsWith('on')) e[k] = attrs[k]; else e.setAttribute(k, attrs[k]); }
    kids.flat().forEach(c => e.append(c == null ? '' : c));
    return e;
  };
  const MAX_LINES = 5000;

  function codeView(text, lang) {
    const lines = hl(text, lang);
    const cut = lines.length > MAX_LINES;
    const shown = cut ? lines.slice(0, MAX_LINES) : lines;
    const box = el('div', {class: 'fv-code', role: 'region', 'aria-label': 'Исходный текст', tabindex: '0'});
    box.innerHTML = shown.map((h, i) => '<div class="l"><span class="n" aria-hidden="true">' + (i + 1) + '</span><span class="c">' + (h || ' ') + '</span></div>').join('');
    if (cut) box.append(el('div', {class: 'fv-note'}, 'Показаны первые ' + MAX_LINES + ' строк из ' + lines.length + '. Скачайте файл, чтобы увидеть всё.'));
    return box;
  }

  const JSON_NODES = 5000, JSON_DEPTH = 30;
  function jsonLimit(text, onText) {
    const d = el('div', {class: 'fv-note'}, text + ' ');
    if (onText) d.append(el('button', {type: 'button', onclick: onText}, 'показать как текст'));
    return d;
  }
  /* Children are built when a node is first opened; at most JSON_NODES nodes in all and JSON_DEPTH levels deep. */
  function jsonNode(key, val, depth, b, onText) {
    if (b.n >= JSON_NODES) { if (b.cut) return null; b.cut = true; return jsonLimit('Слишком много узлов (' + JSON_NODES + '), остальное скрыто.', onText); }
    b.n++;
    const label = key == null ? '' : '<span class="t-p">' + esc(key) + '</span>: ';
    if (val === null || typeof val !== 'object') {
      const cls = typeof val === 'string' ? 's' : typeof val === 'number' ? 'n' : 'k';
      const d = el('div', {class: 'jl'});
      d.innerHTML = label + '<span class="t-' + cls + '">' + esc(JSON.stringify(val)) + '</span>';
      return d;
    }
    if (depth >= JSON_DEPTH) return jsonLimit('Вложенность глубже ' + JSON_DEPTH + ' уровней не показана.', onText);
    const arr = Array.isArray(val), keys = Object.keys(val);
    const det = el('details', {class: 'jn'});
    const sum = el('summary');
    sum.innerHTML = label + (arr ? '[' : '{') + '<span class="stat"> ' + keys.length + (arr ? ' эл.' : ' ключ.') + '</span>' + (arr ? ']' : '}');
    det.append(sum);
    const body = el('div', {class: 'jb'});
    det.append(body);
    let filled = false;
    const fill = () => {
      if (filled) return;
      filled = true;
      keys.slice(0, 300).forEach(k => { const c = jsonNode(arr ? '[' + k + ']' : k, val[k], depth + 1, b, onText); if (c) body.append(c); });
      if (keys.length > 300) body.append(el('div', {class: 'fv-note'}, '… ещё ' + (keys.length - 300)));
    };
    if (depth < 2) { det.open = true; fill(); } else det.addEventListener('toggle', () => { if (det.open) fill(); });
    return det;
  }

  function csvView(r) {
    const wrap = el('div', {class: 'tablewrap fv-csv'});
    let rows = r.rows.slice(), col = -1, dir = 1;
    const table = el('table'), thead = el('thead'), tbody = el('tbody');
    const num = v => v !== '' && !isNaN(Number(String(v).replace(',', '.')));
    const draw = () => {
      tbody.replaceChildren(...rows.map(row => el('tr', null, ...r.header.map((_, i) => el('td', null, row[i] == null ? '' : row[i])))));
      thead.querySelectorAll('th').forEach((th, i) => th.setAttribute('aria-sort', i === col ? (dir > 0 ? 'ascending' : 'descending') : 'none'));
    };
    const tr = el('tr');
    r.header.forEach((h, i) => {
      const b = el('button', {type: 'button', class: 'thb', title: 'Сортировать'}, h || '#' + (i + 1));
      b.onclick = () => {
        dir = col === i ? -dir : 1; col = i;
        const numeric = rows.every(x => x[i] === '' || x[i] == null || num(x[i]));
        rows.sort((a, b2) => (numeric ? Number(String(a[i] || 0).replace(',', '.')) - Number(String(b2[i] || 0).replace(',', '.'))
          : String(a[i] || '').localeCompare(String(b2[i] || ''), 'ru')) * dir);
        draw();
      };
      tr.append(el('th', {scope: 'col', 'aria-sort': 'none'}, b));
    });
    thead.append(tr); table.append(thead, tbody); wrap.append(table);
    draw();
    const note = r.total_rows > r.rows.length ? 'Показаны первые ' + r.rows.length + ' строк из ' + r.total_rows + '.' : r.total_rows + ' строк';
    return el('div', null, el('div', {class: 'fv-note'}, note), wrap);
  }

  /* ---------- the viewer ---------- */
  function mount(host, opts) {
    const slug = encodeURIComponent(opts.slug), api = '/api/projects/' + slug;
    const raw = p => api + '/raw?path=' + encodeURIComponent(p);
    const S = {items: [], root: '', q: '', open: null, sel: null, view: 'rendered', openDirs: new Set(), cache: {}};
    host.classList.add('fv');
    host.innerHTML = '<div class="fv-tree" role="navigation" aria-label="Файлы проекта"><div class="fv-tools"><input type="search" class="fv-q" placeholder="Поиск по имени" aria-label="Поиск файла по имени">'
      + '<a class="btn" id="fv-zip" href="' + api + '/archive" download title="Скачать всю папку архивом">Скачать всё (zip)</a></div><div class="fv-list" id="fv-list" role="tree"></div></div>'
      + '<div class="fv-view" id="fv-view" aria-live="polite"><div class="fv-empty">Выберите файл слева.</div></div>';
    const list = host.querySelector('#fv-list'), view = host.querySelector('#fv-view');
    host.dataset.screen = 'tree';

    const byDir = () => {
      const m = {'': []};
      S.items.forEach(i => { const d = i.path.includes('/') ? i.path.slice(0, i.path.lastIndexOf('/')) : ''; (m[d] = m[d] || []).push(i); });
      return m;
    };
    function row(i, depth, name) {
      const sel = S.sel === i.path;
      const a = el('button', {type: 'button', class: 'fv-row' + (sel ? ' sel' : ''), role: 'treeitem', 'data-p': i.path, style: 'padding-left:' + (8 + depth * 14) + 'px',
        'aria-selected': sel ? 'true' : 'false'});
      if (i.dir) {
        const open = S.openDirs.has(i.path) && !i.collapsed;
        a.setAttribute('aria-expanded', i.collapsed ? 'false' : String(open));
        a.append(el('span', {class: 'ic', 'aria-hidden': 'true'}, i.collapsed ? '📁' : open ? '📂' : '📁'), el('span', {class: 'nm'}, name));
        if (i.collapsed) a.append(el('span', {class: 'stat'}, 'скрыто'));
        else a.onclick = () => { if (open) S.openDirs.delete(i.path); else S.openDirs.add(i.path); drawTree(); };
        if (i.collapsed) a.disabled = true;
      } else {
        a.append(el('span', {class: 'ic', 'aria-hidden': 'true'}, ICON[i.kind] || ICON.file), el('span', {class: 'nm'}, name),
          i.changed ? el('span', {class: 'chg', title: 'Изменён последней задачей'}, 'изменён') : '', el('span', {class: 'sz'}, fmtSize(i.size)));
        a.onclick = () => openFile(i.path);
      }
      return a;
    }
    function drawTree() {
      const q = S.q.trim().toLowerCase(), frag = document.createDocumentFragment();
      if (q) {
        const hit = S.items.filter(i => !i.dir && i.path.toLowerCase().includes(q));
        hit.slice(0, 300).forEach(i => frag.append(row(i, 0, i.path)));
        if (!hit.length) frag.append(el('div', {class: 'fv-empty'}, 'Ничего не найдено.'));
      } else {
        const dirs = byDir();
        const walk = (d, depth) => (dirs[d] || []).sort((a, b) => (b.dir - a.dir) || a.path.localeCompare(b.path)).forEach(i => {
          frag.append(row(i, depth, i.path.slice(i.path.lastIndexOf('/') + 1)));
          if (i.dir && !i.collapsed && S.openDirs.has(i.path)) walk(i.path, depth + 1);
        });
        walk('', 0);
        if (!S.items.length) frag.append(el('div', {class: 'fv-empty'}, 'Файлов пока нет.'));
      }
      list.replaceChildren(frag);
    }
    async function reload() {
      try {
        const r = await fetch(api + '/tree').then(x => x.json());
        if (!r.items) throw new Error(r.error || 'tree');
        S.items = r.items; S.root = r.root || '';
        if (!S.inited) { S.items.filter(i => i.dir && !i.path.includes('/')).forEach(i => S.openDirs.add(i.path)); S.inited = true; }
        drawTree();
      } catch (e) { list.replaceChildren(el('div', {class: 'fv-empty'}, 'Не удалось загрузить файлы. '), el('button', {type: 'button', onclick: reload}, 'Повторить')); }
    }
    host.querySelector('.fv-q').oninput = e => { S.q = e.target.value; drawTree(); };

    function toolbar(r) {
      const rel = r.path, full = S.root && !opts.remote ? S.root + '/' + rel : 'shared/' + rel;
      const bar = el('div', {class: 'fv-bar'});
      bar.append(el('button', {type: 'button', class: 'fv-back', onclick: () => { host.dataset.screen = 'tree'; }}, '← Назад'),
        el('div', {class: 'fv-title'}, el('b', null, r.name), el('span', {class: 'stat'}, ' ' + (KINDRU[r.kind] || '') + ' · ' + fmtSize(r.size))));
      const acts = el('div', {class: 'fv-acts'});
      if (['markdown', 'html', 'json'].includes(r.kind) && r.text != null) {
        const t = el('button', {type: 'button', class: S.view === 'source' ? 'on' : '', 'aria-pressed': String(S.view === 'source')}, 'Исходник');
        t.onclick = () => { S.view = S.view === 'source' ? 'rendered' : 'source'; paint(r); };
        acts.append(t);
      }
      acts.append(el('a', {class: 'btn', href: raw(rel) + '&download=1', download: r.name}, 'Скачать'));
      const cp = el('button', {type: 'button'}, 'Копировать путь');
      cp.onclick = async () => {
        try { await navigator.clipboard.writeText(full); } catch (e) {
          const t = el('textarea'); t.value = full; document.body.append(t); t.select();
          try { document.execCommand('copy'); } catch (e2) { /* nothing else to try */ } t.remove();
        }
        opts.toast && opts.toast('Путь скопирован');
      };
      acts.append(cp);
      bar.append(acts);
      return bar;
    }
    function body(r) {
      const dir = r.path.includes('/') ? r.path.slice(0, r.path.lastIndexOf('/')) : '';
      switch (r.kind) {
        case 'markdown':
          if (S.view === 'source') return codeView(r.text, 'md');
          { const d = el('div', {class: 'md'}); d.innerHTML = md(r.text, {dir, raw}); return d; }
        case 'html':
          if (S.view === 'source') return codeView(r.text, 'html');
          return el('iframe', {class: 'fv-frame', sandbox: 'allow-scripts', title: 'Предпросмотр: ' + r.name,
            src: api + '/preview/' + (r.preview_token ? '@' + r.preview_token + '/' : '') + r.path.split('/').map(encodeURIComponent).join('/')});
        case 'json': {
          if (S.view === 'source') return codeView(r.text, 'json');
          try { const d = el('div', {class: 'fv-json', role: 'tree'}); d.append(jsonNode(null, JSON.parse(r.text), 0, {n: 0}, () => { S.view = 'source'; paint(r); })); return d; } catch (e) {
            return el('div', null, el('div', {class: 'fv-note'}, 'Это не корректный JSON: ' + e.message), codeView(r.text, 'json'));
          }
        }
        case 'csv': return csvView(r);
        case 'code': return codeView(r.text, r.lang);
        case 'text': return codeView(r.text, '');
        case 'image': return el('div', {class: 'fv-media'}, el('img', {src: raw(r.path), alt: r.name}));
        case 'pdf': return el('iframe', {class: 'fv-frame', src: raw(r.path), title: r.name});
        case 'video': return el('div', {class: 'fv-media'}, el('video', {src: raw(r.path), controls: '', preload: 'metadata'}));
        default:
          return el('div', {class: 'fv-info'}, el('p', null, r.kind === 'toolarge' ? 'Файл больше 2 МБ — предпросмотр отключён.' : 'Предпросмотр для этого типа недоступен.'),
            el('p', {class: 'stat'}, r.name + ' · ' + fmtSize(r.size)),
            el('a', {class: 'btn primary', href: raw(r.path) + '&download=1', download: r.name}, 'Скачать'));
      }
    }
    function paint(r) { view.replaceChildren(toolbar(r), r.note ? el('div', {class: 'fv-note'}, r.note) : '', el('div', {class: 'fv-body'}, body(r))); }
    async function openFile(p) {
      S.sel = p; S.view = 'rendered'; host.dataset.screen = 'view';
      drawTree();
      view.replaceChildren(el('div', {class: 'sk-wrap', role: 'status', 'aria-label': 'Загрузка'}, el('i', {class: 'sk'}), el('i', {class: 'sk', style: 'width:70%'})));
      try {
        const r = await fetch(api + '/file?path=' + encodeURIComponent(p)).then(x => x.json());
        if (S.sel !== p) return;
        if (r.error) throw new Error(r.error);
        paint(r);
      } catch (e) {
        view.replaceChildren(el('div', {class: 'fv-empty'}, 'Не удалось открыть файл. '), el('button', {type: 'button', onclick: () => openFile(p)}, 'Повторить'));
      }
    }
    const ready = reload();
    return {open: openFile, reload, async openDefault() {
      await ready;
      const best = S.items.find(i => i.path === 'index.html') || S.items.find(i => /(^|\/)(readme|index)\.(md|html)$/i.test(i.path)) || S.items.find(i => !i.dir);
      if (best) openFile(best.path);
    }};
  }
  window.ArtelFiles = {mount, hl, md, fmtSize, jsonNode: (v, onText) => jsonNode(null, v, 0, {n: 0}, onText)};
})();
