/* Hangar — assistant IA (façon Plane AI) : conversation en flux (SSE), rendu Markdown + KaTeX, contexte de la page,
   « @ » pour ajouter du contexte, « / » pour les commandes, actions proposées, historique, 👍 / 👎.
   Aucune ressource externe : KaTeX et highlight.js sont chargés à la demande depuis /static/vendor. */
(() => {
  'use strict';
  const H = window.HANGAR || {};
  const CFG = H.ai || {};
  const U = CFG.urls || {};
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const tpl = (u, id) => String(u || '').replace('{id}', encodeURIComponent(id));
  const toast = (m, t) => (H.toast ? H.toast(m, t) : null);
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
  const store = { get: (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: (k, v) => { try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) { /* ignore */ } } };

  /* ------------------------------------------------------------------ Icônes (gabarit rendu par le serveur) */
  function icon(name, size) {
    const t = document.querySelector('template.ai-icons');
    const el = t && t.content.querySelector(`[data-i="${name}"]`);
    let html = el ? el.innerHTML : (t && t.content.querySelector('[data-i="page"]') ? t.content.querySelector('[data-i="page"]').innerHTML : '');
    if (size) html = html.replace(/width="\d+"/, `width="${size}"`).replace(/height="\d+"/, `height="${size}"`);
    return html;
  }

  /* ------------------------------------------------------------------ Réseau */
  async function api(url, method = 'GET', data) {
    const opts = { method, headers: { 'X-CSRF-Token': H.csrf, 'X-Requested-With': 'fetch' } };
    if (data !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(data); }
    const r = await fetch(url, opts);
    let j = null;
    try { j = await r.json(); } catch (e) { /* réponse non JSON */ }
    if (!r.ok || (j && j.ok === false)) { const err = new Error((j && j.error) || `Erreur ${r.status}`); err.data = j; throw err; }
    return j || {};
  }

  async function readSSE(resp, onEvent) {
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let k;
      while ((k = buf.indexOf('\n\n')) >= 0) {
        const raw = buf.slice(0, k);
        buf = buf.slice(k + 2);
        let ev = 'message';
        let data = '';
        raw.split('\n').forEach((line) => {
          if (line.startsWith('event:')) ev = line.slice(6).trim();
          else if (line.startsWith('data:')) data += line.slice(5).trimStart();
        });
        if (data) { let d = null; try { d = JSON.parse(data); } catch (e) { /* ignore */ } if (d) onEvent(ev, d); }
      }
    }
  }

  /* ------------------------------------------------------------------ Markdown (rendu pendant le flux) */
  function inline(s) {
    const keep = [];
    const hold = (h) => { keep.push(h); return `\u0000${keep.length - 1}\u0000`; };
    s = s.replace(/`([^`\n]+)`/g, (m, c) => hold(`<code>${c}</code>`));
    s = s.replace(/\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|\$(?!\s)[^$\n]+?\$/g, (m) => hold(m));
    s = s.replace(/\[([^\]\n]+)\]\(((?:https?:\/\/|\/)[^)\s]*)\)/g, (m, t, u) => hold(`<a href="${u}"${u.startsWith('/') ? '' : ' target="_blank" rel="noopener noreferrer"'}>${t}</a>`));
    s = s.replace(/\*\*(?!\s)([\s\S]+?)\*\*/g, '<strong>$1</strong>').replace(/__(?!\s)([\s\S]+?)__/g, '<strong>$1</strong>')
      .replace(/(^|[^*\w])\*(?![\s*])([^*\n]+?)\*(?!\w)/g, '$1<em>$2</em>')
      .replace(/(^|[^_\w])_(?![\s_])([^_\n]+?)_(?!\w)/g, '$1<em>$2</em>')
      .replace(/~~([^~\n]+)~~/g, '<del>$1</del>')
      .replace(/==([^=\n]+)==/g, '<mark>$1</mark>');
    return s.replace(/\u0000(\d+)\u0000/g, (m, n) => keep[+n]).replace(/\u0000(\d+)\u0000/g, (m, n) => keep[+n]);
  }
  const LIST_RE = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
  function list(items, ordered) {
    let html = ordered ? '<ol>' : '<ul>';
    let k = 0;
    while (k < items.length) {
      const it = items[k];
      let j = k + 1;
      const kids = [];
      while (j < items.length && items[j].ind > it.ind + 1) { kids.push(items[j]); j += 1; }
      const task = it.text.match(/^\[( |x|X)\]\s+(.*)$/);
      const body = task ? `<input type="checkbox" disabled${task[1] !== ' ' ? ' checked' : ''}> ${inline(task[2])}` : inline(it.text);
      html += `<li${task ? ' class="task"' : ''}>${body}${kids.length ? list(kids, kids[0].ordered) : ''}</li>`;
      k = j;
    }
    return html + (ordered ? '</ol>' : '</ul>');
  }
  function blocks(lines) {
    const out = [];
    const para = [];
    const flush = () => { if (para.length) { out.push(`<p>${inline(para.join('<br>'))}</p>`); para.length = 0; } };
    const cells = (l) => l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
    let i = 0;
    while (i < lines.length) {
      const l = lines[i];
      let m = l.match(/^\s*```\s*([\w+#.-]*)\s*$/);
      if (m) {
        flush();
        const buf = [];
        i += 1;
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { buf.push(lines[i]); i += 1; }
        i += 1;
        out.push(`<pre><code${m[1] ? ` class="language-${m[1]}"` : ''}>${buf.join('\n')}</code></pre>`);
        continue;
      }
      if (/^\s*\$\$\s*$/.test(l)) {
        flush();
        const buf = [l];
        i += 1;
        while (i < lines.length && !/^\s*\$\$\s*$/.test(lines[i])) { buf.push(lines[i]); i += 1; }
        if (i < lines.length) buf.push(lines[i]);
        i += 1;
        out.push(`<p>${buf.join('\n')}</p>`);
        continue;
      }
      if (!l.trim()) { flush(); i += 1; continue; }
      m = l.match(/^(#{1,6})\s+(.*)$/);
      if (m) { flush(); const n = Math.min(4, m[1].length + 1); out.push(`<h${n}>${inline(m[2])}</h${n}>`); i += 1; continue; }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(l)) { flush(); out.push('<hr>'); i += 1; continue; }
      if (/^&gt;\s?/.test(l)) {
        flush();
        const buf = [];
        while (i < lines.length && /^&gt;\s?/.test(lines[i])) { buf.push(lines[i].replace(/^&gt;\s?/, '')); i += 1; }
        out.push(`<blockquote>${blocks(buf)}</blockquote>`);
        continue;
      }
      if (/^\s*\|.*\|\s*$/.test(l) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        flush();
        const head = cells(l);
        i += 2;
        const rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { rows.push(cells(lines[i])); i += 1; }
        out.push(`<table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join('')}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join('')}</tr>`).join('')}</tbody></table>`);
        continue;
      }
      m = l.match(LIST_RE);
      if (m) {
        flush();
        const items = [];
        while (i < lines.length) {
          const mm = lines[i].match(LIST_RE);
          if (mm) { items.push({ ind: mm[1].length, text: mm[3], ordered: /\d/.test(mm[2]) }); i += 1; } else if (items.length && lines[i].trim() && /^\s{2,}/.test(lines[i])) { items[items.length - 1].text += `<br>${lines[i].trim()}`; i += 1; } else break;
        }
        out.push(list(items, items[0].ordered));
        continue;
      }
      para.push(l);
      i += 1;
    }
    flush();
    return out.join('');
  }
  const md = (src) => blocks(esc(src).split('\n'));

  /* ------------------------------------------------------------------ KaTeX et coloration du code (à la demande) */
  const loading = {};
  const loadScript = (src) => loading[src] || (loading[src] = new Promise((ok, ko) => {
    const s = document.createElement('script'); s.src = src; s.onload = ok; s.onerror = ko; document.head.appendChild(s);
  }));
  const loadCSS = (href) => {
    if (document.querySelector(`link[href="${href}"]`)) return;
    const l = document.createElement('link'); l.rel = 'stylesheet'; l.href = href; document.head.appendChild(l);
  };
  const dark = () => { const t = document.documentElement.dataset.theme; return t === 'dark' || (t !== 'light' && matchMedia('(prefers-color-scheme: dark)').matches); };
  async function typeset(el) {
    if (!el) return;
    if (U.katex && /\$|\\\(|\\\[/.test(el.textContent)) {
      try {
        loadCSS(`${U.katex}katex.min.css`);
        await loadScript(`${U.katex}katex.min.js`);
        await loadScript(`${U.katex}auto-render.min.js`);
        window.renderMathInElement(el, { throwOnError: false, ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code'],
          delimiters: [{ left: '$$', right: '$$', display: true }, { left: '\\[', right: '\\]', display: true }, { left: '$', right: '$', display: false }, { left: '\\(', right: '\\)', display: false }] });
      } catch (e) { /* formules laissées en texte */ }
    }
    if (U.highlight && el.querySelector('pre code')) {
      try {
        loadCSS(`${U.highlight}${dark() ? 'github-dark.min.css' : 'github.min.css'}`);
        await loadScript(`${U.highlight}highlight.min.js`);
        el.querySelectorAll('pre code').forEach((c) => window.hljs.highlightElement(c));
      } catch (e) { /* code non coloré */ }
    }
  }

  function ago(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    const s = (Date.now() - d.getTime()) / 1000;
    if (s < 60) return 'à l’instant';
    if (s < 3600) return `il y a ${Math.floor(s / 60)} min`;
    if (s < 86400) return `il y a ${Math.floor(s / 3600)} h`;
    if (s < 86400 * 7) return `il y a ${Math.floor(s / 86400)} j`;
    return d.toLocaleDateString('fr-FR', { day: 'numeric', month: 'short' });
  }

  async function copyText(t) {
    try { await navigator.clipboard.writeText(t); } catch (e) {
      const ta = document.createElement('textarea'); ta.value = t; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); } catch (e2) { /* ignore */ }
      ta.remove();
    }
    toast('Réponse copiée.', 'success');
  }

  /* ------------------------------------------------------------------ Réponse de l'assistant */
  class Reply {
    constructor(chat, data) {
      this.chat = chat;
      this.raw = '';
      this.mid = null;
      this.finished = false;
      this.el = document.createElement('div');
      this.el.className = 'ai-msg ai-msg-a';
      this.el.innerHTML = `<div class="ai-av">${icon('sparkle', 14)}</div><div class="ai-msg-main"><div class="ai-tools"></div><div class="ai-md"><span class="ai-dots"><i></i><i></i><i></i></span></div><div class="ai-notes"></div><div class="ai-acts"></div><div class="ai-msg-f" hidden></div></div>`;
      this.tools = $('.ai-tools', this.el);
      this.body = $('.ai-md', this.el);
      this.notes = $('.ai-notes', this.el);
      this.acts = $('.ai-acts', this.el);
      this.foot = $('.ai-msg-f', this.el);
      chat.thread.appendChild(this.el);
      this.paint = null;
      if (data) this.fromData(data);
    }
    fromData(m) {
      this.mid = m.id;
      this.raw = m.content || '';
      this.finished = true;
      (m.tools || []).forEach((t) => this.tool({ name: t.name, label: t.label, status: 'done', error: t.error }));
      if (m.status === 'error') { this.body.innerHTML = m.content ? md(m.content) : ''; this.error(m.error || 'Erreur.', null, false); } else {
        this.body.innerHTML = m.html || md(this.raw);
        typeset(this.body);
        this.footer(m.model, m.feedback);
      }
      if (m.status === 'refusal') this.el.classList.add('refusal');
      (m.actions || []).forEach((a) => this.action(a));
    }
    append(t) { this.raw += t; this.render(); }
    truncate(n) { this.raw = this.raw.slice(0, n); this.render(); }
    render() {
      if (this.paint) return;
      this.paint = requestAnimationFrame(() => {
        this.paint = null;
        const near = this.chat.nearBottom();
        this.body.innerHTML = `${md(this.raw)}<span class="ai-cursor"></span>`;
        if (near) this.chat.toBottom();
      });
    }
    tool(d) {
      let line = $$('.ai-tool-l', this.tools).find((x) => x.dataset.name === d.name && x.classList.contains('run'));
      if (!line) {
        line = document.createElement('div');
        line.className = 'ai-tool-l';
        line.dataset.name = d.name;
        this.tools.appendChild(line);
      }
      line.classList.toggle('run', d.status === 'start');
      line.classList.toggle('err', !!d.error);
      line.innerHTML = d.status === 'start'
        ? `<span class="ai-spin"></span>${esc(d.label)}…`
        : `${icon(d.error ? 'alert' : 'check', 12)}${esc(d.label)}${d.error ? ' (sans résultat)' : ''}`;
      this.chat.toBottom(true);
    }
    info(msg) { const n = document.createElement('div'); n.className = 'ai-note'; n.innerHTML = `${icon('zap', 12)} ${esc(msg)}`; this.notes.appendChild(n); }
    action(a) {
      let card = this.acts.querySelector(`[data-aid="${a.id}"]`);
      if (!card) { card = document.createElement('div'); card.dataset.aid = a.id; this.acts.appendChild(card); }
      card.className = `ai-act ${a.status || 'proposed'}`;
      const res = a.result || {};
      let buttons = '';
      if (a.status === 'done') buttons = `<span class="ai-act-ok">${icon('check', 13)} Fait</span>${res.url ? `<a class="btn btn-ghost btn-xs" href="${esc(res.url)}">Ouvrir</a>` : ''}`;
      else if (a.status === 'dismissed') buttons = '<span class="ai-act-ok muted">Écartée</span>';
      else buttons = `<button class="btn btn-primary btn-xs" type="button" data-do>${esc(a.label)}</button><button class="btn btn-ghost btn-xs" type="button" data-skip title="Ignorer">${icon('x', 12)}</button>`;
      card.innerHTML = `<span class="ai-act-ic">${icon(a.icon || 'zap', 14)}</span><div class="ai-act-t"><b>${esc(a.label)}</b><span title="${esc(a.summary)}">${esc(a.summary)}</span>${a.status === 'failed' && res.error ? `<em>${esc(res.error)}</em>` : ''}</div><div class="ai-act-b">${buttons}</div>`;
      const go = $('[data-do]', card);
      if (go) {
        go.addEventListener('click', async () => {
          go.disabled = true;
          try {
            const j = await api(tpl(U.action, a.id), 'POST', {});
            this.action(j.action);
            toast(j.message, 'success');
            if (j.url) { const p = new URL(j.url, location.href); if (p.pathname === location.pathname && H.softReload) H.softReload(700); }
          } catch (e) {
            toast(e.message, 'error');
            if (e.data && e.data.action) this.action(e.data.action); else go.disabled = false;
          }
        });
      }
      const skip = $('[data-skip]', card);
      if (skip) skip.addEventListener('click', async () => { try { const j = await api(tpl(U.dismiss, a.id), 'POST', {}); this.action(j.action); } catch (e) { toast(e.message, 'error'); } });
    }
    footer(model, fb = 0) {
      this.foot.hidden = false;
      this.foot.innerHTML = `<button class="ai-fb" type="button" data-copy title="Copier">${icon('copy', 14)}</button><button class="ai-fb${fb === 1 ? ' on' : ''}" type="button" data-fb="1" title="Bonne réponse">${icon('thumb-up', 14)}</button><button class="ai-fb${fb === -1 ? ' on' : ''}" type="button" data-fb="-1" title="Mauvaise réponse">${icon('thumb-down', 14)}</button>${model ? `<span class="ai-msg-model">${esc(model)}</span>` : ''}`;
      $('[data-copy]', this.foot).addEventListener('click', () => copyText(this.raw));
      $$('[data-fb]', this.foot).forEach((b) => b.addEventListener('click', async () => {
        if (!this.mid) return;
        const v = +b.dataset.fb;
        const next = b.classList.contains('on') ? 0 : v;
        try {
          await api(tpl(U.feedback, this.mid), 'POST', { value: next });
          $$('[data-fb]', this.foot).forEach((x) => x.classList.toggle('on', +x.dataset.fb === next));
          if (next) toast(next > 0 ? 'Merci pour ton retour.' : 'Merci : cette réponse est notée comme à améliorer.', 'info');
        } catch (e) { toast(e.message, 'error'); }
      }));
    }
    done(d) {
      this.finished = true;
      this.mid = d.message_id;
      this.raw = d.text || this.raw;
      this.body.innerHTML = d.html || md(this.raw);
      if (d.status === 'refusal') this.el.classList.add('refusal');
      typeset(this.body);
      this.footer(d.model, 0);
      (d.actions || []).forEach((a) => this.action(a));
      this.chat.toBottom(true);
    }
    error(msg, settings, keep = true) {
      this.finished = true;
      if (keep) this.body.innerHTML = this.raw ? md(this.raw) : '';
      const e = document.createElement('div');
      e.className = 'ai-err';
      e.innerHTML = `${icon('alert', 14)}<div>${esc(msg)}${settings ? ` <a href="${esc(settings)}">Ouvrir les réglages de l’IA</a>` : ''}</div>`;
      this.body.appendChild(e);
      this.chat.toBottom(true);
    }
    stopped() { this.finished = true; this.body.innerHTML = this.raw ? md(this.raw) : ''; this.info('Réponse arrêtée.'); }
  }

  /* ------------------------------------------------------------------ Conversation (panneau ou page) */
  class Chat {
    constructor(root, mode) {
      this.root = root;
      this.mode = mode;
      this.panel = root.closest('.ai-panel');
      this.scroll = $('[data-ai-scroll]', root);
      this.thread = $('[data-ai-thread]', root);
      this.welcome = $('[data-ai-welcome]', root);
      this.hist = $('[data-ai-hist]', root);
      this.form = $('[data-ai-form]', root);
      this.input = $('[data-ai-input]', root);
      this.chips = $('[data-ai-chips]', root);
      this.pop = $('[data-ai-pop]', root);
      this.send = $('[data-ai-send]', root);
      this.convId = null;
      this.page = null;
      this.includePage = true;
      this.selection = '';
      this.mentions = [];
      this.command = '';
      this.busy = false;
      this.ctrl = null;
      this.loaded = false;
      this.popItems = [];
      this.popIndex = 0;
      this.popKind = null;
      this.fetchMentions = debounce((q) => this.fetchMentionsNow(q), 140);
      this.bind();
      if (mode === 'page') {
        this.convId = +root.dataset.conv || null;
        const node = document.getElementById('ai-conv-data');
        let data = null;
        try { data = JSON.parse(node ? node.textContent : 'null'); } catch (e) { /* ignore */ }
        if (data) this.show(data);
        document.addEventListener('hangar:ai-toggle', () => { if (this.input) this.input.focus(); });
        if (this.input) this.input.focus();
      } else {
        this.convId = +store.get('hangar.ai.conv') || null;
        if (document.documentElement.classList.contains('ai-open')) this.onOpen();
        document.addEventListener('hangar:ai-toggle', (e) => { if (e.detail && e.detail.open) this.onOpen(); });
        const grab = () => setTimeout(() => this.grabSelection(), 0);
        document.addEventListener('mouseup', grab);
        document.addEventListener('keyup', (e) => { if (e.shiftKey) grab(); });
      }
    }

    bind() {
      const nb = this.panel ? $('[data-ai-new]', this.panel) : null;
      if (nb) nb.addEventListener('click', () => this.reset());
      const hb = this.panel ? $('[data-ai-history]', this.panel) : null;
      if (hb) hb.addEventListener('click', () => this.toggleHistory());
      $$('[data-ai-cmd]', this.root).forEach((b) => b.addEventListener('click', () => {
        const c = (CFG.commands || []).find((x) => x.id === b.dataset.aiCmd);
        if (c) { this.command = c.id; this.submit(c.prompt); }
      }));
      if (!this.form) return;
      this.form.addEventListener('submit', (e) => { e.preventDefault(); if (this.busy) this.stop(); else this.submit(); });
      this.input.addEventListener('input', () => { this.autosize(); this.detectPop(); });
      this.input.addEventListener('keydown', (e) => this.onKey(e));
      this.input.addEventListener('click', () => this.detectPop());
      this.input.addEventListener('blur', () => setTimeout(() => this.closePop(), 150));
      $('[data-ai-at]', this.form).addEventListener('click', () => this.insertTrigger('@'));
      $('[data-ai-slash]', this.form).addEventListener('click', () => { this.input.value = '/'; this.input.focus(); this.detectPop(); });
      this.chips.addEventListener('click', (e) => {
        const x = e.target.closest('[data-x]');
        const chip = e.target.closest('.ai-chip');
        if (x) {
          const k = x.dataset.x;
          if (k === 'page') this.includePage = false;
          else if (k === 'sel') this.selection = '';
          else this.mentions.splice(+k, 1);
          this.renderChips();
        } else if (chip && chip.classList.contains('off')) { this.includePage = true; this.renderChips(); }
      });
    }

    onOpen() {
      if (!this.loaded) { this.loaded = true; if (this.convId) this.load(this.convId, true); }
      this.refreshContext();
      setTimeout(() => { if (this.input && window.innerWidth > 820) this.input.focus(); }, 60);
    }

    nearBottom() { const s = this.scroll; return s.scrollHeight - s.scrollTop - s.clientHeight < 90; }
    toBottom(force) { if (force === true || this.nearBottom()) this.scroll.scrollTop = this.scroll.scrollHeight; }
    autosize() { const t = this.input; t.style.height = 'auto'; t.style.height = `${Math.min(t.scrollHeight, 200)}px`; }

    /* -- Contexte -- */
    async refreshContext() {
      if (this.mode !== 'panel' || !U.context) return;
      const q = new URLSearchParams({ path: location.pathname, query: location.search, title: document.title });
      try { const j = await api(`${U.context}?${q}`); this.page = j.context; } catch (e) { this.page = null; }
      this.renderChips();
    }
    grabSelection() {
      if (!this.form || !document.documentElement.classList.contains('ai-open')) return;
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed) return;
      const node = sel.anchorNode;
      if (node && this.panel && this.panel.contains(node)) return;
      const t = sel.toString().trim();
      if (t.length < 3) return;
      this.selection = t.slice(0, 8000);
      this.renderChips();
    }
    renderChips() {
      if (!this.chips) return;
      const parts = [];
      if (this.mode === 'panel' && this.page) {
        parts.push(this.includePage
          ? `<span class="ai-chip page" title="La page ouverte est transmise à l’assistant">${icon(this.page.icon || 'page', 13)}<span>${esc(this.page.title)}</span><button type="button" data-x="page" title="Ne pas transmettre la page">${icon('x', 11)}</button></span>`
          : `<span class="ai-chip off" title="Cliquer pour transmettre la page">${icon('plus', 12)}<span>${esc(this.page.title)}</span></span>`);
      }
      if (this.selection) parts.push(`<span class="ai-chip sel" title="${esc(this.selection.slice(0, 300))}">${icon('quote', 13)}<span>Sélection · ${this.selection.length} car.</span><button type="button" data-x="sel" title="Retirer">${icon('x', 11)}</button></span>`);
      this.mentions.forEach((m, n) => parts.push(`<span class="ai-chip" title="${esc(m.subtitle || m.type)}">${icon(m.icon || 'page', 13)}<span>${esc(m.title)}</span><button type="button" data-x="${n}" title="Retirer">${icon('x', 11)}</button></span>`));
      this.chips.innerHTML = parts.join('');
    }

    /* -- « @ » et « / » -- */
    insertTrigger(ch) {
      const t = this.input;
      const pos = t.selectionStart ?? t.value.length;
      const before = t.value.slice(0, pos);
      const pre = before && !/\s$/.test(before) ? ' ' : '';
      t.value = before + pre + ch + t.value.slice(pos);
      t.focus();
      t.selectionStart = t.selectionEnd = pos + pre.length + 1;
      this.detectPop();
    }
    detectPop() {
      const t = this.input;
      const before = t.value.slice(0, t.selectionStart ?? t.value.length);
      let m = before.match(/(^|\s)@([^\s@]{0,40})$/);
      if (m) { this.openMentions(m[2]); return; }
      m = before.match(/^\/([\w-]*)$/);
      if (m) { this.openCommands(m[1]); return; }
      this.closePop();
    }
    openCommands(q) {
      const ql = q.toLowerCase();
      const items = (CFG.commands || []).filter((c) => !ql || c.id.includes(ql) || c.label.toLowerCase().includes(ql));
      this.showPop('cmd', items.map((c) => ({ ...c, title: c.label, subtitle: `/${c.id}` })), 'Commandes');
    }
    openMentions(q) {
      this.mq = q;
      this.fetchMentions(q);
    }
    showPop(kind, items, head) {
      this.popKind = kind;
      this.popItems = items;
      this.popIndex = 0;
      if (!items.length) {
        this.pop.innerHTML = `<div class="ai-pop-empty">${kind === 'cmd' ? 'Aucune commande.' : 'Aucun élément trouvé.'}</div>`;
      } else {
        this.pop.innerHTML = `<div class="ai-pop-h">${esc(head)}</div>${items.map((it, n) => `<button type="button" class="ai-pop-i${n === 0 ? ' on' : ''}" data-n="${n}">${icon(it.icon || 'page', 14)}<span class="t">${esc(it.title)}</span>${it.subtitle ? `<span class="sub">${esc(it.subtitle)}</span>` : ''}</button>`).join('')}`;
        $$('.ai-pop-i', this.pop).forEach((b) => b.addEventListener('mousedown', (e) => { e.preventDefault(); this.pick(+b.dataset.n); }));
      }
      this.pop.hidden = false;
    }
    closePop() { if (this.pop) { this.pop.hidden = true; this.popKind = null; } }
    movePop(d) {
      if (!this.popItems.length) return;
      this.popIndex = (this.popIndex + d + this.popItems.length) % this.popItems.length;
      $$('.ai-pop-i', this.pop).forEach((b, n) => b.classList.toggle('on', n === this.popIndex));
      const on = $('.ai-pop-i.on', this.pop);
      if (on) on.scrollIntoView({ block: 'nearest' });
    }
    pick(n) {
      const it = this.popItems[n];
      if (!it) return;
      const t = this.input;
      const pos = t.selectionStart ?? t.value.length;
      if (this.popKind === 'cmd') {
        t.value = it.prompt;
        this.command = it.id;
      } else {
        const before = t.value.slice(0, pos).replace(/(^|\s)@[^\s@]*$/, '$1');
        t.value = before + t.value.slice(pos);
        t.selectionStart = t.selectionEnd = before.length;
        if (!this.mentions.some((m) => m.url === it.url) && this.mentions.length < 6) this.mentions.push(it);
        this.renderChips();
      }
      this.closePop();
      this.autosize();
      t.focus();
    }
    onKey(e) {
      if (this.popKind && !this.pop.hidden) {
        if (e.key === 'ArrowDown') { e.preventDefault(); this.movePop(1); return; }
        if (e.key === 'ArrowUp') { e.preventDefault(); this.movePop(-1); return; }
        if ((e.key === 'Enter' || e.key === 'Tab') && this.popItems.length) { e.preventDefault(); this.pick(this.popIndex); return; }
        if (e.key === 'Escape') { e.preventDefault(); this.closePop(); return; }
      }
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!this.busy) this.submit(); }
      if (e.key === 'Escape' && this.mode === 'panel' && H.toggleAI && !this.input.value) H.toggleAI(false);
    }

    /* -- Historique -- */
    async toggleHistory(force) {
      const show = force !== undefined ? force : this.hist.hidden;
      this.hist.hidden = !show;
      if (!show) return;
      const list = $('[data-ai-hist-list]', this.hist);
      try {
        const j = await api(U.conversations);
        list.innerHTML = j.conversations.length ? j.conversations.map((c) => `<div class="ai-hist-i${c.id === this.convId ? ' on' : ''}" data-id="${c.id}">${icon('message', 14)}<span class="t">${esc(c.title)}</span><span class="d">${ago(c.updated)}</span><button type="button" class="ai-hist-x" data-del="${c.id}" title="Supprimer">${icon('trash', 13)}</button></div>`).join('') : '<div class="ai-hist-empty">Aucune conversation pour l’instant.</div>';
        $$('.ai-hist-i', list).forEach((row) => row.addEventListener('click', async (e) => {
          const del = e.target.closest('[data-del]');
          if (del) {
            e.stopPropagation();
            if (!confirm('Supprimer cette conversation ?')) return;
            try { await api(tpl(U.deleteConversation, del.dataset.del), 'POST', {}); row.remove(); if (+del.dataset.del === this.convId) this.reset(); } catch (err) { toast(err.message, 'error'); }
            return;
          }
          this.toggleHistory(false);
          this.load(+row.dataset.id);
        }));
      } catch (e) { list.innerHTML = `<div class="ai-hist-empty">${esc(e.message)}</div>`; }
    }

    setConv(c) {
      if (!c || !c.id) return;
      this.convId = c.id;
      if (this.mode === 'panel') store.set('hangar.ai.conv', String(c.id));
      else {
        const title = document.querySelector('[data-ai-title]');
        if (title && c.title) title.textContent = c.title;
        const url = tpl(U.conversationPage, c.id);
        if (url && location.pathname !== url) history.replaceState({}, '', url);
      }
    }
    reset() {
      if (this.ctrl) this.stop();
      this.convId = null;
      this.thread.innerHTML = '';
      this.welcome.hidden = false;
      if (this.hist) this.hist.hidden = true;
      if (this.mode === 'panel') store.set('hangar.ai.conv', null);
      if (this.input) this.input.focus();
    }
    show(conv) {
      this.thread.innerHTML = '';
      this.convId = conv.id;
      (conv.messages || []).forEach((m) => { if (m.role === 'user') this.addUser(m); else new Reply(this, m); });
      this.welcome.hidden = !!(conv.messages || []).length;
      this.toBottom(true);
    }
    async load(id, quiet) {
      try {
        const j = await api(tpl(U.conversation, id));
        this.show(j.conversation);
        this.setConv(j.conversation);
      } catch (e) {
        if (this.mode === 'panel') { this.convId = null; store.set('hangar.ai.conv', null); }
        if (!quiet) toast(e.message, 'error');
      }
    }

    /* -- Envoi -- */
    addUser(m) {
      const el = document.createElement('div');
      el.className = 'ai-msg ai-msg-u';
      const ctx = [];
      if (m.page) ctx.push(`<span class="ai-chip mini">${icon(m.page.icon || 'page', 12)}<span>${esc(m.page.title)}</span></span>`);
      if (m.selection) ctx.push(`<span class="ai-chip mini">${icon('quote', 12)}<span>Sélection · ${m.selection} car.</span></span>`);
      (m.mentions || []).forEach((x) => ctx.push(`<span class="ai-chip mini">${icon(x.icon || 'page', 12)}<span>${esc(x.title)}</span></span>`));
      el.innerHTML = `<div class="ai-u-bubble">${esc(m.content)}</div>${ctx.length ? `<div class="ai-u-ctx">${ctx.join('')}</div>` : ''}`;
      this.thread.appendChild(el);
      return el;
    }
    setBusy(on) {
      this.busy = on;
      if (!this.send) return;
      this.send.classList.toggle('stop', on);
      this.send.innerHTML = icon(on ? 'square' : 'arrow-up', on ? 13 : 16);
      this.send.title = on ? 'Arrêter la réponse' : 'Envoyer (Entrée)';
    }
    stop() { if (this.ctrl) this.ctrl.abort(); }
    async submit(text) {
      if (!this.form || this.busy) return;
      const msg = (text ?? this.input.value).trim();
      if (!msg) return;
      if (this.hist) this.hist.hidden = true;
      this.closePop();
      this.welcome.hidden = true;
      const withPage = this.mode === 'panel' && this.includePage && !!this.page;
      const payload = {
        message: msg, conversation_id: this.convId, path: this.mode === 'panel' ? location.pathname : '',
        query: this.mode === 'panel' ? location.search : '', title: document.title, include_page: withPage,
        selection: this.mode === 'panel' ? this.selection : '', command: this.command,
        mentions: this.mentions.map((m) => ({ url: m.url, title: m.title, type: m.type })),
      };
      this.addUser({ content: msg, page: withPage ? this.page : null, selection: payload.selection.length, mentions: this.mentions });
      this.input.value = '';
      this.autosize();
      this.mentions = [];
      this.selection = '';
      this.command = '';
      this.renderChips();
      const reply = new Reply(this);
      this.toBottom(true);
      this.setBusy(true);
      this.ctrl = new AbortController();
      try {
        const r = await fetch(U.chat, { method: 'POST', body: JSON.stringify(payload), signal: this.ctrl.signal,
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': H.csrf, 'X-Requested-With': 'fetch', Accept: 'text/event-stream' } });
        const ct = r.headers.get('Content-Type') || '';
        if (!ct.includes('event-stream')) {
          let j = {};
          try { j = await r.json(); } catch (e) { /* ignore */ }
          reply.error(j.error || `Erreur ${r.status}`, j.settings);
          return;
        }
        await readSSE(r, (ev, d) => this.onEvent(reply, ev, d));
        if (!reply.finished) reply.error('La réponse s’est interrompue : réessaie.', null);
      } catch (e) {
        if (e.name === 'AbortError') reply.stopped(); else reply.error('Connexion perdue avec Hangar : réessaie.', null);
      } finally {
        this.ctrl = null;
        this.setBusy(false);
      }
    }
    onEvent(reply, ev, d) {
      if (ev === 'start') { this.setConv(d.conversation); }
      else if (ev === 'delta') reply.append(d.text || '');
      else if (ev === 'reset') reply.truncate(d.keep || 0);
      else if (ev === 'tool') reply.tool(d);
      else if (ev === 'action') reply.action(d);
      else if (ev === 'info') reply.info(d.message);
      else if (ev === 'done') { reply.done(d); this.setConv(d.conversation); }
      else if (ev === 'error') reply.error(d.message, d.settings);
    }
    async fetchMentionsNow(q) {
      try {
        const j = await api(`${U.mentions}?${new URLSearchParams({ q })}`);
        if (this.mq !== q) return;
        this.showPop('mention', j.items || [], q ? 'Ajouter au contexte' : 'Suggestions');
      } catch (e) { this.closePop(); }
    }
  }

  /* ------------------------------------------------------------------ Démarrage */
  function start() {
    // Sur téléphone, le panneau couvre tout l'écran : il ne reste pas ouvert d'une page à l'autre.
    if (document.documentElement.classList.contains('ai-open') && matchMedia('(max-width: 820px)').matches && H.toggleAI) H.toggleAI(false);
    document.documentElement.classList.add('ai-ready');
    $$('[data-ai-chat]').forEach((root) => {
      if (root.dataset.aiReady) return;
      root.dataset.aiReady = '1';
      if (root.dataset.aiChat === 'panel' && document.documentElement.classList.contains('ai-fullpage')) return;
      root.aiChat = new Chat(root, root.dataset.aiChat);
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
