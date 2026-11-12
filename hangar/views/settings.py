"""Paramètres : profil, mot de passe, préférences, espace de travail, jetons d'API, sauvegardes."""
import os
import shutil
import secrets
import sqlite3
from datetime import datetime

from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template, request, send_file,
                   url_for)
from flask import has_request_context
from flask_login import current_user, login_required, logout_user

from .. import sauvegarde
from ..extensions import db
from ..models import MOT_DE_PASSE_MIN, PALETTE, APIToken, User, Workspace, now
from ..securite import clean_color
from ..seed import DEMO_PASSWORD
from ..services import to_int
from ..utils import human_size, require_admin

bp = Blueprint("settings", __name__, url_prefix="/settings")


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        u = current_user
        username = (request.form.get("username") or "").strip().lower()
        if username != u.username and User.query.filter_by(username=username).first():
            flash("Cet identifiant est déjà utilisé.", "error")
            return redirect(url_for("settings.profile"))
        import re
        if not re.fullmatch(r"[a-z0-9_.\-]{2,60}", username):
            flash("Identifiant invalide.", "error")
            return redirect(url_for("settings.profile"))
        if username != u.username:
            u.username, u.email = username, User.internal_email(username)
        u.first_name = request.form.get("first_name", "").strip()
        u.last_name = request.form.get("last_name", "").strip()
        u.job_title = request.form.get("job_title", "").strip()
        u.bio = request.form.get("bio", "")
        u.avatar_color = clean_color(request.form.get("avatar_color"), u.avatar_color)
        u.theme = request.form.get("theme") if request.form.get("theme") in ("system", "light", "dark") else "system"
        u.capacity_hours = to_int(request.form.get("capacity_hours")) or u.capacity_hours
        if request.form.get("email_section"):
            from ..mailer import valid_email
            contact = (request.form.get("contact_email") or "").strip()
            if contact and not valid_email(contact):
                flash("Adresse e-mail de notification invalide : elle n’a pas été enregistrée.", "error")
            else:
                u.contact_email = contact or None
            if request.form.get("email_prefs") in ("instant", "digest", "off"):
                u.email_prefs = request.form.get("email_prefs")
            u.email_reminders = bool(request.form.get("email_reminders"))
        db.session.commit()
        flash("Profil mis à jour.", "success")
        return redirect(url_for("settings.profile"))
    return render_template("settings/profile.html", PALETTE=PALETTE, must_change=request.args.get("must_change"),
                           telephone=_telephone())


def _telephone() -> dict:
    """Section « Sur ton téléphone » : adresse à ouvrir, son QR code et le certificat de Hangar (hangar/reseau_local.py)."""
    from .. import qr, reseau_local
    adresse = reseau_local.adresse_telephone(current_app, request)
    info = {"adresse": adresse, "qr": None, "certificat": None, "empreinte": None}
    if adresse:
        info["qr"] = qr.svg(adresse["url"], taille_px=180)
    fichier = reseau_local.certificat_racine(current_app.config["DATA_DIR"])
    if fichier.is_file():
        try:
            info["empreinte"] = reseau_local.empreinte(fichier.read_bytes())
            info["certificat"] = url_for("settings.certificat")
        except (OSError, ValueError):
            pass
    return info


@bp.route("/certificat-hangar.crt")
def certificat():
    """Certificat de l'autorité locale de Caddy (--lan-https), à installer sur le téléphone. Public, sans
    connexion : le téléphone en a besoin avant de faire confiance au site, et il ne contient aucun secret."""
    from .. import reseau_local
    fichier = reseau_local.certificat_racine(current_app.config["DATA_DIR"])
    if not fichier.is_file():
        abort(404, description="Pas encore de certificat : lance Hangar avec scripts\\partager-en-https.bat.")
    return send_file(fichier, mimetype="application/x-x509-ca-cert", as_attachment=True,
                     download_name=reseau_local.NOM_CERTIFICAT, max_age=0)


@bp.route("/password", methods=["POST"])
@login_required
def password():
    u = current_user
    if not u.check_password(request.form.get("current", "")):
        flash("Mot de passe actuel incorrect.", "error")
    elif len(request.form.get("new", "")) < MOT_DE_PASSE_MIN:
        flash(f"Le nouveau mot de passe doit contenir au moins {MOT_DE_PASSE_MIN} caractères.", "error")
    elif request.form.get("new") != request.form.get("confirm"):
        flash("Les deux mots de passe ne correspondent pas.", "error")
    elif request.form.get("new") == DEMO_PASSWORD:
        flash("Ce mot de passe est celui de la démonstration, connu de tous : choisis-en un autre.", "error")
    else:
        u.set_password(request.form["new"])
        u.must_change_password = False
        db.session.commit()
        # L'adresse d'abonnement à l'agenda est gardée : un changement de routine (mot de passe imposé au premier
        # lancement…) ne doit pas casser sans bruit l'agenda du téléphone. En cas de vol, « Déconnecter mes
        # autres appareils » la change (fermer_sessions), et la page Profil le dit.
        abonnement = _a_un_abonnement(u)  # avant _garder_cette_session, qui remplace l'utilisateur courant
        _garder_cette_session(u)  # les autres navigateurs et cookies « Rester connecté » sont déconnectés
        flash("Mot de passe modifié. Tes autres navigateurs sont déconnectés."
              + (" L’adresse d’abonnement à ton agenda, elle, marche toujours : si un appareil a été perdu ou volé, "
                 "clique aussi « Déconnecter mes autres appareils » (Mon profil)." if abonnement else ""), "success")
        return redirect(url_for("agenda.dashboard"))
    return redirect(url_for("settings.profile", must_change=1 if u.must_change_password else None))


def _garder_cette_session(u) -> None:
    """Après un changement de mot de passe ou de `session_version`, toutes les sessions du compte sont caduques
    (models.User.get_id) : celle-ci est rouverte, avec « Rester connecté » seulement si ce cookie existait.
    Une session ouverte par l'application Android reste liée à son jeton (révoqué ou non)."""
    from flask import session
    from flask_login import login_user

    from ..appareils import SESSION_JETON
    jeton = session.get(SESSION_JETON)
    nom = current_app.config.get("REMEMBER_COOKIE_NAME", "remember_token")
    login_user(u, remember=nom in request.cookies)
    if jeton is not None:
        session[SESSION_JETON] = jeton


def _a_un_abonnement(u) -> bool:
    """Le compte a-t-il une adresse d'abonnement à l'agenda active ? (sans créer ses réglages d'agenda)"""
    from ..models import AgendaSettings
    s = db.session.get(AgendaSettings, u.id)
    return bool(s and s.feed_token)


@bp.route("/sessions/fermer", methods=["POST"])
@login_required
def fermer_sessions():
    """« Déconnecter mes autres appareils » : navigateurs et cookies « Rester connecté », sauf celui-ci ;
    les applications Android (jetons d'appareil) seulement si la case est cochée."""
    u = current_user
    u.session_version = (u.session_version or 0) + 1
    n = 0
    if request.form.get("appareils"):
        from ..appareils import JetonAppareil
        n = JetonAppareil.query.filter_by(user_id=u.id).delete(synchronize_session=False)
    # L'adresse d'abonnement .ics (lecture de tout l'agenda) est un accès de plus d'un appareil : on la change
    # (jamais on n'en crée une), sinon un téléphone perdu ou volé continuerait de recevoir l'agenda.
    abonnement = _a_un_abonnement(u)
    db.session.commit()
    if abonnement:
        from ..agenda import regenerate_feed_token
        regenerate_feed_token(u)
    _garder_cette_session(u)
    flash("Tes autres navigateurs sont déconnectés" + (f", ainsi que {n} application(s) Android" if n else "")
          + "." + (" L’adresse d’abonnement à ton agenda a changé : recopie la nouvelle dans ton téléphone "
                   "(Emploi du temps › S’abonner depuis mon téléphone)." if abonnement else ""), "success")
    return redirect(url_for("settings.profile") + "#sessions")


@bp.route("/workspace", methods=["GET", "POST"])
@login_required
def workspace():
    require_admin()
    ws = Workspace.get()
    if request.method == "POST" and request.form.get("action") == "coffre":
        return _changer_coffre()
    if request.method == "POST":
        ws.name = (request.form.get("name") or ws.name).strip()[:120]
        ws.emoji = (request.form.get("emoji") or ws.emoji).strip()[:16]
        ws.description = request.form.get("description", "")
        ws.allow_signup = bool(request.form.get("allow_signup"))
        ws.login_accounts = bool(request.form.get("login_accounts"))
        db.session.commit()
        flash("Espace de travail mis à jour.", "success")
        return redirect(url_for("settings.workspace"))
    stats = {
        "users": User.query.count(),
        "db_size": human_size(os.path.getsize(_db_path())) if os.path.exists(_db_path()) else "?",
        "uploads": human_size(sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in
                                  os.walk(current_app.config["UPLOAD_DIR"]) for f in fs)),
        "data_dir": current_app.config["DATA_DIR"],
    }
    from ..wiki import coffre
    origine = current_app.config.get("WIKI_ORIGINE", coffre.DEFAUT)
    vault = {"dossier": current_app.config["WIKI_DIR"], "origine": origine, "texte": coffre.ORIGINES.get(origine, ""),
             "introuvable": coffre.introuvable(current_app.config), "local": _requete_locale()}
    return render_template("settings/workspace.html", ws=ws, stats=stats, vault=vault)


def _requete_locale() -> bool:
    """La page est ouverte sur l'ordinateur où tourne Hangar (pas depuis un autre poste du réseau)."""
    from ..blocage_connexion import ip_client  # derrière un proxy : l'adresse du vrai visiteur
    return ip_client() in ("127.0.0.1", "::1", "localhost")


def _changer_coffre():
    """Choisit le dossier du coffre du wiki (mémorisé dans data/coffre.txt), sans redémarrer. Seulement depuis
    l'ordinateur de Hangar : un dossier choisi devient lisible par tous les comptes dans le wiki."""
    from ..wiki import coffre
    if not _requete_locale():
        flash("Le coffre ne se change que depuis l’ordinateur où tourne Hangar.", "error")
        return redirect(url_for("settings.workspace"))
    data = current_app.config["DATA_DIR"]
    defaut = bool(request.form.get("defaut"))
    try:
        retenu = coffre.memoriser(data, None if defaut else request.form.get("dossier", ""))
    except FileNotFoundError:
        flash("Ce dossier n’existe pas. Copie son chemin depuis la barre d’adresse de l’Explorateur, et vérifie "
              "que le disque ou la clé est branché.", "error")
        return redirect(url_for("settings.workspace") + "#coffre")
    except (OSError, ValueError) as exc:
        flash(f"Dossier refusé : {exc}", "error")
        return redirect(url_for("settings.workspace") + "#coffre")
    dossier, origine = coffre.choisir(data)
    coffre.appliquer(current_app, dossier, origine)
    if origine == coffre.VARIABLE:
        flash("Choix enregistré, mais la variable HANGAR_VAULT est définie et passe avant : retire-la pour qu’il "
              "s’applique.", "info")
    elif retenu:
        flash(f"Le wiki utilise maintenant le coffre {retenu}. Ce choix est gardé pour les prochains lancements.",
              "success")
    else:
        flash("Le wiki utilise de nouveau le dossier par défaut (data/vault).", "success")
    return redirect(url_for("settings.workspace") + "#coffre")


@bp.route("/tokens", methods=["GET", "POST"])
@login_required
def tokens():
    new_token = None
    if request.method == "POST":
        if request.form.get("delete"):
            t = db.session.get(APIToken, to_int(request.form.get("delete")) or 0)
            if t and t.user_id == current_user.id:
                db.session.delete(t)
                db.session.commit()
                flash("Jeton révoqué.", "info")
            return redirect(url_for("settings.tokens"))
        name = (request.form.get("name") or "").strip() or "Jeton"
        days = to_int(request.form.get("days"))
        t, raw = APIToken.generate(current_user, name[:120], days)
        db.session.add(t)
        db.session.commit()
        new_token = raw
        flash("Jeton créé : copie-le maintenant, il ne sera plus affiché.", "success")
    toks = APIToken.query.filter_by(user_id=current_user.id).order_by(APIToken.created_at.desc()).all()
    return render_template("settings/tokens.html", tokens=toks, new_token=new_token)


def _db_path():
    return os.path.join(current_app.config["DATA_DIR"], "hangar.db")


# Copie de la base seule : gardée ici sous ce nom pour les commandes (cli.py) qui l'importent.
make_backup = sauvegarde.make_backup


def restaurer_base(chemin: str) -> list[str]:
    """Remplace la base par la sauvegarde ``chemin`` (déjà vérifiée par ``sauvegarde.verifier_base``), après
    une sauvegarde de sécurité de l'état actuel, puis la met au niveau du code sans redémarrer (sauvegarde
    d'une version plus ancienne : tables et colonnes ajoutées depuis). Toutes les sessions ouvertes sont fermées
    (jeton des sessions changé) : leur numéro de compte peut désigner quelqu'un d'autre dans la base restaurée. Renvoie les changements de
    structure appliqués (vide si la sauvegarde était déjà au niveau)."""
    from .. import migrer_schema
    from ..models import Workspace as _Workspace
    make_backup()  # filet de sécurité
    db.session.remove()
    db.engine.dispose()
    src = sqlite3.connect(chemin)
    dst = sqlite3.connect(_db_path())
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()
    db.engine.dispose()
    changements = migrer_schema()
    # Nouveau jeton : toutes les sessions ouvertes (autres postes, cookies « se souvenir de moi ») sont fermées.
    _Workspace.get().jeton_sessions = secrets.token_hex(12)
    db.session.commit()
    from ..extensions import vider_journal
    vider_journal()
    current_app.config.pop("_HAS_USERS", None)  # la base restaurée peut n'avoir aucun compte
    if has_request_context():
        logout_user()
    return changements


def _nom_sauvegarde(name: str | None) -> str | None:
    """Nom d'une sauvegarde de data/backups (base .db, complète .zip ou dossier de fichiers mis de côté
    avant un effacement), ou None s'il n'en est pas une."""
    name = os.path.basename(name or "")
    if name.endswith(".db") or sauvegarde.MOTIF_COMPLET.match(name) or sauvegarde.MOTIF_FICHIERS.match(name):
        return name
    return None


def _signaler_manquants(non_copies: list[str], ou: str) -> None:
    """Prévient que des fichiers manquent dans une sauvegarde complète (verrouillés, illisibles…)."""
    if non_copies:
        flash(f"Sauvegarde incomplète : {len(non_copies)} fichier(s) n’ont pas pu être lus et manquent dans {ou} : "
              + ", ".join(non_copies[:5]) + (" …" if len(non_copies) > 5 else "")
              + f". La liste est dans {sauvegarde.MANQUANTS}, dans l’archive. Ferme les logiciels qui les "
              "utilisent (Word, lecteur de PDF, Obsidian…), puis recommence.", "error")


@bp.route("/backup", methods=["GET", "POST"])
@login_required
def backup():
    require_admin()
    folder = current_app.config["BACKUP_DIR"]
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            path = make_backup()
            flash(f"Sauvegarde de la base créée : {os.path.basename(path)}", "success")
        elif action == "create_full":
            try:
                bilan = sauvegarde.make_full_backup()
            except (OSError, sqlite3.Error) as exc:  # disque plein…
                flash(f"La sauvegarde complète a échoué : {exc}", "error")
                return redirect(url_for("settings.backup"))
            flash(f"Sauvegarde complète créée : {os.path.basename(bilan.chemin)} ({bilan.fichiers} fichiers).",
                  "success")
            _signaler_manquants(bilan.non_copies, "l’archive")
        elif action == "delete":
            name = _nom_sauvegarde(request.form.get("name"))
            try:
                if name and sauvegarde.supprimer(folder, name):
                    flash("Sauvegarde supprimée.", "info")
            except OSError as exc:  # fichier ouvert dans un autre logiciel (Windows)…
                fichier = os.path.basename(getattr(exc, "filename", None) or name)
                flash(f"Suppression impossible : « {fichier} » est peut-être ouvert dans un autre logiciel. "
                      "Ferme-le, puis recommence.", "error")
        elif action == "restore":
            name = os.path.basename(request.form.get("name", ""))
            path = os.path.join(folder, name)
            if not (name.endswith(".db") and os.path.exists(path)):
                abort(404)
            probleme = sauvegarde.verifier_base(path)
            if probleme:
                flash(f"Restauration refusée : {probleme}. Ta base actuelle n’a pas été touchée.", "error")
                return redirect(url_for("settings.backup"))
            changements = restaurer_base(path)
            flash(f"Base restaurée depuis {name}. Une sauvegarde de l’état précédent a été créée. Tout le monde a "
                  "été déconnecté, sur tous les postes : reconnecte-toi avec un compte de la sauvegarde.", "success")
            if changements:
                flash("Cette sauvegarde venait d’une version plus ancienne de Hangar : elle a été mise à jour "
                      "automatiquement, pas besoin de redémarrer.", "info")
            cote = sauvegarde.dossier_fichiers(path)
            if cote.is_dir():
                flash(f"Les fichiers mis de côté avec cette sauvegarde (pièces jointes, sets perso…) sont dans "
                      f"{cote} : Hangar arrêté, remets les dossiers qu’il contient dans {current_app.config['DATA_DIR']}.",
                      "info")
            return redirect(url_for("auth.login"))
        return redirect(url_for("settings.backup"))
    files = [dict(f, size=human_size(f["octets"])) for f in sauvegarde.liste(folder)]
    data_dir = current_app.config["DATA_DIR"]
    vault = current_app.config["WIKI_DIR"]
    vault_ailleurs = os.path.realpath(vault) != os.path.realpath(os.path.join(data_dir, "vault"))
    return render_template("settings/backup.html", files=files, data_dir=data_dir, vault=vault,
                           vault_ailleurs=vault_ailleurs)


def _envoyer(fichier, nom: str, mimetype: str):
    return send_file(fichier, as_attachment=True, download_name=nom, mimetype=mimetype, max_age=0)


@bp.route("/backup/download", methods=["GET", "POST"])
@login_required
def backup_download():
    """Une sauvegarde de la liste (GET ?name=…) ; sans nom : la base actuelle, sans secrets et sans rien garder
    sur le disque. Cette copie demande un POST (jeton CSRF) : jamais sur un simple lien ou une image glissée
    dans une page ; un GET sans nom ramène à la page des sauvegardes."""
    require_admin()
    name = request.args.get("name")
    if request.method == "GET":
        if not name:
            return redirect(url_for("settings.backup"))
        name = _nom_sauvegarde(name)
        path = os.path.join(current_app.config["BACKUP_DIR"], name or "")
        if not name or not os.path.isfile(path):
            abort(404)
        return send_file(path, as_attachment=True, download_name=name)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return _envoyer(sauvegarde.fichier_temporaire(sans_secrets=True, complet=False)[0], f"hangar_{stamp}.db",
                    "application/vnd.sqlite3")


@bp.route("/backup/telecharger", methods=["POST"])
@login_required
def backup_now():
    """« Tout télécharger » : sauvegarde complète (.zip) ou base seule (.db), avec ou sans les secrets.
    Le fichier est préparé dans un fichier temporaire : rien n'est ajouté à data/backups/."""
    require_admin()
    complet = request.form.get("format") != "db"
    sans_secrets = not request.form.get("secrets")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fichier, bilan = sauvegarde.fichier_temporaire(sans_secrets=sans_secrets, complet=complet)
    # Affiché au prochain chargement d'une page (le navigateur reste sur la page pendant le téléchargement)
    _signaler_manquants(bilan.non_copies, "l’archive téléchargée")
    if complet:
        return _envoyer(fichier, f"hangar_complet_{stamp}.zip", "application/zip")
    return _envoyer(fichier, f"hangar_{stamp}.db", "application/vnd.sqlite3")


@bp.route("/backup/export.json")
@login_required
def export_json():
    """Export lisible des projets (partie Travail), envoyé directement : rien n'est écrit dans data/."""
    require_admin()
    from ..importer import export_workspace_json
    stamp = datetime.now().strftime("%Y%m%d")
    return Response(export_workspace_json(), mimetype="application/json",
                    headers={"Content-Disposition": f'attachment; filename="hangar_projets_{stamp}.json"'})


@bp.route("/about")
@login_required
def about():
    from .. import lancement
    lance, disque = lancement.code_au_demarrage(), lancement.code_sur_le_disque()
    date = ""
    if lance.get("date"):
        try:
            date = datetime.fromisoformat(lance["date"]).strftime("%d/%m/%Y")
        except ValueError:
            pass
    return render_template("settings/about.html", now=now(), shutil=shutil,
                           version_lancee=lancement.description(lance), version_disque=lancement.description(disque),
                           version_commit=(lance.get("commit") or "")[:12], version_date=date,
                           mise_a_jour=bool(lance.get("commit") and disque.get("commit")
                                            and lance["commit"] != disque["commit"]))
