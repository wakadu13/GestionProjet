"""Workgraph et initiatives (squelette)."""
from flask import Blueprint, render_template
from flask_login import login_required

bp = Blueprint("workgraph", __name__)


@bp.route("/workgraph")
@login_required
def index():
    return render_template("workgraph/placeholder.html")


@bp.route("/initiatives")
@login_required
def initiatives():
    return render_template("workgraph/placeholder.html")
