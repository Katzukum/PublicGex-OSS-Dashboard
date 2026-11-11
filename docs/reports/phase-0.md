PASSED — live Public.com capability probe on November 11, 2026, 09:08–12:00 ET.

# Phase 0 capability checkpoint

The read-only probe used this independent app's credentials and made 171 requests with zero HTTP 429 responses. The full numeric output is in `docs/api-capabilities.json`. The installed SDK was `publicdotcom-py` 0.1.25; the source plan assumed 0.1.28, so the decisions below use observed 0.1.25 behavior.

| Check | Observation | Decision |
| --- | --- | --- |
| 0.1 Chain | 574 SPX contracts; Greeks and implied volatility 98.78%, OI/volume and quote timestamps 100%, bid size 79.44%. A follow-up chain check found bid and ask prices on 100% of the same 574 contracts. | Chain-native Greeks and quotes enabled for collection. Missing per-contract Greeks remain excluded. |
| 0.2 Expirations | 31–39 dates per symbol; ten SPX chain calls took 0.203–0.344 seconds. | Multi-expiration collection enabled. |
| 0.3 Index bars | SPX DAY returned 451 and NDX DAY 467 one-minute bars. WEEK and MONTH with one-minute aggregation returned HTTP 400. | DAY minute bars enabled; snapshot-path fallback remains for missing bars and older history. |
| 0.4 Option bars | 404 one-minute bars with populated volume. | Available for research; conservative option P&L continues to require two-sided bid/ask quotes because OHLCV bars lack them. |
| 0.5 Volatility indexes | VIX quoted; VIX1D and VIX9D returned unsupported-symbol HTTP 400. | VIX display is possible; use straddle term ratios for the rest. |
| 0.6 Quote polling | Batches of 50, 100, and 250 returned full counts over five minutes; volume and last timestamps changed. | Low-confidence estimated-flow polling enabled for a live smoke run. |
| 0.7 Open interest | The same 20 contracts were observed at 09:25, 09:35, and 12:00 ET. Values were identical at all three times. | Store the first reading after 09:35. An actual update time and overnight change remain unverified; no overnight claim is made from this run. |

The live expansion is observational. The Cockpit and NinjaTrader still show the established decision engine, and model probability remains null pending later gates. The collector uses a two-request-per-second per-path cap during the expansion smoke; its main and flow paths have separate limiters and must be assessed together for HTTP 429 responses.
