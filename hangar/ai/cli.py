"""Commandes de l'assistant IA (hangar …) :

    hangar ia-fournisseurs                         liste des fournisseurs et de leur état
    hangar ia-ajouter ollama --model qwen2.5       ajoute un fournisseur depuis un préréglage
    hangar ia-tester [ID]                          teste la connexion (tous les fournisseurs par défaut)
    hangar ia-demander "Question…" [--role rapide] pose une question (réponse en flux)
    hangar ia-usage                                dépense et jetons du mois
    hangar ia-travaux                              fait les travaux en attente et récupère les lots terminés
    hangar ia-evaluer [ID] [--oui]                 rejoue des cas fixes et donne le taux de réussite par modèle
"""
from __future__ import annotations

import sys


def _app(args):
    from .. import create_app
    return create_app(args.data)


def cmd_list(args):
    from .models import AIProvider
    app = _app(args)
    with app.test_request_context("/"):
        rows = AIProvider.query.order_by(AIProvider.priority, AIProvider.id).all()
        if not rows:
            print("Aucun fournisseur d’IA. Ajoute-en un : hangar ia-ajouter anthropic --key sk-ant-…  "
                  "ou  hangar ia-ajouter ollama --model llama3.2")
            return
        for p in rows:
            state = "activé" if p.enabled else "désactivé"
            key = p.masked_key or ("(pas de clé)" if p.needs_key else "")
            test = "" if p.last_test_ok is None else (" · test OK" if p.last_test_ok else " · test en échec")
            print(f"[{p.id}] {p.name} — {p.preset_label}, {state}, modèle {p.model or '?'}"
                  f"{' ' + key if key else ''}{' · ' + p.base_url if p.base_url else ''}"
                  f" · rôles {', '.join(p.role_list)}{test}")


def cmd_add(args):
    from ..extensions import db
    from ..models import now
    from . import providers as P
    from .models import AIProvider
    info = P.PRESET_BY_KEY.get(args.preset)
    if info is None:
        print("✗ Préréglage inconnu. Choix : " + ", ".join(P.PRESET_BY_KEY))
        sys.exit(1)
    app = _app(args)
    with app.test_request_context("/"):
        p = AIProvider(name=args.name or info["label"], kind=info["kind"], preset=args.preset,
                       base_url=(args.url or info.get("base_url") or "").rstrip("/"), api_key=args.key or "",
                       api_key_env=args.key_env or info.get("key_env", ""), model=args.model or info.get("model", ""),
                       model_fast=info.get("model_fast", ""), enabled=True, created_at=now(),
                       priority=(db.session.query(db.func.max(AIProvider.priority)).scalar() or 0) + 10)
        if not p.model:
            print("✗ Indique le modèle avec --model.")
            sys.exit(1)
        if p.kind != "anthropic" and not p.base_url:
            print("✗ Indique l’adresse du serveur avec --url (ex. http://localhost:11434/v1).")
            sys.exit(1)
        p.api_key_env_origin = P.origin(p.base_url, p.kind)  # la clé de la variable ne suit pas une autre adresse
        db.session.add(p)
        db.session.commit()
        print(f"✓ Fournisseur [{p.id}] {p.name} ajouté (modèle {p.model}). Test : hangar ia-tester {p.id}")
        if avis := P.key_withheld_message(p):
            print(f"! {avis}")


def cmd_test(args):
    from ..extensions import db
    from .core import test_provider
    from .models import AIProvider
    app = _app(args)
    with app.test_request_context("/"):
        rows = [db.session.get(AIProvider, args.id)] if args.id else AIProvider.query.order_by(AIProvider.priority).all()
        if not rows or rows[0] is None:
            print("✗ Aucun fournisseur.")
            sys.exit(1)
        bad = 0
        for p in rows:
            ok, msg = test_provider(p)
            print(f"{'✓' if ok else ('?' if ok is None else '✗')} [{p.id}] {p.name} : {msg}")
            bad += 0 if ok else 1
        if bad:
            sys.exit(1)


def cmd_ask(args):
    from .core import generate
    from .providers import AIError
    app = _app(args)
    with app.test_request_context("/"):
        try:
            final = None
            for ev in generate(args.question, role=args.role, stream=True, feature="cli",
                               system="Réponds en français, de façon concise."):
                if ev["type"] == "text":
                    print(ev["text"], end="", flush=True)
                elif ev["type"] == "done":
                    final = ev["result"]
            print()
            if final is not None:
                u = final.usage
                print(f"\n— {final.provider} · {final.model} · {u.input_tokens} + {u.output_tokens} jetons"
                      + (f" · {final.cost:.4f} $" if final.cost else ""), file=sys.stderr)
        except AIError as e:
            print(f"\n✗ {e.message}")
            sys.exit(1)


def cmd_usage(args):
    from .core import month_cost, month_start, usage_rows
    from .models import AISettings
    app = _app(args)
    with app.test_request_context("/"):
        cap = AISettings.get().monthly_cap
        print(f"Dépense du mois : {month_cost():.4f} $" + (f" (plafond {cap:.2f} $)" if cap else " (pas de plafond)"))
        for key, n, tin, tout, cost in usage_rows(month_start(), group="model"):
            print(f"  {key or '?'} : {n} appel(s), {tin} + {tout} jetons, {cost:.4f} $")


def cmd_jobs(args):
    from . import jobs
    app = _app(args)
    with app.test_request_context("/"):
        n_batches = jobs.check_batches()
        n = jobs.run_pending()
        print(f"{n} travail(aux) fait(s), {n_batches} lot(s) récupéré(s).")


def cmd_eval(args):
    from . import core, qualite
    from .models import AISettings
    from .providers import AIError
    app = _app(args)
    with app.test_request_context("/"):
        rows = qualite.fournisseurs(args.id)
        if not rows:
            print("✗ Aucun fournisseur d’IA à évaluer." if args.id is None else f"✗ Fournisseur [{args.id}] introuvable.")
            sys.exit(1)
        cas = [c for c in qualite.CAS if not args.cas or c.id in args.cas]
        if not cas:
            print("✗ Aucun cas ne porte ce nom. Cas : " + ", ".join(c.id for c in qualite.CAS))
            sys.exit(1)
        echec = False
        for row in rows:
            print(f"\n[{row.id}] {row.name} — {len(cas)} cas")
            prevu = qualite.estimation(row, cas) if qualite.payant(row) else 0.0
            if prevu:
                cap = AISettings.get().monthly_cap or 0
                reste = cap - core.month_cost() if cap else None
                print(f"  Passage payant : environ {prevu:.2f} $"
                      + (f" (il reste {max(0.0, reste):.2f} $ sous le plafond du mois)." if reste is not None else "."))
                if reste is not None and prevu > reste:
                    print("  ✗ Plus que ce qui reste sous le plafond : passage annulé. Relève le plafond ou choisis "
                          "moins de cas (--cas).")
                    echec = True
                    continue
                if not args.oui:
                    try:
                        ok = input("  Lancer ce passage ? [o/N] ").strip().lower() in ("o", "oui", "y", "yes")
                    except EOFError:
                        ok = False
                    if not ok:
                        print("  Passage annulé.")
                        continue

            def montrer(x):
                signe = {"ok": "✓", "echec": "✗"}.get(x["statut"], "–")
                print(f"  {signe} {x['titre']} : {x['detail']}")

            try:
                rapport = qualite.evaluer(row, cas, progres=montrer)
            except AIError as e:
                print(f"  ✗ {e.message}")
                echec = True
                continue
            par_modele: dict[str, list] = {}
            for x in rapport["resultats"]:
                if x["statut"] != "sans_objet":
                    par_modele.setdefault(x["modele"], []).append(x["statut"] == "ok")
            for modele, oks in par_modele.items():
                print(f"  → {core.model_label(modele)} : {sum(oks)}/{len(oks)} réussis ({round(100 * sum(oks) / len(oks))} %)")
            if rapport["sans_objet"]:
                print(f"  ({rapport['sans_objet']} cas sans objet : outils désactivés)")
            print(f"  Coût du passage : {rapport['cout']:.4f} $")
            echec = echec or rapport["reussis"] < rapport["total"]
        if echec:
            sys.exit(1)


def register(sub) -> dict:
    sub.add_parser("ia-fournisseurs", help="lister les fournisseurs d’IA")
    a = sub.add_parser("ia-ajouter", help="ajouter un fournisseur d’IA (anthropic, ollama, lmstudio, vllm, llamacpp, "
                                          "openrouter, mistral, groq, together, custom)")
    a.add_argument("preset")
    a.add_argument("--model")
    a.add_argument("--url", help="adresse compatible OpenAI (ex. http://localhost:11434/v1)")
    a.add_argument("--key", help="clé d’API (sinon : variable d’environnement)")
    a.add_argument("--key-env", help="variable d’environnement qui contient la clé")
    a.add_argument("--name")
    t = sub.add_parser("ia-tester", help="tester la connexion aux fournisseurs d’IA")
    t.add_argument("id", nargs="?", type=int)
    q = sub.add_parser("ia-demander", help="poser une question au modèle configuré")
    q.add_argument("question")
    q.add_argument("--role", default="chat", choices=["chat", "rapide", "vision"])
    sub.add_parser("ia-usage", help="dépense et jetons de l’IA ce mois-ci")
    sub.add_parser("ia-travaux", help="faire les travaux d’IA en attente et récupérer les lots terminés")
    e = sub.add_parser("ia-evaluer", help="évaluer les modèles configurés sur des cas fixes (taux de réussite)")
    e.add_argument("id", nargs="?", type=int, help="fournisseur à évaluer (par défaut : tous les fournisseurs activés)")
    e.add_argument("--oui", action="store_true", help="ne pas demander confirmation avant un passage payant")
    e.add_argument("--cas", nargs="*", help="seulement ces cas (ex. tache-vendredi qcm-json)")
    return {"ia-fournisseurs": cmd_list, "ia-ajouter": cmd_add, "ia-tester": cmd_test, "ia-demander": cmd_ask,
            "ia-usage": cmd_usage, "ia-travaux": cmd_jobs, "ia-evaluer": cmd_eval}
