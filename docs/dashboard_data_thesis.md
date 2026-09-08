# OpenGamma Dashboard Data Thesis

## Title

Translating 0DTE Option Gamma Exposure into Market Regime, Liquidity, and Intraday Decision Context

## Abstract

The OpenGamma dashboard is a market structure tool built around one central thesis: very short-dated option positioning, especially same-day expiration option gamma, can help describe the intraday environment that price is moving through. The dashboard does not try to forecast price in the traditional sense. Instead, it attempts to measure the shape of dealer hedging pressure around the current market price, then translate that structure into practical context: whether the market is more likely to compress or expand, where important gamma levels sit, whether price is above or below the gamma flip, and whether the current setup favors call bias, put bias, or waiting.

The data foundation is 0DTE option chain data from Public.com for the configured symbols: SPY, QQQ, IWM, SPX, and NDX. For each collection run, the system stores a raw contract-level record for each relevant option, a symbol-level snapshot summarizing gamma exposure, and a collection-level audit record. The dashboard then uses those records to construct gamma profiles, market compasses, strike matrices, cockpit signals, and NinjaTrader chart levels.

The practical idea is simple but powerful: if dealers are effectively long gamma, hedging flows tend to dampen price movement and support mean reversion. If dealers are effectively short gamma, hedging flows tend to chase price movement and support expansion. The dashboard expresses this idea through net gamma exposure, gamma flip distance, gamma slope, significant strike walls, and weighted market composites.

This paper explains what data is being examined, how the data is transformed, what assumptions are embedded in the model, and how those assumptions are used to produce the dashboard's final market interpretation.

## Core Thesis

The dashboard treats the 0DTE option chain as a real-time map of intraday pressure. The central claim is not that option gamma alone predicts the next price tick. The stronger and more useful claim is that the current gamma structure can describe the conditions under which price movement is happening.

In this framework:

- Positive net gamma exposure implies a compression environment. Price movement is more likely to encounter stabilizing flows, mean reversion, and resistance to runaway trend.
- Negative net gamma exposure implies an expansion environment. Price movement is more likely to encounter reinforcing flows, wider ranges, and momentum continuation.
- The gamma flip is the boundary where the cumulative gamma profile changes sign. Spot price above or below that boundary is treated as a directional context input.
- Large positive or negative gamma clusters are treated as liquidity landmarks. They may behave like resistance, support, magnets, acceleration points, or invalidation areas depending on sign, location, and market regime.
- A single symbol can be noisy, so the dashboard blends symbol-level readings into weighted composites for "Traders" and "Whale" market views.
- The final cockpit signal is a synthesis of broad market pressure, selected-symbol dealer positioning, and local liquidity room.

The dashboard is therefore best understood as a market regime and liquidity interpretation engine. It is not a black-box trading system. It is a structured way to answer: "What kind of market am I trading in right now, and where are the meaningful gamma levels around price?"

## Data Being Examined

### 1. Underlying Symbols

The configured symbol universe currently includes:

- SPY
- QQQ
- IWM
- SPX
- NDX

These symbols are not treated as interchangeable. They represent different views of market participation.

SPY, QQQ, and IWM are used as the primary "Traders" basket. This basket emphasizes ETF option positioning and is meant to represent the more accessible, retail and active-trader layer of market gamma.

SPX, NDX, and IWM are used as the primary "Whale" basket. This basket emphasizes index option positioning and is meant to represent larger institutional or index-level pressure. IWM appears in both baskets because small-cap risk often helps describe breadth, risk appetite, and whether the broader market is participating.

Current default weights are:

| Basket | Symbol | Weight |
| --- | ---: | ---: |
| Traders | SPY | 0.50 |
| Traders | QQQ | 0.30 |
| Traders | IWM | 0.20 |
| Whale | SPX | 0.45 |
| Whale | NDX | 0.35 |
| Whale | IWM | 0.20 |

The weights are normalized when the composite score is calculated. Their job is to say how much each symbol should influence the broader market reading.

### 2. Public.com Market Data

For every configured symbol, the collector asks Public.com for:

- Current underlying quote or spot price
- Available option expirations
- Option chain for the target expiration
- Contract symbols
- Strike prices
- Option type, meaning call or put
- Open interest
- Greeks, mainly gamma, delta, and theta

Gamma is the most important Greek in this system. Delta and theta are stored, but the current dashboard thesis is primarily gamma-driven.

The collector is strict about the target expiration. Before 6:00 PM local time, the target date is the current trading date. At or after 6:00 PM, the target rolls forward to the next weekday. If Public.com does not return an expiration for that target date, the symbol is skipped instead of silently falling back to a later weekly or monthly expiration.

That matters because the dashboard is explicitly focused on 0DTE structure. Mixing a same-day SPY chain with a later-dated SPX chain would weaken the model. The current design chooses missing data over inconsistent expiration horizons.

### 3. Contract-Level Data

Each raw option record represents one option contract at one collection timestamp. The important fields are:

- Symbol
- Expiration date
- OSI option symbol
- Strike price
- Option type
- Delta
- Gamma
- Open interest
- Underlying price
- Calculated gamma exposure value

This raw contract table is the most granular layer of the system. It allows the dashboard to rebuild the option profile by strike, separate calls from puts, calculate net strike exposure, locate magnets, estimate the gamma flip, and draw the gamma liquidity profile.

### 4. Snapshot-Level Data

For each symbol in each collection run, the dashboard stores a summary snapshot. The key snapshot fields are:

- Spot price
- Total net GEX
- Total call GEX
- Total put GEX
- Max call GEX strike
- Max put GEX strike
- Gamma flip strike
- Effective GEX
- Total gamma
- Total theta

The snapshot is the main time-series layer. It lets the dashboard show how net GEX changes over time, whether a symbol is becoming more positive or negative gamma, and whether the broad regime is strengthening or weakening.

### 5. Collection Runs

Each full polling cycle creates a collection run. The run records:

- When the run started
- When it finished
- Whether it succeeded or failed
- Which symbols were requested
- Which symbols succeeded
- Which symbols failed
- Which symbols were skipped

This matters because market data systems often fail partially. A useful dashboard should distinguish "SPY updated but NDX skipped" from "everything updated cleanly." The run record gives the data a provenance trail.

### 6. Current Local Database Shape

When inspected on June 10, 2026, the local database contained:

| Table | Row Count |
| --- | ---: |
| collection_runs | 156 |
| gex_snapshots | 758 |
| raw_option_greeks | 86,559 |

Snapshot timestamps ranged from June 8, 2026 at 21:01:03 to June 9, 2026 at 23:28:17. The latest stored rows were distributed across SPY, QQQ, IWM, SPX, and NDX. This confirms that the system is not only displaying a single live sample. It is retaining enough recent history to compare the latest gamma profile against prior snapshots.

## How Raw Data Becomes Gamma Exposure

The dashboard converts each option contract into a dollar gamma exposure estimate. The formula is:

```text
GEX_i = gamma_i * open_interest_i * contract_multiplier * spot_price^2 * 0.01 * sign_i
```

Where:

- `gamma_i` is the option gamma for contract `i`.
- `open_interest_i` is the number of open contracts.
- `contract_multiplier` is 100 shares per options contract.
- `spot_price^2` converts gamma sensitivity into underlying dollar exposure.
- `0.01` expresses the exposure for a 1 percent move in the underlying.
- `sign_i` is positive for calls and negative for puts.

In simplified terms, this asks: "For a 1 percent move in the underlying, how much gamma exposure does this contract represent?"

The sign convention is central:

```text
Call GEX = positive
Put GEX = negative
Net GEX = Call GEX + Put GEX
```

This is a modeling convention. It does not observe the true owner of every option contract. Instead, it uses the common gamma-dashboard convention that call-side exposure contributes positive gamma pressure and put-side exposure contributes negative gamma pressure. From this convention, positive net GEX is interpreted as stabilizing or compressive, while negative net GEX is interpreted as destabilizing or expansionary.

## Strike Filtering and Effective GEX

The system does not use every option contract in the full chain. It filters to strikes close to the current spot price.

The main collection range is:

```text
spot_price * 0.97 <= strike <= spot_price * 1.03
```

In other words, the dashboard collects strikes within approximately 3 percent of spot.

The effective GEX range is even tighter:

```text
spot_price * 0.98 <= strike <= spot_price * 1.02
```

This effective range focuses on the gamma most likely to matter immediately. For 0DTE options, near-the-money gamma dominates because expiration is close and gamma is highly concentrated around current price.

The assumption is that far out-of-the-money 0DTE strikes are less relevant to immediate intraday hedging unless price moves sharply toward them. This keeps the model focused on actionable intraday structure rather than distant tail strikes.

## Aggregating Gamma by Strike

After each contract receives a GEX value, contracts are grouped by strike:

```text
Net GEX at strike K = sum(GEX_i for all contracts with strike K)
```

This creates a signed gamma profile. Each strike can then be interpreted by its net sign and magnitude:

- Positive net strike GEX is treated as a stabilizing or resistance-like gamma area.
- Negative net strike GEX is treated as a volatility or acceleration-prone area.
- The largest absolute net strike GEX is treated as the current gamma magnet.

The magnet is defined as:

```text
Magnet strike = strike K where abs(Net GEX at K) is largest
```

This does not mean price must go to that strike. It means that strike is the most dominant gamma concentration in the scanned profile. If that dominant strike changes from one snapshot to the next, the dashboard emits a magnet-change event.

## Total Call, Put, Net, Gross, and Effective GEX

The dashboard summarizes the profile several ways.

Total call GEX:

```text
Total Call GEX = sum(positive call-side GEX)
```

Total put GEX:

```text
Total Put GEX = sum(negative put-side GEX)
```

Total net GEX:

```text
Total Net GEX = Total Call GEX + Total Put GEX
```

Gross GEX:

```text
Gross GEX = abs(Total Call GEX) + abs(Total Put GEX)
```

Effective GEX:

```text
Effective GEX = sum(GEX_i for strikes within 2 percent of spot)
```

Net GEX tells us the directional sign of the gamma environment. Gross GEX tells us how much total gamma is present, regardless of sign. Effective GEX tells us how much of that exposure is close enough to spot to matter immediately.

This distinction is important. A symbol can have a large total profile but a weaker immediate effect if most of the exposure sits far away from spot. Conversely, a smaller total profile can matter a lot if it is tightly concentrated around current price.

## The Gamma Flip

The gamma flip is the level where cumulative signed gamma changes sign. It is also called the zero gamma level.

The dashboard calculates it by sorting strikes from low to high, then adding net GEX cumulatively:

```text
Cumulative GEX at K_j = sum(Net GEX at K_i for all K_i <= K_j)
```

The flip is found where cumulative GEX crosses zero. If cumulative GEX moves from negative to positive, or from positive to negative, the crossing is interpolated between the two surrounding strikes.

If the previous strike is `K_prev`, the current strike is `K_curr`, the previous cumulative value is `C_prev`, and the current cumulative value is `C_curr`, then:

```text
flip = K_prev + abs(C_prev) / abs(C_curr - C_prev) * (K_curr - K_prev)
```

This interpolation prevents the flip from being forced onto a listed strike. The model recognizes that the zero crossing may lie between strikes.

The flip is one of the most important levels in the dashboard because it provides the directional half of the market compass. Spot above the flip is treated as bullish pressure. Spot below the flip is treated as bearish pressure. The farther spot is from the flip, the stronger the trend score, up to a capped limit.

## Flip Quality

The system recognizes that not every flip estimate has equal quality.

The best case is a true cumulative zero crossing inside the observed strike range. This receives the highest flip-quality weight.

If no crossing exists, the dashboard can use a lower-quality proxy. The proxy is selected from the strongest opposite-sign gamma cluster. This is saying: "There is no clean zero crossing, but this strike is the most meaningful opposing force in the current profile."

If there is no zero crossing and no opposing cluster, the system can fall back to an edge estimate, which carries a much lower quality weight. An edge estimate means the observed strike window may not contain enough information to locate the real flip.

The practical assumption is:

- Crossing flip: strong reference level
- Proxy flip: usable but uncertain
- Edge flip: weak reference level
- Missing flip: no reliable level

The dashboard lowers Data Quality when the flip is approximate.

## Gamma Slope

Gamma slope measures how quickly net GEX changes around the current price. It is calculated from the two nearest strikes around spot:

```text
GEX slope = (GEX_2 - GEX_1) / (K_2 - K_1)
```

Where `K_1` and `K_2` are the nearest surrounding strikes and `GEX_1` and `GEX_2` are their net gamma exposures.

Slope matters because the level of gamma is not the whole story. The shape of the profile matters too. A steep slope near spot means the hedging environment can change rapidly as price moves. A flatter slope means the local environment is more stable.

In the dashboard, this appears as "GEX Slope" or "Gamma Slope." In NinjaTrader, the gamma profile and nearby slope are also used to describe whether the local structure leans bullish, bearish, or neutral.

## Market Compass

The Market Compass is the dashboard's main regime model. It uses two axes:

```text
X-axis = volatility/compression score
Y-axis = trend score
```

The x-axis answers: "Is the gamma environment stabilizing or expansionary?"

The y-axis answers: "Is spot above or below the gamma flip, and by how much?"

Together, these create four regimes:

| Volatility Score | Trend Score | Regime | Interpretation |
| ---: | ---: | --- | --- |
| Positive | Positive | Grind Up | Positive gamma with spot above flip. Controlled upside and pullback mean reversion are favored. |
| Negative | Positive | Melt Up | Negative gamma with spot above flip. Upside momentum and range expansion are more likely. |
| Positive | Negative | Support / Chop | Positive gamma with spot below flip. Range discipline and mean reversion matter. |
| Negative | Negative | Crash / Flush | Negative gamma with spot below flip. Downside expansion and persistent momentum risk increase. |

These labels should be read as environment labels, not promises. "Crash / Flush" does not mean the market must crash. It means the measured gamma structure is more vulnerable to downside expansion if price confirms in that direction.

## Volatility/Compression Score

The volatility score is based on net GEX relative to gross GEX:

```text
imbalance = Total Net GEX / Gross GEX
```

This value is bounded between -1 and +1 because gross GEX is the absolute total exposure.

The dashboard then smooths the imbalance with a hyperbolic tangent function:

```text
vol_score = tanh(2 * imbalance)
```

This has two advantages:

1. It preserves the sign of the gamma environment.
2. It prevents extremely one-sided readings from creating unlimited scores.

Positive volatility score means positive gamma/compression. Negative volatility score means negative gamma/expansion.

## Trend Score

The trend score is based on the distance between spot and the gamma flip:

```text
distance_pct = (spot - flip) / flip
```

The dashboard scales that distance by symbol-specific sensitivity:

```text
trend_score = distance_pct / sensitivity
```

Then the score is clamped between -1 and +1 and multiplied by the flip-quality weight.

Current sensitivity values are:

| Symbol | Sensitivity |
| --- | ---: |
| SPY | 0.20 percent |
| SPX | 0.20 percent |
| QQQ | 0.35 percent |
| NDX | 0.30 percent |
| IWM | 0.15 percent |
| Default | 0.25 percent |

These sensitivities encode the assumption that different products need different movement thresholds before the distance from flip becomes meaningful. QQQ and NDX are treated as naturally noisier than SPY/SPX. IWM is treated as more sensitive.

If spot is above flip, the trend score is positive. If spot is below flip, the trend score is negative. If spot is very close to flip, the trend score is near zero.

## Weighted Market Composites

Each basket score is the weighted average of its components.

For volatility:

```text
basket_x = sum(vol_score_symbol * weight_symbol) / sum(weight_symbol)
```

For trend:

```text
basket_y = sum(trend_score_symbol * weight_symbol) / sum(weight_symbol)
```

The dashboard calculates this separately for the Traders basket and the Index Gamma Basket.

The Traders basket asks: "What does ETF-linked gamma structure say about the market?"

The Index Gamma Basket asks: "What does larger index-linked gamma structure say about the market?"

When both baskets agree, the dashboard treats the market read as stronger. When they disagree, the cockpit becomes more cautious.

## Data Quality Model

The dashboard does not treat every reading as equally trustworthy. Data Quality begins at 1.0 and is reduced for known weaknesses. It is a freshness/completeness score, not a probability that a trade will win.

Data Quality is reduced when:

- The option profile has fewer than 20 rows.
- Gross GEX is missing or zero.
- The flip is only a proxy.
- The flip is at the edge of the observed range.
- The flip is missing.
- The snapshot is aging.
- The snapshot is stale.

The time freshness assumptions are:

- Older than 7 minutes: aging snapshot
- Older than 15 minutes: stale snapshot

The final Data Quality label is:

| Data Quality Score | Label |
| ---: | --- |
| Below 0.45 | Low |
| 0.45 to 0.70 | Medium |
| Above 0.70 | High |

The compass also checks regime separation:

```text
magnitude = sqrt(x_score^2 + y_score^2)
```

If magnitude is below 0.25, the dashboard treats the regime as poorly separated. That means the compass point is near the center, and the regime label should be read cautiously.

## Cockpit Signal

The cockpit is the most condensed view. It tries to answer: "For the selected symbol, are the current inputs aligned enough to prefer call bias, put bias, or wait?"

It combines three pillars:

- Market Signal
- Dealer Positioning
- Gamma Liquidity

The weights are:

```text
raw_score = 0.45 * market_vote + 0.35 * dealer_vote + 0.20 * liquidity_vote
```

The server-side scenario engine adjusts the raw regime score by agreement and Data Quality:

```text
final_score = raw_score * agreement_multiplier * component_data_quality
```

The agreement multiplier starts at 0.65 and increases when the pillars agree with the final direction:

```text
agreement_multiplier = clamp(0.65 + 0.12 * agreeing_pillars, 0.65, 1.00)
```

The final interpretation is:

| Final Score | Cockpit Bias |
| ---: | --- |
| Greater than 0.22 | Call Bias |
| Less than -0.22 | Put Bias |
| Between -0.22 and +0.22 | Wait |

The key design choice is that the cockpit requires alignment. A single bullish input should not dominate the full signal if the dealer structure and liquidity map disagree.

## Market Signal Pillar

The Market Signal pillar blends the Traders and Index Gamma Basket trend scores using their Data Quality levels.

Conceptually:

```text
market_vote = Data-Quality-weighted average of Traders y-score and Index Basket y-score
```

If the Traders and Index Gamma baskets both point upward with good Data Quality, market vote becomes more positive. If both point downward, it becomes more negative. If they disagree, the result moves toward neutral.

This pillar is directional, but it is still regime-based. It does not say "buy calls because the market is up." It says the broad gamma regime is tilted in a call-supportive or put-supportive direction.

## Dealer Positioning Pillar

The Dealer Positioning pillar focuses on the selected symbol.

It calculates local net gamma inside 2 percent of spot. If local net gamma is negative, the dashboard treats the dealer environment as short gamma and gives the spot-versus-flip direction full weight. If local net gamma is positive, the dashboard treats the dealer environment as long gamma and dampens the directional score.

The reason is that negative gamma environments are more likely to amplify movement, while positive gamma environments are more likely to dampen movement.

The direction is scaled by distance from flip:

```text
direction = clamp((spot - flip) / (flip * 0.006), -1, 1)
```

Then:

```text
dealer_score = direction * gamma_multiplier
```

Where:

- `gamma_multiplier = 1.00` when local net gamma is negative
- `gamma_multiplier = 0.45` when local net gamma is positive

This encodes a very important assumption: being above the flip matters more when gamma is negative because movement can expand. In positive gamma, the same directional location is more likely to be moderated.

## Gamma Liquidity Pillar

The Gamma Liquidity pillar asks where price has room to move.

The dashboard identifies significant gamma levels using a threshold:

```text
significant level = abs(strike_net_gex) >= 20 percent of max_abs_strike_gex
```

It then looks for:

- Upside positive-gamma wall
- Downside positive-gamma wall
- Upside negative-gamma acceleration area
- Downside negative-gamma acceleration area

The model scores "room" based on how close the nearest significant level is.

If a wall is very close, room is poor. If no wall is nearby, room is better. The current distance bands are:

| Distance from Spot | Room Interpretation |
| ---: | --- |
| Less than 0.35 percent | Nearby wall, poor room |
| 0.35 percent to 0.75 percent | Moderate friction |
| Greater than 1.5 percent | Open room |
| No level found | Treated as open room |

The liquidity score is:

```text
liquidity_score = call_room - put_room
```

Positive liquidity score means upside appears more open. Negative liquidity score means downside appears more open.

## Target and Invalidation Logic

When the cockpit score is too mixed, the dashboard does not produce a target or invalidation. It shows a no-trade state.

When the score is strong enough, the dashboard classifies the setup as either:

- Expansion
- Mean reversion

Expansion is favored when:

- Local net gamma is negative, or
- Broad market volatility score is meaningfully negative, or
- The market vote strongly agrees with the trade direction

Mean reversion is favored when the environment is more stabilizing and the objective is a return toward flip or a stabilizing gamma level.

For an expansion setup, the target follows open liquidity in the direction of the signal. For a mean-reversion setup, the target tends to be the flip or the next stabilizing gamma level.

Fallback target distances are:

```text
Mean-reversion fallback move = 0.6 percent
Expansion fallback move = 1.2 percent
```

Invalidation is usually selected from the nearest opposing significant level, the flip if it sits on the opposite side, or a 0.6 percent fallback move against the signal.

These levels are not meant to be mechanical trading orders. They are structured reference points.

## Strike Matrix

The Strike Matrix displays the option profile as a table by strike. It shows:

- Strike
- Regime or sentiment tag
- Total net GEX exposure
- Call GEX
- Put GEX
- Open interest

The table marks:

- Magnet: the strike with the largest absolute net GEX
- Stability: positive net GEX
- Volatility: negative net GEX

This view is useful because it exposes the structure behind the summary signal. A trader can see whether the dashboard's read comes from one dominant strike, a balanced profile, or a series of clustered levels.

## Gamma Liquidity Profile Chart

The Gamma Liquidity Profile chart shows:

- Call GEX by strike
- Put GEX by strike
- Net GEX curve
- Current spot price
- In the cockpit, target, flip, and invalidation markers

The profile chart is the visual version of the model's core thesis. It shows where the gamma is, how it is signed, and how current price is positioned relative to that structure.

## Net Gamma Trend

The Net Gamma Trend chart uses recent snapshots to show how total net GEX is changing over time.

This matters because gamma is dynamic. A market that starts the day deeply negative gamma can become more neutral as price moves and positions change. A market that starts positive gamma can lose that cushion if puts become dominant or spot moves away from stabilizing strikes.

The cockpit also shows a recent GEX change value. The current display labels it as "GEX Change (1h)," but the underlying calculation compares the first and last points in the returned recent history window. The conceptual use is still valid: it shows whether net GEX is rising or falling over the recent dashboard window.

## NinjaTrader Use

The dashboard also broadcasts regime and gamma level data to NinjaTrader. The chart indicator receives:

- Market regime
- Regime code
- Data Quality
- X and Y compass scores
- Strategy/context text
- SPY, SPX, and NDX spot and flip values
- Gamma levels for SPX and NDX
- Acceleration values

On the chart, these levels can be drawn as horizontal support/resistance zones, with width scaled by gamma magnitude. Positive GEX levels are treated as resistance-like. Negative GEX levels are treated as support or acceleration-like depending on location and context.

For futures charts, the indicator also accounts for the spread between index price and futures price. This matters because SPX/NDX gamma levels are index levels, while traders often view ES/NQ futures charts. The practical goal is to let an index-derived gamma map appear in the chart space where the trader is actually watching price.

## Main Assumptions

### Assumption 1: Public.com data is accurate and timely

The model assumes the spot price, option chain, open interest, and Greeks returned by Public.com are accurate enough for intraday analysis. If the data is delayed, incomplete, stale, or malformed, the dashboard's conclusions weaken.

### Assumption 2: Open interest is a useful proxy for positioning

The dashboard uses open interest as the size input in the GEX formula. Open interest is not the same as live trade flow. It updates less frequently than price and may not fully reflect intraday opening and closing activity. The assumption is that open interest is still a useful approximation of where option exposure is concentrated.

### Assumption 3: Call gamma is positive and put gamma is negative

The sign convention is a simplifying assumption. The model does not know whether customers are net long or short each option, nor whether dealers are on the other side in the expected size. It uses the convention that calls contribute positive GEX and puts contribute negative GEX.

This is necessary for a practical public-data dashboard, but it is also one of the largest model assumptions.

### Assumption 4: Net GEX maps to compression or expansion

The dashboard assumes positive net GEX corresponds to a more stabilizing hedging environment and negative net GEX corresponds to a more unstable or momentum-reinforcing environment.

This is the core market microstructure thesis. It is directionally useful, but it is not absolute. News, macro events, liquidity shocks, and large underlying flows can overpower the gamma map.

### Assumption 5: Near-spot 0DTE gamma dominates intraday behavior

Because 0DTE gamma is highly concentrated near the money, the dashboard filters to strikes within 3 percent of spot and emphasizes effective GEX within 2 percent of spot.

This assumes the most actionable hedging pressure is close to current price. It intentionally sacrifices some far-tail information in exchange for a cleaner intraday read.

### Assumption 6: The gamma flip is a meaningful boundary

The model assumes that the zero gamma level is a useful boundary between bullish and bearish pressure states. Spot above flip is treated as positive trend pressure. Spot below flip is treated as negative trend pressure.

This is useful when the flip is well estimated. It is weaker when the flip is a proxy, at the edge of the range, or missing.

### Assumption 7: Symbol sensitivities are reasonable

The dashboard uses different flip-distance sensitivities for different symbols. This assumes SPY, SPX, QQQ, NDX, and IWM should not all be judged by the same percentage movement from flip.

These values are model parameters, not universal truths. They can be tuned over time.

### Assumption 8: Weighted baskets represent useful market layers

The Traders and Index Gamma baskets are interpretations. SPY/QQQ/IWM are treated as trader-linked ETF pressure. SPX/NDX/IWM are treated as larger index-linked pressure.

This is a reasonable market structure split, but the weights are subjective. Different users may want different compositions.

### Assumption 9: Significant gamma clusters act as liquidity landmarks

The dashboard treats large strike-level GEX clusters as areas that may affect price behavior. Positive clusters are treated as resistance or stabilizing walls. Negative clusters are treated as volatility or acceleration zones.

This assumes market makers hedge around these levels in a way that becomes visible in price behavior.

### Assumption 10: Data freshness matters

The model assumes 0DTE gamma readings decay quickly if not refreshed. A snapshot older than 7 minutes is considered aging. Older than 15 minutes is considered stale.

This is especially important near market open, market close, major data releases, and fast trend days.

### Assumption 11: The dashboard is a context engine, not an execution engine

The cockpit can display call bias, put bias, target, and invalidation. These should be treated as structured context, not automatic trade instructions. The dashboard still expects price confirmation, risk control, and trader judgment.

## What the Dashboard Is Actually Doing

At a high level, the dashboard is doing five things.

First, it collects the current 0DTE option structure for the selected symbols.

Second, it converts contract-level Greeks and open interest into signed dollar gamma exposure.

Third, it organizes that exposure by strike and by symbol, producing gamma profiles, total GEX, gamma flips, magnets, effective GEX, and slopes.

Fourth, it converts the profiles into interpretable market states: compression versus expansion, bullish versus bearish trend pressure, Data Quality, and regime labels.

Fifth, it synthesizes the broad market regime, selected-symbol dealer state, and local liquidity map into a cockpit bias.

In plain English, the dashboard is asking:

- Where is the gamma?
- Is it positive or negative?
- Is spot above or below the zero gamma boundary?
- Are large gamma levels blocking price or leaving room?
- Do ETF and index baskets agree?
- Is the data fresh enough to trust?
- Is the final read strong enough to act on, or is waiting the cleaner decision?

## Practical Interpretation

### Positive Gamma Above Flip: Grind Up

This is a constructive but controlled regime. The market is above its flip level and net gamma is positive. The dashboard interprets this as upside with stabilizing flows. Pullbacks may mean revert. Breakouts may be slower and more controlled.

The practical behavior is often "up, but sticky."

### Negative Gamma Above Flip: Melt Up

This is a more aggressive upside regime. The market is above flip, but net gamma is negative. The dashboard interprets this as upside direction with expansion risk. If price keeps moving higher, hedging flows may reinforce the move.

The practical behavior is often "do not fade too early."

### Positive Gamma Below Flip: Support / Chop

This is a mixed regime. Net gamma is stabilizing, but spot is below flip. The dashboard interprets this as a market that may chop, mean revert, or attempt to stabilize after weakness.

The practical behavior is often "range discipline matters."

### Negative Gamma Below Flip: Crash / Flush

This is the most fragile downside regime. Spot is below flip and net gamma is negative. The dashboard interprets this as downside direction with expansion risk. If price keeps moving lower, hedging flows may reinforce selling pressure.

The practical behavior is often "momentum can persist."

## Limitations

The dashboard has several important limitations.

It does not observe actual dealer inventory. It infers pressure from option type, gamma, and open interest.

It does not directly model intraday volume, trade direction, bid/ask imbalance, dealer-to-customer flow, or dark pool activity.

It does not guarantee that a gamma wall will hold or that a negative gamma area will accelerate. Price can ignore gamma levels when macro, news, liquidity, or large directional flows dominate.

It uses a limited strike range. This improves focus but can miss far-tail exposures that become relevant during large moves.

It depends on data freshness. A 0DTE dashboard is only as good as its latest snapshot.

It uses configurable weights and thresholds. These are thoughtful model choices, but they are not immutable laws.

It treats same-day expiration as the correct horizon. That is appropriate for intraday gamma pressure, but it does not capture longer-dated positioning that may matter for swing trading.

## Conclusion

The OpenGamma dashboard is a structured attempt to turn 0DTE option gamma into a practical map of intraday market conditions. It begins with public option chain data, calculates signed gamma exposure, aggregates that exposure by strike and symbol, estimates the gamma flip, measures local slope and liquidity, then blends those readings into market regimes and cockpit signals.

The core thesis is that 0DTE gamma does not need to predict price directly to be valuable. Its value is in describing the environment: whether price is likely trading through compression or expansion, where hedging pressure may concentrate, whether broad market baskets agree, and whether the selected symbol has enough directional alignment to justify a bias.

The dashboard is strongest when data is fresh, the flip is a true crossing, gross GEX is meaningful, and the Traders and Whale composites agree. It is weakest when data is stale, the profile is thin, the flip is only a proxy, or the market is being driven by forces outside the option chain.

Used correctly, the dashboard is not a replacement for judgment. It is a disciplined lens. It turns raw 0DTE option data into a repeatable framework for understanding market pressure, liquidity, and regime.
