"""Configuration for WeatherSniffer.

Everything infra-level comes from the environment (.env); operational knobs
live in the `settings` table and are editable in the Settings UI.
"""
import os
import re
import secrets

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv is optional
    pass

_SCHEMA_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _schema(name, default=''):
    """Schema names get interpolated into search_path — validate them."""
    value = (os.environ.get(name) or default).strip()
    if value and not _SCHEMA_RE.match(value):
        raise ValueError(f"{name} must match ^[A-Za-z_][A-Za-z0-9_]*$ (got {value!r})")
    return value


class BaseConfig:
    DATABASE_URL = os.environ.get('DATABASE_URL', '')
    DATABASE_SCHEMA = _schema('DATABASE_SCHEMA', 'weathersniffer')
    # Auth is enabled when AUTH_DB_SCHEMA is set (normally 'shared').
    AUTH_DB_SCHEMA = _schema('AUTH_DB_SCHEMA', '')

    SECRET_KEY = os.environ.get('SECRET_KEY') or secrets.token_hex(32)

    # Cookie contract — must match the app family (see SHARED_AUTH.md).
    SESSION_COOKIE_NAME = 'session'
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = os.environ.get('SESSION_COOKIE_SECURE', '') in ('1', 'true', 'yes')
    SESSION_COOKIE_DOMAIN = os.environ.get('SESSION_COOKIE_DOMAIN') or None

    WEB_HOST = os.environ.get('WEB_HOST', '0.0.0.0')
    WEB_PORT = int(os.environ.get('WEB_PORT', '7170'))

    # Largest request body the app will accept (forms/JSON are all tiny).
    MAX_CONTENT_LENGTH = 1 * 1024 * 1024

    # Optional key guarding the /api/v1 read API. Unset = open on a trusted LAN.
    API_KEY = os.environ.get('API_KEY', '')

    # Initial seeds for the settings row (only used when the row is created).
    SYSLOG_ADDRESS = os.environ.get('SYSLOG_ADDRESS', '')
    SYSLOG_FACILITY = os.environ.get('SYSLOG_FACILITY', 'local0')
    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')
    DEFAULT_POLL_SECONDS = int(os.environ.get('DEFAULT_POLL_SECONDS', '60'))
    DEFAULT_RETENTION_DAYS = os.environ.get('DEFAULT_RETENTION_DAYS', '')
    SPOT_DEFAULT_BASE_URL = os.environ.get('SPOT_DEFAULT_BASE_URL', '')


class DevConfig(BaseConfig):
    DEBUG = True


class ProdConfig(BaseConfig):
    DEBUG = False


def get_config():
    env = os.environ.get('FLASK_ENV', 'production').lower()
    return DevConfig if env in ('dev', 'development') else ProdConfig
