"""Ligne de commande : hangar [run|demo|reset|backup|create-admin]."""
from __future__ import annotations

import argparse
import getpass
import os
import shutil
import socket
import sys
import threading
import webbrowser


def _lan_ip() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def _port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _is_hangar(port: int) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/login", timeout=2) as r:
            return b"Hangar" in r.read(20000)
    except Exception:
        return False


def cmd_run(args):
    from . import create_app
    port = args.port
    if _port_busy(port):
        if _is_hangar(port):
            url = f"http://127.0.0.1:{port}"
            print(f"\n  ✈️  Hangar tourne déjà sur {url} — ouverture du navigateur.")
            print("  (Pour l’arrêter : Ctrl+C dans le terminal où il a été lancé.)\n")
            if not args.no_browser:
                webbrowser.open(url)
            return
        free = next((p for p in range(port + 1, port + 50) if not _port_busy(p)), None)
        if free is None:
            print(f"✗ Le port {port} est occupé et aucun port libre n’a été trouvé. Essaie --port 8080.")
            sys.exit(1)
        print(f"  ! Le port {port} est utilisé par un autre programme : Hangar utilise le port {free}.")
        port = free
    app = create_app(args.data)
    url = f"http://127.0.0.1:{port}"
    print("\n  ✈️  Hangar — gestion de projet locale")
    print(f"  → Ouvre {url} dans ton navigateur")
    if args.host in ("0.0.0.0", "::"):
        ip = _lan_ip()
        if ip:
            print(f"  → Sur le réseau local : http://{ip}:{port}")
    print(f"  → Données : {app.config['DATA_DIR']}")
    print("  (Ctrl+C pour arrêter)\n")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    from .mailer import start_worker
    start_worker(app)  # envoi des e-mails en file et des e-mails planifiés
    try:
        if args.debug:
            app.run(host=args.host, port=port, debug=True, use_reloader=False)
        else:
            from waitress import serve
            serve(app, host=args.host, port=port, threads=8, ident="Hangar")
    except KeyboardInterrupt:
        print("\n  Hangar arrêté.")


def cmd_demo(args):
    from . import create_app
    from .extensions import db
    from .seed import seed_demo
    app = create_app(args.data)
    with app.test_request_context("/"):
        if args.force:
            db.drop_all()
            db.create_all()
        try:
            seed_demo(args.planning)
        except RuntimeError as exc:
            print(f"✗ {exc}")
            sys.exit(1)
    print("\nDonnées de démonstration prêtes. Lance « uv run hangar » puis connecte-toi avec "
          "l’identifiant « wacogne ».")


def cmd_rebuild_docs(args):
    """Remplace les tickets du projet par ceux issus des documents (sauvegarde avant)."""
    from . import create_app
    from .extensions import db
    from .models import Project, User
    from .seed_documents import clear_project, load_documents
    from .views.settings import make_backup
    app = create_app(args.data)
    with app.test_request_context("/"):
        project = (db.session.get(Project, args.project) if args.project
                   else Project.query.filter_by(identifier="EAE").first() or Project.query.first())
        if project is None:
            print("✗ Aucun projet. Lance d’abord « hangar demo ».")
            sys.exit(1)
        actor = project.lead or User.query.filter_by(role="admin").first()
        if not args.yes:
            ans = input(f"Remplacer tous les tickets de « {project.name} » par ceux issus des documents ? Tape « oui » : ")
            if ans.strip().lower() != "oui":
                print("Annulé.")
                return
        print(f"Sauvegarde de sécurité : {make_backup()}")
        clear_project(project)
        load_documents(project, actor)
        # Prénoms retrouvés dans les documents (s'ils ne sont pas déjà renseignés)
        from .models import Page
        from .seed import TEAM
        for uname, first, _last, _role in TEAM:
            u = User.query.filter_by(username=uname).first()
            if u and first and not u.first_name:
                u.first_name = first
        # La note de réunion brute est remplacée par le compte rendu issu des documents
        for pg in Page.query.filter_by(project_id=project.id, title="Note de réunion"):
            db.session.delete(pg)
        db.session.commit()
    print("✓ Projet reconstruit d’après les documents.")


def cmd_reset(args):
    from . import create_app
    from .extensions import db
    if not args.yes:
        ans = input("Supprimer TOUTES les données (base + fichiers joints) ? Tape « oui » : ")
        if ans.strip().lower() != "oui":
            print("Annulé.")
            return
    app = create_app(args.data)
    with app.app_context():
        from .views.settings import make_backup
        try:
            print(f"Sauvegarde de sécurité : {make_backup()}")
        except Exception:
            pass
        db.drop_all()
        db.create_all()
    up = app.config["UPLOAD_DIR"]
    shutil.rmtree(up, ignore_errors=True)
    os.makedirs(up, exist_ok=True)
    print("✓ Base réinitialisée.")


def cmd_backup(args):
    from . import create_app
    from .views.settings import make_backup
    app = create_app(args.data)
    with app.app_context():
        print(f"✓ Sauvegarde créée : {make_backup()}")


def cmd_create_admin(args):
    from . import create_app
    from .extensions import db
    from .models import User
    app = create_app(args.data)
    with app.app_context():
        username = args.username or input("Identifiant : ").strip().lower()
        password = getpass.getpass("Mot de passe : ")
        u = User.query.filter_by(username=username).first()
        if u:
            u.role, u.active = "admin", True
            u.set_password(password)
            print(f"✓ {username} est maintenant administrateur (mot de passe mis à jour).")
        else:
            u = User(username=username, email=User.internal_email(username), role="admin")
            u.set_password(password)
            db.session.add(u)
            print(f"✓ Administrateur {username} créé.")
        db.session.commit()


def cmd_mail_send(args):
    from . import create_app
    from .agenda import run_jobs
    from .mailer import run_scheduled, send_pending
    app = create_app(args.data)
    with app.app_context():
        force = {"reminders", "digest", "bilan"} if args.force else set()
        res = run_scheduled(force=force)
        ag = run_jobs()
        stats = send_pending(force=True)
    print(f"Planifiés : {res or 'rien à envoyer pour l’instant'}")
    print(f"Agenda : {ag['reminders']} rappel(s), {ag['daily']} programme(s) du jour")
    print(f"File : {stats['sent']} envoyé(s), {stats['outbox']} dans data/outbox, {stats['failed']} en échec"
          + (f", {stats['waiting']} en attente d’un nouvel essai (serveur injoignable)" if stats.get("waiting") else ""))


def cmd_agenda_import(args):
    """Importe un fichier .ics dans l'agenda d'un utilisateur (le dernier import fait foi)."""
    from . import create_app
    from .agenda import import_calendar
    from .ics import ICSError
    from .models import User
    app = create_app(args.data)
    with app.test_request_context("/"):
        user = (User.query.filter_by(username=args.user).first() if args.user
                else User.query.filter_by(role="admin").order_by(User.id).first())
        if user is None:
            print("✗ Utilisateur introuvable (option --user).")
            sys.exit(1)
        try:
            with open(args.file, "rb") as fh:
                imp = import_calendar(user, fh.read(), os.path.basename(args.file), args.mode)
        except (OSError, ICSError) as exc:
            print(f"✗ {exc}")
            sys.exit(1)
        # Message construit tant que la session est ouverte (les objets sont expirés après sa fermeture)
        print(f"✓ Import pour @{user.username} : {imp.total} séances dans le fichier ({imp.range_start:%d/%m} → "
              f"{imp.range_end:%d/%m}) — {imp.added} ajoutée(s), {imp.modified} modifiée(s), "
              f"{imp.removed} retirée(s), {imp.unchanged} inchangée(s).")


def cmd_mail_test(args):
    from . import create_app
    from .extensions import db
    from .mailer import send_test, valid_email
    if not valid_email(args.address):
        print("✗ Adresse invalide.")
        sys.exit(1)
    app = create_app(args.data)
    with app.app_context():
        m = send_test(args.address)
        db.session.refresh(m)
        print(f"Statut : {m.status}" + (f" — {m.error}" if m.error else ""))


def _utf8_output() -> None:
    """Sorties en UTF-8 : sous Windows, une sortie redirigée (fichier, tube) est en cp1252 par défaut et les
    symboles affichés par les commandes (✓, ✗, →…) y provoqueraient une UnicodeEncodeError."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # flux déjà fermé ou non reconfigurable
                pass


def main(argv=None):
    _utf8_output()
    parser = argparse.ArgumentParser(prog="hangar", description="Hangar : agenda, cours, QCM, assistant IA et gestion de projet, en local.")
    parser.add_argument("--data", help="dossier des données (défaut : ./data)")
    sub = parser.add_subparsers(dest="cmd")

    run = sub.add_parser("run", help="lancer le serveur (par défaut)")
    run.add_argument("--host", default="127.0.0.1", help="0.0.0.0 pour partager sur le réseau local")
    run.add_argument("--port", type=int, default=5000)
    run.add_argument("--debug", action="store_true")
    run.add_argument("--no-browser", action="store_true")

    demo = sub.add_parser("demo", help="créer les données de démonstration du projet EAE")
    demo.add_argument("--planning", help="chemin du fichier Excel de planification à importer")
    demo.add_argument("--force", action="store_true", help="effacer la base avant")

    rb = sub.add_parser("rebuild-docs", help="recréer les tickets du projet d’après les documents du dossier")
    rb.add_argument("--project", type=int, help="identifiant numérique du projet (défaut : EAE)")
    rb.add_argument("--yes", action="store_true")

    reset = sub.add_parser("reset", help="effacer toutes les données")
    reset.add_argument("--yes", action="store_true")

    sub.add_parser("backup", help="sauvegarder la base dans data/backups")

    ms = sub.add_parser("mail-send", help="envoyer la file d’e-mails et les envois planifiés dus")
    ms.add_argument("--force", action="store_true", help="forcer rappels, récapitulatifs et bilans maintenant")
    ai = sub.add_parser("agenda-import", help="importer l’emploi du temps (.ics) dans l’agenda")
    ai.add_argument("file", help="fichier .ics")
    ai.add_argument("--user", help="identifiant (par défaut : le premier administrateur)")
    ai.add_argument("--mode", default="weeks", choices=["weeks", "future", "merge"],
                    help="weeks = semaines du fichier (défaut), future = tout le planning à venir, merge = ne rien retirer")
    mt = sub.add_parser("mail-test", help="envoyer un e-mail de test")
    mt.add_argument("address")

    ca = sub.add_parser("create-admin", help="créer ou réparer un compte administrateur")
    ca.add_argument("--username")

    # Commandes des modules (wiki, QCM, IA)
    from .ai import cli as ai_cli
    from .qcm import cli as qcm_cli
    from .wiki import cli as wiki_cli
    extra = {}
    for mod in (wiki_cli, qcm_cli, ai_cli):
        extra.update(mod.register(sub) or {})

    args = parser.parse_args(argv)
    if args.cmd is None:
        args = parser.parse_args((argv or sys.argv[1:]) + ["run"]) if not (argv or sys.argv[1:]) else args
        if args.cmd is None:
            args.cmd, args.host, args.port, args.debug, args.no_browser = "run", "127.0.0.1", 5000, False, False
    {"run": cmd_run, "demo": cmd_demo, "reset": cmd_reset, "backup": cmd_backup, "rebuild-docs": cmd_rebuild_docs,
     "create-admin": cmd_create_admin, "mail-send": cmd_mail_send, "mail-test": cmd_mail_test,
     "agenda-import": cmd_agenda_import, **extra}[args.cmd](args)


if __name__ == "__main__":
    main()
