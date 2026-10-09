# Phase 3 — Persistent spot paper simulator

**No broker connection. No real orders. No short/derivative fills.**

The simulator is intentionally not wired to the TradingAgents CLI. Its caller
must provide:
1. Completed crypto run state with coherent Trader and Portfolio Manager actions.
2. An explicit \`TradeIntent\` for \`SPOT_BUY\` or \`SPOT_SELL\`, with a unique idempotency key.
3. A fresh independently sourced \`MarketSnapshot\` per call, and a timezone-aware clock.

\`PaperTradingEngine.submit(state, intent, quote, now=...)\` invokes phase 2's
\`validate_paper_decision()\` plus the deterministic numerical risk rules. A
Market order fills immediately using the supplied tick plus configurable
adverse slippage. A Limit order is reserved and waits for \`advance()\`; when the
tick crosses its limit, it fills at the limit, not a better price. Pending
limits expire automatically after 24 hours by default, checked at each tick.
\`cancel()\` releases reservations. The SQLite account tracks free/reserved
cash and BTC, fees, weighted entry cost, realized/unrealized PnL and maximum
drawdown *over observed ticks*.

A spot BUY opens one position with stop-loss and take-profit. Subsequent
\`advance()\` calls simulate an exit if the quote crosses either level. A gap
beyond the stop fills at the worse tick price, not the theoretical stop
level. Open SELL limit orders are cancelled before a protective exit.

## Quick example (fabricated prices and fictional capital)

    from datetime import datetime, timezone
    from tradingagents.trade_guard import MarketSnapshot, TradeIntent
    from tradingagents.trade_guard.paper import PaperTradingEngine

    state = {
        "asset_type": "crypto",
        "company_of_interest": "BTC-USD",
        "trader_investment_plan": "**Action**: Buy",
        "final_trade_decision": "**Rating**: Buy",
    }
    now = datetime.now(timezone.utc)
    simulator = PaperTradingEngine(
        "paper.sqlite", symbol="BTC-USD", starting_quote="10000"
    )
    proposal = TradeIntent(
        symbol="BTC-USD", market="SPOT", action="SPOT_BUY",
        order_type="MARKET", quantity_base="0.001",
        stop_loss="80000", take_profit="110000", idempotency_key="demo-001",
    )
    quote = MarketSnapshot(
        symbol="BTC-USD", price="90000", observed_at=now, source="test"
    )
    print(simulator.submit(state, proposal, quote, now=now))
    print(simulator.status("90000"))

The simulator assumes an optimistic-looking **discrete quote stream**: it has
no intrabar path, spread, matching engine, partial fills, latency, market depth,
exchange size precision, minimum notional or real margin. The mark-to-market
values and stop triggers are only as trustworthy as the snapshots supplied.
It does not fetch market data or validate that a source label is genuine.

This is one-asset spot accounting, not a financial simulator for derivatives.
Do not connect it to a real exchange. Paper gains do not predict live returns.

Run \`python -m pytest tests/test_paper_spot_engine.py -q\` and \`ruff check .\`.
