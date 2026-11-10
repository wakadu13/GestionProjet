"""Fournisseurs d'IA : une interface commune pour Claude (SDK officiel `anthropic`) et pour toute API compatible
OpenAI (Ollama, LM Studio, vLLM, llama.cpp server, OpenRouter, Mistral, Groq, Together, ou une autre adresse).

Chaque fournisseur reçoit une requête neutre (`Request`) et renvoie un flux d'événements :
    {"type": "text", "text": "…"}        morceau de réponse
    {"type": "tool_start", "name": "…"}   le modèle commence un appel d'outil
    {"type": "fallback", "to": "…"}       Claude a passé la main à son modèle de repli (refus d'un filtre)
    {"type": "reset"}                     la réponse en cours est abandonnée et recommence (rare)
    {"type": "done", "result": Result}    fin : texte complet, JSON, appels d'outils, usage…

Format neutre des messages (voir aussi core.generate) :
    {"role": "user", "content": "texte" | [parties]}   parties : {"type": "text", "text"},
        {"type": "image", "data": base64, "media_type"}, {"type": "document", "data": base64, "name"} (PDF)
        ou {"type": "document", "text": "…", "name"}
    {"role": "assistant", "content": "texte"}            tour précédent (rejoué en texte)
    {"role": "assistant", "result": Result}              tour produit par generate() (boucle d'outils : rejoué tel quel)
    {"role": "tool", "results": [{"id", "content", "is_error"}]}   résultats des outils
"""
from __future__ import annotations

import base64
import copy
import io
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# Préréglages (un clic dans les réglages)
# ---------------------------------------------------------------------------

PRESETS = [
    {"key": "anthropic", "label": "Claude (Anthropic)", "kind": "anthropic", "base_url": "",
     "model": "claude-opus-5-5", "model_fast": "claude-haiku-5-5", "key_env": "ANTHROPIC_API_KEY", "needs_key": True,
     "icon": "sparkle",
     "help": "Clé d’API à créer sur console.anthropic.com (Settings → API Keys) ; elle commence par « sk-ant- ». "
             "Les appels sont facturés par Anthropic à l’usage."},
    {"key": "ollama", "label": "Ollama (local)", "kind": "openai_compatible", "base_url": "http://localhost:11434/v1",
     "model": "llama3.2", "needs_key": False, "local": True, "icon": "database",
     "help": "Installe Ollama (ollama.com), puis dans un terminal : « ollama pull llama3.2 » (ou qwen2.5, "
             "qui gère bien les outils). Gratuit, tout reste sur ton ordinateur."},
    {"key": "lmstudio", "label": "LM Studio (local)", "kind": "openai_compatible", "base_url": "http://localhost:1234/v1",
     "model": "", "needs_key": False, "local": True, "icon": "database",
     "help": "Dans LM Studio : onglet « Developer », « Start Server », puis charge un modèle et choisis-le ici."},
    {"key": "vllm", "label": "vLLM", "kind": "openai_compatible", "base_url": "http://localhost:8000/v1",
     "model": "", "needs_key": False, "local": True, "icon": "database",
     "help": "« vllm serve <modèle> » (ajoute « --enable-auto-tool-choice --tool-call-parser … » pour les outils)."},
    {"key": "llamacpp", "label": "llama.cpp server", "kind": "openai_compatible", "base_url": "http://localhost:8080/v1",
     "model": "", "needs_key": False, "local": True, "icon": "database",
     "help": "« llama-server -m modele.gguf --jinja » (l’option --jinja active l’appel d’outils)."},
    {"key": "openrouter", "label": "OpenRouter", "kind": "openai_compatible", "base_url": "https://openrouter.ai/api/v1",
     "model": "meta-llama/llama-3.3-70b-instruct", "key_env": "OPENROUTER_API_KEY", "needs_key": True, "icon": "globe",
     "help": "Clé sur openrouter.ai/keys : donne accès à de nombreux modèles ouverts (Llama, Qwen, Mistral…)."},
    {"key": "mistral", "label": "Mistral AI", "kind": "openai_compatible", "base_url": "https://api.mistral.ai/v1",
     "model": "mistral-small-latest", "key_env": "MISTRAL_API_KEY", "needs_key": True, "icon": "globe",
     "help": "Clé sur console.mistral.ai (API Keys)."},
    {"key": "groq", "label": "Groq", "kind": "openai_compatible", "base_url": "https://api.groq.com/openai/v1",
     "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY", "needs_key": True, "icon": "zap",
     "help": "Clé sur console.groq.com/keys : modèles ouverts très rapides."},
    {"key": "together", "label": "Together AI", "kind": "openai_compatible", "base_url": "https://api.together.xyz/v1",
     "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo", "key_env": "TOGETHER_API_KEY", "needs_key": True,
     "icon": "globe", "help": "Clé sur api.together.ai (Settings → API Keys)."},
    {"key": "custom", "label": "Personnalisé (compatible OpenAI)", "kind": "openai_compatible", "base_url": "",
     "model": "", "needs_key": False, "icon": "link",
     "help": "N’importe quelle adresse qui comprend /v1/chat/completions (ex. http://192.168.1.20:11434/v1)."},
]
PRESET_BY_KEY = {p["key"]: p for p in PRESETS}

# ---------------------------------------------------------------------------
# Claude : modèles, tarifs, fonctions prises en charge
# ---------------------------------------------------------------------------

CLAUDE_DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Modèles qui acceptent le repli côté serveur (« fallbacks ») quand un filtre de sécurité refuse une demande légitime.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-2", "claude-fable-5-1", "claude-sonnet-5-5"}
PDF_INLINE_MAX = 20 * 1024 * 1024  # au-delà, le PDF passe par l'API Files (une requête est limitée à 32 Mo)
# Dollars par million de jetons : (entrée, sortie, lecture du cache). Écriture du cache : 1,25 × l'entrée.
CLAUDE_PRICES = {
    "claude-fable-5-2": (8.0, 40.0, 0.20), "claude-mythos-5-2": (8.0, 40.0, 0.20),
    "claude-fable-5-1": (10.0, 50.0, 0.25), "claude-mythos-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.0), "claude-mythos-5": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20), "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50), "claude-opus-4-7": (5.0, 25.0, 0.50), "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10), "claude-sonnet-5": (2.0, 10.0, 0.20), "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01), "claude-haiku-4-5": (1.0, 5.0, 0.10),
}
CACHE_WRITE_FACTOR = 1.25
BATCH_DISCOUNT = 0.5
CLAUDE_MODELS = [
    ("claude-opus-5-5", "Claude Opus 5.5", "recommandé : le plus fiable pour l’assistant et les cours (4 $ / 20 $ par million de jetons)"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5", "deux fois moins cher, très bon en lecture de copies (2 $ / 10 $)"),
    ("claude-haiku-5-5", "Claude Haiku 5.5", "le plus rapide et le moins cher, pour les explications courtes (0,10 $ / 0,50 $)"),
]

_EFFORT_RE = re.compile(r"claude-(opus-(4-[5-9]|5)|sonnet-(4-6|5)|haiku-5|fable|mythos)")
_FORMAT_RE = re.compile(r"claude-(opus-(4-[1-9]|5)|sonnet-(4-5|4-6|5)|haiku-(4-5|5)|fable|mythos)")


def claude_supports_effort(model: str) -> bool:
    return bool(_EFFORT_RE.search(model or ""))


def claude_supports_format(model: str) -> bool:
    return bool(_FORMAT_RE.search(model or ""))


def claude_price(model: str) -> tuple[float, float, float] | None:
    model = (model or "").split("/")[-1]
    for key in sorted(CLAUDE_PRICES, key=len, reverse=True):
        if model == key or model.startswith(key + "-"):
            return CLAUDE_PRICES[key]
    return None


# ---------------------------------------------------------------------------
# Requête, résultat, erreurs
# ---------------------------------------------------------------------------

class AIError(Exception):
    """Erreur lisible (le message est montré tel quel). `code` : indisponible, plafond, cle, acces, modele, limite,
    credits, requete, service, reseau, delai, refus, trop_long, json, reponse, lot, erreur."""

    def __init__(self, message: str, code: str = "erreur", status: int = 502, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.detail = detail


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    iterations: list = field(default_factory=list)  # Claude : une entrée par modèle qui a tourné (repli)


@dataclass
class Request:
    messages: list
    model: str
    system: str | None = None
    max_tokens: int | None = None
    effort: str | None = None
    json_schema: dict | None = None
    tools: list | None = None
    cache_system: bool = True
    cache_messages: bool = False      # Claude : met aussi en cache la conversation (boucles d'outils, multi-tours)
    timeout: float | None = None


@dataclass
class Result:
    text: str = ""
    data: Any = None                  # JSON validé (si un schéma a été demandé)
    tool_calls: list = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    provider: str = ""
    provider_id: int | None = None
    kind: str = ""
    stop_reason: str = "end_turn"     # end_turn | max_tokens | tool_use | refusal | stop_sequence | pause_turn
    cost: float = 0.0
    raw: Any = None                   # contenu natif (rejoué tel quel dans une boucle d'outils)
    fallback_to: str = ""             # Claude : modèle de repli qui a répondu
    tools_disabled: bool = False      # le modèle n'accepte pas les outils : réponse faite sans eux
    warnings: list = field(default_factory=list)
    usage_id: int | None = None

    @property
    def refusal(self) -> bool:
        return self.stop_reason == "refusal"

    @property
    def truncated(self) -> bool:
        return self.stop_reason == "max_tokens"

    def to_dict(self) -> dict:
        return {
            "text": self.text, "data": self.data,
            "tool_calls": [{"id": c.id, "name": c.name, "input": c.input} for c in self.tool_calls],
            "usage": {"input_tokens": self.usage.input_tokens, "output_tokens": self.usage.output_tokens,
                      "cache_read_tokens": self.usage.cache_read_tokens,
                      "cache_write_tokens": self.usage.cache_write_tokens, "iterations": self.usage.iterations},
            "model": self.model, "provider": self.provider, "provider_id": self.provider_id, "kind": self.kind,
            "stop_reason": self.stop_reason, "cost": self.cost, "raw": self.raw, "fallback_to": self.fallback_to,
            "tools_disabled": self.tools_disabled, "warnings": self.warnings,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Result":
        u = d.get("usage") or {}
        return cls(
            text=d.get("text") or "", data=d.get("data"),
            tool_calls=[ToolCall(c["id"], c["name"], c.get("input") or {}) for c in d.get("tool_calls") or []],
            usage=Usage(u.get("input_tokens", 0), u.get("output_tokens", 0), u.get("cache_read_tokens", 0),
                        u.get("cache_write_tokens", 0), u.get("iterations") or []),
            model=d.get("model") or "", provider=d.get("provider") or "", provider_id=d.get("provider_id"),
            kind=d.get("kind") or "", stop_reason=d.get("stop_reason") or "end_turn", cost=d.get("cost") or 0.0,
            raw=d.get("raw"), fallback_to=d.get("fallback_to") or "", tools_disabled=bool(d.get("tools_disabled")),
            warnings=d.get("warnings") or [],
        )


def strict_schema(schema: dict) -> dict:
    """Copie du schéma où chaque objet interdit les propriétés non déclarées (exigé par les sorties structurées)."""
    s = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                node.setdefault("additionalProperties", False)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(s)
    return s


def _all_required(schema) -> bool:
    if isinstance(schema, dict):
        props = schema.get("properties")
        if isinstance(props, dict) and set(props) - set(schema.get("required") or []):
            return False
        return all(_all_required(v) for v in schema.values())
    if isinstance(schema, list):
        return all(_all_required(v) for v in schema)
    return True


def json_instruction(schema: dict) -> str:
    return ("\n\nRéponds uniquement par un objet JSON valide, sans texte autour ni bloc de code, qui respecte ce "
            "schéma JSON :\n" + json.dumps(schema, ensure_ascii=False))


def is_local_url(url: str) -> bool:
    host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    if not host:
        return False
    if host in ("localhost", "host.docker.internal") or host.endswith(".local") or host.endswith(".lan"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def pdf_to_text(data: bytes, max_chars: int = 150_000) -> tuple[str, bool]:
    """Texte d'un PDF (pypdf), page par page ; (texte, tronqué ?)."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        parts, total = [], 0
        for n, page in enumerate(reader.pages, 1):
            t = (page.extract_text() or "").strip()
            if not t:
                continue
            chunk = f"[page {n}]\n{t}"
            parts.append(chunk)
            total += len(chunk)
            if total > max_chars:
                break
    except Exception as e:  # PDF illisible ou protégé
        raise AIError(f"PDF illisible : {e}", "requete", 400) from None
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        return text[:max_chars], True
    return text, False


# ---------------------------------------------------------------------------
# Fournisseurs
# ---------------------------------------------------------------------------

class Provider:
    kind = ""
    supports_batch = False

    def __init__(self, row):
        self.row = row

    @property
    def name(self) -> str:
        return self.row.name

    def tools_supported(self, model: str) -> bool:
        return True

    def stream(self, req: Request) -> Iterator[dict]:  # pragma: no cover - interface
        raise NotImplementedError

    def list_models(self) -> list[dict]:  # pragma: no cover - interface
        raise NotImplementedError

    def test(self) -> tuple[bool | None, str]:  # pragma: no cover - interface
        raise NotImplementedError


def get(row) -> Provider:
    return AnthropicProvider(row) if row.kind == "anthropic" else OpenAICompatProvider(row)


def _flask_config(key: str):
    try:
        from flask import current_app, has_app_context
        return current_app.config.get(key) if has_app_context() else None
    except Exception:  # pragma: no cover
        return None


# --- Claude ------------------------------------------------------------------------------------

_KEEP_FIELDS = {
    "text": ("type", "text"),
    "thinking": ("type", "thinking", "signature"),
    "redacted_thinking": ("type", "data"),
    "tool_use": ("type", "id", "name", "input"),
    "fallback": ("type", "from", "to", "trigger"),
}


def _block_param(block) -> dict | None:
    """Bloc de réponse -> paramètre de requête (pour renvoyer le tour au modèle sans le modifier)."""
    d = block.to_dict(mode="json") if hasattr(block, "to_dict") else dict(block)
    t = d.get("type")
    keep = _KEEP_FIELDS.get(t)
    if keep:
        return {k: d[k] for k in keep if k in d}
    return d


class AnthropicProvider(Provider):
    kind = "anthropic"
    supports_batch = True

    def client(self, timeout: float | None = None):
        factory = _flask_config("AI_ANTHROPIC_FACTORY")
        if factory:
            return factory(self.row)
        import anthropic

        key = self.row.key()
        if not key:
            raise AIError("Aucune clé d’API Anthropic : ajoute-la dans les réglages de l’IA.", "cle", 401)
        kw: dict = {"api_key": key, "max_retries": 2}
        if self.row.base_url:
            kw["base_url"] = self.row.base_url
        if timeout:
            kw["timeout"] = timeout
        return anthropic.Anthropic(**kw)

    # Construction de la requête ---------------------------------------------------------------
    def _content(self, client, parts, files: list) -> list:
        docs, images, texts = [], [], []
        for p in parts:
            t = p.get("type")
            if t == "image":
                images.append({"type": "image", "source": {"type": "base64", "media_type": p.get("media_type") or "image/jpeg",
                                                           "data": p["data"]}})
            elif t == "document":
                title = p.get("name") or "Document"
                if "text" in p:
                    docs.append({"type": "document", "title": title,
                                 "source": {"type": "text", "media_type": "text/plain", "data": p["text"]}})
                    continue
                data = p["data"]
                if len(data) * 3 // 4 > PDF_INLINE_MAX:
                    try:
                        up = client.files.upload(file=(title, base64.b64decode(data), "application/pdf"))
                    except Exception as e:
                        raise AIError("Envoi du PDF à Anthropic impossible : réessaie plus tard.", "reseau", 503,
                                      str(e)) from None
                    files.append(up.id)
                    source = {"type": "file", "file_id": up.id}
                else:
                    source = {"type": "base64", "media_type": "application/pdf", "data": data}
                docs.append({"type": "document", "source": source, "title": title})
            elif t == "text":
                if p.get("text"):
                    texts.append({"type": "text", "text": p["text"]})
        return docs + images + texts

    def build(self, req: Request, client, *, for_batch: bool = False) -> tuple[dict, list]:
        files: list = []
        msgs = []
        for m in req.messages:
            role = m.get("role")
            if role == "tool":
                msgs.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r["id"], "content": str(r.get("content") or "(vide)"),
                     **({"is_error": True} if r.get("is_error") else {})} for r in m.get("results") or []]})
            elif role == "assistant":
                res = m.get("result")
                if res is not None and res.kind == "anthropic" and res.raw:
                    msgs.append({"role": "assistant", "content": res.raw})
                else:
                    msgs.append({"role": "assistant", "content": (res.text if res is not None else m.get("content")) or "…"})
            elif role == "system":
                msgs.append({"role": "system", "content": m.get("content") or ""})
            else:
                c = m.get("content")
                msgs.append({"role": "user", "content": c if isinstance(c, str) else self._content(client, c or [], files)})
        model = req.model or CLAUDE_DEFAULT_MODEL
        body: dict = {"model": model, "max_tokens": req.max_tokens or 16000, "messages": msgs}
        system = req.system or ""
        oc: dict = {}
        if req.effort and claude_supports_effort(model):
            oc["effort"] = req.effort
        if req.json_schema is not None:
            if claude_supports_format(model):
                oc["format"] = {"type": "json_schema", "schema": strict_schema(req.json_schema)}
            else:
                system += json_instruction(req.json_schema)
        if system:
            block = {"type": "text", "text": system}
            if req.cache_system:
                block["cache_control"] = {"type": "ephemeral"}
            body["system"] = [block]
        if oc:
            body["output_config"] = oc
        if req.cache_messages and not for_batch:
            body["cache_control"] = {"type": "ephemeral"}  # dernier bloc : l'historique est relu au tour suivant
        if req.tools:
            eager = not for_batch and not self.row.base_url
            body["tools"] = [{"name": t["name"], "description": t.get("description", ""), "input_schema": t["input_schema"],
                              **({"eager_input_streaming": True} if eager else {})} for t in req.tools]
        if not for_batch and model in FALLBACK_MODELS:
            body["betas"] = [FALLBACK_BETA]
            body["fallbacks"] = "default"
        return body, files

    # Appel ------------------------------------------------------------------------------------
    def stream(self, req: Request) -> Iterator[dict]:
        client = self.client(req.timeout)
        body, files = self.build(req, client)
        try:
            yield from self._run(client, body)
        finally:
            for fid in files:
                try:
                    client.files.delete(fid)
                except Exception:  # noqa: BLE001 - nettoyage au mieux
                    pass

    def _run(self, client, body: dict) -> Iterator[dict]:
        import anthropic

        attempt = 0
        while True:
            try:
                with client.beta.messages.stream(**body) as s:
                    for ev in s:
                        t = getattr(ev, "type", "")
                        if t == "text":
                            if ev.text:
                                yield {"type": "text", "text": ev.text}
                        elif t == "content_block_start":
                            cb = ev.content_block
                            if cb.type == "tool_use":
                                yield {"type": "tool_start", "name": cb.name}
                            elif cb.type == "fallback":
                                yield {"type": "fallback", "to": getattr(getattr(cb, "to", None), "model", "")}
                    msg = s.get_final_message()
                break
            except anthropic.BadRequestError as e:
                if attempt == 0 and "fallbacks" in body and "fallback" in str(getattr(e, "message", e)).lower():
                    # Passerelle qui ne connaît pas le repli : on réessaie sans.
                    body = {k: v for k, v in body.items() if k not in ("fallbacks", "betas")}
                    attempt += 1
                    continue
                raise self.error(e, body["model"]) from None
            except anthropic.APIError as e:
                raise self.error(e, body["model"]) from None
            except ValueError:
                # Entrée d'outil illisible (flux des entrées d'outils) : on refait le tour, deux fois au plus.
                if attempt < 2:
                    attempt += 1
                    yield {"type": "reset"}
                    continue
                raise AIError("Réponse de Claude illisible (appel d’outil incomplet) : réessaie.", "reponse") from None
        yield {"type": "done", "result": self.result(msg, body["model"])}

    def result(self, msg, requested: str) -> Result:
        blocks = list(msg.content or [])
        last_fb = max((i for i, b in enumerate(blocks) if b.type == "fallback"), default=-1)
        raw, calls, texts = [], [], []
        for i, b in enumerate(blocks):
            if b.type == "text":
                texts.append(b.text)
            # Après un repli en cours de réponse, les blocs d'avant la frontière (sauf le texte) ne se renvoient pas.
            if i < last_fb and b.type not in ("text", "fallback"):
                continue
            d = _block_param(b)
            if d:
                raw.append(d)
            if b.type == "tool_use":
                calls.append(ToolCall(b.id, b.name, b.input if isinstance(b.input, dict) else {}))
        u = msg.usage
        usage = Usage(input_tokens=getattr(u, "input_tokens", 0) or 0, output_tokens=getattr(u, "output_tokens", 0) or 0,
                      cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                      cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0)
        fallback_to = ""
        for it in getattr(u, "iterations", None) or []:
            usage.iterations.append({
                "type": it.type, "model": getattr(it, "model", None) or requested,
                "input_tokens": getattr(it, "input_tokens", 0) or 0, "output_tokens": getattr(it, "output_tokens", 0) or 0,
                "cache_read_tokens": getattr(it, "cache_read_input_tokens", 0) or 0,
                "cache_write_tokens": getattr(it, "cache_creation_input_tokens", 0) or 0})
            if it.type == "fallback_message":
                fallback_to = getattr(it, "model", "") or msg.model
        return Result(text="".join(texts).strip(), tool_calls=calls, usage=usage, model=msg.model or requested,
                      kind="anthropic", stop_reason=msg.stop_reason or "end_turn", raw=raw, fallback_to=fallback_to)

    def error(self, e, model: str) -> AIError:
        import anthropic

        msg = str(getattr(e, "message", "") or e)
        rid = getattr(e, "request_id", None)
        detail = f"{type(e).__name__} {getattr(e, 'status_code', '') or ''}: {msg}" + (f" (requête {rid})" if rid else "")
        if isinstance(e, anthropic.AuthenticationError):
            return AIError("Clé d’API refusée par Anthropic : vérifie-la dans les réglages de l’IA.", "cle", 401, detail)
        if isinstance(e, anthropic.PermissionDeniedError):
            return AIError("Cette clé d’API n’a pas accès à ce modèle.", "acces", 403, detail)
        if isinstance(e, anthropic.NotFoundError):
            return AIError(f"Modèle « {model} » introuvable chez Anthropic : vérifie son nom dans les réglages.",
                           "modele", 404, detail)
        if isinstance(e, anthropic.RateLimitError):
            return AIError("Limite d’utilisation de l’API d’Anthropic atteinte : réessaie dans un moment.", "limite",
                           429, detail)
        if isinstance(e, anthropic.BadRequestError):
            if "credit balance" in msg.lower():
                return AIError("Plus de crédits d’API sur ce compte Anthropic : recharge-les sur console.anthropic.com.",
                               "credits", 402, detail)
            return AIError(f"Requête refusée par l’API d’Anthropic : {msg}", "requete", 400, detail)
        if isinstance(e, anthropic.APIStatusError):
            return AIError(f"Service de Claude indisponible (erreur {e.status_code}) : réessaie plus tard.", "service",
                           503, detail)
        if isinstance(e, anthropic.APITimeoutError):
            return AIError("Claude n’a pas répondu à temps : réessaie.", "delai", 504, detail)
        if isinstance(e, anthropic.APIConnectionError):
            return AIError("Serveur de Claude injoignable : vérifie la connexion à Internet.", "reseau", 503, detail)
        return AIError(f"Erreur de l’API d’Anthropic : {msg}", "erreur", 502, detail)

    # Modèles, test, lots ----------------------------------------------------------------------
    def list_models(self) -> list[dict]:
        import anthropic

        try:
            items = list(self.client(timeout=20).models.list(limit=100))
        except anthropic.APIError as e:
            raise self.error(e, "") from None
        return [{"id": m.id, "name": getattr(m, "display_name", "") or m.id} for m in items]

    def test(self) -> tuple[bool | None, str]:
        import anthropic

        if not self.row.key() and not _flask_config("AI_ANTHROPIC_FACTORY"):
            return False, "Aucune clé d’API : colle ta clé (sk-ant-…) ou indique la variable d’environnement qui la contient."
        model = self.row.model or CLAUDE_DEFAULT_MODEL
        try:
            self.client(timeout=20).models.retrieve(model)  # gratuit : aucun texte généré
        except anthropic.APIError as e:
            err = self.error(e, model)
            return (None if err.code in ("reseau", "service", "delai", "limite") else False), err.message
        except AIError as e:
            return False, e.message
        return True, f"Connexion réussie : la clé donne accès à {model}."

    def batch_submit(self, items: list[tuple[str, Request]]) -> tuple[str, list]:
        """Dépose des requêtes dans un lot (Message Batches : moitié prix, résultats sous 24 h)."""
        import anthropic

        client = self.client()
        reqs, files = [], []
        for custom_id, req in items:
            body, f = self.build(req, client, for_batch=True)
            files += f
            reqs.append({"custom_id": custom_id, "params": body})
        try:
            batch = client.beta.messages.batches.create(requests=reqs)
        except anthropic.APIError as e:
            self.delete_files(files)
            raise self.error(e, items[0][1].model if items else "") from None
        return batch.id, files

    def batch_poll(self, batch_id: str) -> dict | None:
        """None tant que le lot tourne ; sinon {custom_id: Result | AIError}."""
        import anthropic

        client = self.client()
        try:
            batch = client.beta.messages.batches.retrieve(batch_id)
            if batch.processing_status != "ended":
                return None
            results = list(client.beta.messages.batches.results(batch_id))
        except anthropic.APIError as e:
            raise self.error(e, "") from None
        out: dict = {}
        for r in results:
            if r.result.type == "succeeded":
                out[r.custom_id] = self.result(r.result.message, r.result.message.model)
            else:
                detail = getattr(getattr(r.result, "error", None), "message", "") or r.result.type
                out[r.custom_id] = AIError(f"Lot : {detail}", "lot")
        return out

    def delete_files(self, ids) -> None:
        if not ids:
            return
        client = self.client()
        for fid in ids:
            try:
                client.files.delete(fid)
            except Exception:  # noqa: BLE001
                pass


# --- API compatibles OpenAI (Ollama, LM Studio, vLLM, llama.cpp, OpenRouter…) -----------------

_NO_TOOLS: dict[tuple, float] = {}  # (fournisseur, modèle) -> instant où le serveur a refusé les outils
_NO_TOOLS_TTL = 6 * 3600


class _HTTPFailure(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _read_error(e: urllib.error.HTTPError) -> str:
    try:
        raw = e.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return str(e.reason or e)
    try:
        data = json.loads(raw)
    except ValueError:
        return raw.strip()[:500] or str(e.reason or e)
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)[:500]
    if err:
        return str(err)[:500]
    return str(data.get("message") or data.get("detail") or raw)[:500] if isinstance(data, dict) else raw[:500]


class _ThinkFilter:
    """Retire les passages <think>…</think> que certains modèles locaux (qwen3, deepseek-r1…) écrivent en clair."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.buf = ""
        self.inside = False

    def feed(self, s: str) -> str:
        self.buf += s
        out = []
        while self.buf:
            if self.inside:
                i = self.buf.find(self.CLOSE)
                if i < 0:
                    self.buf = self.buf[-(len(self.CLOSE) - 1):]
                    break
                self.buf = self.buf[i + len(self.CLOSE):].lstrip("\n")
                self.inside = False
            else:
                i = self.buf.find(self.OPEN)
                if i < 0:
                    k = next((n for n in range(min(len(self.buf), len(self.OPEN) - 1), 0, -1)
                              if self.OPEN.startswith(self.buf[-n:])), 0)
                    out.append(self.buf[:len(self.buf) - k])
                    self.buf = self.buf[len(self.buf) - k:]
                    break
                out.append(self.buf[:i])
                self.buf = self.buf[i + len(self.OPEN):]
                self.inside = True
        return "".join(out)

    def flush(self) -> str:
        s = "" if self.inside else self.buf
        self.buf = ""
        return s


def _mentions(text: str, *words: str) -> bool:
    return any(w in text for w in words)


_FINISH = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use", "function_call": "tool_use",
           "content_filter": "refusal"}


class OpenAICompatProvider(Provider):
    kind = "openai_compatible"

    def base(self) -> str:
        return (self.row.base_url or "").strip().rstrip("/")

    def root(self) -> str:
        b = self.base()
        return b[:-3] if b.endswith("/v1") else b

    def is_ollama(self) -> bool:
        return self.row.preset == "ollama" or ":11434" in self.base()

    def tools_supported(self, model: str) -> bool:
        mode = self.row.tools_mode or "auto"
        if mode == "off":
            return False
        if mode == "on":
            return True
        t = _NO_TOOLS.get((self.row.id, model))
        return not (t and time.time() - t < _NO_TOOLS_TTL)

    # HTTP -------------------------------------------------------------------------------------
    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "User-Agent": "Hangar"}
        k = self.row.key()
        if k:
            h["Authorization"] = f"Bearer {k}"
        return h

    def _open(self, url: str, body: dict | None = None, timeout: float = 120):
        if not self.base():
            raise AIError("Adresse du serveur manquante : indique-la dans les réglages de l’IA.", "requete", 400)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST" if data else "GET")
        # Pas de proxy pour un serveur local (Ollama, LM Studio…), proxy du système sinon.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if is_local_url(url) \
            else urllib.request.build_opener()
        try:
            return opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            raise _HTTPFailure(e.code, _read_error(e)) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            reason = getattr(e, "reason", e)
            hint = " Ollama est-il lancé (« ollama serve ») ?" if self.is_ollama() else ""
            if isinstance(reason, (socket.timeout, TimeoutError)) or isinstance(e, (socket.timeout, TimeoutError)):
                raise AIError(f"{self.row.name} n’a pas répondu à temps ({self.base()}).", "delai", 504, str(e)) from None
            raise AIError(f"Impossible de joindre {self.row.name} à l’adresse {self.base()}.{hint}", "reseau", 503,
                          str(reason)) from None

    def _get_json(self, url: str, timeout: float = 15) -> dict:
        try:
            with self._open(url, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace") or "{}")
        except _HTTPFailure as f:
            raise self._http_error(f, "") from None
        except ValueError:
            raise AIError(f"Réponse illisible de {url}.", "reponse", 502) from None

    def _http_error(self, f: _HTTPFailure, model: str) -> AIError:
        detail = f"HTTP {f.status}: {f.message}"
        low = f.message.lower()
        if f.status in (401, 403):
            return AIError(f"Clé d’API refusée par {self.row.name} : vérifie-la dans les réglages de l’IA.", "cle",
                           401, detail)
        if f.status == 404 or ("not found" in low and "model" in low):
            hint = f" Installe-le avec « ollama pull {model} »." if self.is_ollama() and model else ""
            what = f"Modèle « {model} » introuvable" if model and "model" in low else "Adresse ou modèle introuvable"
            return AIError(f"{what} sur {self.row.name}.{hint}", "modele", 404, detail)
        if f.status == 429:
            return AIError(f"Limite d’utilisation de {self.row.name} atteinte : réessaie dans un moment.", "limite",
                           429, detail)
        if f.status == 402:
            return AIError(f"Crédit épuisé chez {self.row.name}.", "credits", 402, detail)
        if f.status >= 500:
            return AIError(f"{self.row.name} a renvoyé une erreur ({f.status}) : {f.message[:200]}", "service", 503,
                           detail)
        return AIError(f"Requête refusée par {self.row.name} : {f.message[:300]}", "requete", 400, detail)

    # Messages ---------------------------------------------------------------------------------
    def _user_content(self, parts) -> str | list:
        texts, images = [], []
        for p in parts:
            t = p.get("type")
            if t == "text" and p.get("text"):
                texts.append(p["text"])
            elif t == "image":
                images.append({"type": "image_url", "image_url": {
                    "url": f"data:{p.get('media_type') or 'image/jpeg'};base64,{p['data']}"}})
            elif t == "document":
                name = p.get("name") or "Document"
                if "text" in p:
                    texts.insert(0, f"Document « {name} » :\n\n{p['text']}")
                else:
                    text, cut = pdf_to_text(base64.b64decode(p["data"]))
                    note = "\n\n[Document tronqué : il dépasse la taille lue par ce modèle.]" if cut else ""
                    texts.insert(0, f"Document « {name} » (texte extrait du PDF) :\n\n{text or '(aucun texte lisible)'}{note}")
        if not images:
            return "\n\n".join(texts)
        return [{"type": "text", "text": "\n\n".join(texts)}] + images if texts else images

    def _messages(self, req: Request, system: str) -> list:
        out = []
        if system:
            out.append({"role": "system", "content": system})
        for m in req.messages:
            role = m.get("role")
            if role == "tool":
                for r in m.get("results") or []:
                    out.append({"role": "tool", "tool_call_id": r["id"], "content": str(r.get("content") or "(vide)")})
            elif role == "assistant":
                res = m.get("result")
                if res is not None and res.kind == self.kind and res.raw:
                    out.append(res.raw)
                else:
                    out.append({"role": "assistant", "content": (res.text if res is not None else m.get("content")) or ""})
            elif role == "system":
                out.append({"role": "system", "content": m.get("content") or ""})
            else:
                c = m.get("content")
                out.append({"role": "user", "content": c if isinstance(c, str) else self._user_content(c or [])})
        return out

    # Appel ------------------------------------------------------------------------------------
    def stream(self, req: Request) -> Iterator[dict]:
        model = req.model
        system = req.system or ""
        body: dict = {"model": model, "stream": True, "stream_options": {"include_usage": True}}
        if req.max_tokens:
            body["max_tokens"] = req.max_tokens
        if req.tools and self.tools_supported(model):
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                               "parameters": t["input_schema"]}} for t in req.tools]
        json_mode = None
        if req.json_schema is not None:
            schema = strict_schema(req.json_schema)
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "reponse", "schema": schema, "strict": _all_required(schema)}}
            json_mode = "schema"
        tools_disabled = bool(req.tools) and "tools" not in body
        for _ in range(6):
            body["messages"] = self._messages(req, system)
            try:
                resp = self._open(self.base() + "/chat/completions", body, timeout=req.timeout or 300)
                break
            except _HTTPFailure as f:
                if f.status not in (400, 415, 422, 500, 501):
                    raise self._http_error(f, model) from None
                low = f.message.lower()
                if "stream_options" in body and _mentions(low, "stream_options", "include_usage"):
                    body.pop("stream_options")
                elif "tools" in body and _mentions(low, "tool", "function"):
                    body.pop("tools")
                    _NO_TOOLS[(self.row.id, model)] = time.time()
                    tools_disabled = True
                elif json_mode and _mentions(low, "response_format", "json_schema", "schema", "json", "format"):
                    json_mode, system = self._downgrade_json(body, json_mode, req, system)
                elif "stream_options" in body:
                    body.pop("stream_options")
                elif json_mode:
                    json_mode, system = self._downgrade_json(body, json_mode, req, system)
                elif "tools" in body and f.status != 500:
                    body.pop("tools")
                    tools_disabled = True
                else:
                    raise self._http_error(f, model) from None
        else:  # pragma: no cover - toutes les tentatives ont échoué
            raise AIError(f"{self.row.name} refuse la requête.", "requete", 400)
        with resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "event-stream" in ctype or "ndjson" in ctype or not ctype.startswith("application/json"):
                gen = self._read_sse(resp, model)
            else:
                gen = self._read_json(resp, model)
            for ev in gen:
                if ev["type"] == "done":
                    ev["result"].tools_disabled = tools_disabled
                yield ev

    def _downgrade_json(self, body: dict, json_mode: str, req: Request, system: str):
        """json_schema -> json_object -> consigne seule (le JSON est alors extrait du texte)."""
        if json_mode == "schema":
            body["response_format"] = {"type": "json_object"}
            return "object", system + json_instruction(req.json_schema)
        body.pop("response_format", None)
        return "prompt", system if "schéma JSON" in system else system + json_instruction(req.json_schema)

    def _finish(self, model, text, tool_acc, finish, usage) -> Result:
        calls = []
        raw_calls = []
        for n, (_, acc) in enumerate(sorted(tool_acc.items())):
            args = acc["args"]
            try:
                parsed = json.loads(args) if isinstance(args, str) and args.strip() else (args if isinstance(args, dict) else {})
            except ValueError:
                parsed = {"__invalid__": args}
            if not isinstance(parsed, dict):
                parsed = {"__invalid__": args}
            cid = acc["id"] or f"call_{n + 1}"
            calls.append(ToolCall(cid, acc["name"], parsed))
            raw_calls.append({"id": cid, "type": "function", "function": {
                "name": acc["name"], "arguments": args if isinstance(args, str) else json.dumps(args)}})
        raw = {"role": "assistant", "content": text or ""}
        if raw_calls:
            raw["tool_calls"] = raw_calls
        stop = _FINISH.get(finish or "", "end_turn")
        if calls and stop == "end_turn":
            stop = "tool_use"
        u = usage or {}
        details = u.get("prompt_tokens_details") or {}
        return Result(text=text.strip(), tool_calls=calls, model=model, kind=self.kind, stop_reason=stop, raw=raw,
                      usage=Usage(input_tokens=int(u.get("prompt_tokens") or 0),
                                  output_tokens=int(u.get("completion_tokens") or 0),
                                  cache_read_tokens=int(details.get("cached_tokens") or 0)))

    def _read_sse(self, resp, model) -> Iterator[dict]:
        filt = _ThinkFilter()
        parts, tool_acc, finish, usage, served = [], {}, None, None, model
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            try:
                chunk = json.loads(line)
            except ValueError:
                continue
            if not isinstance(chunk, dict):
                continue
            if chunk.get("error"):
                err = chunk["error"]
                raise AIError(f"{self.row.name} : {err.get('message') if isinstance(err, dict) else err}", "service", 502)
            if chunk.get("usage"):
                usage = chunk["usage"]
            served = chunk.get("model") or served
            for ch in chunk.get("choices") or []:
                delta = ch.get("delta") or ch.get("message") or {}
                content = delta.get("content")
                if isinstance(content, str) and content:
                    out = filt.feed(content)
                    if out:
                        parts.append(out)
                        yield {"type": "text", "text": out}
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", len(tool_acc)) if isinstance(tc.get("index"), int) else len(tool_acc)
                    acc = tool_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if tc.get("id"):
                        acc["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name") and not acc["name"]:
                        acc["name"] = fn["name"]
                        yield {"type": "tool_start", "name": fn["name"]}
                    a = fn.get("arguments")
                    if isinstance(a, dict):
                        acc["args"] = a
                    elif a:
                        acc["args"] = (acc["args"] if isinstance(acc["args"], str) else "") + a
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
        rest = filt.flush()
        if rest:
            parts.append(rest)
            yield {"type": "text", "text": rest}
        yield {"type": "done", "result": self._finish(served, "".join(parts), tool_acc, finish, usage)}

    def _read_json(self, resp, model) -> Iterator[dict]:
        try:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except ValueError:
            raise AIError(f"Réponse illisible de {self.row.name}.", "reponse", 502) from None
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        filt = _ThinkFilter()
        text = filt.feed(msg.get("content") or "") + filt.flush()
        if text:
            yield {"type": "text", "text": text}
        acc = {i: {"id": tc.get("id") or "", "name": (tc.get("function") or {}).get("name") or "",
                   "args": (tc.get("function") or {}).get("arguments") or ""}
               for i, tc in enumerate(msg.get("tool_calls") or [])}
        yield {"type": "done", "result": self._finish(data.get("model") or model, text, acc, choice.get("finish_reason"),
                                                       data.get("usage"))}

    # Modèles, test ----------------------------------------------------------------------------
    def list_models(self) -> list[dict]:
        if self.is_ollama():
            try:
                data = self._get_json(self.root() + "/api/tags")
                out = []
                for m in data.get("models") or []:
                    d = m.get("details") or {}
                    extra = " · ".join(x for x in (d.get("parameter_size"), d.get("quantization_level")) if x)
                    out.append({"id": m.get("name") or m.get("model"), "name": m.get("name") or m.get("model"),
                                "detail": extra})
                return out
            except AIError as e:
                if e.code == "reseau":
                    raise
        data = self._get_json(self.base() + "/models")
        items = data.get("data") if isinstance(data.get("data"), list) else data.get("models") or []
        return [{"id": m.get("id") or m.get("name"), "name": m.get("name") or m.get("id"), "detail": ""}
                for m in items if isinstance(m, dict) and (m.get("id") or m.get("name"))]

    def test(self) -> tuple[bool | None, str]:
        if self.row.needs_key and not self.row.key():
            return False, "Aucune clé d’API : colle ta clé ou indique la variable d’environnement qui la contient."
        try:
            models = self.list_models()
        except AIError as e:
            return (None if e.code in ("delai", "service") else False), e.message
        ids = [m["id"] for m in models]
        model = self.row.model
        if not model:
            return True, f"Connexion réussie : {len(ids)} modèle(s) disponible(s). Choisis le modèle à utiliser."
        if ids and not any(i == model or i == f"{model}:latest" or i.split(":")[0] == model for i in ids):
            hint = f" Installe-le avec « ollama pull {model} »." if self.is_ollama() else ""
            return False, f"Connexion réussie, mais le modèle « {model} » n’est pas disponible.{hint}"
        return True, f"Connexion réussie : le modèle « {model} » est disponible ({len(ids)} modèle(s) en tout)."
