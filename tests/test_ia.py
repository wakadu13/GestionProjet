"""Tests du groupe « ia » (vague v2a), sans réseau : faux client Anthropic et faux serveurs compatibles OpenAI.

- piste 24 : plafonds de réponse relevés, réponses coupées signalées, explication coupée jamais gardée ;
- piste 45 : plafond mensuel (lots en cours réservés, réponses interrompues comptées, tarifs inconnus) ;
- piste 46 : annulation des travaux, lots dont la relève échoue, reprise au lancement ;
- piste 47 : pages longues (extraits utiles au lieu du seul début, « page coupée » signalée) ;
- piste 48 : Ollama (mémoire de contexte, aide pour Windows, appels d'outils sous le même index) ;
- piste 61 : clé d'API liée à l'adresse du fournisseur ;
- après relecture : extraits qui remplissent le budget, réserves des lots limitées au dépôt des lots, clé d'une
  variable d'environnement liée elle aussi à l'adresse, avertissements d'Ollama des demandes courtes.

Lancer : uv run python -m unittest tests.test_ia -v
"""
import io
import json
import os
import re
import shutil
import socket
import tempfile
import threading
from argparse import Namespace
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

import anthropic
import httpx2
from sqlalchemy import text
from test_ai import SECRET, FakeStream, claude_msg, sse_text
from test_ai_revision import RevisionTestCase

from hangar.ai import core, demandes, jobs
from hangar.ai import providers as P
from hangar.ai.models import AICopie, AIJob, AIMessage, AIProvider, AISettings, AITutorExchange, AIUsage
from hangar.extensions import db
from hangar.models import Notification, now

ZORGLUB = "Le théorème de Zorglub affirme que la portance double quand l’équipage chante."


def page_longue(chapitres=60):
    """Cours d'environ 120 000 caractères ; le dernier chapitre parle du théorème de Zorglub."""
    blocs = ["# Cours long\n\nIntroduction du cours de mécanique du vol."]
    for i in range(1, chapitres + 1):
        blocs.append(f"## Chapitre {i}\n\n" + f"Paragraphe de remplissage numéro {i} sur la mécanique du vol. " * 30)
    blocs.append(f"## Dernier chapitre\n\n{ZORGLUB}")
    return "\n\n".join(blocs)


def message_start(input_tokens, model="claude-opus-5-5"):
    return SimpleNamespace(type="message_start", message=SimpleNamespace(model=model, usage=SimpleNamespace(
        input_tokens=input_tokens, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0)))


class StreamAvecUsage(FakeStream):
    """Flux qui commence par message_start (jetons d'entrée connus dès le début, comme l'API), et peut planter
    après quelques morceaux (entrée d'outil illisible : ValueError)."""

    def __init__(self, msg, input_tokens=5000, plante_apres=None):
        super().__init__(msg)
        self.input_tokens, self.plante_apres = input_tokens, plante_apres

    def __iter__(self):
        yield message_start(self.input_tokens, self.msg.model)
        for n, ev in enumerate(super().__iter__()):
            if self.plante_apres is not None and n >= self.plante_apres:
                raise ValueError("entrée d'outil illisible")
            yield ev


def port_libre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def espionner_get(srv, routes=None):
    """Le faux serveur note aussi ses requêtes GET (en-tête Authorization compris) ; `routes` : réponses en plus."""
    srv.gets = []
    base = srv.server.RequestHandlerClass

    class Espion(base):
        def do_GET(self):
            srv.gets.append({"path": self.path, "auth": self.headers.get("Authorization")})
            if routes and self.path in routes:
                return self._json(200, routes[self.path])
            return super().do_GET()

    srv.server.RequestHandlerClass = Espion
    return srv


class Redirige:
    """Serveur qui redirige tout vers une autre adresse (autre hôte)."""

    def __init__(self, cible):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _redirige(self):
                self.send_response(302)
                self.send_header("Location", cible + self.path)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = _redirige

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class IATestCase(RevisionTestCase):
    def setUp(self):
        super().setUp()
        P._OLLAMA_CONTEXT.clear()

    def flux(self, *flux):
        """Les prochains appels au faux Claude renvoient ces flux (au lieu de FakeStream)."""
        file = list(flux)

        def stream(**kw):
            self.fake.requests.append(kw)
            return file.pop(0)
        self.fake.beta.messages.stream = stream

    def flashes(self, c):
        with c.session_transaction() as s:
            return " ".join(m for _, m in s.get("_flashes", []))


# ---------------------------------------------------------------------------
# Piste 24 : plafonds de réponse, réponses coupées
# ---------------------------------------------------------------------------

class PlafondsReponseTests(IATestCase):
    def test_explication_coupee_jamais_gardee(self):
        from hangar.qcm.models import Explication

        self.claude()
        c = self.login("alice")
        self.fake.replies.append(claude_msg(["La bonne réponse est B parce que la"], stop="max_tokens",
                                            model="claude-haiku-5-5"))
        r = self.post_json(c, "/qcm/expliquer/", {"set_id": "QCM/Physique/ohm", "question_id": "q1"})
        self.assertEqual(r.status_code, 200, r.json)
        self.assertIn("Réponse coupée", r.json["html"])
        self.assertEqual(Explication.query.count(), 0)  # une explication coupée n'est pas gardée pour tous
        req = self.fake.requests[0]
        self.assertEqual(req["max_tokens"], demandes.MAX_TOKENS_EXPLICATION)
        self.assertGreaterEqual(req["max_tokens"], 4000)
        self.assertEqual(req["thinking"], {"type": "disabled"})  # Haiku 5.5 en effort bas : pas de réflexion
        # La question suivante refait l'appel, et garde la réponse complète
        self.fake.replies.append(claude_msg(["Explication complète."], model="claude-haiku-5-5"))
        r = self.post_json(c, "/qcm/expliquer/", {"set_id": "QCM/Physique/ohm", "question_id": "q1"})
        self.assertFalse(r.json["gardee"])
        self.assertEqual(len(self.fake.requests), 2)
        self.assertEqual(Explication.query.one().texte, "Explication complète.")
        self.assertFalse(r.json["refaire"])  # seul un administrateur peut refaire une explication gardée

    def test_admin_refait_une_explication_gardee(self):
        from hangar.qcm.models import Explication

        self.claude()
        db.session.add(Explication(set_id="QCM/Physique/ohm", question_id="q1", texte="Vieille explication bancale"))
        db.session.commit()
        m = self.login("alice")
        r = self.post_json(m, "/qcm/expliquer/", {"set_id": "QCM/Physique/ohm", "question_id": "q1", "refaire": True})
        self.assertTrue(r.json["gardee"])  # un membre ne peut pas effacer l'explication de tous
        self.assertEqual(self.fake.requests, [])
        c = self.login()
        r = self.post_json(c, "/qcm/expliquer/", {"set_id": "QCM/Physique/ohm", "question_id": "q1"})
        self.assertTrue(r.json["refaire"])
        self.fake.replies.append(claude_msg(["Nouvelle explication."], model="claude-haiku-5-5"))
        r = self.post_json(c, "/qcm/expliquer/", {"set_id": "QCM/Physique/ohm", "question_id": "q1", "refaire": True})
        self.assertEqual((r.status_code, r.json["gardee"]), (200, False))
        self.assertEqual(Explication.query.one().texte, "Nouvelle explication.")

    def test_reflexion_coupee_seulement_pour_haiku_en_effort_modere(self):
        p = self.claude()
        self.fake.replies += [claude_msg(["a"], model="claude-haiku-5-5"), claude_msg(["b"], model="claude-haiku-5-5"),
                              claude_msg(["c"]), claude_msg(["d"])]
        core.generate("x", role="rapide", effort="low")
        core.generate("x", role="rapide", effort="xhigh")   # refusé par l'API au-delà de « high »
        core.generate("x", role="chat")                      # Opus 5.5 : la réflexion ne se coupe jamais
        p.model_fast = "claude-opus-5-5"
        db.session.commit()
        core.generate("x", role="rapide", effort="low")
        thinking = [req.get("thinking") for req in self.fake.requests]
        self.assertEqual(thinking, [{"type": "disabled"}, None, None, None])

    def test_qrc_plafond_releve_et_nouvel_essai(self):
        self.claude()
        c = self.login()
        self.fake.replies += [claude_msg(['{"verdict": "ju'], stop="max_tokens", model="claude-haiku-5-5"),
                              claude_msg(['{"verdict": "juste", "retour": "Bien vu."}'], model="claude-haiku-5-5")]
        r = self.post_json(c, "/qcm/corriger-qrc/", {"set_id": "QCM/Physique/ohm", "question_id": "q2",
                                                     "reponse": "un voltmètre branché en dérivation"})
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual(r.json["verdict"], "juste")
        plafonds = [req["max_tokens"] for req in self.fake.requests]
        self.assertEqual(plafonds, [demandes.MAX_TOKENS_QRC, 2 * demandes.MAX_TOKENS_QRC])
        self.assertGreaterEqual(demandes.MAX_TOKENS_QRC, 2000)

    def test_tuteur_wiki_et_copie_signalent_la_coupure(self):
        self.claude()
        c = self.login()
        coupe = "*(Réponse coupée"
        self.fake.replies.append(claude_msg(["La résistance se mesure en"], stop="max_tokens", model="claude-haiku-5-5"))
        r = self.post_json(c, "/claude/tuteur/", {"page": "Cours/Ohm", "question": "Que vaut R ?"})
        self.assertIn("Réponse coupée", r.json["html"])
        self.assertIn(coupe, AITutorExchange.query.one().reponse)
        self.assertGreaterEqual(self.fake.requests[-1]["max_tokens"], 4000)
        self.fake.replies.append(claude_msg(["Le wiki dit que [1]"], stop="max_tokens", model="claude-haiku-5-5"))
        r = self.post_json(c, "/demander/", {"question": "En quelle unité s'exprime la résistance ?"})
        self.assertIn("Réponse coupée", r.json["html"])
        self.assertGreaterEqual(self.fake.requests[-1]["max_tokens"], 4000)
        self.fake.replies.append(claude_msg(["### Ce que tu as écrit\n1. $U = RI$"], stop="max_tokens"))
        from test_ai import tiny_png
        r = c.post("/copie/", data={"_csrf": c.token, "photos": [(io.BytesIO(tiny_png()), "p.png")]},
                   content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn(coupe, AICopie.query.one().correction)
        self.assertEqual(self.fake.requests[-1]["max_tokens"], demandes.MAX_TOKENS_COPIE)
        self.assertGreaterEqual(demandes.MAX_TOKENS_COPIE, 16000)


# ---------------------------------------------------------------------------
# Piste 45 : plafond mensuel
# ---------------------------------------------------------------------------

class PlafondMensuelTests(IATestCase):
    def setUp(self):
        super().setUp()

        @jobs.register_job("essai_lot")
        def essai_lot(job, ai):
            r = ai(f"Travail {job.payload.get('n')}", max_tokens=64000)
            return {"texte": r.text}

    def test_lots_en_cours_comptent_dans_le_plafond(self):
        self.claude()
        s = AISettings.get()
        s.monthly_cap = 0.3
        db.session.commit()
        lots = [jobs.enqueue("essai_lot", {"n": n}, user=self.member, batch=True) for n in range(5)]
        jobs.run_pending()
        statuts = [db.session.get(AIJob, j.id).status for j in lots]
        # Chaque lot réserve son coût prévu (16 000 jetons de sortie d'Opus 5.5 à moitié prix ≈ 0,16 $, pour une
        # entrée minuscule) : au-delà de 0,30 $ de réserves, les suivants sont refusés au lieu de partir tous.
        self.assertEqual(statuts, ["batch", "batch", "error", "error", "error"])
        message = db.session.get(AIJob, lots[2].id).message
        self.assertIn("des lots en cours réservent 0.32 $", message)
        self.assertIn("sans lot", message)
        self.assertEqual(len(self.fake.batches), 2)
        self.assertTrue(core.cap_reached(reserves=True))
        self.assertGreater(core.reserved_cost(), 0.3)
        # Un nouveau lot est refusé dès le dépôt…
        self.ecrire("Cours/Elec.pdf", __import__("test_ai").tiny_pdf("Electricite"))
        c = self.login()
        r = c.post("/transformations/", data={"_csrf": c.token, "pdf": "Cours/Elec.pdf", "lot": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("des lots en cours réservent", self.flashes(c))
        self.assertEqual(AIJob.query.filter_by(kind="page").count(), 0)
        # … mais pas le reste : la dépense réelle (0 $) est sous le plafond
        self.assertFalse(core.cap_reached())
        self.fake.replies.append(claude_msg(["Bonjour !"]))
        self.chat(c, "Bonjour")
        c.post("/transformations/", data={"_csrf": c.token, "pdf": "Cours/Elec.pdf"})
        self.assertEqual(AIJob.query.filter_by(kind="page").count(), 1)
        page = c.get("/ia/reglages/").get_data(as_text=True)
        self.assertIn("réservés pour 2 lots en cours", page)
        # Au retour des lots, la réserve fait place au coût réel
        for b in self.fake.batches.values():
            b["status"] = "ended"
        self.fake.batch_reply = claude_msg(["Fait."])
        self.assertEqual(jobs.check_batches(), 2)
        self.assertEqual(core.reserved_cost(), 0)
        self.assertFalse(core.cap_reached(reserves=True))

    def test_reponse_interrompue_comptee(self):
        self.claude()
        self.flux(StreamAvecUsage(claude_msg(["Une longue réponse qui sera interrompue par le bouton Arrêter."]),
                                  input_tokens=5000))
        gen = core.generate("Question", stream=True, user=self.admin, feature="chat")
        textes = [next(gen) for _ in range(3)]
        self.assertTrue(all(ev["type"] == "text" for ev in textes))
        gen.close()  # bouton « Arrêter » ou coupure du réseau
        u = AIUsage.query.one()
        self.assertEqual((u.input_tokens, u.feature, u.user_id), (5000, "chat", self.admin.id))
        self.assertGreater(u.output_tokens, 0)
        self.assertGreater(u.cost, 5000 * 4 / 1e6)

    def test_tentative_abandonnee_comptee(self):
        self.claude()
        self.flux(StreamAvecUsage(claude_msg(["Premier essai"]), input_tokens=3000, plante_apres=2),
                  StreamAvecUsage(claude_msg(["Second essai réussi."]), input_tokens=3000))
        r = core.generate("Question", feature="chat")
        self.assertEqual(r.text, "Second essai réussi.")
        self.assertEqual(len(self.fake.requests), 2)
        self.assertEqual([u.input_tokens for u in AIUsage.query.order_by(AIUsage.id)], [3000, 1000])

    def test_panneau_arrete_compte_l_usage(self):
        from hangar.ai.chat import Turn
        from hangar.ai.models import AIConversation

        self.claude()
        self.flux(StreamAvecUsage(claude_msg(["Réponse interrompue au milieu de la phrase."]), input_tokens=4000))
        conv = AIConversation(user_id=self.admin.id)
        db.session.add(conv)
        db.session.commit()
        turn = Turn(self.admin, conv, "Bonjour", page=None, selection="", mentions=[])
        gen = turn.stream()
        events = [next(gen) for _ in range(3)]
        self.assertIn("event: delta", events[-1])
        gen.close()
        self.assertEqual(AIUsage.query.one().input_tokens, 4000)
        self.assertIn("interrompue", db.session.get(AIMessage, turn._ids[3]).content)

    def test_modele_sans_tarif_et_fournisseur_payant_sans_prix(self):
        self.assertEqual(P.claude_price("claude-opus-4-5-20251101"), (5.0, 25.0, 0.50))  # le plus cher des Opus
        self.assertEqual(P.claude_price("claude-sonnet-4-5-20250929"), (3.0, 15.0, 0.30))
        self.assertEqual(P.claude_price("claude-haiku-6"), (1.0, 5.0, 0.10))
        self.assertEqual(P.claude_price("claude-inconnu-9"), (10.0, 50.0, 1.0))  # le plus cher de la table
        self.assertEqual(P.claude_price("claude-opus-5-5"), (4.0, 20.0, 0.20))
        self.assertFalse(P.claude_price_known("claude-opus-4-5-20251101"))
        self.assertTrue(P.claude_price_known("claude-opus-5-5"))
        p = self.claude()
        p.model = "claude-opus-4-5-20251101"
        db.session.commit()
        self.fake.replies.append(claude_msg(["ok"], model="claude-opus-4-5-20251101"))
        r = core.generate("x")
        self.assertAlmostEqual(r.cost, (1000 * 5 + 200 * 25) / 1e6)
        self.assertAlmostEqual(AIUsage.query.one().cost, r.cost)
        db.session.add(AIProvider(name="OpenRouter", kind="openai_compatible", preset="openrouter",
                                  base_url="https://openrouter.ai/api/v1", api_key="sk-or-x", model="llama"))
        db.session.add(AIProvider(name="Ollama", kind="openai_compatible", preset="ollama",
                                  base_url="http://localhost:11434/v1", model="llama3.2"))
        db.session.commit()
        page = self.login().get("/ia/reglages/").get_data(as_text=True)
        self.assertIn("Tarif inconnu pour « claude-opus-4-5-20251101 »", page)
        self.assertEqual(page.count("Tarif inconnu"), 2)  # Claude ancien modèle + OpenRouter sans prix ; pas Ollama
        self.assertIn("plafond mensuel ne le limite pas", page)


# ---------------------------------------------------------------------------
# Piste 46 : annulation, lots bloqués, reprise
# ---------------------------------------------------------------------------

class TravauxTests(IATestCase):
    def setUp(self):
        super().setUp()

        @jobs.register_job("essai_lot")
        def essai_lot(job, ai):
            r = ai("Travail", max_tokens=64000)
            return {"texte": r.text}

    def lot(self):
        job = jobs.enqueue("essai_lot", {}, user=self.admin, title="Lot d'essai", batch=True)
        jobs.run_pending()
        db.session.refresh(job)
        self.assertEqual(job.status, "batch")
        return job

    def test_annuler_un_travail_en_attente_et_un_lot(self):
        self.claude()
        c = self.login()
        lot = self.lot()
        attente = jobs.enqueue("essai_lot", {}, user=self.admin, title="En attente")
        annules = []
        self.fake.beta.messages.batches.cancel = annules.append
        page = c.get("/transformations/").get_data(as_text=True)
        self.assertIn(f'name="annuler" value="{attente.id}"', page)
        self.assertIn(f'name="annuler" value="{lot.id}"', page)
        c.post("/transformations/", data={"_csrf": c.token, "annuler": attente.id})
        c.post("/transformations/", data={"_csrf": c.token, "annuler": lot.id})
        db.session.refresh(attente)
        db.session.refresh(lot)
        self.assertEqual((attente.status, lot.status), ("error", "error"))
        self.assertIn("Annulé", attente.message)
        self.assertEqual(len(annules), 1)  # le lot est aussi annulé chez Anthropic
        self.assertTrue(annules[0].startswith("msgbatch_"))
        self.assertEqual(core.reserved_cost(), 0)
        # Relancer un travail annulé repart de zéro
        c.post("/transformations/", data={"_csrf": c.token, "relancer": attente.id})
        self.assertEqual(db.session.get(AIJob, attente.id).status, "pending")

    def test_annuler_un_travail_en_cours(self):
        self.claude()

        @jobs.register_job("deux_appels")
        def deux_appels(job, ai):
            ai("premier")
            # Clic sur « Annuler » pendant le travail (une autre requête écrit en base)
            db.session.execute(text("UPDATE ai_job SET status='error', message='Annulé à ta demande.' WHERE id=:i"),
                               {"i": job.id})
            db.session.commit()
            ai("second")
            return {"fini": True}

        self.fake.replies += [claude_msg(["un"]), claude_msg(["deux"])]
        job = jobs.enqueue("deux_appels", {}, user=self.admin, title="Long")
        jobs.run_pending()
        db.session.refresh(job)
        self.assertEqual((job.status, job.message, job.result), ("error", "Annulé à ta demande.", None))
        self.assertEqual(len(self.fake.requests), 1)  # le second appel n'est pas parti
        self.assertEqual(Notification.query.filter_by(kind="ai").count(), 0)

    def test_lot_introuvable_ou_resultats_perdus_passe_en_echec(self):
        self.claude()
        req = httpx2.Request("GET", "https://api.anthropic.com/v1/messages/batches/x")

        def introuvable(bid):
            raise anthropic.NotFoundError("not found", response=httpx2.Response(404, request=req), body=None)

        job = self.lot()
        self.fake.beta.messages.batches.retrieve = introuvable
        self.assertEqual(jobs.check_batches(), 1)
        db.session.refresh(job)
        self.assertEqual(job.status, "error")
        self.assertIn("introuvable", job.message)
        self.assertNotIn("Modèle", job.message)
        self.assertFalse(__import__("hangar.ai.travaux", fromlist=["en_cours"]).en_cours("essai_lot"))
        # Lot terminé mais résultats plus disponibles : exception du SDK hors APIError
        self.fake.beta.messages.batches.retrieve = lambda bid: SimpleNamespace(id=bid, processing_status="ended")

        def perdus(bid):
            raise anthropic.AnthropicError("No `results_url` for the given batch")

        self.fake.beta.messages.batches.results = perdus
        job2 = self.lot()
        self.assertEqual(jobs.check_batches(), 1)  # ne plante plus
        db.session.refresh(job2)
        self.assertEqual(job2.status, "error")
        # Erreur passagère : le lot reste à relever… sauf s'il est trop ancien (résultats gardés 29 jours)
        job3 = self.lot()

        def panne(bid):
            raise anthropic.APIConnectionError(request=req)

        self.fake.beta.messages.batches.retrieve = panne
        self.assertEqual(jobs.check_batches(), 0)
        self.assertEqual(db.session.get(AIJob, job3.id).status, "batch")
        opts = job3.options
        opts["batch_at"] = (now() - timedelta(days=30)).isoformat()
        job3.options = opts
        db.session.commit()
        self.assertEqual(jobs.check_batches(), 1)
        self.assertEqual(db.session.get(AIJob, job3.id).status, "error")

    def test_verifier_et_relancer_disent_la_verite(self):
        self.claude()
        c = self.login()
        job = self.lot()
        r = c.post("/transformations/", data={"_csrf": c.token, "relancer": job.id})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("Travail relancé", self.flashes(c))
        self.assertEqual(db.session.get(AIJob, job.id).status, "batch")

        def introuvable(bid):
            req = httpx2.Request("GET", "https://api.anthropic.com/")
            raise anthropic.NotFoundError("not found", response=httpx2.Response(404, request=req), body=None)

        self.fake.beta.messages.batches.retrieve = introuvable
        c.post("/transformations/", data={"_csrf": c.token, "verifier_lots": "1"})
        self.assertIn("échec", self.flashes(c))

    def test_travail_interrompu_par_une_erreur_imprevue(self):
        self.claude()
        job = jobs.enqueue("essai_lot", {}, user=self.admin)

        def plante(j):
            j.status = "running"
            db.session.commit()
            raise RuntimeError("base verrouillée")

        with mock.patch.object(jobs, "run_job", side_effect=plante):
            with self.assertRaises(RuntimeError):
                jobs.run_pending()
        db.session.rollback()
        job = db.session.get(AIJob, job.id)
        self.assertEqual(job.status, "error")  # relançable, au lieu de rester « En cours » jusqu'au redémarrage
        self.assertIn("relance", job.message)

    def test_hangar_run_reprend_les_travaux(self):
        import hangar
        from hangar import cli

        tmp = tempfile.mkdtemp(prefix="hangar-run-")
        apps, vraie = [], hangar.create_app
        try:
            args = Namespace(data=tmp, host="127.0.0.1", port=port_libre(), debug=False, no_browser=True)
            with mock.patch("waitress.serve") as serve, mock.patch("hangar.mailer.start_worker"), \
                    mock.patch("hangar.ai.jobs.resume") as resume, mock.patch("sys.stdout", new=io.StringIO()), \
                    mock.patch("hangar.create_app", side_effect=lambda d: apps.append(vraie(d)) or apps[-1]):
                cli.cmd_run(args)
            serve.assert_called_once()
            resume.assert_called_once_with(apps[0])
        finally:
            for app in apps:
                with app.app_context():
                    db.engine.dispose()  # Windows : la base doit être fermée pour effacer le dossier
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Piste 47 : pages longues
# ---------------------------------------------------------------------------

class PagesLonguesTests(IATestCase):
    def setUp(self):
        super().setUp()
        self.long = page_longue()
        self.assertGreater(len(self.long), demandes.MAX_CHARS_COURS)
        self.ecrire("Cours/Long.md", self.long)

    def url(self):
        from hangar.wiki.vault import get_vault
        return get_vault().notes["cours/long"].url

    def test_tuteur_recoit_les_passages_utiles(self):
        self.claude()
        c = self.login()
        self.fake.replies.append(claude_msg(["Il dit que la portance double."], model="claude-haiku-5-5"))
        r = self.post_json(c, "/claude/tuteur/", {"page": "Cours/Long", "question": "Que dit le théorème de Zorglub ?"})
        self.assertEqual(r.status_code, 200, r.json)
        system = self.fake.requests[0]["system"][0]["text"]
        self.assertIn("Zorglub", system)
        self.assertIn("extraits", system)
        self.assertLess(len(system), demandes.MAX_CHARS_COURS + 3000)
        self.assertIn("Page longue", r.json["html"])  # dit aussi à l'écran que la page n'a pas été lue en entier

    def test_panneau_signale_la_page_coupee_et_garde_les_passages_utiles(self):
        self.claude()
        c = self.login()
        j = c.get("/ia/contexte", query_string={"path": self.url()}).json
        self.assertTrue(j["context"]["coupee"])
        self.assertGreaterEqual(j["context"]["taille"], len(self.long) - 10)  # taille d'origine, pas celle coupée
        self.assertGreater(j["context"]["taille"], j["context"]["transmis"])
        self.fake.replies.append(claude_msg(["Réponse."]))
        self.chat(c, "Que dit le théorème de Zorglub ?", path=self.url())
        prompt = self.fake.requests[0]["messages"][-1]["content"]
        self.assertIn("Zorglub", prompt)
        self.assertIn("extraits", prompt)
        # Une page courte n'est pas signalée
        j = c.get("/ia/contexte", query_string={"path": "/wiki/Cours/Ohm"}).json
        self.assertFalse(j["context"].get("coupee"))

    def test_explication_et_copie_avec_un_long_cours(self):
        self.claude()
        self.ecrire("QCM/Physique/long.json", json.dumps({"titre": "Long", "source_page": "Cours/Long", "questions": [
            {"id": "z1", "type": "QRC", "question": "Que dit le théorème de Zorglub ?", "reponses_texte": ["portance"]}]},
            ensure_ascii=False))
        c = self.login()
        self.fake.replies.append(claude_msg(["Explication."], model="claude-haiku-5-5"))
        r = self.post_json(c, "/qcm/expliquer/", {"set_id": "QCM/Physique/long", "question_id": "z1"})
        self.assertEqual(r.status_code, 200, r.json)
        texte = " ".join(b["text"] for b in self.fake.requests[0]["messages"][0]["content"])
        self.assertIn("Zorglub affirme", texte)
        self.assertIn("extraits", texte)
        from test_ai import tiny_png
        self.fake.replies.append(claude_msg(["### Bilan\n12/20"]))
        r = c.post("/copie/", data={"_csrf": c.token, "enonce": "Applique le théorème de Zorglub.", "page": "Cours/Long",
                                     "photos": [(io.BytesIO(tiny_png()), "p.png")]}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        system = self.fake.requests[1]["system"][0]["text"]
        self.assertIn("Zorglub affirme", system)
        self.assertIn("extraits", system)


# ---------------------------------------------------------------------------
# Piste 48 : Ollama
# ---------------------------------------------------------------------------

class OllamaTests(IATestCase):
    def test_avertit_quand_la_demande_depasse_la_memoire_d_ollama(self):
        srv = espionner_get(self.server())
        self.ollama(srv)
        srv.script.append({"sse": sse_text("Réponse.")})
        r = core.generate("Bonjour")
        self.assertEqual(r.warnings, [])  # petite demande : rien à signaler
        srv.script.append({"sse": sse_text("Réponse.")})
        r = core.generate("Résume ce cours : " + "La portance dépend de la vitesse et de l’incidence. " * 400)
        self.assertEqual(len(r.warnings), 1)
        self.assertIn("4 096", r.warnings[0])
        self.assertIn("Context length", r.warnings[0])
        # Le panneau affiche l'avertissement
        c = self.login()
        srv.script.append({"sse": sse_text("Réponse.")})
        events = self.chat(c, "Résume : " + "Le décrochage survient au-delà de l’incidence critique. " * 300)
        infos = [d["message"] for e, d in events if e == "info"]
        self.assertTrue(any("Context length" in m for m in infos), infos)

    def test_pas_d_avertissement_si_ollama_a_assez_de_memoire(self):
        srv = espionner_get(self.server(), {"/api/ps": {"models": [{"name": "llama3.2:latest", "context_length": 32768}]}})
        self.ollama(srv)
        srv.script.append({"sse": sse_text("Réponse.")})
        r = core.generate("Résume ce cours : " + "La portance dépend de la vitesse et de l’incidence. " * 400)
        self.assertEqual(r.warnings, [])
        self.assertIn("/api/ps", [g["path"] for g in srv.gets])

    def test_aide_pour_windows(self):
        page = self.login().get("/ia/reglages/").get_data(as_text=True)
        self.assertNotIn("OLLAMA_CONTEXT_LENGTH=16384 ollama serve", page)
        self.assertIn("Context length", page)
        self.assertIn("OLLAMA_CONTEXT_LENGTH", page)

    def test_deux_appels_d_outils_sous_le_meme_index(self):
        srv = self.server()
        self.ollama(srv)
        chunks = [
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_a", "type": "function", "function": {
                "name": "lister_taches", "arguments": '{"filtre": "retard"}'}}]}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_b", "type": "function", "function": {
                "name": "chercher", "arguments": '{"texte": "rapport"}'}}]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ]
        srv.script.append({"sse": chunks})
        tools = [{"name": n, "description": "", "input_schema": {"type": "object", "properties": {}}}
                 for n in ("lister_taches", "chercher")]
        events = list(core.generate("x", tools=tools, stream=True))
        r = events[-1]["result"]
        self.assertEqual([(t.id, t.name, t.input) for t in r.tool_calls],
                         [("call_a", "lister_taches", {"filtre": "retard"}), ("call_b", "chercher", {"texte": "rapport"})])
        self.assertEqual([e["name"] for e in events if e["type"] == "tool_start"], ["lister_taches", "chercher"])


# ---------------------------------------------------------------------------
# Piste 61 : clé liée à l'adresse
# ---------------------------------------------------------------------------

CLE_OR = "sk-or-CLE-SECRETE-123456"


class CleFournisseurTests(IATestCase):
    def openrouter(self):
        p = AIProvider(name="OpenRouter", kind="openai_compatible", preset="openrouter",
                       base_url="https://openrouter.ai/api/v1", api_key=CLE_OR, model="llama", enabled=True)
        db.session.add(p)
        db.session.commit()
        return p

    def form(self, p, c, **kw):
        data = {"_csrf": c.token, "id": p.id, "preset": p.preset, "name": p.name, "base_url": p.base_url,
                "api_key": "", "model": p.model, "enabled": ["0", "1"]}
        data.update(kw)
        return data

    def test_changer_d_hote_efface_la_cle(self):
        srv = espionner_get(self.server())
        p = self.openrouter()
        c = self.login()
        # Essai avant d'enregistrer : la clé ne part pas vers la nouvelle adresse
        c.post("/ia/fournisseurs/essai", data=self.form(p, c, base_url=srv.url, what="models"))
        self.assertEqual([g["auth"] for g in srv.gets], [None])
        # Enregistrement : la clé est effacée (il faut la retaper pour ce serveur)
        c.post(f"/ia/fournisseurs/{p.id}", data=self.form(p, c, base_url=srv.url))
        db.session.refresh(p)
        self.assertEqual((p.base_url, p.api_key), (srv.url, ""))
        self.assertIn("clé enregistrée a été effacée", self.flashes(c))
        c.post(f"/ia/fournisseurs/{p.id}/tester", data={"_csrf": c.token})
        self.assertNotIn(f"Bearer {CLE_OR}", [g["auth"] for g in srv.gets])
        # Retaper la clé la garde
        c.post(f"/ia/fournisseurs/{p.id}", data=self.form(p, c, base_url=srv.url, api_key="cle-locale"))
        db.session.refresh(p)
        self.assertEqual(p.api_key, "cle-locale")

    def test_meme_hote_garde_la_cle(self):
        p = self.openrouter()
        c = self.login()
        c.post(f"/ia/fournisseurs/{p.id}", data=self.form(p, c, base_url="https://openrouter.ai/api/v2"))
        db.session.refresh(p)
        self.assertEqual(p.api_key, CLE_OR)

    def test_claude_adresse_personnalisee(self):
        p = self.claude()
        c = self.login()
        vue = mock.patch.object(P.AnthropicProvider, "test", autospec=True,
                                side_effect=lambda prov: (True, "clé : " + (prov.row.api_key or "aucune")))
        with vue:
            r = c.post("/ia/fournisseurs/essai", data=self.form(p, c, base_url="http://127.0.0.1:9", what="test"))
            self.assertNotIn(SECRET, r.get_data(as_text=True))
            r = c.post("/ia/fournisseurs/essai", data=self.form(p, c, what="test"))  # adresse d'origine : clé reprise
            self.assertIn(SECRET, r.json["message"])

    def test_claude_passerelle_sans_redirection(self):
        p = self.claude()
        p.base_url = "https://passerelle.example/anthropic"
        self.app.config.pop("AI_ANTHROPIC_FACTORY")
        client = P.get(p).client()  # vrai client du SDK (aucun appel réseau à la construction)
        self.assertFalse(client._client.follow_redirects)  # x-api-key ne suit pas une redirection
        p.base_url = ""
        self.assertTrue(P.get(p).client()._client.follow_redirects)

    def test_nom_de_variable_d_environnement_limite(self):
        srv = self.server()
        c = self.login()
        base = {"_csrf": c.token, "preset": "custom", "name": "Perso", "base_url": srv.url, "model": "m"}
        c.post("/ia/fournisseurs/nouveau", data={**base, "api_key_env": "SECRET_KEY"})
        c.post("/ia/fournisseurs/nouveau", data={**base, "api_key_env": "HANGAR_SMTP_PASSWORD"})
        self.assertEqual(AIProvider.query.count(), 0)
        self.assertIn("Variable d’environnement refusée", self.flashes(c))
        c.post("/ia/fournisseurs/nouveau", data={**base, "api_key_env": "MA_CLE_API"})
        c.post("/ia/fournisseurs/nouveau", data={**base, "api_key_env": "OPENROUTER_API_KEY"})
        self.assertEqual(AIProvider.query.count(), 2)
        # Un nom déjà enregistré par une version précédente reste accepté (données existantes)
        p = AIProvider.query.first()
        p.api_key_env = "ANCIEN_NOM"
        db.session.commit()
        c.post(f"/ia/fournisseurs/{p.id}", data={**base, "name": "Renommé", "api_key_env": "ANCIEN_NOM"})
        db.session.refresh(p)
        self.assertEqual((p.name, p.api_key_env), ("Renommé", "ANCIEN_NOM"))

    def test_redirection_vers_un_autre_hote_refusee(self):
        cible = espionner_get(self.server())
        redir = Redirige(cible.url.replace("127.0.0.1", "localhost").removesuffix("/v1"))
        try:
            p = AIProvider(name="Perso", kind="openai_compatible", preset="custom", base_url=redir.url,
                           api_key="cle-perso-secrete", model="m", enabled=True)
            db.session.add(p)
            db.session.commit()
            ok, msg = P.get(p).test()
            self.assertFalse(ok)
            self.assertIn("redirig", msg.lower())
            self.assertEqual([g["auth"] for g in cible.gets if g["auth"]], [])
        finally:
            redir.close()


# ---------------------------------------------------------------------------
# Après relecture
# ---------------------------------------------------------------------------

def cours_chapitres(n=42, repetitions=28):
    """Cours en chapitres « ## Chapitre N » dont le texte ne répète pas le titre (comme un vrai cours) ;
    42 chapitres : à peine plus long que la limite de 80 000 caractères."""
    blocs = ["# Cours\n\nIntroduction du cours de mécanique du vol."]
    for i in range(1, n + 1):
        blocs.append(f"## Chapitre {i}\n\n"
                     + f"Contenu propre à la partie numéro-{i}-fin sur l’aérodynamique générale. " * repetitions)
    blocs.append("## Synthèse\n\nLe décrochage survient au-delà de l’incidence critique.")
    return "\n\n".join(blocs)


def texte_du_prompt(contenu):
    return contenu if isinstance(contenu, str) else " ".join(b.get("text", "") for b in contenu)


class ExtraitsBudgetTests(IATestCase):
    """Piste 47 : une page à peine trop longue est envoyée presque entière, pas réduite à quelques titres."""

    def test_tuteur_page_a_peine_trop_longue_et_question_sur_un_titre(self):
        texte = cours_chapitres()
        self.assertTrue(demandes.MAX_CHARS_COURS < len(texte) < 1.1 * demandes.MAX_CHARS_COURS)
        self.ecrire("Cours/Vol.md", texte)
        self.claude()
        c = self.login()
        self.fake.replies.append(claude_msg(["ok"], model="claude-haiku-5-5"))
        r = self.post_json(c, "/claude/tuteur/", {"page": "Cours/Vol", "question": "Explique-moi le chapitre 12"})
        self.assertTrue(r.json["coupee"])
        system = self.fake.requests[0]["system"][0]["text"]
        self.assertIn("numéro-12-fin", system)  # le texte du chapitre 12, pas seulement son titre
        transmis = system.split("n’en parle pas.)\n\n", 1)[1]
        self.assertGreater(len(transmis), 0.9 * demandes.MAX_CHARS_COURS)  # le budget est rempli
        self.assertLessEqual(len(transmis), demandes.MAX_CHARS_COURS)

    def test_panneau_annonce_la_taille_vraiment_transmise(self):
        from hangar.wiki.vault import get_vault

        self.ecrire("Cours/Vol.md", cours_chapitres())
        self.claude()
        c = self.login()
        self.fake.replies.append(claude_msg(["ok"]))
        self.chat(c, "Quand survient le décrochage ?", path=get_vault().notes["cours/vol"].url)
        prompt = texte_du_prompt(self.fake.requests[0]["messages"][-1]["content"])
        self.assertIn("Le décrochage survient", prompt)
        annonce = int(re.search(r"environ ([\d ]+) caractères sur", prompt).group(1).replace(" ", ""))
        self.assertGreater(annonce, 70_000)
        self.assertGreater(len(prompt), annonce)  # la note dit ce qui part vraiment, pas la limite

    def test_long_cours_et_petit_budget(self):
        from hangar.ai.context import extraits

        texte = cours_chapitres(300)
        out, coupe = extraits(texte, "Explique-moi le chapitre 12", 12_000)
        self.assertTrue(coupe)
        self.assertIn("numéro-12-fin", out)
        self.assertTrue(0.9 * 12_000 < len(out) <= 12_000)
        self.assertLess(out.count("## Chapitre"), 15)  # pas la liste de tous les titres « Chapitre N »
        out, _ = extraits(texte, "Quand survient le décrochage ?", 12_000)
        self.assertIn("incidence critique", out)
        self.assertTrue(0.9 * 12_000 < len(out) <= 12_000)
        out, _ = extraits("Une phrase sans fin. " * 20_000 + "Le décrochage arrive.", "décrochage", 12_000)
        self.assertIn("décrochage", out)  # un paragraphe géant est découpé
        self.assertGreater(len(out), 0.9 * 12_000)


class ReservesLotsTests(IATestCase):
    """Piste 45 : les réserves des lots ne suspendent pas l'IA, elles limitent seulement le dépôt de lots."""

    def test_lots_de_pdf_ne_suspendent_pas_l_ia(self):
        from test_ai import tiny_pdf

        self.claude()
        self.assertEqual(AISettings.get().monthly_cap, 5.0)  # plafond par défaut
        c = self.login()
        for n in range(6):
            self.ecrire(f"Cours/P{n}.pdf", tiny_pdf(f"Cours {n}"))
            c.post("/transformations/", data={"_csrf": c.token, "pdf": f"Cours/P{n}.pdf", "lot": "1"})
            jobs.run_pending()
        self.assertEqual([j.status for j in AIJob.query.order_by(AIJob.id)], ["batch"] * 6)
        self.assertLess(core.reserved_cost(), 1.5)  # PDF d'une page : bien moins de 1 $ chacun
        self.assertEqual(core.month_cost(), 0)
        self.assertFalse(core.cap_reached())
        self.fake.replies.append(claude_msg(["Bonjour !"]))
        events = self.chat(c, "Bonjour")
        self.assertNotIn("error", [e for e, _ in events])
        self.fake.replies.append(claude_msg(["R est la résistance."], model="claude-haiku-5-5"))
        r = self.post_json(c, "/claude/tuteur/", {"page": "Cours/Ohm", "question": "Que vaut R ?"})
        self.assertEqual(r.status_code, 200, r.json)

    def test_tarif_inconnu_sans_famille(self):
        notes = core.price_notes(AIProvider(kind="anthropic", model="claude-inconnu-9"))
        self.assertIn("coût estimé au tarif le plus cher de Claude (10 $ / 50 $", notes[0])
        self.assertNotIn("le plus cher le plus cher", notes[0])


class CleVariableTests(IATestCase):
    """Piste 61 : la clé lue dans une variable d'environnement ne suit pas non plus une nouvelle adresse."""

    def test_variable_d_openrouter_ne_suit_pas_une_nouvelle_adresse(self):
        srv = espionner_get(self.server())
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-ENV-SECRETE"}):
            p = AIProvider(name="OpenRouter", kind="openai_compatible", preset="openrouter",
                           base_url="https://openrouter.ai/api/v1", api_key_env="OPENROUTER_API_KEY", model="llama",
                           enabled=True)
            db.session.add(p)
            db.session.commit()
            self.assertEqual(p.key(), "sk-or-ENV-SECRETE")  # vers openrouter.ai, elle part
            c = self.login()
            data = {"_csrf": c.token, "id": p.id, "preset": "openrouter", "name": p.name, "base_url": srv.url,
                    "api_key": "", "api_key_env": "OPENROUTER_API_KEY", "model": "llama", "enabled": ["0", "1"]}
            r = c.post("/ia/fournisseurs/essai", data={**data, "what": "test"})
            self.assertFalse(r.json["ok"])
            self.assertIn("ne part que vers openrouter.ai", r.json["message"])
            c.post("/ia/fournisseurs/essai", data={**data, "what": "models"})
            c.post(f"/ia/fournisseurs/{p.id}", data=data)
            self.assertIn("ne part que vers openrouter.ai", self.flashes(c))
            db.session.refresh(p)
            self.assertEqual((p.base_url, p.key()), (srv.url, ""))
            c.post(f"/ia/fournisseurs/{p.id}/tester", data={"_csrf": c.token})
            self.assertNotIn("Authorization", P.get(p)._headers())
            self.assertEqual([g["auth"] for g in srv.gets if g["auth"]], [])
            self.assertIn("ne part que vers openrouter.ai", c.get("/ia/reglages/").get_data(as_text=True))
            # De retour sur openrouter.ai, la clé repart
            c.post(f"/ia/fournisseurs/{p.id}", data={**data, "base_url": "https://openrouter.ai/api/v1"})
            db.session.refresh(p)
            self.assertEqual(p.key(), "sk-or-ENV-SECRETE")

    def test_variable_anthropic_reservee_a_anthropic(self):
        srv = espionner_get(self.server())
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-ENV-SECRETE"}):
            p = self.claude()
            p.api_key = ""
            db.session.commit()
            self.assertEqual(p.key(), "sk-ant-ENV-SECRETE")  # adresse par défaut : api.anthropic.com
            self.app.config.pop("AI_ANTHROPIC_FACTORY")
            c = self.login()
            data = {"_csrf": c.token, "id": p.id, "preset": "anthropic", "name": p.name,
                    "base_url": srv.url.removesuffix("/v1"), "api_key": "", "model": p.model, "enabled": ["0", "1"],
                    "what": "test"}
            r = c.post("/ia/fournisseurs/essai", data=data)
            self.assertFalse(r.json["ok"])
            self.assertIn("ne part que vers api.anthropic.com", r.json["message"])
            self.assertEqual(srv.gets, [])
            # Une passerelle reste possible en tapant la clé
            vue = mock.patch.object(P.AnthropicProvider, "test", autospec=True,
                                    side_effect=lambda prov: (True, "clé : " + prov.row.key()))
            with vue:
                r = c.post("/ia/fournisseurs/essai", data={**data, "api_key": "sk-ant-passerelle"})
            self.assertIn("sk-ant-passerelle", r.json["message"])

    def test_variable_personnelle_liee_a_l_adresse_ou_elle_a_ete_indiquee(self):
        a, b = espionner_get(self.server()), espionner_get(self.server())
        with mock.patch.dict(os.environ, {"MON_SERVEUR_API_KEY": "cle-perso", "AUTRE_API_KEY": "cle-b"}):
            c = self.login()
            base = {"_csrf": c.token, "preset": "custom", "name": "Perso", "model": "m",
                    "api_key_env": "MON_SERVEUR_API_KEY"}
            c.post("/ia/fournisseurs/nouveau", data={**base, "base_url": a.url})
            p = AIProvider.query.one()
            self.assertEqual(p.key(), "cle-perso")
            c.post("/ia/fournisseurs/essai", data={**base, "id": p.id, "base_url": b.url, "what": "models"})
            self.assertEqual([g["auth"] for g in b.gets], [None])
            c.post(f"/ia/fournisseurs/{p.id}", data={**base, "base_url": b.url})
            db.session.refresh(p)
            self.assertEqual(p.key(), "")  # nouvelle adresse : la clé de la variable ne suit pas
            self.assertIn(f"ne part que vers 127.0.0.1:{a.server.server_address[1]}", self.flashes(c))
            # Une variable indiquée pour la nouvelle adresse vaut pour elle
            c.post(f"/ia/fournisseurs/{p.id}", data={**base, "base_url": b.url, "api_key_env": "AUTRE_API_KEY"})
            db.session.refresh(p)
            self.assertEqual(p.key(), "cle-b")
            # Fournisseur enregistré avant cette règle (adresse liée vide) : rien ne change pour lui
            ancien = AIProvider(name="Ancien", kind="openai_compatible", preset="custom", base_url=a.url,
                                api_key_env="MON_SERVEUR_API_KEY", model="m")
            self.assertIsNone(ancien.api_key_env_origin)
            self.assertEqual(ancien.key(), "cle-perso")

    def test_colonne_ajoutee_a_une_base_existante(self):
        p = AIProvider(name="Perso", kind="openai_compatible", preset="custom", base_url="http://10.0.0.2:8080/v1",
                       api_key_env="MON_SERVEUR_API_KEY", model="m")
        db.session.add(p)
        db.session.commit()
        db.session.execute(text("ALTER TABLE ai_provider DROP COLUMN api_key_env_origin"))  # base d'avant
        db.session.commit()
        db.session.remove()
        from hangar import _auto_migrate
        _auto_migrate()
        p = AIProvider.query.one()
        self.assertIsNone(p.api_key_env_origin)
        with mock.patch.dict(os.environ, {"MON_SERVEUR_API_KEY": "cle-perso"}):
            self.assertEqual(p.key(), "cle-perso")


class AvertissementsOllamaTests(IATestCase):
    """Piste 48 : les demandes courtes ne dégradent plus Ollama sans prévenir."""

    def test_tuteur_avec_ollama_envoie_une_part_a_sa_mesure(self):
        srv = self.server()
        self.ollama(srv)
        self.ecrire("Cours/Long.md", page_longue(20))
        self.assertGreater(len(page_longue(20)), 30_000)
        c = self.login()
        srv.script.append({"sse": sse_text("Réponse du tuteur.")})
        r = self.post_json(c, "/claude/tuteur/", {"page": "Cours/Long", "question": "Que dit le théorème de Zorglub ?"})
        self.assertEqual(r.status_code, 200, r.json)
        self.assertTrue(r.json["coupee"])
        self.assertIn("Page longue", r.json["html"])
        systeme = srv.requests[0]["body"]["messages"][0]["content"]
        self.assertIn("Zorglub affirme", systeme)
        self.assertLess(len(systeme), 14_000)  # 12 000 caractères de page pour un modèle local, comme le panneau

    def test_avertissement_ollama_affiche_par_le_tuteur_et_le_wiki(self):
        petite_memoire = {"/api/ps": {"models": [{"name": "llama3.2:latest", "context_length": 2048}]}}
        srv = espionner_get(self.server(), petite_memoire)
        self.ollama(srv)
        self.ecrire("Cours/Long.md", page_longue(20))
        c = self.login()
        srv.script.append({"sse": sse_text("Réponse du tuteur.")})
        r = self.post_json(c, "/claude/tuteur/", {"page": "Cours/Long", "question": "Que dit le théorème de Zorglub ?"})
        self.assertIn("Context length", r.json["avertissement"])
        self.assertIn("Context length", r.json["html"])  # à l'écran, au-dessus de la réponse
        self.assertNotIn("Context length", AITutorExchange.query.one().reponse)  # pas gardé avec la réponse
        for n in range(4):  # « Demander au wiki » : jusqu'à 40 000 caractères d'extraits, quel que soit le modèle
            self.ecrire(f"Cours/Portance{n}.md", "\n\n".join(
                f"La portance dépend de la vitesse, de la densité de l’air et de l’incidence (cas {n}-{k})." * 2
                for k in range(80)))
        srv.script.append({"sse": sse_text("Réponse du wiki [1].")})
        r = self.post_json(c, "/demander/", {"question": "De quoi dépend la portance ?"})
        self.assertEqual(r.status_code, 200, r.json)
        self.assertIn("Context length", r.json["avertissement"])
        self.assertIn("Context length", r.json["html"])
        # Petite demande : rien à signaler
        srv.script.append({"sse": sse_text("Court.")})
        r = self.post_json(c, "/claude/tuteur/", {"page": "Cours/Ohm", "question": "Que vaut R ?"})
        self.assertEqual(r.json["avertissement"], "")
