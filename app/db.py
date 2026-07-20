"""Database wiring: SQLAlchemy engine + session, search_path, table creation,
and the raw dict-row connection helper the shared-auth code expects.

Schema policy (same as Spot):
  * WeatherSniffer's own tables live in its own schema (default `weathersniffer`)
    and are created additively with Base.metadata.create_all. Destructive
    changes need a manual migration.
  * `shared.app_sessions` is created idempotently ONLY if missing (per
    SHARED_AUTH.md). `shared.users` is never created or altered here.
"""
import logging

import psycopg2
import psycopg2.extras
from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker

log = logging.getLogger('weathersniffer.db')

Base = declarative_base()
SessionLocal = scoped_session(sessionmaker(expire_on_commit=False))

engine = None
_dsn = ''
_auth_schema = ''

# Canonical DDL from SHARED_AUTH.md §2.2 — the session store only.
_APP_SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS {schema}.app_sessions (
    sid         TEXT PRIMARY KEY,
    user_id     INTEGER REFERENCES {schema}.users(id) ON DELETE CASCADE,
    data        TEXT NOT NULL DEFAULT '{{}}',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at  TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_app_sessions_expires ON {schema}.app_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_app_sessions_user    ON {schema}.app_sessions(user_id);
"""


def init_db(config):
    """Create the engine, ensure schemas/tables, bind the session factory."""
    global engine, _dsn, _auth_schema

    _dsn = config['DATABASE_URL']
    if not _dsn:
        raise RuntimeError('DATABASE_URL is not set — WeatherSniffer requires PostgreSQL.')

    schema = config['DATABASE_SCHEMA']
    _auth_schema = config.get('AUTH_DB_SCHEMA') or ''

    search_path = f'{schema},{_auth_schema}' if _auth_schema else schema
    engine = create_engine(
        _dsn,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args={'options': f'-csearch_path={search_path}'},
    )
    SessionLocal.configure(bind=engine)

    # Own schema: create if missing (harmless no-op when the admin pre-created
    # it; logged and tolerated when the role lacks CREATE on the database).
    with engine.connect() as conn:
        try:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS {schema}'))
            conn.commit()
        except Exception as exc:
            conn.rollback()
            log.warning('Could not create schema %s (%s) — assuming it exists.', schema, exc)

    from app import models  # noqa: F401  (register models with Base)
    Base.metadata.create_all(engine)
    _apply_additive_migrations()

    if _auth_schema:
        _ensure_app_sessions()


# Columns added after a table first shipped. create_all only creates whole
# tables, so additive column changes are applied here (idempotent).
_ADDITIVE_COLUMNS = (
    ('sources', 'last_success_at', 'TIMESTAMPTZ'),
)


def _apply_additive_migrations():
    with engine.connect() as conn:
        for table, column, ddl_type in _ADDITIVE_COLUMNS:
            try:
                conn.execute(text(
                    f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl_type}'))
                conn.commit()
            except Exception as exc:
                conn.rollback()
                log.warning('Could not add column %s.%s: %s', table, column, exc)


def _ensure_app_sessions():
    """Idempotently create shared.app_sessions only if it is missing.

    Per SHARED_AUTH.md: if this app can't run the migration (no CREATE grant),
    fall back to verifying the table already exists rather than failing boot.
    """
    with engine.connect() as conn:
        exists = conn.execute(
            text('SELECT 1 FROM information_schema.tables '
                 'WHERE table_schema = :s AND table_name = :t'),
            {'s': _auth_schema, 't': 'app_sessions'},
        ).first()
        if exists:
            return
        try:
            for stmt in _APP_SESSIONS_DDL.format(schema=_auth_schema).split(';'):
                if stmt.strip():
                    conn.execute(text(stmt))
            conn.commit()
            log.info('Created %s.app_sessions (was missing).', _auth_schema)
        except Exception as exc:
            conn.rollback()
            log.error('%s.app_sessions is missing and could not be created (%s). '
                      'Auth will fail closed until it exists.', _auth_schema, exc)


class AuthDB:
    """Thin dict-row DB-API wrapper matching the SHARED_AUTH.md recipe
    (`db.execute(sql, params).fetchone()` with dict-like rows)."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def get_db():
    """Connection for the auth layer: unqualified `users` / `app_sessions`
    resolve to the shared schema via search_path."""
    dsn = _dsn.replace('postgresql+psycopg2://', 'postgresql://')
    conn = psycopg2.connect(dsn, options=f'-csearch_path={_auth_schema}')
    return AuthDB(conn)
