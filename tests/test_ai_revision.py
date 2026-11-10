"""Tests des fonctions d'IA portées de Revision (sans réseau : faux client Anthropic, faux serveur compatible OpenAI).

Explication et correction des QCM, cartes d'une page, QCM / variantes / vocabulaire générés en travail de fond avec
relecture et publication dans le coffre, transformation d'un PDF ou d'un cours LaTeX en page (avec historique),
fiche de révision, tuteur, « Demander au wiki » (citations), copie photographiée, « Améliorer », panneau.

Lancer : uv run python -m unittest tests.test_ai_revision -v
"""
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from test_ai import AITestCase, claude_msg, parse_sse, sse_text, tiny_pdf, tiny_png

from hangar.ai import jobs, revision_views
from hangar.ai.models import AICopie, AIJob, AISettings, AITutorExchange, AIUsage
from hangar.extensions import db
from hangar.models import Notification

COURS = """---
tags: [physique]
---
# Loi d'Ohm

La loi d'Ohm relie la tension et l'intensité dans un conducteur ohmique.

> [!definition] Résistance
> La résistance $R$ s'exprime en ohms : $U = R I$.

La ==tension== se mesure en volts avec un voltmètre branché en dérivation.
"""

SET_OHM = {"titre": "Loi d'Ohm", "description": "Questions de base", "source_page": "Cours/Ohm", "questions": [
    {"id": "q1", "type": "QCM", "question": "Unité de la résistance ?", "choix": ["volt", "ohm", "ampère"], "reponses": [1],
     "explication": "La résistance s'exprime en ohms."},
    {"id": "q2", "type": "QRC", "question": "Appareil qui mesure la tension ?", "reponses_texte": ["voltmètre"]},
]}

QUESTIONS_IA = {"titre": "Ohm (IA)", "description": "Généré", "questions": [
    {"type": "QCM", "question": "Que vaut U ?", "choix": ["R I", "R/I", "I/R", "R+I"], "reponses": [0], "reponses_texte": [],
     "consigne": "", "verso": "", "explication": "Loi d'Ohm."},
    {"type": "TROU", "question": "La tension se mesure en _____.", "choix": [], "reponses": [], "reponses_texte": ["volts"],
     "consigne": "", "verso": "", "explication": ""},
    {"type": "QCM", "question": "Question mal formée", "choix": ["a"], "reponses": [7], "reponses_texte": [],
     "consigne": "", "verso": "", "explication": ""},
]}


class RevisionTestCase(AITestCase):
    def setUp(self):
        super().setUp()
        self.ecrire("Cours/Ohm.md", COURS)
        self.ecrire("QCM/Physique/ohm.json", json.dumps(SET_OHM, ensure_ascii=False))

    def ecrire(self, rel, texte):
        p = Path(self.app.config["WIKI_DIR"]) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(texte, bytes):
            p.write_bytes(texte)
        else:
            p.write_text(texte, encoding="utf-8")
        from hangar.wiki.vault import invalider
        invalider()
        return p

    def lire(self, rel):
        return (Path(self.app.config["WIKI_DIR"]) / rel).read_text(encoding="utf-8")

    def post_json(self, c, url, data):
        return c.post(url, json=data, headers={"X-CSRF-Token": c.token})

    def library(self):
        from hangar.qcm import quiz
        return quiz.get_library()


class QcmTests(RevisionTestCase):
    def test_expliquer_cache_droits_et_plafond(self):
        c = self.login("invite")  # lecture : tous les comptes
        r = self.post_json(c, "/qcm/expliquer/", {"set_id": "QCM/Physique/ohm", "question_id": "q1"})
        self.assertEqual(r.status_code, 503)  # aucun fournisseur
        self.claude()
        self.fake.replies.append(claude_msg(["Parce que $U = R I$ : la **résistance** est en ohms."], model="claude-haiku-5-5"))
        r = self.post_json(c, "/qcm/expliquer/", {"set_id": "QCM/Physique/ohm", "question_id": "q1"})
        self.assertEqual(r.status_code, 200, r.json)
        self.assertFalse(r.json["gardee"])
        self.assertIn("<strong>résistance</strong>", r.json["html"])
        req = self.fake.requests[0]
        self.assertEqual((req["model"], req["output_config"]["effort"]), ("claude-haiku-5-5", "low"))
        self.assertTrue(req["system"][0]["text"].startswith("Tu expliques à un étudiant"))
        texte = " ".join(b["text"] for b in req["messages"][0]["content"])
        self.assertIn("Cours « Ohm »", texte)
        self.assertIn("Bonne(s) réponse(s) : B", texte)
        # Gardée : pas de nouvel appel, même si le plafond est atteint
        AISettings.get().monthly_cap = 0.000001
        db.session.commit()
        r = self.post_json(c, "/qcm/expliquer/", {"set_id": "QCM/Physique/ohm", "question_id": "q1"})
        self.assertTrue(r.json["gardee"])
        self.assertEqual(len(self.fake.requests), 1)
        r = self.post_json(c, "/qcm/expliquer/", {"set_id": "QCM/Physique/ohm", "question_id": "q2"})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(self.post_json(c, "/qcm/expliquer/", {"set_id": "QCM/Physique/ohm", "question_id": "zz"}).status_code, 404)

    def test_corriger_qrc(self):
        self.claude()
        c = self.login()
        self.fake.replies.append(claude_msg(['{"verdict": "partiel", "retour": "C’est presque ça."}'], model="claude-haiku-5-5"))
        r = self.post_json(c, "/qcm/corriger-qrc/", {"set_id": "QCM/Physique/ohm", "question_id": "q2", "reponse": "un multimètre"})
        self.assertEqual((r.json["verdict"], r.json["correct"]), ("partiel", False))
        self.assertEqual(self.fake.requests[0]["output_config"]["format"]["schema"]["required"], ["verdict", "retour"])
        self.assertIn("Réponse de l'étudiant : un multimètre", self.fake.requests[0]["messages"][0]["content"][0]["text"])
        # Une question à choix n'est pas une QRC
        r = self.post_json(c, "/qcm/corriger-qrc/", {"set_id": "QCM/Physique/ohm", "question_id": "q1", "reponse": "x"})
        self.assertEqual(r.status_code, 404)
        AISettings.get().monthly_cap = 0.000001
        db.session.add(AIUsage(cost=1.0))
        db.session.commit()
        r = self.post_json(c, "/qcm/corriger-qrc/", {"set_id": "QCM/Physique/ohm", "question_id": "q2", "reponse": "x"})
        self.assertEqual(r.status_code, 429)

    def test_corriger_question_signalee(self):
        from hangar.qcm.models import Signalement

        self.claude()
        s = Signalement(set_id="QCM/Physique/ohm", question_id="q1", user_id=self.member.id, commentaire="Le volt est faux ?")
        db.session.add(s)
        db.session.commit()
        corrigee = {"type": "QCM", "question": "Unité de la résistance électrique ?", "choix": ["volt", "ohm", "ampère"],
                    "reponses": [1], "reponses_texte": [], "consigne": "", "verso": "", "explication": "En ohms (Ω).",
                    "modification": "Énoncé précisé."}
        self.fake.replies.append(claude_msg([json.dumps(corrigee, ensure_ascii=False)]))
        c = self.login()
        r = c.post("/qcm/signalements/", data={"_csrf": c.token, "pk": s.id, "action": "proposer"})
        self.assertEqual(r.status_code, 302)
        db.session.refresh(s)
        self.assertEqual(s.proposition["question"], "Unité de la résistance électrique ?")
        self.assertEqual(s.proposition_note, "Énoncé précisé.")
        self.assertIn("Le volt est faux ?", self.fake.requests[0]["messages"][0]["content"][-1]["text"])
        self.assertEqual(self.fake.requests[0]["output_config"]["format"]["type"], "json_schema")

    def test_cartes_de_la_page(self):
        c = self.login()
        r = c.post("/qcm/cartes-de-la-page/", data={"_csrf": c.token, "page": "Cours/Ohm"})
        self.assertEqual(r.status_code, 302)
        data = json.loads(self.lire("QCM/Cours/Ohm (cartes).json"))
        self.assertEqual({q["type"] for q in data["questions"]}, {"FLASHCARD", "TROU"})
        self.assertIsNotNone(self.library().get("QCM/Cours/Ohm (cartes)"))
        g = self.login("invite")
        self.assertEqual(g.post("/qcm/cartes-de-la-page/", data={"_csrf": g.token, "page": "Cours/Ohm"}).status_code, 403)

    def test_importer_vocabulaire_sans_ia(self):
        c = self.login()
        r = c.post("/qcm/importer-vocabulaire/", data={"_csrf": c.token, "liste": "dog\tchien\ncat\tchat\n",
                                                        "langue": "Anglais", "cartes": ["decouverte", "production"]})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/qcm/langues/Anglais/", r.headers["Location"])
        self.assertTrue(list((Path(self.app.config["WIKI_DIR"]) / "QCM" / "Vocabulaire").glob("*.json")))


class JobsTests(RevisionTestCase):
    def test_generer_qcm_brouillon_relecture_publication(self):
        self.claude()
        c = self.login()
        r = c.post("/qcm/generer/", data={"_csrf": c.token, "genre": "qcm", "page": "Cours/Ohm", "nombre": "10"})
        self.assertEqual(r.headers["Location"], "/transformations/")
        job = AIJob.query.one()
        self.assertEqual((job.kind, job.status, job.payload["nombre"]), ("qcm", "pending", 10))
        self.fake.replies.append(claude_msg([json.dumps(QUESTIONS_IA, ensure_ascii=False)]))
        jobs.run_pending()
        db.session.refresh(job)
        self.assertEqual(job.status, "draft")
        self.assertIn("1 question mal formée écartée", job.message)
        req = self.fake.requests[0]
        self.assertEqual((req["max_tokens"], req["output_config"]["format"]["type"]), (64000, "json_schema"))
        self.assertIn("Écris 10 questions sur ce cours.", req["messages"][0]["content"][1]["text"])
        contenu = job.result["contenu"]
        self.assertEqual([q["id"] for q in contenu["questions"]], ["ia001", "ia002"])
        self.assertEqual(contenu["source_page"], "Cours/Ohm")
        self.assertEqual(contenu["genere_par"], "Claude (claude-opus-5-5)")
        n = Notification.query.filter_by(user_id=self.admin.id, kind="ai").one()
        self.assertIn("À relire", n.message)
        self.assertEqual(n.url, f"/transformations/{job.id}/relire/")
        page = c.get("/transformations/").get_data(as_text=True)
        self.assertIn("Relire et publier", page)
        self.assertIn("Que vaut U ?", c.get(f"/transformations/{job.id}/relire/").get_data(as_text=True))
        # Relecture : la 1re question corrigée et gardée, la 2e retirée
        r = c.post(f"/transformations/{job.id}/relire/", data={
            "_csrf": c.token, "titre": "Ohm relu", "description": "", "q0-garder": "1", "q0-question": "Que vaut U (en V) ?",
            "q0-choix-0": "R I", "q0-choix-1": "R/I", "q0-choix-2": "", "q0-choix-3": "R+I", "q0-bonne": "0",
            "q0-explication": "U = RI"})
        self.assertEqual(r.status_code, 302)
        db.session.refresh(job)
        self.assertEqual(job.status, "done")
        chemin = job.result["page_ecrite"]
        self.assertEqual(chemin, "QCM/Cours/ohm-relu.json")
        data = json.loads(self.lire(chemin))
        self.assertEqual(data["questions"][0]["choix"], ["R I", "R/I", "R+I"])
        qset = self.library().get("QCM/Cours/ohm-relu")
        self.assertEqual((qset.titre, len(qset.questions)), ("Ohm relu", 1))
        self.assertIn("Ohm relu", c.get("/qcm/").get_data(as_text=True))  # visible dans /qcm/
        self.assertIn("/qcm/jouer/?set=QCM/Cours/ohm-relu", r.headers["Location"])
        # Publication directe, sans relecture
        c.post("/qcm/generer/", data={"_csrf": c.token, "genre": "qcm", "texte": "Le courant se mesure en ampères.",
                                      "nombre": "20", "publier": "1", "categorie": "Physique"})
        self.fake.replies.append(claude_msg([json.dumps(QUESTIONS_IA, ensure_ascii=False)]))
        jobs.run_pending()
        j2 = AIJob.query.order_by(AIJob.id.desc()).first()
        self.assertEqual(j2.status, "done")
        self.assertTrue(j2.result["page_ecrite"].startswith("QCM/Physique/"))

    def test_droits_et_requetes_invalides(self):
        self.claude()
        g = self.login("invite")
        self.assertEqual(g.post("/qcm/generer/", data={"_csrf": g.token, "genre": "qcm", "page": "Cours/Ohm"}).status_code, 403)
        self.assertEqual(g.get("/transformations/").status_code, 403)
        c = self.login()
        r = c.post("/qcm/generer/", data={"_csrf": c.token, "genre": "qcm", "page": "Cours/Inconnue"})
        self.assertIn("#generer", r.headers["Location"])
        r = c.post("/qcm/generer/", data={"_csrf": c.token, "genre": "qcm", "categorie": "a/b", "texte": "x"})
        self.assertEqual(AIJob.query.count(), 0)

    def test_qcm_avec_modele_local(self):
        srv = self.server()
        self.ollama(srv)
        c = self.login()
        c.post("/qcm/generer/", data={"_csrf": c.token, "genre": "qcm", "page": "Cours/Ohm", "nombre": "10"})
        srv.script.append({"sse": sse_text("Voici le QCM :\n```json\n" + json.dumps(QUESTIONS_IA, ensure_ascii=False) + "\n```")})
        jobs.run_pending()
        job = AIJob.query.one()
        self.assertEqual(job.status, "draft", job.message)
        body = srv.requests[0]["body"]
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertNotIn("max_tokens", body)  # plafond laissé au serveur local
        self.assertEqual(len(job.result["contenu"]["questions"]), 2)

    def test_variantes_et_vocabulaire_en_lot(self):
        from hangar.qcm.models import QuizProgress

        self.claude()
        db.session.add(QuizProgress(user_id=self.admin.id, set_id="QCM/Physique/ohm", question_id="q1", seen=2, correct=0,
                                    last_correct=False))
        db.session.commit()
        c = self.login()
        r = c.post("/qcm/generer/", data={"_csrf": c.token, "genre": "variantes", "set": "QCM/Physique/ohm"})
        self.assertEqual(r.headers["Location"], "/transformations/")
        job = AIJob.query.one()
        self.assertEqual((job.kind, job.payload["variantes"]), ("variantes", ["q1"]))
        self.fake.replies.append(claude_msg([json.dumps(QUESTIONS_IA, ensure_ascii=False)]))
        jobs.run_pending()
        db.session.refresh(job)
        self.assertEqual(job.status, "draft")
        self.assertEqual(job.result["contenu"]["titre"], "Variantes · Loi d'Ohm")
        self.assertIn("Questions ratées", self.fake.requests[0]["messages"][0]["content"][-1]["text"])
        # Vocabulaire dans un lot (moitié prix) : résultat récupéré plus tard, puis brouillon
        c.post("/qcm/generer/", data={"_csrf": c.token, "genre": "vocabulaire", "mots": "dog\ncat", "langue": "Anglais",
                                      "cartes": ["decouverte"], "lot": "1"})
        v = AIJob.query.filter_by(kind="vocabulaire").one()
        jobs.run_pending()
        db.session.refresh(v)
        self.assertEqual(v.status, "batch")
        self.fake.batches[v.batch_id]["status"] = "ended"
        fiche = {"mot": "a dog", "traductions": ["un chien"], "definition": "an animal", "variantes": [], "exemple": "The [dog] barks.",
                 "exemple_traduction": "Le chien aboie.", "prononciation": "/dɒɡ/", "note": ""}
        self.fake.batch_reply = claude_msg([json.dumps({"titre": "Animaux", "description": "", "vocabulaire": [fiche]})])
        self.assertEqual(jobs.check_batches(), 1)
        jobs.run_pending()
        db.session.refresh(v)
        self.assertEqual(v.status, "draft")
        self.assertEqual(v.result["contenu"]["vocabulaire"][0]["mot"], "a dog")
        self.assertTrue(AIUsage.query.filter_by(batch=True).first())
        r = c.post("/transformations/", data={"_csrf": c.token, "publier": v.id})
        db.session.refresh(v)
        self.assertEqual(v.status, "done")
        self.assertTrue(v.result["page_ecrite"].startswith("QCM/Vocabulaire/"))


class TransformationTests(RevisionTestCase):
    def test_pdf_en_page_puis_reecriture_avec_historique(self):
        from hangar.wiki import historique

        self.claude()
        self.ecrire("Cours/Elec.pdf", tiny_pdf("Electricite"))
        c = self.login()
        r = c.post("/transformations/", data={"_csrf": c.token, "pdf": "Cours/Elec.pdf"})
        self.assertEqual(r.status_code, 302)
        self.fake.replies.append(claude_msg(["---\ntags: [elec]\n---\n# Électricité\n\nCours transformé."]))
        jobs.run_pending()
        job = AIJob.query.one()
        self.assertEqual(job.status, "done", job.message)
        req = self.fake.requests[0]
        self.assertEqual(req["messages"][0]["content"][0]["type"], "document")
        self.assertEqual((req["max_tokens"], req["output_config"]["effort"]), (128000, "medium"))
        texte = self.lire("Cours/Elec.md")
        self.assertIn("# Électricité", texte)
        self.assertIn("## Document d'origine", texte)
        self.assertIn("![[", texte)
        url, lien = __import__("hangar.ai.travaux", fromlist=["resultat"]).resultat(job)
        self.assertEqual((url, lien), ("/wiki/Cours/Elec", "Ouvrir la page"))
        # Nouvelle transformation : la page n'a pas bougé -> remplacée, l'ancienne version dans l'historique
        c.post("/transformations/", data={"_csrf": c.token, "pdf": "Cours/Elec.pdf"})
        self.fake.replies.append(claude_msg(["# Électricité v2"]))
        jobs.run_pending()
        self.assertIn("# Électricité v2", self.lire("Cours/Elec.md"))
        versions = historique.lister("Cours/Elec.md")
        self.assertEqual(len(versions), 1)
        # Page modifiée à la main entre la demande et le résultat -> version de l'IA à côté
        c.post("/transformations/", data={"_csrf": c.token, "pdf": "Cours/Elec.pdf"})
        self.ecrire("Cours/Elec.md", "# Mes notes à moi")
        self.fake.replies.append(claude_msg(["# Électricité v3"]))
        jobs.run_pending()
        self.assertEqual(self.lire("Cours/Elec.md"), "# Mes notes à moi")
        self.assertIn("# Électricité v3", self.lire("Cours/Elec (IA).md"))

    def test_latex_importe_et_modele_local(self):
        srv = self.server()
        self.ollama(srv)
        c = self.login()
        tex = rb"\documentclass{article}\begin{document}\section{Ohm}La loi $U=RI$.\end{document}"
        r = c.post("/transformations/", data={"_csrf": c.token, "dossier": "Cours",
                                               "fichier": (io.BytesIO(tex), "ohm2.tex")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        job = AIJob.query.one()
        self.assertEqual(job.kind, "page")
        self.assertIn("U=RI", job.payload["texte_source"])
        srv.script.append({"sse": sse_text("# Loi d'Ohm (LaTeX)\n\n$U = RI$")})
        jobs.run_pending()
        db.session.refresh(job)
        self.assertEqual(job.status, "done", job.message)
        self.assertIn("Source LaTeX du cours", srv.requests[0]["body"]["messages"][-1]["content"])
        self.assertIn("# Loi d'Ohm (LaTeX)", self.lire(job.result["page_ecrite"]))

    def test_pdf_avec_modele_local_et_import_du_wiki(self):
        srv = self.server()
        self.ollama(srv)
        c = self.login()
        r = c.post("/importer/", data={"_csrf": c.token, "dossier": "Cours", "claude": "1",
                                        "fichiers": (io.BytesIO(tiny_pdf("Thermodynamique")), "thermo.pdf")},
                   content_type="multipart/form-data", headers={"Accept": "application/json"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("transformation par l’IA lancée", r.json["results"][0]["message"])
        srv.script.append({"sse": sse_text("# Thermodynamique\n\nTexte.")})
        jobs.run_pending()
        job = AIJob.query.one()
        self.assertEqual(job.status, "done", job.message)
        self.assertIn("Thermodynamique", srv.requests[0]["body"]["messages"][-1]["content"])  # texte extrait du PDF
        self.assertIn("# Thermodynamique", self.lire("Cours/thermo.md"))

    def test_fiche_de_revision_et_echec_relance(self):
        self.claude()
        c = self.login()
        c.post("/transformations/", data={"_csrf": c.token, "fiche": "Cours/Ohm"})
        self.fake.replies.append(claude_msg([""]))
        jobs.run_pending()
        job = AIJob.query.one()
        self.assertEqual(job.status, "error")
        self.assertIn("aucun texte", job.message)
        self.assertIn("en échec", Notification.query.filter_by(kind="ai").one().message)
        c.post("/transformations/", data={"_csrf": c.token, "relancer": job.id})
        self.fake.replies.append(claude_msg(["## L'essentiel\n\n- $U = RI$"]))
        jobs.run_pending()
        db.session.refresh(job)
        self.assertEqual(job.status, "done")
        fiche = self.lire("Cours/Ohm (Fiche).md")
        self.assertTrue(fiche.startswith("> [!abstract] Fiche de révision de [[Cours/Ohm|Ohm]]"))


class WikiTests(RevisionTestCase):
    def test_tuteur_historique(self):
        self.claude()
        c = self.login("invite")
        self.fake.replies += [claude_msg(["C’est la **résistance** [en ohms]."], model="claude-haiku-5-5"),
                              claude_msg(["Oui."], model="claude-haiku-5-5")]
        r = self.post_json(c, "/claude/tuteur/", {"page": "Cours/Ohm", "question": "Que vaut R ?"})
        self.assertEqual(r.status_code, 200, r.json)
        self.assertIn("<strong>résistance</strong>", r.json["html"])
        req = self.fake.requests[0]
        self.assertIn("Page « Ohm »", req["system"][0]["text"])
        self.assertIn("La loi d'Ohm relie", req["system"][0]["text"])
        self.post_json(c, "/claude/tuteur/", {"page": "Cours/Ohm", "question": "Et U ?"})
        msgs = self.fake.requests[1]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertEqual(AITutorExchange.query.filter_by(page="Cours/Ohm").count(), 2)
        self.assertEqual(self.post_json(c, "/claude/tuteur/", {"page": "Inconnue", "question": "x"}).status_code, 404)

    def test_demander_au_wiki_citations(self):
        self.claude()
        c = self.login()
        self.fake.replies += [claude_msg(["La résistance se mesure en ohms [1]."], model="claude-haiku-5-5"),
                              claude_msg(["Avec un voltmètre [1]."], model="claude-haiku-5-5")]
        r = self.post_json(c, "/demander/", {"question": "En quelle unité s'exprime la résistance ?"})
        self.assertEqual(r.status_code, 200, r.json)
        self.assertIn('class="citation"', r.json["html"])
        self.assertIn('href="/wiki/Cours/Ohm"', r.json["html"])
        self.assertEqual(r.json["sources"][0]["titre"], "Ohm")
        self.assertTrue(r.json["sources"][0]["cite"])
        self.assertIn("[1] Ohm", self.fake.requests[0]["system"][0]["text"])
        # Question de suite : l'échange précédent est rejoué
        self.post_json(c, "/demander/", {"question": "Et la tension ?"})
        self.assertEqual(len(self.fake.requests[1]["messages"]), 3)
        page = c.get("/demander/?q=ohm").get_data(as_text=True)
        self.assertIn("Avec un voltmètre", page)
        self.assertIn("Sources (", page)

    def test_ameliorer(self):
        self.claude()
        c = self.login()
        self.fake.replies.append(claude_msg(["```markdown\nLa loi d'Ohm relie la tension et le courant.\n```"]))
        r = c.post("/claude/ameliorer/", data={"_csrf": c.token, "texte": "la loi dohm relie tension et courant", "page": "Cours/Ohm"},
                   headers={"Accept": "application/json"})
        self.assertEqual(r.json["texte"], "La loi d'Ohm relie la tension et le courant.")
        self.assertEqual(self.fake.requests[0]["output_config"]["effort"], "low")
        self.assertEqual(c.post("/claude/ameliorer/", data={"_csrf": c.token, "texte": " "}).status_code, 400)
        g = self.login("invite")
        self.assertEqual(g.post("/claude/ameliorer/", data={"_csrf": g.token, "texte": "x"}).status_code, 403)
        r = c.post("/claude/ameliorer/", data={"_csrf": c.token, "texte": "x" * 13000})
        self.assertIn("plus court", r.json["error"])

    def test_copie(self):
        self.claude()
        c = self.login()
        self.fake.replies.append(claude_msg(["### Ce que tu as écrit\n1. $U = RI$\n\n### Bilan\n15/20"]))
        r = c.post("/copie/", data={"_csrf": c.token, "enonce": "Calculer U.", "page": "Cours/Ohm",
                                     "photos": [(io.BytesIO(tiny_png()), "p1.png"), (io.BytesIO(tiny_png()), "p2.png")]},
                   content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("Bilan", r.json["bloc"])
        cp = AICopie.query.one()
        self.assertEqual((cp.photos, cp.page, cp.enonce), (2, "Cours/Ohm", "Calculer U."))
        dossier = Path(self.app.config["DATA_DIR"]) / "copies" / str(self.admin.id)
        self.assertEqual(sorted(p.name for p in dossier.iterdir()), [f"{cp.id}-0.jpg", f"{cp.id}-1.jpg"])
        req = self.fake.requests[0]
        content = req["messages"][0]["content"]
        self.assertEqual([b["type"] for b in content], ["image", "image", "text"])
        self.assertEqual(content[0]["source"]["media_type"], "image/jpeg")
        self.assertIn("Cours de référence « Ohm »", req["system"][0]["text"])
        r = c.get(f"/copie/photo/{cp.id}/0")
        self.assertEqual(r.status_code, 200)
        r.close()
        a = self.login("alice")
        self.assertEqual(a.get(f"/copie/photo/{cp.id}/0").status_code, 404)  # visibles de leur seul auteur
        self.assertEqual(a.post(f"/copie/{cp.id}/supprimer/", headers={"X-CSRF-Token": a.token}).status_code, 404)
        self.assertNotIn("Calculer U.", a.get("/copie/").get_data(as_text=True))
        # 30 dernières par compte
        with mock.patch.object(revision_views, "GARDER_COPIES", 1):
            self.fake.replies.append(claude_msg(["Deuxième."]))
            c.post("/copie/", data={"_csrf": c.token, "photos": [(io.BytesIO(tiny_png()), "p.png")]},
                   content_type="multipart/form-data")
        self.assertEqual(AICopie.query.count(), 1)
        self.assertEqual(len(list(dossier.iterdir())), 1)
        cp = AICopie.query.one()
        self.assertTrue(c.post(f"/copie/{cp.id}/supprimer/", headers={"X-CSRF-Token": c.token}).json["ok"])
        self.assertEqual(list(dossier.iterdir()), [])
        r = c.post("/copie/", data={"_csrf": c.token, "photos": [(io.BytesIO(b"pas une image"), "x.png")]},
                   content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        r = c.post("/copie/", data={"_csrf": c.token, "photos": [(io.BytesIO(tiny_png()), f"{i}.png") for i in range(5)]},
                   content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)

    def test_panneau_demander_au_wiki_et_tuteur(self):
        from hangar.ai import client_config

        self.claude()
        ids = [x["id"] for x in client_config()["commands"]]
        self.assertIn("wiki", ids)
        self.assertIn("tuteur", ids)
        c = self.login()
        self.fake.replies.append(claude_msg(["En ohms [1]."], model="claude-haiku-5-5"))
        r = c.post("/ia/chat", json={"message": "Unité de la résistance ?", "command": "wiki", "path": "/wiki/Cours/Ohm"},
                   headers={"X-CSRF-Token": c.token})
        events = parse_sse(r.get_data(as_text=True))
        done = events[-1][1]
        self.assertEqual(events[-1][0], "done", events)
        self.assertIn('class="citation"', done["html"])
        self.assertEqual(done["sources"][0]["url"], "/wiki/Cours/Ohm")
        req = self.fake.requests[0]
        self.assertEqual(req["model"], "claude-haiku-5-5")
        self.assertNotIn("tools", req)
        self.assertIn("Extraits du wiki", req["system"][0]["text"])
        conv = c.get(f"/ia/c/{done['conversation']['id']}.json").json["conversation"]
        self.assertEqual(conv["messages"][1]["sources"][0]["titre"], "Ohm")
        # Commande « Interroge-moi » : chat normal, avec la page du wiki en contexte (résolveur du wiki)
        self.fake.replies.append(claude_msg(["Première question : que vaut U ?"]))
        tuteur = next(x for x in client_config()["commands"] if x["id"] == "tuteur")
        r = self.post_json(c, "/ia/chat", {"message": tuteur["prompt"], "command": "tuteur", "path": "/wiki/Cours/Ohm"})
        self.assertEqual(parse_sse(r.get_data(as_text=True))[-1][0], "done")
        prompt = self.fake.requests[1]["messages"][-1]["content"]
        self.assertIn("La loi d'Ohm relie", prompt)
        self.assertIn("pose-moi une seule question à la fois", prompt)

    def test_pages_et_menus(self):
        self.claude()
        c = self.login()
        for url in ("/demander/", "/copie/", "/transformations/", "/wiki/Cours/Ohm", "/importer/", "/ia/"):
            self.assertEqual(c.get(url).status_code, 200, url)
        menu = c.get("/ia/").get_data(as_text=True)
        for texte in ("Demander au wiki", "Corriger ma copie", "Transformer un cours", "Travaux"):
            self.assertIn(texte, menu)
        page = c.get("/wiki/Cours/Ohm").get_data(as_text=True)
        self.assertIn('data-ai-ask="tuteur"', page)
        self.assertIn("Fiche de révision (IA)", page)
        self.assertIn("Transformer les cours (PDF, LaTeX)", c.get("/importer/").get_data(as_text=True))
        # Lot à moitié prix : proposé seulement quand les travaux partent chez Claude
        for url in ("/transformations/", "/importer/", "/qcm/importer/"):
            self.assertIn('name="lot"', c.get(url).get_data(as_text=True), url)
        from hangar.ai.models import AIProvider
        AIProvider.query.delete()
        db.session.commit()
        self.ollama(self.server())
        for url in ("/transformations/", "/importer/", "/qcm/importer/"):
            texte = c.get(url).get_data(as_text=True)
            self.assertNotIn('name="lot"', texte, url)
        self.assertIn("Transformer les cours (PDF, LaTeX)", c.get("/importer/").get_data(as_text=True))
        # Sans fournisseur : pas de boutons d'IA
        AIProvider.query.delete()
        db.session.commit()
        page = c.get("/wiki/Cours/Ohm").get_data(as_text=True)
        self.assertNotIn('data-ai-ask="tuteur"', page)
        self.assertNotIn("Générer un QCM", page)


if __name__ == "__main__":
    unittest.main()
