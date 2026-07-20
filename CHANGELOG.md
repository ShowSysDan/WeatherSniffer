# Changelog

All notable changes to WeatherSniffer are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-07-20

### Added
- Initial release: Flask 3 / SQLAlchemy / PostgreSQL app on port 7170,
  single-process single-worker design (poller + engine + janitor in-process).
- Perry Weather polling for `org_location`, `conditions`, `lightning_delay`,
  `aqi`, `observations_v2`, `hardware_station_v2` and `custom_url` sources
  (GUID-keyed public endpoints, no auth), with a tolerant client (JSON object /
  array / bare number) and a generic recursive normalizer producing
  slug-prefixed flat metrics (`{value, unit}` collapse, boolean dual storage,
  UTC-assumed timestamps).
- `metrics_current` (latest per metric) + `readings` (append-only history).
- Rules engine: `threshold` (edge-triggered, hysteresis, fire-on-clear),
  `on_change` (min-change deadband), `every_tick`; per-rule cooldown.
- Actions: HTTP webhook, TCP send, UDP datagram, Spot reading
  (`POST /api/ingest/<token>`), Spot event marker (`POST /api/event/<token>`);
  `str.format` templating (`{value}`, `{metric_key}`, `{source_name}`, `{unit}`,
  `{observed_at}`, `{now}`).
- `action_log` of every fire with outcome/status/latency, filterable UI and
  CSV export.
- Shared-SSO auth per SHARED_AUTH.md: server-side sessions in
  `shared.app_sessions`, read-only `shared.users`, `is_app_user` login gate,
  admin via `role='admin'`/`is_app_admin`, 5-minute role re-check, fail-closed,
  rate-limited login with anti-enumeration dummy hash.
- Dark responsive web UI: dashboard (auto-refresh), sources (Test fetch),
  rules (discovered-metric dropdown, Test fire), action log, admin settings,
  login, 403/404.
- Syslog to stderr + local socket + remote host (FetchLog-compatible),
  `weathersniffer.*` loggers, `actor=`/`via=` tagging; settings-driven.
- Hourly retention janitor for readings and action log.
- External read API `/api/v1` (metrics, metric-by-key, sources) with optional
  `X-API-Key`; `GET /api/version`.
- Packaging: `install.sh` (setup/install/start/stop/status/uninstall), systemd
  unit (gunicorn `--workers 1 --threads 4`), `.env.example`, README.
