"""Rule evaluation: threshold (edge-triggered, with hysteresis and optional
fire-on-clear), on_change (with min_change deadband), and every_tick.

State for edge detection is persisted per rule in `rules.last_state` (JSONB):
{"active": bool|null, "value_num": float|null, "value_text": str|null}.
"""
import logging
from datetime import datetime, timezone

from app.engine import actions
from app.models import Rule

log = logging.getLogger('weathersniffer.rules')


def _utcnow():
    return datetime.now(timezone.utc)


def _truthy(value_num, value_text):
    if value_num is not None:
        return value_num != 0
    return (value_text or '').strip().lower() in ('true', 'yes', 'on', '1')


def _condition(rule, value_num, value_text):
    """Evaluate the raw comparison. Returns True/False, or None when the
    value can't be compared (e.g. numeric operator on a text metric)."""
    op = rule.operator
    if op == 'is_true':
        return _truthy(value_num, value_text)
    if op == 'is_false':
        return not _truthy(value_num, value_text)
    if op in ('eq', 'ne'):
        if rule.threshold_num is not None and value_num is not None:
            result = value_num == rule.threshold_num
        elif rule.threshold_text is not None:
            result = (value_text if value_text is not None else str(value_num)) == rule.threshold_text
        else:
            return None
        return result if op == 'eq' else not result
    # gt / gte / lt / lte need numbers on both sides
    if value_num is None or rule.threshold_num is None:
        return None
    t = rule.threshold_num
    if op == 'gt':
        return value_num > t
    if op == 'gte':
        return value_num >= t
    if op == 'lt':
        return value_num < t
    if op == 'lte':
        return value_num <= t
    return None


def _threshold_active(rule, value_num, value_text, prev_active):
    """Condition with hysteresis: once active, only de-activate after the
    value leaves the threshold ± hysteresis band (numeric operators only)."""
    cond = _condition(rule, value_num, value_text)
    if cond is None:
        return None
    h = rule.hysteresis or 0
    if not h or not prev_active or value_num is None or rule.threshold_num is None:
        return cond
    t = rule.threshold_num
    op = rule.operator
    if op in ('gt', 'gte'):
        return not (value_num <= t - h)     # stay active until below the band
    if op in ('lt', 'lte'):
        return not (value_num >= t + h)     # stay active until above the band
    return cond


def _in_cooldown(rule, now):
    if not rule.cooldown_seconds or not rule.last_fired_at:
        return False
    last = rule.last_fired_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() < rule.cooldown_seconds


def evaluate_rule(db, rule, source, value_num, value_text, unit=None, observed_at=None):
    """Evaluate one rule against a fresh metric value; fire its action if due."""
    now = _utcnow()
    prev = dict(rule.last_state or {})
    should_fire = False

    if rule.trigger_mode == 'every_tick':
        should_fire = True                       # continuous streaming; no cooldown

    elif rule.trigger_mode == 'threshold':
        prev_active = prev.get('active')
        active = _threshold_active(rule, value_num, value_text, bool(prev_active))
        if active is None:
            return                               # not comparable this tick; keep state
        if active and not prev_active:
            should_fire = True                   # false→true edge (or first-ever eval)
        elif not active and prev_active and rule.fire_on_clear:
            should_fire = True                   # true→false edge
        prev['active'] = active
        if should_fire and _in_cooldown(rule, now):
            should_fire = False

    elif rule.trigger_mode == 'on_change':
        had_prev = 'value_num' in prev or 'value_text' in prev
        changed = False
        if had_prev:
            if value_num is not None and prev.get('value_num') is not None:
                delta = abs(value_num - prev['value_num'])
                changed = delta >= rule.min_change if rule.min_change else delta > 0
            else:
                changed = (value_text != prev.get('value_text')
                           or value_num != prev.get('value_num'))
        should_fire = had_prev and changed and not _in_cooldown(rule, now)
        # Don't move the baseline for sub-min_change drift, or tiny moves
        # accumulate invisibly and a real crossing never fires.
        if not had_prev or changed:
            prev['value_num'] = value_num
            prev['value_text'] = value_text

    if rule.trigger_mode != 'on_change':
        prev['value_num'] = value_num
        prev['value_text'] = value_text
    rule.last_state = prev

    if should_fire:
        actions.fire(db, rule, source, value_num, value_text,
                     unit=unit, observed_at=observed_at, via='engine')
        rule.last_fired_at = now


def evaluate_master_rules(db, polled_source):
    """Evaluate rules bound to the aggregated Current-weather readout
    (source_id IS NULL, metric_key like 'weather.wgbt').

    Only the source currently *winning* a canonical field drives its rules:
    the rule follows the freshest healthy feed's cadence and fails over
    automatically when that feed dies, and an every_tick rule doesn't fire
    once per source per interval.
    """
    from app.weather import build_master_readout

    master_rules = (db.query(Rule)
                    .filter(Rule.enabled.is_(True), Rule.source_id.is_(None))
                    .all())
    if not master_rules:
        return
    by_field = {f'weather.{f["field"]}': f for f in build_master_readout(db)}
    for rule in master_rules:
        entry = by_field.get(rule.metric_key)
        if entry is None or entry['source_slug'] != polled_source.slug:
            continue
        try:
            evaluate_rule(db, rule, polled_source,
                          entry['value_num'], entry['value_text'],
                          unit=entry['unit'], observed_at=entry['observed_at'])
        except Exception:
            log.exception('Master rule evaluation failed rule=%s', rule.name)


def evaluate_source_rules(db, source, metric_rows):
    """Run every enabled rule bound to this source against its fresh metrics."""
    by_key = {m.metric_key: m for m in metric_rows}
    rules = (db.query(Rule)
             .filter(Rule.enabled.is_(True), Rule.source_id == source.id)
             .all())
    for rule in rules:
        metric = by_key.get(rule.metric_key)
        if metric is None:
            continue
        try:
            evaluate_rule(db, rule, source, metric.value_num, metric.value_text,
                          unit=metric.unit, observed_at=metric.observed_at)
        except Exception:
            log.exception('Rule evaluation failed rule=%s', rule.name)
