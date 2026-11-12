"""Paramètres : profil, mot de passe, préférences, espace de travail, jetons d'API, sauvegardes."""
import os
import shutil
import sqlite3
from datetime import datetime

from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template, request, send_file,
                   url_for)
from flask_login import current_user, login_required

from .. import sauvegarde
from ..extensions import db
from ..models import PALETTE, APIToken, User, Workspace, now
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
        u.avatar_color = request.form.get("avatar_color") or u.avatar_color
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
    return render_template("settings/profile.html", PALETTE=PALETTE, must_change=request.args.get("must_change"))


@bp.route("/password", methods=["POST"])
@login_required
def password():
    u = current_user
    if not u.check_password(request.form.get("current", "")):
        flash("Mot de passe actuel incorrect.", "error")
    elif len(request.form.get("new", "")) < 6:
        flash("Le nouveau mot de passe doit contenir au moins 6 caractères.", "error")
    elif request.form.get("new") != request.form.get("confirm"):
        flash("Les deux mots de passe ne correspondent pas.", "error")
    elif request.form.get("new") == DEMO_PASSWORD:
        flash("Ce mot de passe est celui de la démonstration, connu de tous : choisis-en un autre.", "error")
    else:
        u.set_password(request.form["new"])
        u.must_change_password = False
        db.session.commit()
        flash("Mot de passe modifié.", "success")
        return redirect(url_for("agenda.dashboard"))
    return redirect(url_for("settings.profile", must_change=1 if u.must_change_password else None))


@bp.route("/workspace", methods=["GET", "POST"])
@login_required
def workspace():
    require_admin()
    ws = Workspace.get()
    if request.method == "POST":
        ws.name = (request.form.get("name") or ws.name).strip()[:120]
        ws.emoji = (request.form.get("emoji") or ws.emoji).strip()[:16]
        ws.description = request.form.get("description", "")
        ws.allow_signup = bool(request.form.get("allow_signup"))
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
    return render_template("settings/workspace.html", ws=ws, stats=stats)


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
            make_backup()  # filet de sécurité
            db.session.remove()
            db.engine.dispose()
            src = sqlite3.connect(path)
            dst = sqlite3.connect(_db_path())
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
            flash(f"Base restaurée depuis {name}. Une sauvegarde de l’état précédent a été créée.", "success")
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


@bp.route("/backup/download")
@login_required
def backup_download():
    """Une sauvegarde de la liste ; sans nom : la base actuelle, sans secrets et sans rien garder sur le disque."""
    require_admin()
    name = request.args.get("name")
    if name:
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
    return render_template("settings/about.html", now=now(), shutil=shutil)
