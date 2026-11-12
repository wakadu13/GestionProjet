"""Lecture prudente des fichiers envoyés qui sont des archives ZIP (.apkg d'Anki, .docx, .xlsx).

Un petit fichier peut se décompresser en plusieurs gigaoctets, ou contenir des centaines de
milliers d'entrées : on regarde ce que l'archive annonce AVANT de l'ouvrir ou de la lire, et on
plafonne ce qui est lu (le module zipfile s'arrête de toute façon à la taille annoncée).
Les messages d'erreur s'adressent à l'utilisateur (ArchiveRefusee est une ValueError).
"""
from __future__ import annotations

import io
import struct
import zipfile

Mo = 1024 * 1024


class ArchiveRefusee(ValueError):
    """Archive trop volumineuse une fois décompressée, ou illisible."""


def _nombre_entrees(contenu: bytes) -> int | None:
    """Nombre d'entrées annoncé par la fin de l'archive (avant de la confier à zipfile, qui crée un
    objet par entrée). None si la fin de l'archive est introuvable (zipfile dira qu'elle est invalide)."""
    fin = contenu[-(65536 + 22):]
    pos = fin.rfind(b"PK\x05\x06")
    if pos < 0 or len(fin) - pos < 22:
        return None
    total = struct.unpack("<H", fin[pos + 10:pos + 12])[0]
    if total != 0xFFFF:
        return total
    # ZIP64 : le localisateur précède l'enregistrement de fin et donne la position du vrai total.
    loc = len(contenu) - len(fin) + pos - 20
    if loc < 0 or contenu[loc:loc + 4] != b"PK\x06\x07":
        return total
    debut = struct.unpack("<Q", contenu[loc + 8:loc + 16])[0]
    if contenu[debut:debut + 4] != b"PK\x06\x06":
        return None
    return struct.unpack("<Q", contenu[debut + 32:debut + 40])[0]


def ouvrir(contenu: bytes, nom: str, max_entrees: int, max_total: int | None = None) -> zipfile.ZipFile:
    """ZipFile sur ``contenu`` après contrôle du nombre d'entrées et, si ``max_total`` est donné,
    de la taille décompressée totale annoncée. Lève ArchiveRefusee."""
    n = _nombre_entrees(contenu)
    if n is not None and n > max_entrees:
        raise ArchiveRefusee(f"{nom} : archive refusée, elle contient trop de fichiers ({n}, {max_entrees} au plus).")
    try:
        archive = zipfile.ZipFile(io.BytesIO(contenu))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, OSError):
        raise ArchiveRefusee(f"{nom} : fichier illisible (archive abîmée ou d'un autre format).") from None
    infos = archive.infolist()
    if len(infos) > max_entrees:
        raise ArchiveRefusee(f"{nom} : archive refusée, elle contient trop de fichiers ({len(infos)}, {max_entrees} au plus).")
    if max_total is not None:
        total = sum(i.file_size for i in infos)
        if total > max_total:
            raise ArchiveRefusee(f"{nom} : fichier refusé, il occuperait {total // Mo} Mo une fois décompressé "
                                 f"({max_total // Mo} Mo au plus).")
    return archive


def _controler_membre(archive: zipfile.ZipFile, membre: str, taille_max: int, nom: str) -> zipfile.ZipInfo:
    try:
        info = archive.getinfo(membre)
    except KeyError:
        raise ArchiveRefusee(f"{nom} : « {membre} » introuvable dans l'archive.") from None
    if info.file_size > taille_max:
        raise ArchiveRefusee(f"{nom} : fichier refusé, son contenu occuperait {info.file_size // Mo} Mo une fois "
                             f"décompressé ({taille_max // Mo} Mo au plus).")
    return info


def lire(archive: zipfile.ZipFile, membre: str, taille_max: int, nom: str) -> bytes:
    """Contenu décompressé d'une entrée, au plus ``taille_max`` octets (ArchiveRefusee au-delà)."""
    info = _controler_membre(archive, membre, taille_max, nom)
    try:
        with archive.open(info) as f:
            data = f.read(taille_max + 1)
    except (zipfile.BadZipFile, NotImplementedError, ValueError, OSError, EOFError):
        raise ArchiveRefusee(f"{nom} : « {membre} » illisible (archive abîmée).") from None
    if len(data) > taille_max:
        raise ArchiveRefusee(f"{nom} : fichier refusé, trop volumineux une fois décompressé ({taille_max // Mo} Mo au plus).")
    return data


def extraire(archive: zipfile.ZipFile, membre: str, destination, taille_max: int, nom: str) -> None:
    """Copie une entrée décompressée dans le fichier ``destination`` (par blocs, sans tout charger
    en mémoire), au plus ``taille_max`` octets."""
    info = _controler_membre(archive, membre, taille_max, nom)
    ecrits = 0
    try:
        with archive.open(info) as src, open(destination, "wb") as dst:
            while True:
                bloc = src.read(Mo)
                if not bloc:
                    break
                ecrits += len(bloc)
                if ecrits > taille_max:
                    raise ArchiveRefusee(f"{nom} : fichier refusé, trop volumineux une fois décompressé "
                                         f"({taille_max // Mo} Mo au plus).")
                dst.write(bloc)
    except (zipfile.BadZipFile, NotImplementedError, ValueError, OSError, EOFError) as e:
        if isinstance(e, ArchiveRefusee):
            raise
        raise ArchiveRefusee(f"{nom} : « {membre} » illisible (archive abîmée).") from None

