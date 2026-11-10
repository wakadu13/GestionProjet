"""Assistant IA : routes du panneau (conversation en flux, contexte, « @ », actions), page plein écran,
réglages des fournisseurs et usage, points d'entrée des fonctions d'IA de Revision."""
from __future__ import annotations

import os
from urllib.parse import parse_qsl

from flask import (
    Blueprint, Response, abort, flash, jsonify, redirect, render_template, request, stream_with_context, url_for,
)
from flask_login import current_user, login_required

from ..extensions import db
from ..icons import ICONS
from ..models import User, now
from . import context as C
from . import core, jobs
from . import providers as P
from .chat import Turn, conversation_dict, resolve_mentions
from .models import (
    ROLES, AIAction, AIConversation, AIErrorLog, AIJob, AIMessage, AIProvider, AISettings, AIUsage,
)
from .providers import AIError
from .tools import ActionError, action_dict, execute_action

bp = Blueprint("ai", __name__)

# Icônes propres au module (ajoutées sans toucher icons.py)
ICONS.setdefault("thumb-up", '<path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 '
                             '17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/>')
ICONS.setdefault("thumb-down", '<path d="M17 14V2"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 '
                               '6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/>')
ICONS.setdefault("at", '<circle cx="12" cy="12" r="4"/><path d="M16 8v5a3 3 0 0 0 6 0v-1a10 10 0 1 0-4 8"/>')
ICONS.setdefault("slash", '<path d="M15 3 9 21"/>')
ICONS.setdefault("arrow-up", '<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>')
ICONS.setdefault("square", '<rect width="14" height="14" x="5" y="5" rx="2"/>')
ICONS.setdefault("maximize", '<path d="M15 3h6v6"/><path d="M9 21H3v-6"/><path d="m21 3-7 7"/><path d="m3 21 7-7"/>')
ICONS.setdefault("message-plus", '<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/><path d="M8 12h8"/><path d="M12 8v8"/>')


def _fetch() -> bool:
    return request.headers.get("X-Requested-With") == "fetch" or request.is_json


def _admin():
    if not current_user.is_admin:
        abort(403)


def _own_conversation(cid: int) -> AIConversation:
    c = db.session.get(AIConversation, cid)
    if c is None or c.user_id != current_user.id:
        abort(404)
    return c


@bp.app_template_global("ai_lots")
def ai_lots() -> bool:
    """Case « Sans urgence (lot à moitié prix) » : seulement quand les travaux partent chez Claude."""
    from . import lots_possibles
    return lots_possibles()


@bp.app_template_global("ai_sidebar")
def ai_sidebar() -> dict:
    """Données du menu de la section IA (conversations récentes, usage du mois)."""
    if not current_user.is_authenticated:
        return {}
    convs = (AIConversation.query.filter_by(user_id=current_user.id)
             .order_by(AIConversation.updated_at.desc()).limit(10).all())
    s = AISettings.get()
    total = core.month_cost()
    from . import disponible
    from .tools import wiki_available
    return {"conversations": convs, "mine": core.month_cost(user_id=current_user.id), "total": total,
            "cap": s.monthly_cap or 0, "pct": min(100, round(100 * total / s.monthly_cap)) if s.monthly_cap else 0,
            "dispo": disponible(), "wiki": wiki_available(), "editeur": not current_user.is_guest,
            "travaux": AIJob.query.filter(AIJob.status.in_(("pending", "running", "batch"))).count(),
            "brouillons": AIJob.query.filter_by(status="draft").count()}


# ---------------------------------------------------------------------------
# Page plein écran et conversations
# ---------------------------------------------------------------------------

@bp.route("/ia/")
@login_required
def index():
    cid = request.args.get("c", type=int)
    conv = _own_conversation(cid) if cid else None
    return render_template("ai/index.html", conv=conv, conv_data=conversation_dict(conv, True) if conv else None)


@bp.route("/ia/c/<int:cid>")
@login_required
def conversation(cid):
    conv = _own_conversation(cid)
    return render_template("ai/index.html", conv=conv, conv_data=conversation_dict(conv, True))


@bp.route("/ia/conversations.json")
@login_required
def conversations():
    rows = (AIConversation.query.filter_by(user_id=current_user.id)
            .order_by(AIConversation.updated_at.desc()).limit(60).all())
    return jsonify(ok=True, conversations=[conversation_dict(c) for c in rows])


@bp.route("/ia/c/<int:cid>.json")
@login_required
def conversation_json(cid):
    return jsonify(ok=True, conversation=conversation_dict(_own_conversation(cid), True))


@bp.route("/ia/c/<int:cid>/supprimer", methods=["POST"])
@login_required
def conversation_delete(cid):
    conv = _own_conversation(cid)
    db.session.delete(conv)
    db.session.commit()
    if _fetch():
        return jsonify(ok=True)
    flash("Conversation supprimée.", "success")
    return redirect(url_for("ai.index"))


@bp.route("/ia/c/<int:cid>/renommer", methods=["POST"])
@login_required
def conversation_rename(cid):
    conv = _own_conversation(cid)
    data = request.get_json(silent=True) or request.form
    title = " ".join(str(data.get("title") or "").split())[:200]
    if title:
        conv.title = title
        db.session.commit()
    if _fetch():
        return jsonify(ok=True, title=conv.title)
    return redirect(url_for("ai.conversation", cid=conv.id))


@bp.route("/ia/messages/<int:mid>/avis", methods=["POST"])
@login_required
def feedback(mid):
    m = db.session.get(AIMessage, mid)
    if m is None or m.role != "assistant" or m.conversation.user_id != current_user.id:
        abort(404)
    data = request.get_json(silent=True) or request.form
    try:
        value = int(data.get("value") or 0)
    except (TypeError, ValueError):
        value = 0
    m.feedback = value if value in (-1, 0, 1) else 0
    if "note" in data:
        m.feedback_note = str(data.get("note") or "")[:500]
    db.session.commit()
    return jsonify(ok=True, feedback=m.feedback)


# ---------------------------------------------------------------------------
# Conversation en flux (SSE), contexte et « @ »
# ---------------------------------------------------------------------------

def _json_error(e: AIError):
    settings = e.code in ("indisponible", "cle", "modele", "reseau", "credits", "plafond", "acces")
    return jsonify(ok=False, error=e.message, code=e.code, settings=url_for("ai.reglages") if settings else None), e.status


@bp.route("/ia/chat", methods=["POST"])
@login_required
def chat():
    from .chat import MAX_INPUT, MAX_SELECTION

    data = request.get_json(silent=True) or {}
    text = str(data.get("message") or "").strip()[:MAX_INPUT]
    if not text:
        return jsonify(ok=False, error="Écris d’abord ta question."), 400
    try:
        core.pick("chat")
        if core.cap_reached():
            raise AIError("Plafond mensuel de l’IA atteint : réessaie le mois prochain ou relève le plafond dans les "
                          "réglages.", "plafond", 429)
    except AIError as e:
        return _json_error(e)
    cid = data.get("conversation_id")
    try:
        conv = _own_conversation(int(cid)) if cid else None
    except (TypeError, ValueError):
        abort(400)
    if conv is None:
        conv = AIConversation(user_id=current_user.id, created_at=now(), updated_at=now())
        db.session.add(conv)
        db.session.flush()
    page = None
    path = str(data.get("path") or "")
    if data.get("include_page", True) and path.startswith("/"):
        page = C.resolve(path, current_user, dict(parse_qsl(str(data.get("query") or "").lstrip("?"))))
        if page is None and data.get("title"):
            page = {"type": "page", "title": str(data["title"])[:200], "text": "", "url": path, "icon": "page", "key": ""}
    selection = str(data.get("selection") or "").strip()[:MAX_SELECTION]
    mentions = resolve_mentions(data.get("mentions"), current_user)
    try:
        turn = Turn(current_user._get_current_object(), conv, text, page=page, selection=selection, mentions=mentions,
                    command=str(data.get("command") or ""))
    except AIError as e:
        db.session.rollback()
        return _json_error(e)
    resp = Response(stream_with_context(turn.stream()), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@bp.route("/ia/contexte")
@login_required
def contexte():
    path = request.args.get("path") or "/"
    query = dict(parse_qsl((request.args.get("query") or "").lstrip("?")))
    r = C.resolve(path, current_user, query)
    if r is None:
        title = (request.args.get("title") or "").split(" · ")[0].strip() or "Page"
        return jsonify(ok=True, known=False, context={"type": "page", "title": title[:120], "icon": "page", "url": path})
    return jsonify(ok=True, known=True, context={k: r[k] for k in ("type", "title", "icon", "url")},
                   size=len(r.get("text") or ""))


@bp.route("/ia/mentions")
@login_required
def mentions():
    return jsonify(ok=True, items=C.search_mentions(request.args.get("q", "")[:100], current_user))


# ---------------------------------------------------------------------------
# Actions proposées
# ---------------------------------------------------------------------------

def _own_action(aid: int) -> AIAction:
    a = db.session.get(AIAction, aid)
    if a is None or a.user_id != current_user.id:
        abort(404)
    return a


@bp.route("/ia/actions/<int:aid>/executer", methods=["POST"])
@login_required
def action_execute(aid):
    a = _own_action(aid)
    try:
        result = execute_action(a, current_user)
    except ActionError as e:
        if e.status not in (404, 409):
            a.status = "failed"
            a.result = {"error": e.message}
            db.session.commit()
        return jsonify(ok=False, error=e.message, action=action_dict(a)), e.status
    return jsonify(ok=True, message=result.get("message") or "C’est fait.", url=result.get("url"), action=action_dict(a))


@bp.route("/ia/actions/<int:aid>/ignorer", methods=["POST"])
@login_required
def action_dismiss(aid):
    a = _own_action(aid)
    if a.status in ("proposed", "failed"):
        a.status = "dismissed"
        db.session.commit()
    return jsonify(ok=True, action=action_dict(a))


# ---------------------------------------------------------------------------
# Réglages : fournisseurs, plafond, usage
# ---------------------------------------------------------------------------

@bp.route("/ia/reglages/", methods=["GET", "POST"])
@login_required
def reglages():
    s = AISettings.get()
    if request.method == "POST":
        _admin()
        raw = (request.form.get("monthly_cap") or "").strip().replace(",", ".").replace("$", "")
        try:
            s.monthly_cap = max(0.0, min(10000.0, float(raw or 0)))
        except ValueError:
            flash("Plafond invalide : un montant en dollars (0 pour aucun plafond).", "error")
            return redirect(url_for("ai.reglages"))
        s.instructions = (request.form.get("instructions") or "").strip()[:4000]
        s.updated_at = now()
        db.session.commit()
        flash("Réglages de l’IA enregistrés.", "success")
        return redirect(url_for("ai.reglages"))
    try:
        jobs.resume()
    except Exception:  # noqa: BLE001
        pass
    start = core.month_start()
    admin = current_user.is_admin
    me = current_user.id
    usage = {
        "mine": core.month_cost(user_id=me), "total": core.month_cost(),
        "mine_calls": AIUsage.query.filter(AIUsage.user_id == me, AIUsage.created_at >= start).count(),
        "by_feature": core.usage_rows(start, user_id=None if admin else me, group="feature"),
        "by_model": core.usage_rows(start, user_id=None if admin else me, group="model"),
        "by_month": core.usage_by_month(6, user_id=None if admin else me),
    }
    if admin:
        rows = core.usage_rows(start, group="user")
        users = {u.id: u for u in User.query.filter(User.id.in_([r[0] for r in rows if r[0]]))} if rows else {}
        usage["by_user"] = [(users.get(r[0]).display_name if users.get(r[0]) else "—", *r[1:]) for r in rows]
    providers = AIProvider.query.order_by(AIProvider.priority, AIProvider.id).all()
    return render_template(
        "ai/reglages.html", s=s, providers=providers, presets=P.PRESETS, roles=ROLES, usage=usage,
        claude_models=P.CLAUDE_MODELS, model_label=core.model_label,
        errors=AIErrorLog.query.order_by(AIErrorLog.id.desc()).limit(30).all() if admin else [],
        jobs=AIJob.query.order_by(AIJob.id.desc()).limit(15).all() if admin else [],
        env_keys={p.id: bool(p.env_name() and os.environ.get(p.env_name())) for p in providers},
    )


def _float(v, default=0.0) -> float:
    try:
        return max(0.0, float(str(v or "").replace(",", ".").replace("$", "").strip() or default))
    except ValueError:
        return default


def _apply_provider_form(p: AIProvider, f, is_new: bool) -> str | None:
    preset = f.get("preset") or p.preset or "custom"
    info = P.PRESET_BY_KEY.get(preset, P.PRESET_BY_KEY["custom"])
    p.preset = preset
    p.kind = info["kind"]
    p.name = (f.get("name") or "").strip()[:120] or info["label"]
    base = (f.get("base_url") or "").strip().rstrip("/")
    if p.kind == "openai_compatible":
        if not base.startswith(("http://", "https://")):
            return "Adresse du serveur invalide : elle commence par http:// ou https:// (ex. http://localhost:11434/v1)."
    elif base and not base.startswith(("http://", "https://")):
        return "Adresse invalide."
    p.base_url = base[:500]
    key = (f.get("api_key") or "").strip()
    if key:
        p.api_key = key
    elif f.get("clear_key"):
        p.api_key = ""
    p.api_key_env = (f.get("api_key_env") or "").strip()[:120]
    p.model = (f.get("model") or "").strip()[:200]
    p.model_fast = (f.get("model_fast") or "").strip()[:200]
    p.model_vision = (f.get("model_vision") or "").strip()[:200]
    if f.get("roles_present") or any(f"role_{r}" in f for r, _ in ROLES):
        roles = [r for r, _ in ROLES if f.get(f"role_{r}")]
    else:
        roles = p.role_list if p.roles else [r for r, _ in ROLES]
    p.roles = ",".join(roles or ["chat"])
    try:
        p.priority = int(f.get("priority") or p.priority or 10)
    except ValueError:
        p.priority = 10
    p.monthly_cap = _float(f.get("monthly_cap"))
    p.tools_mode = f.get("tools_mode") if f.get("tools_mode") in ("auto", "on", "off") else "auto"
    p.effort = f.get("effort") if f.get("effort") in core.EFFORTS else ""
    p.price_input, p.price_output = _float(f.get("price_input")), _float(f.get("price_output"))
    vals = f.getlist("enabled") if hasattr(f, "getlist") else ([f["enabled"]] if "enabled" in f else [])
    if vals:
        p.enabled = vals[-1] in ("1", "on", "true")
    elif is_new:
        p.enabled = True
    if not p.model:
        return "Choisis le modèle à utiliser."
    p.updated_at = now()
    return None


@bp.route("/ia/fournisseurs/nouveau", methods=["POST"])
@login_required
def provider_create():
    _admin()
    p = AIProvider(created_at=now(), priority=(db.session.query(db.func.max(AIProvider.priority)).scalar() or 0) + 10)
    err = _apply_provider_form(p, request.form, True)
    if err:
        flash(err, "error")
        return redirect(url_for("ai.reglages"))
    db.session.add(p)
    db.session.commit()
    flash(f"Fournisseur « {p.name} » ajouté. Teste la connexion pour vérifier qu’il répond.", "success")
    return redirect(url_for("ai.reglages") + f"#fournisseur-{p.id}")


@bp.route("/ia/fournisseurs/<int:pid>", methods=["POST"])
@login_required
def provider_update(pid):
    _admin()
    p = db.session.get(AIProvider, pid) or abort(404)
    err = _apply_provider_form(p, request.form, False)
    if err:
        db.session.rollback()
        flash(err, "error")
    else:
        db.session.commit()
        flash(f"Fournisseur « {p.name} » enregistré.", "success")
    return redirect(url_for("ai.reglages") + f"#fournisseur-{pid}")


@bp.route("/ia/fournisseurs/<int:pid>/activer", methods=["POST"])
@login_required
def provider_toggle(pid):
    _admin()
    p = db.session.get(AIProvider, pid) or abort(404)
    p.enabled = not p.enabled
    db.session.commit()
    if _fetch():
        return jsonify(ok=True, enabled=p.enabled)
    flash(f"« {p.name} » {'activé' if p.enabled else 'désactivé'}.", "success")
    return redirect(url_for("ai.reglages"))


@bp.route("/ia/fournisseurs/<int:pid>/supprimer", methods=["POST"])
@login_required
def provider_delete(pid):
    _admin()
    p = db.session.get(AIProvider, pid) or abort(404)
    db.session.delete(p)
    db.session.commit()
    flash(f"Fournisseur « {p.name} » supprimé (son usage reste dans l’historique).", "success")
    return redirect(url_for("ai.reglages"))


def _transient_provider() -> AIProvider:
    """Fournisseur décrit par le formulaire (non enregistré), pour tester avant d'enregistrer."""
    f = request.form
    existing = db.session.get(AIProvider, f.get("id", type=int) or 0)
    p = AIProvider(id=existing.id if existing else None, enabled=True, roles="chat")
    with db.session.no_autoflush:
        _apply_provider_form(p, f, True)
        if not (f.get("api_key") or "").strip() and existing is not None and not f.get("clear_key"):
            p.api_key = existing.api_key
    if p in db.session:
        db.session.expunge(p)
    return p


@bp.route("/ia/fournisseurs/essai", methods=["POST"])
@login_required
def provider_try():
    """Tester la connexion et lister les modèles d'après le formulaire, avant d'enregistrer."""
    _admin()
    p = _transient_provider()
    what = request.form.get("what") or "test"
    try:
        if what == "models":
            return jsonify(ok=True, models=P.get(p).list_models())
        ok, msg = P.get(p).test()
    except AIError as e:
        return jsonify(ok=False, error=e.message)
    return jsonify(ok=bool(ok), unknown=ok is None, message=msg)


@bp.route("/ia/fournisseurs/<int:pid>/tester", methods=["POST"])
@login_required
def provider_test(pid):
    _admin()
    p = db.session.get(AIProvider, pid) or abort(404)
    ok, msg = core.test_provider(p)
    if _fetch():
        return jsonify(ok=bool(ok), unknown=ok is None, message=msg)
    flash(msg, "success" if ok else "error")
    return redirect(url_for("ai.reglages") + f"#fournisseur-{pid}")


@bp.route("/ia/fournisseurs/<int:pid>/modeles")
@login_required
def provider_models(pid):
    _admin()
    p = db.session.get(AIProvider, pid) or abort(404)
    try:
        return jsonify(ok=True, models=core.list_models(p))
    except AIError as e:
        return jsonify(ok=False, error=e.message), 200


@bp.route("/ia/erreurs/effacer", methods=["POST"])
@login_required
def errors_clear():
    _admin()
    AIErrorLog.query.delete()
    db.session.commit()
    flash("Journal des erreurs de l’IA vidé.", "success")
    return redirect(url_for("ai.reglages") + "#erreurs")


# ---------------------------------------------------------------------------
# Fonctions d'IA de Revision (explications, QCM générés, tuteur, « Demander au wiki », copies, travaux)
# ---------------------------------------------------------------------------

from . import revision_views  # noqa: E402,F401 - ajoute ses routes au blueprint
