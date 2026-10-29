"""Connexion, déconnexion, première installation, inscription."""
import random
import re

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db
from ..models import PALETTE, User, Workspace, now
from ..utils import is_safe_next

bp = Blueprint("auth", __name__)


def _validate_user_fields(form, require_password=True):
    errors = []
    username = (form.get("username") or "").strip().lower()
    password = form.get("password") or ""
    if not re.fullmatch(r"[a-z0-9_.\-]{2,60}", username):
        errors.append("Identifiant invalide (2 à 60 caractères : lettres, chiffres, . _ -).")
    if require_password and len(password) < 6:
        errors.append("Le mot de passe doit contenir au moins 6 caractères.")
    if User.query.filter_by(username=username).first():
        errors.append("Cet identifiant est déjà utilisé.")
    return errors, username, password


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    if db.session.query(User.id).first() is not None:
        return redirect(url_for("home.index"))
    ws = Workspace.get()
    if request.method == "POST":
        errors, username, password = _validate_user_fields(request.form)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("auth/setup.html", form=request.form)
        ws.name = (request.form.get("workspace") or "Mon espace").strip()[:120]
        ws.emoji = (request.form.get("emoji") or "✈️").strip()[:16]
        user = User(email=User.internal_email(username), username=username, role="admin",
                    first_name=request.form.get("first_name", "").strip(),
                    last_name=request.form.get("last_name", "").strip(),
                    avatar_color=random.choice(PALETTE))
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        current_app.config["_HAS_USERS"] = True
        login_user(user, remember=True)
        flash("Bienvenue dans Hangar ! Commence par créer ton premier projet.", "success")
        return redirect(url_for("projects.new"))
    return render_template("auth/setup.html", form={})


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("home.index"))
    ws = Workspace.get()
    if request.method == "POST":
        ident = (request.form.get("login") or "").strip().lower()
        password = request.form.get("password") or ""
        user = User.query.filter(User.username == ident).first()
        if user and user.check_password(password):
            if not user.active:
                flash("Ce compte est désactivé. Contacte un administrateur.", "error")
            else:
                login_user(user, remember=bool(request.form.get("remember")))
                user.last_login_at = now()
                db.session.commit()
                nxt = request.args.get("next")
                return redirect(nxt if is_safe_next(nxt) else url_for("home.index"))
        else:
            flash("Identifiant ou mot de passe incorrect.", "error")
    accounts = User.query.filter_by(active=True).order_by(User.role, User.first_name, User.last_name).all()
    return render_template("auth/login.html", ws=ws, accounts=accounts)


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    ws = Workspace.get()
    if not ws.allow_signup:
        flash("Les inscriptions sont fermées : demande un compte à un administrateur.", "info")
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        errors, username, password = _validate_user_fields(request.form)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("auth/signup.html", ws=ws, form=request.form)
        user = User(email=User.internal_email(username), username=username, role="member",
                    first_name=request.form.get("first_name", "").strip(),
                    last_name=request.form.get("last_name", "").strip(),
                    avatar_color=random.choice(PALETTE))
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        flash("Compte créé. Bienvenue !", "success")
        return redirect(url_for("home.index"))
    return render_template("auth/signup.html", ws=ws, form={})


@bp.route("/logout", methods=["POST", "GET"])
@login_required
def logout():
    logout_user()
    flash("Tu es déconnecté.", "info")
    return redirect(url_for("auth.login"))
