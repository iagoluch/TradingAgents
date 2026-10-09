"""Regression tests: stale Coinbase ticker fallback and fail-closed paper polling."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tradingagents.trade_guard import MarketSnapshot
from tradingagents.trade_guard import market_cli
from tradingagents.trade_guard.coinbase_feed import CoinbaseBTCUSDFeed

NOW = datetime(2026, 10, 9, 20, 15, tzinfo=UTC)


def quote_data(dt: datetime, price="82400"):
    return {
        "price": price,
        "time": dt.isoformat().replace("+00:00", "Z"),
        "bid": "82399",
        "ask": "82401",
    }


class RoutedSession:
    def __init__(self, *, ticker, trades):
        self.ticker = ticker
        self.trades = trades
        self.calls = []

    def get(self, url, *, params, headers, timeout):
        self.calls.append((url, params))
        if url.endswith("/ticker"):
            payload = self.ticker
        elif url.endswith("/trades"):
            payload = self.trades
        else:
            raise AssertionError(f"unexpected endpoint: {url}")
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: payload,
        )


@pytest.mark.unit
def test_stale_ticker_recovers_using_fresh_timestamped_trade_same_pair():
    fresh = NOW - timedelta(seconds=10)
    session = RoutedSession(
        ticker=quote_data(NOW - timedelta(minutes=6)),
        trades=[
            {"price": "82390.1", "time": (NOW - timedelta(minutes=1)).isoformat()},
            {"price": "82401.3", "time": fresh.isoformat()},
        ],
    )
    snap = CoinbaseBTCUSDFeed(session=session).snapshot(now=NOW)
    assert snap.symbol == "BTC-USD"
    assert snap.price == Decimal("82401.3")
    assert snap.observed_at == fresh
    assert snap.source == "coinbase-exchange/BTC-USD/trades"
    assert session.calls[0][0].endswith("/BTC-USD/ticker")
    assert session.calls[1][0].endswith("/BTC-USD/trades")
    assert session.calls[1][1] == {"limit": 5}


@pytest.mark.unit
def test_fresh_ticker_never_requires_trade_history():
    session = RoutedSession(ticker=quote_data(NOW), trades=[])
    snap = CoinbaseBTCUSDFeed(session=session).snapshot(now=NOW)
    assert snap.source == "coinbase-exchange/BTC-USD"
    assert len(session.calls) == 1


@pytest.mark.unit
def test_both_endpoints_stale_block_with_actionable_utc_diagnostics():
    session = RoutedSession(
        ticker=quote_data(NOW - timedelta(hours=1)),
        trades=[{"price": "82400", "time": (NOW - timedelta(minutes=20)).isoformat()}],
    )
    with pytest.raises(ValueError, match="stale Coinbase") as caught:
        CoinbaseBTCUSDFeed(session=session).snapshot(now=NOW)
    message = str(caught.value)
    assert "Windows_UTC" in message
    assert "venue=" in message
    assert "age=" in message
    assert "90s" in message


@pytest.mark.unit
def test_future_ticker_is_not_hidden_by_fresh_history():
    session = RoutedSession(
        ticker=quote_data(NOW + timedelta(minutes=2)),
        trades=[{"price": "82000", "time": NOW.isoformat()}],
    )
    with pytest.raises(ValueError, match="future Coinbase"):
        CoinbaseBTCUSDFeed(session=session).snapshot(now=NOW)
    assert len(session.calls) == 1


@pytest.mark.unit
def test_malformed_ticker_never_silently_uses_trades():
    session = RoutedSession(
        ticker=quote_data(NOW, price="NaN"),
        trades=[{"price": "82400", "time": NOW.isoformat()}],
    )
    with pytest.raises(ValueError, match="finite"):
        CoinbaseBTCUSDFeed(session=session).snapshot(now=NOW)
    assert len(session.calls) == 1


@pytest.mark.unit
@pytest.mark.parametrize("invalid_trades", [
    [], {}, [{"price": "NaN", "time": NOW.isoformat()}],
    [{"price": "82400"}],
])
def test_stale_ticker_cannot_be_rescued_with_invalid_trades(invalid_trades):
    session = RoutedSession(
        ticker=quote_data(NOW - timedelta(hours=1)), trades=invalid_trades
    )
    with pytest.raises(ValueError, match="fallback"):
        CoinbaseBTCUSDFeed(session=session).snapshot(now=NOW)


@pytest.mark.unit
def test_30_tick_poller_continues_after_one_bad_tick(tmp_path, monkeypatch, capsys):
    class FlakyFeed:
        def __init__(self):
            self.count = 0

        def snapshot(self, *, now):
            self.count += 1
            if self.count == 1:
                raise ValueError("stale Coinbase BTC-USD: age=400s")
            return MarketSnapshot(
                symbol="BTC-USD", price="82400", observed_at=now,
                source="coinbase-exchange/BTC-USD/trades",
            )

    monkeypatch.setattr(market_cli, "CoinbaseBTCUSDFeed", FlakyFeed)
    monkeypatch.setattr(market_cli.time, "sleep", lambda seconds: None)
    db = tmp_path / "btc.sqlite"
    result = market_cli.main(["--db", str(db), "--ticks", "2", "--interval", "15"])
    assert result == 0
    stdout = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert stdout[0]["status"] == "SKIPPED_INVALID_OR_STALE_DATA"
    assert stdout[0]["events"] == []
    assert stdout[1]["status"] == "OK"
    assert stdout[1]["paper_base_btc"] == "0"
    assert len(db.with_suffix(".ticks.jsonl").read_text().splitlines()) == 2


@pytest.mark.unit
def test_single_invalid_tick_reports_skip_nonzero_exit_and_no_order(tmp_path, monkeypatch, capsys):
    class AlwaysStale:
        def snapshot(self, *, now):
            raise ValueError("stale source: venue=bad")

    monkeypatch.setattr(market_cli, "CoinbaseBTCUSDFeed", AlwaysStale)
    db = tmp_path / "btc.sqlite"
    result = market_cli.main(["--db", str(db), "--ticks", "1"])
    assert result == 1
    logged = json.loads(db.with_suffix(".ticks.jsonl").read_text().splitlines()[0])
    assert logged["events"] == []
    assert logged["status"] == "SKIPPED_INVALID_OR_STALE_DATA"
    assert "stale source" in capsys.readouterr().out
