"""Appels à l'IA à la demande, courts et synchrones (réponse dans la requête) : explication d'une question de
QCM, correction d'une question signalée, réponse rédigée jugée, tuteur d'une page, « Demander au wiki », copie
photographiée, « Améliorer » de l'éditeur.

Port de ``wiki/claude_demandes.py`` de Revision : mêmes consignes, mêmes schémas JSON, mêmes limites ; l'appel
passe par ``hangar.ai.generate`` (Claude ou modèle local compatible OpenAI). Rôles : « rapide » pour ce que
Revision confiait à Haiku (explications, QRC, tuteur, wiki), « vision » pour les copies (Sonnet chez Revision),
« chat » (modèle principal) pour « Améliorer » et la correction des questions signalées.
Chaque fonction renvoie aussi l'usage (jetons entrée, sortie) et le modèle, comme dans Revision ; l'usage est déjà
enregistré par generate() (feature = nom de la fonction).
"""
from __future__ import annotations

import json

from .core import generate
from .providers import AIError

MAX_CHARS_COURS = 80_000   # longueur de cours envoyée au modèle (explications, corrections, tuteur, variantes)
MAX_CHARS_AMELIORER = 12_000
MAX_CHARS_QUESTION = 2000
MAX_CHARS_REPONSE = 2000
MAX_TOKENS_QRC = 600
MAX_TOKENS_TUTEUR = 1500
MAX_TOKENS_COPIE = 6000
HISTORIQUE_TUTEUR = 6       # derniers échanges rejoués

Echec = AIError  # nom de Revision


def _usage(r):
    return (r.usage.input_tokens + r.usage.cache_read_tokens + r.usage.cache_write_tokens, r.usage.output_tokens)


def _messages(contenu, historique=()):
    """Échanges précédents [(question, réponse)] puis le contenu demandé."""
    msgs = []
    for question, reponse in historique:
        msgs.append({"role": "user", "content": question})
        msgs.append({"role": "assistant", "content": reponse})
    msgs.append({"role": "user", "content": contenu})
    return msgs


def sans_cadre(texte: str) -> str:
    """Retire un bloc ```markdown … ``` mis autour de toute la réponse."""
    lignes = (texte or "").strip().splitlines()
    if len(lignes) >= 2 and lignes[0].strip().lower() in ("```", "```markdown", "```md") and lignes[-1].strip() == "```":
        return "\n".join(lignes[1:-1]).strip()
    return (texte or "").strip()


def cours_du_set(qset):
    """(note, texte du cours) de la page dont le set a été généré, ou (None, "")."""
    if not getattr(qset, "source_page", ""):
        return None, ""
    try:
        from ..wiki.vault import get_vault
        vault = get_vault()
        note = vault.notes.get(qset.source_page.lower())
        if note is None:
            return None, ""
        return note, (vault.root / note.path).read_text(encoding="utf-8")[:MAX_CHARS_COURS]
    except Exception:  # noqa: BLE001 - wiki indisponible
        return None, ""


CHAMPS_QUESTION = ("type", "question", "choix", "reponses", "reponses_texte", "consigne", "verso", "explication")


def question_en_json(question) -> dict:
    """Une question du set sous la forme du JSON des fichiers de QCM."""
    return {"type": question.type, "question": question.enonce, "choix": question.choix, "reponses": question.reponses,
            "reponses_texte": question.reponses_texte, "consigne": question.consigne, "verso": question.verso,
            "explication": question.explication}


# -- « Explique-moi » (question de QCM) et « Améliorer la rédaction » (éditeur) --------------------
CONSIGNE_EXPLICATION = """Tu expliques à un étudiant la réponse d'une question de révision, en français, en 4 à 8 phrases.
- Dis pourquoi la bonne réponse est juste et, si des choix étaient proposés, pourquoi les autres sont faux.
- Appuie-toi sur le cours fourni s'il y en a un ; sinon dis que tu expliques d'après tes connaissances générales.
- Markdown simple (gras, listes courtes, formules en $...$). Pas de titre, pas de préambule."""

CONSIGNE_AMELIORER = """Tu améliores la rédaction d'un extrait de page de wiki (notes de cours) en Markdown.
- Corrige l'orthographe, la grammaire et la ponctuation ; rends les phrases claires et fluides ; garde le sens, \
le niveau de détail, l'ordre et la langue d'origine. N'ajoute aucune information.
- Conserve tel quel tout ce qui est du balisage : titres #, listes, tableaux, blocs de code, formules LaTeX \
($...$, $$...$$), liens [[...]] et ![[...]], encadrés > [!…], propriétés --- en tête.
Réponds uniquement par le texte corrigé, sans commentaire ni bloc de code autour."""


def expliquer(qset, question, user=None):
    """Explication d'une question (rôle « rapide »), d'après la page du cours quand le set en vient.
    Renvoie (texte Markdown, (jetons entrée, sortie), modèle)."""
    note, contexte = cours_du_set(qset)
    lignes = [f"Question ({question.type}) : {question.enonce}"]
    if question.consigne:
        lignes.append(f"Consigne : {question.consigne}")
    if question.choix:
        lignes.append("Choix : " + " ; ".join(f"{chr(65 + i)}. {c}" for i, c in enumerate(question.choix)))
        lignes.append("Bonne(s) réponse(s) : " + ", ".join(chr(65 + i) for i in question.reponses))
    if question.reponses_texte:
        lignes.append("Réponse attendue : " + " / ".join(question.reponses_texte))
    if question.verso:
        lignes.append(f"Réponse : {question.verso}")
    if question.explication:
        lignes.append(f"Explication déjà donnée (à développer) : {question.explication}")
    contenu = []
    if contexte:
        contenu.append({"type": "text", "text": f"Cours « {note.title} » :\n\n{contexte}"})
    contenu.append({"type": "text", "text": "\n".join(lignes) + ("\n\nAucun cours fourni." if not contexte else "")})
    r = generate(_messages(contenu), system=CONSIGNE_EXPLICATION, role="rapide", max_tokens=1500, effort="low",
                 user=user, feature="expliquer")
    if not r.text:
        raise Echec("L’IA n’a renvoyé aucune explication.")
    return r.text, _usage(r), r.model


def ameliorer(texte, user=None):
    """Texte Markdown relu et corrigé (modèle principal). Renvoie (texte, usage)."""
    if len(texte) > MAX_CHARS_AMELIORER:
        raise Echec(f"Sélectionne un passage plus court ({MAX_CHARS_AMELIORER} caractères au plus).")
    r = generate(_messages([{"type": "text", "text": texte}]), system=CONSIGNE_AMELIORER, role="chat", max_tokens=16000,
                 effort="low", user=user, feature="ameliorer")
    resultat = sans_cadre(r.text)
    if not resultat:
        raise Echec("L’IA n’a renvoyé aucun texte.")
    if r.truncated:
        raise Echec("Passage trop long pour une seule relecture : sélectionne-en une partie.")
    return resultat, _usage(r)


# -- Correction d'une question signalée ----------------------------------------------------------
CONSIGNE_CORRECTION = """Tu corriges une question de révision (QCM) qu'un étudiant a signalée : erreur, ambiguïté, \
coquille ou réponse discutable. Appuie-toi sur le cours fourni quand il y en a un. Renvoie la question corrigée \
complète, du même type si possible, avec une bonne réponse sans ambiguïté et une explication courte ; et dans \
« modification », en une ou deux phrases, ce que tu as changé et pourquoi. Si le signalement est infondé, renvoie \
la question inchangée et explique en quoi elle est juste. Les formules s'écrivent en LaTeX entre $."""

_TEXTES = {"type": "array", "items": {"type": "string"}}
QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["QCM", "QCR", "TROU", "QRC", "FLASHCARD"]},
        "question": {"type": "string"},
        "choix": _TEXTES,
        "reponses": {"type": "array", "items": {"type": "integer"}},
        "reponses_texte": _TEXTES,
        "consigne": {"type": "string"},
        "verso": {"type": "string"},
        "explication": {"type": "string"},
    },
    "required": ["type", "question", "choix", "reponses", "reponses_texte", "consigne", "verso", "explication"],
    "additionalProperties": False,
}
SCHEMA_CORRECTION = {
    "type": "object",
    "properties": {**QUESTION_SCHEMA["properties"], "modification": {"type": "string"}},
    "required": [*QUESTION_SCHEMA["required"], "modification"],
    "additionalProperties": False,
}


def corriger_question(qset, question, commentaires, user=None):
    """Correction d'une question signalée, proposée d'après le cours quand le set en vient.
    Renvoie (question JSON corrigée, note de modification, usage, modèle) ; la question est validée comme
    à l'import d'un set (lève Echec sinon). Appelée par la page des signalements des QCM."""
    from ..qcm import quiz

    note, contexte = cours_du_set(qset)
    contenu = []
    if contexte:
        contenu.append({"type": "text", "text": f"Cours « {note.title} » :\n\n{contexte}"})
    texte = (f"Question signalée (set « {qset.titre} ») :\n"
             f"{json.dumps(question_en_json(question), ensure_ascii=False, indent=1)}")
    commentaires = [c for c in (commentaires or []) if c]
    texte += ("\n\nSignalement(s) :\n" + "\n".join(f"- {c}" for c in commentaires)) if commentaires \
        else "\n\nAucun commentaire : vérifie la question."
    if not contexte:
        texte += "\n\nAucun cours fourni."
    contenu.append({"type": "text", "text": texte})
    r = generate(_messages(contenu), system=CONSIGNE_CORRECTION, role="chat", json_schema=SCHEMA_CORRECTION,
                 max_tokens=4000, effort="medium", user=user, feature="correction")
    data = dict(r.data or {})
    modification = str(data.pop("modification", "")).strip()
    nouvelle = {k: v for k, v in data.items() if k in CHAMPS_QUESTION and v not in ("", [], None)}
    try:
        quiz.question_depuis_json({**nouvelle, "id": question.id}, 0, "correction")
    except ValueError as e:
        raise Echec(f"Correction invalide : {e}") from e
    return nouvelle, modification, _usage(r), r.model


# -- Réponses rédigées (QRC) ---------------------------------------------------------------------
CONSIGNE_QRC = """Tu corriges la réponse rédigée d'un étudiant à une question de révision, en français. Compare le fond \
avec la ou les réponses attendues : une formulation différente, plus précise ou plus complète est juste. Verdict \
« juste » si l'essentiel y est sans erreur, « partiel » s'il manque un élément important ou s'il y a une imprécision, \
« faux » sinon (hors sujet, contresens, réponse vide de sens). « retour » : une à trois phrases qui disent ce qui est \
bon, ce qui manque ou ce qui est faux, sans répéter la question ; tutoie l'étudiant."""

SCHEMA_QRC = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": ["juste", "partiel", "faux"]}, "retour": {"type": "string"}},
    "required": ["verdict", "retour"],
    "additionalProperties": False,
}


def corriger_qrc(qset, question, reponse, user=None):
    """Verdict (rôle « rapide ») sur une réponse rédigée : (verdict, retour, usage, modèle)."""
    lignes = [f"Question : {question.enonce}"]
    if question.consigne:
        lignes.append(f"Consigne : {question.consigne}")
    lignes.append("Réponse(s) attendue(s) : " + " / ".join(question.reponses_texte))
    if question.explication:
        lignes.append(f"Explication du corrigé : {question.explication}")
    lignes.append(f"Réponse de l'étudiant : {reponse[:MAX_CHARS_REPONSE]}")
    r = generate(_messages([{"type": "text", "text": "\n".join(lignes)}]), system=CONSIGNE_QRC, role="rapide",
                 json_schema=SCHEMA_QRC, max_tokens=MAX_TOKENS_QRC, effort="low", user=user, feature="qrc")
    data = r.data or {}
    verdict = data.get("verdict") if data.get("verdict") in ("juste", "partiel", "faux") else "faux"
    return verdict, str(data.get("retour", "")).strip(), _usage(r), r.model


# -- Tuteur : questions sur la page ouverte -------------------------------------------------------
CONSIGNE_TUTEUR = """Tu es le tuteur d'un étudiant qui révise la page de cours fournie. Réponds en français, en 3 à 10 \
phrases, en Markdown léger (gras pour les mots clés, listes courtes, formules en LaTeX entre $). Appuie-toi d'abord sur \
la page : cite la section ou la définition utile. Si la réponse n'est pas dans la page, dis-le clairement, puis réponds \
avec prudence d'après tes connaissances. Si l'étudiant propose un raisonnement, dis ce qui est juste et ce qui ne l'est \
pas. Tutoie l'étudiant ; pas de formule de politesse."""


def tuteur(note, page_texte, question, historique=(), user=None):
    """Réponse du tuteur (rôle « rapide ») à une question sur une page : (texte Markdown, usage, modèle).
    La page va dans la consigne système (stable, mise en cache pendant les échanges)."""
    systeme = f"{CONSIGNE_TUTEUR}\n\nPage « {note.title} » :\n\n{page_texte[:MAX_CHARS_COURS]}"
    r = generate(_messages(question[:MAX_CHARS_QUESTION], historique), system=systeme, role="rapide",
                 max_tokens=MAX_TOKENS_TUTEUR, effort="low", user=user, feature="tuteur")
    if not r.text:
        raise Echec("L’IA n’a renvoyé aucune réponse.")
    return r.text, _usage(r), r.model


CONSIGNE_WIKI = """Tu réponds à un étudiant à partir de son wiki de cours : des extraits numérotés de ses pages et \
de ses PDF te sont fournis. Réponds en français, en 3 à 12 phrases, en Markdown léger (gras pour les mots clés, listes \
courtes, formules en LaTeX entre $). Appuie-toi d'abord sur les extraits et cite-les par leur numéro entre crochets, \
par exemple [2] ou [1, 3], juste après l'idée qu'ils appuient. Si les extraits ne contiennent pas la réponse, dis-le \
clairement (« Le wiki n'en parle pas »), puis réponds avec prudence d'après tes connaissances, sans citation. \
N'invente jamais de numéro de source."""


def consigne_wiki(sources) -> str:
    extraits = "\n\n".join(f"[{i}] {s['titre']}\n{s['texte']}" for i, s in enumerate(sources, 1))
    return f"{CONSIGNE_WIKI}\n\nExtraits du wiki :\n\n{extraits or '(aucun extrait ne correspond à la question)'}"


def demander_wiki(question, sources, historique=(), user=None):
    """Réponse (rôle « rapide ») à une question sur tout le wiki, d'après les extraits numérotés :
    (texte Markdown, usage, modèle)."""
    r = generate(_messages(question[:MAX_CHARS_QUESTION], historique), system=consigne_wiki(sources), role="rapide",
                 max_tokens=MAX_TOKENS_TUTEUR, effort="low", user=user, feature="wiki")
    if not r.text:
        raise Echec("L’IA n’a renvoyé aucune réponse.")
    return r.text, _usage(r), r.model


# -- Copie photographiée --------------------------------------------------------------------------
CONSIGNE_COPIE = """Tu corriges la copie manuscrite d'un étudiant, photographiée (images jointes, dans l'ordre des \
pages). Réponds en français, en Markdown (titres ###, listes, formules en LaTeX entre $), en suivant ce plan :

### Ce que tu as écrit
La démarche de l'étudiant, transcrite brièvement étape par étape (numérotées).

### Correction pas à pas
Pour chaque étape : ✅ juste, ⚠️ imprécis ou incomplet, ❌ faux ; une phrase d'explication et, si c'est faux, \
la correction (le bon calcul ou le bon raisonnement).

### Bilan
Ce qui est acquis, l'erreur principale à retenir, et une note indicative sur 20 si l'exercice s'y prête.

Appuie-toi sur l'énoncé et sur le cours fournis s'il y en a. Si une partie est illisible, dis-le au lieu de \
deviner. Si les images ne montrent pas une copie (photo floue, autre chose), dis-le simplement. Sois exigeant \
mais bienveillant."""


def corriger_copie(images, enonce="", cours="", titre_cours="", user=None):
    """Correction (rôle « vision ») d'une copie manuscrite : ``images`` = JPEG (octets), dans l'ordre des pages.
    Renvoie (texte Markdown, usage, modèle)."""
    systeme = CONSIGNE_COPIE
    if cours:
        systeme += f"\n\nCours de référence « {titre_cours} » :\n\n{cours[:MAX_CHARS_COURS // 2]}"
    texte = (f"Énoncé de l'exercice :\n{enonce[:MAX_CHARS_QUESTION * 2]}" if enonce.strip()
             else "Pas d'énoncé fourni : déduis-le de la copie si possible.")
    r = generate(_messages([{"type": "text", "text": texte}]), system=systeme, role="vision",
                 images=[(img, "image/jpeg") for img in images], max_tokens=MAX_TOKENS_COPIE, effort="medium",
                 user=user, feature="copie")
    if not r.text:
        raise Echec("L’IA n’a renvoyé aucune correction.")
    return r.text, _usage(r), r.model
