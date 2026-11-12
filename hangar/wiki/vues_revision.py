"""Routes du wiki reprises de Revision qui n'ont pas leur place dans ``views`` : palette de commandes
(``/palette.json``), documentation (``/documentation/<doc>/``), erreurs du serveur (``/erreurs/``) et
anciennes adresses de Revision (pour que les liens écrits dans les notes du coffre mènent au bon endroit).
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from flask import abort, flash, jsonify, redirect, request, url_for
from flask_login import current_user
from werkzeug.routing import BuildError

from .. import BASE_DIR
from ..extensions import db
from ..icons import icon
from ..texte import cle_tri
from . import commun, installation
from .commun import can_edit, render
from .render import render_note
from .vault import CODE_RE, Note, get_vault, split_frontmatter
from .views import bp

DOCS_DIR = Path(__file__).resolve().parent / "docs"


def _url(endpoint, **kw):
    """Adresse d'une page d'un autre module, ou None s'il n'est pas là (la palette s'en passe)."""
    try:
        return url_for(endpoint, **kw)
    except BuildError:
        return None


# ---------------------------------------------------------------------------
# Palette de commandes (Ctrl+K) : données pour la palette de Hangar (static/wiki/palette.js)
# ---------------------------------------------------------------------------

ICONES_PALETTE = {
    "page": "wiki-file-text", "pdf": "wiki-pdf", "qcm": "graduation", "langue": "languages", "cmd": "arrow-right",
    "home": "home", "liste": "wiki-list-tree", "tag": "hash", "graphe": "wiki-graph", "historique": "history",
    "chercher": "search", "doc": "help", "jour": "sun", "jouer": "play", "stats": "chart", "planning": "target",
    "perso": "lock", "image": "image", "ia": "sparkle", "plein": "wiki-maximize", "modifier": "edit", "plus": "plus",
    "importer": "upload", "latex": "wiki-sigma", "travaux": "timer", "reglages": "settings", "erreurs": "alert",
    "hors-ligne": "download", "drapeau": "flag",
}


def _commandes():
    ia, edition, admin = commun.ai_dispo(), can_edit(), current_user.is_admin
    c = [
        ("Accueil du wiki", "home", _url("wiki.home")),
        ("Sommaire du wiki", "liste", _url("wiki.sommaire")),
        ("Tags du wiki", "tag", _url("wiki.tags")),
        ("Graphe des liens entre pages", "graphe", _url("wiki.graphe")),
        ("Historique des modifications du wiki", "historique", _url("wiki.historique_global")),
        ("Rechercher dans le wiki (pages et PDF)", "chercher", _url("wiki.search")),
        ("Documentation", "doc", _url("wiki.documentation")),
        ("Guide IA (pages et QCM)", "doc", _url("wiki.documentation", doc="ia")),
        ("Pages lisibles hors connexion", "hors-ligne", _url("wiki.hors_connexion")),
        ("Aujourd'hui (révisions du jour)", "jour", _url("qcm.aujourdhui")),
        ("QCM", "qcm", _url("qcm.index")),
        ("Révision intelligente (QCM)", "jouer", _url("qcm.play", revision=1)),
        ("Statistiques des QCM", "stats", _url("qcm.stats")),
        ("Planning des révisions", "planning", _url("qcm.planning_page")),
        ("Langues", "langue", _url("qcm.langues_index")),
        ("Mon espace (QCM personnels)", "perso", _url("qcm.perso")),
        ("Cartes à image masquée (schéma)", "image", _url("qcm.masque_editeur")),
    ]
    if ia:
        c += [("Demander au wiki (IA)", "ia", _url("ai.demander")),
              ("Corriger ma copie (photo, IA)", "image", _url("ai.copie"))]
    c += [("Plein écran", "plein", {"a": "fullscreen"})]
    if edition:
        c += [
            ("Modifier cette page", "modifier", {"a": "edit"}),
            ("Nouvelle page", "plus", _url("wiki.new")),
            ("Importer des fichiers (.md, PDF)", "importer", _url("wiki.importer")),
            ("Convertir un cours LaTeX en page", "latex", _url("wiki.latex")),
            ("Importer des QCM", "importer", _url("qcm.importer")),
            ("Travaux de l'IA", "travaux", _url("ai.transformations")),
        ]
        if ia:
            c += [("Transformer un cours en page avec l'IA", "ia", (_url("ai.transformations") or "") + "#transformer")]
    c += [("Réglages de l'IA", "reglages", _url("ai.reglages"))]
    if admin:
        c += [("Erreurs du serveur", "erreurs", _url("wiki.erreurs")),
              ("Signalements des QCM", "drapeau", _url("qcm.signalements"))]
    sortie = []
    for titre, ic, cible in c:
        if not cible:
            continue
        sortie.append({"t": titre, "ic": ic, **(cible if isinstance(cible, dict) else {"u": cible})})
    return sortie


@bp.route("/palette.json")
def palette():
    """Données de la palette de commandes (Ctrl+K) : pages, PDF, QCM, langues et commandes du wiki et des
    révisions (port de ``views.palette`` de Revision)."""
    vault = get_vault()
    pages = [{"t": n.title, "s": n.folder, "u": n.url, "k": " ".join(n.aliases)}
             for n in vault.notes.values() if not commun.est_qcm(n.folder)]
    pages += [{"t": a.path.stem, "s": "PDF" + (f" · {a.path.parent}" if a.path.parent.parts else ""), "u": a.view_url,
               "ic": "pdf"} for a in vault.attachments.values() if a.suffix == ".pdf"]
    pages.sort(key=lambda x: cle_tri(x["t"]))
    qcm, langues = [], []
    try:
        from ..qcm import services
        library = services.bibliotheque(current_user.id)
        qcm = [{"t": s.titre, "s": s.categorie, "u": url_for("qcm.play", set=s.id, mode="all")}
               for s in library.sorted_sets()]
        for nom in library.noms_langues():
            langues += [{"t": f"Session du jour · {nom}", "u": url_for("qcm.langue_play", langue=nom, mode="quotidien")},
                        {"t": f"Révision intensive · {nom}", "u": url_for("qcm.langue_play", langue=nom, mode="intensif")},
                        {"t": f"Modes et statistiques · {nom}", "u": url_for("qcm.langue_menu", langue=nom)}]
    except Exception:  # noqa: BLE001 - section QCM absente ou en erreur : la palette garde le wiki
        pass
    icones = {cle: str(icon(nom, 15)) for cle, nom in ICONES_PALETTE.items()}
    r = jsonify(pages=pages, qcm=qcm, langues=langues, commandes=_commandes(), icones=icones)
    r.headers["Cache-Control"] = "private, max-age=60"
    return r


# ---------------------------------------------------------------------------
# Documentation : guides du wiki affichés avec le moteur du wiki
# ---------------------------------------------------------------------------

# Clé d'adresse → (fichier, titre de l'onglet). L'ordre est celui des onglets.
DOCS = {
    "": (DOCS_DIR / "Wiki.md", "Le wiki"),
    "ia": (DOCS_DIR / "README-IA.md", "Guide IA (pages et QCM)"),
    "qcm": (DOCS_DIR / "QCM.md", "Guide des QCM"),
    "syntaxe": (installation.DEFAUT_DIR / "Guide" / "Syntaxe Obsidian.md", "Syntaxe Obsidian"),
    "hangar": (BASE_DIR / "README.md", "Hangar"),
}
# Fichiers cités dans les guides (liens relatifs) → onglet de la documentation.
FICHIERS_DOCS = {
    "README-IA.md": "ia", "Wiki.md": "", "QCM.md": "qcm", "Guide/QCM.md": "qcm", "vault/Guide/QCM.md": "qcm",
    "Syntaxe Obsidian.md": "syntaxe", "Guide/Syntaxe Obsidian.md": "syntaxe",
    "vault/Guide/Syntaxe Obsidian.md": "syntaxe", "README.md": "hangar",
}
# Lien Markdown, ou bloc/segment de code (laissé tel quel) : un lien dont le libellé est du code
# (« [`vault/`](vault/) ») commence avant lui et l'emporte.
LIEN_MD_RE = re.compile(r"(?<!!)\[(?P<label>[^\]\n]*)\]\((?P<cible>[^)\s]+)\)|" + CODE_RE.pattern, CODE_RE.flags)


def adapter_liens(text: str) -> str:
    """Les liens relatifs des guides visent des fichiers du dépôt : on les redirige vers la documentation
    ou le wiki quand c'est possible, sinon on garde le texte seul (port de ``_adapter_liens`` de Revision)."""
    def lien(m):
        if m.group("label") is None:
            return m.group(0)
        label, cible = m.group("label"), m.group("cible")
        if cible.startswith(("http://", "https://", "mailto:", "/", "#")):
            return m.group(0)
        chemin, _, ancre = cible.partition("#")
        chemin = chemin.removeprefix("./")
        if chemin in FICHIERS_DOCS:
            cle = FICHIERS_DOCS[chemin]
            return f"[{label}]({url_for('wiki.documentation', doc=cle)}{'#' + ancre if ancre else ''})"
        if chemin.startswith("vault/"):
            reste = chemin.removeprefix("vault/")
            if reste.endswith(".md"):
                return f"[{label}]({url_for('wiki.page', path=reste.removesuffix('.md'))})"
            if not reste:
                return f"[{label}]({url_for('wiki.sommaire')})"
        return label

    return LIEN_MD_RE.sub(lien, text)


@bp.route("/documentation/", defaults={"doc": ""})
@bp.route("/documentation/<doc>/")
def documentation(doc):
    """Documentation du wiki (port de ``views.documentation`` de Revision) : le guide du wiki, le guide pour
    les IA qui écrivent des pages et des QCM, le guide des QCM, la syntaxe et le README de Hangar."""
    if doc not in DOCS:
        abort(404, description="Documentation inconnue")
    chemin, titre = DOCS[doc]
    if not chemin.is_file():
        abort(404, description="Documentation introuvable")
    vault = get_vault()
    meta, body = split_frontmatter(chemin.read_text(encoding="utf-8").lstrip("\ufeff"))
    note = Note(PurePosixPath(chemin.name), chemin.stat().st_mtime, meta, adapter_liens(body))
    content, toc = render_note(vault, note)
    onglets = [(cle, t, cle == doc) for cle, (f, t) in DOCS.items() if f.is_file()]
    return render("wiki/documentation.html", vault=vault, note=note, content=content, toc=toc, onglets=onglets,
                  titre=titre, doc=doc)


# ---------------------------------------------------------------------------
# Erreurs du serveur (administrateurs)
# ---------------------------------------------------------------------------

@bp.route("/erreurs/", methods=["GET", "POST"])
def erreurs():
    """Dernières erreurs du serveur : regroupées, avec leur trace ; marquer comme réglée, effacer."""
    from .models import ErreurServeur
    if not current_user.is_admin:
        abort(403, description="Page réservée aux administrateurs.")
    if request.method == "POST":
        action = request.form.get("action")
        if action == "reglee":
            e = db.session.get(ErreurServeur, int(request.form.get("pk") or 0))
            if e is not None:
                e.reglee = True
        elif action == "rouvrir":
            e = db.session.get(ErreurServeur, int(request.form.get("pk") or 0))
            if e is not None:
                e.reglee = False
        elif action == "tout_regle":
            ErreurServeur.query.filter_by(reglee=False).update({"reglee": True})
        elif action == "effacer":
            ErreurServeur.query.filter_by(reglee=True).delete()
            flash("Erreurs réglées effacées.", "success")
        db.session.commit()
        return redirect(url_for("wiki.erreurs"))
    liste = ErreurServeur.query.order_by(ErreurServeur.reglee, ErreurServeur.derniere.desc()).limit(100).all()
    from .erreurs import masquer  # aussi pour les erreurs gardées par une version plus ancienne
    return render("wiki/erreurs.html", ouvertes=[e for e in liste if not e.reglee],
                  reglees=[e for e in liste if e.reglee], masquer=masquer)


# ---------------------------------------------------------------------------
# Anciennes adresses de Revision : les liens écrits dans les notes du coffre mènent au bon endroit
# ---------------------------------------------------------------------------

# /aujourdhui/, /connexion/, /compte/…, /utilisateurs/ sont définis par hangar/appareils.py (l'appli Android
# intercepte /connexion/ pour rouvrir sa session avec son jeton) : seules les adresses propres au wiki ici.
ALIAS = {
    "/claude/reglages/": "ai.reglages",
}


def _alias():
    cible = _url(ALIAS[request.url_rule.rule])
    if cible is None:
        abort(404)
    if request.query_string:
        cible += "?" + request.query_string.decode("utf-8", "replace")
    return redirect(cible)


for _regle in ALIAS:
    bp.add_url_rule(_regle, "alias_" + _regle.strip("/").replace("/", "_").replace("-", "_"), _alias)
