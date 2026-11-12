"""Préparer un contrôle (piste 75), planifier les révisions dans les créneaux libres (piste 77),
notes et moyennes par matière (piste 80) : hangar/controles.py."""
import json
import re
import shutil
import tempfile
import unittest
from datetime import datetime, time, timedelta
from pathlib import Path

from hangar import agenda as A
from hangar import controles as C
from hangar import create_app, digest
from hangar.extensions import db
from hangar.models import AgendaEvent, AgendaGrade, AgendaTask, User

TITRE = "Techniques numériques"


def _ics(events) -> str:
    """events : [(uid, début, fin, intitulé)] → fichier .ics (heures locales)."""
    out = ["BEGIN:VCALENDAR", "X-WR-TIMEZONE:Europe/Paris"]
    for uid, a, b, summary in events:
        out += ["BEGIN:VEVENT", f"DTSTART:{a:%Y%m%dT%H%M%S}", f"DTEND:{b:%Y%m%dT%H%M%S}", f"UID:{uid}",
                f"SUMMARY:{summary} \\n", "DESCRIPTION:\\n", "END:VEVENT"]
    return "\n".join(out + ["END:VCALENDAR"]) + "\n"


class ControlesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-controles-")
        self.app = create_app(self.tmp, testing=True)
        self.app.config.update(WIKI_VERIF_INTERVALLE=0, QCM_VERIF_INTERVALLE=0)
        vault = Path(self.app.config["WIKI_DIR"])
        pages = [("Cours/TechniquesNumeriques/00 - Sommaire TN.md", "# TN\n" + "mot " * 440),
                 ("Cours/TechniquesNumeriques/01 - Ohmmètre.md", "# Ohm\n" + "tension " * 1100)]
        for rel, text in pages:
            (vault / rel).parent.mkdir(parents=True, exist_ok=True)
            (vault / rel).write_text(text, encoding="utf-8")
        (vault / "Cours/TechniquesNumeriques/poly.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        qset = {"titre": "Ohm", "questions": [
            {"id": f"q{i}", "type": "QCM", "question": f"Question {i} ?", "choix": ["RI", "R/I"], "reponses": [0]}
            for i in range(40)]}
        (vault / "QCM/TECHNIQUES_NUM").mkdir(parents=True, exist_ok=True)
        (vault / "QCM/TECHNIQUES_NUM/ohm.json").write_text(json.dumps(qset), encoding="utf-8")
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.user = User(username="moi", email="moi@hangar.local", role="member", contact_email="moi@exemple.fr")
        self.user.set_password("secret1")
        db.session.add(self.user)
        db.session.commit()
        self.at = datetime.now().replace(second=0, microsecond=0)
        self.today = self.at.date()
        self.exam_day = self.today + timedelta(days=5)

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- outils -------------------------------------------------------------------------------------------
    def planning(self, extra=(), exam=True, passe=True):
        """Cours chaque jour d'ici le contrôle (8 h–12 h, 13 h 30–17 h 30), le contrôle dans 5 jours à 10 h 15,
        et un contrôle passé il y a 2 jours."""
        evs = []
        for k in range(1, 5):
            d = self.today + timedelta(days=k)
            evs.append((f"m{k}", datetime.combine(d, time(8)), datetime.combine(d, time(12)), "Mathématiques 3 CM"))
            evs.append((f"a{k}", datetime.combine(d, time(13, 30)), datetime.combine(d, time(17, 30)),
                        "Anglais 4 TD_TP"))
        if exam:
            evs.append(("exam1", datetime.combine(self.exam_day, time(10, 15)),
                        datetime.combine(self.exam_day, time(12, 15)), f"{TITRE} 15 CO_ECRIT"))
        if passe:
            d = self.today - timedelta(days=2)
            evs.append(("exam0", datetime.combine(d, time(10)), datetime.combine(d, time(12)), f"{TITRE} 14 CO_ECRIT"))
        return _ics(list(evs) + list(extra))

    def importer(self, ics=None, at=None):
        return A.import_calendar(self.user, ics or self.planning(), at=at or self.at)

    def ev(self, uid):
        return AgendaEvent.query.filter_by(uid=uid).one()

    def relier(self, wiki=True, qcm=True):
        A.set_subject_link(self.user, self.ev("exam1").title,
                           wiki_path="Cours/TechniquesNumeriques" if wiki else None,
                           qcm_category="TECHNIQUES_NUM" if qcm else None)
        db.session.commit()

    def client(self):
        from flask import g
        g.pop("_login_user", None)
        c = self.app.test_client()
        token = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        c.post("/login", data={"login": "moi", "password": "secret1", "_csrf": token})
        g.pop("_login_user", None)
        return c, token

    # -- Piste 75 : « Préparer ce contrôle » ---------------------------------------------------------------
    def test_preparer_rassemble_tout_sans_rien_ecrire(self):
        from hangar.qcm.models import QuizDeck
        from hangar.wiki.models import WikiVisit
        self.importer()
        self.relier()
        ev = self.ev("exam1")
        db.session.add(WikiVisit(user_id=self.user.id, key="Cours/TechniquesNumeriques/00 - Sommaire TN", views=1))
        db.session.add(AgendaTask(user_id=self.user.id, title="Apporter la calculatrice", event_id=ev.id,
                                  due_date=self.exam_day))
        db.session.commit()
        p = C.preparer(self.user, ev, self.at)
        # Cours : 2 pages, 1 ouverte, temps de lecture restant = la page non ouverte (1100 mots / 220 = 5 min)
        self.assertEqual((p["lecture"]["total"], p["lecture"]["lues"]), (2, 1))
        self.assertEqual(p["lecture"]["minutes_restantes"], 5)
        self.assertEqual([f["nom"] for f in p["lecture"]["pdfs"]], ["poly.pdf"])
        # QCM : statut estimé pour le jour du contrôle alors qu'aucune date d'examen n'est fixée
        self.assertEqual(len(p["sets"]), 1)
        self.assertEqual(p["sets"][0]["e"]["objectif"], self.exam_day)
        self.assertIn(p["sets"][0]["statut"], ("dans_les_temps", "a_commencer", "retard"))
        self.assertIn(p["statut"], C.STATUTS)
        self.assertEqual([t.title for t in p["taches"]], ["Apporter la calculatrice"])
        self.assertEqual(p["temps"], {"minutes": 0, "semaine": 0})
        # Rien n'est écrit : pas de réglage de set créé pour l'estimation
        db.session.commit()
        self.assertEqual(QuizDeck.query.count(), 0)

    def test_page_preparer(self):
        self.importer()
        c, token = self.client()
        ev = self.ev("exam1")
        page = c.get(f"/agenda/events/{ev.id}/preparer")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        # Matière pas encore reliée : suggestions proposées sur la page
        self.assertIn("Relier la matière", html)
        self.assertIn("TECHNIQUES_NUM ?", html)
        c.post(f"/agenda/events/{ev.id}/link", data={"_csrf": token, "qcm_category": "TECHNIQUES_NUM",
                                                       "wiki_path": "Cours/TechniquesNumeriques"})
        html = c.get(f"/agenda/events/{ev.id}/preparer").get_data(as_text=True)
        self.assertIn("Examen blanc", html)
        self.assertIn("Date d’examen des QCM fixée au", html)  # relier la matière a déjà fixé la date
        self.assertIn("01 - Ohmmètre", html)
        self.assertIn("Planifier mes révisions", html)
        # Date d'examen effacée (ou fixée pour un autre contrôle) : le bouton est proposé ; il fixe la date
        from hangar.qcm.models import QuizDeck
        QuizDeck.query.update({"date_objectif": None})
        db.session.commit()
        self.assertIn("Fixer la date d’examen des QCM au", c.get(f"/agenda/events/{ev.id}/preparer").get_data(as_text=True))
        r = c.post(f"/agenda/events/{ev.id}/exam", data={"_csrf": token,
                                                         "next": f"/agenda/events/{ev.id}/preparer"})
        self.assertTrue(r.headers["Location"].endswith(f"/agenda/events/{ev.id}/preparer"))
        self.assertIn("Date d’examen des QCM fixée", c.get(f"/agenda/events/{ev.id}/preparer").get_data(as_text=True))
        # Un seul « Préparer ce contrôle » : la fenêtre de la séance renvoie vers la page
        data = A.event_payload(ev, set(), lambda ep, **kw: f"/{ep}/{kw.get('eid')}")
        self.assertEqual(data["prepare"], f"/agenda.event_prepare/{ev.id}")
        js = (Path(self.app.static_folder) / "js" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("Préparer ce contrôle : date d’examen", js)
        # Autre compte : 404
        self.assertEqual(c.get("/agenda/events/999999/preparer").status_code, 404)

    def test_tableau_de_bord_affiche_le_statut(self):
        self.importer()
        self.relier()
        c, _ = self.client()
        html = c.get("/").get_data(as_text=True)
        ev = self.ev("exam1")
        self.assertIn(f"/agenda/events/{ev.id}/preparer", html)
        st = C.indicateur(self.user, ev, self.at)
        self.assertIn(C.libelle(st), html)

    def test_statut_calcule_une_fois_par_matiere_et_par_jour(self):
        from unittest import mock

        from hangar.qcm import planning
        d = self.exam_day
        self.importer(self.planning(extra=[("exam2", datetime.combine(d, time(14)), datetime.combine(d, time(16)),
                                            f"{TITRE} 16 CO_ORAL")]))
        self.relier()
        evs = [self.ev("exam1"), self.ev("exam2")]
        with mock.patch.object(planning, "estimer", wraps=planning.estimer) as estimer:
            st = C.indicateurs(self.user, evs, self.at)
        self.assertEqual(estimer.call_count, 1)  # un set, deux contrôles le même jour : une seule estimation
        self.assertEqual(st[evs[0].id], st[evs[1].id])

    def test_resume_quotidien_statut_sans_requete_web(self):
        from flask import has_request_context
        self.importer()
        self.relier()
        self.assertFalse(has_request_context())
        jour = self.exam_day - timedelta(days=1)
        sections = digest.calculer(self.user, jour, datetime.combine(jour - timedelta(days=1), time(18)))
        sec = next(s for s in sections if s.cle == "agenda.controles")
        ev = self.ev("exam1")
        self.assertIn(f"/agenda/events/{ev.id}/preparer", sec.html)
        self.assertRegex(sec.html, r"(prêt|dans les temps|à commencer|en retard)</a>")

    # -- Piste 77 : planifier les révisions ----------------------------------------------------------------
    def test_creneaux_libres(self):
        self.importer()
        s = A.settings_of(self.user)
        d = self.today + timedelta(days=1)
        libres = C.creneaux_libres(self.user, d, s, self.at)
        h = [(a.time(), b.time()) for a, b in libres]
        # 12 h 10 (fin du cours + marge) → 13 h 30, mais midi-13 h est le repas ; 17 h 40 → 19 h ; 20 h → 22 h
        self.assertEqual(h, [(time(13), time(13, 30)), (time(17, 40), time(19)), (time(20), time(22))])

    def test_proposer_puis_creer_les_blocs(self):
        self.importer()
        self.relier()
        s = A.settings_of(self.user)
        s.default_reminder = 60  # un rappel par défaut ne doit pas s'appliquer aux blocs
        db.session.commit()
        ev = self.ev("exam1")
        prop = C.proposer(self.user, ev, self.at)
        self.assertTrue(prop["blocs"])
        for b in prop["blocs"]:
            self.assertLess(b["date"], self.exam_day)
            self.assertGreaterEqual(b["minutes"], C.BLOC_MIN)
            a, z = datetime.combine(b["date"], b["debut"]), datetime.combine(b["date"], b["fin"])
            for e in A.events_between(self.user, a, z, include_removed=False):
                self.fail(f"bloc sur une séance : {e.title}")
        self.assertEqual(AgendaTask.query.count(), 0)  # l'aperçu n'écrit rien
        c, token = self.client()
        r = c.post(f"/agenda/events/{ev.id}/planifier", data={"_csrf": token})
        self.assertEqual(r.status_code, 302)
        blocs = AgendaTask.query.filter_by(revision_auto=True).all()
        self.assertEqual(len(blocs), len(prop["blocs"]))
        self.assertTrue(all(t.event_id == ev.id and t.category == C.CATEGORIE_TACHE for t in blocs))
        self.assertTrue(all(not t.reminders for t in blocs))
        self.assertTrue(blocs[0].title.startswith(f"Réviser {ev.title}"))
        # Recommencer remplace les blocs à venir (pas de doublons)
        c.post(f"/agenda/events/{ev.id}/planifier", data={"_csrf": token})
        self.assertEqual(AgendaTask.query.filter_by(revision_auto=True).count(), len(prop["blocs"]))
        c.post(f"/agenda/events/{ev.id}/planifier", data={"_csrf": token, "action": "retirer"})
        self.assertEqual(AgendaTask.query.filter_by(revision_auto=True).count(), 0)

    def test_blocs_replaces_apres_import(self):
        self.importer()
        self.relier()
        ev = self.ev("exam1")
        C.appliquer(self.user, ev, self.at)
        db.session.commit()
        bloc = AgendaTask.query.filter_by(revision_auto=True).order_by(AgendaTask.due_date, AgendaTask.start_time).first()
        # Nouveau cours posé exactement sur le bloc
        a, b = datetime.combine(bloc.due_date, bloc.start_time), datetime.combine(bloc.due_date, bloc.end_time)
        imp = self.importer(self.planning(extra=[("ajout", a, b, "Droit 2 CM")]), at=self.at + timedelta(minutes=1))
        self.assertIn("blocs de révision replacés", imp.exam_notes)
        for t in AgendaTask.query.filter_by(revision_auto=True):
            self.assertFalse(C._en_conflit(self.user, t), t.title)
        # Contrôle retiré du planning : ses blocs à venir disparaissent
        imp = self.importer(self.planning(exam=False), at=self.at + timedelta(minutes=2))
        self.assertIn("bloc(s) de révision supprimé(s)", imp.exam_notes)
        self.assertEqual(AgendaTask.query.filter_by(revision_auto=True).count(), 0)

    # -- Piste 80 : notes et moyennes ----------------------------------------------------------------------
    def test_note_survit_a_la_suppression_de_la_seance(self):
        self.importer(self.planning(exam=False))
        imp = self.importer(at=self.at + timedelta(minutes=1))  # ajoute le contrôle à venir
        ev = self.ev("exam1")
        C.noter(self.user, ev, 14.5, 20, 2)
        db.session.commit()
        A.undo_import(imp, at=self.at + timedelta(minutes=2))  # la séance ajoutée est supprimée
        self.assertIsNone(AgendaEvent.query.filter_by(uid="exam1").first())
        g = AgendaGrade.query.one()
        self.assertIsNone(g.event_id)
        self.assertEqual((g.subject, g.day, g.value, g.coefficient), (ev.title, self.exam_day, 14.5, 2))

    def test_maitrise_figee_le_jour_du_controle(self):
        from hangar.qcm.models import QuizProgress
        self.importer()
        self.relier()
        ev = self.ev("exam1")
        for i in range(20):  # 20 questions sur 40 bien sues (stabilité au-delà de la maîtrise)
            db.session.add(QuizProgress(user_id=self.user.id, set_id="QCM/TECHNIQUES_NUM/ohm", question_id=f"q{i}",
                                        box=5, seen=3, correct=3, stabilite=400.0, difficulte=3.0,
                                        last_review=self.at, echeance=self.at + timedelta(days=300)))
        db.session.commit()
        self.assertEqual(C.figer_maitrise(ev.start - timedelta(minutes=5)), 0)  # pas encore
        A.run_jobs(self.app, at=ev.start + timedelta(minutes=1))
        g = AgendaGrade.query.filter_by(event_id=ev.id).one()
        self.assertIsNotNone(g.mastery_at)
        self.assertGreater(g.mastery, 0)
        self.assertIsNone(g.value)
        figee = g.mastery
        # Plus de progrès ensuite : la valeur figée ne bouge pas
        QuizProgress.query.update({"stabilite": 1.0})
        db.session.commit()
        C.figer_maitrise(ev.start + timedelta(hours=3))
        self.assertEqual(AgendaGrade.query.filter_by(event_id=ev.id).one().mastery, figee)

    def test_maitrise_sans_qcm_figee_sans_valeur(self):
        self.importer()
        passe = self.ev("exam0")  # contrôle d'il y a 2 jours, matière non reliée : rattrapé, sans valeur
        self.assertEqual(C.figer_maitrise(self.at), 1)
        g = AgendaGrade.query.filter_by(event_id=passe.id).one()
        self.assertIsNone(g.mastery)
        self.assertIsNotNone(g.mastery_at)
        self.assertEqual(C.figer_maitrise(self.at + timedelta(minutes=1)), 0)  # pas recalculé toutes les 30 s

    def test_moyennes_et_a_noter(self):
        self.importer()
        passe = self.ev("exam0")
        self.assertEqual([e.id for e in C.a_noter(self.user)], [passe.id])
        C.noter(self.user, passe, 15, 20, 2)
        C.noter(self.user, None, 8, 10, 1, subject=TITRE, day=self.today)          # même matière, saisie à la main
        C.noter(self.user, None, 12, 20, 1, subject="Sport", day=self.today)
        db.session.commit()
        self.assertEqual(C.a_noter(self.user), [])
        m = C.moyennes(self.user)
        par = {x["matiere"]: x["moyenne"] for x in m["matieres"]}
        self.assertAlmostEqual(par[passe.title], (15 * 2 + 16) / 3)
        self.assertAlmostEqual(par["Sport"], 12)
        self.assertAlmostEqual(m["generale"], (15 * 2 + 16 + 12) / 4)
        g = C.graphiques(m)
        self.assertIn("<svg", g["evolution"])
        self.assertIn("<svg", g["matieres"])

    def test_routes_notes(self):
        self.importer()
        c, token = self.client()
        passe = self.ev("exam0")
        # Rappel au tableau de bord
        self.assertIn("Notes à saisir", c.get("/").get_data(as_text=True))
        # Page du contrôle passé : formulaire de note, pas de planification
        html = c.get(f"/agenda/events/{passe.id}/preparer").get_data(as_text=True)
        self.assertIn('id="note"', html)
        self.assertNotIn("Planifier mes révisions", html)
        # Fenêtre de la séance passée : formulaire de note
        data = A.event_payload(passe, set(), lambda ep, **kw: f"/{ep}/{kw.get('eid')}")
        self.assertEqual(data["grade"]["url"], f"/agenda.event_grade/{passe.id}")
        # Note impossible (au-delà du barème) : refusée
        c.post(f"/agenda/events/{passe.id}/resultat", data={"_csrf": token, "value": "25", "scale": "20"})
        self.assertIsNone(AgendaGrade.query.filter(AgendaGrade.value.isnot(None)).first())
        c.post(f"/agenda/events/{passe.id}/resultat", data={"_csrf": token, "value": "14", "scale": "20",
                                                             "coef": "3"})
        g = AgendaGrade.query.filter_by(event_id=passe.id).one()
        self.assertEqual((g.value, g.coefficient), (14, 3))
        self.assertNotIn("Notes à saisir", c.get("/").get_data(as_text=True))
        c.post("/agenda/notes", data={"_csrf": token, "subject": "Sport", "value": "16", "scale": "20", "coef": "1",
                                      "day": self.today.isoformat()})
        page = c.get("/agenda/notes").get_data(as_text=True)
        self.assertIn("Sport", page)
        self.assertIn("<svg", page)
        self.assertRegex(page, r"data-moyenne-generale>14,5<")  # (14 × 3 + 16) / 4
        # « Pas de note » : plus rappelé
        AgendaGrade.query.filter_by(event_id=passe.id).delete()
        db.session.commit()
        c.post(f"/agenda/events/{passe.id}/resultat", data={"_csrf": token, "skip": "1"})
        self.assertEqual(C.a_noter(self.user), [])

    # -- Relecture ----------------------------------------------------------------------------------------
    def test_annuler_import_supprime_les_blocs_du_controle_ajoute(self):
        """L'import qui avait ajouté le contrôle est annulé : ses blocs de révision disparaissent avec lui."""
        from hangar.models import AgendaImport
        self.importer(self.planning(exam=False))
        self.importer(at=self.at + timedelta(minutes=1))
        self.relier()
        fait = AgendaTask(user_id=self.user.id, title="Déjà révisé", due_date=self.today, revision_auto=True,
                          event_id=self.ev("exam1").id, done_at=self.at)
        db.session.add(fait)
        r = C.appliquer(self.user, self.ev("exam1"), self.at)
        db.session.commit()
        self.assertGreater(r["crees"], 0)
        imp = AgendaImport.query.order_by(AgendaImport.id.desc()).first()
        notes = A.undo_import(imp, at=self.at + timedelta(minutes=2))
        self.assertEqual(AgendaEvent.query.filter_by(uid="exam1").count(), 0)
        restes = AgendaTask.query.filter_by(revision_auto=True).all()
        self.assertEqual([t.title for t in restes], ["Déjà révisé"])  # un bloc déjà fait reste (historique)
        self.assertTrue(any("bloc(s) de révision supprimé(s)" in n for n in notes), notes)

    def test_note_refusee_hors_controle_passe(self):
        self.importer()
        c, token = self.client()
        futur, cours = self.ev("exam1"), self.ev("m1")
        r = c.post(f"/agenda/events/{futur.id}/resultat", data={"_csrf": token, "value": "12", "scale": "20"})
        self.assertIn(r.status_code, (302, 400))
        self.assertEqual(AgendaGrade.query.filter_by(event_id=futur.id).count(), 0)
        # « Pas de note » reste possible sur un contrôle à venir (annulé, pas noté…)
        c.post(f"/agenda/events/{futur.id}/resultat", data={"_csrf": token, "skip": "1"})
        self.assertTrue(AgendaGrade.query.filter_by(event_id=futur.id).one().skipped)
        r = c.post(f"/agenda/events/{cours.id}/resultat", data={"_csrf": token, "value": "12", "scale": "20"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(AgendaGrade.query.filter_by(event_id=cours.id).count(), 0)
        self.assertEqual(c.get(f"/agenda/events/{cours.id}/preparer").status_code, 404)

    def test_bloc_de_revision_propose_la_revision_de_la_categorie(self):
        self.importer()
        self.relier()
        C.appliquer(self.user, self.ev("exam1"), self.at)
        db.session.commit()
        bloc = AgendaTask.query.filter_by(revision_auto=True).first()
        url = lambda ep, **kw: ep + "?" + "&".join(f"{k}={v}" for k, v in sorted(kw.items()))
        self.assertEqual(A.task_payload(bloc, url)["revise"], "qcm.play?categorie=TECHNIQUES_NUM&revision=1")
        simple = AgendaTask(user_id=self.user.id, title="Courses", event_id=self.ev("exam1").id)
        db.session.add(simple)
        db.session.commit()
        self.assertEqual(A.task_payload(simple, url)["revise"], "")
        c, _ = self.client()
        self.assertIn("data-tm-revise", c.get("/agenda/").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
