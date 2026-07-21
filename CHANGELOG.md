# Changelog

All notable changes to WeatherSniffer are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-07-21

### Added
- **Master weather readout**: sources reporting the same measurement are
  automatically merged into one canonical value per field (temperature, WBGT,
  humidity, wind, rain, lightning hold, PM2.5, …), with known synonyms
  resolved (`relativeHumidity`→humidity, `wetBulbGlobalTemp`→WBGT,
  `rain1Hr`→rain-1hr, `heatIndex`→feels-like). Per field the freshest value
  from a healthy source wins; cards show source attribution and data age, and
  values older than 15 minutes (or from a failing source) are flagged stale.
  Rendered at the top of the dashboard and served at `GET /api/v1/weather`.
  Canonical fields/synonyms are defined in `app/weather.py`.

## [0.2.0] - 2026-07-20

### Added
- **Stale-data guard**: every source now maintains a synthetic
  `<slug>._data_age_seconds` metric — seconds since the feed last produced
  fresh data (`observationTime` where present, else the last successful
  fetch). It keeps counting up when the endpoint fails or returns frozen data,
  so ordinary threshold rules can alert on a dead/stuck feed. Rules bound to
  it are evaluated even while fetches are failing.
- `sources.last_success_at` column (additive migration applied on boot).
- Inline help on the Rules page for the stale-data guard and lightning-delay
  semantics.

### Changed
- **Source management is now admin-only**: add/edit/enable/disable/delete and
  Test fetch (and with them all polling-interval control) require
  `role='admin'` or `is_app_admin`. All app users can still view the source
  list, dashboard, rules and action log.
- Lightning delay is documented and labeled as a countdown in **seconds**
  (unit `sec`, was `min`): a strike (re)starts the 10-minute hold at 600 and
  it counts down; positive = hold active, 0 = all clear.

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
