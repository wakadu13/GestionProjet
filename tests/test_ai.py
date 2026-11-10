"""Tests de l'assistant IA, sans réseau : Claude simulé (faux client du SDK anthropic) et serveur local compatible
OpenAI (http.server dans un fil, réponses en flux SSE comprises).

Lancer : uv run python -m unittest tests.test_ai -v
"""
import io
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import unittest
from datetime import date, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

from anthropic.types.beta import BetaMessage
from flask import g, has_app_context
from flask.testing import FlaskClient

from hangar import create_app
from hangar.ai import _CONTEXT_RESOLVERS, client_config, disponible, register_context
from hangar.ai import context as C
from hangar.ai import core, jobs
from hangar.ai import providers as P
from hangar.ai.models import AIAction, AIConversation, AIErrorLog, AIMessage, AIProvider, AISettings, AIUsage
from hangar.ai.providers import AIError, ToolCall
from hangar.ai.rendering import render
from hangar.ai.tools import ActionError, ToolBox, execute_action
from hangar.extensions import db
from hangar.models import AgendaEvent, AgendaTask, Comment, Cycle, Issue, Page, User
from hangar.services import create_issue, create_project, ensure_member

SECRET = "sk-ant-api03-TRESSECRET0123456789-abcd"
logging.getLogger("hangar.ai").setLevel(logging.ERROR)  # erreurs simulées : déjà vérifiées dans le journal


# ---------------------------------------------------------------------------
# Faux Claude (même interface que le SDK : beta.messages.stream, models, batches, files)
# ---------------------------------------------------------------------------

def claude_msg(content, stop="end_turn", model="claude-opus-5-5", usage=None):
    blocks = [{"type": "text", "text": c} if isinstance(c, str) else c for c in content]
    return BetaMessage.model_validate({
        "id": "msg_test", "type": "message", "role": "assistant", "model": model, "content": blocks,
        "stop_reason": stop, "stop_sequence": None,
        "usage": usage or {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 0,
                           "cache_creation_input_tokens": 0}})


def tool_use(name, inp, tid="toolu_1"):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        for b in self.msg.content:
            yield SimpleNamespace(type="content_block_start", content_block=b)
            if b.type == "text":
                for i in range(0, len(b.text), 7):
                    yield SimpleNamespace(type="text", text=b.text[i:i + 7])

    def get_final_message(self):
        return self.msg


class FakeAnthropic:
    def __init__(self):
        self.replies, self.requests, self.batches = [], [], {}
        self.batch_reply = None
        self.uploads, self.deleted = [], []
        self.beta = SimpleNamespace(messages=SimpleNamespace(
            stream=self._stream,
            batches=SimpleNamespace(create=self._bcreate, retrieve=self._bretrieve, results=self._bresults)))
        self.models = SimpleNamespace(
            list=lambda **kw: [SimpleNamespace(id="claude-opus-5-5", display_name="Claude Opus 5.5"),
                               SimpleNamespace(id="claude-haiku-5-5", display_name="Claude Haiku 5.5")],
            retrieve=lambda m, **kw: SimpleNamespace(id=m))
        self.files = SimpleNamespace(upload=self._upload, delete=self.deleted.append)

    def _stream(self, **kw):
        self.requests.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return FakeStream(r)

    def _upload(self, file):
        self.uploads.append(file)
        return SimpleNamespace(id=f"file_{len(self.uploads)}")

    def _bcreate(self, requests, **kw):
        bid = f"msgbatch_{len(self.batches) + 1}"
        self.batches[bid] = {"requests": list(requests), "status": "in_progress", "kw": kw}
        return SimpleNamespace(id=bid)

    def _bretrieve(self, bid):
        return SimpleNamespace(id=bid, processing_status=self.batches[bid]["status"])

    def _bresults(self, bid):
        return [SimpleNamespace(custom_id=r["custom_id"], result=SimpleNamespace(type="succeeded", message=self.batch_reply))
                for r in self.batches[bid]["requests"]]


# ---------------------------------------------------------------------------
# Faux serveur compatible OpenAI (Ollama, LM Studio…)
# ---------------------------------------------------------------------------

def sse_text(text, usage=(12, 5)):
    chunks = [{"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}]
    chunks += [{"choices": [{"index": 0, "delta": {"content": text[i:i + 5]}}]} for i in range(0, len(text), 5)]
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    chunks.append({"choices": [], "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}})
    return chunks


def sse_tool(name, args, cid="call_1"):
    raw = json.dumps(args)
    return [
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": cid, "type": "function",
                                                              "function": {"name": name, "arguments": ""}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": raw[:6]}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": raw[6:]}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 30, "completion_tokens": 9}},
    ]


class FakeOpenAI:
    def __init__(self):
        self.script, self.requests = [], []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, data):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/tags":
                    return self._json(200, {"models": [{"name": "llama3.2:latest", "details": {
                        "parameter_size": "3.2B", "quantization_level": "Q4_K_M"}}, {"name": "qwen2.5:latest"}]})
                if self.path == "/v1/models":
                    return self._json(200, {"object": "list", "data": [{"id": "llama3.2:latest"}, {"id": "qwen2.5:latest"}]})
                return self._json(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.requests.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
                item = outer.script.pop(0) if outer.script else {"sse": sse_text("OK")}
                if "error" in item:
                    return self._json(item.get("status", 400), {"error": {"message": item["error"]}})
                if "json" in item:
                    return self._json(200, item["json"])
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for ch in item["sse"]:
                    self.wfile.write(f"data: {json.dumps(ch)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def tiny_pdf(text="Bonjour PDF"):
    content = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def tiny_png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


def parse_sse(text):
    out = []
    for raw in text.split("\n\n"):
        ev, data = None, ""
        for line in raw.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if ev:
            out.append((ev, json.loads(data)))
    return out


class Client(FlaskClient):
    """Client de test qui oublie l'utilisateur mis en cache par Flask-Login (contexte d'application partagé)."""

    def open(self, *args, **kwargs):
        if has_app_context():
            g.pop("_login_user", None)
        return super().open(*args, **kwargs)


# ---------------------------------------------------------------------------

class AITestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-ai-")
        self.app = create_app(self.tmp, testing=True)
        self.fake = FakeAnthropic()
        self.app.config["AI_ANTHROPIC_FACTORY"] = lambda row: self.fake
        self.app.test_client_class = Client
        self.ctx = self.app.test_request_context("/")
        self.ctx.push()
        self.admin = self._user("moi", "admin", "Moi")
        self.member = self._user("alice", "member", "Alice")
        self.guest = self._user("invite", "guest", "Invité")
        db.session.commit()
        self.servers = []
        P._NO_TOOLS.clear()

    def tearDown(self):
        for s in self.servers:
            s.close()
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _user(self, name, role, first):
        u = User(username=name, email=f"{name}@hangar.local", role=role, first_name=first)
        u.set_password("secret1")
        db.session.add(u)
        return u

    def claude(self, **kw):
        p = AIProvider(name="Claude", kind="anthropic", preset="anthropic", api_key=SECRET, model="claude-opus-5-5",
                       model_fast="claude-haiku-5-5", enabled=True, priority=10, **kw)
        db.session.add(p)
        db.session.commit()
        return p

    def server(self):
        s = FakeOpenAI()
        self.servers.append(s)
        return s

    def ollama(self, server, **kw):
        kw.setdefault("priority", 20)
        p = AIProvider(name="Ollama", kind="openai_compatible", preset="ollama", base_url=server.url, model="llama3.2",
                       enabled=True, **kw)
        db.session.add(p)
        db.session.commit()
        return p

    def login(self, username="moi"):
        c = self.app.test_client()
        page = c.get("/login").get_data(as_text=True)
        token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        r = c.post("/login", data={"login": username, "password": "secret1", "_csrf": token})
        self.assertEqual(r.status_code, 302)
        c.token = token
        return c

    def chat(self, c, message, **kw):
        r = c.post("/ia/chat", json={"message": message, **kw}, headers={"X-CSRF-Token": c.token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("text/event-stream", r.headers["Content-Type"])
        return parse_sse(r.get_data(as_text=True))

    def project(self):
        p = create_project("Projet EAE", "EAE", self.admin, member_ids=[self.member.id])
        db.session.commit()
        return p


# ---------------------------------------------------------------------------

class ProviderConfigTests(AITestCase):
    def test_disponible_and_client_config(self):
        self.assertFalse(disponible())
        self.assertFalse(client_config()["enabled"])
        p = self.claude()
        self.assertTrue(disponible())
        cfg = client_config()
        self.assertTrue(cfg["enabled"])
        self.assertEqual((cfg["model"], cfg["modelLabel"], cfg["kind"]), ("claude-opus-5-5", "Claude Opus 5.5", "anthropic"))
        self.assertIn("{id}", cfg["urls"]["action"])
        self.assertNotIn(SECRET, json.dumps(cfg))
        self.assertTrue(cfg["commands"])
        p.enabled = False
        db.session.commit()
        self.assertFalse(disponible())
        # Claude sans clé (ni variable d'environnement) : inutilisable
        p.enabled, p.api_key = True, ""
        db.session.commit()
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(disponible())
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-env-0000wxyz"}):
            self.assertTrue(disponible())
            self.assertEqual(p.key_source, "env")
            self.assertEqual(p.masked_key, "sk-ant-…wxyz")

    def test_settings_form_masks_key_and_checks_rights(self):
        c = self.login()
        r = c.post("/ia/fournisseurs/nouveau", data={
            "_csrf": c.token, "preset": "anthropic", "name": "Claude", "api_key": SECRET, "model": "claude-opus-5-5",
            "model_fast": "claude-haiku-5-5", "roles_present": "1", "role_chat": "1", "role_rapide": "1",
            "enabled": ["0", "1"]})
        self.assertEqual(r.status_code, 302)
        p = AIProvider.query.one()
        self.assertEqual((p.kind, p.api_key, p.role_list, p.enabled), ("anthropic", SECRET, ["chat", "rapide"], True))
        page = c.get("/ia/reglages/").get_data(as_text=True)
        self.assertNotIn(SECRET, page)
        self.assertIn("sk-ant-…abcd", page)
        self.assertNotIn(SECRET, c.get("/").get_data(as_text=True))
        # Clé vide : on garde l'ancienne ; « effacer la clé » l'efface
        c.post(f"/ia/fournisseurs/{p.id}", data={"_csrf": c.token, "preset": "anthropic", "name": "Claude 2",
                                                "api_key": "", "model": "claude-opus-5-5", "enabled": ["0", "1"]})
        db.session.refresh(p)
        self.assertEqual((p.name, p.api_key), ("Claude 2", SECRET))
        c.post(f"/ia/fournisseurs/{p.id}", data={"_csrf": c.token, "preset": "anthropic", "name": "Claude 2",
                                                "clear_key": "1", "model": "claude-opus-5-5", "enabled": ["0"]})
        db.session.refresh(p)
        self.assertEqual((p.api_key, p.enabled), ("", False))
        # Compatible OpenAI : adresse obligatoire
        r = c.post("/ia/fournisseurs/nouveau", data={"_csrf": c.token, "preset": "custom", "name": "X", "base_url": "ftp://x",
                                                     "model": "m"})
        self.assertEqual(AIProvider.query.count(), 1)
        # Un membre voit son usage mais ne gère pas les fournisseurs
        m = self.login("alice")
        self.assertEqual(m.get("/ia/reglages/").status_code, 200)
        self.assertEqual(m.post("/ia/fournisseurs/nouveau", data={"_csrf": m.token, "preset": "ollama", "name": "x",
                                                                  "base_url": "http://localhost:11434/v1", "model": "m"}).status_code, 403)
        self.assertEqual(m.post(f"/ia/fournisseurs/{p.id}/supprimer", data={"_csrf": m.token}).status_code, 403)
        self.assertEqual(m.post("/ia/reglages/", data={"_csrf": m.token, "monthly_cap": "100"}).status_code, 403)
        self.assertEqual(m.post(f"/ia/fournisseurs/{p.id}/tester", data={"_csrf": m.token}).status_code, 403)

    def test_presets(self):
        for key in ("anthropic", "ollama", "lmstudio", "vllm", "llamacpp", "openrouter", "mistral", "groq", "together", "custom"):
            self.assertIn(key, P.PRESET_BY_KEY)
        self.assertEqual(P.PRESET_BY_KEY["ollama"]["base_url"], "http://localhost:11434/v1")
        self.assertEqual(P.PRESET_BY_KEY["anthropic"]["model"], "claude-opus-5-5")

    def test_test_connection_and_model_lists(self):
        srv = self.server()
        c = self.login()
        r = c.post("/ia/fournisseurs/essai", data={"_csrf": c.token, "preset": "ollama", "name": "Ollama",
                                                   "base_url": srv.url, "model": "llama3.2", "what": "models"},
                   headers={"X-Requested-With": "fetch"})
        self.assertEqual([m["id"] for m in r.json["models"]], ["llama3.2:latest", "qwen2.5:latest"])
        r = c.post("/ia/fournisseurs/essai", data={"_csrf": c.token, "preset": "ollama", "name": "Ollama",
                                                   "base_url": srv.url, "model": "llama3.2", "what": "test"})
        self.assertTrue(r.json["ok"], r.json)
        r = c.post("/ia/fournisseurs/essai", data={"_csrf": c.token, "preset": "ollama", "name": "Ollama",
                                                   "base_url": srv.url, "model": "mistral", "what": "test"})
        self.assertFalse(r.json["ok"])
        self.assertIn("ollama pull mistral", r.json["message"])
        self.assertEqual(AIProvider.query.count(), 0)  # rien d'enregistré
        p = self.claude()
        r = c.post(f"/ia/fournisseurs/{p.id}/tester", data={"_csrf": c.token}, headers={"X-Requested-With": "fetch"})
        self.assertTrue(r.json["ok"])
        db.session.refresh(p)
        self.assertTrue(p.last_test_ok)
        r = c.get(f"/ia/fournisseurs/{p.id}/modeles")
        self.assertIn("claude-opus-5-5", [m["id"] for m in r.json["models"]])
        # Serveur injoignable
        dead = AIProvider(name="Mort", kind="openai_compatible", preset="ollama", base_url="http://127.0.0.1:9/v1",
                          model="llama3.2")
        ok, msg = P.get(dead).test()
        self.assertFalse(ok)
        self.assertIn("Ollama est-il lancé", msg)


class GenerateTests(AITestCase):
    def test_claude_simple_request(self):
        self.claude()
        self.fake.replies.append(claude_msg(["Bonjour ! Je suis là."]))
        r = core.generate("Salut", system="Consigne stable", user=self.admin, feature="test")
        self.assertEqual(r.text, "Bonjour ! Je suis là.")
        req = self.fake.requests[0]
        self.assertEqual(req["model"], "claude-opus-5-5")
        self.assertNotIn("temperature", req)
        self.assertNotIn("thinking", req)
        self.assertEqual(req["output_config"], {"effort": "medium"})
        self.assertEqual(req["system"], [{"type": "text", "text": "Consigne stable", "cache_control": {"type": "ephemeral"}}])
        self.assertEqual((req["betas"], req["fallbacks"]), (["server-side-fallback-2026-07-01"], "default"))
        self.assertEqual(req["messages"], [{"role": "user", "content": "Salut"}])
        self.assertNotIn("cache_control", req)  # appel unique : seule la consigne système est mise en cache
        u = AIUsage.query.one()
        self.assertEqual((u.user_id, u.feature, u.input_tokens, u.output_tokens), (self.admin.id, "test", 1000, 200))
        self.assertAlmostEqual(u.cost, (1000 * 4 + 200 * 20) / 1e6)
        self.assertAlmostEqual(r.cost, u.cost)

    def test_claude_fast_role_json_and_cache_pricing(self):
        self.claude()
        schema = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["juste", "faux"]},
                                                   "retour": {"type": "string"}}, "required": ["verdict", "retour"]}
        self.fake.replies.append(claude_msg(['{"verdict": "juste", "retour": "Bien."}'], model="claude-haiku-5-5",
                                            usage={"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 1000,
                                                   "cache_creation_input_tokens": 400}))
        r = core.generate("Corrige", role="rapide", json_schema=schema, feature="qrc")
        self.assertEqual(r.data, {"verdict": "juste", "retour": "Bien."})
        req = self.fake.requests[0]
        self.assertEqual(req["model"], "claude-haiku-5-5")
        self.assertEqual(req["output_config"]["effort"], "low")
        self.assertEqual(req["output_config"]["format"]["schema"]["additionalProperties"], False)
        self.assertNotIn("fallbacks", req)  # pas de repli serveur pour Haiku 5.5
        self.assertAlmostEqual(r.cost, (100 * 0.10 + 20 * 0.50 + 1000 * 0.01 + 400 * 0.10 * 1.25) / 1e6)
        # JSON tronqué : erreur lisible
        self.fake.replies.append(claude_msg(['{"verdict": "ju'], stop="max_tokens"))
        with self.assertRaises(AIError) as e:
            core.generate("x", json_schema=schema)
        self.assertEqual(e.exception.code, "trop_long")

    def test_claude_refusal_fallback_and_errors(self):
        import anthropic
        import httpx2

        self.claude()
        self.fake.replies.append(claude_msg(["Je ne"], stop="refusal"))
        with self.assertRaises(AIError) as e:
            core.generate("Question")
        self.assertEqual(e.exception.code, "refus")
        self.fake.replies.append(claude_msg([], stop="refusal"))
        self.assertTrue(core.generate("Question", allow_refusal=True).refusal)
        # Repli en cours de réponse : texte des deux côtés, coût par itération, frontière gardée
        usage = {"input_tokens": 10, "output_tokens": 5, "iterations": [
            {"type": "message", "model": "claude-opus-5-5", "input_tokens": 10, "output_tokens": 2,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 10, "output_tokens": 5,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}]}
        self.fake.replies.append(claude_msg(
            ["Début ", {"type": "thinking", "thinking": "", "signature": "sig"},
             {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-4-8"},
              "trigger": {"type": "refusal"}}, "et suite."], model="claude-opus-4-8", usage=usage))
        r = core.generate("Question")
        self.assertEqual(r.text, "Début et suite.")
        self.assertEqual(r.fallback_to, "claude-opus-4-8")
        self.assertEqual([b["type"] for b in r.raw], ["text", "fallback", "text"])  # réflexion d'avant la frontière omise
        self.assertAlmostEqual(r.cost, (10 * 4 + 2 * 20) / 1e6 + (10 * 5 + 5 * 25) / 1e6)
        # Erreurs de l'API -> messages lisibles + journal
        req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        self.fake.replies.append(anthropic.AuthenticationError("invalid x-api-key", response=httpx2.Response(401, request=req), body=None))
        with self.assertRaises(AIError) as e:
            core.generate("x")
        self.assertEqual(e.exception.code, "cle")
        self.fake.replies.append(anthropic.APIConnectionError(request=req))
        with self.assertRaises(AIError) as e:
            core.generate("x")
        self.assertEqual(e.exception.code, "reseau")
        self.assertEqual([x.code for x in AIErrorLog.query.order_by(AIErrorLog.id)], ["cle", "reseau"])
        self.assertNotIn(SECRET, " ".join(x.detail for x in AIErrorLog.query))

    def test_claude_images_and_pdf(self):
        self.claude()
        self.fake.replies.append(claude_msg(["Copie lue."]))
        core.generate("Corrige ma copie", role="vision", images=[tiny_png()], documents=[("cours.pdf", tiny_pdf())])
        content = self.fake.requests[0]["messages"][0]["content"]
        self.assertEqual([b["type"] for b in content], ["document", "image", "text"])
        self.assertEqual(content[0]["source"]["media_type"], "application/pdf")
        self.assertEqual(content[0]["title"], "cours.pdf")
        self.assertEqual(content[1]["source"]["media_type"], "image/png")
        # Un très gros PDF passe par l'API Files (puis le fichier est supprimé)
        self.fake.replies.append(claude_msg(["Lu."]))
        with mock.patch.object(P, "PDF_INLINE_MAX", 100):
            core.generate("Résume", documents=[tiny_pdf()])
        self.assertEqual(self.fake.requests[1]["messages"][0]["content"][0]["source"], {"type": "file", "file_id": "file_1"})
        self.assertEqual(self.fake.deleted, ["file_1"])

    def test_openai_stream_and_usage(self):
        srv = self.server()
        self.ollama(srv)
        srv.script.append({"sse": sse_text("Bonjour Hangar, voici la réponse.")})
        events = list(core.generate("Salut", system="Consigne", stream=True, feature="test"))
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "text"), "Bonjour Hangar, voici la réponse.")
        res = events[-1]["result"]
        self.assertEqual((res.usage.input_tokens, res.usage.output_tokens, res.cost), (12, 5, 0))
        body = srv.requests[0]["body"]
        self.assertEqual((body["model"], body["stream"], body["stream_options"]), ("llama3.2", True, {"include_usage": True}))
        self.assertEqual(body["messages"], [{"role": "system", "content": "Consigne"}, {"role": "user", "content": "Salut"}])
        self.assertIsNone(srv.requests[0]["auth"])
        self.assertEqual(AIUsage.query.one().provider_name, "Ollama")
        # Raisonnement <think> d'un modèle local retiré, même coupé entre deux morceaux
        srv.script.append({"sse": sse_text("<think>je réfléchis longuement</think>Réponse nette.")})
        self.assertEqual(core.generate("x").text, "Réponse nette.")

    def test_openai_json_downgrade_and_corrective_retry(self):
        srv = self.server()
        self.ollama(srv)
        schema = {"type": "object", "properties": {"titre": {"type": "string"}, "n": {"type": "integer"}},
                  "required": ["titre", "n"]}
        srv.script += [{"error": "response_format json_schema is not supported", "status": 400},
                       {"sse": sse_text('Voici :\n```json\n{"titre": "T", "n": 2}\n```')}]
        r = core.generate("Fais un JSON", json_schema=schema)
        self.assertEqual(r.data, {"titre": "T", "n": 2})
        self.assertEqual(srv.requests[0]["body"]["response_format"]["type"], "json_schema")
        self.assertEqual(srv.requests[1]["body"]["response_format"], {"type": "json_object"})
        self.assertIn("schéma JSON", srv.requests[1]["body"]["messages"][0]["content"])
        # Réponse non conforme : une relance corrective
        srv.script += [{"sse": sse_text('{"titre": "T"}')}, {"sse": sse_text('{"titre": "T", "n": 3}')}]
        self.assertEqual(core.generate("Encore", json_schema=schema).data["n"], 3)
        self.assertIn("manquant", srv.requests[-1]["body"]["messages"][-1]["content"])

    def test_openai_pdf_image_and_errors(self):
        srv = self.server()
        p = self.ollama(srv, api_key="cle-locale")
        srv.script.append({"sse": sse_text("ok")})
        core.generate("Résume", documents=[("cours.pdf", tiny_pdf("Bonjour PDF"))])
        user = srv.requests[0]["body"]["messages"][-1]["content"]
        self.assertIsInstance(user, str)
        self.assertIn("Bonjour PDF", user)
        self.assertEqual(srv.requests[0]["auth"], "Bearer cle-locale")
        srv.script.append({"sse": sse_text("ok")})
        core.generate("Décris", images=[tiny_png()])
        parts = srv.requests[1]["body"]["messages"][-1]["content"]
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        srv.script.append({"error": 'model "llama3.2" not found, try pulling it first', "status": 404})
        with self.assertRaises(AIError) as e:
            core.generate("x")
        self.assertEqual(e.exception.code, "modele")
        self.assertIn("ollama pull llama3.2", e.exception.message)
        p.base_url = "http://127.0.0.1:9/v1"
        db.session.commit()
        with self.assertRaises(AIError) as e:
            core.generate("x")
        self.assertEqual(e.exception.code, "reseau")

    def test_rendering(self):
        html = str(render("Formule $a_1 * b_2$ et **gras**\n\n<script>alert(1)</script>\n\n```python\nx = 1\n```"))
        self.assertIn("$a_1 * b_2$", html)
        self.assertIn("<strong>gras</strong>", html)
        self.assertNotIn("<script>", html)
        self.assertIn("<pre><code", html)


class ToolAndChatTests(AITestCase):
    def setUp(self):
        super().setUp()
        self.p = self.project()
        self.issue = create_issue(self.p, {"title": "Rédiger le rapport", "description": "Partie 2 à finir",
                                           "target_date": (date.today() - timedelta(days=2)).isoformat(),
                                           "assignee_ids": [self.admin.id]}, self.admin)
        self.task = AgendaTask(user_id=self.admin.id, title="Rendre le TP de physique", due_date=date.today() - timedelta(days=1))
        db.session.add(self.task)
        db.session.commit()

    def test_claude_chat_with_tools_and_action(self):
        self.claude()
        c = self.login()
        self.fake.replies += [
            claude_msg(["Je regarde tes tâches.", tool_use("lister_taches", {"filtre": "en_retard"})], stop="tool_use"),
            claude_msg([tool_use("proposer_tache", {"titre": "Réviser la BDD", "date": "2026-11-20", "heure_debut": "14:00",
                                                    "priorite": "high"}, "toolu_2")], stop="tool_use"),
            claude_msg(["Je te propose de créer la tâche « Réviser la BDD »."]),
        ]
        events = self.chat(c, "Qu'est-ce qui est en retard ?", path="/", title="Agenda")
        kinds = [e for e, _ in events]
        self.assertEqual(kinds[0], "start")
        self.assertEqual(kinds[-1], "done")
        self.assertIn("action", kinds)
        self.assertIn(("tool", "start"), [(e, d.get("status")) for e, d in events if e == "tool"])
        # Le contexte de la page (tableau de bord) et la date du jour partent avec la question
        first = self.fake.requests[0]
        prompt = first["messages"][-1]["content"]
        self.assertIn("Tableau de bord de l’agenda", prompt)
        self.assertIn(date.today().isoformat(), prompt)
        self.assertIn("Rendre le TP de physique", prompt)
        names = [t["name"] for t in first["tools"]]
        self.assertIn("chercher_tickets", names)
        self.assertIn("proposer_ajout_page", names)
        self.assertTrue(all(t.get("eager_input_streaming") for t in first["tools"]))
        self.assertEqual(first["cache_control"], {"type": "ephemeral"})  # la boucle d'outils relit le même début
        self.assertTrue(first["system"][0]["text"].startswith("Tu es l’assistant IA de Hangar"))
        # Le résultat de l'outil est renvoyé au modèle, le tour d'avant est rejoué tel quel
        second = self.fake.requests[1]["messages"]
        self.assertEqual(second[-2]["content"][1]["type"], "tool_use")
        self.assertEqual(second[-1]["content"][0]["tool_use_id"], "toolu_1")
        self.assertIn("Rendre le TP de physique", second[-1]["content"][0]["content"])
        done = events[-1][1]
        self.assertEqual(done["text"], "Je regarde tes tâches.\n\nJe te propose de créer la tâche « Réviser la BDD ».")
        a = AIAction.query.one()
        self.assertEqual((a.kind, a.label, a.status), ("tache", "Créer la tâche", "proposed"))
        self.assertEqual(done["actions"][0]["id"], a.id)
        # Rien n'est créé avant le clic
        self.assertEqual(AgendaTask.query.count(), 1)
        r = c.post(f"/ia/actions/{a.id}/executer", json={}, headers={"X-CSRF-Token": c.token})
        self.assertTrue(r.json["ok"], r.json)
        t = AgendaTask.query.filter_by(title="Réviser la BDD").one()
        self.assertEqual((t.due_date, t.start_time, t.priority), (date(2026, 11, 20), time(14, 0), "high"))
        self.assertEqual(c.post(f"/ia/actions/{a.id}/executer", json={}, headers={"X-CSRF-Token": c.token}).status_code, 409)
        # Conversation enregistrée
        conv = AIConversation.query.one()
        msgs = conv.messages
        self.assertEqual([m.role for m in msgs], ["user", "assistant"])
        self.assertEqual(msgs[1].status, "ok")
        self.assertEqual([x["name"] for x in msgs[1].tools], ["lister_taches", "proposer_tache"])
        self.assertEqual(msgs[1].input_tokens, 3000)
        self.assertEqual(AIUsage.query.count(), 3)

    def test_openai_chat_with_tool_calls(self):
        srv = self.server()
        self.ollama(srv)
        c = self.login()
        srv.script += [{"sse": sse_tool("chercher_tickets", {"texte": "rapport"})},
                       {"sse": sse_text("Le ticket EAE-1 est en retard.")}]
        events = self.chat(c, "Où en est le rapport ?", path=f"/projects/{self.p.id}/issues/1")
        done = events[-1][1]
        self.assertEqual(events[-1][0], "done", events)
        self.assertIn('href="/i/EAE-1"', done["html"])
        second = srv.requests[1]["body"]["messages"]
        self.assertEqual(second[-2]["tool_calls"][0]["function"]["name"], "chercher_tickets")
        self.assertEqual(second[-1]["role"], "tool")
        self.assertIn("EAE-1 · Rédiger le rapport", second[-1]["content"])
        self.assertIn("Ticket EAE-1 : Rédiger le rapport", srv.requests[0]["body"]["messages"][-1]["content"])

    def test_openai_model_without_tools(self):
        srv = self.server()
        self.ollama(srv)
        c = self.login()
        srv.script += [{"error": "registry.ollama.ai/library/llama3.2 does not support tools", "status": 400},
                       {"sse": sse_text("Réponse sans outils.")}]
        events = self.chat(c, "Bonjour")
        self.assertEqual(events[-1][1]["text"], "Réponse sans outils.")
        self.assertIn("info", [e for e, _ in events])
        self.assertIn("tools", srv.requests[0]["body"])
        self.assertNotIn("tools", srv.requests[1]["body"])
        self.assertFalse(client_config()["tools"])  # mémorisé : plus d'outils proposés à ce modèle
        srv.script.append({"sse": sse_text("Encore.")})
        self.chat(c, "Et ensuite ?")
        self.assertNotIn("tools", srv.requests[2]["body"])
        self.assertIn("pas d’outils", srv.requests[2]["body"]["messages"][0]["content"])

    def test_actions_page_cycle_deadline_done_and_rights(self):
        today = date.today()
        page = Page(project=self.p, title="Compte rendu", content="# Compte rendu\n\nIntro.", owner=self.admin,
                    updated_by=self.admin)
        cyc = Cycle(project=self.p, name="Sprint 1", start_date=today - timedelta(days=3), end_date=today + timedelta(days=4))
        db.session.add_all([page, cyc])
        db.session.commit()
        conv = AIConversation(user_id=self.admin.id)
        db.session.add(conv)
        db.session.flush()
        msg = AIMessage(conversation_id=conv.id, role="assistant", content="")
        db.session.add(msg)
        db.session.commit()
        box = ToolBox(self.admin, msg, page={"url": f"/projects/{self.p.id}/pages/{page.id}"})
        out, err = box.execute(ToolCall("t1", "proposer_ajout_page", {"contenu": "## Décisions\n- Valider le plan", "position": "debut"}))
        self.assertFalse(err, out)
        box.execute(ToolCall("t2", "proposer_ajout_cycle", {"cles": ["EAE-1"]}))
        box.execute(ToolCall("t3", "proposer_echeance", {"cible": "EAE-1", "date": "2026-12-01"}))
        box.execute(ToolCall("t4", "proposer_terminer", {"cible": f"#{self.task.id}"}))
        out, err = box.execute(ToolCall("t5", "proposer_ticket", {"titre": "Nouveau", "projet": "ZZZ"}))
        self.assertTrue(err)
        out, err = box.execute(ToolCall("t6", "lire_ticket", {"cle": 12}))  # mauvais type d'argument
        self.assertTrue(err)
        self.assertIn("Arguments invalides", out)
        acts = {a.kind: a for a in AIAction.query}
        self.assertEqual(set(acts), {"ajout_page", "ajout_cycle", "echeance", "terminer"})
        for a in acts.values():
            execute_action(a, self.admin)
        db.session.refresh(page)
        self.assertTrue(page.content.startswith("# Compte rendu\n\n## Décisions\n- Valider le plan\n\nIntro."), page.content)
        self.assertEqual(len(page.versions), 1)
        db.session.refresh(self.issue)
        self.assertEqual((self.issue.cycle_id, self.issue.target_date), (cyc.id, date(2026, 12, 1)))
        db.session.refresh(self.task)
        self.assertIsNotNone(self.task.done_at)
        # Droits : un invité ne crée pas de ticket ; l'action d'un autre est introuvable
        ensure_member(self.p, self.guest)
        db.session.commit()
        ga = AIAction(user_id=self.guest.id, kind="ticket", label="Créer le ticket", summary="x")
        ga.payload = {"project_id": self.p.id, "titre": "Piratage"}
        db.session.add(ga)
        db.session.commit()
        with self.assertRaises(ActionError) as e:
            execute_action(ga, self.guest)
        self.assertEqual(e.exception.status, 403)
        with self.assertRaises(ActionError) as e:
            execute_action(ga, self.admin)
        self.assertEqual(e.exception.status, 404)
        g = self.login("invite")
        self.assertEqual(g.post(f"/ia/actions/{acts['ajout_page'].id}/executer", json={},
                                headers={"X-CSRF-Token": g.token}).status_code, 404)
        self.assertEqual(Issue.query.count(), 1)
        # Un invité ne peut pas proposer d'ajout à une page qu'il ne peut pas modifier
        out, err = ToolBox(self.guest, msg, page={"url": f"/projects/{self.p.id}/pages/{page.id}"}).execute(
            ToolCall("t7", "proposer_ajout_page", {"contenu": "x"}))
        self.assertTrue(err)


class ContextTests(AITestCase):
    def test_agenda_dashboard_and_calendar(self):
        today = date.today()
        at = datetime.combine(today, time(10, 0))
        db.session.add_all([
            AgendaEvent(user_id=self.admin.id, uid="e1", title="Mécanique du vol", kind="CM", location="SALLE A",
                        start=at, end=at + timedelta(hours=2)),
            AgendaEvent(user_id=self.admin.id, uid="e2", title="Contrôle de physique", kind="CO_ECRIT",
                        start=at + timedelta(days=3), end=at + timedelta(days=3, hours=2)),
            AgendaTask(user_id=self.admin.id, title="Rendre le TP", due_date=today - timedelta(days=1)),
            AgendaTask(user_id=self.member.id, title="Tâche d’Alice", due_date=today),
        ])
        db.session.commit()
        ctx = C.resolve("/", self.admin)
        self.assertEqual(ctx["type"], "agenda")
        for s in ("Mécanique du vol", "SALLE A", "Contrôle de physique", "[CONTRÔLE]", "Rendre le TP", "En retard"):
            self.assertIn(s, ctx["text"])
        self.assertNotIn("Tâche d’Alice", ctx["text"])
        cal = C.resolve("/agenda/calendar", self.admin, {"date": today.isoformat(), "view": "week"})
        self.assertIn("Mécanique du vol", cal["text"])
        tasks = C.resolve("/agenda/tasks", self.admin)
        self.assertIn("Rendre le TP", tasks["text"])

    def test_issue_context_and_rights(self):
        p = self.project()
        i = create_issue(p, {"title": "Rédiger le rapport", "description": "Partie 2 à finir", "priority": "high",
                             "assignee_ids": [self.member.id]}, self.admin)
        db.session.add(Comment(issue=i, author=self.member, body="J’ai fini l’intro."))
        db.session.commit()
        ctx = C.resolve(f"/projects/{p.id}/issues/{i.sequence_id}", self.admin)
        self.assertEqual((ctx["type"], ctx["title"]), ("ticket", "EAE-1 · Rédiger le rapport"))
        for s in ("Partie 2 à finir", "État : ", "Haute", "Alice", "J’ai fini l’intro."):
            self.assertIn(s, ctx["text"])
        self.assertIn("EAE-1", C.resolve(f"/projects/{p.id}/issues", self.admin)["text"])
        self.assertIn("EAE-1", C.resolve("/i/EAE-1", self.admin)["text"])
        # Projet privé : invisible pour un non-membre
        p.network = "private"
        outsider = self._user("bob", "member", "Bob")
        db.session.commit()
        self.assertIsNone(C.resolve(f"/projects/{p.id}/issues/1", outsider))
        self.assertIsNotNone(C.resolve(f"/projects/{p.id}/issues/1", self.member))

    def test_registered_resolver_and_mentions(self):
        def wiki(path, args, user):
            if path.startswith("/wiki/"):
                return {"type": "wiki", "title": "Loi d’Ohm", "text": "U = R I"}
            return None
        register_context(wiki)
        try:
            ctx = C.resolve("/wiki/Cours/Ohm", self.admin)
            self.assertEqual((ctx["type"], ctx["text"], ctx["icon"]), ("wiki", "U = R I", "book"))
        finally:
            _CONTEXT_RESOLVERS.remove(wiki)
        self.assertIsNone(C.resolve("/wiki/Cours/Ohm", self.admin))
        p = self.project()
        create_issue(p, {"title": "Préparer la soutenance"}, self.admin)
        db.session.add(AgendaTask(user_id=self.admin.id, title="Réviser la BDD"))
        db.session.commit()
        c = self.login()
        items = c.get("/ia/mentions?q=EAE-1").json["items"]
        self.assertEqual(items[0]["type"], "ticket")
        self.assertIn("Réviser la BDD", [x["title"] for x in c.get("/ia/mentions?q=BDD").json["items"]])
        r = c.get(f"/ia/contexte?path=/projects/{p.id}/issues/1").json
        self.assertTrue(r["known"])
        self.assertEqual(r["context"]["title"], "EAE-1 · Préparer la soutenance")
        self.assertNotIn("text", r["context"])
        self.assertFalse(c.get("/ia/contexte?path=/analytics&title=Analyses · Hangar").json["known"])


class ConversationTests(AITestCase):
    def test_history_feedback_and_ownership(self):
        p = self.project()
        create_issue(p, {"title": "Rédiger le rapport"}, self.admin)
        db.session.commit()
        self.claude()
        c = self.login()
        self.fake.replies += [claude_msg(["Première réponse."]), claude_msg(["Deuxième réponse."])]
        path = f"/projects/{p.id}/issues/1"
        e1 = self.chat(c, "Résume ce ticket", path=path, command="resumer")
        cid = e1[0][1]["conversation"]["id"]
        e2 = self.chat(c, "Et ensuite ?", path=path, conversation_id=cid,
                       mentions=[{"url": "/agenda/tasks", "title": "Tâches", "type": "taches"}], selection="Partie 2")
        self.assertEqual(e2[-1][1]["conversation"]["id"], cid)
        msgs = self.fake.requests[1]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertIn("Ticket EAE-1", msgs[0]["content"])
        self.assertEqual(msgs[1]["content"], "Première réponse.")
        self.assertIn("inchangée depuis le message précédent", msgs[2]["content"])
        self.assertIn("<selection>\nPartie 2", msgs[2]["content"])
        self.assertIn("Élément ajouté avec @ : Tâches", msgs[2]["content"])
        conv = db.session.get(AIConversation, cid)
        self.assertEqual(conv.title, "Résume ce ticket")
        self.assertEqual(conv.messages[0].command, "resumer")
        lst = c.get("/ia/conversations.json").json["conversations"]
        self.assertEqual([x["id"] for x in lst], [cid])
        data = c.get(f"/ia/c/{cid}.json").json["conversation"]
        self.assertEqual(len(data["messages"]), 4)
        self.assertIn("<p>Deuxième réponse.</p>", data["messages"][3]["html"])
        mid = data["messages"][3]["id"]
        self.assertEqual(c.post(f"/ia/messages/{mid}/avis", json={"value": -1}, headers={"X-CSRF-Token": c.token}).json["feedback"], -1)
        self.assertEqual(db.session.get(AIMessage, mid).feedback, -1)
        self.assertEqual(c.get(f"/ia/c/{cid}").status_code, 200)
        self.assertEqual(c.post(f"/ia/c/{cid}/renommer", json={"title": "Rapport"}, headers={"X-CSRF-Token": c.token}).json["title"], "Rapport")
        # Les conversations sont privées
        a = self.login("alice")
        self.assertEqual(a.get(f"/ia/c/{cid}.json").status_code, 404)
        self.assertEqual(a.post(f"/ia/messages/{mid}/avis", json={"value": 1}, headers={"X-CSRF-Token": a.token}).status_code, 404)
        r = a.post("/ia/chat", json={"message": "x", "conversation_id": cid}, headers={"X-CSRF-Token": a.token})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(c.post(f"/ia/c/{cid}/supprimer", headers={"X-CSRF-Token": c.token, "X-Requested-With": "fetch"}).json["ok"], True)
        self.assertEqual(AIMessage.query.count(), 0)

    def test_stream_outlives_the_request_session(self):
        """Comme en production : le flux tourne après la fin de la requête (session de la vue fermée)."""
        p = self.project()
        create_issue(p, {"title": "Rédiger le rapport"}, self.admin)
        db.session.add(AgendaTask(user_id=self.admin.id, title="Rendre le TP", due_date=date.today()))
        db.session.commit()
        self.claude()
        c = self.login()
        self.fake.replies += [claude_msg([tool_use("proposer_tache", {"titre": "Finir le TP", "date": "2026-11-21"})],
                                         stop="tool_use"),
                              claude_msg(["C’est proposé."])]
        path = f"/projects/{p.id}/issues/1"
        self.ctx.pop()
        try:
            r = c.post("/ia/chat", json={"message": "Aide-moi", "path": path},
                       headers={"X-CSRF-Token": c.token})
            events = parse_sse(r.get_data(as_text=True))
        finally:
            self.ctx.push()
        self.assertEqual(events[-1][0], "done", events[-1])
        self.assertEqual(events[-1][1]["text"], "C’est proposé.")
        self.assertIn("action", [e for e, _ in events])
        self.assertEqual(AIMessage.query.filter_by(role="assistant").one().status, "ok")

    def test_errors_are_shown_and_saved(self):
        c = self.login()
        r = c.post("/ia/chat", json={"message": "x"}, headers={"X-CSRF-Token": c.token})
        self.assertEqual((r.status_code, r.json["code"]), (503, "indisponible"))
        self.assertEqual(r.json["settings"], "/ia/reglages/")
        self.claude()
        import anthropic
        import httpx2
        req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        self.fake.replies.append(anthropic.AuthenticationError("bad key", response=httpx2.Response(401, request=req), body=None))
        events = self.chat(c, "Bonjour")
        self.assertEqual(events[-1][0], "error")
        self.assertEqual(events[-1][1]["settings"], "/ia/reglages/")
        self.assertEqual(AIMessage.query.filter_by(role="assistant").one().status, "error")
        self.fake.replies.append(claude_msg(["Je ne peux"], stop="refusal"))
        events = self.chat(c, "Question limite")
        self.assertEqual(events[-1][1]["status"], "refusal")
        self.assertIn("refusé", events[-1][1]["text"])
        self.assertEqual(c.post("/ia/chat", json={"message": "  "}, headers={"X-CSRF-Token": c.token}).status_code, 400)
        self.assertEqual(c.post("/ia/chat", json={"message": "x"}).status_code, 400)  # sans jeton CSRF


class CapAndJobsTests(AITestCase):
    def test_global_cap(self):
        p = self.claude()
        s = AISettings.get()
        s.monthly_cap = 0.01
        db.session.add(AIUsage(provider_id=p.id, provider_name="Claude", model="claude-opus-5-5", cost=0.02, user_id=self.admin.id))
        db.session.commit()
        with self.assertRaises(AIError) as e:
            core.generate("x")
        self.assertEqual(e.exception.code, "plafond")
        self.assertTrue(client_config()["capReached"])
        c = self.login()
        r = c.post("/ia/chat", json={"message": "x"}, headers={"X-CSRF-Token": c.token})
        self.assertEqual(r.status_code, 429)
        s.monthly_cap = 0
        db.session.commit()
        self.fake.replies.append(claude_msg(["ok"]))
        self.assertEqual(core.generate("x").text, "ok")
        # L'usage du mois dernier ne compte pas
        self.assertAlmostEqual(core.month_cost(start=core.month_start(date.today() + timedelta(days=40))), 0)

    def test_provider_cap_switches_provider(self):
        p = self.claude(monthly_cap=0.01)
        srv = self.server()
        self.ollama(srv)
        db.session.add(AIUsage(provider_id=p.id, provider_name="Claude", cost=0.5))
        db.session.commit()
        self.assertEqual(core.pick("chat").name, "Ollama")
        srv.script.append({"sse": sse_text("local")})
        self.assertEqual(core.generate("x").provider, "Ollama")

    def test_jobs_direct_and_batch(self):
        self.claude()
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}

        @jobs.register_job("essai")
        def essai(job, ai):
            r = ai(f"Travail {job.payload['n']}", json_schema=schema, max_tokens=64000)
            return {"ok": r.data["ok"], "model": r.model}

        self.fake.replies.append(claude_msg(['{"ok": true}']))
        job = jobs.enqueue("essai", {"n": 1}, user=self.admin, title="Essai")
        self.assertEqual(jobs.run_pending(), 1)
        db.session.refresh(job)
        self.assertEqual((job.status, job.result), ("done", {"ok": True, "model": "claude-opus-5-5"}))
        self.assertEqual(self.fake.requests[0]["max_tokens"], 64000)
        # En lot : la requête part dans un lot (sans le repli, refusé par l'API des lots), résultat à moitié prix
        job2 = jobs.enqueue("essai", {"n": 2}, user=self.admin, batch=True)
        jobs.run_pending()
        db.session.refresh(job2)
        self.assertEqual(job2.status, "batch")
        batch = self.fake.batches[job2.batch_id]
        self.assertEqual(batch["requests"][0]["custom_id"], f"job{job2.id}-0")
        self.assertNotIn("fallbacks", batch["requests"][0]["params"])
        self.assertEqual(jobs.check_batches(), 0)  # lot pas terminé
        batch["status"] = "ended"
        self.fake.batch_reply = claude_msg(['{"ok": false}'])
        self.assertEqual(jobs.check_batches(), 1)
        jobs.run_pending()
        db.session.refresh(job2)
        self.assertEqual((job2.status, job2.result["ok"]), ("done", False))
        u = AIUsage.query.filter_by(batch=True).one()
        self.assertAlmostEqual(u.cost, (1000 * 4 + 200 * 20) / 1e6 / 2)
        # Erreur lisible
        self.fake.replies.append(claude_msg(["pas du json"]))
        job3 = jobs.enqueue("essai", {"n": 3})
        jobs.run_pending()
        db.session.refresh(job3)
        self.assertEqual(job3.status, "error")
        self.assertIn("JSON", job3.message)


class PageTests(AITestCase):
    def test_pages_answer(self):
        c = self.login()
        home = c.get("/").get_data(as_text=True)
        self.assertIn('id="ai-panel"', home)
        self.assertIn("Configurer un fournisseur", home)
        self.claude()
        conv = AIConversation(user_id=self.admin.id, title="Essai")
        db.session.add(conv)
        db.session.commit()
        for url in ("/", "/ia/", "/ia/?new=1", f"/ia/c/{conv.id}", "/ia/reglages/", "/home", "/agenda/tasks",
                    "/qcm/expliquer/", "/demander/", "/copie/", "/transformations/"):
            r = c.get(url)
            self.assertEqual(r.status_code, 200, url)
        page = c.get("/").get_data(as_text=True)
        self.assertIn("data-ai-form", page)
        self.assertIn("Claude Opus 5.5", page)
        for url in ("/qcm/expliquer/", "/qcm/corriger-qrc/", "/claude/tuteur/", "/qcm/generer/", "/copie/"):
            r = c.post(url, json={}, headers={"X-CSRF-Token": c.token})
            self.assertEqual(r.status_code, 503, url)
            self.assertFalse(r.json["ok"])
        m = self.login("alice")
        self.assertEqual(m.get("/ia/reglages/").status_code, 200)
        self.assertEqual(m.get(f"/ia/c/{conv.id}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
