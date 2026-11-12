"""Agenda : séance de l'emploi du temps ↔ collection du wiki ↔ catégorie de QCM, dates d'examen des QCM."""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, time

from flask import current_app

from .. import agenda as A  # le paquet : voir hangar/agenda/__init__.py
from .. import ics
from ..extensions import db
from ..models import AgendaEvent, AgendaSubjectLink
from ..texte import cle_tri
from ..utils import normalize


# ---------------------------------------------------------------------------
# Matières : séance de l'emploi du temps ↔ collection du wiki ↔ catégorie de QCM
# ---------------------------------------------------------------------------

_STOP = {"de", "du", "des", "la", "le", "les", "et", "en", "sur", "aux", "au", "un", "une", "a", "l", "d", "niveau"}
_IGNORED_FOLDERS = {"qcm", "image", "images", "fichiers", "guide", "attachments", "assets"}


def subject_key(title: str) -> str:
    return normalize(title)[:300]


def _ancienne_cle(title: str) -> str:
    """Titre normalisé comme avant que ``normalize`` écrive « œ » en « oe » et lise « ’ » comme
    « ' » (« Manœuvre » donnait « manuvre », « Droit de l’homme » « droit de lhomme »). Garde les
    liens de matière enregistrés avant et la couleur des séances (calculée sur ce titre)."""
    text = unicodedata.normalize("NFKD", str(title or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _subject_link(user, title: str) -> AgendaSubjectLink | None:
    """Lien de matière d'un titre de séance, y compris s'il a été enregistré avec l'ancienne clé."""
    key, old = subject_key(title), _ancienne_cle(title)[:300]
    links = AgendaSubjectLink.query.filter(AgendaSubjectLink.user_id == user.id,
                                           AgendaSubjectLink.subject.in_({key, old})).all()
    return next((l for l in links if l.subject == key), links[0] if links else None)


def _words(text: str) -> list[str]:
    out = []
    for w in normalize(re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")).split():
        if w not in _STOP and not w.isdigit():
            out.append(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return out


def _similarity(title: str, candidate: str) -> int:
    """Score 0–100 de ressemblance entre un intitulé de cours et un nom de dossier / catégorie."""
    a, b = _words(title), _words(candidate)
    if not a or not b:
        return 0
    ca, cb = "".join(a), "".join(b)
    if ca == cb:
        return 100
    score = 0
    inter = set(a) & set(b)
    if inter:
        score = max(score, int(80 * len(inter) / len(set(a) | set(b))) + 20)
    acronym = "".join(w[0] for w in a if len(w) > 2)
    for tok in b:
        if len(tok) >= 2 and len(acronym) >= 2 and (acronym.startswith(tok) or tok.startswith(acronym)):
            score = max(score, 65)
    if len(cb) >= 4 and (cb in ca or ca in cb):
        score = max(score, 55)
    return min(score, 99)


def wiki_collections() -> list[dict]:
    """Dossiers du coffre proposés comme « cours » : [{path, label, url}] (vide si le wiki est indisponible)."""
    from flask import g
    if "_wiki_collections" in g:
        return g.get("_wiki_collections")
    g._wiki_collections = _wiki_collections()
    return g._wiki_collections


def _wiki_collections() -> list[dict]:
    try:
        from flask import url_for as _url
        from ..wiki.vault import get_vault
        vault = get_vault()
    except Exception:  # noqa: BLE001 - wiki absent ou coffre illisible
        return []
    out = []
    for path in sorted(vault.folders(), key=cle_tri):
        parts = [p for p in str(path).split("/") if p]
        if not parts or len(parts) > 3 or parts[0].lower() in _IGNORED_FOLDERS or any(p.startswith(".") for p in parts):
            continue
        out.append({"path": "/".join(parts), "label": parts[-1], "group": "/".join(parts[:-1]),
                    "url": _url("wiki.sommaire", dossier="/".join(parts))})
    return out


def _wiki_target(path: str) -> dict | None:
    """Lien vers une collection : sa page « 00 - Sommaire … » si elle existe, sinon le sommaire du dossier."""
    if not path:
        return None
    try:
        from flask import url_for as _url
        from ..wiki.vault import get_vault
        vault = get_vault()
    except Exception:  # noqa: BLE001
        return None
    prefix = path.strip("/").lower() + "/"
    notes = [n for n in vault.notes.values() if str(n.path).lower().startswith(prefix)
             and str(n.path).count("/") == path.count("/") + 1]
    home = next((n for n in sorted(notes, key=lambda n: str(n.path).lower()) if "sommaire" in n.name.lower()), None)
    label = path.rsplit("/", 1)[-1]
    if home is not None:
        return {"label": label, "url": home.url, "path": path}
    return {"label": label, "url": _url("wiki.sommaire", dossier=path), "path": path}


def _par_requete(cle, calcul):
    """Valeur calculée une fois par requête (rangée sur `request` : g peut survivre à plusieurs
    requêtes) ; recalculée à chaque appel hors requête."""
    from flask import has_request_context, request
    if not has_request_context():
        return calcul()
    memo = getattr(request, "_agenda_memo", None)
    if memo is None:
        memo = request._agenda_memo = {}
    if cle not in memo:
        memo[cle] = calcul()
    return memo[cle]


def qcm_categories(user) -> list[str]:
    """Catégories de sets de QCM d'un compte, dans l'ordre de la bibliothèque (vide si le module QCM
    est indisponible). Les noms seulement : les cartes dues sont dans `qcm_due`."""
    def calcul():
        try:
            from ..qcm.services import bibliotheque
            return list(bibliotheque(user.id).categories())
        except Exception:  # noqa: BLE001
            return []
    return _par_requete(("qcm_categories", user.id), calcul)


def qcm_due(user) -> dict[str, int]:
    """{catégorie : cartes à réviser aujourd'hui}, comme le résumé des QCM du tableau de bord (dont il
    partage le calcul), sans l'estimation des examens. Calculé seulement quand une séance affichée
    est reliée à une catégorie."""
    def calcul():
        try:
            from ..qcm.services import cartes_par_categorie, etat_du_jour_page
            return cartes_par_categorie(etat_du_jour_page(user.id))
        except Exception:  # noqa: BLE001
            return {}
    return _par_requete(("qcm_due", user.id), calcul)


def subject_lists(user) -> dict:
    """Toutes les collections du wiki et catégories de QCM proposées dans les fenêtres de détail :
    une fois pour toute la page (et non recopiées dans chaque séance)."""
    return {"wikiAll": [c["path"] for c in wiki_collections()], "qcmAll": sorted(qcm_categories(user), key=str.lower)}


def _suggestions(user, ev: AgendaEvent, kind: str, cols, cats) -> list[str]:
    """Trois meilleures collections du wiki (kind « wiki ») ou catégories de QCM (« qcm ») pour une
    séance : calculées une fois par matière et par page (le même cours revient chaque semaine)."""
    def calcul():
        teachers = [w for w in _words(ev.teachers or "") if len(w) > 3]

        def score(cand: str) -> int:
            sc = _similarity(ev.title, cand)
            cw = "".join(_words(cand))
            if any(t in cw for t in teachers):  # « TELECOM_SANTORI » pour un cours de M. Santori
                sc = max(sc, 60)
            return sc

        if kind == "wiki":
            scored = sorted(((max(score(c["label"]), score(c["path"])), c) for c in cols), key=lambda x: -x[0])
            return [c["path"] for s, c in scored[:3] if s >= 45]
        scored = sorted(((score(c), c) for c in cats), key=lambda x: -x[0])
        return [c for s, c in scored[:3] if s >= 45]
    return _par_requete(("suggestions", kind, user.id, ev.title, ev.teachers or ""), calcul)


def subject_links(user, ev: AgendaEvent, links: dict | None = None, listes: bool = True) -> dict:
    """Liens et suggestions d'une séance pour la fenêtre de détail.

    `links` : liens de matière du compte déjà chargés ({clé : lien}, voir `preload_payloads`) ;
    `listes` : joindre toutes les collections et catégories (wikiAll, qcmAll) ; une page entière les
    met une seule fois à la racine de ses données (`subject_lists`)."""
    key = subject_key(ev.title)
    if links is not None:
        link = links.get(key) or links.get(_ancienne_cle(ev.title)[:300])
    else:
        link = _subject_link(user, ev.title)
    out = {"subject": ev.title, "wiki": None, "qcm": None, "wikiSuggest": [], "qcmSuggest": []}
    if listes:
        out.update(wikiAll=[], qcmAll=[])
    cols = wiki_collections()
    cats = qcm_categories(user)
    if link and link.wiki_path:
        out["wiki"] = _wiki_target(link.wiki_path)
    if link and link.qcm_category:
        from flask import url_for as _url
        from ..qcm.views import _qcm_slug  # même ancre que la page QCM (id="cat-{{ cat.nom|qcm_slug }}")
        out["qcm"] = {"label": link.qcm_category, "due": qcm_due(user).get(link.qcm_category, 0),
                      "url": _url("qcm.index") + "#cat-" + _qcm_slug(link.qcm_category)}
    if out["wiki"] is None and cols:
        out["wikiSuggest"] = _suggestions(user, ev, "wiki", cols, cats)
        if listes:
            out["wikiAll"] = [c["path"] for c in cols]
    if out["qcm"] is None and cats:
        out["qcmSuggest"] = _suggestions(user, ev, "qcm", cols, cats)
        if listes:
            out["qcmAll"] = sorted(cats, key=cle_tri)
    return out


def set_subject_link(user, title: str, wiki_path: str | None = None, qcm_category: str | None = None) -> AgendaSubjectLink:
    key = subject_key(title)
    link = _subject_link(user, title)
    if link is not None and link.subject != key:
        link.subject = key  # enregistré avec l'ancienne clé : mis à jour en passant
    if link is None:
        link = AgendaSubjectLink(user_id=user.id, subject=key, title=title[:300], created_at=A.now())
        db.session.add(link)
    if wiki_path is not None:
        link.wiki_path = wiki_path.strip("/")[:500]
    if qcm_category is not None:
        link.qcm_category = qcm_category[:300]
    link.updated_at = A.now()
    return link


def _category_decks(user, category: str, create: bool = True) -> list:
    """Réglages de révision (QuizDeck) des sets d'une catégorie de QCM, créés au besoin. Sans valider la base."""
    from ..qcm.models import QuizDeck
    from ..qcm.services import bibliotheque
    sets = bibliotheque(user.id).categories().get(category, [])
    have = {d.set_id: d for d in QuizDeck.query.filter(QuizDeck.user_id == user.id,
                                                        QuizDeck.set_id.in_([st.id for st in sets] or [""]))}
    out = []
    for st in sets:
        deck = have.get(st.id)
        if deck is None and create:
            deck = QuizDeck(user_id=user.id, set_id=st.id)
            db.session.add(deck)
        if deck is not None:
            out.append(deck)
    return out


def set_exam_objective(user, ev: AgendaEvent) -> int:
    """Date d'examen des sets de la catégorie de QCM liée = jour du contrôle. Retourne le nombre de sets."""
    link = _subject_link(user, ev.title)
    if link is None or not link.qcm_category:
        return 0
    decks = _category_decks(user, link.qcm_category)
    for deck in decks:
        deck.date_objectif = ev.start.date()
    db.session.commit()
    return len(decks)


def _exam_snap(snap: dict | None) -> tuple[str, date] | None:
    """(clé de matière, jour) d'un instantané de séance s'il s'agit d'un contrôle actif, sinon None."""
    if not snap or not snap.get("start") or snap.get("cancelled") or snap.get("status", "active") != "active":
        return None
    if not ics.is_exam(snap.get("kind") or "", snap.get("title") or ""):
        return None
    return subject_key(snap.get("title") or ""), datetime.fromisoformat(snap["start"]).date()


def exam_changes(details: list, undo: bool = False) -> dict[str, dict]:
    """Contrôles touchés par un import : {matière: {"old": jours quittés, "moves": {ancien jour: nouveau jour},
    "touched": un contrôle ajouté ou modifié}}. `undo` : pour l'annulation, l'état « après » de l'import est
    celui qu'on quitte et l'état « avant » celui qu'on retrouve."""
    out: dict[str, dict] = {}
    for d in details:
        before, after = d.get("before"), d.get("after")
        if undo:
            before, after = after, (d.get("before") if d.get("type") != "added" else None)
        old, new = _exam_snap(before), _exam_snap(after)
        for ex in (old, new):
            if ex is not None:
                out.setdefault(ex[0], {"old": set(), "moves": {}, "touched": False})
        if old is not None:
            out[old[0]]["old"].add(old[1])
        if new is not None:
            out[new[0]]["touched"] = True
            if old is not None and old[0] == new[0] and old[1] != new[1]:
                out[old[0]]["moves"][old[1]] = new[1]
    return out


def _restore_auto_quota(user, set_ids: list[str]) -> list[str]:
    """Annulation d'un import : remet l'ajustement automatique des sets que cet import avait coupé (quand ils
    ont retrouvé une date d'examen)."""
    if not set_ids:
        return []
    from ..qcm.models import QuizDeck
    n = 0
    for deck in QuizDeck.query.filter(QuizDeck.user_id == user.id, QuizDeck.set_id.in_(set_ids)):
        if deck.date_objectif is not None and not deck.auto_quota:
            deck.auto_quota = True
            n += 1
    return [f"Ajustement automatique (« Ajuster auto. ») réactivé pour {n} set(s) de QCM."] if n else []


def sync_exam_objectives(user, at: datetime, changes: dict[str, dict],
                         quota_off: list[str] | None = None) -> list[str]:
    """Fait suivre le planning à la date d'examen des sets de QCM des matières reliées (sans valider la base).

    Pour chaque matière touchée (voir `exam_changes`) et reliée à une catégorie de QCM, on vise le prochain
    contrôle à venir :
    - une date d'examen qui valait l'ancien jour d'un contrôle déplacé suit le contrôle ;
    - un contrôle retiré : la date passe au prochain contrôle ; s'il n'y en a plus, elle est effacée et
      l'ajustement automatique coupé (les sets concernés sont ajoutés à `quota_off`, pour l'annulation) ;
    - un set sans date (ou avec une date passée) est préparé pour le prochain contrôle ;
    - une date saisie à la main (qui ne vaut aucun ancien jour de contrôle) n'est jamais modifiée.
    Retourne les phrases à afficher dans le compte rendu de l'import."""
    if not changes:
        return []
    links = [lk for lk in AgendaSubjectLink.query.filter_by(user_id=user.id)
             if lk.qcm_category and lk.subject in changes]
    if not links:
        return []
    today = at.date()
    upcoming: dict[str, list[date]] = {}
    for e in AgendaEvent.query.filter(AgendaEvent.user_id == user.id, AgendaEvent.status == "active",
                                      AgendaEvent.cancelled.is_(False),
                                      AgendaEvent.start >= datetime.combine(today, time(0))):
        if A.is_exam(e):
            upcoming.setdefault(subject_key(e.title), []).append(e.start.date())
    notes = []
    for link in links:
        row = changes[link.subject]
        dates = sorted(set(upcoming.get(link.subject, [])))
        nxt = dates[0] if dates else None
        old = row["old"] - set(dates)  # jours qui ne portent plus aucun contrôle de la matière
        touched = row["touched"]
        if not old and not (touched and nxt):
            continue
        try:
            decks = _category_decks(user, link.qcm_category, create=nxt is not None and touched)
        except Exception:  # noqa: BLE001 - module QCM indisponible : l'import passe quand même
            current_app.logger.exception("Dates d'examen des QCM après un import")
            continue
        moved, carried, prepared, cleared = {}, {}, 0, 0
        for deck in decks:
            d = deck.date_objectif
            if d in old:  # la date suivait un contrôle qui a bougé ou disparu
                target = row["moves"].get(d)
                bucket = moved if target in dates else carried  # contrôle déplacé, ou retiré
                target = target if target in dates else nxt
                if target is None:
                    if deck.auto_quota and quota_off is not None:
                        quota_off.append(deck.set_id)
                    deck.date_objectif, deck.auto_quota = None, False
                    cleared += 1
                else:
                    deck.date_objectif = target
                    bucket[(d, target)] = bucket.get((d, target), 0) + 1
            elif touched and nxt is not None and (d is None or d < today):
                deck.date_objectif = nxt
                prepared += 1
        name = link.title or link.subject
        for (d, target), n in sorted(moved.items()):
            notes.append(f"{name} : contrôle déplacé du {d:%d/%m} au {target:%d/%m}, date d’examen recalée pour "
                         f"{n} set(s) de QCM.")
        for (d, target), n in sorted(carried.items()):
            notes.append(f"{name} : contrôle du {d:%d/%m} retiré : date d’examen reportée au prochain contrôle "
                         f"({target:%d/%m}) pour {n} set(s) de QCM.")
        if prepared:
            notes.append(f"{name} : date d’examen fixée au {nxt:%d/%m} (prochain contrôle) pour {prepared} set(s) "
                         "de QCM.")
        if cleared:
            notes.append(f"{name} : plus de contrôle prévu, date d’examen effacée pour {cleared} set(s) de QCM.")
    return notes
