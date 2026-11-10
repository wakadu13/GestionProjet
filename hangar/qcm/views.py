"""QCM : routes (squelette, remplacé par le portage de Revision)."""
from flask import Blueprint, render_template
from flask_login import login_required

bp = Blueprint("qcm", __name__)


@bp.route("/qcm/")
@login_required
def index():
    return render_template("qcm/placeholder.html")
