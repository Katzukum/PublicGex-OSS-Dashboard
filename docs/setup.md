# Setup & Configuration

## Prerequisites
*   Python 3.10+
*   NinjaTrader 8 (Optional, for charting)
*   A Public.com Account (with API Key)

## Installation

### 1. Python Environment
1.  **Clone the Repository**
    ```bash
    git clone https://github.com/your-username/public-gex-dashboard.git
    cd public-gex-dashboard
    ```
2.  **Install Dependencies**
    ```bash
    pip install -r requirements.txt
    ```

### 2. NinjaTrader 8 Setup
1.  Copy `OpenGamma.cs` to your specific NinjaTrader custom folder:
    *   `Documents\NinjaTrader 8\bin\Custom\Indicators\OpenGamma.cs`
2.  Open NinjaTrader 8.
3.  Go to **Tools > New > NinjaScript Editor**.
4.  Press **F5** to compile. You should see "compilation successful" at the bottom.
5.  Add the indicator **OpenGamma** to any chart (e.g., ES or NQ).

## Configuration

### 1. Environment Variables (`.env`)
Create a `.env` file in the root directory. You must provide your Public.com credentials.
You can start from the included template:

```bash
copy .env.example .env
```

```ini
PUBLIC_API_KEY=your_api_key_here
PUBLIC_ACCOUNT_ID=your_account_id_here
API_RATE_LIMIT=60
```

### 2. Application Settings (`settings.json`)
The dashboard behavior is controlled by `settings.json`.

```json
{
  "theme": "dark",
  "backend_update_delay": 180,
  "raw_retention_days": 30,
  "weights": {
    "SPY": 1.0,
    "QQQ": 0.5,
    "IWM": 0.2
  },
  "symbols": ["SPY", "QQQ", "IWM", "SPX", "NDX"]
}
```
*   **symbols**: The list of tickers `publicData.py` will track.
*   **weights**: How much influence each symbol has on the global "Market Compass" score.
*   **backend_update_delay**: Seconds between polling collector runs.
*   **raw_retention_days**: Number of days to keep raw option rows before compaction.

> [!NOTE]
> Collection is strict target-day 0DTE. Before 6 PM local time, the target is today; at or after 6 PM, the target rolls to the next weekday. If Public.com does not return an expiration for that target date, the symbol is skipped instead of falling back to a later weekly or monthly expiration.

## Running the System

### Step 1: Start the Dashboard
This launches the UI and the Event Listener.
```bash
python appy.py
```

### Step 2: Start Data Collection (Separate Terminal)
This begins the polling loop.
```bash
python publicData.py
```

> [!TIP]
> Use `python publicData.py --once` for a single collector run. The Dashboard UI also uses this one-off mode for manual refreshes.

### Resetting the Local Database
If the database schema changes or you want a clean start, run:

```bash
python publicData.py --reset-db
```

The existing `gex_data.db` is renamed to a timestamped backup before the new schema is created.
