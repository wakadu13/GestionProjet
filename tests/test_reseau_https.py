"""Accès depuis le téléphone en HTTPS sur le réseau local (vague v3a, piste 82).

- QR code de l'adresse, encodé sans dépendance (hangar/qr.py), comparé à des références de la norme ;
- ``hangar run --lan-https`` : Hangar n'écoute que 127.0.0.1, Caddy (simulé : aucun téléchargement) ouvre le
  réseau en HTTPS, l'adresse et son QR code s'affichent au démarrage et dans Réglages › Profil ;
- certificat de l'autorité locale proposé au téléphone, avec son empreinte ;
- adresse de retour (back_url) juste quand un proxy sert la page en https sans le dire ;
- /setup (premier compte administrateur) réservé à l'ordinateur où tourne Hangar.

Lancer : uv run python -m unittest tests.test_reseau_https -v
"""
import base64
import contextlib
import hashlib
import io
import logging
import os
import shutil
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from hangar import create_app, qr, reseau_local
from hangar.extensions import db
from hangar.models import User

CSRF = "jeton-de-test"
IP = "192.168.1.20"


def _empreinte_matrices(texte):
    """Même calcul que les références : SHA-256 des 8 matrices (un masque chacune)."""
    def h(m):
        return hashlib.sha256("\n".join("".join("1" if c else "0" for c in r) for r in m).encode()).hexdigest()
    return hashlib.sha256("".join(h(qr.matrice(texte, k)) for k in range(8)).encode()).hexdigest()


# Références produites par la bibliothèque « qrcode » (niveau M, mode octets, masque imposé), une seule fois,
# hors du dépôt : Hangar n'en dépend pas.
REFERENCE_V2 = """
#######...#..####.#######
#.....#.#.##..###.#.....#
#.###.#..####.#.#.#.###.#
#.###.#..###...##.#.###.#
#.###.#.#..##..##.#.###.#
#.....#..#..#..#..#.....#
#######.#.#.#.#.#.#######
.........##.##..#........
#.#.#.#..#..#####...#..#.
.#......#.#..##...##....#
#.##.###...#..#.#..##.###
#....#..##.#.#.#...#...#.
.#.##.#####.####..##.#.##
..#....#..##..#...##.#..#
#....##.#....##.#.##..###
.#.###..###.##..###.#..#.
#.#######.#####.######...
........#.#..##.#...##.##
#######........##.#.##.##
#.....#...#..####...##.##
#.###.#.##...##.######...
#.###.#..##..##..#.####..
#.###.#.##..#...#...#...#
#.....#..#.###..#.#.##.#.
#######.#.#..##.#..#...##
""".split()
REFERENCES = [  # (texte, côté, empreinte des 8 masques)
    ("https://192.168.1.20:5443/", 25, "ef46900097ea2607e646d4824c20d270e9dcba63878daec06fad18e13d60a10c"),
    ("https://192.168.1.20:5443/settings/certificat-hangar.crt?é=" + "x" * 80, 49,
     "566510cb3a354bfd149562084641a939d9fd9001731192ed750f6401b6d344d8"),
    ("h" * 213, 57, "5c80dda197956042157d1125b56c8001aee860fba22c9d802441fd73db6d9333"),
]


class QRCode(unittest.TestCase):
    def test_matrice_identique_a_la_reference(self):
        m = qr.matrice("https://192.168.1.20:5443/", masque=0)
        self.assertEqual(["".join("#" if c else "." for c in ligne) for ligne in m], REFERENCE_V2)

    def test_toutes_versions_et_masques(self):
        # Versions 2, 8 (information de version, accent en UTF-8) et 10 (capacité maximale), 8 masques chacune
        for texte, cote, attendu in REFERENCES:
            with self.subTest(longueur=len(texte)):
                self.assertEqual(len(qr.matrice(texte)), cote)
                self.assertEqual(_empreinte_matrices(texte), attendu)

    def test_texte_trop_long(self):
        with self.assertRaises(ValueError):
            qr.matrice("h" * 214)

    def test_svg_et_console(self):
        image = qr.svg('https://exemple/?a=1&b="2"')
        self.assertTrue(image.startswith("<svg"))
        self.assertIn('fill="#fff"', image)  # fond blanc même en thème sombre
        self.assertIn("&amp;b=&quot;2&quot;", image)
        self.assertNotIn('"2"', image)
        texte = qr.terminal("https://192.168.1.20:5443/")
        lignes = texte.splitlines()
        self.assertEqual(len(lignes), (25 + 4 + 1) // 2)
        self.assertTrue(all(len(ligne) == 29 for ligne in lignes))
        self.assertEqual(lignes[0], "█" * 29)  # marge claire


class Caddy(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hangar-https-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_caddyfile(self):
        texte = reseau_local.caddyfile(IP, 5000, self.tmp / "https" / "caddy")
        self.assertIn(f"https://{IP}:5443 {{", texte)
        self.assertIn("reverse_proxy 127.0.0.1:5000", texte)
        self.assertIn("tls internal", texte)
        self.assertIn("skip_install_trust", texte)  # aucune fenêtre d'administrateur sur le PC
        self.assertIn("admin off", texte)
        self.assertIn("auto_https disable_redirects", texte)  # rien en http sur le réseau
        self.assertNotIn("\\", texte.split("root", 1)[1].split("\n", 1)[0])  # chemin Windows en « / »

    def test_trouver_caddy(self):
        exe = "caddy.exe" if os.name == "nt" else "caddy"
        with mock.patch.dict(os.environ, {"HANGAR_CADDY": "", "LOCALAPPDATA": str(self.tmp / "local")}), \
                mock.patch("shutil.which", return_value=None):
            self.assertIsNone(reseau_local.trouver_caddy(self.tmp))
            winget = self.tmp / "local" / "Microsoft" / "WinGet" / "Links" / exe
            winget.parent.mkdir(parents=True)
            winget.write_bytes(b"")
            self.assertEqual(reseau_local.trouver_caddy(self.tmp), winget)
            outil = self.tmp / "outils" / exe
            outil.parent.mkdir()
            outil.write_bytes(b"")
            self.assertEqual(reseau_local.trouver_caddy(self.tmp), outil)

    def test_empreinte(self):
        der = bytes(range(200))
        pem = ("-----BEGIN CERTIFICATE-----\n" + base64.encodebytes(der).decode()
               + "-----END CERTIFICATE-----\n").encode()
        h = hashlib.sha256(der).hexdigest().upper()
        self.assertEqual(reseau_local.empreinte(pem), ":".join(h[i:i + 2] for i in range(0, 64, 2)))
        with self.assertRaises(ValueError):
            reseau_local.empreinte(b"pas un certificat")


class FauxCaddy:
    def __init__(self):
        self.arrete = False

    def poll(self):
        return 0 if self.arrete else None

    def terminate(self):
        self.arrete = True

    def wait(self, timeout=None):
        return 0


class LancementLanHttps(unittest.TestCase):
    """``hangar run --lan-https`` avec Caddy simulé : rien n'est téléchargé ni ouvert au réseau."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-lan-https-")
        self.apps = []
        self.handlers = list(logging.getLogger().handlers)

    def tearDown(self):
        for app in self.apps:
            with app.app_context():
                db.session.remove()
                db.engine.dispose()
        racine = logging.getLogger()
        for h in list(racine.handlers):
            if h not in self.handlers:
                racine.removeHandler(h)
                h.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def lancer(self, caddy=None, demarrer=None, **options):
        import hangar
        from hangar import cli
        vraie = hangar.create_app
        args = Namespace(data=self.tmp, host=options.get("host", "127.0.0.1"), port=5000, debug=False,
                         no_browser=True, lan_https=True, https_port=None, cloudflare=False, behind_proxy=False,
                         https=False)
        sortie = io.StringIO()
        with mock.patch.dict(os.environ, {"HANGAR_BEHIND_PROXY": "", "HANGAR_CADDY": ""}), \
                mock.patch.object(cli, "_port_busy", return_value=False), \
                mock.patch("waitress.serve") as serve, mock.patch("hangar.mailer.start_worker"), \
                mock.patch("hangar.ai.jobs.resume"), \
                mock.patch("hangar.create_app", side_effect=lambda d: self.apps.append(vraie(d)) or self.apps[-1]), \
                mock.patch.object(reseau_local, "ip_reseau", return_value=IP), \
                mock.patch.object(reseau_local, "trouver_caddy", return_value=caddy), \
                contextlib.redirect_stdout(sortie):
            if demarrer is not None:
                with mock.patch.object(reseau_local, "demarrer_caddy", side_effect=demarrer):
                    cli.cmd_run(args)
            else:
                cli.cmd_run(args)
            proxy = os.environ.get("HANGAR_BEHIND_PROXY")
        return sortie.getvalue(), serve, proxy

    def test_https_avec_caddy(self):
        faux, appels = FauxCaddy(), []

        def demarrer(caddy, data_dir, ip, port, port_https):
            appels.append((ip, port, port_https))
            return faux
        sortie, serve, proxy = self.lancer(caddy=Path("caddy"), demarrer=demarrer, host="0.0.0.0")
        self.assertEqual(serve.call_args.kwargs["host"], "127.0.0.1")  # seul Caddy parle au réseau
        self.assertEqual(appels, [(IP, 5000, 5443)])
        self.assertEqual(proxy, "1")  # les en-têtes de Caddy sont crus (adresse du visiteur, https)
        self.assertIn(f"https://{IP}:5443/", sortie)
        self.assertIn("▀", sortie)  # QR code dans la console
        self.assertIn("certificat", sortie)
        self.assertTrue(faux.arrete)  # Caddy arrêté avec Hangar
        self.assertEqual(self.apps[0].config["PARTAGE"], {"host": "127.0.0.1", "port": 5000, "https_port": 5443})

    def test_sans_caddy_reste_local_et_explique(self):
        sortie, serve, _ = self.lancer(caddy=None)
        self.assertEqual(serve.call_args.kwargs["host"], "127.0.0.1")
        self.assertIn("Caddy n’est pas installé", sortie)
        self.assertIn("partager-en-https.bat", sortie)
        self.assertIsNone(self.apps[0].config["PARTAGE"]["https_port"])

    def test_caddy_qui_s_arrete_au_demarrage(self):
        # Un « Caddy » qui s'arrête aussitôt (ici Python, qui ne trouve pas le fichier « run ») : message clair
        sortie, serve, _ = self.lancer(caddy=Path(sys.executable))
        self.assertIn("Caddy s’est arrêté au démarrage", sortie)
        self.assertIn("caddy.log", sortie)
        serve.assert_called_once()
        caddyfile = Path(self.tmp) / "https" / "Caddyfile"
        self.assertIn(f"https://{IP}:5443", caddyfile.read_text(encoding="utf-8"))

    def test_partage_http_affiche_aussi_le_qr_code(self):
        import hangar
        from hangar import cli
        vraie = hangar.create_app
        args = Namespace(data=self.tmp, host="0.0.0.0", port=5000, debug=False, no_browser=True)
        sortie = io.StringIO()
        with mock.patch.object(cli, "_port_busy", return_value=False), mock.patch("waitress.serve"), \
                mock.patch("hangar.mailer.start_worker"), mock.patch("hangar.ai.jobs.resume"), \
                mock.patch("hangar.create_app", side_effect=lambda d: self.apps.append(vraie(d)) or self.apps[-1]), \
                mock.patch.object(reseau_local, "ip_reseau", return_value=IP), contextlib.redirect_stdout(sortie):
            cli.cmd_run(args)
        self.assertIn(f"http://{IP}:5000", sortie.getvalue())
        self.assertIn("▀", sortie.getvalue())
        self.assertIn("partager-en-https.bat", sortie.getvalue())

    def test_option_dans_l_aide(self):
        from hangar import cli
        sortie = io.StringIO()
        with contextlib.redirect_stdout(sortie), self.assertRaises(SystemExit):
            cli.main(["run", "--help"])
        self.assertIn("--lan-https", sortie.getvalue())


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-tel-")
        self.app = create_app(self.tmp, testing=True)
        with self.app.app_context():
            u = User(username="eleve", email="eleve@hangar.local", role="admin", first_name="Marie")
            u.set_password("secret-eleve")
            db.session.add(u)
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self, **kw):
        c = self.app.test_client(**kw)
        with c.session_transaction() as s:
            s["_csrf"] = CSRF
        return c

    def connecte(self, **kw):
        c = self.app.test_client()
        with c.session_transaction(base_url=kw.get("base_url", "http://localhost")) as s:
            s["_csrf"] = CSRF
        r = c.post("/login", data={"_csrf": CSRF, "login": "eleve", "password": "secret-eleve"}, **kw)
        self.assertEqual(r.status_code, 302)
        return c

    def poser_certificat(self, der=b"autorite-de-test"):
        f = reseau_local.certificat_racine(self.app.config["DATA_DIR"])
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"-----BEGIN CERTIFICATE-----\n" + base64.encodebytes(der) + b"-----END CERTIFICATE-----\n")
        return f


class ReglagesTelephone(Base):
    def test_local_seulement(self):
        page = self.connecte().get("/settings/profile").get_data(as_text=True)
        self.assertIn("Sur ton téléphone", page)
        self.assertIn("n’est ouvert qu’à cet ordinateur", page)
        self.assertIn("partager-en-https.bat", page)

    def test_adresse_https_et_qr_code(self):
        self.app.config["PARTAGE"] = {"host": "127.0.0.1", "port": 5000, "https_port": 5443}
        self.poser_certificat()
        with mock.patch.object(reseau_local, "ip_reseau", return_value=IP):
            page = self.connecte().get("/settings/profile").get_data(as_text=True)
        self.assertIn(f"https://{IP}:5443/", page)
        self.assertIn('aria-label="QR code : https://192.168.1.20:5443/"', page)
        self.assertIn("/settings/certificat-hangar.crt", page)
        h = hashlib.sha256(b"autorite-de-test").hexdigest().upper()
        self.assertIn(h[:2] + ":" + h[2:4], page)  # empreinte à comparer sur le téléphone

    def test_adresse_http_partagee(self):
        self.app.config["PARTAGE"] = {"host": "0.0.0.0", "port": 5000, "https_port": None}
        with mock.patch.object(reseau_local, "ip_reseau", return_value=IP):
            page = self.connecte().get("/settings/profile").get_data(as_text=True)
        self.assertIn(f"http://{IP}:5000/", page)
        self.assertIn("non chiffrée", page)

    def test_page_ouverte_depuis_le_telephone(self):
        distant = {"base_url": f"http://{IP}:5000", "environ_base": {"REMOTE_ADDR": "192.168.1.30"}}
        page = self.connecte(**distant).get("/settings/profile", **distant).get_data(as_text=True)
        self.assertIn(f"http://{IP}:5000/", page)

    def test_certificat_public(self):
        c = self.client()
        self.assertEqual(c.get("/settings/certificat-hangar.crt").status_code, 404)
        self.poser_certificat()
        r = c.get("/settings/certificat-hangar.crt")  # sans connexion : le téléphone n'a pas encore de compte
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/x-x509-ca-cert")
        self.assertIn(b"BEGIN CERTIFICATE", r.data)
        r.close()


class RetourDerriereUnProxy(Base):
    def test_referer_https_meme_hote(self):
        from hangar.utils import back_url
        with self.app.test_request_context("/agenda/tasks", method="POST", base_url=f"http://{IP}:5443",
                                           headers={"Referer": f"https://{IP}:5443/agenda/calendar?semaine=2"}):
            self.assertEqual(back_url("/defaut"), "/agenda/calendar?semaine=2")
        with self.app.test_request_context("/agenda/tasks", method="POST", base_url=f"http://{IP}:5443",
                                           headers={"Referer": "https://autre.exemple:5443/agenda/calendar"}):
            self.assertEqual(back_url("/defaut"), "/defaut")
        with self.app.test_request_context("/agenda/tasks", method="POST", base_url=f"http://{IP}:5443",
                                           headers={"Referer": f"https://{IP}:5443//autre.exemple/x"}):
            self.assertEqual(back_url("/defaut"), "/defaut")

    def test_derriere_caddy_avec_lan_https(self):
        # Requête telle que Caddy la transmet : Hangar (ProxyFix) voit https://IP:5443 et revient à la page
        with mock.patch.dict(os.environ, {"HANGAR_BEHIND_PROXY": "1"}):
            app = create_app(os.path.join(self.tmp, "proxy"), testing=True)
        try:
            with app.app_context():
                u = User(username="eleve", email="eleve@hangar.local", role="admin")
                u.set_password("secret-eleve")
                db.session.add(u)
                db.session.commit()
            entetes = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": f"{IP}:5443",
                       "X-Forwarded-For": "192.168.1.30"}
            c = app.test_client()
            with c.session_transaction() as s:
                s["_csrf"] = CSRF
            c.post("/login", data={"_csrf": CSRF, "login": "eleve", "password": "secret-eleve"}, headers=entetes)
            r = c.post("/agenda/tasks", data={"_csrf": CSRF, "title": ""},
                       headers={**entetes, "Referer": f"https://{IP}:5443/agenda/calendar"})
            self.assertEqual(r.status_code, 302)
            self.assertEqual(r.headers["Location"], f"https://{IP}:5443/agenda/calendar")
        finally:
            with app.app_context():
                db.session.remove()
                db.engine.dispose()


class SetupReserveACetOrdinateur(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hangar-setup-")
        self.app = create_app(self.tmp, testing=True)

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def poster(self, **kw):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["_csrf"] = CSRF
        return c.post("/setup", data={"_csrf": CSRF, "username": "intrus", "password": "mot-de-passe-long",
                                      "workspace": "Pris"}, **kw)

    def comptes(self):
        with self.app.app_context():
            return User.query.count()

    def test_refuse_depuis_le_reseau(self):
        r = self.poster(environ_base={"REMOTE_ADDR": "192.168.1.30"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("127.0.0.1", r.get_data(as_text=True))
        self.assertEqual(self.comptes(), 0)
        r = self.app.test_client().get("/setup", environ_base={"REMOTE_ADDR": "192.168.1.30"})
        self.assertEqual(r.status_code, 403)

    def test_refuse_a_travers_un_proxy_local(self):
        # Caddy ou un tunnel tournent sur ce PC : la connexion vient de 127.0.0.1, mais pour un visiteur
        r = self.poster(headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.comptes(), 0)

    def test_accepte_sur_cet_ordinateur(self):
        r = self.poster()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.comptes(), 1)


class ScriptsWindows(unittest.TestCase):
    def test_script_partager_en_https(self):
        racine = Path(__file__).resolve().parent.parent / "scripts"
        bat = (racine / "partager-en-https.bat").read_bytes().decode("ascii")  # cmd.exe : pas d'accents
        self.assertIn("partager-en-https.ps1", bat)
        brut = (racine / "partager-en-https.ps1").read_bytes()
        self.assertTrue(brut.startswith(b"\xef\xbb\xbf"), "PowerShell 5.1 lit l'UTF-8 sans BOM de travers")
        ps1 = brut.decode("utf-8-sig")
        self.assertIn("CaddyServer.Caddy", ps1)  # winget
        self.assertIn("--lan-https", ps1)
        self.assertIn("hangar-commun.ps1", ps1)


if __name__ == "__main__":
    unittest.main()
