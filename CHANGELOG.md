# Changelog

All notable changes to WeatherSniffer are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.0] - 2026-07-21

### Fixed
- **Spontaneous logouts**: the hourly expired-session purge compared the
  naive-UTC `expires_at` column against `CURRENT_TIMESTAMP`, which Postgres
  resolves in the database's timezone — on a DB whose timezone is ahead of
  UTC this deleted LIVE sessions hours early. The purge now compares against
  `now() AT TIME ZONE 'utc'`, matching how the app family writes expiries.

### Added
- **Session diagnostics**: every rejected session is logged with the reason —
  `expired` (idle past lifetime), `no-row` (logged out from another family
  app or purged), or access revoked in the shared directory — so an
  unexpected logout is explained in the logs.
- `SESSION_LIFETIME_HOURS` env var (default 12, the family standard) to tune
  the idle session lifetime.
- **Syslog minimum severity** setting (default `WARNING`): only records at or
  above it are forwarded to the local/remote syslog targets — failures, auth
  problems, 404s/500s — so streaming rules no longer flood FetchLog with one
  INFO line per fire. stderr/journalctl keeps the full stream. 404s and
  unhandled server errors are now logged (WARNING/ERROR) with path + remote,
  and a proper 500 page was added.

## [0.6.0] - 2026-07-21

### Added
- **Per-source aggregation priority**: a new "Aggregation priority" field on
  the (admin-only) source form. Higher priority wins the Current-weather
  readout for the fields that source provides, even when another source is
  fresher. Selection order is now healthy → non-stale → priority →
  freshness, so a preferred source is skipped automatically while its polls
  fail or its data goes stale (>15 min) and the readout falls back to the
  next-best feed. Priority shows as a badge in the sources list and in
  `GET /api/v1/sources`. Aggregated (master) rules follow the same choice.
  (Schema: `sources.aggregation_priority`, idempotent migration on boot.)

## [0.5.0] - 2026-07-21

### Added
- **Rules can target the aggregated Current-weather readout**: pick
  “★ Current weather (aggregated)” as the rule's source and a canonical field
  (`weather.wgbt`, `weather.lightningDelay`, …) as the metric. The rule is
  evaluated on the winning source's poll cadence and fails over automatically
  when that feed dies or goes stale — verified live (disabling the winning
  source seamlessly switched a Spot stream to the next-best feed).
  `GET /api/weather/fields` feeds the rule editor's canonical-field dropdown.
  (Schema: `rules.source_id` is now nullable; NULL = aggregated rule.)

### Changed
- All numeric values on the dashboard and in the action log display rounded
  to 1 decimal place (91.0 renders as 91). Display-only: storage, rule
  evaluation, action payloads and CSV export keep full precision.

## [0.4.0] - 2026-07-21

### Security
- **CSRF protection**: every state-changing request from a logged-in session
  now requires a token (hidden `_csrf` form field, auto-stamped by JS, or
  `X-CSRF-Token` header for the internal JSON API). The token is an HMAC of
  the opaque session id — stateless, rotates on login.
- Security headers on every response (`X-Content-Type-Options: nosniff`,
  `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy: same-origin`).
- Request bodies capped at 1 MB (`MAX_CONTENT_LENGTH`).
- Perry responses capped at 5 MB (streamed read) so a misbehaving endpoint
  can't balloon memory.
- Action templates reject absurd format-spec padding widths
  (`{value:>999999999}` can no longer allocate a gigabyte string).
- API-key comparison uses `hmac.compare_digest` (timing-safe).

### Fixed
- Rule form had the same hidden-invalid-control trap as 0.3.1's source form
  (e.g. a bad TCP port left behind after switching the action type to webhook
  silently blocked Save). Hidden trigger/action sections are now disabled so
  they're exempt from browser validation and omitted from the POST.
- Favicon 404 in the browser console (inline SVG icon).

### Added
- Janitor also purges expired `shared.app_sessions` rows hourly (they were
  otherwise only deleted when the same sid was presented again, so abandoned
  sessions accumulated forever).

## [0.3.1] - 2026-07-21

### Fixed
- **Source edit form: Save did nothing for GUID-based sources.** The hidden
  URL field rendered Python `None` as `value="None"`, which failed the
  browser's `type="url"` validation; because the field was hidden the browser
  blocked submission silently ("an invalid form control … is not focusable").
  None values now render empty, and whichever of GUID/URL doesn't apply to
  the selected type is disabled so it's exempt from validation.

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
