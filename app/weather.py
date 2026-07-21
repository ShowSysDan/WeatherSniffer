"""Master weather readout — one canonical value per weather field.

Several source types report the same physical measurement under overlapping
(or synonymous) keys: `conditions` and `observations_v2` both carry
`ambientTemperature`, the hardware-station endpoint calls humidity
`relativeHumidity` and WBGT `wetBulbGlobalTemp`, and so on. This module
groups every source's current metrics by canonical field, resolves the known
synonyms, and picks the freshest value from a healthy source — producing the
single "standard weather readout" shown at the top of the dashboard and
served at /api/v1/weather.

Selection rule per field: candidates from sources whose last poll succeeded
beat candidates from erroring sources; within that, the freshest
observed_at (falling back to fetch time) wins. Values older than
STALE_AFTER_SECONDS are still shown but flagged stale.
"""
from datetime import datetime, timezone

from app.models import MetricCurrent, Source

STALE_AFTER_SECONDS = 900

# (canonical key, display label, accepted metric-key tails in priority-neutral
# synonym order). Tails are matched after stripping the source slug and any
# leading 'data.' envelope, so both typed and custom_url sources line up.
CANONICAL_FIELDS = (
    ('ambientTemperature', 'Temperature',    ('ambientTemperature',)),
    ('feelLike',           'Feels Like',     ('feelLike', 'heatIndex')),
    ('wgbt',               'WBGT',           ('wgbt', 'wetBulbGlobalTemp')),
    ('turfTemp',           'Turf Temp',      ('turfTemp',)),
    ('humidity',           'Humidity',       ('humidity', 'relativeHumidity')),
    ('dewPoint',           'Dew Point',      ('dewPoint',)),
    ('windSpeed',          'Wind',           ('windSpeed',)),
    ('windDirection',      'Wind Dir',       ('windDirection',)),
    ('windGust',           'Wind Gust',      ('windGust',)),
    ('windGustDirection',  'Gust Dir',       ('windGustDirection',)),
    ('precipitation',      'Precipitation',  ('precipitation',)),
    ('rainHour',           'Rain (1 hr)',    ('rainHour', 'rain1Hr')),
    ('rainToday',          'Rain Today',     ('rainToday',)),
    ('dayLight',           'Daylight',       ('dayLight',)),
    ('weather',            'Conditions',     ('weather_code.text',)),
    ('lightningDelay',     'Lightning Hold', ('lightningDelay',)),
    ('pm25',               'PM2.5 AQI',      ('airQuality.pM2_5.nowcast', 'PM2_5.nowcast')),
)

_TAIL_TO_CANONICAL = {}
for _canon, _label, _tails in CANONICAL_FIELDS:
    for _tail in _tails:
        _TAIL_TO_CANONICAL[_tail] = _canon


def _base_key(slug, metric_key):
    """Strip the source slug and the generic flattener's 'data.' envelope."""
    base = metric_key
    prefix = f'{slug}.'
    if base.startswith(prefix):
        base = base[len(prefix):]
    if base.startswith('data.'):
        base = base[len('data.'):]
    return base


def _as_utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_master_readout(db):
    """Returns an ordered list of canonical field dicts (only fields that at
    least one enabled source currently reports)."""
    now = datetime.now(timezone.utc)
    sources = {s.id: s for s in db.query(Source).filter(Source.enabled.is_(True))}
    if not sources:
        return []

    best = {}
    rows = (db.query(MetricCurrent)
            .filter(MetricCurrent.source_id.in_(sources.keys()))
            .all())
    for m in rows:
        source = sources[m.source_id]
        canon = _TAIL_TO_CANONICAL.get(_base_key(source.slug, m.metric_key))
        if canon is None:
            continue
        if m.value_num is None and m.value_text is None:
            continue
        healthy = source.last_status == 'ok'
        freshness = _as_utc(m.observed_at) or _as_utc(m.updated_at) or now
        rank = (healthy, freshness)      # healthy beats erroring, then freshest
        incumbent = best.get(canon)
        if incumbent is None or rank > incumbent['_rank']:
            age = (now - freshness).total_seconds()
            best[canon] = {
                '_rank': rank,
                'value_num': m.value_num,
                'value_text': m.value_text,
                'unit': m.unit,
                'metric_key': m.metric_key,
                'source': source.name,
                'source_slug': source.slug,
                'observed_at': _as_utc(m.observed_at),
                'age_seconds': round(age, 1),
                'stale': age > STALE_AFTER_SECONDS or not healthy,
            }

    readout = []
    for canon, label, _tails in CANONICAL_FIELDS:
        entry = best.get(canon)
        if entry:
            item = {k: v for k, v in entry.items() if not k.startswith('_')}
            item['field'] = canon
            item['label'] = label
            readout.append(item)
    return readout
