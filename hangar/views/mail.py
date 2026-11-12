"""E-mails : configuration (administrateurs), historique / boîte d'envoi, rédaction manuelle."""
import json
import os
from datetime import date

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from markupsafe import escape

from .. import mailer
from ..extensions import db
from ..models import EmailMessage, Issue, MailConfig, User
from ..services import to_int
from ..utils import can, render_markdown, require_admin, tri_fr

bp = Blueprint("mail", __name__)

PRESETS = {
    "gmail": {"label": "Gmail", "host": "smtp.gmail.com", "port": 587, "security": "starttls",
              "note": "Utilise un « mot de passe d’application » Google (compte avec validation en deux étapes)."},
    "outlook": {"label": "Outlook / Office 365", "host": "smtp.office365.com", "port": 587, "security": "starttls",
                "note": "Identifiant = adresse complète. L’envoi SMTP doit être autorisé sur le compte."},
    "orange": {"label": "Orange", "host": "smtp.orange.fr", "port": 465, "security": "ssl",
               "note": "Identifiant = adresse Orange complète."},
    "free": {"label": "Free", "host": "smtp.free.fr", "port": 587, "security": "starttls",
             "note": "Identifiant = adresse Free complète."},
}

TEMPLATES = {
    "relance": ("Relance", "Relance : ",
                "Bonjour,\n\nJe me permets de revenir vers vous au sujet de …\n\n"
                "Pourriez-vous nous faire un retour d’ici le … ?\n\nMerci d’avance,\n\n{signature}"),
    "reunion": ("Convocation à une réunion", "Réunion projet – ",
                "Bonjour à tous,\n\nJe vous propose un point d’avancement le **…** à **…** (lieu : …).\n\n"
                "**Ordre du jour**\n\n1. Avancement des tickets en cours\n2. Points bloquants\n3. Prochaines étapes\n\n"
                "Merci de confirmer votre présence.\n\n{signature}"),
    "cr": ("Compte rendu", "Compte rendu – réunion du ",
           "Bonjour,\n\nVoici le compte rendu de notre réunion du ….\n\n**Points abordés**\n\n- …\n\n"
           "**Décisions**\n\n- …\n\n**Actions**\n\n- [ ] … — responsable — échéance\n\nBonne journée,\n\n{signature}"),
    "documents": ("Demande de documents", "Demande de documents – ",
                  "Bonjour,\n\nDans le cadre de notre projet, nous aurions besoin des documents suivants :\n\n- …\n- …\n\n"
                  "Pourriez-vous nous les transmettre avant le … ?\n\nMerci beaucoup,\n\n{signature}"),
}


def _counts():
    rows = db.session.query(EmailMessage.status, db.func.count(EmailMessage.id)).group_by(EmailMessage.status).all()
    return {k: v for k, v in rows}


@bp.route("/settings/mail", methods=["GET", "POST"])
@login_required
def settings():
    require_admin()
    cfg = MailConfig.get()
    if request.method == "POST":
        action = request.form.get("action")
        f = request.form
        if action == "config":
            cfg.enabled = bool(f.get("enabled"))
            cfg.mode = "smtp" if f.get("mode") == "smtp" else "outbox"
            cfg.smtp_host = (f.get("smtp_host") or "").strip()
            cfg.smtp_port = to_int(f.get("smtp_port")) or 587
            cfg.smtp_security = f.get("smtp_security") if f.get("smtp_security") in ("starttls", "ssl", "none") else "starttls"
            cfg.smtp_user = (f.get("smtp_user") or "").strip()
            if f.get("smtp_password"):
                cfg.smtp_password = f.get("smtp_password")
            if f.get("clear_password"):
                cfg.smtp_password = ""
            cfg.from_name = (f.get("from_name") or "Hangar").strip()[:120]
            from_address = (f.get("from_address") or "").strip()
            if from_address and not mailer.valid_email(from_address):
                flash("Adresse d’expédition invalide.", "error")
            else:
                cfg.from_address = from_address
            cfg.base_url = (f.get("base_url") or "http://127.0.0.1:5000").strip().rstrip("/")
            cfg.reminder_hour = min(max(to_int(f.get("reminder_hour")) or 8, 0), 23)
            cfg.digest_weekday = min(max(to_int(f.get("digest_weekday")) or 0, 0), 6)
            cfg.bilan_weekday = min(max(to_int(f.get("bilan_weekday")) or 0, 0), 6)
            cfg.bilan_hour = min(max(to_int(f.get("bilan_hour")) or 17, 0), 23)
            for k in ("send_notifications", "send_reminders", "send_digest", "send_bilan"):
                setattr(cfg, k, bool(f.get(k)))
            if cfg.mode == "smtp" and not cfg.smtp_host:
                flash("Mode SMTP choisi sans serveur : renseigne l’adresse du serveur.", "error")
            db.session.commit()
            flash("Configuration des e-mails enregistrée.", "success")
        elif action == "members":
            errors = 0
            for u in User.query.filter_by(active=True):
                key = f"email_{u.id}"
                if key not in f:
                    continue
                addr = (f.get(key) or "").strip()
                if addr and not mailer.valid_email(addr):
                    errors += 1
                    continue
                u.contact_email = addr or None
                prefs = f.get(f"prefs_{u.id}")
                if prefs in ("instant", "digest", "off"):
                    u.email_prefs = prefs
            db.session.commit()
            flash("Adresses des membres enregistrées." + (f" {errors} adresse(s) invalide(s) ignorée(s)." if errors else ""),
                  "success" if not errors else "info")
        elif action == "test":
            addr = (f.get("address") or "").strip()
            if not mailer.valid_email(addr):
                flash("Adresse de test invalide.", "error")
            else:
                m = mailer.send_test(addr, current_user)
                _flash_result([m])
        elif action == "run":
            job = f.get("job")
            if job in ("reminders", "digest", "bilan"):
                res = mailer.run_scheduled(force={job})
                stats = mailer.send_pending()
                label = {"reminders": "rappel(s)", "digest": "récapitulatif(s)", "bilan": "bilan(s)"}[job]
                flash(f"{res.get(job, 0)} {label} préparé(s) — {_stats_text(stats)}.", "success")
        elif action == "flush":
            stats = mailer.send_pending()
            flash(f"File traitée — {_stats_text(stats)}.", "success")
        elif action == "clear":
            folder = mailer.outbox_dir()
            n = 0
            for m in EmailMessage.query.filter(EmailMessage.status.in_(("outbox", "sent", "failed"))):
                db.session.delete(m)
                n += 1
            for name in os.listdir(folder):
                if name.endswith(".eml"):
                    os.remove(os.path.join(folder, name))
            db.session.commit()
            flash(f"Historique vidé ({n} message(s)) et fichiers .eml supprimés.", "info")
        return redirect(url_for("mail.settings") + (f"#{action}" if action else ""))
    users = User.query.filter_by(active=True).order_by(tri_fr(User.first_name), tri_fr(User.last_name)).all()
    history = EmailMessage.query.order_by(EmailMessage.created_at.desc(), EmailMessage.id.desc()).limit(100).all()
    return render_template("mail/settings.html", cfg=cfg, users=users, history=history, counts=_counts(),
                           presets=PRESETS, KIND_LABELS=mailer.KIND_LABELS, STATUS_LABELS=mailer.STATUS_LABELS,
                           WEEKDAYS=mailer.WEEKDAYS, outbox_path=mailer.outbox_dir())


def _stats_text(stats):
    parts = []
    if stats.get("sent"):
        parts.append(f"{stats['sent']} envoyé(s)")
    if stats.get("outbox"):
        parts.append(f"{stats['outbox']} enregistré(s) dans la boîte d’envoi locale")
    if stats.get("failed"):
        parts.append(f"{stats['failed']} en échec")
    return ", ".join(parts) or "aucun message en attente"


def _flash_result(messages):
    for m in messages:
        db.session.refresh(m)
    sent = [m for m in messages if m.status == "sent"]
    outbox = [m for m in messages if m.status == "outbox"]
    failed = [m for m in messages if m.status == "failed"]
    queued = [m for m in messages if m.status == "queued"]
    if sent:
        flash(f"E-mail envoyé à {len(sent)} destinataire(s).", "success")
    if outbox:
        flash(f"{len(outbox)} e-mail(s) enregistré(s) dans la boîte d’envoi locale (data/outbox).", "success")
    if queued:
        flash(f"{len(queued)} e-mail(s) en file d’attente.", "info")
    if failed:
        flash(f"Échec d’envoi : {failed[0].error}", "error")


def _message(mid) -> EmailMessage:
    require_admin()
    m = db.session.get(EmailMessage, mid)
    if m is None:
        abort(404)
    return m


@bp.route("/settings/mail/<int:mid>")
@login_required
def message(mid):
    m = _message(mid)
    return render_template("mail/message.html", m=m, KIND_LABELS=mailer.KIND_LABELS,
                           STATUS_LABELS=mailer.STATUS_LABELS)


@bp.route("/settings/mail/<int:mid>.eml")
@login_required
def message_eml(mid):
    m = _message(mid)
    data = bytes(mailer.build_mime(m, MailConfig.get()))
    return Response(data, mimetype="message/rfc822",
                    headers={"Content-Disposition": f"attachment; filename=hangar_{m.id}.eml"})


@bp.route("/settings/mail/<int:mid>/<action>", methods=["POST"])
@login_required
def message_action(mid, action):
    m = _message(mid)
    if action == "resend":
        m.status, m.error = "queued", None
        db.session.commit()
        mailer.send_pending(ids=[m.id])
        _flash_result([m])
    elif action == "delete":
        db.session.delete(m)
        db.session.commit()
        flash("Message supprimé de l’historique.", "info")
    else:
        abort(404)
    return redirect(url_for("mail.settings") + "#history")


@bp.route("/mail/compose", methods=["GET", "POST"])
@login_required
def compose():
    if current_user.is_guest:
        abort(403)
    issue = None
    iid = to_int(request.values.get("issue"))
    if iid:
        issue = db.session.get(Issue, iid)
        if issue is not None and not can(current_user, "view", issue.project):
            issue = None
    members = User.query.filter(User.active.is_(True)).order_by(tri_fr(User.first_name), tri_fr(User.last_name)).all()
    with_email = [u for u in members if mailer.valid_email(u.contact_email)]
    if request.method == "POST":
        f = request.form
        subject = (f.get("subject") or "").strip()
        body = (f.get("body") or "").strip()
        recipients: dict[str, User | None] = {}
        for uid in f.getlist("user_ids"):
            u = db.session.get(User, to_int(uid) or 0)
            if u and mailer.valid_email(u.contact_email):
                recipients[u.contact_email.lower()] = u
        invalid = []
        for addr in mailer.split_addresses(f.get("others")):
            if mailer.valid_email(addr):
                recipients.setdefault(addr.lower(), None)
            else:
                invalid.append(addr)
        ref = (f.get("issue_ref") or "").strip().upper()
        linked = issue
        if ref:
            from .issues import _find_issue_ref
            linked = _find_issue_ref(ref, issue.project if issue else None) or linked
            if linked is None:
                invalid.append(ref)
        if invalid:
            flash("Ignoré(s) car invalide(s) : " + ", ".join(invalid), "error")
        if not subject or not body:
            flash("L’objet et le message sont obligatoires.", "error")
        elif not recipients:
            flash("Choisis au moins un destinataire avec une adresse e-mail.", "error")
        else:
            cfg = MailConfig.get()
            content = mailer.absolutize_links(str(render_markdown(body)), cfg)
            button = None
            if linked is not None:
                content += mailer.issue_card(linked, cfg)
                button = ("Voir le ticket", mailer.absolute(mailer.issue_path(linked), cfg))
            footer = (f"Message envoyé par {escape(current_user.display_name)} depuis Hangar"
                      f"{' — tu peux répondre directement à cet e-mail' if current_user.contact_email else ''}.")
            html = mailer.layout(subject, f'<div style="font-size:14px">{content}</div>', button=button,
                                 footer=footer, cfg=cfg)
            msgs = [mailer.queue_email(addr, subject, html, kind="manual", to_user=u, created_by=current_user,
                                       reply_to=current_user.contact_email if mailer.valid_email(current_user.contact_email) else None)
                    for addr, u in recipients.items()]
            db.session.commit()
            mailer.send_pending(ids=[m.id for m in msgs])
            _flash_result(msgs)
            if linked is not None:
                from ..services import log_activity
                log_activity(linked, current_user, "emailed",
                             new=f"« {subject} » à {len(msgs)} destinataire(s)")
                db.session.commit()
                return redirect(url_for("issues.detail", pid=linked.project_id, seq=linked.sequence_id))
            return redirect(url_for("mail.compose"))
    prefill_subject, prefill_body = "", ""
    if issue is not None:
        prefill_subject = f"[{issue.key}] {issue.title}"
        prefill_body = (f"Bonjour,\n\nJe vous écris au sujet du ticket **{issue.key} — {issue.title}**.\n\n…\n\n"
                        f"{current_user.display_name}")
    signature = current_user.display_name + (f"\n{current_user.job_title}" if current_user.job_title else "")
    templates = {k: {"label": lab, "subject": subj, "body": body.replace("{signature}", signature)}
                 for k, (lab, subj, body) in TEMPLATES.items()}
    return render_template("mail/compose.html", issue=issue, members=members, with_email=with_email,
                           templates=templates, templates_json=json.dumps(templates, ensure_ascii=False),
                           prefill_subject=prefill_subject, prefill_body=prefill_body,
                           cfg=MailConfig.get(), today=date.today())
