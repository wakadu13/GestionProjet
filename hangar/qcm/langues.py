"""Module Langues : vocabulaire, conjugaison et grammaire.

Porté depuis l'application « revision_qcm » (src/langues.rs). Il applique les
techniques dont l'efficacité est la mieux établie par la recherche sur la
mémoire et l'acquisition des langues :

- **Répétition espacée optimisée (FSRS)** : chaque élément revient juste avant
  d'être oublié (voir fsrs.py).
- **Rappel actif en production** : on tape la réponse au lieu de la reconnaître.
- **Facettes multiples d'un même mot** : reconnaissance (langue → français),
  puis, une fois le mot découvert, production (français → langue) et rappel en
  contexte (phrase à compléter). Deux facettes d'un même mot ne sont jamais
  posées dans la même session.
- **Conjugaison personne par personne** : chaque forme est suivie séparément,
  le tableau complet est montré à la correction.
- **Grammaire par la pratique** : phrases à trous, remise en ordre de mots,
  correction d'erreurs, en plus des QCM.
- **Entrelacement** : vocabulaire, conjugaison et grammaire sont mélangés au sein
  d'une session.
- **Correction immédiate et réessai** : les différences sont surlignées, puis
  l'exercice revient quelques questions plus tard (géré par le navigateur).
- **Tolérance raisonnable** : en vocabulaire, un accent manquant ou une faute de
  frappe compte comme « presque » (note Difficile) plutôt qu'un échec.

Comme quiz.py, ce module ne dépend ni des modèles ni des vues : la progression
lui est passée sous la forme {(set_id, exercice_id): stats}, où `stats` a les
attributs de QuizProgress (stabilite, difficulte, echeance, decouverte, oublis,
last_review, last_correct).
"""

from __future__ import annotations

import random
import re
import zlib
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import PurePosixPath

from . import fsrs, quiz

VOCABULAIRE, CONJUGAISON, GRAMMAIRE = "vocabulaire", "conjugaison", "grammaire"
GENRES = (VOCABULAIRE, CONJUGAISON, GRAMMAIRE)
NOMS_GENRES = {VOCABULAIRE: "Vocabulaire", CONJUGAISON: "Conjugaison", GRAMMAIRE: "Grammaire"}

# Modes de session : session du jour (tout mélangé), un seul genre, points faibles,
# révision intensive (tout, jusqu'à tout savoir : avant un examen).
QUOTIDIEN, FAIBLES, INTENSIF = "quotidien", "faibles", "intensif"
MODES = {QUOTIDIEN: "Session du jour", **NOMS_GENRES, FAIBLES: "Points faibles", INTENSIF: "Révision intensive"}
TAILLE_MAX_INTENSIF = 200

# Quota par défaut de nouveaux éléments par jour : au-delà, la charge de
# révisions des jours suivants devient difficile à tenir.
NOUVEAUX_PAR_JOUR = 20
TAILLE_SESSION = 20
MAITRISE_JOURS = 21   # stabilité à partir de laquelle un élément est « maîtrisé »

# Formes d'exercice.
CARTE, SAISIE, CHOIX, ORDRE = "carte", "saisie", "choix", "ordre"


# ===========================================================================
# Modèle
# ===========================================================================
@dataclass
class Exercice:
    """Une facette d'un élément (mot, forme verbale, point de grammaire).

    `donnees` dépend de `forme` :
      carte  : recto, verso (et oral = {reponses} : prononciation vérifiée au micro)
      saisie : consigne, invite, reponses, tolerant, prerempli, tableau
               (tableau = {titre, lignes: [[personne, forme]], surligne} ou None)
      choix  : enonce, choix, reponses, multiple
      ordre  : consigne, mots, reponses
    """

    id: str                 # clé de suivi (ex. "voc-001#prod")
    entree: str             # élément d'origine : ses facettes sont limitées par session
    genre: str
    etiquette: str          # type d'exercice affiché (ex. « Vocabulaire · production »)
    resume: str             # résumé court (statistiques, points faibles)
    forme: str
    donnees: dict
    details: list[str] = field(default_factory=list)  # montrés après la réponse
    prerequis: str | None = None                      # facette à connaître avant celle-ci
    max_par_session: int = 1
    audio: dict | None = None                         # {texte, quand} lu par la synthèse vocale


@dataclass
class SetLangue:
    """Un fichier de langue (`"langue": "..."`)."""

    id: str
    langue: str
    titre: str
    description: str
    exercices: list[Exercice]
    particules: list[str] = field(default_factory=list)          # mots facultatifs en tête de réponse
    caracteres_speciaux: list[str] = field(default_factory=list)  # boutons sous le champ de saisie
    nouveaux_par_jour: int | None = None
    path: PurePosixPath | None = None
    types_vocabulaire: list[str] = field(default_factory=list)  # cartes de vocabulaire du paquet
    code_langue: str = ""  # synthèse vocale (ex. "en-GB")
    proprietaire: str = ""  # espace personnel : nom (quiz.nom_perso) du seul compte qui le voit
    fichier: object = None  # chemin complet du fichier JSON (Path)

    def __len__(self):
        return len(self.exercices)

    @property
    def noms_types(self):
        """Types de cartes de vocabulaire, en clair (« Carte FR → EN »…)."""
        return [nom_type(t, self.langue) for t in self.types_vocabulaire]

    def exercice(self, exo_id):
        return next((e for e in self.exercices if e.id == exo_id), None)


# ===========================================================================
# Chargement
# ===========================================================================
def est_fichier_langue(data) -> bool:
    """Vrai si le JSON décrit un fichier de langue plutôt qu'un set de questions."""
    return isinstance(data, dict) and "langue" in data and "questions" not in data


def _texte(v) -> str:
    return "" if v is None else str(v)


def _textes(v) -> list[str]:
    """Chaîne ou liste de chaînes → liste (vide si absent)."""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    return [str(v)] if str(v).strip() else []


def _objet(v, contexte):
    if not isinstance(v, dict):
        raise ValueError(f"{contexte} : un objet JSON est attendu.")
    return v


def _requis(o, clef, contexte):
    if clef not in o:
        raise ValueError(f"{contexte} : clé '{clef}' manquante.")
    s = _texte(o[clef])
    if not s.strip():
        raise ValueError(f"{contexte} : '{clef}' ne doit pas être vide.")
    return s


def _section(data, clef, nom):
    v = data.get(clef)
    if v is None:
        return []
    if not isinstance(v, list):
        raise ValueError(f"{nom} : '{clef}' doit être une liste.")
    return v


def trouer(exemple: str):
    """« She [achieved] her goal. » → (« She _____ her goal. », « achieved »), ou None."""
    m = re.search(r"\[([^\]]*)\]", exemple)
    if not m or not m.group(1).strip():
        return None
    return exemple[:m.start()] + "_____" + exemple[m.end():], m.group(1)


def _sans_crochets(s):
    return s.replace("[", "").replace("]", "")


def abreviation(langue: str) -> str:
    """« Anglais » → « EN », « Français » → « FR » (sinon deux premières lettres)."""
    connues = {
        "anglais": "EN", "english": "EN", "français": "FR", "francais": "FR", "french": "FR",
        "espagnol": "ES", "español": "ES", "spanish": "ES", "allemand": "DE", "deutsch": "DE",
        "german": "DE", "italien": "IT", "italiano": "IT", "italian": "IT", "portugais": "PT",
        "português": "PT",
    }
    l = langue.lower()
    return connues.get(l) or "".join(c for c in l if c.isalpha())[:2].upper()


def mots_a_ordonner(phrase: str) -> list[str]:
    """Mots d'une phrase à remettre en ordre (ponctuation finale retirée, première
    lettre en minuscule pour ne pas donner d'indice, sauf « I »)."""
    mots = phrase.strip().rstrip(".!?…").strip().split()
    if mots and mots[0] != "I" and not mots[0].startswith("I'"):
        premier = mots[0]
        # Première lettre (après une éventuelle ponctuation ouvrante : « ¿Dónde »).
        for i, c in enumerate(premier):
            if c.isalpha():
                mots[0] = premier[:i] + c.lower() + premier[i + 1:]
                break
    return mots


# Types de cartes de vocabulaire, choisis par fichier (clé « exercices ») :
# L = langue apprise, M = langue maternelle.
RECONNAISSANCE = "reconnaissance"  # carte L → M
DECOUVERTE = "decouverte"          # carte M → L
PRODUCTION = "production"          # traduction à taper M → L
TRADUCTION = "traduction"          # traduction à taper L → M
CHOIX_MOT = "choix"                # QCM M → L : le bon mot parmi ceux du paquet
CONTEXTE = "contexte"              # phrase d'exemple à compléter
DICTEE = "dictee"                  # le mot est lu (synthèse vocale), on l'écrit en L
PRONONCIATION = "prononciation"    # le mot est à dire : vérifié par la reconnaissance vocale
# Tout en langue apprise (anglais → anglais) : d'après la « definition » de chaque mot.
DEFINITION = "definition"                # carte : la définition, puis le mot
DEFINITION_SAISIE = "definition_saisie"  # la définition, le mot à taper (indice : 1re lettre, longueur)
DEFINITION_CHOIX = "definition_choix"    # QCM : le mot qui correspond à la définition
# Autres QCM.
CHOIX_TRADUCTION = "choix_traduction"    # QCM L → M : la bonne traduction
CONTEXTE_CHOIX = "contexte_choix"        # QCM : le mot qui manque dans la phrase d'exemple
ECOUTE_CHOIX = "ecoute_choix"            # QCM : le mot est lu (sans être écrit), choisir son sens
TYPES_VOCABULAIRE = {
    DECOUVERTE: "Carte {M} → {L}",
    PRODUCTION: "Traduction à taper {M} → {L}",
    CHOIX_MOT: "QCM {M} → {L} (choisir le bon mot)",
    CONTEXTE: "Phrase à compléter (en {langue})",
    CONTEXTE_CHOIX: "QCM phrase à compléter (choisir le mot qui manque, en {langue})",
    DICTEE: "Dictée : écouter le mot et l'écrire (en {langue})",
    ECOUTE_CHOIX: "QCM à l'écoute : entendre le mot, choisir son sens",
    RECONNAISSANCE: "Carte {L} → {M}",
    TRADUCTION: "Traduction à taper {L} → {M}",
    CHOIX_TRADUCTION: "QCM {L} → {M} (choisir la bonne traduction)",
    DEFINITION: "Carte définition → mot ({L} → {L})",
    DEFINITION_SAISIE: "Définition → mot à remplir ({L} → {L})",
    DEFINITION_CHOIX: "QCM définition → mot ({L} → {L})",
    PRONONCIATION: "Prononciation : dire le mot (en {langue}), vérifiée au micro",
}
# Types qui demandent une traduction, une définition ou une phrase d'exemple « [mot] ».
AVEC_TRADUCTION = {DECOUVERTE, PRODUCTION, TRADUCTION, CHOIX_MOT, CHOIX_TRADUCTION}
AVEC_DEFINITION = {DEFINITION, DEFINITION_SAISIE, DEFINITION_CHOIX}
AVEC_EXEMPLE = {CONTEXTE, CONTEXTE_CHOIX}
# Types possibles sans traduction (listes « mot – définition » dans la langue apprise).
SANS_TRADUCTION = [DEFINITION, DEFINITION_SAISIE, DEFINITION_CHOIX, RECONNAISSANCE, CONTEXTE, CONTEXTE_CHOIX,
                   ECOUTE_CHOIX, DICTEE, PRONONCIATION]
VOCABULAIRE_DEFINITIONS = [DEFINITION, DEFINITION_CHOIX, DEFINITION_SAISIE]
# Code de langue (BCP 47) pour la synthèse vocale du navigateur ; la clé
# « code_langue » d'un fichier l'impose (ex. "en-US").
CODES_LANGUE = {
    "EN": "en-GB", "FR": "fr-FR", "ES": "es-ES", "DE": "de-DE", "IT": "it-IT", "PT": "pt-PT",
    "RO": "ro-RO", "NL": "nl-NL", "RU": "ru-RU", "JA": "ja-JP", "ZH": "zh-CN", "AR": "ar-SA",
}


def code_langue(langue: str) -> str:
    abr = abreviation(langue)
    return CODES_LANGUE.get(abr, abr.lower())
# Sans clé « exercices » : le fonctionnement d'origine.
VOCABULAIRE_PAR_DEFAUT = [RECONNAISSANCE, PRODUCTION, CONTEXTE]
# La première carte retenue (dans cet ordre) fait découvrir le mot ; les autres
# ne viennent qu'une fois celle-ci réussie.
ORDRE_DECOUVERTE = [DECOUVERTE, RECONNAISSANCE, DEFINITION, CHOIX_MOT, CHOIX_TRADUCTION, DEFINITION_CHOIX, PRODUCTION,
                    TRADUCTION, DEFINITION_SAISIE, CONTEXTE, CONTEXTE_CHOIX, ECOUTE_CHOIX, DICTEE, PRONONCIATION]
SUFFIXES = {RECONNAISSANCE: "rec", DECOUVERTE: "dec", PRODUCTION: "prod", TRADUCTION: "trad",
            CHOIX_MOT: "qcm", CONTEXTE: "ctx", DICTEE: "dict", PRONONCIATION: "oral",
            DEFINITION: "def", DEFINITION_SAISIE: "defm", DEFINITION_CHOIX: "defq",
            CHOIX_TRADUCTION: "qtrad", CONTEXTE_CHOIX: "qctx", ECOUTE_CHOIX: "qson"}
# Quand lire le mot à voix haute : à l'affichage, au retournement de la carte, après la correction.
DEBUT, VERSO, FIN = "debut", "verso", "fin"


def types_vocabulaire(data, rel="") -> list[str]:
    """Types de cartes de vocabulaire d'un fichier (clé « exercices »), validés."""
    valeur = data.get("exercices")
    if valeur is None:
        return list(VOCABULAIRE_PAR_DEFAUT)
    types = _textes(valeur)
    inconnus = [t for t in types if t not in TYPES_VOCABULAIRE]
    if inconnus or not types:
        raise ValueError(f"{rel} : 'exercices' doit lister des types parmi {', '.join(TYPES_VOCABULAIRE)}.")
    return types


def nom_type(t, langue, maternelle="Français") -> str:
    return TYPES_VOCABULAIRE[t].format(L=abreviation(langue), M=abreviation(maternelle), langue=langue.lower())


def _distracteurs(id_, mot, autres_mots, n=3, exclus=()):
    """Choix du QCM : le mot et jusqu'à `n` autres mots du paquet, dans un ordre
    stable (le même à chaque chargement). ``exclus`` : réponses justes aussi (autres
    traductions du mot), jamais proposées comme pièges. Renvoie (choix, indice du bon mot)."""
    hasard = random.Random(zlib.crc32(id_.encode("utf-8")))
    interdits = {mot.strip().lower(), *(x.strip().lower() for x in exclus)}
    autres = sorted({m for m in autres_mots if m.strip().lower() not in interdits})
    choix = hasard.sample(autres, min(n, len(autres))) + [mot]
    hasard.shuffle(choix)
    return choix, choix.index(mot)


def sans_particule(mot, particules):
    """« to achieve » → « achieve » (petits mots facultatifs du paquet en tête)."""
    premier, _, reste = mot.partition(" ")
    return reste.strip() if reste.strip() and premier.lower() in particules else mot


def _masquer(texte, formes):
    """La définition sans le mot qu'elle définit, ni ses variantes (remplacés par _____)."""
    for f in sorted({x.strip() for x in formes if len(x.strip()) >= 3}, key=len, reverse=True):
        texte = re.sub(rf"(?<!\w){re.escape(f)}(?!\w)", "_____", texte, flags=re.IGNORECASE)
    return texte


def indice_lettres(mot):
    """Première lettre et longueur de chaque mot : « achieve » → « a······ (7 lettres) »."""
    mots = mot.split()
    trace = " ".join(m[0] + "·" * (len(m) - 1) for m in mots)
    if len(mots) > 1:
        return f"{trace} ({len(mots)} mots)"
    return f"{trace} ({len(mot)} lettre{'s' if len(mot) > 1 else ''})"


def _facettes_vocabulaire(id_, mot, trads, o, types, langue, abr_l, abr_m, autres, particules=()):
    """Exercices d'un mot selon les types de cartes choisis. ``autres`` : les autres
    entrées du paquet ({mot, trad, definition}), pour les choix des QCM."""
    trad = ", ".join(trads)
    exemple = _texte(o.get("exemple"))
    ex_trad = _texte(o.get("exemple_traduction"))
    prononciation = _texte(o.get("prononciation"))
    variantes = _textes(o.get("variantes"))
    reponses = [mot] + variantes
    base = sans_particule(mot, particules)
    definition = _masquer(_texte(o.get("definition")).strip(), [mot, base, *variantes])
    sens = trad or definition  # ce que veut dire le mot, en français si possible
    avec_prononciation = f"{mot}   {prononciation}" if prononciation else mot
    autres_mots = [a["mot"] for a in autres]

    details = []
    if exemple:
        d = f"Ex. : « {_sans_crochets(exemple)} »"
        if ex_trad:
            d += f" = « {ex_trad} »"
        details.append(d)
    details += [_texte(o[c]) for c in ("note", "explication") if _texte(o.get(c))]
    # Après la réponse, la définition complète les cartes qui ne la montrent pas déjà.
    detail_definition = [f"Définition : « {definition} »"] if definition and trads else []

    troue = trouer(exemple)
    # QCM du sens : traductions des autres mots (ou leurs définitions s'il n'y a pas de traduction).
    sens_autres = [a["trad"] for a in autres if a["trad"]] if trads else [a["definition"] for a in autres if a["definition"]]
    possibles = [t for t in types
                 if not (t in AVEC_TRADUCTION and not trads) and not (t in AVEC_DEFINITION and not definition)
                 and not (t in AVEC_EXEMPLE and not troue) and not (t == RECONNAISSANCE and not sens)
                 and not (t in (CHOIX_MOT, DEFINITION_CHOIX, CONTEXTE_CHOIX) and not autres_mots)
                 and not (t in (CHOIX_TRADUCTION, ECOUTE_CHOIX) and not (sens and sens_autres))]
    entree = next((t for t in ORDRE_DECOUVERTE if t in possibles), None)
    # Son : le mot (ou la phrase d'exemple) en langue apprise, lu par le navigateur.
    phrase_lue = _sans_crochets(exemple)
    sons = {RECONNAISSANCE: (mot, DEBUT), DECOUVERTE: (mot, VERSO), PRODUCTION: (mot, FIN), TRADUCTION: (mot, DEBUT),
            CHOIX_MOT: (mot, FIN), CONTEXTE: (phrase_lue, FIN), DICTEE: (mot, DEBUT), PRONONCIATION: (mot, VERSO),
            DEFINITION: (mot, VERSO), DEFINITION_SAISIE: (mot, FIN), DEFINITION_CHOIX: (mot, FIN),
            CHOIX_TRADUCTION: (mot, DEBUT), CONTEXTE_CHOIX: (phrase_lue, FIN), ECOUTE_CHOIX: (mot, DEBUT)}
    facettes = []
    for t in possibles:
        exo_id = f"{id_}#{SUFFIXES[t]}"
        prerequis = None if t == entree else f"{id_}#{SUFFIXES[entree]}"
        commun = {"entree": id_, "genre": VOCABULAIRE, "prerequis": prerequis,
                  "details": details + ([] if t in AVEC_DEFINITION else detail_definition),
                  "audio": {"texte": sons[t][0], "quand": sons[t][1]}}
        if t == RECONNAISSANCE:
            etiquette = (f"Vocabulaire · reconnaissance ({abr_l} → {abr_m})" if trads
                         else f"Vocabulaire · mot → définition ({abr_l} → {abr_l})")
            facettes.append(Exercice(id=exo_id, etiquette=etiquette,
                                     resume=f"{mot} → {sens}", forme=CARTE,
                                     donnees={"recto": avec_prononciation, "verso": sens}, **commun))
        elif t == DECOUVERTE:
            facettes.append(Exercice(id=exo_id, etiquette=f"Vocabulaire · carte ({abr_m} → {abr_l})",
                                     resume=f"{trad} → {mot}", forme=CARTE,
                                     donnees={"recto": trad, "verso": avec_prononciation}, **commun))
        elif t == PRODUCTION:
            facettes.append(Exercice(id=exo_id, etiquette=f"Vocabulaire · production ({abr_m} → {abr_l})",
                                     resume=f"{trad} → {mot}", forme=SAISIE,
                                     donnees=_saisie(f"Comment dit-on « {trad} » en {langue.lower()} ?", reponses, tolerant=True),
                                     **commun))
        elif t == TRADUCTION:
            facettes.append(Exercice(id=exo_id, etiquette=f"Vocabulaire · compréhension ({abr_l} → {abr_m})",
                                     resume=f"{mot} → {trad}", forme=SAISIE,
                                     donnees=_saisie(f"Que veut dire « {mot} » ?", trads, tolerant=True), **commun))
        elif t == CHOIX_MOT:
            choix, bon = _distracteurs(id_, mot, autres_mots)
            facettes.append(Exercice(id=exo_id, etiquette=f"Vocabulaire · QCM ({abr_m} → {abr_l})",
                                     resume=f"{trad} → {mot}", forme=CHOIX,
                                     donnees={"enonce": f"Comment dit-on « {trad} » en {langue.lower()} ?",
                                              "choix": choix, "reponses": [bon], "multiple": False},
                                     **commun))
        elif t == CONTEXTE:
            trouee, attendu = troue
            facettes.append(Exercice(id=exo_id, etiquette="Vocabulaire · en contexte",
                                     resume=f"{mot} (en contexte)", forme=SAISIE,
                                     donnees=_saisie(f"Complète : {trouee}" + (f"\n(indice : {sens})" if sens else ""),
                                                     [a.strip() for a in attendu.split("|")], tolerant=True),
                                     **commun))
        elif t == DICTEE:
            if sens:
                commun["details"] = [f"Sens : « {sens} »"] + commun["details"]
            facettes.append(Exercice(id=exo_id, etiquette=f"Vocabulaire · dictée ({abr_l})",
                                     resume=f"{mot} (dictée)", forme=SAISIE,
                                     donnees={**_saisie(f"Écoute le mot et écris-le en {langue.lower()}.", reponses, tolerant=True),
                                              "indice": sens},
                                     **commun))
        elif t == PRONONCIATION:
            facettes.append(Exercice(id=exo_id, etiquette=f"Vocabulaire · prononciation ({abr_l})",
                                     resume=f"{mot} (à l'oral)", forme=CARTE,
                                     donnees={"recto": f"Dis à voix haute : **{mot}**" + (f"\n({sens})" if sens else ""),
                                              "verso": avec_prononciation, "oral": {"reponses": reponses}},
                                     **commun))
        elif t == DEFINITION:
            facettes.append(Exercice(id=exo_id, etiquette=f"Vocabulaire · définition ({abr_l} → {abr_l})",
                                     resume=f"{definition[:40]} → {mot}", forme=CARTE,
                                     donnees={"recto": f"« {definition} »", "verso": avec_prononciation}, **commun))
        elif t == DEFINITION_SAISIE:
            facettes.append(Exercice(id=exo_id, etiquette=f"Vocabulaire · définition → mot ({abr_l} → {abr_l})",
                                     resume=f"{mot} (d'après sa définition)", forme=SAISIE,
                                     donnees=_saisie(f"Quel mot correspond à cette définition ?\n« {definition} »\n"
                                                     f"Indice : {indice_lettres(base)}", reponses, tolerant=True),
                                     **commun))
        elif t == DEFINITION_CHOIX:
            choix, bon = _distracteurs(exo_id, mot, autres_mots)
            facettes.append(Exercice(id=exo_id, etiquette=f"Vocabulaire · QCM définition ({abr_l} → {abr_l})",
                                     resume=f"{mot} (d'après sa définition)", forme=CHOIX,
                                     donnees={"enonce": f"Quel mot correspond à cette définition ?\n« {definition} »",
                                              "choix": choix, "reponses": [bon], "multiple": False},
                                     **commun))
        elif t == CHOIX_TRADUCTION:
            choix, bon = _distracteurs(exo_id, trads[0], sens_autres, exclus=trads)
            facettes.append(Exercice(id=exo_id, etiquette=f"Vocabulaire · QCM ({abr_l} → {abr_m})",
                                     resume=f"{mot} → {trad}", forme=CHOIX,
                                     donnees={"enonce": f"Que veut dire « {avec_prononciation} » ?",
                                              "choix": choix, "reponses": [bon], "multiple": False},
                                     **commun))
        elif t == CONTEXTE_CHOIX:
            trouee, attendu = troue
            bonne = attendu.split("|")[0].strip()
            pieges = [sans_particule(m, particules) for m in autres_mots]
            choix, bon = _distracteurs(exo_id, bonne, pieges, exclus=[a.strip() for a in attendu.split("|")])
            facettes.append(Exercice(id=exo_id, etiquette="Vocabulaire · QCM en contexte",
                                     resume=f"{mot} (en contexte)", forme=CHOIX,
                                     donnees={"enonce": f"Quel mot complète la phrase ?\n{trouee}",
                                              "choix": choix, "reponses": [bon], "multiple": False},
                                     **commun))
        elif t == ECOUTE_CHOIX:
            bonne = trads[0] if trads else definition
            choix, bon = _distracteurs(exo_id, bonne, sens_autres, exclus=trads)
            facettes.append(Exercice(id=exo_id, etiquette=f"Vocabulaire · QCM à l'écoute ({abr_l})",
                                     resume=f"{mot} (à l'écoute)", forme=CHOIX,
                                     donnees={"enonce": "Écoute le mot et choisis son sens.", "choix": choix,
                                              "reponses": [bon], "multiple": False, "ecoute": True, "indice": mot},
                                     **commun))
    return facettes


def set_depuis_json(data, rel: PurePosixPath) -> SetLangue:
    """Construit un fichier de langue et ses facettes. Lève ValueError si malformé."""
    langue = _texte(data.get("langue"))
    if not langue.strip():
        raise ValueError(f"{rel} : 'langue' ne doit pas être vide.")
    maternelle = _texte(data.get("langue_maternelle")) or "Français"
    abr_l, abr_m = abreviation(langue), abreviation(maternelle)
    exercices: list[Exercice] = []

    # --- Vocabulaire ---
    types = types_vocabulaire(data, rel)
    particules = [p.lower() for p in _textes(data.get("particules_optionnelles"))]
    entrees = _section(data, "vocabulaire", rel)
    fiches = []
    for i, v in enumerate(entrees, start=1):
        contexte = f"{rel} » vocabulaire #{i}"
        o = _objet(v, contexte)
        mot = _requis(o, "mot", contexte)
        trads = _textes(o.get("traductions", o.get("traduction")))
        definition = _texte(o.get("definition")).strip()
        if not trads and not definition:
            raise ValueError(f"{contexte} : clé 'traductions' (ou 'definition') manquante ou vide.")
        formes = [mot, sans_particule(mot, particules), *_textes(o.get("variantes"))]
        fiches.append((i, o, mot, trads, {"mot": mot, "trad": trads[0] if trads else "",
                                           "definition": _masquer(definition, formes)}))
    for i, o, mot, trads, _ in fiches:
        id_ = _texte(o["id"]) if "id" in o else f"voc{i}"
        exercices += _facettes_vocabulaire(
            id_, mot, trads, o, types, langue, abr_l, abr_m,
            autres=[f for _, _, m, _, f in fiches if m != mot], particules=particules,
        )
    if entrees and not any(e.genre == VOCABULAIRE for e in exercices):
        raise ValueError(f"{rel} : aucune carte de vocabulaire possible avec les types choisis (les cartes "
                         "de traduction demandent des 'traductions', celles de définition une 'definition').")

    # --- Conjugaison ---
    for i, c in enumerate(_section(data, "conjugaison", rel), start=1):
        contexte = f"{rel} » conjugaison #{i}"
        o = _objet(c, contexte)
        verbe = _requis(o, "verbe", contexte)
        temps = _requis(o, "temps", contexte)
        formes = o.get("formes")
        if not isinstance(formes, dict) or not formes:
            raise ValueError(f"{contexte} : clé 'formes' (objet personne → forme) manquante.")
        id_ = _texte(o["id"]) if "id" in o else f"conj{i}"
        traduction = _texte(o.get("traduction"))
        explication = _texte(o.get("explication"))
        lignes = [(str(p), _textes(f)) for p, f in formes.items()]
        for p, f in lignes:
            if not f:
                raise ValueError(f"{contexte} : forme vide pour « {p} ».")
        titre_verbe = f"« {verbe} » ({traduction})" if traduction else f"« {verbe} »"
        tableau_lignes = [[p, f[0]] for p, f in lignes]
        for k, (personne, reponses) in enumerate(lignes):
            exercices.append(Exercice(
                id=f"{id_}#{personne}", entree=id_, genre=CONJUGAISON,
                etiquette=f"Conjugaison · {temps}",
                resume=f"{verbe} ({temps}) — {personne}",
                forme=SAISIE,
                donnees=_saisie(f"Conjugue {titre_verbe} — {temps}", reponses, invite=personne,
                                tableau={"titre": f"{verbe} — {temps}", "lignes": tableau_lignes, "surligne": k}),
                details=[explication] if explication else [],
                max_par_session=2,
            ))

    # --- Grammaire ---
    for i, g in enumerate(_section(data, "grammaire", rel), start=1):
        contexte = f"{rel} » grammaire #{i}"
        o = _objet(g, contexte)
        type_g = _texte(o.get("type", "QCM")).upper()
        id_ = _texte(o["id"]) if "id" in o else f"gram{i}"
        explication = _texte(o.get("explication"))
        traduction = _texte(o.get("traduction"))
        details = []
        if type_g == "ORDRE":
            phrase = _requis(o, "phrase", contexte)
            mots = mots_a_ordonner(phrase)
            if len(mots) < 2:
                raise ValueError(f"{contexte} : la phrase doit contenir au moins 2 mots.")
            consigne = (f"Remets les mots dans l'ordre : « {traduction} »" if traduction
                        else "Remets les mots dans l'ordre.")
            etiquette, resume, forme = "Grammaire · remise en ordre", phrase, ORDRE
            donnees = {"consigne": consigne, "mots": mots, "reponses": [phrase] + _textes(o.get("variantes"))}
        elif type_g == "CORRECTION":
            phrase = _requis(o, "phrase", contexte)
            corrections = _textes(o.get("corrections", o.get("correction")))
            if not corrections:
                raise ValueError(f"{contexte} : clé 'corrections' manquante ou vide.")
            consigne = f"Cette phrase contient une erreur. Corrige-la :\n« {phrase} »"
            if traduction:
                consigne += f"\n(sens : « {traduction} »)"
            etiquette, resume, forme = "Grammaire · correction d'erreur", phrase, SAISIE
            donnees = _saisie(consigne, corrections, prerempli=phrase)
        else:
            q = quiz.question_depuis_json(o, i, contexte)
            if traduction:
                details.append(f"Traduction : « {traduction} »")
            resume = q.enonce
            if q.type in quiz.TYPES_TEXTE:
                etiquette, forme = "Grammaire · phrase à trous", SAISIE
                consigne = f"{q.consigne}\n{q.enonce}" if q.consigne else q.enonce
                donnees = _saisie(consigne, q.reponses_texte)
            elif q.est_flashcard:
                etiquette, forme = "Grammaire · carte", CARTE
                donnees = {"recto": q.enonce, "verso": q.verso}
            else:
                etiquette = "Grammaire · QCR (plusieurs réponses)" if q.est_qcr else "Grammaire · QCM"
                forme = CHOIX
                donnees = {"enonce": q.enonce, "choix": q.choix, "reponses": q.reponses, "multiple": q.est_qcr}
        if explication:
            details.append(explication)
        exercices.append(Exercice(
            id=id_, entree=id_, genre=GRAMMAIRE, etiquette=etiquette,
            resume=resume[:70], forme=forme, donnees=donnees, details=details,
        ))

    if not exercices:
        raise ValueError(f"{rel} : aucun élément ('vocabulaire', 'conjugaison' ou 'grammaire').")
    vus = set()
    for e in exercices:
        if e.id in vus:
            raise ValueError(f"{rel} : identifiant en double « {e.id} ».")
        vus.add(e.id)

    quota = data.get("nouveaux_par_jour")
    return SetLangue(
        id=rel.with_suffix("").as_posix(),
        langue=langue,
        titre=_texte(data.get("titre")) or rel.stem,
        description=_texte(data.get("description")),
        exercices=exercices,
        particules=particules,
        caracteres_speciaux=_textes(data.get("caracteres_speciaux")),
        nouveaux_par_jour=quota if isinstance(quota, int) and not isinstance(quota, bool) and quota >= 0 else None,
        path=rel,
        types_vocabulaire=types if entrees else [],
        code_langue=_texte(data.get("code_langue")).strip() or code_langue(langue),
    )


def _saisie(consigne, reponses, *, tolerant=False, invite="", prerempli="", tableau=None):
    return {"consigne": consigne, "invite": invite, "reponses": reponses, "tolerant": tolerant,
            "prerempli": prerempli, "tableau": tableau}


# ===========================================================================
# Correction des réponses
# ===========================================================================
JUSTE, ACCENTS, FAUTE, FAUX = "juste", "accents", "faute", "faux"
MESSAGES = {
    JUSTE: "✓ Correct !",
    ACCENTS: "≈ Presque : attention aux accents",
    FAUTE: "≈ Presque : faute de frappe",
    FAUX: "✗ Incorrect",
}
_RANG = {JUSTE: 0, ACCENTS: 1, FAUTE: 2, FAUX: 3}


def note_du_verdict(verdict: str) -> int:
    """Juste → Bien ; presque (accent, faute de frappe) → Difficile ; faux → À revoir."""
    return {JUSTE: fsrs.BIEN, ACCENTS: fsrs.DIFFICILE, FAUTE: fsrs.DIFFICILE}.get(verdict, fsrs.A_REVOIR)


def normaliser_strict(s: str) -> str:
    """Casse, espaces, apostrophes typographiques et ponctuation finale ignorés ;
    accents conservés."""
    s = str(s).strip().lower()
    for c in "’‘`ʼ":
        s = s.replace(c, "'")
    for c in "“”«»":
        s = s.replace(c, '"')
    s = s.rstrip(".!?…;: \t\n\r")
    res = ""
    for mot in s.split():
        # Espace typographique avant « ? ! : ; » (usage français) ignoré.
        if res and not (len(mot) == 1 and mot in "?!:;,"):
            res += " "
        res += mot
    return res


def _sans_particule(s: str, particules) -> str:
    tete, _, reste = s.partition(" ")
    return reste if reste and tete in particules else s


def distance(a: str, b: str) -> int:
    """Distance d'édition (Levenshtein avec transpositions adjacentes)."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cout = a[i - 1] != b[j - 1]
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cout)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def noter_saisie(saisie: str, reponses, tolerant: bool, particules=()):
    """Note une réponse tapée. Renvoie (verdict, réponse attendue la plus proche)."""
    s = _sans_particule(normaliser_strict(saisie), particules)
    s_plat = quiz.normaliser_texte(s)
    meilleur = (FAUX, float("inf"), reponses[0] if reponses else "")
    for r in reponses:
        a = _sans_particule(normaliser_strict(r), particules)
        if a == s:
            return JUSTE, r
        a_plat = quiz.normaliser_texte(a)
        dist = distance(s_plat, a_plat)
        if not tolerant:
            verdict = FAUX
        elif dist == 0:
            verdict = ACCENTS
        else:
            n = len(a_plat)
            seuil = 2 if n >= 10 else 1 if n >= 5 else 0
            verdict = FAUTE if dist <= seuil else FAUX
        if (_RANG[verdict], dist) < (_RANG[meilleur[0]], meilleur[1]):
            meilleur = (verdict, dist, r)
    return meilleur[0], meilleur[2]


def diff(saisie: str, attendu: str):
    """Caractères de `attendu`, chacun marqué True s'il correspond à la saisie (plus
    longue sous-séquence commune, casse ignorée) : sert à surligner les différences."""
    a, s = attendu, saisie
    eq = lambda x, y: x.lower() == y.lower()  # noqa: E731
    lcs = [[0] * (len(s) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) - 1, -1, -1):
        for j in range(len(s) - 1, -1, -1):
            lcs[i][j] = lcs[i + 1][j + 1] + 1 if eq(a[i], s[j]) else max(lcs[i + 1][j], lcs[i][j + 1])
    i = j = 0
    res = []
    while i < len(a):
        if j < len(s) and eq(a[i], s[j]):
            res.append((a[i], True))
            i += 1
            j += 1
        elif j < len(s) and lcs[i][j + 1] >= lcs[i + 1][j]:
            j += 1
        else:
            res.append((a[i], a[i].isspace()))
            i += 1
    return res


def corriger(set_langue: SetLangue, exo: Exercice, reponse):
    """Corrige une réponse (texte tapé, indices choisis, ou mots dans l'ordre).

    Renvoie {verdict, message, note, attendu, diff, autres} ou None si la réponse
    est incomplète (champ vide, aucun choix, mots non tous placés).
    """
    d = exo.donnees
    if exo.forme == SAISIE:
        saisie = str(reponse or "").strip()
        if not saisie:
            return None
        verdict, attendu = noter_saisie(saisie, d["reponses"], d["tolerant"], set_langue.particules)
    elif exo.forme == CHOIX:
        if not isinstance(reponse, list) or not reponse:
            return None
        verdict, attendu, saisie = (JUSTE if sorted(set(reponse)) == d["reponses"] else FAUX), "", ""
    elif exo.forme == ORDRE:
        if (not isinstance(reponse, list) or sorted(reponse) != list(range(len(d["mots"])))):
            return None
        saisie = " ".join(d["mots"][k] for k in reponse)
        verdict, attendu = noter_saisie(saisie, d["reponses"], False)
    else:
        return None
    reponses = d.get("reponses", [])
    return {
        "verdict": verdict,
        "message": MESSAGES[verdict],
        "note": note_du_verdict(verdict),
        "saisie": saisie,
        "attendu": attendu,
        "diff": diff(saisie, attendu) if exo.forme == SAISIE and verdict != JUSTE else [],
        "autres": [r for r in reponses if r != attendu] if exo.forme == SAISIE else [],
    }


# ===========================================================================
# Suivi FSRS
# ===========================================================================
def _jours_depuis(st, now) -> float:
    return 0.0 if st.last_review is None else max(0.0, (now - st.last_review).total_seconds()) / 86400


def est_nouveau(st) -> bool:
    return st is None or st.stabilite is None


def est_du(st, now) -> bool:
    return st.echeance is None or st.echeance <= now


def retention(st, now) -> float:
    return 0.0 if st.stabilite is None else fsrs.retrievabilite(_jours_depuis(st, now), st.stabilite)


def etat_suivant(st, note: int, now) -> fsrs.Etat:
    if st is not None and st.stabilite is not None and st.difficulte is not None:
        return fsrs.suivante(fsrs.Etat(st.stabilite, st.difficulte), _jours_depuis(st, now), note)
    return fsrs.premiere(note)


def delai(etat: fsrs.Etat, note: int, retention=None) -> timedelta:
    """Délai avant la prochaine révision : un élément oublié revient dès la
    prochaine session ; sinon l'intervalle FSRS arrondi (au moins un jour), pour
    la rétention visée (`retention`, 90 % par défaut)."""
    if note == fsrs.A_REVOIR:
        return timedelta(minutes=10)
    jours = int(fsrs.intervalle_jours(etat.stabilite, retention or fsrs.RETENTION) + 0.5)
    return timedelta(days=min(36_500, max(1, jours)))


def texte_delai(d: timedelta) -> str:
    """Texte court d'un délai : « 10 min », « 3 j », « 2 mois », « 1,5 an »."""
    jours = d.days
    if jours < 1:
        return f"{int(d.total_seconds() // 60)} min"
    if jours < 31:
        return f"{jours} j"
    if jours < 365:
        return f"{int(jours / 30.4 + 0.5)} mois"
    return f"{jours / 365:.1f} an".replace(".", ",")


def previsions(st, now) -> dict:
    """Délai prévu pour chaque note donnée maintenant (affiché sur les boutons)."""
    return {n: texte_delai(delai(etat_suivant(st, n, now), n)) for n in fsrs.NOTES}


# ===========================================================================
# Sessions
# ===========================================================================
@dataclass
class ExoSession:
    set: SetLangue
    exo: Exercice
    nouveau: bool
    melange: list[int] = field(default_factory=list)  # ordre de présentation des mots (ORDRE)


def debloque(prog, set_langue, exo) -> bool:
    if exo.prerequis is None:
        return True
    st = prog.get((set_langue.id, exo.prerequis))
    return st is not None and st.stabilite is not None and st.last_correct is True


def quota_nouveaux(sets, prog, now) -> int:
    """Nouveaux éléments qu'il reste possible de découvrir aujourd'hui."""
    aujourdhui = quiz.jour(now)
    quotas = [s.nouveaux_par_jour for s in sets if s.nouveaux_par_jour is not None]
    quota = max(quotas) if quotas else NOUVEAUX_PAR_JOUR
    decouverts = 0
    for s in sets:
        for e in s.exercices:
            st = prog.get((s.id, e.id))
            if st is not None and st.decouverte is not None and quiz.jour(st.decouverte) == aujourdhui:
                decouverts += 1
    return max(0, quota - decouverts)


def melanger_mots(n: int, hasard=random) -> list[int]:
    """Permutation aléatoire, différente de l'ordre d'origine."""
    m = list(range(n))
    for _ in range(10):
        hasard.shuffle(m)
        if m != sorted(m):
            break
    return m


def _files_par_genre(items):
    files = {g: deque() for g in GENRES}
    for e in items:
        files[e.exo.genre].append(e)
    return files


def _repartir_equitablement(items):
    """Nouveautés prises à tour de rôle dans chaque genre (parts égales)."""
    files = _files_par_genre(items)
    res = []
    while any(files.values()):
        for f in files.values():
            if f:
                res.append(f.popleft())
    return res


def entrelacer(items):
    """Entrelace les genres : chaque genre est réparti uniformément sur la session
    (on puise dans la file la moins avancée proportionnellement), et jamais deux
    exercices du même genre à la suite tant qu'un autre genre est disponible."""
    files = _files_par_genre(items)
    tailles = {g: max(1, len(f)) for g, f in files.items()}
    res = []
    dernier = None
    while True:
        ordre = sorted((g for g in GENRES if files[g]), key=lambda g: -len(files[g]) / tailles[g])
        if not ordre:
            return res
        g = next((x for x in ordre if x != dernier), ordre[0])
        file = files[g]
        # Évite deux formes du même verbe à la suite.
        if res and len(file) > 1 and file[0].exo.entree == res[-1].exo.entree:
            file[0], file[1] = file[1], file[0]
        res.append(file.popleft())
        dernier = g


def session_intensive(sets, prog, now, hasard=random, limite=TAILLE_MAX_INTENSIF):
    """Révision intensive : tous les exercices de la sélection, sans échéance, ni
    quota de nouveautés, ni limite de facettes par élément.

    Les facettes d'un même élément se suivent dans l'ordre (la carte qui fait
    découvrir le mot d'abord), par tours : la 1re facette de chaque élément, puis
    la 2e, etc., chaque tour mélangé. Au-delà de `limite` exercices, les éléments
    les plus fragiles passent d'abord (oubliés, ratés, peu retenus, puis nouveaux).
    """
    par_entree = {}
    for s in sets:
        for exo in s.exercices:
            par_entree.setdefault((s.id, exo.entree), []).append(ExoSession(s, exo, est_nouveau(prog.get((s.id, exo.id)))))

    def fragilite(items):
        stats = [prog.get((i.set.id, i.exo.id)) for i in items]
        vus = [st for st in stats if st is not None and st.stabilite is not None]
        if not vus:
            return (1, 0, 1.0)  # jamais vu : après les éléments déjà travaillés
        oublis = sum(st.oublis or 0 for st in vus) + sum(st.last_correct is False for st in vus)
        return (0, -oublis, min(retention(st, now) for st in vus))

    entrees = list(par_entree.values())
    hasard.shuffle(entrees)
    entrees.sort(key=fragilite)
    retenues, total = [], 0
    for items in entrees:
        if total and total + len(items) > limite:
            continue
        # La facette sans prérequis (découverte) d'abord, puis les autres dans l'ordre du fichier.
        retenues.append(sorted(items, key=lambda i: i.exo.prerequis is not None))
        total += len(items)

    session = []
    for rang in range(max((len(items) for items in retenues), default=0)):
        tour = [items[rang] for items in retenues if rang < len(items)]
        hasard.shuffle(tour)
        session += tour
    for e in session:
        if e.exo.forme == ORDRE:
            e.melange = melanger_mots(len(e.exo.donnees["mots"]), hasard)
    return session


def construire_session(sets, prog, mode, n, now, hasard=random):
    """Construit une session d'au plus `n` exercices (révision intensive : voir
    session_intensive, qui prend tout).

    1. révisions dues (échéance passée), les plus menacées d'oubli d'abord ;
    2. nouveautés débloquées, réparties à parts égales entre les genres, dans la
       limite du quota du jour ;
    3. au plus `max_par_session` facettes par élément ;
    4. entrelacement des genres.
    En mode « points faibles » : éléments souvent oubliés ou ratés la dernière fois.
    """
    if mode == INTENSIF:
        return session_intensive(sets, prog, now, hasard)
    revues, nouveaux, faibles = [], [], []
    for s in sets:
        for exo in s.exercices:
            if mode in GENRES and exo.genre != mode:
                continue
            st = prog.get((s.id, exo.id))
            item = ExoSession(s, exo, est_nouveau(st))
            if item.nouveau:
                if debloque(prog, s, exo):
                    nouveaux.append(item)
                continue
            r = retention(st, now)
            if mode == FAIBLES:
                if st.oublis > 0 or st.last_correct is False:
                    faibles.append(((-st.oublis, r), item))
            elif est_du(st, now):
                revues.append((r, item))

    if mode == FAIBLES:
        faibles.sort(key=lambda t: t[0])
        candidats = [e for _, e in faibles]
        quota = 0
    else:
        revues.sort(key=lambda t: t[0])
        # Les révisions passent avant la nouveauté, mais une session garde une part
        # de nouveaux éléments (un tiers) quand il y en a.
        quota = quota_nouveaux(sets, prog, now)
        reserve = min(len(nouveaux), max(n // 3, 3), quota)
        nb_revues = min(len(revues), max(0, n - reserve))
        candidats = [e for _, e in revues[:nb_revues]] + _repartir_equitablement(nouveaux)

    # Limite de facettes par entrée (pas « réussir » la production juste après
    # avoir vu la carte du même mot) et quota de nouveautés.
    nb_nouveaux = 0
    par_entree = {}
    choisis = []
    for e in candidats:
        if len(choisis) >= n:
            break
        if e.nouveau and nb_nouveaux >= quota:
            continue
        cle = (e.set.id, e.exo.entree)
        if par_entree.get(cle, 0) < e.exo.max_par_session:
            par_entree[cle] = par_entree.get(cle, 0) + 1
            nb_nouveaux += e.nouveau
            choisis.append(e)

    session = entrelacer(choisis)
    for e in session:
        if e.exo.forme == ORDRE:
            e.melange = melanger_mots(len(e.exo.donnees["mots"]), hasard)
    return session


# ===========================================================================
# Statistiques
# ===========================================================================
def resume(sets, prog, now) -> dict:
    """Par genre : total, nouveaux, à revoir, maîtrisés, rétention estimée ; et
    révisions des 7 prochains jours, éléments les plus souvent oubliés."""
    dans_7j = now + timedelta(days=7)
    genres = {g: {"nom": NOMS_GENRES[g], "total": 0, "nouveaux": 0, "a_revoir": 0, "maitrises": 0,
                  "retention": None, "_somme": 0.0, "_n": 0} for g in GENRES}
    r = {"a_revoir": 0, "nouveaux_disponibles": 0, "sept_jours": 0, "difficiles": []}
    for s in sets:
        for exo in s.exercices:
            rg = genres[exo.genre]
            rg["total"] += 1
            st = prog.get((s.id, exo.id))
            if est_nouveau(st):
                rg["nouveaux"] += 1
                if debloque(prog, s, exo):
                    r["nouveaux_disponibles"] += 1
                continue
            if est_du(st, now):
                rg["a_revoir"] += 1
                r["a_revoir"] += 1
            elif st.echeance <= dans_7j:
                r["sept_jours"] += 1
            if st.stabilite >= MAITRISE_JOURS:
                rg["maitrises"] += 1
            rg["_somme"] += retention(st, now)
            rg["_n"] += 1
            if st.oublis >= 2:
                r["difficiles"].append((exo.resume, st.oublis))
    for rg in genres.values():
        if rg["_n"]:
            rg["retention"] = rg["_somme"] / rg["_n"] * 100
        rg["appris"] = rg["total"] - rg["nouveaux"]
        del rg["_somme"], rg["_n"]
    r["genres"] = [rg for rg in genres.values() if rg["total"]]
    r["nouveaux_disponibles"] = min(r["nouveaux_disponibles"], quota_nouveaux(sets, prog, now))
    r["difficiles"] = sorted(r["difficiles"], key=lambda d: -d[1])[:8]
    return r
