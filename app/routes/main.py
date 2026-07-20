"""HTML pages: dashboard, sources, rules, action log, settings."""
import csv
import io
import json
import logging
import re

from flask import (Blueprint, Response, abort, flash, redirect,
                   render_template, request, session, url_for)

from app.auth import admin_required
from app.db import SessionLocal
from app.engine import poller
from app.logging_setup import apply_settings
from app.models import (ACTION_TYPES, OPERATORS, SOURCE_TYPES, TRIGGER_MODES,
                        ActionLog, MetricCurrent, Rule, Source, get_settings)

log = logging.getLogger('weathersniffer.web')

bp = Blueprint('main', __name__)


def _actor():
    return session.get('username', 'anonymous')


def _slugify(name):
    slug = re.sub(r'[^a-z0-9]+', '_', (name or '').lower()).strip('_')
    return slug or 'source'


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@bp.route('/')
def index():
    db = SessionLocal()
    sources = db.query(Source).order_by(Source.name).all()
    metrics = {}
    for m in db.query(MetricCurrent).order_by(MetricCurrent.metric_key):
        metrics.setdefault(m.source_id, []).append(m)
    recent_fires = (db.query(ActionLog)
                    .order_by(ActionLog.fired_at.desc())
                    .limit(15).all())
    return render_template('dashboard.html', sources=sources, metrics=metrics,
                           recent_fires=recent_fires)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

@bp.route('/sources')
def sources():
    db = SessionLocal()
    rows = db.query(Source).order_by(Source.name).all()
    return render_template('sources.html', sources=rows)


def _source_from_form(db, source=None):
    form = request.form
    name = form.get('name', '').strip()
    if not name:
        raise ValueError('Name is required.')
    source_type = form.get('source_type', '')
    if source_type not in SOURCE_TYPES:
        raise ValueError('Invalid source type.')
    slug = re.sub(r'[^a-zA-Z0-9_]+', '_', form.get('slug', '').strip()) or _slugify(name)
    guid = form.get('guid', '').strip() or None
    url = form.get('url', '').strip() or None
    if source_type == 'custom_url':
        if not url:
            raise ValueError('custom_url sources need a URL.')
    elif not guid:
        raise ValueError('This source type needs a Perry GUID.')
    settings = get_settings(db)
    try:
        interval = int(form.get('poll_interval_seconds')
                       or settings.default_poll_interval_seconds)
    except ValueError:
        raise ValueError('Poll interval must be a number of seconds.')
    if interval < 5:
        raise ValueError('Poll interval must be at least 5 seconds.')

    if source is None:
        source = Source()
        db.add(source)
    source.name = name
    source.slug = slug
    source.source_type = source_type
    source.guid = guid
    source.url = url
    source.poll_interval_seconds = interval
    source.enabled = form.get('enabled') == 'on'
    return source


@bp.route('/sources/new', methods=['GET', 'POST'])
@admin_required
def source_new():
    db = SessionLocal()
    if request.method == 'POST':
        try:
            source = _source_from_form(db)
            db.commit()
        except ValueError as exc:
            db.rollback()
            flash(str(exc), 'error')
            return render_template('source_form.html', source=None,
                                   source_types=SOURCE_TYPES, form=request.form)
        poller.reschedule_source(source)
        log.info('Source created source=%s actor=%s via=web', source.slug, _actor())
        flash(f'Source “{source.name}” created.', 'success')
        return redirect(url_for('main.sources'))
    return render_template('source_form.html', source=None,
                           source_types=SOURCE_TYPES, form=None)


@bp.route('/sources/<int:source_id>/edit', methods=['GET', 'POST'])
@admin_required
def source_edit(source_id):
    db = SessionLocal()
    source = db.get(Source, source_id) or abort(404)
    if request.method == 'POST':
        try:
            _source_from_form(db, source)
            db.commit()
        except ValueError as exc:
            db.rollback()
            flash(str(exc), 'error')
            return render_template('source_form.html', source=source,
                                   source_types=SOURCE_TYPES, form=request.form)
        poller.reschedule_source(source)
        log.info('Source updated source=%s actor=%s via=web', source.slug, _actor())
        flash(f'Source “{source.name}” saved.', 'success')
        return redirect(url_for('main.sources'))
    return render_template('source_form.html', source=source,
                           source_types=SOURCE_TYPES, form=None)


@bp.route('/sources/<int:source_id>/toggle', methods=['POST'])
@admin_required
def source_toggle(source_id):
    db = SessionLocal()
    source = db.get(Source, source_id) or abort(404)
    source.enabled = not source.enabled
    db.commit()
    poller.reschedule_source(source)
    log.info('Source %s source=%s actor=%s via=web',
             'enabled' if source.enabled else 'disabled', source.slug, _actor())
    return redirect(url_for('main.sources'))


@bp.route('/sources/<int:source_id>/delete', methods=['POST'])
@admin_required
def source_delete(source_id):
    db = SessionLocal()
    source = db.get(Source, source_id) or abort(404)
    slug = source.slug
    poller.remove_source(source.id)
    db.delete(source)
    db.commit()
    log.info('Source deleted source=%s actor=%s via=web', slug, _actor())
    flash(f'Source “{slug}” deleted (its metrics, history and rules went with it).', 'success')
    return redirect(url_for('main.sources'))


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

@bp.route('/rules')
def rules():
    db = SessionLocal()
    rows = db.query(Rule).order_by(Rule.name).all()
    sources_by_id = {s.id: s for s in db.query(Source)}
    return render_template('rules.html', rules=rows, sources_by_id=sources_by_id)


def _action_config_from_form(action_type):
    form = request.form
    cfg = {}
    if action_type == 'webhook_http':
        cfg['method'] = (form.get('cfg_method') or 'POST').upper()
        cfg['url'] = form.get('cfg_url', '').strip()
        if not cfg['url']:
            raise ValueError('Webhook actions need a URL.')
        headers_raw = form.get('cfg_headers', '').strip()
        if headers_raw:
            try:
                headers = json.loads(headers_raw)
                if not isinstance(headers, dict):
                    raise ValueError
                cfg['headers'] = headers
            except ValueError:
                raise ValueError('Headers must be a JSON object, e.g. {"X-Token": "abc"}.')
        cfg['body_template'] = form.get('cfg_body_template', '').strip() or '{value}'
    elif action_type in ('tcp', 'udp'):
        cfg['host'] = form.get('cfg_host', '').strip()
        try:
            cfg['port'] = int(form.get('cfg_port', ''))
        except ValueError:
            raise ValueError('TCP/UDP actions need a numeric port.')
        if not cfg['host']:
            raise ValueError('TCP/UDP actions need a host.')
        cfg['payload_template'] = form.get('cfg_payload_template', '').strip() or '{value}'
        if action_type == 'tcp':
            cfg['append_newline'] = form.get('cfg_append_newline') == 'on'
    elif action_type in ('spot_reading', 'spot_event'):
        cfg['base_url'] = form.get('cfg_base_url', '').strip()
        cfg['token'] = form.get('cfg_token', '').strip()
        if not cfg['token']:
            raise ValueError('Spot actions need a monitor token.')
        if action_type == 'spot_reading':
            cfg['value_metric_key'] = form.get('cfg_value_metric_key', '').strip() or None
            cfg['label_template'] = form.get('cfg_label_template', '').strip() or None
        else:
            cfg['label_template'] = (form.get('cfg_label_template', '').strip()
                                     or '{metric_key} = {value}')
    return cfg


def _float_or_none(name):
    raw = request.form.get(name, '').strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f'{name.replace("_", " ")} must be a number.')


def _rule_from_form(db, rule=None):
    form = request.form
    name = form.get('name', '').strip()
    if not name:
        raise ValueError('Name is required.')
    try:
        source_id = int(form.get('source_id', ''))
    except ValueError:
        raise ValueError('Pick a source.')
    if not db.get(Source, source_id):
        raise ValueError('Pick a source.')
    metric_key = form.get('metric_key', '').strip()
    if not metric_key:
        raise ValueError('Pick a metric.')
    trigger_mode = form.get('trigger_mode', '')
    if trigger_mode not in TRIGGER_MODES:
        raise ValueError('Invalid trigger mode.')
    operator = form.get('operator') or None
    if trigger_mode == 'threshold':
        if operator not in OPERATORS:
            raise ValueError('Threshold rules need an operator.')
    else:
        operator = None
    action_type = form.get('action_type', '')
    if action_type not in ACTION_TYPES:
        raise ValueError('Invalid action type.')

    threshold_num = _float_or_none('threshold_num')
    threshold_text = form.get('threshold_text', '').strip() or None
    if trigger_mode == 'threshold' and operator in ('gt', 'gte', 'lt', 'lte') \
            and threshold_num is None:
        raise ValueError('Numeric operators need a numeric threshold.')
    if trigger_mode == 'threshold' and operator in ('eq', 'ne') \
            and threshold_num is None and threshold_text is None:
        raise ValueError('eq/ne needs a numeric or text threshold.')

    cooldown = int(form.get('cooldown_seconds') or 0)
    if trigger_mode == 'every_tick':
        cooldown = 0                     # continuous streaming — no suppression

    if rule is None:
        rule = Rule()
        db.add(rule)
    rule.name = name
    rule.enabled = form.get('enabled') == 'on'
    rule.source_id = source_id
    rule.metric_key = metric_key
    rule.trigger_mode = trigger_mode
    rule.operator = operator
    rule.threshold_num = threshold_num
    rule.threshold_text = threshold_text
    rule.hysteresis = _float_or_none('hysteresis')
    rule.fire_on_clear = form.get('fire_on_clear') == 'on'
    rule.min_change = _float_or_none('min_change')
    rule.cooldown_seconds = max(0, cooldown)
    rule.action_type = action_type
    rule.action_config = _action_config_from_form(action_type)
    rule.last_state = None               # restart edge detection cleanly
    return rule


@bp.route('/rules/new', methods=['GET', 'POST'])
def rule_new():
    db = SessionLocal()
    sources_list = db.query(Source).order_by(Source.name).all()
    if request.method == 'POST':
        try:
            rule = _rule_from_form(db)
            db.commit()
        except ValueError as exc:
            db.rollback()
            flash(str(exc), 'error')
            return render_template('rule_form.html', rule=None, sources=sources_list,
                                   operators=OPERATORS, form=request.form)
        log.info('Rule created rule=%s actor=%s via=web', rule.name, _actor())
        flash(f'Rule “{rule.name}” created.', 'success')
        return redirect(url_for('main.rules'))
    return render_template('rule_form.html', rule=None, sources=sources_list,
                           operators=OPERATORS, form=None)


@bp.route('/rules/<int:rule_id>/edit', methods=['GET', 'POST'])
def rule_edit(rule_id):
    db = SessionLocal()
    rule = db.get(Rule, rule_id) or abort(404)
    sources_list = db.query(Source).order_by(Source.name).all()
    if request.method == 'POST':
        try:
            _rule_from_form(db, rule)
            db.commit()
        except ValueError as exc:
            db.rollback()
            flash(str(exc), 'error')
            return render_template('rule_form.html', rule=rule, sources=sources_list,
                                   operators=OPERATORS, form=request.form)
        log.info('Rule updated rule=%s actor=%s via=web', rule.name, _actor())
        flash(f'Rule “{rule.name}” saved.', 'success')
        return redirect(url_for('main.rules'))
    return render_template('rule_form.html', rule=rule, sources=sources_list,
                           operators=OPERATORS, form=None)


@bp.route('/rules/<int:rule_id>/toggle', methods=['POST'])
def rule_toggle(rule_id):
    db = SessionLocal()
    rule = db.get(Rule, rule_id) or abort(404)
    rule.enabled = not rule.enabled
    if rule.enabled:
        rule.last_state = None
    db.commit()
    log.info('Rule %s rule=%s actor=%s via=web',
             'enabled' if rule.enabled else 'disabled', rule.name, _actor())
    return redirect(url_for('main.rules'))


@bp.route('/rules/<int:rule_id>/delete', methods=['POST'])
def rule_delete(rule_id):
    db = SessionLocal()
    rule = db.get(Rule, rule_id) or abort(404)
    name = rule.name
    db.delete(rule)
    db.commit()
    log.info('Rule deleted rule=%s actor=%s via=web', name, _actor())
    flash(f'Rule “{name}” deleted.', 'success')
    return redirect(url_for('main.rules'))


# ---------------------------------------------------------------------------
# Action log
# ---------------------------------------------------------------------------

def _filtered_log_query(db):
    q = db.query(ActionLog)
    rule_filter = request.args.get('rule', '').strip()
    if rule_filter:
        q = q.filter(ActionLog.rule_name.ilike(f'%{rule_filter}%'))
    outcome = request.args.get('outcome', '').strip()
    if outcome in ('success', 'failure'):
        q = q.filter(ActionLog.outcome == outcome)
    return q.order_by(ActionLog.fired_at.desc())


@bp.route('/log')
def action_log():
    db = SessionLocal()
    entries = _filtered_log_query(db).limit(200).all()
    return render_template('log.html', entries=entries,
                           rule_filter=request.args.get('rule', ''),
                           outcome_filter=request.args.get('outcome', ''))


@bp.route('/log/export.csv')
def action_log_csv():
    db = SessionLocal()
    entries = _filtered_log_query(db).limit(10000).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['fired_at', 'rule', 'metric_key', 'value', 'action_type',
                     'target', 'outcome', 'status_code', 'latency_ms', 'error'])
    for e in entries:
        value = e.trigger_value_num if e.trigger_value_num is not None else e.trigger_value_text
        writer.writerow([e.fired_at.isoformat() if e.fired_at else '', e.rule_name,
                         e.metric_key, value, e.action_type, e.target or '',
                         e.outcome, e.status_code or '', e.latency_ms or '', e.error or ''])
    log.info('Action log exported rows=%d actor=%s via=web', len(entries), _actor())
    return Response(buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=weathersniffer_action_log.csv'})


# ---------------------------------------------------------------------------
# Settings (admin-only)
# ---------------------------------------------------------------------------

@bp.route('/settings', methods=['GET', 'POST'])
@admin_required
def settings():
    db = SessionLocal()
    row = get_settings(db)
    if request.method == 'POST':
        form = request.form
        try:
            row.syslog_local_enabled = form.get('syslog_local_enabled') == 'on'
            row.syslog_local_address = form.get('syslog_local_address', '').strip() or '/dev/log'
            row.syslog_remote_enabled = form.get('syslog_remote_enabled') == 'on'
            row.syslog_remote_host = form.get('syslog_remote_host', '').strip() or None
            row.syslog_remote_port = int(form.get('syslog_remote_port') or 514)
            row.syslog_facility = form.get('syslog_facility', 'local0').strip() or 'local0'
            row.default_poll_interval_seconds = max(5, int(form.get('default_poll_interval_seconds') or 60))
            retention = form.get('default_retention_days', '').strip()
            row.default_retention_days = int(retention) if retention else None
            row.spot_default_base_url = form.get('spot_default_base_url', '').strip() or None
            row.log_level = form.get('log_level', 'INFO').upper()
        except ValueError:
            db.rollback()
            flash('Numeric fields must contain numbers.', 'error')
            return render_template('settings.html', settings=row)
        db.commit()
        apply_settings(row)
        log.info('Settings updated actor=%s via=web', _actor())
        flash('Settings saved and applied.', 'success')
        return redirect(url_for('main.settings'))
    return render_template('settings.html', settings=row)
