"""Petit encodeur de QR codes, sans dépendance : l'adresse de Hangar à scanner avec le téléphone.

Mode octets (UTF-8), correction d'erreurs de niveau M (15 %), versions 1 à 10 (jusqu'à 213 octets : bien assez
pour une adresse). Construction selon la norme ISO/IEC 18004, à la manière de l'encodeur de Nayuki :
motifs fixes, données + codes de Reed-Solomon entrelacés, choix du masque le moins pénalisé.

- ``matrice(texte)`` : liste de lignes de booléens (True = module foncé), sans marge ;
- ``svg(texte)`` : image SVG (noir sur blanc, marge de 4 modules) à insérer dans une page ;
- ``terminal(texte)`` : le même QR code en caractères (demi-blocs), pour la console au démarrage.
"""
from __future__ import annotations

from html import escape
from itertools import pairwise

# Niveau M, versions 1 à 10 : codes de correction par bloc et nombre de blocs (table 9 de la norme)
_ECC_PAR_BLOC = (None, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26)
_NB_BLOCS = (None, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5)
VERSION_MAX = 10
_FORMAT_M = 0  # bits du niveau M dans l'information de format


def _octets_bruts(version: int) -> int:
    """Nombre d'octets (données + correction) que contient un symbole de cette version."""
    n = (16 * version + 128) * version + 64
    if version >= 2:
        nb_align = version // 7 + 2
        n -= (25 * nb_align - 10) * nb_align - 55
        if version >= 7:
            n -= 36
    return n // 8


def _octets_donnees(version: int) -> int:
    return _octets_bruts(version) - _ECC_PAR_BLOC[version] * _NB_BLOCS[version]


# --- Reed-Solomon sur GF(256), polynôme 0x11D ---

def _mul(x: int, y: int) -> int:
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _diviseur(degre: int) -> list[int]:
    res = [0] * (degre - 1) + [1]
    racine = 1
    for _ in range(degre):
        for j in range(degre):
            res[j] = _mul(res[j], racine)
            if j + 1 < degre:
                res[j] ^= res[j + 1]
        racine = _mul(racine, 0x02)
    return res


def _reste(donnees: list[int], diviseur: list[int]) -> list[int]:
    res = [0] * len(diviseur)
    for b in donnees:
        facteur = b ^ res.pop(0)
        res.append(0)
        for i, coef in enumerate(diviseur):
            res[i] ^= _mul(coef, facteur)
    return res


# --- Données ---

def _version_pour(n_octets: int) -> int:
    for v in range(1, VERSION_MAX + 1):
        bits = 4 + (8 if v <= 9 else 16) + 8 * n_octets
        if bits <= _octets_donnees(v) * 8:
            return v
    raise ValueError("Texte trop long pour un QR code de Hangar.")


def _mots_de_code(octets: bytes, version: int) -> list[int]:
    capacite = _octets_donnees(version) * 8
    bits: list[int] = []

    def ajouter(valeur: int, n: int) -> None:
        bits.extend((valeur >> i) & 1 for i in range(n - 1, -1, -1))

    ajouter(0b0100, 4)  # mode octets
    ajouter(len(octets), 8 if version <= 9 else 16)
    for b in octets:
        ajouter(b, 8)
    ajouter(0, min(4, capacite - len(bits)))  # terminaison
    ajouter(0, (-len(bits)) % 8)
    donnees = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    for i in range(capacite // 8 - len(donnees)):  # octets de remplissage, en alternance
        donnees.append(0xEC if i % 2 == 0 else 0x11)
    # Blocs (les premiers sont plus courts d'un octet), correction, puis entrelacement
    nb_blocs, ecc = _NB_BLOCS[version], _ECC_PAR_BLOC[version]
    bruts = _octets_bruts(version)
    nb_courts = nb_blocs - bruts % nb_blocs
    long_court = bruts // nb_blocs
    diviseur = _diviseur(ecc)
    blocs, k = [], 0
    for i in range(nb_blocs):
        n = long_court - ecc + (0 if i < nb_courts else 1)
        bloc = donnees[k:k + n]
        k += n
        correction = _reste(bloc, diviseur)
        if i < nb_courts:
            bloc.append(0)  # case vide, sautée à l'entrelacement
        blocs.append(bloc + correction)
    resultat = []
    for i in range(len(blocs[0])):
        for j, bloc in enumerate(blocs):
            if i != long_court - ecc or j >= nb_courts:
                resultat.append(bloc[i])
    return resultat


# --- Placement ---

def _positions_alignement(version: int) -> list[int]:
    if version == 1:
        return []
    nb = version // 7 + 2
    taille = version * 4 + 17
    pas = (version * 8 + nb * 3 + 5) // (nb * 4 - 4) * 2
    res = [taille - 7 - i * pas for i in range(nb - 1)] + [6]
    return sorted(res)


class _Symbole:
    def __init__(self, version: int):
        self.version = version
        self.taille = version * 4 + 17
        self.m = [[False] * self.taille for _ in range(self.taille)]
        self.fixe = [[False] * self.taille for _ in range(self.taille)]

    def poser(self, x: int, y: int, fonce: bool) -> None:
        self.m[y][x] = fonce
        self.fixe[y][x] = True

    def motifs(self) -> None:
        t = self.taille
        for i in range(t):
            self.poser(6, i, i % 2 == 0)
            self.poser(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (t - 4, 3), (3, t - 4)):  # repères de position et séparateurs
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < t and 0 <= y < t:
                        self.poser(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _positions_alignement(self.version)
        dernier = len(pos) - 1
        for i, a in enumerate(pos):
            for j, b in enumerate(pos):
                if (i, j) in ((0, 0), (0, dernier), (dernier, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.poser(a + dx, b + dy, max(abs(dx), abs(dy)) != 1)
        self.format(0)  # réserve les cases ; réécrit avec le vrai masque
        if self.version >= 7:
            reste = self.version
            for _ in range(12):
                reste = (reste << 1) ^ ((reste >> 11) * 0x1F25)
            bits = self.version << 12 | reste
            for i in range(18):
                bit = (bits >> i) & 1 == 1
                a, b = t - 11 + i % 3, i // 3
                self.poser(a, b, bit)
                self.poser(b, a, bit)

    def format(self, masque: int) -> None:
        t = self.taille
        donnees = _FORMAT_M << 3 | masque
        reste = donnees
        for _ in range(10):
            reste = (reste << 1) ^ ((reste >> 9) * 0x537)
        bits = (donnees << 10 | reste) ^ 0x5412

        def bit(i):
            return (bits >> i) & 1 == 1
        for i in range(6):
            self.poser(8, i, bit(i))
        self.poser(8, 7, bit(6))
        self.poser(8, 8, bit(7))
        self.poser(7, 8, bit(8))
        for i in range(9, 15):
            self.poser(14 - i, 8, bit(i))
        for i in range(8):
            self.poser(t - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.poser(8, t - 15 + i, bit(i))
        self.poser(8, t - 8, True)  # module toujours foncé

    def donnees(self, mots: list[int]) -> None:
        t, i, droite = self.taille, 0, self.taille - 1
        total = len(mots) * 8
        while droite >= 1:
            if droite == 6:
                droite = 5
            for vert in range(t):
                for j in range(2):
                    x = droite - j
                    montee = ((droite + 1) & 2) == 0
                    y = t - 1 - vert if montee else vert
                    if not self.fixe[y][x] and i < total:
                        self.m[y][x] = (mots[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            droite -= 2

    def masquer(self, masque: int) -> None:
        f = _MASQUES[masque]
        for y in range(self.taille):
            for x in range(self.taille):
                if not self.fixe[y][x] and f(x, y):
                    self.m[y][x] = not self.m[y][x]


_MASQUES = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)

_MOTIF_A = (True, False, True, True, True, False, True, False, False, False, False)
_MOTIF_B = tuple(reversed(_MOTIF_A))


def _penalite(m: list[list[bool]]) -> int:
    t = len(m)
    score = 0
    lignes = [list(r) for r in m] + [[m[y][x] for y in range(t)] for x in range(t)]
    for ligne in lignes:
        n = 1
        for a, b in pairwise(ligne):
            if a == b:
                n += 1
            else:
                if n >= 5:
                    score += n - 2
                n = 1
        if n >= 5:
            score += n - 2
        for i in range(t - 10):
            morceau = tuple(ligne[i:i + 11])
            if morceau == _MOTIF_A or morceau == _MOTIF_B:
                score += 40
    for y in range(t - 1):
        for x in range(t - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    fonces = sum(sum(r) for r in m)
    total = t * t
    k = (abs(fonces * 20 - total * 10) + total - 1) // total - 1
    return score + k * 10


def matrice(texte: str, masque: int | None = None) -> list[list[bool]]:
    """Modules du QR code (True = foncé), sans marge. ``masque`` (0-7) : imposé, sinon le meilleur."""
    octets = texte.encode("utf-8")
    version = _version_pour(len(octets))
    mots = _mots_de_code(octets, version)
    meilleur, meilleur_score = None, None
    for essai in (range(8) if masque is None else (masque,)):
        s = _Symbole(version)
        s.motifs()
        s.donnees(mots)
        s.masquer(essai)
        s.format(essai)
        score = _penalite(s.m) if masque is None else 0
        if meilleur_score is None or score < meilleur_score:
            meilleur, meilleur_score = s.m, score
    return meilleur


def svg(texte: str, marge: int = 4, taille_px: int = 200) -> str:
    """QR code en SVG (noir sur blanc, même en thème sombre : les lecteurs en ont besoin)."""
    m = matrice(texte)
    n = len(m) + 2 * marge
    chemin = "".join(f"M{x + marge},{y + marge}h1v1h-1z" for y, ligne in enumerate(m)
                     for x, fonce in enumerate(ligne) if fonce)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" width="{taille_px}" '
            f'height="{taille_px}" shape-rendering="crispEdges" role="img" '
            f'aria-label="QR code : {escape(texte)}"><rect width="{n}" height="{n}" fill="#fff"/>'
            f'<path fill="#000" d="{chemin}"/></svg>')


def terminal(texte: str, marge: int = 2) -> str:
    """QR code en demi-blocs pour la console. Les modules CLAIRS sont dessinés (« █ ») : sur le fond noir
    habituel de la console, le QR code apparaît noir sur blanc, comme il doit l'être."""
    m = matrice(texte)
    t = len(m)
    n = t + 2 * marge

    def clair(x, y):
        x, y = x - marge, y - marge
        return not (0 <= x < t and 0 <= y < t and m[y][x])
    lignes = []
    for y in range(0, n, 2):
        ligne = ""
        for x in range(n):
            haut, bas = clair(x, y), (clair(x, y + 1) if y + 1 < n else False)
            ligne += "█" if haut and bas else "▀" if haut else "▄" if bas else " "
        lignes.append(ligne)
    return "\n".join(lignes)
