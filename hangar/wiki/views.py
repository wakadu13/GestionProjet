"""Wiki : routes (squelette, remplacé par le portage de Revision)."""
from flask import Blueprint, render_template
from flask_login import login_required

bp = Blueprint("wiki", __name__)


@bp.route("/wiki/")
@login_required
def home():
    return render_template("wiki/placeholder.html")
