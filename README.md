Sovereign

A high-frequency execution client designed to trade Bitcoin spot markets using sub-50ms unconfirmed mempool data. 

Standard Bitcoin Core RPC polling is too slow for algorithmic execution. Sovereign bypasses local disk I/O by connecting directly to the Mempool Oracle C-Engine via a persistent SSE stream, allowing it to calculate on-chain supply shocks and RBF velocity before block confirmation.

Architecture

Sovereign runs a decoupled, dual-thread environment:
- Oracle Worker: Maintains the SSE connection to the C-Engine. Ingests raw binary packet data (TXID, Satoshi value, fee density, RBF flags) in milliseconds.
- Market Worker: Monitors Binance spot execution, orderbook imbalance (OB), and lagging indicators (MACD, VWAP, RSI).

Trades are executed via a Composite Signal Score (CSS) that dynamically weighs real-time mempool supply shocks against current exchange liquidity.

Execution & Risk Logic

- Position Sizing: Dynamic Fractional Kelly Criterion (scaled to a 25% cap) calculated against the live win-rate and R:R ratio.
- Trailing Stops: Dynamic ATR (Average True Range) multipliers instead of hard percentages to account for live volatility.
- Signal Filtering: Time-decayed event clustering. Mempool shocks are tracked in a rolling 2-minute window with a logarithmic half-life. The bot executes on concentrated liquidity shocks and ignores isolated noise.

Quick Start

1. Install dependencies
```bash
git clone [https://github.com/Ahmadyskhan/mempool-oracle-client-python.git](https://github.com/Ahmadyskhan/mempool-oracle-client-python.git)
cd mempool-oracle-client-python
pip install -r requirements.txt
