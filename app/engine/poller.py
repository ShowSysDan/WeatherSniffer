"""APScheduler-driven poller: one interval job per enabled source.

Runs inside the single gunicorn worker (the whole app is one process on
purpose — see README). Jobs are (re)scheduled whenever a source is added,
edited, enabled, disabled, or deleted.
"""
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from app.db import SessionLocal
from app.engine import rules as rules_engine
from app.models import MetricCurrent, Reading, Source
from app.perry import client, normalize

log = logging.getLogger('weathersniffer.poller')

scheduler = BackgroundScheduler(
    timezone='UTC',
    job_defaults={'coalesce': True, 'max_instances': 1, 'misfire_grace_time': 30},
)

_app = None


def _job_id(source_id):
    return f'source-{source_id}'


def start(app):
    """Start the scheduler and schedule every enabled source + the janitor."""
    global _app
    _app = app
    if not scheduler.running:
        scheduler.start()
    with app.app_context():
        db = SessionLocal()
        try:
            count = 0
            for source in db.query(Source).filter(Source.enabled.is_(True)):
                _schedule(source)
                count += 1
        finally:
            SessionLocal.remove()
    from app import janitor
    scheduler.add_job(janitor.run, 'interval', hours=1, id='janitor', replace_existing=True)
    log.info('Poller started: %d source job(s) scheduled', count)


def shutdown():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        log.info('Poller stopped')


def _schedule(source):
    interval = max(5, int(source.poll_interval_seconds or 60))
    scheduler.add_job(
        poll_source, 'interval',
        seconds=interval,
        args=[source.id],
        id=_job_id(source.id),
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc),   # poll immediately, then on interval
    )


def reschedule_source(source):
    """Call after a source is created/edited/toggled: sync its job."""
    if source.enabled:
        _schedule(source)
        log.info('Scheduled source %s every %ss', source.slug, source.poll_interval_seconds)
    else:
        remove_source(source.id)


def remove_source(source_id):
    try:
        scheduler.remove_job(_job_id(source_id))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# The poll job
# ---------------------------------------------------------------------------

def poll_source(source_id):
    with _app.app_context():
        try:
            db = SessionLocal()
            source = db.get(Source, source_id)
            if source is None:
                remove_source(source_id)
                return
            if not source.enabled:
                return
            poll_once(db, source)
        except Exception:
            log.exception('Poll job crashed for source id=%s', source_id)
        finally:
            SessionLocal.remove()


def poll_once(db, source):
    """Fetch → normalize → upsert current → append history → evaluate rules.
    Returns (metric_rows, raw_text). Never raises past the status update."""
    now = datetime.now(timezone.utc)
    try:
        payload, raw = client.fetch(source.source_type, guid=source.guid, url=source.url)
        metrics, extras = normalize.normalize(source.source_type, source.slug, payload)
    except Exception as exc:
        source.last_polled_at = now
        source.last_status = 'error'
        source.last_error = str(exc)[:500]
        db.commit()
        log.warning('Fetch failed source=%s type=%s error=%s',
                    source.slug, source.source_type, exc)
        return [], None

    current = {m.metric_key: m
               for m in db.query(MetricCurrent).filter_by(source_id=source.id)}
    touched = []
    for m in metrics:
        row = current.get(m['metric_key'])
        if row is None:
            row = MetricCurrent(source_id=source.id, metric_key=m['metric_key'])
            db.add(row)
        row.value_num = m['value_num']
        row.value_text = m['value_text']
        row.unit = m['unit']
        row.observed_at = m['observed_at']
        row.updated_at = now
        touched.append(row)
        db.add(Reading(
            source_id=source.id,
            metric_key=m['metric_key'],
            value_num=m['value_num'],
            value_text=m['value_text'],
            observed_at=m['observed_at'],
            fetched_at=now,
        ))

    if extras.get('location_info'):
        source.location_info = extras['location_info']
    source.last_polled_at = now
    source.last_status = 'ok'
    source.last_error = None
    db.commit()

    rules_engine.evaluate_source_rules(db, source, touched)
    db.commit()
    return touched, raw
