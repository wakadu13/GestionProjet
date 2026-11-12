"""Commandes des QCM (hangar …).

`uv run hangar qcm-verifier [fichier.json | dossier …]` : vérifie des fichiers de questions ou
de langue (par exemple écrits par une IA) avant de les déposer dans le coffre ; sans argument,
tout le dossier QCM/ du coffre (port de la commande `verifier_qcm` de Revision).

`uv run hangar qcm-import-revision <db.sqlite3> --compte <identifiant>` : reprend la progression
de Revision (états des questions, réglages des sets, série, temps, espace personnel), voir reprise.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

VERT, ROUGE, JAUNE, FIN = "\033[32m", "\033[31m", "\033[33m", "\033[0m"


def _couleur(texte, code):
    return f"{code}{texte}{FIN}" if sys.stdout.isatty() else texte


def cmd_verifier(args) -> None:
    from .verifier import verifier_fichier

    chemins = [Path(c) for c in args.chemins]
    if not chemins:
        from .. import create_app
        app = create_app(args.data)
        chemins = [Path(app.config["WIKI_DIR"]) / "QCM"]
    fichiers = []
    for p in chemins:
        if p.is_dir():
            fichiers += [(f, p) for f in sorted(p.rglob("*.json"))
                         if not any(part.startswith(".") for part in f.relative_to(p).parts)]
        elif p.is_file():
            fichiers.append((p, p.parent))
        else:
            print(_couleur(f"✗ Introuvable : {p}", ROUGE))
            sys.exit(1)

    invalides = 0
    for f, racine in fichiers:
        ok, resume, avert = verifier_fichier(f, racine)
        if ok:
            print(_couleur("✓ ", VERT) + str(f))
            print(f"    {resume}")
            for a in avert:
                print(_couleur(f"    ⚠ {a}", JAUNE))
        else:
            invalides += 1
            print(_couleur("✗ ", ROUGE) + str(f))
            print(_couleur(f"    {resume}", ROUGE))
    print()
    if invalides:
        print(_couleur(f"{invalides} fichier(s) invalide(s) : ils seraient ignorés par Hangar. "
                       "Recolle l'erreur à l'IA pour qu'elle corrige.", ROUGE))
        sys.exit(1)
    print(_couleur(f"{len(fichiers)} fichier(s) valide(s).", VERT))


def cmd_rappels(args) -> None:
    """Envoie les rappels quotidiens dus par notification web (port de `envoyer_rappels`).
    Hangar le fait déjà tout seul pendant qu'il tourne (`hangar run`) ; la commande sert à un
    serveur lancé autrement, par exemple par cron toutes les 15 minutes."""
    from .. import create_app
    from .rappels import envoyer_rappels_dus

    app = create_app(args.data)
    with app.app_context():
        c = envoyer_rappels_dus(journal=lambda m: print(_couleur(m, ROUGE), file=sys.stderr))
    print(f"{c['envoyes']} envoyé(s), {c['rien']} sans objet, {c['expires']} abonnement(s) expiré(s), "
          f"{c['erreurs']} erreur(s)")


def cmd_debloquer(args) -> None:
    """Lève les blocages de connexion (port de `axes_reset`) : tous, ou ceux d'un identifiant."""
    from .. import blocage_connexion, create_app

    app = create_app(args.data)
    with app.app_context():
        n = blocage_connexion.debloquer(args.identifiant or None, args.ip or None)
    print(_couleur(f"{n} blocage(s) levé(s).", VERT))


def cmd_import_revision(args) -> None:
    """Reprend la progression de Revision (base db.sqlite3 du Raspberry Pi) dans un compte de Hangar."""
    from sqlalchemy import func, or_

    from .. import create_app
    from ..models import User
    from .reprise import RepriseImpossible, reprendre

    app = create_app(args.data)
    with app.app_context():
        ident = args.compte.strip().lower()
        user = User.query.filter(or_(func.lower(User.username) == ident, func.lower(User.email) == ident)).first()
        if user is None:
            print(_couleur(f"✗ Aucun compte « {args.compte} » dans Hangar (identifiant ou adresse e-mail).", ROUGE))
            sys.exit(1)
        try:
            nom = user.username
            b = reprendre(args.base, user, joueur=args.joueur, coffre=args.coffre, perso=args.perso, essai=args.essai)
        except RepriseImpossible as e:
            print(_couleur(f"✗ {e}", ROUGE))
            sys.exit(1)
    p, r = b.progression, b.reglages
    if args.essai:
        print(_couleur(f"Essai : rien n'a été enregistré. Voici ce que la reprise ferait pour le compte « {nom} ».", JAUNE))
    else:
        print(_couleur(f"✓ Progression de Revision reprise dans le compte « {nom} ».", VERT))
    print(f"  Questions suivies : {p['ajoutees']} ajoutée(s), {p['mises_a_jour']} mise(s) à jour, "
          f"{p['gardees']} déjà à jour dans Hangar.")
    print(f"  Réglages des sets (quotas, dates d'examen) : {r['ajoutes']} ajouté(s), {r['completes']} complété(s).")
    print(f"  Activité : {b.jours_activite} jour(s) ; temps par matière : {b.jours_temps} ligne(s)"
          + (" ; objectif quotidien repris" if b.objectif else "") + f" ; {b.explications} explication(s) de l'IA.")
    if b.fichiers_perso or b.fichiers_perso_gardes:
        print(f"  Mon espace : {len(b.fichiers_perso)} fichier(s) copié(s)"
              + (f", {len(b.fichiers_perso_gardes)} déjà présent(s) et différent(s), gardé(s) tels quels"
                 if b.fichiers_perso_gardes else "") + ".")
    if b.par_contenu:
        print(f"  {len(b.par_contenu)} set(s) retrouvé(s) par leur contenu (renommés ou déplacés) :")
        for ancien, nouveau in sorted(b.par_contenu.items()):
            print(f"    {ancien} → {nouveau}")
    if b.introuvables:
        print(_couleur(f"  ⚠ {len(b.introuvables)} set(s) introuvable(s) dans le coffre de Hangar : leur progression "
                       "est gardée et apparaît sur la page des statistiques des QCM, où tu peux la rattacher "
                       "à un set (ou lance d'abord « hangar wiki-import », puis relance cette commande) :", JAUNE))
        for sid in b.introuvables[:20]:
            print(f"    {sid}")
        if len(b.introuvables) > 20:
            print(f"    … et {len(b.introuvables) - 20} autre(s)")
    if b.tables_absentes:
        print(f"  (Base d'une ancienne version de Revision : {', '.join(b.tables_absentes)} absente(s), ignorée(s).)")
    print("  Tu peux relancer la commande sans risque : rien n'est compté deux fois.")


def register(sub) -> dict:
    p = sub.add_parser("qcm-verifier", help="vérifier des fichiers de QCM ou de langue (.json)")
    p.add_argument("chemins", nargs="*", help="fichiers .json ou dossiers (défaut : QCM/ du coffre)")
    sub.add_parser("qcm-rappels", help="envoyer les rappels de révision dus (notifications web)")
    p = sub.add_parser("connexions-debloquer", help="lever les blocages après des mots de passe faux")
    p.add_argument("identifiant", nargs="?", help="identifiant à débloquer (défaut : tous)")
    p.add_argument("--ip", help="adresse IP à débloquer")
    p = sub.add_parser("qcm-import-revision",
                       help="reprendre la progression de Revision (base db.sqlite3 du Raspberry Pi), sans la modifier")
    p.add_argument("base", help="fichier db.sqlite3 de Revision (copié depuis le Pi)")
    p.add_argument("--compte", required=True, help="identifiant (ou adresse e-mail) du compte Hangar qui reçoit la progression")
    p.add_argument("--joueur", help="nom d'utilisateur dans Revision (défaut : l'identifiant du compte Hangar)")
    p.add_argument("--coffre", help="dossier vault/ de Revision : retrouve par leur contenu les sets renommés ou déplacés")
    p.add_argument("--perso", help="dossier perso/<nom> de Revision (défaut : à côté de la base, s'il existe)")
    p.add_argument("--essai", action="store_true", help="tout vérifier et afficher le bilan, sans rien enregistrer")
    return {"qcm-verifier": cmd_verifier, "qcm-rappels": cmd_rappels, "connexions-debloquer": cmd_debloquer,
            "qcm-import-revision": cmd_import_revision}
