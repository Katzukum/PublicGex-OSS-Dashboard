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
API_RATE_LIMIT_PER_SECOND=10
```

### 2. Application Settings (`settings.json`)
The dashboard behavior is controlled by `settings.json`.

```json
{
  "theme": "dark",
  "api_rate_limit_per_second": 10,
  "api_rate_limit_utilization": 0.6,
  "min_poll_interval_seconds": 15,
  "max_poll_interval_seconds": 120,
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
*   **api_rate_limit_per_second**: Public.com request ceiling used by the collector.
*   **api_rate_limit_utilization**: Fraction of that ceiling the collector is allowed to plan around.
*   **min_poll_interval_seconds** / **max_poll_interval_seconds**: Bounds for the collector's calculated next poll delay.
*   **raw_retention_days**: Number of days to keep raw option rows before compaction.

> [!NOTE]
> Collection is strict target-day 0DTE. Before 6 PM local time, the target is today; at or after 6 PM, the target rolls to the next weekday. If Public.com does not return an expiration for that target date, the symbol is skipped instead of falling back to a later weekly or monthly expiration.

## Running the System

### Step 1: Start the App
This launches the UI, Event Listener, NinjaTrader bridge, and Public.com collector. The collector is stopped when the dashboard exits.
```bash
python app.py
```

### Optional: Start Data Collection By Itself
Use this only for headless collection or debugging.
```bash
python publicData.py
```

> [!TIP]
> Use `python publicData.py --once` for a single collector run.

### Resetting the Local Database
If the database schema changes or you want a clean start, run:

```bash
python publicData.py --reset-db
```

The existing `gex_data.db` is renamed to a timestamped backup before the new schema is created.
