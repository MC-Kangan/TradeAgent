"""Optional bounded remote adapters with canonical opaque source references."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import cast
from urllib.parse import quote

import httpx

from trade_research.domain import InstrumentId
from trade_research.providers.contracts import (
    MAX_HTTP_BYTES,
    MAX_PRICE_POINTS,
    OptionalProviderDependencyError,
    PricePoint,
    ProviderConfigurationError,
    ProviderContractError,
)

HttpGet = Callable[[str, Mapping[str, str]], str]

_RETRY_LIMIT = 3
_RETRY_BACKOFF_BASE = 0.5
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

_YAHOO_SUFFIXES = {
    "US": "",
    "NASDAQ": "",
    "NYSE": "",
    "AMEX": "",
    "OTC": "",
    "ETF": "",
    "UK": ".L",
    "LSE": ".L",
    "AIM": ".L",
    "EU": ".PA",
    "EURONEXT": ".PA",
    "XETRA": ".DE",
    "BME": ".MC",
    "BORSA_ITALIANA": ".MI",
    "SIX": ".SW",
}


def resolve_provider_symbol(provider: str, instrument: InstrumentId) -> str:
    """Map a validated market to one provider's bounded symbol convention."""

    normalized_provider = provider.strip().lower()
    if normalized_provider == "ccxt":
        if instrument.market != "CRYPTO":
            raise ProviderConfigurationError("CCXT accepts only CRYPTO instruments")
        return instrument.symbol
    suffixes = {"yahoo": _YAHOO_SUFFIXES}.get(normalized_provider)
    if suffixes is None:
        raise ProviderConfigurationError(f"unknown market-symbol provider '{provider}'")
    try:
        suffix = suffixes[instrument.market]
    except KeyError as error:
        raise ProviderConfigurationError(
            f"{normalized_provider} does not support market '{instrument.market}'"
        ) from error
    symbol = f"{instrument.symbol}{suffix}"
    return symbol if normalized_provider == "yahoo" else symbol.lower()


def _http_get(url: str, headers: Mapping[str, str]) -> str:
    """Fetch a URL with retry and exponential backoff for transient failures.

    Retries on timeouts, HTTP 429 (rate-limit), and HTTP 5xx errors up to
    _RETRY_LIMIT attempts with exponential backoff starting at _RETRY_BACKOFF_BASE
    seconds. Raises ProviderConfigurationError after all retries are exhausted.
    """
    merged = {"User-Agent": "trade-research/0.1.0", **dict(headers)}
    last_error: Exception | None = None
    for attempt in range(_RETRY_LIMIT):
        try:
            response = httpx.get(
                url, headers=merged, timeout=10.0, follow_redirects=True
            )
            if response.status_code in _RETRYABLE_STATUSES:
                raise ProviderConfigurationError(
                    f"remote provider returned HTTP {response.status_code}"
                )
            response.raise_for_status()
            if len(response.content) > MAX_HTTP_BYTES:
                raise ProviderContractError("remote provider exceeded the byte limit")
            try:
                return response.content.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ProviderContractError(
                    "remote provider returned invalid UTF-8"
                ) from error
        except (httpx.TimeoutException, httpx.ConnectError) as error:
            last_error = error
        except ProviderConfigurationError:
            raise
        if attempt < _RETRY_LIMIT - 1:
            time.sleep(_RETRY_BACKOFF_BASE * (2**attempt))
    raise ProviderConfigurationError(
        "remote provider request failed after retries"
    ) from last_error


def _bounded_payload(payload: str) -> str:
    if len(payload.encode("utf-8")) > MAX_HTTP_BYTES:
        raise ProviderContractError("remote provider exceeded the byte limit")
    return payload


class YahooPriceProvider:
    """Yahoo chart endpoint adapter with a fixed symbol-only request shape.

    Returns **adjusted close** prices from the Yahoo Finance v8 chart API.
    The ``close`` values are adjusted for splits and dividends.  Open, high,
    low, and volume are **unadjusted** as reported by the exchange.
    """

    def __init__(self, http_get: HttpGet = _http_get) -> None:
        self._http_get = http_get

    def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
        symbol = resolve_provider_symbol("yahoo", instrument)
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{quote(symbol, safe='')}?range=1y&interval=1d"
        )
        payload_text = _bounded_payload(self._http_get(url, {"Accept": "application/json"}))
        try:
            payload = json.loads(payload_text)
            result = payload["chart"]["result"][0]
            quote_data = result["indicators"]["quote"][0]
            timestamps = result["timestamp"]
            series = (
                quote_data["open"],
                quote_data["high"],
                quote_data["low"],
                quote_data["close"],
                quote_data["volume"],
            )
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            raise ProviderContractError("Yahoo returned a malformed response") from error
        if not isinstance(timestamps, list) or any(not isinstance(item, list) for item in series):
            raise ProviderContractError("Yahoo returned a malformed price series")
        if len(timestamps) > MAX_PRICE_POINTS:
            raise ProviderContractError("Yahoo exceeded the point limit")
        if any(len(item) != len(timestamps) for item in series):
            raise ProviderContractError("Yahoo returned mismatched price arrays")
        snapshot = _canonical_reference(payload)
        return tuple(
            PricePoint(
                instrument=instrument,
                observed_at=datetime.fromtimestamp(float(timestamp), tz=UTC),
                close=float(close),
                source="yahoo",
                provenance={
                    "provider_kind": "yahoo",
                    "vendor_field": "OHLCV",
                    "snapshot_ref": snapshot,
                    "reference": _canonical_reference(
                        [timestamp, open_value, high, low, close, volume]
                    ),
                },
                open=_optional_float(open_value),
                high=_optional_float(high),
                low=_optional_float(low),
                volume=_optional_float(volume),
            )
            for timestamp, open_value, high, low, close, volume in zip(
                timestamps, *series, strict=True
            )
            if close is not None
        )


class CcxtPriceProvider:
    """Optional crypto adapter, loaded only when CCXT is installed and configured."""

    def __init__(self, exchange_id: str) -> None:
        self._exchange_id = exchange_id

    def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
        symbol = resolve_provider_symbol("ccxt", instrument)
        try:
            import ccxt  # type: ignore[import-not-found]
        except ImportError as error:
            raise OptionalProviderDependencyError(
                "CCXT provider requires the optional 'ccxt' dependency. "
                "Install it with: pip install ccxt"
            ) from error
        try:
            exchange_type = getattr(ccxt, self._exchange_id)
        except AttributeError as error:
            raise ProviderConfigurationError(
                f"unknown CCXT exchange '{self._exchange_id}'"
            ) from error
        candles = exchange_type({"enableRateLimit": True}).fetch_ohlcv(
            symbol, timeframe="1d", limit=MAX_PRICE_POINTS + 1
        )
        if not isinstance(candles, list) or len(candles) > MAX_PRICE_POINTS:
            raise ProviderContractError("CCXT exceeded the point limit")
        snapshot = _canonical_reference(candles)
        try:
            return tuple(
                PricePoint(
                    instrument=instrument,
                    observed_at=datetime.fromtimestamp(float(candle[0]) / 1000, tz=UTC),
                    close=float(candle[4]),
                    source="ccxt",
                    provenance={
                        "provider_kind": "ccxt",
                        "vendor_field": "OHLCV",
                        "snapshot_ref": snapshot,
                        "reference": _canonical_reference(candle),
                    },
                    open=float(candle[1]),
                    high=float(candle[2]),
                    low=float(candle[3]),
                    volume=float(candle[5]),
                )
                for candle in candles
            )
        except (IndexError, TypeError, ValueError) as error:
            raise ProviderContractError("CCXT returned malformed candles") from error


def _canonical_reference(value: object) -> str:
    if isinstance(value, str):
        encoded = value.encode()
    else:
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _optional_float(value: object) -> float | None:
    return None if value is None else float(cast(float | int | str, value))


def _row_float(row: Mapping[str, str], field: str) -> float | None:
    value = row.get(field)
    if value is None or value in ("", "N/D"):
        return None
    return float(value)
