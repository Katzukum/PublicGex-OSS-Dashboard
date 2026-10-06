# Implementation context

User requested a completely separate recreation on September 30, 2026. Original directory is read-only reference. New architecture: React/TypeScript + Plotly frontend, Rust/Tauri host, retained Python analytics behind authenticated loopback RPC.

Subagent ownership: frontend_review src/index.html; backend_performance backend; migration_risk src-tauri/native packaging/integrations. Root owns Vite development bridge, dependency setup, integration verification, documentation and isolation audit.

Wire protocol: service.py --data-dir ABS --port 0 prints a single ready JSON object {type,port,token,data_dir}. POST /rpc with bearer token and {method,args}; response {result} or {error}. Rust backend_request returns unwrapped result. Browser dev bridge /api/rpc proxies same envelope. No original process, credential, DB or file is shared. Internal listeners stay private; version 0.1.2 uses the requested original external NinjaTrader port 5010.

## Functional parity correction, version 0.1.1

The original HTML/JavaScript controls and configured settings were used as the checklist. Reference screenshots under `docs/screenshots/reference` render original UI files read-only against this recreation's isolated synthetic service. Those screenshots do not open the original database or contact a market-data account.

| Area | Restored or verified behavior |
| --- | --- |
| Instruments | SPY, QQQ, IWM, SPX, NDX in configured order, visible before collection. Dashboard and collector share defaults. Exact old generated starter settings upgrade with a backup; explicit/customized symbols remain intact. |
| Cockpit | Four signal tiles, seven GEX metrics, bias, modeled candidate risk/reward, fractional strikes, profile orientation and focus/zoom controls, separate exposure axes, zero-gamma levels. |
| Gamma Sweep | Modeled net gamma and cumulative hedge demand, current spot and zero crossings, available independently in Cockpit and One-Off. |
| Regime | Trader and index-basket compasses, six-position trails, component pressure cards, data age, flip quality, acceleration and warnings. |
| TRACE | Session and exposure selection, candles, spot path, spot/flip levels, overlays, replay/window controls, latest or cursor profile, stability and session metadata. Candle bodies use the same wall-clock coordinates as their wicks. |
| Strike Matrix | Activity threshold, optional all strikes, dealer states, ATM/ITM/OTM, call/put exposure bars, OI, search/sort, jump to spot and CSV export. |
| Edge Lab | Evidence horizon/regime/quality/event filters, evidence metadata, journal context and CRUD, weekly review, per-instrument unsaved journal drafts. |
| One-Off | Independent NVDA default, expiration requests, saved profile selection and metadata, Gamma Sweep, strike matrix and export. |
| Settings | Original collection ranges, risk/fees/basket weights, startup toggles, bridge port, clear runtime status, independent credential path. |
| Shell and state | Retained visited views, per-tab scroll, chart zoom, filters and drafts; hidden replay timers suspend. Sidebar/fullscreen controls, refresh countdown, health and coalesced activity feed. |
| Automatic startup | Live bridge starts on isolated port 15010 by default. Collector starts with this workspace's credentials. Failures remain visible without blocking the UI. Demo never starts either live integration. |

Automated source verification passed 108 Python tests, 47 TypeScript/API tests, ten browser workflows and four Rust tests. This includes real fresh-live service and UI startup with all five instruments and a connectable automatically started bridge, missing-credential behavior, port failure isolation, graceful cleanup, both Gamma Sweep charts, zoom and tab-state retention. Native release verification is recorded in README. No live provider account or installed NinjaTrader session is exercised by the automated suites.

## User corrections, version 0.1.2

- Restored external NinjaTrader TCP port 5010, matching original `appy.py`, broadcaster and OpenGamma indicator. Port 5005 belongs to original collector event traffic; the authenticated new collector bridge remains dynamic. Existing generated 15010 settings migrate once with a byte-preserving backup; custom ports and later explicit 15010 choices persist. Windows exclusive binding reports occupied ports without sharing another app's listener.
- Moved Gamma Sweep into the landscape column immediately below Gamma landscape.
- Replaced the overlay activity drawer with a default-visible right column containing activity events and actionable alerts. The hide/show control remains available; inner panels adapt to remaining content width.
- Corrected the TRACE contract: stored scenario labels can be null. Decision overlays now fall back to the event type, as the original does, instead of calling `replaceAll` on null. Unit and browser tests reproduce null, missing, and blank labels and verify replay remains usable.

Verification: 117 Python tests, 48 TypeScript/API tests, twelve browser workflows, four Rust lifecycle tests. The browser layout check verifies the notification column stays to the right at 1024 and 1500 pixels without page overflow and Gamma Sweep directly follows the landscape panel.
