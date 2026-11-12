"""Tests des QCM (port de Revision) : chargement des sets, FSRS, réponses et échéances, quotas et
planning, import/export (JSON, CSV, Anki), langues, image masquée, pages et résumé quotidien.

Lancer : uv run python -m unittest tests.test_qcm -v
Données synthétiques uniquement (aucun cours de l'utilisateur).
"""
import io
import json
import re
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import date, datetime, time, timedelta
from unittest import mock
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from hangar import create_app, digest
from hangar.extensions import db
from hangar.models import User
from hangar.qcm import anki, fsrs, langues, masque, paquets, planning, quiz, services, verifier
from hangar.qcm.models import Activite, QuizDeck, QuizProgress, Signalement, TempsRevision

SET_MIXTE = {
    "titre": "Électronique de base",
    "description": "Set de test",
    "questions": [
        {"id": "ohm", "type": "QCM", "question": "Loi d'Ohm : $U = ?$", "choix": ["$RI$", "$R/I$", "$I/R$"], "reponses": [0],
         "explication": "**U = RI**"},
        {"id": "pairs", "type": "QCR", "question": "Nombres pairs ?", "choix": ["2", "3", "4"], "reponses": [0, 2]},
        {"id": "trou", "type": "TROU", "question": "La capitale de la France est ___.", "reponses_texte": ["Paris"]},
        {"id": "chien", "type": "QRC", "question": "chien", "consigne": "Traduire", "reponses_texte": ["dog"], "inverse": True},
        {"id": "carte", "type": "FLASHCARD", "question": "Recto ![[schema.png|100]]", "verso": "Verso"},
    ],
}
SET_MASQUE = {
    "titre": "Schéma",
    "questions": [{"id": "s", "type": "MASQUE", "image": "schema.png", "masquer_tout": True,
                   "zones": [{"id": "a", "x": 10, "y": 10, "l": 20, "h": 20, "reponse": "Zone A"},
                             {"id": "b", "x": 50, "y": 50, "l": 20, "h": 20, "reponse": "Zone B"}]}],
}
LANGUE = {
    "langue": "Anglais", "titre": "Cuisine",
    "vocabulaire": [
        {"id": "v1", "mot": "kitchen", "traductions": ["cuisine"], "exemple": "The [kitchen] is clean."},
        {"id": "v2", "mot": "oven", "traductions": ["four"], "exemple": "Preheat the [oven]."},
        {"id": "v3", "mot": "sink", "traductions": ["évier"]},
    ],
    "conjugaison": [{"id": "be", "verbe": "to be", "temps": "présent", "formes": {"I": "am", "you": "are", "he": "is"}}],
    "grammaire": [
        {"id": "g1", "type": "ORDRE", "phrase": "I am very happy."},
        {"id": "g2", "type": "CORRECTION", "phrase": "He go home.", "corrections": ["He goes home."]},
        {"id": "g3", "type": "QCM", "question": "She ___ tall.", "choix": ["is", "are"], "reponses": [0]},
    ],
}


def _png(couleur="red", taille=(40, 30)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", taille, couleur).save(buf, "PNG")
    return buf.getvalue()


class QcmTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-qcm-")
        self.app = create_app(self.tmp, testing=True)
        self.app.config["QCM_VERIF_INTERVALLE"] = 0  # le coffre est relu à chaque accès
        self.vault = Path(self.app.config["WIKI_DIR"])
        self._ecrire("QCM/Physique/elec.json", SET_MIXTE)
        self._ecrire("QCM/Physique/schema-set.json", SET_MASQUE)
        self._ecrire("QCM/Anglais/Maison/cuisine.json", LANGUE)
        (self.vault / "Images").mkdir(parents=True, exist_ok=True)
        (self.vault / "Images" / "schema.png").write_bytes(_png())
        (self.vault / "Notes.md").write_text("# Pas un set", encoding="utf-8")
        (self.vault / "QCM" / "autre.json").write_text('{"pas": "un set"}', encoding="utf-8")
        self.ctx = self.app.test_request_context("/")
        self.ctx.push()
        self.user = User(username="eleve", email="eleve@hangar.local", role="admin", first_name="Élève",
                         contact_email="eleve@exemple.fr")
        self.user.set_password("secret1")
        self.invite = User(username="invite", email="invite@hangar.local", role="guest")
        self.invite.set_password("secret1")
        db.session.add_all([self.user, self.invite])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ecrire(self, rel, data):
        p = self.vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

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
        return quiz.get_library().pour(self.user.id if user_id is None else user_id)


class ChargementTests(QcmTestCase):
    def test_types_et_bibliotheque(self):
        lib = self.lib()
        self.assertEqual(set(lib.sets), {"QCM/Physique/elec", "QCM/Physique/schema-set"})
        self.assertIn("QCM/Anglais/Maison/cuisine", lib.langues)
        s = lib.get("QCM/Physique/elec")
        self.assertEqual(s.categorie, "Physique")
        types = [q.type for q in s.questions]
        self.assertEqual(types, ["QCM", "QCR", "TROU", "QRC", "QRC", "FLASHCARD"])  # QRC inverse ajoutée
        inv = s.questions[4]
        self.assertEqual((inv.id, inv.enonce, inv.reponses_texte), ("chien~inverse", "dog", ["chien"]))
        self.assertTrue(s.questions[2].est_correcte_texte("  paris. "))
        self.assertTrue(s.questions[1].est_correcte([2, 0]))
        self.assertTrue(s.correspond("ohm electronique"))
        m = lib.get("QCM/Physique/schema-set")
        self.assertTrue(m.est_masque)
        self.assertEqual([q.id for q in m.questions], ["s#a", "s#b"])
        self.assertEqual(lib.errors, [])  # « autre.json » n'est pas un set : ignoré sans erreur

    def test_set_malforme_signale(self):
        self._ecrire("QCM/Physique/casse.json", {"questions": [{"type": "QCM", "question": "?", "choix": ["a"], "reponses": [3]}]})
        quiz.get_library()
        self.assertTrue(any("hors limites" in e for e in self.lib().errors))
        with self.assertRaises(ValueError):
            quiz.set_depuis_json({"questions": [{"type": "INCONNU", "question": "?"}]}, PurePosixPath("x.json"))

    def test_espace_personnel_prive(self):
        dossier = quiz.dossier_perso(self.user.id) / "Mes sets"
        dossier.mkdir(parents=True)
        (dossier / "perso.json").write_text(json.dumps({"titre": "Perso", "questions": [
            {"type": "FLASHCARD", "question": "a", "verso": "b"}]}), encoding="utf-8")
        self.assertIn(f"~{self.user.id}/Mes sets/perso", self.lib().sets)
        self.assertNotIn(f"~{self.user.id}/Mes sets/perso", self.lib(self.invite.id).sets)
        self.assertEqual(len(self.lib().personnels(self.user.id)), 1)

    def test_verifier(self):
        ok, resume, avert = verifier.verifier_fichier(self.vault / "QCM/Physique/elec.json", self.vault)
        self.assertTrue(ok)
        self.assertIn("6 questions", resume)
        ok, resume, _ = verifier.verifier_fichier(self.vault / "QCM/Anglais/Maison/cuisine.json", self.vault)
        self.assertTrue(ok)
        self.assertIn("3 mots", resume)


class FsrsTests(QcmTestCase):
    def test_boites_et_fsrs(self):
        self.assertEqual(quiz.nouvelle_boite(3, False), 0)
        self.assertEqual(quiz.nouvelle_boite(0, "difficile"), 1)
        self.assertEqual(quiz.nouvelle_boite(2, "facile"), 4)
        e = fsrs.premiere(fsrs.BIEN)
        self.assertAlmostEqual(e.stabilite, fsrs.W[2])
        plus_tard = fsrs.suivante(e, 3, fsrs.BIEN)
        self.assertGreater(plus_tard.stabilite, e.stabilite)
        oubli = fsrs.suivante(plus_tard, 10, fsrs.A_REVOIR)
        self.assertLess(oubli.stabilite, plus_tard.stabilite)
        self.assertAlmostEqual(fsrs.retrievabilite(e.stabilite, e.stabilite), 0.9, places=3)

    def test_record_et_echeances(self):
        t0 = datetime(2026, 11, 10, 9, 0)
        p = QuizProgress(user_id=self.user.id, set_id="s", question_id="q")
        p.record(True, quand=t0)
        self.assertEqual((p.seen, p.correct, p.box, p.oublis), (1, 1, 1, 0))
        self.assertEqual(p.decouverte, t0)
        self.assertGreaterEqual(p.echeance, t0 + timedelta(days=1))
        self.assertFalse(p.is_due(t0 + timedelta(hours=1)))
        self.assertTrue(p.is_due(p.echeance))
        p.record(False, quand=p.echeance)
        self.assertEqual((p.box, p.oublis), (0, 1))
        self.assertEqual(p.echeance - p.last_review, timedelta(minutes=10))  # oublié : revient dans la session
        # Échéance plus serrée à l'approche d'un examen
        a, b = (QuizProgress(user_id=1, set_id="s", question_id=x) for x in "ab")
        a.record("bien", quand=t0)
        b.record("bien", quand=t0, retention=planning.RETENTION_EXAMEN)
        self.assertLessEqual(b.echeance, a.echeance)

    def test_progression_leitner_reprise(self):
        t0 = datetime(2026, 11, 10, 9, 0)
        p = QuizProgress(user_id=1, set_id="s", question_id="q", box=4, seen=5, correct=5, last_review=t0 - timedelta(days=15))
        self.assertTrue(p.is_due(t0))  # boîte 4 : 14 jours
        p.record(True, quand=t0)
        self.assertGreater(p.stabilite, 14)  # stabilité estimée d'après la boîte


def _midi():
    """Midi aujourd'hui : « + 11 minutes » reste le même jour, quelle que soit l'heure où la suite tourne."""
    return datetime.combine(date.today(), time(12))


class ReponsesTests(QcmTestCase):
    @mock.patch.object(quiz, "maintenant", _midi)
    def test_reponses_enregistrees_et_file_du_jour(self):
        c = self.client()
        h = {"X-CSRF-Token": self.token}
        r = c.post("/qcm/reponse/", json={"set_id": "QCM/Physique/elec", "question_id": "ohm", "correct": True, "duree_ms": 4000}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = c.post("/qcm/reponse/", json={"set_id": "QCM/Physique/elec", "question_id": "carte", "note": "a_revoir"}, headers=h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.post("/qcm/reponse/", json={"set_id": "x", "question_id": "y", "correct": True}, headers=h).status_code, 404)
        self.assertEqual(c.post("/qcm/reponse/", json={"set_id": "QCM/Physique/elec"}, headers=h).status_code, 400)
        self.assertEqual(c.post("/qcm/reponse/", json={}).status_code, 400)  # sans jeton CSRF
        prog = QuizProgress.map_for(self.user.id, "QCM/Physique/elec")
        self.assertEqual(set(prog), {"ohm", "carte"})
        act = Activite.query.filter_by(user_id=self.user.id).one()
        self.assertEqual((act.reponses, act.justes, act.nouveaux), (2, 1, 2))
        self.assertEqual(TempsRevision.query.one().matiere, "Physique")
        deck = QuizDeck.query.filter_by(user_id=self.user.id, set_id="QCM/Physique/elec").one()
        self.assertEqual((deck.temps_ms, deck.reponses_chronometrees), (4000, 1))
        qset = self.lib().get("QCM/Physique/elec")
        file, detail = services.file_du_jour(qset, prog, None, quiz.maintenant() + timedelta(minutes=11))
        self.assertEqual(detail["reapprendre"], 1)  # la carte ratée revient dans la journée
        self.assertEqual(file[0].id, "carte")
        self.assertNotIn("ohm", [q.id for q in file])  # réussie : pas avant demain
        # Réponse faite hors connexion puis envoyée avec sa date
        hier = (datetime.now() - timedelta(days=1)).replace(microsecond=0)
        c.post("/qcm/reponse/", json={"set_id": "QCM/Physique/elec", "question_id": "pairs", "correct": True,
                                      "date": hier.isoformat()}, headers=h)
        self.assertEqual(QuizProgress.query.filter_by(question_id="pairs").one().last_review, hier)
        self.assertEqual(Activite.serie(self.user.id), (2, 2))

    def test_serie_et_calendrier(self):
        auj = date(2026, 11, 10)
        for d in (0, 1, 2, 5):
            db.session.add(Activite(user_id=self.user.id, jour=auj - timedelta(days=d), reponses=10, justes=8))
        db.session.commit()
        self.assertEqual(Activite.serie(self.user.id, auj), (3, 3))
        self.assertEqual(Activite.serie(self.user.id, auj + timedelta(days=1)), (3, 3))  # tient encore le lendemain
        self.assertEqual(Activite.serie(self.user.id, auj + timedelta(days=2)), (0, 3))
        grille = Activite.calendrier(self.user.id, auj)
        self.assertEqual(len(grille), 26)
        self.assertTrue(all(len(s) == 7 for s in grille))
        res = Activite.resume(self.user.id, auj)
        self.assertEqual((res["aujourdhui"], res["sept_jours"], res["justes_sept_jours"]), (10, 40, 32))


class PlanningTests(QcmTestCase):
    def test_quotas_et_estimation(self):
        qset = self.lib().get("QCM/Physique/elec")
        now = datetime(2026, 11, 10, 10, 0)
        deck = QuizDeck(user_id=self.user.id, set_id=qset.id, nouvelles_jour=2)
        file, detail = services.file_du_jour(qset, {}, deck, now)
        self.assertEqual((len(file), detail["nouvelles"], detail["dues"]), (2, 2, 6))
        self.assertTrue(detail["quota_nouvelles"])
        # Deux nouvelles déjà vues aujourd'hui : quota atteint
        prog = {}
        for qid in ("ohm", "pairs"):
            p = QuizProgress(user_id=1, set_id=qset.id, question_id=qid)
            p.record(True, quand=now - timedelta(hours=1))
            prog[qid] = p
        file, detail = services.file_du_jour(qset, prog, deck, now)
        self.assertEqual(file, [])
        self.assertTrue(services.resume_set(qset, prog, now, deck)["quota_atteint"])
        # Estimation : sans examen, avec examen confortable, puis trop proche
        e = planning.estimer(qset, {}, deck, now)
        self.assertGreater(e["jours"], 1)
        deck.date_objectif = now.date() + timedelta(days=200)
        self.assertEqual(planning.estimer(qset, {}, deck, now)["statut"], "ok")
        deck.date_objectif = now.date() + timedelta(days=2)
        deck.nouvelles_jour = 1
        e = planning.estimer(qset, {}, deck, now)
        self.assertIn(e["statut"], ("retard", "impossible"))
        self.assertEqual(planning.retention_cible(now.date() + timedelta(days=10), now), planning.RETENTION_EXAMEN)
        self.assertEqual(planning.retention_cible(None, now), fsrs.RETENTION)
        deck.auto_quota = True
        if e["statut"] == "retard":
            self.assertEqual(planning.quotas_effectifs(qset, {}, deck, now), (e["nouvelles_conseillees"], None))

    def test_pages_planning(self):
        c = self.client()
        r = c.post("/qcm/planning/", data={"_csrf": self.token, "set": "QCM/Physique/elec", "nouvelles_jour": "3",
                                           "date_objectif": (date.today() + timedelta(days=5)).isoformat(), "auto": "1"})
        self.assertEqual(r.status_code, 302)
        deck = QuizDeck.query.filter_by(user_id=self.user.id, set_id="QCM/Physique/elec").one()
        self.assertEqual((deck.nouvelles_jour, deck.auto_quota), (3, True))
        r = c.post("/qcm/planning/", data={"_csrf": self.token, "set": "QCM/Physique/elec", "nouvelles_jour": "0"})
        self.assertEqual(QuizDeck.query.filter_by(set_id="QCM/Physique/elec").one().nouvelles_jour, 3)  # refusé
        r = c.post("/qcm/planning/", data={"_csrf": self.token, "categorie": "Physique", "langue": "",
                                           "date_objectif": (date.today() + timedelta(days=9)).isoformat()})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(QuizDeck.query.filter(QuizDeck.date_objectif.isnot(None)).count(), 2)
        self.assertEqual(c.get("/qcm/planning/").status_code, 200)
        QuizDeck.query.filter_by(set_id="QCM/Physique/elec").one().auto_quota = False  # quota fixe (pas d'estimation)
        db.session.commit()
        r = services.resume_du_jour(self.user)
        self.assertEqual(len(r["objectifs"]), 2)
        self.assertEqual(r["nouvelles"], 3)  # nouvelles prévues par le quota du set


class ImportExportTests(QcmTestCase):
    def test_import_json_et_droits(self):
        c = self.client()
        corps = json.dumps({"titre": "Importé", "questions": [{"type": "QCM", "question": "1+1", "choix": ["2", "3"], "reponses": [0]}]})
        r = c.post("/qcm/importer/", data={"_csrf": self.token, "categorie": "Maths",
                                           "fichiers": (io.BytesIO(corps.encode()), "mon-set.json")},
                   content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        self.assertTrue((self.vault / "QCM/Maths/mon-set.json").exists())
        self.assertIn("QCM/Maths/mon-set", self.lib().sets)
        # Set invalide refusé, fichier existant protégé
        c.post("/qcm/importer/", data={"_csrf": self.token, "fichiers": (io.BytesIO(b'{"questions": []}'), "vide.json")},
               content_type="multipart/form-data")
        self.assertFalse((self.vault / "QCM/Importés/vide.json").exists())
        # Un invité ne gère pas le coffre
        g = self.client("invite")
        self.assertEqual(g.get("/qcm/importer/").status_code, 403)
        self.assertEqual(g.post("/qcm/supprimer/", data={"_csrf": self.token, "set": "QCM/Maths/mon-set"}).status_code, 403)

    def test_export_anki_tsv_json_et_reimport(self):
        c = self.client()
        sid = quote("QCM/Physique/elec")
        r = c.get(f"/qcm/exporter/?set={sid}")
        self.assertEqual(r.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(r.data)) as z:
            self.assertEqual(set(z.namelist()), {"collection.anki2", "media"})
            chemin = Path(self.tmp) / "c.anki2"
            chemin.write_bytes(z.read("collection.anki2"))
        base = sqlite3.connect(chemin)
        self.assertEqual(base.execute("SELECT count(*) FROM notes").fetchone()[0], 6)
        base.close()
        tsv = c.get(f"/qcm/exporter/?set={sid}&format=tsv").get_data(as_text=True)
        self.assertEqual(len(tsv.strip().split("\n")), 6)
        self.assertEqual(json.loads(c.get(f"/qcm/exporter/?set={sid}&format=json").data)["titre"], SET_MIXTE["titre"])
        self.assertEqual(c.get(f"/qcm/exporter/?set={quote('QCM/Anglais/Maison/cuisine')}").status_code, 200)
        # Le paquet exporté se réimporte (import .apkg) en cartes
        set_json = anki.lire_apkg(r.data, "elec.apkg")
        self.assertEqual(len(set_json["questions"]), 6)
        self.assertTrue(all(q["type"] == "FLASHCARD" for q in set_json["questions"]))
        self.assertIn("$RI$", json.dumps(set_json, ensure_ascii=False))  # MathJax → $…$
        quiz.set_depuis_json(set_json, PurePosixPath("QCM/x/anki.json"))
        r = c.post("/qcm/importer/", data={"_csrf": self.token, "categorie": "Anki",
                                           "fichiers": (io.BytesIO(r.data), "elec.apkg")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(len(list((self.vault / "QCM/Anki").glob("*.json"))), 1)
        with self.assertRaises(anki.ImportAnkiImpossible):
            anki.lire_apkg(b"pas un zip")

    def test_import_anki_texte_a_trous(self):
        notes = [("n1", "Le {{c1::cœur}} pompe le {{c2::sang}}", "", False)]
        contenu = anki.apkg("x", "Bio", "", notes)
        # transformer la note en « cloze » : premier champ avec {{c1::…}}
        s = anki.lire_apkg(contenu, "bio.apkg")
        trous = [q for q in s["questions"] if q["type"] == "TROU"]
        self.assertEqual(len(trous), 2)
        self.assertEqual(trous[0]["reponses_texte"], ["cœur"])
        self.assertIn("___", trous[0]["question"])

    def test_liste_de_mots_csv(self):
        fiches, ignorees = paquets.analyser_liste("# export Anki\nkitchen\tcuisine\noven\tfour\n\nsink\tévier\tWash in the [sink].\n?\n")
        self.assertEqual([f["mot"] for f in fiches], ["kitchen", "oven", "sink"])
        self.assertEqual(fiches[2]["exemple"], "Wash in the [sink].")
        self.assertEqual(ignorees, 1)
        fiches, _ = paquets.analyser_liste("mot;traduction\n\"to be\";être\nhouse;maison", paquets.LANGUE_PUIS_FR)  # CSV
        self.assertEqual([f["mot"] for f in fiches], ["mot", "to be", "house"])
        fiches, _ = paquets.analyser_liste("chien = dog\nchat = cat", paquets.FR_PUIS_LANGUE)
        self.assertEqual((fiches[0]["mot"], fiches[0]["traductions"]), ("dog", ["chien"]))
        from werkzeug.datastructures import MultiDict
        form = MultiDict([("liste", "a job\tun emploi, un travail\nto achieve\tréussir"), ("langue", "Anglais"),
                          ("cartes", "decouverte"), ("cartes", "production"), ("ordre", "langue_fr")])
        paquet, options, ignorees, erreur = paquets.lire_liste(form)
        self.assertIsNone(erreur)
        self.assertEqual(len(paquet["vocabulaire"]), 2)
        self.assertEqual(paquet["vocabulaire"][0]["traductions"], ["un emploi", "un travail"])
        sl = langues.set_depuis_json(paquet, PurePosixPath("QCM/Vocabulaire/x.json"))
        self.assertEqual(len(sl.exercices), 4)
        # Mon espace : la liste devient un paquet personnel
        c = self.client()
        r = c.post("/qcm/mon-espace/", data={"_csrf": self.token, "action": "liste", "liste": "dog\tchien\ncat\tchat",
                                             "langue": "Anglais", "cartes": ["decouverte", "production"], "ordre": "langue_fr"})
        self.assertEqual(r.status_code, 302)
        perso = [s for s in self.lib().langues.values() if s.proprietaire]
        self.assertEqual(len(perso), 1)
        self.assertEqual(c.get(r.headers["Location"]).status_code, 200)
        # Import d'un set JSON dans « Mon espace », puis suppression
        r = c.post("/qcm/mon-espace/", data={"_csrf": self.token, "action": "json", "categorie": "Moi",
                                             "fichiers": (io.BytesIO(json.dumps(SET_MIXTE).encode()), "elec.json")},
                   content_type="multipart/form-data")
        sid = f"~{self.user.id}/Moi/elec"
        self.assertIn(sid, self.lib().sets)
        self.assertNotIn(sid, self.lib(self.invite.id).sets)
        c.post("/qcm/mon-espace/", data={"_csrf": self.token, "action": "supprimer", "set": sid})
        self.assertNotIn(sid, self.lib().sets)

    def test_deplacer_et_supprimer(self):
        c = self.client()
        for qid in ("ohm", "trou"):
            p = QuizProgress(user_id=self.user.id, set_id="QCM/Physique/elec", question_id=qid)
            p.record(True)
            db.session.add(p)
        db.session.commit()
        r = c.post("/qcm/deplacer/", data={"_csrf": self.token, "set": "QCM/Physique/elec", "dossier": "Électricité"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue((self.vault / "QCM/Électricité/elec.json").exists())
        self.assertFalse((self.vault / "QCM/Physique/elec.json").exists())
        self.assertEqual(QuizProgress.query.filter_by(set_id="QCM/Électricité/elec").count(), 2)  # la progression suit
        r = c.post("/qcm/supprimer/", data={"_csrf": self.token, "categorie": "Électricité", "langue": ""})
        self.assertFalse((self.vault / "QCM/Électricité").exists())
        self.assertEqual(QuizProgress.query.count(), 0)


class LanguesTests(QcmTestCase):
    def test_exercices_et_correction(self):
        sl = self.lib().langues["QCM/Anglais/Maison/cuisine"]
        genres = {e.genre for e in sl.exercices}
        self.assertEqual(genres, {langues.VOCABULAIRE, langues.CONJUGAISON, langues.GRAMMAIRE})
        prod = sl.exercice("v1#prod")
        self.assertEqual(prod.prerequis, "v1#rec")
        self.assertEqual(langues.corriger(sl, prod, "kitchen")["verdict"], langues.JUSTE)
        self.assertEqual(langues.corriger(sl, prod, "kichen")["verdict"], langues.FAUTE)
        self.assertEqual(langues.corriger(sl, sl.exercice("g2"), "He goes home")["verdict"], langues.JUSTE)
        self.assertEqual(langues.corriger(sl, sl.exercice("g1"), [0, 1, 2, 3])["verdict"], langues.JUSTE)
        now = datetime(2026, 11, 10, 9)
        session = langues.construire_session([sl], {}, langues.QUOTIDIEN, 20, now)
        self.assertTrue(session)
        self.assertTrue(all(e.exo.prerequis is None for e in session if e.exo.genre == langues.VOCABULAIRE))
        r = langues.resume([sl], {}, now)
        self.assertEqual(r["a_revoir"], 0)
        self.assertGreater(r["nouveaux_disponibles"], 0)

    def test_routes_langues(self):
        c = self.client()
        h = {"X-CSRF-Token": self.token}
        for url in ("/qcm/langues/", "/qcm/langues/Anglais/", "/qcm/langues/Anglais/?dossier=Maison",
                    f"/qcm/langues/Anglais/?fichier={quote('QCM/Anglais/Maison/cuisine')}",
                    "/qcm/langues/Anglais/jouer/?mode=quotidien", "/qcm/langues/Anglais/jouer/?mode=intensif",
                    "/qcm/langues/Anglais/jouer/?mode=vocabulaire"):
            self.assertEqual(c.get(url).status_code, 200, url)
        self.assertEqual(c.get("/qcm/langues/Klingon/").status_code, 404)
        corps = {"set_id": "QCM/Anglais/Maison/cuisine", "exo_id": "v1#prod", "reponse": "kitchen"}
        r = c.post("/qcm/langue/corriger/", json=corps, headers=h)
        self.assertEqual(r.get_json()["verdict"], "juste")
        r = c.post("/qcm/langue/reponse/", json={**corps, "note": 3, "duree_ms": 3000}, headers=h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(TempsRevision.query.one().matiere, "Anglais")
        r = c.post("/qcm/langues/Anglais/reinitialiser/", data={"_csrf": self.token})
        self.assertEqual(QuizProgress.query.count(), 0)


class MasqueTests(QcmTestCase):
    def test_cartes_et_editeur(self):
        qset = self.lib().get("QCM/Physique/schema-set")
        q = qset.questions[0]
        self.assertEqual(q.masque["cible"], "a")
        html = masque.html(qset, q.masque)
        self.assertIn("/qcm/fichiers/Images/schema.png", html)
        self.assertEqual(html.count("<rect"), 2)
        with self.assertRaises(ValueError):
            masque.questions({"image": "x.png", "zones": [{"x": 200, "y": 0, "l": 1, "h": 1, "reponse": "?"}]}, 1, "t")
        c = self.client()
        r = c.get("/qcm/fichiers/Images/schema.png")
        self.assertEqual(r.status_code, 200)
        r.close()
        self.assertEqual(c.get("/qcm/fichiers/../hangar.db").status_code, 404)
        self.assertEqual(c.get(f"/qcm/image-masquee/?set={quote(qset.id)}").status_code, 200)
        # Nouveau set dans « Mon espace » avec une image envoyée
        zones = [{"id": "z1", "x": 5, "y": 5, "l": 30, "h": 30, "reponse": "Haut gauche"}]
        r = c.post("/qcm/image-masquee/", data={"_csrf": self.token, "titre": "Mon schéma", "destination": "perso",
                                                "masquer_tout": "1", "zones": json.dumps(zones),
                                                "fichier": (io.BytesIO(_png("blue")), "photo.png")},
                   content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = r.get_json()
        self.assertTrue(data["image"].startswith("perso:"))
        r = c.get(data["image_url"])
        self.assertEqual(r.status_code, 200)
        r.close()
        page = c.get(data["jouer"]).get_data(as_text=True)
        self.assertIn('data-masque', page)
        self.assertEqual(self.client("invite").get(data["image_url"]).status_code, 404)  # image privée


class PagesTests(QcmTestCase):
    def test_pages_200(self):
        # Un set de QCM rattaché à une langue sans fichier de langue reste visible (section Langues)
        self._ecrire("QCM/Espagnol/verbes.json", {"titre": "Verbes irréguliers", "langue": "Espagnol", "questions": [
            {"type": "QRC", "question": "tener (yo)", "reponses_texte": ["tengo"]}]})
        c = self.client()
        self.assertIn("Verbes irréguliers", c.get("/qcm/").get_data(as_text=True))
        sid = quote("QCM/Physique/elec")
        for url in ("/qcm/", "/qcm/?q=ohm", "/qcm/aujourdhui/", "/qcm/stats/", "/qcm/planning/", "/qcm/importer/",
                    "/qcm/mon-espace/", "/qcm/image-masquee/", "/qcm/signalements/", "/qcm/langues/",
                    f"/qcm/jouer/?set={sid}&mode=all", f"/qcm/jouer/?set={sid}&mode=review",
                    f"/qcm/jouer/?set={sid}&mode=intensif", f"/qcm/jouer/?set={sid}&mode=examen&n=3&duree=5",
                    "/qcm/jouer/?revision=1", "/qcm/jouer/?categorie=Physique&mode=intensif",
                    "/qcm/jouer/?categorie=Physique&mode=examen"):
            r = c.get(url)
            self.assertEqual(r.status_code, 200, url)
        page = c.get(f"/qcm/jouer/?set={sid}&mode=all").get_data(as_text=True)
        donnees = json.loads(re.search(r'id="quiz-data">(.*?)</script>', page, re.S).group(1))
        self.assertEqual(len(donnees), 6)
        ohm = next(q for q in donnees if q["id"] == "ohm")
        self.assertIn('class="math math-inline"', ohm["choix"][0])
        self.assertIn("<strong>U = RI</strong>", ohm["explication"])
        carte = next(q for q in donnees if q["id"] == "carte")
        # Rendu du wiki quand il est présent (/fichiers/…), sinon rendu de repli du module QCM (/qcm/fichiers/…)
        self.assertRegex(carte["enonce"], r'src="(/qcm)?/fichiers/Images/schema\.png"')
        self.assertNotIn("data-expliquer-url", page)  # pas d'IA configurée : pas de bouton « Explique-moi »
        self.assertEqual(c.get("/qcm/jouer/?set=inconnu").status_code, 404)
        self.assertEqual(self.client("invite").get("/qcm/signalements/").status_code, 403)

    def test_signalements(self):
        c = self.client()
        h = {"X-CSRF-Token": self.token}
        r = c.post("/qcm/signaler/", json={"set_id": "QCM/Physique/elec", "question_id": "pairs", "commentaire": "Ambigu"}, headers=h)
        self.assertTrue(r.get_json()["nouveau"])
        s = Signalement.query.one()
        self.assertIn("Ambigu", c.get("/qcm/signalements/").get_data(as_text=True))
        r = c.post("/qcm/signalements/", data={"_csrf": self.token, "pk": s.id, "action": "retirer"})
        self.assertEqual(r.status_code, 302)
        donnees = json.loads((self.vault / "QCM/Physique/elec.json").read_text(encoding="utf-8"))
        self.assertNotIn("pairs", [q.get("id") for q in donnees["questions"]])
        self.assertTrue(db.session.get(Signalement, s.id).traite)


class ResumeTests(QcmTestCase):
    def _due(self):
        hier = datetime.now() - timedelta(days=3)
        for qid in ("ohm", "trou"):
            p = QuizProgress(user_id=self.user.id, set_id="QCM/Physique/elec", question_id=qid)
            p.record(True, quand=hier)
            p.echeance = hier + timedelta(days=1)
            db.session.add(p)
        db.session.add(QuizDeck(user_id=self.user.id, set_id="QCM/Physique/elec", nouvelles_jour=1,
                                date_objectif=date.today() + timedelta(days=6)))
        db.session.add(Activite(user_id=self.user.id, jour=date.today() - timedelta(days=1), reponses=5, justes=4))
        db.session.commit()

    def test_resume_du_jour(self):
        self._due()
        r = services.resume_du_jour(self.user)
        self.assertEqual(r["revisions"], 2)
        self.assertEqual(r["nouvelles"], 1)
        self.assertEqual(r["cartes_dues"], 3)
        self.assertEqual(r["serie"], 1)
        self.assertEqual(r["objectifs"][0]["jours"], 6)
        self.assertEqual(r["par_matiere"][0], {"matiere": "Physique", "cartes": 3})
        self.assertEqual(r["url"], "/qcm/aujourdhui/")

    def test_section_resume_quotidien(self):
        self.assertIn("qcm.revisions", digest.sections_enregistrees())
        sections = digest.calculer(self.user, date.today())
        self.assertFalse([s for s in sections if s.cle == "qcm.revisions"])  # rien de dû : pas de section
        self._due()
        sec = next(s for s in digest.calculer(self.user, date.today()) if s.cle == "qcm.revisions")
        self.assertTrue(sec.a_faire)
        self.assertEqual(sec.compte, 3)
        self.assertIn("Électronique de base", sec.html)
        self.assertIn("Examens à moins de 14 jours", sec.html)
        self.assertIn("/qcm/", sec.html)
        built = digest.construire(self.user, date.today())
        self.assertIn("3 cartes à réviser", built[0])

    def test_contexte_ia(self):
        self._due()
        ctx = services.contexte_ia("/qcm/jouer/", {"set": "QCM/Physique/elec"}, self.user)
        self.assertEqual(ctx["type"], "qcm_set")
        self.assertIn("Loi d'Ohm", ctx["text"])
        ctx = services.contexte_ia("/qcm/", {}, self.user)
        self.assertIn("révisions dues", ctx["text"])
        self.assertIsNone(services.contexte_ia("/agenda/", {}, self.user))


class CliTests(QcmTestCase):
    def test_qcm_verifier(self):
        from contextlib import redirect_stdout

        from hangar.cli import main
        out = io.StringIO()
        with redirect_stdout(out):
            main(["qcm-verifier", str(self.vault / "QCM/Physique")])
        self.assertIn("2 fichier(s) valide(s)", out.getvalue())
        (self.vault / "QCM/Physique/faux.json").write_text('{"questions": [{"question": "?"}]}', encoding="utf-8")
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            main(["qcm-verifier", str(self.vault / "QCM/Physique")])


if __name__ == "__main__":
    unittest.main()
