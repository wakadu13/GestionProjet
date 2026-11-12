"""Écriture des fichiers de QCM et de langue dans le coffre (QCM/<dossier>/…).

Commun à la génération par Claude (claude.py) et à l'import de listes de
vocabulaire (CSV Quizlet, Anki) : nom de fichier libre d'après le titre,
validation par les chargeurs, écriture en JSON lisible.

Porté de Revision : `slugify` de Django et les aides du coffre (`wiki/vault.py`) sont
remplacés par leurs équivalents locaux (`slugify` ci-dessous, `coffre.py`). On y trouve aussi
la lecture d'une liste de mots envoyée par formulaire (`lire_liste`, dans `claude_views.py`
chez Revision), qui ne fait pas appel à l'IA : « Mon espace », la page d'import et l'agent IA
peuvent la réutiliser.
"""

import json
import unicodedata
from datetime import date
from pathlib import PurePosixPath

from . import langues, quiz
from .coffre import DOSSIER_QCM, INVALID_NAME_RE, invalider, racine_ecriture


def slugify(value):
    """Comme django.utils.text.slugify : ASCII, minuscules, tirets."""
    import re as _re
    value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    value = _re.sub(r"[^\w\s-]", "", value.lower())
    return _re.sub(r"[-\s]+", "-", value).strip("-_")


class PaquetInvalide(ValueError):
    """Le fichier ne passe pas le chargeur ; le message est montré tel quel."""


def dossier_valide(nom):
    """Nom de dossier sous QCM/ (un seul niveau), ou None s'il est invalide."""
    nom = " ".join(str(nom).split())
    if not nom or "/" in nom or "\\" in nom or nom.startswith(".") or INVALID_NAME_RE.search(nom):
        return None
    return nom


_LIGATURES = str.maketrans({"œ": "oe", "Œ": "Oe", "æ": "ae", "Æ": "Ae", "ß": "ss"})


def slug(texte):
    """Nom de fichier tiré d'un titre (« Le cœur » → « le-coeur ») : slugify, ligatures comprises."""
    return slugify(str(texte).translate(_LIGATURES))


def chemin_libre(dossier, titre, defaut):
    """QCM/<dossier>/<titre en minuscules>.json, avec « -2 », « -3 »… si le nom est pris."""
    root = _racine_ecriture()
    base = (slug(titre) or defaut)[:60]
    for i in range(1, 1000):
        rel = PurePosixPath(DOSSIER_QCM, dossier, f"{base}.json" if i == 1 else f"{base}-{i}.json")
        if not (root / rel).exists():
            return rel
    raise PaquetInvalide("Aucun nom de fichier libre.")


def _racine_ecriture():
    try:
        return racine_ecriture()
    except ValueError as e:  # coffre introuvable : message montré tel quel, rien n'est créé
        raise PaquetInvalide(str(e)) from None


def enregistrer_json(rel, donnees):
    root = _racine_ecriture()
    cible = root / rel
    if not cible.resolve().is_relative_to(root.resolve()):
        raise PaquetInvalide("Dossier invalide.")
    cible.parent.mkdir(parents=True, exist_ok=True)
    cible.write_text(json.dumps(donnees, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    invalider()


def ecrire_paquet_langue(dossier, paquet):
    """Valide un fichier de langue et l'écrit ; renvoie son chemin dans le coffre."""
    rel = chemin_libre(dossier, paquet.get("titre", ""), "vocabulaire")
    try:
        langues.set_depuis_json(paquet, rel)
    except ValueError as e:
        raise PaquetInvalide(f"Paquet invalide : {e}") from None
    enregistrer_json(rel, paquet)
    return rel.as_posix()


def ecrire_set_qcm(dossier, set_json):
    """Valide un set de questions et l'écrit ; renvoie son chemin dans le coffre."""
    rel = chemin_libre(dossier, set_json.get("titre", ""), "qcm")
    try:
        quiz.set_depuis_json(set_json, rel)
    except ValueError as e:
        raise PaquetInvalide(f"QCM invalide : {e}") from None
    enregistrer_json(rel, set_json)
    return rel.as_posix()


def paquet_vocabulaire(langue, titre, description, types, fiches, origine):
    """Fichier de langue d'après des fiches {mot, traductions et/ou definition, …} (ids ajoutés)."""
    prefixe = (slugify(titre) or "voc")[:30]
    vus, propres = set(), []
    for fiche in fiches:
        mot = str(fiche.get("mot", "")).strip()
        trads = [t for t in (fiche.get("traductions") or []) if str(t).strip()]
        if not mot or not (trads or str(fiche.get("definition") or "").strip()) or mot.lower() in vus:
            continue
        vus.add(mot.lower())
        propres.append({"id": f"{prefixe}-{len(propres) + 1:03d}", **{k: v for k, v in fiche.items() if v not in ("", [], None)}})
    paquet = {"langue": langue, "titre": titre, "description": description, "origine": origine,
              "exercices": list(types), "vocabulaire": propres}
    if langues.abreviation(langue) == "EN":
        paquet["particules_optionnelles"] = ["to", "a", "an", "the"]
    return paquet


# -- Listes de mots collées ou exportées (Quizlet, Anki, tableur) --------------------------------

import csv  # noqa: E402
import html as html_  # noqa: E402
import io  # noqa: E402
import re  # noqa: E402

LANGUE_PUIS_FR, FR_PUIS_LANGUE, MOT_PUIS_DEFINITION = "langue_fr", "fr_langue", "langue_def"
ORDRES = (LANGUE_PUIS_FR, FR_PUIS_LANGUE, MOT_PUIS_DEFINITION)
MAX_LIGNES = 500
_BALISES = re.compile(r"<[^>]+>")


def _propre(cellule):
    """Sans balises HTML (export Anki), entités décodées, espaces normalisés."""
    texte = html_.unescape(_BALISES.sub(" ", str(cellule))).replace("\xa0", " ")
    return " ".join(texte.split())


def _separateur(lignes):
    echantillon = "\n".join(lignes[:30])
    if "\t" in echantillon:
        return "\t"
    try:
        return csv.Sniffer().sniff(echantillon, delimiters=";,|").delimiter
    except csv.Error:
        pass
    if sum(" = " in l or "=" in l for l in lignes) >= max(1, len(lignes) // 2):
        return "="
    return "," if "," in echantillon else ";"


def analyser_liste(texte, ordre=LANGUE_PUIS_FR):
    """Lignes « mot ⇄ traduction » → (fiches {mot, traductions, exemple?, note?}, lignes ignorées).

    Accepte les exports Quizlet et Anki (tabulations, lignes « #… » d'en-tête,
    HTML), un CSV de tableur (virgules, points-virgules, guillemets) ou des
    lignes « mot = traduction ». Un 3e champ devient l'exemple, un 4e la note.
    Plusieurs traductions séparées par « , », « ; » ou « / » ; plusieurs formes
    du mot séparées par « / » (la première est le mot, les autres des variantes).
    MOT_PUIS_DEFINITION : « mot – définition » dans la langue apprise (pas de
    traduction), comme un set Quizlet anglais → anglais ; avec des virgules ou des
    points-virgules comme séparateur, tout ce qui suit le mot est la définition.
    """
    # splitlines : fins de ligne Windows (CRLF), Unix (LF) et anciens Mac (CR seul, « CSV (Macintosh) »).
    lignes = [l for l in texte.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    if len(lignes) > MAX_LIGNES:
        raise PaquetInvalide(f"Trop de lignes ({len(lignes)}) : {MAX_LIGNES} au plus par paquet.")
    if not lignes:
        return [], 0
    sep = _separateur(lignes)
    if sep == "=":
        rangs = [[part.strip() for part in l.split("=", 1)] for l in lignes]
    else:
        try:
            rangs = list(csv.reader(io.StringIO("\n".join(lignes)), delimiter=sep, skipinitialspace=True))
        except csv.Error:
            raise PaquetInvalide("Liste illisible : enregistre-la en texte (UTF-8), une ligne par mot, "
                                 "le mot et sa traduction séparés par une tabulation ou un point-virgule.") from None
    fiches, ignorees = [], 0
    for rang in rangs:
        cellules = [_propre(c) for c in rang]
        cellules = [c for c in cellules if c] + [""] * 2
        if not cellules[0] or not cellules[1]:
            ignorees += 1
            continue
        a, b = cellules[0], cellules[1]
        if ordre == MOT_PUIS_DEFINITION:
            formes = [f.strip() for f in a.split("/") if f.strip()]
            if not formes:
                ignorees += 1
                continue
            fiche = {"mot": formes[0]}
            if len(formes) > 1:
                fiche["variantes"] = formes[1:]
            if sep in ",;":
                fiche["definition"] = f"{sep} ".join(c for c in cellules[1:] if c)
            else:
                fiche["definition"] = b
                extra = [c for c in cellules[2:] if c]
                if extra:
                    fiche["exemple"] = extra[0]
                if len(extra) > 1:
                    fiche["note"] = extra[1]
            fiches.append(fiche)
            continue
        mot_brut, trad_brut = (a, b) if ordre == LANGUE_PUIS_FR else (b, a)
        formes = [f.strip() for f in mot_brut.split("/") if f.strip()]
        trads = [t.strip() for t in re.split(r"[;,/]", trad_brut) if t.strip()]
        if not formes or not trads:
            ignorees += 1
            continue
        fiche = {"mot": formes[0], "traductions": trads}
        if len(formes) > 1:
            fiche["variantes"] = formes[1:]
        extra = [c for c in cellules[2:] if c]
        if extra:
            fiche["exemple"] = extra[0]
        if len(extra) > 1:
            fiche["note"] = extra[1]
        fiches.append(fiche)
    return fiches, ignorees


# -- Liste de mots envoyée par formulaire (« Mon espace », import, agent IA) ----------------------

LANGUE_RE = re.compile(r"[^\W\d_][^\W\d_ '-]*(?:[ '-][^\W\d_]+)*")
TAILLE_MAX_LISTE = 1024 * 1024
DOSSIER_VOCABULAIRE = "Vocabulaire"
# Cartes cochées par défaut (comme claude.CARTES_PAR_DEFAUT de Revision).
CARTES_PAR_DEFAUT = [langues.DECOUVERTE, langues.PRODUCTION, langues.CONTEXTE]
DOSSIER_INVALIDE = 'Dossier invalide : un seul nom de dossier, sans / \\ : * ? " < > | # ^ [ ].'


def options_vocabulaire(form):
    """Langue, types de cartes, dossier et titre d'un paquet de vocabulaire demandé
    par formulaire : ({langue, types, dossier, titre}, message d'erreur ou "")."""
    langue = " ".join(form.get("langue", "").split()) or "Anglais"
    types = [t for t in form.getlist("cartes") if t in langues.TYPES_VOCABULAIRE]
    dossier = form.get("dossier", "").strip()
    erreur = (
        "Langue invalide." if len(langue) > 40 or not LANGUE_RE.fullmatch(langue)
        else "Coche au moins un type de carte." if not types
        else DOSSIER_INVALIDE if dossier and dossier_valide(dossier) is None
        else ""
    )
    options = {"langue": langue[0].upper() + langue[1:], "types": types, "dossier": dossier_valide(dossier) or "",
               "titre": form.get("titre", "").strip()[:120]}
    return options, erreur


def decoder_liste(brut: bytes) -> str:
    """Texte d'une liste de mots envoyée en fichier : UTF-8 (avec ou sans BOM), UTF-16 (export
    « Texte Unicode » d'Excel, reconnu à son BOM), sinon Windows-1252 (« ANSI »)."""
    if brut.startswith((b"\xff\xfe", b"\xfe\xff")):
        return brut.decode("utf-16", errors="replace")
    try:
        return brut.decode("utf-8-sig")
    except UnicodeDecodeError:
        return brut.decode("cp1252", errors="replace")


def lire_liste(form, fichier=None, aujourdhui=None):
    """Paquet de vocabulaire d'après une liste collée ou un fichier exporté (Quizlet,
    Anki, tableur) du formulaire : (paquet, options, lignes ignorées, erreur).

    ``form`` : les champs (``request.form``) ; ``fichier`` : le fichier envoyé (FileStorage) ou None."""
    options, erreur = options_vocabulaire(form)
    texte = form.get("liste", "")
    if fichier is not None and getattr(fichier, "filename", ""):
        brut = fichier.read(TAILLE_MAX_LISTE + 1)
        if len(brut) > TAILLE_MAX_LISTE:
            erreur = erreur or "Fichier trop gros (1 Mo au plus)."
        else:
            texte = decoder_liste(brut)
    ordre = form.get("ordre", LANGUE_PUIS_FR)
    if ordre not in ORDRES:
        ordre = LANGUE_PUIS_FR
    if ordre == MOT_PUIS_DEFINITION:
        # Liste dans la seule langue apprise : les cartes qui se passent de traduction.
        options["types"] = [t for t in options["types"] if t in langues.SANS_TRADUCTION] or list(langues.VOCABULAIRE_DEFINITIONS)
    fiches, ignorees = [], 0
    if not erreur:
        try:
            fiches, ignorees = analyser_liste(texte, ordre)
        except PaquetInvalide as e:
            erreur = str(e)
        else:
            if not fiches:
                erreur = ("Aucune ligne « mot – traduction » (ou « mot – définition ») reconnue : une ligne par mot, "
                          "séparés par une tabulation, une virgule ou « = ».")
    if erreur:
        return None, options, 0, erreur
    jour = aujourdhui or date.today()
    titre = options["titre"] or f"Vocabulaire {options['langue'].lower()} du {jour:%d/%m/%Y}"
    paquet = paquet_vocabulaire(options["langue"], titre, "", options["types"], fiches, "Import de liste")
    return paquet, options, ignorees, None


def message_paquet(paquet, ignorees):
    n = len(paquet["vocabulaire"])
    return (f"Paquet « {paquet['titre']} » créé : {n} mot{'s' if n > 1 else ''}"
            + (f", {ignorees} ligne{'s' if ignorees > 1 else ''} ignorée{'s' if ignorees > 1 else ''}" if ignorees else "") + ".")
