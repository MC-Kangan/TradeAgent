"""Explicit FX closes used only to convert factor-study asset returns to USD."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from trade_research.domain.models import FxLevelPoint, FxLevelSeries
from trade_research.providers.contracts import ProviderConfigurationError, ProviderContractError
from trade_research.providers.remote import HttpGet, _http_get


class YahooFxProvider:
    def __init__(self, http_get: HttpGet = _http_get) -> None:
        self._get = http_get

    def fx_history(
        self, currency: str, start: date, end: date, quote_currency: str = "USD"
    ) -> FxLevelSeries:
        if quote_currency != "USD" or currency not in {"EUR", "GBP", "CHF"}:
            raise ProviderConfigurationError("Yahoo FX pair is not configured")
        symbol = currency + "USD=X"
        begin = int(datetime.combine(start, datetime.min.time(), tzinfo=UTC).timestamp())
        stop = int(
            datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=UTC).timestamp()
        )
        raw = self._get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{currency}USD%3DX"
            f"?period1={begin}&period2={stop}&interval=1d",
            {"Accept": "application/json"},
        )
        try:
            result = json.loads(raw)["chart"]["result"][0]
            if result["meta"]["symbol"] != symbol or result["meta"]["currency"] != "USD":
                raise ValueError("FX identity mismatch")
            timezone = ZoneInfo(result["meta"]["exchangeTimezoneName"])
            values = result["indicators"]["quote"][0]["close"]
            return FxLevelSeries(
                base_currency=currency,
                source="yahoo",
                retrieved_at=datetime.now(UTC),
                points=tuple(
                    FxLevelPoint(date=datetime.fromtimestamp(t, timezone).date(), value=v)
                    for t, v in zip(result["timestamp"], values, strict=True)
                    if v is not None
                ),
            )
        except (ValueError, KeyError, TypeError, IndexError, OverflowError) as error:
            raise ProviderContractError("Invalid Yahoo FX data") from error


class InlineFxProvider:
    def __init__(self, series: tuple[FxLevelSeries, ...]) -> None:
        self.series = {(s.base_currency, s.quote_currency): s for s in series}

    def fx_history(
        self, currency: str, start: date, end: date, quote_currency: str = "USD"
    ) -> FxLevelSeries:
        if (currency, quote_currency) not in self.series:
            raise ProviderConfigurationError("Required FX series not supplied")
        return self.series[(currency, quote_currency)]
