"""Hangar : agenda personnel (emploi du temps, tâches, rappels) et gestion de projet, 100 % local."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from flask import Flask, abort, g, redirect, render_template, request, session, url_for
from flask_login import current_user

from .extensions import db, login_manager

__version__ = "1.1.0"

BASE_DIR = Path(__file__).resolve().parent.parent


def _secret_key(data_dir: Path) -> str:
    f = data_dir / "secret.key"
    if f.exists():
        return f.read_text().strip()
    key = secrets.token_hex(32)
    f.write_text(key)
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass
    return key


def create_app(data_dir: str | os.PathLike | None = None, testing: bool = False) -> Flask:
    data = Path(data_dir or os.environ.get("HANGAR_DATA") or BASE_DIR / "data").resolve()
    data.mkdir(parents=True, exist_ok=True)
    (data / "uploads").mkdir(exist_ok=True)
    (data / "backups").mkdir(exist_ok=True)

    from .wiki import coffre
    wiki_dir, wiki_origine = coffre.choisir(data)

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=_secret_key(data),
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{data / 'hangar.db'}",
        SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"timeout": 15, "check_same_thread": False}},
        DATA_DIR=str(data),
        UPLOAD_DIR=str(data / "uploads"),
        BACKUP_DIR=str(data / "backups"),
        # Wiki (coffre Obsidian) : HANGAR_VAULT, sinon le dossier mémorisé dans data/coffre.txt, sinon data/vault
        WIKI_DIR=str(wiki_dir),
        WIKI_ORIGINE=wiki_origine,
        WIKI_HOME=os.environ.get("HANGAR_WIKI_HOME", "Accueil"),
        MAX_CONTENT_LENGTH=50 * 1024 * 1024,
        SESSION_COOKIE_SAMESITE="Lax",
        REMEMBER_COOKIE_DURATION=60 * 60 * 24 * 30,
        TESTING=testing,
        JSON_AS_ASCII=False,
    )
    app.json.ensure_ascii = False

    db.init_app(app)
    login_manager.init_app(app)

    from . import models  # noqa: F401
    from .ai import models as _ai_models  # noqa: F401
    from .qcm import models as _qcm_models  # noqa: F401
    from .wiki import models as _wiki_models  # noqa: F401
    if wiki_origine == coffre.DEFAUT:  # un coffre choisi n'est jamais créé : introuvable, le wiki le dit
        wiki_dir.mkdir(parents=True, exist_ok=True)

    with app.app_context():
        migrer_schema()
        models.Workspace.get()

    @login_manager.user_loader
    def load_user(uid):
        u = db.session.get(models.User, int(uid))
        return u if u and u.active else None

    _register_blueprints(app)
    _register_template_helpers(app)
    _register_hooks(app)
    from . import securite
    securite.init_app(app)
    return app


def migrer_schema() -> list[str]:
    """Met la base au niveau du code : tables manquantes (``db.create_all``), puis colonnes et index manquants
    (``_auto_migrate``). Appelée au démarrage et juste après la restauration d'une sauvegarde (une base d'une
    version plus ancienne marche alors sans redémarrer). Renvoie la liste des changements faits (vide : la base
    était déjà à jour)."""
    from sqlalchemy import inspect
    avant = set(inspect(db.engine).get_table_names())
    db.create_all()
    changements = [f"table {t.name}" for t in db.metadata.sorted_tables if t.name not in avant]
    return changements + _auto_migrate()


def _auto_migrate() -> list[str]:
    """Ajoute aux tables existantes les colonnes déclarées dans les modèles mais absentes de la base, et crée
    leurs index manquants (``db.create_all`` ne touche pas une table qui existe déjà).

    Permet de faire évoluer le modèle sans perdre les données (SQLite : ALTER TABLE ADD COLUMN). Une colonne
    obligatoire (NOT NULL) présente dans la base mais plus dans le modèle signale un renommage : chaque création
    échouerait. Elle est seulement signalée dans le journal (garder l'ancien nom avec ``db.Column("ancien", …)``).
    Renvoie la liste des changements faits."""
    import logging

    from sqlalchemy import inspect, text
    insp = inspect(db.engine)
    changements = []
    for table in db.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        colonnes = insp.get_columns(table.name)
        existing = {c["name"] for c in colonnes}
        for col in table.columns:
            if col.name in existing:
                continue
            coltype = col.type.compile(dialect=db.engine.dialect)
            default = ""
            if col.default is not None and getattr(col.default, "is_scalar", False):
                v = col.default.arg
                if isinstance(v, bool):
                    default = f" DEFAULT {int(v)}"
                elif isinstance(v, (int, float)):
                    default = f" DEFAULT {v}"
                elif isinstance(v, str):
                    default = " DEFAULT '" + v.replace("'", "''") + "'"
            db.session.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}{default}'))
            changements.append(f"colonne {table.name}.{col.name}")
        for c in colonnes:
            if c["name"] not in table.columns and not c.get("nullable", True) and c.get("default") is None:
                logging.getLogger(__name__).warning(
                    "Base : la colonne obligatoire %s.%s n'existe plus dans le modèle (renommée ?) : "
                    "les créations dans cette table échoueront.", table.name, c["name"])
    if changements:
        db.session.commit()
    noms_index = {i["name"] for t in insp.get_table_names() for i in insp.get_indexes(t)}
    for table in db.metadata.sorted_tables:
        for index in table.indexes:
            if index.name and index.name not in noms_index:
                try:
                    index.create(bind=db.engine, checkfirst=True)
                except Exception as exc:  # noqa: BLE001 - index unique sur des doublons : Hangar démarre quand même
                    logging.getLogger(__name__).warning("Base : index %s impossible à créer (%s)", index.name, exc)
                    continue
                changements.append(f"index {index.name}")
    return changements


def _register_blueprints(app: Flask) -> None:
    from .ai import views as ai_views
    from .qcm import views as qcm_views
    from .views import (agenda, analytics, api, apiv1, auth, bilan, cycles, home, intake, issues, mail, modules,
                        pages, planning, projects, saved_views, settings, team, workgraph)
    from .wiki import views as wiki_views
    for bp in (auth.bp, agenda.bp, home.bp, projects.bp, issues.bp, cycles.bp, modules.bp, saved_views.bp, pages.bp,
               intake.bp, analytics.bp, team.bp, settings.bp, api.bp, apiv1.bp, mail.bp, planning.bp, bilan.bp,
               workgraph.bp, wiki_views.bp, qcm_views.bp, ai_views.bp):
        app.register_blueprint(bp)


def csrf_token() -> str:
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(20)
    return session["_csrf"]


def _register_hooks(app: Flask) -> None:
    from .models import User

    @app.before_request
    def _setup_and_csrf():
        if request.endpoint == "static":
            return None
        if not app.config.get("_HAS_USERS"):
            if db.session.query(User.id).first() is None:
                if request.endpoint not in ("auth.setup",):
                    return redirect(url_for("auth.setup"))
            else:
                app.config["_HAS_USERS"] = True
        # `g.csrf_exempt` : posé par un crochet de blueprint qui passe avant celui-ci, seulement pour une
        # requête authentifiée sans cookie (jeton d'appareil vérifié, connexion de l'appli Android).
        if (request.method in ("POST", "PUT", "PATCH", "DELETE") and not request.path.startswith("/api/v1/")
                and not g.get("csrf_exempt")):
            sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
            token = session.get("_csrf")
            if not token or not sent or not secrets.compare_digest(token, sent):
                abort(400, description="Jeton de sécurité expiré : recharge la page et réessaie.")
        if (current_user.is_authenticated and current_user.must_change_password
                and request.endpoint not in ("settings.profile", "settings.password", "auth.logout",
                                             "appareils.alias_deconnexion")
                and not request.path.startswith("/api/")):
            return redirect(url_for("settings.profile", must_change=1))
        return None

    # Message par défaut de Werkzeug (en anglais) quand la vue n'en a pas donné.
    messages = {
        404: "Cette page n’existe pas (ou plus). Vérifie l’adresse, ou reviens à l’accueil.",
        405: "Cette adresse ne s’ouvre pas directement : reviens en arrière et passe par le bouton prévu.",
    }

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(405)
    @app.errorhandler(413)
    def _http_error(err):
        message = getattr(err, "description", None) or str(err)
        if err.code in messages and message == type(err).description:
            message = messages[err.code]
        # 405 : l'en-tête Allow (méthodes acceptées) reste dans la réponse, comme l'exige HTTP.
        headers = {"Allow": ", ".join(err.valid_methods)} if getattr(err, "valid_methods", None) else {}
        if request.path.startswith("/api/"):
            return {"ok": False, "error": message}, err.code, headers
        return render_template("errors.html", error=err, message=message), err.code, headers

    @app.errorhandler(500)
    def _server_error(err):
        # Rien ici ne doit lever à son tour : une base au mauvais format (sauvegarde ancienne restaurée…)
        # ferait sinon tomber aussi la page d'erreur, et le navigateur n'afficherait qu'un texte anglais brut.
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            app.logger.exception("Annulation impossible après une erreur")
        try:
            from .wiki import erreurs  # page /erreurs/ des administrateurs
            erreurs.noter(getattr(err, "original_exception", None), request=request)
        except Exception:  # noqa: BLE001
            app.logger.exception("Erreur impossible à noter")
        if request.path.startswith("/api/"):
            return {"ok": False, "error": "Erreur interne"}, 500
        try:
            return render_template("errors.html", error=err), 500
        except Exception:  # noqa: BLE001
            app.logger.exception("Page d'erreur impossible à afficher")
            return PAGE_ERREUR_MINIMALE, 500, {"Content-Type": "text/html; charset=utf-8"}


# Page d'erreur sans gabarit ni base de données : affichée quand la vraie page d'erreur échoue elle-même.
PAGE_ERREUR_MINIMALE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Erreur · Hangar</title></head>
<body style="font-family:system-ui,sans-serif;max-width:560px;margin:15vh auto;padding:0 20px;color:#1f2937">
<h1 style="font-size:22px">Une erreur inattendue s’est produite</h1>
<p>Hangar n’arrive pas à afficher cette page. Réessaie dans un instant.</p>
<p>Si l’erreur revient sur toutes les pages, ferme la fenêtre de Hangar puis relance-le
(double-clic sur <b>lancer.bat</b>, ou <code>uv run hangar</code>).</p>
<p><a href="/">Revenir à l’accueil</a></p></body></html>"""


def _register_template_helpers(app: Flask) -> None:
    from . import charts, icons, issue_query, models, utils
    from .services import activity_text

    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True
    app.jinja_env.add_extension("jinja2.ext.do")

    filters = {
        "date": utils.fmt_date,
        "date_long": utils.fmt_date_long,
        "datetime": utils.fmt_datetime,
        "ago": utils.time_ago,
        "due": utils.relative_due,
        "md": utils.render_markdown,
        "minutes": utils.fmt_minutes,
        "excerpt": utils.excerpt,
        "filesize": utils.human_size,
        "contrast": utils.contrast_text,
        "nl2br": utils.nl2br,
    }
    app.jinja_env.filters.update(filters)

    app.jinja_env.globals.update(
        icon=icons.icon, state_icon=icons.state_icon, priority_icon=icons.priority_icon, charts=charts,
        csrf_token=csrf_token, M=models, IQ=issue_query, activity_text=activity_text, version=__version__,
        can=utils.can, project_role=utils.project_role, is_favorite=utils.is_favorite,
        markdown_toc=utils.markdown_toc, greeting=utils.greeting, section_of=section_of,
        ai_client_config=_ai_client_config,
        fmtiso=lambda s: utils.fmt_date(__import__("datetime").date.fromisoformat(str(s)[:10]), True) if s else "—",
    )

    @app.context_processor
    def _inject():
        ctx = {
            "icon": icons.icon,
            "state_icon": icons.state_icon,
            "priority_icon": icons.priority_icon,
            "charts": charts,
            "csrf_token": csrf_token,
            "M": models,
            "IQ": issue_query,
            "activity_text": activity_text,
            "today": __import__("datetime").date.today(),
            "version": __version__,
            "can": utils.can,
            "project_role": utils.project_role,
            "is_favorite": utils.is_favorite,
            "markdown_toc": utils.markdown_toc,
            "greeting": utils.greeting,
            "qs": lambda **kw: issue_query.query_string(request.args, **kw),
        }
        if current_user.is_authenticated:
            ctx["ws"] = models.Workspace.get()
            ctx["sidebar"] = _sidebar_data
            ctx["picker_data"] = _picker_data
        return ctx


SECTIONS = {"agenda": "agenda", "wiki": "wiki", "qcm": "qcm", "ai": "ia", "settings": "reglages", "mail": "reglages"}


def section_of(endpoint: str | None) -> str:
    """Section de la barre d'icônes (Agenda, Travail, Wiki, QCM, IA, Réglages) d'une page.

    Une page peut l'imposer avec `{% set nav_section = "wiki" %}` en tête de son gabarit."""
    endpoint = endpoint or ""
    if endpoint in ("home.notifications", "mail.compose"):
        return "agenda" if endpoint == "home.notifications" else "travail"
    return SECTIONS.get(endpoint.split(".")[0], "travail")


def _ai_client_config() -> dict:
    try:
        from .ai import client_config
        return client_config() if current_user.is_authenticated else {}
    except Exception:  # l'assistant ne doit jamais empêcher une page de s'afficher
        return {}


def _sidebar_data():
    """Données de la barre latérale (calculées une fois par requête)."""
    if "sidebar" in g:
        return g.sidebar
    from .models import Cycle, Favorite, Module, Notification, Page, Project, SavedView
    from .utils import joined_projects

    favs = []
    for f in Favorite.query.filter_by(user_id=current_user.id).order_by(Favorite.created_at):
        if f.entity_type == "project":
            p = db.session.get(Project, f.entity_id)
            if p and not p.archived_at:
                favs.append({"title": p.name, "emoji": p.emoji, "url": url_for("issues.index", pid=p.id)})
        elif f.entity_type == "cycle":
            c = db.session.get(Cycle, f.entity_id)
            if c:
                favs.append({"title": c.name, "icon": "cycle", "url": url_for("cycles.detail", pid=c.project_id, cid=c.id)})
        elif f.entity_type == "module":
            m = db.session.get(Module, f.entity_id)
            if m:
                favs.append({"title": m.name, "icon": "module", "url": url_for("modules.detail", pid=m.project_id, mid=m.id)})
        elif f.entity_type == "page":
            pg = db.session.get(Page, f.entity_id)
            if pg:
                favs.append({"title": pg.title, "emoji": pg.emoji, "url": url_for("pages.view", pid=pg.project_id, page_id=pg.id)})
        elif f.entity_type == "view":
            v = db.session.get(SavedView, f.entity_id)
            if v:
                url = (url_for("saved_views.open", pid=v.project_id, vid=v.id) if v.project_id
                       else url_for("saved_views.open_global", vid=v.id))
                favs.append({"title": v.name, "icon": "eye", "url": url})
    unread = Notification.query.filter(
        Notification.user_id == current_user.id, Notification.read_at.is_(None),
        Notification.archived_at.is_(None)).count()
    g.sidebar = {"projects": joined_projects(current_user), "favorites": favs, "unread": unread}
    return g.sidebar


def _picker_data():
    """Options des sélecteurs (états, étiquettes, membres...) pour les projets visibles."""
    if "picker" in g:
        return g.picker
    from .models import ESTIMATE_TYPES
    from .utils import can, visible_projects

    out = {}
    for p in visible_projects(current_user):
        out[p.id] = {
            "id": p.id, "identifier": p.identifier, "name": p.name, "emoji": p.emoji,
            "states": [{"id": s.id, "name": s.name, "color": s.color, "group": s.group} for s in p.states],
            "labels": [{"id": l.id, "name": l.name, "color": l.color} for l in p.labels],
            "members": [{"id": u.id, "name": u.display_name, "initials": u.initials, "color": u.avatar_color}
                        for u in p.member_users],
            "cycles": [{"id": c.id, "name": c.name, "status": c.status} for c in p.cycles
                       if c.archived_at is None and c.status != "completed"],
            "all_cycles": [{"id": c.id, "name": c.name, "status": c.status} for c in p.cycles if c.archived_at is None],
            "modules": [{"id": m.id, "name": m.name} for m in p.modules if m.archived_at is None],
            "estimates": [{"id": v, "name": lab} for v, lab in ESTIMATE_TYPES.get(p.estimate_type or "none")[1]],
            "default_state": (p.default_state().id if p.default_state() else None),
            "editable": can(current_user, "edit", p),
            "intake": bool(p.intake_enabled and can(current_user, "comment", p)),
        }
    g.picker = out
    return out
