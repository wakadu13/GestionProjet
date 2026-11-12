"""Modèles de l'assistant IA (SQLAlchemy).

- `AIProvider` : un fournisseur (Claude par l'API Anthropic, ou une API compatible OpenAI : Ollama, LM Studio,
  vLLM, llama.cpp, OpenRouter, Mistral, Groq, Together…). La clé d'API reste en base et n'est jamais renvoyée
  aux pages (seulement masquée) ; elle peut aussi venir d'une variable d'environnement.
- `AISettings` : réglages généraux (plafond mensuel de dépense, consignes ajoutées à l'assistant).
- `AIUsage` / `AIErrorLog` : suivi des jetons, du coût estimé et des erreurs.
- `AIConversation` / `AIMessage` / `AIAction` : conversations du panneau, et actions proposées par l'IA
  (exécutées seulement quand l'utilisateur clique).
- `AIJob` : travaux en arrière-plan (et lots à moitié prix) pour les longues générations.
"""
from __future__ import annotations

import json
import os

from ..extensions import db
from ..models import now

ROLES = [("chat", "Conversation"), ("rapide", "Rapide"), ("vision", "Vision (images)")]
ROLE_LABELS = dict(ROLES)


def _loads(raw, default):
    try:
        v = json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return v if isinstance(v, type(default)) else default


def _dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


class AIProvider(db.Model):
    __tablename__ = "ai_provider"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    kind = db.Column(db.String(30), nullable=False, default="openai_compatible")  # anthropic | openai_compatible
    preset = db.Column(db.String(30), default="custom")
    base_url = db.Column(db.String(500), default="")
    api_key = db.Column(db.Text, default="")              # jamais renvoyée aux pages
    api_key_env = db.Column(db.String(120), default="")   # variable d'environnement de repli
    # Adresse (origine) où la clé de cette variable peut partir : celle où la variable a été indiquée. Vide pour un
    # fournisseur enregistré avant cette règle (son adresse actuelle). Ignorée pour une variable connue
    # (ANTHROPIC_API_KEY, OPENROUTER_API_KEY…), qui ne part que vers son service. Voir env_origin().
    api_key_env_origin = db.Column(db.String(300))
    model = db.Column(db.String(200), default="")         # rôle « chat »
    model_fast = db.Column(db.String(200), default="")    # rôle « rapide » (vide = modèle principal)
    model_vision = db.Column(db.String(200), default="")  # rôle « vision » (vide = modèle principal)
    roles = db.Column(db.String(60), default="chat,rapide,vision")
    enabled = db.Column(db.Boolean, default=True)
    priority = db.Column(db.Integer, default=10)          # plus petit = essayé d'abord
    monthly_cap = db.Column(db.Float, default=0.0)        # dollars par mois (0 = pas de plafond propre)
    tools_mode = db.Column(db.String(10), default="auto")  # auto | on | off (appel d'outils)
    effort = db.Column(db.String(10), default="")         # "" = selon le rôle (low pour « rapide », medium sinon)
    price_input = db.Column(db.Float, default=0.0)        # $ / million de jetons (services payants compatibles OpenAI)
    price_output = db.Column(db.Float, default=0.0)
    last_test_at = db.Column(db.DateTime)
    last_test_ok = db.Column(db.Boolean)
    last_test_message = db.Column(db.String(500), default="")
    created_at = db.Column(db.DateTime, default=now)
    updated_at = db.Column(db.DateTime, default=now)

    @property
    def role_list(self) -> list[str]:
        return [r for r in (self.roles or "").split(",") if r in ROLE_LABELS] or ["chat"]

    def model_for(self, role: str = "chat") -> str:
        if role == "rapide" and self.model_fast:
            return self.model_fast
        if role == "vision" and self.model_vision:
            return self.model_vision
        return self.model or ""

    def env_name(self) -> str:
        if self.api_key_env:
            return self.api_key_env
        return "ANTHROPIC_API_KEY" if self.kind == "anthropic" else ""

    def env_origin(self) -> str:
        """Seule adresse (origine) où peut partir la clé lue dans la variable d'environnement : le service d'une
        variable connue (ANTHROPIC_API_KEY → api.anthropic.com, OPENROUTER_API_KEY → openrouter.ai…), sinon
        l'adresse où la variable a été indiquée. Changer l'adresse n'emporte donc pas la clé vers un autre serveur."""
        from .providers import env_home_origin, origin
        return env_home_origin(self.env_name()) or self.api_key_env_origin or origin(self.base_url, self.kind)

    def env_key_withheld(self) -> str:
        """Hôte auquel la clé de la variable d'environnement est réservée, quand elle existe mais ne part pas vers
        l'adresse actuelle (sans clé enregistrée) ; sinon ""."""
        from urllib.parse import urlsplit

        from .providers import origin
        env = self.env_name()
        if self.api_key or not env or not (os.environ.get(env) or "").strip():
            return ""
        bound = self.env_origin()
        if bound == origin(self.base_url, self.kind):
            return ""
        parts = urlsplit(bound)
        port = "" if parts.port in (None, {"http": 80, "https": 443}.get(parts.scheme)) else f":{parts.port}"
        return f"{parts.hostname}{port}" if parts.hostname else bound

    def key(self) -> str:
        """Clé effective : celle enregistrée, sinon la variable d'environnement (seulement vers son adresse)."""
        if self.api_key:
            return self.api_key.strip()
        env = self.env_name()
        if not env or self.env_key_withheld():
            return ""
        return (os.environ.get(env) or "").strip()

    @property
    def key_source(self) -> str:
        if self.api_key:
            return "base"
        return "env" if self.key() else ""

    @property
    def masked_key(self) -> str:
        """Clé masquée pour l'affichage (« sk-ant-…a1b2 ») ; jamais la clé entière."""
        k = self.key()
        if not k:
            return ""
        if len(k) <= 10:
            return "…" + k[-2:]
        head = k[:7] if k.startswith("sk-") else k[:3]
        return f"{head}…{k[-4:]}"

    @property
    def needs_key(self) -> bool:
        from .providers import PRESET_BY_KEY
        if self.kind == "anthropic":
            return True
        return bool(PRESET_BY_KEY.get(self.preset or "", {}).get("needs_key"))

    @property
    def usable(self) -> bool:
        return bool(self.enabled and self.model and (self.key() or not self.needs_key))

    @property
    def preset_label(self) -> str:
        from .providers import PRESET_BY_KEY
        return PRESET_BY_KEY.get(self.preset or "", {}).get("label") or "Personnalisé"

    @property
    def is_local(self) -> bool:
        from .providers import is_local_url
        return self.kind != "anthropic" and is_local_url(self.base_url or "")


class AISettings(db.Model):
    """Réglages généraux de l'assistant (une seule ligne, id = 1)."""
    __tablename__ = "ai_settings"
    id = db.Column(db.Integer, primary_key=True)
    monthly_cap = db.Column(db.Float, default=5.0)  # dollars par mois, tous fournisseurs (0 = pas de plafond)
    instructions = db.Column(db.Text, default="")   # consignes ajoutées à l'assistant
    updated_at = db.Column(db.DateTime, default=now)

    @staticmethod
    def get() -> "AISettings":
        s = db.session.get(AISettings, 1)
        if s is None:
            s = AISettings(id=1)
            db.session.add(s)
            db.session.commit()
        return s


class AIUsage(db.Model):
    """Un appel à un modèle : jetons et coût estimé (0 pour les modèles locaux)."""
    __tablename__ = "ai_usage"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=now, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"), index=True)
    provider_id = db.Column(db.Integer, index=True)
    provider_name = db.Column(db.String(120), default="")
    model = db.Column(db.String(200), default="")
    feature = db.Column(db.String(60), default="chat")
    input_tokens = db.Column(db.Integer, default=0)
    output_tokens = db.Column(db.Integer, default=0)
    cache_read_tokens = db.Column(db.Integer, default=0)
    cache_write_tokens = db.Column(db.Integer, default=0)
    cost = db.Column(db.Float, default=0.0)
    batch = db.Column(db.Boolean, default=False)

    user = db.relationship("User")


class AIErrorLog(db.Model):
    """Journal lisible des erreurs des fournisseurs."""
    __tablename__ = "ai_error_log"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=now, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    provider_name = db.Column(db.String(120), default="")
    model = db.Column(db.String(200), default="")
    feature = db.Column(db.String(60), default="")
    code = db.Column(db.String(30), default="erreur")
    message = db.Column(db.String(500), default="")
    detail = db.Column(db.Text, default="")

    user = db.relationship("User")


class AIConversation(db.Model):
    __tablename__ = "ai_conversation"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    title = db.Column(db.String(200), default="Nouvelle conversation")
    created_at = db.Column(db.DateTime, default=now)
    updated_at = db.Column(db.DateTime, default=now, index=True)
    last_model = db.Column(db.String(200), default="")

    messages = db.relationship("AIMessage", back_populates="conversation", cascade="all, delete-orphan",
                               order_by="AIMessage.id", passive_deletes=True)


class AIMessage(db.Model):
    __tablename__ = "ai_message"
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("ai_conversation.id", ondelete="CASCADE"),
                                nullable=False, index=True)
    role = db.Column(db.String(12), nullable=False)       # user | assistant
    content = db.Column(db.Text, default="")              # texte saisi / réponse finale (Markdown)
    prompt = db.Column(db.Text, default="")               # utilisateur : texte envoyé au modèle (contexte compris)
    command = db.Column(db.String(30), default="")        # commande « / » utilisée
    context_json = db.Column(db.Text, default="")         # pastilles : page courante, sélection, éléments « @ »
    tools_json = db.Column(db.Text, default="")           # outils appelés pendant la réponse
    feedback = db.Column(db.Integer, default=0)           # 1 = 👍, -1 = 👎
    feedback_note = db.Column(db.String(500), default="")
    status = db.Column(db.String(12), default="ok")       # ok | streaming | error | refusal
    error = db.Column(db.String(500), default="")
    model = db.Column(db.String(200), default="")
    provider_name = db.Column(db.String(120), default="")
    input_tokens = db.Column(db.Integer, default=0)
    output_tokens = db.Column(db.Integer, default=0)
    cost = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=now)

    conversation = db.relationship("AIConversation", back_populates="messages")
    actions = db.relationship("AIAction", back_populates="message", cascade="all, delete-orphan",
                              order_by="AIAction.id", passive_deletes=True)

    @property
    def context(self) -> dict:
        return _loads(self.context_json, {})

    @context.setter
    def context(self, v) -> None:
        self.context_json = _dumps(v or {})

    @property
    def tools(self) -> list:
        return _loads(self.tools_json, [])

    @tools.setter
    def tools(self, v) -> None:
        self.tools_json = _dumps(v or [])


class AIAction(db.Model):
    """Action proposée par l'IA (« Créer la tâche », « Ajouter à la page »…), exécutée au clic de l'utilisateur."""
    __tablename__ = "ai_action"
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey("ai_message.id", ondelete="CASCADE"), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = db.Column(db.String(40), nullable=False)
    label = db.Column(db.String(120), default="")         # texte du bouton
    summary = db.Column(db.String(500), default="")       # ce que fera l'action
    payload_json = db.Column(db.Text, default="{}")
    status = db.Column(db.String(12), default="proposed")  # proposed | done | failed | dismissed
    result_json = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=now)
    done_at = db.Column(db.DateTime)

    message = db.relationship("AIMessage", back_populates="actions")

    @property
    def payload(self) -> dict:
        return _loads(self.payload_json, {})

    @payload.setter
    def payload(self, v) -> None:
        self.payload_json = _dumps(v or {})

    @property
    def result(self) -> dict:
        return _loads(self.result_json, {})

    @result.setter
    def result(self, v) -> None:
        self.result_json = _dumps(v or {})


class AIJob(db.Model):
    """Travail d'IA en arrière-plan (transformation de cours, génération de QCM…), éventuellement en lot."""
    __tablename__ = "ai_job"
    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(60), nullable=False, index=True)
    status = db.Column(db.String(12), default="pending", index=True)  # pending|running|batch|draft|done|error
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"), index=True)
    feature = db.Column(db.String(60), default="")
    title = db.Column(db.String(300), default="")
    payload_json = db.Column(db.Text, default="{}")
    options_json = db.Column(db.Text, default="{}")
    calls_json = db.Column(db.Text, default="[]")         # résultats des appels déjà faits (rejoués après un lot)
    result_json = db.Column(db.Text, default="")
    message = db.Column(db.String(500), default="")
    batch_id = db.Column(db.String(120), default="")
    provider_id = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=now, index=True)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)

    user = db.relationship("User")

    @property
    def payload(self) -> dict:
        return _loads(self.payload_json, {})

    @payload.setter
    def payload(self, v) -> None:
        self.payload_json = _dumps(v or {})

    @property
    def options(self) -> dict:
        return _loads(self.options_json, {})

    @options.setter
    def options(self, v) -> None:
        self.options_json = _dumps(v or {})

    @property
    def calls(self) -> list:
        return _loads(self.calls_json, [])

    @calls.setter
    def calls(self, v) -> None:
        self.calls_json = _dumps(v or [])

    @property
    def result(self):
        try:
            return json.loads(self.result_json) if self.result_json else None
        except ValueError:
            return None

    @result.setter
    def result(self, v) -> None:
        self.result_json = _dumps(v) if v is not None else ""

    def totaux(self) -> dict:
        """Résumé des appels faits par le travail (rejoués depuis `calls`) : modèle, jetons, coût estimé."""
        calls = [c for c in self.calls if isinstance(c, dict) and "usage" in c]
        u = [c.get("usage") or {} for c in calls]
        return {"model": calls[-1].get("model", "") if calls else "",
                "input": sum(x.get("input_tokens", 0) + x.get("cache_read_tokens", 0) + x.get("cache_write_tokens", 0)
                             for x in u),
                "output": sum(x.get("output_tokens", 0) for x in u),
                "cost": sum(float(c.get("cost") or 0) for c in calls) if calls else None}


class AITutorExchange(db.Model):
    """Question posée au tuteur sur une page du wiki, ou à tout le wiki (« Demander au wiki », page = "*"),
    et la réponse : l'historique du compte nourrit la question suivante (EchangeTuteur de Revision)."""
    __tablename__ = "ai_echange_tuteur"
    PAGE_WIKI = "*"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    page = db.Column(db.String(500), nullable=False)        # clé de la note, ou "*" pour tout le wiki
    question = db.Column(db.Text, nullable=False)
    reponse = db.Column(db.Text, nullable=False)
    sources_json = db.Column(db.Text, default="[]")         # « Demander au wiki » : extraits [{titre, url, type}]
    modele = db.Column(db.String(200), default="")
    jetons_entree = db.Column(db.Integer, default=0)
    jetons_sortie = db.Column(db.Integer, default=0)
    cree_le = db.Column(db.DateTime, default=now, index=True)

    @property
    def sources(self) -> list:
        return _loads(self.sources_json, [])

    @sources.setter
    def sources(self, v) -> None:
        self.sources_json = _dumps(v or [])


class AICopie(db.Model):
    """Copie manuscrite photographiée et sa correction (CorrectionCopie de Revision). Les photos, allégées, sont
    gardées hors du coffre : DATA_DIR/copies/<user_id>/<id>-<n>.jpg, visibles de leur seul auteur."""
    __tablename__ = "ai_copie"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    enonce = db.Column(db.Text, default="")
    page = db.Column(db.String(500), default="")             # page du cours donnée comme référence
    photos = db.Column(db.Integer, default=0)
    correction = db.Column(db.Text, default="")
    modele = db.Column(db.String(200), default="")
    jetons_entree = db.Column(db.Integer, default=0)
    jetons_sortie = db.Column(db.Integer, default=0)
    cree_le = db.Column(db.DateTime, default=now, index=True)
