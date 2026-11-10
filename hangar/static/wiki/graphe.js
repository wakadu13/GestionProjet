// Graphe des liens entre pages, dessiné sur un canvas (sans bibliothèque) :
// placement par forces (ressorts sur les liens, répulsion entre pages voisines,
// légère attraction vers le centre), déplacement et zoom à la souris ou aux doigts,
// survol : la page et ses liens ressortent ; clic : la page s'ouvre.
(function () {
  "use strict";
  const canvas = document.querySelector("[data-graphe]");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const info = document.querySelector("[data-graphe-info]");
  const recherche = document.querySelector("[data-graphe-recherche]");
  const portee = document.querySelector("[data-graphe-portee]");
  const isolees = document.querySelector("[data-graphe-isolees]");
  const compte = document.querySelector("[data-graphe-compte]");
  const legende = document.querySelector("[data-graphe-legende]");
  const centreCle = canvas.dataset.centre || "";

  let tous = { noeuds: [], liens: [] };
  let noeuds = [];
  let liens = [];
  let vue = { x: 0, y: 0, k: 1 }; // translation (pixels CSS) et zoom
  let chaleur = 0;
  let survol = null;
  let choisi = null;
  let trouve = null;
  let dessinPrevu = false;
  const couleurs = new Map();

  const replier = (s) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();

  function variables() {
    const st = getComputedStyle(document.documentElement);
    const v = (n, d) => (st.getPropertyValue(n) || d).trim();
    return { texte: v("--text", "#222"), discret: v("--muted", "#888"), bord: v("--border", "#ddd"), accent: v("--accent", "#7852ee"), fond: v("--bg", "#fff") };
  }

  function couleurDossier(d) {
    if (!couleurs.has(d)) {
      const i = couleurs.size;
      couleurs.set(d, d ? `hsl(${Math.round((i * 137.508) % 360)}, 55%, 55%)` : "hsl(0, 0%, 60%)");
    }
    return couleurs.get(d);
  }

  // -- Données ----------------------------------------------------------------

  function voisinsJusqua(depart, profondeur) {
    const garde = new Set([depart]);
    let front = [depart];
    for (let p = 0; p < profondeur; p++) {
      const suivant = [];
      for (const i of front) for (const j of tous.adjacence[i]) if (!garde.has(j)) { garde.add(j); suivant.push(j); }
      front = suivant;
    }
    return garde;
  }

  function construire() {
    const centre = tous.noeuds.findIndex((n) => n.k === centreCle);
    const choix = portee ? portee.value : "tout";
    let garde;
    if (centre >= 0 && choix !== "tout") garde = voisinsJusqua(centre, Number(choix));
    else garde = new Set(tous.noeuds.map((_, i) => i).filter((i) => isolees.checked || tous.adjacence[i].length || i === centre));
    const ancien = new Map(noeuds.map((n) => [n.i, n]));
    const R = 30 * Math.sqrt(garde.size + 1);
    noeuds = [...garde].map((i, rang) => {
      const a = ancien.get(i);
      if (a) return a;
      const angle = rang * 2.399963; // spirale de Fermat : départ régulier
      const r = R * Math.sqrt((rang + 0.5) / garde.size);
      return { i, ...tous.noeuds[i], x: Math.cos(angle) * r, y: Math.sin(angle) * r, vx: 0, vy: 0, fixe: false, voisins: [] };
    });
    const parIndice = new Map(noeuds.map((n) => [n.i, n]));
    for (const n of noeuds) n.voisins = [];
    liens = [];
    for (const [a, b] of tous.liens) {
      const na = parIndice.get(a), nb = parIndice.get(b);
      if (na && nb) { liens.push([na, nb]); na.voisins.push(nb); nb.voisins.push(na); }
    }
    for (const n of noeuds) n.rayon = 3 + Math.min(9, Math.sqrt(n.voisins.length) * 1.6);
    if (centre >= 0) {
      const c = parIndice.get(centre);
      if (c) { c.x = 0; c.y = 0; c.centre = true; }
    }
    compte.textContent = `${noeuds.length} page${noeuds.length > 1 ? "s" : ""}, ${liens.length} lien${liens.length > 1 ? "s" : ""}`;
    legende.replaceChildren(...[...new Set(noeuds.map((n) => n.d))].sort((a, b) => a.localeCompare(b, "fr")).map((d) => {
      const li = document.createElement("li");
      const pastille = document.createElement("span");
      pastille.className = "graphe-pastille";
      pastille.style.background = couleurDossier(d);
      li.append(pastille, document.createTextNode(d || "(racine)"));
      return li;
    }));
    survol = choisi = trouve = null;
    info.hidden = true;
    rechauffer(1);
  }

  // -- Simulation ---------------------------------------------------------------

  function etape() {
    const portee2 = 140 * 140;
    const cellule = 140;
    const grille = new Map();
    for (const n of noeuds) {
      const cle = Math.floor(n.x / cellule) + "," + Math.floor(n.y / cellule);
      (grille.get(cle) || grille.set(cle, []).get(cle)).push(n);
    }
    for (const n of noeuds) {
      const cx = Math.floor(n.x / cellule), cy = Math.floor(n.y / cellule);
      for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
        const voisins = grille.get((cx + dx) + "," + (cy + dy));
        if (!voisins) continue;
        for (const m of voisins) {
          if (m === n) continue;
          let ex = n.x - m.x, ey = n.y - m.y;
          let d2 = ex * ex + ey * ey;
          if (d2 > portee2) continue;
          if (d2 < 0.01) { ex = Math.random() - 0.5; ey = Math.random() - 0.5; d2 = 0.25; }
          const f = 900 / d2;
          n.vx += ex * f * 0.02;
          n.vy += ey * f * 0.02;
        }
      }
    }
    for (const [a, b] of liens) {
      const ex = b.x - a.x, ey = b.y - a.y;
      const d = Math.sqrt(ex * ex + ey * ey) || 1;
      const f = (d - 55) * 0.02;
      const fx = (ex / d) * f, fy = (ey / d) * f;
      a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
    }
    for (const n of noeuds) {
      n.vx -= n.x * 0.004;
      n.vy -= n.y * 0.004;
      if (n.fixe) { n.vx = n.vy = 0; continue; }
      n.vx *= 0.6; n.vy *= 0.6;
      const v = Math.hypot(n.vx, n.vy), max = 25;
      if (v > max) { n.vx *= max / v; n.vy *= max / v; }
      n.x += n.vx * chaleur;
      n.y += n.vy * chaleur;
    }
    chaleur *= 0.985;
  }

  function rechauffer(c) {
    chaleur = Math.max(chaleur, c);
    animer();
  }

  function animer() {
    if (dessinPrevu) return;
    dessinPrevu = true;
    requestAnimationFrame(() => {
      dessinPrevu = false;
      if (chaleur > 0.02) {
        const debut = performance.now();
        do etape(); while (chaleur > 0.02 && performance.now() - debut < 12);
        dessiner();
        if (chaleur > 0.02) animer();
      } else {
        dessiner();
      }
    });
  }

  // -- Dessin -----------------------------------------------------------------

  function taille() {
    const r = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(r.width * dpr);
    canvas.height = Math.round(r.height * dpr);
    animer();
  }

  const versEcran = (n) => [n.x * vue.k + vue.x + canvas.clientWidth / 2, n.y * vue.k + vue.y + canvas.clientHeight / 2];
  const versGraphe = (px, py) => [(px - vue.x - canvas.clientWidth / 2) / vue.k, (py - vue.y - canvas.clientHeight / 2) / vue.k];

  function dessiner() {
    const c = variables();
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
    const actif = survol || choisi || trouve;
    const proches = actif ? new Set([actif, ...actif.voisins]) : null;
    ctx.save();
    ctx.translate(vue.x + canvas.clientWidth / 2, vue.y + canvas.clientHeight / 2);
    ctx.scale(vue.k, vue.k);
    ctx.lineWidth = 1 / vue.k;
    for (const [a, b] of liens) {
      const fort = actif && (a === actif || b === actif);
      ctx.strokeStyle = fort ? c.accent : c.bord;
      ctx.globalAlpha = actif && !fort ? 0.25 : fort ? 1 : 0.9;
      ctx.lineWidth = (fort ? 2 : 1) / vue.k;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    }
    for (const n of noeuds) {
      ctx.globalAlpha = proches && !proches.has(n) ? 0.25 : 1;
      ctx.fillStyle = couleurDossier(n.d);
      ctx.beginPath();
      ctx.arc(n.x, n.y, n.rayon, 0, Math.PI * 2);
      ctx.fill();
      if (n === actif || n.centre) {
        ctx.lineWidth = 2.5 / vue.k;
        ctx.strokeStyle = c.accent;
        ctx.stroke();
      }
    }
    ctx.restore();
    // Titres : la page active et ses voisines, sinon les pages les plus liées d'abord
    // (toutes en grand zoom) ; un titre qui en chevaucherait un autre n'est pas écrit.
    ctx.font = "12px " + (getComputedStyle(document.body).fontFamily || "sans-serif");
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const places = [];
    const candidats = noeuds
      .filter((n) => (proches ? proches.has(n) : vue.k > 1.3 || n.voisins.length >= 3 || n.centre))
      .sort((a, b) => (b === actif) - (a === actif) || !!b.centre - !!a.centre || b.voisins.length - a.voisins.length);
    for (const n of candidats) {
      const [x, y] = versEcran(n);
      if (x < -100 || y < -20 || x > canvas.clientWidth + 100 || y > canvas.clientHeight + 20) continue;
      const titre = n.t.length > 40 ? n.t.slice(0, 39) + "…" : n.t;
      const l = ctx.measureText(titre).width + 6, haut = y + n.rayon * vue.k + 2;
      const boite = [x - l / 2, haut, x + l / 2, haut + 15];
      if (places.some((p) => boite[0] < p[2] && boite[2] > p[0] && boite[1] < p[3] && boite[3] > p[1])) continue;
      places.push(boite);
      ctx.globalAlpha = 1;
      ctx.lineWidth = 3;
      ctx.strokeStyle = c.fond;
      ctx.strokeText(titre, x, y + n.rayon * vue.k + 3);
      ctx.fillStyle = n === actif ? c.accent : c.texte;
      ctx.fillText(titre, x, y + n.rayon * vue.k + 3);
    }
    ctx.globalAlpha = 1;
  }

  // -- Interactions -------------------------------------------------------------

  function noeudSous(px, py) {
    const [gx, gy] = versGraphe(px, py);
    let meilleur = null, dmin = Infinity;
    for (const n of noeuds) {
      const d = Math.hypot(n.x - gx, n.y - gy);
      const marge = n.rayon + 6 / vue.k;
      if (d < marge && d < dmin) { meilleur = n; dmin = d; }
    }
    return meilleur;
  }

  function montrerInfo(n) {
    if (!n) { info.hidden = true; return; }
    info.replaceChildren();
    const titre = document.createElement("b");
    titre.textContent = n.t;
    const detail = document.createElement("span");
    detail.className = "muted";
    detail.textContent = ` · ${n.voisins.length} lien${n.voisins.length > 1 ? "s" : ""}${n.d ? " · " + n.d : ""} `;
    const ouvrir = document.createElement("a");
    ouvrir.className = "button small";
    ouvrir.href = n.u;
    ouvrir.textContent = "Ouvrir";
    const centrer = document.createElement("a");
    centrer.className = "button small";
    centrer.href = "?page=" + encodeURIComponent(n.k);
    centrer.textContent = "Centrer ici";
    info.append(titre, detail, ouvrir, " ", centrer);
    info.hidden = false;
  }

  const pointeurs = new Map();
  let geste = null;

  canvas.addEventListener("pointerdown", (e) => {
    canvas.setPointerCapture(e.pointerId);
    pointeurs.set(e.pointerId, { x: e.offsetX, y: e.offsetY });
    if (pointeurs.size === 2) {
      const [a, b] = [...pointeurs.values()];
      geste = { type: "pince", d: Math.hypot(a.x - b.x, a.y - b.y), k: vue.k, mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2 };
      return;
    }
    const n = noeudSous(e.offsetX, e.offsetY);
    geste = { type: n ? "noeud" : "vue", n, x0: e.offsetX, y0: e.offsetY, vx: vue.x, vy: vue.y, bouge: false, tactile: e.pointerType !== "mouse" };
    if (n) n.fixe = true;
  });

  canvas.addEventListener("pointermove", (e) => {
    if (pointeurs.has(e.pointerId)) pointeurs.set(e.pointerId, { x: e.offsetX, y: e.offsetY });
    if (!geste) {
      const n = noeudSous(e.offsetX, e.offsetY);
      if (n !== survol) {
        survol = n;
        canvas.style.cursor = n ? "pointer" : "grab";
        animer();
      }
      return;
    }
    if (geste.type === "pince" && pointeurs.size === 2) {
      const [a, b] = [...pointeurs.values()];
      zoomer(geste.k * Math.hypot(a.x - b.x, a.y - b.y) / (geste.d || 1), geste.mx, geste.my);
      return;
    }
    const dx = e.offsetX - geste.x0, dy = e.offsetY - geste.y0;
    if (Math.hypot(dx, dy) > 4) geste.bouge = true;
    if (!geste.bouge) return;
    if (geste.type === "noeud") {
      const [gx, gy] = versGraphe(e.offsetX, e.offsetY);
      geste.n.x = gx; geste.n.y = gy;
      rechauffer(0.3);
    } else if (geste.type === "vue") {
      vue.x = geste.vx + dx; vue.y = geste.vy + dy;
      canvas.style.cursor = "grabbing";
      animer();
    }
  });

  function fin(e) {
    pointeurs.delete(e.pointerId);
    if (!geste) return;
    const g = geste;
    if (g.type === "pince") { if (pointeurs.size === 0) geste = null; return; }
    geste = null;
    if (g.n) g.n.fixe = false;
    canvas.style.cursor = "grab";
    if (g.bouge || e.type === "pointercancel") return;
    if (g.n) {
      if (!g.tactile) { window.location.href = g.n.u; return; }
      choisi = g.n; // tactile : choisir d'abord, « Ouvrir » ensuite
      montrerInfo(g.n);
    } else {
      choisi = null;
      montrerInfo(null);
    }
    animer();
  }
  canvas.addEventListener("pointerup", fin);
  canvas.addEventListener("pointercancel", fin);
  canvas.addEventListener("pointerleave", () => { if (!geste && survol) { survol = null; animer(); } });

  function zoomer(k, px, py) {
    k = Math.min(6, Math.max(0.1, k));
    const [gx, gy] = versGraphe(px, py);
    vue.k = k;
    vue.x = px - canvas.clientWidth / 2 - gx * k;
    vue.y = py - canvas.clientHeight / 2 - gy * k;
    animer();
  }
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    zoomer(vue.k * Math.exp(-e.deltaY * 0.0015), e.offsetX, e.offsetY);
  }, { passive: false });

  function cadrer() {
    if (!noeuds.length) return;
    const xs = noeuds.map((n) => n.x), ys = noeuds.map((n) => n.y);
    const l = Math.max(...xs) - Math.min(...xs) + 80, h = Math.max(...ys) - Math.min(...ys) + 80;
    vue.k = Math.min(2, Math.max(0.15, Math.min(canvas.clientWidth / l, canvas.clientHeight / h)));
    vue.x = -((Math.max(...xs) + Math.min(...xs)) / 2) * vue.k;
    vue.y = -((Math.max(...ys) + Math.min(...ys)) / 2) * vue.k;
  }

  recherche.addEventListener("input", () => {
    const q = replier(recherche.value.trim());
    trouve = q ? noeuds.find((n) => replier(n.t).includes(q)) || null : null;
    montrerInfo(trouve);
    animer();
  });
  recherche.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || !trouve) return;
    e.preventDefault();
    vue.k = Math.max(vue.k, 1.5);
    vue.x = -trouve.x * vue.k;
    vue.y = -trouve.y * vue.k;
    animer();
  });
  if (portee) portee.addEventListener("change", construire);
  isolees.addEventListener("change", construire);
  new ResizeObserver(taille).observe(canvas);
  new MutationObserver(animer).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", animer);

  fetch(canvas.dataset.url, { credentials: "same-origin" })
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))))
    .then((donnees) => {
      tous = donnees;
      tous.adjacence = donnees.noeuds.map(() => []);
      for (const [a, b] of donnees.liens) { tous.adjacence[a].push(b); tous.adjacence[b].push(a); }
      construire();
      // Placement initial calculé d'un coup (sans animation), puis cadrage.
      for (let i = 0; i < 250 && chaleur > 0.05; i++) etape();
      cadrer();
      animer();
    })
    .catch(() => { compte.textContent = "Graphe indisponible (hors connexion ?)."; });
})();
