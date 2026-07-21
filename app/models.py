"""SQLAlchemy models — WeatherSniffer's own schema.

Kept flat and legible, like the sibling apps. Enum-ish columns are plain TEXT
validated at the edges (routes/engine) so additive changes never need DDL.
"""
from datetime import datetime, timezone

from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey, Integer,
                        Text, UniqueConstraint, Index)
from sqlalchemy.dialects.postgresql import JSONB

from app.db import Base

SOURCE_TYPES = ('org_location', 'conditions', 'lightning_delay', 'aqi',
                'observations_v2', 'hardware_station_v2', 'custom_url')
TRIGGER_MODES = ('threshold', 'on_change', 'every_tick')
OPERATORS = ('gt', 'lt', 'gte', 'lte', 'eq', 'ne', 'is_true', 'is_false')
ACTION_TYPES = ('webhook_http', 'tcp', 'udp', 'spot_reading', 'spot_event')


def utcnow():
    return datetime.now(timezone.utc)


class Source(Base):
    __tablename__ = 'sources'

    id = Column(Integer, primary_key=True)
    name = Column(Text, nullable=False)
    slug = Column(Text, nullable=False, unique=True)
    source_type = Column(Text, nullable=False)
    guid = Column(Text, nullable=True)
    url = Column(Text, nullable=True)               # custom_url only
    poll_interval_seconds = Column(Integer, nullable=False, default=60)
    enabled = Column(Boolean, nullable=False, default=True)
    # Current-weather aggregation preference: higher wins a canonical field
    # over fresher-but-lower-priority sources (while healthy and not stale).
    aggregation_priority = Column(Integer, nullable=False, default=0)
    last_polled_at = Column(DateTime(timezone=True), nullable=True)
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    last_status = Column(Text, nullable=True)       # ok | error
    last_error = Column(Text, nullable=True)
    location_info = Column(JSONB, nullable=True)    # org_location metadata (address/lat/long/…)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class MetricCurrent(Base):
    __tablename__ = 'metrics_current'
    __table_args__ = (UniqueConstraint('source_id', 'metric_key', name='uq_metrics_current_source_key'),)

    id = Column(Integer, primary_key=True)
    source_id = Column(Integer, ForeignKey('sources.id', ondelete='CASCADE'), nullable=False)
    metric_key = Column(Text, nullable=False)
    value_num = Column(Float, nullable=True)
    value_text = Column(Text, nullable=True)
    unit = Column(Text, nullable=True)
    observed_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class Reading(Base):
    __tablename__ = 'readings'
    __table_args__ = (
        Index('ix_readings_key_fetched', 'metric_key', 'fetched_at'),
        Index('ix_readings_source_fetched', 'source_id', 'fetched_at'),
    )

    id = Column(Integer, primary_key=True)
    source_id = Column(Integer, ForeignKey('sources.id', ondelete='CASCADE'), nullable=False)
    metric_key = Column(Text, nullable=False)
    value_num = Column(Float, nullable=True)
    value_text = Column(Text, nullable=True)
    observed_at = Column(DateTime(timezone=True), nullable=True)
    fetched_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class Rule(Base):
    __tablename__ = 'rules'

    id = Column(Integer, primary_key=True)
    name = Column(Text, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    # NULL source_id = a "master" rule bound to the aggregated Current-weather
    # readout; metric_key is then a canonical field like 'weather.wgbt'.
    source_id = Column(Integer, ForeignKey('sources.id', ondelete='CASCADE'), nullable=True)
    metric_key = Column(Text, nullable=False)
    trigger_mode = Column(Text, nullable=False)     # threshold | on_change | every_tick
    operator = Column(Text, nullable=True)          # threshold mode only
    threshold_num = Column(Float, nullable=True)
    threshold_text = Column(Text, nullable=True)
    hysteresis = Column(Float, nullable=True)
    fire_on_clear = Column(Boolean, nullable=False, default=False)
    min_change = Column(Float, nullable=True)       # on_change mode
    cooldown_seconds = Column(Integer, nullable=False, default=0)
    action_type = Column(Text, nullable=False)
    action_config = Column(JSONB, nullable=False, default=dict)
    last_state = Column(JSONB, nullable=True)       # edge detection: last value + condition
    last_fired_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class ActionLog(Base):
    __tablename__ = 'action_log'

    id = Column(Integer, primary_key=True)
    rule_id = Column(Integer, ForeignKey('rules.id', ondelete='SET NULL'), nullable=True)
    rule_name = Column(Text, nullable=False)        # denormalized snapshot
    fired_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    metric_key = Column(Text, nullable=False)
    trigger_value_num = Column(Float, nullable=True)
    trigger_value_text = Column(Text, nullable=True)
    action_type = Column(Text, nullable=False)
    target = Column(Text, nullable=True)            # url / host:port (tokens redacted)
    outcome = Column(Text, nullable=False)          # success | failure
    status_code = Column(Integer, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)


class Settings(Base):
    """Singleton (id=1) of operator-tunable values; infra stays in .env."""
    __tablename__ = 'settings'

    id = Column(Integer, primary_key=True, default=1)
    syslog_local_enabled = Column(Boolean, nullable=False, default=True)
    syslog_local_address = Column(Text, nullable=False, default='/dev/log')
    syslog_remote_enabled = Column(Boolean, nullable=False, default=False)
    syslog_remote_host = Column(Text, nullable=True)
    syslog_remote_port = Column(Integer, nullable=False, default=514)
    syslog_facility = Column(Text, nullable=False, default='local0')
    # Minimum severity forwarded to syslog (stderr/journal keeps log_level).
    # Default WARNING: fetch failures, action failures, auth failures, 404s —
    # not the per-fire INFO chatter of every_tick streaming rules.
    syslog_min_level = Column(Text, nullable=False, default='WARNING')
    default_poll_interval_seconds = Column(Integer, nullable=False, default=60)
    default_retention_days = Column(Integer, nullable=True)   # blank = keep forever
    spot_default_base_url = Column(Text, nullable=True)
    log_level = Column(Text, nullable=False, default='INFO')


def get_settings(db):
    """Fetch the singleton settings row, creating it from env seeds if needed."""
    from flask import current_app
    row = db.get(Settings, 1)
    if row is None:
        cfg = current_app.config
        retention = cfg.get('DEFAULT_RETENTION_DAYS') or None
        row = Settings(
            id=1,
            syslog_local_enabled=True,
            syslog_local_address=cfg.get('SYSLOG_ADDRESS') or '/dev/log',
            syslog_facility=cfg.get('SYSLOG_FACILITY') or 'local0',
            default_poll_interval_seconds=cfg.get('DEFAULT_POLL_SECONDS') or 60,
            default_retention_days=int(retention) if retention else None,
            spot_default_base_url=cfg.get('SPOT_DEFAULT_BASE_URL') or None,
            log_level=cfg.get('LOG_LEVEL') or 'INFO',
        )
        db.add(row)
        db.commit()
    return row
