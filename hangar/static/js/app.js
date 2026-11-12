/* Hangar — interactions de l'interface (aucune dépendance externe) */
(() => {
  'use strict';
  const H = window.HANGAR || {};
  H.projects = H.projects || {};
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
  const isTyping = (el) => el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));

  /* ------------------------------------------------------------------ Réseau */
  async function api(url, method = 'GET', data) {
    const opts = { method, headers: { 'X-CSRF-Token': H.csrf, 'X-Requested-With': 'fetch' } };
    if (data !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(data); }
    const r = await fetch(url, opts);
    let j = null;
    try { j = await r.json(); } catch (e) { /* réponse non JSON */ }
    if (!r.ok || (j && j.ok === false)) throw new Error((j && j.error) || `Erreur ${r.status}`);
    return j || {};
  }
  async function postForm(url, fields = {}) {
    const fd = new FormData();
    fd.append('_csrf', H.csrf);
    Object.entries(fields).forEach(([k, v]) => fd.append(k, v));
    const r = await fetch(url, { method: 'POST', body: fd, headers: { 'X-Requested-With': 'fetch', 'X-CSRF-Token': H.csrf } });
    let j = null;
    try { j = await r.json(); } catch (e) { /* réponse non JSON */ }
    // Comme api() : le message d'erreur du serveur (« Impossible d'annuler : … ») plutôt que « Erreur 400 »
    if (!r.ok) throw new Error((j && j.error) || `Erreur ${r.status}`);
    return j || {};
  }

  /* ------------------------------------------------------------------ Icônes */
  const svg = (body, size = 14, extra = '') => `<svg class="ic" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" ${extra}>${body}</svg>`;
  const ICON = {
    check: svg('<path d="M20 6 9 17l-5-5"/>'),
    x: svg('<path d="M18 6 6 18"/><path d="m6 6 12 12"/>', 13),
    user: svg('<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>', 13),
    tag: svg('<path d="M12.586 2.586A2 2 0 0 0 11.172 2H4a2 2 0 0 0-2 2v7.172a2 2 0 0 0 .586 1.414l8.704 8.704a2.426 2.426 0 0 0 3.42 0l6.58-6.58a2.426 2.426 0 0 0 0-3.42z"/>', 13),
    cycle: svg('<circle cx="12" cy="12" r="9"/><path d="M12 17a5 5 0 0 0 0-10v10z" fill="currentColor"/>', 13),
    module: svg('<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>', 13),
    estimate: svg('<path d="M12 2 2 7l10 5 10-5-10-5Z"/><path d="m2 17 10 5 10-5"/>', 13),
    plus: svg('<path d="M5 12h14"/><path d="M12 5v14"/>', 15),
    home: svg('<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/>', 15),
    layers: svg('<path d="m12 2 10 5-10 5L2 7z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/>', 15),
    page: svg('<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>', 15),
    users: svg('<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/>', 15),
    chart: svg('<path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/>', 15),
    bell: svg('<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>', 15),
    moon: svg('<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>', 15),
    briefcase: svg('<rect width="20" height="14" x="2" y="7" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/>', 15),
    arrow: svg('<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>', 15),
    flag: svg('<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" x2="4" y1="22" y2="15"/>', 13),
    calendar: svg('<rect width="18" height="18" x="3" y="4" rx="2"/><path d="M16 2v4"/><path d="M8 2v4"/><path d="M3 10h18"/>', 13),
    success: svg('<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>', 16),
    error: svg('<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>', 16),
    info: svg('<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/>', 16),
  };
  const GROUP_COLORS = { backlog: '#a3a3a3', unstarted: '#3f76ff', started: '#f59e0b', completed: '#16a34a', cancelled: '#dc2626' };
  function stateIcon(group, color, size = 14) {
    color = esc(color || GROUP_COLORS[group] || '#a3a3a3'); // couleur saisie (état d'un projet) : échappée
    let inner;
    if (group === 'backlog') inner = `<circle cx="12" cy="12" r="9" stroke="${color}" stroke-dasharray="3.2 3"/>`;
    else if (group === 'unstarted') inner = `<circle cx="12" cy="12" r="9" stroke="${color}"/>`;
    else if (group === 'started') inner = `<circle cx="12" cy="12" r="9" stroke="${color}"/><path d="M12 6.5a5.5 5.5 0 0 1 0 11z" fill="${color}" stroke="none"/>`;
    else if (group === 'completed') inner = `<circle cx="12" cy="12" r="10" fill="${color}" stroke="none"/><path d="m7.5 12.5 3 3 6-6.5" stroke="#fff" stroke-width="2.4"/>`;
    else inner = `<circle cx="12" cy="12" r="10" fill="${color}" stroke="none"/><path d="m9 9 6 6M15 9l-6 6" stroke="#fff" stroke-width="2.4"/>`;
    return `<svg class="ic" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round">${inner}</svg>`;
  }
  const PRIO_COLORS = { urgent: '#ef4444', high: '#f97316', medium: '#eab308', low: '#3b82f6', none: '#a3a3a3' };
  function prioIcon(p, size = 14) {
    const c = PRIO_COLORS[p] || '#a3a3a3';
    let inner;
    if (p === 'urgent') inner = `<rect x="2" y="2" width="20" height="20" rx="5" fill="${c}"/><path d="M12 6.5v7" stroke="#fff" stroke-width="2.6" stroke-linecap="round"/><circle cx="12" cy="17.3" r="1.5" fill="#fff"/>`;
    else if (p === 'none' || !p) inner = `<rect x="2.5" y="2.5" width="19" height="19" rx="5" fill="none" stroke="${c}" stroke-width="1.6" stroke-dasharray="3 2.4"/><path d="M8 12h8" stroke="${c}" stroke-width="2" stroke-linecap="round"/>`;
    else {
      const filled = { high: 3, medium: 2, low: 1 }[p] || 0;
      inner = [[3.5, 14, 6.5], [10.25, 9, 11.5], [17, 4, 16.5]].map(([x, y, h], i) => `<rect x="${x}" y="${y}" width="4" height="${h}" rx="1.2" fill="${c}" opacity="${i < filled ? 1 : 0.25}"/>`).join('');
    }
    return `<svg class="ic" width="${size}" height="${size}" viewBox="0 0 24 24">${inner}</svg>`;
  }
  const avatarHTML = (m, size = 18) => `<span class="avatar" style="--av:${esc(m.color)};width:${size}px;height:${size}px;font-size:${Math.round(size * 0.42)}px">${esc(m.initials)}</span>`;

  /* ------------------------------------------------------------------ Toasts */
  function toast(msg, type = 'info', ms = 4200, action = null) {
    const box = $('#toasts');
    if (!box) return;
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.innerHTML = `${ICON[type] || ICON.info}<div>${esc(msg)}</div>`;
    if (action) { // bouton dans le message (ex. « Annuler »)
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'btn btn-xs'; b.style.marginLeft = 'auto'; b.textContent = action.label;
      b.addEventListener('click', () => { el.remove(); action.run(); });
      el.appendChild(b);
    }
    box.appendChild(el);
    setTimeout(() => { el.classList.add('hiding'); setTimeout(() => el.remove(), 300); }, ms);
  }
  $$('[data-flash]').forEach((el, i) => setTimeout(() => { el.classList.add('hiding'); setTimeout(() => el.remove(), 300); }, 5000 + i * 500));
  H.toast = toast;

  /* ------------------------------------------------------------------ Rechargement doux (conserve le défilement) */
  const SCROLLERS = ['.content', '.issue-main', '.board', '.sheet-wrap', '.g-right', '.intake-list', '.intake-detail'];
  function softReload(delay = 0) {
    const pos = {};
    SCROLLERS.forEach((s) => { const el = $(s); if (el) pos[s] = [el.scrollLeft, el.scrollTop]; });
    $$('.bcol-body').forEach((el, i) => { pos[`bcol${i}`] = [0, el.scrollTop]; });
    sessionStorage.setItem('hangar.scroll', JSON.stringify({ path: location.pathname + location.search, pos }));
    setTimeout(() => location.reload(), delay);
  }
  H.softReload = softReload;
  try {
    const saved = JSON.parse(sessionStorage.getItem('hangar.scroll') || 'null');
    if (saved && saved.path === location.pathname + location.search) {
      Object.entries(saved.pos).forEach(([s, [x, y]]) => {
        const el = s.startsWith('bcol') ? $$('.bcol-body')[+s.slice(4)] : $(s);
        if (el) { el.scrollLeft = x; el.scrollTop = y; }
      });
    }
    sessionStorage.removeItem('hangar.scroll');
  } catch (e) { /* ignore */ }

  /* ------------------------------------------------------------------ Menus déroulants */
  document.addEventListener('click', (e) => {
    const trigger = e.target.closest('[data-menu]');
    if (trigger) {
      e.preventDefault();
      const dd = trigger.closest('.dd');
      const wasOpen = dd.classList.contains('open');
      $$('.dd.open').forEach((d) => d.classList.remove('open'));
      if (!wasOpen) {
        dd.classList.add('open');
        const menu = $('.menu', dd);
        if (menu) {
          menu.classList.remove('up');
          const r = menu.getBoundingClientRect();
          if (r.bottom > window.innerHeight - 8 && r.top > r.height) menu.classList.add('up');
          const inp = $('[data-opt-filter]', menu);
          if (inp) setTimeout(() => inp.focus(), 30);
        }
      }
      return;
    }
    if (!e.target.closest('.dd .menu')) $$('.dd.open').forEach((d) => d.classList.remove('open'));
    else if (e.target.closest('.menu-item') && !e.target.closest('form')) $$('.dd.open').forEach((d) => d.classList.remove('open'));
  });

  // Filtre textuel dans les menus d'options
  document.addEventListener('input', (e) => {
    if (!e.target.matches('[data-opt-filter]')) return;
    const q = e.target.value.trim().toLowerCase();
    const form = e.target.closest('form');
    $$('.opt[data-opt-text]', form).forEach((o) => { o.hidden = q && !o.dataset.optText.includes(q); });
    $$('.pm-section', form).forEach((sec) => {
      const opts = $$('.opt[data-opt-text]', sec);
      if (opts.length) sec.hidden = opts.every((o) => o.hidden);
    });
  });
  document.addEventListener('change', (e) => {
    if (e.target.matches('[data-autosubmit]')) e.target.form.submit();
  });

  /* ------------------------------------------------------------------ Modales */
  document.addEventListener('click', (e) => {
    const op = e.target.closest('[data-open]');
    if (op) { e.preventDefault(); const d = document.getElementById(op.dataset.open); if (d) { $$('.dd.open').forEach((x) => x.classList.remove('open')); d.showModal(); const f = $('input:not([type=hidden]), textarea', d); if (f) f.focus(); } return; }
    const cl = e.target.closest('[data-close]');
    if (cl) { const d = cl.closest('dialog'); if (d) d.close(); return; }
    if (e.target.tagName === 'DIALOG' && e.target.open) {
      const r = e.target.getBoundingClientRect();
      if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) e.target.close();
    }
  });
  document.addEventListener('submit', (e) => {
    const f = e.target;
    if (f.dataset.confirm && !confirm(f.dataset.confirm)) e.preventDefault();
  }, true);
  document.addEventListener('click', (e) => {
    const t = e.target.closest('[data-toggle]');
    if (!t) return;
    const el = $(t.dataset.toggle);
    if (el) { el.classList.toggle('hide'); const f = $('input:not([type=hidden]), textarea', el); if (f && !el.classList.contains('hide')) f.focus(); }
  });

  /* ------------------------------------------------------------------ Popover de sélection */
  let pop = null;
  function closePop() {
    if (!pop) return;
    const p = pop;
    pop = null;
    p.el.remove();
    if (p.onClose) p.onClose();
  }
  function placePop(el, anchor) {
    const r = anchor.getBoundingClientRect();
    const w = el.offsetWidth, h = el.offsetHeight;
    let left = Math.min(r.left, window.innerWidth - w - 8);
    let top = r.bottom + 4;
    if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 4);
    el.style.left = `${Math.max(8, left)}px`;
    el.style.top = `${top}px`;
  }
  function openPop(anchor, cfg) {
    closePop();
    const el = document.createElement('div');
    el.className = 'pop';
    el.innerHTML = `${cfg.noSearch ? '' : `<input class="pop-search" placeholder="${esc(cfg.placeholder || 'Rechercher…')}">`}<div class="pop-list"></div>`;
    document.body.appendChild(el);
    const list = $('.pop-list', el);
    const input = $('.pop-search', el);
    const selected = cfg.selected || new Set();
    let kb = cfg.noSearch ? 0 : -1;
    let shown = [];
    const render = () => {
      const q = input ? input.value.trim().toLowerCase() : '';
      shown = cfg.options.filter((o) => !q || o.label.toLowerCase().includes(q) || (o.hint || '').toLowerCase().includes(q));
      list.innerHTML = shown.length ? shown.map((o, i) => `<div class="pop-opt ${selected.has(o.id) ? 'sel' : ''} ${i === kb ? 'kb' : ''}" data-i="${i}">${o.icon || ''}<span class="ellipsis grow">${esc(o.label)}</span>${o.hint ? `<span class="muted tiny nowrap">${esc(o.hint)}</span>` : ''}<span class="check">${ICON.check}</span></div>`).join('') : '<div class="pop-empty">Aucun résultat</div>';
      const cur = $('.pop-opt.kb', list);
      if (cur) cur.scrollIntoView({ block: 'nearest' });
    };
    const choose = (o) => {
      if (!o) return;
      if (cfg.multi) {
        if (selected.has(o.id)) selected.delete(o.id); else selected.add(o.id);
        render();
        cfg.onPick(o, selected);
      } else {
        closePop();
        cfg.onPick(o, selected);
      }
    };
    list.addEventListener('mousedown', (ev) => {
      const it = ev.target.closest('.pop-opt');
      if (it) { ev.preventDefault(); choose(shown[+it.dataset.i]); }
    });
    const keys = (ev) => {
      if (ev.key === 'ArrowDown') { kb = Math.min(kb + 1, shown.length - 1); render(); ev.preventDefault(); return true; }
      if (ev.key === 'ArrowUp') { kb = Math.max(kb - 1, 0); render(); ev.preventDefault(); return true; }
      if (ev.key === 'Enter' || (cfg.noSearch && ev.key === 'Tab')) { ev.preventDefault(); choose(shown[Math.max(kb, 0)]); return true; }
      if (ev.key === 'Escape') { ev.preventDefault(); closePop(); return true; }
      return false;
    };
    if (input) {
      input.addEventListener('input', () => { kb = 0; render(); });
      input.addEventListener('keydown', keys);
    }
    render();
    placePop(el, anchor);
    if (input) input.focus();
    pop = { el, anchor, onClose: cfg.onClose, keys, mention: !!cfg.mention };
    return pop;
  }
  document.addEventListener('mousedown', (e) => { if (pop && !pop.el.contains(e.target) && !pop.anchor.contains(e.target)) closePop(); });
  window.addEventListener('resize', closePop);
  document.addEventListener('scroll', (e) => { if (pop && !pop.el.contains(e.target) && !pop.mention) closePop(); }, true);

  /* ------------------------------------------------------------------ Options des propriétés */
  const PRIORITIES = [['urgent', 'Urgente'], ['high', 'Haute'], ['medium', 'Moyenne'], ['low', 'Basse'], ['none', 'Aucune']];
  const MULTI = new Set(['assignees', 'labels', 'modules']);
  const FIELD = { state: 'state_id', priority: 'priority', assignees: 'assignee_ids', labels: 'label_ids', cycle: 'cycle_id', modules: 'module_ids', estimate: 'estimate' };
  const GROUP_OF = { state: ['state', 'state_group'], priority: ['priority'], assignees: ['assignee'], labels: ['label'], cycle: ['cycle'], modules: ['module'] };
  const PLACEHOLDER = { state: 'État', priority: 'Priorité', assignees: 'Assigner', labels: 'Étiquettes', cycle: 'Cycle', modules: 'Module', estimate: 'Estimation' };
  const CYCLE_STATUS = { current: 'en cours', upcoming: 'à venir', draft: 'brouillon', completed: 'terminé' };

  function optionsFor(kind, pid) {
    const p = H.projects[pid];
    if (!p) return [];
    switch (kind) {
      case 'state': return p.states.map((s) => ({ id: String(s.id), label: s.name, icon: stateIcon(s.group, s.color) }));
      case 'priority': return PRIORITIES.map(([k, l]) => ({ id: k, label: l, icon: prioIcon(k) }));
      case 'assignees': return p.members.map((m) => ({ id: String(m.id), label: m.name, icon: avatarHTML(m) }));
      case 'labels': return p.labels.map((l) => ({ id: String(l.id), label: l.name, icon: `<span class="dot" style="background:${esc(l.color)}"></span>` }));
      case 'cycle': return [{ id: '', label: 'Aucun cycle', icon: ICON.x }].concat(p.all_cycles.map((c) => ({ id: String(c.id), label: c.name, icon: ICON.cycle, hint: CYCLE_STATUS[c.status] })));
      case 'modules': return p.modules.map((m) => ({ id: String(m.id), label: m.name, icon: ICON.module }));
      case 'estimate': return [{ id: '', label: 'Aucune', icon: ICON.x }].concat(p.estimates.map((v) => ({ id: String(v.id), label: v.name, icon: ICON.estimate })));
      default: return [];
    }
  }
  function valueFromIssue(kind, is) {
    switch (kind) {
      case 'state': return is.state ? String(is.state.id) : '';
      case 'priority': return is.priority;
      case 'assignees': return is.assignees.map((u) => u.id).join(',');
      case 'labels': return is.labels.map((l) => l.id).join(',');
      case 'cycle': return is.cycle ? String(is.cycle.id) : '';
      case 'modules': return is.modules.map((m) => m.id).join(',');
      case 'estimate': return is.estimate == null ? '' : String(is.estimate);
      default: return '';
    }
  }
  function isEmpty(kind, is) {
    return (kind === 'assignees' && !is.assignees.length) || (kind === 'labels' && !is.labels.length) || (kind === 'cycle' && !is.cycle)
      || (kind === 'modules' && !is.modules.length) || (kind === 'estimate' && is.estimate == null) || (kind === 'priority' && is.priority === 'none');
  }
  function applyIssue(iid, r) {
    if (!r || !r.props) return;
    Object.entries(r.props).forEach(([kind, html]) => {
      $$(`.prop[data-issue="${iid}"][data-pick="${kind}"]`).forEach((b) => {
        b.innerHTML = html;
        if (r.issue) { b.dataset.value = valueFromIssue(kind, r.issue); b.classList.toggle('is-empty', isEmpty(kind, r.issue)); }
      });
    });
    if (r.issue) {
      const closed = r.issue.state && ['completed', 'cancelled'].includes(r.issue.state.group);
      $$(`[data-id="${iid}"]`).forEach((row) => row.classList.toggle('done', !!closed));
      $$(`input[type=date][data-issue="${iid}"][data-field="target_date"]`).forEach((inp) => markDate(inp, r.issue));
    }
  }
  function currentGroupBy(anchor) {
    const root = anchor.closest('[data-group-by]') || $('[data-group-by]');
    return root ? root.dataset.groupBy : null;
  }
  function needsRegroup(kind, anchor) {
    const gb = currentGroupBy(anchor);
    return gb && (GROUP_OF[kind] || []).includes(gb) && anchor.closest('[data-group-key]');
  }

  async function patchIssue(iid, payload) {
    const r = await api(`/api/issues/${iid}`, 'PATCH', payload);
    applyIssue(iid, r);
    return r;
  }

  function openIssuePicker(btn) {
    const kind = btn.dataset.pick;
    const pid = btn.dataset.project;
    const iid = btn.dataset.issue;
    const multi = MULTI.has(kind);
    const vals = (btn.dataset.value || '').split(',').filter((v) => v !== '');
    const selected = new Set(multi ? vals : [btn.dataset.value || '']);
    let dirty = false;
    openPop(btn, {
      options: optionsFor(kind, pid),
      selected,
      multi,
      placeholder: `${PLACEHOLDER[kind]}…`,
      onPick: async (o, sel) => {
        const value = multi ? Array.from(sel).map(Number) : (o.id === '' ? null : o.id);
        try {
          const r = await patchIssue(iid, { [FIELD[kind]]: value });
          if (r.changed && r.changed.length) dirty = true;
          if (!multi && dirty && needsRegroup(kind, btn)) softReload(250);
          else if (!multi && dirty && $('.issue-layout')) softReload(150);
        } catch (err) { toast(err.message, 'error'); }
      },
      onClose: () => { if (multi && dirty && (needsRegroup(kind, btn) || $('.issue-layout'))) softReload(100); },
    });
  }

  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.prop[data-pick]');
    if (btn && !btn.disabled) { e.preventDefault(); e.stopPropagation(); openIssuePicker(btn); return; }
    const lbl = e.target.closest('.prop-date');
    if (lbl) {
      const inp = $('input[type=date]', lbl);
      if (inp && !inp.disabled && typeof inp.showPicker === 'function' && e.target !== inp) {
        e.preventDefault();
        try { inp.showPicker(); } catch (err) { inp.focus(); }
      }
    }
  });

  /* ------------------------------------------------------------------ Dates */
  const fmtDate = (v) => {
    if (!v) return '';
    const d = new Date(`${v}T00:00:00`);
    const opts = { day: 'numeric', month: 'short' };
    if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
    return d.toLocaleDateString('fr-FR', opts);
  };
  function markDate(inp, issue) {
    const lab = inp.closest('.prop');
    if (!lab) return;
    const v = inp.value;
    const span = $('[data-date-label]', lab);
    if (span) span.textContent = v ? fmtDate(v) : (inp.dataset.field === 'start_date' ? 'Début' : 'Échéance');
    lab.classList.toggle('is-empty', !v);
    if (inp.dataset.field === 'target_date') {
      const closed = issue && issue.state && ['completed', 'cancelled'].includes(issue.state.group);
      const today = new Date().toISOString().slice(0, 10);
      lab.classList.toggle('is-overdue', !!v && v < today && !closed);
    }
  }
  document.addEventListener('change', async (e) => {
    const inp = e.target;
    if (inp.matches('input[type=date][data-issue]')) {
      try {
        const r = await patchIssue(inp.dataset.issue, { [inp.dataset.field]: inp.value || null });
        $$(`input[type=date][data-issue="${inp.dataset.issue}"][data-field="${inp.dataset.field}"]`).forEach((o) => { o.value = inp.value; markDate(o, r.issue); });
        toast(inp.value ? `Date enregistrée : ${fmtDate(inp.value)}` : 'Date retirée', 'success', 2200);
        if ($('[data-calendar]') || $('[data-gantt]')) softReload(300);
      } catch (err) { toast(err.message, 'error'); }
    } else if (inp.matches('[data-form-date]')) {
      const lab = inp.closest('.prop');
      $('[data-date-label]', lab).textContent = inp.value ? fmtDate(inp.value) : (inp.name === 'start_date' ? 'Début' : 'Échéance');
      lab.classList.toggle('is-empty', !inp.value);
    } else if (inp.matches('[data-set-parent]')) {
      try { await patchIssue(inp.dataset.setParent, { parent_id: inp.value || null }); softReload(); } catch (err) { toast(err.message, 'error'); }
    }
  });

  /* ------------------------------------------------------------------ Création de ticket */
  const ciModal = $('#create-issue-modal');
  function renderFormPick(box, pid) {
    const kind = box.dataset.formPick;
    const multi = box.dataset.multi === '1';
    const vals = (box.dataset.value || '').split(',').filter((v) => v !== '');
    const opts = optionsFor(kind, pid);
    const chosen = opts.filter((o) => vals.includes(o.id) && o.id !== '');
    let label = PLACEHOLDER[kind];
    let ic = { state: stateIcon('unstarted'), priority: prioIcon('none'), assignees: ICON.user, labels: ICON.tag, cycle: ICON.cycle, modules: ICON.module, estimate: ICON.estimate }[kind];
    if (chosen.length) {
      label = multi && chosen.length > 1 ? (kind === 'assignees' ? `${chosen.length} personnes` : `${chosen.length} sélectionnés`) : chosen[0].label;
      ic = multi && kind === 'assignees' ? `<span class="avatars">${chosen.slice(0, 3).map((c) => c.icon).join('')}</span>` : chosen[0].icon;
    }
    const empty = !chosen.length || (kind === 'priority' && vals[0] === 'none');
    box.innerHTML = `<button type="button" class="prop ${empty ? 'is-empty' : ''}">${ic}<span class="pl">${esc(label)}</span></button>${vals.map((v) => `<input type="hidden" name="${esc(box.dataset.name)}" value="${esc(v)}">`).join('')}`;
  }
  function initFormPickers(form, pid, d = {}) {
    const p = H.projects[pid];
    if (!p) return;
    $$('[data-form-pick]', form).forEach((box) => {
      const kind = box.dataset.formPick;
      let vals = [];
      if (kind === 'state') vals = [String(d.state || p.default_state || '')];
      else if (kind === 'priority') vals = [d.priority || 'none'];
      else if (kind === 'cycle' && d.cycle) vals = [String(d.cycle)];
      else if (kind === 'modules' && d.module) vals = [String(d.module)];
      else if (kind === 'assignees' && d.assignee) vals = [String(d.assignee)];
      else if (kind === 'labels' && d.label) vals = [String(d.label)];
      box.dataset.value = vals.join(',');
      renderFormPick(box, pid);
      box.hidden = (kind === 'cycle' && !p.all_cycles.length) || (kind === 'modules' && !p.modules.length)
        || (kind === 'estimate' && !p.estimates.length) || (kind === 'labels' && !p.labels.length)
        || (d.intake && ['state', 'cycle', 'modules', 'estimate'].includes(kind));
    });
    $$('[data-form-date]', form).forEach((inp) => {
      inp.value = inp.name === 'target_date' && d.target ? d.target : '';
      inp.dispatchEvent(new Event('change', { bubbles: true }));
    });
  }
  function openCreate(d = {}) {
    if (!ciModal) return;
    const form = $('[data-create-form]', ciModal);
    const sel = $('[data-ci-project]', form);
    if (!sel.options.length) { toast('Aucun projet où tu peux créer de ticket.', 'error'); return; }
    let pid = d.project || H.contextProject || localStorage.getItem('hangar.lastProject');
    if (!pid || !$(`option[value="${pid}"]`, sel)) pid = sel.options[0].value;
    sel.value = pid;
    form.title.value = '';
    form.description.value = '';
    const ed = $('[data-md-editor]', form);
    if (ed) ed.classList.remove('previewing');
    form.parent_id.value = d.parent || '';
    form.intake.value = d.intake ? '1' : '';
    $('[data-ci-parent-badge]', form).classList.toggle('hide', !d.parent);
    $('[data-ci-heading]', form).textContent = d.intake ? 'Nouvelle demande (intake)' : (d.parent ? 'Nouveau sous-ticket' : 'Nouveau ticket');
    $('[data-ci-submit]', form).textContent = d.intake ? 'Envoyer la demande' : 'Créer le ticket';
    sel.disabled = !!d.parent;
    form.dataset.defaults = JSON.stringify(d);
    initFormPickers(form, pid, d);
    ciModal.showModal();
    setTimeout(() => form.title.focus(), 30);
  }
  H.openCreate = openCreate;
  if (ciModal) {
    const form = $('[data-create-form]', ciModal);
    const sel = $('[data-ci-project]', form);
    sel.addEventListener('change', () => initFormPickers(form, sel.value, JSON.parse(form.dataset.defaults || '{}').intake ? { intake: true } : {}));
    form.addEventListener('submit', () => {
      sel.disabled = false;
      localStorage.setItem('hangar.lastProject', sel.value);
      localStorage.setItem('hangar.createMore', $('[data-create-more]', form).checked ? JSON.stringify(JSON.parse(form.dataset.defaults || '{}')) : '');
    });
    form.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-form-pick] .prop');
      if (!btn) return;
      const box = btn.closest('[data-form-pick]');
      const kind = box.dataset.formPick;
      const multi = box.dataset.multi === '1';
      const vals = (box.dataset.value || '').split(',').filter((v) => v !== '');
      openPop(btn, {
        options: optionsFor(kind, sel.value), selected: new Set(multi ? vals : [vals[0] || '']), multi,
        placeholder: `${PLACEHOLDER[kind]}…`,
        onPick: (o, s) => {
          box.dataset.value = multi ? Array.from(s).join(',') : o.id;
          if (multi) { const keep = pop; renderFormPick(box, sel.value); if (keep) keep.anchor = $('.prop', box); }
          else renderFormPick(box, sel.value);
        },
      });
    });
    if (location.hash === '#create-more') {
      try {
        const d = JSON.parse(localStorage.getItem('hangar.createMore') || 'null');
        if (d) { openCreate(d); $('[data-create-more]', form).checked = true; }
      } catch (e) { /* ignore */ }
      history.replaceState(null, '', location.pathname + location.search);
    }
  }
  document.addEventListener('click', (e) => {
    const t = e.target.closest('[data-create-issue]');
    if (t) { e.preventDefault(); $$('.dd.open').forEach((x) => x.classList.remove('open')); openCreate({ ...t.dataset }); }
  });

  /* ------------------------------------------------------------------ Glisser-déposer : liste & kanban */
  let drag = null;
  $$('[data-dnd-root] a, [data-calendar] a.cal-chip *').forEach((a) => a.setAttribute('draggable', 'false'));
  document.addEventListener('dragstart', (e) => {
    const chip = e.target.closest('.cal-chip[draggable=true]');
    if (chip) { drag = { item: chip, calendar: true }; chip.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', chip.dataset.id); return; }
    const item = e.target.closest('[data-dnd-root] [data-id][draggable=true]');
    if (!item) return;
    const root = item.closest('[data-dnd-root]');
    const grp = item.closest('[data-group-key]');
    drag = { item, root, fromKey: grp ? grp.dataset.groupKey : null, fromParent: item.parentElement, fromNext: item.nextElementSibling };
    item.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', item.dataset.id);
  });
  function afterElement(container, y) {
    const els = $$(':scope > [data-id]:not(.dragging)', container);
    let best = { offset: -Infinity, el: null };
    els.forEach((c) => {
      const box = c.getBoundingClientRect();
      const off = y - box.top - box.height / 2;
      if (off < 0 && off > best.offset) best = { offset: off, el: c };
    });
    return best.el;
  }
  document.addEventListener('dragover', (e) => {
    if (!drag) return;
    if (drag.calendar) {
      const cell = e.target.closest('[data-drop-date]');
      $$('.drop-target').forEach((c) => c !== cell && c.classList.remove('drop-target'));
      if (cell) { e.preventDefault(); cell.classList.add('drop-target'); }
      return;
    }
    const zone = e.target.closest('[data-drop]');
    if (!zone || !drag.root.contains(zone)) return;
    const grp = zone.closest('[data-group-key]');
    if (drag.root.dataset.dnd !== '1' && grp && grp.dataset.groupKey !== drag.fromKey) return;
    e.preventDefault();
    const after = afterElement(zone, e.clientY);
    if (after == null) zone.appendChild(drag.item); else if (after !== drag.item) zone.insertBefore(drag.item, after);
  });
  document.addEventListener('drop', async (e) => {
    if (!drag) return;
    e.preventDefault();
    if (drag.calendar) {
      const cell = e.target.closest('[data-drop-date]');
      const chip = drag.item;
      $$('.drop-target').forEach((c) => c.classList.remove('drop-target'));
      if (cell) {
        cell.appendChild(chip);
        try { await patchIssue(chip.dataset.id, { target_date: cell.dataset.date }); toast(`Échéance déplacée au ${fmtDate(cell.dataset.date)}`, 'success', 2200); } catch (err) { toast(err.message, 'error'); softReload(); }
      }
    }
  });
  document.addEventListener('dragend', async () => {
    if (!drag) return;
    const d = drag;
    drag = null;
    d.item.classList.remove('dragging');
    $$('.drop-target').forEach((c) => c.classList.remove('drop-target'));
    if (d.calendar) return;
    const grp = d.item.closest('[data-group-key]');
    if (!grp) return;
    const toKey = grp.dataset.groupKey;
    const prev = d.item.previousElementSibling && d.item.previousElementSibling.matches('[data-id]') ? d.item.previousElementSibling.dataset.id : null;
    const next = d.item.nextElementSibling && d.item.nextElementSibling.matches('[data-id]') ? d.item.nextElementSibling.dataset.id : null;
    if (d.item.parentElement === d.fromParent && d.item.nextElementSibling === d.fromNext) return; // rien n'a bougé
    try {
      const r = await api(`/api/issues/${d.item.dataset.id}/move`, 'POST', { group_by: d.root.dataset.groupBy, from: d.fromKey, to: toKey, prev, next });
      applyIssue(d.item.dataset.id, r);
      updateCounts(d.root);
      if (d.fromKey !== toKey) {
        toast('Ticket déplacé', 'success', 1800);
        if (['assignee', 'label', 'module'].includes(d.root.dataset.groupBy)) softReload(400);
      }
    } catch (err) { toast(err.message, 'error'); softReload(); }
  });
  function updateCounts(root) {
    $$('[data-group-key]', root).forEach((g) => {
      const c = $('.count-pill', g);
      const body = $('[data-drop]', g);
      if (c && body) c.textContent = $$(':scope > [data-id]', body).length;
    });
  }

  /* ------------------------------------------------------------------ Ajout rapide */
  function openQuick(qa) {
    $$('.quick-add.open').forEach((x) => x !== qa && x.classList.remove('open'));
    qa.classList.add('open');
    const inp = $('input', qa);
    inp.focus();
  }
  document.addEventListener('click', (e) => {
    const qo = e.target.closest('[data-quick-open]');
    if (qo) { const g = qo.closest('[data-group-key]'); const qa = g && $('[data-quick-add]', g); if (qa) { g.classList.remove('collapsed'); openQuick(qa); } return; }
    const qb = e.target.closest('.qa-open');
    if (qb) openQuick(qb.closest('[data-quick-add]'));
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && e.target.closest && e.target.closest('[data-quick-add]')) e.target.closest('[data-quick-add]').classList.remove('open');
  });
  document.addEventListener('submit', async (e) => {
    const qa = e.target.closest('[data-quick-add]');
    if (!qa) return;
    e.preventDefault();
    const inp = $('input', qa);
    const title = inp.value.trim();
    if (!title) return;
    inp.disabled = true;
    try {
      const payload = { project_id: qa.dataset.project, group_by: qa.dataset.groupBy, group_key: qa.dataset.groupKey, title };
      if (qa.dataset.cycle) payload.cycle_id = qa.dataset.cycle;
      if (qa.dataset.module) payload.module_ids = [qa.dataset.module];
      const r = await api('/api/issues/quick', 'POST', payload);
      sessionStorage.setItem('hangar.quickReopen', qa.dataset.groupKey);
      toast(`${r.key} créé`, 'success', 1500);
      softReload();
    } catch (err) { toast(err.message, 'error'); inp.disabled = false; }
  });
  const reopen = sessionStorage.getItem('hangar.quickReopen');
  if (reopen !== null) {
    sessionStorage.removeItem('hangar.quickReopen');
    const g = $(`[data-group-key="${CSS.escape(reopen)}"] [data-quick-add]`);
    if (g) openQuick(g);
  }

  /* ------------------------------------------------------------------ Replier / préférences d'affichage */
  const collapsed = new Set(JSON.parse(localStorage.getItem('hangar.collapsed') || '[]'));
  $$('[data-collapse-key]').forEach((g) => { if (collapsed.has(g.dataset.collapseKey)) g.classList.add('collapsed'); });
  document.addEventListener('click', (e) => {
    const t = e.target.closest('[data-collapse]');
    if (!t) return;
    const g = t.closest('[data-collapse-key]');
    g.classList.toggle('collapsed');
    if (g.classList.contains('collapsed')) collapsed.add(g.dataset.collapseKey); else collapsed.delete(g.dataset.collapseKey);
    localStorage.setItem('hangar.collapsed', JSON.stringify(Array.from(collapsed).slice(-300)));
  });
  const hidden = new Set(JSON.parse(localStorage.getItem('hangar.hiddenProps') || '["cycle","start"]'));
  const applyHidden = () => {
    $$('[data-issues-root]').forEach((r) => {
      if (r.closest('.issue-side')) return;
      ['key', 'state', 'priority', 'assignees', 'labels', 'start', 'target', 'estimate', 'cycle', 'modules', 'sub'].forEach((k) => r.classList.toggle(`hide-p-${k}`, hidden.has(k)));
    });
    $$('[data-prop-toggle]').forEach((b) => b.classList.toggle('active', !hidden.has(b.dataset.propToggle)));
  };
  applyHidden();
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-prop-toggle]');
    if (!b) return;
    const k = b.dataset.propToggle;
    if (hidden.has(k)) hidden.delete(k); else hidden.add(k);
    localStorage.setItem('hangar.hiddenProps', JSON.stringify(Array.from(hidden)));
    applyHidden();
  });

  /* ------------------------------------------------------------------ Tableur : sélection et actions groupées */
  const bulkForm = $('[data-bulk-form]');
  if (bulkForm) {
    const bar = $('[data-bulkbar]', bulkForm);
    const count = $('[data-bulk-count]', bulkForm);
    const update = () => {
      const checks = $$('[data-row-check]', bulkForm);
      const n = checks.filter((c) => c.checked).length;
      count.textContent = n;
      bar.classList.toggle('show', n > 0);
      checks.forEach((c) => c.closest('tr').classList.toggle('selected', c.checked));
    };
    const submitBulk = (action, value) => {
      $('[name=bulk_action]', bulkForm).value = action;
      $('[name=value]', bulkForm).value = value;
      bulkForm.submit();
    };
    bulkForm.addEventListener('change', (e) => {
      if (e.target.matches('[data-select-all]')) $$('[data-row-check]:not(:disabled)', bulkForm).forEach((c) => { c.checked = e.target.checked; });
      if (e.target.matches('[data-bulk]') && e.target.value) submitBulk(e.target.dataset.bulk, e.target.value);
      update();
    });
    bar.addEventListener('click', (e) => {
      const b = e.target.closest('[data-bulk-btn]');
      if (b) { if (b.dataset.confirmMsg && !confirm(b.dataset.confirmMsg)) return; submitBulk(b.dataset.bulkBtn, ''); }
      if (e.target.closest('[data-bulk-clear]')) { $$('[data-row-check], [data-select-all]', bulkForm).forEach((c) => { c.checked = false; }); update(); }
    });
    bulkForm.addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target.tagName === 'INPUT') e.preventDefault(); });
  }

  /* ------------------------------------------------------------------ Frises : glisser / redimensionner les barres */
  const isoD = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const addDaysD = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const parseD = (v) => new Date(`${v}T00:00:00`);
  // Utilisé par la vue Gantt des tickets et par la page Planning.
  // Déplacer : décale début et échéance ; poignées : modifie le début ou l'échéance.
  function barDrag(root, cfg) {
    let g = null;
    root.addEventListener('mousedown', (e) => {
      const bar = e.target.closest(cfg.selector);
      if (!bar || e.button !== 0) return;
      e.preventDefault();
      let mode = e.target.classList.contains('l') ? 'l' : e.target.classList.contains('r') ? 'r' : 'move';
      if (bar.dataset.milestone) mode = 'move';
      g = { bar, mode, x0: e.clientX, left0: bar.offsetLeft, width0: bar.offsetWidth, moved: false, delta: 0 };
    });
    document.addEventListener('mousemove', (e) => {
      if (!g) return;
      if (!g.moved && Math.abs(e.clientX - g.x0) <= 3) return;
      g.moved = true;
      g.bar.classList.add('dragging-bar');
      const dw = cfg.dayW();
      const delta = Math.round((e.clientX - g.x0) / dw);
      g.delta = delta;
      const dx = delta * dw;
      const minW = g.bar.dataset.milestone ? g.width0 : dw;
      if (g.mode === 'move') g.bar.style.left = `${g.left0 + dx}px`;
      else if (g.mode === 'r') g.bar.style.width = `${Math.max(minW, g.width0 + dx)}px`;
      else { const w = Math.max(minW, g.width0 - dx); g.bar.style.left = `${g.left0 + g.width0 - w}px`; g.bar.style.width = `${w}px`; }
      if (cfg.onMove) cfg.onMove(g.bar);
    });
    document.addEventListener('mouseup', async () => {
      if (!g) return;
      const { bar, moved, mode, delta, left0, width0 } = g;
      g = null;
      bar.classList.remove('dragging-bar');
      if (!moved) { if (cfg.onClick) cfg.onClick(bar); return; }
      if (!delta) { bar.style.left = `${left0}px`; bar.style.width = `${width0}px`; if (cfg.onMove) cfg.onMove(bar); return; }
      let s = parseD(bar.dataset.start);
      let en = parseD(bar.dataset.end);
      if (mode === 'move') { s = addDaysD(s, delta); en = addDaysD(en, delta); }
      else if (mode === 'r') { en = addDaysD(en, delta); if (en < s) en = new Date(s); }
      else { s = addDaysD(s, delta); if (s > en) s = new Date(en); }
      try {
        const r = await patchIssue(bar.dataset.id, { start_date: isoD(s), target_date: isoD(en) });
        bar.dataset.start = isoD(s);
        bar.dataset.end = isoD(en);
        toast(s.getTime() === en.getTime() ? `Planifié le ${fmtDate(isoD(s))}` : `Planifié du ${fmtDate(isoD(s))} au ${fmtDate(isoD(en))}`, 'success', 2200);
        if (cfg.onDone) cfg.onDone(bar, r);
      } catch (err) { toast(err.message, 'error'); softReload(); }
    });
  }

  /* ------------------------------------------------------------------ Gantt (vue des tickets) */
  const gantt = $('[data-gantt]');
  if (gantt) {
    const right = $('[data-gantt-right]', gantt);
    const left = $('[data-gantt-left]', gantt);
    const dayW = +gantt.dataset.dayW;
    right.addEventListener('scroll', () => { left.scrollTop = right.scrollTop; });
    left.addEventListener('wheel', (e) => { right.scrollTop += e.deltaY; e.preventDefault(); }, { passive: false });
    const today = $('.g-today', gantt);
    if (today) right.scrollLeft = Math.max(0, parseFloat(today.style.left) - right.clientWidth / 3);
    barDrag(gantt, {
      selector: '.g-bar[data-editable]',
      dayW: () => dayW,
      onClick: (bar) => {
        const rows = [...bar.closest('.g-body').querySelectorAll('.g-row')];
        const a = $('.g-row-l a.t', left.children[rows.indexOf(bar.closest('.g-row'))]);
        if (a) location.href = a.href;
      },
      onDone: (bar) => { bar.title = `${fmtDate(bar.dataset.start)} → ${fmtDate(bar.dataset.end)}`; },
    });
  }

  /* ------------------------------------------------------------------ Planning (feuille de route) */
  const plan = $('[data-planning]');
  if (plan) {
    const right = $('[data-pl-right]', plan);
    const left = $('[data-pl-left]', plan);
    const body = $('[data-pl-body]', plan);
    const svgEl = $('[data-pl-svg]', plan);
    const pathsG = $('[data-pl-paths]', plan);
    const dw = +plan.dataset.dw;
    const rowH = +plan.dataset.rowH;
    const deps = JSON.parse(plan.dataset.deps || '[]');
    right.addEventListener('scroll', () => { left.scrollTop = right.scrollTop; });
    left.addEventListener('wheel', (e) => { right.scrollTop += e.deltaY; right.scrollLeft += e.deltaX; e.preventDefault(); }, { passive: false });
    const todayLine = $('[data-pl-today-line]', plan);
    const goToday = () => { if (todayLine) right.scrollTo({ left: Math.max(0, parseFloat(todayLine.style.left) - right.clientWidth / 3), behavior: 'smooth' }); };
    if (todayLine) right.scrollLeft = Math.max(0, parseFloat(todayLine.style.left) - right.clientWidth / 3);

    const elbow = (x1, y1, x2, y2) => {
      if (x2 - x1 >= 16) return `M${x1},${y1} H${x1 + 8} V${y2} H${x2 - 2}`;
      const ym = y2 > y1 ? y2 - rowH / 2 : y2 + rowH / 2;
      return `M${x1},${y1} h8 V${ym} H${x2 - 10} V${y2} H${x2 - 2}`;
    };
    const geom = (id) => {
      const bar = $(`.pl-bar[data-id="${id}"]`, body);
      if (!bar) return null;
      const row = bar.closest('.pl-row');
      if (row.classList.contains('pl-hide')) return null;
      return { x1: bar.offsetLeft, x2: bar.offsetLeft + bar.offsetWidth, y: row.offsetTop + rowH / 2, bar };
    };
    let raf = 0;
    function drawArrows() {
      svgEl.setAttribute('height', Math.max(body.scrollHeight, 1));
      if (plan.classList.contains('no-arrows')) return;
      const out = [];
      deps.forEach(({ f, t }) => {
        const a = geom(f);
        const b = geom(t);
        if (!a || !b) return;
        const bad = b.bar.dataset.start <= a.bar.dataset.end;
        out.push(`<path class="pl-dep ${bad ? 'bad' : ''}" d="${elbow(a.x2, a.y, b.x1, b.y)}" marker-end="url(#${bad ? 'pl-arr-bad' : 'pl-arr'})"/>`);
      });
      pathsG.innerHTML = out.join('');
    }
    const redraw = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(drawArrows); };

    // Couloirs repliables (mémorisés)
    const CKEY = 'hangar.plCollapsed';
    const collapsedKeys = new Set(JSON.parse(localStorage.getItem(CKEY) || '[]'));
    const applyCollapse = () => {
      $$('[data-row]', plan).forEach((r) => {
        const parents = (r.dataset.parents || '').split(' ').filter(Boolean);
        r.classList.toggle('pl-hide', parents.some((p) => collapsedKeys.has(p)));
        r.classList.toggle('collapsed', collapsedKeys.has(r.dataset.key));
      });
      localStorage.setItem(CKEY, JSON.stringify(Array.from(collapsedKeys).slice(-500)));
      redraw();
    };
    plan.addEventListener('click', (e) => {
      const t = e.target.closest('[data-pl-toggle]');
      if (!t) return;
      const k = t.dataset.plToggle;
      if (collapsedKeys.has(k)) collapsedKeys.delete(k); else collapsedKeys.add(k);
      applyCollapse();
    });
    const allBtn = $('[data-pl-collapse-all]');
    if (allBtn) allBtn.addEventListener('click', () => {
      const keys = $$('[data-pl-toggle]', left).map((b) => b.dataset.plToggle).filter((k) => !k.startsWith('p') || k.includes('-'));
      const anyOpen = keys.some((k) => !collapsedKeys.has(k));
      keys.forEach((k) => (anyOpen ? collapsedKeys.add(k) : collapsedKeys.delete(k)));
      applyCollapse();
    });

    // Dépendances affichées / masquées
    const arrowsBtn = $('[data-pl-arrows]');
    const setArrows = (on) => {
      plan.classList.toggle('no-arrows', !on);
      if (arrowsBtn) arrowsBtn.classList.toggle('active', on);
      localStorage.setItem('hangar.plArrows', on ? '1' : '0');
      redraw();
    };
    setArrows(localStorage.getItem('hangar.plArrows') !== '0');
    if (arrowsBtn) arrowsBtn.addEventListener('click', () => setArrows(plan.classList.contains('no-arrows')));
    const todayBtn = $('[data-pl-today]');
    if (todayBtn) todayBtn.addEventListener('click', goToday);

    // Survol : met en évidence la ligne des deux côtés
    plan.addEventListener('mouseover', (e) => {
      const r = e.target.closest('[data-row]');
      $$('[data-row].hl', plan).forEach((x) => x.classList.remove('hl'));
      if (r) $$(`[data-row][data-key="${r.dataset.key}"]`, plan).forEach((x) => x.classList.add('hl'));
    });

    const today = isoD(new Date());
    const syncLabel = (bar) => {
      const lab = $(`[data-pl-label="${bar.dataset.id}"]`, body);
      if (lab) lab.style.left = `${bar.offsetLeft + bar.offsetWidth + 6}px`;
    };
    barDrag(body, {
      selector: '.pl-bar[data-editable]',
      dayW: () => dw,
      onMove: (bar) => { syncLabel(bar); redraw(); },
      onClick: (bar) => { if (bar.dataset.url) location.href = bar.dataset.url; },
      onDone: (bar, r) => {
        syncLabel(bar);
        const dates = $(`[data-pl-dates="${bar.dataset.id}"]`, left);
        if (dates) dates.textContent = bar.dataset.start === bar.dataset.end ? fmtDate(bar.dataset.start) : `${fmtDate(bar.dataset.start)} → ${fmtDate(bar.dataset.end)}`;
        const closed = r && r.issue && r.issue.state && ['completed', 'cancelled'].includes(r.issue.state.group);
        bar.classList.toggle('overdue', bar.dataset.end < today && !closed);
        redraw();
      },
    });
    applyCollapse();
    window.addEventListener('resize', redraw);
  }

  /* ------------------------------------------------------------------ Palette de commandes */
  const pal = $('#palette');
  const ACTIONS = [
    { title: 'Nouvelle tâche', icon: ICON.plus, kbd: 'T', run: () => openTask({}) },
    { title: 'Tableau de bord (agenda)', icon: ICON.home, url: H.urls && H.urls.home },
    { title: 'Calendrier', icon: ICON.calendar, url: H.urls && H.urls.calendar },
    { title: 'Mes tâches', icon: ICON.check, url: H.urls && H.urls.tasks },
    { title: 'Importer l’emploi du temps (.ics)', icon: ICON.arrow, url: H.urls && H.urls.imports },
    { title: 'Rappels et e-mails', icon: ICON.bell, url: H.agenda && H.agenda.settingsUrl },
    { title: 'Créer un ticket', icon: ICON.plus, kbd: 'C', run: () => openCreate() },
    { title: 'Créer un projet', icon: ICON.briefcase, url: H.urls && H.urls.newProject },
    { title: 'Accueil des projets', icon: ICON.briefcase, url: H.urls && H.urls.projectsHome },
    { title: 'Mes tickets', icon: ICON.layers, url: H.urls && H.urls.myIssues },
    { title: 'Notifications', icon: ICON.bell, url: H.urls && H.urls.notifications },
    { title: 'Analytique', icon: ICON.chart, url: H.urls && H.urls.analytics },
    { title: 'Équipe', icon: ICON.users, url: H.urls && H.urls.team },
    { title: 'Charge de travail de l’équipe', icon: ICON.users, url: H.urls && H.urls.workload },
    { title: 'Projets', icon: ICON.briefcase, url: H.urls && H.urls.projects },
    { title: 'Mon profil et préférences', icon: ICON.user, url: H.urls && H.urls.profile },
    { title: 'Basculer le thème clair / sombre', icon: ICON.moon, kbd: 'Maj D', run: () => toggleTheme() },
  ];
  Object.values(H.projects || {}).forEach((p) => {
    ACTIONS.push({ title: `Ouvrir ${p.name}`, icon: `<span>${esc(p.emoji)}</span>`, url: `/projects/${p.id}/issues`, sub: p.identifier });
  });
  let palItems = [];
  let palKb = 0;
  function palRender(groups) {
    const res = $('[data-palette-res]', pal);
    palItems = [];
    let html = '';
    groups.forEach(([title, items]) => {
      if (!items.length) return;
      html += `<div class="grp">${esc(title)}</div>`;
      items.forEach((it) => {
        palItems.push(it);
        html += `<div class="it" data-i="${palItems.length - 1}">${it.icon || ''}<span class="ellipsis">${it.key ? `<span class="key">${esc(it.key)}</span> ` : ''}${esc(it.title)}</span>${it.kbd ? `<span class="sub"><kbd>${esc(it.kbd)}</kbd></span>` : (it.sub ? `<span class="sub">${esc(it.sub)}</span>` : '')}</div>`;
      });
    });
    res.innerHTML = html || '<div class="pop-empty">Aucun résultat</div>';
    palKb = 0;
    palHighlight();
  }
  function palHighlight() {
    $$('.it', pal).forEach((el, i) => el.classList.toggle('kb', i === palKb));
    const cur = $('.it.kb', pal);
    if (cur) cur.scrollIntoView({ block: 'nearest' });
  }
  function palRun(it) {
    if (!it) return;
    pal.close();
    if (it.run) it.run(); else if (it.url) location.href = it.url;
  }
  const sansAccents = (s) => s.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
  const palSearch = debounce(async (q) => {
    const acts = ACTIONS.filter((a) => !q || sansAccents(a.title).includes(sansAccents(q)));
    let r = { issues: [], projects: [], pages: [], members: [], cycles: [], modules: [] };
    try { r = await api(`${H.urls.search}?q=${encodeURIComponent(q)}`); } catch (e) { /* hors ligne */ }
    // Sources des modules (wiki : pages, PDF, QCM, langues, commandes) : H.paletteSources, [[groupe, éléments], …]
    const extra = (await Promise.all((H.paletteSources || []).map((f) => Promise.resolve().then(() => f(q)).catch(() => [])))).flat();
    const vus = new Set(extra.flatMap(([, items]) => items.map((it) => it.url)).filter(Boolean));
    const issueIt = (i) => ({ title: i.title, key: i.key, sub: i.sub, url: i.url, icon: stateIcon(i.state_group, i.state_color) });
    const gen = (arr, ic) => arr.map((x) => ({ title: x.title, sub: x.sub, url: x.url, icon: x.emoji ? `<span>${esc(x.emoji)}</span>` : (x.initials ? avatarHTML(x) : ic) }));
    palRender([
      [q ? 'Tickets' : 'Récemment modifiés', r.issues.map(issueIt)],
      ['Actions', q ? acts : acts.slice(0, 6)],
      ...extra,
      ['Projets', gen(r.projects, ICON.briefcase)],
      ['Pages', gen(r.pages, ICON.page).filter((it) => !vus.has(it.url))],
      ['Cycles', gen(r.cycles, ICON.cycle)],
      ['Modules', gen(r.modules, ICON.module)],
      ['Membres', gen(r.members, ICON.user)],
    ]);
  }, 140);
  function openPalette() {
    if (!pal) return;
    if (pal.open) { pal.close(); return; }
    pal.showModal();
    const inp = $('[data-palette-input]', pal);
    inp.value = '';
    inp.focus();
    palSearch('');
  }
  H.openPalette = openPalette;
  if (pal) {
    const inp = $('[data-palette-input]', pal);
    inp.addEventListener('input', () => palSearch(inp.value.trim()));
    inp.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown') { palKb = Math.min(palKb + 1, palItems.length - 1); palHighlight(); e.preventDefault(); }
      else if (e.key === 'ArrowUp') { palKb = Math.max(palKb - 1, 0); palHighlight(); e.preventDefault(); }
      else if (e.key === 'Enter') { e.preventDefault(); palRun(palItems[palKb]); }
    });
    $('[data-palette-res]', pal).addEventListener('click', (e) => { const it = e.target.closest('.it'); if (it) palRun(palItems[+it.dataset.i]); });
    $('[data-palette-full]', pal).addEventListener('click', (e) => { e.preventDefault(); location.href = `${e.currentTarget.href}?q=${encodeURIComponent(inp.value)}`; });
  }
  document.addEventListener('click', (e) => { if (e.target.closest('[data-palette]')) { e.preventDefault(); openPalette(); } });

  /* ------------------------------------------------------------------ Thème */
  function effectiveTheme() {
    const t = document.documentElement.dataset.theme;
    if (t === 'system') return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    return t;
  }
  async function toggleTheme() {
    const next = effectiveTheme() === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { await api(H.urls.theme, 'POST', { theme: next }); } catch (e) { /* ignore */ }
  }
  document.addEventListener('click', (e) => { if (e.target.closest('[data-theme-toggle]')) toggleTheme(); });

  /* ------------------------------------------------------------------ Raccourcis clavier */
  let gPrefix = 0;
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette(); return; }
    if (pop && pop.mention && pop.keys(e)) return;
    if (isTyping(e.target) || $('dialog[open]') || e.altKey || e.ctrlKey || e.metaKey) {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && e.target.tagName === 'TEXTAREA') {
        const f = e.target.closest('form');
        if (f) { e.preventDefault(); f.requestSubmit(); }
      }
      return;
    }
    const k = e.key;
    if (gPrefix && Date.now() - gPrefix < 1200) {
      gPrefix = 0;
      const map = { h: H.urls.home, i: H.urls.myIssues, p: H.urls.projects, n: H.urls.notifications, a: H.urls.analytics, t: H.urls.team, c: H.urls.calendar, l: H.urls.tasks, e: H.urls.imports };
      if (map[k]) { e.preventDefault(); location.href = map[k]; }
      return;
    }
    if (k === 'g') { gPrefix = Date.now(); return; }
    if (k === 'c' && ciModal) { e.preventDefault(); openCreate(); }
    else if (k === 't' && $('#task-modal')) { e.preventDefault(); openTask({}); }
    else if (k === '/') { e.preventDefault(); openPalette(); }
    else if (k === '?') { e.preventDefault(); const a = $('[data-shortcuts]'); if (a) location.href = a.href; }
    else if (k === 'D' && e.shiftKey) { e.preventDefault(); toggleTheme(); }
    else if (k === '[') { document.body.classList.toggle('sb-open'); }
  });

  /* ------------------------------------------------------------------ Éditeur markdown */
  function mdApply(ta, kind) {
    const s = ta.selectionStart;
    const en = ta.selectionEnd;
    const sel = ta.value.slice(s, en);
    const wrap = (a, b = a, ph = 'texte') => {
      const t = sel || ph;
      ta.setRangeText(a + t + b, s, en, 'end');
      ta.setSelectionRange(s + a.length, s + a.length + t.length);
    };
    const line = (prefix) => {
      const ls = ta.value.lastIndexOf('\n', s - 1) + 1;
      ta.setRangeText(prefix, ls, ls, 'end');
    };
    switch (kind) {
      case 'bold': wrap('**'); break;
      case 'italic': wrap('_'); break;
      case 'code': if (sel.includes('\n')) wrap('```\n', '\n```', 'code'); else wrap('`', '`', 'code'); break;
      case 'heading': line('## '); break;
      case 'ul': line('- '); break;
      case 'task': line('- [ ] '); break;
      case 'quote': line('> '); break;
      case 'link': wrap('[', '](https://)', 'lien'); break;
      case 'table': ta.setRangeText('\n| Colonne 1 | Colonne 2 |\n|---|---|\n|  |  |\n', s, en, 'end'); break;
      default: break;
    }
    ta.focus();
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  }
  document.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-md]');
    if (b) { const ed = b.closest('[data-md-editor]'); ed.classList.remove('previewing'); $$('[data-md-tab]', ed).forEach((t) => t.classList.toggle('active', t.dataset.mdTab === 'write')); mdApply($('textarea', ed), b.dataset.md); return; }
    const t = e.target.closest('[data-md-tab]');
    if (t) {
      const ed = t.closest('[data-md-editor]');
      $$('[data-md-tab]', ed).forEach((x) => x.classList.toggle('active', x === t));
      const preview = t.dataset.mdTab === 'preview';
      ed.classList.toggle('previewing', preview);
      if (preview) {
        const box = $('.md-preview', ed);
        box.innerHTML = '<p class="muted">Chargement…</p>';
        try { const r = await api(H.urls.markdown, 'POST', { text: $('textarea', ed).value }); box.innerHTML = r.html || '<p class="muted">Rien à prévisualiser.</p>'; } catch (err) { box.textContent = err.message; }
      }
    }
  });

  // Mentions @
  let membersCache = null;
  document.addEventListener('input', async (e) => {
    const ta = e.target;
    if (!ta.matches || !ta.matches('textarea[data-mention]')) return;
    const pos = ta.selectionStart;
    const m = ta.value.slice(0, pos).match(/(?:^|[\s(])@([\w.-]*)$/);
    if (!m) { if (pop && pop.mention) closePop(); return; }
    if (!membersCache) { try { membersCache = (await api(H.urls.members)).members; } catch (err) { return; } }
    const q = m[1].toLowerCase();
    const opts = membersCache.filter((u) => u.username.includes(q) || u.name.toLowerCase().includes(q)).slice(0, 8)
      .map((u) => ({ id: u.username, label: u.name, hint: `@${u.username}`, icon: avatarHTML(u) }));
    if (!opts.length) { if (pop && pop.mention) closePop(); return; }
    openPop(ta, {
      options: opts, noSearch: true, mention: true,
      onPick: (o) => { const st = ta.selectionStart - m[1].length; ta.setRangeText(`${o.id} `, st, ta.selectionStart, 'end'); ta.focus(); },
    });
    ta.focus();
  });

  /* ------------------------------------------------------------------ Détail d'un ticket */
  $$('[data-autoresize]').forEach((ta) => {
    const fit = () => { ta.style.height = 'auto'; ta.style.height = `${ta.scrollHeight}px`; };
    fit();
    ta.addEventListener('input', fit);
  });
  const titleTa = $('[data-issue-title]');
  if (titleTa) {
    let original = titleTa.value;
    titleTa.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); titleTa.blur(); } if (e.key === 'Escape') { titleTa.value = original; titleTa.blur(); } });
    titleTa.addEventListener('blur', async () => {
      const v = titleTa.value.trim();
      if (!v) { titleTa.value = original; return; }
      if (v === original) return;
      try { await postForm(titleTa.dataset.issueTitle, { title: v }); original = v; toast('Titre mis à jour', 'success', 1800); document.title = document.title.replace(/^(\S+) .*? ·/, `$1 ${v} ·`); } catch (err) { toast(err.message, 'error'); }
    });
  }
  $$('[data-desc-view]').forEach((v) => v.addEventListener('click', (e) => {
    if (e.target.closest('a')) return;
    const blk = v.closest('[data-desc-block]');
    v.classList.add('hide');
    const f = $('[data-desc-form]', blk);
    f.classList.remove('hide');
    $('textarea', f).focus();
  }));
  document.addEventListener('click', (e) => {
    const c = e.target.closest('[data-desc-cancel]');
    if (c) { const blk = c.closest('[data-desc-block]'); $('[data-desc-form]', blk).classList.add('hide'); $('[data-desc-view]', blk).classList.remove('hide'); }
    const ce = e.target.closest('[data-comment-edit]');
    if (ce) { const card = ce.closest('[data-comment]'); card.classList.add('editing'); $('textarea', card).focus(); }
    const cc = e.target.closest('[data-comment-cancel]');
    if (cc) cc.closest('[data-comment]').classList.remove('editing');
  });
  document.addEventListener('click', async (e) => {
    const r = e.target.closest('[data-react]');
    if (!r) return;
    const payload = { emoji: r.dataset.react };
    if (r.dataset.commentId) payload.comment_id = r.dataset.commentId; else payload.issue_id = r.dataset.issueId;
    try { await api('/api/reactions', 'POST', payload); softReload(); } catch (err) { toast(err.message, 'error'); }
  });
  document.addEventListener('click', async (e) => {
    const s = e.target.closest('[data-subscribe]');
    if (s) {
      try {
        const r = await postForm(s.dataset.subscribe);
        s.classList.toggle('active', r.subscribed);
        $('span', s).textContent = r.subscribed ? 'Suivi' : 'Suivre';
        toast(r.subscribed ? 'Tu suis ce ticket' : 'Tu ne suis plus ce ticket', 'success', 2000);
      } catch (err) { toast(err.message, 'error'); }
      return;
    }
    const f = e.target.closest('[data-fav]');
    if (f) {
      e.preventDefault();
      try { const r = await postForm(f.dataset.fav); f.classList.toggle('on', r.favorite); toast(r.favorite ? 'Ajouté aux favoris' : 'Retiré des favoris', 'success', 1800); } catch (err) { toast(err.message, 'error'); }
      return;
    }
    const cp = e.target.closest('[data-copy]');
    if (cp) {
      const text = cp.dataset.copy;
      try { await navigator.clipboard.writeText(text); } catch (err) {
        const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove();
      }
      toast('Copié dans le presse-papiers', 'success', 1800);
    }
  });
  // Dépôt de fichiers
  $$('[data-dropzone]').forEach((dz) => {
    const form = dz.closest('form');
    const input = $('input[type=file]', dz);
    input.addEventListener('change', () => { if (input.files.length) form.submit(); });
    ['dragenter', 'dragover'].forEach((ev) => dz.addEventListener(ev, (e) => { if (drag) return; e.preventDefault(); dz.classList.add('over'); }));
    ['dragleave', 'drop'].forEach((ev) => dz.addEventListener(ev, () => dz.classList.remove('over')));
    dz.addEventListener('drop', (e) => { if (drag) return; e.preventDefault(); input.files = e.dataTransfer.files; form.submit(); });
  });

  /* ------------------------------------------------------------------ Pense-bêtes */
  $$('[data-sticky]').forEach((st) => {
    const url = st.dataset.sticky;
    const ta = $('textarea', st);
    const save = debounce(() => postForm(url, { content: ta.value }).catch(() => toast('Enregistrement impossible', 'error')), 600);
    ta.addEventListener('input', save);
    st.addEventListener('click', async (e) => {
      const sw = e.target.closest('[data-sticky-color]');
      if (sw) { st.style.background = sw.dataset.stickyColor; await postForm(url, { color: sw.dataset.stickyColor }); }
      if (e.target.closest('[data-sticky-delete]') && confirm('Supprimer ce pense-bête ?')) { await postForm(url, { delete: '1' }); st.remove(); }
    });
  });

  /* ------------------------------------------------------------------ Éditeur de page (aperçu + sauvegarde auto) */
  const pageEd = $('[data-page-editor]');
  if (pageEd) {
    const form = pageEd.closest('form');
    const ta = $('textarea[name=content]', form);
    const titleIn = $('[name=title]', form);
    const preview = $('[data-page-preview]', form);
    const status = $('[data-save-status]');
    const versionIn = $('[name=version]', form);  // empreinte de la page telle qu'elle a été chargée
    const explicitIn = $('[name=explicit]', form);
    const conflictBox = $('[data-page-conflict]', form);
    // Réaffichée après un conflit : le texte de l'éditeur n'est pas enregistré (alerte avant de quitter).
    let lastSaved = form.hasAttribute('data-unsaved') ? null : ta.value + titleIn.value;
    let saving = false;
    let again = false;
    let conflict = false;
    const refresh = debounce(async () => {
      if (pageEd.classList.contains('single')) return;
      try { const r = await api(H.urls.markdown, 'POST', { text: ta.value }); preview.innerHTML = r.html; } catch (e) { /* ignore */ }
    }, 350);
    // Page modifiée ailleurs : le serveur a refusé (409) sans rien écrire. On arrête l'enregistrement
    // automatique et on garde le texte ; « Enregistrer » réaffiche l'éditeur avec la version actuelle à côté.
    const showConflict = (msg) => {
      conflict = true;
      if (conflictBox) { $('[data-page-conflict-text]', conflictBox).textContent = msg; conflictBox.classList.remove('hide'); }
      if (status) status.textContent = 'Enregistrement automatique arrêté';
      toast(msg, 'error', 8000);
    };
    const save = async () => {
      const cur = ta.value + titleIn.value;
      if (conflict || cur === lastSaved) return;
      if (saving) { again = true; return; }  // une seule sauvegarde à la fois : la suivante part avec la nouvelle empreinte
      saving = true;
      if (status) status.textContent = 'Enregistrement…';
      try {
        const r = await fetch(form.action, { method: 'POST', body: new FormData(form), headers: { 'X-Requested-With': 'fetch' } });
        let j = {};
        try { j = await r.json(); } catch (e) { /* réponse non JSON (session expirée…) */ }
        if (r.ok && j.ok) {
          lastSaved = cur;
          if (versionIn && j.version) versionIn.value = j.version;
          if (explicitIn) explicitIn.value = '0';
          if (status) status.textContent = `Enregistré à ${j.saved_at}`;
        } else if (r.status === 409) {
          showConflict(j.error || 'Cette page a été modifiée ailleurs pendant que tu écrivais. Rien n’a été écrasé.');
        } else if (status) status.textContent = 'Échec de l’enregistrement automatique';
      } catch (e) { if (status) status.textContent = 'Échec de l’enregistrement automatique'; }
      saving = false;
      if (again) { again = false; save(); }
    };
    const autosave = debounce(save, 2500);
    ta.addEventListener('input', () => { refresh(); autosave(); if (status && !conflict) status.textContent = 'Modifications non enregistrées'; });
    titleIn.addEventListener('input', autosave);
    ta.addEventListener('keydown', (e) => {
      if (e.key === 'Tab') { e.preventDefault(); ta.setRangeText('  ', ta.selectionStart, ta.selectionEnd, 'end'); }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); submitNow(); }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'b') { e.preventDefault(); mdApply(ta, 'bold'); }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'i') { e.preventDefault(); mdApply(ta, 'italic'); }
    });
    $$('[data-page-tool]').forEach((b) => b.addEventListener('click', () => mdApply(ta, b.dataset.pageTool)));
    const toggle = $('[data-preview-toggle]');
    if (toggle) toggle.addEventListener('click', () => { pageEd.classList.toggle('single'); toggle.classList.toggle('active', !pageEd.classList.contains('single')); refresh(); });
    refresh();
    window.addEventListener('beforeunload', (e) => { if (ta.value + titleIn.value !== lastSaved && !form.dataset.submitting) { e.preventDefault(); e.returnValue = ''; } });
    // Enregistrement explicite (bouton, Ctrl+S) : attendre la sauvegarde automatique en cours, pour envoyer
    // l'empreinte à jour (sinon le serveur verrait un faux conflit).
    function submitNow() {
      if (saving) { setTimeout(submitNow, 150); return; }
      if (explicitIn) explicitIn.value = '1';
      form.dataset.submitting = '1';
      form.submit();
    }
    form.addEventListener('submit', (e) => {
      if (saving) { e.preventDefault(); submitNow(); return; }
      form.dataset.submitting = '1';
    });
  }

  /* ------------------------------------------------------------------ Barre latérale mobile */
  const isMobile = () => window.matchMedia('(max-width: 820px)').matches;
  function setCollapsed(on) {
    document.documentElement.classList.toggle('sb-collapsed', on);
    try { localStorage.setItem('hangar.sbCollapsed', on ? '1' : '0'); } catch (e) { /* ignore */ }
  }
  document.addEventListener('click', (e) => {
    if (e.target.closest('[data-sidebar-toggle]')) {
      if (isMobile()) document.body.classList.toggle('sb-open');
      else setCollapsed(!document.documentElement.classList.contains('sb-collapsed'));
      return;
    }
    if (e.target.closest('[data-sidebar-collapse]')) {
      if (isMobile()) document.body.classList.remove('sb-open'); else setCollapsed(true);
      return;
    }
    if (document.body.classList.contains('sb-open') && !e.target.closest('#sidebar')) document.body.classList.remove('sb-open');
  });

  /* ------------------------------------------------------------------ Panneau de l'assistant IA */
  function toggleAI(force) {
    const on = force === undefined ? !document.documentElement.classList.contains('ai-open') : force;
    document.documentElement.classList.toggle('ai-open', on);
    try { localStorage.setItem('hangar.ai', on ? '1' : '0'); } catch (e) { /* ignore */ }
    document.dispatchEvent(new CustomEvent('hangar:ai-toggle', { detail: { open: on } }));
  }
  H.toggleAI = toggleAI;
  document.addEventListener('click', (e) => { if (e.target.closest('[data-ai-toggle]')) { e.preventDefault(); toggleAI(); } });
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'j') { e.preventDefault(); toggleAI(); }
  });

  /* ------------------------------------------------------------------ Notifications (sondage léger) */
  if (H.me && H.urls.notif) {
    let lastUnread = null;
    const badge = $('#notif-badge');
    const poll = async () => {
      if (document.hidden) return;
      try {
        const r = await api(H.urls.notif);
        if (badge) { badge.textContent = r.unread > 99 ? '99+' : r.unread; badge.classList.toggle('hide', !r.unread); }
        if (lastUnread !== null && r.unread > lastUnread && r.latest) toast(r.latest.message, 'info', 6000);
        lastUnread = r.unread;
        document.title = document.title.replace(/^\(\d+\) /, '');
        if (r.unread) document.title = `(${r.unread}) ${document.title}`;
      } catch (e) { /* serveur arrêté */ }
    };
    poll();
    setInterval(poll, 45000);
  }
  document.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-notif-action]');
    if (!b) return;
    e.preventDefault();
    try { await postForm(b.dataset.notifAction); softReload(); } catch (err) { toast(err.message, 'error'); }
  });
  /* ------------------------------------------------------------------ Agenda : tâches, séances, semaine */
  const AG = () => window.AGENDA || { events: {}, tasks: {} };
  const tmModal = $('#task-modal');
  const pad2 = (n) => String(n).padStart(2, '0');
  const isoDate = (d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
  async function loadDayEvents(form, date, selected) {
    const sel = $('[data-tm-events]', form);
    if (!sel || !H.agenda) return;
    sel.innerHTML = '<option value="">Aucune</option>';
    if (!date) { sel.disabled = true; return; }
    sel.disabled = false;
    try {
      const r = await api(`${H.agenda.dayUrl}?date=${encodeURIComponent(date)}`);
      r.events.forEach((ev) => { const o = document.createElement('option'); o.value = ev.id; o.textContent = ev.label; o.dataset.start = ev.multi ? '' : ev.start; o.dataset.end = ev.multi ? '' : ev.end; sel.appendChild(o); });
    } catch (e) { /* hors ligne */ }
    if (selected && !$(`option[value="${selected}"]`, sel)) { const o = document.createElement('option'); o.value = selected; o.textContent = 'Séance liée (autre jour)'; sel.appendChild(o); }
    sel.value = selected ? String(selected) : '';
  }
  function chipFor(form, offset, label) {
    let cb = $(`.tm-offsets input[value="${offset}"]`, form);
    if (!cb) {
      const lab = document.createElement('label');
      lab.className = 'tm-chip'; lab.dataset.extra = '1';
      lab.innerHTML = `<input type="checkbox" name="reminders" value="${offset}"><span>${esc(label || `${offset} min avant`)}</span>`;
      $('.tm-offsets', form).appendChild(lab);
      cb = $('input', lab);
    }
    return cb;
  }
  function syncTaskForm(form) {
    const hasDate = !!form.date.value;
    const hasTime = !!form.start.value;
    form.start.disabled = !hasDate; form.end.disabled = !hasDate || !hasTime;
    const until = $('[data-tm-until]', form);
    if (until) until.style.visibility = form.recurrence.value ? 'visible' : 'hidden';
    form.recurrence.disabled = !hasDate;
    const help = $('[data-tm-help-allday]', form);
    if (help) help.hidden = !hasDate || hasTime;
  }
  function openTask(d = {}) {
    if (!tmModal) return;
    const form = $('[data-task-form]', tmModal);
    const edit = !!d.id;
    form.reset();
    $$('[data-extra]', form).forEach((x) => x.remove());
    form.action = edit ? d.urls.save : H.agenda.createUrl;
    form.next.value = location.pathname + location.search;
    $('[data-tm-heading]', form).textContent = edit ? 'Modifier la tâche' : 'Nouvelle tâche';
    $('[data-tm-submit]', form).textContent = edit ? 'Enregistrer' : 'Créer la tâche';
    form.elements.title.value = d.title || '';
    form.date.value = d.date || '';
    form.start.value = d.start || '';
    form.end.value = d.end || '';
    form.notes.value = d.notes || '';
    form.priority.value = d.priority || 'none';
    form.category.value = d.category || '';
    const col = $(`input[name=color][value="${d.color || ''}"]`, form); if (col) col.checked = true;
    form.recurrence.value = d.recurrence || '';
    form.until.value = d.until || '';
    form.reminder_channel.value = d.channel || (H.agenda && H.agenda.channel) || 'email';
    const fixed = $('[data-tm-fixed]', form);
    fixed.innerHTML = '';
    if (edit) {
      (d.reminders || []).forEach((r) => {
        if (r.offset === null || r.offset === undefined) {
          fixed.insertAdjacentHTML('beforeend', `<label class="checkbox"><input type="checkbox" name="remove_reminder" value="${r.id}"> Supprimer le rappel du ${esc(r.when)} <span class="muted">(${esc(r.statusLabel)})</span></label>`);
        } else {
          chipFor(form, r.offset, r.label).checked = true;
        }
      });
    } else if (H.agenda && H.agenda.defaultReminder >= 0 && d.date) {
      chipFor(form, H.agenda.defaultReminder).checked = true;
    }
    const del = $('[data-tm-delete]', form);
    del.classList.toggle('hide', !edit);
    del.dataset.url = edit ? d.urls.delete : '';
    const doneWrap = $('[data-tm-done-wrap]', form);
    doneWrap.classList.toggle('hide', !edit);
    $('[data-tm-done]', form).checked = !!d.done;
    $('[data-tm-done]', form).dataset.url = edit ? d.urls.toggle : '';
    syncTaskForm(form);
    loadDayEvents(form, form.date.value, d.event);
    $$('.dd.open').forEach((x) => x.classList.remove('open'));
    const em = $('#event-modal'); if (em && em.open) em.close();
    tmModal.showModal();
    setTimeout(() => form.elements.title.focus(), 30);
  }
  H.openTask = openTask;
  if (tmModal) {
    const form = $('[data-task-form]', tmModal);
    form.date.addEventListener('change', () => { syncTaskForm(form); loadDayEvents(form, form.date.value, form.event_id.value); });
    form.start.addEventListener('change', () => {
      syncTaskForm(form);
      if (form.start.value && !form.end.value) {
        const [h, m] = form.start.value.split(':').map(Number);
        const t = Math.min(h * 60 + m + 60, 23 * 60 + 55);
        form.end.value = `${pad2(Math.floor(t / 60))}:${pad2(t % 60)}`;
      }
    });
    form.recurrence.addEventListener('change', () => syncTaskForm(form));
    form.event_id.addEventListener('change', () => {
      const o = form.event_id.selectedOptions[0];
      if (o && o.dataset.start && !form.start.value) { form.start.value = o.dataset.start; form.end.value = o.dataset.end || ''; syncTaskForm(form); }
    });
    $('[data-tm-delete]', form).addEventListener('click', async (e) => {
      const url = e.currentTarget.dataset.url;
      if (!url || !confirm('Supprimer cette tâche ?')) return;
      try { await postForm(url); tmModal.close(); toast('Tâche supprimée', 'success', 2000); softReload(300); } catch (err) { toast(err.message, 'error'); }
    });
    $('[data-tm-done]', form).addEventListener('change', async (e) => {
      try { const r = await postForm(e.target.dataset.url); tmModal.close(); doneToast(r, 400); } catch (err) { toast(err.message, 'error'); }
    });
  }
  // Après une coche : message, puis rechargement. Tâche répétée : « Annuler » pendant quelques secondes
  // (le rechargement attend, sinon le bouton disparaîtrait avec la page).
  function doneToast(r, delay) {
    if (!r.undo) { toast(r.message || 'Enregistré', 'success', r.next ? 4000 : 2200); softReload(r.next ? 900 : delay); return; }
    let undone = false;
    toast(r.message || 'Enregistré', 'success', 6000, { label: 'Annuler', run: async () => {
      undone = true;
      try { const u = await postForm(r.undo, { prev: r.prev }); toast(u.message || 'Coche annulée', 'success', 2500); } catch (err) { toast(err.message, 'error'); }
      softReload(600);
    } });
    setTimeout(() => { if (!undone) softReload(); }, 6300);
  }
  document.addEventListener('click', (e) => {
    const nt = e.target.closest('[data-new-task]');
    if (nt) { e.preventDefault(); openTask({ date: nt.dataset.date || '', start: nt.dataset.start || '', event: nt.dataset.event || '' }); return; }
    const et = e.target.closest('[data-edit-task]');
    if (et) {
      e.preventDefault();
      const t = AG().tasks[et.dataset.editTask];
      if (t) openTask(t); else if (H.urls.tasks) location.href = `${H.urls.tasks}?focus=${et.dataset.editTask}`;
    }
  });
  // Cocher une tâche
  document.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-task-toggle]');
    if (!b || b.disabled) return;
    e.preventDefault();
    b.disabled = true;
    try {
      const r = await postForm(b.dataset.taskToggle);
      b.classList.toggle('on', !!r.done); b.classList.add('pulse');
      const row = b.closest('.tk-row, .wk-task, .ag-line, .ag-chip, .wk-bar');
      if (row) row.classList.toggle('done', !!r.done);
      doneToast(r, 700);
    } catch (err) { toast(err.message, 'error'); b.disabled = false; }
  });

  // Fenêtre de détail d'une séance
  const emModal = $('#event-modal');
  function openEvent(id) {
    const ev = AG().events[id];
    if (!ev || !emModal) return;
    const set = (sel, txt) => { const el = $(sel, emModal); if (el) el.textContent = txt || ''; };
    $('[data-em-color]', emModal).style.background = ev.color;
    set('[data-em-title]', ev.title);
    const badges = [];
    if (ev.kind) badges.push(`<span class="badge" style="background:color-mix(in srgb, ${esc(ev.color)} 16%, transparent);color:var(--text)">${esc(ev.kind)}${ev.session ? ` · séance ${esc(ev.session)}` : ''}</span>`);
    if (ev.exam) badges.push('<span class="badge badge-danger">Contrôle</span>');
    if (ev.tentative) badges.push('<span class="badge badge-warning">Prévisionnel</span>');
    if (ev.cancelled) badges.push('<span class="badge badge-danger">Annulé</span>');
    if (ev.removed) badges.push('<span class="badge badge-danger">Retiré du planning</span>');
    if (ev.badge && !ev.removed) badges.push(`<span class="badge badge-${esc(ev.badgeClass)}">${esc(ev.badge)} au dernier import</span>`);
    $('[data-em-badges]', emModal).innerHTML = badges.join('');
    set('[data-em-when]', ev.when);
    ['location', 'teachers', 'groups'].forEach((k) => { $(`[data-em-row="${k}"]`, emModal).hidden = !ev[k]; set(`[data-em-${k}]`, ev[k]); });
    set('[data-em-description]', ev.description);
    $('[data-em-changes]', emModal).innerHTML = ev.changes.length ? `<b>Changements :</b><br>${ev.changes.map(esc).join('<br>')}` : '';
    const nf = $('[data-em-note-form]', emModal);
    nf.action = ev.urls.note; nf.note.value = ev.note; nf.next.value = location.pathname + location.search;
    const rf = $('[data-em-reminder-form]', emModal);
    rf.action = ev.urls.reminder; rf.next.value = location.pathname + location.search;
    if (H.agenda) rf.channel.value = H.agenda.channel || 'email';
    $('[data-em-reminders]', emModal).innerHTML = ev.reminders.map((r) => `<div class="em-rem">${ICON.bell}<span class="grow">${esc(r.label)}${r.auto ? ' <span class="muted">(automatique)</span>' : ''} <span class="muted">· ${esc(r.when)} · ${r.channel === 'email' ? 'e-mail' : 'appli'}</span></span><span class="badge ${r.status === 'pending' ? 'badge-accent' : ''}">${esc(r.statusLabel)}</span><button type="button" class="btn btn-ghost btn-icon btn-xs" data-rem-del="${r.id}" title="Supprimer">${ICON.x}</button></div>`).join('');
    $('[data-em-tasks]', emModal).innerHTML = ev.tasks.map((t) => `<button type="button" class="em-rem" data-edit-task="${t.id}" style="border:0;font:inherit;color:inherit;cursor:pointer;text-align:left">${t.done ? ICON.check : ICON.calendar}<span class="grow ${t.done ? 'muted' : ''}" style="${t.done ? 'text-decoration:line-through' : ''}">${esc(t.title)}</span><span class="muted small">${esc(t.date)}</span></button>`).join('');
    $('[data-em-add-task]', emModal).onclick = () => { emModal.close(); openTask({ date: ev.date, event: ev.id, start: '', title: '' }); };
    renderEventLinks(ev);
    emModal.showModal();
  }
  H.openEvent = openEvent;
  // Liens de la matière : collection du wiki et catégorie de QCM (+ date d'examen pour un contrôle)
  function renderEventLinks(ev) {
    const sec = $('[data-em-links-section]', emModal);
    const box = $('[data-em-links]', emModal);
    const L = ev.links || {};
    const hasWiki = L.wiki || (L.wikiAll || []).length;
    const hasQcm = L.qcm || (L.qcmAll || []).length;
    sec.hidden = !(hasWiki || hasQcm);
    if (sec.hidden) { box.innerHTML = ''; return; }
    const next = esc(location.pathname + location.search);
    const form = (field, value, label, cls = 'btn btn-xs') => `<form method="post" action="${esc(ev.urls.link)}" class="inline-form"><input type="hidden" name="_csrf" value="${esc(H.csrf)}"><input type="hidden" name="next" value="${next}"><input type="hidden" name="${field}" value="${esc(value)}"><button class="${cls}">${label}</button></form>`;
    const picker = (field, all, placeholder) => `<form method="post" action="${esc(ev.urls.link)}" class="em-link-pick"><input type="hidden" name="_csrf" value="${esc(H.csrf)}"><input type="hidden" name="next" value="${next}"><select class="input input-sm" name="${field}" aria-label="${esc(placeholder)}"><option value="">${esc(placeholder)}</option>${all.map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join('')}</select><button class="btn btn-xs">Relier</button></form>`;
    let html = '';
    if (L.wiki) {
      html += `<div class="em-link"><span class="em-link-k">Cours</span><a class="em-link-v" href="${esc(L.wiki.url)}">${esc(L.wiki.label)}</a>${form('wiki_path', '', 'Délier', 'btn btn-ghost btn-xs')}</div>`;
    } else if (hasWiki) {
      html += `<div class="em-link"><span class="em-link-k">Cours</span><span class="em-link-sug">${(L.wikiSuggest || []).map((p) => form('wiki_path', p, `${esc(p.split('/').pop())} ?`, 'btn btn-xs em-sug')).join('')}${picker('wiki_path', L.wikiAll || [], 'Choisir une collection du wiki…')}</span></div>`;
    }
    if (L.qcm) {
      html += `<div class="em-link"><span class="em-link-k">QCM</span><a class="em-link-v" href="${esc(L.qcm.url)}">${esc(L.qcm.label)}</a>${L.qcm.due ? `<span class="badge badge-accent">${L.qcm.due} à revoir</span>` : ''}${form('qcm_category', '', 'Délier', 'btn btn-ghost btn-xs')}</div>`;
      if (ev.exam && !ev.removed) html += `<form method="post" action="${esc(ev.urls.exam)}" class="em-link-exam"><input type="hidden" name="_csrf" value="${esc(H.csrf)}"><input type="hidden" name="next" value="${next}"><button class="btn btn-sm btn-primary">Préparer ce contrôle : date d’examen des QCM au ${esc(ev.date.split('-').reverse().join('/'))}</button></form>`;
    } else if (hasQcm) {
      html += `<div class="em-link"><span class="em-link-k">QCM</span><span class="em-link-sug">${(L.qcmSuggest || []).map((c) => form('qcm_category', c, `${esc(c)} ?`, 'btn btn-xs em-sug')).join('')}${picker('qcm_category', L.qcmAll || [], 'Choisir une catégorie de QCM…')}</span></div>`;
    }
    box.innerHTML = html;
  }
  document.addEventListener('click', async (e) => {
    const ev = e.target.closest('[data-ev]');
    if (ev && !e.target.closest('[data-task-toggle]')) { e.preventDefault(); openEvent(ev.dataset.ev); return; }
    const rd = e.target.closest('[data-rem-del]');
    if (rd && H.agenda) {
      if (!confirm('Supprimer ce rappel ?')) return;
      try { await postForm(H.agenda.reminderDelete.replace('/0/', `/${rd.dataset.remDel}/`)); rd.closest('.em-rem').remove(); toast('Rappel supprimé', 'success', 1800); softReload(600); } catch (err) { toast(err.message, 'error'); }
    }
  });

  // Clic sur un créneau libre de la semaine / un jour du mois : nouvelle tâche à cette date (et heure)
  document.addEventListener('click', (e) => {
    const col = e.target.closest('[data-slot-date]');
    if (col && e.target === col) {
      const wk = col.closest('[data-week]');
      const h0 = +wk.dataset.h0;
      const hourH = parseFloat(getComputedStyle(wk).getPropertyValue('--hour-h')) || 48;
      const y = e.clientY - col.getBoundingClientRect().top;
      let mins = h0 * 60 + Math.round((y / hourH) * 60 / 15) * 15;
      mins = Math.max(0, Math.min(mins, 23 * 60));
      const end = Math.min(mins + 60, 23 * 60 + 55);
      openTask({ date: col.dataset.slotDate, start: `${pad2(Math.floor(mins / 60))}:${pad2(mins % 60)}`, end: `${pad2(Math.floor(end / 60))}:${pad2(end % 60)}` });
      return;
    }
    const day = e.target.closest('[data-month-day]');
    if (day && (e.target === day || e.target.closest('.num') && !e.target.closest('button'))) openTask({ date: day.dataset.monthDay });
  });

  // Ligne « maintenant » et compte à rebours
  function tickNow() {
    const now = new Date();
    $$('[data-week]').forEach((wk) => {
      const line = $('[data-now-line]', wk);
      if (!line) return;
      const col = line.closest('[data-slot-date]');
      if (col.dataset.slotDate !== isoDate(now)) { line.remove(); return; }
      const t = (now.getHours() - +wk.dataset.h0) * 60 + now.getMinutes();
      if (t < 0 || now.getHours() >= +wk.dataset.h1) line.hidden = true; else { line.hidden = false; line.style.setProperty('--t', t); }
    });
    $$('[data-countdown]').forEach((el) => {
      const diff = Math.round((new Date(el.dataset.countdown) - now) / 60000);
      if (diff <= 0) { el.textContent = 'commence maintenant'; return; }
      const d = Math.floor(diff / 1440); const h = Math.floor((diff % 1440) / 60); const m = diff % 60;
      el.textContent = 'dans ' + (d ? `${d} j ${h} h` : (h ? `${h} h ${pad2(m)}` : `${m} min`));
    });
  }
  tickNow();
  setInterval(tickNow, 30000);
  // Fait défiler la grande grille de la semaine jusqu'à l'heure courante
  const bigWeek = $('.ag-week-full');
  if (bigWeek) { const line = $('[data-now-line]', bigWeek); if (line) bigWeek.scrollTop = Math.max(0, line.offsetTop - 160); }

  // Import : envoi dès que le fichier est choisi
  document.addEventListener('change', (e) => { if (e.target.matches('[data-autoupload]') && e.target.files.length) e.target.form.submit(); });
  // Mise en avant d'une tâche (lien depuis un e-mail ou une autre page)
  if (window.AGENDA_FOCUS) {
    const row = document.getElementById(`task-${window.AGENDA_FOCUS}`);
    if (row) { row.classList.add('focus'); row.scrollIntoView({ block: 'center' }); }
    const t = AG().tasks[window.AGENDA_FOCUS];
    if (t) openTask(t);
  }
})();
