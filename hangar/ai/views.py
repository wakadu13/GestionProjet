"""Assistant IA : routes (squelette, remplacé par l'implémentation complète)."""
from flask import Blueprint, jsonify, render_template
from flask_login import login_required

bp = Blueprint("ai", __name__)


def _bientot():
    return jsonify(ok=False, error="L’assistant IA n’est pas encore configuré."), 503


@bp.route("/ia/")
@login_required
def index():
    return render_template("ai/placeholder.html")


@bp.route("/ia/reglages/", methods=["GET", "POST"])
@login_required
def reglages():
    return render_template("ai/placeholder.html")


@bp.route("/ia/chat", methods=["POST"])
@login_required
def chat():
    return _bientot()


# Points d'entrée des fonctions d'IA de Revision (mêmes adresses que dans Revision)
for _rule, _name in [("/qcm/expliquer/", "expliquer"), ("/qcm/corriger-qrc/", "corriger_qrc"),
                     ("/qcm/cartes-de-la-page/", "cartes_page"), ("/qcm/generer/", "generer_qcm"),
                     ("/qcm/importer-vocabulaire/", "importer_vocabulaire"), ("/claude/ameliorer/", "ameliorer"),
                     ("/claude/tuteur/", "tuteur"), ("/transformations/", "transformations"),
                     ("/demander/", "demander"), ("/copie/", "copie")]:
    bp.add_url_rule(_rule, _name, login_required(_bientot), methods=["GET", "POST"])
