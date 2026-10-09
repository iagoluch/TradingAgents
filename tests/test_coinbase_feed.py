"""No live API calls: mocked BTC-USD feed and paper engine orchestration."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tradingagents.trade_guard.coinbase_feed import Candle, CoinbaseBTCUSDFeed
from tradingagents.trade_guard.market_cli import poll_once
from tradingagents.trade_guard.paper import PaperTradingEngine

NOW = datetime(2026, 10, 9, 16, 0, 40, tzinfo=UTC)


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, *, params, headers, timeout):
        self.calls.append((url, params, headers, timeout))
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: self.payload,
        )


def ticker_data(price="84000", time=NOW, **updates):
    value = {
        "price": price,
        "time": time.isoformat().replace("+00:00", "Z") if isinstance(time, datetime) else time,
        "bid": "83999", "ask": "84001",
    }
    value.update(updates)
    return value


def feed(data):
    return CoinbaseBTCUSDFeed(session=FakeSession(data))


@pytest.mark.unit
def test_ticker_is_real_btc_usd_and_uses_venue_timestamp():
    session = FakeSession(ticker_data())
    snapshot = CoinbaseBTCUSDFeed(session=session).snapshot(now=NOW)
    assert snapshot.symbol == "BTC-USD"
    assert snapshot.source == "coinbase-exchange/BTC-USD"
    assert snapshot.price == Decimal("84000")
    assert snapshot.observed_at == NOW
    url, params, headers, timeout = session.calls[0]
    assert url == "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
    assert params is None and timeout == 10
    assert "Authorization" not in headers


@pytest.mark.unit
@pytest.mark.parametrize("invalid", [
    {"price": None}, {"price": "NaN"}, {"price": "-1"},
    {"time": "not-a-time"}, {"time": None},
    {"bid": "85000"}, {"ask": "83000"},
])
def test_invalid_ticker_fails_closed(invalid):
    with pytest.raises(ValueError):
        feed(ticker_data(**invalid)).snapshot(now=NOW)


@pytest.mark.unit
def test_stale_future_and_naive_timestamps_rejected():
    with pytest.raises(ValueError, match="stale"):
        feed(ticker_data(time=NOW - timedelta(minutes=3))).snapshot(now=NOW)
    with pytest.raises(ValueError, match="future"):
        feed(ticker_data(time=NOW + timedelta(minutes=1))).snapshot(now=NOW)
    with pytest.raises(ValueError, match="timezone"):
        feed(ticker_data()).snapshot(now=datetime(2026, 10, 9, 16, 0))


@pytest.mark.unit
@pytest.mark.parametrize("payload", [[], {}, "broken", [{"price": "84000"}]])
def test_unexpected_ticker_shape_does_not_advance(payload):
    with pytest.raises((ValueError, AttributeError)):
        feed(payload).snapshot(now=NOW)


@pytest.mark.unit
def test_completed_minute_candles_are_sorted_and_filter_extras():
    t = int(datetime(2026, 10, 9, 15, 59, tzinfo=UTC).timestamp())
    raw = [
        [t, "83000", "85000", "84000", "84500", "1.2"],
        [t - 60, "82000", "84500", "83000", "84000", "0.5"],
        [t + 60, "80000", "90000", "84000", "86000", "3"],  # incomplete minute
    ]
    session = FakeSession(raw)
    candles = CoinbaseBTCUSDFeed(session=session).candles(
        "1m", count=2, now=NOW,
    )
    assert len(candles) == 2
    assert all(isinstance(c, Candle) for c in candles)
    assert candles[0].observed_at < candles[1].observed_at
    assert candles[-1].close == Decimal("84500")
    assert session.calls[0][1]["granularity"] == 60


@pytest.mark.unit
def test_broken_ohlcv_and_duplicates_rejected():
    t = int(datetime(2026, 10, 9, 15, 59, tzinfo=UTC).timestamp())
    with pytest.raises(ValueError, match="OHLC"):
        feed([[t, 100, 105, 110, 104, 1]]).candles("1m", now=NOW)
    with pytest.raises(ValueError, match="duplicate"):
        feed([[t, 100, 110, 105, 106, 1]] * 2).candles("1m", now=NOW)
    with pytest.raises(ValueError, match="candle"):
        feed([[t, 100, 110, 105]]).candles("1m", now=NOW)


@pytest.mark.unit
def test_candle_limits_and_future_window():
    f = feed([])
    with pytest.raises(ValueError, match="granularity"):
        f.candles("2m", now=NOW)
    with pytest.raises(ValueError, match="count"):
        f.candles("1m", count=301, now=NOW)
    with pytest.raises(ValueError, match="future"):
        f.candles("1m", end=NOW + timedelta(minutes=2), now=NOW)


@pytest.mark.unit
def test_poll_advances_only_existing_paper_orders(tmp_path):
    db = tmp_path / "btc-usd.sqlite"
    ledger = PaperTradingEngine(db, symbol="BTC-USD", fee_bps="10", slippage_bps="5")
    result = poll_once(
        feed(ticker_data()),
        ledger,
        now=NOW,
    )
    assert result["last_trade_usd"] == "84000"
    assert result["events"] == []
    assert Decimal(result["paper_cash_usd"]) == 10000
    assert Decimal(result["paper_base_btc"]) == 0


@pytest.mark.unit
def test_bad_feed_never_changes_paper_book(tmp_path):
    ledger = PaperTradingEngine(tmp_path / "x.sqlite", symbol="BTC-USD")
    before = ledger.status("84000")
    with pytest.raises(ValueError, match="stale"):
        poll_once(
            feed(ticker_data(time=NOW - timedelta(minutes=4))),
            ledger,
            now=NOW,
        )
    after = ledger.status("84000")
    assert before == after


@pytest.mark.unit
def test_remote_http_error_fails_without_fallback_to_fake_quote(tmp_path):
    class HTTPErrorSession(FakeSession):
        def get(self, *args, **kwargs):
            def fail():
                raise RuntimeError("HTTP 429 rate limited")

            return SimpleNamespace(raise_for_status=fail, json=lambda: ticker_data())

    ledger = PaperTradingEngine(tmp_path / "x.sqlite", symbol="BTC-USD")
    with pytest.raises(RuntimeError, match="429"):
        poll_once(CoinbaseBTCUSDFeed(session=HTTPErrorSession(None)), ledger, now=NOW)


@pytest.mark.unit
def test_candle_future_alignment_and_nan_volume_rejected():
    t = int(datetime(2026, 10, 9, 15, 59, tzinfo=UTC).timestamp())
    with pytest.raises(ValueError, match="finite"):
        feed([[t, "100", "110", "105", "107", "NaN"]]).candles("1m", now=NOW)
    with pytest.raises(ValueError, match="aligned"):
        feed([[t + 5, "100", "110", "105", "107", "1"]]).candles("1m", now=NOW)
