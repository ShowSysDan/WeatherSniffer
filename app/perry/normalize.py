"""Normalize Perry Weather responses into flat metrics.

A generic recursive flattener does most of the work; small per-type hints
handle the shapes that benefit from nicer keys (AQI pollutants, the bare-number
lightning delay, the double-nested v2 observations envelope).

Every metric is a dict: {metric_key, value_num, value_text, unit, observed_at}.
Keys are prefixed with the source slug by the caller-facing `normalize()`.

Timestamps: `observationTime` / `lastUpdated` values without a timezone
(e.g. 2026-07-20T16:21:25.703) are assumed to be UTC. Everything is stored UTC.
"""
import re
from datetime import datetime, timezone

_KEY_SANITIZE_RE = re.compile(r'[^A-Za-z0-9_\-]')


def _clean(part):
    """Make a JSON key safe for use as a dotted metric-key component."""
    return _KEY_SANITIZE_RE.sub('_', str(part).strip()) or '_'


def parse_observed_at(value):
    """Parse an ISO-ish timestamp; naive values are assumed UTC."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _find_observed_at(node):
    """Depth-first search for the response's observationTime / lastUpdated."""
    if isinstance(node, dict):
        for key in ('observationTime', 'lastUpdated'):
            ts = parse_observed_at(node.get(key))
            if ts:
                return ts
        for v in node.values():
            ts = _find_observed_at(v)
            if ts:
                return ts
    elif isinstance(node, list):
        for v in node:
            ts = _find_observed_at(v)
            if ts:
                return ts
    return None


def _emit(out, key, value, unit=None):
    """Append one flat metric. Booleans land in BOTH columns (text true/false
    and num 1/0) so rules can use either. Nulls and empty strings are skipped."""
    if value is None:
        return
    m = {'metric_key': key, 'value_num': None, 'value_text': None, 'unit': unit}
    if isinstance(value, bool):
        m['value_num'] = 1.0 if value else 0.0
        m['value_text'] = 'true' if value else 'false'
    elif isinstance(value, (int, float)):
        m['value_num'] = float(value)
    elif isinstance(value, str):
        if not value.strip():
            return
        m['value_text'] = value
    else:
        return
    out.append(m)


def _is_value_unit(node):
    """{"value": X, "unit": U} objects collapse to a single metric."""
    return (isinstance(node, dict)
            and 'value' in node
            and set(node.keys()) <= {'value', 'unit'}
            and not isinstance(node.get('value'), (dict, list)))


def flatten(node, prefix, out):
    """Generic recursive flattener (§5.2)."""
    if _is_value_unit(node):
        _emit(out, prefix, node.get('value'), unit=node.get('unit'))
        return
    if isinstance(node, dict):
        for k, v in node.items():
            key = f'{prefix}.{_clean(k)}' if prefix else _clean(k)
            flatten(v, key, out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            flatten(v, f'{prefix}.{i}', out)
    else:
        _emit(out, prefix, node)


# ---------------------------------------------------------------------------
# Per-type handlers
# ---------------------------------------------------------------------------

def _normalize_conditions(payload, out):
    body = payload.get('data', payload) if isinstance(payload, dict) else payload
    flatten(body, '', out)
    if isinstance(payload, dict) and 'weatherStation' in payload:
        flatten(payload['weatherStation'], 'weatherStation', out)


def _normalize_observations_v2(payload, out):
    # v2 envelope: {"message": ..., "data": {"data": {...obs...}, "type", "weatherStation", "airQuality"}}
    envelope = payload.get('data', payload) if isinstance(payload, dict) else payload
    if isinstance(envelope, dict):
        flatten(envelope.get('data', {}), '', out)
        for section in ('weatherStation', 'airQuality'):
            if section in envelope:
                flatten(envelope[section], section, out)
    else:
        flatten(envelope, '', out)


def _normalize_lightning_delay(payload, out):
    value = payload
    if isinstance(payload, dict):  # tolerate a wrapped variant
        value = payload.get('data', payload.get('value'))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        _emit(out, 'lightningDelay', float(value), unit='min')
    else:
        flatten(payload, 'lightningDelay', out)


_AQI_INDEXES = (('nowCastIndex', 'nowcast'),
                ('oneHourIndex', 'oneHour'),
                ('twentyFourHourIndex', 'twentyFourHour'))


def _normalize_aqi(payload, out):
    if not isinstance(payload, list):
        flatten(payload, '', out)
        return
    for pollutant in payload:
        if not isinstance(pollutant, dict):
            continue
        name = _clean(pollutant.get('pollutantName') or 'unknown')
        aqi_data = pollutant.get('aqiData') or {}
        for source_key, nice_key in _AQI_INDEXES:
            index = aqi_data.get(source_key) or {}
            _emit(out, f'{name}.{nice_key}', index.get('value'))
        nowcast = aqi_data.get('nowCastIndex') or {}
        _emit(out, f'{name}.category', nowcast.get('shortDescription'))
        _emit(out, f'{name}.isMainPollutant', bool(pollutant.get('isMainPollutant')))
        if pollutant.get('isMainPollutant'):
            _emit(out, 'mainPollutant', pollutant.get('pollutantName'))


_LOCATION_FIELDS = ('name', 'address', 'city', 'state', 'zip', 'latitude',
                    'longitude', 'lat', 'long', 'zone', 'county', 'timeZone')


def _normalize_org_location(payload, out):
    """Mostly metadata; returns the location-info dict for the source record."""
    body = payload.get('data', payload) if isinstance(payload, dict) else payload
    flatten(body, '', out)
    info = {}
    if isinstance(body, dict):
        for field in _LOCATION_FIELDS:
            if body.get(field) is not None:
                info[field] = body[field]
    return info


def _normalize_generic(payload, out):
    body = payload
    # Unwrap common envelopes ({"data": {...}} and the v2 double nesting).
    while isinstance(body, dict) and set(body.keys()) <= {'message', 'data'} and 'data' in body:
        body = body['data']
    if isinstance(body, (dict, list)):
        flatten(body, '', out)
    else:
        _emit(out, 'value', body)   # bare scalar body -> <slug>.value


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def normalize(source_type, slug, payload):
    """Returns (metrics, extras).

    metrics: list of {metric_key, value_num, value_text, unit, observed_at}
             with keys prefixed by the source slug.
    extras:  dict of side-channel info (e.g. {'location_info': {...}}).
    """
    out = []
    extras = {}
    if source_type in ('conditions',):
        _normalize_conditions(payload, out)
    elif source_type == 'observations_v2':
        _normalize_observations_v2(payload, out)
    elif source_type == 'lightning_delay':
        _normalize_lightning_delay(payload, out)
    elif source_type == 'aqi':
        _normalize_aqi(payload, out)
    elif source_type == 'org_location':
        info = _normalize_org_location(payload, out)
        if info:
            extras['location_info'] = info
    else:  # hardware_station_v2, custom_url, anything else
        _normalize_generic(payload, out)

    observed_at = _find_observed_at(payload)
    for m in out:
        m['metric_key'] = f'{slug}.{m["metric_key"]}'
        m['observed_at'] = observed_at
    return out, extras
