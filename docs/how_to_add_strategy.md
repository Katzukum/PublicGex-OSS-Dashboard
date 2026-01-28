# How-To: Add a New Strategy

This guide explains how to extend the "Market Compass" logic in `appy.py`.

## The Compass Logic
The Dashboard determines the market regime based on two factors:
1.  **Volatility (X-Axis)**: Is the market Gamma Positive (Stability) or Negative (Velocity)?
2.  **Trend (Y-Axis)**: Is the price above (Bullish) or below (Bearish) the Gamma Flip level?

## Adding a New Regime

1.  Open `appy.py`.
2.  Navigate to `get_market_overview()`.
3.  Locate the quadrant logic (around line 280):

```python
if is_pos_gex and is_bull_trend:
    base_label = "GRIND UP"
    base_strategy = "Buy Calls / Sell Put Spreads."
```

4.  Add your custom logic or sub-conditions. For example, check `total_net_gex` magnitude to distinguish between "Slow Grind" and "Rocketship".

## Adding New Symbols

1.  Open `settings.json`.
2.  Add the symbol to the `symbols` list (to track data).
3.  Add the symbol to the `weights` dictionary (to influence the Compass).

```json
{
  "symbols": ["SPY", "QQQ", "TSLA"],
  "weights": {
    "SPY": 1.0,
    "QQQ": 0.5,
    "TSLA": 0.1
  }
}
```

> [!WARNING]
> Increasing the number of symbols increases the time the Data Collector takes to complete a loop. Ensure `API_RATE_LIMIT` in `.env` is high enough.
