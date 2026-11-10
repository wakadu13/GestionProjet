/* Workgraph et initiatives (Hangar) — sans bibliothèque externe.
 *
 * Graphe : colonnes Espace → Initiatives (ou Responsables) → Projets → Modules, liens hiérarchiques en courbes,
 * dépendances (« bloque » / « bloqué par ») en flèches courbes colorées, sélection d'un nœud avec mise en
 * évidence de sa chaîne, survol, zoom et déplacement à la souris ou au doigt, bouton « Recentrer ».
 * Tableau : lignes hiérarchiques dépliables.
 */
(() => {
  'use strict';
  const H = window.HANGAR || {};
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const toast = (m, t) => (H.toast ? H.toast(m, t) : alert(m));
  const store = {
    get(k, d) { try { const v = localStorage.getItem(`hangar.wg.${k}`); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(`hangar.wg.${k}`, JSON.stringify(v)); } catch (e) { /* stockage indisponible */ } },
  };

  /* ------------------------------------------------------------------ Page détail : focus de la note */
  document.addEventListener('click', (e) => {
    const a = e.target.closest('[data-wg-focus]');
    if (a) setTimeout(() => { const t = document.getElementById(a.dataset.wgFocus); if (t) t.focus(); }, 60);
  });

  /* ------------------------------------------------------------------ Tableau hiérarchique */
  const table = $('[data-wg-table]');
  if (table) {
    const rows = $$('tr[data-path]', table);
    const collapsed = new Set();
    const refresh = () => {
      rows.forEach((r) => {
        const parts = r.dataset.path.split('/');
        let hidden = false;
        for (let i = 2; i < parts.length && !hidden; i += 1) hidden = collapsed.has(parts.slice(0, i).join('/'));
        r.hidden = hidden;
        const tw = $('[data-wg-toggle]', r);
        if (tw) tw.setAttribute('aria-expanded', collapsed.has(r.dataset.path) ? 'false' : 'true');
      });
    };
    table.addEventListener('click', (e) => {
      const b = e.target.closest('[data-wg-toggle]');
      if (!b) return;
      const p = b.closest('tr').dataset.path;
      if (collapsed.has(p)) collapsed.delete(p); else collapsed.add(p);
      refresh();
    });
    document.addEventListener('click', (e) => {
      const b = e.target.closest('[data-wg-expand]');
      if (!b) return;
      collapsed.clear();
      if (b.dataset.wgExpand === '0') rows.forEach((r) => { if ($('[data-wg-toggle]', r) && r.classList.contains('d-0')) collapsed.add(r.dataset.path); });
      refresh();
    });
  }

  /* ------------------------------------------------------------------ Graphe */
  const root = $('[data-wg-graph]');
  if (!root) return;
  const data = JSON.parse($('#wg-data').textContent);
  const ICONS = JSON.parse(($('#wg-icons') || { textContent: '{}' }).textContent);
  const ic = (n) => ICONS[n] || '';
  const canEdit = root.dataset.canEdit === '1';
  const viewport = $('.wg-viewport', root);
  const world = $('.wg-world', root);
  const svg = $('.wg-links', root);
  const gEdges = $('.wg-edges', root);
  const gDeps = $('.wg-deps', root);
  const nodesBox = $('.wg-nodes', root);
  const headsBox = $('.wg-heads', root);
  const detail = $('.wg-detail', root);
  const tip = $('.wg-tip', root);
  const zoomVal = $('[data-wg-zoom-val]', root);

  const byId = new Map(data.nodes.map((n) => [n.id, n]));
  const WIDTH = { workspace: 210, initiative: 276, group: 276, person: 240, project: 256, module: 256 };
  const COL_GAP = 104;
  const ROW_GAP = 14;
  const prefs = {
    deps: store.get('deps', 'all'),
    modules: store.get('modules', true),
    compact: store.get('compact', false),
    right: 'all',
  };
  let sel = root.dataset.sel && byId.has(root.dataset.sel) ? root.dataset.sel : null;
  let hover = null;
  let pos = {};        // id -> {x, y, w, h}
  let visible = [];    // nœuds affichés
  let vEdges = [];
  let vDeps = [];
  let bounds = { x0: 0, y0: 0, x1: 100, y1: 100 };
  const view = { x: 0, y: 0, k: 1 };

  /* ---------- Rendu d'une carte */
  const pill = (h, title) => (h ? `<span class="wg-pill" style="--c:${esc(h.color)}" ${title ? `title="${esc(title)}"` : ''}><span class="dot"></span>${esc(h.label)}</span>` : '');
  const ghost = (s) => (s ? `<span class="wg-pill ghost" style="--c:${esc(s.color)}"><span class="dot"></span>${esc(s.label)}</span>` : '');
  const ring = (p, s = 22) => `<span class="wg-ring" style="--p:${p};--s:${s}px"></span>`;
  const avatar = (u, s = 20) => (u ? `<span class="avatar" style="--av:${esc(u.color)};width:${s}px;height:${s}px;font-size:${Math.round(s * 0.42)}px" title="${esc(u.name)}">${esc(u.initials)}</span>` : '');
  const depBadges = (n) => `${n.blocked_by ? `<span class="wg-dep-n in" title="Bloqué par ${n.blocked_by} ticket(s)">${ic('ban')}${n.blocked_by}</span>` : ''}${n.blocks ? `<span class="wg-dep-n out" title="Bloque ${n.blocks} ticket(s)">${ic('arrow-right')}${n.blocks}</span>` : ''}`;
  const due = (n) => (n.target_label ? `<span class="wg-n-date ${n.late ? 'late' : ''}" title="Échéance">${ic('calendar')}${esc(n.target_label)}</span>` : '');

  function cardHTML(n) {
    const t = esc(n.title);
    switch (n.kind) {
      case 'workspace':
        return `<span class="wg-ws-logo">${esc(n.emoji || '✈️')}</span><div class="grow"><div class="wg-n-title">${t}</div><div class="wg-n-sub">${esc(n.badge)}</div></div>`;
      case 'group':
        return `<span class="wg-group-ic">${ic('layers')}</span><div class="grow"><div class="wg-n-title">${t}</div><div class="wg-n-sub">${esc(n.sub || '')}</div></div>`;
      case 'person':
        return `${n.person ? avatar(n.person, 28) : `<span class="avatar none" style="width:28px;height:28px">${ic('user')}</span>`}<div class="grow"><div class="wg-n-title">${t}</div><div class="wg-n-sub">${esc(n.badge)} · ${esc(n.sub || '')}</div></div>`;
      case 'initiative':
        return `<div class="wg-n-top"><span class="wg-type initiative">${ic('lightbulb')}Initiative</span><span class="wg-n-ring">${ring(n.pct)}<b>${n.pct} %</b></span></div>
          <div class="wg-n-title"><span class="wg-emoji">${esc(n.emoji)}</span><span class="wg-clamp">${t}</span></div>
          <div class="wg-n-pills">${pill(n.health, (n.reasons || []).join(' · '))}${ghost(n.status)}${n.lead ? `<span class="wg-n-lead">${avatar(n.lead, 18)}<span>${esc(n.lead.name.split(' ')[0])}</span></span>` : ''}</div>
          <div class="wg-n-foot">${due(n)}${depBadges(n)}</div>`;
      case 'project':
        return `<div class="wg-n-top"><span class="wg-type project">${ic('briefcase')}Projet<span class="k">${esc(n.identifier)}</span></span><span class="right">${avatar(n.lead, 20)}</span></div>
          <div class="wg-n-title"><span class="wg-emoji">${esc(n.emoji)}</span><span class="wg-clamp">${t}</span></div>
          <div class="wg-n-pills">${pill(n.health, (n.reasons || []).join(' · '))}${depBadges(n)}${due(n)}</div>
          <div class="wg-n-foot"><span class="wg-n-bar"><span style="width:${n.pct}%"></span></span><b class="wg-n-pct">${n.pct} %</b></div>`;
      case 'module':
        return `<div class="wg-n-top"><span class="wg-type module">${ic('module')}Module<span class="k">${esc(n.identifier)}</span></span><span class="wg-n-ring">${ring(n.pct, 20)}<b>${n.pct} %</b></span></div>
          <div class="wg-n-title"><span class="wg-clamp">${t}</span></div>
          <div class="wg-n-pills">${ghost(n.status)}${pill(n.health, (n.reasons || []).join(' · '))}</div>
          <div class="wg-n-foot">${due(n)}${depBadges(n)}${avatar(n.lead, 18) ? `<span class="right">${avatar(n.lead, 18)}</span>` : ''}</div>`;
      default:
        return t;
    }
  }

  /* ---------- Sélection des nœuds, liens et dépendances affichés */
  function lastKind() { return prefs.modules ? 'module' : 'project'; }
  function computeVisible() {
    const depsOf = new Map();
    data.deps.forEach((d) => {
      if (prefs.deps === 'open' && !d.open) return;
      [d.s, d.t].forEach((id) => depsOf.set(id, (depsOf.get(id) || 0) + 1));
    });
    const keep = (n) => {
      if (n.kind === 'module' && !prefs.modules) return false;
      if (n.kind === lastKind() && prefs.right !== 'all') {
        if (prefs.right === 'deps') return depsOf.has(n.id);
        if (prefs.right === 'late') return !!n.late;
        if (prefs.right === 'open') return !n.closed;
      }
      return true;
    };
    visible = data.nodes.filter(keep);
    const ids = new Set(visible.map((n) => n.id));
    vEdges = data.edges.filter((e) => ids.has(e.s) && ids.has(e.t));
    vDeps = prefs.deps === 'none' ? [] : data.deps.filter((d) => ids.has(d.s) && ids.has(d.t) && (prefs.deps !== 'open' || d.open));
    return ids;
  }

  /* ---------- Placement en arbre « propre » (colonnes, parents centrés sur leurs enfants) */
  function layout() {
    const ids = computeVisible();
    nodesBox.innerHTML = visible.map((n) => `<div class="wg-node k-${n.kind}${n.health ? ` h-${n.health.key}` : ''}${n.closed ? ' closed' : ''}" data-id="${esc(n.id)}" style="width:${WIDTH[n.kind] || 240}px" tabindex="0" role="button" aria-label="${esc(`${n.badge || ''} ${n.title}`)}">${cardHTML(n)}</div>`).join('');
    const els = new Map($$('.wg-node', nodesBox).map((el) => [el.dataset.id, el]));
    const cols = [...new Set(visible.map((n) => n.col))].sort((a, b) => a - b);
    const colIndex = new Map(cols.map((c, i) => [c, i]));
    const colW = cols.map((c) => Math.max(...visible.filter((n) => n.col === c).map((n) => WIDTH[n.kind] || 240)));
    const colX = [];
    cols.forEach((c, i) => { colX[i] = i ? colX[i - 1] + colW[i - 1] + COL_GAP : 0; });
    // Colonnes empilées (cartes serrées, regroupées par parent principal), décalées pour suivre leurs parents
    const order = new Map(data.nodes.map((n, i) => [n.id, i]));
    const primary = new Map(visible.map((n) => [n.id, (n.parents || []).find((x) => ids.has(x)) || null]));
    pos = {};
    const rank = new Map();
    const byCol = cols.map((c) => visible.filter((n) => n.col === c));
    byCol.forEach((list, ci) => {
      list.sort((a, b) => {
        const pa = primary.get(a.id);
        const pb = primary.get(b.id);
        const ra = pa && rank.has(pa) ? rank.get(pa) : -1;
        const rb = pb && rank.has(pb) ? rank.get(pb) : -1;
        return ra - rb || order.get(a.id) - order.get(b.id);
      });
      let y = 0;
      let prevParent;
      list.forEach((n, i) => {
        const par = primary.get(n.id);
        if (i && par !== prevParent) y += 10;
        prevParent = par;
        rank.set(n.id, i);
        const h = els.get(n.id).offsetHeight;
        pos[n.id] = { x: colX[ci], y, w: WIDTH[n.kind] || 240, h };
        y += h + ROW_GAP;
      });
      if (ci > 0) {  // aligne la colonne sur ses parents (sans remonter au-dessus de 0)
        const diffs = list.map((n) => { const p = pos[primary.get(n.id)]; return p ? (p.y + p.h / 2) - (pos[n.id].y + pos[n.id].h / 2) : 0; });
        const off = Math.max(0, diffs.reduce((s, d) => s + d, 0) / (diffs.length || 1));
        list.forEach((n) => { pos[n.id].y += off; });
      }
    });
    // Racines (espace de travail) centrées sur leurs enfants
    byCol[0].forEach((n) => {
      const ch = visible.filter((m) => primary.get(m.id) === n.id).map((m) => pos[m.id]);
      if (ch.length) pos[n.id].y = Math.max(0, (Math.min(...ch.map((p) => p.y)) + Math.max(...ch.map((p) => p.y + p.h))) / 2 - pos[n.id].h / 2);
    });
    els.forEach((el, id) => { const p = pos[id]; el.style.transform = `translate(${p.x}px, ${p.y}px)`; });
    // En-têtes de colonnes
    headsBox.innerHTML = cols.map((c, i) => {
      const meta = data.columns.find((x) => x.col === c) || { title: '' };
      const count = visible.filter((n) => n.col === c && n.kind === meta.kind).length;
      const right = i === cols.length - 1 && i > 0;
      return `<div class="wg-head" style="transform:translate(${colX[i]}px, -46px);width:${colW[i]}px">
        <span class="wg-head-t">${esc(c === 0 ? 'Espace' : meta.title)}</span>${c ? `<span class="wg-head-n">${count}</span>` : ''}
        ${right ? `<span class="wg-head-r"><select class="wg-head-sel" data-wg-right title="Filtrer la colonne">
            ${[['all', 'Tout'], ['open', 'En cours'], ['late', 'En retard'], ['deps', 'Avec dépendances']].map(([k, l]) => `<option value="${k}" ${prefs.right === k ? 'selected' : ''}>${l}</option>`).join('')}
          </select>${canEdit && data.tab === 'initiative' ? `<button class="wg-head-add" type="button" data-wg-add title="Lier des projets ou modules à l’initiative sélectionnée">${ic('plus')}</button>` : ''}</span>` : ''}
      </div>`;
    }).join('');
    // Bornes (avec la place des arcs de dépendances à droite)
    const xs = Object.values(pos);
    bounds = {
      x0: -10, y0: -56,
      x1: Math.max(...xs.map((p) => p.x + p.w)) + (vDeps.length ? 110 : 20),
      y1: Math.max(...xs.map((p) => p.y + p.h)) + 20,
    };
    svg.setAttribute('width', bounds.x1 + 200);
    svg.setAttribute('height', bounds.y1 + 200);
    drawLinks();
    paint();
  }

  /* ---------- Courbes */
  function drawLinks() {
    gEdges.innerHTML = vEdges.map((e) => {
      const a = pos[e.s];
      const b = pos[e.t];
      const x1 = a.x + a.w;
      const y1 = a.y + a.h / 2;
      const x2 = b.x;
      const y2 = b.y + b.h / 2;
      const dx = Math.max(36, (x2 - x1) * 0.5);
      return `<path class="wg-edge ${e.kind}" data-s="${esc(e.s)}" data-t="${esc(e.t)}" d="M${x1} ${y1} C${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}"/>`;
    }).join('');
    // Dépendances : arcs sur le bord droit de la colonne, répartis pour éviter les superpositions
    const outs = new Map();
    const ins = new Map();
    vDeps.forEach((d) => { outs.set(d.s, (outs.get(d.s) || []).concat(d)); ins.set(d.t, (ins.get(d.t) || []).concat(d)); });
    const slot = (list, d, p) => {
      const i = list.indexOf(d);
      const n = list.length;
      const span = Math.min(p.h - 16, n * 7);
      return p.y + p.h / 2 + (n > 1 ? (i / (n - 1) - 0.5) * span : 0);
    };
    const lane = new Map();
    gDeps.innerHTML = vDeps.map((d, idx) => {
      const a = pos[d.s];
      const b = pos[d.t];
      const y1 = slot(outs.get(d.s), d, a);
      const y2 = slot(ins.get(d.t), d, b);
      let path;
      if (Math.abs(a.x - b.x) < 2) {
        const xr = a.x + a.w;
        const l = lane.get(a.x) || 0;
        lane.set(a.x, l + 1);
        const off = 24 + Math.min(70, Math.abs(y2 - y1) * 0.2) + (l % 5) * 5;
        path = `M${xr} ${y1} C${xr + off} ${y1}, ${xr + off} ${y2}, ${xr + 4} ${y2}`;
      } else {
        const fwd = b.x > a.x;
        const x1 = fwd ? a.x + a.w : a.x;
        const x2 = fwd ? b.x - 4 : b.x + b.w + 4;
        const dx = Math.max(40, Math.abs(x2 - x1) * 0.5) * (fwd ? 1 : -1);
        path = `M${x1} ${y1} C${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
      }
      const cls = d.open ? 'open' : 'done';
      return `<g class="wg-dep ${cls}" data-i="${idx}" data-s="${esc(d.s)}" data-t="${esc(d.t)}"><path class="hit" d="${path}"/><path class="line" d="${path}" marker-end="url(#wg-ah-${cls})"/></g>`;
    }).join('');
  }

  /* ---------- Mise en évidence (sélection, survol) */
  function chainOf(id) {
    const up = new Set();
    const down = new Set([id]);
    const parents = new Map();
    const children = new Map();
    vEdges.forEach((e) => {
      parents.set(e.t, (parents.get(e.t) || []).concat(e.s));
      children.set(e.s, (children.get(e.s) || []).concat(e.t));
    });
    const walk = (start, map, acc) => { const st = [start]; while (st.length) { const x = st.pop(); (map.get(x) || []).forEach((y) => { if (!acc.has(y)) { acc.add(y); st.push(y); } }); } };
    walk(id, parents, up);
    if (id !== 'ws') walk(id, children, down);
    return { up, down };
  }

  function paint() {
    const focus = sel || hover;
    const nodeEls = $$('.wg-node', nodesBox);
    if (!focus || !pos[focus]) {
      root.classList.remove('has-focus', 'has-sel', 'has-hover');
      nodeEls.forEach((el) => el.classList.remove('sel', 'in-chain', 'related', 'hovered'));
      $$('.wg-edge', gEdges).forEach((p) => p.classList.remove('hl'));
      $$('.wg-dep', gDeps).forEach((g) => { g.classList.remove('hl', 'out', 'in', 'dim'); setMarker(g); });
      return;
    }
    // Sélection : chaîne mise en évidence, reste estompé ; survol : liens surlignés seulement
    const { up, down } = chainOf(focus);
    const chain = new Set([...up, ...down]);
    const related = new Set();
    $$('.wg-dep', gDeps).forEach((g) => {
      const s = g.dataset.s;
      const t = g.dataset.t;
      const out = down.has(s);
      const inn = down.has(t);
      g.classList.toggle('hl', out || inn);
      g.classList.toggle('out', out && !inn);
      g.classList.toggle('in', inn && !out);
      g.classList.toggle('dim', Boolean(sel) && !(out || inn));
      if (out && !chain.has(t)) related.add(t);
      if (inn && !chain.has(s)) related.add(s);
      setMarker(g);
    });
    root.classList.toggle('has-focus', Boolean(sel));
    root.classList.toggle('has-sel', Boolean(sel));
    root.classList.toggle('has-hover', !sel);
    nodeEls.forEach((el) => {
      const id = el.dataset.id;
      el.classList.toggle('sel', id === sel);
      el.classList.toggle('hovered', !sel && id === hover);
      el.classList.toggle('in-chain', chain.has(id));
      el.classList.toggle('related', related.has(id));
    });
    $$('.wg-edge', gEdges).forEach((p) => p.classList.toggle('hl', chain.has(p.dataset.s) && chain.has(p.dataset.t)));
  }
  function setMarker(g) {
    const line = $('.line', g);
    const k = g.classList.contains('out') ? 'out' : g.classList.contains('in') ? 'in' : g.classList.contains('hl') ? 'hl' : g.classList.contains('open') ? 'open' : 'done';
    line.setAttribute('marker-end', `url(#wg-ah-${k})`);
  }

  /* ---------- Panneau de détail */
  function depList(id) {
    const blocks = vDeps.filter((d) => d.s === id);
    const blocked = vDeps.filter((d) => d.t === id);
    const item = (d, other) => {
      const o = byId.get(other);
      return `<button type="button" class="wg-d-dep" data-wg-goto="${esc(other)}"><span class="ellipsis">${esc(o ? o.title : other)}</span><span class="tiny ${d.open ? 'danger-text' : 'muted'}">${d.open ? `${d.open} ouverte${d.open > 1 ? 's' : ''}` : 'résolue'}</span></button>`;
    };
    let h = '';
    if (blocked.length) h += `<div class="wg-d-h4 in">${ic('ban')}Bloqué par</div>${blocked.map((d) => item(d, d.s)).join('')}`;
    if (blocks.length) h += `<div class="wg-d-h4 out">${ic('arrow-right')}Bloque</div>${blocks.map((d) => item(d, d.t)).join('')}`;
    return h;
  }
  function showDetail() {
    const n = sel && byId.get(sel);
    if (!n || n.kind === 'workspace') { detail.hidden = true; return; }
    const st = n.stats;
    const badge = { initiative: 'Initiative', project: 'Projet', module: 'Module', person: n.badge, group: 'Groupe' }[n.kind];
    detail.innerHTML = `
      <div class="wg-d-top"><span class="wg-type ${esc(n.kind)}">${esc(badge)}${n.identifier ? `<span class="k">${esc(n.identifier)}</span>` : ''}</span>
        <button class="btn btn-ghost btn-icon btn-xs right" type="button" data-wg-close title="Fermer (Échap)">${ic('x')}</button></div>
      <div class="wg-d-title">${n.emoji ? `<span class="wg-emoji">${esc(n.emoji)}</span>` : ''}${esc(n.title)}</div>
      <div class="wg-n-pills">${pill(n.health)}${ghost(n.status)}${n.lead ? `<span class="wg-n-lead">${avatar(n.lead, 18)}<span>${esc(n.lead.name)}</span></span>` : ''}</div>
      ${n.pct !== undefined ? `<div class="wg-d-prog"><span class="wg-n-bar"><span style="width:${n.pct}%"></span></span><b>${n.pct} %</b></div>` : ''}
      ${st ? `<div class="wg-d-stats"><span><b>${st.done}</b>/${st.total} terminés</span><span><b>${st.open}</b> ouverts</span><span class="${st.overdue ? 'danger-text' : ''}"><b>${st.overdue}</b> en retard</span></div>` : ''}
      ${n.target_label ? `<div class="small ${n.late ? 'danger-text' : 'muted'}">${ic('calendar')} Échéance : ${esc(n.target_label)}</div>` : ''}
      ${n.reasons && n.reasons.length ? `<ul class="wg-reasons">${n.reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>` : ''}
      ${depList(n.id)}
      <div class="wg-d-actions">
        ${n.url ? `<a class="btn btn-sm btn-primary" href="${esc(n.url)}">Ouvrir ${ic('arrow-right')}</a>` : ''}
        ${n.kind === 'initiative' && canEdit && n.link_url ? `<button class="btn btn-sm" type="button" data-wg-add>${ic('link')} Lier…</button>` : ''}
      </div>`;
    detail.hidden = false;
  }

  function select(id, opts = {}) {
    sel = id && byId.has(id) ? id : null;
    paint();
    showDetail();
    try {
      const u = new URL(location.href);
      if (sel) u.searchParams.set('sel', sel); else u.searchParams.delete('sel');
      history.replaceState(null, '', u);
    } catch (e) { /* ignore */ }
    if (sel && opts.reveal && pos[sel]) reveal(sel);
  }

  /* ---------- Zoom et déplacement */
  function apply(anim) {
    world.classList.toggle('anim', !!anim);
    world.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.k})`;
    zoomVal.textContent = `${Math.round(view.k * 100)} %`;
    if (anim) setTimeout(() => world.classList.remove('anim'), 260);
  }
  function zoomAt(k, cx, cy, anim) {
    k = clamp(k, 0.25, 2.5);
    const wx = (cx - view.x) / view.k;
    const wy = (cy - view.y) / view.k;
    view.k = k;
    view.x = cx - wx * k;
    view.y = cy - wy * k;
    apply(anim);
  }
  /* Cadre une zone : toute la largeur du graphe (vue par défaut, alignée en haut) ou une chaîne sélectionnée. */
  function frame(box, whole, anim) {
    const vr = viewport.getBoundingClientRect();
    // garde la place du panneau de détail (à droite sur grand écran)
    const r = { width: vr.width - (sel && vr.width > 820 ? 340 : 0), height: vr.height };
    const pad = r.width < 600 ? 14 : 32;
    const bw = box.x1 - box.x0;
    const bh = box.y1 - box.y0;
    const minK = r.width < 600 ? 0.62 : 0.5;
    let k = whole ? (r.width - pad * 2) / bw : Math.min((r.width - pad * 2) / bw, (r.height - pad * 2) / bh);
    k = clamp(k, minK, 1);
    view.k = k;
    view.x = bw * k <= r.width - pad * 2 ? (r.width - bw * k) / 2 - box.x0 * k : pad - box.x0 * k;
    view.y = bh * k <= r.height - pad * 2 ? (r.height - bh * k) / 2 - box.y0 * k : pad - box.y0 * k;
    apply(anim);
  }
  function chainBox(id) {
    const { up, down } = chainOf(id);
    const ids = [...up, ...down].filter((x) => pos[x] && x !== 'ws');
    if (!ids.length) return bounds;
    const ps = ids.map((x) => pos[x]);
    return { x0: Math.min(...ps.map((p) => p.x)) - 10, y0: Math.min(...ps.map((p) => p.y)) - 20,
      x1: Math.max(...ps.map((p) => p.x + p.w)) + (vDeps.length ? 100 : 20), y1: Math.max(...ps.map((p) => p.y + p.h)) + 20 };
  }
  function fit(anim) {
    if (sel && pos[sel]) {
      frame(chainBox(sel), false, anim);
      reveal(sel, anim);
    } else frame(bounds, true, anim);
  }
  function reveal(id, anim = true) {
    const p = pos[id];
    const vr = viewport.getBoundingClientRect();
    const r = { width: vr.width - (sel && vr.width > 820 ? 340 : 0), height: vr.height - (sel && vr.width <= 820 ? vr.height * 0.5 : 0) };
    const k = view.k;
    const sx = view.x + p.x * k;
    const sy = view.y + p.y * k;
    const m = 12;
    let moved = false;
    if (sx < m || sx + p.w * k > r.width - m) { view.x = p.w * k + 2 * m > r.width ? m - p.x * k : r.width / 2 - (p.x + p.w / 2) * k; moved = true; }
    if (sy < m || sy + p.h * k > r.height - m) { view.y = r.height / 2 - (p.y + p.h / 2) * k; moved = true; }
    if (moved) apply(anim);
  }

  // Molette : déplacement (Maj = horizontal) ; Ctrl/⌘ + molette ou pincement du pavé tactile : zoom
  viewport.addEventListener('wheel', (e) => {
    e.preventDefault();
    hideTip();
    const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1;
    if (e.ctrlKey || e.metaKey) {
      const r = viewport.getBoundingClientRect();
      zoomAt(view.k * Math.exp(-e.deltaY * unit * 0.0025), e.clientX - r.left, e.clientY - r.top);
      return;
    }
    const dx = (e.shiftKey && !e.deltaX ? e.deltaY : e.deltaX) * unit;
    const dy = (e.shiftKey && !e.deltaX ? 0 : e.deltaY) * unit;
    view.x -= dx;
    view.y -= dy;
    apply();
  }, { passive: false });

  const pts = new Map();
  let drag = null;
  let pinch = null;
  let suppressClick = false;
  const INTERACTIVE = 'button, a, select, input, label, .menu';
  function onMove(e) {
    if (!pts.has(e.pointerId)) return;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pinch && pts.size >= 2) {
      const [a, b] = [...pts.values()];
      const r = viewport.getBoundingClientRect();
      const k = clamp(pinch.k * Math.hypot(a.x - b.x, a.y - b.y) / pinch.d, 0.25, 2.5);
      view.k = k;
      view.x = (a.x + b.x) / 2 - r.left - pinch.wx * k;
      view.y = (a.y + b.y) / 2 - r.top - pinch.wy * k;
      apply();
    } else if (drag) {
      const dx = e.clientX - drag.x;
      const dy = e.clientY - drag.y;
      if (!drag.moved && Math.hypot(dx, dy) > 4) { drag.moved = true; viewport.classList.add('dragging'); hideTip(); }
      if (drag.moved) { view.x = drag.vx + dx; view.y = drag.vy + dy; apply(); }
    }
  }
  function onUp(e) {
    if (!pts.has(e.pointerId)) return;
    pts.delete(e.pointerId);
    if (pts.size < 2) pinch = null;
    if (pts.size === 1) {
      const [p] = [...pts.values()];
      drag = { x: p.x, y: p.y, vx: view.x, vy: view.y, moved: true };
    }
    if (pts.size === 0) {
      suppressClick = !!(drag && drag.moved);
      drag = null;
      viewport.classList.remove('dragging');
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('pointercancel', onUp);
      setTimeout(() => { suppressClick = false; }, 0);
    }
  }
  viewport.addEventListener('pointerdown', (e) => {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    if (e.target.closest(INTERACTIVE)) return;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pts.size === 1) {
      drag = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y, moved: false };
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
      window.addEventListener('pointercancel', onUp);
    } else if (pts.size === 2) {
      const [a, b] = [...pts.values()];
      const r = viewport.getBoundingClientRect();
      pinch = { d: Math.max(10, Math.hypot(a.x - b.x, a.y - b.y)), k: view.k, wx: ((a.x + b.x) / 2 - r.left - view.x) / view.k, wy: ((a.y + b.y) / 2 - r.top - view.y) / view.k };
      if (drag) drag.moved = true;
    }
  });

  viewport.addEventListener('click', (e) => {
    if (suppressClick) { e.preventDefault(); e.stopPropagation(); return; }
    if (e.target.closest(INTERACTIVE)) return;
    const el = e.target.closest('.wg-node');
    if (el) { select(el.dataset.id === sel ? null : el.dataset.id); viewport.focus({ preventScroll: true }); return; }
    const dep = e.target.closest('.wg-dep');
    if (dep) return;
    select(null);
  });
  viewport.addEventListener('dblclick', (e) => {
    const el = e.target.closest('.wg-node');
    const n = el && byId.get(el.dataset.id);
    if (n && n.url) location.href = n.url;
  });

  /* ---------- Survol : nœuds et flèches (infobulle) */
  viewport.addEventListener('pointerover', (e) => {
    if (drag || e.pointerType === 'touch') return;
    const el = e.target.closest('.wg-node');
    const id = el ? el.dataset.id : null;
    if (id !== hover) { hover = id; if (!sel) paint(); }
    const dep = e.target.closest('.wg-dep');
    if (dep) showTip(dep, e); else hideTip();
  });
  viewport.addEventListener('pointerleave', () => { if (hover) { hover = null; if (!sel) paint(); } hideTip(); });
  viewport.addEventListener('pointermove', (e) => { if (!tip.hidden && !drag) placeTip(e); });
  function showTip(g, e) {
    const d = vDeps[+g.dataset.i];
    if (!d) return;
    const s = byId.get(d.s);
    const t = byId.get(d.t);
    tip.innerHTML = `<div class="wg-tip-h"><b>${esc(s.title)}</b> bloque <b>${esc(t.title)}</b></div>
      <div class="tiny muted">${d.n} relation${d.n > 1 ? 's' : ''} « bloqué par » · ${d.open} ouverte${d.open > 1 ? 's' : ''}</div>
      <ul>${d.items.map((it) => `<li class="${it.open ? '' : 'done'}"><span class="mono">${esc(it.blocker)}</span> → <span class="mono">${esc(it.blocked)}</span> <span class="ellipsis">${esc(it.blocked_title)}</span></li>`).join('')}${d.more ? `<li class="muted">… et ${d.more} autre(s)</li>` : ''}</ul>`;
    tip.hidden = false;
    placeTip(e);
  }
  function placeTip(e) {
    const r = root.getBoundingClientRect();
    const w = tip.offsetWidth;
    const h = tip.offsetHeight;
    let x = e.clientX - r.left + 14;
    let y = e.clientY - r.top + 14;
    if (x + w > r.width - 8) x = e.clientX - r.left - w - 14;
    if (y + h > r.height - 8) y = e.clientY - r.top - h - 14;
    tip.style.transform = `translate(${Math.max(8, x)}px, ${Math.max(8, y)}px)`;
  }
  function hideTip() { tip.hidden = true; }

  /* ---------- Clavier */
  viewport.addEventListener('focusin', (e) => {
    const el = e.target.closest('.wg-node');
    if (el && pos[el.dataset.id] && !drag) reveal(el.dataset.id);
  });
  viewport.addEventListener('keydown', (e) => {
    const r = viewport.getBoundingClientRect();
    const nodeEl = e.target.closest('.wg-node');
    if (nodeEl && (e.key === 'Enter' || e.key === ' ')) {
      e.preventDefault();
      const n = byId.get(nodeEl.dataset.id);
      if (sel === n.id && n.url && e.key === 'Enter') location.href = n.url;
      else select(n.id, { reveal: true });
      return;
    }
    if (e.key === '+' || e.key === '=') zoomAt(view.k * 1.2, r.width / 2, r.height / 2, true);
    else if (e.key === '-') zoomAt(view.k / 1.2, r.width / 2, r.height / 2, true);
    else if (e.key === '0') fit(true);
    else if (e.key === 'Escape') select(null);
    else if (e.key === 'Enter' && sel && byId.get(sel).url) location.href = byId.get(sel).url;
    else if (e.key.startsWith('Arrow')) {
      const s = 60;
      view.x += e.key === 'ArrowLeft' ? s : e.key === 'ArrowRight' ? -s : 0;
      view.y += e.key === 'ArrowUp' ? s : e.key === 'ArrowDown' ? -s : 0;
      apply(true);
    } else return;
    e.preventDefault();
  });

  /* ---------- Boutons, préférences, liaison */
  root.addEventListener('click', (e) => {
    const z = e.target.closest('[data-wg-zoom]');
    if (z) { const r = viewport.getBoundingClientRect(); zoomAt(view.k * (z.dataset.wgZoom === 'in' ? 1.25 : 0.8), r.width / 2, r.height / 2, true); return; }
    if (e.target.closest('[data-wg-fit]')) { fit(true); return; }
    if (e.target.closest('[data-wg-close]')) { select(null); return; }
    const go = e.target.closest('[data-wg-goto]');
    if (go) { select(go.dataset.wgGoto, { reveal: true }); return; }
    if (e.target.closest('[data-wg-add]')) { openLink(); }
  });
  root.addEventListener('change', (e) => {
    if (e.target.matches('[data-wg-right]')) { prefs.right = e.target.value; layout(); showDetail(); }
  });
  function openLink() {
    const n = sel && byId.get(sel);
    const dlg = document.getElementById('wg-link-modal');
    if (!n || n.kind !== 'initiative' || !dlg) { toast('Sélectionne d’abord une initiative dans le graphe pour lui lier des projets ou des modules.', 'info'); return; }
    const form = $('form', dlg);
    form.action = n.link_url;
    form.elements.next.value = `${location.pathname}${location.search}`;
    $('[data-wg-link-name]', dlg).textContent = `« ${n.title} »`;
    const linked = new Set(data.edges.filter((ed) => ed.s === n.id).map((ed) => ed.t));
    $$('[data-wg-cand]', dlg).forEach((o) => { o.hidden = linked.has(o.dataset.wgCand); $('input', o).checked = false; });
    dlg.showModal();
  }
  $$('[data-wg-pref]').forEach((inp) => {
    const k = inp.dataset.wgPref;
    if (inp.type === 'radio') inp.checked = prefs[k] === inp.value; else inp.checked = !!prefs[k];
    inp.addEventListener('change', () => {
      prefs[k] = inp.type === 'radio' ? inp.value : inp.checked;
      store.set(k, prefs[k]);
      root.classList.toggle('compact', !!prefs.compact);
      layout();
      fit(true);
    });
  });
  root.classList.toggle('compact', !!prefs.compact);

  let rt = null;
  window.addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(() => fit(), 150); });

  /* ---------- Démarrage */
  layout();
  fit();
  if (sel) { paint(); showDetail(); }
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { layout(); if (!sel) fit(); });
})();
