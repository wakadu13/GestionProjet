"""Paramètres : profil, mot de passe, préférences, espace de travail, jetons d'API, sauvegardes."""
import os
import shutil
import sqlite3
from datetime import datetime

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import PALETTE, APIToken, User, Workspace, now
from ..securite import clean_color
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


def make_backup() -> str:
    """Copie cohérente de la base SQLite (API de sauvegarde) dans data/backups/."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(current_app.config["BACKUP_DIR"], f"hangar_{stamp}.db")
    src = sqlite3.connect(_db_path())
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    return dest


@bp.route("/backup", methods=["GET", "POST"])
@login_required
def backup():
    require_admin()
    folder = current_app.config["BACKUP_DIR"]
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            path = make_backup()
            flash(f"Sauvegarde créée : {os.path.basename(path)}", "success")
        elif action == "delete":
            name = os.path.basename(request.form.get("name", ""))
            path = os.path.join(folder, name)
            if name.endswith(".db") and os.path.exists(path):
                os.remove(path)
                flash("Sauvegarde supprimée.", "info")
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
            return redirect(url_for("auth.login"))
        return redirect(url_for("settings.backup"))
    files = []
    for name in sorted(os.listdir(folder), reverse=True):
        if name.endswith(".db"):
            p = os.path.join(folder, name)
            files.append({"name": name, "size": human_size(os.path.getsize(p)),
                          "date": datetime.fromtimestamp(os.path.getmtime(p))})
    return render_template("settings/backup.html", files=files)


@bp.route("/backup/download", methods=["GET", "POST"])
@login_required
def backup_download():
    """Une sauvegarde existante (GET ?name=…) ou une copie de la base actuelle (POST : elle crée un fichier,
    donc jamais sur un simple lien ou une image glissée dans une page)."""
    require_admin()
    name = request.args.get("name")
    if request.method == "POST":
        path = make_backup()
    elif name:
        path = os.path.join(current_app.config["BACKUP_DIR"], os.path.basename(name))
        if not os.path.exists(path):
            abort(404)
    else:
        return redirect(url_for("settings.backup"))
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


@bp.route("/backup/export.json")
@login_required
def export_json():
    require_admin()
    from ..importer import export_workspace_json
    stamp = datetime.now().strftime("%Y%m%d")
    data = export_workspace_json()
    tmp = os.path.join(current_app.config["BACKUP_DIR"], f"export_{stamp}.json")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
    return send_file(tmp, as_attachment=True, download_name=f"hangar_export_{stamp}.json",
                     mimetype="application/json")


@bp.route("/about")
@login_required
def about():
    return render_template("settings/about.html", now=now(), shutil=shutil)
