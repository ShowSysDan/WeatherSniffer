"""Action dispatch: webhook_http / tcp / udp / spot_reading / spot_event.

Every fire — engine-triggered or Test fire — writes an action_log row and a
syslog line, success or failure.
"""
import json
import logging
import re
import socket
import time
from datetime import datetime, timezone

import requests

from app.models import ActionLog, MetricCurrent, get_settings

log = logging.getLogger('weathersniffer.actions')

HTTP_TIMEOUT = 5
SOCKET_TIMEOUT = 5


def redact_token(token):
    if not token:
        return ''
    return '…' + token[-4:] if len(token) > 4 else '…'


_PLACEHOLDER_RE = re.compile(
    r'\{(value|metric_key|source_name|unit|observed_at|now)(:[^{}]*)?\}')


def render_template_str(template, ctx):
    """str.format-style templating: {value}, {value:.1f}, {metric_key},
    {source_name}, {unit}, {observed_at}, {now}. Blank template -> {value}.

    Only the known placeholders are substituted; all other braces stay
    literal, so JSON bodies like {"wbgt": {value}} work without escaping.
    """
    template = template or '{value}'

    def _sub(match):
        key, spec = match.group(1), match.group(2) or ''
        try:
            return ('{0' + spec + '}').format(ctx.get(key, ''))
        except (ValueError, TypeError):
            # e.g. a numeric format spec on a text value — degrade gracefully
            return str(ctx.get(key, ''))

    return _PLACEHOLDER_RE.sub(_sub, template)


def build_context(rule, source, value_num, value_text, unit, observed_at):
    value = value_num if value_num is not None else (value_text or '')
    return {
        'value': value,
        'metric_key': rule.metric_key,
        'source_name': source.name if source else '',
        'unit': unit or '',
        'observed_at': observed_at.isoformat() if observed_at else '',
        'now': datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Dispatchers — each returns (target, status_code, error)
# ---------------------------------------------------------------------------

def _do_webhook(cfg, ctx):
    url = (cfg.get('url') or '').strip()
    if not url:
        raise ValueError('webhook_http action has no url')
    method = (cfg.get('method') or 'POST').upper()
    headers = dict(cfg.get('headers') or {})
    body = render_template_str(cfg.get('body_template'), ctx)
    if 'Content-Type' not in headers:
        try:
            json.loads(body)
            headers['Content-Type'] = 'application/json'
        except (json.JSONDecodeError, ValueError):
            headers['Content-Type'] = 'text/plain'
    resp = requests.request(method, url, data=body.encode('utf-8'),
                            headers=headers, timeout=HTTP_TIMEOUT)
    error = None if resp.ok else f'HTTP {resp.status_code}'
    return url, resp.status_code, error


def _do_tcp(cfg, ctx):
    host, port = cfg.get('host'), int(cfg.get('port') or 0)
    if not host or not port:
        raise ValueError('tcp action needs host and port')
    payload = render_template_str(cfg.get('payload_template'), ctx)
    if cfg.get('append_newline', True):
        payload += '\n'
    with socket.create_connection((host, port), timeout=SOCKET_TIMEOUT) as sock:
        sock.sendall(payload.encode('utf-8'))
    return f'{host}:{port}', None, None


def _do_udp(cfg, ctx):
    host, port = cfg.get('host'), int(cfg.get('port') or 0)
    if not host or not port:
        raise ValueError('udp action needs host and port')
    payload = render_template_str(cfg.get('payload_template'), ctx)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(payload.encode('utf-8'), (host, port))
    finally:
        sock.close()
    return f'{host}:{port}', None, None


def _spot_base(cfg, db):
    base = (cfg.get('base_url') or '').strip()
    if not base:
        settings = get_settings(db)
        base = (settings.spot_default_base_url or '').strip()
    if not base:
        raise ValueError('no Spot base_url set (action config or Settings default)')
    return base.rstrip('/')


def _do_spot_reading(cfg, ctx, db, rule, value_num, observed_at):
    base = _spot_base(cfg, db)
    token = (cfg.get('token') or '').strip()
    if not token:
        raise ValueError('spot_reading action needs a token')

    value = value_num
    other_key = (cfg.get('value_metric_key') or '').strip()
    if other_key:
        row = db.query(MetricCurrent).filter_by(metric_key=other_key).first()
        value = row.value_num if row else None
    if value is None:
        raise ValueError('no numeric value available to send to Spot')

    payload = {'value': value}
    if cfg.get('label_template'):
        payload['label'] = render_template_str(cfg['label_template'], ctx)
    if observed_at:
        payload['ts'] = observed_at.isoformat()

    url = f'{base}/api/ingest/{token}'
    resp = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
    target = f'{base}/api/ingest/{redact_token(token)}'
    error = None if resp.ok else f'HTTP {resp.status_code}'
    return target, resp.status_code, error


def _do_spot_event(cfg, ctx, db):
    base = _spot_base(cfg, db)
    token = (cfg.get('token') or '').strip()
    if not token:
        raise ValueError('spot_event action needs a token')
    label = render_template_str(cfg.get('label_template'), ctx)
    url = f'{base}/api/event/{token}'
    resp = requests.post(url, data=label.encode('utf-8'),
                         headers={'Content-Type': 'text/plain'}, timeout=HTTP_TIMEOUT)
    target = f'{base}/api/event/{redact_token(token)}'
    error = None if resp.ok else f'HTTP {resp.status_code}'
    return target, resp.status_code, error


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def fire(db, rule, source, value_num, value_text, unit=None, observed_at=None, via='engine'):
    """Dispatch a rule's action, log the outcome (action_log + syslog).
    Returns the ActionLog row (not yet committed)."""
    cfg = rule.action_config or {}
    ctx = build_context(rule, source, value_num, value_text, unit, observed_at)

    target, status_code, error = None, None, None
    started = time.monotonic()
    try:
        if rule.action_type == 'webhook_http':
            target, status_code, error = _do_webhook(cfg, ctx)
        elif rule.action_type == 'tcp':
            target, status_code, error = _do_tcp(cfg, ctx)
        elif rule.action_type == 'udp':
            target, status_code, error = _do_udp(cfg, ctx)
        elif rule.action_type == 'spot_reading':
            target, status_code, error = _do_spot_reading(cfg, ctx, db, rule, value_num, observed_at)
        elif rule.action_type == 'spot_event':
            target, status_code, error = _do_spot_event(cfg, ctx, db)
        else:
            error = f'unknown action_type {rule.action_type!r}'
    except Exception as exc:
        error = str(exc)[:500]
    latency_ms = int((time.monotonic() - started) * 1000)

    outcome = 'failure' if error else 'success'
    entry = ActionLog(
        rule_id=rule.id,
        rule_name=rule.name,
        fired_at=datetime.now(timezone.utc),
        metric_key=rule.metric_key,
        trigger_value_num=value_num,
        trigger_value_text=value_text,
        action_type=rule.action_type,
        target=target,
        outcome=outcome,
        status_code=status_code,
        latency_ms=latency_ms,
        error=error,
    )
    db.add(entry)

    value_repr = value_num if value_num is not None else value_text
    if outcome == 'success':
        log.info('Rule fired rule=%s via=%s metric=%s value=%s action=%s target=%s outcome=success',
                 rule.name, via, rule.metric_key, value_repr, rule.action_type, target)
    else:
        log.warning('Rule fired rule=%s via=%s metric=%s value=%s action=%s target=%s outcome=failure error=%s',
                    rule.name, via, rule.metric_key, value_repr, rule.action_type, target, error)
    return entry
