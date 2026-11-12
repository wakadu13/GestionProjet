"""Progression des sets renommés ou déplacés.

L'identifiant d'un set de QCM ou d'un paquet de langue est son chemin dans le coffre, sans
l'extension : la progression FSRS, les quotas, la date d'examen, les signalements et les
explications de l'IA sont rangés sous ce chemin. Ce module les fait suivre quand le fichier change
de nom ou de dossier, y compris hors de Hangar (Obsidian, explorateur de fichiers, synchronisation).
Les fichiers du coffre ne sont jamais modifiés.

- `verifier(library)` : à chaque rechargement de la bibliothèque (services.bibliotheque), garde en
  base l'empreinte du contenu de chaque set (identifiants et textes des questions, pas le titre).
  Un set disparu du coffre est rattaché tout seul au seul set sans progression qui a le même
  contenu, ou presque (au moins SEUIL_PROCHE des questions inchangées) ; ou, sans empreinte (sets
  renommés avant cette version), au seul set dont le chemin ne diffère que par la casse ou la forme
  des accents. Un choix ambigu (deux copies identiques) est laissé à l'élève.
- `rattacher(session, ancien, nouveau)` : progression (fusionnée question par question quand les
  deux en ont), réglages, signalements et explications suivent, pour tous les comptes ; l'alias
  ancien → nouveau est gardé. Sert aussi à « Déplacer dans un dossier… » et à « Rattacher à… »
  (avec `user_id` quand le rattachement ne vaut que pour ce compte : alias propre au compte).
- `meme_set(session, set_id, element)` / `mettre_a_part(session, set_id)` : « Déplacer » vers un
  chemin qui garde la progression d'un autre set, supprimé depuis : elle est mise à part (toujours
  listée sur la page des statistiques) au lieu d'être versée dans le set déplacé.
- `resoudre(library, set_id, user_id)` : identifiant actuel d'un set, pour les réponses envoyées
  plus tard sous l'ancien (application hors ligne, page restée ouverte).
- `attendu(library, user_id, set_id)` : set momentanément introuvable (fichier invalide, ou
  progression sans fichier pas encore rattachée ni oubliée) : la synchronisation hors ligne garde
  ses réponses sur l'appareil au lieu de les perdre.
- `orphelins(user_id, library)` : progressions d'un compte restées sans fichier (page des statistiques).
"""
from __future__ import annotations

import hashlib
import json
import threading
import unicodedata
from datetime import timedelta
from pathlib import PurePosixPath

from flask import current_app
from sqlalchemy import func

from ..extensions import db
from . import quiz
from .models import _JAMAIS, AliasSet, EmpreinteSet, Explication, QuizDeck, QuizProgress, Signalement

SEUIL_PROCHE = 2 / 3    # part des questions inchangées pour reconnaître un set renommé ET modifié
SEUIL_SUGGESTION = 0.5  # « Rattacher à… » : set proposé d'office
JOURS_EMPREINTE = 30    # empreinte d'un set disparu, sans progression, gardée ce temps-là

_verrou = threading.Lock()


# ---------------------------------------------------------------------------
# Empreintes
# ---------------------------------------------------------------------------
def _contenu(item) -> str:
    def dico(d):
        return json.dumps(d, ensure_ascii=False, sort_keys=True, default=str) if d else ""
    if isinstance(item, quiz.Question):
        return repr((item.id, item.type, item.enonce, tuple(item.choix), tuple(item.reponses),
                     tuple(item.reponses_texte), item.verso, dico(item.masque)))
    return repr((item.id, item.forme, dico(item.donnees)))  # exercice d'un paquet de langue


def empreinte(element):
    """(empreinte du set, empreintes courtes de ses questions) : identifiant et contenu de chaque
    question ou exercice ; ni le titre ni le chemin, qui changent quand on range ses fichiers."""
    items = element.questions if isinstance(element, quiz.QuestionSet) else element.exercices
    cles = sorted({hashlib.blake2b(_contenu(it).encode(), digest_size=8).hexdigest() for it in items})
    return hashlib.sha256(" ".join(cles).encode()).hexdigest(), cles


def est_langue(element) -> bool:
    return not isinstance(element, quiz.QuestionSet)


def _espace(set_id):
    """Espace d'un set : commun (""), ou espace personnel d'un compte (« ~<compte> »)."""
    return set_id.split("/", 1)[0] if set_id.startswith(quiz.PERSO) else ""


def libelle(chemin):
    """Chemin d'un set (ou d'un dossier) tel qu'on l'affiche : « Mon espace/… » pour l'espace
    personnel du compte, plutôt que son identifiant technique « ~<compte>/… »."""
    if not chemin.startswith(quiz.PERSO):
        return chemin
    reste = chemin.split("/", 1)[1] if "/" in chemin else ""
    return "Mon espace" + ("/" + reste if reste else "")


def _cle_casse(set_id):
    return unicodedata.normalize("NFC", set_id).casefold()


def _similarite(a, b):
    return len(a & b) / max(len(a), len(b)) if a and b else 0.0


def apparier(disparus, neufs):
    """Paires (ancien, nouveau) : chaque set disparu et le seul set neuf qui a son contenu.

    `disparus`, `neufs` : {set_id: (empreinte, cles, langue)}. Même espace (commun ou personnel) et
    même nature (QCM ou langue) ; d'abord le contenu identique, puis au moins SEUIL_PROCHE des
    questions inchangées, sans ex aequo et dans les deux sens. Un choix ambigu n'est pas fait."""
    def groupe(sid, v):
        return _espace(sid), v[2]

    paires, pris_d, pris_n = [], set(), set()
    par_contenu: dict = {}
    for sid, v in neufs.items():
        par_contenu.setdefault((groupe(sid, v), v[0]), []).append(sid)
    par_contenu_d: dict = {}
    for sid, v in disparus.items():
        par_contenu_d.setdefault((groupe(sid, v), v[0]), []).append(sid)
    for cle, ds in par_contenu_d.items():
        ns = par_contenu.get(cle, [])
        if len(ds) == 1 and len(ns) == 1:
            paires.append((ds[0], ns[0]))
        if ns:  # identiques : apparié, ou ambigu (pas de rattachement « au plus proche » non plus)
            pris_d.update(ds)
            pris_n.update(ns)

    restants_d = {sid: v for sid, v in disparus.items() if sid not in pris_d}
    restants_n = {sid: v for sid, v in neufs.items() if sid not in pris_n}

    def meilleur(sid, v, autres):
        scores = sorted(((_similarite(set(v[1]), set(w[1])), autre) for autre, w in autres.items()
                         if groupe(autre, w) == groupe(sid, v)), reverse=True)
        if not scores or scores[0][0] < SEUIL_PROCHE or (len(scores) > 1 and scores[1][0] == scores[0][0]):
            return None
        return scores[0][1]

    if restants_d and restants_n:
        for d, v in sorted(restants_d.items()):
            n = meilleur(d, v, restants_n)
            if n is not None and meilleur(n, restants_n[n], restants_d) == d:
                paires.append((d, n))
    return paires


# ---------------------------------------------------------------------------
# Rattachement
# ---------------------------------------------------------------------------
def _fusionner(cible, autre):
    """Deux suivis de la même question : l'état de la réponse la plus récente, les compteurs cumulés."""
    if (autre.last_review or _JAMAIS) > (cible.last_review or _JAMAIS):
        for champ in ("box", "last_review", "last_correct", "stabilite", "difficulte", "echeance"):
            setattr(cible, champ, getattr(autre, champ))
    for champ in ("seen", "correct", "oublis"):
        setattr(cible, champ, (getattr(cible, champ) or 0) + (getattr(autre, champ) or 0))
    for champ in ("premiere_revue", "decouverte"):
        dates = [d for d in (getattr(cible, champ), getattr(autre, champ)) if d is not None]
        setattr(cible, champ, min(dates) if dates else None)


def rattachement_global(ancien, nouveau, user_id, peut_ranger) -> bool:
    """« Rattacher à… » vaut-il pour tous les comptes ? Oui si les deux sets sont dans le même espace
    et que le compte peut le ranger : sets communs (admins et membres), ou son espace personnel."""
    espace = _espace(ancien)
    if espace != _espace(nouveau):
        return False
    return peut_ranger if not espace else espace == quiz.PERSO + quiz.nom_perso(user_id)


def rattacher(session, ancien, nouveau, user_id=None):
    """Fait suivre au set `nouveau` tout ce qui était rangé sous `ancien`, pour tous les comptes :
    progression (fusionnée question par question si `nouveau` en a déjà), réglages (ceux de
    `nouveau` d'abord), signalements, explications de l'IA, empreinte ; garde l'alias ancien → nouveau.
    Avec `user_id` : seulement la progression et les réglages de ce compte, et un alias propre à ce
    compte (ses réponses hors ligne encore sous l'ancien nom suivent ; celles des autres comptes
    attendent toujours leur propre rattachement).
    Rien n'est effacé, sauf les doublons fusionnés. Ne valide pas la transaction."""
    if not ancien or not nouveau or ancien == nouveau:
        return

    def lignes(modele, set_id):
        q = session.query(modele).filter_by(set_id=set_id)
        return q.filter_by(user_id=user_id) if user_id is not None else q

    cibles = {(p.user_id, p.question_id): p for p in lignes(QuizProgress, nouveau)}
    for p in lignes(QuizProgress, ancien).all():
        c = cibles.get((p.user_id, p.question_id))
        if c is None:
            p.set_id = nouveau
        else:
            _fusionner(c, p)
            session.delete(p)

    decks = {d.user_id: d for d in lignes(QuizDeck, nouveau)}
    for d in lignes(QuizDeck, ancien).all():
        c = decks.get(d.user_id)
        if c is None:
            d.set_id = nouveau
            continue
        for champ in ("nouvelles_jour", "revisions_jour", "date_objectif"):
            if getattr(c, champ) is None:
                setattr(c, champ, getattr(d, champ))
        c.auto_quota = bool(c.auto_quota or d.auto_quota)
        c.temps_ms = (c.temps_ms or 0) + (d.temps_ms or 0)
        c.reponses_chronometrees = (c.reponses_chronometrees or 0) + (d.reponses_chronometrees or 0)
        session.delete(d)
    if user_id is not None:
        _garder_alias(session, ancien, nouveau, user_id)
        session.flush()
        return

    for s in session.query(Signalement).filter_by(set_id=ancien).all():
        s.set_id = nouveau
    deja = {q for (q,) in session.query(Explication.question_id).filter_by(set_id=nouveau)}
    for e in session.query(Explication).filter_by(set_id=ancien).all():
        if e.question_id in deja:
            session.delete(e)
        else:
            e.set_id = nouveau

    e_ancien = session.query(EmpreinteSet).filter_by(set_id=ancien).first()
    if e_ancien is not None:
        if session.query(EmpreinteSet).filter_by(set_id=nouveau).first() is None:
            e_ancien.set_id, e_ancien.disparu_le = nouveau, None
        else:
            session.delete(e_ancien)

    _garder_alias(session, ancien, nouveau, None)
    session.flush()


def _garder_alias(session, ancien, nouveau, user_id):
    """Alias ancien → nouveau, pour tous les comptes (`user_id` None) ou pour un seul. `nouveau` est
    le nom actuel (plus un alias) ; ceux qui menaient à `ancien` y mènent aussi."""
    q = session.query(AliasSet)
    if user_id is not None:
        q = q.filter(AliasSet.user_id == user_id)
    for a in q.filter((AliasSet.ancien == nouveau) | (AliasSet.nouveau == ancien)).all():
        if a.ancien == nouveau:
            session.delete(a)
        else:
            a.nouveau = nouveau
    a = session.query(AliasSet).filter_by(ancien=ancien, user_id=user_id).first()
    if a is None:
        session.add(AliasSet(ancien=ancien, nouveau=nouveau, user_id=user_id))
    else:
        a.nouveau = nouveau


def meme_set(session, set_id, element) -> bool:
    """La progression restée sous `set_id` sans fichier (set supprimé ou renommé hors de Hangar)
    est-elle celle de `element`, qu'on s'apprête à ranger à ce chemin (« Déplacer ») ? Oui si
    l'empreinte gardée de l'ancien set lui ressemble (au moins SEUIL_PROCHE des questions
    inchangées) ; sans empreinte (set disparu avant cette version), si toutes les questions suivies
    existent dans `element`. Les identifiants par défaut (q0, q1…) se ressemblent d'un set à
    l'autre : seule l'empreinte, quand elle existe, tranche vraiment."""
    e = session.query(EmpreinteSet).filter_by(set_id=set_id).first()
    if e is not None:
        return (bool(e.langue) == est_langue(element)
                and _similarite(e.cles, set(empreinte(element)[1])) >= SEUIL_PROCHE)
    items = element.exercices if est_langue(element) else element.questions
    suivies = {q for (q,) in session.query(QuizProgress.question_id).filter_by(set_id=set_id).distinct()}
    return suivies <= {it.id for it in items}


def a_des_restes(session, set_id) -> bool:
    """Reste-t-il quelque chose sous `set_id` (progression, réglages, signalements, explications) ?"""
    return any(session.query(m.id).filter_by(set_id=set_id).first() is not None
               for m in (QuizProgress, QuizDeck, Signalement, Explication))


def mettre_a_part(session, set_id):
    """Range sous un nom libre, « <set_id> (ancien) », tout ce qui reste sous `set_id` : progression,
    réglages, signalements, explications de l'IA, empreinte, pour tous les comptes. Sert quand un
    autre set prend ce chemin : rien n'est versé dans ce set ni effacé, et la progression reste
    listée sur la page des statistiques (« Rattacher à… », « Oublier »). Renvoie le nom choisi.
    Ne valide pas la transaction."""
    pris = (_ids_avec_progression(session)
            | {sid for (sid,) in session.query(EmpreinteSet.set_id)}
            | {sid for (sid,) in session.query(Signalement.set_id).distinct()}
            | {sid for (sid,) in session.query(Explication.set_id).distinct()})
    a_part, n = f"{set_id} (ancien)", 1
    while a_part in pris:
        n += 1
        a_part = f"{set_id} (ancien {n})"
    for modele in (QuizProgress, QuizDeck, Signalement, Explication, EmpreinteSet):
        session.query(modele).filter_by(set_id=set_id).update({"set_id": a_part}, synchronize_session=False)
    # Les anciens noms qui menaient à cette progression y mènent toujours.
    session.query(AliasSet).filter_by(nouveau=set_id).update({"nouveau": a_part}, synchronize_session=False)
    session.flush()
    return a_part


def _ids_avec_progression(session):
    return ({sid for (sid,) in session.query(QuizProgress.set_id).distinct()}
            | {sid for (sid,) in session.query(QuizDeck.set_id).distinct()})


def _ecriture_en_cours(session) -> bool:
    """La requête en cours a-t-elle déjà écrit sans valider ? (on ne valide pas son travail à sa place)"""
    try:
        brute = session.connection().connection.driver_connection
    except Exception:  # noqa: BLE001
        return True
    return bool(getattr(brute, "in_transaction", False))


def verifier(library):
    """Rattache la progression des sets renommés ou déplacés depuis le dernier chargement de la
    bibliothèque, et tient les empreintes à jour. Une fois par bibliothèque chargée ; ne casse
    jamais une page (en cas d'échec, l'état en base est inchangé et la vérification suivante
    reprendra les mêmes sets)."""
    if getattr(library, "_rattachement_fait", False):
        return
    with _verrou:
        if getattr(library, "_rattachement_fait", False):
            return
        s = db.session
        if s.new or s.dirty or s.deleted or _ecriture_en_cours(s):
            return  # la requête a des écritures en cours : on recommencera au prochain accès
        try:
            _reconcilier(s, library)
            s.commit()
        except Exception:  # noqa: BLE001
            s.rollback()
            current_app.logger.warning("QCM : rattachement des sets renommés impossible", exc_info=True)
        library._rattachement_fait = True


def _reconcilier(s, library):
    now = quiz.maintenant()
    presents = {el.id: el for el in (*library.sets.values(), *library.langues.values())}
    calcul = {sid: empreinte(el) for sid, el in presents.items()}
    invalides = getattr(library, "ids_invalides", set())
    lignes = {e.set_id: e for e in s.query(EmpreinteSet)}
    avec_progression = _ids_avec_progression(s)

    # 1. Sets disparus retrouvés à leur contenu, dans un set qui n'a pas encore de progression.
    disparus = {sid: (e.empreinte, e.cles, bool(e.langue)) for sid, e in lignes.items()
                if sid not in presents and sid not in invalides}
    neufs = {sid: (calcul[sid][0], set(calcul[sid][1]), est_langue(presents[sid]))
             for sid in presents if sid not in avec_progression}
    rattaches = set()
    for ancien, nouveau in apparier(disparus, neufs):
        rattacher(s, ancien, nouveau)
        rattaches.add(ancien)

    # 2. Progressions restées sans fichier : nom actuel connu (alias), ou même chemin à la casse
    #    et aux accents près (cas le plus courant sous Windows, et progression d'avant les empreintes).
    alias = {}
    for a in s.query(AliasSet).all():
        if a.ancien in presents:
            s.delete(a)  # un fichier porte de nouveau ce nom : ce n'est plus un ancien nom
        elif a.user_id is None:  # un alias propre à un compte ne décide pas pour les autres
            alias[a.ancien] = a.nouveau
    par_casse: dict = {}
    for sid in presents:
        par_casse.setdefault(_cle_casse(sid), []).append(sid)
    for sid in sorted((avec_progression | set(disparus)) - rattaches - set(presents) - invalides):
        cible = alias.get(sid)
        if cible not in presents:
            memes = par_casse.get(_cle_casse(sid), [])
            cible = memes[0] if len(memes) == 1 else None
        if cible is not None:
            rattacher(s, sid, cible)

    # 3. Empreintes des sets présents.
    lignes = {e.set_id: e for e in s.query(EmpreinteSet)}
    for sid, el in presents.items():
        emp, cles = calcul[sid]
        e = lignes.get(sid)
        if e is None:
            s.add(EmpreinteSet(set_id=sid, empreinte=emp, questions=" ".join(cles), langue=est_langue(el)))
        elif e.empreinte != emp or e.disparu_le is not None or bool(e.langue) != est_langue(el):
            e.empreinte, e.questions, e.langue, e.disparu_le = emp, " ".join(cles), est_langue(el), None

    # 4. Sets disparus : gardés tant qu'une progression les attend, sinon JOURS_EMPREINTE jours.
    avec_progression = _ids_avec_progression(s)
    for sid, e in lignes.items():
        if sid in presents or sid in invalides:
            continue
        if e.disparu_le is None:
            e.disparu_le = now
        elif sid not in avec_progression and e.disparu_le < now - timedelta(days=JOURS_EMPREINTE):
            s.delete(e)


# ---------------------------------------------------------------------------
# Synchronisation et page des statistiques
# ---------------------------------------------------------------------------
def resoudre(library, set_id, user_id=None):
    """Identifiant actuel d'un set : le sien s'il est dans la bibliothèque, sinon celui auquel sa
    progression a été rattachée (set déplacé ou renommé), s'il y en a un. L'alias propre au compte
    `user_id` (« Rattacher à… » qui ne valait que pour lui) passe avant celui de tous les comptes."""
    vus = set()
    while set_id not in library.sets and set_id not in library.langues and set_id not in vus:
        vus.add(set_id)
        a = None
        if user_id is not None:
            a = AliasSet.query.filter_by(ancien=set_id, user_id=user_id).first()
        if a is None:
            a = AliasSet.query.filter_by(ancien=set_id, user_id=None).first()
        if a is None:
            break
        set_id = a.nouveau
    return set_id


def _a_une_progression(user_id, set_id) -> bool:
    if QuizProgress.query.filter_by(user_id=user_id, set_id=set_id).first() is not None:
        return True
    deck = QuizDeck.query.filter_by(user_id=user_id, set_id=set_id).first()
    return deck is not None and deck.configure


def attendu(library, user_id, set_id) -> bool:
    """Set absent de la bibliothèque mais qui devrait revenir : fichier présent mais invalide (en
    cours de modification), ou progression du compte restée sans fichier, ni rattachée ni oubliée
    (page des statistiques). Un set supprimé dans Hangar n'est pas attendu."""
    if set_id in library.sets or set_id in library.langues:
        return False
    return set_id in getattr(library, "ids_invalides", ()) or _a_une_progression(user_id, set_id)


def _suggestion(sid, emp, cible_alias, elements, empreintes):
    if cible_alias in elements:
        return cible_alias
    if emp is not None:
        cles = emp.cles
        scores = sorted(((_similarite(cles, empreintes[i].cles), i) for i, el in elements.items()
                         if i in empreintes and est_langue(el) == bool(emp.langue)), reverse=True)
        if scores and scores[0][0] >= SEUIL_SUGGESTION:
            return scores[0][1]
    nom = _cle_casse(PurePosixPath(sid).name)
    memes = [i for i in elements if _cle_casse(PurePosixPath(i).name) == nom]
    return memes[0] if len(memes) == 1 else None


def orphelins(user_id, library, suggestions=True):
    """Progressions d'un compte sans fichier dans le coffre (set renommé, déplacé ou supprimé hors
    de Hangar, et pas reconnu tout seul) : [{id, libelle, nom, dossier, commun, vues, derniere,
    suggestion}] (`libelle`, `dossier` : chemins affichés ; `commun` : hors de l'espace personnel)."""
    presents = set(library.sets) | set(library.langues)
    ecartes = presents | set(getattr(library, "ids_invalides", ()))
    infos: dict = {}
    lignes = (db.session.query(QuizProgress.set_id, func.count(QuizProgress.id), func.max(QuizProgress.last_review))
              .filter(QuizProgress.user_id == user_id).group_by(QuizProgress.set_id))
    for sid, vues, derniere in lignes:
        if sid not in ecartes:
            infos[sid] = {"vues": vues, "derniere": derniere}
    for d in QuizDeck.query.filter_by(user_id=user_id):
        if d.configure and d.set_id not in ecartes:
            infos.setdefault(d.set_id, {"vues": 0, "derniere": None})
    if not infos:
        return []
    out = []
    if suggestions:
        elements = {**library.sets, **library.langues}
        empreintes = {e.set_id: e for e in EmpreinteSet.query}
        alias = {}
        for a in (AliasSet.query.filter(AliasSet.ancien.in_(list(infos)))
                  .filter((AliasSet.user_id == user_id) | AliasSet.user_id.is_(None))
                  .order_by(AliasSet.user_id.is_(None).desc())):
            alias[a.ancien] = a.nouveau  # celui du compte en dernier : il l'emporte
    for sid, info in infos.items():
        chemin = PurePosixPath(sid)
        o = {"id": sid, "libelle": libelle(sid), "nom": chemin.name, "dossier": libelle(str(chemin.parent)),
             "commun": not _espace(sid), **info, "suggestion": None}
        if suggestions:
            o["suggestion"] = _suggestion(sid, empreintes.get(sid), alias.get(sid), elements, empreintes)
        out.append(o)
    out.sort(key=lambda o: o["id"].lower())
    return out
