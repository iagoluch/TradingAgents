# BTC/USD Coinbase public feed — phase 4

This phase adds **read-only** BTC-USD last-trade and completed OHLCV data from
the Coinbase Exchange public REST API. No API key is required.

Public endpoints:
- \`https://api.exchange.coinbase.com/products/BTC-USD/ticker\`
- \`https://api.exchange.coinbase.com/products/BTC-USD/candles\`

The symbol is fixed to **BTC-USD**, NOT BTC-BRL and NOT BTC-USDT. Other assets
or venues require a separate, reviewed adapter.

## CLI quick start (Windows CMD)

In the TradingAgents project directory, after pulling this change:

    .venv\Scripts\python.exe -m tradingagents.trade_guard.market_cli --ticks 1

Poll 30 times every 60 seconds (foreground process; Ctrl+C to stop):

    .venv\Scripts\python.exe -m tradingagents.trade_guard.market_cli --ticks 30 --interval 60

Fetch the latest 60 completed 1-hour candles:

    .venv\Scripts\python.exe -m tradingagents.trade_guard.market_cli --candles 1h --candle-count 60

Timeframes: 1m, 5m, 15m, 1h and 1d, maximum 300 candles per request.

## Safety/behavior

- The ticker is taken from the venue, including exchange timestamp. Stale
  quotes older than 90 seconds, malformed prices and future quotes are refused.
  Network errors abort the cycle; there is **no fabricated fallback price**.
- The poller opens a local SQLite SPOT paper ledger at
  \`.paper_trading/btc-usd.sqlite\`. It only calls \`advance()\` with a validated
  MarketSnapshot and prints/persists results in
  \`.paper_trading/btc-usd.ticks.jsonl\`. These files are gitignored.
- **It does not generate signals or automatically submit orders.** To simulate
  buys/sells, a separate caller must supply a fully specified TradeIntent,
  fresh data, and a coherent completed crypto agent decision to the existing
  paper ledger, which remains protected by the risk validator.
- Only spot positions can be advanced. No margin, shorts, broker auth or real
  orders. Market data being real does **not** make the accounting results a
  prediction of live trading profits.
- The poll interval can skip stop triggers or cross limit orders between ticks.
  The simulator assumes discrete tick prices and does not track intratick
  spreads or order book fills.
- The JSONL tick audit is best-effort and is **not** the canonical ledger; the
  SQLite database remains canonical. If logging fails, the tick may already
  have been applied. Retry with operator review rather than inventing missing
  history.
- Coinbase may respond with throttling, maintenance or outage errors. A ticker
  failure is reported, and the ledger is not advanced in that cycle.

Offline tests:

    python -m pytest tests/test_coinbase_feed.py -q
    ruff check .
