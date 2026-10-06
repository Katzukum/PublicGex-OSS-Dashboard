# Optional NinjaTrader indicator

`PublicGexDashboard.cs` is a separate indicator for this application. Its class,
generated helpers and cache folder are distinct from the original `OpenGamma`
indicator. Both use the original NinjaTrader TCP port **5010** and schema-v2 feed.

## Installation and connection

1. Copy `PublicGexDashboard.cs` into your NinjaTrader 8
   `Documents\NinjaTrader 8\bin\Custom\Indicators` folder as a **new file**.
   Keep any existing `OpenGamma.cs` file and indicator unchanged.
2. Open NinjaTrader's NinjaScript Editor and compile the new indicator.
3. Add **PublicGex Dashboard** to the desired chart and leave its Listen Port
   at **5010**. An existing OpenGamma indicator already using this port can receive
   the dashboard's compatible feed without installing the separately named copy.
4. Open the new dashboard in live mode. Its NinjaTrader bridge starts automatically
   on port **5010**. Settings shows its status and allows manual stop/start or
   disabling automatic startup. The bridge cannot start in demo mode.

The protocol remains UTF-8, newline-delimited schema-v2 JSON over loopback TCP.
Port 5005 is the original app's internal collector event port, not a NinjaTrader
listener. This dashboard uses a private dynamic port for collector events.
If another application already owns port 5010, Settings reports the conflict;
that application must release the port before this bridge can start.
Synthetic demo data cannot be broadcast to trading charts.

Despite the setting name **Listen Port**, the indicator is a TCP **client**:
it connects to the dashboard's server at `127.0.0.1:5010`. It retries a lost
connection every five seconds.

## How to read the indicator

The display has three parts: horizontal gamma bars at price levels, a magenta
exposure profile with an orange modeled-zero line, and a dashboard panel at the
bottom left. Read the panel for the model's context, then locate the relevant
levels on the chart. The indicator displays analytics; it does not place orders.

### Check the instrument and price units first

| NinjaTrader chart | Index data used |
| ----------------- | --------------- |
| ES / MES          | SPX             |
| NQ / MNQ          | NDX             |

These are the automatic mappings implemented in this copy. Other instruments do
not have a supported automatic mapping. The chart instrument selects its index
fields from the feed; the dashboard's selected instrument does not turn an ES
chart into an NDX chart.

**The text labels use index prices, while the plotted levels use futures prices.**
The indicator estimates the difference using a smoothed spread:

```text
Raw spread = index spot - futures reference price
Chart level = index level - smoothed spread
```

For example, with SPX at 6,000 and the futures reference at 6,025, a settled
spread of -25 places the SPX 6,010 bar at 6,035 on the ES price axis. The text
beside the bar still reads `6010`. The orange line uses the same conversion.

The bottom panel's **Target**, **Invalid**, and **Flip** values also remain in
index units; they are not converted futures order prices. Labels are rounded to
whole points, so use the chart axis for the plotted futures location. Because
the spread is smoothed, the alignment can lag a sudden change in the basis.

### Read the gamma bars and profile

| Visual                              | Meaning in this indicator                                                                                              |
| ----------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Red horizontal bar                  | Positive net gamma exposure at that strike. The code calls this a resistance level.                                    |
| Green horizontal bar                | Non-positive net gamma exposure at that strike, normally negative. The code calls this a support level.                |
| Filled bar                          | A key level selected by the backend.                                                                                   |
| Hollow outline                      | Another profile level retained for context.                                                                            |
| Longer bar                          | Larger absolute **net** GEX, relative to the largest net GEX in the received profile.                                  |
| Magenta curve                       | Signed net GEX across price levels. Positive values extend right of its neutral position; negative values extend left. |
| Orange dotted line, `Modeled 0 GEX` | The nearest available modeled zero-gamma crossing from Gamma Sweep, converted to the futures price axis.               |

Bar colors encode the GEX sign, not whether the level is above or below current
price. A red bar can be below price and a green bar can be above it. The
support/resistance names are the indicator's conventions, not a promise that
price will bounce or a buy/sell instruction.

Key-level selection takes up to five strong strikes below spot and five at or
above spot. Strength considers the largest absolute net, call, or put exposure
at each strike. Consequently, a filled bar can be narrow when large call and
put exposures largely cancel in net GEX. Filled does not mean nearest to price.

Widths rescale with each received profile, and very small bars have a minimum
visible width. Compare widths within the current profile rather than treating
pixels as a fixed dollar scale. Bar height is a fixed drawing size, not the
width of a price zone. The magenta curve is an exposure profile across strikes,
not a price forecast through time.

The orange line is shown only when a valid modeled crossing is available and
its converted price is visible. It can differ from the panel's **Flip**: Flip
comes from the stored strike profile and may use a proxy when a direct crossing
is unavailable; Gamma Sweep reprices the chain across hypothetical spot prices.

### Read the bottom-left dashboard

| Field                      | How to interpret it                                                                                                                                                                                                       |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Symbol and context         | The underlying index and a short description of the current model setup.                                                                                                                                                  |
| Bias and signed percentage | The model's directional score, shown as a percentage: positive favors the call/upside side; negative favors the put/downside side. `WAIT` means the model has no active directional setup. This is not a win probability. |
| Data Q                     | A heuristic assessment of input completeness, freshness, and flip quality. It is not trade confidence or a win rate. Before usable data arrives, this can show `0%`.                                                      |
| Target                     | The current model's directional objective in **index points**. `--` means unavailable.                                                                                                                                    |
| Invalid                    | The model's failure/invalidation reference in **index points**. This is a displayed reference, not a placed stop order.                                                                                                   |
| Flip                       | The stored profile's gamma flip or proxy, in **index points**. It is separate from the orange modeled-zero line.                                                                                                          |
| Market Signal              | The combined directional vote from the market compasses.                                                                                                                                                                  |
| Dealer Positioning         | The model's dealer-positioning vote, such as Long Gamma, Short Gamma, or Mixed Gamma. It incorporates spot versus flip and effective gamma; it is not a direct observation of dealer holdings.                            |
| Gamma Liquidity            | A model of the room/asymmetry around nearby gamma walls. It does not measure NinjaTrader's order book or available bid/ask size.                                                                                          |
| Index Gamma Basket         | The combined index-basket regime, using the configured weights. Defaults are SPX 45%, NDX 35%, and IWM 20%; this is not a feed of observed whale trades.                                                                  |
| Empirical Edge             | Historical outcomes for the displayed setup or fallback group; read the source label and sample count as well as the percentage.                                                                                          |

The bias number is green above +5%, red below -5%, and amber between those
thresholds or when missing. Color uses the underlying unrounded score. Those
are **display-color thresholds**. A green
number can still accompany `WAIT`; color alone does not activate a setup.
The four context tiles reuse the bias color, so read their text rather than
treating each tile's color as an independent signal. Target, Invalid, and Flip
are panel values; this copy does not draw separate target or stop lines.

The bridge derives these model references from the market overview. Its Target
and Invalid values can differ from the Cockpit's separate scenario-engine
calculations; check which view supplied a level when comparing the two.

An edge summary such as `30m 60% n=20 med +8` means:

- `30m`: the evaluated outcome horizon is 30 minutes.
- `60%`: the historical win rate in that group. A target reached first wins;
  invalidation reached first loses. If neither is hit, a favorable
  direction-adjusted move at the horizon counts as a win.
- `n=20`: 20 independent labeled outcomes in the selected group.
- `med +8`: the median direction-adjusted move at the 30-minute horizon is
  +8 underlying **index points**. This is not the maximum favorable excursion,
  option profit, futures P&L, or dollars.

The source indicates whether the history matches the specific setup or falls
back to a broader symbol/bias group. `30m no labeled samples` means no usable
outcomes are available. Small samples and the absence of costs or uncertainty
intervals in this compact panel limit what the displayed win rate tells you;
use the dashboard's Edge Lab for the fuller evidence.

### Know whether the display is current

Gamma levels are cached in `PublicGexDashboardCache` under NinjaTrader's user
data directory. Cached bars and the modeled-zero line can appear immediately
after loading the indicator, before a live connection. Existing drawings can
also remain after the connection is lost. Their presence does not establish
that collection is running or that the market data is fresh.

Check the dashboard's collector status, data age, and NinjaTrader bridge status.
In NinjaTrader's NinjaScript Output, look for `Connected to Server on port 5010`
and subsequent `PublicGexDashboard:` update messages showing index/futures
prices, raw/JMA spread, and level count. A connected socket alone does not make
an old market snapshot current. `WAITING`, `--`, a zero Data Q, or a cache-load
message are reasons to check the data source before interpreting the display.

### Display and spread settings

| Setting               | Default | What it changes                                                                                                                |
| --------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------ |
| Listen Port           | 5010    | The dashboard server port to connect to.                                                                                       |
| Gamma Bars On Right   | Off     | Moves the bars and their strike labels to the right edge. The magenta curve and bottom panel remain on the left.               |
| Time Series (Minutes) | 2       | The secondary futures minute series used as the spread's price reference.                                                      |
| Length - JMA          | 13      | Length parameter of the spread smoothing filter.                                                                               |
| Phase - JMA           | 78      | Phase parameter of that filter.                                                                                                |
| Power - JMA           | 2       | Power parameter of that filter.                                                                                                |
| Reset Threshold - JMA | 25      | Resets the filter when the raw spread differs from its smoothed value by more than this many points. Zero disables this reset. |

The filter updates when dashboard messages arrive, using the latest reference
price from the configured secondary series. These controls affect index-to-
futures alignment; they do not recalculate the option-chain GEX or change the
dashboard's directional score.

## Verification status

This copy has not been compiled or exercised inside a live NinjaTrader session.
The application's bridge/protocol tests do not replace that optional platform
verification. No installed NinjaTrader files have been changed by this project.
