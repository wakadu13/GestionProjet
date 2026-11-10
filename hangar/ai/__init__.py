"""Assistant IA : fournisseurs (Claude par l'API Anthropic, modèles open source par Ollama, LM Studio, vLLM,
llama.cpp ou toute API compatible OpenAI), panneau latéral façon Plane AI, fonctions d'IA du wiki et des QCM.

Interface publique utilisée par les autres modules :
- `disponible()` : un fournisseur d'IA est configuré et activé (les boutons d'IA ne s'affichent que dans ce cas) ;
- `register_context(fn)` : ajoute un résolveur de contexte `fn(path, args, user) -> dict | None`
  ({"type", "title", "text", "url", "icon"}) qui décrit la page courante au panneau IA (voir context.py) ;
- `client_config()` : réglages exposés au JavaScript (window.HANGAR.ai) ;
- `generate(...)`, `run_agent(...)`, `AIError`, `plafond_atteint()` : appels aux modèles (voir core.py) ;
- `register_mention_source(fn)` : éléments proposés après « @ » ; `register_action(kind, fn)` : exécuteur d'action ;
- `jobs` : travaux en arrière-plan (et lots à moitié prix) pour les longues générations.
"""
from __future__ import annotations

from .providers import AIError, Result, ToolCall  # noqa: F401 - réexportés

_CONTEXT_RESOLVERS: list = []


def register_context(fn):
    """fn(path: str, args: dict, user) -> dict | None : {"type", "title", "text", "url", ...} pour la page courante.

    `args` contient les paramètres de la route reconnue (ex. {"pid": 1, "seq": 12}), ceux de la requête (?date=…)
    et "_endpoint" (nom Flask de la route). Les résolveurs enregistrés passent avant ceux de l'assistant."""
    if fn not in _CONTEXT_RESOLVERS:
        _CONTEXT_RESOLVERS.append(fn)
    return fn


def disponible() -> bool:
    """Vrai si au moins un fournisseur d'IA activé est utilisable (modèle choisi, clé présente si nécessaire)."""
    try:
        from flask import has_app_context
        if not has_app_context():
            return False
        from .models import AIProvider
        return any(p.usable for p in AIProvider.query.filter_by(enabled=True))
    except Exception:  # noqa: BLE001 - l'assistant ne doit jamais empêcher une page de s'afficher
        return False


def _url_template(endpoint: str, **kw) -> str:
    from flask import url_for
    marker = 987654321
    return url_for(endpoint, **{k: marker for k in kw}).replace(str(marker), "{id}")


def client_config() -> dict:
    """Ce qu'il faut au JavaScript du panneau : activé ?, modèle, adresses, commandes « / »."""
    from flask import url_for
    from flask_login import current_user

    from . import providers as P
    from .models import AIProvider

    rows = [p for p in AIProvider.query.filter_by(enabled=True).order_by(AIProvider.priority, AIProvider.id) if p.usable]
    cfg = {
        "enabled": bool(rows),
        "admin": bool(current_user.is_authenticated and current_user.is_admin),
        "urls": {
            "chat": url_for("ai.chat"), "context": url_for("ai.contexte"), "mentions": url_for("ai.mentions"),
            "conversations": url_for("ai.conversations"), "page": url_for("ai.index"),
            "settings": url_for("ai.reglages"),
            "conversation": _url_template("ai.conversation_json", cid=1),
            "conversationPage": _url_template("ai.conversation", cid=1),
            "deleteConversation": _url_template("ai.conversation_delete", cid=1),
            "feedback": _url_template("ai.feedback", mid=1),
            "action": _url_template("ai.action_execute", aid=1),
            "dismiss": _url_template("ai.action_dismiss", aid=1),
            "katex": url_for("static", filename="vendor/katex/"),
            "highlight": url_for("static", filename="vendor/highlight/"),
        },
    }
    if not rows:
        return cfg
    from .chat import COMMANDS
    from .core import cap_reached, model_label

    row = next((p for p in rows if "chat" in p.role_list), rows[0])
    model = row.model_for("chat")
    cfg.update(model=model, modelLabel=model_label(model), provider=row.name, kind=row.kind, local=row.is_local,
               tools=P.get(row).tools_supported(model), capReached=cap_reached(),
               commands=[{k: c[k] for k in ("id", "label", "icon", "prompt")} for c in COMMANDS])
    return cfg


def generate(*args, **kwargs):
    """Voir core.generate : appel d'un modèle (texte, JSON validé, images, PDF, outils, flux)."""
    from .core import generate as _generate
    return _generate(*args, **kwargs)


def run_agent(*args, **kwargs):
    """Voir core.run_agent : conversation avec outils, en flux."""
    from .core import run_agent as _run_agent
    return _run_agent(*args, **kwargs)


def plafond_atteint() -> bool:
    """Vrai si la dépense du mois a atteint le plafond des réglages (0 = pas de plafond)."""
    from .core import cap_reached
    return cap_reached()


def register_mention_source(fn):
    from .context import register_mention_source as _register
    return _register(fn)


def register_action(kind: str, fn):
    from .tools import register_action as _register
    return _register(kind, fn)
