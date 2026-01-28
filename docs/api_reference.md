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
*   **Key Classes**:
    *   `RawOptionGreek`: Database model for individual contracts.
    *   `GexSnapshot`: Database model for summary metrics.
*   **Key Logic**:
    *   `process_symbol(client, session, symbol)`: The main ETL loop.
    *   `calculate_flip_point(gex_by_strike)`: Mathematical logic for the flip.

### 3. `ninjatrader_broadcaster.py`
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

### 4. `event_utils.py`
Helper for IPC.
*   **Location**: `[event_utils.py](../event_utils.py)`
*   **Usage**: `send_event('magnet_change', payload)`

## Database Schema

The SQLite database (`gex_data.db`) contains two primary tables:

1.  **gex_snapshots**: High-level history (Net GEX, Spot Price, Regime). ideal for time-series plotting.
2.  **raw_option_greeks**: Heavy table containing every option contract fetched. Used for "Profile" views (Bar charts).
