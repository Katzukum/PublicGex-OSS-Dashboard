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
> You can also trigger a "one-off" refresh from the Dashboard UI by clicking the "Refresh Data" button.
