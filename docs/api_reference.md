# API Reference

This section provides a technical overview of the codebase modules. The source code is fully annotated with Google Style docstrings for deep inspection.

## Core Modules

### 1. `appy.py` (Backend Server)
The main entry point for the Dashboard.
*   **Location**: `[appy.py](../appy.py)`
*   **Key Functions**:
    *   `get_dashboard_data(symbol)`: Returns charts/profiles.
    *   `get_market_overview()`: Calculates the global market compass.
    *   `run_event_server(port)`: Listens for updates from `publicData.py`.

### 2. `publicData.py` (Data Collector)
The ETL (Extract, Transform, Load) worker.
*   **Location**: `[publicData.py](../publicData.py)`
*   **Key Logic**:
    *   `process_symbol(client, session, run, symbol, config, rate_limiter)`: Processes one symbol for a collection run.
    *   `calculate_flip_point(gex_by_strike)`: Mathematical logic for the flip.
    *   CLI modes:
        *   `python publicData.py`: polling collector using `backend_update_delay`.
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

Old-schema databases are backed up to `gex_data_legacy_*.db` and replaced with a fresh schema.
