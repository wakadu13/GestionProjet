"""Modèles des QCM (SQLAlchemy) : progression FSRS, réglages des sets, activité, temps de
révision, objectifs, signalements et explications de l'IA.

Port des modèles Django de Revision (`wiki/models.py`) : le « joueur » (nom d'utilisateur)
devient `user_id`, les dates sont des heures locales naïves. Les méthodes (`record`, FSRS,
`urgence`, `serie`, `calendrier`…) sont gardées telles quelles.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func

from ..extensions import db
from ..texte import cle_tri
from . import fsrs, langues, quiz

# Date très ancienne, pour trier les questions jamais révisées en premier.
_JAMAIS = datetime.min


def _get_or_create(model, **kw):
    obj = model.query.filter_by(**kw).first()
    if obj is None:
        obj = model(**kw)
        db.session.add(obj)
    return obj


class _Defauts:
    """Valeurs par défaut posées dès la création de l'objet (comme Django), pas seulement à l'écriture."""
    _DEFAUTS: dict = {}

    def __init__(self, **kw):
        for k, v in self._DEFAUTS.items():
            kw.setdefault(k, v)
        super().__init__(**kw)


class QuizProgress(_Defauts, db.Model):
    """Suivi d'une question (ou d'une facette d'exercice de langue) pour un compte."""
    __tablename__ = "qcm_progress"
    __table_args__ = (db.UniqueConstraint("user_id", "set_id", "question_id", name="uq_qcm_progress"),
                      db.Index("ix_qcm_progress_user_set", "user_id", "set_id"))
    _DEFAUTS = {"box": 0, "seen": 0, "correct": 0, "oublis": 0}

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    set_id = db.Column(db.String(500), nullable=False)
    question_id = db.Column(db.String(255), nullable=False)
    box = db.Column(db.Integer, default=0)          # boîte de Leitner (0–5), indicateur hérité
    seen = db.Column(db.Integer, default=0)         # nombre de passages
    correct = db.Column(db.Integer, default=0)      # réponses correctes
    last_review = db.Column(db.DateTime)
    premiere_revue = db.Column(db.DateTime)         # 1re réponse : compte les nouvelles du jour
    last_correct = db.Column(db.Boolean)
    stabilite = db.Column(db.Float)                 # jours avant 90 % de rappel
    difficulte = db.Column(db.Float)                # 1 à 10
    echeance = db.Column(db.DateTime)               # prochaine révision
    decouverte = db.Column(db.DateTime)             # 1re révision : quota de nouveautés
    oublis = db.Column(db.Integer, default=0)       # échecs après avoir été appris

    def record(self, reponse, quand=None, retention=None) -> None:
        """Enregistre une réponse : note (quiz.NOTES) ou booléen juste/faux. Met à jour
        les statistiques, la boîte de Leitner (indicateur hérité) et l'état FSRS, qui fixe
        la prochaine échéance.

        `quand` : date de la réponse si elle n'est pas immédiate (réponses gardées sur
        l'appareil puis envoyées) ; `retention` : probabilité de rappel visée (90 % par
        défaut, plus haute à l'approche d'un examen)."""
        now = quand or quiz.maintenant()
        note = quiz.note_de(reponse)
        correcte = note != quiz.A_REVOIR
        note_fsrs = quiz.NOTES.index(note) + 1
        deja_appris = self.stabilite is not None or self.seen > 0
        etat = self._etat_suivant(note_fsrs, now)  # avant de changer last_review
        self.seen += 1
        if correcte:
            self.correct += 1
        self.box = quiz.nouvelle_boite(self.box, reponse)
        self.last_correct = correcte
        self.last_review = now
        if self.premiere_revue is None:
            self.premiere_revue = now
        self.stabilite = round(etat.stabilite, 3)
        self.difficulte = round(etat.difficulte, 3)
        self.echeance = now + langues.delai(etat, note_fsrs, retention)
        if self.decouverte is None and not deja_appris:
            self.decouverte = now
        if deja_appris and not correcte:
            self.oublis += 1

    def _etat_suivant(self, note: int, now):
        if self.stabilite is not None and self.difficulte is not None:
            return langues.etat_suivant(self, note, now)
        if self.seen > 0:
            # Progression antérieure à FSRS (boîtes de Leitner) : stabilité estimée d'après la boîte.
            depuis = langues._jours_depuis(self, now)
            return fsrs.suivante(fsrs.Etat(quiz.STABILITE_LEITNER.get(self.box, 30.0), 5.0), depuis, note)
        return fsrs.premiere(note)

    def record_langue(self, note: int, quand=None) -> None:
        """Enregistre une révision du module Langues (note FSRS 1 à 4) ; même suivi que les QCM."""
        self.record(quiz.NOTES[note - 1], quand)

    def is_due(self, now=None) -> bool:
        now = now or quiz.maintenant()
        if self.echeance is not None:
            return self.echeance <= now
        return quiz.intervalle_ecoule(self.box, self.last_review, now)

    def urgence(self, now=None) -> float:
        """0 (oublié) à 1 (bien su) : rétention FSRS, ou boîte de Leitner pour une progression ancienne."""
        if self.stabilite is not None:
            return langues.retention(self, now or quiz.maintenant())
        return self.box / quiz.BOITE_MAX

    def priority(self, now=None):
        """Clé de tri : à réviser d'abord, puis les plus menacées d'oubli, puis plus ancien."""
        return (0 if self.is_due(now) else 1, self.urgence(now), self.last_review or _JAMAIS)

    @classmethod
    def map_for(cls, user_id: int, set_id: str) -> dict:
        """{question_id: QuizProgress} pour un set et un compte."""
        return {p.question_id: p for p in cls.query.filter_by(user_id=user_id, set_id=set_id)}

    @classmethod
    def map_all(cls, user_id: int) -> dict:
        """{set_id: {question_id: QuizProgress}} pour tous les sets d'un compte (une seule requête)."""
        out: dict = {}
        for p in cls.query.filter_by(user_id=user_id):
            out.setdefault(p.set_id, {})[p.question_id] = p
        return out

    @classmethod
    def map_langue(cls, user_id: int, sets) -> dict:
        """{(set_id, exercice_id): QuizProgress} pour des fichiers de langue."""
        rows = cls.query.filter(cls.user_id == user_id, cls.set_id.in_([s.id for s in sets])) if sets else []
        return {(p.set_id, p.question_id): p for p in rows}


class QuizDeck(_Defauts, db.Model):
    """Réglages d'un set pour un compte (quotas journaliers, date d'examen) et temps de
    réponse mesuré, qui sert à estimer la durée des révisions."""
    __tablename__ = "qcm_deck"
    __table_args__ = (db.UniqueConstraint("user_id", "set_id", name="uq_qcm_deck"),)
    _DEFAUTS = {"auto_quota": False, "temps_ms": 0, "reponses_chronometrees": 0}

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    set_id = db.Column(db.String(500), nullable=False)
    nouvelles_jour = db.Column(db.Integer)          # questions jamais vues, max par jour
    revisions_jour = db.Column(db.Integer)          # questions déjà vues, max par jour
    date_objectif = db.Column(db.Date)              # date d'examen
    auto_quota = db.Column(db.Boolean, default=False)  # nouvelles/jour ajustées pour être prêt à temps
    temps_ms = db.Column(db.BigInteger, default=0)  # cumul des temps de réponse
    reponses_chronometrees = db.Column(db.Integer, default=0)

    @property
    def configure(self) -> bool:
        return bool(self.nouvelles_jour or self.revisions_jour or self.date_objectif)

    def secondes_par_carte(self, defaut, minimum):
        """(secondes moyennes par réponse, mesurée ?) ; `defaut` s'il y a moins de `minimum` mesures."""
        if (self.reponses_chronometrees or 0) < minimum:
            return defaut, False
        return self.temps_ms / self.reponses_chronometrees / 1000, True

    @classmethod
    def map_for(cls, user_id: int) -> dict:
        """{set_id: QuizDeck} pour un compte."""
        return {d.set_id: d for d in cls.query.filter_by(user_id=user_id)}

    @classmethod
    def pour(cls, decks: dict, user_id: int, set_id: str):
        """Réglages du set, ou réglages vides (non enregistrés) s'il n'en a pas."""
        return decks.get(set_id) or cls(user_id=user_id, set_id=set_id)


class Activite(_Defauts, db.Model):
    """Réponses d'un compte par jour (QCM et langues) : série de jours, calendrier."""
    __tablename__ = "qcm_activite"
    __table_args__ = (db.UniqueConstraint("user_id", "jour", name="uq_qcm_activite"),)
    _DEFAUTS = {"reponses": 0, "justes": 0, "nouveaux": 0}

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    jour = db.Column(db.Date, nullable=False)
    reponses = db.Column(db.Integer, default=0)
    justes = db.Column(db.Integer, default=0)
    nouveaux = db.Column(db.Integer, default=0)  # questions ou mots vus pour la 1re fois

    # Paliers du calendrier : 0 réponse, puis 1-9, 10-29, 30-59, 60 et plus.
    PALIERS = (1, 10, 30, 60)

    @classmethod
    def noter(cls, user_id, correct, nouveau=False, quand=None):
        """Compte une réponse (à la date `quand`, maintenant par défaut)."""
        obj = _get_or_create(cls, user_id=user_id, jour=quiz.jour(quand or quiz.maintenant()))
        obj.reponses = (obj.reponses or 0) + 1
        obj.justes = (obj.justes or 0) + (1 if correct else 0)
        obj.nouveaux = (obj.nouveaux or 0) + (1 if nouveau else 0)

    @classmethod
    def jours_actifs(cls, user_id):
        return {j for (j,) in db.session.query(cls.jour).filter(cls.user_id == user_id, cls.reponses > 0)}

    @classmethod
    def serie(cls, user_id, aujourdhui=None, jours=None):
        """(série en cours, record) : jours consécutifs avec au moins une réponse.
        La série tient encore si la dernière réponse date d'hier."""
        aujourdhui = aujourdhui or quiz.jour(quiz.maintenant())
        jours = cls.jours_actifs(user_id) if jours is None else jours
        courante = 0
        depart = aujourdhui if aujourdhui in jours else aujourdhui - timedelta(days=1)
        while depart in jours:
            courante += 1
            depart -= timedelta(days=1)
        record = 0
        for j in jours:
            if j - timedelta(days=1) not in jours:
                n, k = 0, j
                while k in jours:
                    n += 1
                    k += timedelta(days=1)
                record = max(record, n)
        return courante, record

    @classmethod
    def calendrier(cls, user_id, aujourdhui=None, semaines=26):
        """Semaines (lundi → dimanche) des `semaines` dernières semaines, pour le
        calendrier d'activité : [{jour, reponses, justes, niveau}] par semaine."""
        aujourdhui = aujourdhui or quiz.jour(quiz.maintenant())
        fin = aujourdhui + timedelta(days=6 - aujourdhui.weekday())  # dimanche de la semaine
        debut = fin - timedelta(weeks=semaines) + timedelta(days=1)
        par_jour = {a.jour: a for a in cls.query.filter(cls.user_id == user_id, cls.jour >= debut, cls.jour <= fin)}
        grille = []
        jour = debut
        while jour <= fin:
            semaine = []
            for _ in range(7):
                a = par_jour.get(jour)
                n = a.reponses if a else 0
                semaine.append({"jour": jour, "reponses": n, "justes": a.justes if a else 0,
                                "niveau": sum(n >= p for p in cls.PALIERS), "futur": jour > aujourdhui})
                jour += timedelta(days=1)
            grille.append(semaine)
        return grille

    @classmethod
    def resume(cls, user_id, aujourdhui=None):
        """Série, record, réponses du jour, des 7 et des 30 derniers jours, jours actifs sur 30."""
        aujourdhui = aujourdhui or quiz.jour(quiz.maintenant())
        jours = cls.jours_actifs(user_id)
        courante, record = cls.serie(user_id, aujourdhui, jours)
        depuis = aujourdhui - timedelta(days=29)
        recents = cls.query.filter(cls.user_id == user_id, cls.jour >= depuis, cls.jour <= aujourdhui).all()
        sept = [a for a in recents if a.jour > aujourdhui - timedelta(days=7)]
        auj = next((a for a in recents if a.jour == aujourdhui), None)
        return {
            "serie": courante, "record": record,
            "aujourdhui": auj.reponses if auj else 0,
            "justes_aujourdhui": auj.justes if auj else 0,
            "nouveaux_aujourdhui": auj.nouveaux if auj else 0,
            "trente_jours": sum(a.reponses for a in recents),
            "justes_trente_jours": sum(a.justes for a in recents),
            "sept_jours": sum(a.reponses for a in sept),
            "justes_sept_jours": sum(a.justes for a in sept),
            "jours_actifs_trente": sum(1 for j in jours if depuis <= j <= aujourdhui),
        }


class Explication(db.Model):
    """Explication d'une question de QCM écrite par l'IA à la demande (« Explique-moi »),
    gardée pour ne pas payer deux fois (utilisée par le module ai)."""
    __tablename__ = "qcm_explication"
    __table_args__ = (db.UniqueConstraint("set_id", "question_id", name="uq_qcm_explication"),)

    id = db.Column(db.Integer, primary_key=True)
    set_id = db.Column(db.String(500), nullable=False)
    question_id = db.Column(db.String(255), nullable=False)
    texte = db.Column(db.Text, nullable=False)
    modele = db.Column(db.String(64), default="")
    jetons_entree = db.Column(db.Integer, default=0)
    jetons_sortie = db.Column(db.Integer, default=0)
    cree_le = db.Column(db.DateTime, default=quiz.maintenant)


class Signalement(db.Model):
    """Question de QCM signalée par un compte (erreur, ambiguïté, coquille). L'admin la
    corrige à la main, la retire, ou applique la correction proposée par l'IA."""
    __tablename__ = "qcm_signalement"

    id = db.Column(db.Integer, primary_key=True)
    set_id = db.Column(db.String(500), nullable=False)
    question_id = db.Column(db.String(255), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    commentaire = db.Column(db.Text, default="")
    cree_le = db.Column(db.DateTime, default=quiz.maintenant)
    traite = db.Column(db.Boolean, default=False, index=True)
    traite_le = db.Column(db.DateTime)
    proposition = db.Column(db.JSON)                 # question corrigée proposée par l'IA
    proposition_note = db.Column(db.Text, default="")  # ce que l'IA a changé, et pourquoi

    user = db.relationship("User")

    @classmethod
    def ouverts(cls):
        return cls.query.filter_by(traite=False).order_by(cls.cree_le.desc(), cls.id.desc())

    def __repr__(self):
        return f"<Signalement {self.set_id} » {self.question_id}>"


class EmpreinteSet(db.Model):
    """Empreinte du contenu d'un set ou d'un paquet de langue (identifiants et textes des questions),
    pour le retrouver quand son fichier est renommé ou déplacé hors de Hangar (rattachement.py)."""
    __tablename__ = "qcm_empreinte"

    id = db.Column(db.Integer, primary_key=True)
    set_id = db.Column(db.String(500), nullable=False, unique=True)
    empreinte = db.Column(db.String(64), nullable=False, index=True)  # de tout le set
    questions = db.Column(db.Text, default="")      # empreintes courtes des questions, séparées par des espaces
    langue = db.Column(db.Boolean, default=False)   # paquet de langue (sinon set de QCM)
    disparu_le = db.Column(db.DateTime)             # fichier absent du coffre depuis…

    @property
    def cles(self) -> set:
        return set((self.questions or "").split())


class AliasSet(db.Model):
    """Ancien identifiant d'un set → identifiant actuel (set déplacé, renommé ou rattaché à la main) :
    les réponses envoyées plus tard sous l'ancien identifiant (application hors ligne, page restée
    ouverte) rejoignent la bonne progression. `user_id` vide : pour tous les comptes ; sinon, pour ce
    compte seulement (« Rattacher à… » qui ne vaut que pour lui), et prioritaire pour lui."""
    __tablename__ = "qcm_alias"
    __table_args__ = (db.UniqueConstraint("ancien", "user_id", name="uq_qcm_alias"),)

    id = db.Column(db.Integer, primary_key=True)
    ancien = db.Column(db.String(500), nullable=False, index=True)
    nouveau = db.Column(db.String(500), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), index=True)
    cree_le = db.Column(db.DateTime, default=quiz.maintenant)

    def __repr__(self):
        return f"<AliasSet {self.ancien} → {self.nouveau}>"


class TempsRevision(_Defauts, db.Model):
    """Temps passé à réviser, par compte, jour et matière (catégorie de QCM, ou langue)."""
    __tablename__ = "qcm_temps"
    __table_args__ = (db.UniqueConstraint("user_id", "jour", "matiere", name="uq_qcm_temps"),)
    _DEFAUTS = {"temps_ms": 0, "reponses": 0}

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    jour = db.Column(db.Date, nullable=False)
    matiere = db.Column(db.String(200), nullable=False)
    temps_ms = db.Column(db.BigInteger, default=0)
    reponses = db.Column(db.Integer, default=0)

    DUREE_MAX_MS = 120_000  # au-delà, on s'est sans doute absenté

    @classmethod
    def noter(cls, user_id, matiere, duree, quand=None):
        """Ajoute une durée de réponse (ms) ; ignore une durée absente ou invalide."""
        if not matiere or not isinstance(duree, (int, float)) or isinstance(duree, bool) or duree <= 0:
            return
        obj = _get_or_create(cls, user_id=user_id, jour=quiz.jour(quand or quiz.maintenant()), matiere=str(matiere)[:200])
        obj.temps_ms = (obj.temps_ms or 0) + int(min(duree, cls.DUREE_MAX_MS))
        obj.reponses = (obj.reponses or 0) + 1

    @classmethod
    def minutes_du_jour(cls, user_id, jour):
        total = db.session.query(func.sum(cls.temps_ms)).filter(cls.user_id == user_id, cls.jour == jour).scalar() or 0
        return round(total / 60000)

    @classmethod
    def tableau(cls, user_id, aujourdhui=None, semaines=8):
        """Minutes par matière et par semaine (lundi), les `semaines` dernières :
        (semaines [lundis], lignes [{matiere, minutes: [..], total}], totaux [..])."""
        aujourdhui = aujourdhui or quiz.jour(quiz.maintenant())
        lundi = aujourdhui - timedelta(days=aujourdhui.weekday())
        lundis = [lundi - timedelta(weeks=k) for k in range(semaines - 1, -1, -1)]
        par_matiere: dict = {}
        for t in cls.query.filter(cls.user_id == user_id, cls.jour >= lundis[0]):
            k = (t.jour - lundis[0]).days // 7
            if 0 <= k < semaines:
                par_matiere.setdefault(t.matiere, [0] * semaines)[k] += t.temps_ms
        lignes = [{"matiere": m, "minutes": [round(ms / 60000) for ms in v], "total": round(sum(v) / 60000)}
                  for m, v in par_matiere.items()]
        lignes.sort(key=lambda l: (-l["total"], cle_tri(l["matiere"])))
        totaux = [sum(l["minutes"][k] for l in lignes) for k in range(semaines)]
        return lundis, lignes, totaux


class ObjectifRevision(db.Model):
    """Objectif quotidien d'un compte : nombre de réponses par jour (page « Aujourd'hui »)."""
    __tablename__ = "qcm_objectif"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, unique=True)
    reponses_jour = db.Column(db.Integer)

    @classmethod
    def de(cls, user_id):
        o = cls.query.filter_by(user_id=user_id).first()
        return o.reponses_jour if o else None


class AbonnementPush(db.Model):
    """Notifications web (navigateur, application installée) : un abonnement par appareil, pour le
    rappel quotidien de révision (rappels.py, push.py)."""
    __tablename__ = "qcm_abonnement_push"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint = db.Column(db.String(1000), nullable=False, unique=True)
    p256dh = db.Column(db.String(200), nullable=False)
    auth = db.Column(db.String(100), nullable=False)
    heure = db.Column(db.Integer, default=19)
    minute = db.Column(db.Integer, default=0)
    agent = db.Column(db.String(300), default="")
    cree_le = db.Column(db.DateTime, default=quiz.maintenant)
    dernier_envoi = db.Column(db.Date)  # au plus un rappel par jour
    # Envois en échec (réseau, service de notifications) : nouvel essai 15 min plus tard au plus tôt ;
    # abonnement supprimé après plusieurs refus (403) d'affilée.
    dernier_echec = db.Column(db.DateTime)
    refus = db.Column(db.Integer, default=0)
    # Ce que reçoit l'appareil : le rappel quotidien des QCM (heure, minute) et/ou les rappels de
    # l'agenda dont le moyen est « notification push » (activés depuis « Rappels et e-mails »). Un appareil
    # peut ne vouloir que l'un des deux ; couper l'un ne coupe pas l'autre.
    quotidien = db.Column(db.Boolean, default=True)
    agenda = db.Column(db.Boolean, default=False)

    user = db.relationship("User")

    def __repr__(self):
        return f"<AbonnementPush {self.user_id} · {self.heure:02d}:{self.minute:02d}>"


# Tables des jetons de l'application Android et du blocage des connexions (modules de Hangar
# hors QCM, mais créées avec celles-ci : create_app importe ce fichier avant db.create_all()).
from .. import appareils as _appareils, blocage_connexion as _blocage  # noqa: E402,F401
