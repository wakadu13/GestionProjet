"""Export d'un set de QCM ou d'un paquet de langue vers Anki (.apkg) ou en texte
tabulé (Quizlet, tableur), et import d'un paquet Anki (.apkg) en set de cartes.

Un fichier .apkg est une archive zip : une base SQLite « collection.anki2 » au
format historique d'Anki (version 11, toujours importé par les versions
récentes) et un index « media » (vide ici : les images restent sur le wiki).
Les identifiants (paquet, type de note, notes) sont dérivés du contenu : un
second export du même set met les notes à jour dans Anki au lieu de les
dupliquer.

L'import (ajouté dans Hangar) lit la base d'un .apkg (« collection.anki21 » ou
« collection.anki2 ») : chaque note devient une FLASHCARD (premier champ au recto,
second au verso, les autres en explication), formules MathJax remises en $…$.
Les paquets du format le plus récent d'Anki (« collection.anki21b », compressé en
zstd) ne sont pas lisibles sans bibliothèque supplémentaire : il faut alors cocher
« Compatibilité avec les anciennes versions » à l'export dans Anki.

Comme quiz.py, ce module ne dépend ni des modèles ni des vues.
"""

import hashlib
import html
import io
import json
import re
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

from . import langues, quiz

SEPARATEUR_CHAMPS = "\x1f"
_LATEX_PRE = ("\\documentclass[12pt]{article}\n\\special{papersize=3in,5in}\n\\usepackage[utf8]{inputenc}\n"
              "\\usepackage{amssymb,amsmath}\n\\pagestyle{empty}\n\\setlength{\\parindent}{0in}\n\\begin{document}\n")
_CSS = (".card { font-family: arial; font-size: 20px; text-align: center; color: black; background-color: white; }\n"
        ".recto-liste { text-align: left; display: inline-block; margin-top: .6em; }\n"
        ".explication { font-size: 16px; color: #555; margin-top: 1em; }")

_SCHEMA = """
CREATE TABLE col (id integer primary key, crt integer not null, mod integer not null, scm integer not null,
  ver integer not null, dty integer not null, usn integer not null, ls integer not null, conf text not null,
  models text not null, decks text not null, dconf text not null, tags text not null);
CREATE TABLE notes (id integer primary key, guid text not null, mid integer not null, mod integer not null,
  usn integer not null, tags text not null, flds text not null, sfld integer not null, csum integer not null,
  flags integer not null, data text not null);
CREATE TABLE cards (id integer primary key, nid integer not null, did integer not null, ord integer not null,
  mod integer not null, usn integer not null, type integer not null, queue integer not null, due integer not null,
  ivl integer not null, factor integer not null, reps integer not null, lapses integer not null, left integer not null,
  odue integer not null, odid integer not null, flags integer not null, data text not null);
CREATE TABLE revlog (id integer primary key, cid integer not null, usn integer not null, ease integer not null,
  ivl integer not null, lastIvl integer not null, factor integer not null, time integer not null, type integer not null);
CREATE TABLE graves (usn integer not null, oid integer not null, type integer not null);
CREATE INDEX ix_notes_usn ON notes (usn);
CREATE INDEX ix_cards_usn ON cards (usn);
CREATE INDEX ix_revlog_usn ON revlog (usn);
CREATE INDEX ix_cards_nid ON cards (nid);
CREATE INDEX ix_cards_sched ON cards (did, queue, due);
CREATE INDEX ix_revlog_cid ON revlog (cid);
CREATE INDEX ix_notes_csum ON notes (csum);
"""


def _identifiant(*parties):
    """Entier positif stable (31 bits) dérivé d'une clé : identifiants Anki reproductibles."""
    h = hashlib.sha1("\x00".join(parties).encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big") & 0x7FFFFFFF or 1


def _guid(*parties):
    return hashlib.sha1("\x00".join(parties).encode("utf-8")).hexdigest()[:10]


def _csum(champ):
    return int(hashlib.sha1(_strip_html(champ).encode("utf-8")).hexdigest()[:8], 16)


def _strip_html(texte):
    return re.sub(r"<[^>]+>", "", texte)


def formater(texte):
    """Texte du wiki (Markdown léger, LaTeX $...$) vers le HTML d'une carte Anki :
    les formules passent en MathJax (\\(...\\)), que Anki affiche nativement."""
    texte = html.escape(str(texte), quote=False)
    texte = re.sub(r"\$\$(.+?)\$\$", lambda m: "\\[" + m.group(1) + "\\]", texte, flags=re.S)
    texte = re.sub(r"(?<!\\)\$([^$\n]+?)\$", lambda m: "\\(" + m.group(1) + "\\)", texte)
    texte = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", texte)
    texte = re.sub(r"(?<![\w*])\*([^*\n]+?)\*(?!\w)", r"<i>\1</i>", texte)
    texte = re.sub(r"`([^`\n]+?)`", r"<code>\1</code>", texte)
    return texte.strip().replace("\n", "<br>")


# ---------------------------------------------------------------------------
# Notes à exporter : (recto, verso, inversable)
# ---------------------------------------------------------------------------
def notes_qcm(qset):
    """Notes recto/verso d'un set de QCM : la question (avec ses choix) devant,
    la réponse et l'explication derrière."""
    notes = []
    for q in qset.questions:
        if q.masque:
            continue  # image masquée : l'image reste sur le wiki (pas de médias dans l'export)
        recto = formater(q.enonce)
        if q.consigne:
            recto = f"<i>{formater(q.consigne)}</i><br>{recto}"
        if q.choix:
            lignes = "".join(f"{chr(65 + i)}. {formater(c)}<br>" for i, c in enumerate(q.choix))
            recto += f'<div class="recto-liste">{lignes}</div>'
            verso = ", ".join(f"{chr(65 + i)}. {formater(q.choix[i])}" for i in q.reponses)
        elif q.est_flashcard:
            verso = formater(q.verso)
        else:
            verso = " / ".join(formater(r) for r in q.reponses_texte)
        if q.explication:
            verso += f'<div class="explication">{formater(q.explication)}</div>'
        notes.append((q.id, recto, verso, q.est_flashcard or q.est_qrc))
    return notes


def notes_langue(set_langue, donnees):
    """Notes d'un paquet de langue : le vocabulaire d'après le JSON d'origine (mot,
    traductions, exemple, prononciation), la conjugaison et la grammaire d'après les
    exercices."""
    notes = []
    for i, v in enumerate(donnees.get("vocabulaire") or [], start=1):
        if not isinstance(v, dict) or not v.get("mot"):
            continue
        id_ = str(v.get("id", f"voc{i}"))
        trads = v.get("traductions", v.get("traduction"))
        trads = trads if isinstance(trads, list) else [trads]
        recto = formater(v["mot"])
        if v.get("prononciation"):
            recto += f' <span class="muted">{formater(v["prononciation"])}</span>'
        verso = ", ".join(formater(t) for t in trads if t)
        extra = []
        if v.get("definition"):
            # Liste sans traduction : la définition au dos ; sinon en complément.
            if verso:
                extra.append(f"<i>{formater(v['definition'])}</i>")
            else:
                verso = formater(v["definition"])
        if v.get("exemple"):
            exemple = formater(str(v["exemple"]).replace("[", "<b>").replace("]", "</b>"))
            extra.append(exemple + (f"<br><i>{formater(v['exemple_traduction'])}</i>" if v.get("exemple_traduction") else ""))
        if v.get("note"):
            extra.append(formater(v["note"]))
        if extra:
            verso += '<div class="explication">' + "<br>".join(extra) + "</div>"
        notes.append((id_, recto, verso, True))
    for e in set_langue.exercices:
        if e.genre == langues.VOCABULAIRE:
            continue
        d = e.donnees
        if e.forme == langues.CARTE:
            notes.append((e.id, formater(d["recto"]), formater(d["verso"]), False))
        elif e.forme == langues.SAISIE:
            recto = formater(d["consigne"])
            if d.get("invite"):
                recto += f"<br><b>{formater(d['invite'])}</b>"
            notes.append((e.id, recto, formater(d["reponses"][0]), False))
        elif e.forme == langues.CHOIX:
            lignes = "".join(f"{chr(65 + i)}. {formater(c)}<br>" for i, c in enumerate(d["choix"]))
            verso = ", ".join(f"{chr(65 + i)}. {formater(d['choix'][i])}" for i in d["reponses"])
            notes.append((e.id, formater(d["enonce"]) + f'<div class="recto-liste">{lignes}</div>', verso, False))
        elif e.forme == langues.ORDRE:
            notes.append((e.id, formater(d["consigne"]) + "<br>" + " · ".join(formater(m) for m in d["mots"]),
                          formater(d["reponses"][0]), False))
    return notes


# ---------------------------------------------------------------------------
# Fichiers
# ---------------------------------------------------------------------------
def _modele(mid, nom, did, inverse, maintenant):
    champs = [{"name": n, "ord": i, "sticky": False, "rtl": False, "font": "Arial", "size": 20, "media": []}
              for i, n in enumerate(("Recto", "Verso"))]
    gabarits = [{"name": "Carte 1", "ord": 0, "qfmt": "{{Recto}}", "afmt": "{{FrontSide}}<hr id=answer>{{Verso}}",
                 "bqfmt": "", "bafmt": "", "did": None, "bfont": "", "bsize": 0}]
    req = [[0, "all", [0]]]
    if inverse:
        gabarits.append({"name": "Carte 2", "ord": 1, "qfmt": "{{Verso}}", "afmt": "{{FrontSide}}<hr id=answer>{{Recto}}",
                         "bqfmt": "", "bafmt": "", "did": None, "bfont": "", "bsize": 0})
        req.append([1, "all", [1]])
    return {"id": mid, "name": nom, "type": 0, "mod": maintenant, "usn": -1, "sortf": 0, "did": did,
            "tmpls": gabarits, "flds": champs, "css": _CSS, "latexPre": _LATEX_PRE, "latexPost": "\\end{document}",
            "latexsvg": False, "req": req, "tags": [], "vers": []}


def _deck(did, nom, maintenant, description=""):
    return {"id": did, "name": nom, "mod": maintenant, "usn": -1, "lrnToday": [0, 0], "revToday": [0, 0],
            "newToday": [0, 0], "timeToday": [0, 0], "collapsed": False, "browserCollapsed": False,
            "desc": description, "dyn": 0, "conf": 1, "extendNew": 0, "extendRev": 0}


_DCONF = {"1": {"id": 1, "name": "Default", "replayq": True, "mod": 0, "usn": 0, "autoplay": True, "timer": 0,
                "maxTaken": 60,
                "new": {"perDay": 20, "delays": [1, 10], "separate": True, "ints": [1, 4, 7], "initialFactor": 2500,
                        "bury": True, "order": 1},
                "rev": {"perDay": 200, "fuzz": 0.05, "ivlFct": 1, "maxIvl": 36500, "ease4": 1.3, "bury": True, "minSpace": 1},
                "lapse": {"leechFails": 8, "minInt": 1, "delays": [10], "leechAction": 0, "mult": 0}}}


def apkg(set_id, titre, description, notes):
    """Archive .apkg (octets) d'une liste de notes (id, recto, verso, inversable)."""
    maintenant = int(time.time())
    did = _identifiant("paquet", set_id)
    modeles = {}
    for inverse in (False, True):
        if any(n[3] == inverse for n in notes):
            mid = _identifiant("modele", set_id, "inverse" if inverse else "simple")
            modeles[inverse] = _modele(mid, "Hangar" + (" (et carte inversée)" if inverse else ""), did, inverse, maintenant)
    conf = {"nextPos": len(notes) + 1, "estTimes": True, "activeDecks": [did], "sortType": "noteFld", "timeLim": 0,
            "sortBackwards": False, "addToCur": True, "curDeck": did, "newBury": True, "newSpread": 0,
            "dueCounts": True, "curModel": str(next(iter(modeles.values()))["id"]) if modeles else "", "collapseTime": 1200}
    decks = {"1": _deck(1, "Default", maintenant), str(did): _deck(did, f"Hangar::{titre}", maintenant, description)}

    base = sqlite3.connect(":memory:")
    base.executescript(_SCHEMA)
    base.execute("INSERT INTO col VALUES (1, ?, ?, ?, 11, 0, 0, 0, ?, ?, ?, ?, '{}')",
                 (maintenant - maintenant % 86400, maintenant * 1000, maintenant * 1000, json.dumps(conf),
                  json.dumps({str(m["id"]): m for m in modeles.values()}), json.dumps(decks), json.dumps(_DCONF)))
    base_id = maintenant * 1000
    for position, (id_, recto, verso, inverse) in enumerate(notes):
        nid = base_id + position
        mid = modeles[inverse]["id"]
        base.execute("INSERT INTO notes VALUES (?, ?, ?, ?, -1, '', ?, ?, ?, 0, '')",
                     (nid, _guid(set_id, id_), mid, maintenant, SEPARATEUR_CHAMPS.join((recto, verso)),
                      _strip_html(recto), _csum(recto)))
        for ord_ in range(2 if inverse else 1):
            base.execute("INSERT INTO cards VALUES (?, ?, ?, ?, ?, -1, 0, 0, ?, 0, 0, 0, 0, 0, 0, 0, 0, '')",
                         (base_id + 1_000_000 + position * 2 + ord_, nid, did, ord_, maintenant, position + 1))
    base.commit()
    contenu = io.BytesIO()
    with zipfile.ZipFile(contenu, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("collection.anki2", _dump_sqlite(base))
        archive.writestr("media", "{}")
    base.close()
    return contenu.getvalue()


def _dump_sqlite(base):
    """Octets du fichier SQLite d'une base en mémoire (copie sur disque, puis lecture)."""
    with tempfile.TemporaryDirectory() as dossier:
        chemin = Path(dossier) / "collection.anki2"
        cible = sqlite3.connect(chemin)
        base.backup(cible)
        cible.close()
        return chemin.read_bytes()


def tsv(notes):
    """Texte tabulé (recto, tabulation, verso) : import direct dans Quizlet ou un tableur."""
    lignes = []
    for _, recto, verso, _ in notes:
        r = _strip_html(recto.replace("<br>", " ").replace("<div", " <div"))
        v = _strip_html(verso.replace("<br>", " ").replace('<div class="explication">', " — "))
        lignes.append(" ".join(html.unescape(r).split()) + "\t" + " ".join(html.unescape(v).split()))
    return "\n".join(lignes) + "\n"


def exporter(element, donnees=None):
    """(titre, notes) d'un set de QCM ou d'un paquet de langue (`donnees` : son JSON)."""
    if isinstance(element, quiz.QuestionSet):
        return element.titre, notes_qcm(element)
    return element.titre, notes_langue(element, donnees or {})


# ---------------------------------------------------------------------------
# Import d'un paquet Anki (.apkg) : un set de flashcards
# ---------------------------------------------------------------------------
TAILLE_MAX_APKG = 50 * 1024 * 1024
MAX_NOTES_IMPORT = 5000
# Garde-fous de l'import (un petit .apkg peut se décompresser en gigaoctets, et sa base SQLite est
# fournie par l'expéditeur) : base décompressée, nombre de fichiers de l'archive, taille d'un champ,
# durée de lecture de la base.
TAILLE_MAX_BASE = 200 * 1024 * 1024
MAX_ENTREES_APKG = 100_000
TAILLE_MAX_CHAMP = 1024 * 1024
# Volume total (en caractères) des champs et étiquettes lus : une base de quelques Ko peut renvoyer
# la même longue valeur pour chaque note (valeur par défaut d'une colonne ajoutée après coup).
VOLUME_MAX_NOTES = 64 * 1024 * 1024
DUREE_MAX_LECTURE = 15  # secondes


class ImportAnkiImpossible(ValueError):
    """Le fichier n'est pas un paquet Anki lisible (message montré tel quel)."""


def _depuis_html(champ):
    """HTML d'un champ Anki → Markdown simple du wiki (formules MathJax → $…$, sauts de ligne gardés)."""
    texte = str(champ)
    texte = re.sub(r"\[sound:[^\]]*\]", "", texte)
    texte = re.sub(r"\\\[(.+?)\\\]", lambda m: "$$" + m.group(1) + "$$", texte, flags=re.S)
    texte = re.sub(r"\\\((.+?)\\\)", lambda m: "$" + m.group(1) + "$", texte, flags=re.S)
    texte = re.sub(r"<\s*br\s*/?\s*>", "\n", texte, flags=re.I)
    texte = re.sub(r"</\s*(div|p|li)\s*>", "\n", texte, flags=re.I)
    texte = re.sub(r"<\s*(b|strong)\s*>(.*?)<\s*/\s*\1\s*>", r"**\2**", texte, flags=re.I | re.S)
    texte = re.sub(r"<\s*(i|em)\s*>(.*?)<\s*/\s*\1\s*>", r"*\2*", texte, flags=re.I | re.S)
    texte = _strip_html(texte)
    texte = html.unescape(texte).replace("\xa0", " ")
    lignes = [" ".join(l.split()) for l in texte.split("\n")]
    return "\n".join(l for l in lignes if l).strip()


def _lire_base(chemin, nom):
    """(notes, ligne de « col ») de la base SQLite d'un paquet, avec des garde-fous : lecture seule,
    `notes` et `col` doivent être de vraies tables (une vue ferait exécuter la requête de son
    choix), sans colonne calculée ni cachée, schéma non fiable, taille d'une valeur et volume total
    lu bornés, durée bornée, 5 000 notes au plus."""
    try:
        base = sqlite3.connect(f"{Path(chemin).as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise ImportAnkiImpossible(f"{nom} : base illisible ({e}).") from None
    limite = time.monotonic() + DUREE_MAX_LECTURE
    trop_long = []

    def surveiller():
        if time.monotonic() > limite:
            trop_long.append(True)
            return 1  # interrompt la requête
        return 0

    try:
        base.set_progress_handler(surveiller, 10_000)
        base.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, TAILLE_MAX_CHAMP)
        base.execute("PRAGMA trusted_schema = OFF")
        types = dict(base.execute("SELECT name, type FROM sqlite_master WHERE name IN ('notes', 'col')").fetchall())
        if types.get("notes") != "table" or types.get("col") != "table":
            raise ImportAnkiImpossible(f"{nom} : ce n'est pas un paquet Anki (tables des notes introuvables).")
        # Une colonne générée (GENERATED ALWAYS AS …) est recalculée à chaque lecture : quelques Ko
        # de fichier pourraient produire des gigaoctets. Anki n'en utilise jamais.
        for table in ("notes", "col"):
            if any(c[6] != 0 for c in base.execute(f"PRAGMA table_xinfo({table})")):
                raise ImportAnkiImpossible(f"{nom} : ce n'est pas un paquet Anki (structure inattendue).")
        lignes, volume = [], 0
        # Lecture dans l'ordre de stockage (pas d'ORDER BY : SQLite trierait toutes les notes, champs
        # compris, avant d'en rendre une seule), puis tri par id ici. Dans un vrai paquet, id est la
        # clé de la table : l'ordre est le même.
        curseur = base.execute("SELECT id, mid, flds, tags FROM notes LIMIT ?", (MAX_NOTES_IMPORT,))
        for ligne in curseur:  # une note à la fois, pour arrêter dès que le volume lu est trop grand
            volume += sum(len(v) for v in ligne[2:] if isinstance(v, (str, bytes)))
            if volume > VOLUME_MAX_NOTES:
                raise ImportAnkiImpossible(f"{nom} : paquet trop volumineux une fois ouvert "
                                           f"({VOLUME_MAX_NOTES // (1024 * 1024)} Mo de texte au plus).")
            lignes.append(ligne)
        curseur.close()
        lignes.sort(key=lambda l: (not isinstance(l[0], int), l[0] if isinstance(l[0], int) else 0))
        col = base.execute("SELECT decks, models FROM col LIMIT 1").fetchone()
    except sqlite3.DatabaseError as e:
        if trop_long:
            raise ImportAnkiImpossible(f"{nom} : lecture du paquet trop longue (plus de {DUREE_MAX_LECTURE} s), "
                                       "import abandonné.") from None
        if "too big" in str(e):
            raise ImportAnkiImpossible(f"{nom} : une carte du paquet est trop volumineuse "
                                       f"({TAILLE_MAX_CHAMP // (1024 * 1024)} Mo au plus).") from None
        raise ImportAnkiImpossible(f"{nom} : base illisible ({e}).") from None
    finally:
        base.close()
    return lignes, col


def lire_apkg(contenu: bytes, nom: str = "paquet.apkg"):
    """Set de QCM (dict JSON au format du wiki) d'après un paquet Anki : une FLASHCARD par note.

    Les notes « texte à trous » d'Anki ({{c1::…}}) deviennent des questions TROU. Lève
    ImportAnkiImpossible si le fichier n'est pas lisible."""
    from ..archives import ArchiveRefusee, extraire, ouvrir

    if len(contenu) > TAILLE_MAX_APKG:
        raise ImportAnkiImpossible("Paquet trop gros (50 Mo au plus).")
    if not zipfile.is_zipfile(io.BytesIO(contenu)):
        raise ImportAnkiImpossible(f"{nom} : ce n'est pas un paquet Anki (.apkg).")
    try:
        archive = ouvrir(contenu, nom, MAX_ENTREES_APKG)
    except ArchiveRefusee as e:
        raise ImportAnkiImpossible(str(e)) from None
    noms = set(archive.namelist())
    # Un export récent contient collection.anki21b (compressée, illisible ici) et un collection.anki2
    # factice (« mets Anki à jour ») : on le reconnaît avant de lire collection.anki2.
    if "collection.anki21" in noms:
        base_nom = "collection.anki21"
    elif "collection.anki21b" in noms:
        raise ImportAnkiImpossible(f"{nom} : format Anki récent (compressé) : dans Anki, exporte le paquet en cochant "
                                   "« Compatibilité avec les anciennes versions d'Anki ».")
    elif "collection.anki2" in noms:
        base_nom = "collection.anki2"
    else:
        raise ImportAnkiImpossible(f"{nom} : base du paquet introuvable.")
    with tempfile.TemporaryDirectory() as dossier:
        chemin = Path(dossier) / "collection.sqlite"
        try:
            extraire(archive, base_nom, chemin, TAILLE_MAX_BASE, nom)
        except ArchiveRefusee as e:
            raise ImportAnkiImpossible(str(e)) from None
        lignes, col = _lire_base(chemin, nom)
    titre = Path(nom).stem
    modeles = {}
    try:
        decks = json.loads(col[0]) if col else {}
        noms_paquets = [d.get("name", "") for k, d in decks.items() if k != "1" and d.get("name")]
        if noms_paquets:
            titre = noms_paquets[0].split("::")[-1]
        modeles = json.loads(col[1]) if col else {}
    except (ValueError, TypeError, AttributeError):
        pass
    questions = []
    for nid, mid, flds, _tags in lignes:
        champs = [_depuis_html(c) for c in str(flds).split(SEPARATEUR_CHAMPS)]
        modele = modeles.get(str(mid), {}) if isinstance(modeles, dict) else {}
        if modele.get("type") == 1 or re.search(r"\{\{c\d+::", champs[0] if champs else ""):
            # Texte à trous : une question par numéro de trou, comme les cartes d'Anki ({{c1::…}},
            # {{c2::…}}) ; les autres trous restent écrits en clair.
            texte = champs[0] if champs else ""
            extra = "\n".join(c for c in champs[1:] if c)
            numeros = sorted({int(n) for n in re.findall(r"\{\{c(\d+)::", texte)})
            for n in numeros:
                motif = re.compile(r"\{\{c(\d+)::(.*?)(?:::(.*?))?\}\}", re.S)
                reponses = [m.group(2) for m in motif.finditer(texte) if int(m.group(1)) == n]

                def trou(m, n=n):
                    if int(m.group(1)) != n:
                        return m.group(2)
                    return "___" + (f" ({m.group(3)})" if m.group(3) else "")

                enonce = motif.sub(trou, texte)
                q = {"id": f"anki-{nid}-c{n}", "type": "TROU", "question": enonce, "reponses_texte": [" ".join(reponses)]}
                if extra:
                    q["explication"] = extra
                questions.append(q)
            continue
        recto = champs[0] if champs else ""
        verso = next((c for c in champs[1:] if c), "")
        if not recto or not verso:
            continue
        q = {"id": f"anki-{nid}", "type": "FLASHCARD", "question": recto, "verso": verso}
        autres = [c for c in champs[2:] if c and c != verso]
        if autres:
            q["explication"] = "\n".join(autres)
        questions.append(q)
    if not questions:
        raise ImportAnkiImpossible(f"{nom} : aucune carte recto/verso trouvée dans le paquet.")
    return {"titre": titre, "description": f"Importé d'Anki ({len(questions)} cartes).", "origine": "Anki",
            "questions": questions}
