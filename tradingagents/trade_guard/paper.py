"""Persistent, deterministic SPOT-only paper execution.

No broker client, network access, margin, shorts or live order capability.
Snapshots must come from an independently verified caller-supplied feed.
All updates happen inside SQLite write transactions; no background process is
started. The caller must supply successive snapshots to advance pending orders
and trigger protective exits.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .risk import MarketSnapshot, PortfolioSnapshot, RiskLimits, TradeIntent
from .review import validate_paper_decision

ZERO = Decimal("0")


def _dec(value) -> Decimal:
    return Decimal(str(value))


class PaperTradingEngine:
    """One BTC-USD-style SPOT book per SQLite file, in quote currency units."""

    def __init__(
        self,
        path: str | Path,
        *,
        symbol: str,
        starting_quote: Decimal | str = "10000",
        fee_bps: Decimal | str = "10",
        slippage_bps: Decimal | str = "5",
        max_pending_hours: int = 24,
    ):
        self.path = str(path)
        self.symbol = symbol
        self.fee_rate = _dec(fee_bps) / 10000
        self.slippage_rate = _dec(slippage_bps) / 10000
        self.max_pending_hours = max_pending_hours
        initial = _dec(starting_quote)
        if initial <= 0 or not 0 <= self.fee_rate <= Decimal("0.05"):
            raise ValueError("starting_quote and fee_bps must be within supported bounds")
        if not 0 <= self.slippage_rate <= Decimal("0.05") or max_pending_hours <= 0:
            raise ValueError("invalid slippage or pending-order lifetime")

        with closing(self._connect()) as conn, conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS account (
                    id INTEGER PRIMARY KEY CHECK(id = 1), symbol TEXT NOT NULL,
                    cash TEXT NOT NULL, base TEXT NOT NULL, cost TEXT NOT NULL,
                    realized TEXT NOT NULL, fees TEXT NOT NULL,
                    stop TEXT, target TEXT, protection_id TEXT,
                    high_water TEXT NOT NULL, worst_drawdown TEXT NOT NULL,
                    last_mark_price TEXT
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS paper_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL, action TEXT NOT NULL,
                    order_type TEXT NOT NULL, quantity TEXT NOT NULL,
                    limit_price TEXT, stop_loss TEXT, take_profit TEXT,
                    status TEXT NOT NULL, created_at TEXT NOT NULL,
                    expires_at TEXT, filled_at TEXT, filled_price TEXT,
                    fee TEXT, reasons TEXT NOT NULL DEFAULT '[]'
                )"""
            )
            conn.execute(
                """INSERT OR IGNORE INTO account
                   (id, symbol, cash, base, cost, realized, fees, stop, target,
                    protection_id, high_water, worst_drawdown, last_mark_price)
                   VALUES (1, ?, ?, '0', '0', '0', '0', NULL, NULL, NULL, ?, '0', NULL)""",
                (symbol, str(initial), str(initial)),
            )
            current = conn.execute("SELECT symbol FROM account WHERE id=1").fetchone()
            if current["symbol"] != symbol:
                raise ValueError("database already belongs to a different symbol")

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value

    def _check_quote(self, quote: MarketSnapshot, now: datetime, max_age: int = 90):
        self._utc(now)
        if quote.symbol != self.symbol:
            raise ValueError("market symbol does not match paper book")
        age = now - quote.observed_at
        if age > timedelta(seconds=max_age) or age < -timedelta(seconds=5):
            raise ValueError("market quote is stale or from the future")

    @staticmethod
    def _row_order(row):
        d = dict(row)
        d["reasons"] = json.loads(d["reasons"])
        return d

    def order(self, key: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM paper_orders WHERE idempotency_key=?", (key,)
            ).fetchone()
            return self._row_order(row) if row else None

    def _account(self, conn):
        return conn.execute("SELECT * FROM account WHERE id=1").fetchone()

    def _reserved(self, conn) -> tuple[Decimal, Decimal]:
        quote, base = ZERO, ZERO
        for row in conn.execute(
            "SELECT action, quantity, limit_price FROM paper_orders WHERE status='PENDING'"
        ):
            qty = _dec(row["quantity"])
            if row["action"] == "SPOT_BUY":
                quote += qty * _dec(row["limit_price"]) * (1 + self.fee_rate)
            else:
                base += qty
        return quote, base

    def _balances(self, conn, quote_price: Decimal) -> PortfolioSnapshot:
        a = self._account(conn)
        held_quote, held_base = self._reserved(conn)
        return PortfolioSnapshot(
            equity_quote=_dec(a["cash"]) + _dec(a["base"]) * quote_price,
            free_quote=max(ZERO, _dec(a["cash"]) - held_quote),
            free_base=max(ZERO, _dec(a["base"]) - held_base),
        )

    def status(self, mark_price: Decimal | str) -> dict:
        """Read-only accounting metrics; drawdown updates only on supplied ticks."""
        price = _dec(mark_price)
        if price <= 0:
            raise ValueError("mark price must be positive")
        with closing(self._connect()) as conn:
            a = self._account(conn)
            held_quote, held_base = self._reserved(conn)
            cash, base, basis = _dec(a["cash"]), _dec(a["base"]), _dec(a["cost"])
            return {
                "symbol": self.symbol,
                "cash_quote": cash,
                "base_quantity": base,
                "free_quote": cash - held_quote,
                "free_base": base - held_base,
                "reserved_quote": held_quote,
                "reserved_base": held_base,
                "equity_quote": cash + base * price,
                "unrealized_pnl_quote": base * (price - basis),
                "realized_pnl_quote": _dec(a["realized"]),
                "fees_quote": _dec(a["fees"]),
                "high_water_quote": _dec(a["high_water"]),
                "max_drawdown_fraction": _dec(a["worst_drawdown"]),
                "stop_loss": _dec(a["stop"]) if a["stop"] else None,
                "take_profit": _dec(a["target"]) if a["target"] else None,
            }

    def _mark(self, conn, price: Decimal):
        account = self._account(conn)
        equity = _dec(account["cash"]) + _dec(account["base"]) * price
        high = max(equity, _dec(account["high_water"]))
        dd = (high - equity) / high if high > 0 else ZERO
        worst = max(dd, _dec(account["worst_drawdown"]))
        conn.execute(
            "UPDATE account SET high_water=?, worst_drawdown=?, last_mark_price=? WHERE id=1",
            (str(high), str(worst), str(price)),
        )

    def _insert(self, conn, intent: TradeIntent, fingerprint: str, now: datetime,
                status: str, reasons=()) -> int:
        expiry = now + timedelta(hours=self.max_pending_hours)
        cursor = conn.execute(
            """INSERT INTO paper_orders
               (idempotency_key, fingerprint, action, order_type, quantity, limit_price,
                stop_loss, take_profit, status, created_at, expires_at, reasons)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                intent.idempotency_key, fingerprint, intent.action, intent.order_type or "",
                str(intent.quantity_base or ZERO),
                str(intent.limit_price) if intent.limit_price is not None else None,
                str(intent.stop_loss) if intent.stop_loss is not None else None,
                str(intent.take_profit) if intent.take_profit is not None else None,
                status, now.isoformat(), expiry.isoformat(), json.dumps(list(reasons)),
            ),
        )
        return cursor.lastrowid

    @staticmethod
    def _crossed(order, price: Decimal) -> bool:
        if order["order_type"] == "MARKET":
            return True
        limit = _dec(order["limit_price"])
        if order["action"] == "SPOT_BUY":
            return price <= limit
        return price >= limit

    def _fill(self, conn, order, quote_price: Decimal, now: datetime):
        action = order["action"]
        qty = _dec(order["quantity"])
        if order["order_type"] == "LIMIT":
            fill_price = _dec(order["limit_price"])  # conservative limit-price fill
        elif action == "SPOT_BUY":
            fill_price = quote_price * (1 + self.slippage_rate)
        else:
            fill_price = quote_price * (1 - self.slippage_rate)
        gross = qty * fill_price
        fee = gross * self.fee_rate
        a = self._account(conn)
        cash, base, cost = _dec(a["cash"]), _dec(a["base"]), _dec(a["cost"])
        realized, fees = _dec(a["realized"]), _dec(a["fees"])
        stop, target, parent = a["stop"], a["target"], a["protection_id"]
        if action == "SPOT_BUY":
            if base != 0 or cash < gross + fee:
                raise ValueError("paper fill has conflicting position or insufficient cash")
            stop, target = order["stop_loss"], order["take_profit"]
            if not stop or not target or not _dec(stop) < fill_price < _dec(target):
                raise ValueError("paper fill invalidates protective price levels")
            base = qty
            cash -= gross + fee
            cost = (gross + fee) / qty
            parent = order["idempotency_key"]
        else:
            if base < qty:
                raise ValueError("paper fill exceeds held position")
            cash += gross - fee
            realized += gross - fee - qty * cost
            base -= qty
            if base == 0:
                cost, stop, target, parent = ZERO, None, None, None
        fees += fee
        conn.execute(
            """UPDATE account SET cash=?, base=?, cost=?, realized=?, fees=?,
               stop=?, target=?, protection_id=? WHERE id=1""",
            (str(cash), str(base), str(cost), str(realized), str(fees),
             stop, target, parent),
        )
        conn.execute(
            """UPDATE paper_orders SET status='FILLED', filled_at=?, filled_price=?,
               fee=? WHERE id=?""",
            (now.isoformat(), str(fill_price), str(fee), order["id"]),
        )

    def submit(self, state: dict, intent: TradeIntent, quote: MarketSnapshot,
               *, now: datetime, limits: RiskLimits | None = None) -> dict:
        """Persist a proposed order exactly once. Never executes outside SQLite."""
        self._check_quote(quote, now, (limits or RiskLimits()).max_quote_age_seconds)
        if intent.idempotency_key is None or not intent.idempotency_key.strip():
            raise ValueError("idempotency key required for paper ledger")
        payload = intent.model_dump(mode="json")
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM paper_orders WHERE idempotency_key=?",
                (intent.idempotency_key,),
            ).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ValueError("idempotency key collision with different order")
                return self._row_order(existing)

            balances = self._balances(conn, quote.price)
            verdict = validate_paper_decision(
                state, intent, quote, balances, now=now, limits=limits,
            )
            reasons = list(verdict.reasons)
            if intent.action not in ("SPOT_BUY", "SPOT_SELL") or intent.market != "SPOT":
                reasons.append("PAPER_ENGINE_SPOT_ONLY")
            if intent.action == "SPOT_BUY":
                a = self._account(conn)
                pending_buy = conn.execute(
                    "SELECT COUNT(*) FROM paper_orders WHERE status='PENDING' AND action='SPOT_BUY'"
                ).fetchone()[0]
                if _dec(a["base"]) > 0 or pending_buy:
                    reasons.append("SINGLE_SPOT_POSITION_ONLY")
                if intent.quantity_base and intent.order_type:
                    entry = intent.limit_price if intent.order_type == "LIMIT" else quote.price
                    if entry is not None:
                        total = entry * intent.quantity_base * (1 + self.fee_rate)
                        if total > balances.free_quote:
                            reasons.append("BUY_FEES_EXCEED_FREE_BALANCE")
            status = "REJECTED" if reasons else "PENDING"
            row_id = self._insert(conn, intent, fingerprint, now, status, reasons)
            if not reasons:
                row = conn.execute("SELECT * FROM paper_orders WHERE id=?", (row_id,)).fetchone()
                if self._crossed(row, quote.price):
                    try:
                        self._fill(conn, row, quote.price, now)
                    except ValueError as exc:
                        conn.execute(
                            "UPDATE paper_orders SET status='REJECTED', reasons=? WHERE id=?",
                            (json.dumps([str(exc)]), row_id),
                        )
            self._mark(conn, quote.price)
            row = conn.execute("SELECT * FROM paper_orders WHERE id=?", (row_id,)).fetchone()
            return self._row_order(row)

    def advance(self, quote: MarketSnapshot, *, now: datetime) -> list[dict]:
        """Process one verified tick: expirations, protective exit, then limits."""
        self._check_quote(quote, now)
        changes = []
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            for order in conn.execute(
                "SELECT * FROM paper_orders WHERE status='PENDING' ORDER BY id"
            ).fetchall():
                if now >= datetime.fromisoformat(order["expires_at"]):
                    conn.execute(
                        "UPDATE paper_orders SET status='EXPIRED' WHERE id=?", (order["id"],)
                    )
                    changes.append(order["id"])

            a = self._account(conn)
            if _dec(a["base"]) > 0 and (
                (a["stop"] and quote.price <= _dec(a["stop"]))
                or (a["target"] and quote.price >= _dec(a["target"]))
            ):
                reason = "STOP" if quote.price <= _dec(a["stop"]) else "TARGET"
                # A protective exit supersedes any unfilled discretionary spot sell.
                for order in conn.execute(
                    "SELECT id FROM paper_orders WHERE status='PENDING' AND action='SPOT_SELL'"
                ).fetchall():
                    conn.execute(
                        "UPDATE paper_orders SET status='CANCELLED' WHERE id=?",
                        (order["id"],),
                    )
                    changes.append(order["id"])
                key = f"protection:{a['protection_id']}:{reason}"
                # The market-order exit is generated locally, not by an LLM.
                auto = TradeIntent(
                    symbol=self.symbol, market="SPOT", action="SPOT_SELL",
                    order_type="MARKET", quantity_base=_dec(a["base"]),
                    idempotency_key=key,
                )
                row_id = self._insert(conn, auto, "SYSTEM_PROTECTIVE_EXIT", now, "PENDING")
                row = conn.execute("SELECT * FROM paper_orders WHERE id=?", (row_id,)).fetchone()
                self._fill(conn, row, quote.price, now)
                changes.append(row_id)

            for order in conn.execute(
                "SELECT * FROM paper_orders WHERE status='PENDING' ORDER BY id"
            ).fetchall():
                if self._crossed(order, quote.price):
                    try:
                        self._fill(conn, order, quote.price, now)
                    except ValueError as exc:
                        conn.execute(
                            "UPDATE paper_orders SET status='REJECTED', reasons=? WHERE id=?",
                            (json.dumps([str(exc)]), order["id"]),
                        )
                    changes.append(order["id"])
            self._mark(conn, quote.price)
            for row_id in changes:
                row = conn.execute("SELECT * FROM paper_orders WHERE id=?", (row_id,)).fetchone()
                changes[changes.index(row_id)] = self._row_order(row)
        return changes

    def cancel(self, key: str) -> dict:
        """Cancel an unfilled simulated order, releasing its reservation."""
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM paper_orders WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row is None:
                raise KeyError(key)
            if row["status"] == "PENDING":
                conn.execute(
                    "UPDATE paper_orders SET status='CANCELLED' WHERE id=?", (row["id"],)
                )
            row = conn.execute("SELECT * FROM paper_orders WHERE id=?", (row["id"],)).fetchone()
            return self._row_order(row)
