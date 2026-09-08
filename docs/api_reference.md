# API Reference

This section provides a technical overview of the codebase modules. The source code is fully annotated with Google Style docstrings for deep inspection.

## Core Modules

### 1. `appy.py` (Backend Server)
The main entry point for the Dashboard.
*   **Location**: `[appy.py](../appy.py)`
*   **Key Functions**:
    *   `get_decision_workspace(symbol)`: Primary schema-version-2 Cockpit contract. Returns a single selected snapshot, Data Quality, market context, regime, scenarios, eligibility, Historical Edge, candidates, and alert IDs.
    *   `get_edge_lab(filters)`: Filtered independent-opportunity evidence.
    *   `get_trace_dates(symbol)` / `get_trace_data(symbol, minutes, session_date)`: Bounded replay with overlays and modeled pressure fields.
    *   `create/update/delete/get_journal_*`: Validated local user-execution journal APIs.
    *   `get_dashboard_data(symbol)`: Returns charts/profiles, including `gamma_sweep` when enough stored contract Greeks are available.
    *   `get_market_overview()`: Calculates the global market compass.
    *   `run_event_server(port)`: Listens for updates from `publicData.py`.

`gamma_sweep` is a modeled V1 payload derived from the current stored 0DTE profile. It contains `status`, `model`, `range`, `current`, `zero_crossings`, `skipped_contracts`, and `points`; each point has `spot`, `net_gex`, and `hedge_shares`.

### 2. `publicData.py` (Data Collector)
The ETL (Extract, Transform, Load) worker.
*   **Location**: `[publicData.py](../publicData.py)`
*   **Key Logic**:
    *   `process_symbol(client, session, run, symbol, config, rate_limiter)`: Processes one symbol for a collection run.
    *   `calculate_flip_point(gex_by_strike)`: Mathematical logic for the flip.
    *   CLI modes:
        *   `python publicData.py`: rate-aware polling collector using the configured Public.com request ceiling and poll bounds.
        *   `python publicData.py --once`: one collection pass.
        *   `python publicData.py --reset-db`: backs up the current DB and creates the current schema.

### 3. `models.py`
Shared database schema and lifecycle helpers.
*   **Location**: `[models.py](../models.py)`
*   **Key Classes**:
    *   `CollectionRun`: A collector pass across configured symbols.
    *   `GexSnapshot`: Summary metrics for one symbol in one run.
    *   `RawOptionGreek`: Contract-level data linked to a snapshot.
*   **Key Logic**:
    *   `initialize_database()`: Creates the schema and backs up old-schema DBs.
    *   `reset_database()`: Explicitly backs up and recreates the local DB.

### 4. `ninjatrader_broadcaster.py`
TCP Server for external indicators.
*   **Location**: `[ninjatrader_broadcaster.py](../ninjatrader_broadcaster.py)`
*   **Protocol**: Sends newline-delimited JSON strings.
*   **Default Port**: 5010

## Client Modules (NinjaTrader 8)

### `OpenGamma.cs`
A custom C# Indicator that visualizes the data on NT8 charts.
*   **Location**: `Documents\NinjaTrader 8\bin\Custom\Indicators\OpenGamma.cs`
*   **Role**: TCP Client (connects to local port 5010).
*   **Features**:
    *   **Regime Panel**: Displays Market Compass state (e.g., "Grind Up", "Crash").
    *   **Gamma Levels**: Draws Support/Resistance zones based on GEX clusters.
    *   **Futures Adjustment**: Automatically calculates spread between Index (SPX/NDX) and Futures (ES/NQ).

### 5. `event_utils.py`
Helper for IPC.
*   **Location**: `[event_utils.py](../event_utils.py)`
*   **Usage**: `send_event('magnet_change', payload)`

## Database Schema

The SQLite database (`gex_data.db`) contains three primary tables:

1.  **collection_runs**: One row per collector pass.
2.  **gex_snapshots**: High-level history linked to a collection run.
3.  **raw_option_greeks**: Contract rows linked to a snapshot by `snapshot_id`.

Current databases are upgraded additively by `schema_migrations.py` using `PRAGMA user_version`; a pre-migration copy is made before the first production-path migration. New tables persist market context, decision alerts, and journal entries. Old snapshots remain readable with null quote fields.

## Decision contract rules

`data_quality` describes freshness/completeness only and never populates `edge_probability`. `historical_edge` is gated by independent opportunities, unique days, and positive holdout expectancy. Candidate status is `EXECUTABLE`, `MODELED_ONLY`, or `REJECTED`; only executable candidates may contain a contract count. Compatibility aliases (`confidence`, `compass_whale`, and legacy NinjaTrader dashboard names) remain for one verified release.
