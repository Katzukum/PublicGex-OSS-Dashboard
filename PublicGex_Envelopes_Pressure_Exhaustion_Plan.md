# PublicGex Plan: Option Envelopes, Estimated Hedge Pressure, and Option Exhaustion

**Repository reviewed:** `Katzukum/PublicGex-OSS-Dashboard`  
**Target branch:** `main`  
**Reviewed revision:** `038cf3adedd581e8da51c34b8eb7a8af18ea37b8`  
**Architecture as of October 6, 2026:** Rust/Tauri desktop shell + React/TypeScript frontend + isolated Python analytical sidecar.

## Executive summary

The updated PublicGex codebase is in a much better position to add these features than the earlier Python/Eel version.

The current collector already persists the core fields needed for all three features:

- option delta
- option gamma
- open interest
- cumulative option volume
- bid / ask / mid / last
- bid and ask sizes
- bid / ask / last timestamps
- implied volatility
- underlying spot
- per-contract snapshots through the session

The new `backend/positioning.py` already calculates:

- unsigned gamma-weighted OI concentration
- 5-minute gamma-weighted recent activity
- 15-minute gamma-weighted recent activity
- observation coverage
- gap/reset handling
- persistent OI concentration levels

That means **Pressure should extend the existing observation pipeline rather than create a second data pipeline**.

The recommended implementation order is:

1. **Option Envelopes**
2. **Estimated Hedge Pressure**
3. **Option Exhaustion**
4. Historical shadow validation
5. Only after validation, consider allowing Pressure/Exhaustion to influence scenario decisions or NinjaTrader trade bias

The most important design rule is:

> PublicGex must distinguish observed data from inferred dealer behavior.

Public.com does not identify CBOE participant type or give us true dealer/customer trade direction. Therefore the feature names and payload metadata should remain explicit:

- `Option Envelopes` can be a high-quality structural/flow proxy.
- `Estimated Hedge Pressure` is an inference, not observed market-maker pressure.
- `Option Exhaustion` is an inferred exhaustion candidate, not a direct CBOE dealer-flow signal.

DeepCharts references:

- Option Envelopes: https://www.deepcharts.com/helpcenter/deepgamma-web/article/option-envelopes
- Option Market Maker Pressure: https://www.deepcharts.com/helpcenter/deepgamma-web/article/option-market-maker-pressure-deepgamma-web
- Option Exhaustion Detector: https://www.deepcharts.com/helpcenter/deepgamma-web/article/option-exhaustion-detector

---

# 1. What already exists in PublicGex

## Current architecture

The October 6 migration changed the application architecture significantly.

Current flow:

```text
Public.com
    |
    v
backend/publicData.py
    |
    v
SQLite
    |
    +--> backend/positioning.py
    +--> backend/gamma_sweep.py
    +--> backend/appy.py
    +--> backend/scenario_engine.py
    |
    v
backend/service.py
    |
    v
Rust/Tauri
    |
    v
React + TypeScript + TradingView Lightweight Charts
```

NinjaTrader remains a separate optional integration on port 5010.

## Relevant stored contract fields

`backend/models.py -> RawOptionGreek` already stores:

```text
delta
gamma
open_interest
underlying_price
gex_value

bid
ask
mid_price
last_price

bid_size
ask_size
volume

implied_volatility

bid_timestamp
ask_timestamp
last_timestamp
```

This is enough to calculate the first version of all three proposed features without adding a new market-data vendor.

## Existing positioning/activity layer

`backend/positioning.py` already calculates gamma-weighted activity using consecutive cumulative-volume observations.

Current logic is effectively:

```text
Activity =
Delta cumulative option volume
x current gamma
x spot^2
```

The code already handles several critical problems correctly:

- same-session filtering
- maximum 120-second interval gap
- missing contract observations
- volume counter resets
- duplicate observations
- null vs measured zero
- partial coverage warnings

This existing machinery should be retained.

## Existing TRACE infrastructure

`backend/appy.py -> get_trace_data()` already provides:

- minute-selected option snapshots
- gamma heatmap data
- minute spot path
- raw spot ticks
- current positioning
- modeled delta pressure
- modeled charm pressure
- decision overlays

This gives Pressure and Exhaustion a natural historical/replay surface.

---

# 2. Feature 1 — Option Envelopes

## Goal

Add three current-session gamma reference levels:

```text
Zero Gamma
Major Gamma
Minor Gamma
```

The feature should support two PublicGex data modes:

```text
Flow Gamma
Structural Gamma
```

### Flow Gamma

Closest approximation to DeepCharts' standard Gamma (call - put) envelope mode.

Use cumulative current-session volume:

```text
Call Flow Gamma =
call volume x gamma x spot^2

Put Flow Gamma =
-put volume x gamma x spot^2
```

The `100 x 0.01` options multiplier conversion cancels to 1, matching the units already used by `positioning.py`.

### Structural Gamma

Use reported open interest:

```text
Call Structural Gamma =
call OI x gamma x spot^2

Put Structural Gamma =
-put OI x gamma x spot^2
```

This is closest to DeepCharts' EOD OI gamma mode.

It is **not** equivalent to DeepCharts' Advanced real-time CBOE OI-R data.

---

## Envelopes calculation

Create:

```text
backend/option_envelopes.py
```

Recommended public function:

```python
build_option_envelopes(
    rows,
    *,
    spot,
    source="volume_gamma",
    symbol=None,
    as_of=None,
)
```

Return:

```json
{
  "model": "publicgex_envelopes_v1",
  "source": "volume_gamma",
  "status": "ready",
  "as_of": "...",
  "zero_gamma": 6742.5,
  "major_gamma": 6750.0,
  "minor_gamma": 6725.0,
  "major_strength": 48200000.0,
  "minor_strength": 35100000.0,
  "coverage": {
    "contracts": 412,
    "usable_contracts": 390,
    "min_strike": 6550,
    "max_strike": 6950
  },
  "warnings": []
}
```

---

## Major and Minor Gamma

DeepCharts describes:

- Major Gamma = highest gamma concentration
- Minor Gamma = secondary gamma concentration

For PublicGex v1:

```text
signed_gamma_at_strike =
sum(call weighted gamma)
-
sum(put weighted gamma)

strength_at_strike =
abs(signed_gamma_at_strike)
```

Then:

```text
Major Gamma = strongest valid strike
Minor Gamma = second strongest valid strike
```

Recommended tie breaking:

1. larger absolute concentration
2. closer to spot
3. lower strike for deterministic output

The API should expose the strength values as well as the levels.

This matters because two levels that are very close in strength should not be presented as though Major is overwhelmingly more important.

---

## Zero Gamma

Do not create a second unrelated zero-gamma algorithm.

Instead, refactor `backend/gamma_sweep.py` so the same modeled gamma-across-spot engine can accept a contract weight source:

```text
open_interest
volume
```

Conceptually:

```python
build_gamma_sweep(..., weight_field="open_interest")
build_gamma_sweep(..., weight_field="volume")
```

Then:

- Structural Envelope Zero Gamma uses OI weighting.
- Flow Envelope Zero Gamma uses volume weighting.

This keeps the zero-crossing methodology consistent with the existing Gamma Sweep instead of mixing a modeled spot sweep with a simple cumulative-strike approximation.

---

## Important strike-window caveat

The live collector currently uses:

```python
STRIKE_RANGE_PCT = 0.03
```

So the normal contract universe is approximately:

```text
spot +/- 3%
```

That is appropriate for the current intraday dashboard, but an envelope level could exist outside this window.

### Phase 1 recommendation

Do **not** widen the collector immediately.

Instead:

- calculate Envelopes using the current stored range
- expose `min_strike` / `max_strike`
- add a warning when Major/Minor sits close to the edge of the collected universe
- record whether Zero Gamma lies near/outside the modeled range

Example:

```text
"Envelope concentration is near the collected strike boundary; a stronger level may exist outside the stored +/-3% window."
```

### Phase 2 collector improvement

After verifying API load and database growth, separate:

```text
collection window
legacy signal window
display window
```

For example:

```text
collection: +/-5% or configurable
legacy calculations: preserve current +/-3% semantics
display: user-selected/focused range
```

This avoids silently changing current scenario behavior just to support the new feature.

---

## Envelopes backend integration

Modify:

```text
backend/appy.py
```

Add to dashboard payload:

```json
"envelopes": {
  "flow": {...},
  "structural": {...}
}
```

Also add to TRACE payload so historical cursor/replay can display the level state associated with that snapshot.

For one-off profiles, include Structural Gamma Envelopes immediately.

Flow Gamma should be unavailable for a one-off historical profile unless that profile has valid same-session volume observations.

---

## Envelopes frontend integration

Modify:

```text
src/types.ts
src/api.ts
src/Cockpit.tsx
src/TraceCharts.tsx
src/ProfileCharts.tsx
```

Recommended UI:

### Cockpit

Add an `Option Envelopes` section near Gamma Sweep.

Controls:

```text
Source:
[ Flow Gamma | Structural Gamma ]
```

Display:

```text
Zero Gamma      6742.5
Major Gamma     6750
Minor Gamma     6725
```

Show source metadata:

```text
Flow Gamma · Public volume/gamma proxy
```

or:

```text
Structural Gamma · reported OI/gamma proxy
```

### TRACE

Overlay current/historical Envelopes on the price path:

- Zero Gamma: solid line
- Major Gamma: stronger broken line
- Minor Gamma: lighter broken line

Do not hard-code DeepCharts colors unless desired; PublicGex should keep its own visual language.

---

# 3. Feature 2 — Estimated Hedge Pressure

## Goal

Create a session-reset cumulative line analogous in use to options CVD:

```text
Estimated Hedge Pressure
EMA 15
EMA 30
```

DeepCharts has CBOE participant information.

PublicGex does not.

Therefore the correct name is:

```text
Estimated Hedge Pressure
```

not:

```text
Market Maker Pressure
```

---

# 4. Pressure should extend positioning.py

Do not replace the existing unsigned recent-activity calculation.

It answers a different question:

```text
Recent Activity:
"How much gamma-weighted option turnover occurred?"
```

Pressure should answer:

```text
Estimated Hedge Pressure:
"What direction would underlying hedging likely have been pushed if the inferred option aggressor was the customer and the liquidity provider was on the other side?"
```

Recommended new module:

```text
backend/flow_pressure.py
```

Reuse the snapshot/interval validation conventions from:

```text
backend/positioning.py
```

Ideally extract shared interval utilities rather than duplicate them.

---

# 5. Inferring option aggressor direction

For every contract interval:

```text
DeltaVolume =
current cumulative volume
-
previous cumulative volume
```

Only classify when:

- DeltaVolume > 0
- both snapshots contain the contract
- interval gap <= 120 seconds
- volume counter did not reset
- bid/ask/last are valid
- quote timestamps are not obviously stale

## Proposed aggressor score

Calculate:

```text
mid = (bid + ask) / 2
half_spread = (ask - bid) / 2
```

Then:

```text
aggressor_score =
clip((last - mid) / half_spread, -1, +1)
```

Interpretation:

```text
+1.0 = last at/near ask
 0.0 = last around mid
-1.0 = last at/near bid
```

If:

```text
ask <= bid
```

or quotes/last are unusable:

```text
aggressor_score = unknown
```

Do not force unknown flow to zero.

Track it separately as unclassified volume.

---

# 6. Estimated hedge impulse

For every classified interval:

```text
hedge_shares =
DeltaVolume
x aggressor_score
x option_delta
x 100
```

This produces the expected hedge direction naturally.

Examples:

```text
customer buys call
aggressor = +1
delta > 0
=> positive hedge pressure
```

```text
customer buys put
aggressor = +1
delta < 0
=> negative hedge pressure
```

```text
customer sells call
aggressor = -1
delta > 0
=> negative hedge pressure
```

```text
customer sells put
aggressor = -1
delta < 0
=> positive hedge pressure
```

Also return:

```text
hedge_dollars = hedge_shares x spot
```

Recommended primary displayed unit:

```text
$ delta pressure
```

because it is intuitive across SPX/NDX.

Keep `hedge_shares` in the API for diagnostics.

---

# 7. One-minute Pressure series

The collector has an adaptive polling interval.

Therefore do not define the moving averages as "15 polls" and "30 polls."

Instead:

1. calculate valid interval impulses
2. assign each impulse to a one-minute bucket
3. sum impulses inside each minute
4. create the cumulative session pressure line
5. calculate EMA 15 and EMA 30 on the one-minute cumulative series

Return:

```json
{
  "model": "estimated_hedge_pressure_v1",
  "status": "partial",
  "session_date": "2026-10-06",
  "points": [
    {
      "timestamp": "2026-10-06 10:31:00",
      "impulse_dollars": 12500000,
      "cumulative_dollars": 78000000,
      "ema_15": 71200000,
      "ema_30": 64300000
    }
  ],
  "current": {
    "cumulative_dollars": 78000000,
    "slope_5m": 0.42,
    "relative_to_ema15": 6800000,
    "relative_to_ema30": 13700000
  },
  "coverage": {
    "classified_contract_volume": 12104,
    "unclassified_contract_volume": 3290,
    "classification_ratio": 0.786
  },
  "warnings": []
}
```

---

# 8. Pressure confidence/coverage

Pressure should never be presented without classification quality.

Track at least:

```text
classified volume
unclassified volume
classification ratio
usable intervals
discarded intervals
max interval gap
quote-age warnings
```

Suggested status:

```text
ready:
classification ratio >= 70%
and enough observed session minutes

partial:
30%-70%

unavailable:
<30% or insufficient observations
```

These are initial engineering thresholds, not trading thresholds, and should be tuned from actual live data.

---

# 9. Pressure frontend

Create:

```text
src/PressurePanel.tsx
src/pressure.css
```

Add below or near the existing Positioning panel in Cockpit.

Chart:

```text
Estimated Hedge Pressure
EMA 15
EMA 30
```

Below the chart:

```text
Direction       Rising / Falling / Flat
vs EMA15        +$6.8M
5m impulse      +$12.5M
Coverage        78.6%
```

Always display:

```text
Estimated from option volume + quotes; dealer/customer identity is not observed.
```

### TRACE

Add Pressure as a lower time-series panel synchronized with TRACE's session time axis.

This is particularly valuable for visual study of:

```text
price high + pressure lower high
price low + pressure higher low
pressure EMA crosses
pressure turn near gamma levels
```

---

# 10. Do not put Pressure into the scenario engine yet

The current repository deliberately states that the new positioning/activity research measures do not change existing scenario gates.

Preserve that rule.

Do not initially modify:

```text
backend/scenario_engine.py
```

to add Pressure to the decision score.

Instead add it to the decision workspace as informational research metadata:

```json
"estimated_pressure": {...}
```

The existing decision should remain unchanged.

This makes the rollout falsifiable:

```text
Old decision
vs
Old decision + observed Pressure condition
```

can be tested without contaminating the baseline.

---

# 11. Feature 3 — Option Exhaustion

## Goal

Produce chart markers showing areas where an option-flow move may be losing strength.

Recommended PublicGex name:

```text
Option Exhaustion
```

but every event should carry:

```text
model = "estimated_option_exhaustion_v1"
```

and inferred direction labels should use:

```text
Estimated Call Buy
Estimated Call Sell
Estimated Put Buy
Estimated Put Sell
```

not definitive participant labels.

---

# 12. Four inferred flow buckets

The Pressure classifier can already identify:

```text
option side
+
estimated aggressor side
```

Aggregate classified volume/pressure into:

```text
call_buy
call_sell
put_buy
put_sell
```

For each one-minute bucket calculate:

```text
contract volume
delta-dollar flow
gamma-weighted activity
classification quality
```

This gives Exhaustion a richer input than a single cumulative line.

---

# 13. Price filter

DeepCharts defaults:

```text
Period: 5 bars
Call marker: price above 70% of recent range
Put marker: price below 30% of recent range
```

PublicGex already has `spot_ticks` and a minute `spot_path` in TRACE.

For v1, construct **observed one-minute bars** from stored spot ticks:

```text
open = first observation
high = maximum observation
low = minimum observation
close = last observation
```

Important caveat:

These are not exchange-complete OHLC bars.

They are bars made from the collector's observed spot samples.

Expose:

```text
bar_source = "collector_spot_observations"
```

If a minute has insufficient observations, retain it as partial rather than manufacturing an artificial high/low.

Later, if Public's underlying historical-bar endpoint is incorporated, the price filter can switch to true one-minute OHLC without changing the Exhaustion API.

---

# 14. Exhaustion event logic

Avoid inventing a black-box probability.

Use an evidence-based candidate score.

A marker requires:

## A. Price location

Call-side candidate:

```text
price_position_5bar >= 0.70
```

Put-side candidate:

```text
price_position_5bar <= 0.30
```

## B. Flow strength

The relevant inferred flow bucket must be unusually large relative to the session.

Use rolling percentile instead of a fixed dollar threshold.

Suggested research presets:

```text
Low       >= 70th percentile
Medium    >= 80th percentile
High      >= 90th percentile
Strong    >= 95th percentile
```

DeepCharts defaults to High.

PublicGex can start with High as well, but this should remain a configurable research threshold.

## C. Exhaustion evidence

Add evidence flags rather than one opaque rule:

```text
pressure_turn
pressure_price_divergence
ema15_reversal
flow_deceleration
major_gamma_proximity
minor_gamma_proximity
```

Example:

```json
{
  "timestamp": "...",
  "side": "CALL",
  "action": "BUY",
  "label": "Estimated Call Buy",
  "price": 6757.25,
  "flow_percentile": 0.94,
  "price_position": 0.87,
  "evidence": [
    "pressure_turn",
    "major_gamma_proximity"
  ],
  "score": 0.78
}
```

The score is an **evidence score**, not a probability of reversal.

---

# 15. Recommended Exhaustion score

Start simple and transparent.

Example research score:

```text
30% flow extremity
25% pressure reversal/deceleration
20% price-range location
15% price/pressure divergence
10% envelope proximity
```

Do not permanently lock these weights in before validation.

Store component scores so the historical evaluator can test whether some inputs have no predictive value.

---

# 16. Exhaustion UI

Create:

```text
src/ExhaustionMarkers.ts
```

or integrate into the existing TRACE primitive/overlay system.

### TRACE

This is the primary home.

Plot markers on the price chart:

```text
Est. Call Buy Exhaustion
Est. Call Sell Exhaustion
Est. Put Buy Exhaustion
Est. Put Sell Exhaustion
```

Tooltip should include:

```text
Flow percentile
Pressure state
5-bar range position
Nearest Envelope level
Classification coverage
Evidence flags
```

### Cockpit

Show only the current state:

```text
Exhaustion
Upside candidate · High evidence
```

or:

```text
No active exhaustion condition
```

Do not flood Cockpit with every historical marker.

---

# 17. Backend modules to add

Recommended new files:

```text
backend/option_envelopes.py
backend/flow_pressure.py
backend/exhaustion.py
```

Responsibility boundaries:

## option_envelopes.py

Owns:

```text
source weighting
per-strike concentration
major/minor ranking
zero gamma adapter
coverage/warnings
```

## flow_pressure.py

Owns:

```text
valid observation intervals
volume deltas
aggressor inference
estimated hedge impulse
1-minute pressure series
EMAs
classification coverage
four inferred flow buckets
```

## exhaustion.py

Owns:

```text
observed minute price bars
price filter
flow-percentile calculation
pressure turn/divergence
envelope proximity
marker/evidence construction
```

Keep these calculations pure where possible so they can be unit tested without SQLite.

---

# 18. Existing backend files to modify

## backend/publicData.py

No new quote fields are required.

Keep the current storage of:

```text
bid
ask
mid
last
volume
delta
gamma
timestamps
```

Potential later change:

```text
separate wider collection window from legacy +/-3% signal window
```

Do not make that change in the first feature commit.

## backend/gamma_sweep.py

Refactor contract weighting so the same gamma sweep can be reused for:

```text
OI-weighted zero gamma
volume-weighted zero gamma
```

Preserve existing default behavior exactly.

## backend/positioning.py

Extract/reuse observation-interval utilities with `flow_pressure.py`.

Do not alter the semantics of existing:

```text
oi_activity_v1
```

until the new feature is proven.

## backend/appy.py

Add:

```text
dashboard.envelopes
dashboard.estimated_pressure
```

and TRACE:

```text
envelopes
estimated_pressure
exhaustion_events
```

Use snapshot-keyed caching in the same style as existing dashboard/TRACE caches.

## backend/scenario_engine.py

Phase 1-3:

```text
NO scoring change
```

Optionally place observational state into warnings/context metadata.

Only add it as a decision input after validation.

## backend/signal_performance.py

Do not change current bias generation.

For shadow research, include new observational fields inside stored payload JSON or add research tags when events are recorded:

```text
pressure_rising
pressure_falling
pressure_above_ema
pressure_divergence
upside_exhaustion
downside_exhaustion
near_major_gamma
near_minor_gamma
```

## backend/positioning_validation.py

Extend this rather than inventing a separate validation framework.

Add evaluation families:

```text
envelope_touch
pressure_direction
pressure_divergence
exhaustion_reversal
```

Retain chronological development/holdout separation.

## backend/demo_data.py

Generate deterministic synthetic:

```text
envelopes
pressure line
EMA lines
exhaustion markers
```

Demo must remain clearly synthetic.

## backend/ninjatrader_broadcaster.py

Do not make these required schema fields initially.

After validation, optional additive fields can be sent:

```text
envelope_zero
envelope_major
envelope_minor

pressure_state
pressure_score

exhaustion_state
```

Keep schema-v2 backward compatibility.

---

# 19. Frontend files to modify

Primary:

```text
src/types.ts
src/api.ts
src/Cockpit.tsx
src/TraceCharts.tsx
src/ProfileCharts.tsx
src/ResearchViews.tsx
```

New:

```text
src/PressurePanel.tsx
src/pressure.css
```

Optional:

```text
src/ExhaustionMarkers.ts
```

## Type additions

Suggested shape:

```typescript
interface OptionEnvelopes {
  model: 'publicgex_envelopes_v1';
  source: 'volume_gamma' | 'oi_gamma';
  status: 'ready' | 'partial' | 'unavailable';
  zero_gamma: number | null;
  major_gamma: number | null;
  minor_gamma: number | null;
  major_strength: number | null;
  minor_strength: number | null;
  coverage: EnvelopeCoverage;
  warnings: string[];
}

interface PressurePoint {
  timestamp: string;
  impulse_dollars: number | null;
  cumulative_dollars: number | null;
  ema_15: number | null;
  ema_30: number | null;
}

interface EstimatedPressure {
  model: 'estimated_hedge_pressure_v1';
  status: 'ready' | 'partial' | 'unavailable';
  points: PressurePoint[];
  coverage: PressureCoverage;
  warnings: string[];
}

interface ExhaustionEvent {
  timestamp: string;
  side: 'CALL' | 'PUT';
  action: 'BUY' | 'SELL';
  label: string;
  price: number;
  price_position: number;
  flow_percentile: number;
  score: number;
  evidence: string[];
}
```

Update strict validation in:

```text
src/api.ts
```

Do not silently accept malformed new payloads.

---

# 20. Settings

Add research/display settings in:

```text
backend/app_settings.py
src/types.ts
Settings UI
```

Suggested defaults:

```text
envelope_source = volume_gamma

pressure_ema_1 = 15
pressure_ema_2 = 30

exhaustion_sensitivity = high
exhaustion_price_filter_enabled = true
exhaustion_period_bars = 5
exhaustion_call_above = 0.70
exhaustion_put_below = 0.30
```

Do not expose too many low-level knobs in the first UI.

The defaults above are enough for initial research.

---

# 21. Database/schema strategy

## Initial implementation

No new table is required.

The features can be recomputed from:

```text
gex_snapshots
raw_option_greeks
```

Benefits:

- no duplicated derived data
- no migration risk
- historical sessions immediately become researchable when quote/volume fields are available
- algorithm changes do not invalidate stored derived rows

Use short-lived caches keyed by:

```text
symbol
snapshot_id
session_date
model version
```

## Later optimization

If pressure history becomes expensive, add a migration for a derived research table such as:

```text
option_flow_metrics
```

but only after profiling proves it is needed.

Do not persist premature derived state.

---

# 22. Historical-data limitation

Older database rows may not contain:

```text
volume
bid
ask
last
quote timestamps
```

even though the schema now has those columns.

Therefore:

## Envelopes

OI-based Structural Envelopes can work on older rows if gamma/OI exist.

Flow Envelopes need volume.

## Pressure

Requires consecutive snapshots with:

```text
volume
delta
usable quote state
```

## Exhaustion

Requires Pressure plus enough spot observations.

All backtests must report sample coverage instead of silently treating missing data as zero.

---

# 23. Validation plan

The current repo already has:

```text
backend/positioning_validation.py
```

and a chronological holdout methodology.

Use the same philosophy.

## A. Envelopes validation

For each session evaluate:

```text
distance from price when level becomes active
touch rate
time to touch
reaction after touch
maximum excursion through level
rejection magnitude
```

Compare:

```text
Flow Major
Flow Minor
Structural Major
Structural Minor
existing persistent OI levels
nearest-strike baseline
```

The goal is not to prove the names.

The goal is to determine whether these ranked levels contain incremental information.

## B. Pressure validation

Do not test only whether the cumulative value is positive/negative.

Test:

```text
5m pressure slope
15m pressure slope
pressure vs EMA15
pressure vs EMA30
EMA cross
price/pressure divergence
pressure acceleration/deceleration
```

Forward outcomes:

```text
5m return
15m return
30m return
MFE
MAE
target hit
invalidation hit
```

## C. Exhaustion validation

Measure:

```text
reversal within 5m
reversal within 15m
reversal within 30m
continuation failure rate
MFE against marker
MAE against marker
```

Segment by:

```text
call buy
call sell
put buy
put sell

positive gamma regime
negative gamma regime

near Major
near Minor
not near Envelope
```

This will show whether the marker is genuinely exhaustion or merely high activity during continuation.

---

# 24. Holdout discipline

Keep chronological development/holdout separation.

Recommended minimum before any signal integration:

```text
20+ independent sessions
and
100+ usable exhaustion candidates
```

Prefer more.

Do not tune threshold weights on the holdout sample.

Store model version strings in every payload so future changes can be compared cleanly.

---

# 25. Test plan

Add backend tests:

```text
backend/tests/test_option_envelopes.py
backend/tests/test_flow_pressure.py
backend/tests/test_exhaustion.py
```

Extend:

```text
test_collector_activity.py
test_positioning.py
test_positioning_validation.py
test_gamma_sweep.py
test_scenario_engine.py
test_service.py
```

## Envelopes tests

Cover:

- call/put weighting
- volume vs OI mode
- missing gamma
- zero volume
- ranking ties
- Major/Minor deterministic ordering
- zero crossing
- edge-of-strike-range warning
- no mutation of existing Gamma Sweep defaults

## Pressure tests

Cover:

- volume increase
- unchanged volume
- volume reset
- missing prior observation
- >120s gap
- last at ask
- last at bid
- last at mid
- zero/negative spread
- stale quote timestamp
- call buy sign
- call sell sign
- put buy sign
- put sell sign
- unclassified volume
- session reset
- one-minute bucketing
- EMA values
- classification ratio

## Exhaustion tests

Cover:

- 5-bar filter
- exactly 70% call threshold
- exactly 30% put threshold
- insufficient price observations
- rolling percentile behavior
- no look-ahead
- pressure turn
- divergence
- Major/Minor proximity
- Low/Medium/High/Strong sensitivity
- duplicate marker suppression

---

# 26. Frontend tests

Add tests for:

```text
Pressure panel rendering
null vs zero Pressure
partial coverage warning
Envelope source toggle
Envelope line labels
Exhaustion marker tooltips
TRACE replay with historical markers
tab-state retention
theme changes
PNG export
```

Extend strict API contract tests so malformed research payloads fail loudly.

---

# 27. End-to-end tests

Add a synthetic session fixture containing:

```text
rising call-buy pressure
pressure rollover
price at upper 5-bar range
Major Gamma nearby
upside exhaustion marker
```

and a second fixture for downside.

E2E should prove:

1. app starts in demo mode
2. Cockpit displays Envelopes
3. Pressure chart renders
4. TRACE displays Pressure
5. Exhaustion marker appears
6. replay preserves marker timing
7. existing scenario output is unchanged

---

# 28. Regression requirement

This is critical.

Before Pressure or Exhaustion affects decisions, the following existing outputs must remain unchanged for the same input fixtures:

```text
regime
scenario type
bias
target
invalidation
execution candidates
NinjaTrader schema-v2 numerical fields
Gamma Sweep default OI behavior
existing positioning values
```

The repo already emphasizes preserving analytical semantics through the migration.

Continue that approach.

---

# 29. Recommended implementation sequence

## Commit 1 — Shared observation utilities

Refactor only.

Extract shared code from `positioning.py` for:

```text
contract identity
time parsing
bounded consecutive intervals
gap validation
volume delta validation
```

All existing positioning tests must stay green.

No UI change.

---

## Commit 2 — Option Envelopes backend

Add:

```text
backend/option_envelopes.py
```

Refactor Gamma Sweep for configurable position weight.

Expose Flow and Structural Envelopes through dashboard payload.

Add backend tests.

No decision change.

---

## Commit 3 — Envelopes UI

Add Cockpit and TRACE visualization.

Add API types/validators.

Add demo data and E2E coverage.

---

## Commit 4 — Estimated Hedge Pressure backend

Add:

```text
backend/flow_pressure.py
```

Implement:

```text
aggressor classifier
classified/unclassified volume
hedge impulse
minute buckets
cumulative line
EMA15
EMA30
coverage
```

Expose dashboard + TRACE payload.

No decision change.

---

## Commit 5 — Pressure UI

Add:

```text
src/PressurePanel.tsx
```

Cockpit summary + TRACE synchronized panel.

Add coverage/warning surfaces.

---

## Commit 6 — Exhaustion backend

Add:

```text
backend/exhaustion.py
```

Implement:

```text
observed minute bars
5-bar price filter
flow percentile
pressure turn
divergence
envelope proximity
marker evidence
```

No scenario-engine change.

---

## Commit 7 — Exhaustion UI

TRACE markers + Cockpit current-state summary.

Add settings controls:

```text
sensitivity
period
70/30 filter thresholds
```

---

## Commit 8 — Historical shadow evaluator

Extend:

```text
backend/positioning_validation.py
```

Produce JSON/CSV summary for:

```text
Envelopes
Pressure
Exhaustion
```

Keep development/holdout split.

---

## Commit 9 — Optional NinjaTrader research fields

Only after desktop behavior is stable.

Send additive optional fields.

Do not modify existing bias logic.

---

## Commit 10 — Signal integration decision

Only after enough holdout evidence exists.

Possible future uses:

```text
Pressure confirmation multiplier
Pressure divergence warning
Exhaustion target-management warning
Envelope target selection
Envelope invalidation selection
```

Each should be tested independently against the existing baseline.

---

# 30. How these features should eventually work together

The intended model is:

```text
                  OPTION ENVELOPES
             structural / flow location
                         |
                         v
                 Where can price react?
                         |
                         v
               EST. HEDGE PRESSURE
             directional flow / momentum
                         |
                         v
              Is options flow pushing
              with or against price?
                         |
                         v
                 OPTION EXHAUSTION
          pressure + price + envelope context
                         |
                         v
             Is the current push weakening?
```

That gives PublicGex three separate analytical dimensions:

```text
STRUCTURE
Envelopes

FLOW
Estimated Hedge Pressure

TRANSITION
Option Exhaustion
```

This separation is preferable to combining everything immediately into one score.

---

# 31. Data-source caveats that must remain visible

## Public.com OI

Treat reported OI as structural OI.

Do not call it real-time dealer OI.

DeepCharts Advanced Envelopes uses CBOE-derived real-time OI-R.

PublicGex cannot reproduce that exactly with the current feed.

## Trade direction

PublicGex does not receive definitive buyer/seller aggressor classification for every option print.

Bid/ask/last classification is an estimate.

## Dealer identity

PublicGex does not know whether the opposing participant is a market maker.

Therefore Pressure is:

```text
estimated hedge pressure
```

not observed market-maker pressure.

## Opening vs closing

Volume may represent:

```text
opening
closing
rolling
same contracts changing hands repeatedly
```

Do not infer Delta OI from Delta Volume.

## Snapshot granularity

A cumulative volume increase between two snapshots can contain trades on both sides.

The current last price cannot identify every trade inside the interval.

This is the largest Pressure limitation.

## Strike coverage

The current +/-3% storage universe can truncate Envelopes.

Surface this explicitly until the collector window is safely separated from legacy calculation semantics.

---

# 32. Acceptance criteria

The feature set is ready for a first research release when:

## Envelopes

- Flow and Structural modes work.
- Zero/Major/Minor levels are returned.
- coverage and edge warnings are visible.
- TRACE and Cockpit render the levels.
- existing Gamma Sweep values are unchanged in default mode.

## Pressure

- cumulative session line renders.
- EMA15/EMA30 render.
- classified vs unclassified flow is exposed.
- missing data stays null.
- volume resets/gaps do not create fake impulses.
- session reset is correct.
- feature is clearly labeled estimated.

## Exhaustion

- four inferred option-side/action markers work.
- 5-bar 70/30 filter works.
- sensitivity presets work.
- every marker exposes evidence.
- TRACE replay is deterministic.
- insufficient data produces no marker, not a false zero.

## Regression

- all current backend tests pass.
- all current TypeScript tests pass.
- all browser workflows pass.
- native Rust lifecycle tests pass.
- existing decision outputs remain unchanged.

Current verification commands:

```powershell
npm run build
npm test
npm run test:backend
npm run test:e2e
npm run native:test
```

---

# 33. Final recommendation

The updated repository changes the best implementation path.

Earlier, Pressure would have required substantial collector/schema work.

It no longer does.

The project already has:

```text
volume snapshots
quote state
contract delta
contract gamma
spot history
activity windows
coverage semantics
TRACE replay
historical validation infrastructure
```

So the work should focus on **deriving better information from data already being collected**, not on adding another feed or rebuilding the pipeline.

Recommended priority:

```text
1. Envelopes
   Lowest risk, strongest data fidelity.

2. Estimated Hedge Pressure
   High potential value, but inference quality must be measured.

3. Exhaustion
   Build on Pressure rather than independently.

4. Shadow validation
   Keep all three observational.

5. Signal integration
   Only after holdout evidence.
```

The most important architectural choice is to preserve the current decision engine as the baseline while these features accumulate evidence. That lets PublicGex determine whether the new signals actually add predictive value rather than assuming that a DeepCharts-like indicator automatically improves the existing model.
