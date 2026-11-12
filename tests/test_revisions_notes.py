"""Relecture v3, groupe « révisions et notes » : blocs de révision manqués, annulation d'un import (blocs
rendus, note rattachée), fiches du kit hors des pages de cours, date d'examen du kit relue à la publication.

Lancer : uv run python -m unittest discover -s tests -p "test_revisions_notes.py" -v
"""
import json
import unittest
from datetime import datetime, time, timedelta
from pathlib import Path

import test_controles as TC
import test_ia_qualite as TQ
from test_ai import claude_msg
from test_ai_revision import QUESTIONS_IA

from hangar import agenda as A
from hangar import controles as C
from hangar import digest
from hangar.extensions import db
from hangar.models import AgendaEvent, AgendaGrade, AgendaTask


class BlocsEtNotesTests(TC.ControlesTests):
    """Réutilise la fixture de test_controles (seuls les tests de cette classe sont lancés, voir load_tests)."""

    def blocs(self):
        return [(t.title, t.due_date, t.start_time, t.end_time) for t in
                AgendaTask.query.filter_by(revision_auto=True).order_by(AgendaTask.due_date, AgendaTask.start_time)]

    # -- Constats 1 et 18 : blocs passés non cochés ----------------------------------------------------
    def test_bloc_de_revision_passe_ni_en_retard_ni_au_resume(self):
        self.importer()
        self.relier()
        ev = self.ev("exam1")
        C.appliquer(self.user, ev, self.at)
        tache = AgendaTask(user_id=self.user.id, title="Rendre le rapport", due_date=self.today + timedelta(days=1))
        db.session.add(tache)
        s = A.settings_of(self.user)
        s.daily_mode, s.daily_hour = "morning", 6
        db.session.commit()
        self.assertTrue(self.blocs())
        apres = self.exam_day + timedelta(days=3)
        at2 = datetime.combine(apres, time(5))
        A.import_calendar(self.user, self.planning(), at=at2)  # replanification après le contrôle
        # Les blocs passés restent dans l'agenda (historique), mais ne sont pas « en retard »
        self.assertTrue(AgendaTask.query.filter_by(revision_auto=True, done_at=None).count())
        g = A.open_task_groups(self.user, apres)
        self.assertEqual([t.title for t in g["overdue"]], ["Rendre le rapport"])
        self.assertTrue(all(not t.revision_auto for grp in g.values() for t in grp))
        sec = next(x for x in digest.calculer(self.user, apres, at2) if x.cle == "agenda.taches")
        self.assertEqual(sec.compte, 1)
        self.assertNotIn("Réviser", sec.html)
        # Sans autre tâche en retard : aucune section « Tâches », donc pas de résumé envoyé pour rien
        tache.done_at = at2
        db.session.commit()
        self.assertFalse([x for x in digest.calculer(self.user, apres, at2) if x.cle == "agenda.taches"])
        self.assertIsNone(A.build_day_email(self.user, apres, at2))
        bloc = AgendaTask.query.filter_by(revision_auto=True).first()
        self.assertFalse(A.tache_en_retard(bloc, apres))
        self.assertTrue(A.bloc_manque(bloc, apres))

    def test_pastille_des_taches_ignore_les_blocs_manques(self):
        self.importer()
        self.relier()
        C.appliquer(self.user, self.ev("exam1"), self.at)
        db.session.add(AgendaTask(user_id=self.user.id, title="Rendre le rapport", due_date=self.today + timedelta(days=1)))
        db.session.commit()
        n = AgendaTask.query.filter_by(revision_auto=True).count()
        self.assertGreater(n, 1)
        apres = self.exam_day + timedelta(days=3)
        self.assertEqual(A.compte_taches_dues(self.user, apres), (1, 1))  # seul le rapport est en retard
        # La veille du premier bloc : un bloc pour aujourd'hui compte (à faire), pas encore manqué
        premier = AgendaTask.query.filter_by(revision_auto=True).order_by(AgendaTask.due_date).first()
        dues, retard = A.compte_taches_dues(self.user, premier.due_date)
        self.assertEqual(retard, 0)
        self.assertGreaterEqual(dues, 1)

    # -- Constat 2 : annuler un import rend les blocs supprimés --------------------------------------------
    def test_annuler_import_rend_les_blocs_supprimes(self):
        self.importer()
        self.relier()
        C.appliquer(self.user, self.ev("exam1"), self.at)
        db.session.commit()
        avant = self.blocs()
        self.assertTrue(avant)
        imp = self.importer(self.planning(exam=False), at=self.at + timedelta(minutes=1))
        self.assertIn("bloc(s) de révision supprimé(s)", imp.exam_notes)
        self.assertEqual(self.blocs(), [])
        notes = A.undo_import(imp, at=self.at + timedelta(minutes=2))
        self.assertEqual(self.ev("exam1").status, "active")
        self.assertEqual(self.blocs(), avant)
        self.assertTrue(all(t.event_id == self.ev("exam1").id for t in AgendaTask.query.filter_by(revision_auto=True)))
        self.assertTrue(any("bloc(s) de révision rétabli(s)" in n for n in notes), notes)

    # -- Constat 19 : annuler puis refaire un import ne redemande pas la note ----------------------------
    def test_annuler_puis_refaire_import_rattache_la_note(self):
        imp = self.importer()
        C.noter(self.user, self.ev("exam0"), 14)
        db.session.commit()
        A.undo_import(imp, self.at)
        self.assertIsNone(AgendaGrade.query.one().event_id)
        self.importer()
        ev0 = self.ev("exam0")
        self.assertEqual(C.a_noter(self.user, self.at), [])
        self.assertEqual(AgendaGrade.query.one().event_id, ev0.id)
        C.noter(self.user, ev0, 15)  # corriger la note : la même ligne
        db.session.commit()
        self.assertEqual([(g.value, g.event_id) for g in AgendaGrade.query], [(15, ev0.id)])
        self.assertEqual(len(C.moyennes(self.user)["notes"]), 1)

    def test_note_orpheline_d_une_autre_matiere_ou_d_un_autre_jour_pas_rattachee(self):
        jour0 = self.today - timedelta(days=2)
        db.session.add_all([AgendaGrade(user_id=self.user.id, subject="Sport", day=jour0, value=12),
                            AgendaGrade(user_id=self.user.id, subject=f"{TC.TITRE} 3 CO_ECRIT",
                                        day=jour0 - timedelta(days=1), value=11)])
        db.session.commit()
        self.importer()
        self.assertEqual(AgendaGrade.query.filter(AgendaGrade.event_id.isnot(None)).count(), 0)
        self.assertEqual([e.title for e in C.a_noter(self.user, self.at)], [self.ev("exam0").title])

    # -- Constat 16 : les fiches du kit ne sont pas des pages de cours à lire -----------------------------
    def test_fiches_et_versions_ia_hors_des_pages_a_lire(self):
        from hangar.wiki.models import WikiVisit
        from hangar.wiki.vault import invalider
        self.importer()
        self.relier(qcm=False)
        for k in ("Cours/TechniquesNumeriques/00 - Sommaire TN", "Cours/TechniquesNumeriques/01 - Ohmmètre"):
            db.session.add(WikiVisit(user_id=self.user.id, key=k, views=1))
        db.session.commit()
        ev = self.ev("exam1")
        self.assertEqual(C.indicateur(self.user, ev, self.at), "pret")
        v = Path(self.app.config["WIKI_DIR"]) / "Cours/TechniquesNumeriques"
        (v / "01 - Ohmmètre (Fiche).md").write_text("# Fiche\n" + "résumé " * 660, encoding="utf-8")
        (v / "01 - Ohmmètre (IA).md").write_text("# Ohm\n" + "tension " * 660, encoding="utf-8")
        invalider()
        lect = C.lecture(self.user, "Cours/TechniquesNumeriques")
        self.assertEqual((lect["lues"], lect["total"], lect["minutes_restantes"]), (2, 2, 0))
        self.assertEqual(C.indicateur(self.user, ev, self.at), "pret")
        self.assertTrue(all(b["lecture"] == 0 for b in C.proposer(self.user, ev, self.at)["blocs"]))
        # Une page seule reliée directement reste comptée, même si c'est une fiche
        seule = C.lecture(self.user, "Cours/TechniquesNumeriques/01 - Ohmmètre (Fiche)")
        self.assertEqual(seule["total"], 1)


class KitDateTests(TQ.RevisionTestCase):
    def setUp(self):
        super().setUp()
        TQ.KitTests.preparer(self)

    def lancer_et_relire(self):
        from hangar.ai import jobs
        from hangar.ai.models import AIJob
        self.claude()
        c = self.login()
        c.post("/ia/kit/", data={"_csrf": c.token, "dossier": "Cours/Elec", "evenement": self.ev.id, "nombre": "10",
                                 "qcm": ["Cours/Elec/Ohm"]})
        qcm = AIJob.query.filter_by(kind="qcm").one()
        self.fake.replies.append(claude_msg([json.dumps(QUESTIONS_IA, ensure_ascii=False)]))
        jobs.run_pending()
        db.session.refresh(qcm)
        self.assertEqual(qcm.status, "draft")
        return qcm

    def date_publiee(self, qcm):
        from hangar.ai import travaux
        from hangar.qcm.models import QuizDeck
        chemin = travaux.publier(qcm)
        return QuizDeck.query.filter_by(user_id=self.admin.id, set_id=chemin.removesuffix(".json")).one().date_objectif

    def test_controle_deplace_avant_la_publication(self):
        qcm = self.lancer_et_relire()
        self.assertEqual(qcm.payload["evenement"], self.ev.id)
        nouveau = self.jour - timedelta(days=2)
        self.ev.start, self.ev.end = nouveau, nouveau + timedelta(hours=2)  # un import a avancé le contrôle
        db.session.commit()
        self.assertEqual(self.date_publiee(qcm), nouveau.date())

    def test_controle_retire_prochain_controle_de_la_matiere(self):
        qcm = self.lancer_et_relire()
        suivant = self.jour + timedelta(days=7)
        db.session.add(AgendaEvent(user_id=self.admin.id, uid="ctrl2", title="Électricité", kind="CO_ORAL",
                                   start=suivant, end=suivant + timedelta(hours=1)))
        self.ev.status = "removed"
        db.session.commit()
        self.assertEqual(self.date_publiee(qcm), suivant.date())

    def test_controle_retire_sans_suivant_pas_de_date(self):
        from hangar.qcm.models import QuizDeck
        qcm = self.lancer_et_relire()
        db.session.delete(self.ev)
        db.session.commit()
        from hangar.ai import travaux
        chemin = travaux.publier(qcm)
        deck = QuizDeck.query.filter_by(user_id=self.admin.id, set_id=chemin.removesuffix(".json")).first()
        self.assertTrue(deck is None or deck.date_objectif is None)


def load_tests(loader, tests, pattern):
    """Seulement les tests de ce fichier (pas ceux des classes de base importées)."""
    suite = unittest.TestSuite()
    for cls in (BlocsEtNotesTests, KitDateTests):
        for nom in sorted(vars(cls)):
            if nom.startswith("test_"):
                suite.addTest(cls(nom))
    return suite


if __name__ == "__main__":
    unittest.main()
