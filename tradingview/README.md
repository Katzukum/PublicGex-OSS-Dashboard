# OpenGamma for TradingView

This folder contains a Pine Script port of `OpenGamma.cs` for the TradingView
desktop app.

## Important limitation

The NinjaTrader indicator connects to the local dashboard over TCP port `5010`.
TradingView Pine scripts cannot open local sockets, read `gex_data.db`, or read a
local file directly. The Pine version therefore keeps the charting behavior and
uses pasteable inputs for the latest gamma levels and dashboard values.

## Files

- `OpenGamma_TradingView.pine` - Pine Script indicator to paste into TradingView.
- `export_tradingview_levels.py` - exports the latest OpenGamma levels from
  `gex_data.db` into a paste-ready text file.

## Use it in TradingView desktop

1. Open TradingView desktop.
2. Open a futures chart such as `NQ`, `MNQ`, `ES`, or `MES`.
3. Open Pine Editor.
4. Paste the full contents of `OpenGamma_TradingView.pine`.
5. Save it, then choose **Add to chart**.
6. In this repo, export the latest levels:

   ```powershell
   python .\tradingview\export_tradingview_levels.py --symbol NDX
   ```

   For ES/MES charts, use:

   ```powershell
   python .\tradingview\export_tradingview_levels.py --symbol SPX
   ```

7. Open the generated file, for example
   `tradingview\OpenGamma_NDX_TradingView_inputs.txt`.
8. Copy the line under "Paste this into..." into the indicator's **Gamma levels**
   input.
9. Optionally copy the dashboard values into the matching inputs.

By default the exporter keeps up to 80 levels, prioritizing key levels and then
the strongest remaining gamma levels. To export every level from the latest
payload, add:

```powershell
python .\tradingview\export_tradingview_levels.py --symbol NDX --max-levels 0
```

## Refreshing data

Run the exporter again whenever the dashboard has a newer snapshot, then replace
the indicator's **Gamma levels** input with the new exported string.

The indicator adjusts index strikes to futures prices using the same spread idea
as `OpenGamma.cs`:

```text
futures level = index strike - smoothed(index spot - futures close)
```

If TradingView cannot load the `NDX` or `SPX` spot ticker on your account, set
the indicator's **Manual spread override** to the spread shown by your dashboard
or NinjaTrader indicator.
