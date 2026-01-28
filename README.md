# PublicGex OSS Dashboard 🚀

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/platform-windows-lightgrey)

**Real-time Market Regime Analysis powered by 0DTE Option Gamma Exposure.**

This dashboard bridges the gap between raw option chain data (from Public.com) and actionable trading signals. It calculates "Gamma Flips," "Magnets," and "Market Regimes" (e.g., Grind Up vs. Crash) and broadcasts them directly to **NinjaTrader 8**.

---

## 🌟 Features

*   **Real-Time Gamma Analysis**: Fetches 0DTE option chains and calculates Net GEX instantly.
*   **Gamma Flip Detection**: Identifies the precise strike price where market stability flips.
*   **Market Compass**: Visualizes Trend vs. Volatility to categorize the market regime (Grind Up, Melt Up, Chop, Crash).
*   **NinjaTrader Integration**: Includes a custom C# indicator (`OpenGamma.cs`) to plot levels and regimes directly on your charts.
*   **Live Dashboard**: A local web UI (Eel/HTML) for monitoring the system.

## 📚 Documentation

Detailed documentation is available in the `docs/` directory:

*   [**📖 Project Overview**](docs/index.md): System architecture and data flow.
*   [**⚙️ Setup Guide**](docs/setup.md): Installation, API keys, and NinjaTrader configuration.
*   [**🔧 API Reference**](docs/api_reference.md): Technical deep dive into the Python modules and C# indicator.
*   [**🛠 Developer Guide**](docs/how_to_add_strategy.md): How to add custom strategies and weighted symbols.

## ⚡ Quick Start

1.  **Clone the repo**:
    ```bash
    git clone https://github.com/your-username/public-gex-dashboard.git
    cd public-gex-dashboard
    ```

2.  **Install dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

3.  **Configure `.env`** (See [Setup Guide](docs/setup.md)).

4.  **Run the Dashboard**:
    ```bash
    python appy.py
    ```

5.  **Start Data Collection** (in a new terminal):
    ```bash
    python publicData.py
    ```

## ⚠️ Disclaimer

This software is for educational purposes only. Do not use it as the sole basis for real-money trading decisions. Option Gamma is a theoretical model and market conditions can change rapidly.

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.
