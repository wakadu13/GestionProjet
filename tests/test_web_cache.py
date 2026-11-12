"""Vague 2b, groupe « web-cache » : poids des pages et cache (piste 50), en-têtes de sécurité et données
gardées sur l'appareil (piste 51).

Lancer : uv run python -m unittest tests.test_web_cache -v
"""
import gzip
import os
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import unquote
from unittest import mock

from flask import Response, g, url_for
from flask.testing import FlaskClient

from hangar import create_app, web
from hangar.extensions import db
from hangar.icons import ICONS, icon
from hangar.models import User
from hangar.wiki import vault as wiki_vault
from hangar.wiki.models import WikiVisit

STATIC = Path(__file__).resolve().parent.parent / "hangar" / "static"


class ClientIsole(FlaskClient):
    """Chaque requête repart d'un ``g`` vide (le contexte de test poussé dans setUp est partagé)."""

    def open(self, *args, **kwargs):
        for cle in list(g):
            g.pop(cle, None)
        return super().open(*args, **kwargs)


class WebTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-web-test-")
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for var in ("HANGAR_VAULT", "HANGAR_CSP", "HANGAR_COMPRESSION"):
            os.environ.pop(var, None)
        self.app = create_app(self.tmp, testing=True)
        self.routes_d_essai(self.app)
        self.app.test_client_class = ClientIsole
        self.app.config.update(WIKI_VERIF_INTERVALLE=0, QCM_VERIF_INTERVALLE=0)
        self.root = Path(self.app.config["WIKI_DIR"])
        self.ctx = self.app.test_request_context("/")
        self.ctx.push()
        self.user = User(username="moi", email="moi@hangar.local", role="admin", first_name="Moi")
        self.user.set_password("secret1")
        db.session.add(self.user)
        db.session.commit()
        self.client = self.login()

    def routes_d_essai(self, app):
        """Routes propres à un test (à ajouter avant la première requête)."""

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def login(self, username="moi", password="secret1", **kw):
        c = self.app.test_client()
        page = c.get("/login", **kw).get_data(as_text=True)
        token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        r = c.post("/login", data={"login": username, "password": password, "_csrf": token}, **kw)
        self.assertEqual(r.status_code, 302)
        with c.session_transaction() as s:
            s["_csrf"] = "jeton-test"
        return c

    def write(self, path, text):
        f = self.root / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
        return f


# ---------------------------------------------------------------------------
# Piste 50 — pages trop lourdes, fichiers statiques redemandés
# ---------------------------------------------------------------------------
class PrechargementTests(WebTestCase):
    """« ⤓ Hors ligne » télécharge les pages avec l'en-tête X-Hangar-Prechargement."""

    def test_prechargement_hors_des_recents(self):
        self.write("Cours/Lu.md", "# Lu\n")
        self.write("Cours/Precharge.md", "# Préchargée\n")
        self.assertEqual(self.client.get("/wiki/Cours/Lu").status_code, 200)
        r = self.client.get("/wiki/Cours/Precharge", headers={"X-Hangar-Prechargement": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Préchargée", r.get_data(as_text=True))
        cles = {v.key for v in WikiVisit.query.filter_by(user_id=self.user.id)}
        self.assertIn("Cours/Lu", cles)
        self.assertNotIn("Cours/Precharge", cles)  # avant : les pages préchargées remplaçaient les Récents
        # Les Récents du menu ne montrent que la page vraiment lue.
        page = self.client.get("/wiki/Cours/Lu").get_data(as_text=True)
        recents = page.split("Récents", 1)[1].split("sb-section", 1)[0]
        self.assertNotIn("Precharge", recents)

    def test_prechargement_sans_qcm_ni_tuteur(self):
        self.write("Cours/Page.md", "# Page\n")
        from hangar.wiki import views
        with mock.patch.object(views, "_qcm_de_la_page", return_value=[]) as qcm, \
                mock.patch.object(views, "_tuteur_de_la_page", return_value=None) as tuteur:
            self.client.get("/wiki/Cours/Page", headers={"X-Hangar-Prechargement": "1"})
            self.assertEqual((qcm.call_count, tuteur.call_count), (0, 0))
            self.client.get("/wiki/Cours/Page")
            self.assertEqual((qcm.call_count, tuteur.call_count), (1, 1))

    def test_script_hors_ligne_envoie_l_entete(self):
        js = (STATIC / "wiki" / "hors_ligne_wiki.js").read_text(encoding="utf-8")
        self.assertIn('"X-Hangar-Prechargement": "1"', js)


class MenuWikiTests(WebTestCase):
    def setUp(self):
        super().setUp()
        self.write("Accueil.md", "# Accueil\n")
        self.write("Cours/Maths/Integrales.md", "# Intégrales\n")
        self.write("Cours/Maths/Derivees.md", "# Dérivées\n")
        self.write("Cours/Physique/Optique.md", "# Optique\n")
        self.write("Hack/Linux/Shell.md", "# Shell\n")
        self.write("Hack/Notes.md", "# Notes\n")

    def menu(self, html):
        return re.search(r'<nav class="sb-scroll wiki-sb".*?</nav>', html, re.S).group(0)

    def test_menu_ne_contient_que_la_branche_ouverte(self):
        menu = self.menu(self.client.get("/wiki/Cours/Maths/Integrales").get_data(as_text=True))
        # Branche de la page : ses pages sœurs sont là.
        self.assertIn("/wiki/Cours/Maths/Derivees", menu)
        # Les autres dossiers sont annoncés, pas déroulés (avant : tout le coffre dans chaque page).
        self.assertNotIn("/wiki/Cours/Physique/Optique", menu)
        self.assertNotIn("/wiki/Hack/Linux/Shell", menu)
        self.assertNotIn("/wiki/Hack/Notes", menu)
        self.assertIn('data-menu-dossier="Cours/Physique/"', menu)
        self.assertIn('data-menu-dossier="Hack/"', menu)
        self.assertNotIn('data-menu-dossier="Cours/"', menu)  # dossier de la page : ouvert et rempli
        self.assertIn("menu_wiki.js", self.client.get("/wiki/").get_data(as_text=True))

    def test_contenu_d_un_dossier_a_la_demande(self):
        r = self.client.get("/menu-wiki/?dossier=Hack")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("/wiki/Hack/Notes", html)
        self.assertIn('data-menu-dossier="Hack/Linux/"', html)  # sous-dossier : à son tour à la demande
        self.assertNotIn("/wiki/Hack/Linux/Shell", html)
        self.assertNotIn("<html", html)  # morceau de page, pas une page entière
        self.assertIn("--d: 1", html)  # retrait du niveau
        linux = self.client.get("/menu-wiki/?dossier=Hack/Linux").get_data(as_text=True)
        self.assertIn("/wiki/Hack/Linux/Shell", linux)
        self.assertEqual(self.client.get("/menu-wiki/?dossier=Inconnu").status_code, 404)
        self.assertIn("private", r.headers["Cache-Control"])
        anonyme = self.app.test_client()
        self.assertEqual(anonyme.get("/menu-wiki/?dossier=Hack").status_code, 302)  # connexion demandée

    def test_service_worker_garde_les_dossiers_ouverts(self):
        sw = self.client.get("/sw.js").get_data(as_text=True)
        self.assertIn(r"/^\/menu-wiki\/$/", sw)

    def test_dossier_garde_hors_ligne_reste_navigable_par_le_menu(self):
        # Avant : « ⤓ Hors ligne » gardait les pages mais pas le menu chargé à la demande ; hors ligne,
        # chaque sous-dossier affichait « Impossible d'afficher ce dossier ».
        self.write("Hack/Web Request/Été (v2)!.md", "# Été\n")
        d = self.client.get("/hors-ligne/dossier.json?dossier=Hack").get_json()
        menus = [u for u in d["urls"] if u.startswith("/menu-wiki/")]
        # Écrites exactement comme menu_wiki.js les demande : encodeURIComponent(data-menu-dossier).
        self.assertIn("/menu-wiki/?dossier=Hack%2F", menus)
        self.assertIn("/menu-wiki/?dossier=Hack%2FLinux%2F", menus)
        self.assertIn("/menu-wiki/?dossier=Hack%2FWeb%20Request%2F", menus)
        self.assertEqual(len(menus), 3)
        self.assertEqual(d["pages"], 3)  # le compte des pages ne change pas
        # Chaque adresse annoncée correspond à un dossier présent dans le menu (data-menu-dossier) et répond.
        annonces = set(re.findall(r'data-menu-dossier="([^"]+)"', self.client.get("/wiki/Accueil").get_data(as_text=True)))
        annonces |= set(re.findall(r'data-menu-dossier="([^"]+)"', self.client.get("/menu-wiki/?dossier=Hack%2F").get_data(as_text=True)))
        for u in menus:
            self.assertIn(unquote(u.split("=", 1)[1]), annonces)
            self.assertEqual(self.client.get(u).status_code, 200, u)
        # Sous-dossier gardé seul : son menu, pas celui des dossiers voisins.
        linux = self.client.get("/hors-ligne/dossier.json?dossier=Hack/Linux").get_json()["urls"]
        self.assertEqual([u for u in linux if u.startswith("/menu-wiki/")], ["/menu-wiki/?dossier=Hack%2FLinux%2F"])
        # Tout le coffre : tous les dossiers.
        tout = self.client.get("/hors-ligne/dossier.json").get_json()["urls"]
        self.assertEqual(len([u for u in tout if u.startswith("/menu-wiki/")]), 6)

    def test_menu_dit_quand_la_connexion_manque(self):
        js = (Path(self.app.static_folder) / "wiki" / "menu_wiki.js").read_text(encoding="utf-8")
        self.assertIn("navigator.onLine === false", js)
        self.assertIn("Pas de connexion : ce dossier n’a pas été gardé sur cet appareil", js)


class IconesTests(WebTestCase):
    def test_icone_est_une_reference_a_la_feuille(self):
        html = str(icon("home", 18, "x", title="Accueil"))
        self.assertRegex(html, r'<use href="/icones\.svg\?v=[0-9a-f]+#home"/>')
        self.assertNotIn("<path", html)  # avant : le dessin complet à chaque emploi
        self.assertIn('width="18"', html)
        self.assertIn("<title>Accueil</title>", html)
        self.assertIn("#hash", str(icon("inconnue")))

    def test_feuille_d_icones(self):
        url = web.adresse_icones()
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content_type.startswith("image/svg+xml"))
        svg = r.get_data(as_text=True)
        for nom in ("home", "wiki-graph", "flame", "thumb-up"):  # icônes de base et ajoutées par les modules
            self.assertIn(f'<symbol id="{nom}"', svg)
        self.assertEqual(svg.count("<symbol"), len(ICONS))
        self.assertEqual(r.headers["Cache-Control"], web.CACHE_LONG)
        self.assertEqual(self.client.get("/icones.svg").headers["Cache-Control"], "no-cache")
        anonyme = self.app.test_client()
        self.assertEqual(anonyme.get(url).status_code, 200)  # page de connexion : icônes sans compte

    def test_pages_plus_legeres(self):
        projet = self.client.get("/").get_data(as_text=True)
        traits = re.findall(r'<svg class="ic [^"]*"[^>]*>(.*?)</svg>', projet, re.S)
        self.assertTrue(traits)
        self.assertTrue(all("<use href=" in t and "<path" not in t for t in traits))


class StatiquesTests(WebTestCase):
    def test_adresses_versionnees_par_le_contenu(self):
        url = url_for("static", filename="css/app.css")
        self.assertRegex(url, r"^/static/css/app\.css\?v=[0-9a-f]{10}$")
        self.assertEqual(url_for("static", filename="vendor/"), "/static/vendor/")  # dossier : pas d'empreinte
        self.assertEqual(url_for("static", filename="css/app.css", v=None), "/static/css/app.css")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn(url, page)
        self.assertNotIn("?v=1.1.0", page)  # avant : ?v=<version de Hangar>, inchangé après un « git pull »

    def test_empreinte_suit_le_fichier(self):
        dossier = Path(self.tmp) / "statique"
        dossier.mkdir()
        f = dossier / "a.js"
        f.write_text("un", encoding="utf-8")
        avant = web.jeton_statique(str(dossier), "a.js")
        f.write_text("deux !", encoding="utf-8")
        os.utime(f, ns=(time.time_ns(), time.time_ns() + 5_000_000_000))
        web._jetons.clear()
        apres = web.jeton_statique(str(dossier), "a.js")
        self.assertTrue(avant and apres and avant != apres)
        self.assertIsNone(web.jeton_statique(str(dossier), "absent.js"))
        self.assertIsNone(web.jeton_statique(str(dossier), "../a.js"))

    def test_cache_long_seulement_avec_la_bonne_empreinte(self):
        url = url_for("static", filename="js/app.js")
        r = self.client.get(url)
        self.assertEqual(r.headers["Cache-Control"], "public, max-age=31536000, immutable")
        self.assertEqual(self.client.get("/static/js/app.js").headers["Cache-Control"], "no-cache")
        self.assertEqual(self.client.get("/static/js/app.js?v=perimee").headers["Cache-Control"], "no-cache")
        # Le réglage global de Flask n'est pas touché : pièces jointes, sauvegardes, .ics restent hors cache long.
        self.assertIsNone(self.app.config["SEND_FILE_MAX_AGE_DEFAULT"])
        self.write("image.png", "png")
        fichier = self.client.get("/fichiers/image.png")
        self.assertNotIn("max-age=31536000", fichier.headers.get("Cache-Control", ""))
        self.assertIn("private", fichier.headers["Cache-Control"])

    def test_listes_des_service_workers_identiques_aux_pages(self):
        self.write("Accueil.md", "# Accueil\n")
        page = self.client.get("/wiki/").get_data(as_text=True)
        sw = self.client.get("/sw.js").get_data(as_text=True)
        precharge = re.search(r"const A_PRECHARGER = (\[.*?\]);", sw).group(1)
        for nom in ("css/app.css", "js/app.js", "wiki/wiki.js", "wiki/menu_wiki.js", "js/appareil.js"):
            adresse = url_for("static", filename=nom)
            self.assertIn(adresse, page, nom)
            self.assertIn(f'"{adresse}"', precharge, nom)
        self.assertIn(web.adresse_icones(), precharge)
        # Bibliothèques chargées par wiki.js : mêmes adresses versionnées que celles préchargées.
        for adresse in re.search(r'"fichiers": (\{.*?\})', page).group(1).split('"'):
            if adresse.startswith("/static/") and "highlight" in adresse or "katex.min" in adresse:
                self.assertIn(adresse, precharge)
        self.assertIn('"/static/vendor/katex/fonts/KaTeX_Main-Regular.woff2"', precharge)  # lues par la feuille
        self.assertIn("cacheDabord", sw)
        qsw = self.client.get("/qcm/sw.js").get_data(as_text=True)
        self.assertIn(f'"{url_for("static", filename="qcm/quiz.js")}"', qsw)
        self.assertIn(f'"{web.adresse_icones()}"', qsw)
        self.assertNotIn('+ V,', qsw)
        self.assertIn('"/static/vendor/katex/fonts/KaTeX_Main-Regular.woff2"', qsw)
        hl = self.client.get("/qcm/hors-ligne/").get_data(as_text=True)
        for adresse in re.findall(r'(?:src|href)="(/static/[^"]+)"', hl):
            self.assertIn(adresse, qsw)


class ParcoursTests(WebTestCase):
    @staticmethod
    def ancien_scan(root):
        """Parcours d'avant (os.walk + Path.stat), pour comparer."""
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in wiki_vault.IGNORED_DIRS and not d.startswith(".")]
            for filename in filenames:
                if filename.startswith("."):
                    continue
                full = Path(dirpath, filename)
                try:
                    stat = full.stat()
                except OSError:
                    continue
                files.append((full.relative_to(root).as_posix(), stat.st_mtime_ns, stat.st_size))
        return tuple(sorted(files))

    def test_scandir_meme_resultat(self):
        for chemin in ("A.md", "Dossier/B.md", "Dossier/Sous/C.png", ".cache/x.md", "Dossier/.cachee.md",
                       "__pycache__/y.md", ".obsidian/app.json", "QCM/set.json", "É accents/note é.md"):
            self.write(chemin, "x")
        if hasattr(os, "symlink"):
            try:
                os.symlink(self.root / "Dossier", self.root / "Lien", target_is_directory=True)
            except OSError:
                pass
        self.assertEqual(wiki_vault.scan(self.root), self.ancien_scan(self.root))
        self.assertNotIn(".cache/x.md", [f[0] for f in wiki_vault.scan(self.root)])

    def test_un_seul_parcours_pour_le_wiki_et_les_qcm(self):
        from hangar.qcm import coffre
        self.write("Cours/A.md", "# A\n")
        self.app.config.update(WIKI_VERIF_INTERVALLE=60, QCM_VERIF_INTERVALLE=60)
        wiki_vault.invalider()
        vrai, vrai_qcm = wiki_vault.scan, coffre.scan

        def compter():
            # Parcours du wiki et (avant) parcours propre aux QCM : tous comptés.
            return (mock.patch.object(wiki_vault, "scan", side_effect=vrai),
                    mock.patch.object(coffre, "scan", side_effect=vrai_qcm))

        a, b = compter()
        with a as parcours_wiki, b as parcours_qcm:
            wiki_vault.get_vault()
            coffre.signature(coffre.racine())
            coffre.signature(coffre.racine())
        # Avant : un parcours pour le wiki, un autre pour les QCM.
        self.assertEqual(parcours_wiki.call_count + parcours_qcm.call_count, 1)
        # Une écriture des QCM est vue par les deux, toujours avec un seul parcours.
        coffre.invalider()
        a, b = compter()
        with a as parcours_wiki, b as parcours_qcm:
            coffre.signature(coffre.racine())
            wiki_vault.get_vault()
        self.assertEqual(parcours_wiki.call_count + parcours_qcm.call_count, 1)


class CompressionTests(WebTestCase):
    def routes_d_essai(self, app):
        def flux():
            yield "data: un\n\n" * 200
            yield "data: deux\n\n"
        app.add_url_rule("/essai-flux", "essai_flux", lambda: Response(flux(), mimetype="text/event-stream"))

    def test_pages_compressees_pour_un_autre_appareil(self):
        distant = {"REMOTE_ADDR": "192.168.1.20"}
        r = self.client.get("/", headers={"Accept-Encoding": "gzip"}, environ_base=distant)
        self.assertEqual(r.headers.get("Content-Encoding"), "gzip")
        self.assertIn("Accept-Encoding", r.headers["Vary"])
        # Texte stable de la page (pas la salutation, qui dépend de l'heure).
        corps = gzip.decompress(r.get_data()).decode()
        self.assertIn("<title>", corps)
        self.assertIn("</html>", corps)
        # Même machine : pas de compression (aucun gain).
        self.assertNotIn("Content-Encoding", self.client.get("/", headers={"Accept-Encoding": "gzip"}).headers)
        # Navigateur qui ne la demande pas.
        self.assertNotIn("Content-Encoding", self.client.get("/", environ_base=distant).headers)

    def test_fichier_statique_compresse(self):
        self.app.config["COMPRESSION"] = "toujours"
        url = url_for("static", filename="css/app.css")
        r = self.client.get(url, headers={"Accept-Encoding": "gzip"})
        self.assertEqual(r.headers["Content-Encoding"], "gzip")
        self.assertEqual(gzip.decompress(r.get_data()), (STATIC / "css" / "app.css").read_bytes())
        self.assertTrue(r.headers["ETag"].startswith('W/"'))
        self.assertEqual(r.headers["Cache-Control"], web.CACHE_LONG)
        r.close()
        etag = r.headers["ETag"]
        r2 = self.client.get("/static/css/app.css", headers={"Accept-Encoding": "gzip", "If-None-Match": etag})
        self.assertEqual(r2.status_code, 304)  # revalidation toujours possible

    def test_flux_de_l_ia_jamais_compresse(self):
        self.app.config["COMPRESSION"] = "toujours"
        r = self.client.get("/essai-flux", headers={"Accept-Encoding": "gzip"})
        self.assertNotIn("Content-Encoding", r.headers)
        self.assertEqual(r.get_data(as_text=True), "data: un\n\n" * 200 + "data: deux\n\n")


# ---------------------------------------------------------------------------
# Piste 51 — en-têtes de sécurité, cache hors ligne après la déconnexion
# ---------------------------------------------------------------------------
class EntetesTests(WebTestCase):
    def test_entetes_sur_chaque_reponse(self):
        self.write("Accueil.md", "# Accueil\n")
        for url in ("/", "/home", "/wiki/", "/qcm/", "/ia/", "/settings/backup", "/static/css/app.css", "/sw.js"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff", url)
            self.assertEqual(r.headers.get("X-Frame-Options"), "SAMEORIGIN", url)
            self.assertEqual(r.headers.get("Referrer-Policy"), "same-origin", url)
            csp = r.headers.get("Content-Security-Policy", "")
            for directive in ("default-src 'self'", "connect-src 'self'", "object-src 'none'", "base-uri 'self'",
                              "frame-ancestors 'self'", "form-action 'self'"):
                self.assertIn(directive, csp, url)
            self.assertNotIn("unsafe-inline", csp.split("script-src", 1)[1].split(";", 1)[0], url)
        anonyme = self.app.test_client()
        self.assertIn("Content-Security-Policy", anonyme.get("/login").headers)

    def test_scripts_en_ligne_signes_par_le_nonce(self):
        self.write("Accueil.md", "# Accueil\n")
        for url in ("/", "/wiki/", "/agenda/tasks", "/ia/", "/settings/mail", "/mail/compose"):
            r = self.client.get(url)
            nonce = re.search(r"'nonce-([^']+)'", r.headers["Content-Security-Policy"]).group(1)
            html = r.get_data(as_text=True)
            scripts = re.findall(r"<script(?![^>]*\bsrc=)(?![^>]*application/json)([^>]*)>", html)
            self.assertTrue(scripts, url)
            for attributs in scripts:
                self.assertIn(f'nonce="{nonce}"', attributs, url)
            self.assertIsNone(re.search(r"\son(click|load|error|submit|change)=", html), url)
            self.assertNotIn('href="javascript:', html, url)
        # Deux requêtes, deux jetons.
        n1 = re.search(r"'nonce-([^']+)'", self.client.get("/").headers["Content-Security-Policy"]).group(1)
        n2 = re.search(r"'nonce-([^']+)'", self.client.get("/").headers["Content-Security-Policy"]).group(1)
        self.assertNotEqual(n1, n2)

    def test_gabarits_sans_script_en_ligne_non_signe(self):
        gabarits = Path(__file__).resolve().parent.parent / "hangar" / "templates"
        for f in gabarits.rglob("*.html"):
            texte = f.read_text(encoding="utf-8")
            for balise in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", texte):
                if "application/json" in balise:
                    continue
                self.assertIn('nonce="{{ csp_nonce() }}"', balise, f.name)
            self.assertIsNone(re.search(r"\son(click|load|error|submit|change|input)=", texte), f.name)
            self.assertNotIn("javascript:", texte, f.name)

    def test_politique_propre_d_une_route_gardee(self):
        self.write("image.png", "png")
        r = self.client.get("/fichiers/image.png")
        self.assertIn("sandbox", r.headers["Content-Security-Policy"])

    def test_mode_rapport_et_appli_android(self):
        self.app.config["CSP"] = "rapport"
        r = self.client.get("/")
        self.assertNotIn("Content-Security-Policy", r.headers)
        self.assertIn("Content-Security-Policy-Report-Only", r.headers)
        self.app.config["CSP"] = "active"
        # Appli Android : la déconnexion renvoie vers ses pages embarquées, que form-action doit permettre.
        r = self.client.get("/", headers={"User-Agent": "Mozilla/5.0 ProjetPluriApp/2.1"})
        self.assertIn("form-action 'self' https://appassets.androidplatform.net", r.headers["Content-Security-Policy"])

    def test_cookie_se_souvenir_de_moi(self):
        self.assertEqual(self.app.config["REMEMBER_COOKIE_SAMESITE"], "Lax")
        c = self.app.test_client()
        token = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        r = c.post("/login", data={"login": "moi", "password": "secret1", "_csrf": token, "remember": "1"})
        cookies = r.headers.getlist("Set-Cookie")
        self.assertTrue(any(k.startswith("remember_token=") and "SameSite=Lax" in k for k in cookies), cookies)


class DeconnexionTests(WebTestCase):
    def test_lien_get_ne_deconnecte_pas(self):
        r = self.client.get("/logout")
        self.assertEqual(r.status_code, 200)  # avant : déconnexion par un simple lien
        html = r.get_data(as_text=True)
        self.assertIn("data-deconnexion", html)
        self.assertIn("Effacer aussi les pages gardées sur cet appareil", html)
        self.assertEqual(self.client.get("/home").status_code, 200)  # toujours connecté
        r = self.client.post("/logout", data={"_csrf": "jeton-test"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/home").status_code, 302)

    def test_case_cochee_hors_de_la_machine_locale(self):
        coche = re.compile(r"data-effacer-appareil checked")
        self.assertIsNone(coche.search(self.client.get("/home").get_data(as_text=True)))
        distant = self.login(base_url="http://192.168.1.10")
        html = distant.get("/home", base_url="http://192.168.1.10").get_data(as_text=True)
        self.assertRegex(html, coche)
        self.assertIn("js/appareil.js", html)

    def test_script_d_effacement(self):
        js = (STATIC / "js" / "appareil.js").read_text(encoding="utf-8")
        for attendu in ("hangar-wiki", "hangar-qcm", "hangar-qcm-hors-ligne", "wiki-dossiers-hors-ligne",
                        "wiki-brouillon:", "hangar.qcm.attente.", "QcmAttente.envoyer", "confirm("):
            self.assertIn(attendu, js)
        self.assertNotIn("Clear-Site-Data", self.client.post("/logout", data={"_csrf": "jeton-test"}).headers)

    def test_brouillon_range_sous_le_compte(self):
        js = (STATIC / "wiki" / "editor.js").read_text(encoding="utf-8")
        self.assertIn('"wiki-brouillon:" + (compte ? compte + ":" : "") + noteKey', js)
        self.assertIn("window.HANGAR.me.id", js)

    def test_copie_hors_ligne_signalee(self):
        sw = self.client.get("/sw.js").get_data(as_text=True)
        self.assertIn("data-copie-hors-ligne", sw)
        self.assertIn("copieMarquee", sw)
        wiki_js = (STATIC / "wiki" / "wiki.js").read_text(encoding="utf-8")
        self.assertIn("copieHorsLigne", wiki_js)
        self.assertIn("Copie gardée sur cet appareil", wiki_js)


if __name__ == "__main__":
    unittest.main()
