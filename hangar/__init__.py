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

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=_secret_key(data),
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{data / 'hangar.db'}",
        SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"timeout": 15, "check_same_thread": False}},
        DATA_DIR=str(data),
        UPLOAD_DIR=str(data / "uploads"),
        BACKUP_DIR=str(data / "backups"),
        # Wiki (coffre Obsidian) : dossier de notes, réglable avec HANGAR_VAULT (ex. un coffre Obsidian existant)
        WIKI_DIR=str(Path(os.environ.get("HANGAR_VAULT") or data / "vault").resolve()),
        WIKI_HOME=os.environ.get("HANGAR_WIKI_HOME", "Accueil"),
        MAX_CONTENT_LENGTH=50 * 1024 * 1024,
        SESSION_COOKIE_SAMESITE="Lax",
        REMEMBER_COOKIE_SAMESITE="Lax",  # cookie « Rester connecté » : pas envoyé depuis un autre site
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
    Path(app.config["WIKI_DIR"]).mkdir(parents=True, exist_ok=True)

    with app.app_context():
        db.create_all()
        _auto_migrate()
        models.Workspace.get()

    @login_manager.user_loader
    def load_user(uid):
        u = db.session.get(models.User, int(uid))
        return u if u and u.active else None

    from . import securite, web
    # securite d'abord : ses fonctions after_request passent après celles de web (ordre inverse de Flask)
    securite.init_app(app)
    web.init_app(app)  # sécurité, cache des fichiers statiques, icônes, compression (avant les blueprints)
    _register_blueprints(app)
    _register_template_helpers(app)
    _register_hooks(app)
    return app


def _auto_migrate() -> None:
    """Ajoute aux tables existantes les colonnes déclarées dans les modèles mais absentes de la base.

    Permet de faire évoluer le modèle sans perdre les données (SQLite : ALTER TABLE ADD COLUMN).
    """
    from sqlalchemy import inspect, text
    insp = inspect(db.engine)
    changed = False
    for table in db.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
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
            changed = True
    if changed:
        db.session.commit()


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
        if request.endpoint in ("static", "icones"):
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

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(405)
    @app.errorhandler(413)
    def _http_error(err):
        description = getattr(err, "description", None)
        if not description or description == getattr(type(err), "description", None):
            # Texte anglais par défaut de Werkzeug (abort(403) sans explication) : on le remplace.
            description = HTTP_MESSAGES.get(err.code, "La demande n’a pas abouti.")
        # 405 : les méthodes acceptées, comme dans la réponse d'origine
        headers = {"Allow": ", ".join(err.valid_methods)} if getattr(err, "valid_methods", None) else {}
        if request.path.startswith("/api/"):
            return {"ok": False, "error": description}, err.code, headers
        return render_template("errors.html", error=err, description=description, message=description), err.code, headers

    @app.errorhandler(500)
    def _server_error(err):
        db.session.rollback()
        from .wiki import erreurs  # page /erreurs/ des administrateurs
        erreurs.noter(getattr(err, "original_exception", None), request=request)
        if request.path.startswith("/api/"):
            return {"ok": False, "error": "Erreur interne"}, 500
        return render_template("errors.html", error=err), 500


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
            "can_delete_issues": utils.can_delete_issues,
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


# Explication affichée quand une erreur n'en donne pas (les textes par défaut de Werkzeug sont en anglais).
HTTP_MESSAGES = {
    400: "La demande n’a pas pu être traitée. Recharge la page et réessaie.",
    403: "Tu n’as pas accès à cette page ou à cette action. Demande à un administrateur si tu en as besoin.",
    404: "Cette page n’existe pas ou plus. Vérifie l’adresse, ou repars de l’accueil.",
    405: "Cette adresse ne s’ouvre pas directement : passe par le bouton prévu dans l’application.",
    413: "Fichier trop volumineux.",
}


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
    from .models import Favorite, Notification
    from .utils import favorite_target, joined_projects

    favs = []
    for f in Favorite.query.filter_by(user_id=current_user.id).order_by(Favorite.created_at):
        # Seuls les objets encore visibles (accès retiré, page devenue privée…) : jamais un titre privé.
        obj = favorite_target(current_user, f.entity_type, f.entity_id)
        if obj is None:
            continue
        if f.entity_type == "project":
            if not obj.archived_at:
                favs.append({"title": obj.name, "emoji": obj.emoji, "url": url_for("issues.index", pid=obj.id)})
        elif f.entity_type == "cycle":
            favs.append({"title": obj.name, "icon": "cycle", "url": url_for("cycles.detail", pid=obj.project_id, cid=obj.id)})
        elif f.entity_type == "module":
            favs.append({"title": obj.name, "icon": "module", "url": url_for("modules.detail", pid=obj.project_id, mid=obj.id)})
        elif f.entity_type == "page":
            favs.append({"title": obj.title, "emoji": obj.emoji, "url": url_for("pages.view", pid=obj.project_id, page_id=obj.id)})
        elif f.entity_type == "view":
            url = (url_for("saved_views.open", pid=obj.project_id, vid=obj.id) if obj.project_id
                   else url_for("saved_views.open_global", vid=obj.id))
            favs.append({"title": obj.name, "icon": "eye", "url": url})
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
