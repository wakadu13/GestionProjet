"""Assistant IA : fournisseurs (Claude, modèles open source compatibles OpenAI / Ollama), panneau latéral,
fonctions d'IA du wiki et des QCM.

Interface publique utilisée par les autres modules :
- `disponible()` : un fournisseur d'IA est configuré et activé ;
- `register_context(fn)` : ajoute un résolveur de contexte (page courante -> texte transmis à l'IA) ;
- `client_config()` : réglages exposés au JavaScript (window.HANGAR.ai).
"""
from __future__ import annotations

_CONTEXT_RESOLVERS: list = []


def disponible() -> bool:
    return False


def register_context(fn):
    """fn(path: str, args: dict, user) -> dict | None : {"type", "title", "text", "url", ...} pour la page courante."""
    if fn not in _CONTEXT_RESOLVERS:
        _CONTEXT_RESOLVERS.append(fn)
    return fn


def client_config() -> dict:
    return {"enabled": False}
