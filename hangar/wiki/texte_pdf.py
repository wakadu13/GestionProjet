"""Texte des PDF du coffre, pour la recherche et « Demander au wiki ».

Le texte est extrait page par page avec pypdf, en arrière-plan (un seul fil à la
fois), et gardé dans la base (modèle WikiPdfText) : un PDF n'est relu que s'il change.
La commande ``hangar wiki-indexer-pdf`` fait le même travail d'un coup.

Port de ``wiki/texte_pdf.py`` de Revision (ORM Django → SQLAlchemy, fil lancé avec
le contexte de l'application Flask).
"""

import logging
import re
import threading
from datetime import datetime

from flask import current_app
from sqlalchemy import func

from ..extensions import db
from .vault import Vault, fold

MAX_PAGES = 600
MAX_CHARS_PAGE = 20_000
_verrou = threading.Lock()
_fil = None
_cache = {"cle": None, "pages": {}}
_COUPURE_RE = re.compile(r"(\w)-\s*\n\s*(\w)")
journal = logging.getLogger(__name__)


def _signature(chemin):
    st = chemin.stat()
    return f"{st.st_mtime_ns}-{st.st_size}"


def extraire(chemin):
    """Texte de chaque page (espaces regroupés, mots coupés en fin de ligne recollés)."""
    from pypdf import PdfReader

    journal_pypdf = logging.getLogger("pypdf")
    niveau = journal_pypdf.level
    journal_pypdf.setLevel(logging.ERROR)  # avertissements de pypdf sur les PDF imparfaits
    try:
        pages = []
        for i, page in enumerate(PdfReader(chemin).pages):
            if i >= MAX_PAGES:
                break
            try:
                texte = page.extract_text() or ""
            except Exception:  # noqa: BLE001 - une page illisible n'arrête pas les autres
                texte = ""
            pages.append(" ".join(_COUPURE_RE.sub(r"\1\2", texte).split())[:MAX_CHARS_PAGE])
        return pages
    finally:
        journal_pypdf.setLevel(niveau)


def a_indexer(vault):
    """PDF du coffre absents de l'index ou modifiés depuis : [(pièce jointe, signature)]."""
    from .models import WikiPdfText

    connus = dict(db.session.query(WikiPdfText.chemin, WikiPdfText.signature).all())
    restants = []
    for pdf in vault.pdfs():
        try:
            signature = _signature(vault.root / pdf.path)
        except OSError:
            continue
        if connus.get(str(pdf.path)) != signature:
            restants.append((pdf, signature))
    return restants


def indexer(vault):
    """Extrait le texte des PDF nouveaux ou modifiés et oublie ceux qui ont disparu.
    Renvoie le nombre de PDF lus."""
    from .models import WikiPdfText

    presents = {str(pdf.path) for pdf in vault.pdfs()}
    disparus = [c for (c,) in db.session.query(WikiPdfText.chemin).all() if c not in presents]
    if disparus:
        WikiPdfText.query.filter(WikiPdfText.chemin.in_(disparus)).delete(synchronize_session=False)
        db.session.commit()
    n = 0
    for pdf, signature in a_indexer(vault):
        try:
            pages, erreur = extraire(vault.root / pdf.path), ""
        except Exception as e:  # noqa: BLE001 - PDF chiffré ou abîmé : noté, pas d'arrêt
            pages, erreur = [], (str(e) or e.__class__.__name__)[:300]
        ligne = WikiPdfText.query.filter_by(chemin=str(pdf.path)).first()
        if ligne is None:
            ligne = WikiPdfText(chemin=str(pdf.path))
            db.session.add(ligne)
        # Date à la microseconde : elle sert de clé au cache de _index().
        ligne.signature, ligne.pages, ligne.erreur, ligne.indexe_le = signature, pages, erreur, datetime.now()
        db.session.commit()
        n += 1
    return n


def lancer():
    """Lance l'indexation en arrière-plan (sans effet si elle tourne déjà).

    Réglage ``WIKI_INDEX_PDF_FIL`` (faux pendant les tests : l'indexation se fait alors
    seulement à la demande, avec indexer())."""
    global _fil
    app = current_app._get_current_object()
    if not app.config.get("WIKI_INDEX_PDF_FIL", not app.testing):
        return
    with _verrou:
        if _fil is not None and _fil.is_alive():
            return
        _fil = threading.Thread(target=_travail, args=(app,), name="index-pdf", daemon=True)
        _fil.start()


def en_cours():
    return _fil is not None and _fil.is_alive()


def _travail(app):
    from .vault import get_vault

    with app.app_context():
        try:
            indexer(get_vault())
        except Exception:  # noqa: BLE001 - noté dans le journal du serveur
            journal.exception("Indexation du texte des PDF")
            db.session.rollback()
        finally:
            db.session.remove()


def _index():
    """{chemin: (pages, pages repliées)}, gardé en mémoire tant que l'index ne change pas."""
    from .models import WikiPdfText

    n, d = db.session.query(func.count(WikiPdfText.id), func.max(WikiPdfText.indexe_le)).one()
    cle = (current_app.config["DATA_DIR"], n, d)
    if _cache["cle"] != cle:
        pages = {chemin: (p or [], [fold(x) for x in p or []])
                 for chemin, p in db.session.query(WikiPdfText.chemin, WikiPdfText.pages).all()}
        _cache.update(cle=cle, pages=pages)
    return _cache["pages"]


def chercher(vault, requete, limite=20, pages_par_pdf=3):
    """PDF dont une page contient tous les mots : [{pdf, nombre, pages: [(numéro, extrait HTML)]}]."""
    mots = [fold(m) for m in requete.split() if m]
    if not mots:
        return []
    index = _index()
    resultats = []
    for pdf in vault.pdfs():
        brutes, repliees = index.get(str(pdf.path), ((), ()))
        trouvees = [(sum(r.count(m) for m in mots), i + 1, b, r)
                    for i, (b, r) in enumerate(zip(brutes, repliees)) if all(m in r for m in mots)]
        if not trouvees:
            continue
        score = sum(t[0] for t in trouvees) + 5 * sum(m in fold(pdf.path.stem) for m in mots)
        meilleures = sorted(sorted(trouvees, key=lambda t: (-t[0], t[1]))[:pages_par_pdf], key=lambda t: t[1])
        resultats.append((score, pdf, len(trouvees), [(n, Vault._snippet(b, r, mots)) for _, n, b, r in meilleures]))
    resultats.sort(key=lambda r: (-r[0], str(r[1].path).lower()))
    return [{"pdf": pdf, "nombre": n, "pages": pages} for _, pdf, n, pages in resultats[:limite]]


def pages_pertinentes(vault, mots, limite=4):
    """Pages de PDF qui parlent le plus des mots-clés (repliés) : [(pdf, numéro, texte)]."""
    if not mots:
        return []
    index = _index()
    minimum = max(1, (len(mots) + 1) // 2)
    trouvees = []
    for pdf in vault.pdfs():
        brutes, repliees = index.get(str(pdf.path), ((), ()))
        for i, (b, r) in enumerate(zip(brutes, repliees)):
            presents = [m for m in mots if m in r]
            if len(presents) >= minimum:
                trouvees.append((10 * len(presents) + sum(min(r.count(m), 5) for m in presents), pdf, i + 1, b))
    trouvees.sort(key=lambda t: (-t[0], str(t[1].path).lower(), t[2]))
    return [(pdf, n, b) for _, pdf, n, b in trouvees[:limite]]
