"""WeatherSniffer app factory.

One process, one gunicorn worker: the poller (APScheduler), the rules/actions
engine, and the retention janitor all live in this process with in-memory
state. Never run with --workers > 1 (jobs would fire N times).
"""
import os
from datetime import timedelta

from flask import Flask, render_template

from app.__version__ import __version__
from app import logging_setup
from app.config import get_config
from app.db import SessionLocal, init_db


def create_app():
    app = Flask(__name__)
    app.config.from_object(get_config())
    app.config['WS_VERSION'] = __version__
    app.permanent_session_lifetime = timedelta(hours=12)

    logging_setup.setup_base(app.config.get('LOG_LEVEL', 'INFO'))

    init_db(app.config)

    # Shared SSO (SHARED_AUTH.md): server-side sessions in shared.app_sessions.
    from app import auth
    if app.config.get('AUTH_DB_SCHEMA'):
        app.session_interface = auth.DBSessionInterface()
    auth.install_gate(app)

    auth.limiter.init_app(app)

    from app.routes import api, external_api, main
    app.register_blueprint(auth.bp)
    app.register_blueprint(main.bp)
    app.register_blueprint(api.bp)
    app.register_blueprint(external_api.bp)

    # --- Template helpers -------------------------------------------------
    from flask import session as flask_session

    @app.context_processor
    def _inject_globals():
        return {
            'ws_version': __version__,
            'auth_enabled': bool(app.config.get('AUTH_DB_SCHEMA')),
            'current_user': dict(flask_session) if flask_session else {},
            'is_admin': auth.is_admin() if app.config.get('AUTH_DB_SCHEMA') else True,
            'csrf_token': auth.csrf_token,
        }

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        resp.headers.setdefault('Referrer-Policy', 'same-origin')
        return resp

    @app.template_filter('localtime')
    def _localtime(dt, fmt='%Y-%m-%d %H:%M:%S'):
        """UTC storage, server-local display (like Spot)."""
        if dt is None:
            return '—'
        from datetime import timezone
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime(fmt)

    @app.errorhandler(403)
    def _forbidden(e):
        return render_template('403.html'), 403

    @app.errorhandler(404)
    def _not_found(e):
        return render_template('404.html'), 404

    @app.teardown_appcontext
    def _remove_session(exc=None):
        SessionLocal.remove()

    # --- Settings-driven logging, then background machinery ---------------
    from app.models import get_settings
    with app.app_context():
        db = SessionLocal()
        try:
            settings = get_settings(db)
            logging_setup.apply_settings(settings)
        finally:
            SessionLocal.remove()

    from app import janitor
    from app.engine import poller
    janitor.init(app)
    if os.environ.get('WS_DISABLE_POLLER', '') not in ('1', 'true', 'yes'):
        poller.start(app)

    logging_setup.log.info('WeatherSniffer v%s started', __version__)
    return app
