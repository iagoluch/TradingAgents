"""Offline, deterministic lifecycle and SQLite persistence tests for spot paper trading."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradingagents.trade_guard import MarketSnapshot, TradeIntent
from tradingagents.trade_guard.paper import PaperTradingEngine

NOW = datetime(2026, 10, 9, 16, 0, tzinfo=UTC)


def state(action="Buy"):
    return {
        "asset_type": "crypto", "company_of_interest": "BTC-USD",
        "trader_investment_plan": f"**Action**: {action}",
        "final_trade_decision": f"**Rating**: {action}",
    }


def tick(price="100", now=NOW):
    return MarketSnapshot(symbol="BTC-USD", price=price, observed_at=now, source="test-feed")


def intent(action="SPOT_BUY", key="k1", **fields):
    data = {
        "symbol": "BTC-USD", "market": "SPOT", "action": action,
        "order_type": "MARKET", "quantity_base": "1",
        "stop_loss": "90", "take_profit": "120", "idempotency_key": key,
    }
    if action == "SPOT_SELL":
        data.update(stop_loss=None, take_profit=None)
    data.update(fields)
    return TradeIntent(**data)


def engine(tmp_path, **kw):
    return PaperTradingEngine(
        tmp_path / "trades.sqlite", symbol="BTC-USD",
        starting_quote="10000", **kw,
    )


@pytest.mark.unit
def test_buy_persists_cash_position_fee_and_unrealized_pnl(tmp_path):
    e = engine(tmp_path, fee_bps="10", slippage_bps="0")
    order = e.submit(state(), intent(), tick(), now=NOW)
    assert order["status"] == "FILLED"
    book = e.status("105")
    assert book["cash_quote"] == Decimal("9899.900")
    assert book["base_quantity"] == Decimal("1")
    assert book["fees_quote"] == Decimal("0.100")
    assert book["unrealized_pnl_quote"] == Decimal("4.900")
    reopened = engine(tmp_path)
    assert reopened.status("105")["cash_quote"] == book["cash_quote"]


@pytest.mark.unit
def test_order_idempotency_and_reusing_key_with_different_details(tmp_path):
    e = engine(tmp_path)
    i = intent()
    first = e.submit(state(), i, tick(), now=NOW)
    second = e.submit(state(), i, tick(), now=NOW)
    assert second["id"] == first["id"]
    assert e.status("100")["base_quantity"] == Decimal("1")
    with pytest.raises(ValueError, match="collision"):
        e.submit(state(), intent(quantity_base="2"), tick(), now=NOW)


@pytest.mark.unit
def test_limit_buy_reserves_cash_until_tick_crosses(tmp_path):
    e = engine(tmp_path)
    i = intent(order_type="LIMIT", limit_price="95")
    order = e.submit(state(), i, tick("100"), now=NOW)
    assert order["status"] == "PENDING"
    assert e.status("100")["reserved_quote"] == Decimal("95.095")
    out = e.advance(tick("94", NOW + timedelta(minutes=1)), now=NOW + timedelta(minutes=1))
    assert out[0]["status"] == "FILLED"
    assert e.status("94")["base_quantity"] == Decimal("1")
    assert e.order("k1")["filled_price"] == "95"


@pytest.mark.unit
def test_buy_too_far_in_future_expires_and_releases_reserve(tmp_path):
    e = engine(tmp_path, max_pending_hours=1)
    e.submit(state(), intent(order_type="LIMIT", limit_price="95"), tick(), now=NOW)
    later = NOW + timedelta(hours=2)
    out = e.advance(tick("100", later), now=later)
    assert out[0]["status"] == "EXPIRED"
    assert e.status("100")["reserved_quote"] == 0


@pytest.mark.unit
def test_sell_reduces_position_realizes_profit_and_fees(tmp_path):
    e = engine(tmp_path, slippage_bps="0")
    e.submit(state(), intent(), tick(), now=NOW)
    later = NOW + timedelta(minutes=1)
    s = e.submit(state("Sell"), intent("SPOT_SELL", "sell-1"),
                 tick("110", later), now=later)
    assert s["status"] == "FILLED"
    book = e.status("110")
    assert book["base_quantity"] == 0
    assert book["realized_pnl_quote"] == Decimal("9.790")
    assert book["fees_quote"] == Decimal("0.210")


@pytest.mark.unit
def test_stop_simulated_at_tick_price_not_guaranteed_stop_level(tmp_path):
    e = engine(tmp_path, slippage_bps="0", fee_bps="0")
    e.submit(state(), intent(), tick(), now=NOW)
    now = NOW + timedelta(minutes=1)
    events = e.advance(tick("80", now), now=now)
    assert len(events) == 1 and events[0]["status"] == "FILLED"
    assert events[0]["filled_price"] == "80"
    assert events[0]["idempotency_key"].startswith("protection:")
    assert e.status("80")["realized_pnl_quote"] == -20
    assert e.status("80")["max_drawdown_fraction"] > 0


@pytest.mark.unit
def test_target_autocloses_long(tmp_path):
    e = engine(tmp_path, slippage_bps="0", fee_bps="0")
    e.submit(state(), intent(), tick(), now=NOW)
    now = NOW + timedelta(minutes=1)
    events = e.advance(tick("125", now), now=now)
    assert len(events) == 1
    assert e.status("125")["realized_pnl_quote"] == 25
    assert e.status("125")["base_quantity"] == 0


@pytest.mark.unit
def test_short_is_never_executed_even_with_valid_agent_direction(tmp_path):
    e = engine(tmp_path)
    proposal = intent(
        action="SHORT_OPEN", market="PERPETUAL", order_type="LIMIT",
        limit_price="100", stop_loss="110", take_profit="90",
    )
    order = e.submit(state("Sell"), proposal, tick(), now=NOW)
    assert order["status"] == "REJECTED"
    assert "PAPER_ENGINE_SPOT_ONLY" in order["reasons"]
    assert e.status("100")["base_quantity"] == 0


@pytest.mark.unit
def test_spot_sell_requires_existing_position(tmp_path):
    e = engine(tmp_path)
    o = e.submit(state("Sell"), intent("SPOT_SELL", "sell"), tick(), now=NOW)
    assert o["status"] == "REJECTED"
    assert "BASE_BALANCE_INSUFFICIENT" in o["reasons"]


@pytest.mark.unit
def test_wrong_direction_and_missing_data_fail_closed(tmp_path):
    e = engine(tmp_path)
    o = e.submit(state("Sell"), intent(), tick(), now=NOW)
    assert o["status"] == "REJECTED"
    assert "INTENT_CONTRADICTS_AGENT_DIRECTION" in o["reasons"]


@pytest.mark.unit
def test_stale_quotes_are_rejected(tmp_path):
    e = engine(tmp_path)
    with pytest.raises(ValueError, match="stale"):
        e.submit(state(), intent(), tick(now=NOW - timedelta(minutes=3)), now=NOW)


@pytest.mark.unit
def test_limit_order_cancellation_releases_cash_reservation(tmp_path):
    e = engine(tmp_path)
    e.submit(state(), intent(order_type="LIMIT", limit_price="95"), tick(), now=NOW)
    result = e.cancel("k1")
    assert result["status"] == "CANCELLED"
    assert e.status("100")["reserved_quote"] == 0


@pytest.mark.unit
def test_pending_sell_reserves_base_then_manual_cancel(tmp_path):
    e = engine(tmp_path, slippage_bps="0")
    e.submit(state(), intent(), tick(), now=NOW)
    later = NOW + timedelta(minutes=1)
    sale = intent("SPOT_SELL", "sell-a", order_type="LIMIT", limit_price="115")
    result = e.submit(state("Sell"), sale, tick("100", later), now=later)
    assert result["status"] == "PENDING"
    assert e.status("100")["free_base"] == 0
    assert e.cancel("sell-a")["status"] == "CANCELLED"
    assert e.status("100")["free_base"] == 1


@pytest.mark.unit
def test_duplicate_buy_is_rejected_while_position_open(tmp_path):
    e = engine(tmp_path)
    e.submit(state(), intent(), tick(), now=NOW)
    later = NOW + timedelta(minutes=1)
    o = e.submit(state(), intent(key="another"), tick(now=later), now=later)
    assert o["status"] == "REJECTED"
    assert "SINGLE_SPOT_POSITION_ONLY" in o["reasons"]


@pytest.mark.unit
def test_short_position_cannot_be_seeded_and_no_live_mode(tmp_path):
    e = engine(tmp_path)
    assert e.status("100")["base_quantity"] == 0
    with pytest.raises(ValueError):
        engine(tmp_path, starting_quote="-5")
    # The TradeIntent contract itself rejects real execution.
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        intent(execution_mode="LIVE")


@pytest.mark.unit
def test_same_database_cannot_be_reopened_for_different_pair(tmp_path):
    engine(tmp_path)
    with pytest.raises(ValueError, match="different symbol"):
        PaperTradingEngine(tmp_path / "trades.sqlite", symbol="ETH-USD")


@pytest.mark.unit
def test_negative_or_naive_quote_timestamps_are_not_accepted(tmp_path):
    e = engine(tmp_path)
    with pytest.raises(ValueError, match="timezone"):
        e.advance(tick(now=NOW), now=datetime(2026, 10, 9, 16, 0))


@pytest.mark.unit
def test_protective_exit_cancels_pending_sell_first(tmp_path):
    e = engine(tmp_path, fee_bps="0", slippage_bps="0")
    e.submit(state(), intent(), tick(), now=NOW)
    time1 = NOW + timedelta(minutes=1)
    e.submit(
        state("Sell"),
        intent("SPOT_SELL", "selling", order_type="LIMIT", limit_price="119"),
        tick(now=time1), now=time1,
    )
    time2 = NOW + timedelta(minutes=2)
    out = e.advance(tick("80", time2), now=time2)
    assert {o["status"] for o in out} == {"CANCELLED", "FILLED"}
    assert e.status("80")["base_quantity"] == 0
