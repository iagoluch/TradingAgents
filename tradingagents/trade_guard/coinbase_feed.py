"""Read-only Coinbase Exchange BTC-USD market data for paper simulations.

No credentials or authenticated endpoints. A last trade is NOT a guaranteed
execution price. Fail closed for stale, malformed, or mismatched market data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import ClassVar

import requests
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .risk import MarketSnapshot

_GRANULARITIES = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}


def _decimal(value, name: str, *, zero_allowed: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} is missing or invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{name} is not a valid decimal") from exc
    if not result.is_finite() or (result < 0 if zero_allowed else result <= 0):
        raise ValueError(f"{name} must be finite and nonnegative/positive")
    return result


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(UTC)


class Candle(BaseModel):
    """One completed OHLCV bucket, with prices in USD and volume in BTC."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    observed_at: datetime
    granularity_seconds: int = Field(gt=0)
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume_btc: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def _ohlc_consistency(self):
        _aware(self.observed_at)
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("OHLC prices outside their high/low bounds")
        if self.high < self.low:
            raise ValueError("candle high below low")
        return self


class CoinbaseBTCUSDFeed:
    """Public Coinbase Exchange endpoints fixed to BTC-USD (never BTC-USDT)."""

    PRODUCT: ClassVar[str] = "BTC-USD"
    BASE_URL: ClassVar[str] = "https://api.exchange.coinbase.com"
    SOURCE: ClassVar[str] = "coinbase-exchange/BTC-USD"

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = 10.0,
        max_age_seconds: int = 90,
    ):
        if not 0 < timeout <= 60 or not 0 < max_age_seconds <= 3600:
            raise ValueError("invalid network timeout or maximum quote age")
        self._session = session or requests.Session()
        self.timeout = timeout
        self.max_age_seconds = max_age_seconds

    def _request(self, suffix: str, *, params: dict | None = None):
        response = self._session.get(
            f"{self.BASE_URL}/products/{self.PRODUCT}/{suffix}",
            params=params,
            headers={"Accept": "application/json", "User-Agent": "TradingAgents-Paper/1"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def snapshot(self, *, now: datetime | None = None) -> MarketSnapshot:
        """Return a fresh BTC-USD last trade, timestamped by the venue."""
        clock = _aware(now or datetime.now(UTC))
        data = self._request("ticker")
        if not isinstance(data, dict):
            raise ValueError("ticker payload must be an object")
        price = _decimal(data.get("price"), "last trade price")
        stamp = data.get("time")
        if not isinstance(stamp, str):
            raise ValueError("ticker has no exchange timestamp")
        try:
            exchange_time = _aware(datetime.fromisoformat(stamp.replace("Z", "+00:00")))
        except ValueError as exc:
            raise ValueError("invalid exchange timestamp") from exc

        age = clock - exchange_time
        if age > timedelta(seconds=self.max_age_seconds) or age < -timedelta(seconds=5):
            raise ValueError("stale or future Coinbase BTC-USD ticker")
        for name in ("bid", "ask"):
            if data.get(name) is not None:
                _decimal(data[name], name)
        if data.get("bid") is not None and data.get("ask") is not None:
            if _decimal(data["bid"], "bid") > _decimal(data["ask"], "ask"):
                raise ValueError("bid exceeds ask")
        return MarketSnapshot(
            symbol=self.PRODUCT,
            price=price,
            observed_at=exchange_time,
            source=self.SOURCE,
        )

    def candles(
        self,
        granularity: str,
        *,
        count: int = 60,
        end: datetime | None = None,
        now: datetime | None = None,
    ) -> tuple[Candle, ...]:
        """Fetch up to 300 *completed* venue candles in ascending UTC order.

        Coinbase can return extra buckets outside the requested interval.
        They are filtered out, never treated as complete candles.
        """
        if granularity not in _GRANULARITIES:
            raise ValueError("granularity must be 1m, 5m, 15m, 1h or 1d")
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 300:
            raise ValueError("candle count must be between 1 and 300")
        clock = _aware(now or datetime.now(UTC))
        seconds = _GRANULARITIES[granularity]
        boundary = datetime.fromtimestamp(
            int(clock.timestamp()) // seconds * seconds, tz=UTC
        )
        if end is None:
            end_time = boundary
        else:
            end_time = _aware(end)
            if end_time > boundary or int(end_time.timestamp()) % seconds:
                raise ValueError("candle end must be aligned and not in the future")
        start_time = end_time - timedelta(seconds=count * seconds)
        raw = self._request(
            "candles",
            params={
                "granularity": seconds,
                "start": start_time.isoformat().replace("+00:00", "Z"),
                "end": end_time.isoformat().replace("+00:00", "Z"),
            },
        )
        if not isinstance(raw, list):
            raise ValueError("candle payload must be an array")
        parsed = {}
        for bucket in raw:
            if not isinstance(bucket, list) or len(bucket) != 6:
                raise ValueError("unexpected Coinbase candle format")
            ts = bucket[0]
            if isinstance(ts, bool) or not isinstance(ts, int):
                raise ValueError("candle timestamp must be integer Unix seconds")
            time = datetime.fromtimestamp(ts, tz=UTC)
            if time < start_time or time >= end_time:
                continue
            if ts % seconds:
                raise ValueError("unaligned candle timestamp")
            if ts in parsed:
                raise ValueError("duplicate candle bucket")
            low, high, opened, closed, volume = (
                _decimal(bucket[1], "low"),
                _decimal(bucket[2], "high"),
                _decimal(bucket[3], "open"),
                _decimal(bucket[4], "close"),
                _decimal(bucket[5], "volume", zero_allowed=True),
            )
            parsed[ts] = Candle(
                observed_at=time,
                granularity_seconds=seconds,
                open=opened,
                high=high,
                low=low,
                close=closed,
                volume_btc=volume,
            )
        if not parsed:
            raise ValueError("no completed candles returned for BTC-USD")
        return tuple(parsed[k] for k in sorted(parsed))
