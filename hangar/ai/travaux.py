"""Travaux de fond de l'IA : un cours (PDF, LaTeX) transformé en page du wiki, fiche de révision d'une page,
QCM écrit d'après une page ou un texte collé, variantes des questions ratées, paquet de vocabulaire.

Port de ``wiki/claude.py`` de Revision (mêmes consignes, mêmes schémas JSON, mêmes protections) sur la file
de travaux de ``hangar.ai.jobs`` : un travail à la fois, hors des requêtes web, repris après un redémarrage,
envoyé en lot à moitié prix si demandé (Claude). Le suivi est sur /transformations/ (« Travaux »).

- Une page déjà modifiée à la main n'est jamais écrasée : la version de l'IA est enregistrée à côté
  (« Page (IA).md ») ; une page remplacée garde son texte précédent dans l'historique du wiki.
- Les QCM et paquets arrivent en brouillon (statut « draft ») : relus, corrigés, puis publiés dans le coffre
  (QCM/<catégorie>/…), sauf demande de publication directe.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath

from flask import current_app, url_for

from ..extensions import db
from ..models import now
from ..wiki import coffre
from . import jobs
from .demandes import (NOTE_EXTRAITS, QUESTION_SCHEMA, budget_cours, extraits_cours, question_en_json,
                       sans_cadre)
from .models import AIJob
from .providers import AIError

Echec = AIError

GENRES = {"page": "Page de cours", "qcm": "QCM", "variantes": "QCM (variantes)", "vocabulaire": "Vocabulaire",
          "fiche": "Fiche de révision"}
STATUTS = {"pending": "En attente", "running": "En cours", "batch": "Dans un lot", "draft": "À relire",
           "done": "Terminé", "error": "Échec"}
MAX_TOKENS = 128000            # page d'un long cours (réponse en flux)
MAX_TOKENS_GENERATION = 64000
NOMBRES_QUESTIONS = (10, 15, 20, 30, 40)
MAX_MOTS = 150
MAX_TEXTE_COLLE = 300_000
MAX_VARIANTES = 20
CATEGORIE_GENEREE = "Générés"
DOSSIER_VOCABULAIRE = "Vocabulaire"
SUFFIXE_IA = "IA"              # « Page (IA).md » quand la page a été modifiée entre-temps (Revision : « (Claude) »)

CONSIGNE = """Tu transformes un cours (PDF ou LaTeX) en une page de wiki au format Markdown d'Obsidian, \
pour qu'un étudiant le révise.

Contenu :
- Garde tout le contenu du cours : définitions, théorèmes, démonstrations, exemples, exercices, \
formules, tableaux, remarques. Ne résume pas et ne coupe rien.
- Reformule seulement pour que le texte se lise bien : des phrases complètes et des listes claires \
à la place des fragments de diapositives, sans répétitions d'en-têtes ni numéros de pages.
- Écris dans la langue du cours.

Forme :
- Commence par les propriétés : une ligne ---, puis tags: [2 à 5 mots-clés en minuscules, sans \
espaces], puis une ligne ---.
- Puis un titre # avec le nom du cours, un court paragraphe d'introduction qui dit ce que couvre le \
cours, et des sections ## et ### qui suivent son plan.
- Formules en LaTeX : $...$ dans le texte, $$...$$ seul sur ses lignes pour les formules centrées.
- Encadrés Obsidian pour ce qui doit ressortir, chaque ligne commençant par « > » :
  > [!definition] Nom de la notion
  > [!theoreme] Nom du théorème (propriétés, lois, formules à connaître)
  > [!demonstration]- Démonstration (le « - » la replie)
  > [!exemple] Titre
  > [!exercice] Titre
  > [!methode] Titre (méthodes, étapes à suivre)
  > [!remarque], > [!warning] (pièges et erreurs fréquentes), > [!retenir] (l'essentiel d'une partie)
- Tableaux Markdown pour les tableaux, blocs ``` avec le langage pour le code. Un schéma du cours \
qui s'y prête peut devenir un bloc ```mermaid ; sinon, décris-le en une ou deux phrases.
- Mets en **gras** les notions clés à leur première apparition.
- Pas de liens [[...]] vers d'autres pages ni d'images : elles n'existent pas dans le wiki.

Réponds uniquement par le contenu de la page, sans bloc de code autour et sans commentaire sur ta \
démarche."""

CONSIGNE_QCM = """Tu écris des questions de révision à partir d'un cours, pour qu'un étudiant vérifie \
qu'il le connaît.

- Les questions sont fidèles au cours : rien qui n'y figure pas, aucune erreur. Elles couvrent tout le \
cours, des définitions aux applications, sans poser deux fois la même chose.
- Types (champ type) : QCM, une seule bonne réponse parmi 4 choix ; QCR, 2 ou 3 bonnes réponses parmi \
4 ou 5 choix ; TROU, une phrase du cours où un mot clé est remplacé par _____ ; QRC, une question à \
réponse courte (un mot, un nombre, une formule courte) ; FLASHCARD, une notion au recto, son explication \
au verso. Surtout des QCM, puis quelques QCR, TROU, QRC et FLASHCARD.
- choix : les choix (QCM et QCR ; sinon liste vide). reponses : les indices des bonnes réponses, à partir \
de 0 (QCM et QCR ; sinon liste vide). reponses_texte : les réponses acceptées, variantes d'écriture \
comprises (TROU et QRC ; sinon liste vide). verso : le dos de la carte (FLASHCARD ; sinon vide). \
consigne : une précision pour une QRC, sinon vide. explication : une phrase qui justifie la réponse.
- Mauvais choix plausibles, de longueur proche de la bonne réponse ; pas de « toutes les réponses » ni \
d'« aucune ». Varie la place de la bonne réponse.
- Formules en LaTeX entre $...$.
- titre : un nom court pour le QCM ; description : une phrase sur ce qu'il couvre.
Écris dans la langue du cours."""

SCHEMA_QCM = {
    "type": "object",
    "properties": {
        "titre": {"type": "string"},
        "description": {"type": "string"},
        "questions": {"type": "array", "items": QUESTION_SCHEMA},
    },
    "required": ["titre", "description", "questions"],
    "additionalProperties": False,
}

CONSIGNE_VOCABULAIRE = """Tu prépares des fiches de vocabulaire en {langue} pour un francophone qui révise.

On te donne une liste de mots ou d'expressions, un par ligne, en français ou en {langue}, parfois avec \
leur traduction. Pour chaque ligne, dans l'ordre, une fiche :
- mot : le mot en {langue}, sous sa forme de dictionnaire. En anglais : « a » ou « an » devant les noms \
dénombrables, « to » devant les verbes.
- traductions : une à trois traductions françaises usuelles, avec l'article pour les noms (« un emploi »).
- definition : une définition courte et simple en {langue}, comme dans un dictionnaire pour apprenants \
(niveau B1), qui n'emploie pas le mot lui-même.
- variantes : d'autres façons correctes de dire le mot en {langue} (orthographe britannique ou \
américaine, synonyme très proche), sinon liste vide.
- exemple : une phrase simple et naturelle en {langue} où le mot à retrouver est entre crochets, comme \
« She [achieved] her goal. ».
- exemple_traduction : la traduction française de l'exemple.
- prononciation : la prononciation en alphabet phonétique international, entre barres obliques.
- note : un piège, un registre, un faux ami ou une construction utile, sinon vide.
Ignore les lignes qui ne sont pas des mots (titres, numéros). Une fiche par mot.
titre : un nom court pour le paquet (le thème des mots) ; description : une phrase."""

_TEXTES = {"type": "array", "items": {"type": "string"}}
SCHEMA_VOCABULAIRE = {
    "type": "object",
    "properties": {
        "titre": {"type": "string"},
        "description": {"type": "string"},
        "vocabulaire": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "mot": {"type": "string"},
                "traductions": _TEXTES,
                "definition": {"type": "string"},
                "variantes": _TEXTES,
                "exemple": {"type": "string"},
                "exemple_traduction": {"type": "string"},
                "prononciation": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["mot", "traductions", "definition", "variantes", "exemple", "exemple_traduction",
                         "prononciation", "note"],
            "additionalProperties": False,
        }},
    },
    "required": ["titre", "description", "vocabulaire"],
    "additionalProperties": False,
}

CONSIGNE_FICHE = """Tu rédiges la fiche de révision d'une page de cours, pour un étudiant qui révise la veille d'un examen.

- Une seule page : l'essentiel, rien de plus. Pas de phrase d'introduction, pas de commentaire.
- Dans l'ordre du cours : définitions clés, formules et lois à connaître (en LaTeX : $...$ ou $$...$$), \
méthodes et étapes à suivre, pièges et erreurs fréquentes, ordres de grandeur et exemples très courts.
- Markdown d'Obsidian : sections ##, listes courtes, **gras** pour les mots clés, encadrés \
> [!definition], > [!theoreme], > [!methode], > [!warning] (pièges), > [!retenir] (l'essentiel en 5 lignes, à la fin).
- Rien qui ne soit pas dans la page. Pas de liens [[...]] ni d'images. Dans la langue de la page.
Réponds uniquement par le contenu de la fiche, sans bloc de code autour."""

CONSIGNE_VARIANTES = """Tu écris des variantes de questions de révision qu'un étudiant a ratées, pour qu'il retravaille \
la notion au lieu de réapprendre une réponse par cœur. Pour chaque question d'origine, écris une question nouvelle \
sur la même notion : angle différent (application, contre-exemple, cas particulier, autre formulation, calcul \
voisin), même difficulté ou un peu plus, en variant les types (QCM, QCR, TROU, QRC, FLASHCARD). Jamais la même \
question reformulée. Fidèle au cours fourni quand il y en a un ; explication courte. Les formules s'écrivent en LaTeX \
entre $. Réponds avec le titre, une description et la liste des questions."""


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------

def _vault():
    from ..wiki.vault import get_vault
    return get_vault()


def empreinte(texte: str) -> str:
    return hashlib.sha256(texte.encode("utf-8")).hexdigest()


PAGE_INACCESSIBLE = ("L'ordinateur refuse d'ouvrir la page (elle est verrouillée par un autre programme, ou pas "
                     "encore téléchargée par OneDrive) : relance le travail dans un moment.")


def _texte(fichier) -> str:
    """Texte d'une page du coffre, quel que soit son encodage (« ANSI » du Bloc-notes…), comme dans l'éditeur."""
    from ..wiki.vault import lire_note
    try:
        return lire_note(fichier)[0]
    except OSError as e:
        raise Echec(PAGE_INACCESSIBLE) from e


def _empreinte_actuelle(page: str) -> str:
    fichier = _vault().root / page
    if not fichier.is_file():
        return ""
    try:
        return empreinte(_texte(fichier))
    except Echec:
        return "illisible"  # jamais égale à une empreinte : la page ne sera pas écrasée (version de l'IA à côté)


def dossier_valide(nom):
    from ..qcm.paquets import dossier_valide as _dv
    return _dv(nom)


def nom(job: AIJob) -> str:
    """Nom du cours, de la page ou du set concerné (affiché dans les travaux)."""
    p = job.payload
    if job.kind in ("qcm", "fiche", "variantes"):
        return p.get("nom") or str(p.get("source", "")).rsplit("/", 1)[-1].removesuffix(".md") or job.title
    if job.kind == "vocabulaire":
        return p.get("titre") or f"Vocabulaire ({p.get('langue', 'Anglais')})"
    return str(p.get("source", "")).rsplit("/", 1)[-1] or job.title


def _sans_vides(o: dict) -> dict:
    return {k: v for k, v in o.items() if v not in ("", [], None)}


def _msgs(*textes):
    return [{"role": "user", "content": [{"type": "text", "text": t} for t in textes]}]


def _genere_par(r) -> str:
    return f"{r.provider or 'IA'} ({r.model})"


def _json(ai_call, trop_long: str):
    """Appel JSON : le message « trop long » de Revision quand la réponse est coupée."""
    try:
        return ai_call()
    except AIError as e:
        if e.code == "trop_long":
            raise Echec(trop_long) from None
        raise


# ---------------------------------------------------------------------------
# Demandes (création des travaux)
# ---------------------------------------------------------------------------

def _enqueue(kind, payload, user, title, lot=False, url=None):
    from .core import cap_reached, check_batch_cap

    if lot:  # lots en cours compris : sinon N travaux en lot partiraient tous avant le premier retour
        check_batch_cap()
    elif cap_reached():
        raise Echec("Plafond mensuel de l’IA atteint : réessaie le mois prochain ou relève le plafond dans les "
                    "réglages de l’IA.", "plafond", 429)
    job = jobs.enqueue(kind, payload, user=user, feature={"page": "transformation"}.get(kind, kind), title=title,
                       batch=lot, start=False)
    if url:
        opts = job.options
        opts["url"] = url
        job.options = opts
        db.session.commit()
    jobs.start_worker()
    return job


def demander_pdf(attachment, user=None, lot=False) -> AIJob:
    """Transformation d'un PDF du coffre en page (à côté de lui, même nom en .md)."""
    page = PurePosixPath(attachment.path).with_suffix(".md").as_posix()
    return _enqueue("page", {"source": PurePosixPath(attachment.path).as_posix(), "page": page,
                             "empreinte": _empreinte_actuelle(page)}, user, attachment.name, lot)


def demander_latex(nom_fichier, texte, page, user=None, lot=False) -> AIJob:
    """Transformation d'un cours LaTeX importé : remplace la page convertie sur place, si personne ne l'a
    modifiée d'ici là."""
    return _enqueue("page", {"source": nom_fichier, "texte_source": texte, "page": page,
                             "empreinte": _empreinte_actuelle(page)}, user, nom_fichier, lot)


def demander_qcm(*, note=None, texte="", nombre=20, categorie="", titre="", user=None, publier=False,
                 lot=False) -> AIJob:
    """QCM généré d'après une note du wiki (``note``) ou un texte collé."""
    source = note.path.as_posix() if note is not None else "Texte collé"
    if not categorie:
        categorie = (note.path.parent.name if note is not None else "") or CATEGORIE_GENEREE
    return _enqueue("qcm", {"source": source, "texte_source": "" if note is not None else texte,
                            "nombre": nombre, "categorie": categorie, "titre": titre,
                            "nom": note.title if note is not None else "", "publier": bool(publier)},
                    user, f"QCM · {note.title if note is not None else 'texte collé'}", lot)


def demander_vocabulaire(*, mots, langue="Anglais", types=(), dossier="", titre="", user=None, publier=False,
                         lot=False) -> AIJob:
    """Paquet de vocabulaire (un fichier de langue à lui) d'après une liste de mots collée."""
    return _enqueue("vocabulaire", {"texte_source": mots, "langue": langue, "exercices": list(types),
                                    "dossier": dossier or DOSSIER_VOCABULAIRE, "titre": titre, "publier": bool(publier)},
                    user, titre or f"Vocabulaire ({langue})", lot)


def demander_fiche(note, user=None) -> AIJob:
    """Fiche de révision d'une page du wiki, écrite à côté (« Page (Fiche).md »)."""
    page = note.path.with_name(f"{note.path.stem} (Fiche).md").as_posix()
    return _enqueue("fiche", {"source": note.path.as_posix(), "page": page, "empreinte": _empreinte_actuelle(page),
                              "nom": note.title, "cle": note.key}, user, f"Fiche · {note.title}")


def demander_variantes(qset, question_ids, user=None) -> AIJob:
    """Brouillon de QCM fait de variantes des questions ratées d'un set (une par question)."""
    titre = f"Variantes · {qset.titre}"
    return _enqueue("variantes", {"source": qset.path.as_posix() if qset.path else qset.id,
                                  "variantes": list(question_ids)[:MAX_VARIANTES], "set_source": qset.id,
                                  "categorie": qset.categorie, "titre": titre, "nom": titre, "publier": False},
                    user, titre)


def en_cours(kind=None, source=None) -> bool:
    q = AIJob.query.filter(AIJob.status.in_(("pending", "running", "batch")))
    if kind:
        q = q.filter(AIJob.kind == kind)
    return any((source is None or j.payload.get("source") == source) for j in q)


def relancer(job: AIJob) -> bool:
    """Relance un travail en échec (ou annulé), depuis le début ; False s'il n'y a rien à relancer."""
    if job.status != "error":
        return False
    job.status, job.message, job.started_at, job.finished_at = "pending", "", None, None
    job.calls, job.result, job.batch_id = [], None, ""
    opts = job.options
    for k in ("reserve", "batch_at", "batch_call", "files"):
        opts.pop(k, None)
    job.options = opts
    db.session.commit()
    jobs.start_worker()
    return True


def transformer_cours(results, user=None, lot=False) -> int:
    """Cours importés dans le wiki (PDF, LaTeX) confiés à l'IA, qui les transforme en pages en arrière-plan
    (``lot`` : dans un lot, moitié prix, sous 24 h). Complète le message des résultats ; renvoie le nombre de
    transformations lancées. Appelé par l'import du wiki (case « Transformer les cours en pages avec l'IA »)."""
    vault = _vault()
    suivi = url_for("ai.transformations")
    lances = 0
    for r in results:
        if not getattr(r, "ok", False):
            continue
        try:
            if getattr(r, "source_latex", ""):
                demander_latex(r.name, r.source_latex, r.path, user, lot=lot)
            elif r.path.lower().endswith(".pdf") and (pdf := vault.attachments.get(r.path.lower())) is not None:
                demander_pdf(pdf, user, lot=lot)
            else:
                continue
        except AIError as e:  # plafond mensuel atteint : le fichier est importé, sans transformation
            r.message += f" · transformation par l’IA non lancée : {e.message}"
            continue
        r.message += " · transformation par l’IA " + ("dans un lot (sous 24 h)" if lot else "lancée")
        r.claude = suivi
        lances += 1
    return lances


# ---------------------------------------------------------------------------
# Travaux (exécutés par la file de jobs.py)
# ---------------------------------------------------------------------------

@jobs.register_job("page")
def _travail_page(job, ai):
    p = job.payload
    titre = nom(job)
    if p.get("texte_source"):
        r = ai(_msgs(f"Source LaTeX du cours « {titre} » :\n\n{p['texte_source']}",
                     f"Transforme ce cours ({titre}) en page du wiki."),
               system=CONSIGNE, max_tokens=MAX_TOKENS, effort="medium")
    else:
        vault = _vault()
        pdf = vault.attachments.get(str(p.get("source", "")).lower())
        if pdf is None or not (vault.root / pdf.path).is_file():
            raise Echec("Le PDF n'est plus dans le wiki.")
        r = ai(_msgs(f"Transforme ce cours ({titre}) en page du wiki."), system=CONSIGNE, max_tokens=MAX_TOKENS,
               effort="medium", documents=[(pdf.path.name, (vault.root / pdf.path).read_bytes())])
    texte = sans_cadre(r.text)
    if not texte:
        raise Echec("L’IA n’a renvoyé aucun texte.")
    avertissement = "Cours trop long : la fin de la page manque." if r.truncated else None
    page = ecrire_page(job, texte, avertissement, pdf_source=not p.get("texte_source"))
    job.message = avertissement or ""
    _lien_resultat(job, page)
    return {"page_ecrite": page}


@jobs.register_job("fiche")
def _travail_fiche(job, ai):
    p = job.payload
    fichier = _vault().root / p.get("source", "")
    if not fichier.is_file():
        raise Echec("La page n'est plus dans le wiki.")
    r = ai(_msgs(f"Page « {p.get('nom')} » :\n\n{_texte(fichier)}",
                 "Rédige la fiche de révision de cette page."),
           system=CONSIGNE_FICHE, max_tokens=MAX_TOKENS_GENERATION, effort="medium")
    texte = sans_cadre(r.text)
    if not texte:
        raise Echec("L’IA n’a renvoyé aucun texte.")
    texte = f"> [!abstract] Fiche de révision de [[{p.get('cle')}|{p.get('nom')}]]\n\n{texte}"
    avertissement = "Fiche trop longue : la fin manque." if r.truncated else None
    page = ecrire_page(job, texte, avertissement, pdf_source=False)
    job.message = avertissement or ""
    _lien_resultat(job, page)
    return {"page_ecrite": page}


def _questions_valides(data):
    """Questions d'une réponse JSON qui passent le chargeur des sets (identifiants ia001, ia002…), et nombre
    de questions écartées."""
    from ..qcm import quiz

    questions, ecartees = [], 0
    for i, q in enumerate(data.get("questions") or [], start=1):
        q = _sans_vides(q if isinstance(q, dict) else {})
        q["id"] = f"ia{i:03d}"
        try:
            quiz.question_depuis_json(q, i, "question")
        except ValueError:
            ecartees += 1
            continue
        questions.append(q)
    if not questions:
        raise Echec("L’IA n’a renvoyé aucune question utilisable : relance.")
    return questions, ecartees


def _avertissement_ecartees(ecartees):
    if not ecartees:
        return None
    s = "s" if ecartees > 1 else ""
    return f"{ecartees} question{s} mal formée{s} écartée{s}."


@jobs.register_job("qcm")
def _travail_qcm(job, ai):
    p = job.payload
    nombre = p.get("nombre") if p.get("nombre") in NOMBRES_QUESTIONS else 20
    if p.get("texte_source"):
        cours, nom_cours = p["texte_source"], "texte collé"
    else:
        fichier = _vault().root / p.get("source", "")
        if not fichier.is_file():
            raise Echec("La page du cours n'est plus dans le wiki.")
        cours, nom_cours = _texte(fichier), p.get("nom") or PurePosixPath(p["source"]).stem
    r = _json(lambda: ai(_msgs(f"Cours « {nom_cours} » :\n\n{cours}", f"Écris {nombre} questions sur ce cours."),
                               system=CONSIGNE_QCM, json_schema=SCHEMA_QCM, max_tokens=MAX_TOKENS_GENERATION,
                               effort="medium"),
              "Réponse trop longue : demande moins de questions.")
    questions, ecartees = _questions_valides(r.data)
    titre = str(p.get("titre") or r.data.get("titre") or nom_cours).strip()
    set_json = {"titre": titre, "description": str(r.data.get("description", "")).strip(), "genere_par": _genere_par(r),
                "questions": questions}
    if not p.get("texte_source"):
        set_json["source_page"] = str(p["source"]).removesuffix(".md")
    categorie = dossier_valide(p.get("categorie", "")) or CATEGORIE_GENEREE
    return _finir_contenu(job, categorie, set_json, _avertissement_ecartees(ecartees))


@jobs.register_job("variantes")
def _travail_variantes(job, ai):
    from ..qcm import quiz

    p = job.payload
    qset = quiz.get_library().get(str(p.get("set_source", "")))
    if qset is None:
        raise Echec("Le set d'origine n'est plus dans le wiki.")
    voulues = set(p.get("variantes") or [])
    questions = [q for q in qset.questions if q.id in voulues]
    if not questions:
        raise Echec("Les questions ratées n'existent plus dans le set.")
    textes = []
    note = None
    if qset.source_page:
        vault = _vault()
        note = vault.notes.get(qset.source_page.lower())
        if note is not None:
            sujet = " ".join(str(x) for q in questions for x in (q.enonce, *(q.choix or []), *(q.reponses_texte or [])) if x)
            cours, coupe = extraits_cours(_texte(vault.root / note.path), sujet, budget_cours("chat"))
            textes.append(f"Cours « {note.title} »" + (f" {NOTE_EXTRAITS}" if coupe else " :") + f"\n\n{cours}")
    n = len(questions)
    texte = "Questions ratées :\n" + json.dumps([question_en_json(q) for q in questions], ensure_ascii=False, indent=1)
    texte += f"\n\nÉcris {n} variante{'s' if n > 1 else ''} : une par question, dans le même ordre."
    if note is None:
        texte += "\n\nAucun cours fourni : reste fidèle aux questions et à leurs explications."
    textes.append(texte)
    r = _json(lambda: ai(_msgs(*textes), system=CONSIGNE_VARIANTES, json_schema=SCHEMA_QCM,
                               max_tokens=MAX_TOKENS_GENERATION, effort="medium"),
              "Réponse trop longue : demande des variantes pour moins de questions.")
    questions_json, ecartees = _questions_valides(r.data)
    set_json = {"titre": str(p.get("titre") or f"Variantes · {qset.titre}"),
                "description": f"Variantes des questions ratées de « {qset.titre} », pour retravailler les notions "
                               "sans apprendre les réponses par cœur.",
                "genere_par": _genere_par(r), "questions": questions_json}
    if qset.source_page:
        set_json["source_page"] = qset.source_page
    return _finir_contenu(job, dossier_valide(qset.categorie) or CATEGORIE_GENEREE, set_json,
                          _avertissement_ecartees(ecartees))


@jobs.register_job("vocabulaire")
def _travail_vocabulaire(job, ai):
    from ..qcm import langues, paquets

    p = job.payload
    langue = str(p.get("langue") or "Anglais").strip()
    r = _json(lambda: ai(_msgs(f"Mots à préparer en {langue.lower()} :\n\n{p.get('texte_source', '')}"),
                               system=CONSIGNE_VOCABULAIRE.format(langue=langue.lower()), json_schema=SCHEMA_VOCABULAIRE,
                               max_tokens=MAX_TOKENS_GENERATION, effort="medium"),
              "Réponse trop longue : envoie moins de mots à la fois.")
    dossier = dossier_valide(p.get("dossier", "")) or DOSSIER_VOCABULAIRE
    types = [x for x in p.get("exercices") or [] if x in langues.TYPES_VOCABULAIRE] or list(langues.VOCABULAIRE_PAR_DEFAUT)
    titre = str(p.get("titre") or r.data.get("titre") or f"Vocabulaire ({langue})").strip()
    fiches = [_sans_vides(v) for v in r.data.get("vocabulaire") or [] if isinstance(v, dict)]
    paquet = paquets.paquet_vocabulaire(langue, titre, str(r.data.get("description", "")).strip(), types, fiches,
                                        _genere_par(r))
    if not paquet["vocabulaire"]:
        raise Echec("L’IA n’a renvoyé aucun mot utilisable : vérifie la liste et relance.")
    return _finir_contenu(job, dossier, paquet, None)


def _finir_contenu(job, dossier, contenu, avertissement):
    """Set ou paquet généré : publié tout de suite si demandé, sinon brouillon à relire."""
    if job.payload.get("publier"):
        chemin = _publier_contenu(job.kind, dossier, contenu)
        job.message = avertissement or ""
        _lien_resultat(job, chemin)
        return {"page_ecrite": chemin}
    job.status = "draft"
    job.message = " ".join(filter(None, [avertissement, "À relire avant publication."]))
    opts = job.options
    opts["url"] = url_for("ai.relire", jid=job.id)
    job.options = opts
    return {"dossier": dossier, "contenu": contenu}


def _publier_contenu(kind, dossier, contenu):
    from ..qcm import paquets

    try:
        if kind == "vocabulaire":
            return paquets.ecrire_paquet_langue(dossier, contenu)
        return paquets.ecrire_set_qcm(dossier, contenu)
    except paquets.PaquetInvalide as e:
        raise Echec(str(e)) from None


def publier(job: AIJob, contenu=None) -> str:
    """Publie un brouillon (tel quel, ou relu : ``contenu`` corrigé). Renvoie le chemin écrit ; lève Echec si le
    contenu ne passe pas le chargeur."""
    res = job.result or {}
    if job.status != "draft" or not res.get("contenu"):
        raise Echec("Ce travail n'est pas un brouillon.")
    chemin = _publier_contenu(job.kind, res["dossier"], contenu if contenu is not None else res["contenu"])
    job.result, job.status, job.message, job.finished_at = {"page_ecrite": chemin}, "done", "", now()
    _lien_resultat(job, chemin)
    db.session.commit()
    return chemin


def supprimer_brouillon(job: AIJob) -> None:
    job.result, job.status, job.message, job.finished_at = None, "error", "Brouillon supprimé sans publication.", now()
    db.session.commit()


def resultat(job: AIJob) -> tuple[str, str]:
    """(adresse, texte du lien) du résultat d'un travail, ou ("", "")."""
    page = (job.result or {}).get("page_ecrite") if job.status == "done" else ""
    if not page:
        return "", ""
    if job.kind in ("qcm", "variantes"):
        return url_for("qcm.play", set=page.removesuffix(".json"), mode="all"), "Jouer ce QCM"
    if job.kind == "vocabulaire":
        return (url_for("qcm.langue_menu", langue=job.payload.get("langue", "Anglais"), fichier=page.removesuffix(".json")),
                "Réviser ce paquet")
    note = _vault().notes.get(page.removesuffix(".md").lower())
    if note is None:
        return "", ""
    return url_for("wiki.page", path=note.key), ("Ouvrir la fiche" if job.kind == "fiche" else "Ouvrir la page")


def _lien_resultat(job, page):
    """Adresse du résultat, pour la notification de fin."""
    try:
        url, _ = resultat(job) if job.status == "done" and (job.result or {}).get("page_ecrite") else ("", "")
    except Exception:  # noqa: BLE001
        url = ""
    if not url:
        if job.kind in ("qcm", "variantes"):
            url = url_for("qcm.play", set=page.removesuffix(".json"), mode="all")
        elif job.kind == "vocabulaire":
            url = url_for("qcm.langue_menu", langue=job.payload.get("langue", "Anglais"), fichier=page.removesuffix(".json"))
        else:
            url = url_for("wiki.page", path=page.removesuffix(".md"))
    opts = job.options
    opts["url"] = url
    job.options = opts


def ecrire_page(job: AIJob, texte: str, avertissement: str | None, pdf_source: bool) -> str:
    """Écrit la page dans le coffre ; renvoie son chemin. Une page modifiée depuis la demande n'est pas écrasée :
    la version de l'IA va à côté. Une page remplacée garde son texte précédent dans l'historique du wiki."""
    from ..wiki import historique
    from ..wiki.vault import INVALID_NAME_RE, invalider

    vault = _vault()
    p = job.payload
    page = PurePosixPath(p.get("page", ""))
    if page.suffix != ".md" or any(INVALID_NAME_RE.search(x) or x.startswith(".") for x in page.parts):
        raise Echec("Nom de page invalide.")
    if pdf_source:
        pdf = vault.attachments.get(str(p.get("source", "")).lower())
        if pdf is not None:
            texte += f"\n\n## Document d'origine\n\n![[{vault.link_for(pdf)}]]"
    if avertissement:
        texte += f"\n\n> [!warning] {avertissement}\n> Le document d'origine reste disponible ci-dessus ou dans le " \
                 "dossier du cours."
    cible = vault.root / page
    modifiee = False
    if cible.is_file():
        try:
            modifiee = empreinte(_texte(cible)) != p.get("empreinte", "")
        except Echec:
            modifiee = True  # illisible pour l'instant : surtout pas écrasée
    if modifiee:
        # Page modifiée (ou créée) par quelqu'un d'autre : la version de l'IA va à côté.
        origine = page
        for i in range(1, 100):
            page = origine.with_name(f"{origine.stem} ({SUFFIXE_IA}).md" if i == 1 else f"{origine.stem} ({SUFFIXE_IA} {i}).md")
            cible = vault.root / page
            if not cible.exists():
                break
        else:
            raise Echec("Aucun nom libre pour la page.")
    if not cible.resolve().is_relative_to(vault.root.resolve()):
        raise Echec("Nom de page invalide.")
    try:
        coffre.dossier_pour_ecrire(current_app.config)  # coffre introuvable : surtout pas recréé vide
    except coffre.CoffreIntrouvable as e:
        raise Echec(str(e)) from None
    cible.parent.mkdir(parents=True, exist_ok=True)
    if cible.is_file():
        # Page « ANSI » comprise : réécrite en UTF-8, son texte d'origine gardé dans l'historique.
        historique.sauvegarder(page.as_posix(), _texte(cible), SUFFIXE_IA)
    cible.write_text(texte + "\n", encoding="utf-8", newline="\n")
    invalider()
    return page.as_posix()


def lignes_travaux(limit: int = 50) -> list[dict]:
    """Travaux récents, pour la page de suivi."""
    out = []
    for j in AIJob.query.order_by(AIJob.id.desc()).limit(limit):
        url, lien = ("", "")
        try:
            url, lien = resultat(j)
        except Exception:  # noqa: BLE001 - page ou set supprimé depuis
            pass
        out.append({"job": j, "nom": nom(j), "genre": GENRES.get(j.kind, j.kind), "statut": STATUTS.get(j.status, j.status),
                    "url": url, "lien": lien, "totaux": j.totaux(), "lot": bool(j.options.get("batch")),
                    "page_ecrite": (j.result or {}).get("page_ecrite", "") if j.status == "done" else ""})
    return out
