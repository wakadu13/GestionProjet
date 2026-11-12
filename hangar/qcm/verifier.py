"""Contrôle des fichiers de questions (par exemple générés par une IA) avant de
les utiliser : `uv run hangar qcm-verifier <fichier.json | dossier> …`.

Porté depuis l'application « revision_qcm » (src/cli/verifier.rs). Chaque
fichier est chargé comme le fait le site (set de questions ou fichier de
langue) : une erreur bloquante donne le message à recoller à l'IA, et des
avertissements signalent ce qui est accepté mais probablement involontaire.
"""

from collections import Counter
from pathlib import Path, PurePosixPath

from . import langues, quiz


def _latex(ou, texte, acc):
    """Formule « $ » non fermée (nombre impair de $ non échappés)."""
    n = len(texte.replace("\\$", "").split("$")) - 1
    if n % 2:
        acc.append(f"{ou} : formule « $ » non fermée")


def verifier_set(s: quiz.QuestionSet):
    avert = []
    compte = Counter(q.type for q in s.questions)
    toutes_justes = []
    for q in s.questions:
        ou = f"question {q.id}"
        for t in [q.enonce, *q.choix, q.verso, q.explication]:
            _latex(ou, t, avert)
        if q.est_trou and "___" not in q.enonce:
            avert.append(f"{ou} : pas de « ___ » dans la phrase à trous")
        if q.est_qcr and len(q.reponses) == len(q.choix):
            toutes_justes.append(q.id)
    if toutes_justes:
        avert.append("QCR dont toutes les propositions sont justes (à éviter si c'est fréquent : "
                     "la réponse se devine) : " + ", ".join(toutes_justes))
    ids = Counter(q.id for q in s.questions)
    for id_ in sorted(i for i, n in ids.items() if n > 1):
        avert.append(f"identifiant « {id_} » utilisé plusieurs fois (progression partagée entre ces questions)")
    detail = ", ".join(f"{t} {compte[t]}" for t in ("QCM", "QCR", "TROU", "QRC", "FLASHCARD") if compte[t])
    return f"set « {s.titre} » : {len(s)} questions ({detail}) — catégorie « {s.categorie} »", avert


def verifier_langue(s: langues.SetLangue):
    avert = []
    traductions = {}
    entrees = {g: set() for g in langues.GENRES}
    ids = {e.id for e in s.exercices}
    for e in s.exercices:
        entrees[e.genre].add(e.entree)
        ou = f"{e.genre} {e.entree}"
        d = e.donnees
        if e.forme == langues.CARTE:
            _latex(ou, d["recto"], avert)
            _latex(ou, d["verso"], avert)
            if e.genre == langues.VOCABULAIRE:
                traductions.setdefault(d["verso"], []).append(e.entree)
                if f"{e.entree}#ctx" not in ids:
                    avert.append(f"{ou} : pas d'exemple avec le mot entre [crochets] (pas d'exercice en contexte)")
        elif e.forme == langues.SAISIE:
            _latex(ou, d["consigne"], avert)
        elif e.forme == langues.CHOIX:
            for t in [d["enonce"], *d["choix"]]:
                _latex(ou, t, avert)
        elif e.forme == langues.ORDRE and len(d["mots"]) > 9:
            avert.append(f"{ou} : {len(d['mots'])} mots à ordonner (9 au plus conseillé, raccourcis clavier limités à 9)")
    for t, mots in sorted(traductions.items()):
        if len(mots) > 1:
            avert.append(f"traduction « {t} » commune à {', '.join(mots)} : l'exercice de production sera ambigu")
    resume = (f"fichier de langue « {s.titre} » ({s.langue}) : {len(entrees[langues.VOCABULAIRE])} mots, "
              f"{len(entrees[langues.CONJUGAISON])} tableaux de conjugaison, "
              f"{len(entrees[langues.GRAMMAIRE])} exercices de grammaire → {len(s)} exercices")
    return resume, avert


def verifier_donnees(data, rel: PurePosixPath):
    """(résumé, avertissements) pour des données JSON ; ValueError si invalide."""
    if langues.est_fichier_langue(data):
        return verifier_langue(langues.set_depuis_json(data, rel))
    return verifier_set(quiz.set_depuis_json(data, rel))


def verifier_fichier(chemin: Path, racine: Path):
    """(valide ?, résumé ou message d'erreur, avertissements)."""
    try:
        rel, data = quiz.lire_json(chemin, racine)
    except (ValueError, OSError) as e:
        return False, str(e), []
    try:
        resume, avert = verifier_donnees(data, rel)
    except Exception as e:  # noqa: BLE001 — valeur de type imprévu : on le dit et on passe au suivant
        return False, quiz.erreur_chargement(rel, e), []
    return True, resume, avert
