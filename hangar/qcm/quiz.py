"""QCM de révision : chargement des sets de questions et logique de Leitner.

Porté depuis l'application « revision_qcm » (https://github.com/wakadu13/revision_qcm), puis de
Revision (Django) vers Hangar (Flask) : seuls l'accès aux réglages et le parcours du coffre changent.
Les sets sont des fichiers JSON déposés dans le coffre (même format que l'appli
d'origine). Un fichier JSON est considéré comme un set s'il contient une liste
« questions ». La catégorie est le nom du dossier qui contient le fichier ; une
clé « langue » facultative (ex. "Anglais") range le set dans la section Langues.

Ce module reste « pur » (aucun import des modèles ni du rendu) : la persistance de
la progression est gérée par le modèle QuizProgress, et le rendu LaTeX/Markdown
des énoncés est fait dans les vues. Les dates sont des heures locales naïves (comme
partout dans Hangar).
"""

from __future__ import annotations

import json
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import cached_property
from pathlib import Path, PurePosixPath

from flask import current_app


def maintenant() -> datetime:
    """Heure locale (naïve), comme dans tout Hangar."""
    return datetime.now().replace(microsecond=0)


def jour(dt) -> date:
    """Date locale d'un instant (remplace timezone.localdate de Django)."""
    return dt.date() if isinstance(dt, datetime) else dt

# --- Révision espacée ---
# Les questions sont planifiées par FSRS (fsrs.py, comme le module Langues) ; la
# boîte de Leitner reste tenue à jour comme indicateur simple (0 à 5). Une
# progression antérieure à FSRS (sans échéance) suit encore les intervalles de
# Leitner jusqu'à sa prochaine réponse, où sa stabilité est estimée d'après sa boîte.
INTERVALLES_JOURS = {0: 0, 1: 1, 2: 3, 3: 7, 4: 14, 5: 30}
STABILITE_LEITNER = {0: 0.5, 1: 1.0, 2: 3.0, 3: 7.0, 4: 14.0, 5: 30.0}
BOITE_MAX = 5
TYPES_VALIDES = {"QCM", "QCR", "TROU", "QRC", "FLASHCARD"}
TYPES_TEXTE = {"TROU", "QRC"}  # réponse tapée au clavier, comparée à reponses_texte
CATEGORIE_DEFAUT = "Général"


def normaliser_texte(s: str) -> str:
    """Normalise une réponse libre : casse, accents et espaces superflus ignorés."""
    s = unicodedata.normalize("NFKD", str(s).strip().casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.split())


def cle_naturelle(s: str):
    """Clé de tri qui range les nombres dans l'ordre (« Linux 2 » avant « Linux 10 »)."""
    return [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"(\d+)", normaliser_texte(s)) if p]


def normaliser_reponse(s: str) -> str:
    """Comme normaliser_texte, en ignorant aussi les apostrophes typographiques et
    la ponctuation finale (. ! ? ; :) — utile pour les réponses tapées (TROU, QRC)."""
    s = normaliser_texte(str(s).replace("\u2019", "'").replace("\u2018", "'"))
    return s.rstrip(".!?;: ")


def intervalle_ecoule(boite: int, last_review, now) -> bool:
    """Vrai si l'intervalle de Leitner est écoulé (ou si jamais révisée)."""
    if last_review is None:
        return True
    jours = INTERVALLES_JOURS.get(boite, INTERVALLES_JOURS[BOITE_MAX])
    return now >= last_review + timedelta(days=jours)


# Note d'une réponse : auto-évaluation des flashcards ; les autres types sont
# notés « bien » (juste) ou « à revoir » (faux).
A_REVOIR, DIFFICILE, BIEN, FACILE = "a_revoir", "difficile", "bien", "facile"
NOTES = (A_REVOIR, DIFFICILE, BIEN, FACILE)


def note_de(reponse) -> str:
    """Note d'une réponse donnée comme note ou comme booléen (juste/faux)."""
    if reponse in NOTES:
        return reponse
    return BIEN if reponse else A_REVOIR


def nouvelle_boite(boite: int, reponse) -> int:
    """Boîte après une réponse (note ou booléen juste/faux).

    À revoir → retour en boîte 0 : la question est oubliée, elle est réapprise
    le jour même puis repart du début ; difficile → reste dans sa boîte (au
    moins la 1) ; bien → monte d'une boîte ; facile → monte de deux.
    """
    note = note_de(reponse)
    if note == A_REVOIR:
        return 0
    if note == DIFFICILE:
        return max(1, boite)
    return min(BOITE_MAX, boite + (2 if note == FACILE else 1))


# ---------------------------------------------------------------------------
# Modèles de données
# ---------------------------------------------------------------------------
@dataclass
class Question:
    id: str
    type: str
    enonce: str
    choix: list[str] = field(default_factory=list)
    reponses: list[int] = field(default_factory=list)        # indices 0-based, QCM/QCR
    reponses_texte: list[str] = field(default_factory=list)  # réponses acceptées, TROU/QRC
    consigne: str = ""                                       # ex. « Traduire en anglais », QRC
    verso: str = ""                                          # dos de carte, FLASHCARD
    explication: str = ""
    masque: dict | None = None                               # carte d'image masquée (wiki/masque.py)

    @property
    def est_qcr(self) -> bool:
        return self.type == "QCR"

    @property
    def est_trou(self) -> bool:
        return self.type == "TROU"

    @property
    def est_qrc(self) -> bool:
        return self.type == "QRC"

    @property
    def est_flashcard(self) -> bool:
        return self.type == "FLASHCARD"

    def est_correcte(self, selection) -> bool:
        return set(selection) == set(self.reponses)

    def est_correcte_texte(self, reponse: str) -> bool:
        cible = normaliser_reponse(reponse)
        return any(cible == normaliser_reponse(r) for r in self.reponses_texte)


@dataclass
class QuestionSet:
    id: str            # identifiant stable = chemin relatif sans extension
    titre: str
    description: str
    questions: list[Question]
    categorie: str = CATEGORIE_DEFAUT
    path: PurePosixPath | None = None
    langue: str = ""   # set rangé dans la section Langues (ex. "Anglais")
    source_page: str = ""  # page du wiki dont le set a été généré (clé), s'il y en a une
    proprietaire: str = ""  # espace personnel : nom (nom_perso) du seul compte qui le voit
    fichier: Path | None = None  # chemin complet du fichier JSON

    def __len__(self) -> int:
        return len(self.questions)

    @property
    def est_masque(self) -> bool:
        """Set de cartes à image masquée (modifiable dans l'éditeur de zones)."""
        return any(q.masque for q in self.questions)

    @cached_property
    def texte_recherche(self) -> str:
        """Titre, description, catégorie et énoncés, normalisés pour la recherche."""
        parts = [self.titre, self.description, self.categorie]
        parts += [q.enonce for q in self.questions]
        return normaliser_texte(" ".join(parts))

    def correspond(self, requete: str) -> bool:
        """Vrai si tous les mots de la requête figurent dans le set (accents ignorés)."""
        return all(mot in self.texte_recherche for mot in normaliser_texte(requete).split())


# ---------------------------------------------------------------------------
# Chargement et validation
# ---------------------------------------------------------------------------
def lire_json(chemin, racine):
    """Lit un fichier JSON du coffre. Renvoie (chemin relatif, données) ; ValueError si invalide."""
    chemin = Path(chemin)
    rel = PurePosixPath(chemin.relative_to(racine).as_posix())
    try:
        return rel, json.loads(chemin.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON invalide dans {rel} : {e}")


def question_depuis_json(q, i: int, contexte: str) -> Question:
    """Valide une question (QCM, QCR, TROU, QRC ou FLASHCARD). `i` est son rang (à
    partir de 1), utilisé pour l'identifiant par défaut `q{i}`. Réutilisé par le
    module Langues pour ses exercices de grammaire."""
    if not isinstance(q, dict) or "question" not in q:
        raise ValueError(f"{contexte} : clé 'question' manquante.")
    type_q = str(q.get("type", "QCM")).upper()
    if type_q not in TYPES_VALIDES:
        raise ValueError(f"{contexte} : type '{type_q}' inconnu (QCM, QCR, TROU, QRC, FLASHCARD, MASQUE).")

    choix, reponses, reponses_texte, verso, consigne = [], [], [], "", ""
    if type_q in ("QCM", "QCR"):
        for clef in ("choix", "reponses"):
            if clef not in q:
                raise ValueError(f"{contexte} : clé '{clef}' manquante.")
        choix = [str(c) for c in q["choix"]]
        reponses = sorted(q["reponses"]) if isinstance(q["reponses"], list) else []
        if not choix:
            raise ValueError(f"{contexte} : la liste 'choix' est vide.")
        if not reponses:
            raise ValueError(f"{contexte} : aucune bonne réponse fournie.")
        for r in reponses:
            if not isinstance(r, int) or isinstance(r, bool) or not (0 <= r < len(choix)):
                raise ValueError(f"{contexte} : indice de réponse {r} hors limites.")
        if type_q == "QCM" and len(reponses) != 1:
            raise ValueError(f"{contexte} : un QCM doit avoir exactement 1 réponse.")
    elif type_q in TYPES_TEXTE:
        reponses_texte = [str(r) for r in q.get("reponses_texte", []) if str(r).strip()]
        if not reponses_texte:
            raise ValueError(f"{contexte} : 'reponses_texte' doit contenir une réponse.")
        consigne = str(q.get("consigne", "")) if type_q == "QRC" else ""
    elif type_q == "FLASHCARD":
        verso = str(q.get("verso", ""))
        if not verso.strip():
            raise ValueError(f"{contexte} : clé 'verso' manquante (dos de la carte).")

    return Question(
        id=str(q.get("id", f"q{i}")),
        type=type_q,
        enonce=str(q["question"]),
        choix=choix,
        reponses=reponses,
        reponses_texte=reponses_texte,
        consigne=consigne,
        verso=verso,
        explication=str(q.get("explication", "")),
    )


def set_depuis_json(data, rel: PurePosixPath) -> QuestionSet:
    """Construit un set à partir du JSON lu dans `rel`. Lève ValueError si malformé."""
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        raise ValueError(f"{rel} : clé 'questions' (liste) manquante.")

    questions: list[Question] = []
    for i, q in enumerate(data["questions"], start=1):
        if isinstance(q, dict) and str(q.get("type", "")).upper() == "MASQUE":
            from .masque import questions as questions_masque

            questions.extend(questions_masque(q, i, f"{rel} » question #{i}"))
            continue
        question = question_depuis_json(q, i, f"{rel} » question #{i}")
        questions.append(question)
        # QRC « inverse » : on pose aussi la question dans l'autre sens
        # (ex. chien → dog, puis dog → chien), suivie séparément dans Leitner.
        if question.est_qrc and q.get("inverse"):
            questions.append(Question(
                id=f"{question.id}~inverse",
                type="QRC",
                enonce=question.reponses_texte[0],
                reponses_texte=[question.enonce],
                consigne=str(q.get("consigne_inverse", "")),
                explication=question.explication,
            ))
    if not questions:
        raise ValueError(f"{rel} : aucune question.")

    return QuestionSet(
        id=rel.with_suffix("").as_posix(),
        titre=str(data.get("titre") or rel.stem),
        description=str(data.get("description", "")),
        questions=questions,
        categorie=rel.parent.name or CATEGORIE_DEFAUT,
        path=rel,
        langue=str(data.get("langue", "")).strip(),
        source_page=str(data.get("source_page", "")).strip(),
    )


def charger_set(chemin, racine) -> QuestionSet:
    """Charge et valide un set depuis un fichier JSON. Lève ValueError si malformé."""
    rel, data = lire_json(chemin, racine)
    return set_depuis_json(data, rel)


def charger(chemin, racine):
    """Charge un set de questions ou un fichier de langue (clé « langue »)."""
    from . import langues

    rel, data = lire_json(chemin, racine)
    if langues.est_fichier_langue(data):
        return langues.set_depuis_json(data, rel)
    return set_depuis_json(data, rel)


# --- Espaces personnels ---
# Chaque compte peut importer ses propres sets et paquets de langue, visibles de lui
# seul : DATA_DIR/qcm_perso/<id du compte>/<dossier>/<nom>.json, identifiants « ~<id>/… ».
# (Dans Revision, le « joueur » était le nom d'utilisateur ; dans Hangar, c'est user_id.)
PERSO = "~"


def nom_perso(player):
    """Nom du dossier personnel d'un compte (caractères sûrs pour un nom de fichier)."""
    return re.sub(r"[^\w.@+-]", "_", str(player if player is not None else ""))[:150] or "_"


def racine_perso():
    return Path(current_app.config["DATA_DIR"]) / "qcm_perso"


def dossier_perso(player):
    return racine_perso() / nom_perso(player)


class Library:
    """Ensemble des sets de questions et des fichiers de langue : ceux du coffre
    (communs) et ceux des espaces personnels (vus seulement par leur propriétaire,
    voir pour())."""

    def __init__(self, root, files, perso_root=None, perso_files=()):
        from .langues import SetLangue

        self.root = Path(root)
        self.sets: dict[str, QuestionSet] = {}
        self.langues: dict[str, SetLangue] = {}
        self.errors: list[str] = []
        self.erreurs_perso: dict[str, list[str]] = {}
        for rel, _mtime, _size in files:
            if not rel.lower().endswith(".json"):
                continue
            try:
                charge = charger(self.root / rel, self.root)
            except ValueError as e:
                # On distingue « pas un set » (JSON sans 'questions', ex. .canvas)
                # d'un vrai set malformé, qu'on signale.
                if "questions' (liste) manquante" not in str(e):
                    self.errors.append(str(e))
                continue
            charge.fichier = self.root / rel
            if isinstance(charge, SetLangue):
                self.langues[charge.id] = charge
            else:
                self.sets[charge.id] = charge
        if perso_root is None:
            return
        from . import langues

        perso_root = Path(perso_root)
        for rel, _mtime, _size in perso_files:
            if not rel.lower().endswith(".json") or "/" not in rel:
                continue
            proprietaire = rel.split("/", 1)[0]
            chemin = perso_root / rel
            try:
                _, data = lire_json(chemin, perso_root)
                rel_id = PurePosixPath(PERSO + rel)
                charge = (langues.set_depuis_json(data, rel_id) if langues.est_fichier_langue(data)
                          else set_depuis_json(data, rel_id))
            except ValueError as e:
                if "questions' (liste) manquante" not in str(e):
                    self.erreurs_perso.setdefault(proprietaire, []).append(str(e))
                continue
            charge.proprietaire, charge.fichier = proprietaire, chemin
            if isinstance(charge, SetLangue):
                self.langues[charge.id] = charge
            else:
                self.sets[charge.id] = charge

    def pour(self, player):
        """La bibliothèque vue par un compte : sets communs et ses sets personnels.
        ``player`` None : sets communs seulement (rangement du coffre par les admins)."""
        import copy

        nom = nom_perso(player) if player is not None else None
        vue = copy.copy(self)
        vue.sets = {k: s for k, s in self.sets.items() if not s.proprietaire or s.proprietaire == nom}
        vue.langues = {k: s for k, s in self.langues.items() if not s.proprietaire or s.proprietaire == nom}
        vue.errors = self.errors + (self.erreurs_perso.get(nom, []) if nom else [])
        return vue

    def commune(self):
        return self.pour(None)

    def personnels(self, player):
        """Sets et paquets de l'espace personnel d'un compte."""
        nom = nom_perso(player)
        return sorted([s for s in (*self.sets.values(), *self.langues.values()) if s.proprietaire == nom],
                      key=lambda s: (s.path.parent.name.lower(), s.titre.lower()))

    def get(self, set_id):
        return self.sets.get(set_id)

    def sorted_sets(self):
        # Ordre « naturel » (Linux 2 avant Linux 10) : ajout de Hangar.
        return sorted(self.sets.values(), key=lambda s: (s.categorie.lower(), cle_naturelle(s.titre)))

    def categories(self):
        """{catégorie: [sets]} trié, pour l'affichage groupé."""
        cats: dict[str, list[QuestionSet]] = {}
        for qset in self.sorted_sets():
            cats.setdefault(qset.categorie, []).append(qset)
        return cats

    def noms_langues(self):
        """Langues distinctes (fichiers de langue et sets rattachés à une langue), triées."""
        noms = {s.langue for s in self.langues.values()} | {s.langue for s in self.sets.values() if s.langue}
        return sorted(noms, key=str.lower)

    def sets_langue(self, langue):
        """Fichiers d'une langue, dans l'ordre des chemins."""
        return [s for _, s in sorted(self.langues.items()) if s.langue == langue]

    def all_questions(self):
        """Toutes les paires (set, question) de la bibliothèque."""
        return [(qset, q) for qset in self.sorted_sets() for q in qset.questions]


_lock = threading.Lock()
_cache = {"cle": None, "library": None}


def get_library():
    """Renvoie la bibliothèque de QCM (coffre et espaces personnels), réindexée
    seulement si un fichier a changé. Pour un compte : get_library().pour(compte)."""
    from .coffre import signature as signature_coffre  # import tardif : évite un cycle à l'import du module

    root = Path(current_app.config["WIKI_DIR"])
    perso = racine_perso()
    signature = tuple(f for f in signature_coffre(root) if f[0].lower().endswith(".json"))
    signature_perso = tuple(f for f in signature_coffre(perso) if f[0].lower().endswith(".json"))
    cle = (root, perso, signature, signature_perso)
    with _lock:
        if _cache["library"] is None or _cache["cle"] != cle:
            _cache.update(cle=cle, library=Library(root, signature, perso, signature_perso))
        return _cache["library"]
