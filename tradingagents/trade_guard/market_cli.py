"""Opt-in BTC-USD public feed -> existing SPOT PAPER ledger, no order creation.

Example:
    python -m tradingagents.trade_guard.market_cli --ticks 1
    python -m tradingagents.trade_guard.market_cli --ticks 30 --interval 60

Only an existing open paper position/limit order can be advanced. This program
does NOT consult LLMs, open new positions, or connect to authenticated APIs.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

from .coinbase_feed import CoinbaseBTCUSDFeed
from .paper import PaperTradingEngine


def poll_once(feed: CoinbaseBTCUSDFeed, engine: PaperTradingEngine, *,
              now: datetime) -> dict:
    """On any feed/validation failure, do not advance or modify the ledger."""
    snapshot = feed.snapshot(now=now)
    events = engine.advance(snapshot, now=now)
    portfolio = engine.status(snapshot.price)
    return {
        "symbol": snapshot.symbol,
        "source": snapshot.source,
        "exchange_time": snapshot.observed_at.isoformat(),
        "checked_at": now.isoformat(),
        "last_trade_usd": str(snapshot.price),
        "paper_equity_usd": str(portfolio["equity_quote"]),
        "paper_cash_usd": str(portfolio["cash_quote"]),
        "paper_base_btc": str(portfolio["base_quantity"]),
        "paper_realized_pnl_usd": str(portfolio["realized_pnl_quote"]),
        "paper_unrealized_pnl_usd": str(portfolio["unrealized_pnl_quote"]),
        "paper_drawdown_fraction": str(portfolio["max_drawdown_fraction"]),
        "events": [
            {
                "id": event["id"],
                "action": event["action"],
                "status": event["status"],
                "filled_price": event["filled_price"],
            }
            for event in events
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Coinbase BTC-USD feed into SPOT paper ledger, no real orders."
    )
    parser.add_argument(
        "--db", type=Path, default=Path(".paper_trading") / "btc-usd.sqlite",
        help="separate local SQLite paper ledger",
    )
    parser.add_argument("--ticks", type=int, default=1, help="number of polls; 1 is default")
    parser.add_argument("--interval", type=int, default=60, help="seconds between polls, minimum 15")
    parser.add_argument("--candles", choices=("1m", "5m", "15m", "1h", "1d"))
    parser.add_argument("--candle-count", type=int, default=60)
    args = parser.parse_args(argv)

    if not 1 <= args.ticks <= 100000 or args.interval < 15:
        parser.error("ticks must be 1..100000 and interval must be >= 15 seconds")
    if not 1 <= args.candle_count <= 300:
        parser.error("candle-count must be between 1 and 300")
    feed = CoinbaseBTCUSDFeed()
    if args.candles:
        candles = feed.candles(args.candles, count=args.candle_count)
        print(json.dumps({
            "symbol": "BTC-USD",
            "granularity": args.candles,
            "candles": [c.model_dump(mode="json") for c in candles],
        }, ensure_ascii=False))
        return 0

    args.db.parent.mkdir(parents=True, exist_ok=True)
    engine = PaperTradingEngine(args.db, symbol="BTC-USD")
    log = args.db.with_suffix(".ticks.jsonl")
    successful_ticks = 0
    for n in range(args.ticks):
        if n:
            time.sleep(args.interval)
        current = datetime.now(UTC)
        try:
            report = poll_once(feed, engine, now=current)
        except (ValueError, requests.RequestException) as exc:
            # Do not make up a fresh quote or advance the paper ledger.
            # A 30-tick monitor keeps polling after a bad tick.
            report = {
                "symbol": "BTC-USD",
                "status": "SKIPPED_INVALID_OR_STALE_DATA",
                "checked_at": current.isoformat(),
                "error": str(exc),
                "events": [],
            }
        else:
            successful_ticks += 1
            report["status"] = "OK"
        line = json.dumps(report, ensure_ascii=False)
        print(line, flush=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    return 0 if successful_ticks else 1


if __name__ == "__main__":
    raise SystemExit(main())
