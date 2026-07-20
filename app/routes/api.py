"""Internal JSON API for the UI (session-gated by the auth before_request):
dashboard refresh, test fetch, metric-key discovery, test fire.
"""
import logging

from flask import Blueprint, abort, jsonify, request, session

from app.__version__ import __version__
from app.db import SessionLocal
from app.engine import actions, poller
from app.models import ActionLog, MetricCurrent, Rule, Source
from app.perry import client, normalize

log = logging.getLogger('weathersniffer.api')

bp = Blueprint('api', __name__, url_prefix='/api')


def _metric_json(m, source=None):
    return {
        'source_id': m.source_id,
        'source': source.name if source else None,
        'metric_key': m.metric_key,
        'value_num': m.value_num,
        'value_text': m.value_text,
        'unit': m.unit,
        'observed_at': m.observed_at.isoformat() if m.observed_at else None,
        'updated_at': m.updated_at.isoformat() if m.updated_at else None,
    }


@bp.route('/version')
def version():
    return jsonify({'version': __version__})


@bp.route('/dashboard')
def dashboard():
    """Everything the dashboard needs for its auto-refresh."""
    db = SessionLocal()
    sources = db.query(Source).order_by(Source.name).all()
    metrics = db.query(MetricCurrent).order_by(MetricCurrent.metric_key).all()
    fires = db.query(ActionLog).order_by(ActionLog.fired_at.desc()).limit(15).all()
    return jsonify({
        'sources': [{
            'id': s.id, 'name': s.name, 'slug': s.slug, 'type': s.source_type,
            'enabled': s.enabled,
            'last_polled_at': s.last_polled_at.isoformat() if s.last_polled_at else None,
            'last_status': s.last_status, 'last_error': s.last_error,
        } for s in sources],
        'metrics': [_metric_json(m) for m in metrics],
        'recent_fires': [{
            'fired_at': f.fired_at.isoformat() if f.fired_at else None,
            'rule_name': f.rule_name, 'metric_key': f.metric_key,
            'value': f.trigger_value_num if f.trigger_value_num is not None else f.trigger_value_text,
            'action_type': f.action_type, 'target': f.target,
            'outcome': f.outcome, 'error': f.error,
        } for f in fires],
    })


@bp.route('/sources/test_fetch', methods=['POST'])
def source_test_fetch():
    """Test fetch for the source form (unsaved values) OR a saved source.

    Body: {"id": 1} or {"source_type": ..., "guid": ..., "url": ..., "slug": ...}.
    A saved source gets a full poll (stores metrics + evaluates rules) so the
    metric dropdown populates; an unsaved probe only previews.
    """
    body = request.get_json(silent=True) or {}
    db = SessionLocal()

    if body.get('id'):
        source = db.get(Source, int(body['id'])) or abort(404)
        touched, raw = poller.poll_once(db, source)
        if source.last_status == 'error':
            return jsonify({'ok': False, 'error': source.last_error}), 502
        return jsonify({'ok': True, 'stored': True, 'raw': (raw or '')[:20000],
                        'metric_keys': sorted(m.metric_key for m in touched)})

    source_type = body.get('source_type', '')
    slug = body.get('slug') or 'test'
    try:
        payload, raw = client.fetch(source_type, guid=body.get('guid'), url=body.get('url'))
        metrics, _extras = normalize.normalize(source_type, slug, payload)
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)[:500]}), 502
    return jsonify({'ok': True, 'stored': False, 'raw': raw[:20000],
                    'metric_keys': sorted(m['metric_key'] for m in metrics)})


@bp.route('/sources/<int:source_id>/metric_keys')
def source_metric_keys(source_id):
    """Discovered metric keys for the rule editor's dropdown."""
    db = SessionLocal()
    rows = (db.query(MetricCurrent)
            .filter_by(source_id=source_id)
            .order_by(MetricCurrent.metric_key).all())
    return jsonify({'metric_keys': [m.metric_key for m in rows]})


@bp.route('/rules/<int:rule_id>/test_fire', methods=['POST'])
def rule_test_fire(rule_id):
    """Dispatch the rule's action once, right now, using the metric's current
    value. Logged to the action log like any real fire."""
    db = SessionLocal()
    rule = db.get(Rule, rule_id) or abort(404)
    source = db.get(Source, rule.source_id)
    metric = (db.query(MetricCurrent)
              .filter_by(source_id=rule.source_id, metric_key=rule.metric_key)
              .first())
    value_num = metric.value_num if metric else None
    value_text = metric.value_text if metric else None
    unit = metric.unit if metric else None
    observed_at = metric.observed_at if metric else None
    if metric is None:
        log.info('Test fire with no current value for metric=%s (sending nulls)', rule.metric_key)

    entry = actions.fire(db, rule, source, value_num, value_text,
                         unit=unit, observed_at=observed_at, via='test')
    db.commit()
    log.info('Test fire rule=%s actor=%s via=web outcome=%s',
             rule.name, session.get('username', 'anonymous'), entry.outcome)
    return jsonify({
        'ok': entry.outcome == 'success',
        'outcome': entry.outcome,
        'target': entry.target,
        'status_code': entry.status_code,
        'latency_ms': entry.latency_ms,
        'error': entry.error,
    })
