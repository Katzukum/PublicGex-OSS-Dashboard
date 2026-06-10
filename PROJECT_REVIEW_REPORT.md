# PublicGex OSS Dashboard Review Report

Review date: 2026-06-08

## Scope

Reviewed the Python backend/collector, Eel frontend, NinjaTrader indicator bridge, docs, and repository setup files for:

- Broken code
- Bad assumptions
- Possible improvements

Validation performed:

- `python -m py_compile appy.py publicData.py event_utils.py ninjatrader_broadcaster.py` passed.
- `node --check web\main.js` passed.
- Confirmed `requirements.txt`, `LICENSE`, and `LICENSE.md` are missing.
- Confirmed local SQLite database currently has data:
  - `raw_option_greeks`: 924,696 rows, latest timestamp `2026-06-08 20:19:42.315392`
  - `gex_snapshots`: 9,088 rows, latest timestamp `2026-06-08 20:19:42.315392`

## Executive Summary

The project is not syntactically broken, but several runtime and packaging issues will trip up a fresh user. The dashboard relies on local database state that may not exist yet, documentation points to missing install/license files, the Market Signal view has a confirmed missing DOM reference, and the historical chart currently selects the oldest 100 snapshots rather than the most recent 100.

The highest-priority fixes are:

1. Add `requirements.txt` or `pyproject.toml`, and add the referenced license file.
2. Fix `web/main.js:395` so it does not reference missing `#trafficLight`.
3. Fix dashboard history querying in `appy.py:280-284` to return the latest 100 rows.
4. Decide whether `publicData.py` is a one-shot collector or a polling service, then update code/docs to match.
5. Make dashboard startup work on a fresh clone without requiring a pre-existing `gex_data.db`.

## Broken Code

### 1. Missing install and license artifacts

Severity: High

`README.md:40` and `docs/setup.md:18` instruct users to run:

```bash
pip install -r requirements.txt
```

But `requirements.txt` does not exist. `README.md:61` also says the license is in a `LICENSE` file, but no `LICENSE` or `LICENSE.md` is present.

Impact:

- Fresh setup fails immediately.
- Users cannot reliably recreate the environment.
- The advertised MIT license is not actually included in the repository.

Recommended fix:

- Add `requirements.txt` or `pyproject.toml`.
- Include at least: `eel`, `pandas`, `sqlalchemy`, `python-dotenv`, and the correct Public.com SDK package.
- Add the MIT `LICENSE` file or update the README if the project is not actually MIT licensed.

### 2. Market Signal view references a missing DOM element

Severity: High

`web/main.js:395` runs:

```js
document.getElementById('trafficLight').dataset.loaded = "true";
```

There is no element with `id="trafficLight"` in `web/index.html`.

Impact:

- `loadOverview()` can throw `Cannot read properties of null`.
- The Market Signal view may render partially, then break subsequent JS execution.

Recommended fix:

- Remove the line if it is leftover state from an older UI.
- Or add a real `trafficLight` element if the UI still needs it.
- Prefer a null guard if this is optional:

```js
const trafficLight = document.getElementById('trafficLight');
if (trafficLight) trafficLight.dataset.loaded = "true";
```

### 3. Historical chart queries the oldest 100 snapshots, not the latest 100

Severity: High

`appy.py:280-284` uses:

```sql
ORDER BY timestamp ASC
LIMIT 100
```

Impact:

- With a growing database, the dashboard history chart becomes stale because it shows the first 100 rows ever recorded for a symbol.
- The local DB already has 9,088 snapshot rows, so this is likely visible now.

Recommended fix:

- Query the latest rows first, then reverse them for chart display:

```sql
SELECT timestamp, total_net_gex, spot_price
FROM (
  SELECT timestamp, total_net_gex, spot_price
  FROM gex_snapshots
  WHERE symbol = :symbol
  ORDER BY timestamp DESC
  LIMIT 100
)
ORDER BY timestamp ASC
```

### 4. Fresh dashboard startup assumes database tables already exist

Severity: High

`appy.py:166-176` calls:

```sql
SELECT DISTINCT symbol FROM raw_option_greeks
```

But table creation only happens in `publicData.py` via `Base.metadata.create_all(engine)`. The docs tell users to start `appy.py` first, then `publicData.py`.

Impact:

- On a fresh clone with no `gex_data.db`, the dashboard can fail before the collector has created tables.

Recommended fix:

- Move shared SQLAlchemy models/schema setup into a shared module.
- Ensure the dashboard can create or verify schema at startup.
- Return an empty symbol list with a friendly UI message when no data exists yet.

### 5. Collector behavior does not match documentation

Severity: Medium

`docs/setup.md:64` says `python publicData.py` "begins the polling loop." In reality, `publicData.py:689-834` processes configured symbols once and exits.

Impact:

- Users expecting continuous collection from the separate terminal will get a single refresh.
- The web UI compensates by spawning `publicData.py` repeatedly, but that is not what the docs describe.

Recommended fix:

- Either add a real polling loop around `main()` using `backend_update_delay`, or update docs to call it a one-shot refresh worker.
- If adding a loop, handle graceful shutdown and market-hours behavior.

### 6. Refresh success can be reported even when collection failed

Severity: Medium

`appy.py:517-535` uses `subprocess.run(..., check=True)` and returns `True` if the process exits with code 0. But `publicData.py:825-827` catches broad global errors, logs them, and still exits normally.

Impact:

- Missing API credentials or collector failures can look like successful dashboard refreshes.
- The UI may say refresh complete while no new data was saved.

Recommended fix:

- In `publicData.py`, return a non-zero process exit code when collection cannot run.
- In `trigger_data_refresh()`, capture stdout/stderr and return a structured result:

```json
{ "ok": false, "message": "PUBLIC_API_KEY is missing" }
```

### 7. Import-time settings parsing can crash the collector before logging is useful

Severity: Medium

`publicData.py:24-27` reads `settings.json` at import time. `publicData.py:33` parses `API_RATE_LIMIT` at import time:

```py
API_RATE_LIMIT_PER_MINUTE = int(os.getenv("API_RATE_LIMIT", "60"))
```

Impact:

- Missing/malformed `settings.json` or a non-numeric `API_RATE_LIMIT` prevents the module from loading cleanly.
- This makes failures harder to report through the UI.

Recommended fix:

- Move config loading into a `load_config()` function.
- Validate with clear defaults and error messages.

## Bad Assumptions

### 1. "0DTE" behavior is not consistently 0DTE

`publicData.py:306-329` targets today for `SPY`, `QQQ`, and `IWM`, but targets nearest Friday for `SPX`, `NDX`, `SPXW`, and `NDXP`.

Impact:

- Docs and UI describe 0DTE analysis, but index symbols may use non-0DTE expirations Monday through Thursday.
- Regime calculations may mix different expiration horizons.

Recommended fix:

- Rename the strategy to reflect mixed same-day/weekly logic, or make the expiration mode explicit per symbol in `settings.json`.

### 2. Dashboard startup assumes live API access is available

`web/main.js:18-25` triggers `eel.trigger_data_refresh()` during initial page load before listing symbols.

Impact:

- Initial UI load depends on Public.com credentials, network availability, market data availability, and collector speed.
- A user with valid cached data can still have a slow or failed startup because a live refresh blocks first render.

Recommended fix:

- Load cached symbols/data first.
- Offer refresh as a background action and surface its result separately.

### 3. NinjaTrader broadcast server listens on all network interfaces

`ninjatrader_broadcaster.py:49` binds to:

```py
self.server_socket.bind(('0.0.0.0', port))
```

The NinjaTrader indicator connects to `IPAddress.Loopback`.

Impact:

- The Python process accepts clients from the LAN even though the integration appears local-only.
- This is unnecessary exposure for trading-related signals.

Recommended fix:

- Bind to `127.0.0.1` by default.
- Make LAN binding an explicit opt-in setting.

### 4. C# JSON parsing assumes a very narrow payload shape

`OpenGamma.cs:277-303` and `OpenGamma.cs:305-368` parse JSON manually with string scanning.

Impact:

- Escaped quotes, unexpected whitespace, nested values, or culture-specific number formatting can break parsing.
- Adding fields to the payload increases risk.

Recommended fix:

- Use a real JSON parser available in the NinjaTrader runtime, such as `System.Web.Script.Serialization.JavaScriptSerializer`, `DataContractJsonSerializer`, or a bundled Newtonsoft.Json dependency if allowed.

### 5. Settings weights are displayed as percentages even if they do not sum to 1

`appy.py:329` formats each weight as `int(w*100)%`.

Impact:

- If weights sum to more or less than 1, the displayed composition can be misleading, even though score normalization later divides by total weight.

Recommended fix:

- Display normalized weights or validate that configured weights sum to 1.

## Possible Improvements

### Repository and setup

- Add `requirements.txt` or `pyproject.toml` with pinned or ranged dependency versions.
- Add `LICENSE`.
- Add a `.env.example` file instead of requiring users to infer required variables from docs.
- Add a small startup health check command, for example `python -m publicData --check-config`.
- Consider ignoring `gex_collector.log`; `.gitignore` currently ignores `gex_data.db` and `.env`, but not the log.

### Backend and data model

- Move database models into a shared `models.py` module so both `appy.py` and `publicData.py` use the same schema.
- Add an index on `gex_snapshots(symbol, timestamp)` similar to `raw_option_greeks`.
- Add retention/compaction for `raw_option_greeks`; the DB is already large and will keep growing quickly.
- Replace exact timestamp joins with snapshot IDs or a collection run ID to make raw/profile/snapshot linkage explicit.
- Return structured errors from Eel endpoints instead of only printing exceptions.

### Frontend

- Wrap Eel calls in `try/catch` and show user-visible failures.
- Avoid blocking `init()` on data refresh; render cached data first.
- Avoid `innerHTML` for values derived from event payloads unless they are sanitized. The current event source is local, but this becomes riskier if the TCP listener is ever exposed.
- Remove duplicate `document.getElementById(...).style.display = 'block';` in `web/main.js:46-48`.
- Add empty states for "no symbols", "no data", and "collector failed".

### Collector

- Make one-shot vs daemon mode explicit:
  - `python publicData.py --once`
  - `python publicData.py --loop`
- Use a typed config object and validate symbols, weights, rate limits, and credentials before collection starts.
- Add per-symbol success/failure summary and exit non-zero if all symbols fail.
- Consider market-hours awareness to avoid repeated empty or stale refresh attempts.

### NinjaTrader integration

- Bind Python broadcaster to loopback by default.
- Use newline-delimited JSON consistently and document the payload contract.
- Replace manual C# JSON parsing with a parser.
- Include a schema version in payloads, for example `"schema_version": 1`.

### Tests

Suggested starter tests:

- Unit tests for `calculate_flip_point`, `calculate_gex_slope`, `get_target_expiration`, and `extract_all_options`.
- A config validation test for missing/malformed `settings.json` and `API_RATE_LIMIT`.
- A lightweight integration test using a temporary SQLite DB to verify `get_dashboard_data()` returns latest history.
- A frontend smoke test that loads `web/index.html` with mocked `eel` and checks all nav views render without JS errors.

## Suggested Fix Order

1. Add missing setup artifacts: `requirements.txt`, `LICENSE`, `.env.example`.
2. Fix confirmed UI crash at `web/main.js:395`.
3. Fix stale history query in `appy.py`.
4. Make DB schema initialization shared and fresh-clone safe.
5. Decide and implement collector mode: one-shot or loop.
6. Improve refresh error reporting.
7. Tighten NinjaTrader networking and JSON parsing.

