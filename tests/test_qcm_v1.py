"""QCM, corrections de la vague 1 :

- piste 3 : « Ajuster auto. » quand l'examen est trop proche pour tout savoir (statut « impossible ») :
  les nouvelles questions restantes sont étalées jusqu'à la veille de l'examen, sans limite de révisions ;
- piste 8 : un set déplacé ou renommé (dans Hangar ou hors de Hangar) garde sa progression, et une
  séance faite hors ligne pendant ce temps n'est pas perdue.

Lancer : uv run python -m unittest tests.test_qcm_v1 -v
Données synthétiques uniquement.
"""
import json
import math
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hangar import heure as horloge  # heure de l'agenda (Paris), pas celle de la machine
from hangar import create_app
from hangar.extensions import db
from hangar.models import User
from hangar.qcm import planning, quiz, services
from hangar.qcm.models import Explication, QuizDeck, QuizProgress, Signalement

SET_ELEC = {
    "titre": "Électricité",
    "questions": [
        {"id": "ohm", "type": "QCM", "question": "Loi d'Ohm : U = ?", "choix": ["RI", "R/I"], "reponses": [0]},
        {"id": "pairs", "type": "QCR", "question": "Nombres pairs ?", "choix": ["2", "3", "4"], "reponses": [0, 2]},
        {"id": "trou", "type": "TROU", "question": "Unité de la tension : ___", "reponses_texte": ["volt"]},
        {"id": "carte", "type": "FLASHCARD", "question": "Puissance", "verso": "P = UI"},
    ],
}
SET_MECA = {
    "titre": "Mécanique",
    "questions": [
        {"type": "FLASHCARD", "question": "Deuxième loi de Newton", "verso": "F = ma"},
        {"type": "FLASHCARD", "question": "Énergie cinétique", "verso": "Ec = mv²/2"},
    ],
}
LANGUE = {
    "langue": "Anglais", "titre": "Cuisine",
    "vocabulaire": [
        {"id": "v1", "mot": "kitchen", "traductions": ["cuisine"]},
        {"id": "v2", "mot": "oven", "traductions": ["four"]},
    ],
}


def _set_neuf(n, set_id="QCM/Examen/gros"):
    questions = [quiz.Question(id=f"q{i}", type="FLASHCARD", enonce=f"Question {i}", verso="R") for i in range(n)]
    return quiz.QuestionSet(id=set_id, titre="Gros set", description="", questions=questions, categorie="Examen")


# ---------------------------------------------------------------------------
# Piste 3 : quota automatique quand l'examen est trop proche
# ---------------------------------------------------------------------------
class QuotaAutoExamenProcheTests(unittest.TestCase):
    NOW = datetime(2026, 11, 12, 10, 0)

    def _deck(self, jours, nouvelles=10, revisions=None, auto=True):
        return QuizDeck(user_id=1, set_id="QCM/Examen/gros", nouvelles_jour=nouvelles, revisions_jour=revisions,
                        auto_quota=auto, date_objectif=self.NOW.date() + timedelta(days=jours))

    def test_examen_dans_une_semaine_toutes_les_questions_vues_avant_la_veille(self):
        qset = _set_neuf(150)
        deck = self._deck(7, revisions=40)
        e = planning.estimer(qset, {}, deck, self.NOW)
        self.assertEqual(e["statut"], "impossible")
        # 150 nouvelles sur les 6 jours qui précèdent la veille (gardée pour les révisions) : 25 par jour,
        # révisions sans limite. Avant : le quota saisi à la main (10 par jour, 40 révisions).
        self.assertEqual(planning.quotas_effectifs(qset, {}, deck, self.NOW), (25, None))
        self.assertEqual(e["nouvelles_conseillees"], 25)

    def test_pas_de_falaise_a_l_approche_de_l_examen(self):
        qset = _set_neuf(150)
        precedent = None
        for jours in range(11, 0, -1):
            deck = self._deck(jours)
            e = planning.estimer(qset, {}, deck, self.NOW)
            n, r = planning.quotas_effectifs(qset, {}, deck, self.NOW)
            self.assertIsNone(r)
            self.assertGreaterEqual(n, 10, f"J-{jours}")
            if e["statut"] == "impossible":
                # Assez de nouvelles chaque jour pour toutes les voir avant la veille de l'examen.
                self.assertGreaterEqual(n * max(1, jours - 1), 150, f"J-{jours}")
                if precedent is not None and precedent[0] == "impossible":
                    self.assertGreaterEqual(n, precedent[1], f"J-{jours} : le quota ne baisse pas en approchant")
            precedent = (e["statut"], n)
        # La veille de l'examen : tout ce qui reste.
        self.assertEqual(planning.quotas_effectifs(qset, {}, self._deck(1), self.NOW), (150, None))

    def test_quota_stable_dans_la_journee_et_reglages_respectes(self):
        qset = _set_neuf(150)
        deck = self._deck(7)
        # 10 nouvelles déjà découvertes aujourd'hui : le quota du jour ne fond pas (toujours 25).
        prog = {}
        for i in range(10):
            p = QuizProgress(user_id=1, set_id=qset.id, question_id=f"q{i}")
            p.record(True, quand=self.NOW - timedelta(hours=1))
            prog[p.question_id] = p
        self.assertEqual(planning.quotas_effectifs(qset, prog, deck, self.NOW), (25, None))
        # Un quota saisi plus haut que le minimum est gardé.
        self.assertEqual(planning.quotas_effectifs(qset, {}, self._deck(7, nouvelles=40), self.NOW), (40, None))
        # Nouvelles déjà sans limite : elles le restent.
        self.assertEqual(planning.quotas_effectifs(qset, {}, self._deck(7, nouvelles=None), self.NOW), (None, None))
        # Case décochée : les quotas saisis.
        self.assertEqual(planning.quotas_effectifs(qset, {}, self._deck(7, revisions=40, auto=False), self.NOW),
                         (10, 40))
        # Examen passé : les quotas saisis.
        self.assertEqual(planning.quotas_effectifs(qset, {}, self._deck(0, revisions=40), self.NOW), (10, 40))

    def test_file_du_jour_suit_le_quota_ajuste(self):
        qset = _set_neuf(60)
        deck = self._deck(4)  # 60 nouvelles en 3 jours : 20 par jour
        file, detail = services.file_du_jour(qset, {}, deck, self.NOW)
        self.assertEqual((len(file), detail["nouvelles"]), (20, 20))
        self.assertEqual(math.ceil(60 / 3), 20)


# ---------------------------------------------------------------------------
# Base commune des tests avec l'application
# ---------------------------------------------------------------------------
class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-qcm-v1-")
        self.app = create_app(self.tmp, testing=True)
        self.app.config["QCM_VERIF_INTERVALLE"] = 0  # le coffre est relu à chaque accès
        self.vault = Path(self.app.config["WIKI_DIR"])
        self._ecrire("QCM/Physique/elec.json", SET_ELEC)
        self._ecrire("QCM/Physique/meca.json", SET_MECA)
        self._ecrire("QCM/Anglais/Maison/cuisine.json", LANGUE)
        self.ctx = self.app.test_request_context("/")
        self.ctx.push()
        self.user = User(username="eleve", email="eleve@hangar.local", role="admin", first_name="Élève")
        self.user.set_password("secret1")
        self.autre = User(username="camarade", email="camarade@hangar.local", role="member")
        self.autre.set_password("secret1")
        self.invite = User(username="invite", email="invite@hangar.local", role="guest")
        self.invite.set_password("secret1")
        db.session.add_all([self.user, self.autre, self.invite])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ecrire(self, rel, data):
        p = self.vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data, encoding="utf-8")

    def client(self, login="eleve"):
        from flask import g
        g.pop("_login_user", None)  # le contexte du test est partagé : Flask-Login y garde le dernier compte
        c = self.app.test_client()
        page = c.get("/login").get_data(as_text=True)
        self.token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        r = c.post("/login", data={"login": login, "password": "secret1", "_csrf": self.token})
        self.assertEqual(r.status_code, 302)
        return c

    def lib(self, user_id=None):
        return services.bibliotheque(self.user.id if user_id is None else user_id)

    def _progresser(self, set_id, qids, user_id=None, quand=None):
        for qid in qids:
            p = QuizProgress(user_id=self.user.id if user_id is None else user_id, set_id=set_id, question_id=qid)
            p.record(True, quand=quand or horloge.now().replace(microsecond=0) - timedelta(days=2))
            db.session.add(p)
        db.session.commit()

    def _lignes(self, set_id, user_id=None):
        db.session.expire_all()
        q = QuizProgress.query.filter_by(set_id=set_id)
        if user_id is not None:
            q = q.filter_by(user_id=user_id)
        return sorted(p.question_id for p in q)

    def _synchro(self, c, reponses):
        csrf = c.get("/qcm/hors-ligne/catalogue/").get_json()["csrf"]
        r = c.post("/qcm/hors-ligne/synchro/", data=json.dumps({"reponses": reponses}),
                   content_type="application/json", headers={"X-CSRF-Token": csrf})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    @staticmethod
    def _reponse(uid, set_id, qid="ohm", jours=1):
        return {"uid": uid, "set_id": set_id, "question_id": qid, "correct": True,
                "date": (datetime.now(timezone.utc) - timedelta(days=jours)).isoformat()}


class PlanningPageTests(AppTestCase):
    def test_page_planning_et_badge_disent_que_l_ajustement_est_actif(self):
        deck = QuizDeck(user_id=self.user.id, set_id="QCM/Physique/elec", nouvelles_jour=1, auto_quota=True,
                        date_objectif=horloge.today() + timedelta(days=3))
        db.session.add(deck)
        db.session.commit()
        c = self.client()
        page = c.get("/qcm/planning/").get_data(as_text=True)
        # 4 questions neuves, examen dans 3 jours : 2 par jour pour les voir toutes avant la veille.
        self.assertIn("Ajustement automatique actif", page)
        self.assertIn("2 nouvelles par jour", page)
        self.assertIn("avant la veille de l’examen", page)
        self.assertIn("ajustement auto.", c.get("/qcm/aujourdhui/").get_data(as_text=True))
        # Sans la case : le conseil, sans l'ajustement.
        QuizDeck.query.filter_by(set_id="QCM/Physique/elec").one().auto_quota = False
        db.session.commit()
        page = c.get("/qcm/planning/").get_data(as_text=True)
        self.assertNotIn("Ajustement automatique actif", page)
        self.assertIn("au moins <b>2 nouvelles par jour</b>", page)


# ---------------------------------------------------------------------------
# Piste 8 : sets déplacés ou renommés
# ---------------------------------------------------------------------------
class RenommageHorsHangarTests(AppTestCase):
    def test_dossier_renomme_dans_l_explorateur_la_progression_suit(self):
        self._progresser("QCM/Physique/elec", ["ohm", "pairs"])
        self._progresser("QCM/Physique/elec", ["ohm"], user_id=self.autre.id)
        db.session.add_all([
            QuizDeck(user_id=self.user.id, set_id="QCM/Physique/elec", nouvelles_jour=5,
                     date_objectif=horloge.today() + timedelta(days=30)),
            Explication(set_id="QCM/Physique/elec", question_id="ohm", texte="Parce que."),
            Signalement(set_id="QCM/Physique/elec", question_id="pairs", user_id=self.user.id, commentaire="?"),
        ])
        db.session.commit()
        self.assertEqual(services.resume_set(self.lib().get("QCM/Physique/elec"),
                                             QuizProgress.map_for(self.user.id, "QCM/Physique/elec"),
                                             quiz.maintenant())["rencontrees"], 2)
        # Renommage du dossier hors de Hangar (Obsidian, explorateur de fichiers).
        (self.vault / "QCM/Physique").rename(self.vault / "QCM/Physique-S1")
        lib = self.lib()
        self.assertIn("QCM/Physique-S1/elec", lib.sets)
        self.assertEqual(self._lignes("QCM/Physique-S1/elec", self.user.id), ["ohm", "pairs"])
        self.assertEqual(self._lignes("QCM/Physique-S1/elec", self.autre.id), ["ohm"])
        self.assertEqual(self._lignes("QCM/Physique/elec"), [])
        deck = QuizDeck.query.filter_by(user_id=self.user.id, set_id="QCM/Physique-S1/elec").one()
        self.assertEqual((deck.nouvelles_jour, deck.date_objectif), (5, horloge.today() + timedelta(days=30)))
        self.assertEqual(Explication.query.filter_by(set_id="QCM/Physique-S1/elec").count(), 1)
        self.assertEqual(Signalement.query.filter_by(set_id="QCM/Physique-S1/elec").count(), 1)
        c = self.client()
        page = c.get("/qcm/stats/").get_data(as_text=True)
        self.assertIn("2/4", page)  # vues / total
        self.assertNotIn("Progression sans fichier", page)

    def test_fichier_renomme_et_un_peu_modifie_la_progression_suit(self):
        self._progresser("QCM/Physique/elec", ["ohm", "pairs", "trou"])
        self.lib()  # empreintes connues
        (self.vault / "QCM/Physique/elec.json").unlink()
        modifie = json.loads(json.dumps(SET_ELEC))
        modifie["questions"][3]["verso"] = "P = U × I"  # une coquille corrigée au passage
        modifie["titre"] = "Électricité (S1)"
        self._ecrire("QCM/Électricité/electricite.json", modifie)
        self.assertIn("QCM/Électricité/electricite", self.lib().sets)
        self.assertEqual(self._lignes("QCM/Électricité/electricite"), ["ohm", "pairs", "trou"])

    def test_copie_puis_suppression_puis_retour_a_l_ancien_nom(self):
        self._progresser("QCM/Physique/elec", ["ohm"])
        self.lib()
        # Copie dans un autre dossier (vue par Hangar entre-temps), puis suppression de l'original.
        self._ecrire("QCM/Electro/elec.json", SET_ELEC)
        self.lib()
        (self.vault / "QCM/Physique/elec.json").unlink()
        self.lib()
        self.assertEqual(self._lignes("QCM/Electro/elec"), ["ohm"])
        # Retour à l'ancien nom : la progression revient.
        (self.vault / "QCM/Electro/elec.json").rename(self.vault / "QCM/Physique/elec.json")
        self.lib()
        self.assertEqual(self._lignes("QCM/Physique/elec"), ["ohm"])
        from hangar.qcm.models import AliasSet
        self.assertEqual({(a.ancien, a.nouveau) for a in AliasSet.query}, {("QCM/Electro/elec", "QCM/Physique/elec")})

    def test_seule_la_casse_change_progression_d_avant_la_mise_a_jour(self):
        # Progression enregistrée sous l'ancien nom, avant que Hangar ne garde les empreintes.
        self._progresser("QCM/physique/elec", ["ohm"])
        self.assertIn("QCM/Physique/elec", self.lib().sets)
        self.assertEqual(self._lignes("QCM/Physique/elec"), ["ohm"])
        self.assertEqual(self._lignes("QCM/physique/elec"), [])

    def test_copie_identique_ambigue_rattachement_a_la_main(self):
        self._progresser("QCM/Physique/elec", ["ohm"])
        self.lib()
        (self.vault / "QCM/Physique/elec.json").unlink()
        self._ecrire("QCM/A/elec.json", SET_ELEC)
        self._ecrire("QCM/B/elec.json", SET_ELEC)
        self.lib()
        # Deux candidats identiques : rien n'est rattaché tout seul…
        self.assertEqual(self._lignes("QCM/Physique/elec"), ["ohm"])
        c = self.client()
        self.assertIn("1 progression sans fichier", c.get("/qcm/").get_data(as_text=True))
        page = c.get("/qcm/stats/").get_data(as_text=True)
        self.assertIn("Progression sans fichier", page)
        self.assertIn("QCM/Physique/elec", page)
        # … l'élève choisit.
        r = c.post("/qcm/progression-orpheline/", data={"_csrf": self.token, "action": "rattacher",
                                                       "ancien": "QCM/Physique/elec", "nouveau": "QCM/B/elec"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._lignes("QCM/B/elec"), ["ohm"])
        self.assertNotIn("Progression sans fichier", c.get("/qcm/stats/").get_data(as_text=True))

    def test_oublier_une_progression_sans_fichier(self):
        self._progresser("QCM/Physique/elec", ["ohm"])
        self._progresser("QCM/Physique/elec", ["ohm"], user_id=self.autre.id)
        self.lib()
        (self.vault / "QCM/Physique/elec.json").unlink()
        c = self.client()
        r = c.post("/qcm/progression-orpheline/", data={"_csrf": self.token, "action": "oublier",
                                                       "ancien": "QCM/Physique/elec"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._lignes("QCM/Physique/elec", self.user.id), [])
        self.assertEqual(self._lignes("QCM/Physique/elec", self.autre.id), ["ohm"])  # la sienne seulement
        # Un set qui existe encore ne s'oublie pas par là.
        r = c.post("/qcm/progression-orpheline/", data={"_csrf": self.token, "action": "oublier",
                                                       "ancien": "QCM/Physique/meca"})
        self.assertEqual(r.status_code, 404)

    def test_invite_ne_rattache_que_sa_propre_progression(self):
        self._progresser("QCM/Physique/elec", ["ohm"], user_id=self.invite.id)
        self._progresser("QCM/Physique/elec", ["pairs"])
        self.lib()
        (self.vault / "QCM/Physique/elec.json").unlink()
        self._ecrire("QCM/A/elec.json", SET_ELEC)
        self._ecrire("QCM/B/elec.json", SET_ELEC)
        c = self.client("invite")
        self.assertIn("Progression sans fichier", c.get("/qcm/stats/").get_data(as_text=True))
        r = c.post("/qcm/progression-orpheline/", data={"_csrf": self.token, "action": "rattacher",
                                                       "ancien": "QCM/Physique/elec", "nouveau": "QCM/B/elec"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._lignes("QCM/B/elec"), ["ohm"])                           # la sienne
        self.assertEqual(self._lignes("QCM/Physique/elec", self.user.id), ["pairs"])   # pas celle des autres
        from hangar.qcm.models import AliasSet
        # Ancien nom gardé pour lui seul (ses réponses hors ligne), pas pour tous les comptes.
        self.assertEqual(AliasSet.query.filter_by(ancien="QCM/Physique/elec", user_id=None).count(), 0)
        self.assertEqual(AliasSet.query.filter_by(ancien="QCM/Physique/elec", user_id=self.invite.id).one().nouveau,
                         "QCM/B/elec")
        # Un admin, lui, rattache pour tout le monde (avec l'ancien nom, pour les réponses hors ligne).
        c = self.client()
        c.post("/qcm/progression-orpheline/", data={"_csrf": self.token, "action": "rattacher",
                                                   "ancien": "QCM/Physique/elec", "nouveau": "QCM/A/elec"})
        self.assertEqual(self._lignes("QCM/A/elec"), ["pairs"])
        self.assertEqual(AliasSet.query.filter_by(ancien="QCM/Physique/elec", user_id=None).one().nouveau,
                         "QCM/A/elec")


class AppariementTests(unittest.TestCase):
    """Rapprochement d'un set disparu et d'un set apparu, d'après leur contenu (sans base)."""

    def test_apparier(self):
        from hangar.qcm.rattachement import apparier
        abc = {"a", "b", "c"}
        # Contenu identique : apparié ; un set personnel ne passe pas dans l'espace d'un autre compte.
        self.assertEqual(apparier({"QCM/A/x": ("e1", abc, False), "~1/Mes sets/y": ("e2", {"d"}, False)},
                                  {"QCM/B/x": ("e1", abc, False), "~2/Mes sets/y": ("e2", {"d"}, False)}),
                         [("QCM/A/x", "QCM/B/x")])
        # Presque identique (3 questions sur 4) : apparié ; un paquet de langue n'est pas un set de QCM.
        self.assertEqual(apparier({"QCM/A/x": ("e1", abc, False)}, {"QCM/C/z": ("e3", abc | {"f"}, False)}),
                         [("QCM/A/x", "QCM/C/z")])
        self.assertEqual(apparier({"QCM/A/x": ("e1", abc, False)}, {"QCM/C/z": ("e3", abc | {"f"}, True)}), [])
        # Trop différent, ou deux candidats aussi proches : rien.
        self.assertEqual(apparier({"QCM/A/x": ("e1", abc, False)}, {"QCM/C/z": ("e3", {"a", "f", "g"}, False)}), [])
        self.assertEqual(apparier({"QCM/A/x": ("e1", abc, False)},
                                  {"QCM/C/z": ("e3", abc | {"f"}, False), "QCM/D/z": ("e4", abc | {"g"}, False)}), [])
        self.assertEqual(apparier({"QCM/A/x": ("e1", abc, False)},
                                  {"QCM/C/x": ("e1", abc, False), "QCM/D/x": ("e1", abc, False)}), [])

    def test_rattachement_global(self):
        from hangar.qcm.rattachement import rattachement_global
        self.assertTrue(rattachement_global("QCM/A/x", "QCM/B/x", 3, True))
        self.assertFalse(rattachement_global("QCM/A/x", "QCM/B/x", 3, False))       # invité : sa progression
        self.assertTrue(rattachement_global("~3/Mes sets/x", "~3/Mes sets/y", 3, False))
        self.assertFalse(rattachement_global("QCM/A/x", "~3/Mes sets/x", 3, True))  # d'un espace à l'autre


class DeplacerTests(AppTestCase):
    def test_deplacer_vers_l_ancien_dossier_n_efface_plus_la_progression(self):
        # Progression restée sous QCM/Electro/elec (set renommé avant cette version, puis remis ailleurs).
        self._progresser("QCM/Electro/elec", ["trou", "carte"])
        self._progresser("QCM/Physique/elec", ["ohm"])
        db.session.add(Explication(set_id="QCM/Physique/elec", question_id="ohm", texte="Parce que."))
        db.session.commit()
        c = self.client()
        r = c.post("/qcm/deplacer/", data={"_csrf": self.token, "set": "QCM/Physique/elec", "dossier": "Electro"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue((self.vault / "QCM/Electro/elec.json").exists())
        # Avant : l'ancienne progression de QCM/Electro/elec était effacée pour tous les comptes.
        self.assertEqual(self._lignes("QCM/Electro/elec"), ["carte", "ohm", "trou"])
        self.assertEqual(Explication.query.filter_by(set_id="QCM/Electro/elec").count(), 1)

    def test_deplacer_fusionne_question_par_question(self):
        ancienne = horloge.now().replace(microsecond=0) - timedelta(days=10)
        self._progresser("QCM/Electro/elec", ["ohm"], quand=ancienne)
        self._progresser("QCM/Physique/elec", ["ohm"])
        c = self.client()
        c.post("/qcm/deplacer/", data={"_csrf": self.token, "set": "QCM/Physique/elec", "dossier": "Electro"})
        db.session.expire_all()
        p = QuizProgress.query.filter_by(set_id="QCM/Electro/elec", question_id="ohm").one()
        self.assertEqual((p.seen, p.correct), (2, 2))         # réponses cumulées
        self.assertEqual(p.premiere_revue, ancienne)          # première découverte gardée
        self.assertGreater(p.last_review, ancienne)           # état de la réponse la plus récente


class SynchroHorsLigneTests(AppTestCase):
    def test_reponses_d_un_set_au_fichier_invalide_gardees_sur_l_appareil(self):
        self._progresser("QCM/Physique/elec", ["pairs"])
        self._ecrire("QCM/Physique/elec.json", '{"titre": "Électricité", "questions": [')  # en cours d'écriture
        c = self.client()
        res = self._synchro(c, [self._reponse("u1", "QCM/Physique/elec"), self._reponse("u2", "QCM/Physique/elec")])
        # Avant : {"traitees": ["u1", "u2"], "enregistrees": 0} et la séance était perdue.
        self.assertEqual(res["traitees"], [])
        self.assertEqual(sorted(res["en_attente"]), ["u1", "u2"])
        self.assertEqual(self._lignes("QCM/Physique/elec"), ["pairs"])
        # Le set reste sur l'appareil (il n'est pas « supprimé du serveur »).
        data = c.get("/qcm/hors-ligne/sets/?id=QCM/Physique/elec").get_json()
        self.assertEqual(data["manquants"], [])
        self.assertEqual(data["indisponibles"], ["QCM/Physique/elec"])
        # Fichier réparé : la synchronisation suivante les enregistre.
        self._ecrire("QCM/Physique/elec.json", SET_ELEC)
        res = self._synchro(c, [self._reponse("u1", "QCM/Physique/elec")])
        self.assertEqual((res["traitees"], res["enregistrees"]), (["u1"], 1))
        self.assertEqual(self._lignes("QCM/Physique/elec"), ["ohm", "pairs"])

    def test_seance_hors_ligne_sur_un_set_renomme_entre_temps(self):
        self._progresser("QCM/Physique/elec", ["pairs"])
        c = self.client()
        c.get("/qcm/hors-ligne/sets/?id=QCM/Physique/elec")  # téléchargé sur le téléphone
        (self.vault / "QCM/Physique").rename(self.vault / "QCM/Physique-S1")  # renommé sur le PC
        res = self._synchro(c, [self._reponse("u1", "QCM/Physique/elec"),
                                self._reponse("u2", "QCM/Physique/elec", "trou")])
        self.assertEqual((sorted(res["traitees"]), res["enregistrees"]), (["u1", "u2"], 2))
        self.assertEqual(self._lignes("QCM/Physique-S1/elec"), ["ohm", "pairs", "trou"])
        # Le téléphone reprend le set sous son nouveau nom.
        data = c.get("/qcm/hors-ligne/sets/?id=QCM/Physique/elec").get_json()
        self.assertEqual([s["id"] for s in data["sets"]], ["QCM/Physique-S1/elec"])
        self.assertEqual(data["manquants"], ["QCM/Physique/elec"])
        self.assertEqual(data["renommes"], {"QCM/Physique/elec": "QCM/Physique-S1/elec"})
        # Une réponse envoyée en ligne sous l'ancien nom (page restée ouverte) est enregistrée aussi.
        r = c.post("/qcm/reponse/", json={"set_id": "QCM/Physique/elec", "question_id": "carte", "correct": True},
                   headers={"X-CSRF-Token": self.token})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._lignes("QCM/Physique-S1/elec"), ["carte", "ohm", "pairs", "trou"])

    def test_seance_hors_ligne_apres_deplacer_dans_hangar(self):
        self._progresser("QCM/Physique/elec", ["pairs"])
        c = self.client()
        c.post("/qcm/deplacer/", data={"_csrf": self.token, "set": "QCM/Physique/elec", "dossier": "Electro"})
        res = self._synchro(c, [self._reponse("u1", "QCM/Physique/elec")])
        self.assertEqual((res["traitees"], res["enregistrees"]), (["u1"], 1))
        self.assertEqual(self._lignes("QCM/Electro/elec"), ["ohm", "pairs"])

    def test_paquet_de_langue_renomme(self):
        c = self.client()
        exo = c.get("/qcm/hors-ligne/sets/?id=QCM/Anglais/Maison/cuisine").get_json()["sets"][0]["exercices"][0]["exo_id"]
        (self.vault / "QCM/Anglais/Maison").rename(self.vault / "QCM/Anglais/Cuisine")
        res = self._synchro(c, [{"uid": "l1", "set_id": "QCM/Anglais/Maison/cuisine", "exo_id": exo, "note": 3,
                                 "date": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()}])
        self.assertEqual((res["traitees"], res["enregistrees"]), (["l1"], 1))
        self.assertEqual(self._lignes("QCM/Anglais/Cuisine/cuisine"), [exo])

    def test_set_supprime_hors_hangar_reponses_gardees_jusqu_a_ce_qu_on_tranche(self):
        self._progresser("QCM/Physique/elec", ["pairs"])
        c = self.client()
        c.get("/qcm/stats/")
        (self.vault / "QCM/Physique/elec.json").unlink()
        res = self._synchro(c, [self._reponse("u1", "QCM/Physique/elec")])
        self.assertEqual((res["traitees"], res["en_attente"]), ([], ["u1"]))
        # « Oublier » sur la page des statistiques : la réponse est alors abandonnée.
        c.post("/qcm/progression-orpheline/", data={"_csrf": self.token, "action": "oublier",
                                                   "ancien": "QCM/Physique/elec"})
        res = self._synchro(c, [self._reponse("u1", "QCM/Physique/elec")])
        self.assertEqual((res["traitees"], res["enregistrees"]), (["u1"], 0))
        self.assertNotIn("en_attente", res)
        self.assertEqual(c.get("/qcm/hors-ligne/sets/?id=QCM/Physique/elec").get_json()["manquants"],
                         ["QCM/Physique/elec"])

    def test_set_supprime_dans_hangar_reponses_abandonnees(self):
        self._progresser("QCM/Physique/elec", ["pairs"])
        c = self.client()
        c.post("/qcm/supprimer/", data={"_csrf": self.token, "set": "QCM/Physique/elec"})
        res = self._synchro(c, [self._reponse("u1", "QCM/Physique/elec"), self._reponse("u2", "QCM/inconnu")])
        self.assertEqual((sorted(res["traitees"]), res["enregistrees"]), (["u1", "u2"], 0))

    def test_reponse_datee_de_plus_d_un_an_ignoree(self):
        c = self.client()
        res = self._synchro(c, [{"uid": "v", "set_id": "QCM/Physique/elec", "question_id": "ohm", "correct": True,
                                 "date": "2015-01-01T10:00:00Z"}])
        # Horloge du téléphone fausse : acquittée, mais pas enregistrée (avant : last_review en 2015).
        self.assertEqual((res["traitees"], res["enregistrees"]), (["v"], 0))
        self.assertEqual(self._lignes("QCM/Physique/elec"), [])
        recente = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        res = self._synchro(c, [{"uid": "w", "set_id": "QCM/Physique/elec", "question_id": "ohm", "correct": True,
                                 "date": recente}])
        self.assertEqual(res["enregistrees"], 1)  # un long stage sans réseau reste accepté


# ---------------------------------------------------------------------------
# Relecture des pistes 3 et 8
# ---------------------------------------------------------------------------
class RelectureQuotaAutoTests(AppTestCase):
    """Piste 3 : « Ajuster auto. » vaut aussi hors ligne, et se voit partout où « en retard » s'affiche."""

    def _gros_set(self, auto=True):
        self._ecrire("QCM/Examen/gros.json", {"titre": "Gros", "questions": [
            {"type": "FLASHCARD", "question": f"Question {i}", "verso": "R"} for i in range(150)]})
        db.session.add(QuizDeck(user_id=self.user.id, set_id="QCM/Examen/gros", nouvelles_jour=10, revisions_jour=40,
                                auto_quota=auto, date_objectif=quiz.jour(quiz.maintenant()) + timedelta(days=7)))
        db.session.commit()

    def test_appli_hors_ligne_recoit_les_quotas_ajustes(self):
        self._gros_set()
        c = self.client()
        data = c.get("/qcm/hors-ligne/sets/?id=QCM/Examen/gros").get_json()
        # Avant : les quotas saisis à la main ({10, 40}) ; le téléphone ne proposait que 10 nouvelles par jour.
        self.assertEqual(data["sets"][0]["quotas"], {"nouvelles_jour": 25, "revisions_jour": None})
        # Case décochée : les quotas saisis.
        QuizDeck.query.filter_by(set_id="QCM/Examen/gros").one().auto_quota = False
        db.session.commit()
        data = c.get("/qcm/hors-ligne/sets/?id=QCM/Examen/gros").get_json()
        self.assertEqual(data["sets"][0]["quotas"], {"nouvelles_jour": 10, "revisions_jour": 40})
        # Set sans réglages : pas de quota.
        data = c.get("/qcm/hors-ligne/sets/?id=QCM/Physique/meca").get_json()
        self.assertEqual(data["sets"][0]["quotas"], {"nouvelles_jour": None, "revisions_jour": None})

    def test_accueil_et_resume_par_e_mail_disent_que_l_ajustement_est_actif(self):
        from hangar import digest
        self._gros_set()
        c = self.client()
        page = c.get("/qcm/").get_data(as_text=True)
        examen = page[page.index('class="qcm-exams"'):]
        examen = examen[:examen.index("</div>")]
        self.assertIn("en retard", examen)
        self.assertIn("ajustement auto.", examen)  # avant : « en retard » seul
        section = services._digest_revisions(digest.Contexte(user=self.user, jour=horloge.today(), at=horloge.now()))
        self.assertIn("en retard (quotas ajustés automatiquement)", section.html)
        # Case décochée : « en retard » seul.
        QuizDeck.query.filter_by(set_id="QCM/Examen/gros").one().auto_quota = False
        db.session.commit()
        page = c.get("/qcm/").get_data(as_text=True)
        self.assertNotIn("ajustement auto.", page[page.index('class="qcm-exams"'):])
        section = services._digest_revisions(digest.Contexte(user=self.user, jour=horloge.today(), at=horloge.now()))
        self.assertIn("en retard", section.html)
        self.assertNotIn("quotas ajustés automatiquement", section.html)


class RelectureRattachementTests(AppTestCase):
    """Piste 8 : rattachement propre à un compte, « Déplacer » sur la progression d'un autre set, textes."""

    def _copies_ambigues(self):
        (self.vault / "QCM/Physique/elec.json").unlink()
        self._ecrire("QCM/A/elec.json", SET_ELEC)
        self._ecrire("QCM/B/elec.json", SET_ELEC)

    def test_invite_reponses_hors_ligne_suivent_son_rattachement(self):
        self._progresser("QCM/Physique/elec", ["ohm"], user_id=self.invite.id)
        self._progresser("QCM/Physique/elec", ["pairs"])
        self.lib()
        self._copies_ambigues()  # rattachement ambigu : laissé à chacun
        c = self.client("invite")
        res = self._synchro(c, [self._reponse("u1", "QCM/Physique/elec", "trou")])
        self.assertEqual((res["traitees"], res["en_attente"]), ([], ["u1"]))
        r = c.post("/qcm/progression-orpheline/", data={"_csrf": self.token, "action": "rattacher",
                                                       "ancien": "QCM/Physique/elec", "nouveau": "QCM/B/elec"})
        self.assertEqual(r.status_code, 302)
        # Avant : {"traitees": ["u1"], "enregistrees": 0}, la réponse était effacée du téléphone sans être gardée.
        res = self._synchro(c, [self._reponse("u1", "QCM/Physique/elec", "trou")])
        self.assertEqual((res["traitees"], res["enregistrees"]), (["u1"], 1))
        self.assertEqual(self._lignes("QCM/B/elec", self.invite.id), ["ohm", "trou"])
        # Le téléphone reprend le set sous le nom choisi.
        data = c.get("/qcm/hors-ligne/sets/?id=QCM/Physique/elec").get_json()
        self.assertEqual(data["renommes"], {"QCM/Physique/elec": "QCM/B/elec"})
        # Les autres comptes ne sont pas concernés : leurs réponses attendent leur propre choix.
        c = self.client()
        res = self._synchro(c, [self._reponse("u2", "QCM/Physique/elec", "carte")])
        self.assertEqual((res["traitees"], res["en_attente"]), ([], ["u2"]))
        self.assertEqual(self._lignes("QCM/B/elec", self.user.id), [])
        self.assertEqual(self._lignes("QCM/Physique/elec", self.user.id), ["pairs"])

    def test_membre_rattache_un_set_commun_a_son_set_perso(self):
        self._progresser("QCM/Physique/elec", ["ohm"], user_id=self.autre.id)
        self._progresser("QCM/Physique/elec", ["pairs"])
        self.lib()
        (self.vault / "QCM/Physique/elec.json").unlink()
        perso = quiz.dossier_perso(self.autre.id) / "Mes sets" / "elec2.json"
        perso.parent.mkdir(parents=True, exist_ok=True)
        perso.write_text(json.dumps(SET_ELEC | {"titre": "Elec perso"}, ensure_ascii=False), encoding="utf-8")
        perso_id = f"~{self.autre.id}/Mes sets/elec2"
        c = self.client("camarade")
        page = c.get("/qcm/stats/").get_data(as_text=True)
        # Libellés lisibles : « Mon espace/… » plutôt que l'identifiant technique « ~<compte>/… ».
        self.assertRegex(page, re.escape(f'<option value="{perso_id}"') + r"( selected)?"
                         + re.escape(">Mon espace/Mes sets/elec2 · Elec perso</option>"))
        self.assertNotRegex(page, r'<option value="[^"]*">~')
        # La bulle ne promet plus « pour tous les comptes » sans nuance.
        self.assertIn("pour tous les comptes si c’est un set commun, seulement les tiens si c’est un set de ton espace",
                      page)
        c.post("/qcm/progression-orpheline/", data={"_csrf": self.token, "action": "rattacher",
                                                   "ancien": "QCM/Physique/elec", "nouveau": perso_id})
        self.assertEqual(self._lignes(perso_id), ["ohm"])
        self.assertEqual(self._lignes("QCM/Physique/elec"), ["pairs"])  # celle des autres reste
        res = self._synchro(c, [self._reponse("u1", "QCM/Physique/elec", "trou")])
        self.assertEqual(res["enregistrees"], 1)
        self.assertEqual(self._lignes(perso_id), ["ohm", "trou"])
        # Un invité : sa progression seulement, dit sans détour.
        self._progresser("QCM/Physique/elec", ["ohm"], user_id=self.invite.id)
        page = self.client("invite").get("/qcm/stats/").get_data(as_text=True)
        self.assertIn('title="Ta progression, tes quotas et ta date d’examen passent sur ce set"', page)

    def test_deplacer_ne_verse_pas_la_progression_d_un_autre_set(self):
        maths = {"titre": "Maths chap 1", "questions": [
            {"type": "FLASHCARD", "question": f"Dérivée {i}", "verso": "x"} for i in range(5)]}
        physique = {"titre": "Physique chap 1", "questions": [
            {"type": "FLASHCARD", "question": f"Force {i}", "verso": "y"} for i in range(5)]}
        self._ecrire("QCM/Maths/chap1.json", maths)
        self._ecrire("QCM/Physique/chap1.json", physique)
        self.lib()  # empreintes connues
        for qid in ("q1", "q2", "q3"):  # identifiants par défaut, les mêmes dans les deux sets
            p = QuizProgress(user_id=self.user.id, set_id="QCM/Maths/chap1", question_id=qid)
            p.record(False, quand=horloge.now().replace(microsecond=0) - timedelta(days=3))
            db.session.add(p)
        db.session.add(Explication(set_id="QCM/Maths/chap1", question_id="q1", texte="Dérivée de x²."))
        db.session.commit()
        (self.vault / "QCM/Maths/chap1.json").unlink()  # supprimé dans Obsidian
        c = self.client()
        r = c.post("/qcm/deplacer/", data={"_csrf": self.token, "set": "QCM/Physique/chap1", "dossier": "Maths"},
                   follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("gardée à part sous « chap1 (ancien) »", r.get_data(as_text=True))
        # Avant : q1, q2, q3 « vues et ratées » dans le set de physique, jamais révisé.
        self.assertEqual(self._lignes("QCM/Maths/chap1"), [])
        self.assertEqual(Explication.query.filter_by(set_id="QCM/Maths/chap1").count(), 0)
        # Rien n'est perdu : la progression des maths reste à rattacher ou à oublier.
        self.assertEqual(self._lignes("QCM/Maths/chap1 (ancien)"), ["q1", "q2", "q3"])
        self.assertEqual(Explication.query.filter_by(set_id="QCM/Maths/chap1 (ancien)").count(), 1)
        page = c.get("/qcm/stats/").get_data(as_text=True)
        self.assertIn("Progression sans fichier", page)
        self.assertIn("chap1 (ancien)", page)
        # Le fichier de maths revient ailleurs : sa progression le retrouve toute seule (empreinte gardée).
        self._ecrire("QCM/Archives/chap1.json", maths)
        self.lib()
        self.assertEqual(self._lignes("QCM/Archives/chap1"), ["q1", "q2", "q3"])

    def test_deplacer_fusionne_si_l_empreinte_ressemble(self):
        self._progresser("QCM/Electro/elec", ["trou"])
        self._ecrire("QCM/Electro/elec.json", SET_ELEC)
        self.lib()  # empreinte de QCM/Electro/elec connue
        (self.vault / "QCM/Electro/elec.json").unlink()
        self._progresser("QCM/Physique/elec", ["ohm"])
        c = self.client()
        r = c.post("/qcm/deplacer/", data={"_csrf": self.token, "set": "QCM/Physique/elec", "dossier": "Electro"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._lignes("QCM/Electro/elec"), ["ohm", "trou"])
        self.assertEqual(QuizProgress.query.filter(QuizProgress.set_id.like("%(ancien%")).count(), 0)

    def test_message_hors_ligne_accorde_au_singulier(self):
        js = (Path(__file__).resolve().parent.parent / "hangar/static/qcm/qcm_hors_ligne.js").read_text(encoding="utf-8")
        debut = js.index("if (envoi.restantes)")
        bloc = js[debut:js.index("}", debut)]
        # Avant : « 1 réponse gardée sur l'appareil : leur set est introuvable… Elles partiront… ».
        for attendu in ('"leur" : "son"', '"Elles partiront" : "Elle partira"'):
            self.assertIn(attendu, bloc)


class CompatibiliteTests(AppTestCase):
    def test_base_existante_sans_les_nouvelles_tables(self):
        """Une base créée par la version précédente (sans les tables des empreintes et des alias) marche
        après la mise à jour, sans rien faire : les tables sont créées au démarrage."""
        import sqlite3
        self._progresser("QCM/Physique/elec", ["ohm"])
        uid = self.user.id
        chemin = Path(self.app.config["DATA_DIR"]) / "hangar.db"
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        with sqlite3.connect(chemin) as con:
            for table in ("qcm_empreinte", "qcm_alias"):
                con.execute(f'DROP TABLE IF EXISTS "{table}"')
        self.app = create_app(self.tmp, testing=True)
        self.app.config["QCM_VERIF_INTERVALLE"] = 0
        self.ctx = self.app.test_request_context("/")
        self.ctx.push()
        (self.vault / "QCM/Physique").rename(self.vault / "QCM/physique")  # casse seule
        self.assertIn("QCM/physique/elec", self.lib(uid).sets)
        self.assertEqual(self._lignes("QCM/physique/elec"), ["ohm"])


if __name__ == "__main__":
    unittest.main()
