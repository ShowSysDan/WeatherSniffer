"""External read API (/api/v1) for sibling apps, guarded by an optional
API_KEY (X-API-Key header or ?api_key=), matching Leash. Leave API_KEY unset
for open access on a trusted LAN.
"""
import logging

from flask import Blueprint, current_app, jsonify, request

from app.db import SessionLocal
from app.models import MetricCurrent, Source
from app.weather import build_master_readout

log = logging.getLogger('weathersniffer.api')

bp = Blueprint('external_api', __name__, url_prefix='/api/v1')


@bp.before_request
def _check_api_key():
    required = current_app.config.get('API_KEY', '')
    if not required:
        return
    supplied = request.headers.get('X-API-Key') or request.args.get('api_key', '')
    if supplied != required:
        log.warning('Rejected /api/v1 request (bad API key) from %s', request.remote_addr)
        return jsonify({'error': 'invalid or missing API key'}), 401


def _metric_json(m, source):
    return {
        'metric_key': m.metric_key,
        'source': source.name if source else None,
        'source_slug': source.slug if source else None,
        'value_num': m.value_num,
        'value_text': m.value_text,
        'unit': m.unit,
        'observed_at': m.observed_at.isoformat() if m.observed_at else None,
        'updated_at': m.updated_at.isoformat() if m.updated_at else None,
    }


@bp.route('/metrics')
def metrics():
    db = SessionLocal()
    sources = {s.id: s for s in db.query(Source)}
    rows = db.query(MetricCurrent).order_by(MetricCurrent.metric_key).all()
    return jsonify({'metrics': [_metric_json(m, sources.get(m.source_id)) for m in rows]})


@bp.route('/metrics/<path:metric_key>')
def metric(metric_key):
    db = SessionLocal()
    m = db.query(MetricCurrent).filter_by(metric_key=metric_key).first()
    if m is None:
        return jsonify({'error': f'no such metric: {metric_key}'}), 404
    source = db.get(Source, m.source_id)
    return jsonify(_metric_json(m, source))


@bp.route('/weather')
def weather():
    """The master readout: one canonical value per weather field, deduped
    across sources (freshest healthy value wins, synonyms resolved)."""
    db = SessionLocal()
    readout = build_master_readout(db)
    for f in readout:
        f['observed_at'] = f['observed_at'].isoformat() if f['observed_at'] else None
    return jsonify({'weather': readout})


@bp.route('/sources')
def sources():
    db = SessionLocal()
    rows = db.query(Source).order_by(Source.name).all()
    return jsonify({'sources': [{
        'id': s.id,
        'name': s.name,
        'slug': s.slug,
        'source_type': s.source_type,
        'enabled': s.enabled,
        'poll_interval_seconds': s.poll_interval_seconds,
        'last_polled_at': s.last_polled_at.isoformat() if s.last_polled_at else None,
        'last_status': s.last_status,
        'last_error': s.last_error,
    } for s in rows]})
