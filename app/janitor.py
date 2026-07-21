"""Retention janitor: purges readings and action_log rows older than
`default_retention_days` (Settings), plus expired shared-SSO session rows.

Runs hourly as an APScheduler job in the same single process.
"""
import logging
from datetime import datetime, timedelta, timezone

from app.db import SessionLocal, get_db
from app.models import ActionLog, Reading, get_settings

log = logging.getLogger('weathersniffer.db')

_app = None


def init(app):
    global _app
    _app = app


def _purge_expired_sessions():
    """Expired shared.app_sessions rows are otherwise only deleted when that
    exact sid is presented again — abandoned sessions would accumulate
    forever. Expired rows are dead in every app, so purging is safe."""
    db = get_db()
    try:
        # expires_at holds NAIVE UTC (the family writes datetime.utcnow()).
        # Compare against UTC explicitly: plain CURRENT_TIMESTAMP would make
        # Postgres interpret the naive column in the server's timezone and,
        # on a DB whose timezone is ahead of UTC, purge LIVE sessions early
        # (spontaneous logouts).
        cur = db.execute("DELETE FROM app_sessions WHERE expires_at < (now() AT TIME ZONE 'utc')")
        db.commit()
        if cur.rowcount:
            log.info('Retention purge: %d expired session row(s) removed', cur.rowcount)
    finally:
        db.close()


def run():
    with _app.app_context():
        try:
            if _app.config.get('AUTH_DB_SCHEMA'):
                try:
                    _purge_expired_sessions()
                except Exception:
                    log.exception('Expired-session purge failed')
            db = SessionLocal()
            settings = get_settings(db)
            days = settings.default_retention_days
            if not days:
                return
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            purged_readings = (db.query(Reading)
                               .filter(Reading.fetched_at < cutoff)
                               .delete(synchronize_session=False))
            purged_log = (db.query(ActionLog)
                          .filter(ActionLog.fired_at < cutoff)
                          .delete(synchronize_session=False))
            db.commit()
            if purged_readings or purged_log:
                log.info('Retention purge: %d readings, %d action_log rows older than %dd removed',
                         purged_readings, purged_log, days)
        except Exception:
            log.exception('Retention janitor failed')
        finally:
            SessionLocal.remove()
