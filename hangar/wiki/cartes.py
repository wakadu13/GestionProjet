"""Cartes de révision tirées d'une page du wiki, sans Claude.

- Chaque encadré (callout) à titre d'un type « à apprendre » (définition,
  théorème, méthode, formule…) devient une carte recto/verso : le recto pose
  « Définition : Échantillonnage », le verso est le contenu de l'encadré.
- Chaque surlignage ``==texte==`` devient un texte à trous : la phrase avec le
  surlignage remplacé par _____, la réponse attendue étant le texte surligné.

Les identifiants des questions dérivent de leur contenu : régénérer les cartes
d'une page modifiée garde la progression des cartes inchangées. Comme quiz.py,
ce module ne dépend ni des modèles ni des vues.
"""

import hashlib
import re

from .render import CALLOUT_ALIASES, CALLOUT_RE, FENCE_LINE_RE

TYPES_CARTES = {
    "definition": "Définition", "theoreme": "Théorème", "lemme": "Lemme", "corollaire": "Corollaire",
    "propriete": "Propriété", "proposition": "Proposition", "methode": "Méthode", "formule": "Formule",
    "loi": "Loi", "regle": "Règle", "principe": "Principe", "retenir": "À retenir",
}
SURLIGNAGE_RE = re.compile(r"==([^=\n]+?)==")
LISTE_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
TROU = "_____"
MAX_CARTES = 300


def _identifiant(*parties):
    return "c" + hashlib.sha1("\x1f".join(parties).encode("utf-8")).hexdigest()[:10]


def _sans_frontmatter(lines):
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return lines[i + 1:]
    return lines


def _trous(ligne):
    """Textes à trous d'une ligne : un par surlignage (les autres restent lisibles)."""
    texte = LISTE_RE.sub("", ligne.strip())
    marques = SURLIGNAGE_RE.findall(texte)
    if not marques or len(texte) > 400:
        return []
    questions = []
    for k, mot in enumerate(marques):
        mot = mot.strip()
        if not mot or len(mot) > 80:
            continue
        compteur = iter(range(len(marques)))
        enonce = SURLIGNAGE_RE.sub(lambda m: TROU if next(compteur) == k else m.group(1), texte)
        questions.append({"id": _identifiant("trou", enonce, mot), "type": "TROU", "question": enonce,
                          "reponses_texte": [mot]})
    return questions


def extraire(texte):
    """Questions (au format JSON des sets) tirées du Markdown d'une page."""
    lines = _sans_frontmatter(texte.split("\n"))
    questions, fence, i = [], None, 0
    while i < len(lines):
        line = lines[i]
        fence_match = FENCE_LINE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)[0]
            fence = marker if fence is None else (None if marker == fence else fence)
            i += 1
            continue
        if fence:
            i += 1
            continue
        m = CALLOUT_RE.match(line)
        if m is None:
            if not line.lstrip().startswith("#"):
                questions += _trous(line)
            i += 1
            continue
        i += 1
        prefix = re.compile(rf"^{re.escape(m.group('indent'))}>[ \t]?")
        corps = []
        while i < len(lines) and prefix.match(lines[i]):
            corps.append(prefix.sub("", lines[i], count=1))
            i += 1
        kind = m.group("type").lower()
        kind = CALLOUT_ALIASES.get(kind, kind)
        titre = SURLIGNAGE_RE.sub(r"\1", m.group("title")).strip()
        verso = "\n".join(corps).strip()
        if kind in TYPES_CARTES and titre and verso:
            recto = f"{TYPES_CARTES[kind]} : {titre}"
            questions.append({"id": _identifiant("carte", recto, verso), "type": "FLASHCARD", "question": recto,
                              "verso": SURLIGNAGE_RE.sub(r"**\1**", verso)})
        for ligne in corps:
            questions += _trous(ligne)
    vus, uniques = set(), []
    for q in questions:
        if q["id"] not in vus:
            vus.add(q["id"])
            uniques.append(q)
    return uniques[:MAX_CARTES]


def set_de_la_page(note, questions):
    """JSON du set de cartes d'une page (clé `source_page` : la page le liste dans
    « Réviser ce cours »)."""
    return {
        "titre": f"{note.title} — cartes",
        "description": "Cartes tirées des encadrés (définitions, théorèmes, méthodes…) et des surlignages de la page.",
        "source_page": note.key,
        "genere_par": "Cartes de la page",
        "questions": questions,
    }


def chemin_du_set(note, dossier_qcm="QCM"):
    """Chemin du set dans le coffre : QCM/<dossier de la page ou Pages>/<page> (cartes).json."""
    dossier = note.path.parent.name or "Pages"
    return f"{dossier_qcm}/{dossier}/{note.path.stem} (cartes).json"
