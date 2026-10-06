# PublicGex Dashboard

This is an independent recreation. Never modify or use the original PublicGex OSS Dashboard directory at runtime. Keep databases, credentials, settings, logs, collector locks, and internal event listeners isolated. The user explicitly requires the original externally compatible NinjaTrader port 5010.

Use React + strict TypeScript and TradingView Lightweight Charts for every chart, including custom series/primitives for the heatmap, compass and horizontal profiles. The user explicitly replaced earlier chart-library choices. Keep Python analytical semantics, read-only broker usage, schema-v2 contracts, and null prices. Tauri owns the desktop process lifecycle.

All backend RPCs must be explicit allowlisted methods. The user requires automatic Collector and NinjaTrader startup for live launches, using only this app's credentials/data and the original NinjaTrader port 5010. Startup preferences can disable either service; missing credentials or a busy port must leave the UI usable and report the issue. Do not terminate another application's listener to claim that port. Demo data must remain labeled and separate, with live collection/broadcast disabled.

Verification: npm run build; npm test; npm run test:e2e; python -m pytest backend/tests -q. Native compilation: follow scripts/native-* and README.
