"""Préparer un contrôle : tout ce qu'il faut au même endroit, révisions placées dans la semaine, notes.

- `preparer` : page « Préparer ce contrôle » (compte à rebours, cours du wiki lu ou non, sets de QCM avec
  leur maîtrise et leur statut, temps passé sur la matière, tâches liées). Le statut des QCM est estimé
  pour le jour du contrôle avec un réglage temporaire (rien n'est écrit en base : la date d'examen des
  sets n'est fixée que par le bouton « Fixer la date d'examen ») ;
- `indicateur` : le même statut en un mot (prêt, dans les temps, à commencer, en retard), sans adresse
  web, pour le tableau de bord et le résumé quotidien (calculé hors de toute visite de page) ;
- `proposer` / `appliquer` / `replanifier_apres_import` : blocs de révision placés dans les créneaux
  libres de l'emploi du temps, du jour où il faut commencer jusqu'à la veille du contrôle ;
- `figer_maitrise`, `a_noter`, `moyennes` : notes des contrôles, moyennes par matière, maîtrise des QCM
  le jour du contrôle (figée par le fil d'arrière-plan, l'historique des QCM n'étant pas gardé).

« Pages lues » reste une estimation : une page du wiki compte comme lue dès qu'elle a été ouverte une
fois, et les PDF ne sont pas suivis.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta

from . import agenda as A
from .extensions import db
from .models import AgendaEvent, AgendaGrade, AgendaTask, User, now
from .texte import cle_tri

# Statuts d'un contrôle : (libellé, classe du badge)
STATUTS = {
    "pret": ("Prêt", "success"),
    "dans_les_temps": ("Dans les temps", "accent"),
    "a_commencer": ("À commencer", "warning"),
    "retard": ("En retard", "danger"),
    "passe": ("Passé", ""),
    "a_relier": ("Matière à relier", ""),
}
MOTS_PAR_MINUTE = 220          # vitesse de lecture (comme la page du wiki)
FIN_SOIREE = 22                # les blocs de révision peuvent aller jusqu'à 22 h
REPAS = ((time(12, 0), time(13, 0)), (time(19, 0), time(20, 0)))  # jamais de révision pendant les repas
MARGE_APRES_COURS = timedelta(minutes=10)
BLOC_MIN, BLOC_MAX = 15, 120   # durée d'un bloc (minutes)
CRENEAU_CONFORT = timedelta(minutes=45)  # créneau préféré : assez long pour s'y mettre vraiment
CATEGORIE_TACHE = "Révisions"
JOURS_FIGER = 3                # maîtrise figée jusqu'à 3 jours après le contrôle (appli fermée ce jour-là)
JOURS_A_NOTER = 45             # contrôles passés sans note rappelés au tableau de bord
# Pages écrites par l'IA à côté d'une page du cours : fiches du kit de révision (« Page (Fiche) ») et versions
# proposées (« Page (IA) », « Page (IA 2) »). Ce ne sont pas des pages du cours à lire (même règle que le kit,
# hangar/ai/kit.py) : elles ne comptent ni dans les pages lues, ni dans le temps de lecture restant.
PAGES_IA = re.compile(r"\((?:fiche|ia(?: \d+)?)\)\s*$", re.I)


def libelle(statut: str | None) -> str:
    return STATUTS.get(statut or "", ("", ""))[0]


def classe(statut: str | None) -> str:
    return STATUTS.get(statut or "", ("", ""))[1]


def compte_a_rebours(ev: AgendaEvent, at: datetime) -> str:
    if ev.start <= at:
        return "en cours" if ev.end > at else "passé"
    jours = (ev.start.date() - at.date()).days
    if jours == 0:
        h = int((ev.start - at).total_seconds() // 3600)
        return f"dans {h} h" if h else "dans moins d’une heure"
    if jours == 1:
        return "demain"
    return f"dans {jours} jours"


# ---------------------------------------------------------------------------
# Rassembler : cours du wiki, sets de QCM, temps passé
# ---------------------------------------------------------------------------

def _lien(user, ev: AgendaEvent):
    return A._subject_link(user, ev.title)


def lecture(user, wiki_path: str) -> dict | None:
    """Pages du cours relié (collection du wiki ou page seule), lues ou non, et temps de lecture restant.

    Une page compte comme lue si elle a été ouverte au moins une fois (estimation) ; les PDF sont seulement
    comptés : leur lecture n'est pas suivie."""
    if not wiki_path:
        return None
    try:
        from .wiki.models import WikiVisit
        from .wiki.vault import get_vault
        vault = get_vault()
    except Exception:  # noqa: BLE001 - wiki indisponible
        return None
    path = wiki_path.strip("/")
    prefix = path.lower() + "/"
    seule = vault.notes.get(path.lower().removesuffix(".md"))
    notes = [seule] if seule is not None else [n for n in vault.notes.values()
                                              if str(n.path).lower().startswith(prefix) and not PAGES_IA.search(n.name)]
    notes.sort(key=lambda n: cle_tri(str(n.path)))
    pdfs = [] if seule is not None else [a for a in vault.attachments.values()
                                         if a.suffix == ".pdf" and str(a.path).lower().startswith(prefix)]
    cles = [n.key for n in notes]
    vues: dict[str, datetime] = {}
    for k in range(0, len(cles), 500):
        for v in WikiVisit.query.filter(WikiVisit.user_id == user.id, WikiVisit.key.in_(cles[k:k + 500])):
            vues[v.key] = v.visited_at
    pages = []
    for n in notes:
        mots = len(re.findall(r"\w+", n.clean_body))
        pages.append({"titre": n.title, "cle": n.key, "url": n.url, "mots": mots,
                      "minutes": max(1, round(mots / MOTS_PAR_MINUTE)), "lue": n.key in vues,
                      "vue_le": vues.get(n.key)})
    return {"chemin": path, "pages": pages, "total": len(pages), "lues": sum(p["lue"] for p in pages),
            "minutes_restantes": sum(p["minutes"] for p in pages if not p["lue"]),
            "pdfs": [{"nom": a.name, "url": a.view_url} for a in sorted(pdfs, key=lambda a: cle_tri(str(a.path)))]}


def _statut_set(e: dict) -> str:
    if e.get("su"):
        return "pret"
    st = e.get("statut")
    if st == "passee":
        return "passe"
    if st in ("retard", "impossible"):
        return "retard"
    if st == "ok":
        return "a_commencer" if e.get("commencer_maintenant") else "dans_les_temps"
    return "a_commencer"


def qcm(user, categorie: str, jour_controle: date, at: datetime, resume: bool = True) -> list[dict]:
    """Sets de la catégorie avec leur estimation pour le jour du contrôle (réglage temporaire, jamais
    enregistré : la date d'examen réelle des sets ne change pas)."""
    from .qcm import planning
    from .qcm.models import QuizDeck, QuizProgress
    from .qcm.services import bibliotheque, resume_set
    sets = bibliotheque(user.id).categories().get(categorie, [])
    if not sets:
        return []
    decks = {d.set_id: d for d in QuizDeck.query.filter(QuizDeck.user_id == user.id,
                                                       QuizDeck.set_id.in_([s.id for s in sets]))}
    out = []
    for qset in sets:
        progress = QuizProgress.map_for(user.id, qset.id)
        reel = decks.get(qset.id)
        temp = QuizDeck(user_id=user.id, set_id=qset.id, date_objectif=jour_controle,
                        nouvelles_jour=reel.nouvelles_jour if reel else None,
                        revisions_jour=reel.revisions_jour if reel else None,
                        auto_quota=bool(reel.auto_quota) if reel else False,
                        temps_ms=(reel.temps_ms or 0) if reel else 0,
                        reponses_chronometrees=(reel.reponses_chronometrees or 0) if reel else 0)
        e = planning.estimer(qset, progress, temp, at)
        x = {"set": qset, "e": e, "statut": _statut_set(e), "objectif": reel.date_objectif if reel else None}
        if resume:
            x["resume"] = resume_set(qset, progress, at, reel)
        out.append(x)
    return out


def temps_passe(user, categorie: str, sets: list) -> dict:
    """Minutes de QCM passées sur la matière (catégorie, ou langue de ses sets), en tout et sur 7 jours.
    La lecture du wiki n'est pas chronométrée."""
    from .qcm.models import TempsRevision
    from .qcm.services import matiere_de
    matieres = {categorie} | {matiere_de(x["set"] if isinstance(x, dict) else x) for x in sets}
    total = semaine = 0
    depuis = now().date() - timedelta(days=6)
    for t in TempsRevision.query.filter(TempsRevision.user_id == user.id, TempsRevision.matiere.in_(matieres)):
        total += t.temps_ms or 0
        if t.jour >= depuis:
            semaine += t.temps_ms or 0
    return {"minutes": round(total / 60000), "semaine": round(semaine / 60000)}


def statut_global(sets: list[dict], lect: dict | None, passe: bool = False) -> str:
    if passe:
        return "passe"
    statuts = [x["statut"] for x in sets]
    reste = bool(lect and lect["total"] and lect["lues"] < lect["total"])
    if not statuts:
        if lect and lect["total"]:
            return "a_commencer" if reste else "pret"
        return "a_relier"
    if "retard" in statuts:
        return "retard"
    if all(s == "pret" for s in statuts):
        return "a_commencer" if reste else "pret"
    if "a_commencer" in statuts:
        return "a_commencer"
    return "dans_les_temps"


def preparer(user, ev: AgendaEvent, at: datetime | None = None) -> dict:
    """Tout ce qu'il faut pour préparer un contrôle (sans rien écrire en base)."""
    at = at or now()
    link = _lien(user, ev)
    wiki_path = link.wiki_path if link and link.wiki_path else ""
    categorie = link.qcm_category if link and link.qcm_category else ""
    passe = ev.start <= at
    lect = lecture(user, wiki_path)
    sets = qcm(user, categorie, ev.start.date(), at) if categorie else []
    taches = AgendaTask.query.filter_by(event_id=ev.id).order_by(AgendaTask.due_date, AgendaTask.start_time,
                                                                 AgendaTask.id).all()
    return {
        "ev": ev, "at": at, "passe": passe, "rebours": compte_a_rebours(ev, at),
        "jours": (ev.start.date() - at.date()).days,
        "wiki_path": wiki_path, "categorie": categorie, "lecture": lect, "sets": sets,
        "maitrise": _maitrise_moyenne([(x["set"], x["resume"]["pct_maitrise"]) for x in sets]) if sets else None,
        "temps": temps_passe(user, categorie, sets) if categorie else None,
        "taches": taches, "statut": statut_global(sets, lect, passe),
        "objectif_fixe": bool(sets) and all(x["objectif"] == ev.start.date() for x in sets),
        "autres_controles": _autres_controles(user, ev),
        "note": AgendaGrade.query.filter_by(user_id=user.id, event_id=ev.id).first(),
    }


def _autres_controles(user, ev: AgendaEvent) -> list[AgendaEvent]:
    """Autres contrôles à venir de la même matière (un set n'a qu'une date d'examen : le dire)."""
    key = A.subject_key(ev.title)
    return [e for e in AgendaEvent.query.filter(AgendaEvent.user_id == user.id, AgendaEvent.status == "active",
                                                AgendaEvent.cancelled.is_(False), AgendaEvent.id != ev.id,
                                                AgendaEvent.start >= datetime.combine(now().date(), time(0)))
            .order_by(AgendaEvent.start)
            if A.is_exam(e) and A.subject_key(e.title) == key]


def _maitrise_moyenne(pairs) -> float | None:
    """Maîtrise moyenne (%) de plusieurs sets, pondérée par leur nombre de questions."""
    total = sum(len(s) for s, _ in pairs)
    if not total:
        return None
    return sum(len(s) * m for s, m in pairs) / total


def indicateur(user, ev: AgendaEvent, at: datetime | None = None, memo: dict | None = None) -> str:
    """Statut d'un contrôle en un mot, sans adresse web (tableau de bord, résumé quotidien)."""
    at = at or now()
    if ev.start <= at:
        return "passe"
    cle = (user.id, A.subject_key(ev.title), ev.start.date())
    if memo is not None and cle in memo:
        return memo[cle]
    link = _lien(user, ev)
    lect = lecture(user, link.wiki_path) if link and link.wiki_path else None
    sets = qcm(user, link.qcm_category, ev.start.date(), at, resume=False) if link and link.qcm_category else []
    st = statut_global(sets, lect)
    if memo is not None:
        memo[cle] = st
    return st


def indicateurs(user, evs, at: datetime | None = None) -> dict[int, str]:
    """{id de séance : statut} ; une erreur d'un module (wiki, QCM) ne fait jamais tomber la page."""
    memo: dict = {}
    out = {}
    for ev in evs:
        try:
            out[ev.id] = indicateur(user, ev, at, memo)
        except Exception:  # noqa: BLE001
            from flask import current_app
            current_app.logger.exception("Statut du contrôle %s", ev.id)
    return out


# ---------------------------------------------------------------------------
# Planifier les révisions dans les créneaux libres
# ---------------------------------------------------------------------------

def _arrondi5(m: float) -> int:
    return int(math.ceil(m / 5) * 5)


def charge_par_jour(user, ev: AgendaEvent, at: datetime) -> dict:
    """Minutes de révision à prévoir chaque jour, d'aujourd'hui à la veille du contrôle.

    QCM : pour chaque set de la catégorie, les minutes par jour de l'estimation (à partir du jour conseillé
    pour commencer) ; en retard, les minutes conseillées pour rattraper, dès aujourd'hui. Cours : le temps de
    lecture des pages pas encore ouvertes, réparti jusqu'à l'avant-veille."""
    link = _lien(user, ev)
    jour0, examen = at.date(), ev.start.date()
    jours = [jour0 + timedelta(days=i) for i in range(max(0, (examen - jour0).days))]
    qcm_j = {d: 0.0 for d in jours}
    lecture_j = {d: 0.0 for d in jours}
    sets = qcm(user, link.qcm_category, examen, at, resume=False) if link and link.qcm_category else []
    for x in sets:
        e = x["e"]
        if e.get("su") or not jours:
            continue
        st = e.get("statut")
        if st == "ok":
            debut, m = max(jour0, e.get("debut") or jour0), e.get("minutes_jour") or 0
        elif st == "retard":
            debut, m = jour0, e.get("minutes_conseillees") or 0
        elif st == "impossible":
            # Pas le temps de tout savoir : découvrir les questions restantes avant la veille ; chaque nouvelle
            # revient en moyenne une fois en révision.
            debut, m = jour0, (e.get("nouvelles_conseillees") or 0) * 2 * (e.get("secondes") or 20) / 60
        else:
            continue
        for d in jours:
            if d >= debut:
                qcm_j[d] += m
    lect = lecture(user, link.wiki_path) if link and link.wiki_path else None
    if lect and lect["minutes_restantes"] and jours:
        lecture_jours = jours[:-1] or jours  # la veille reste pour les QCM
        part = lect["minutes_restantes"] / len(lecture_jours)
        for d in lecture_jours:
            lecture_j[d] += part
    return {"jours": [{"date": d, "qcm": round(qcm_j[d]), "lecture": round(lecture_j[d]),
                       "minutes": round(qcm_j[d] + lecture_j[d])} for d in jours],
            "categorie": link.qcm_category if link else "", "lecture": lect, "sets": sets}


def _occupe(user, d: date, s, ignorer: set[int]) -> tuple[list[tuple[datetime, datetime]], bool]:
    """Créneaux occupés d'un jour : séances, tâches à heure fixe (sauf `ignorer`), repas. Le booléen dit si la
    journée entière est prise (bloc de plusieurs jours : formation, stage…)."""
    a = datetime.combine(d, time(0))
    occupe = [(datetime.combine(d, t0), datetime.combine(d, t1)) for t0, t1 in REPAS]
    for e in A.events_between(user, a, a + timedelta(days=1), include_removed=False):
        if e.cancelled:
            continue
        if e.multi_day:
            return [], True
        occupe.append((e.start, e.end + MARGE_APRES_COURS))
    for o in A.task_occurrences(user, d, d, include_done=False):
        t = o.task
        if t.start_time is None or t.id in ignorer:
            continue
        st = datetime.combine(d, t.start_time)
        en = datetime.combine(d, t.end_time) if t.end_time and t.end_time > t.start_time else st + timedelta(minutes=30)
        occupe.append((st, en))
    return occupe, False


def creneaux_libres(user, d: date, s, at: datetime, ignorer: set[int] = frozenset()) -> list[tuple[datetime, datetime]]:
    """Créneaux libres d'un jour, entre le début de journée de l'agenda et 22 h (au moins BLOC_MIN minutes)."""
    h0 = s.day_start_hour if s.day_start_hour is not None else 8
    debut = datetime.combine(d, time(min(max(h0, 0), 23)))
    fin = datetime.combine(d, time(FIN_SOIREE))
    if d == at.date():
        prochain = at.replace(second=0, microsecond=0) + timedelta(minutes=15 - at.minute % 15)
        debut = max(debut, prochain)
    occupe, journee = _occupe(user, d, s, set(ignorer))
    if journee or debut >= fin:
        return []
    libres, curseur = [], debut
    for a, b in sorted(occupe):
        if b <= curseur:
            continue
        if a > curseur:
            libres.append((curseur, min(a, fin)))
        curseur = max(curseur, b)
        if curseur >= fin:
            break
    if curseur < fin:
        libres.append((curseur, fin))
    return [(a, b) for a, b in libres if (b - a) >= timedelta(minutes=BLOC_MIN)]


def _placer(libres, minutes: int):
    """Premier vrai créneau (au moins CRENEAU_CONFORT, pas un quart d'heure coincé avant un cours) qui contient
    tout le bloc ; sinon le premier qui le contient ; sinon le plus long, bloc raccourci. None : rien."""
    duree = timedelta(minutes=minutes)
    for mini in (max(duree, CRENEAU_CONFORT), duree):
        for a, b in libres:
            if b - a >= mini:
                return a, a + duree
    if not libres:
        return None
    a, b = max(libres, key=lambda x: x[1] - x[0])
    m = int((b - a).total_seconds() // 60) // 5 * 5
    return (a, a + timedelta(minutes=m)) if m >= BLOC_MIN else None


def _blocs_auto(user, ev: AgendaEvent, at: datetime, futurs: bool = True) -> list[AgendaTask]:
    """Blocs de révision générés pour ce contrôle, pas encore faits (à venir seulement si `futurs`)."""
    q = AgendaTask.query.filter(AgendaTask.user_id == user.id, AgendaTask.event_id == ev.id,
                                AgendaTask.revision_auto.is_(True), AgendaTask.done_at.is_(None))
    out = []
    for t in q:
        if futurs and t.due_date is not None and (t.due_date < at.date() or (
                t.due_date == at.date() and t.start_time and datetime.combine(t.due_date, t.start_time) < at)):
            continue  # bloc passé non coché : gardé tel quel, en retard jusqu'au contrôle, puis « manqué » (agenda.taches.bloc_manque)
        out.append(t)
    return out


def proposer(user, ev: AgendaEvent, at: datetime | None = None) -> dict:
    """Aperçu : un bloc par jour, de la durée de la charge du jour, dans le premier créneau libre.
    Les blocs déjà générés pour ce contrôle (à venir) sont remplacés : ils ne comptent pas comme occupés."""
    at = at or now()
    out = {"blocs": [], "sans_creneau": [], "total": 0, "charge": None}
    if ev.start <= at or not ev.is_active:
        return out
    charge = charge_par_jour(user, ev, at)
    out["charge"] = charge
    s = A.settings_of(user)
    ignorer = {t.id for t in _blocs_auto(user, ev, at)}
    for j in charge["jours"]:
        if j["minutes"] <= 0:
            continue
        minutes = min(BLOC_MAX, max(BLOC_MIN, _arrondi5(j["minutes"])))
        place = _placer(creneaux_libres(user, j["date"], s, at, ignorer), minutes)
        if place is None:
            out["sans_creneau"].append(j["date"])
            continue
        a, b = place
        duree = int((b - a).total_seconds() // 60)
        out["blocs"].append({"date": j["date"], "debut": a.time(), "fin": b.time(), "minutes": duree,
                             "charge": j["minutes"], "qcm": j["qcm"], "lecture": j["lecture"],
                             "titre": f"Réviser {ev.title} · {A.duration_label(duree)}"[:300]})
        out["total"] += duree
    return out


def _notes_bloc(ev: AgendaEvent, bloc: dict, charge: dict) -> str:
    bits = [f"Bloc placé par « Planifier mes révisions » pour le contrôle du {ev.start:%d/%m/%Y}."]
    if bloc["qcm"]:
        bits.append(f"QCM ({charge['categorie']}) : environ {bloc['qcm']} min (révision de la catégorie).")
    if bloc["lecture"]:
        bits.append(f"Cours : environ {bloc['lecture']} min de lecture des pages pas encore ouvertes.")
    return "\n".join(bits)


def appliquer(user, ev: AgendaEvent, at: datetime | None = None) -> dict:
    """Crée les blocs proposés comme tâches liées au contrôle (marquées « générées ») et remplace les blocs
    générés à venir. Pas de rappel : des dizaines de blocs ne doivent pas multiplier les e-mails. Sans valider."""
    at = at or now()
    prop = proposer(user, ev, at)
    anciens = _blocs_auto(user, ev, at)
    for t in anciens:
        db.session.delete(t)
    for b in prop["blocs"]:
        db.session.add(AgendaTask(user_id=user.id, title=b["titre"], due_date=b["date"], start_time=b["debut"],
                                  end_time=b["fin"], category=CATEGORIE_TACHE, event_id=ev.id, revision_auto=True,
                                  notes=_notes_bloc(ev, b, prop["charge"]), created_at=at, updated_at=at))
    db.session.flush()
    return {"crees": len(prop["blocs"]), "retires": len(anciens), "sans_creneau": prop["sans_creneau"],
            "total": prop["total"]}


def _photo_bloc(t: AgendaTask) -> dict:
    return {"title": t.title, "due_date": t.due_date.isoformat() if t.due_date else None,
            "start_time": t.start_time.isoformat(timespec="minutes") if t.start_time else None,
            "end_time": t.end_time.isoformat(timespec="minutes") if t.end_time else None,
            "category": t.category or "", "notes": t.notes or "", "color": t.color or "",
            "priority": t.priority or ""}


def retirer(user, ev: AgendaEvent, at: datetime | None = None, journal: dict | None = None) -> int:
    """Supprime les blocs générés à venir d'un contrôle. Sans valider. `journal` ({id de séance : [blocs]}) reçoit
    la photo des blocs supprimés : l'annulation de l'import qui a retiré le contrôle les rend (`rendre_blocs`)."""
    at = at or now()
    blocs = _blocs_auto(user, ev, at)
    if journal is not None and blocs:
        journal.setdefault(ev.id, []).extend(_photo_bloc(t) for t in blocs)
    for t in blocs:
        db.session.delete(t)
    return len(blocs)


def rendre_blocs(user, ev: AgendaEvent, photos: list[dict], at: datetime | None = None) -> int:
    """Annulation d'un import : recrée les blocs que cet import avait supprimés (voir `retirer`), sauf ceux dont
    le moment est passé entre-temps ou si le contrôle n'est plus à venir. Sans valider."""
    at = at or now()
    if not photos or not ev.is_active or ev.start <= at:
        return 0
    n = 0
    for p in photos:
        try:
            jour = date.fromisoformat(p["due_date"]) if p.get("due_date") else None
            debut = time.fromisoformat(p["start_time"]) if p.get("start_time") else None
            fin = time.fromisoformat(p["end_time"]) if p.get("end_time") else None
        except (TypeError, ValueError):
            continue
        if jour is not None and (jour < at.date() or (jour == at.date() and debut and datetime.combine(jour, debut) < at)):
            continue
        db.session.add(AgendaTask(user_id=user.id, title=(p.get("title") or f"Réviser {ev.title}")[:300],
                                  due_date=jour, start_time=debut, end_time=fin,
                                  category=p.get("category") or CATEGORIE_TACHE, notes=p.get("notes") or "",
                                  color=p.get("color") or "", priority=p.get("priority") or "none",
                                  event_id=ev.id, revision_auto=True, created_at=at, updated_at=at))
        n += 1
    db.session.flush()
    return n


def _en_conflit(user, t: AgendaTask) -> bool:
    """Le bloc tombe-t-il sur une séance (ou une journée prise) ?"""
    if t.due_date is None or t.start_time is None:
        return False
    a = datetime.combine(t.due_date, t.start_time)
    b = datetime.combine(t.due_date, t.end_time) if t.end_time and t.end_time > t.start_time else a + timedelta(minutes=30)
    jour = datetime.combine(t.due_date, time(0))
    for e in A.events_between(user, jour, jour + timedelta(days=1), include_removed=False):
        if e.cancelled:
            continue
        if e.multi_day or (e.start < b and e.end > a):
            return True
    return False


def replanifier_apres_import(user, at: datetime | None = None, journal: dict | None = None) -> list[str]:
    """Après un import (ou son annulation) : les blocs générés qui tombent sur un cours, ou après un contrôle
    déplacé, sont replacés ; ceux d'un contrôle retiré sont supprimés (photographiés dans `journal`, voir
    `retirer`). Retourne les phrases du compte rendu. Sans valider la base."""
    at = at or now()
    blocs = AgendaTask.query.filter(AgendaTask.user_id == user.id, AgendaTask.revision_auto.is_(True),
                                    AgendaTask.done_at.is_(None), AgendaTask.event_id.isnot(None),
                                    AgendaTask.due_date >= at.date()).all()
    par_controle: dict[int, list[AgendaTask]] = {}
    for t in blocs:
        par_controle.setdefault(t.event_id, []).append(t)
    notes = []
    for eid, ts in par_controle.items():
        ev = db.session.get(AgendaEvent, eid)
        if ev is None:
            continue
        nom = ev.title
        if not ev.is_active or ev.start <= at:
            n = retirer(user, ev, at, journal)
            if n:
                notes.append(f"{nom} : contrôle retiré du planning, {n} bloc(s) de révision supprimé(s).")
            continue
        if any(t.due_date >= ev.start.date() or _en_conflit(user, t) for t in ts):
            r = appliquer(user, ev, at)
            notes.append(f"{nom} : blocs de révision replacés ({r['crees']} bloc(s) dans les créneaux libres).")
    return notes


# ---------------------------------------------------------------------------
# Notes et moyennes
# ---------------------------------------------------------------------------

def maitrise_matiere(user, ev: AgendaEvent, at: datetime) -> float | None:
    """Maîtrise moyenne (%) des sets de la catégorie de QCM reliée à la matière ; None sans lien."""
    link = _lien(user, ev)
    if link is None or not link.qcm_category:
        return None
    from .qcm.models import QuizProgress
    from .qcm.services import bibliotheque, resume_set
    sets = bibliotheque(user.id).categories().get(link.qcm_category, [])
    return _maitrise_moyenne([(s, resume_set(s, QuizProgress.map_for(user.id, s.id), at)["pct_maitrise"])
                              for s in sets])


def note_de(user, ev: AgendaEvent) -> AgendaGrade | None:
    return AgendaGrade.query.filter_by(user_id=user.id, event_id=ev.id).first()


def _nouvelle_note(user_id: int, ev: AgendaEvent, at: datetime) -> AgendaGrade:
    g = AgendaGrade(user_id=user_id, event_id=ev.id, subject=ev.title[:300], day=ev.start.date(),
                    label=(A.kind_label(ev.kind) + (f" {ev.session_no}" if ev.session_no else ""))[:200] if ev.kind else "",
                    scale=20, coefficient=1, created_at=at, updated_at=at)
    db.session.add(g)
    return g


def figer_maitrise(at: datetime | None = None) -> int:
    """Fil d'arrière-plan : fige la maîtrise des QCM de la matière au début de chaque contrôle (rattrapé
    jusqu'à JOURS_FIGER jours après si l'application était fermée). Retourne le nombre de contrôles figés."""
    at = at or now()
    evs = (AgendaEvent.query.filter(AgendaEvent.status == "active", AgendaEvent.cancelled.is_(False),
                                    AgendaEvent.start <= at, AgendaEvent.start >= at - timedelta(days=JOURS_FIGER))
           .all())
    evs = [e for e in evs if A.is_exam(e)]
    if not evs:
        return 0
    faites = {g.event_id: g for g in AgendaGrade.query.filter(AgendaGrade.event_id.in_([e.id for e in evs]))}
    n = 0
    for ev in evs:
        g = faites.get(ev.id)
        if g is not None and g.mastery_at is not None:
            continue
        user = db.session.get(User, ev.user_id)
        if user is None:
            continue
        try:
            m = maitrise_matiere(user, ev, at)
        except Exception:  # noqa: BLE001 - module QCM indisponible : rien à comparer
            m = None
        if g is None:
            g = _nouvelle_note(ev.user_id, ev, at)
        g.mastery, g.mastery_at = m, at
        n += 1
    db.session.commit()
    return n


def lire_nombre(v) -> float | None:
    """« 14,5 » ou « 14.5 » → 14.5 ; vide ou illisible → None."""
    v = str(v or "").strip().replace(",", ".")
    if not v:
        return None
    try:
        x = float(v)
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def verifier_note(value, scale, coefficient) -> str | None:
    """Message d'erreur (tutoiement) ou None si la note est valable."""
    if value is None:
        return "Indique ta note (un nombre, par exemple 14,5)."
    if scale is None or scale <= 0:
        return "Le barème doit être un nombre positif (par exemple 20)."
    if value < 0 or value > scale:
        return f"La note doit être comprise entre 0 et {scale:g}."
    if coefficient is None or coefficient <= 0:
        return "Le coefficient doit être un nombre positif."
    return None


def noter(user, ev: AgendaEvent | None, value: float, scale: float = 20, coefficient: float = 1,
          subject: str = "", day: date | None = None, label: str | None = None,
          grade: AgendaGrade | None = None, at: datetime | None = None) -> AgendaGrade:
    """Enregistre la note d'un contrôle (ou une note saisie à la main, sans séance). Sans valider."""
    at = at or now()
    g = grade or (note_de(user, ev) if ev is not None else None)
    if g is None:
        if ev is not None:
            g = _nouvelle_note(user.id, ev, at)
        else:
            g = AgendaGrade(user_id=user.id, subject=(subject or "Sans matière")[:300], day=day or at.date(),
                            created_at=at, label="")
            db.session.add(g)
    if ev is None and subject:
        g.subject = subject.strip()[:300]
    if ev is None and day:
        g.day = day
    if label is not None:
        g.label = label.strip()[:200]
    g.value, g.scale, g.coefficient, g.skipped, g.updated_at = value, scale, coefficient, False, at
    return g


def a_noter(user, at: datetime | None = None, jours: int = JOURS_A_NOTER) -> list[AgendaEvent]:
    """Contrôles passés (moins de `jours` jours) dont la note n'est pas saisie (ni marquée « pas de note »)."""
    at = at or now()
    evs = [e for e in AgendaEvent.query.filter(AgendaEvent.user_id == user.id, AgendaEvent.status == "active",
                                               AgendaEvent.cancelled.is_(False), AgendaEvent.end <= at,
                                               AgendaEvent.start >= at - timedelta(days=jours))
           .order_by(AgendaEvent.start.desc()) if A.is_exam(e)]
    if not evs:
        return []
    faites = {g.event_id for g in AgendaGrade.query.filter(AgendaGrade.user_id == user.id,
                                                          AgendaGrade.event_id.in_([e.id for e in evs]))
              if g.value is not None or g.skipped}
    return [e for e in evs if e.id not in faites]


def _moyenne(notes: list[AgendaGrade]) -> float | None:
    coefs = sum(g.coefficient or 1 for g in notes)
    if not coefs:
        return None
    return sum(g.sur_20 * (g.coefficient or 1) for g in notes) / coefs


def moyennes(user) -> dict:
    """Notes saisies, moyennes (sur 20, pondérées par les coefficients) par matière et générale, évolution de
    la moyenne générale, et maîtrise des QCM le jour du contrôle à côté de la note."""
    notes = [g for g in AgendaGrade.query.filter(AgendaGrade.user_id == user.id, AgendaGrade.value.isnot(None))
             .order_by(AgendaGrade.day, AgendaGrade.id) if g.sur_20 is not None]
    par_matiere: dict[str, dict] = {}
    for g in notes:
        cle = A.subject_key(g.subject) or g.subject
        m = par_matiere.setdefault(cle, {"matiere": g.subject, "notes": []})
        m["notes"].append(g)
    matieres = []
    for m in par_matiere.values():
        m["moyenne"] = _moyenne(m["notes"])
        m["coefficients"] = sum(g.coefficient or 1 for g in m["notes"])
        matieres.append(m)
    matieres.sort(key=lambda m: cle_tri(m["matiere"]))
    evolution = []
    for i, g in enumerate(notes):
        evolution.append({"jour": g.day, "note": g.sur_20, "moyenne": _moyenne(notes[:i + 1])})
    comparaison = [{"note": g, "pct_note": g.sur_20 * 5, "maitrise": g.mastery} for g in notes if g.mastery is not None]
    return {"notes": notes, "matieres": matieres, "generale": _moyenne(notes) if notes else None,
            "evolution": evolution, "comparaison": comparaison}


def graphiques(m: dict) -> dict:
    """Graphiques SVG de la page des notes (hangar/charts.py)."""
    from . import charts
    out = {}
    if m["evolution"]:
        labels = [f"{x['jour']:%d/%m}" for x in m["evolution"]]
        out["evolution"] = charts.line_chart(labels, [
            {"name": "Note sur 20", "color": "#8b5cf6", "values": [round(x["note"], 2) for x in m["evolution"]]},
            {"name": "Moyenne générale", "color": "#3f76ff", "values": [round(x["moyenne"], 2) for x in m["evolution"]],
             "fill": True},
        ], height=220)
    if m["matieres"]:
        out["matieres"] = charts.bar_chart([(x["matiere"], round(x["moyenne"], 1), A.SUBJECT_COLORS[i % len(A.SUBJECT_COLORS)])
                                            for i, x in enumerate(m["matieres"])], value_suffix=" / 20")
    if m["comparaison"]:
        labels = [f"{x['note'].day:%d/%m}" for x in m["comparaison"]]
        out["comparaison"] = charts.line_chart(labels, [
            {"name": "Maîtrise des QCM (%)", "color": "#14b8a6", "values": [round(x["maitrise"]) for x in m["comparaison"]]},
            {"name": "Note (en %)", "color": "#8b5cf6", "values": [round(x["pct_note"]) for x in m["comparaison"]]},
        ], height=200)
    return out
