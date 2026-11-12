"""Indexation d'un coffre Obsidian : notes .md, pièces jointes, liens et tags.

Le coffre est relu automatiquement dès qu'un fichier est ajouté, modifié ou
supprimé : il suffit de déposer un fichier .md dans le dossier pour créer une
nouvelle page.

Port de ``wiki/vault.py`` de Revision : seules les dépendances à Django changent
(``settings`` → ``current_app.config``, ``reverse`` → ``url_for``).
"""

import codecs
import html
import json
import logging
import os
import posixpath
import re
import threading
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cached_property, lru_cache
from pathlib import Path, PurePosixPath

import yaml
from flask import current_app, has_app_context

from .. import heure
from ..texte import UN_POUR_UN, cle_tri
from .liens import wiki_url

journal = logging.getLogger(__name__)

IGNORED_DIRS = {".obsidian", ".trash", ".git", "__pycache__"}
HOME_CANDIDATES = ("index", "home", "accueil", "readme")

FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)
WIKILINK_RE = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")
MDLINK_RE = re.compile(r"\]\(<?([^)\s>]+?\.md)(?:#[^)\s>]*)?>?\)")
COMMENT_RE = re.compile(r"%%.*?%%", re.S)
# Un tag Obsidian : #mot, #mot-composé, #parent/enfant (pas uniquement des chiffres).
TAG_RE = re.compile(r"(?<![\w&#/\\(\[\"'=:])#([\w/-]*[^\W\d][\w/-]*|[\w/-]*[-/][\w/-]*)")
# Blocs de code (``` ou ~~~) et code en ligne, où la syntaxe Obsidian est ignorée.
_BLOC_DE_CODE = r"^[ \t]*(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^[ \t]*(?P=fence)[ \t]*$"
CODE_RE = re.compile(_BLOC_DE_CODE + r"|(?P<tick>`+)(?:[^`\n]|\n(?![ \t]*\n))+?(?P=tick)(?!`)", re.M | re.S)
# Blocs de code seuls (sans le code en ligne) : ce qui est entre eux commence toujours en début de ligne.
FENCED_CODE_RE = re.compile(_BLOC_DE_CODE, re.M | re.S)


def escape(text):
    return html.escape(str(text))


def _config(name, default=None):
    """Réglage de l'application Flask (``WIKI_HOME``, ``WIKI_VERIF_INTERVALLE``…)."""
    return current_app.config.get(name, default) if has_app_context() else default


class _FoldTable(dict):
    """Table de ``str.translate`` remplie à la demande : chaque caractère n'est calculé qu'une fois."""

    def __missing__(self, code):
        ch = chr(code)
        if ch in UN_POUR_UN:  # apostrophes et guillemets typographiques, espaces insécables
            self[code] = UN_POUR_UN[ch]
            return UN_POUR_UN[ch]
        base = "".join(c for c in unicodedata.normalize("NFD", ch) if not unicodedata.combining(c))
        folded = (base if len(base) == 1 else ch).lower()
        # Garde la longueur (« İ ».lower() donne 2 caractères) pour que les positions concordent.
        self[code] = folded = folded if len(folded) == 1 else ch
        return folded


_FOLD_TABLE = _FoldTable()


def fold(text):
    """Minuscules sans accents, en gardant la longueur (é→e, ’→') pour repérer les extraits.

    Permet de chercher « reseau d'acces » et de trouver « Réseau d’accès ». « œ » et « æ »
    restent tels quels (la longueur changerait) : ``trouve``, ``compte`` et ``positions``
    les font correspondre à « oe » et « ae ».
    """
    return text.translate(_FOLD_TABLE)


_LIGATURE_RE = re.compile("oe|œ|ae|æ")


@lru_cache(maxsize=512)
def _motif(mot):
    """Motif d'un mot replié par ``fold`` où « oe » et « œ » (« ae » et « æ ») se valent :
    « noeud » trouve « nœud » et inversement. ``None`` si le mot n'en contient pas."""
    if not _LIGATURE_RE.search(mot):
        return None
    morceaux, debut = [], 0
    for m in _LIGATURE_RE.finditer(mot):
        morceaux += [re.escape(mot[debut:m.start()]), "(?:oe|œ)" if m.group() in ("oe", "œ") else "(?:ae|æ)"]
        debut = m.end()
    morceaux.append(re.escape(mot[debut:]))
    return re.compile("".join(morceaux))


def trouve(mot, texte):
    """Vrai si le mot replié (``fold``) figure dans le texte replié, ligatures comprises."""
    motif = _motif(mot)
    return mot in texte if motif is None else motif.search(texte) is not None


def compte(mot, texte, debut=0, fin=None):
    """Nombre d'occurrences du mot replié dans ``texte[debut:fin]``, ligatures comprises."""
    fin = len(texte) if fin is None else fin
    motif = _motif(mot)
    return texte.count(mot, debut, fin) if motif is None else len(motif.findall(texte, debut, fin))


def positions(mot, texte):
    """(début, fin) de chaque occurrence du mot replié dans le texte replié."""
    return [m.span() for m in re.finditer(_motif(mot) or re.escape(mot), texte)] if mot else []


def map_outside_code(text, fn, pattern=CODE_RE):
    """Applique ``fn`` aux portions de ``text`` situées hors des blocs de code (et du code
    en ligne, sauf avec ``pattern=FENCED_CODE_RE``)."""
    out, pos = [], 0
    for m in pattern.finditer(text):
        out.append(fn(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(fn(text[pos:]))
    return "".join(out)


def _repli_cp1252(erreur):
    """Octets qui ne sont pas de l'UTF-8 : lus comme du Windows-1252 (« ANSI » du Bloc-notes)."""
    return erreur.object[erreur.start:erreur.end].decode("cp1252", errors="replace"), erreur.end


codecs.register_error("hangar-cp1252", _repli_cp1252)


def decoder(donnees):
    """Texte d'une note et son encodage d'origine : ``"utf-8"`` (le cas normal), ``"utf-16"``
    (« Unicode » du Bloc-notes, repéré par son BOM) ou ``"cp1252"`` (« ANSI » d'un vieux
    Bloc-notes ; dans un fichier mélangé, les passages en UTF-8 restent lus en UTF-8)."""
    if donnees.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return donnees.decode("utf-16", errors="replace"), "utf-16"
    try:
        return donnees.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return donnees.decode("utf-8", errors="hangar-cp1252"), "cp1252"


def lire_note(chemin):
    """``(texte, encodage)`` d'une note du coffre, quel que soit son encodage (voir :func:`decoder`), fins de
    ligne Windows ramenées à « \\n » comme le fait ``Path.read_text`` (même texte, donc mêmes empreintes et
    mêmes comparaisons qu'avant pour une note en UTF-8).
    Lève ``OSError`` si le système refuse la lecture (fichier verrouillé, OneDrive hors connexion…)."""
    texte, encodage = decoder(Path(chemin).read_bytes())
    return texte.replace("\r\n", "\n").replace("\r", "\n"), encodage


def split_frontmatter(text):
    """Sépare les propriétés YAML (entre ``---``) du corps de la note."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, text[m.end():]


def as_list(value, sep=r"[,\s]+"):
    if value is None:
        return []
    if isinstance(value, str):
        return [v for v in re.split(sep, value) if v]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v is not None]
    return [str(value)]


INVALID_NAME_RE = re.compile(r'[\\:*?"<>|#^\[\]\x00-\x1f]')


def clean_key(raw):
    """Valide un chemin de note saisi (``Dossier/Ma note``), ou renvoie None."""
    parts = [p.strip() for p in raw.replace("\\", "/").strip().removesuffix(".md").split("/")]
    parts = [p for p in parts if p]
    if not parts or any(p.startswith(".") or INVALID_NAME_RE.search(p) for p in parts):
        return None
    return "/".join(parts)


def parse_link_target(raw):
    """``'Page#Titre|Alias'`` -> ``('Page', 'Titre', 'Alias')``."""
    target, _, alias = raw.partition("|")
    target = target.rstrip("\\").strip()  # [[Page\|alias]] dans les tableaux
    page, _, fragment = target.partition("#")
    return page.strip(), fragment.strip(), alias.strip()


@dataclass(eq=False)
class Attachment:
    path: PurePosixPath

    @property
    def name(self):
        return self.path.name

    @property
    def suffix(self):
        return self.path.suffix.lower()

    @cached_property
    def url(self):
        return wiki_url("wiki.file", str(self.path))

    @cached_property
    def view_url(self):
        """Adresse pour consulter le fichier : le lecteur intégré pour un PDF."""
        return wiki_url("wiki.pdf", str(self.path)) if self.suffix == ".pdf" else self.url


@dataclass(eq=False)
class Note:
    path: PurePosixPath
    mtime: float
    meta: dict
    body: str

    @property
    def name(self):
        return self.path.stem

    @property
    def key(self):
        """Chemin dans le coffre sans l'extension .md (sert d'URL)."""
        return str(self.path.parent / self.path.stem) if self.path.parent.parts else self.path.stem

    @property
    def folder(self):
        return str(self.path.parent) if self.path.parent.parts else ""

    @property
    def folders(self):
        return self.path.parent.parts

    @property
    def modified(self):
        """Date de modification, en heure naïve de l'agenda (hangar.heure, comme le reste de Hangar), quel que
        soit le fuseau de la machine."""
        return heure.vers_agenda(datetime.fromtimestamp(self.mtime, timezone.utc))

    @cached_property
    def url(self):
        return wiki_url("wiki.page", self.key)

    @cached_property
    def title(self):
        return str(self.meta.get("title") or self.name)

    @cached_property
    def aliases(self):
        return as_list(self.meta.get("aliases") or self.meta.get("alias"), sep=r"\s*,\s*")

    @cached_property
    def clean_body(self):
        """Corps sans les commentaires %% ... %%."""
        return map_outside_code(self.body, lambda s: COMMENT_RE.sub("", s))

    @cached_property
    def folded_title(self):
        """Titre, chemin et alias repliés, pour la recherche."""
        return fold(f"{self.title} {self.key} {' '.join(self.aliases)}")

    @cached_property
    def folded_body(self):
        """Corps replié (même longueur que ``clean_body``), pour la recherche."""
        return fold(self.clean_body)

    @cached_property
    def _text_outside_code(self):
        """Portions de ``clean_body`` hors blocs de code, découpées une seule fois."""
        segments = []
        map_outside_code(self.clean_body, lambda s: segments.append(s) or s)
        return segments

    @cached_property
    def tags(self):
        tags = {t.lstrip("#") for t in as_list(self.meta.get("tags") or self.meta.get("tag"))}
        for segment in self._text_outside_code:
            tags.update(TAG_RE.findall(segment))
        return sorted(t for t in tags if t)

    @cached_property
    def link_targets(self):
        """Pages ciblées par les liens [[...]] et [texte](page.md) de la note."""
        targets = []
        for segment in self._text_outside_code:
            targets.extend(parse_link_target(m.group(2))[0] for m in WIKILINK_RE.finditer(segment))
            targets.extend(m.group(1) for m in MDLINK_RE.finditer(segment))
        return [t for t in targets if t]

    @cached_property
    def properties(self):
        """Propriétés à afficher (hors titre, tags et alias)."""
        hidden = {"title", "tags", "tag", "aliases", "alias", "cssclasses", "cssclass"}
        return [
            (key, ", ".join(as_list(value)) if isinstance(value, (list, tuple)) else value)
            for key, value in self.meta.items()
            if key not in hidden
        ]


class Vault:
    def __init__(self, root, files, previous=None):
        self.root = Path(root)
        self.notes = {}  # chemin sans .md, en minuscules -> Note
        self.attachments = {}  # chemin en minuscules -> Attachment
        self._by_name = defaultdict(list)  # nom, alias ou nom de fichier en minuscules
        self._rendered = {}
        # (chemin, date, taille) -> Note : seules les notes modifiées depuis ``previous`` sont relues.
        self._parsed = {}
        # Notes que le système refuse de lire (fichier verrouillé par un antivirus, « en ligne
        # uniquement » avec OneDrive hors connexion…) : ignorées, ou gardées dans leur dernière
        # version lue (``_gardees``, hors de ``_parsed`` pour être relues au prochain passage).
        self.illisibles = []
        self._gardees = {}
        self.lu_le = time.monotonic()
        meme_coffre = previous is not None and previous.root == self.root
        reusable = previous._parsed if meme_coffre else {}
        anciennes = None

        for rel, mtime, size in files:
            path = PurePosixPath(rel)
            if path.suffix.lower() == ".md":
                note = reusable.get((rel, mtime, size))
                if note is None:
                    try:
                        text, _ = lire_note(self.root / path)
                    except OSError:
                        self.illisibles.append(rel)
                        if anciennes is None:
                            anciennes = {cle[0]: n for cle, n in reusable.items()}
                            anciennes.update(previous._gardees if meme_coffre else {})
                        note = anciennes.get(rel)
                        if note is None:
                            continue
                        self._gardees[rel] = note
                    else:
                        meta, body = split_frontmatter(text.lstrip("﻿").replace("\r\n", "\n"))
                        note = Note(path, mtime / 1e9, meta, body)
                        self._parsed[(rel, mtime, size)] = note
                else:
                    self._parsed[(rel, mtime, size)] = note
                self.notes[note.key.lower()] = note
                self._by_name[note.name.lower()].append(note)
                for alias in note.aliases:
                    self._by_name[alias.lower()].append(note)
            else:
                attachment = Attachment(path)
                self.attachments[rel.lower()] = attachment
                self._by_name[path.name.lower()].append(attachment)
        nouvelles = set(self.illisibles) - set(previous.illisibles if meme_coffre else ())
        if nouvelles:
            journal.warning("Wiki : lecture refusée par le système pour %s", ", ".join(sorted(nouvelles)))

        self.backlinks = defaultdict(set)  # clé de note -> notes qui la citent
        self.attachment_backlinks = defaultdict(set)  # chemin de pièce jointe -> notes qui la citent
        self.unresolved = defaultdict(set)  # cible inexistante -> notes qui la citent
        self.tags = defaultdict(set)  # tag (et ses parents) -> notes
        for note in self.notes.values():
            for target in note.link_targets:
                found = self.resolve(target, note)
                if isinstance(found, Note):
                    if found is not note:
                        self.backlinks[found.key.lower()].add(note)
                elif isinstance(found, Attachment):
                    self.attachment_backlinks[str(found.path).lower()].add(note)
                elif found is None:
                    self.unresolved[target.lower()].add(note)
            for tag in note.tags:
                parts = tag.lower().split("/")
                for i in range(1, len(parts) + 1):
                    self.tags["/".join(parts[:i])].add(note)

    def resolve(self, target, source=None):
        """Trouve la note ou la pièce jointe désignée par un lien, comme Obsidian.

        Ordre : chemin relatif à la note source, chemin depuis la racine du
        coffre, puis nom de fichier (ou alias) n'importe où dans le coffre, en
        privilégiant le dossier de la note source puis le chemin le plus court.
        """
        target = target.strip().replace("\\", "/").lstrip("/")
        if not target:
            return source
        low = target.lower()
        paths = [low]
        if source is not None and source.folder:
            relative = posixpath.normpath(posixpath.join(source.folder.lower(), low))
            if not relative.startswith(".."):
                paths.insert(0, relative)
        for path in paths:
            note = self.notes.get(path.removesuffix(".md"))
            if note is not None:
                return note
            if path in self.attachments:
                return self.attachments[path]

        name = low.rsplit("/", 1)[-1]
        candidates = self._by_name.get(name.removesuffix(".md"), []) + self._by_name.get(name, [])
        candidates = [
            c for c in candidates
            if "/" not in low or str(c.path).lower().removesuffix(".md").endswith(low.removesuffix(".md"))
        ]
        if not candidates:
            return None
        folder = source.folder.lower() if source is not None else ""
        return min(
            candidates,
            key=lambda c: (str(c.path.parent).lower() != (folder or "."), len(c.path.parts), str(c.path).lower()),
        )

    def home(self):
        names = (str(_config("WIKI_HOME", "") or "").lower(),) + HOME_CANDIDATES
        for name in names:
            if name and name in self.notes:
                return self.notes[name]
        return None

    def sorted_notes(self):
        return sorted(self.notes.values(), key=lambda n: cle_tri(n.key))

    def recent_notes(self, limit=10):
        return sorted(self.notes.values(), key=lambda n: n.mtime, reverse=True)[:limit]

    def notes_with_tag(self, tag):
        return sorted(self.tags.get(tag.lower().lstrip("#"), ()), key=lambda n: cle_tri(n.title))

    def all_tags(self):
        return sorted(((tag, len(notes)) for tag, notes in self.tags.items()), key=lambda t: cle_tri(t[0]))

    def pdfs(self):
        return sorted((a for a in self.attachments.values() if a.suffix == ".pdf"), key=lambda a: cle_tri(str(a.path)))

    @cached_property
    def tree(self):
        """Arborescence des notes (et des PDF, lisibles dans le wiki) pour la barre latérale."""
        root = {"name": "", "path": "", "folders": {}, "notes": [], "files": []}

        def folder(parts):
            node = root
            for part in parts:
                node = node["folders"].setdefault(
                    part,
                    {"name": part, "path": f"{node['path']}{part}/", "folders": {}, "notes": [], "files": []},
                )
            return node

        for note in self.sorted_notes():
            folder(note.folders)["notes"].append(note)
        for pdf in self.pdfs():
            folder(pdf.path.parent.parts)["files"].append(pdf)

        def freeze(node):
            node["folders"] = [freeze(f) for _, f in sorted(node["folders"].items(), key=lambda i: cle_tri(i[0]))]
            node["count"] = len(node["notes"]) + len(node["files"]) + sum(f["count"] for f in node["folders"])
            return node

        return freeze(root)

    def folders(self):
        """Tous les dossiers du coffre (chemins relatifs)."""
        found = set()
        for dirpath, dirnames, _ in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")]
            found.update(Path(dirpath, d).relative_to(self.root).as_posix() for d in dirnames)
        return sorted(found, key=cle_tri)

    def link_for(self, item):
        """Texte à mettre entre [[ ]] : le nom seul suffit, sauf si un autre
        fichier du même genre porte le même nom (on donne alors le chemin)."""
        if isinstance(item, Note):
            same = [n for n in self._by_name[item.name.lower()] if isinstance(n, Note)]
            return item.name if len(same) == 1 else item.key
        same = [a for a in self._by_name[item.name.lower()] if isinstance(a, Attachment)]
        return item.name if len(same) == 1 else str(item.path)

    def link_choices(self):
        """Notes proposées dans l'éditeur, avec le texte à insérer entre [[ ]]."""
        return [
            {"link": self.link_for(note), "title": note.title, "folder": note.folder, "aliases": note.aliases}
            for note in self.sorted_notes()
        ]

    def attachment_choices(self, suffixes):
        """Pièces jointes intégrables (images, PDF...) proposées après ![[ dans l'éditeur."""
        found = [a for a in self.attachments.values() if a.suffix in suffixes]
        return [
            {"link": self.link_for(a), "folder": str(a.path.parent) if a.path.parent.parts else "", "url": a.view_url}
            for a in sorted(found, key=lambda a: cle_tri(str(a.path)))
        ]

    def attachment_folder(self, note_key):
        """Dossier où ranger une image collée dans une note, réglé comme dans
        Obsidian (``attachmentFolderPath`` de ``.obsidian/app.json``) : racine du
        coffre par défaut, ``./`` pour le dossier de la note, ``./sous-dossier``
        ou un chemin depuis la racine."""
        try:
            config = json.loads((self.root / ".obsidian" / "app.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = {}
        setting = config.get("attachmentFolderPath") if isinstance(config, dict) else None
        setting = str(setting or "/").strip()
        note_folder = note_key.rpartition("/")[0]
        if setting == "." or setting.startswith("./"):
            folder = f"{note_folder}/{setting[2:]}"
        else:
            folder = setting
        return clean_key(folder) or ""

    def render(self, note):
        """HTML de la note (mis en cache jusqu'à la prochaine modification du coffre)."""
        if note.key not in self._rendered:
            from .render import render_note

            self._rendered[note.key] = render_note(self, note)
        return self._rendered[note.key]

    def search(self, query, limit=50):
        """Recherche insensible à la casse, aux accents, à la forme de l'apostrophe et aux
        ligatures (« noeud d'acces » trouve « Nœud d’accès »). Renvoie (note, extrait HTML)."""
        words = [fold(w) for w in query.split() if w]
        if not words:
            return []
        results = []
        for note in self.notes.values():
            title, body = note.folded_title, note.folded_body
            in_title = [w for w in words if trouve(w, title)]
            if not all(w in in_title or trouve(w, body) for w in words):
                continue
            score = 5 * len(in_title) + sum(compte(w, body) for w in words)
            results.append((score, note))
        results.sort(key=lambda r: (-r[0], cle_tri(r[1].title)))
        return [(note, self._snippet(note.clean_body, note.folded_body, words)) for _, note in results[:limit]]

    @staticmethod
    def _snippet(text, folded, words, width=240):
        """Extrait du corps centré sur la première occurrence, termes surlignés."""
        spans = sorted(span for w in words for span in positions(w, folded))
        if not spans:
            snippet = text[:width]
            return escape(snippet) + ("…" if len(text) > width else "")
        start = max(spans[0][0] - width // 4, 0)
        end = min(start + width, len(text))
        out, cursor = [], start
        for s, e in spans:
            if s < start or e > end:
                continue
            out.append(escape(text[cursor:s]))
            out.append(f"<mark>{escape(text[s:e])}</mark>")
            cursor = e
        out.append(escape(text[cursor:end]))
        snippet = "".join(out).replace("\n", " ")
        return ("… " if start else "") + snippet + (" …" if end < len(text) else "")


def scan(root):
    """Liste (chemin relatif, date de modification, taille) des fichiers du coffre.

    Parcours avec ``os.scandir`` et des chaînes (pas un ``Path`` par fichier) : plusieurs fois plus
    rapide qu'``os.walk`` + ``Path.stat()``, et sous Windows ``DirEntry.stat()`` ne refait pas d'appel
    système. Même résultat qu'avant : dossiers cachés et ``IGNORED_DIRS`` exclus, liens vers un dossier
    non suivis, fichiers cachés ou illisibles ignorés. Partagé par le wiki et les QCM (``qcm/coffre.py``).
    """
    files = []
    pile = [("", os.fspath(root))]
    while pile:
        prefixe, dossier = pile.pop()
        try:
            entrees = os.scandir(dossier)
        except OSError:
            continue
        with entrees:
            for entree in entrees:
                nom = entree.name
                if nom.startswith("."):
                    continue
                try:
                    est_dossier = entree.is_dir()
                except OSError:
                    est_dossier = False
                if est_dossier:
                    if nom not in IGNORED_DIRS and not entree.is_symlink():
                        pile.append((f"{prefixe}{nom}/", entree.path))
                    continue
                try:
                    stat = entree.stat()
                except OSError:
                    continue
                files.append((prefixe + nom, stat.st_mtime_ns, stat.st_size))
    return tuple(sorted(files))


def intervalle_verif():
    """Délai (secondes) entre deux parcours du coffre, le même pour les pages et les QCM :
    ``WIKI_VERIF_INTERVALLE`` (2 s par défaut). ``QCM_VERIF_INTERVALLE``, ancien réglage propre aux
    QCM, reste accepté : s'il est plus court, il s'applique à tout le coffre."""
    valeurs = [_config(nom) for nom in ("WIKI_VERIF_INTERVALLE", "QCM_VERIF_INTERVALLE")]
    valeurs = [float(v) for v in valeurs if v is not None]
    return min(valeurs) if valeurs else 2.0


# Détection des changements du coffre. Parcourir tout le coffre à chaque requête
# coûte ; le parcours, partagé par les pages et les QCM, est donc fait au plus toutes les
# ``intervalle_verif()`` secondes. Les écritures faites par le wiki lui-même sont
# vues tout de suite : pendant une requête POST le coffre est toujours relu
# (crochets de requête du blueprint, voir views.py), et invalider() force le prochain parcours (après
# la requête, ou après une écriture du fil de Claude).
_scan_lock = threading.Lock()
_scans = {}  # dossier -> {"signature", "quand", "generation"} (coffre, espaces personnels)
_generation = 0
_local = threading.local()


def invalider():
    """Le prochain accès au coffre le relira (un fichier vient d'être écrit)."""
    global _generation
    with _scan_lock:
        _generation += 1


class toujours_verifier:
    """Pendant ce bloc (une requête qui écrit), le coffre est relu à chaque accès."""

    def __enter__(self):
        _local.forcer = getattr(_local, "forcer", 0) + 1

    def __exit__(self, *exc):
        _local.forcer -= 1
        invalider()


def signature_coffre(root, intervalle=None, suivre_ecritures=True):
    """Liste (chemin, date, taille) des fichiers du coffre, parcourue au plus toutes les
    ``intervalle_verif()`` secondes. Un seul parcours, partagé par les pages et les QCM : la liste
    des sets de QCM (``qcm/coffre.signature``) passe ``suivre_ecritures=False`` (pendant une requête
    qui écrit, elle n'est relue qu'après un ``invalider()``, pas à chaque accès)."""
    if intervalle is None:
        intervalle = intervalle_verif()
    maintenant = time.monotonic()
    root = Path(root)
    forcer = suivre_ecritures and getattr(_local, "forcer", 0)
    with _scan_lock:
        c = _scans.get(root)
        if (c is not None and c["generation"] == _generation and not forcer
                and maintenant - c["quand"] < intervalle):
            return c["signature"]
        generation = _generation
    signature = scan(root) if root.is_dir() else ()
    with _scan_lock:
        _scans[root] = {"signature": signature, "quand": maintenant, "generation": generation}
    return signature


_lock = threading.Lock()
_cache = {"root": None, "signature": None, "vault": None}


def get_vault():
    """Renvoie le coffre, réindexé seulement si un fichier a changé (ou, tant qu'une note est
    illisible, au plus toutes les ``intervalle_verif()`` secondes, pour la relire dès que possible)."""
    root = Path(current_app.config["WIKI_DIR"])
    signature = signature_coffre(root)
    with _lock:
        vault = _cache["vault"]
        relire = vault is not None and vault.illisibles and (
            time.monotonic() - vault.lu_le >= intervalle_verif())
        if vault is None or relire or _cache["root"] != root or _cache["signature"] != signature:
            _cache.update(root=root, signature=signature, vault=Vault(root, signature, previous=_cache["vault"]))
        return _cache["vault"]
