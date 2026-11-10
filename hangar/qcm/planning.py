"""Planning des révisions d'un set : quotas journaliers et estimation de la durée.

Deux quotas par jour, comme dans Anki : les nouvelles questions (jamais vues) et
les révisions (questions déjà vues, revenues à échéance).

L'estimation simule la révision espacée (FSRS, comme la planification réelle)
jour après jour : chaque jour, les questions dues sont révisées dans la limite
des quotas, avec un taux de bonnes réponses égal à la précision du joueur sur ce
set. Une question est « sue » dès que sa stabilité atteint STABILITE_OBJECTIF
jours, même si elle retombe ensuite (compter les questions actuellement stables
ne convergerait pas toujours). Le set est prêt quand une part PART_OBJECTIF des
questions est sue (viser 100 % ferait dépendre l'estimation de la question la
plus malchanceuse).

À l'approche d'un examen, la rétention visée passe de 90 % à RETENTION_EXAMEN :
les révisions se resserrent. Comme quiz.py, ce module ne dépend ni des modèles
ni des vues.
"""

import math
from datetime import timedelta

from . import fsrs, langues, quiz

STABILITE_OBJECTIF = 7.0    # stabilité (jours de rétention à 90 %) à partir de laquelle une question est sue
PART_OBJECTIF = 0.95        # part des questions sues pour considérer le set prêt
RETENTION_EXAMEN = 0.95     # rétention visée quand l'examen approche
JOURS_EXAMEN_PROCHE = 30    # … c'est-à-dire à moins de 30 jours
PRECISION_DEFAUT = 0.8      # tant qu'il y a trop peu de réponses pour la mesurer
PRECISION_MIN, PRECISION_MAX = 0.6, 0.97
REPONSES_MIN_PRECISION = 20
SECONDES_DEFAUT = 20        # temps par question tant qu'il n'est pas mesuré
REPONSES_MIN_CHRONO = 10
HORIZON_JOURS = 365


def faites_aujourdhui(progress, now):
    """(nouvelles, révisions) déjà faites aujourd'hui sur ce set (heure locale).

    Une question découverte aujourd'hui compte comme nouvelle, même si elle a
    été revue ensuite dans la journée.
    """
    jour = quiz.jour(now)
    nouvelles = revisions = 0
    for st in progress.values():
        if st.premiere_revue and quiz.jour(st.premiere_revue) == jour:
            nouvelles += 1
        elif st.last_review and quiz.jour(st.last_review) == jour:
            revisions += 1
    return nouvelles, revisions


def restant(limite, deja):
    """Places restantes aujourd'hui pour un quota (None : pas de limite)."""
    return None if limite is None else max(0, limite - deja)


def retention_cible(date_objectif, now):
    """Rétention visée à l'enregistrement d'une réponse : plus haute (révisions plus
    serrées) dans les JOURS_EXAMEN_PROCHE derniers jours avant l'examen."""
    if date_objectif is None:
        return fsrs.RETENTION
    dispo = (date_objectif - quiz.jour(now)).days
    return RETENTION_EXAMEN if 0 < dispo <= JOURS_EXAMEN_PROCHE else fsrs.RETENTION


def simuler(cartes, quota_nouvelles, quota_revisions, precision, deja=(0, 0), retention=fsrs.RETENTION):
    """Simule les révisions. `cartes` : liste de (état FSRS ou None si jamais vue,
    jour où elle est due, jours depuis la dernière révision).

    Les quotas sont des nombres par jour ; `deja` = (nouvelles, révisions) déjà
    faites aujourd'hui. Renvoie (jours nécessaires, nombre de réponses), ou None
    au-delà de HORIZON_JOURS.
    """
    etats = [e for e, _, _ in cartes]
    dues = [d for _, d, _ in cartes]
    derniere = [-depuis for _, _, depuis in cartes]
    nouvelles = [i for i, (e, _, _) in enumerate(cartes) if e is None]
    vues = [i for i, (e, _, _) in enumerate(cartes) if e is not None]
    sues = {i for i in vues if etats[i].stabilite >= STABILITE_OBJECTIF}
    restantes = math.ceil(PART_OBJECTIF * len(cartes)) - len(sues)
    if restantes <= 0:
        return 0, 0
    # Échecs répartis régulièrement, carte par carte (une réponse sur 1/(1-p), premier
    # échec décalé d'une carte à l'autre) plutôt que tirés au sort : estimation stable,
    # sans carte « malchanceuse » qui concentrerait tous les échecs, et plus de
    # nouvelles par jour donne une durée plus courte (dichotomie ci-dessous).
    echecs = [(i * 0.6180339887) % 1.0 for i in range(len(cartes))]
    reponses = 0
    for jour in range(HORIZON_JOURS):
        places_n = max(0, quota_nouvelles - (deja[0] if jour == 0 else 0))
        places_r = max(0, quota_revisions - (deja[1] if jour == 0 else 0))
        # Même ordre que la file du jour : les plus menacées d'oubli d'abord, puis nouvelles.
        a_revoir = sorted((i for i in vues if dues[i] <= jour),
                          key=lambda i: (fsrs.retrievabilite(jour - derniere[i], etats[i].stabilite), dues[i]))[:places_r]
        decouvertes, nouvelles = nouvelles[:places_n], nouvelles[places_n:]
        for i in a_revoir + decouvertes:
            reponses += 1
            echecs[i] += 1 - precision
            reussie = echecs[i] < 1
            if not reussie:
                echecs[i] -= 1
            note = fsrs.BIEN if reussie else fsrs.A_REVOIR
            etats[i] = fsrs.premiere(note) if etats[i] is None else fsrs.suivante(etats[i], jour - derniere[i], note)
            derniere[i] = jour
            dues[i] = jour + (1 if not reussie else max(1, int(fsrs.intervalle_jours(etats[i].stabilite, retention) + 0.5)))
            if etats[i].stabilite >= STABILITE_OBJECTIF and i not in sues:
                sues.add(i)
                restantes -= 1
        vues += decouvertes
        if restantes <= 0:
            return jour + 1, reponses
    return None


def _cartes(qset, progress, now):
    aujourdhui = quiz.jour(now)
    cartes = []
    for q in qset.questions:
        st = progress.get(q.id)
        if st is None or st.seen == 0 and st.stabilite is None:
            cartes.append((None, 0, 0))
            continue
        if st.stabilite is not None:
            etat = fsrs.Etat(st.stabilite, st.difficulte if st.difficulte is not None else 5.0)
            echeance = st.echeance
        else:
            # Progression antérieure à FSRS : boîte de Leitner.
            etat = fsrs.Etat(quiz.STABILITE_LEITNER.get(st.box, 30.0), 5.0)
            echeance = st.last_review + timedelta(days=quiz.INTERVALLES_JOURS[st.box]) if st.last_review else None
        due = 0 if st.is_due(now) or echeance is None else max(1, (quiz.jour(echeance) - aujourdhui).days)
        cartes.append((etat, due, langues._jours_depuis(st, now)))
    return cartes


def precision(progress):
    reponses = sum(st.seen for st in progress.values())
    if reponses < REPONSES_MIN_PRECISION:
        return PRECISION_DEFAUT, False
    p = sum(st.correct for st in progress.values()) / reponses
    return min(PRECISION_MAX, max(PRECISION_MIN, p)), True


def estimer(qset, progress, deck, now):
    """Durée estimée pour tout savoir, et date à laquelle commencer pour l'examen."""
    aujourdhui = quiz.jour(now)
    cartes = _cartes(qset, progress, now)
    total = len(cartes)
    p, p_mesuree = precision(progress)
    secondes, s_mesure = deck.secondes_par_carte(SECONDES_DEFAUT, REPONSES_MIN_CHRONO)
    deja = faites_aujourdhui(progress, now)
    # La simulation garde la rétention standard : le resserrement avant l'examen
    # (retention_cible) joue sur la planification réelle, pas sur « quand saurai-je ».
    retention = retention_cible(deck.date_objectif, now)

    def duree(nouvelles, revisions):
        return simuler(cartes, nouvelles, revisions, p, deja)

    def par_jour(resultat):
        jours, reponses = resultat
        return round(reponses / jours) if jours else 0

    def minutes(resultat):
        jours, reponses = resultat
        return round(reponses / jours * secondes / 60) if jours else 0

    # Sans limite, toutes les questions peuvent passer le même jour.
    res = duree(deck.nouvelles_jour or total, deck.revisions_jour or total)
    e = {
        "nouvelles_jour": deck.nouvelles_jour,
        "revisions_jour": deck.revisions_jour,
        "auto": deck.auto_quota,
        "precision": round(p * 100),
        "precision_mesuree": p_mesuree,
        "secondes": round(secondes),
        "secondes_mesurees": s_mesure,
        "stabilite_objectif": int(STABILITE_OBJECTIF),
        "part_objectif": round(PART_OBJECTIF * 100),
        "retention": round(retention * 100),
        "su": res == (0, 0),
        "jours": res[0] if res else None,
        "fin": aujourdhui + timedelta(days=res[0] - 1) if res and res[0] else None,
        "questions_jour": par_jour(res) if res else None,
        "minutes_jour": minutes(res) if res else None,
        "objectif": deck.date_objectif,
    }
    if not deck.date_objectif or e["su"]:
        return e

    # Jours de révision disponibles : d'aujourd'hui à la veille de l'examen.
    dispo = (deck.date_objectif - aujourdhui).days
    e["jours_dispo"] = dispo
    if dispo <= 0:
        e["statut"] = "passee"
    elif res and res[0] <= dispo:
        debut = deck.date_objectif - timedelta(days=res[0])
        e["statut"] = "ok"
        e["debut"] = debut
        e["commencer_maintenant"] = debut <= aujourdhui
        e["marge"] = dispo - res[0]
    else:
        # Conseil à la Anki : ne pas plafonner les révisions, et chercher le plus
        # petit nombre de nouvelles questions par jour qui suffit (dichotomie).
        meilleur = duree(total, total)
        if not meilleur or meilleur[0] > dispo:
            e["statut"] = "impossible"
            e["jours_min"] = meilleur[0] if meilleur else None
            return e
        bas, haut = 1, total
        while bas < haut:
            milieu = (bas + haut) // 2
            r = duree(milieu, total)
            if r and r[0] <= dispo:
                haut, meilleur = milieu, r
            else:
                bas = milieu + 1
        e["statut"] = "retard"
        e["nouvelles_conseillees"] = haut
        e["questions_conseillees"] = par_jour(meilleur)
        e["minutes_conseillees"] = minutes(meilleur)
        e["revisions_limitees"] = bool(deck.revisions_jour)
    return e


def quotas_effectifs(qset, progress, deck, now):
    """Quotas du jour : ceux du set, ou, si l'ajustement automatique est coché et que
    l'estimation dit « en retard » pour l'examen, les nouvelles conseillées et des
    révisions sans limite. Renvoie (nouvelles_jour, revisions_jour)."""
    if deck is None:
        return None, None
    if deck.auto_quota and deck.date_objectif:
        e = estimer(qset, progress, deck, now)
        if e.get("statut") == "retard":
            return e["nouvelles_conseillees"], None
    return deck.nouvelles_jour, deck.revisions_jour
