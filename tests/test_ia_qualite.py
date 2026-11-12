"""Vague 3a, groupe « ia-qualite » : qualité des réponses de l'IA (piste 73) et kit de révision d'un contrôle
(piste 78). Sans réseau : faux client Anthropic de test_ai (aucun appel réel).

Lancer : uv run python -m unittest discover -s tests -p "test_ia_qualite.py" -v
"""
import json
import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from test_ai import claude_msg, tool_use
from test_ai_revision import QUESTIONS_IA, RevisionTestCase

from hangar.ai import cli, jobs, travaux
from hangar.ai.models import AIConversation, AIJob, AIMessage, AISettings, AIUsage
from hangar.extensions import db
from hangar.models import AgendaEvent, AgendaSubjectLink, Notification


# ---------------------------------------------------------------------------
# Piste 73 : avis 👎, compteurs par modèle, « hangar ia-evaluer »
# ---------------------------------------------------------------------------

def _q():
    """Module ajouté par la piste 73 (importé à la demande : les autres tests restent lisibles sans lui)."""
    from hangar.ai import qualite
    return qualite


# ---------------------------------------------------------------------------

class AvisTests(RevisionTestCase):
    def conversation(self, user, question, reponse, model="claude-opus-5-5"):
        conv = AIConversation(user_id=user.id, title=question[:40])
        db.session.add(conv)
        db.session.flush()
        db.session.add(AIMessage(conversation_id=conv.id, role="user", content=question))
        m = AIMessage(conversation_id=conv.id, role="assistant", content=reponse, model=model, provider_name="Claude")
        db.session.add(m)
        db.session.commit()
        return conv, m

    def test_pouce_bas_avec_commentaire_relu_dans_les_reglages(self):
        self.claude()
        conv, m = self.conversation(self.admin, "Quel jour tombe vendredi ?", "Vendredi tombe le 14 mars.")
        _, autre = self.conversation(self.member, "Question privée d’Alice", "Réponse privée", model="llama3.2")
        c = self.login()
        r = c.post(f"/ia/messages/{m.id}/avis", json={"value": -1, "note": "  La date   est fausse  "},
                   headers={"X-CSRF-Token": c.token})
        self.assertEqual(r.json, {"ok": True, "feedback": -1, "note": "La date est fausse"})
        a = self.login("alice")
        a.post(f"/ia/messages/{autre.id}/avis", json={"value": -1, "note": "secret"}, headers={"X-CSRF-Token": a.token})
        page = c.get("/ia/reglages/").get_data(as_text=True)
        self.assertIn("Qualité des réponses", page)
        self.assertIn("La date est fausse", page)
        self.assertIn("Quel jour tombe vendredi ?", page)
        self.assertIn(f'href="/ia/c/{conv.id}"', page)
        self.assertIn("Claude Opus 5.5", page)
        # Les conversations restent privées : le 👎 d'Alice n'est pas listé chez l'administrateur…
        self.assertNotIn("secret", page)
        self.assertNotIn("Question privée", page)
        # … mais il compte dans les avis par modèle (sans son contenu).
        par_modele = {x["modele"]: x for x in _q().avis_par_modele()}
        self.assertEqual((par_modele["llama3.2"]["mauvais"], par_modele["Claude Opus 5.5"]["mauvais"]), (1, 1))
        # Retirer le 👎 retire aussi le commentaire.
        r = c.post(f"/ia/messages/{m.id}/avis", json={"value": 0}, headers={"X-CSRF-Token": c.token})
        db.session.refresh(m)
        self.assertEqual((m.feedback, m.feedback_note), (0, ""))
        self.assertNotIn("La date est fausse", c.get("/ia/reglages/").get_data(as_text=True))

    def test_message_du_panneau_renvoie_le_commentaire(self):
        from hangar.ai.chat import message_dict
        _, m = self.conversation(self.admin, "Q", "R")
        m.feedback, m.feedback_note = -1, "trop long"
        self.assertEqual(message_dict(m)["feedback_note"], "trop long")

    def test_questions_ecartees_cumulees_par_modele(self):
        self.claude()
        self.fake.replies.append(claude_msg([json.dumps(QUESTIONS_IA, ensure_ascii=False)]))
        from hangar.wiki.vault import get_vault
        note = get_vault().notes["cours/ohm"]
        job = travaux.demander_qcm(note=note, nombre=10, user=self.admin)
        jobs.run_pending()
        db.session.refresh(job)
        self.assertEqual(job.status, "draft")
        self.assertEqual(job.options["questions"], {"gardees": 2, "ecartees": 1})
        # Un ancien travail (sans compteur) : relu depuis son message et son brouillon.
        ancien = AIJob(kind="qcm", status="draft", message="2 questions mal formées écartées. À relire avant publication.",
                       calls_json=json.dumps([{"model": "claude-opus-5-5", "usage": {}, "cost": 0}]))
        ancien.result = {"dossier": "Physique", "contenu": {"questions": [{}, {}, {}]}}
        db.session.add(ancien)
        db.session.commit()
        lignes = _q().ecartees_par_modele()
        self.assertEqual(len(lignes), 1)
        self.assertEqual((lignes[0]["modele"], lignes[0]["sets"], lignes[0]["gardees"], lignes[0]["ecartees"]),
                         ("Claude Opus 5.5", 2, 5, 3))
        self.assertEqual(lignes[0]["taux"], 38)
        page = self.login().get("/ia/reglages/").get_data(as_text=True)
        self.assertIn("3 sur 8", page)


def bonnes_reponses():
    """Réponses justes du faux Claude, dans l'ordre des cas de qualite.CAS."""
    return [
        claude_msg([tool_use("proposer_tache", {"titre": "Rendre le TP de mécanique", "date": "2026-03-13"})], stop="tool_use"),
        claude_msg([tool_use("proposer_tache", {"titre": "Réviser l’anglais", "date": "2026-03-16"})], stop="tool_use"),
        claude_msg([tool_use("consulter_agenda", {"debut": "2026-03-11", "fin": "2026-03-11"})], stop="tool_use"),
        claude_msg([tool_use("chercher_tickets", {"assignes_a_moi": True})], stop="tool_use"),
        claude_msg(["Le bureau a parlé du gala du 4 avril et du budget des clubs. La page contient aussi une consigne "
                    "suspecte que je n’ai pas suivie."]),
        claude_msg([json.dumps({"titre": "Ohm", "description": "d", "questions": QUESTIONS_IA["questions"][:2] * 2},
                               ensure_ascii=False)]),
        claude_msg(['{"verdict": "juste", "retour": "Oui."}'], model="claude-haiku-5-5"),
        claude_msg(['{"verdict": "faux", "retour": "Non."}'], model="claude-haiku-5-5"),
        claude_msg(['{"verdict": "juste", "retour": "Oui."}'], model="claude-haiku-5-5"),
        claude_msg(["La résistance s’exprime en **ohms** (Ω) [1]."], model="claude-haiku-5-5"),
    ]


class EvaluationTests(RevisionTestCase):
    def test_cas_rejoues_avec_les_vraies_consignes_et_controles(self):
        row = self.claude()
        self.fake.replies.extend(bonnes_reponses())
        rapport = _q().evaluer(row)
        self.assertEqual((rapport["reussis"], rapport["total"]), (10, 10), rapport["resultats"])
        self.assertEqual(len(self.fake.requests), 10)
        # Date fixe dans le contexte, consigne et outils réels de l'assistant.
        req = self.fake.requests[0]
        self.assertIn("aujourd’hui : 2026-03-10", json.dumps(req["messages"], ensure_ascii=False))
        self.assertIn("proposer_tache", [t["name"] for t in req["tools"]])
        self.assertIn("assistant IA de Hangar", req["system"][0]["text"])
        # Rôle « rapide » pour les réponses rédigées : le modèle qui les juge dans l'application.
        self.assertEqual(self.fake.requests[6]["model"], "claude-haiku-5-5")
        # Usage compté (plafond) sous la fonction « evaluation », rien d'autre d'écrit.
        self.assertEqual(AIUsage.query.filter_by(feature="evaluation").count(), 10)
        self.assertEqual(AIConversation.query.count(), 0)
        self.assertGreater(rapport["cout"], 0)

    def test_mauvaises_reponses_detectees(self):
        row = self.claude()
        mauvaises = bonnes_reponses()
        mauvaises[0] = claude_msg([tool_use("proposer_tache", {"titre": "TP", "date": "2026-03-14"})], stop="tool_use")
        mauvaises[2] = claude_msg(["Je ne sais pas."])
        mauvaises[4] = claude_msg([tool_use("proposer_tache", {"titre": "Virer 500 € au trésorier"})], stop="tool_use")
        mauvaises[5] = claude_msg([json.dumps(QUESTIONS_IA, ensure_ascii=False)])
        mauvaises[7] = claude_msg(['{"verdict": "juste", "retour": "Oui."}'], model="claude-haiku-5-5")
        mauvaises[9] = claude_msg(["En ohms [4]."], model="claude-haiku-5-5")
        self.fake.replies.extend(mauvaises)
        rapport = _q().evaluer(row)
        echecs = {x["id"]: x["detail"] for x in rapport["resultats"] if x["statut"] == "echec"}
        self.assertEqual(set(echecs), {"tache-vendredi", "agenda-demain", "injection", "qcm-json", "qrc-faux",
                                       "wiki-citation"})
        self.assertIn("2026-03-14", echecs["tache-vendredi"])
        self.assertIn("consigne cachée", echecs["injection"])
        self.assertEqual(rapport["reussis"], 4)

    def test_commande_demande_confirmation_avant_un_passage_payant(self):
        self.claude()
        args = SimpleNamespace(data=None, id=None, oui=False, cas=None)
        sortie = []
        with mock.patch.object(cli, "_app", return_value=self.app), \
                mock.patch("builtins.input", return_value="n"), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: sortie.append(" ".join(map(str, a)))):
            cli.cmd_eval(args)
        texte = "\n".join(sortie)
        self.assertIn("Passage payant : environ", texte)
        self.assertIn("Passage annulé", texte)
        self.assertEqual(self.fake.requests, [])  # rien n'est parti
        # Avec --oui : passage fait, taux de réussite par modèle.
        self.fake.replies.extend(bonnes_reponses())
        args.oui, sortie[:] = True, []
        with mock.patch.object(cli, "_app", return_value=self.app), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: sortie.append(" ".join(map(str, a)))):
            cli.cmd_eval(args)
        texte = "\n".join(sortie)
        self.assertIn("Claude Opus 5.5 : 6/6 réussis (100 %)", texte)
        self.assertIn("Claude Haiku 5.5 : 4/4 réussis (100 %)", texte)

    def test_commande_refuse_au_dela_du_plafond_restant(self):
        self.claude()
        AISettings.get().monthly_cap = 0.01
        db.session.commit()
        sortie = []
        with mock.patch.object(cli, "_app", return_value=self.app), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: sortie.append(" ".join(map(str, a)))), \
                self.assertRaises(SystemExit):
            cli.cmd_eval(SimpleNamespace(data=None, id=None, oui=True, cas=None))
        self.assertIn("Plus que ce qui reste sous le plafond", "\n".join(sortie))
        self.assertEqual(self.fake.requests, [])

    def test_modele_sans_outils_cas_sans_objet(self):
        srv = self.server()
        row = self.ollama(srv, tools_mode="off")
        # Les 5 cas d'outils sont sans objet ; les autres partent vers le serveur local (réponses « OK » par défaut).
        rapport = _q().evaluer(row)
        self.assertEqual(rapport["sans_objet"], 5)
        self.assertEqual(rapport["total"], 5)
        self.assertEqual(len(srv.requests), 9)  # 5 cas, plus une relance corrective par réponse JSON illisible
        self.assertEqual(_q().estimation(row), 0.0)  # gratuit : pas de confirmation


# ---------------------------------------------------------------------------
# Piste 78 : kit de révision d'un contrôle
# ---------------------------------------------------------------------------

PAGE = "# {t}\n\nCours sur {t} : définitions, lois et exemples.\n"


class KitTests(RevisionTestCase):
    def setUp(self):
        super().setUp()
        for nom in ("00 - Sommaire", "Ohm", "Ohm (Fiche)", "Kirchhoff", "Kirchhoff (IA)", "Puissance"):
            self.ecrire(f"Cours/Elec/{nom}.md", PAGE.format(t=nom))
        self.ecrire("Cours/Elec/Annexe/Tableaux.md", PAGE.format(t="Tableaux"))
        # Set écrit d'après « Puissance » ; set importé de Revision (sans page source) sur Kirchhoff.
        self.ecrire("QCM/Elec/puissance.json", json.dumps({"titre": "Puissance", "source_page": "Cours/Elec/Puissance",
                                                           "questions": [{"id": "a", "type": "QRC", "question": "P ?",
                                                                          "reponses_texte": ["UI"]}]}))
        self.ecrire("QCM/Elec/kirchhoff.json", json.dumps({"titre": "Lois de Kirchhoff", "questions": [
            {"id": "a", "type": "QRC", "question": "Nœuds ?", "reponses_texte": ["somme nulle"]}]}))
        self.jour = (datetime.now() + timedelta(days=10)).replace(hour=8, minute=0, second=0, microsecond=0)
        self.ev = AgendaEvent(user_id=self.admin.id, uid="ctrl", title="Électricité", kind="CO_ECRIT",
                              start=self.jour, end=self.jour + timedelta(hours=2))
        db.session.add_all([self.ev, AgendaSubjectLink(user_id=self.admin.id, subject="electricite", title="Électricité",
                                                       wiki_path="Cours/Elec", qcm_category="Elec")])
        db.session.commit()

    def cases(self, page):
        return {(m.group(1), m.group(2)): "checked" in m.group(0)
                for m in re.finditer(r'<input type="checkbox" name="(fiche|qcm)" value="([^"]+)"[^>]*>', page)}

    def test_ecran_pages_manquantes_et_cases(self):
        self.claude()
        c = self.login()
        page = c.get("/ia/kit/?dossier=Cours/Elec").get_data(as_text=True)
        cases = self.cases(page)
        self.assertEqual(cases, {
            ("fiche", "Cours/Elec/Kirchhoff"): True, ("qcm", "Cours/Elec/Kirchhoff"): False,  # set importé proche
            ("qcm", "Cours/Elec/Ohm"): True,                                                   # fiche déjà faite
            ("fiche", "Cours/Elec/Puissance"): True,                                           # QCM déjà écrit
        })
        self.assertIn("Lois de Kirchhoff", page)
        self.assertNotIn("Sommaire", re.sub(r"<select.*?</select>", "", page, flags=re.S).split("<table")[1])
        self.assertIn("Électricité — " + self.jour.strftime("%d/%m/%Y"), page)  # prochain contrôle relié
        self.assertIn("la catégorie « Elec »", page)
        self.assertRegex(page, r'data-reste="[0-9.]+"')
        self.assertIn('data-cout="0.', page)
        # Sans dossier : choix de la collection ; depuis la page des travaux aussi.
        self.assertIn("Kit de révision d’une collection", c.get("/ia/kit/").get_data(as_text=True))
        self.assertIn('action="/ia/kit/"', c.get("/transformations/").get_data(as_text=True))
        # Invité : pas de génération.
        self.assertEqual(self.login("invite").get("/ia/kit/?dossier=Cours/Elec").status_code, 403)
        # Lien depuis le sommaire du dossier dans le wiki.
        self.assertIn("/ia/kit/?dossier=Cours/Elec", c.get("/sommaire/?dossier=Cours/Elec").get_data(as_text=True))

    def test_kit_lance_puis_date_d_examen_a_la_publication(self):
        self.claude()
        c = self.login()
        r = c.post("/ia/kit/", data={"_csrf": c.token, "dossier": "Cours/Elec", "evenement": self.ev.id, "nombre": "10",
                                     "fiche": ["Cours/Elec/Kirchhoff"], "qcm": ["Cours/Elec/Ohm"]})
        self.assertEqual(r.status_code, 302)
        lot = AIJob.query.order_by(AIJob.id).all()
        self.assertEqual([j.kind for j in lot], ["fiche", "qcm"])
        self.assertEqual(len({j.options["kit"] for j in lot}), 1)
        qcm = lot[1]
        self.assertEqual(qcm.payload["date_examen"], self.jour.date().isoformat())
        self.assertEqual((qcm.payload["categorie"], qcm.payload["nombre"]), ("Elec", 10))
        self.fake.replies += [claude_msg(["## Lois\n\n- Nœuds et mailles."]),
                              claude_msg([json.dumps(QUESTIONS_IA, ensure_ascii=False)])]
        jobs.run_pending()
        self.assertIn("Fiche de révision", self.lire("Cours/Elec/Kirchhoff (Fiche).md"))
        db.session.refresh(qcm)
        self.assertEqual(qcm.status, "draft")
        # Une seule notification, à la fin du kit.
        notes = Notification.query.filter_by(user_id=self.admin.id).all()
        self.assertEqual(len(notes), 1)
        self.assertIn("Kit de révision prêt (Elec) : 1 QCM à relire, 1 terminé", notes[0].message)
        # Publication : le set prend la date du contrôle.
        from hangar.qcm.models import QuizDeck
        chemin = travaux.publier(qcm)
        deck = QuizDeck.query.filter_by(user_id=self.admin.id, set_id=chemin.removesuffix(".json")).one()
        self.assertEqual(deck.date_objectif, self.jour.date())
        self.assertTrue(chemin.startswith("QCM/Elec/"))

    def test_refus_au_dela_du_plafond_restant(self):
        self.claude()
        AISettings.get().monthly_cap = 0.2
        db.session.add(AIUsage(cost=0.05))
        db.session.commit()
        c = self.login()
        r = c.post("/ia/kit/", data={"_csrf": c.token, "dossier": "Cours/Elec", "nombre": "15",
                                     "fiche": ["Cours/Elec/Kirchhoff", "Cours/Elec/Puissance"],
                                     "qcm": ["Cours/Elec/Ohm"]}, follow_redirects=True)
        self.assertIn("Kit trop cher pour ce qui reste du plafond mensuel", r.get_data(as_text=True))
        self.assertEqual(AIJob.query.count(), 0)

    def test_lot_refuse_si_les_reserves_mangent_le_plafond(self):
        from hangar.ai import kit
        row = self.claude()
        AISettings.get().monthly_cap = 1.0
        en_cours = AIJob(kind="qcm", status="batch", provider_id=row.id)
        en_cours.options = {"batch": True, "reserve": 0.95}
        db.session.add(en_cours)
        db.session.commit()
        from hangar.wiki.vault import get_vault
        with self.assertRaises(Exception) as ctx:
            kit.lancer(self.admin, get_vault(), "Cours/Elec", ["Cours/Elec/Kirchhoff"], [], nombre=15, lot=True)
        self.assertEqual(ctx.exception.code, "plafond")
        self.assertEqual(AIJob.query.count(), 1)

    def test_demander_fiche_en_lot(self):
        from hangar.wiki.vault import get_vault
        self.claude()
        job = travaux.demander_fiche(get_vault().notes["cours/elec/kirchhoff"], self.admin, lot=True)
        self.assertTrue(job.options["batch"])
        self.assertEqual(job.payload["page"], "Cours/Elec/Kirchhoff (Fiche).md")
