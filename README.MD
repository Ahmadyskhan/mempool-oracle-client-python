# SOVEREIGN: Quantitative Mempool Execution Engine

Sovereign is a reference architecture for high-frequency algorithmic trading. It is specifically designed to ingest sub-50ms mempool data (supply shocks, RBF panic, whale movements) and execute automated spot trades based on a composite microstructure analysis.

**Note:** This is a reference implementation. It is designed to consume the ultra-low latency **Mempool Oracle C-Engine** data feed. 

##  Core Architecture
Rather than relying on standard, slow RPC node polling, Sovereign uses a dual-thread architecture:
1. **The Oracle Worker:** Maintains an encrypted, real-time SSE connection to the Mempool Oracle C-Engine, ingesting raw binary packet data (TXID, Satoshi value, fee density) in milliseconds.
2. **The Market Worker:** Continually monitors orderbook imbalance (OB) and standard technical indicators (MACD, VWAP, RSI) via Binance.

Execution is driven by a **Composite Signal Score (CSS)**, dynamically weighing real-time on-chain data against lagging market indicators.

##  Risk Management (Built-in)
* **Fractional Kelly Criterion:** Position sizing is dynamically calculated based on the bot's live win rate and R:R ratio, scaled down to a conservative 25% Kelly fraction.
* **Dynamic ATR Trailing Stops:** Hard percentages are replaced with Average True Range (ATR) multipliers to adapt to live market volatility.
* **Time-Decayed Clustering:** Mempool events are tracked in a rolling 2-minute memory window with a logarithmic half-life, ensuring the bot only reacts to concentrated supply shocks, not isolated noise.

##  Quick Start

### 1. Install Dependencies
Sovereign is built to be lightweight and fast. 
```bash
pip install -r requirements.txt
