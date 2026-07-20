"""Retention janitor: purges readings and action_log rows older than
`default_retention_days` (Settings). Blank retention = keep forever.

Runs hourly as an APScheduler job in the same single process.
"""
import logging
from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import ActionLog, Reading, get_settings

log = logging.getLogger('weathersniffer.db')

_app = None


def init(app):
    global _app
    _app = app


def run():
    with _app.app_context():
        try:
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
