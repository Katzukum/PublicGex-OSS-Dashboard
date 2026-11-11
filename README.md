# PublicGex Dashboard

The main Windows desktop version of PublicGex: Rust/Tauri hosts a React + TypeScript interface, TradingView Lightweight Charts renders every chart, and an isolated Python sidecar retains the analytical engine.

## Repository migration

As of October 6, 2026, `main` contains this Rust/Tauri version. The latest original dashboard, including its prediction and research work, is preserved on [`codex/original-backup-2026-10-06`](https://github.com/Katzukum/PublicGex-OSS-Dashboard/tree/codex/original-backup-2026-10-06) at commit `b75e1e01bcc0ae1e5fcfd3e5ee12e31f50ecdee9`. The migration continues that history; earlier branches remain available.

Use the `PublicGexDashboard` folder for ongoing local development. The original source is retained on the backup branch; a second local checkout is no longer required. Before removing a legacy checkout, preserve its local credentials, databases, prediction data, and other untracked files in a private backup outside the repository. Git preserves source and included research artifacts; credentials, local databases, virtual environments, and generated builds remain outside version control. This repository migration does not transfer runtime data or credentials between the two apps.

## Open the app

Double-click **Launch PublicGex Dashboard.bat** for a clean live workspace, or **Launch PublicGex Demo.bat** to explore synthetic data. Demo prices, opportunities, and journal fills are explicitly labeled and stored separately. Demo mode cannot start the collector or NinjaTrader broadcast.

The native release executable and its companion `publicgex-backend.exe` must remain together under `src-tauri/target/release`. The installer is produced under `src-tauri/target/release/bundle/nsis`.

Both BAT launchers reuse the existing desktop build without rebuilding. They check that the native executable and companion backend exist and compare their timestamps with the source files. If source files are newer, a warning offers to launch the existing build anyway (type `y`) or cancel (Enter). Missing executables stop the launcher. This is a quick timestamp check, not a content-integrity check; deleted files or changes preserving old timestamps are not detected. To update the desktop app, close it and run `npm run native:build` with the development dependencies below installed. This rebuilds the frontend, Python sidecar, and native release; `npm run build` alone does not update the desktop app.

## Independence from the original app

- No runtime imports, credentials, settings, databases, or virtual environment are shared with the original project.
- Browser development and native debug use this project's `data` folder. Demo uses `.demo-data`.
- Installed/release native builds use their own Windows local app-data directory under `com.publicgex.dashboard`, with separate `data` and `.demo-data` subdirectories. Settings displays the exact active path.
- Backend HTTP and collector-event listeners use dynamic loopback ports and private session tokens.
- Live launches automatically start the collector when this workspace's credentials are configured, and start the NinjaTrader bridge on the original port **5010**. Settings provides startup preferences, manual stop/start, and startup failure messages. Demo integrations stay off.
- The separately named `PublicGexDashboard` NinjaTrader indicator and the original `OpenGamma` indicator use the same compatible schema-v2 feed on port **5010**. This project does not overwrite installed indicator files. Another running app occupying that port is reported in Settings; it is not stopped automatically. Internal collector events still use a private authenticated dynamic port (5005 is the original app's internal event port).
- `npm run verify:original` is an optional local development check against the historical integrity baseline recorded while building the recreation. It requires the original checkout at the path in `docs/source-baseline.json` and can report subsequent legacy changes, including the prediction work preserved in the backup branch. It is not a clean-clone verification step. No original database was copied or migrated during development.

## Features

The default instrument universe is **SPY, QQQ, IWM, SPX, NDX**, in that order, including a fresh live workspace before collection. Cockpit provides data quality, server-owned scenarios and read-only execution candidates, gamma profiles, historical trends, and Gamma Sweep with cumulative hedge demand. Edge Lab provides historical evidence and a manual trade journal with weekly review. Regime shows quality-weighted market compasses and component diagnostics. TRACE provides session selection, exposure modes, price candles, spot path, overlays, profile selection, and replay. Strike Matrix provides dealer states, searchable/sortable exposure rows and CSV export. One-Off stores separately requested expiration profiles, including its own Gamma Sweep. Settings controls this workspace and its integrations, collection limits, risk budget, fees, and basket weights.

Switching tabs preserves chart zoom, controls, replay position, selected one-off profile, table filters, scroll position, and unsaved forms. Replay pauses while its tab is hidden. The previously generated untouched SPY-only starter configuration is upgraded with a backup; intentionally customized instruments are preserved.

Gamma Sweep is enabled by default and sits immediately below the OI-based positioning proxy. Notifications and market alerts occupy a visible right-hand column. TRACE decision overlays support older events without a scenario label. The settings migration introduced in version 0.1.2 converts the previous default NinjaTrader port 15010 to 5010 once, saving the previous settings in `settings.before-ninjatrader-port-v3.json`; other custom ports are preserved.

Charts use native Lightweight Charts series plus custom canvas series/primitives for TRACE heatmaps, horizontal exposure profiles, compass trails, and decision annotations. Wheel zoom, drag pan, crosshair tooltips, reset, PNG export, and independent exposure scales work across the views. The shipped chart license and notice are in `public/licenses`; TradingView attribution is visible in the app footer.

Data quality is not a win probability. Modeled sensitivities are labeled. Public.com access is limited to market data and quotes; the app does not place orders.

## OI concentration and recent activity

Cockpit and TRACE include an observational positioning view. OI concentration is the unsigned sum of option gamma times reported open interest, multiplied by `100 × current spot² × 0.01`. Calls and puts can be inspected separately or combined without canceling each other. The signed legacy chart is labeled an **OI-based positioning proxy**: its call-positive/put-negative convention does not identify actual dealer ownership. Dealer direction remains **unknown**.

The 5- and 15-minute activity views weight observed increases in cumulative contract volume by each contract's current gamma and the same current-spot dollar conversion. They measure **gamma-weighted trading activity**, not positions opened, net dealer inventory, or observed hedge flows. Only within-session intervals fully inside the selected window are counted. Missing observations, volume-counter decreases, and gaps over 120 seconds are excluded and surfaced through coverage/status notes. Missing activity remains unknown rather than becoming a zero. A cold start needs successive observations before activity is available.

Persistent levels are ranked by how often a strike appears among the five largest unsigned OI concentrations in observed snapshots over the last 15 minutes, with current concentration breaking ties. Persistence is an observation frequency, not a probability that a level holds. TRACE's new positioning panel describes its latest snapshot even when the separate historical TRACE cursor is moved.

The collector retains zero-OI contracts with trading activity or usable positive gamma. Their observations support activity analysis while their legacy OI-based GEX contribution remains zero. Historical data cannot recover zero-OI contracts discarded by earlier releases. Existing scenario gates and NinjaTrader numerical fields retain the previous OI-based semantics; the new measures do not influence trade signals while validation is pending.

The historical shadow evaluator opens an explicitly selected database read-only and prints JSON. For the native live workspace:

```powershell
.venv\Scripts\python.exe backend/positioning_validation.py --db "$env:LOCALAPPDATA/com.publicgex.dashboard/data/gex_data.db" --symbol SPX
```

Add `--include-samples` to inspect the individual selected levels and outcomes. Fixed top-three rankings for OI concentration, recent activity, absolute net OI proxy, and nearest strikes share nonoverlapping 15-minute observations. The earliest 80% of recorded sessions form the development sample; newer sessions are held out. At least two sessions are needed. Incomplete feature or forward-price coverage is excluded for every method together. Touch rates, initial distances, and distance buckets assess level reach; they do not establish dealer ownership, profitable signals, or an improvement without enough comparable observations.

## Option Envelopes, Estimated Hedge Pressure, and Option Exhaustion

The dashboard includes three versioned observational research models. They do not change regime, scenario, bias, target, invalidation, execution candidates, or NinjaTrader schema-v2 values.

- Option Envelopes rank Major and Minor strikes by absolute signed call-minus-put gamma concentration. Flow uses current-session cumulative volume; Structural uses reported open interest. Zero Gamma reuses the existing modeled Gamma Sweep with the selected weight. Coverage and strike-boundary warnings stay visible because the collector's normal universe remains approximately spot +/-3%.
- Estimated Hedge Pressure classifies positive cumulative-volume changes from the right-endpoint bid/ask/last quote, then estimates underlying delta shares and dollars. The session series uses completed one-minute buckets and configured fast/slow EMAs. Unknown, stale, reset, duplicated, or widely spaced observations remain excluded; nulls are not converted to zero. Public.com does not identify customers, dealers, opening trades, or every print between polls.
- Option Exhaustion combines trailing bucket-flow percentiles, observed five-bar price location, pressure weakening/divergence, and nearby Envelopes. Its score is an evidence score, not a reversal probability or trade recommendation. TRACE markers retain their evidence and replay availability time.

Cockpit shows current research; TRACE shows historical stepped Envelopes, a synchronized pressure pane, and exhaustion markers. One-Off profiles show Structural Envelopes and do not borrow live-session flow. Settings includes source, EMA, sensitivity, price-filter, threshold, and observation-timezone controls under **Option research settings**. Stored collector timestamps are local wall clock and default to `America/New_York`; provider quote timestamps follow the collector's UTC serialization convention. If clock provenance cannot be established, the affected research remains unavailable.

New collector snapshots are timestamped after all option quote batches return. This keeps slow responses from being rejected as future quotes and prevents replay from exposing fetched data before it was available. Earlier snapshots retain their recorded timestamps because their exact receipt times were not stored; their pressure coverage may remain incomplete. The five-second future-quote guard and minimum classification coverage remain enforced.

The deterministic demo runs the same calculators against isolated synthetic multi-poll observations and includes pressure turns and upper/lower exhaustion examples. Demo collection and broadcasting remain disabled.

Run the expanded read-only evaluator for one or more research families:

```powershell
.venv\Scripts\python.exe backend/positioning_validation.py `
  --db "$env:LOCALAPPDATA/com.publicgex.dashboard/data/gex_data.db" `
  --symbol SPX `
  --families envelope_touch pressure_direction pressure_divergence exhaustion_reversal `
  --include-samples `
  --output-json option-research.json `
  --output-csv option-research.csv
```

The evaluator calculates features only from each timestamp-bounded prefix, then loads 5/15/30-minute outcomes separately. It preserves the chronological 80/20 session split and reports clock, interval, classification, and forward-coverage exclusions. Twenty independent sessions and 100 usable exhaustion candidates are prerequisites for a later signal-integration review; reaching those counts does not enable signals automatically.

On the verification host (AMD Ryzen 7 2700X, 16 logical processors, 23.9 GB RAM, Node 25.8.2, Python 3.12.6), the deterministic SPX session measured 587.3 ms cold and 63.8 ms warm for the dashboard payload (129,132 bytes), and 1,311.6 ms cold and 78.5 ms warm for TRACE (603,205 bytes). Both paths remain below the research budgets of two seconds cold, 500 ms warm, and 10 MB per JSON payload. The measurement covers RPC calculation and serialization after sidecar startup.

## Configure market data

The sample file is [`.env.example`](.env.example) in the project root. Both `PUBLIC_API_KEY` and `PUBLIC_ACCOUNT_ID` are required.

For the desktop launchers or installed app, edit `%LOCALAPPDATA%\com.publicgex.dashboard\data\.env`. For browser development with `npm run dev`, copy the sample to `data\.env` inside this project. Settings shows the exact active credentials path. A `.env` saved in the project root is not read by the app.

Fill in the two values, save the file, and reopen the live app for automatic collection, or click Start collector in Settings immediately. Demo mode cannot collect live data. No keys are copied from the original app.

## Development

Requires Node.js 22.12+ and Python 3.12 for source development. From this directory:

```powershell
npm ci
npm run setup:python
npm run dev:demo
```

The browser development server is at `http://127.0.0.1:1420`. `npm run dev` starts an empty live workspace instead. The development proxy starts and owns its Python service automatically. Closing the host closes its lifetime pipe and stops the service and its collector.

Native development uses a Rust toolchain stored under `.tools`, plus installed Visual Studio C++ Build Tools and a Windows SDK. It does not change the system PATH:

```powershell
npm run native:demo
npm run native:dev
npm run native:build
```

`native:build` packages Python with PyInstaller, compiles the native app, and creates an NSIS installer. Python dependencies installed for this build are recorded in `backend/requirements-lock.txt`. Node and Rust dependencies are pinned in their lockfiles.

## Verification

```powershell
npm run build
npm test
npm run test:backend
npm run test:e2e
npm run native:test
```

Browser tests use installed Microsoft Edge and a synthetic Python workspace. They require no broker credentials. Native integration does not exercise a real NinjaTrader session or a live market-data account.

Version 0.1.4 passed 202 Python tests, 85 TypeScript/API tests, seventeen browser workflows, and four Rust lifecycle tests. Positioning and observational-research checks cover window boundaries, counter resets, missing observations, zero-OI retention, unchanged legacy scenario/broadcast/backtest behavior, chronological holdout evaluation, causal availability, pressure classification, envelope ranking, exhaustion evidence, and the distinction between unknown and zero activity. Browser checks cover all chart families through real Lightweight Charts APIs and raster output, wheel zoom, drag pan, tooltips, PNG export, exact fractional zoom retention across theme/tab/refresh, nullable-label TRACE overlays, dual exposure axes, Gamma Sweep defaults and placement, research replay ordering, the right notification column, all five instruments, replay, journal edits, settings, and transport recovery. Screenshots are saved under `docs/screenshots`; `reference` contains original UI captures served read-only against isolated synthetic data.

The packaged 0.1.4 release passed fresh live and demo backend checks, including the new positioning payload, automatic bridge startup and shutdown. Its native demo WebView passed Rust IPC, the new positioning chart, existing chart families, decision overlays, retained tab state, and Settings under the production content security policy. The native live WebView's automatic Collector/NinjaTrader startup and workspace-isolation checks were last run on 0.1.3. The 0.1.4 executable and NSIS installer were built; the installer was not installed. An actual NinjaTrader chart session and live market-data accuracy remain outside these checks.

Version 0.1.5 corrects the elapsed-time window used for 15-minute realized volatility and adds separate historical counts for eligible scenario episodes and independent 30-minute opportunities. The full replay still fails the version-2 scenario frequency gate for four of five symbols; version 2 remains observational and the Cockpit and NinjaTrader continue to use the established decision. See `docs/reports/phase-2.md` for counts and limits.

After a native build, `node scripts/native-ui-smoke.mjs` exercises the packaged demo through its actual WebView and Rust IPC. `python scripts/native-smoke.py` checks the frozen backend's automatic startup and shutdown using fresh temporary workspaces without credentials. Adding `--live` to the UI smoke checks the actual separate live workspace and honors its automatic startup preferences.

## Optional historical import

For a **development** workspace, close the new app and ensure its `data/gex_data.db` does not exist. Run:

```powershell
python scripts/import-history.py "C:\path\to\an\existing\gex_data.db"
```

This creates a consistent SQLite backup in the new project's data folder using a read-only source connection. It refuses to overwrite an existing destination and never copies credentials or settings. Start the new app afterward to apply migrations to its copy. This utility was provided for an optional import; it was not run against the original history during recreation.

See `integrations/README.md` for the separately named NinjaTrader indicator.
