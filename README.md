# WeatherSniffer

**Current version: 0.3.0**

WeatherSniffer polls **Perry Weather** public widget/client endpoints (keyed by
GUIDs — no authentication required), normalizes each response into flat
metrics, keeps the current value plus a rolling history in **PostgreSQL**, and
runs a **rules / actions engine**: when a metric crosses a threshold, changes,
or on every poll, it fires an HTTP webhook, a raw TCP send, a UDP datagram, or
a push into **[Spot](https://github.com/ShowSysDan/Spot)**. Important events go
to **syslog** (local and/or a remote server such as
[FetchLog](https://github.com/ShowSysDan/FetchLog)), and a small read API
(`/api/v1/`) lets sibling apps consume current metrics.

It is a member of the same app family as
[Leash](https://github.com/ShowSysDan/Leash), FetchLog and Spot, and shares the
family's single sign-on (see
[SHARED_AUTH.md](https://github.com/ShowSysDan/FetchLog/blob/main/SHARED_AUTH.md)).

---

## ⚠️ Single process / single worker — do not scale out

WeatherSniffer runs as **one process with exactly one gunicorn worker**
(`--workers 1 --threads 4`). The Perry poller (APScheduler), the rules/actions
engine, and the retention janitor all live in that process with in-memory
state. Running more than one worker would fire **every poll and every rule N
times** — duplicate webhooks, duplicate Spot readings, duplicate syslog spam.
This is the same deliberate design as Spot and FetchLog. Don't change it.

---

## How it works

```
Perry Weather endpoints (GUID-keyed, no auth)
        │  GET every poll_interval_seconds (APScheduler, one job per source)
        ▼
  normalize → flat metrics (slug-prefixed dotted keys, e.g. dpac_conditions.wgbt)
        │
        ├── metrics_current  (latest value per metric — dashboard + rule input)
        ├── readings         (append-only history, retention-pruned)
        ▼
  rules engine (threshold / on_change / every_tick, hysteresis, cooldown)
        │ on fire
        ├── HTTP webhook / TCP send / UDP datagram
        ├── Spot reading  (POST {spot}/api/ingest/<token>)
        ├── Spot event    (POST {spot}/api/event/<token>)
        ├── action_log    (every fire, success or failure, CSV-exportable)
        └── syslog        (local /dev/log and/or remote, e.g. FetchLog)
```

## Requirements

- Python **3.11+**
- A reachable **PostgreSQL** (the family's shared database). There is no
  SQLite fallback.
- For building `psycopg2` and local syslog:
  `apt-get install libpq-dev build-essential rsyslog`

## Quick start (dev / smoke test)

```bash
git clone <url> WeatherSniffer && cd WeatherSniffer
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
#   → fill in DATABASE_URL and SECRET_KEY.
#   → leave AUTH_DB_SCHEMA empty to skip the login gate in dev.
.venv/bin/python run.py            # http://<host>:7170
```

## Service install (production)

`install.sh` mirrors the family's installer (subcommands, systemd, dedicated
unprivileged user):

```bash
sudo ./install.sh setup      # create .venv, pip install
sudo ./install.sh install    # write /etc/systemd/system/weathersniffer.service
sudo ./install.sh start
# also: stop | status | uninstall     (bare ./install.sh = setup+install+start)
```

The unit runs `gunicorn wsgi:app --workers 1 --threads 4 --bind 0.0.0.0:7170`
as the `weathersniffer` user with `Environment=TZ=America/New_York`
(**storage is always UTC; display is server-local**, like Spot).

Overridable via env before `install`: `WS_WEB_PORT`, `WS_HOST`, `WS_USER`,
`WS_VENV`.

**Firewall:** open `7170/tcp` (web UI + external API). WeatherSniffer's TCP/UDP
actions are **outbound**, so no inbound rules are needed for them.

**Upgrades:** `git pull` → `sudo ./install.sh setup` (deps) →
`sudo systemctl restart weathersniffer`. `.env` is gitignored, so a pull never
clobbers config. Tables are created/extended additively on boot
(`create_all`); destructive schema changes require a manual migration (same
policy as Spot).

## Database setup & grants

WeatherSniffer keeps its own tables in its own schema and reads the shared
login schema. Run once as a Postgres admin:

```sql
-- Own schema
CREATE SCHEMA IF NOT EXISTS weathersniffer AUTHORIZATION weathersniffer;

-- Shared login schema (read users, read/write sessions)
GRANT USAGE ON SCHEMA shared TO weathersniffer;
GRANT SELECT ON shared.users TO weathersniffer;
GRANT SELECT, INSERT, UPDATE, DELETE ON shared.app_sessions TO weathersniffer;

-- Optional: only if WeatherSniffer should auto-create app_sessions on first boot
-- GRANT CREATE ON SCHEMA shared TO weathersniffer;
```

## Authentication (shared SSO)

Implemented exactly per
[SHARED_AUTH.md](https://github.com/ShowSysDan/FetchLog/blob/main/SHARED_AUTH.md):
server-side sessions in `shared.app_sessions` (opaque `session` cookie,
`SameSite=Lax`, `HttpOnly`), users read **read-only** from `shared.users`,
passwords verified with Werkzeug scrypt.

- **Login is gated on `is_app_user`**; `role == 'admin'` or `is_app_admin`
  unlocks admin-only functions (Settings, and all source/polling management).
- Roles/flags are re-read from the shared DB every ~5 minutes; revoked access
  ends the session mid-flight. If the shared DB is unreachable while auth is
  enabled, WeatherSniffer **fails closed**.
- `/login` is rate-limited (15/min) and runs a dummy hash on unknown usernames.
- Auth is **enabled when `AUTH_DB_SCHEMA` is set** (`shared`); leave it empty
  in dev to run without a login gate.

## `.env` variables

| var | purpose |
|---|---|
| `DATABASE_URL` | Postgres DSN for the shared database (`postgresql://…`) |
| `DATABASE_SCHEMA` | WeatherSniffer's own schema (`weathersniffer`) |
| `AUTH_DB_SCHEMA` | shared schema name (`shared`); empty = disable auth (dev) |
| `SECRET_KEY` | per-app Flask secret (flash/CSRF); need not match siblings |
| `SESSION_COOKIE_SECURE` | `1` on HTTPS deployments |
| `SESSION_COOKIE_DOMAIN` | shared parent domain for cross-subdomain SSO, else unset |
| `WEB_HOST` / `WEB_PORT` | bind (`0.0.0.0` / `7170`) |
| `API_KEY` | optional key for `/api/v1`; unset = open on LAN |
| `SYSLOG_ADDRESS`, `SYSLOG_FACILITY`, `LOG_LEVEL` | initial syslog seeds (also editable in Settings) |
| `DEFAULT_POLL_SECONDS`, `DEFAULT_RETENTION_DAYS`, `SPOT_DEFAULT_BASE_URL` | operational seeds |

Infra stays in `.env`; operational knobs (syslog targets, retention, defaults,
Spot base URL, log level) live in **Settings** so they can change without a
redeploy.

## Perry Weather sources

A **source** is one Perry endpoint to poll; add as many as you like, of mixed
types. No credentials are involved — the GUID-keyed URLs return data directly.

| source_type | endpoint |
|---|---|
| `org_location` | `widget.api.perryweather.com/v1/Widget/OrganizationLocation/{guid}` |
| `conditions` | `widget.api.perryweather.com/v1/Widget/Conditions/{guid}` |
| `lightning_delay` | `widget.api.perryweather.com/v1/Widget/LightningDelay/{guid}` |
| `aqi` | `widget.api.perryweather.com/v1/Widget/Aqi/{guid}` |
| `observations_v2` | `client.api.perryweather.com/v2/observations/ForLocation/{guid}` |
| `hardware_station_v2` | `client.api.perryweather.com/v2/Hardware/WeatherStation/Data/{guid}` |
| `custom_url` | any URL, used verbatim |

Each response is flattened into dotted metric keys prefixed with the source's
**slug**: `dpac_conditions.wgbt`, `dpac_conditions.ambientTemperature`,
`dpac_lightning.lightningDelay`, `dpac_aqi.PM2_5.nowcast`, …
`{"value": X, "unit": U}` objects collapse to one metric; booleans land in both
columns (`true/false` text **and** `1/0` numeric) so rules can use either;
null fields are skipped.

**Lightning delay:** the endpoint returns a bare number — the lightning-hold
countdown in **seconds**. A strike (re)starts the 10-minute hold at **600**
and it counts down; another strike during the hold resets it back to 600.
It becomes `<slug>.lightningDelay` (unit `sec`); **positive = hold active,
0 = all clear**. The canonical rule is `lightningDelay > 0` with **fire on
clear** ticked, so one rule covers both the hold starting and the all-clear —
and because it's edge-triggered, mid-hold resets to 600 extend the hold
without re-firing.

## Master weather readout

Different source types report the same physical measurement — `conditions`
and `observations_v2` both carry `ambientTemperature`, the hardware-station
endpoint calls humidity `relativeHumidity` and WBGT `wetBulbGlobalTemp`, etc.
WeatherSniffer automatically merges them into one **standard weather readout**
shown at the top of the dashboard and served at `GET /api/v1/weather`:

- Metrics are grouped by **canonical field** (temperature, WBGT, humidity,
  wind, rain, lightning hold, PM2.5, …) with known synonyms resolved
  (`relativeHumidity`→humidity, `wetBulbGlobalTemp`→WBGT,
  `rain1Hr`→rain-1hr, `heatIndex`→feels-like).
- Per field, the value from a **healthy source with the freshest
  observation time wins**; each card shows which source it came from and how
  old it is. Values older than 15 minutes (or from a failing source) are
  flagged **stale** (⚠, dimmed).
- Every source's full metric set still appears in its own panel below — the
  readout is a view, not a replacement.

The canonical field list and synonym map live in `app/weather.py`
(`CANONICAL_FIELDS`) — add a tuple there to teach the readout a new field or
alias.

**Stale-data guard:** every source also maintains a synthetic metric
`<slug>._data_age_seconds` — seconds since the feed last produced fresh data
(the response's `observationTime` where one exists, otherwise the last
successful fetch). It keeps counting up when the endpoint fails **or** keeps
returning HTTP 200 with frozen data, so an ordinary threshold rule (e.g.
`<slug>._data_age_seconds > 600` → webhook/Spot event) alerts on a dead or
stuck feed. Strongly recommended for any source that drives safety decisions
(lightning holds).

**Timestamps:** the API's `observationTime`/`lastUpdated` values carry no
timezone and are **assumed UTC**. Everything is stored UTC and displayed in the
server's local timezone.

## Using WeatherSniffer — adding a rule

1. **Add the source first** (admin only). Sources → New: pick a **type** (e.g.
   `conditions`), paste the Perry **GUID**, give it a **name**, set a **poll
   interval**, Save. Hit **Test fetch** to confirm data returns and to populate
   the metric list.
2. **Create the rule.** Rules → New:
   - **Name** it (e.g. "Lightning delay active → notify Q-SYS").
   - **Metric**: choose from the dropdown (e.g. `dpac_lightning.lightningDelay`).
   - **Trigger mode**:
     - **Threshold** — fires when the metric crosses a value. Pick an
       **operator** (`>`, `>=`, `<`, `<=`, `==`, `!=`, is-true, is-false) and a
       **threshold**. Optional **hysteresis** stops it flapping near the
       boundary; tick **fire on clear** to also fire when it drops back below.
     - **On change** — fires whenever the value moves (optionally ignore tiny
       moves with **min change**).
     - **Every tick** — fires on every poll; use this for **continuous
       streaming to Spot**.
   - **Cooldown** (seconds): after firing, wait this long before firing again
     (leave 0 for `every_tick`).
   - **Action**:
     - **HTTP webhook** — method, URL, optional headers, and a body template.
     - **TCP** / **UDP** — host, port, payload template.
     - **Send to Spot (reading)** — Spot base URL + monitor **token**; sends
       the metric's value as a Spot reading. Leave the value-metric blank to
       send this rule's metric.
     - **Spot event marker** — Spot base URL + token + a label template; drops
       a vertical marker on Spot's graph (great for threshold crossings).
   - Templates accept `{value}`, `{value:.1f}`, `{metric_key}`,
     `{source_name}`, `{unit}`, `{observed_at}`, `{now}`.
   - Save, then **Test fire** to verify the endpoint receives it. Watch the
     Action Log.
3. **Examples**
   - *Webhook on a heat threshold:* metric `dpac_conditions.wgbt`, threshold
     `>= 90`, hysteresis `1`, action HTTP `POST https://host/hook` body
     `{"wbgt": {value}}`.
   - *Stream a metric to Spot:* metric `dpac_conditions.ambientTemperature`,
     trigger `every_tick`, action **Send to Spot (reading)** with your Spot
     monitor token.
   - *Annotate a lightning hold:* metric `dpac_lightning.lightningDelay`,
     threshold `> 0`, `fire on clear` on, action **Spot event** label
     `Lightning hold ({value} sec)`.
   - *Alert on a dead feed:* metric `dpac_conditions._data_age_seconds`,
     threshold `> 600`, action HTTP webhook to your ops endpoint.

Threshold rules are **edge-triggered**: they fire on the false→true transition,
not on every poll while the condition stays true. Rule edits reset the edge
state so behavior after a change is predictable.

## Web UI

| page | what it does |
|---|---|
| `/` | Dashboard: **master weather readout** (one canonical value per field, deduped across sources), then current metrics grouped by source, poll status, recent fires; auto-refreshes every 15 s |
| `/sources` | List sources (all users); add / edit / enable / disable / delete and **Test fetch** are **admin-only** (poll intervals live here) |
| `/rules` | List / add / edit / enable / disable / delete rules; metric dropdown from discovered metrics; **Test fire** |
| `/log` | Action log: filterable (rule, outcome), CSV export |
| `/settings` | Admin-only: syslog targets, retention, defaults, Spot base URL, log level |
| `/login`, `/logout` | Shared SSO (when `AUTH_DB_SCHEMA` is set) |

Dark and responsive, like the rest of the family.

## APIs

### Version

- `GET /api/version` → `{"version": "0.1.0"}` (no auth)

### Internal API (session-gated, used by the UI)

- `GET  /api/dashboard` — sources + current metrics + recent fires
- `POST /api/sources/test_fetch` — body `{"id": 1}` (saved source: full poll)
  or `{"source_type","guid","url","slug"}` (preview probe)
- `GET  /api/sources/<id>/metric_keys` — discovered keys for the rule editor
- `POST /api/rules/<id>/test_fire` — dispatch the rule's action now

### External read API (`/api/v1`, optional `API_KEY`)

Pass the key as an `X-API-Key` header or `?api_key=`. Leave `API_KEY` unset
for open access on a trusted LAN.

- `GET /api/v1/metrics` — all current metrics (with source, unit, observed_at)
- `GET /api/v1/metrics/<metric_key>` — one current metric
- `GET /api/v1/weather` — the **master readout**: one canonical value per
  weather field, deduped across sources (see below)
- `GET /api/v1/sources` — sources + last poll status

```bash
curl -H 'X-API-Key: …' http://host:7170/api/v1/metrics/dpac_conditions.wgbt
```

## Syslog

Loggers are children of `weathersniffer.*` (`.web`, `.poller`, `.rules`,
`.actions`, `.db`, `.api`). Records go to **stderr** (visible via
`journalctl -u weathersniffer`) **and** to syslog: local `/dev/log`
(`/var/run/syslog` on macOS is auto-detected) and/or a remote `host:port` —
point the remote target at a **FetchLog** host in Settings.

What gets logged: service start + version; poller start/stop; source fetch
failures; **rule fires** (rule name, metric, value, target, outcome); action
failures; auth failures; retention purges. UI changes carry `actor=<username>`
and engine fires carry `rule=<name> via=engine`, echoing Leash's tagging.

## Retention

`Settings → Retention (days)` prunes `readings` and `action_log` rows older
than N days, hourly. Blank = keep forever.

## Repository layout

```
app/
├── __init__.py       app factory (config, DB, sessions, logging, scheduler, blueprints)
├── __version__.py    single source of the SemVer version
├── config.py         env-driven config (Dev/Prod)
├── db.py             engine + search_path, create_all, idempotent shared.app_sessions
├── auth.py           shared-SSO (SHARED_AUTH.md): DBSessionInterface, login, gating
├── models.py         sources, metrics_current, readings, rules, action_log, settings
├── perry/            endpoints.py (URL templates) · client.py (tolerant GET) · normalize.py (flattener)
├── engine/           poller.py (APScheduler) · rules.py (evaluation) · actions.py (dispatch)
├── logging_setup.py  stderr + local/remote syslog (FetchLog-compatible)
├── janitor.py        hourly retention purge
├── routes/           main.py (pages) · api.py (internal) · external_api.py (/api/v1)
├── templates/ static/
run.py · wsgi.py · install.sh · weathersniffer.service · requirements.txt
```

## Versioning

SemVer, tracked in `app/__version__.py`, served at `GET /api/version`, and
logged in [CHANGELOG.md](CHANGELOG.md) (Keep a Changelog format). The version
is bumped on every functional change.
