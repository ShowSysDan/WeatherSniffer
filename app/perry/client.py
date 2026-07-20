"""HTTP client for Perry Weather endpoints — tolerant by design.

Responses may be a JSON object, a JSON array, or a bare number (the
LightningDelay endpoint returns e.g. `0.0`), and many fields are null.
"""
import json
import logging

import requests

from app.__version__ import __version__
from app.perry.endpoints import url_for_source

log = logging.getLogger('weathersniffer.poller')

TIMEOUT_SECONDS = 10

_session = requests.Session()
_session.headers.update({
    'User-Agent': f'WeatherSniffer/{__version__}',
    'Accept': 'application/json',
})


def fetch(source_type, guid=None, url=None):
    """GET the endpoint for a source and parse the body.

    Returns (parsed, raw_text). `parsed` is whatever the body decodes to:
    dict, list, or a bare number. Raises on network/HTTP/parse errors.
    """
    target = url_for_source(source_type, guid=guid, url=url)
    resp = _session.get(target, timeout=TIMEOUT_SECONDS)
    resp.raise_for_status()
    raw = resp.text.strip()
    if not raw:
        raise ValueError('empty response body')
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Last resort: some endpoints return a bare number without JSON headers.
        try:
            parsed = float(raw)
        except ValueError:
            raise ValueError(f'unparseable response body: {raw[:200]!r}')
    return parsed, raw
