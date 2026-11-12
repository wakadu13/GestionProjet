"""Comparer et trier des textes en français, sans dépendance (bibliothèque standard seulement).

- ``plier`` : forme « pliée » d'un texte pour la recherche — sans casse ni accents, apostrophes
  (’ ' ʼ) et guillemets confondus, « œ » écrit « oe », « æ » écrit « ae », espaces insécables
  changées en espaces. « Établir le plan d’actions » et « etablir le plan d'actions » se valent.
- ``cle_tri`` : clé de tri « à la française » : « anglais, Droit, École, Tactique, Zèbre »
  (et non « Droit, Tactique, Zèbre, anglais, École » comme dans l'ordre des octets), les nombres
  dans l'ordre (« Linux 2 » avant « Linux 10 »).

Ce module n'importe rien de Hangar : la base de données (extensions.py) enregistre à chaque connexion
les fonctions SQLite ``plier(texte)`` et ``contient_plie(texte, aiguille)`` (voir ``utils.contient``)
et la collation « fr » (``comparer``).
"""
from __future__ import annotations

import re
import threading
import unicodedata
from functools import lru_cache

# Caractères qu'on tape différemment selon le clavier ou le logiciel (Word met des apostrophes
# courbes et des espaces insécables, un clavier AZERTY n'a pas de touche « œ ») : remplacés avant
# la décomposition Unicode. ``UN_POUR_UN`` garde la longueur du texte (repris par wiki.vault.fold).
UN_POUR_UN = {
    **dict.fromkeys("\u2019\u2018\u201a\u201b\u02bc\u2032\u00b4", "'"),  # ’ ‘ ‚ ‛ ʼ ′ ´
    **dict.fromkeys("\u00ab\u00bb\u201c\u201d\u201e\u201f", '"'),         # « » “ ” „ ‟
    **dict.fromkeys("\u00a0\u202f\u2007", " "),                          # espaces insécables
    **dict.fromkeys("\u2010\u2011", "-"),                                 # traits d'union typographiques
}
_LIGATURES = {"œ": "oe", "Œ": "oe", "æ": "ae", "Æ": "ae", "ǽ": "ae", "Ǽ": "ae", "ǣ": "ae", "Ǣ": "ae"}
_EQUIVALENTS = str.maketrans({**UN_POUR_UN, **_LIGATURES})
# Signes diacritiques (accents, cédille, tréma…) laissés par la décomposition NFKD.
_DIACRITIQUES = re.compile("[\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f]")
_NOMBRES = re.compile(r"(\d+)")

# Nom de la collation SQLite qui trie avec ``cle_tri`` : ``Project.name.collate(COLLATION_FR)``.
COLLATION_FR = "fr"


class _TablePliage(dict):
    """Table de ``str.translate`` : forme pliée de chaque caractère, calculée une seule fois
    (deux fois plus rapide que de décomposer tout le texte à chaque appel)."""

    def __missing__(self, code):
        s = unicodedata.normalize("NFKD", chr(code).translate(_EQUIVALENTS)).casefold()
        # Une seconde passe pour ce que la décomposition ou la casse font apparaître (« ǽ » → « æ »).
        s = _DIACRITIQUES.sub("", unicodedata.normalize("NFKD", s).translate(_EQUIVALENTS))
        self[code] = s
        return s


_TABLE = _TablePliage()


def plier(texte):
    """Forme pliée de ``texte`` pour comparer sans casse ni accents (``None`` reste ``None``).

    « Établir le plan d’actions » → « etablir le plan d'actions » ; « Nœud » → « noeud » ;
    « 10 h » avec une espace insécable → « 10 h » avec une espace simple.
    La longueur peut changer (« œ » → « oe ») : pour placer un extrait dans le texte d'origine,
    voir ``wiki.vault.fold``, qui garde la longueur.
    """
    if texte is None:
        return None
    s = str(texte)
    if s.isascii():  # rien à décomposer (et isascii() ne coûte rien)
        return s.lower()
    return s.translate(_TABLE)


def contient_plie(aiguille, botte):
    """Vrai si ``aiguille`` figure dans ``botte``, casse, accents et apostrophes ignorés."""
    return plier(aiguille or "") in plier(botte or "")


# Textes longs déjà pliés (descriptions, pages, commentaires), gardés d'une recherche à l'autre :
# dans la palette Ctrl+K, chaque lettre tapée relance la recherche sur les mêmes textes (trois à
# quatre fois plus rapide ensuite). La clé est le texte lui-même : un texte modifié est plié à nouveau.
# Une fois plein, le mémo n'accepte plus rien (une recherche relit les textes toujours dans le même
# ordre : chasser les plus anciens ne garderait jamais ceux qui servent) ; il est vidé quand les refus
# dépassent quatre fois le nombre de textes gardés, pour ne pas garder sans fin des textes modifiés depuis.
_MEMO: dict[str, str] = {}
_MEMO_MAX = 16_000_000  # caractères gardés au plus (textes d'origine et pliés)
_MEMO_MIN = 200         # en dessous, plier coûte moins que de garder
_memo = {"taille": 0, "refus": 0}
_memo_verrou = threading.Lock()


def _plier_memo(texte: str) -> str:
    plie = _MEMO.get(texte)
    if plie is None:
        plie = texte.translate(_TABLE)
        n = len(texte) + len(plie)
        if len(texte) >= _MEMO_MIN:
            with _memo_verrou:
                if _memo["taille"] + n <= _MEMO_MAX:
                    if texte not in _MEMO:
                        _MEMO[texte] = plie
                        _memo["taille"] += n
                else:
                    _memo["refus"] += 1
                    if _memo["refus"] > 4 * len(_MEMO):
                        _MEMO.clear()
                        _memo.update(taille=0, refus=0)
    return plie


def contient_sql(texte, aiguille_pliee):
    """Fonction SQLite ``contient_plie(colonne, aiguille déjà pliée)`` : 1 si la colonne la contient
    (casse, accents, apostrophes et ligatures ignorés), 0 sinon, NULL si la colonne est NULL.
    « % » et « _ » n'y sont pas des jokers (ce n'est pas un LIKE)."""
    if texte is None or aiguille_pliee is None:
        return None
    s = texte if isinstance(texte, str) else str(texte)
    if s.isascii():
        return aiguille_pliee in s.lower()
    return aiguille_pliee in _plier_memo(s)


@lru_cache(maxsize=8192)
def cle_tri(texte):
    """Clé de tri à la française : casse et accents ignorés, nombres dans l'ordre.

    À égalité (« droit » et « Droit », « elodie » et « Élodie »), la forme sans majuscule
    puis sans accent passe en premier, comme dans un dictionnaire.
    """
    s = "" if texte is None else str(texte)
    morceaux = _NOMBRES.split(plier(s))
    # Après split, les indices impairs sont les nombres : comparés comme des entiers.
    cle = tuple((0, int(m), "") if i % 2 else (1, 0, m) for i, m in enumerate(morceaux) if m)
    return cle, s.swapcase()


def comparer(a, b):
    """Comparaison à la française de deux textes (-1, 0 ou 1), pour la collation SQLite « fr »."""
    ka, kb = cle_tri(a), cle_tri(b)
    return (ka > kb) - (ka < kb)
