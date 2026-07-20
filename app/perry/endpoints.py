"""Perry Weather public widget/client endpoint templates.

These endpoints are keyed by GUIDs and require NO authentication — a plain
GET returns data directly.
"""

URL_TEMPLATES = {
    'org_location':        'https://widget.api.perryweather.com/v1/Widget/OrganizationLocation/{guid}',
    'conditions':          'https://widget.api.perryweather.com/v1/Widget/Conditions/{guid}',
    'lightning_delay':     'https://widget.api.perryweather.com/v1/Widget/LightningDelay/{guid}',
    'aqi':                 'https://widget.api.perryweather.com/v1/Widget/Aqi/{guid}',
    'observations_v2':     'https://client.api.perryweather.com/v2/observations/ForLocation/{guid}',
    'hardware_station_v2': 'https://client.api.perryweather.com/v2/Hardware/WeatherStation/Data/{guid}',
}


def url_for_source(source_type, guid=None, url=None):
    """Resolve the URL to poll for a source. `custom_url` uses `url` verbatim."""
    if source_type == 'custom_url':
        if not url:
            raise ValueError('custom_url sources need a URL')
        return url
    template = URL_TEMPLATES.get(source_type)
    if template is None:
        raise ValueError(f'Unknown source_type: {source_type}')
    if not guid:
        raise ValueError(f'{source_type} sources need a GUID')
    return template.format(guid=guid.strip())
