"""Parité du wiki avec Revision : lecture hors ligne (service worker, manifeste, page « hors connexion »,
dossier gardé hors ligne), palette de commandes, documentation, erreurs du serveur, anciennes adresses,
panneau « Réviser ce cours », tuteur sous la page, mode lecture et menu du wiki.

Lancer : uv run python -m unittest tests.test_wiki_parite -v
"""
import json
import re
from pathlib import Path
from unittest import mock

from test_ai import AITestCase, claude_msg
from test_wiki import ClientIsole, WikiTestCase, pdf_texte

from hangar.extensions import db
from hangar.models import User
from hangar.wiki import erreurs
from hangar.wiki.models import ErreurServeur
from hangar.wiki.vault import invalider
from hangar.wiki.vues_revision import adapter_liens

COURS = """---
tags: [physique]
---
# Loi d'Ohm

> [!definition] Résistance
> La résistance $R$ s'exprime en ohms : $U = R I$.

La ==tension== se mesure en volts.
"""
SET_OHM = {"titre": "Loi d'Ohm", "source_page": "Cours/Ohm", "questions": [
    {"id": "q1", "type": "QCM", "question": "Unité de la résistance ?", "choix": ["volt", "ohm"], "reponses": [1]},
    {"id": "q2", "type": "FLASHCARD", "question": "U = ?", "verso": "R I"},
]}
LANGUE = {"langue": "Anglais", "titre": "Anglais — test", "vocabulaire": [
    {"id": "v1", "mot": "dog", "traductions": ["chien"]}, {"id": "v2", "mot": "cat", "traductions": ["chat"]}]}


class PariteTestCase(WikiTestCase):
    def setUp(self):
        super().setUp()
        self.write("Accueil.md", "# Accueil\n\nBienvenue.")
        self.write("Cours/Ohm.md", COURS)
        self.write("Cours/Électricité.md", "# Électricité\n\nVoir [[Ohm]].")
        (self.root / "Cours" / "Support.pdf").write_bytes(pdf_texte("Loi d'Ohm\nU = R I"))
        (self.root / "Cours" / "schema.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        self.write("Autre/Note.md", "# Note")
        self.write("QCM/Physique/ohm.json", json.dumps(SET_OHM, ensure_ascii=False))
        self.write("QCM/langues/anglais.json", json.dumps(LANGUE, ensure_ascii=False))
        invalider()

    def utilisateur(self, nom, role):
        u = User(username=nom, email=f"{nom}@hangar.local", role=role, first_name=nom.title())
        u.set_password("secret1")
        db.session.add(u)
        db.session.commit()
        return self.login(nom)


class HorsLigneTests(PariteTestCase):
    def test_service_worker_sans_compte(self):
        anonyme = self.app.test_client()
        r = anonyme.get("/sw.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn("javascript", r.headers["Content-Type"])
        self.assertEqual(r.headers["Cache-Control"], "no-cache")
        js = r.get_data(as_text=True)
        self.assertIn('const CACHE = "hangar-wiki-v1"', js)
        self.assertIn('"/hors-connexion/"', js)
        # Ce qu'il faut pour afficher une page sans réseau, à l'adresse exacte des gabarits
        for statique in ("/static/css/app.css?v=", "/static/js/app.js?v=", "/static/wiki/wiki.css?v=",
                         "/static/wiki/palette.js?v=", "/static/vendor/katex/katex.min.js", "/static/wiki/icone-192.png"):
            self.assertIn(statique, js)
        self.assertIn("KaTeX_Main-Regular.woff2", js)
        # Seules les adresses de lecture du wiki : ni POST, ni API, ni QCM, ni autres sections
        self.assertIn('if (request.method !== "GET") return;', js)
        pages = re.search(r"const PAGES = \[(.*?)\];", js, re.S).group(1)
        ressources = re.search(r"const RESSOURCES = \[(.*?)\];", js, re.S).group(1)
        for chemin in ("wiki", "pdf", "sommaire", "tags", "documentation", "hors-connexion"):
            self.assertIn(chemin, pages)
        for interdit in ("api", "qcm", "agenda", "ia", "modifier", "importer", "recherche"):
            self.assertNotIn(f"\\/{interdit}", pages + ressources)
        self.assertNotIn("Coffre", js)  # aucun contenu du wiki

    def test_manifeste_et_icones(self):
        anonyme = self.app.test_client()
        r = anonyme.get("/manifest.webmanifest")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Content-Type"], "application/manifest+json")
        m = json.loads(r.get_data(as_text=True))
        self.assertEqual((m["start_url"], m["scope"], m["display"], m["lang"]), ("/wiki/", "/", "standalone", "fr"))
        self.assertEqual([i["sizes"] for i in m["icons"]], ["192x192", "512x512"])
        for icone in m["icons"]:
            self.assertEqual(self.client.get(icone["src"]).status_code, 200)
        # Les pages du wiki annoncent le manifeste et le service worker
        page = self.client.get("/wiki/Cours/Ohm").get_data(as_text=True)
        self.assertIn('<link rel="manifest" href="/manifest.webmanifest">', page)
        self.assertIn('"sw": "/sw.js"', page)

    def test_page_hors_connexion(self):
        r = self.app.test_client().get("/hors-connexion/")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("Pas de connexion", html)
        self.assertIn("data-pages-hors-ligne", html)
        self.assertIn('data-cache="hangar-wiki-v1"', html)
        self.assertIn("hors_ligne_wiki.js", html)
        self.assertNotIn("Loi d'Ohm", html)  # rien du wiki dans la page elle-même

    def test_dossier_hors_ligne(self):
        r = self.client.get("/hors-ligne/dossier.json?dossier=Cours")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual((d["dossier"], d["pages"], d["pdf"]), ("Cours", 2, 1))
        urls = d["urls"]
        self.assertIn("/wiki/Cours/Ohm", urls)
        self.assertIn("/fichiers/Cours/Support.pdf", urls)
        self.assertIn("/pdf/Cours/Support.pdf", urls)
        self.assertIn("/fichiers/Cours/schema.png", urls)
        self.assertTrue(any(u.startswith("/static/wiki/vendor/pdfjs/pdf.worker") for u in urls))  # lecteur de PDF
        self.assertNotIn("/wiki/Autre/Note", urls)
        tout = self.client.get("/hors-ligne/dossier.json").get_json()
        self.assertIn("/wiki/Autre/Note", tout["urls"])
        self.assertEqual(self.client.get("/hors-ligne/dossier.json?dossier=Inconnu").status_code, 404)
        self.assertEqual(self.app.test_client().get("/hors-ligne/dossier.json").status_code, 302)  # compte requis

    def test_sommaire_bouton_hors_ligne(self):
        html = self.client.get("/sommaire/").get_data(as_text=True)
        self.assertIn('data-dossier-url="/hors-ligne/dossier.json"', html)
        self.assertIn('summary data-dossier="Cours"', html)
        self.assertIn("hors_ligne_wiki.js", html)


class PaletteTests(PariteTestCase):
    def test_palette_json(self):
        r = self.client.get("/palette.json")
        self.assertEqual(r.status_code, 200)
        self.assertIn("private", r.headers["Cache-Control"])
        d = r.get_json()
        pages = {p["t"]: p for p in d["pages"]}
        self.assertEqual(pages["Ohm"]["u"], "/wiki/Cours/Ohm")
        self.assertEqual(pages["Support"]["u"], "/pdf/Cours/Support.pdf")
        self.assertTrue(pages["Support"]["s"].startswith("PDF"))
        self.assertFalse(any(p["s"].startswith("QCM") for p in d["pages"]))
        qcm = {q["t"]: q for q in d["qcm"]}
        self.assertIn("/qcm/jouer/?set=QCM/Physique/ohm", qcm["Loi d'Ohm"]["u"].replace("%2F", "/"))
        self.assertIn("mode=all", qcm["Loi d'Ohm"]["u"])
        langues = [x["t"] for x in d["langues"]]
        self.assertEqual(langues, ["Session du jour · Anglais", "Révision intensive · Anglais", "Modes et statistiques · Anglais"])
        self.assertIn("mode=quotidien", d["langues"][0]["u"])
        commandes = {c["t"]: c for c in d["commandes"]}
        for titre in ("Aujourd'hui (révisions du jour)", "Nouvelle page", "Importer des fichiers (.md, PDF)", "Plein écran",
                      "Modifier cette page", "Révision intelligente (QCM)", "Documentation", "Erreurs du serveur"):
            self.assertIn(titre, commandes)
        self.assertEqual(commandes["Plein écran"]["a"], "fullscreen")
        self.assertEqual(commandes["Modifier cette page"]["a"], "edit")
        self.assertEqual(commandes["Aujourd'hui (révisions du jour)"]["u"], "/qcm/aujourdhui/")
        self.assertNotIn("Demander au wiki (IA)", commandes)  # pas d'IA configurée
        self.assertIn("<svg", d["icones"]["pdf"])

    def test_palette_droits(self):
        invite = self.utilisateur("invite", "guest")
        commandes = {c["t"] for c in invite.get("/palette.json").get_json()["commandes"]}
        self.assertNotIn("Nouvelle page", commandes)
        self.assertNotIn("Modifier cette page", commandes)
        self.assertNotIn("Erreurs du serveur", commandes)
        self.assertIn("Aujourd'hui (révisions du jour)", commandes)
        membre = self.utilisateur("alice", "member")
        commandes = {c["t"] for c in membre.get("/palette.json").get_json()["commandes"]}
        self.assertIn("Nouvelle page", commandes)
        self.assertNotIn("Erreurs du serveur", commandes)

    def test_palette_branchee_sur_celle_de_hangar(self):
        page = self.client.get("/agenda/").get_data(as_text=True)
        self.assertIn("/static/wiki/palette.js", page)  # sur toutes les pages, pas seulement le wiki
        racine = Path(self.app.root_path)
        app_js = (racine / "static" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("H.paletteSources", app_js)
        palette_js = (racine / "static" / "wiki" / "palette.js").read_text(encoding="utf-8")
        self.assertIn("paletteSources", palette_js)
        # Recherche tolérante aux accents (décomposition NFKD, comme hangar/texte.py, plier).
        self.assertRegex(palette_js, r"""normalize\(["']NFK?D["']\)""")


class DocumentationTests(PariteTestCase):
    def test_onglets(self):
        for url, attendu in (("/documentation/", "Le wiki de Hangar"), ("/documentation/ia/", "Guide pour les IA"),
                             ("/documentation/qcm/", "Guide des QCM"), ("/documentation/syntaxe/", "Syntaxe"),
                             ("/documentation/hangar/", "Hangar")):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            html = r.get_data(as_text=True)
            self.assertIn(attendu, html)
            for onglet in ('href="/documentation/"', 'href="/documentation/ia/"', 'href="/documentation/qcm/"'):
                self.assertIn(onglet, html)
        self.assertEqual(self.client.get("/documentation/inconnue/").status_code, 404)
        ia = self.client.get("/documentation/ia/").get_data(as_text=True)
        for revision in ("Raspberry", "docker compose", "manage.py", "prompts_ia"):
            self.assertNotIn(revision, ia)  # adapté à Hangar
        self.assertIn("hangar qcm-verifier", ia)
        self.assertIn('href="/documentation/syntaxe/"', ia)

    def test_liens_reecrits(self):
        texte = ("[IA](README-IA.md) [qcm](vault/Guide/QCM.md) [cours](vault/Cours/BDD/5.Jointure.md) "
                 "[coffre](vault/) [script](scripts/serve-lan.ps1) [site](https://exemple.fr) [abs](/qcm/) "
                 "`[code](README-IA.md)` [hangar](README.md#commandes)")
        sortie = adapter_liens(texte)
        self.assertIn("[IA](/documentation/ia/)", sortie)
        self.assertIn("[qcm](/documentation/qcm/)", sortie)
        self.assertIn("[cours](/wiki/Cours/BDD/5.Jointure)", sortie)
        self.assertIn("[coffre](/sommaire/)", sortie)
        self.assertIn(" script ", sortie)  # fichier du dépôt : le texte seul
        self.assertIn("[site](https://exemple.fr)", sortie)
        self.assertIn("[abs](/qcm/)", sortie)
        self.assertIn("`[code](README-IA.md)`", sortie)  # code laissé tel quel
        self.assertIn("[hangar](/documentation/hangar/#commandes)", sortie)


class ErreursTests(PariteTestCase):
    def provoquer(self, message="Coffre en panne", client=None):
        self.app.config["PROPAGATE_EXCEPTIONS"] = False
        with mock.patch("hangar.wiki.views.get_vault", side_effect=RuntimeError(message)), \
                mock.patch.object(self.app.logger, "error"):  # trace attendue : pas dans la sortie des tests
            return (client or self.client).get("/tags/?x=1")

    def test_capture_regroupement_et_page(self):
        r = self.provoquer()
        self.assertEqual(r.status_code, 500)
        e = ErreurServeur.query.one()
        self.assertEqual((e.type, e.message, e.nombre, e.methode, e.utilisateur, e.reglee),
                         ("RuntimeError", "Coffre en panne", 1, "GET", "moi", False))
        self.assertEqual(e.chemin, "/tags/?x=1")
        self.assertTrue(e.lieu.startswith("hangar/wiki/views.py:"), e.lieu)
        self.assertIn("Traceback", e.trace)
        self.provoquer()
        self.assertEqual(ErreurServeur.query.one().nombre, 2)  # même empreinte : regroupée
        self.provoquer("Autre panne")
        self.assertEqual(ErreurServeur.query.count(), 2)
        self.assertEqual(erreurs.ouvertes(), 2)
        # Page des administrateurs, et badge dans le menu des réglages
        html = self.client.get("/erreurs/").get_data(as_text=True)
        self.assertIn("Coffre en panne", html)
        self.assertIn("2 fois", html)
        self.assertIn("hangar/wiki/views.py:", html)
        reglages = self.client.get("/settings/profile").get_data(as_text=True)
        self.assertIn('href="/erreurs/"', reglages)
        self.assertRegex(reglages, r'sb-badge danger[^>]*>2<')
        # Réglée, rouverte si elle revient ; tout régler ; effacer
        pk = ErreurServeur.query.filter_by(message="Coffre en panne").one().id
        self.post("/erreurs/", {"action": "reglee", "pk": pk})
        self.assertTrue(db.session.get(ErreurServeur, pk).reglee)
        self.provoquer()
        db.session.expire_all()
        self.assertFalse(db.session.get(ErreurServeur, pk).reglee)
        self.post("/erreurs/", {"action": "tout_regle"})
        self.assertEqual(erreurs.ouvertes(), 0)
        self.assertIn("Aucune erreur à regarder", self.client.get("/erreurs/").get_data(as_text=True))
        self.post("/erreurs/", {"action": "effacer"})
        self.assertEqual(ErreurServeur.query.count(), 0)

    def test_reservee_aux_admins(self):
        membre = self.utilisateur("alice", "member")
        self.assertEqual(membre.get("/erreurs/").status_code, 403)
        self.assertNotIn('href="/erreurs/"', membre.get("/settings/profile").get_data(as_text=True))
        r = self.provoquer(client=membre)
        self.assertEqual(r.status_code, 500)
        self.assertEqual(ErreurServeur.query.one().utilisateur, "alice")

    def test_api_en_erreur(self):
        self.app.config["PROPAGATE_EXCEPTIONS"] = False
        with mock.patch("hangar.views.api._wiki_hits", side_effect=ValueError("API cassée")), \
                mock.patch("hangar.views.api.search_everything", side_effect=ValueError("API cassée")), \
                mock.patch.object(self.app.logger, "error"):
            r = self.client.get("/api/search?q=ohm")
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.get_json()["ok"], False)
        self.assertEqual(ErreurServeur.query.one().type, "ValueError")

    def test_incident_du_fil_des_travaux(self):
        from hangar.ai import jobs
        from hangar.ai.models import AIJob

        @jobs.register_job("test-parite-boum")
        def _boum(job, ai):
            raise KeyError("champ manquant")

        job = jobs.enqueue("test-parite-boum", {}, start=False)
        with mock.patch.object(jobs, "log"):
            jobs.run_pending(limit=1)
        db.session.expire_all()
        self.assertEqual(db.session.get(AIJob, job.id).status, "error")
        e = ErreurServeur.query.one()
        self.assertEqual(e.type, "KeyError")
        self.assertEqual(e.chemin, f"Travail d'IA {job.id} (test-parite-boum)")
        self.assertTrue(e.lieu.startswith("tests/test_wiki_parite.py:"), e.lieu)


class AliasTests(PariteTestCase):
    def test_anciennes_adresses_de_revision(self):
        for ancienne, nouvelle in (("/claude/reglages/", "/ia/reglages/"), ("/aujourdhui/", "/qcm/aujourdhui/"),
                                   ("/compte/", "/settings/profile"), ("/compte/mot-de-passe/", "/settings/profile#password"),
                                   ("/utilisateurs/", "/team/")):
            r = self.client.get(ancienne)
            self.assertEqual(r.status_code, 302, ancienne)
            self.assertTrue(r.headers["Location"].endswith(nouvelle), (ancienne, r.headers["Location"]))
            self.assertEqual(self.client.get(ancienne, follow_redirects=True).status_code, 200, ancienne)
        r = self.client.get("/aujourdhui/?jour=2026-11-10")
        self.assertTrue(r.headers["Location"].endswith("/qcm/aujourdhui/?jour=2026-11-10"))
        r = self.app.test_client().get("/connexion/")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/login"))
        # Adresses de Revision gardées telles quelles
        for url in ("/qcm/", "/demander/", "/documentation/ia/", "/sommaire/", "/recherche/?q=ohm", "/graphe/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)


class LecturePageTests(PariteTestCase):
    def test_mode_lecture_et_epingle(self):
        html = self.client.get("/wiki/Cours/Ohm").get_data(as_text=True)
        self.assertRegex(html, r'<body class="[^"]*\bmenu-cache\b')
        self.assertIn("data-imprimer", html)
        pdf = self.client.get("/pdf/Cours/Support.pdf").get_data(as_text=True)
        self.assertRegex(pdf, r'<body class="[^"]*\bmenu-cache\b')
        self.assertNotRegex(self.client.get("/sommaire/").get_data(as_text=True), r'<body class="[^"]*\bmenu-cache\b')
        self.client.set_cookie("wiki_menu", "epingle")
        html = self.client.get("/wiki/Cours/Ohm").get_data(as_text=True)
        self.assertNotRegex(html, r'<body class="[^"]*\bmenu-cache\b')
        self.assertIn('data-epingler-menu aria-pressed="true"', html)

    def test_reviser_ce_cours(self):
        html = self.client.get("/wiki/Cours/Ohm").get_data(as_text=True)
        self.assertIn("Réviser ce cours", html)
        self.assertIn("Loi d&#39;Ohm", html)
        self.assertIn("2 questions · maîtrise 0 %", html)
        self.assertRegex(html, r'href="/qcm/jouer/\?set=QCM(/|%2F)Physique(/|%2F)ohm&amp;mode=review">.*Réviser \(2\)')
        self.assertIn("mode=examen", html)
        self.assertIn("Créer les cartes de la page (2)", html)  # un encadré à titre et un surlignage
        self.assertIn("Cache-Control", self.client.get("/wiki/Cours/Ohm").headers)
        self.assertIn("private", self.client.get("/wiki/Cours/Ohm").headers["Cache-Control"])
        # Sans QCM ni encadré, et sans IA : pas de panneau
        html = self.client.get("/wiki/Autre/Note").get_data(as_text=True)
        self.assertNotIn("Réviser ce cours", html)
        self.assertNotIn('id="tuteur"', html)

    def test_cartes_reservees_aux_editeurs(self):
        invite = self.utilisateur("invite", "guest")
        html = invite.get("/wiki/Cours/Ohm").get_data(as_text=True)
        self.assertIn("Réviser ce cours", html)  # le QCM de la page reste proposé
        self.assertNotIn("cartes de la page", html)


class MenuTests(PariteTestCase):
    def test_section_revision(self):
        html = self.client.get("/wiki/Cours/Ohm").get_data(as_text=True)
        menu = html[html.index('class="sb-scroll wiki-sb"'):html.index("</nav>", html.index('class="sb-scroll wiki-sb"'))]
        for lien in ('href="/qcm/aujourdhui/"', 'href="/qcm/"', 'href="/qcm/langues/"', 'href="/documentation/"',
                     'href="/transformations/"', 'href="/hors-connexion/"'):
            self.assertIn(lien, menu)
        self.assertNotIn('href="/demander/"', menu)  # pas d'IA configurée
        self.assertNotIn('href="/copie/"', menu)
        self.assertIn("data-epingler-menu", html)
        self.assertIn("data-plein-ecran", html)
        invite = self.utilisateur("invite", "guest")
        html = invite.get("/wiki/Cours/Ohm").get_data(as_text=True)
        self.assertNotIn('href="/transformations/"', html)
        self.assertIn('href="/qcm/aujourdhui/"', html)


class TuteurTests(AITestCase):
    def setUp(self):
        super().setUp()
        self.app.test_client_class = ClientIsole  # menu de gauche recalculé à chaque requête (g vidé)
        racine = Path(self.app.config["WIKI_DIR"])
        (racine / "Cours").mkdir(parents=True, exist_ok=True)
        (racine / "Cours" / "Ohm.md").write_text(COURS, encoding="utf-8")
        invalider()

    def test_tuteur_sous_la_page(self):
        c = self.login("invite")  # le tuteur est ouvert à tous les comptes
        html = c.get("/wiki/Cours/Ohm").get_data(as_text=True)
        self.assertNotIn('id="tuteur"', html)  # aucun fournisseur d'IA
        self.claude()
        html = c.get("/wiki/Cours/Ohm").get_data(as_text=True)
        self.assertIn('id="tuteur" data-tuteur-url="/claude/tuteur/" data-page="Cours/Ohm"', html)
        self.assertIn("wiki/tuteur.js", html)
        self.assertIn('href="/copie/?page=Cours/Ohm"', html.replace("%2F", "/"))
        self.assertIn('data-ai-ask="tuteur"', html)  # commande du panneau gardée
        self.fake.replies.append(claude_msg(["La résistance se mesure en **ohms**."], model="claude-haiku-5-5"))
        r = c.post("/claude/tuteur/", json={"page": "Cours/Ohm", "question": "Unité de R ?"},
                   headers={"X-CSRF-Token": c.token})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("<strong>ohms</strong>", r.get_json()["html"])
        html = c.get("/wiki/Cours/Ohm").get_data(as_text=True)
        self.assertIn('<p class="tuteur-question">Unité de R ?</p>', html)
        self.assertIn("<strong>ohms</strong>", html)
        # Les liens d'IA du menu du wiki apparaissent
        self.assertIn('href="/demander/"', html)
        self.assertIn('href="/copie/"', html)
        # Plafond atteint : plus de formulaire
        from hangar.ai.models import AISettings, AIUsage
        AISettings.get().monthly_cap = 0.000001
        db.session.add(AIUsage(cost=1.0))
        db.session.commit()
        html = c.get("/wiki/Cours/Ohm").get_data(as_text=True)
        self.assertIn("Plafond mensuel de l’IA atteint", html)
        self.assertNotIn("data-tuteur-form", html)

    def test_palette_avec_ia(self):
        self.claude()
        c = self.login("moi")
        commandes = {x["t"] for x in c.get("/palette.json").get_json()["commandes"]}
        self.assertIn("Demander au wiki (IA)", commandes)
        self.assertIn("Corriger ma copie (photo, IA)", commandes)
        self.assertIn("Transformer un cours en page avec l'IA", commandes)
