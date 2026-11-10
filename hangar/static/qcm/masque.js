// Éditeur de cartes à image masquée : une image, des rectangles tracés à la souris ou
// au doigt (en % de l'image), un nom pour chacun ; enregistrement en JSON (masque_views.py).
(function () {
  "use strict";
  const page = document.getElementById("masque-editeur");
  if (!page) return;
  const donnees = JSON.parse(document.getElementById("masque-donnees").textContent);
  const images = JSON.parse(document.getElementById("masque-images-json").textContent);
  const scene = page.querySelector("[data-scene]");
  const img = page.querySelector("[data-image]");
  const calque = page.querySelector("[data-calque]");
  const vide = page.querySelector("[data-vide]");
  const liste = page.querySelector("[data-zones]");
  const compte = page.querySelector("[data-compte]");
  const choix = page.querySelector("[data-image-choix]");
  const envoi = page.querySelector("[data-image-fichier]");
  const form = page.querySelector("[data-masque-form]");
  const etat = page.querySelector("[data-masque-etat]");
  const bouton = form.querySelector("button[type=submit]");
  const MAX_ZONES = 60;

  let zones = [];
  let imageRef = "";
  let fichier = null;
  let choisie = null;
  let geste = null;
  let modifie = false;
  let setId = donnees ? donnees.set : "";

  const borner = (v, min, max) => Math.min(max, Math.max(min, v));
  const arrondi = (v) => Math.round(v * 100) / 100;

  function dire(texte, classe) {
    etat.textContent = texte;
    etat.className = classe || "";
  }

  function nouvelId() {
    let n = zones.length + 1;
    while (zones.some((z) => z.id === "z" + n)) n++;
    return "z" + n;
  }

  // -- Image ------------------------------------------------------------------

  function montrerImage(url) {
    img.src = url;
    scene.hidden = false;
    vide.hidden = true;
  }

  choix.addEventListener("change", () => {
    const trouvee = images.find((i) => i.lien === choix.value.trim());
    if (!trouvee) {
      if (choix.value.trim()) dire("Image inconnue : choisissez-la dans la liste proposée.", "erreur");
      return;
    }
    imageRef = trouvee.lien;
    fichier = null;
    envoi.value = "";
    modifie = true;
    montrerImage(trouvee.url);
    dire("");
  });

  envoi.addEventListener("change", () => {
    const f = envoi.files[0];
    if (!f) return;
    if (f.size > 10 * 1024 * 1024) {
      dire("Image trop grosse (10 Mo au plus).", "erreur");
      envoi.value = "";
      return;
    }
    fichier = f;
    imageRef = "";
    choix.value = "";
    modifie = true;
    const lecteur = new FileReader(); // data: (les adresses blob: sont refusées par la politique de sécurité)
    lecteur.onload = () => montrerImage(lecteur.result);
    lecteur.readAsDataURL(f);
    dire("");
  });

  // -- Zones ------------------------------------------------------------------

  function dessinerZones() {
    calque.replaceChildren(...zones.map((z, i) => {
      const d = document.createElement("div");
      d.className = "masque-zone" + (z === choisie ? " choisie" : "") + (z.reponse.trim() ? "" : " sans-nom");
      d.dataset.zone = z.id;
      d.style.left = z.x + "%";
      d.style.top = z.y + "%";
      d.style.width = z.l + "%";
      d.style.height = z.h + "%";
      d.title = z.reponse || "Zone " + (i + 1) + " sans nom";
      const n = document.createElement("span");
      n.className = "masque-numero";
      n.textContent = i + 1;
      d.append(n);
      if (z === choisie) {
        const p = document.createElement("span");
        p.className = "masque-poignee";
        p.dataset.poignee = "";
        d.append(p);
      }
      return d;
    }));
    compte.textContent = zones.length ? `(${zones.length})` : "";
  }

  function dessinerListe(focaliser) {
    let champAFocaliser = null;
    liste.replaceChildren(...zones.map((z, i) => {
      const li = document.createElement("li");
      li.className = z === choisie ? "choisie" : "";
      const champ = document.createElement("input");
      champ.className = "input";
      champ.value = z.reponse;
      champ.placeholder = "Ce que cache la zone " + (i + 1);
      champ.setAttribute("aria-label", "Nom de la zone " + (i + 1));
      champ.maxLength = 200;
      champ.addEventListener("input", () => {
        z.reponse = champ.value;
        modifie = true;
        dessinerZones();
      });
      champ.addEventListener("focus", () => {
        if (choisie !== z) { choisie = z; dessinerZones(); marquerListe(); }
      });
      champ.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); champ.blur(); }
      });
      const suppr = document.createElement("button");
      suppr.type = "button";
      suppr.className = "btn btn-ghost btn-icon btn-xs";
      suppr.textContent = "✕";
      suppr.title = "Effacer cette zone";
      suppr.setAttribute("aria-label", "Effacer la zone " + (i + 1));
      suppr.addEventListener("click", () => effacer(z));
      li.append(champ, suppr);
      if (focaliser === z) champAFocaliser = champ;
      return li;
    }));
    if (champAFocaliser) champAFocaliser.focus();
  }

  function marquerListe() {
    [...liste.children].forEach((li, i) => li.classList.toggle("choisie", zones[i] === choisie));
  }

  function effacer(z) {
    zones = zones.filter((x) => x !== z);
    if (choisie === z) choisie = null;
    modifie = true;
    dessinerZones();
    dessinerListe();
  }

  function position(e) {
    const r = calque.getBoundingClientRect();
    return [borner(((e.clientX - r.left) / r.width) * 100, 0, 100), borner(((e.clientY - r.top) / r.height) * 100, 0, 100)];
  }

  calque.addEventListener("pointerdown", (e) => {
    if (e.button > 0) return;
    e.preventDefault();
    calque.setPointerCapture(e.pointerId);
    const [px, py] = position(e);
    const surZone = e.target.closest("[data-zone]");
    if (surZone) {
      const z = zones.find((x) => x.id === surZone.dataset.zone);
      choisie = z;
      geste = { type: e.target.closest("[data-poignee]") ? "taille" : "deplacer", z, px, py, x: z.x, y: z.y, l: z.l, h: z.h, bouge: false };
      dessinerZones();
      marquerListe();
    } else {
      if (zones.length >= MAX_ZONES) {
        dire(`${MAX_ZONES} zones au plus.`, "erreur");
        return;
      }
      geste = { type: "tracer", z: { id: nouvelId(), x: px, y: py, l: 0, h: 0, reponse: "" }, px, py };
    }
  });

  calque.addEventListener("pointermove", (e) => {
    if (!geste) return;
    const [px, py] = position(e);
    const g = geste;
    const z = g.z;
    if (g.type === "tracer") {
      z.x = Math.min(px, g.px); z.y = Math.min(py, g.py);
      z.l = Math.abs(px - g.px); z.h = Math.abs(py - g.py);
      if (!zones.includes(z) && z.l > 0.8 && z.h > 0.8) { zones.push(z); choisie = z; }
    } else if (g.type === "deplacer") {
      if (Math.abs(px - g.px) + Math.abs(py - g.py) > 0.3) g.bouge = true;
      z.x = borner(g.x + px - g.px, 0, 100 - z.l);
      z.y = borner(g.y + py - g.py, 0, 100 - z.h);
    } else {
      z.l = borner(g.l + px - g.px, 1, 100 - z.x);
      z.h = borner(g.h + py - g.py, 1, 100 - z.y);
    }
    modifie = true;
    dessinerZones();
  });

  function finGeste() {
    if (!geste) return;
    const g = geste;
    geste = null;
    if (g.type === "tracer") {
      if (!zones.includes(g.z)) return;
      if (g.z.l < 1.5 || g.z.h < 1.5) { effacer(g.z); return; }
      dessinerZones();
      dessinerListe(g.z); // nommer la zone tout de suite
    } else if (g.type !== "deplacer" || !g.bouge) {
      dessinerListe(g.z);
    }
  }
  calque.addEventListener("pointerup", finGeste);
  calque.addEventListener("pointercancel", finGeste);

  document.addEventListener("keydown", (e) => {
    if (!choisie || (e.key !== "Delete" && e.key !== "Backspace")) return;
    if (e.target.closest("input, textarea, select")) return;
    e.preventDefault();
    effacer(choisie);
  });

  // -- Enregistrement ---------------------------------------------------------

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!imageRef && !fichier) return dire("Choisissez d'abord une image.", "erreur");
    if (!zones.length) return dire("Tracez au moins une zone sur l'image.", "erreur");
    const sansNom = zones.find((z) => !z.reponse.trim());
    if (sansNom) {
      dire(`Nommez la zone ${zones.indexOf(sansNom) + 1} : ce qu'elle cache est la réponse de sa carte.`, "erreur");
      choisie = sansNom;
      dessinerZones();
      dessinerListe(sansNom);
      return;
    }
    const corps = new FormData(form);
    corps.set("masquer_tout", form.elements.masquer_tout.checked ? "1" : "0");
    corps.set("zones", JSON.stringify(zones.map((z) => ({ id: z.id, x: arrondi(z.x), y: arrondi(z.y), l: arrondi(z.l), h: arrondi(z.h), reponse: z.reponse.trim() }))));
    corps.set("image", imageRef);
    if (fichier) corps.set("fichier", fichier);
    if (setId) corps.set("set", setId);
    bouton.disabled = true;
    dire("Enregistrement…");
    try {
      const r = await fetch(page.dataset.url, { method: "POST", body: corps, headers: { "X-CSRF-Token": (window.HANGAR && window.HANGAR.csrf) || "", "X-Requested-With": "fetch" }, credentials: "same-origin" });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || "Enregistrement impossible (" + r.status + ").");
      setId = data.set;
      imageRef = data.image;
      fichier = null;
      envoi.value = "";
      modifie = false;
      etat.replaceChildren(document.createTextNode(data.message + " "));
      etat.className = "ok";
      const jouer = document.createElement("a");
      jouer.className = "btn btn-sm";
      jouer.href = data.jouer;
      jouer.textContent = "Réviser maintenant";
      etat.append(jouer);
    } catch (err) {
      dire(err.message, "erreur");
    } finally {
      bouton.disabled = false;
    }
  });

  window.addEventListener("beforeunload", (e) => {
    if (modifie) e.preventDefault();
  });

  // -- Set existant -------------------------------------------------------------

  if (donnees) {
    form.elements.titre.value = donnees.titre || "";
    form.elements.question.value = donnees.question || "";
    form.elements.masquer_tout.checked = donnees.masquer_tout !== false;
    zones = (donnees.zones || []).map((z) => ({ id: String(z.id), x: +z.x, y: +z.y, l: +z.l, h: +z.h, reponse: String(z.reponse || "") }));
    imageRef = donnees.image || "";
    choix.value = imageRef;
    if (donnees.image_url) montrerImage(donnees.image_url);
    dessinerZones();
    dessinerListe();
  }
})();
