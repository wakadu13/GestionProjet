"""Images externes des notes : ``![légende](https://…)`` ou ``<img src="https://…">``.

Le wiki ne charge jamais une ressource d'un autre site : à l'affichage, une image externe devient
un lien vers son adresse (``render.images_externes_en_liens``). Ce module les repère, et les
télécharge dans le coffre quand on le demande (``hangar wiki-import --images`` ou le bouton de la
page Importer) : elles s'affichent alors dans le wiki, aussi hors ligne, et ne dépendent plus du
site d'origine.

Garde-fous du téléchargement : adresses http(s) seulement ; seules de vraies images (PNG, JPEG,
GIF, WebP, BMP, AVIF, reconnues à leur contenu, jamais de SVG) de TAILLE_MAX au plus sont gardées ;
chaque adresse n'est téléchargée qu'une fois. Les liens des pages sont ensuite réécrits en
``![[nom.png|légende]]``, l'ancien texte de chaque page modifiée restant dans l'historique.
"""
from __future__ import annotations

import http.client
import json
import re
import threading
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from flask import current_app

from . import historique
from .render import EXTERNAL_URL_RE, attributs_html
from .vault import CODE_RE, FRONTMATTER_RE, INVALID_NAME_RE, invalider, lire_note, scan

TAILLE_MAX = 20 * 1024 * 1024
DELAI = 30  # secondes par image
DOSSIER_PAR_DEFAUT = "image"  # comme le guide pour les IA (docs/README-IA.md)

# ![légende](adresse "titre") ; la légende peut contenir des crochets appariés (« [1] »).
IMAGE_MD_RE = re.compile(
    r"!\[(?P<alt>(?:[^\[\]\n]|\[[^\[\]\n]*\])*)\]\(\s*<?(?P<url>(?:https?:)?//[^\s)>]+)>?"
    r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^)\n]*\)))?\s*\)",
    re.I,
)
IMAGE_HTML_RE = re.compile(r"<img\b[^>]*>", re.I)
TAILLE_RE = re.compile(r"\d+(?:x\d+)?")
LIGNE_DE_TABLEAU_RE = re.compile(r"^[ \t>]*\|")
NOMS_RESERVES_WINDOWS = {"CON", "PRN", "AUX", "NUL",
                         *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
EXTENSIONS_EQUIVALENTES = {".jpg": {".jpg", ".jpeg"}, ".png": {".png"}, ".gif": {".gif"}, ".webp": {".webp"},
                           ".bmp": {".bmp"}, ".avif": {".avif"}}


@dataclass
class Occurrence:
    debut: int
    fin: int
    url: str
    legende: str = ""
    largeur: str = ""
    dans_tableau: bool = False


def _adresse(src: str) -> str:
    return "https:" + src if src.startswith("//") else src


def trouver(texte: str) -> list[Occurrence]:
    """Images externes du texte d'une note, hors blocs de code et code en ligne."""
    if "//" not in texte:
        return []
    code = [(m.start(), m.end()) for m in CODE_RE.finditer(texte)]

    def dans_le_code(debut):
        return any(a <= debut < b for a, b in code)

    trouvees = []
    for m in IMAGE_MD_RE.finditer(texte):
        if not dans_le_code(m.start()):
            trouvees.append(Occurrence(m.start(), m.end(), _adresse(m.group("url")), " ".join(m.group("alt").split())))
    for m in IMAGE_HTML_RE.finditer(texte):
        attributs = attributs_html(m.group(0))
        src = attributs.get("src", "").strip()
        if EXTERNAL_URL_RE.match(src) and not dans_le_code(m.start()):
            largeur = attributs.get("width", "").strip()
            trouvees.append(Occurrence(m.start(), m.end(), _adresse(src), " ".join(attributs.get("alt", "").split()),
                                       largeur if largeur.isdigit() else ""))
    for occ in trouvees:
        ligne = texte[texte.rfind("\n", 0, occ.debut) + 1:occ.debut]
        occ.dans_tableau = bool(LIGNE_DE_TABLEAU_RE.match(ligne))
    return sorted(trouvees, key=lambda o: o.debut)


def _lien(occ: Occurrence, cible: str) -> str:
    """``![[cible|légende]]`` (``\\|`` dans un tableau, comme Obsidian ; la légende ne doit
    ni fermer le lien ni passer pour une taille)."""
    texte = occ.largeur or " ".join(re.sub(r"[\[\]|]", " ", occ.legende).split())
    if not occ.largeur and TAILLE_RE.fullmatch(texte):
        texte = ""
    if not texte:
        return f"![[{cible}]]"
    separateur = "\\|" if occ.dans_tableau else "|"
    return f"![[{cible}{separateur}{texte}]]"


def remplacer(texte: str, liens: dict[str, str]) -> str:
    """Réécrit les images externes dont l'adresse est dans ``liens`` (adresse → fichier du coffre) en
    intégrations ``![[…]]``. Les propriétés YAML du début de la note ne sont pas touchées."""
    entete = FRONTMATTER_RE.match(texte)
    debut = entete.end() if entete else 0
    morceaux, pos = [], debut
    for occ in trouver(texte[debut:]):
        cible = liens.get(occ.url)
        if cible is None:
            continue
        morceaux.append(texte[pos:debut + occ.debut])
        morceaux.append(_lien(occ, cible))
        pos = debut + occ.fin
    return texte[:debut] + "".join(morceaux) + texte[pos:] if morceaux else texte


def inventaire(vault) -> tuple[int, int]:
    """(nombre d'images externes, nombre de pages qui en contiennent) dans le coffre ; gardé avec
    le coffre (relu, donc recalculé, dès qu'un fichier change)."""
    resultat = getattr(vault, "_inventaire_images", None)
    if resultat is None:
        images = pages = 0
        for note in vault.notes.values():
            n = len(trouver(note.body))
            images += n
            pages += bool(n)
        resultat = vault._inventaire_images = (images, pages)
    return resultat


# ---------------------------------------------------------------------------
# Téléchargement
# ---------------------------------------------------------------------------

class ImageRefusee(Exception):
    """Adresse non téléchargée ; le message dit pourquoi, en français simple."""


def type_image(donnees: bytes) -> str | None:
    """Extension d'après le contenu (jamais d'après l'adresse ni l'en-tête du site)."""
    if donnees.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if donnees.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if donnees.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if donnees[:4] == b"RIFF" and donnees[8:12] == b"WEBP":
        return ".webp"
    if donnees[4:12] in (b"ftypavif", b"ftypavis"):
        return ".avif"
    if donnees.startswith(b"BM") and len(donnees) > 26:
        return ".bmp"
    return None


def _ouvreur():
    """Comme ``urllib.request.urlopen``, mais sans fichiers locaux ni FTP : http(s) seulement,
    redirections comprises."""
    ouvreur = urllib.request.OpenerDirector()
    for gestionnaire in (urllib.request.ProxyHandler(), urllib.request.UnknownHandler(),
                         urllib.request.HTTPHandler(), urllib.request.HTTPSHandler(),
                         urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPRedirectHandler(),
                         urllib.request.HTTPErrorProcessor()):
        ouvreur.add_handler(gestionnaire)
    return ouvreur


def ouvrir(url: str) -> bytes:
    """Contenu d'une adresse http(s), TAILLE_MAX au plus."""
    if urlsplit(url).scheme.lower() not in ("http", "https"):
        raise ImageRefusee("adresse non prise en charge")
    requete = urllib.request.Request(url, headers={"User-Agent": "Hangar-wiki", "Accept": "image/*"})
    with _ouvreur().open(requete, timeout=DELAI) as reponse:
        longueur = reponse.headers.get("Content-Length", "")
        if longueur.isdigit() and int(longueur) > TAILLE_MAX:
            raise ImageRefusee(f"trop volumineuse (plus de {TAILLE_MAX // 1024 // 1024} Mo)")
        donnees = reponse.read(TAILLE_MAX + 1)
    if len(donnees) > TAILLE_MAX:
        raise ImageRefusee(f"trop volumineuse (plus de {TAILLE_MAX // 1024 // 1024} Mo)")
    return donnees


def telecharger_une(url: str, lire=None) -> tuple[bytes, str]:
    """(contenu, extension) de l'image, ou ImageRefusee."""
    try:
        donnees = (lire or ouvrir)(url)
    except ImageRefusee:
        raise
    except urllib.error.HTTPError as exc:
        raise ImageRefusee(f"le site répond « erreur {exc.code} »") from exc
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        raise ImageRefusee("site injoignable") from exc
    extension = type_image(donnees)
    if extension is None:
        raise ImageRefusee("ce n'est pas une image (PNG, JPEG, GIF, WebP, BMP ou AVIF)")
    return donnees, extension


def _nom(url: str, extension: str) -> str:
    nom = PurePosixPath(unquote(urlsplit(url).path)).name
    base = PurePosixPath(nom)
    if base.suffix.lower() in EXTENSIONS_EQUIVALENTES[extension]:
        extension, nom = base.suffix, base.stem
    elif base.suffix.lower() in {e for exts in EXTENSIONS_EQUIVALENTES.values() for e in exts}:
        nom = base.stem
    nom = INVALID_NAME_RE.sub("-", nom).strip(" .-")[:80].strip(" .-") or "image"
    if nom.upper() in NOMS_RESERVES_WINDOWS:
        nom = f"image-{nom}"
    return nom + extension


def _dossier(vault, cle_note: str) -> str:
    """Dossier des images : celui réglé dans Obsidian (``attachmentFolderPath``), sinon ``image/``."""
    try:
        reglage = json.loads((vault.root / ".obsidian" / "app.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        reglage = {}
    if isinstance(reglage, dict) and str(reglage.get("attachmentFolderPath") or "").strip():
        return vault.attachment_folder(cle_note)
    return DOSSIER_PAR_DEFAUT


def _enregistrer(racine: Path, dossier: str, nom: str, donnees: bytes) -> str:
    """Écrit l'image sous un nom libre (« nom 1.png »…) ; réutilise un fichier identique déjà là."""
    chemin = PurePosixPath(dossier, nom) if dossier else PurePosixPath(nom)
    for i in range(10_000):
        essai = chemin if i == 0 else chemin.with_name(f"{chemin.stem} {i}{chemin.suffix}")
        cible = racine / essai
        if cible.is_file():
            try:
                if cible.read_bytes() == donnees:
                    return essai.as_posix()
            except OSError:
                pass
            continue
        if cible.exists():
            continue
        cible.parent.mkdir(parents=True, exist_ok=True)
        cible.write_bytes(donnees)
        return essai.as_posix()
    raise ImageRefusee("aucun nom de fichier libre")


@dataclass
class BilanImages:
    images: int = 0  # images externes trouvées (une même adresse peut revenir dans plusieurs pages)
    pages: int = 0
    adresses: int = 0
    telechargees: list[str] = field(default_factory=list)  # fichiers écrits dans le coffre
    echecs: list[tuple[str, str]] = field(default_factory=list)  # (adresse, raison)
    pages_modifiees: list[str] = field(default_factory=list)
    pages_illisibles: list[str] = field(default_factory=list)


def telecharger(vault, auteur: str = "", lire=None, progression=None) -> BilanImages:
    """Télécharge les images externes du coffre et réécrit les pages qui les citent."""
    from .uploads import alleger_image

    racine = vault.root
    bilan = BilanImages()
    pages, adresses = {}, {}  # chemin de la page -> note ; adresse -> clé de la première page
    for note in vault.sorted_notes():
        trouvees = trouver(note.body)
        if trouvees:
            pages[note.path.as_posix()] = note
            bilan.images += len(trouvees)
            for occ in trouvees:
                adresses.setdefault(occ.url, note.key)
    bilan.pages, bilan.adresses = len(pages), len(adresses)

    fichiers = {}  # adresse -> chemin dans le coffre
    for i, (url, cle_note) in enumerate(adresses.items(), 1):
        if progression:
            progression(i, len(adresses), url)
        try:
            donnees, extension = telecharger_une(url, lire)
            chemin = _enregistrer(racine, _dossier(vault, cle_note), _nom(url, extension), donnees)
        except ImageRefusee as exc:
            bilan.echecs.append((url, str(exc)))
            continue
        except OSError:
            bilan.echecs.append((url, "écriture impossible dans le coffre"))
            continue
        if chemin not in bilan.telechargees:
            alleger_image(racine / chemin)
            bilan.telechargees.append(chemin)
        fichiers[url] = chemin
    if not fichiers:
        return bilan

    # Texte du lien : le nom seul s'il est unique dans le coffre (comme Obsidian), sinon le chemin.
    noms = Counter(PurePosixPath(rel).name.lower() for rel, _, _ in scan(racine) if not rel.lower().endswith(".md"))
    liens = {url: (PurePosixPath(chemin).name if noms[PurePosixPath(chemin).name.lower()] == 1 else chemin)
             for url, chemin in fichiers.items()}
    for rel in pages:
        fichier = racine / rel
        try:
            texte, _ = lire_note(fichier)  # relue juste avant d'écrire : la page a pu changer entre-temps
            nouveau = remplacer(texte, liens)
            if nouveau != texte:
                historique.sauvegarder(rel, texte, auteur or "images")
                fichier.write_text(nouveau, encoding="utf-8", newline="\n")
                bilan.pages_modifiees.append(rel)
        except OSError:
            bilan.pages_illisibles.append(rel)
    invalider()
    return bilan


# ---------------------------------------------------------------------------
# Téléchargement en arrière-plan (bouton de la page Importer)
# ---------------------------------------------------------------------------

_verrou = threading.Lock()
_etats: dict[str, dict] = {}  # dossier du coffre -> {"fil", "faites", "total", "bilan", "erreur"}


def etat(racine) -> dict | None:
    e = _etats.get(str(racine))
    if e is None:
        return None
    return dict(e, en_cours=e.get("fil") is not None and e["fil"].is_alive())


def lancer(auteur: str) -> bool:
    """Lance le téléchargement en arrière-plan ; faux s'il tourne déjà pour ce coffre.

    Réglage ``WIKI_IMAGES_FIL`` (faux pendant les tests : le travail se fait alors pendant la requête)."""
    app = current_app._get_current_object()
    racine = str(Path(app.config["WIKI_DIR"]))
    with _verrou:
        e = _etats.get(racine)
        if e is not None and e.get("fil") is not None and e["fil"].is_alive():
            return False
        _etats[racine] = e = {"fil": None, "faites": 0, "total": 0, "bilan": None, "erreur": ""}
        if not app.config.get("WIKI_IMAGES_FIL", not app.testing):
            _travail(app, auteur, e)
            return True
        e["fil"] = threading.Thread(target=_travail, args=(app, auteur, e), name="images-externes", daemon=True)
        e["fil"].start()
    return True


def _travail(app, auteur, e):
    from .vault import get_vault

    def progression(i, total, _url):
        e["faites"], e["total"] = i - 1, total

    with app.app_context():
        try:
            e["bilan"] = telecharger(get_vault(), auteur, progression=progression)
        except Exception:  # noqa: BLE001 - noté dans le journal du serveur, affiché dans la page Importer
            app.logger.exception("Téléchargement des images externes du wiki")
            e["erreur"] = "Le téléchargement s'est arrêté sur une erreur inattendue."
        e["faites"] = e["total"]
