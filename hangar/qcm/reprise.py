"""Reprise de la progression de Revision (l'application d'origine, sur le Raspberry Pi) :
`uv run hangar qcm-import-revision <db.sqlite3> --compte <identifiant>`.

`wiki-import` copie les fichiers du coffre (cours et sets) ; la progression, elle, est dans la
base Django de Revision (DATA_DIR/db.sqlite3, hors du dépôt git), avec les espaces personnels
(DATA_DIR/perso/<compte>/). Cette reprise :

- lit la base de Revision SANS la modifier (ouverture en lecture seule) ;
- copie, pour un « joueur » de Revision (son nom d'utilisateur) vers un compte de Hangar : la
  progression de chaque question (wiki_quizprogress), les réglages des sets (quotas, date d'examen,
  temps mesuré : wiki_quizdeck), l'activité par jour (série, calendrier : wiki_activite), le temps
  par matière (wiki_tempsrevision), l'objectif quotidien (wiki_objectifrevision), les explications
  de l'IA déjà payées (wiki_explication) et les fichiers de l'espace personnel ;
- suit chaque set par son identifiant (son chemin dans le coffre, le même dans les deux
  applications), par les alias de Hangar (set renommé ou déplacé depuis), ou par son contenu :
  avec `--coffre`, le fichier du set dans le coffre de Revision est comparé aux sets de Hangar
  (mêmes empreintes que rattachement.py). Un set introuvable garde son identifiant : sa
  progression apparaît sur la page des statistiques, prête à être rattachée ;
- se relance sans rien doubler : pour une question, la révision la plus récente l'emporte
  (compteurs : le plus grand) ; pour l'activité et le temps, le plus grand de chaque jour ; les
  réglages déjà faits dans Hangar sont gardés ;
- convertit les dates-heures (UTC dans Django) en heure de l'agenda ; les dates simples (jour,
  date d'examen) sont copiées telles quelles.
"""
from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath

from .. import heure
from ..extensions import db
from . import quiz, rattachement
from .models import Activite, Explication, ObjectifRevision, QuizDeck, QuizProgress, TempsRevision

TABLES = {
    "progression": "wiki_quizprogress",
    "reglages": "wiki_quizdeck",
    "activite": "wiki_activite",
    "temps": "wiki_tempsrevision",
    "objectif": "wiki_objectifrevision",
    "explications": "wiki_explication",
}


class RepriseImpossible(Exception):
    """Base illisible, ou qui n'est pas celle de Revision."""


@dataclass
class Bilan:
    progression: dict = field(default_factory=lambda: {"ajoutees": 0, "mises_a_jour": 0, "gardees": 0})
    reglages: dict = field(default_factory=lambda: {"ajoutes": 0, "completes": 0})
    jours_activite: int = 0
    jours_temps: int = 0
    objectif: bool = False
    explications: int = 0
    fichiers_perso: list = field(default_factory=list)
    fichiers_perso_gardes: list = field(default_factory=list)
    sets: dict = field(default_factory=dict)          # ancien identifiant → identifiant dans Hangar
    par_contenu: dict = field(default_factory=dict)   # rattachés grâce au contenu (--coffre)
    introuvables: list = field(default_factory=list)  # sets absents de Hangar (progression gardée)
    tables_absentes: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Lecture de la base de Revision
# ---------------------------------------------------------------------------
def ouvrir(chemin) -> sqlite3.Connection:
    """Base de Revision en lecture seule (rien n'y est jamais écrit)."""
    chemin = Path(chemin)
    if not chemin.is_file():
        raise RepriseImpossible(f"Base introuvable : {chemin}")
    try:
        base = sqlite3.connect(f"{chemin.resolve().as_uri()}?mode=ro", uri=True)
        base.row_factory = sqlite3.Row
        tables = {r[0] for r in base.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    except sqlite3.DatabaseError as e:
        raise RepriseImpossible(f"{chemin} n'est pas une base SQLite lisible ({e}).") from None
    if TABLES["progression"] not in tables:
        base.close()
        raise RepriseImpossible(f"{chemin} n'est pas la base de Revision (table {TABLES['progression']} absente). "
                                "Donne le fichier db.sqlite3 du dossier de données de Revision.")
    return base


def _tables(base):
    return {r[0] for r in base.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def joueurs(base) -> list[str]:
    """Noms des joueurs qui ont une progression dans Revision."""
    return [r[0] for r in base.execute(f"SELECT DISTINCT player FROM {TABLES['progression']} ORDER BY player")]


def _date_heure(valeur):
    """Date-heure de Django (UTC, texte « AAAA-MM-JJ HH:MM:SS[.ffffff] ») → heure naïve de l'agenda (Paris)."""
    if valeur in (None, ""):
        return None
    if isinstance(valeur, datetime):
        quand = valeur
    else:
        quand = datetime.fromisoformat(str(valeur).strip().replace("Z", "+00:00"))
    if quand.tzinfo is None:
        quand = quand.replace(tzinfo=timezone.utc)
    return heure.vers_agenda(quand).replace(microsecond=0)


def _date(valeur):
    """Date simple (jour, date d'examen) : copiée telle quelle."""
    if valeur in (None, ""):
        return None
    return valeur if isinstance(valeur, date) else date.fromisoformat(str(valeur)[:10])


def _booleen(valeur):
    return None if valeur is None else bool(valeur)


def _entier(valeur):
    return int(valeur or 0)


# ---------------------------------------------------------------------------
# Identifiants des sets
# ---------------------------------------------------------------------------
class Correspondance:
    """Identifiant dans Hangar d'un set de Revision : le même chemin, un alias de Hangar, ou le
    set de Hangar qui a le même contenu (avec le coffre de Revision)."""

    def __init__(self, library, user_id, joueur, coffre=None, perso_revision=None):
        self.library, self.user_id = library, user_id
        self.prefixe_revision = quiz.PERSO + _nom_perso_revision(joueur) + "/"
        self.prefixe_hangar = quiz.PERSO + quiz.nom_perso(user_id) + "/"
        self.coffre = Path(coffre) if coffre else None
        self.perso_revision = Path(perso_revision) if perso_revision else None
        self.cache: dict = {}
        self.par_contenu: dict = {}
        self._neufs = None

    def _dans_hangar(self, set_id):
        return set_id in self.library.sets or set_id in self.library.langues

    def __call__(self, set_id: str) -> str:
        if set_id not in self.cache:
            self.cache[set_id] = self._resoudre(set_id)
        return self.cache[set_id]

    def _resoudre(self, ancien):
        sid = ancien
        if sid.startswith(self.prefixe_revision):  # espace personnel : « ~<nom> » → « ~<id du compte> »
            sid = self.prefixe_hangar + sid[len(self.prefixe_revision):]
        if self._dans_hangar(sid):
            return sid
        alias = rattachement.resoudre(self.library, sid, self.user_id)
        if self._dans_hangar(alias):
            return alias
        trouve = self._par_contenu(ancien, sid)
        if trouve:
            self.par_contenu[ancien] = trouve
            return trouve
        return sid

    def _fichier_revision(self, ancien):
        if ancien.startswith(self.prefixe_revision):
            if self.perso_revision is None:
                return None, None
            return self.perso_revision / (ancien[len(self.prefixe_revision):] + ".json"), self.perso_revision
        if self.coffre is None or ancien.startswith(quiz.PERSO):
            return None, None
        return self.coffre / (ancien + ".json"), self.coffre

    def _par_contenu(self, ancien, sid):
        chemin, racine = self._fichier_revision(ancien)
        if chemin is None or not chemin.is_file():
            return None
        try:
            element = quiz.charger(chemin, racine)
        except Exception:  # noqa: BLE001 — fichier de Revision illisible : pas de rattachement par contenu
            return None
        emp, cles = rattachement.empreinte(element)
        if self._neufs is None:
            self._neufs = {}
            for e in (*self.library.sets.values(), *self.library.langues.values()):
                if e.proprietaire and e.proprietaire != quiz.nom_perso(self.user_id):
                    continue
                e_emp, e_cles = rattachement.empreinte(e)
                self._neufs[e.id] = (e_emp, set(e_cles), rattachement.est_langue(e))
        paires = rattachement.apparier({sid: (emp, set(cles), rattachement.est_langue(element))}, self._neufs)
        return paires[0][1] if paires else None


def _nom_perso_revision(joueur):
    """Nom du dossier personnel d'un joueur dans Revision (même règle que Hangar)."""
    return quiz.nom_perso(joueur)


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------
def _plus_recente(a, b):
    """Vrai si la révision `a` est strictement plus récente que `b` (None : jamais révisé)."""
    return a is not None and (b is None or a > b)


def _min(a, b):
    return b if a is None else a if b is None else min(a, b)


def _progression(base, bilan, uid, joueur, ids):
    existantes = {}
    for p in QuizProgress.query.filter_by(user_id=uid):
        existantes[(p.set_id, p.question_id)] = p
    for r in base.execute(f"SELECT * FROM {TABLES['progression']} WHERE player = ?", (joueur,)):
        set_id = ids(r["set_id"])
        source = {
            "box": _entier(r["box"]), "seen": _entier(r["seen"]), "correct": _entier(r["correct"]),
            "last_review": _date_heure(r["last_review"]), "premiere_revue": _date_heure(r["premiere_revue"]),
            "last_correct": _booleen(r["last_correct"]), "stabilite": r["stabilite"], "difficulte": r["difficulte"],
            "echeance": _date_heure(r["echeance"]), "decouverte": _date_heure(r["decouverte"]),
            "oublis": _entier(r["oublis"]),
        }
        p = existantes.get((set_id, r["question_id"]))
        if p is None:
            p = QuizProgress(user_id=uid, set_id=set_id, question_id=r["question_id"], **source)
            db.session.add(p)
            existantes[(set_id, r["question_id"])] = p
            bilan.progression["ajoutees"] += 1
            continue
        # Déjà suivie dans Hangar : l'état de la révision la plus récente l'emporte ; les compteurs
        # gardent le plus grand (jamais additionnés : relancer la reprise ne double rien).
        avant = _cle(p)
        if _plus_recente(source["last_review"], p.last_review):
            for champ in ("box", "last_review", "last_correct", "stabilite", "difficulte", "echeance"):
                setattr(p, champ, source[champ])
        p.seen = max(p.seen or 0, source["seen"])
        p.correct = max(p.correct or 0, source["correct"])
        p.oublis = max(p.oublis or 0, source["oublis"])
        p.premiere_revue = _min(p.premiere_revue, source["premiere_revue"])
        p.decouverte = _min(p.decouverte, source["decouverte"])
        if _cle(p) != avant:
            bilan.progression["mises_a_jour"] += 1
        else:
            bilan.progression["gardees"] += 1


def _cle(p):
    return (p.box, p.seen, p.correct, p.last_review, p.premiere_revue, p.last_correct, p.stabilite, p.difficulte,
            p.echeance, p.decouverte, p.oublis)


def _reglages(base, bilan, uid, joueur, ids):
    decks = {d.set_id: d for d in QuizDeck.query.filter_by(user_id=uid)}
    for r in base.execute(f"SELECT * FROM {TABLES['reglages']} WHERE player = ?", (joueur,)):
        set_id = ids(r["set_id"])
        source = {"nouvelles_jour": r["nouvelles_jour"], "revisions_jour": r["revisions_jour"],
                  "date_objectif": _date(r["date_objectif"]), "auto_quota": bool(r["auto_quota"]),
                  "temps_ms": _entier(r["temps_ms"]), "reponses_chronometrees": _entier(r["reponses_chronometrees"])}
        d = decks.get(set_id)
        if d is None:
            d = QuizDeck(user_id=uid, set_id=set_id, **source)
            db.session.add(d)
            decks[set_id] = d
            bilan.reglages["ajoutes"] += 1
            continue
        avant = (d.nouvelles_jour, d.revisions_jour, d.date_objectif, d.auto_quota, d.temps_ms, d.reponses_chronometrees)
        # Réglages déjà faits dans Hangar gardés ; ceux qui manquent viennent de Revision.
        for champ in ("nouvelles_jour", "revisions_jour", "date_objectif"):
            if getattr(d, champ) is None:
                setattr(d, champ, source[champ])
        d.auto_quota = bool(d.auto_quota or source["auto_quota"])
        if source["reponses_chronometrees"] > (d.reponses_chronometrees or 0):
            d.temps_ms, d.reponses_chronometrees = source["temps_ms"], source["reponses_chronometrees"]
        if (d.nouvelles_jour, d.revisions_jour, d.date_objectif, d.auto_quota, d.temps_ms,
                d.reponses_chronometrees) != avant:
            bilan.reglages["completes"] += 1


def _activite(base, bilan, uid, joueur):
    jours = {a.jour: a for a in Activite.query.filter_by(user_id=uid)}
    for r in base.execute(f"SELECT * FROM {TABLES['activite']} WHERE player = ?", (joueur,)):
        jour = _date(r["jour"])
        a = jours.get(jour)
        if a is None:
            a = Activite(user_id=uid, jour=jour)
            db.session.add(a)
            jours[jour] = a
        # Le plus grand de chaque jour (un jour où les deux applications ont servi n'est pas additionné).
        a.reponses = max(a.reponses or 0, _entier(r["reponses"]))
        a.justes = max(a.justes or 0, _entier(r["justes"]))
        a.nouveaux = max(a.nouveaux or 0, _entier(r["nouveaux"]))
        bilan.jours_activite += 1


def _temps(base, bilan, uid, joueur):
    lignes = {(t.jour, t.matiere): t for t in TempsRevision.query.filter_by(user_id=uid)}
    for r in base.execute(f"SELECT * FROM {TABLES['temps']} WHERE player = ?", (joueur,)):
        cle = (_date(r["jour"]), str(r["matiere"])[:200])
        t = lignes.get(cle)
        if t is None:
            t = TempsRevision(user_id=uid, jour=cle[0], matiere=cle[1])
            db.session.add(t)
            lignes[cle] = t
        if _entier(r["temps_ms"]) > (t.temps_ms or 0):
            t.temps_ms, t.reponses = _entier(r["temps_ms"]), _entier(r["reponses"])
        bilan.jours_temps += 1


def _objectif(base, bilan, uid, joueur):
    r = base.execute(f"SELECT reponses_jour FROM {TABLES['objectif']} WHERE player = ?", (joueur,)).fetchone()
    if r is None or r["reponses_jour"] is None:
        return
    o = ObjectifRevision.query.filter_by(user_id=uid).first()
    if o is None:
        db.session.add(ObjectifRevision(user_id=uid, reponses_jour=r["reponses_jour"]))
        bilan.objectif = True
    elif o.reponses_jour is None:
        o.reponses_jour = r["reponses_jour"]
        bilan.objectif = True


def _explications(base, bilan, ids):
    """Explications de l'IA déjà payées dans Revision (communes à tous les comptes) : ajoutées si absentes."""
    deja = {(e.set_id, e.question_id) for e in Explication.query.with_entities(Explication.set_id, Explication.question_id)}
    for r in base.execute(f"SELECT * FROM {TABLES['explications']}"):
        cle = (ids(r["set_id"]), r["question_id"])
        if cle in deja or not r["texte"]:
            continue
        db.session.add(Explication(set_id=cle[0], question_id=cle[1], texte=r["texte"], modele=r["modele"] or "",
                                   jetons_entree=_entier(r["jetons_entree"]), jetons_sortie=_entier(r["jetons_sortie"]),
                                   cree_le=_date_heure(r["cree_le"]) or quiz.maintenant()))
        deja.add(cle)
        bilan.explications += 1


def copier_perso(source, destination, bilan, essai=False):
    """Fichiers .json de l'espace personnel de Revision → espace personnel du compte dans Hangar.
    Un fichier déjà présent n'est jamais écrasé."""
    source, destination = Path(source), Path(destination)
    for f in sorted(source.rglob("*.json")):
        rel = f.relative_to(source)
        if any(part.startswith(".") for part in rel.parts):
            continue
        cible = destination / rel
        if cible.exists():
            if cible.read_bytes() != f.read_bytes():
                bilan.fichiers_perso_gardes.append(PurePosixPath(rel.as_posix()).as_posix())
            continue
        bilan.fichiers_perso.append(PurePosixPath(rel.as_posix()).as_posix())
        if not essai:
            cible.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, cible)


# ---------------------------------------------------------------------------
# Reprise
# ---------------------------------------------------------------------------
def reprendre(chemin_base, user, joueur=None, coffre=None, perso=None, essai=False) -> Bilan:
    """Copie la progression du joueur `joueur` de Revision (par défaut : l'identifiant du compte)
    vers le compte `user` de Hangar. `coffre` : dossier `vault/` de Revision (rattachement des sets
    par leur contenu) ; `perso` : dossier perso/<joueur> de Revision (par défaut, à côté de la
    base). `essai` : rien n'est enregistré. Lève RepriseImpossible."""
    from .coffre import invalider

    joueur = joueur or user.username
    base = ouvrir(chemin_base)
    bilan = Bilan()
    try:
        presents = _tables(base)
        bilan.tables_absentes = [t for t in TABLES.values() if t not in presents]
        if joueur not in joueurs(base):
            autres = ", ".join(joueurs(base)) or "aucun"
            raise RepriseImpossible(f"Aucune progression pour « {joueur} » dans cette base (joueurs : {autres}). "
                                    "Indique le bon nom avec --joueur.")
        if perso is None:
            candidat = Path(chemin_base).resolve().parent / "perso" / _nom_perso_revision(joueur)
            perso = candidat if candidat.is_dir() else None
        if perso is not None:
            copier_perso(perso, quiz.dossier_perso(user.id), bilan, essai)
            if not essai:
                invalider()
        library = quiz.get_library()
        ids = Correspondance(library, user.id, joueur, coffre, perso)

        _progression(base, bilan, user.id, joueur, ids)
        if TABLES["reglages"] in presents:
            _reglages(base, bilan, user.id, joueur, ids)
        if TABLES["activite"] in presents:
            _activite(base, bilan, user.id, joueur)
        if TABLES["temps"] in presents:
            _temps(base, bilan, user.id, joueur)
        if TABLES["objectif"] in presents:
            _objectif(base, bilan, user.id, joueur)
        if TABLES["explications"] in presents:
            _explications(base, bilan, ids)
        bilan.sets = dict(ids.cache)
        bilan.par_contenu = dict(ids.par_contenu)
        # (À l'essai, les fichiers de l'espace personnel ne sont pas copiés : ils comptent comme présents.)
        a_copier = {quiz.PERSO + quiz.nom_perso(user.id) + "/" + PurePosixPath(f).with_suffix("").as_posix()
                    for f in bilan.fichiers_perso}
        bilan.introuvables = sorted({n for n in ids.cache.values()
                                     if n not in library.sets and n not in library.langues and n not in a_copier})
        if essai:
            db.session.rollback()
        else:
            db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    finally:
        base.close()
    return bilan
