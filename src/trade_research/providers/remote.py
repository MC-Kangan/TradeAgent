"""Optional remote adapters; their bounded APIs avoid external query execution."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import cast
from urllib.parse import quote
from urllib.request import Request, urlopen

from trade_research.domain import Evidence, InstrumentId
from trade_research.providers.contracts import (
    OptionalProviderDependencyError,
    PricePoint,
    ProviderConfigurationError,
)

HttpGet = Callable[[str, Mapping[str, str]], str]


def _http_get(url: str, headers: Mapping[str, str]) -> str:
    request = Request(url, headers=dict(headers))
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 - URLs are fixed by adapters.
            return cast(bytes, response.read()).decode("utf-8")
    except OSError as error:
        raise ProviderConfigurationError(f"remote provider request failed: {error}") from error


class YahooPriceProvider:
    """Yahoo chart endpoint adapter with a fixed symbol-only request shape."""

    def __init__(self, http_get: HttpGet = _http_get) -> None:
        self._http_get = http_get

    def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(instrument.symbol)}?range=1y&interval=1d"
        payload = json.loads(self._http_get(url, {"Accept": "application/json"}))
        result = payload["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        return tuple(
            PricePoint(
                observed_at=datetime.fromtimestamp(timestamp, tz=UTC),
                close=float(close),
                source="yahoo",
                provenance={"symbol": instrument.symbol, "endpoint": "chart"},
            )
            for timestamp, close in zip(result["timestamp"], closes, strict=True)
            if close is not None
        )


class StooqPriceProvider:
    """Stooq daily CSV adapter with a fixed query shape."""

    def __init__(self, http_get: HttpGet = _http_get) -> None:
        self._http_get = http_get

    def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
        url = f"https://stooq.com/q/d/l/?s={quote(instrument.symbol.lower())}&i=d"
        rows = csv.DictReader(io.StringIO(self._http_get(url, {"Accept": "text/csv"})))
        return tuple(
            PricePoint(
                observed_at=datetime.fromisoformat(row["Date"]).replace(tzinfo=UTC),
                close=float(row["Close"]),
                source="stooq",
                provenance={"symbol": instrument.symbol, "interval": "daily"},
            )
            for row in rows
            if row.get("Close") not in (None, "", "N/D")
        )


class SecFilingsProvider:
    """SEC submissions adapter; callers must provide an identifying user agent."""

    def __init__(
        self,
        cik_by_symbol: Mapping[str, str],
        user_agent: str | None,
        http_get: HttpGet = _http_get,
    ) -> None:
        if not user_agent:
            raise ProviderConfigurationError(
                "SEC provider requires a configured identifying user agent"
            )
        self._cik_by_symbol = dict(cik_by_symbol)
        self._user_agent = user_agent
        self._http_get = http_get

    def filings(self, instrument: InstrumentId) -> tuple[Evidence, ...]:
        try:
            cik = self._cik_by_symbol[instrument.symbol]
        except KeyError as error:
            raise ProviderConfigurationError(
                f"no SEC CIK configured for {instrument.symbol}"
            ) from error
        payload = json.loads(
            self._http_get(
                f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json",
                {"Accept": "application/json", "User-Agent": self._user_agent},
            )
        )
        now = datetime.now(tz=UTC)
        return (
            Evidence(source="sec", content=json.dumps(payload, sort_keys=True), collected_at=now),
        )


class CcxtPriceProvider:
    """Optional crypto adapter, loaded only when CCXT is installed and configured."""

    def __init__(self, exchange_id: str) -> None:
        self._exchange_id = exchange_id

    def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
        try:
            import ccxt  # type: ignore[import-not-found]
        except ImportError as error:
            raise OptionalProviderDependencyError(
                "CCXT provider requires the optional 'ccxt' dependency"
            ) from error
        try:
            exchange_type = getattr(ccxt, self._exchange_id)
        except AttributeError as error:
            raise ProviderConfigurationError(
                f"unknown CCXT exchange '{self._exchange_id}'"
            ) from error
        candles = exchange_type({"enableRateLimit": True}).fetch_ohlcv(
            instrument.symbol, timeframe="1d"
        )
        return tuple(
            PricePoint(
                observed_at=datetime.fromtimestamp(candle[0] / 1000, tz=UTC),
                close=float(candle[4]),
                source=f"ccxt:{self._exchange_id}",
                provenance={"symbol": instrument.symbol, "timeframe": "1d"},
            )
            for candle in candles
        )
