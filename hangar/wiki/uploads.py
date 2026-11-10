"""Import de fichiers dans le coffre depuis le navigateur.

- Notes ``.md`` (ou ``.markdown``) : chacune devient une page, en gardant les
  sous-dossiers quand on dépose un dossier entier (un coffre Obsidian, par ex.).
- Pièces jointes intégrables dans les notes : images, PDF, audio, vidéo.

Tout le reste est refusé (HTML, scripts, JSON de QCM...). On n'écrit jamais
hors du coffre, ni dans ses dossiers cachés (``.obsidian``, ``.trash``).

Port de ``wiki/uploads.py`` de Revision : les fichiers reçus par Flask (Werkzeug)
passent par :class:`FichierEnvoye`, qui offre l'interface des fichiers de Django.
"""

import io
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from . import historique, latex
from .liens import wiki_url
from .render import AUDIO_EXTS, IMAGE_EXTS, VIDEO_EXTS
from .vault import IGNORED_DIRS, INVALID_NAME_RE

NOTE_EXTS = {".md", ".markdown"}
LATEX_EXTS = {".tex", ".latex"}
ATTACHMENT_EXTS = IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS | {".pdf"}
MAX_NOTE_SIZE = 5 * 1024 * 1024
MAX_ATTACHMENT_SIZE = 100 * 1024 * 1024  # PDF scannés compris
# Images allégées à l'import : réduites à IMAGE_COTE_MAX pixels de côté au plus et
# recompressées dans leur format (une photo de téléphone de 4 Mo tient en 400 Ko).
IMAGE_COTE_MAX = 2000
IMAGE_QUALITE = 85
IMAGES_ALLEGEES = {".png", ".jpg", ".jpeg", ".webp"}
FORBIDDEN_CHARS = '/ \\ : * ? " < > | # ^ [ ]'


class FichierEnvoye:
    """Fichier reçu (``werkzeug.datastructures.FileStorage``) vu comme un fichier envoyé
    de Django : ``name``, ``size``, ``read()`` et ``chunks()``."""

    def __init__(self, storage):
        self.storage = storage
        self.name = storage.filename or ""
        flux = storage.stream
        try:
            debut = flux.tell()
            flux.seek(0, io.SEEK_END)
            self.size = flux.tell() - debut
            flux.seek(debut)
        except (OSError, AttributeError, ValueError):
            self._contenu = flux.read()
            self.size = len(self._contenu)

    def read(self):
        if hasattr(self, "_contenu"):
            return self._contenu
        return self.storage.stream.read()

    def chunks(self, taille=1024 * 1024):
        if hasattr(self, "_contenu"):
            yield self._contenu
            return
        while True:
            bloc = self.storage.stream.read(taille)
            if not bloc:
                break
            yield bloc


@dataclass
class Result:
    name: str  # chemin du fichier tel qu'envoyé
    ok: bool
    message: str
    ignored: bool = False
    path: str = ""  # chemin final dans le coffre
    url: str = ""
    link: str = ""  # texte à mettre entre [[ ]]
    is_note: bool = False
    claude: str = ""  # adresse du suivi, si Claude transforme ce cours
    # Cours LaTeX : son texte d'origine (le coffre ne garde que la page convertie).
    source_latex: str = field(default="", repr=False)

    def as_dict(self):
        d = asdict(self)
        del d["source_latex"]
        return d


def _parts(raw):
    return [p.strip() for p in str(raw).replace("\\", "/").split("/") if p.strip()]


def _hidden(part):
    return part.startswith(".") or part in IGNORED_DIRS


def clean_folder(raw):
    """Dossier de destination saisi (``""`` = racine du coffre), ou None s'il est invalide."""
    parts = _parts(raw)
    if any(_hidden(p) or INVALID_NAME_RE.search(p) for p in parts):
        return None
    return "/".join(parts)


def _free_path(root, vault, path, is_note):
    """« image.png » déjà pris → « image 1.png », « image 2.png »..."""
    for i in range(1, 10_000):
        candidate = path.with_name(f"{path.stem} {i}{path.suffix}")
        key = str(candidate.with_suffix("")).lower() if is_note else str(candidate).lower()
        taken = vault.notes if is_note else vault.attachments
        if key not in taken and not (root / candidate).exists():
            return candidate
    raise ValueError("aucun nom libre")


def import_file(vault, upload, relative, folder="", replace=False, rename=False, auteur=""):
    """Écrit un fichier envoyé dans ``folder/relative``. Renvoie un :class:`Result`.

    Un fichier qui existe déjà est refusé, sauf avec ``replace`` (écrasé : pour une
    note, l'ancien texte part dans l'historique) ou ``rename`` (enregistré sous un
    nom libre, comme Obsidian pour les images collées).
    """
    root = vault.root
    parts = _parts(relative or upload.name)
    shown = "/".join(parts) or str(upload.name)
    if not parts:
        return Result(shown, False, "nom de fichier vide")
    if any(_hidden(p) for p in parts):
        return Result(shown, False, "ignoré : fichier ou dossier caché (.obsidian, .trash…)", ignored=True)
    if any(INVALID_NAME_RE.search(p) for p in parts):
        return Result(shown, False, f"nom invalide : caractères interdits {FORBIDDEN_CHARS}")

    suffix = PurePosixPath(parts[-1]).suffix.lower()
    if suffix in NOTE_EXTS or suffix in LATEX_EXTS:
        is_note, limit = True, MAX_NOTE_SIZE
        parts[-1] = PurePosixPath(parts[-1]).stem + ".md"
    elif suffix in ATTACHMENT_EXTS:
        is_note, limit = False, MAX_ATTACHMENT_SIZE
    else:
        return Result(shown, False, "type de fichier non accepté (notes .md ou .tex, images, PDF, audio ou vidéo)")
    if upload.size > limit:
        return Result(shown, False, f"trop volumineux (maximum {limit // 1024 // 1024} Mo)")

    if is_note:
        try:
            text = upload.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            return Result(shown, False, "ce n'est pas un fichier texte UTF-8")
        text = text.replace("\r\n", "\n")
        source_latex = text if suffix in LATEX_EXTS else ""
        if suffix in LATEX_EXTS:
            try:
                text = latex.convert(text)
            except (RecursionError, ValueError, IndexError):
                return Result(shown, False, "ce LaTeX n'a pas pu être converti")

    path = PurePosixPath(*_parts(folder), *parts)
    existing = (
        vault.notes.get(str(path.with_suffix("")).lower()) if is_note else vault.attachments.get(str(path).lower())
    )
    message = "converti depuis LaTeX" if suffix in LATEX_EXTS else "importé"
    if existing is not None or (root / path).exists():
        if rename:
            path = _free_path(root, vault, path, is_note)
            message = f"importé sous le nom « {path.name} »"
        elif replace:
            # Même fichier à une casse près : on écrase celui-là plutôt que d'en créer un second.
            path = existing.path if existing is not None else path
            message = "remplacé"
        else:
            return Result(shown, False, "existe déjà (cochez « Remplacer » pour l'écraser)")

    target = root / path
    if not target.resolve().is_relative_to(root.resolve()):
        return Result(shown, False, "chemin invalide")
    if target.is_dir():
        return Result(shown, False, "un dossier porte déjà ce nom")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if is_note:
            if target.is_file():
                historique.sauvegarder(path.as_posix(), target.read_text(encoding="utf-8", errors="replace"), auteur)
            target.write_text(text, encoding="utf-8", newline="\n")
        else:
            with open(target, "wb") as out:
                for chunk in upload.chunks():
                    out.write(chunk)
    except OSError:
        # Ex. : un fichier porte le nom d'un des dossiers du chemin.
        return Result(shown, False, "écriture impossible à cet emplacement")
    if not is_note:
        gain = alleger_image(target)
        if gain:
            message += f" (image allégée : {taille_lisible(gain[0])} → {taille_lisible(gain[1])})"
    return Result(shown, True, message, path=path.as_posix(), is_note=is_note,
                  source_latex=source_latex if is_note else "")


def taille_lisible(octets):
    if octets >= 1024 * 1024:
        return f"{octets / 1024 / 1024:.1f} Mo".replace(".", ",")
    return f"{max(1, round(octets / 1024))} Ko"


def alleger_image(chemin):
    """Réduit une image trop grande (côté > IMAGE_COTE_MAX) et la recompresse dans son
    format, en gardant le nom du fichier (les liens des notes restent valables).
    Renvoie (taille avant, taille après), ou None si l'image est gardée telle quelle
    (petite et déjà bien compressée, animation, GIF ou SVG, fichier illisible...)."""
    chemin = Path(chemin)
    if chemin.suffix.lower() not in IMAGES_ALLEGEES:
        return None
    try:
        from PIL import Image, ImageOps

        avant = chemin.stat().st_size
        with Image.open(chemin) as image:
            if getattr(image, "n_frames", 1) > 1:
                return None  # animation : gardée intacte
            image = ImageOps.exif_transpose(image)  # orientation des photos
            reduite = max(image.size) > IMAGE_COTE_MAX
            if reduite:
                image.thumbnail((IMAGE_COTE_MAX, IMAGE_COTE_MAX), Image.Resampling.LANCZOS)
            tampon = io.BytesIO()
            if chemin.suffix.lower() in (".jpg", ".jpeg"):
                if image.mode not in ("RGB", "L"):
                    image = image.convert("RGB")
                image.save(tampon, "JPEG", quality=IMAGE_QUALITE, optimize=True, progressive=True)
            elif chemin.suffix.lower() == ".png":
                image.save(tampon, "PNG", optimize=True)
            else:
                image.save(tampon, "WEBP", quality=IMAGE_QUALITE, method=4)
    except Exception:  # noqa: BLE001 — image illisible ou démesurée : gardée telle quelle
        return None
    apres = tampon.getbuffer().nbytes
    if apres >= avant or (not reduite and apres > avant * 0.9):
        return None  # gain négligeable
    chemin.write_bytes(tampon.getvalue())
    return avant, apres


def finish(vault, results):
    """Complète les fichiers importés avec leur adresse et le texte du lien,
    une fois le coffre relu (``vault`` doit les contenir)."""
    for result in results:
        if not result.ok:
            continue
        if result.is_note:
            item = vault.notes.get(result.path.removesuffix(".md").lower())
            if item is not None:
                result.url = wiki_url("wiki.page", item.key)
        else:
            item = vault.attachments.get(result.path.lower())
            if item is not None:
                result.url = item.view_url
        if item is not None:
            result.link = vault.link_for(item)
    return results
