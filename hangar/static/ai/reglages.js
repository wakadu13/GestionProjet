/* Hangar — réglages de l'IA : ajout d'un fournisseur par préréglage, modification, test de connexion, modèles. */
(() => {
  'use strict';
  const H = window.HANGAR || {};
  const root = document.querySelector('[data-ai-settings]');
  if (!root) return;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const toast = (m, t) => (H.toast ? H.toast(m, t) : null);
  const modal = $('#ai-prov-modal');
  const form = modal ? $('[data-ai-prov-form]', modal) : null;
  const createAction = form ? form.getAttribute('action') : '';
  let presets = [];
  try { presets = JSON.parse(($('#ai-presets') || {}).textContent || '[]'); } catch (e) { /* ignore */ }
  const preset = (k) => presets.find((p) => p.key === k) || presets.find((p) => p.key === 'custom') || {};

  async function post(url, fd) {
    const r = await fetch(url, { method: 'POST', body: fd, headers: { 'X-CSRF-Token': H.csrf, 'X-Requested-With': 'fetch' } });
    let j = {};
    try { j = await r.json(); } catch (e) { /* ignore */ }
    if (!r.ok && !j.message && !j.error) j.error = `Erreur ${r.status}`;
    return j;
  }
  function showResult(el, j) {
    if (!el) return;
    const ok = j.ok && !j.error;
    el.className = `ai-test-res ${ok ? 'ok' : (j.unknown ? '' : 'ko')}`;
    el.textContent = j.message || j.error || '';
  }

  function setKind(kind) {
    $$('[data-ai-only]', form).forEach((el) => { el.hidden = el.dataset.aiOnly !== kind; });
  }
  function fill(data, isNew) {
    const p = preset(data.preset);
    form.reset();
    form.setAttribute('action', isNew ? createAction : data.action);
    form.elements.preset.value = data.preset || 'custom';
    form.elements.id.value = isNew ? '' : data.id;
    form.elements.name.value = data.name || p.label || '';
    form.elements.base_url.value = data.base_url || '';
    form.elements.api_key.value = '';
    form.elements.api_key_env.value = data.api_key_env || '';
    form.elements.model.value = data.model || '';
    form.elements.model_fast.value = data.model_fast || '';
    form.elements.model_vision.value = data.model_vision || '';
    form.elements.priority.value = data.priority ?? '';
    form.elements.monthly_cap.value = data.monthly_cap ? data.monthly_cap : '';
    form.elements.effort.value = data.effort || '';
    form.elements.tools_mode.value = data.tools_mode || 'auto';
    form.elements.price_input.value = data.price_input || '';
    form.elements.price_output.value = data.price_output || '';
    $$('input[name^="role_"]', form).forEach((cb) => { cb.checked = !data.roles || data.roles.includes(cb.name.slice(5)); });
    $$('input[name="enabled"][type="checkbox"]', form).forEach((cb) => { cb.checked = data.enabled !== false; });
    const key = form.elements.api_key;
    key.placeholder = data.masked_key ? `Laisser vide pour garder la clé (${data.masked_key})` : (p.kind === 'anthropic' ? 'sk-ant-…' : 'Clé d’API');
    const claude = p.kind === 'anthropic';
    form.elements.api_key_env.placeholder = p.key_env || (claude ? 'ANTHROPIC_API_KEY' : 'MA_CLE_API');
    form.elements.model.placeholder = claude ? 'claude-opus-5-5' : (p.model || 'llama3.2');
    form.elements.model_fast.placeholder = claude ? 'claude-haiku-5-5' : '(le modèle principal)';
    $('[data-ai-key-opt]', form).textContent = p.needs_key ? '' : '(facultative)';
    $('[data-ai-clear-key]', form).hidden = !data.key_saved;
    $('[data-ai-pm-title]', form).textContent = isNew ? `Ajouter : ${p.label || 'fournisseur'}` : `Modifier : ${data.name}`;
    $('[data-ai-pm-help]', form).textContent = p.help || '';
    $('[data-ai-pm-res]', form).textContent = '';
    $('[data-ai-pm-res]', form).className = 'ai-test-res';
    $('[data-ai-models-note]', form).textContent = '';
    $('#pm-models').innerHTML = (p.kind === 'anthropic' ? ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5'] : []).map((m) => `<option value="${m}">`).join('');
    setKind(p.kind || 'openai_compatible');
    modal.showModal();
    form.elements.name.focus();
  }

  $$('[data-ai-preset]', root).forEach((b) => b.addEventListener('click', () => {
    const p = preset(b.dataset.aiPreset);
    fill({ preset: p.key, name: p.label, base_url: p.base_url, model: p.model, model_fast: p.model_fast || '', api_key_env: p.key_env || '', enabled: true }, true);
  }));
  $$('[data-ai-edit]', root).forEach((b) => b.addEventListener('click', () => {
    const node = $(`script[data-ai-prov="${b.dataset.aiEdit}"]`);
    if (node) fill(JSON.parse(node.textContent), false);
  }));

  if (form) {
    $('[data-ai-pm-test]', form).addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      const res = $('[data-ai-pm-res]', form);
      btn.disabled = true;
      res.className = 'ai-test-res';
      res.textContent = 'Test en cours…';
      const fd = new FormData(form);
      fd.set('what', 'test');
      try { showResult(res, await post(root.dataset.tryUrl, fd)); } catch (err) { showResult(res, { error: 'Test impossible.' }); }
      btn.disabled = false;
    });
    $('[data-ai-load-models]', form).addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      const note = $('[data-ai-models-note]', form);
      btn.disabled = true;
      note.textContent = 'Chargement…';
      const fd = new FormData(form);
      fd.set('what', 'models');
      try {
        const j = await post(root.dataset.tryUrl, fd);
        if (j.ok) {
          $('#pm-models').innerHTML = (j.models || []).map((m) => `<option value="${esc(m.id)}">${esc(m.detail || m.name || '')}</option>`).join('');
          note.textContent = j.models && j.models.length ? `${j.models.length} modèle(s) : clique dans le champ pour choisir.` : 'Aucun modèle installé.';
          if (!form.elements.model.value && j.models && j.models[0]) form.elements.model.value = j.models[0].id;
        } else note.textContent = j.error || 'Liste indisponible.';
      } catch (err) { note.textContent = 'Liste indisponible.'; }
      btn.disabled = false;
    });
  }

  $$('[data-ai-test]', root).forEach((b) => b.addEventListener('click', async () => {
    const card = b.closest('.ai-prov');
    const res = card ? $('[data-ai-test-res]', card) : null;
    b.disabled = true;
    if (res) { res.className = 'ai-test-res'; res.textContent = 'Test en cours…'; }
    const fd = new FormData();
    fd.append('_csrf', H.csrf);
    try {
      const j = await post(b.dataset.aiTest, fd);
      showResult(res, j);
      toast(j.message || j.error || '', j.ok ? 'success' : 'error');
    } catch (err) { showResult(res, { error: 'Test impossible.' }); }
    b.disabled = false;
  }));
})();
