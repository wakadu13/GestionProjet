/* Hangar — pages d'IA portées de Revision : « Demander au wiki » (réponse et sources citées) et « Corriger ma copie »
   (photos réduites avant l'envoi, correction ajoutée en tête, suppression). */
(() => {
  'use strict';
  const H = window.HANGAR || {};
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const typeset = (el) => { if (H.aiTypeset) H.aiTypeset(el); };
  const headers = () => ({ 'X-CSRF-Token': H.csrf, 'X-Requested-With': 'fetch' });
  document.querySelectorAll('.ai-dm-rep, .ai-copie-texte').forEach(typeset);

  /* ---------------------------------------------------------------- Demander au wiki */
  const page = $('#demander');
  const form = page ? $('[data-demander-form]', page) : null;
  if (form) {
    const zone = $('[data-demander-echanges]', page);
    const champ = form.querySelector('textarea');
    const bouton = form.querySelector('button');
    const sources = (liste) => `<details class="ai-sources" open><summary>Sources (${liste.length})</summary><ol>${liste.map((s) => `<li value="${s.n}"${s.cite ? ' class="cite"' : ''}><a href="${esc(s.url)}">${esc(s.titre)}</a>${s.type === 'pdf' ? ' <span class="muted">PDF</span>' : ''}</li>`).join('')}</ol></details>`;
    const demander = async () => {
      const question = champ.value.trim();
      if (!question || bouton.disabled) return;
      bouton.disabled = true;
      zone.querySelectorAll('.ai-sources[open]').forEach((d) => { d.open = false; });
      const bloc = document.createElement('div');
      bloc.className = 'ai-dm-echange';
      bloc.innerHTML = `<div class="ai-msg ai-msg-u"><div class="ai-u-bubble">${esc(question)}</div></div><div class="ai-msg ai-msg-a"><div class="ai-av">✦</div><div class="ai-msg-main"><div class="markdown wiki-markdown ai-dm-rep"><span class="ai-dots"><i></i><i></i><i></i></span> <span class="help">L’IA cherche dans le wiki…</span></div></div></div>`;
      zone.prepend(bloc);
      const rep = bloc.querySelector('.ai-dm-rep');
      try {
        const r = await fetch(page.dataset.url, { method: 'POST', headers: { ...headers(), 'Content-Type': 'application/json' }, body: JSON.stringify({ question }) });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.error || `Réponse impossible pour l’instant (${r.status}).`);
        rep.innerHTML = data.html;
        if (data.sources && data.sources.length) rep.insertAdjacentHTML('afterend', sources(data.sources));
        typeset(rep);
        champ.value = '';
      } catch (e) {
        rep.innerHTML = `<div class="ai-err">${esc(e.message)}</div>`;
      } finally {
        bouton.disabled = false;
        champ.focus();
      }
    };
    form.addEventListener('submit', (e) => { e.preventDefault(); demander(); });
    champ.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey || !e.shiftKey)) { e.preventDefault(); demander(); }
    });
    if (champ.value) { champ.setSelectionRange(champ.value.length, champ.value.length); }
    champ.focus();
  }

  /* ---------------------------------------------------------------- Corriger ma copie */
  const copie = $('#copie');
  if (copie) {
    const historique = $('[data-copie-historique]', copie);
    const max = Number(copie.dataset.max) || 4;
    historique.addEventListener('click', async (e) => {
      const b = e.target.closest('[data-copie-supprimer]');
      if (!b) return;
      const bloc = b.closest('[data-copie]');
      if (!window.confirm('Supprimer cette correction et ses photos ?')) return;
      const r = await fetch(`${copie.dataset.url}${bloc.dataset.copie}/supprimer/`, { method: 'POST', headers: headers() });
      if (r.ok) bloc.remove();
    });
    const f = $('[data-copie-form]', copie);
    if (f) {
      const champ = $('[data-copie-photos]', f);
      const apercu = $('[data-copie-apercu]', f);
      const etat = $('[data-copie-etat]', f);
      const bouton = f.querySelector('button[type=submit]');
      let photos = [];
      const dire = (t, cls) => { etat.textContent = t; etat.className = `small ${cls || ''}`; };
      // Photo d'appareil (souvent 4000 px et plusieurs Mo) : réduite à 2000 px en JPEG avant l'envoi ; le serveur
      // la réduit encore pour le modèle.
      const reduire = async (fichier) => {
        if (!window.createImageBitmap || !fichier.type.startsWith('image/')) return fichier;
        try {
          const image = await createImageBitmap(fichier, { imageOrientation: 'from-image' });
          const k = Math.min(1, 2000 / Math.max(image.width, image.height));
          if (k === 1 && fichier.size < 1500000) return fichier;
          const toile = document.createElement('canvas');
          toile.width = Math.round(image.width * k);
          toile.height = Math.round(image.height * k);
          toile.getContext('2d').drawImage(image, 0, 0, toile.width, toile.height);
          const blob = await new Promise((res) => toile.toBlob(res, 'image/jpeg', 0.85));
          return blob ? new File([blob], `${fichier.name.replace(/\.\w+$/, '')}.jpg`, { type: 'image/jpeg' }) : fichier;
        } catch (e) { return fichier; }
      };
      const montrer = () => {
        apercu.replaceChildren(...photos.map((p, i) => {
          const fig = document.createElement('figure');
          const img = document.createElement('img');
          img.alt = `Page ${i + 1}`;
          const lecteur = new FileReader();
          lecteur.onload = () => { img.src = lecteur.result; };
          lecteur.readAsDataURL(p);
          const retirer = document.createElement('button');
          retirer.type = 'button';
          retirer.className = 'btn btn-ghost btn-xs';
          retirer.textContent = '✕';
          retirer.setAttribute('aria-label', `Retirer la page ${i + 1}`);
          retirer.addEventListener('click', () => { photos.splice(i, 1); montrer(); });
          const legende = document.createElement('figcaption');
          legende.append(`Page ${i + 1} `, retirer);
          fig.append(img, legende);
          return fig;
        }));
      };
      champ.addEventListener('change', () => {
        const nouvelles = [...champ.files].filter((x) => x.type.startsWith('image/') || /\.(jpe?g|png|webp|heic)$/i.test(x.name));
        photos = photos.concat(nouvelles).slice(0, max);
        dire(photos.length === max && nouvelles.length ? `${max} pages au plus.` : '');
        champ.value = '';
        montrer();
      });
      f.addEventListener('submit', async (e) => {
        e.preventDefault();
        if (!photos.length) { dire('Ajoute au moins une photo de la copie.', 'danger-text'); return; }
        const corps = new FormData();
        bouton.disabled = true;
        dire('Préparation des photos…');
        try {
          for (const p of photos) { const x = await reduire(p); corps.append('photos', x, x.name); }
          corps.append('enonce', f.elements.enonce.value);
          corps.append('page', f.elements.page.value);
          corps.append('_csrf', H.csrf);
          dire('L’IA lit et corrige la copie… (jusqu’à une minute)');
          const r = await fetch(copie.dataset.url, { method: 'POST', body: corps, headers: headers(), credentials: 'same-origin' });
          const data = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(data.error || `Correction impossible pour l’instant (${r.status}).`);
          historique.querySelectorAll('details[open]').forEach((d) => { d.open = false; });
          if (!historique.querySelector('h2')) historique.insertAdjacentHTML('afterbegin', '<h2 class="ai-sub">Mes corrections</h2>');
          const t = document.createElement('template');
          t.innerHTML = data.bloc.trim();
          const bloc = t.content.firstElementChild;
          historique.querySelector('h2').after(bloc);
          typeset(bloc);
          photos = [];
          montrer();
          f.elements.enonce.value = '';
          dire('Correction prête ✓', 'success-text');
          bloc.scrollIntoView({ behavior: 'smooth', block: 'start' });
        } catch (err) {
          dire(err.message, 'danger-text');
        } finally {
          bouton.disabled = false;
        }
      });
    }
  }
})();
