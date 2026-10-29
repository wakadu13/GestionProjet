"""Extensions Flask partagées (base de données, sessions utilisateur)."""
import sqlite3

from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Connecte-toi pour accéder à cette page."
login_manager.login_message_category = "info"


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    """Active les clés étrangères et le mode WAL (plusieurs lecteurs simultanés)."""
    if isinstance(dbapi_conn, sqlite3.Connection):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()
