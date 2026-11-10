"""QCM : logique commune aux pages, au résumé quotidien et au panneau IA.

- file du jour d'un set, résumé d'un set (port de `quiz_views.py` de Revision) ;
- `resume_du_jour(user)` : ce qu'il y a à réviser (affiché sur le tableau de bord de l'agenda) ;
- section « Révisions » du résumé quotidien par e-mail (`hangar.digest`) ;
- résolveur de contexte du panneau IA pour les adresses `/qcm/…`.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from flask import has_request_context, request, url_for
from markupsafe import escape

from . import langues, planning, quiz
from .models import _JAMAIS, Activite, ObjectifRevision, QuizDeck, QuizProgress, TempsRevision

JOURS_EXAMEN_PROCHE = 14  # objectifs d'examen signalés dans le résumé quotidien
JOURS_PREVISION = 14
MOIS_COURTS = ["Jan", "Fév", "Mars", "Avr", "Mai", "Juin", "Juil", "Août", "Sep", "Oct", "Nov", "Déc"]


def bibliotheque(user_id):
    """Bibliothèque vue par un compte : sets communs et son espace personnel."""
    return quiz.get_library().pour(user_id)


def peut_modifier(user) -> bool:
    """Gestion des QCM du coffre (import, rangement, suppression) : admins et membres, pas les invités."""
    return bool(user and user.is_authenticated and not user.is_guest)


def matiere_de(qset):
    """Matière d'un set pour les temps de révision : sa langue, sinon sa catégorie."""
    return qset.langue or qset.categorie


# ---------------------------------------------------------------------------
# File du jour et résumé d'un set (quiz_views.py de Revision)
# ---------------------------------------------------------------------------
def file_du_jour(qset, progress, deck, now):
    """Questions à réviser aujourd'hui, et leur répartition.

    Ordre : questions ratées aujourd'hui (réapprentissage, hors quota), puis
    révisions (les plus menacées d'oubli, puis les plus anciennes), puis nouvelles.
    Les quotas du set tiennent compte de ce qui a déjà été fait aujourd'hui.
    Renvoie (file, {"dues", "nouvelles", "revisions", "reapprendre"}).
    """
    aujourdhui = quiz.jour(now)
    reapprendre, vues, nouvelles = [], [], []
    for q in qset.questions:
        st = progress.get(q.id)
        if st is None:
            nouvelles.append(q)
        elif st.is_due(now):
            if st.last_review and quiz.jour(st.last_review) == aujourdhui:
                reapprendre.append(q)
            else:
                vues.append((st, q))
    vues.sort(key=lambda t: (t[0].urgence(now), t[0].last_review or _JAMAIS))
    vues = [q for _, q in vues]
    dues = len(reapprendre) + len(vues) + len(nouvelles)
    quota_n = None
    if deck is not None:
        deja_n, deja_r = planning.faites_aujourdhui(progress, now)
        quota_n, quota_r = planning.quotas_effectifs(qset, progress, deck, now)
        places_r = planning.restant(quota_r, deja_r)
        places_n = planning.restant(quota_n, deja_n)
        vues = vues[:places_r] if places_r is not None else vues
        nouvelles = nouvelles[:places_n] if places_n is not None else nouvelles
    detail = {"dues": dues, "nouvelles": len(nouvelles), "revisions": len(vues), "reapprendre": len(reapprendre),
              "quota_nouvelles": quota_n is not None}
    return reapprendre + vues + nouvelles, detail


def question_ratee(st):
    """Question à retravailler : ratée la dernière fois, ou réussie une fois sur deux au plus."""
    return st.last_correct is False or (st.seen >= 2 and st.correct * 2 <= st.seen)


def resume_set(qset, progress, now, deck=None):
    """Statistiques d'un set (maîtrise, à réviser, précision, répartition)."""
    seen = corrects = total_rep = maitrisees = 0
    maitrise = retention = 0.0
    repartition = {b: 0 for b in range(quiz.BOITE_MAX + 1)}
    for q in qset.questions:
        st = progress.get(q.id)
        if st:
            seen += 1
            corrects += st.correct
            total_rep += st.seen
            repartition[min(st.box, quiz.BOITE_MAX)] += 1
            # Maîtrise : stabilité FSRS rapportée à MAITRISE_JOURS (boîte de Leitner pour une progression ancienne).
            maitrise += min(1.0, st.stabilite / langues.MAITRISE_JOURS) if st.stabilite is not None else st.box / quiz.BOITE_MAX
            retention += st.urgence(now)
            maitrisees += st.stabilite is not None and st.stabilite >= langues.MAITRISE_JOURS
        else:
            repartition[0] += 1
    file, detail = file_du_jour(qset, progress, deck, now)
    ids = {q.id for q in qset.questions}
    return {
        "total": len(qset),
        "rencontrees": seen,
        "maitrisees": maitrisees,
        "retention": retention / seen * 100 if seen else None,
        "a_reviser": len(file),
        "ratees": sum(1 for qid, st in progress.items() if qid in ids and question_ratee(st)),
        **detail,
        "quota_atteint": detail["dues"] > 0 and not file,
        "precision": (corrects / total_rep * 100) if total_rep else 0.0,
        "repartition": repartition,
        "pct_maitrise": maitrise / (len(qset) or 1) * 100,
    }


def resume_langue(user_id, langue, sets, now, prog=None):
    """Résumé d'une langue : à revoir, nouveaux disponibles (langues.resume)."""
    return langues.resume(sets, prog if prog is not None else QuizProgress.map_langue(user_id, sets), now)


def dossier_de(set_langue):
    """Dossier d'un fichier de langue : le nom du dossier qui le contient."""
    return (set_langue.path.parent.name if set_langue.path else "") or quiz.CATEGORIE_DEFAUT


def previsions(user_id, library, now, jours=JOURS_PREVISION):
    """Révisions à venir (QCM et langues) : une case par jour, les retards comptés aujourd'hui."""
    aujourdhui = quiz.jour(now)
    compteurs = [0] * jours
    connus = set(library.sets) | set(library.langues)
    for p in QuizProgress.query.filter(QuizProgress.user_id == user_id, QuizProgress.last_review.isnot(None)):
        if p.set_id not in connus:
            continue
        echeance = p.echeance or (p.last_review + timedelta(days=quiz.INTERVALLES_JOURS.get(p.box, 30)))
        d = (quiz.jour(echeance) - aujourdhui).days
        if d < jours:
            compteurs[max(0, d)] += 1
    return [{"jour": aujourdhui + timedelta(days=d), "n": compteurs[d]} for d in range(jours)]


def mois_du_calendrier(grille):
    """Étiquettes de mois au-dessus du calendrier : (colonne, nom) à chaque changement de mois."""
    etiquettes, precedent = [], None
    for i, semaine in enumerate(grille):
        mois = semaine[0]["jour"].month
        if mois != precedent:
            etiquettes.append((i + 1, MOIS_COURTS[mois - 1]))
            precedent = mois
    return etiquettes


# ---------------------------------------------------------------------------
# Vue d'ensemble d'un compte (tuiles, menu, page « Aujourd'hui », résumé quotidien)
# ---------------------------------------------------------------------------
def etat_page(user_id):
    """`etat()` d'une page : calculé une fois par requête (la page et le menu de gauche le partagent)."""
    if not has_request_context():
        return etat(user_id)
    deja = getattr(request, "_qcm_etat", None)  # sur la requête (g peut survivre à plusieurs requêtes)
    if deja is not None and deja[0] == user_id:
        return deja[1]
    e = etat(user_id)
    request._qcm_etat = (user_id, e)
    return e


def etat(user_id, now=None, library=None):
    """Tout ce qui est dû pour un compte : par set, par langue, totaux."""
    now = now or quiz.maintenant()
    library = library or bibliotheque(user_id)
    decks = QuizDeck.map_for(user_id)
    tous = QuizProgress.map_all(user_id)
    sets = []
    tot = {"a_reviser": 0, "revisions": 0, "nouvelles": 0, "nouvelles_prevues": 0, "dues": 0}
    for qset in library.sorted_sets():
        progress = tous.get(qset.id, {})
        deck = decks.get(qset.id)
        s = resume_set(qset, progress, now, deck)
        revisions = s["revisions"] + s["reapprendre"]
        sets.append({"set": qset, "summary": s, "deck": deck, "revisions": revisions})
        tot["a_reviser"] += s["a_reviser"]
        tot["revisions"] += revisions
        tot["nouvelles"] += s["nouvelles"]
        tot["dues"] += s["dues"]
        if s["quota_nouvelles"]:
            tot["nouvelles_prevues"] += s["nouvelles"]
    langs = []
    for nom in library.noms_langues():
        fichiers = library.sets_langue(nom)
        if not fichiers:
            continue
        ids = {f.id for f in fichiers}
        prog = {(sid, qid): p for sid, d in tous.items() if sid in ids for qid, p in d.items()}
        r = resume_langue(user_id, nom, fichiers, now, prog)
        langs.append({"nom": nom, "a_revoir": r["a_revoir"], "nouveaux": r["nouveaux_disponibles"], "resume": r,
                      "fichiers": fichiers})
    out = {"sets": sets, "langues": langs, **tot,
           "langues_a_revoir": sum(l["a_revoir"] for l in langs),
           "langues_nouveaux": sum(l["nouveaux"] for l in langs),
           "decks": decks, "library": library}
    out["cartes_dues"] = out["revisions"] + out["langues_a_revoir"]
    return out


def objectifs(user_id, library, decks, now, jours_max=None):
    """Examens à venir (date d'examen d'un set), avec l'estimation du planning."""
    jour = quiz.jour(now)
    out = []
    for deck in decks.values():
        if deck.date_objectif is None or deck.date_objectif < jour:
            continue
        if jours_max is not None and (deck.date_objectif - jour).days > jours_max:
            continue
        qset = library.get(deck.set_id)
        if qset is None:
            continue
        e = planning.estimer(qset, QuizProgress.map_for(user_id, qset.id), deck, now)
        out.append({"set": qset, "date": deck.date_objectif, "jours": (deck.date_objectif - jour).days, "e": e})
    out.sort(key=lambda x: (x["date"], x["set"].titre.lower()))
    return out


def objectifs_par_categorie(examens):
    """Examens regroupés par (catégorie, date) : un examen de catégorie fixe la même date à tous ses sets."""
    groupes: dict = {}
    for x in examens:
        cle = (x["set"].langue, x["set"].categorie, x["date"])
        groupes.setdefault(cle, []).append(x)
    out = []
    for (langue, cat, date_), xs in groupes.items():
        statuts = [x["e"].get("statut") for x in xs]
        out.append({"categorie": cat, "langue": langue, "date": date_, "jours": xs[0]["jours"], "sets": xs,
                    "pret": all(x["e"].get("su") for x in xs),
                    "retard": any(s in ("retard", "impossible") for s in statuts),
                    "a_commencer": any(x["e"].get("commencer_maintenant") for x in xs)})
    out.sort(key=lambda o: (o["date"], o["categorie"].lower()))
    return out


def resume_du_jour(user, jour=None) -> dict:
    """Ce qu'il y a à réviser pour un compte (tableau de bord de l'agenda, résumé quotidien).

    Clés : `cartes_dues` (révisions dues : cartes déjà vues revenues à échéance, QCM et langues),
    `revisions` (QCM seuls), `langues_a_revoir`, `nouvelles` (nouvelles prévues par les quotas
    des sets), `nouvelles_disponibles` (toutes les nouvelles du jour, quotas compris),
    `a_reviser` (taille de la révision intelligente), `serie`, `record`, `aujourdhui` (réponses du
    jour), `objectif` (réponses par jour) ; `par_matiere` [{matiere, cartes}], `par_set`
    [{titre, categorie, cartes, nouvelles, url}], `objectifs` [{titre, categorie, date, jours,
    statut, url}] (examens à moins de 14 jours) ; `url` (/qcm/aujourdhui/), `url_reviser`.

    `jour` : le jour résumé (aujourd'hui par défaut) ; pour un autre jour, l'état est calculé
    à la fin de ce jour-là (cartes qui arriveront à échéance dans la journée comprises)."""
    maintenant = quiz.maintenant()
    jour = jour or maintenant.date()
    # Cartes qui arrivent à échéance plus tard dans la journée : dues « ce jour-là ».
    now = max(maintenant, datetime.combine(jour, time(23, 59)))
    e = etat(user.id, now)
    act = Activite.resume(user.id, maintenant.date())
    par_matiere: dict = {}
    par_set = []
    for x in e["sets"]:
        cartes = x["revisions"] + (x["summary"]["nouvelles"] if x["summary"]["quota_nouvelles"] else 0)
        if not cartes:
            continue
        par_matiere[matiere_de(x["set"])] = par_matiere.get(matiere_de(x["set"]), 0) + cartes
        par_set.append({"titre": x["set"].titre, "categorie": x["set"].categorie, "cartes": cartes,
                        "revisions": x["revisions"],
                        "nouvelles": x["summary"]["nouvelles"] if x["summary"]["quota_nouvelles"] else 0,
                        "url": _url("qcm.play", set=x["set"].id, mode="review")})
    for l in e["langues"]:
        if l["a_revoir"]:
            par_matiere[l["nom"]] = par_matiere.get(l["nom"], 0) + l["a_revoir"]
    par_set.sort(key=lambda s: (-s["cartes"], s["titre"].lower()))
    exams = []
    for x in objectifs(user.id, e["library"], e["decks"], now, JOURS_EXAMEN_PROCHE):
        exams.append({"titre": x["set"].titre, "categorie": x["set"].categorie, "date": x["date"],
                      "jours": (x["date"] - jour).days, "statut": "pret" if x["e"].get("su") else x["e"].get("statut"),
                      "commencer_maintenant": bool(x["e"].get("commencer_maintenant")),
                      "url": _url("qcm.planning_page") + "#" + ancre(x["set"].id)})
    nouvelles = e["nouvelles_prevues"]
    return {
        "cartes_dues": e["cartes_dues"] + nouvelles,
        "revisions": e["revisions"],
        "langues_a_revoir": e["langues_a_revoir"],
        "nouvelles": nouvelles,
        "nouvelles_disponibles": e["nouvelles"] + e["langues_nouveaux"],
        "a_reviser": e["a_reviser"],
        "serie": act["serie"], "record": act["record"], "aujourdhui": act["aujourdhui"],
        "objectif": ObjectifRevision.de(user.id),
        "par_matiere": sorted(({"matiere": m, "cartes": n} for m, n in par_matiere.items()),
                              key=lambda d: (-d["cartes"], d["matiere"].lower())),
        "par_set": par_set,
        "objectifs": exams,
        "url": _url("qcm.aujourdhui"),
        "url_reviser": _url("qcm.play", revision=1),
    }


def _url(endpoint, **kw):
    try:
        return url_for(endpoint, **kw)
    except Exception:  # noqa: BLE001 — hors contexte de requête
        return "/qcm/"


def ancre(set_id):
    from .paquets import slugify
    return "set-" + slugify(set_id.replace("/", "-"))


def chronometrer(user_id, set_id, duree, matiere="", quand=None):
    """Cumule un temps de réponse : estimation de la durée quotidienne des révisions
    (par set), et temps de révision par matière et par jour (statistiques)."""
    from .models import _get_or_create
    TempsRevision.noter(user_id, matiere, duree, quand)
    if isinstance(duree, (int, float)) and not isinstance(duree, bool) and duree > 0:
        deck = _get_or_create(QuizDeck, user_id=user_id, set_id=set_id)
        deck.temps_ms = (deck.temps_ms or 0) + int(min(duree, TempsRevision.DUREE_MAX_MS))
        deck.reponses_chronometrees = (deck.reponses_chronometrees or 0) + 1


# ---------------------------------------------------------------------------
# Résumé quotidien par e-mail
# ---------------------------------------------------------------------------
from .. import digest  # noqa: E402


@digest.section("qcm.revisions", ordre=30)
def _digest_revisions(ctx):
    r = resume_du_jour(ctx.user, ctx.jour)
    if not (r["cartes_dues"] or r["objectifs"]):
        return None
    lien = ctx.lien("/qcm/")
    bits = []
    if r["revisions"] or r["langues_a_revoir"]:
        bits.append(f"<b>{r['revisions'] + r['langues_a_revoir']}</b> révision{'s' if r['revisions'] + r['langues_a_revoir'] > 1 else ''} due{'s' if r['revisions'] + r['langues_a_revoir'] > 1 else ''}")
    if r["nouvelles"]:
        bits.append(f"<b>{r['nouvelles']}</b> nouvelle{'s' if r['nouvelles'] > 1 else ''} carte{'s' if r['nouvelles'] > 1 else ''} prévue{'s' if r['nouvelles'] > 1 else ''}")
    html = ""
    if bits:
        html += f'<div style="margin-bottom:6px">{" · ".join(bits)}'
        if r["serie"]:
            html += f' · série de <b>{r["serie"]}</b> jour{"s" if r["serie"] > 1 else ""} 🔥'
        html += "</div>"
    if r["par_matiere"]:
        html += digest.liste_html([f"{escape(m['matiere'])} : <b>{m['cartes']}</b>" for m in r["par_matiere"][:8]])
    if r["par_set"]:
        top = ", ".join(f"{escape(s['titre'])} ({s['cartes']})" for s in r["par_set"][:5])
        html += f'<div style="color:#6b7280;font-size:12.5px;margin-top:6px">Sets les plus chargés : {top}</div>'
    if r["objectifs"]:
        items = []
        for o in r["objectifs"][:6]:
            quand = "aujourd’hui" if o["jours"] == 0 else ("demain" if o["jours"] == 1 else f"dans {o['jours']} jours")
            etat_txt = {"pret": "prêt ✓", "retard": "en retard", "impossible": "en retard",
                        "ok": "à commencer" if o["commencer_maintenant"] else "dans les temps"}.get(o["statut"], "")
            items.append(f"Examen <b>{escape(o['titre'])}</b> ({escape(o['categorie'])}) {quand}"
                         + (f" — {etat_txt}" if etat_txt else ""))
        html += '<div style="font-size:12.5px;font-weight:600;color:#dc2626;margin:10px 0 2px">Examens à moins de 14 jours</div>'
        html += digest.liste_html(items)
    html += (f'<div style="margin-top:8px"><a href="{escape(lien)}" style="color:#3f76ff">Ouvrir les QCM →</a></div>')
    n = r["cartes_dues"]
    return digest.Section("qcm.revisions", "Révisions (QCM)", html, compte=n,
                          libelle=digest.pluriel(n, "carte") + " à réviser" if n else "",
                          couleur="#8b5cf6", a_faire=n > 0, tuile=n > 0,
                          extra={"serie": r["serie"], "objectifs": len(r["objectifs"])})


# ---------------------------------------------------------------------------
# Contexte du panneau IA
# ---------------------------------------------------------------------------
def contexte_ia(path, args, user):
    """Résumé de la page QCM affichée, transmis à l'assistant IA (hangar.ai.register_context)."""
    if not path.startswith("/qcm/") or user is None or not getattr(user, "is_authenticated", False):
        return None
    if path.startswith(("/qcm/expliquer", "/qcm/corriger-qrc", "/qcm/generer", "/qcm/cartes-de-la-page",
                        "/qcm/importer-vocabulaire")):
        return None
    try:
        library = bibliotheque(user.id)
        set_id = (args or {}).get("set") or ""
        qset = library.get(set_id) if set_id else None
        if qset is not None:
            progress = QuizProgress.map_for(user.id, qset.id)
            deck = QuizDeck.query.filter_by(user_id=user.id, set_id=qset.id).first()
            s = resume_set(qset, progress, quiz.maintenant(), deck)
            lignes = [f"Set de QCM « {qset.titre} » (catégorie {qset.categorie}, {len(qset)} questions)."]
            if qset.description:
                lignes.append(qset.description)
            lignes.append(f"Maîtrise {s['pct_maitrise']:.0f} %, {s['a_reviser']} à réviser aujourd'hui, "
                          f"{s['rencontrees']} questions déjà vues, précision {s['precision']:.0f} %, {s['ratees']} ratées.")
            if deck and deck.date_objectif:
                lignes.append(f"Examen prévu le {deck.date_objectif:%d/%m/%Y}.")
            if qset.source_page:
                lignes.append(f"Écrit d'après la page du wiki « {qset.source_page} ».")
            lignes.append("Questions :")
            taille = sum(len(l) for l in lignes)
            for q in qset.questions:
                if q.choix:
                    rep = " / ".join(q.choix[i] for i in q.reponses)
                    ligne = f"- [{q.type}] {q.enonce} — choix : {' | '.join(q.choix)} — réponse : {rep}"
                elif q.reponses_texte:
                    ligne = f"- [{q.type}] {q.enonce} — réponse : {' / '.join(q.reponses_texte)}"
                else:
                    ligne = f"- [{q.type}] {q.enonce} — verso : {q.verso}"
                taille += len(ligne)
                if taille > 12000:
                    lignes.append("- …")
                    break
                lignes.append(ligne)
            return {"type": "qcm_set", "title": qset.titre, "text": "\n".join(lignes),
                    "url": _url("qcm.play", set=qset.id, mode="all"), "set_id": qset.id}
        e = etat(user.id)
        act = Activite.resume(user.id)
        lignes = [f"Section QCM de révision : {len(library.sets)} sets, {len(library.langues)} paquets de langue.",
                  f"À réviser aujourd'hui : {e['cartes_dues']} révisions dues (cartes déjà vues), "
                  f"{e['nouvelles']} nouvelles questions disponibles.",
                  f"Série de {act['serie']} jour(s) (record {act['record']}), {act['aujourdhui']} réponses aujourd'hui, "
                  f"{act['justes_sept_jours']}/{act['sept_jours']} justes sur 7 jours."]
        charges = sorted((x for x in e["sets"] if x["revisions"]), key=lambda x: -x["revisions"])[:10]
        if charges:
            lignes.append("Sets les plus chargés : " + ", ".join(f"{x['set'].titre} ({x['revisions']})" for x in charges))
        faibles = sorted((x for x in e["sets"] if x["summary"]["rencontrees"]), key=lambda x: x["summary"]["pct_maitrise"])[:5]
        if faibles:
            lignes.append("Sets les moins maîtrisés : " + ", ".join(
                f"{x['set'].titre} ({x['summary']['pct_maitrise']:.0f} %)" for x in faibles))
        for l in e["langues"]:
            lignes.append(f"Langue {l['nom']} : {l['a_revoir']} à revoir, {l['nouveaux']} nouveaux disponibles.")
        return {"type": "qcm", "title": "QCM de révision", "text": "\n".join(lignes), "url": _url("qcm.index")}
    except Exception:  # noqa: BLE001 — le panneau IA ne doit jamais casser une page
        return None


try:
    from .. import ai as _ai
    _ai.register_context(contexte_ia)
except Exception:  # noqa: BLE001 — module IA absent
    pass
