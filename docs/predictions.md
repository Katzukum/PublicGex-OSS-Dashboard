# NinjaTrader futures forecasts

OpenGamma displays **provisional EWMA bands** while it prepares two independent
**shadow models** on ES, MES, NQ and MNQ charts: price-only GARCH and a two-state
Markov volatility model. There is no minimum number of historical days. Each produces
nominal 80% price intervals at 15 and 30 minutes. These are endpoint intervals,
not bounds that contain the whole path. The Markov probability refers to high
residual volatility over the next five minutes; it is not an up/down signal.

Forecasts are enabled **at all hours**, including overnight, whenever the chart
supplies fresh completed five-minute bars. Outside the cash session, or when a
forecast horizon crosses its close, the panel says **EXTENDED HOURS | experimental**.

The Python service runs independently of the dashboard. The existing gamma and
regime connection remains on `127.0.0.1:5010`. Forecasts and futures bar uploads
use a separate, bidirectional connection on `127.0.0.1:5011`. The service does
not use Public.com credentials or place orders.

## Start

1. In the repository's Python environment, install the forecast dependencies:

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements-prediction.txt
   ```

   If you do not use `.venv`, substitute `python`. The launcher prefers `.venv`
   when present, so install into that same environment.

2. Install the updated indicator source:

   ```powershell
   .\Install-PredictionIndicator.ps1
   ```

   This backs up the previous installed `OpenGamma.cs` outside NinjaTrader's
   compilation directory before copying the repository version. In NinjaScript
   Editor press **F5**. Remove and re-add OpenGamma to reload its new secondary
   data series. This step replaces the installed indicator with this repository's
   version; review any separate custom changes in your installed copy first.

3. Use your normal chart contract, such as `NQ DEC26` or `ES 12-26`, and load the history you
   have available. Today's history is enough to start provisional bands after
   six consecutive completed five-minute closes at any hour. Choose a chart
   **Trading Hours** template that includes overnight trading if you want
   overnight forecasts; a cash-only chart cannot supply overnight bars.
   **Keep your existing merge policy.**
   `MergeBackAdjusted`, `MergeNonBackAdjusted` and `DoNotMerge` are all supported.
   Prop-firm feeds that backfill prior contracts into the newest chart contract
   are supported. The chart name identifies the supplied series; it does not
   prove every historical bar came from that expiry. The indicator reads the
   effective setting without changing it.

   Both NinjaTrader month-name labels (`MAR26`, `JUN26`, `SEP26`, `DEC26`) and
   numeric labels (`03-26`, `06-26`, `09-26`, `12-26`) are supported for ES, MES,
   NQ and MNQ. The exact supplied label is preserved in replies, stored bars and
   model artifacts. Different labels remain separate series; the service never
   silently substitutes a different root or expiry.

   **Prediction Feed Label** defaults to `ChartFeed`. Give different providers
   distinct labels, such as `PropFeed` and `BrokerFeed`. Charts using the same
   provider/history should share a label. No account details are read.
   [NinjaTrader merge-policy documentation](https://docs.ninjatrader.com/ninjascript/mergepolicy)

4. Start the service in a separate terminal:

   ```powershell
   .\Start-Predictions.ps1
   ```

   Double-clicking **Launch Predictions.bat** does the same. Keep the service
   process running; the dashboard window may be closed. Connect NinjaTrader to
   a live market-data feed. **Enable Predictions** defaults to true; **Prediction
   Port** defaults to 5011. Use `Start-Predictions.ps1 -Port 5012` and the same
   indicator port if you need a different local port.

   Opening the launcher again recognizes an already running prediction service
   and exits successfully without starting another instance. You can close that
   duplicate launcher window. An unrelated listener or a service using a different
   data folder produces a clear port-conflict message. Older running services
   are recognized too, with a notice that their data folder cannot be verified.

The indicator automatically uploads completed historical five-minute bars and
then fresh completed bars. Its five-minute input series is independent of the
chart's primary interval and the existing JMA spread series. Network I/O and
model fitting do not run in the chart's price-update callback.
Intrabar callbacks capture the completed prior bar even if the first tick of
the next bar was missed; `OnBarClose` callbacks capture the completed current
bar. Timestamps suppress duplicate uploads and prevent unfinished bars from
becoming live observations.

The first fit may take longer while optional Numba compilation warms up. No
Numba installation is required; the same research implementation can run
without it. Provisional forecasts continue while fitting runs or if it fails.
After a successful fit, the next fresh five-minute origin uses GARCH and Markov.

## What appears on the chart

- An upper-left panel shows 15/30-minute lower, center and upper values, the full
  contract, fixed origin price, forecast age, model status and sample counts.
- **PROVISIONAL EWMA** uses recent squared log returns to estimate volatility,
  with zero expected log return. Two EWMA lanes appear until fitted models are
  available. The panel explains the limited-history or fitting status and does
  not display a Markov state probability for this mode.
- Blue GARCH and amber Markov endpoint bands appear in four labeled lanes at
  the right of the chart. These lanes distinguish horizons/models; their
  horizontal position is not a forecast time axis.
- The high-volatility probability is labeled **Next 5m**. State labels reflect
  variance only. Band centers are exponentiated log-price forecasts, not a
  separately validated directional prediction.
- A separate row labels **CASH SESSION** or **EXTENDED HOURS | experimental**
  for both provisional and fitted forecasts. Overnight performance has not been
  established by the earlier cash-session experiments.
- Forecast prices use the supplied chart's price units. No gamma/index-to-futures
  spread is applied, and an issued forecast never moves its origin with live
  ticks. The panel identifies merged chart history.
- A forecast expires 6 minutes 30 seconds after its origin, allowing 90 seconds
  for the next completed bar to arrive. Expired bands are hidden. Connection
  failures, contract mismatch, clock mismatch and invalid data hide forecasts.
- Historical charts and Playback do not display or generate live forecasts.

## Training and prospective validation

The original SPX/NDX experiments are **not** deployed as futures models. Each
chart series and history revision gets its own newly fitted, price-only models.
Series identity includes the instrument, effective merge policy and feed label.
A provider can backfill under any setting, so `DoNotMerge` is not treated as
proof of unmerged source data. The initial
release does not add GEX to the forecast equations: its incremental benefit in
the index experiment was small and inconsistent.

No minimum day count or session-coverage percentage blocks forecasts. Six
consecutive five-minute closes provide five observed returns for provisional
EWMA bands. EWMA starts from their mean squared return and updates with decay
0.94 as more closes arrive. These bands are explicitly provisional and their
outcomes are scored separately from GARCH and Markov.

GARCH and Markov fitting starts with at least **20 observed consecutive-bar
returns**, the numerical minimum accepted by the fitters. That minimum is not
evidence of parameter stability or calibrated coverage. Partial days count;
the service uses up to 60 observed New York calendar dates, including overnight
prices. It prefers prior-day observations. If those
provide fewer than 20 returns, it also uses today's observations strictly before
the forecast origin. Neither the origin's return nor later data enters that fit.

The first successful model pair per history revision is frozen for the forecast
day, saved as a versioned JSON artifact with its exact training cutoff, and
restored after a restart. Both fits must converge. Fits run in a background
worker per active series. A failed fit leaves provisional bands available and
can retry when the training data changes. An already issued origin is never
replaced when fitting finishes; the next origin can use the fitted models.
All-hours fits use separate `all-hours-v1` artifacts and forecast model IDs;
earlier cash-only artifacts and issued forecasts remain intact.

No clock, weekend, cash holiday or end-of-session gate blocks a fresh forecast.
The cash-market holiday and early-close calendar for 2025–2027 is used only to
label forecasts. A forecast is `CASH` when its origin and full 30-minute horizon
lie inside that day's cash session; everything else is `EXTENDED`, including
unknown calendar years. [Calendar source](https://ir.theice.com/press/news-details/2024/NYSE-Group-Announces-2025-2026-and-2027-Holiday-and-Early-Closings-Calendar/default.aspx)

All valid closes contribute, including partial historical sessions. A live
origin requires six consecutive five-minute closes in total; loaded history
counts toward those six. Contiguous sequences continue across midnight and cash
boundaries. Missing bars reset the live filter and split training blocks, so a
maintenance break or weekend price jump is not treated as a five-minute return.
Live bars must arrive within 90 seconds of their close.
Conflicting live prices require a complete history reload.

All-hours support does not create prices while the exchange or feed is closed.
With no new bars, the existing forecast expires normally. Forecasts resume after
six consecutive completed bars following a gap. Endpoints falling in a closure
remain unscored unless the exact live endpoint actually arrives; a later reopen
price is never substituted.

Historical uploads are staged until complete. Compatible additions reuse the
current revision. If overlapping historical closes changed, or the uploaded
history has no overlap with the stored series, a new revision is created from
the newly supplied snapshot. Its models are refitted, including during the
same day; old forecasts, prices, artifacts and scores remain intact. Other
connections on the retired revision must reload their history. Known older
revisions are rejected rather than alternately replacing each other when two
charts have different cached history. An intentional provider reversion
requires a new feed label, such as `PropFeed-v2`.

Observed gaps split the return model. The feed does not identify every
underlying contract or intraday splice, so an unidentified splice can still affect the volatility
fit. Additive back-adjustments also change log returns. Predictions describe
the supplied chart series and still require prospective validation.

Every forecast is written to SQLite **before** it is sent to a chart. Each
series-revision/origin/model pair is immutable. Only an exact subsequent 15/30-minute
endpoint received from the validated live channel can score it; history
backfills and later substitute prices cannot manufacture outcomes. Coverage,
interval score, width and center error are measured separately for each model,
horizon, model version and cash/extended-hours category, with series/revision
provenance in the report. Older unlabeled records remain `LEGACY`. A new
revision cannot fill missing outcomes for old forecasts. Overlapping forecasts
are dependent observations.

These live futures bands remain labeled **SHADOW**, with nominal coverage.
Neither the existing index results nor merely completing a futures fit proves
80% coverage or trading profitability. Prospective records provide the evidence
for later calibration and model selection.

Inspect the accumulated results:

```powershell
.\Start-Predictions.ps1 -Report
# Or:
python -m prediction.service --report
```

Local data is kept under ignored `prediction_data/series/`, in revision
directories containing `forecasts.sqlite3` and `models/`. Registry metadata
records each feed, policy and revision. Data from the initial unscoped version
is preserved and reported as `legacy-unscoped`; it is never silently relabeled
or used to fit a new series. There is no automatic retention purge. Forecast
data does not go into `gex_data.db`. Back up the prediction directory along with the
dashboard's snapshot history. Stop the service with Ctrl+C before copying the
SQLite database without its WAL files.

## Status messages

| Status | Meaning / next action |
|---|---|
| WAITING_FOR_HISTORY | The connection is waiting for the chart's initial history upload to finish. There is no minimum day count. |
| TRAINING | A background fit is running and no fresh live origin is available yet. |
| WARMING_UP | Await a fresh live five-minute close and six consecutive closes with measurable price variation, at any hour. |
| SHADOW | Fresh, recorded forecast available, labeled PROVISIONAL EWMA or FITTED; nominal intervals are being evaluated prospectively. |
| STALE | The feed/service stopped updating, or the latest forecast expired. |
| OUTSIDE_SESSION | Legacy cash-only service status. Restart with the updated all-hours service if this appears. |
| ERROR | Inspect the service terminal; check port, model fit, clock or conflicting bars. Reload history if another chart committed a revision or the merge setting changed. |
| PLAYBACK DISABLED / LIVE ONLY | Forecasts are deliberately unavailable in historical/replay mode. |

## Local protocol and verification

Transport is bounded UTF-8 newline-delimited JSON, request/response, protocol
version 2. Update both the indicator and service together. The service binds
only IPv4 loopback and accepts up to eight clients.
Each connection sends:

```json
{"schema_version":2,"type":"HELLO","instrument":"ES 12-26","symbol":"ES","bar_minutes":5,"mode":"live","history_policy":"MergeBackAdjusted","feed_label":"PropFeed"}
{"schema_version":2,"type":"BAR_BATCH","source":"history","bars":[{"end_utc":"2026-09-29T14:30:00Z","close":6000.25}]}
{"schema_version":2,"type":"HISTORY_END"}
{"schema_version":2,"type":"BAR_BATCH","source":"live","bars":[{"end_utc":"2026-09-30T14:30:00Z","close":6010.25}]}
{"schema_version":2,"type":"PING"}
```

Historical batches have at most 256 bars. Timestamps include a timezone and
identify the completed bar's **end**, not its start. The examples illustrate
the schema; old dates must not be replayed as a live source. The first reply and
nonforecast replies are `STATUS` frames with a server timestamp, policy and feed
label. `HISTORY_END` returns a `STATUS` with the committed `series_id`, which all
subsequent replies must match. A `FORECAST` frame includes series/revision,
chart instrument, model ID, origin/generation/expiry, nominal coverage and
training/observation counts. `forecast_mode`, `model_family` and
`available_models` distinguish the supported modes:

- `PROVISIONAL`, `EWMA`, `["ewma"]`: two sets of flat
  `ewma_{15|30}_{lower|center|upper}` fields; `high_vol_probability` is null.
- `FITTED`, `GARCH_MARKOV`, `["garch","markov"]`: four sets of flat
  `{garch|markov}_{15|30}_{lower|center|upper}` fields and a numeric
  `high_vol_probability`.

The indicator accepts previous schema-2 fitted forecasts without these mode
fields as legacy fitted frames. Unknown explicit modes fail validation.
New forecasts also declare `session_scope: "ALL_HOURS"`,
`session_kind: "CASH"` or `"EXTENDED"`, and `training_policy: "all-hours-v1"`.

```powershell
python -m pytest -q test_prediction_models.py test_prediction_provisional.py test_prediction_store.py test_prediction_series.py test_prediction_service.py test_prediction_integration.py test_prediction_cold_start.py test_prediction_all_hours.py test_prediction_all_hours_data.py test_prediction_contracts.py test_prediction_contract_protocol.py test_prediction_ninjascript_contract.py test_prediction_ninjascript_parser.py test_prediction_ninjascript_capture.py test_prediction_startup.py
```

The tests cover real fitted-model parity with the offline research code,
serialization, causal filtering, chronology, missing bars, feed/history isolation,
prospective scoring, immutable revisions, six-close cold starts, partial-session
fitting, overnight and midnight forecasts, native contract labels,
provisional-to-fitted promotion, the executable NinjaScript reply parser and the TCP round trip. They do not
substitute for a live NinjaTrader chart check with your data provider.
