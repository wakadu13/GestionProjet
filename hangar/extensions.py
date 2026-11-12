"""Extensions Flask partagées (base de données, sessions utilisateur)."""
import sqlite3

from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

from .texte import COLLATION_FR, comparer, contient_sql, plier

db = SQLAlchemy()

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Connecte-toi pour accéder à cette page."
login_manager.login_message_category = "info"


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    """Active les clés étrangères et le mode WAL (plusieurs lecteurs simultanés), et déclare la
    recherche et le tri à la française (voir texte.py) : les fonctions ``plier(texte)`` (sans casse
    ni accents) et ``contient_plie(texte, aiguille)`` (utils.contient), et la collation « fr »
    (``ORDER BY nom COLLATE fr`` : « École » avant « Zèbre »)."""
    if isinstance(dbapi_conn, sqlite3.Connection):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()
        dbapi_conn.create_function("plier", 1, plier, deterministic=True)
        dbapi_conn.create_function("contient_plie", 2, contient_sql, deterministic=True)
        dbapi_conn.create_collation(COLLATION_FR, comparer)


def vider_journal() -> bool:
    """Recopie le journal WAL (``hangar.db-wal``) dans ``hangar.db`` et le vide : le fichier ``hangar.db`` seul
    contient alors tout, même si Hangar est fermé brutalement ensuite (fenêtre fermée sous Windows). Appelé
    après chaque sauvegarde, régulièrement par le fil d'arrière-plan et à l'arrêt. Renvoie False si des
    lectures en cours l'en ont empêché (il sera refait au prochain passage). Ne lève jamais."""
    import logging

    from sqlalchemy import text
    try:
        with db.engine.connect() as con:
            occupe = con.execute(text("PRAGMA wal_checkpoint(TRUNCATE)")).fetchone()
        return not (occupe and occupe[0])
    except Exception:  # noqa: BLE001 - base verrouillée, hors application : on réessaiera
        logging.getLogger(__name__).warning("Journal de la base non vidé", exc_info=True)
        return False
