"""Historique des pages : versions précédentes gardées hors du coffre, différences,
restauration.

À chaque enregistrement qui modifie une page existante (éditeur, import avec
remplacement, page écrite par l'IA, corbeille, restauration), le texte **précédent** est
copié dans ``DATA_DIR/wiki_versions/<chemin de la page>/`` ; la page elle-même reste la
version courante. Deux sortes de versions, d'après le séparateur du nom de fichier :

- ``<horodatage>-<auteur>.md`` : **point de repère**, gardé avant une opération qui
  remplace ou retire le texte (corbeille, restauration, import, IA). Toujours créé, jamais
  éclairci. C'est aussi le format de toutes les versions écrites avant la rétention par
  paliers : l'historique déjà sur le disque est gardé tel quel.
- ``<horodatage>_<auteur>.md`` : version d'**édition** (éditeur, citation ajoutée depuis un
  PDF). Les enregistrements rapprochés sont regroupés (pas de nouvelle version tant que la
  dernière, du même auteur, a moins de REGROUPEMENT) puis, en vieillissant, éclaircis selon
  PALIERS : on garde dans chaque tranche la plus ancienne version, c'est-à-dire l'état de la
  page avant les changements de cette tranche.

Plafonds par page : MAX_VERSIONS fichiers et MAX_OCTETS ; au-delà, partent d'abord les
versions d'édition de moins d'une heure qui seront de toute façon éclaircies dans l'heure,
puis les plus anciennes, sauf la toute première (le texte d'origine de la page) et la plus
récente.

Horloge qui recule (passage à l'heure d'hiver, heure du PC corrigée) : une dernière version
datée « dans le futur » est regroupée comme une version récente, sinon chaque enregistrement
de la séance créerait une version.

Ce module ne dépend ni des modèles ni des vues (port de ``wiki/historique.py`` de
Revision ; heures locales naïves, comme le reste de Hangar).
"""

import difflib
import re
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from flask import current_app

MAX_VERSIONS = 100
MAX_OCTETS = 20 * 1024 * 1024
REGROUPEMENT = timedelta(minutes=10)
# Éclaircissage des versions d'édition : (âge maximal, tranche) ; une version par tranche.
PALIERS = (
    (timedelta(hours=1), None),                                # moins d'une heure : toutes
    (timedelta(days=1), lambda d: (d.date(), d.hour)),         # moins d'un jour : une par heure
    (timedelta(days=30), lambda d: d.date()),                  # moins d'un mois : une par jour
    (timedelta(days=182), lambda d: (d.toordinal() - 1) // 7),  # six mois : une par semaine (lundi-dimanche)
    (None, lambda d: (d.toordinal() - 1) // 28),               # au-delà : une toutes les quatre semaines
)
_FORMAT = "%Y%m%dT%H%M%S%f"
_NOM_RE = re.compile(r"^(\d{8}T\d{12})([-_])([\w.-]*)\.md$")


def dossier() -> Path:
    return Path(current_app.config["DATA_DIR"]) / "wiki_versions"


def _dossier_page(rel) -> Path:
    rel = PurePosixPath(str(rel).replace("\\", "/"))
    if not rel.parts or any(p in ("..", "") for p in rel.parts):
        raise ValueError("chemin de page invalide")
    cible = (dossier() / rel.as_posix()).resolve()
    if not cible.is_relative_to(dossier().resolve()):
        raise ValueError("chemin de page invalide")
    return cible


def _lire_nom(nom: str):
    """(date, auteur tel qu'écrit dans le nom, version d'édition ?) d'un nom de version, ou None."""
    m = _NOM_RE.match(nom)
    if not m:
        return None
    try:
        date = datetime.strptime(m.group(1), _FORMAT)
    except ValueError:
        return None
    return date, m.group(3), m.group(2) == "_"


def _auteur_affiche(qui: str) -> str:
    return qui.replace("_", " ") if qui != "inconnu" else ""


def _versions(d: Path):
    """Versions du dossier d'une page, la plus ancienne d'abord : [(fichier, date, auteur, édition ?)]."""
    trouvees = []
    for f in d.glob("*.md"):
        infos = _lire_nom(f.name)
        if infos and f.is_file():
            trouvees.append((f, *infos))
    return sorted(trouvees, key=lambda v: (v[1], v[0].name))


def _retirer(f: Path) -> None:
    try:
        f.unlink()
    except OSError:  # déjà retiré par un enregistrement simultané, ou fichier verrouillé (Windows)
        pass


def sauvegarder(rel, contenu: str, auteur: str = "", edition: bool = False, regrouper: bool = False):
    """Garde `contenu` (le texte de la page avant modification) comme version datée.

    Par défaut, c'est un point de repère (corbeille, restauration, import, IA) : toujours créé,
    jamais éclairci. `edition` : simple enregistrement (éditeur, citation ajoutée), éclairci
    avec le temps. `regrouper` (version d'édition) : rien n'est créé si la dernière version est
    du même auteur et a moins de REGROUPEMENT ; elle garde déjà l'état d'avant la séance. À ne
    demander que si le texte remplacé vient de la même séance d'écriture, ou reste entier dans
    le nouveau texte (ajout à la fin) : sinon on perdrait une modification faite entre-temps.

    Renvoie l'identifiant de la version créée (nom du fichier), ou None si elle est regroupée."""
    d = _dossier_page(rel)
    d.mkdir(parents=True, exist_ok=True)
    maintenant = datetime.now()
    qui = re.sub(r"[^\w.-]", "_", auteur or "")[:40] or "inconnu"
    if regrouper:
        versions = _versions(d)
        if versions:
            _, date, auteur_derniere, _ = versions[-1]
            # Écart négatif : l'horloge a reculé depuis (heure d'hiver, heure du PC corrigée) ;
            # on regroupe aussi, sinon chaque enregistrement de la séance créerait une version.
            if auteur_derniere == qui and maintenant - date < REGROUPEMENT:
                return None
    sorte = "_" if edition or regrouper else "-"
    horodatage = maintenant
    while any((d / f"{horodatage.strftime(_FORMAT)}{s}{qui}.md").exists() for s in "-_"):
        horodatage += timedelta(microseconds=1)  # horloge grossière (Windows) : jamais écraser une version
    nom = f"{horodatage.strftime(_FORMAT)}{sorte}{qui}.md"
    (d / nom).write_text(contenu, encoding="utf-8", newline="\n")
    _eclaircir(d, maintenant)
    return nom


def _eclaircir(d: Path, maintenant: datetime) -> None:
    """Éclaircit les versions d'édition d'une page (PALIERS), puis applique les plafonds."""
    gardees, tranches, heures, en_sursis = [], set(), set(), []
    for v in _versions(d):
        f, date, _, edition = v
        age = maintenant - date
        for i, (age_max, tranche) in enumerate(PALIERS):
            if age_max is None or age < age_max:
                break
        cle = (i, tranche(date)) if tranche else None
        if cle is not None and cle in tranches and edition:
            _retirer(f)  # une version plus ancienne garde déjà cette tranche
            continue
        if cle is not None:
            tranches.add(cle)
        heure = (date.date(), date.hour)
        if cle is None and edition and heure in heures:
            en_sursis.append(f)  # moins d'une heure, mais une plus ancienne garde déjà son heure
        heures.add(heure)
        gardees.append(f)
    tailles = {}
    for f in gardees:
        try:
            tailles[f] = f.stat().st_size
        except OSError:
            tailles[f] = 0
    total = sum(tailles.values())
    # Plafonds : d'abord les versions en sursis (éclaircies de toute façon dans l'heure : une rafale
    # d'enregistrements ne chasse pas l'historique ancien), puis les plus anciennes ; jamais la toute
    # première (texte d'origine) ni la plus récente.
    if en_sursis and en_sursis[-1] == gardees[-1]:
        en_sursis.pop()
    while len(gardees) > 2 and (len(gardees) > MAX_VERSIONS or total > MAX_OCTETS):
        f = en_sursis.pop(0) if en_sursis else gardees[1]
        gardees.remove(f)
        total -= tailles[f]
        _retirer(f)


def lister(rel):
    """Versions d'une page, la plus récente d'abord : [{id, date, auteur, taille}]."""
    d = _dossier_page(rel)
    if not d.is_dir():
        return []
    versions = []
    for f, date, qui, _ in _versions(d):
        try:
            taille = f.stat().st_size
        except OSError:
            continue
        versions.append({"id": f.name, "date": date, "auteur": _auteur_affiche(qui), "taille": taille})
    return versions[::-1]


def lire(rel, id_):
    """Texte d'une version, ou None."""
    if not _lire_nom(str(id_)):
        return None
    f = _dossier_page(rel) / id_
    return f.read_text(encoding="utf-8") if f.is_file() else None


def differences(ancien: str, nouveau: str):
    """Lignes du texte en différences : [{type: 'suppr'|'ajout'|'contexte'|'info', texte}],
    avec 3 lignes de contexte autour de chaque changement."""
    lignes = []
    for l in difflib.unified_diff(ancien.splitlines(), nouveau.splitlines(), "version", "page actuelle", lineterm="", n=3):
        if l.startswith(("---", "+++")):
            continue
        if l.startswith("@@"):
            lignes.append({"type": "info", "texte": l})
        elif l.startswith("+"):
            lignes.append({"type": "ajout", "texte": l[1:]})
        elif l.startswith("-"):
            lignes.append({"type": "suppr", "texte": l[1:]})
        else:
            lignes.append({"type": "contexte", "texte": l[1:]})
    return lignes


def recentes(limite=40):
    """Dernières versions gardées, toutes pages confondues (la plus récente d'abord) :
    [{page: chemin .md dans le coffre, id, date, auteur}] (historique global du wiki)."""
    racine = dossier()
    if not racine.is_dir():
        return []
    trouvees = []
    for f in racine.rglob("*.md"):
        infos = _lire_nom(f.name)
        if infos and f.parent != racine:
            trouvees.append((infos[0], f.name, f, infos[1]))
    trouvees.sort(key=lambda t: t[:2], reverse=True)
    return [{"page": f.parent.relative_to(racine).as_posix(), "id": f.name, "date": date, "auteur": _auteur_affiche(qui)}
            for date, _, f, qui in trouvees[:limite]]
