"""Application Android de Revision (« ProjetPluri ») branchée sur Hangar, et adresses de Revision.

Port de `accounts/api.py`, `accounts/models.py` et `accounts/middleware.py` de Revision : l'APK
(android/ dans Revision) fonctionne en pointant vers Hangar, avec les mêmes adresses, les mêmes
formats JSON et les mêmes en-têtes.

- `POST /api/appareil/connexion/` {"username", "password", "appareil"} → {"jeton", "joueur"} :
  identifiants contre un **jeton d'appareil** (seule son empreinte SHA-256 est gardée) ; les échecs
  comptent pour le blocage des connexions (`blocage_connexion`).
- `Authorization: Bearer <jeton>` : accepté seulement sur les adresses dont l'appli a besoin
  (`CHEMINS_AUTORISES` : QCM hors ligne, images des questions, partage vers le wiki, rappel) ;
  ailleurs → 403. Un jeton inconnu, révoqué ou d'un compte désactivé → 401. Le jeton n'est pas
  envoyé automatiquement par un navigateur : la vérification CSRF est levée pour ces requêtes.
- `POST /api/appareil/session/` : échange le jeton contre une session Flask-Login pour la WebView,
  **liée au jeton** (le révoquer ferme aussi cette session) ; `POST /api/appareil/deconnexion/`
  révoque le jeton porteur.
- `GET /api/status/` : {"status": "ok", "time": …} (surveillance du serveur, sans connexion).
- Jetons révoqués depuis « Mon profil » (`/compte/appareils/<id>/revoquer/`), et automatiquement
  au changement de mot de passe et à la désactivation du compte.
- L'appli reconnaît la page de connexion à son adresse `/connexion…` pour rouvrir la session avec le
  jeton : pour l'appli (User-Agent « ProjetPluriApp/ »), les renvois vers `/login` passent par
  `/connexion/`. « Se déconnecter » de Hangar ouvre les réglages de l'appli (qui révoque le jeton).
- Alias des adresses de Revision : `/aujourdhui/`, `/connexion/`, `/deconnexion/`, `/compte/`,
  `/compte/mot-de-passe/`, `/inscription/`, `/utilisateurs/`.

Le blueprint est enregistré par celui des QCM (`qcm/views.py`) ; ses tables sont créées avec celles
des QCM (`qcm/models.py`).
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from urllib.parse import urlencode, urlsplit

from flask import Blueprint, abort, flash, g, jsonify, redirect, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user, user_logged_in
from sqlalchemy import delete, event
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session as _SessionSA

from . import blocage_connexion as blocage
from .extensions import db
from .models import User

bp = Blueprint("appareils", __name__)

SESSION_JETON = "jeton_appareil"   # clé de session : le jeton dont vient une session ouverte par l'appli
PREFIXE = "Bearer "
CHEMINS_AUTORISES = (
    "/qcm/hors-ligne/catalogue/", "/qcm/hors-ligne/sets/", "/qcm/hors-ligne/synchro/",
    "/fichiers/", "/qcm/fichiers/", "/qcm/mon-espace/images/",
    "/api/appareil/deconnexion/", "/api/appareil/session/", "/importer/", "/api/rappel/",
)
AGENT_APPLI = "ProjetPluriApp/"
APPLI_LOCAL = "https://appassets.androidplatform.net/"  # pages embarquées dans l'APK
UTILISATION_MAJ_S = 3600  # « vu le … » mis à jour au plus une fois par heure


def _empreinte(cle: str) -> str:
    return hashlib.sha256(cle.encode()).hexdigest()


def _maintenant():
    return datetime.now().replace(microsecond=0)


class JetonAppareil(db.Model):
    """Jeton d'une application installée (APK Android) : remplace la session.

    Seule l'empreinte SHA-256 est gardée en base ; le jeton en clair n'est connu que du téléphone.
    Il est révoqué à la déconnexion de l'application, depuis « Mon profil », au changement de mot
    de passe et quand le compte est désactivé."""
    __tablename__ = "jeton_appareil"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    nom = db.Column(db.String(100), default="")
    empreinte = db.Column(db.String(64), nullable=False, unique=True)
    cree = db.Column(db.DateTime, default=_maintenant)
    utilise = db.Column(db.DateTime)

    user = db.relationship("User")

    @classmethod
    def creer(cls, user, nom=""):
        """Crée un jeton ; renvoie (objet, jeton en clair)."""
        cle = secrets.token_urlsafe(32)
        jeton = cls(user_id=user.id, nom=(nom or "")[:100], empreinte=_empreinte(cle))
        db.session.add(jeton)
        return jeton, cle

    @classmethod
    def trouver(cls, cle):
        return cls.query.filter_by(empreinte=_empreinte(cle)).first() if cle else None

    def __repr__(self):
        return f"<JetonAppareil {self.user_id} · {self.nom or 'appareil'}>"


def dans_appli() -> bool:
    """Page affichée dans l'application Android (WebView) ?"""
    return AGENT_APPLI in request.headers.get("User-Agent", "")


def jetons_de(user) -> list:
    if user is None or not getattr(user, "is_authenticated", False):
        return []
    return JetonAppareil.query.filter_by(user_id=user.id).order_by(JetonAppareil.cree.desc()).all()


# ---------------------------------------------------------------------------
# Avant / après chaque requête
# ---------------------------------------------------------------------------
def _exempter_csrf():
    """Requête sans cookie de navigateur à usurper (jeton d'appareil déjà vérifié, connexion de l'appli) :
    la vérification CSRF de Hangar (`_setup_and_csrf`, qui passe après ce crochet) est levée pour elle."""
    g.csrf_exempt = True
    g._session_vide = not session


@bp.before_app_request
def _avant():
    g.pop("jeton_appareil", None)
    entete = request.headers.get("Authorization", "")
    if entete.startswith(PREFIXE):
        if not request.path.startswith(CHEMINS_AUTORISES):
            return jsonify(error="jeton refusé sur cette adresse"), 403
        jeton = JetonAppareil.trouver(entete[len(PREFIXE):].strip())
        user = db.session.get(User, jeton.user_id) if jeton else None
        if jeton is None or user is None or not user.active:
            return jsonify(error="jeton invalide ou révoqué : reconnectez l'application"), 401
        now = _maintenant()
        if jeton.utilise is None or (now - jeton.utilise).total_seconds() > UTILISATION_MAJ_S:
            jeton.utilise = now
            db.session.commit()
        g._login_user = user  # Flask-Login : compte de cette requête seulement (pas de session)
        g.jeton_appareil = jeton
        _exempter_csrf()
        return None
    if request.path == "/api/appareil/connexion/":
        _exempter_csrf()
        return None
    jeton_id = session.get(SESSION_JETON)
    if jeton_id is not None and current_user.is_authenticated:
        # Session ouverte par l'application : fermée si son jeton a été révoqué.
        if JetonAppareil.query.filter_by(id=jeton_id, user_id=current_user.id).first() is None:
            logout_user()
            session.pop(SESSION_JETON, None)
    if request.endpoint == "auth.logout" and dans_appli() and current_user.is_authenticated:
        # Dans l'appli, se déconnecter = révoquer le jeton, depuis les réglages de l'appli.
        return redirect(APPLI_LOCAL + "connexion.html")
    return None


@bp.after_app_request
def _apres(response):
    if g.get("csrf_exempt") and g.pop("_session_vide", False) and not g.get("_ouverture_appareil"):
        session.clear()  # requête de l'appli sans cookie : n'en crée pas (Flask-Login peut y avoir écrit)
        session.modified = False
    if g.pop("jeton_appareil", None) is not None and not g.pop("_ouverture_appareil", False):
        g.pop("_login_user", None)  # compte de cette requête seulement
    if dans_appli() and 300 <= response.status_code < 400 and response.location:
        # L'appli reconnaît la connexion à « /connexion… » et rouvre la session avec son jeton.
        cible = urlsplit(response.location)
        if cible.path == url_for("auth.login") and (not cible.netloc or cible.netloc == request.host):
            response.location = "/connexion/" + (f"?{cible.query}" if cible.query else "")
    return response


@user_logged_in.connect
def _connexion_classique(_app, user=None, **_extra):
    """Connexion par mot de passe : la session n'est plus celle d'un jeton."""
    if not g.get("_ouverture_appareil"):
        session.pop(SESSION_JETON, None)


# ---------------------------------------------------------------------------
# Révocation automatique : mot de passe changé, compte désactivé
# ---------------------------------------------------------------------------
@event.listens_for(_SessionSA, "before_flush")
def _revoquer(sess, _contexte, _instances):
    comptes, mdp = set(), set()
    for obj in list(sess.dirty):
        if not isinstance(obj, User) or obj.id is None:
            continue
        etat = sa_inspect(obj)
        change_mdp = etat.attrs.password_hash.history.has_changes()
        desactive = etat.attrs.active.history.has_changes() and not obj.active
        if change_mdp or desactive:
            comptes.add(obj.id)
        if change_mdp:
            mdp.add((obj.username or "").lower())
    if comptes:
        sess.execute(delete(JetonAppareil).where(JetonAppareil.user_id.in_(comptes)))
    if mdp:
        # Nouveau mot de passe (par le compte ou un admin) : ses blocages de connexion sont levés.
        sess.execute(delete(blocage.EchecConnexion).where(blocage.EchecConnexion.identifiant.in_(mdp)))


# ---------------------------------------------------------------------------
# API de l'application
# ---------------------------------------------------------------------------
def _sans_cache(reponse):
    reponse.headers["Cache-Control"] = "no-store"
    return reponse


@bp.route("/api/appareil/connexion/", methods=["POST"])
def connexion():
    """{"username", "password", "appareil"} → {"jeton", "joueur"} ; les échecs comptent pour le blocage."""
    data = request.get_json(silent=True, force=True)
    try:
        username, password = str(data["username"]), str(data["password"])
    except (KeyError, TypeError):
        return _sans_cache(jsonify(error="requête invalide")), 400
    ident, ip = username.strip().lower(), blocage.ip_client()
    minutes = blocage.bloque(ident, ip)
    if minutes:
        return _sans_cache(jsonify(error=f"trop d'essais : compte bloqué {minutes} minutes")), 403
    user = User.query.filter(User.username == ident).first()
    if user is None or not user.check_password(password):
        blocage.noter_echec(ident, ip)
        return _sans_cache(jsonify(error="identifiant ou mot de passe incorrect")), 401
    if not user.active:
        return _sans_cache(jsonify(error="identifiant ou mot de passe incorrect")), 401
    blocage.reussite(ident, ip)
    _, cle = JetonAppareil.creer(user, str((data or {}).get("appareil", "")))
    user.last_login_at = _maintenant()
    from .seed import est_mot_de_passe_public
    if est_mot_de_passe_public(password):  # ancien mot de passe de démo, publié : à changer
        user.must_change_password = True
    db.session.commit()
    return _sans_cache(jsonify(jeton=cle, joueur=user.username))


@bp.route("/api/appareil/deconnexion/", methods=["POST"])
def deconnexion():
    """Révoque le jeton qui porte la requête."""
    jeton = g.get("jeton_appareil")
    if jeton is None:
        return _sans_cache(jsonify(error="jeton requis")), 400
    db.session.delete(jeton)
    db.session.commit()
    return _sans_cache(jsonify(ok=True))


@bp.route("/api/appareil/session/", methods=["POST"])
def session_appareil():
    """Ouvre une session Hangar pour la WebView de l'application (jeton → cookie de session),
    liée au jeton : révoquer le jeton la ferme aussi."""
    jeton = g.get("jeton_appareil")
    if jeton is None:
        return _sans_cache(jsonify(error="jeton requis")), 400
    user = db.session.get(User, jeton.user_id)
    g._ouverture_appareil = True
    login_user(user, remember=False)
    session.permanent = True
    session[SESSION_JETON] = jeton.id
    return _sans_cache(jsonify(ok=True, joueur=user.username))


@bp.route("/api/status/")
def statut():
    """Statut du serveur (moniteur externe, script de surveillance) : sans connexion."""
    return _sans_cache(jsonify(status="ok", time=datetime.now().astimezone().isoformat()))


# ---------------------------------------------------------------------------
# Mon profil : appareils connectés ; admins : connexions bloquées
# ---------------------------------------------------------------------------
@bp.route("/compte/appareils/<int:pk>/revoquer/", methods=["POST"])
@login_required
def revoquer(pk):
    """Déconnecte une application Android (son jeton ne marche plus)."""
    jeton = JetonAppareil.query.filter_by(id=pk, user_id=current_user.id).first() or abort(404)
    db.session.delete(jeton)
    db.session.commit()
    flash("Appareil déconnecté.", "success")
    return redirect(url_for("settings.profile") + "#appareils")


@bp.route("/compte/connexions/debloquer/", methods=["POST"])
@login_required
def debloquer():
    """Admins : lève un blocage de connexion (un identifiant, une adresse, ou tous)."""
    if not current_user.is_admin:
        abort(403)
    if request.form.get("tout") == "1":
        n = blocage.debloquer()
    else:
        ident = request.form.get("identifiant") or None
        ip = request.form.get("ip") or None
        if ident is None and ip is None:
            abort(400)
        n = blocage.debloquer(ident, ip)
    flash("Connexion débloquée." if n else "Rien à débloquer.", "success" if n else "info")
    return redirect(url_for("settings.profile") + "#connexions")


@bp.app_context_processor
def _gabarits():
    return {"appli_android": dans_appli, "appli_locale": APPLI_LOCAL, "jetons_appareil": jetons_de,
            "connexions_bloquees": blocage.bloques}


# ---------------------------------------------------------------------------
# Adresses de Revision → Hangar
# ---------------------------------------------------------------------------
def _avec_args(endpoint, ancre=""):
    url = url_for(endpoint)
    if request.args:
        url += "?" + urlencode(list(request.args.items(multi=True)))
    return redirect(url + ancre)


@bp.route("/aujourdhui/")
def alias_aujourdhui():
    return _avec_args("qcm.aujourdhui")


@bp.route("/connexion/")
def alias_connexion():
    return _avec_args("auth.login")


@bp.route("/deconnexion/", methods=["GET", "POST"])
def alias_deconnexion():
    return redirect(url_for("auth.logout"))


@bp.route("/compte/")
def alias_compte():
    return redirect(url_for("settings.profile"))


@bp.route("/compte/mot-de-passe/")
def alias_mot_de_passe():
    return redirect(url_for("settings.profile") + "#password")


@bp.route("/inscription/")
def alias_inscription():
    return redirect(url_for("auth.signup"))


@bp.route("/utilisateurs/")
def alias_utilisateurs():
    return redirect(url_for("team.index"))
