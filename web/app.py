"""Flask application factory for Awesome Router 2 web GUI."""
from __future__ import annotations
import os
import sys

from flask import Flask

# Ensure awesome_router package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web.routes.dashboard import bp as dashboard_bp
from web.routes.wans import bp as wans_bp
from web.routes.system import bp as system_bp
from web.routes.failover import bp as failover_bp
from web.routes.api import bp as api_bp


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        static_folder=os.path.join(os.path.dirname(__file__), "static"),
    )
    app.secret_key = os.environ.get("FLASK_SECRET", os.urandom(32))

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(wans_bp, url_prefix="/wans")
    app.register_blueprint(system_bp, url_prefix="/system")
    app.register_blueprint(failover_bp, url_prefix="/failover")
    app.register_blueprint(api_bp, url_prefix="/api")

    return app
