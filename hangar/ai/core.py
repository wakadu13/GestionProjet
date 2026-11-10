"""Cœur de l'assistant IA : choix du fournisseur, `generate()`, boucle d'outils, usage, plafond, erreurs.

Interface unique pour tous les modules (panneau IA, wiki, QCM…) :

    from hangar.ai import generate, AIError
    r = generate("Explique la loi d'Ohm", system="Tu es un tuteur…", role="rapide", feature="tuteur")
    r.text, r.usage, r.model, r.provider, r.cost

    r = generate(messages, json_schema=SCHEMA, documents=[pdf_bytes], max_tokens=64000, feature="qcm")
    r.data  # JSON validé contre le schéma

    for ev in generate(messages, stream=True):   # {"type": "text", "text"} … puis {"type": "done", "result"}
        ...

Rôles : "chat" (par défaut), "rapide" (explications courtes, effort bas), "vision" (images, copies manuscrites).
Le plafond mensuel (réglages) et le plafond propre à chaque fournisseur sont vérifiés avant chaque appel.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
from datetime import date, datetime, time
from typing import Any, Callable, Iterable, Iterator

from sqlalchemy import func

from ..extensions import db
from ..models import now
from . import providers as P
from .models import AIErrorLog, AIProvider, AISettings, AIUsage
from .providers import AIError, Request, Result, ToolCall

log = logging.getLogger(__name__)

EFFORT_BY_ROLE = {"rapide": "low"}
EFFORTS = ("low", "medium", "high", "xhigh", "max")
CLAUDE_MAX_TOKENS = 16000
LOCAL_MAX_TOKENS = 32000
MAX_TOOL_ROUNDS = 6


# ---------------------------------------------------------------------------
# Fournisseurs
# ---------------------------------------------------------------------------

def enabled_providers() -> list[AIProvider]:
    return AIProvider.query.filter_by(enabled=True).order_by(AIProvider.priority, AIProvider.id).all()


def pick(role: str = "chat", provider=None) -> AIProvider:
    """Fournisseur pour un rôle : le premier activé (priorité) qui déclare ce rôle et n'a pas atteint son plafond."""
    if provider is not None:
        row = provider if isinstance(provider, AIProvider) else db.session.get(AIProvider, int(provider))
        if row is None:
            raise AIError("Fournisseur d’IA introuvable.", "indisponible", 404)
        return row
    rows = [p for p in enabled_providers() if p.usable]
    if not rows:
        raise AIError("Aucun fournisseur d’IA n’est configuré : ajoute Claude ou un modèle local dans les réglages "
                      "de l’IA.", "indisponible", 503)
    preferred = [p for p in rows if role in p.role_list] or [p for p in rows if "chat" in p.role_list] or rows
    capped = []
    for p in preferred:
        if p.monthly_cap and month_cost(provider_id=p.id) >= p.monthly_cap:
            capped.append(p.name)
            continue
        return p
    raise AIError(f"Plafond mensuel de dépense atteint ({', '.join(capped)}) : réessaie le mois prochain ou "
                  "relève le plafond dans les réglages.", "plafond", 429)


def model_label(model: str) -> str:
    for mid, label, _ in P.CLAUDE_MODELS:
        if model == mid:
            return label
    return model


# ---------------------------------------------------------------------------
# Usage, coût, plafond
# ---------------------------------------------------------------------------

def month_start(d: date | None = None) -> datetime:
    d = d or date.today()
    return datetime.combine(d.replace(day=1), time(0))


def month_cost(user_id: int | None = None, provider_id: int | None = None, start: datetime | None = None) -> float:
    q = db.session.query(func.coalesce(func.sum(AIUsage.cost), 0.0)).filter(AIUsage.created_at >= (start or month_start()))
    if user_id is not None:
        q = q.filter(AIUsage.user_id == user_id)
    if provider_id is not None:
        q = q.filter(AIUsage.provider_id == provider_id)
    return float(q.scalar() or 0.0)


def cap_reached() -> bool:
    cap = AISettings.get().monthly_cap or 0
    return cap > 0 and month_cost() >= cap


def estimate_cost(row: AIProvider, res: Result, batch: bool = False) -> float:
    u = res.usage
    if row.kind == "anthropic":
        entries = u.iterations or [{"model": res.model, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                                    "cache_read_tokens": u.cache_read_tokens, "cache_write_tokens": u.cache_write_tokens}]
        total = 0.0
        for it in entries:
            price = P.claude_price(it.get("model") or res.model) or P.claude_price(res.model)
            if not price:
                continue
            pin, pout, pread = price
            inp, cr, cw = it.get("input_tokens", 0), it.get("cache_read_tokens", 0), it.get("cache_write_tokens", 0)
            if (it.get("model") or res.model).startswith("claude-haiku-5-5") and inp + cr + cw > 100_000:
                pin, pout, pread = 0.50, 2.50, 0.05  # Haiku 5.5 : tarif des longs prompts
            total += (inp * pin + it.get("output_tokens", 0) * pout + cr * pread + cw * pin * P.CACHE_WRITE_FACTOR) / 1e6
    else:
        total = (u.input_tokens * (row.price_input or 0) + u.output_tokens * (row.price_output or 0)) / 1e6
    return round(total * (P.BATCH_DISCOUNT if batch else 1), 6)


def _user_id(user) -> int | None:
    if user is None:
        try:
            from flask import has_request_context
            from flask_login import current_user
            if has_request_context() and current_user.is_authenticated:
                return current_user.id
        except Exception:  # noqa: BLE001
            return None
        return None
    return user if isinstance(user, int) else getattr(user, "id", None)


def record_usage(row: AIProvider, res: Result, user=None, feature: str = "chat", batch: bool = False) -> None:
    res.cost = estimate_cost(row, res, batch)
    u = AIUsage(user_id=_user_id(user), provider_id=row.id, provider_name=row.name, model=res.model, feature=feature,
                input_tokens=res.usage.input_tokens, output_tokens=res.usage.output_tokens,
                cache_read_tokens=res.usage.cache_read_tokens, cache_write_tokens=res.usage.cache_write_tokens,
                cost=res.cost, batch=batch, created_at=now())
    try:
        db.session.add(u)
        db.session.commit()
        res.usage_id = u.id
    except Exception:  # noqa: BLE001 - le suivi ne doit pas faire échouer la réponse
        db.session.rollback()
        log.exception("Usage de l'IA non enregistré")


def record_error(row: AIProvider | None, model: str, feature: str, user, e: AIError) -> None:
    log.warning("IA (%s, %s) : %s %s", row.name if row else "-", model, e.message, e.detail)
    try:
        db.session.add(AIErrorLog(user_id=_user_id(user), provider_name=row.name if row else "", model=model or "",
                                  feature=feature, code=e.code, message=e.message[:500], detail=(e.detail or "")[:4000],
                                  created_at=now()))
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()


# ---------------------------------------------------------------------------
# Pièces jointes : images et PDF
# ---------------------------------------------------------------------------

def _b64(data) -> str:
    if isinstance(data, str):
        return data
    return base64.standard_b64encode(bytes(data)).decode("ascii")


def sniff_image(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def prepare_image(source, max_side: int = 1568, quality: int = 85) -> tuple[bytes, str]:
    """Photo allégée en JPEG (côté ≤ max_side, orientation de l'appareil appliquée) : (octets, type).
    `source` : octets, chemin ou fichier. ValueError si ce n'est pas une image lisible."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    src = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    try:
        with Image.open(src) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            out = io.BytesIO()
            image.save(out, "JPEG", quality=quality, optimize=True)
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
        raise ValueError("Ce n’est pas une image lisible (JPEG, PNG ou WebP).") from None
    return out.getvalue(), "image/jpeg"


def _attachments(images: Iterable, documents: Iterable) -> list[dict]:
    parts = []
    for d in documents or ():
        if isinstance(d, (bytes, bytearray)):
            parts.append({"type": "document", "data": _b64(d), "name": "document.pdf"})
        elif isinstance(d, tuple):
            name, data = d[0], d[1]
            parts.append({"type": "document", "data": _b64(data), "name": name})
        elif isinstance(d, dict):
            if "text" in d:
                parts.append({"type": "document", "text": d["text"], "name": d.get("name") or "Document"})
            else:
                parts.append({"type": "document", "data": _b64(d["data"]), "name": d.get("name") or "document.pdf"})
    for im in images or ():
        if isinstance(im, (bytes, bytearray)):
            parts.append({"type": "image", "data": _b64(im), "media_type": sniff_image(bytes(im))})
        elif isinstance(im, tuple):
            data, mt = im[0], (im[1] if len(im) > 1 else None)
            parts.append({"type": "image", "data": _b64(data),
                          "media_type": mt or (sniff_image(bytes(data)) if not isinstance(data, str) else "image/jpeg")})
        elif isinstance(im, dict):
            data = im["data"]
            parts.append({"type": "image", "data": _b64(data), "media_type": im.get("media_type")
                          or (sniff_image(bytes(data)) if not isinstance(data, str) else "image/jpeg")})
    return parts


def _normalize(messages, images=(), documents=()) -> list[dict]:
    msgs = [{"role": "user", "content": messages}] if isinstance(messages, str) else [dict(m) for m in messages]
    extra = _attachments(images, documents)
    if extra:
        idx = max((i for i, m in enumerate(msgs) if m.get("role", "user") == "user"), default=None)
        if idx is None:
            msgs.append({"role": "user", "content": []})
            idx = len(msgs) - 1
        c = msgs[idx].get("content")
        parts = [{"type": "text", "text": c}] if isinstance(c, str) else list(c or [])
        msgs[idx]["content"] = extra + parts
    return msgs


# ---------------------------------------------------------------------------
# JSON : extraction robuste et validation légère
# ---------------------------------------------------------------------------

def extract_json(text: str):
    t = (text or "").strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    m = re.search(r"```(?:json|JSON)?\s*(.*?)```", t, re.S)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except ValueError:
            pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = t.find(opener)
        while start >= 0:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(t)):
                ch = t[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                elif ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(t[start:i + 1])
                        except ValueError:
                            break
            start = t.find(opener, start + 1)
    raise AIError("Réponse illisible : le modèle n’a pas renvoyé de JSON valide.", "json", 502)


_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _type_ok(value, t: str) -> bool:
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    cls = _TYPES.get(t)
    return cls is None or isinstance(value, cls)


def validate_json(value, schema: dict, path: str = "$") -> list[str]:
    """Erreurs de conformité (types, champs obligatoires, énumérations) ; liste vide si tout va bien."""
    if not isinstance(schema, dict):
        return []
    errors: list[str] = []
    t = schema.get("type")
    if t:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(value, x) for x in types):
            return [f"{path} : type {types[0]} attendu"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} : valeur hors de {schema['enum']}")
    if isinstance(value, dict):
        for k in schema.get("required") or []:
            if k not in value:
                errors.append(f"{path}.{k} : manquant")
        for k, sub in (schema.get("properties") or {}).items():
            if k in value:
                errors += validate_json(value[k], sub, f"{path}.{k}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            errors += validate_json(item, schema["items"], f"{path}[{i}]")
            if len(errors) > 20:
                break
    return errors


def _parse_json(res: Result, schema: dict):
    if res.truncated:
        raise AIError("Réponse trop longue : le JSON est incomplet. Demande moins d’éléments.", "trop_long", 502)
    data = extract_json(res.text)
    errs = validate_json(data, schema)
    if errs:
        raise AIError("Réponse JSON non conforme : " + " ; ".join(errs[:3]), "json", 502)
    return data


# ---------------------------------------------------------------------------
# generate()
# ---------------------------------------------------------------------------

def prepare(messages, *, system: str | None = None, role: str = "chat", model: str | None = None, provider=None,
            max_tokens: int | None = None, effort: str | None = None, json_schema: dict | None = None,
            images: Iterable = (), documents: Iterable = (), tools: list | None = None, cache_system: bool = True,
            cache_messages: bool = False, timeout: float | None = None,
            check_cap: bool = True) -> tuple[AIProvider, P.Provider, Request]:
    """Choisit le fournisseur et construit la requête neutre (sans l'envoyer) : (ligne, fournisseur, requête)."""
    msgs = _normalize(messages, images, documents)
    row = pick(role, provider)
    if check_cap and cap_reached():
        raise AIError("Plafond mensuel de l’IA atteint : réessaie le mois prochain ou relève le plafond dans les "
                      "réglages.", "plafond", 429)
    prov = P.get(row)
    mdl = row.model_for(role)
    if model and not (model.startswith("claude-") and row.kind != "anthropic"):
        mdl = model
    eff = effort or row.effort or EFFORT_BY_ROLE.get(role, "medium")
    if row.kind != "anthropic" and max_tokens and max_tokens > LOCAL_MAX_TOKENS:
        max_tokens = None  # un serveur local refuse un plafond plus grand que sa mémoire : il choisit lui-même
    req = Request(messages=msgs, model=mdl, system=system, max_tokens=max_tokens or
                  (CLAUDE_MAX_TOKENS if row.kind == "anthropic" else None), effort=eff if eff in EFFORTS else "medium",
                  json_schema=json_schema, tools=tools or None, cache_system=cache_system,
                  cache_messages=cache_messages, timeout=timeout)
    return row, prov, req


def generate(messages, *, system: str | None = None, role: str = "chat", model: str | None = None, provider=None,
             max_tokens: int | None = None, effort: str | None = None, json_schema: dict | None = None,
             images: Iterable = (), documents: Iterable = (), tools: list | None = None, stream: bool = False,
             user=None, feature: str = "chat", cache_system: bool = True, cache_messages: bool = False,
             timeout: float | None = None, allow_refusal: bool = False, check_cap: bool = True):
    """Appel d'un modèle par le fournisseur configuré pour `role`.

    messages     : texte (un message de l'utilisateur) ou liste de messages au format neutre (voir providers.py).
    system       : consigne système (mise en cache chez Claude : garde-la stable, mets le variable dans les messages).
    cache_messages: Claude : met aussi en cache les messages (utile quand le même début est renvoyé : boucle
                   d'outils, conversation) ; inutile pour un appel unique.
    role         : "chat" | "rapide" | "vision" ; model / provider : pour forcer un modèle ou un fournisseur
                   (un nom de modèle « claude-… » est ignoré si le fournisseur choisi n'est pas Claude).
    max_tokens   : plafond de la réponse (Claude : 16 000 par défaut ; jusqu'à 128 000 en flux).
    effort       : "low" | "medium" | "high" | "xhigh" | "max" (Claude ; par défaut low en « rapide », medium sinon).
    json_schema  : réponse JSON validée -> Result.data (Claude : sorties structurées ; autres : response_format,
                   ou consigne + extraction, avec une relance corrective hors flux).
    images       : octets, (octets, type) ou {"data", "media_type"} ; documents : PDF (octets, (nom, octets),
                   {"data", "name"}) ou {"text", "name"} — ajoutés au dernier message de l'utilisateur.
                   Claude reçoit le PDF tel quel (bloc document) ; les autres en reçoivent le texte (pypdf).
    tools        : [{"name", "description", "input_schema"}] -> Result.tool_calls (voir run_agent pour la boucle).
    stream       : False -> Result ; True -> itérateur d'événements (text, tool_start, reset, fallback, done).
    user, feature: pour le suivi de l'usage (par défaut : l'utilisateur connecté).
    allow_refusal: hors flux, renvoyer le Result d'un refus au lieu de lever AIError(code="refus").
    Lève AIError (message lisible, .code, .status) : aucun fournisseur, plafond, clé refusée, réseau…
    """
    row, prov, req = prepare(messages, system=system, role=role, model=model, provider=provider,
                             max_tokens=max_tokens, effort=effort, json_schema=json_schema, images=images,
                             documents=documents, tools=tools, cache_system=cache_system,
                             cache_messages=cache_messages, timeout=timeout, check_cap=check_cap)
    msgs = req.messages
    events = _run(prov, row, req, user=user, feature=feature)
    if stream:
        return events
    result = _last(events)
    if json_schema is not None and isinstance(result, AIError):
        if result.code == "json" and row.kind != "anthropic":
            # Une relance corrective pour les modèles sans sorties structurées.
            req.messages = msgs + [{"role": "assistant", "content": getattr(result, "text", "") or "…"},
                                   {"role": "user", "content": f"{result.message}. Réponds uniquement par le JSON "
                                                               "corrigé, conforme au schéma."}]
            result = _last(_run(prov, row, req, user=user, feature=feature))
        if isinstance(result, AIError):
            raise result
    if result.refusal and not allow_refusal:
        raise AIError("Le modèle a refusé cette demande.", "refus", 422)
    return result


def _last(events: Iterator[dict]):
    result = None
    try:
        for ev in events:
            if ev["type"] == "done":
                result = ev["result"]
    except AIError as e:
        if e.code == "json":
            return e
        raise
    return result


def _run(prov: P.Provider, row: AIProvider, req: Request, *, user=None, feature: str = "chat") -> Iterator[dict]:
    try:
        for ev in prov.stream(req):
            if ev["type"] == "done":
                res: Result = ev["result"]
                res.provider, res.provider_id = row.name, row.id
                record_usage(row, res, user, feature)
                if req.json_schema is not None and not res.refusal:
                    try:
                        res.data = _parse_json(res, req.json_schema)
                    except AIError as e:
                        e.text = res.text  # pour la relance corrective
                        raise
            yield ev
    except AIError as e:
        record_error(row, req.model, feature, user, e)
        raise


# ---------------------------------------------------------------------------
# Boucle d'outils
# ---------------------------------------------------------------------------

def run_agent(messages: list, *, tools: list, execute: Callable[[ToolCall], tuple[str, bool]], system: str | None = None,
              role: str = "chat", user=None, feature: str = "chat", max_rounds: int = MAX_TOOL_ROUNDS,
              **kw) -> Iterator[dict]:
    """Conversation avec outils, en flux : le modèle appelle des outils, `execute(appel) -> (contenu, erreur ?)`
    les exécute, et la boucle continue jusqu'à la réponse finale.

    Événements : ceux de generate (text, tool_start, reset, fallback), puis
      {"type": "tool", "id", "name", "input", "content", "is_error"}  après chaque outil exécuté,
      {"type": "round"}                                                entre deux tours,
      {"type": "done", "result": dernier Result, "results": [...], "text": texte de tous les tours}.
    """
    msgs = list(messages)
    results: list[Result] = []
    texts: list[str] = []
    for rnd in range(max_rounds):
        res = None
        kw.setdefault("cache_messages", True)
        for ev in generate(msgs, system=system, role=role, tools=tools, stream=True, user=user, feature=feature, **kw):
            if ev["type"] == "done":
                res = ev["result"]
            else:
                yield ev
        results.append(res)
        if res.text:
            texts.append(res.text)
        # Rien à exécuter après un refus ou une réponse coupée (entrée d'outil peut-être tronquée).
        if res.refusal or res.truncated or not res.tool_calls:
            break
        msgs.append({"role": "assistant", "result": res})
        outs = []
        for call in res.tool_calls:
            try:
                content, is_error = execute(call)
            except Exception as e:  # noqa: BLE001 - l'erreur est renvoyée au modèle
                log.exception("Outil %s", call.name)
                content, is_error = f"Erreur de l’outil : {e}", True
            yield {"type": "tool", "id": call.id, "name": call.name, "input": call.input, "content": content,
                   "is_error": is_error}
            outs.append({"id": call.id, "content": content, "is_error": is_error})
        if rnd >= max_rounds - 2 and outs:
            outs[-1]["content"] = f"{outs[-1]['content']}\n\n(Limite d’outils atteinte : réponds maintenant, sans appeler " \
                                  "d’autre outil.)"
        msgs.append({"role": "tool", "results": outs})
        yield {"type": "round"}
    yield {"type": "done", "result": results[-1], "results": results, "text": "\n\n".join(texts)}


def test_provider(row: AIProvider) -> tuple[bool | None, str]:
    ok, msg = P.get(row).test()
    row.last_test_at, row.last_test_ok, row.last_test_message = now(), ok, (msg or "")[:500]
    db.session.commit()
    return ok, msg


def list_models(row: AIProvider) -> list[dict]:
    return P.get(row).list_models()


def usage_rows(start: datetime, end: datetime | None = None, user_id: int | None = None, group: str = "feature"):
    """[(clé, appels, jetons entrée, jetons sortie, coût)] groupés par fonction, modèle, mois ou utilisateur."""
    col = {"feature": AIUsage.feature, "model": AIUsage.model, "provider": AIUsage.provider_name,
           "user": AIUsage.user_id}.get(group, AIUsage.feature)
    q = db.session.query(col, func.count(AIUsage.id), func.coalesce(func.sum(AIUsage.input_tokens), 0),
                         func.coalesce(func.sum(AIUsage.output_tokens), 0), func.coalesce(func.sum(AIUsage.cost), 0.0)) \
        .filter(AIUsage.created_at >= start)
    if end is not None:
        q = q.filter(AIUsage.created_at < end)
    if user_id is not None:
        q = q.filter(AIUsage.user_id == user_id)
    return q.group_by(col).order_by(func.sum(AIUsage.cost).desc(), func.count(AIUsage.id).desc()).all()


def usage_by_month(months: int = 6, user_id: int | None = None) -> list[dict]:
    """Coût et appels des derniers mois (le plus récent en premier)."""
    out = []
    d = date.today().replace(day=1)
    for _ in range(months):
        start = datetime.combine(d, time(0))
        nxt = (d.replace(year=d.year + 1, month=1) if d.month == 12 else d.replace(month=d.month + 1))
        q = db.session.query(func.count(AIUsage.id), func.coalesce(func.sum(AIUsage.cost), 0.0),
                             func.coalesce(func.sum(AIUsage.input_tokens + AIUsage.output_tokens), 0)) \
            .filter(AIUsage.created_at >= start, AIUsage.created_at < datetime.combine(nxt, time(0)))
        if user_id is not None:
            q = q.filter(AIUsage.user_id == user_id)
        n, cost, tokens = q.one()
        out.append({"month": d, "calls": int(n or 0), "cost": float(cost or 0), "tokens": int(tokens or 0)})
        d = (d.replace(year=d.year - 1, month=12) if d.month == 1 else d.replace(month=d.month - 1))
    return out


def as_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
